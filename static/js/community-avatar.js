/* GoodMarket shared profile-picture (avatar) module.

   Loaded by BOTH the community feed (`/community`) and the shared public
   profile (`/community/u/<username>`, `/chatroom/u/<username>`), because the
   picture is ONE row (`community_feed_avatars`) and the two pages must never
   disagree about it. Keeping the upload here also means the feed bundle and the
   profile bundle do not each carry a copy of the canvas/presign/PUT logic.

   Flow (identical to post media, own tighter image cap):
     pickFile(fileList)
       -> downscale + strip EXIF via a canvas (GIF passes through untouched)
       -> POST /community/api/avatar/presign   (signed PUT, images only)
       -> PUT straight to R2 with the SIGNED headers
       -> POST /community/api/avatar           (server re-verifies the object)

   Public API (window.GMCommunityAvatar):
     open() / close()          — show/hide the modal
     pickFile(fileList)        — start an upload from an <input type=file>
     remove()                  — delete the picture
     setUrl(url) / getUrl()    — the current picture URL ('' when none)
     onChange(fn)              — called with the new URL after a change
     setUsername(name)         — the initial-letter fallback for the preview
*/
(function () {
    'use strict';

    var BOOT = window.GM_COMMUNITY_BOOT || window.GM_PROFILE_BOOT || {};
    var WALLET = BOOT.wallet || '';
    var USERNAME = BOOT.username || '';
    var STORAGE_READY = !!BOOT.storageReady;
    var MAX_AVATAR_MB = BOOT.maxAvatarMb || 5;
    var LONG_EDGE = 512;         // a picture is rendered small — 512px is plenty
    var JPEG_QUALITY = 0.85;

    var _url = BOOT.avatarUrl || '';
    var _listeners = [];
    var _busy = false;

    function $(id) { return document.getElementById(id); }

    function esc(text) {
        return String(text === undefined || text === null ? '' : text);
    }

    function api(path, options) {
        options = options || {};
        var headers = { 'Accept': 'application/json' };
        if (options.body) headers['Content-Type'] = 'application/json';
        return fetch(path, {
            method: options.method || 'GET',
            headers: headers,
            credentials: 'same-origin',
            body: options.body,
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                return { status: response.status, ok: response.ok, data: data };
            });
        });
    }

    function toast(message, kind) {
        var node = $('cfToast') || $('cpToast');
        if (!node) return;
        node.textContent = esc(message);
        node.className = 'cf-toast' + (kind ? ' is-' + kind : '');
        node.hidden = false;
        setTimeout(function () { node.hidden = true; }, 3200);
    }

    function requiresLogin() {
        if (typeof window.gmRequireLogin === 'function') { window.gmRequireLogin(); return; }
        if (typeof window.requireLogin === 'function') { window.requireLogin(); return; }
        // Fall back to the page's connect button, if it rendered one.
        var connect = $('cfConnectBtn') || $('cpConnectBtn');
        if (connect && connect.click) connect.click();
    }

    function setStatus(message, isError) {
        var node = $('cfAvatarStatus');
        if (!node) return;
        node.textContent = esc(message);
        node.className = 'cf-avatar-status' + (isError ? ' is-error' : '');
    }

    // Render the picture (or the initial) into every node that mirrors it: the
    // composer ring, the profile hero and the modal preview.
    function renderInto(node, url, username) {
        if (!node) return;
        node.textContent = '';
        var src = esc(url).trim();
        if (src) {
            var img = document.createElement('img');
            img.className = 'cf-avatar-img';
            img.src = src;
            img.alt = '';
            img.loading = 'lazy';
            img.decoding = 'async';
            img.referrerPolicy = 'no-referrer';
            node.appendChild(img);
            return;
        }
        node.textContent = (esc(username) || 'A')[0].toUpperCase();
    }

    function paint() {
        renderInto($('cfComposerAvatar'), _url, USERNAME || 'You');
        renderInto($('cfAvatarPreview'), _url, USERNAME || 'You');
        renderInto($('cpProfileAvatar'), _url, USERNAME || 'You');
        var remove = $('cfAvatarRemove');
        if (remove) remove.hidden = !_url;
    }

    function setUrl(url, notify) {
        _url = url || '';
        paint();
        if (notify !== false) {
            _listeners.forEach(function (fn) {
                try { fn(_url); } catch (e) { /* a listener must not break the save */ }
            });
        }
    }

    function open() {
        if (!WALLET) { requiresLogin(); return; }
        var modal = $('cfAvatarModal');
        if (!modal) return;
        modal.hidden = false;
        document.body.classList.add('cf-modal-open');
        setStatus('');
    }

    function close() {
        var modal = $('cfAvatarModal');
        if (modal) modal.hidden = true;
        document.body.classList.remove('cf-modal-open');
    }

    // Downscale to LONG_EDGE and re-encode as JPEG. Re-encoding also STRIPS the
    // EXIF block, so a phone photo does not publish the user's GPS coordinates.
    // A GIF is passed through untouched (the canvas would flatten its frames).
    function fileToImage(file) {
        return new Promise(function (resolve, reject) {
            var url = URL.createObjectURL(file);
            var img = new Image();
            img.onload = function () { resolve({ img: img, url: url }); };
            img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('decode')); };
            img.src = url;
        });
    }

    function prepareImage(file) {
        if (file.type === 'image/gif') {
            return Promise.resolve({ blob: file, passthrough: true });
        }
        return fileToImage(file).then(function (result) {
            var img = result.img;
            var scale = Math.min(1, LONG_EDGE / Math.max(img.naturalWidth, img.naturalHeight));
            var width = Math.max(1, Math.round(img.naturalWidth * scale));
            var height = Math.max(1, Math.round(img.naturalHeight * scale));
            var canvas = document.createElement('canvas');
            canvas.width = width;
            canvas.height = height;
            canvas.getContext('2d').drawImage(img, 0, 0, width, height);
            return new Promise(function (resolve) {
                canvas.toBlob(function (blob) {
                    URL.revokeObjectURL(result.url);
                    resolve({ blob: blob || file, passthrough: false });
                }, 'image/jpeg', JPEG_QUALITY);
            });
        }).catch(function () {
            // A decode failure must not block the upload — send the original.
            return { blob: file, passthrough: true };
        });
    }

    function presign(contentType, bytes) {
        return api('/community/api/avatar/presign', {
            method: 'POST',
            body: JSON.stringify({ content_type: contentType, bytes: bytes }),
        }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error)
                    || 'Could not start the upload.');
            }
            return result.data;
        });
    }

    // The PUT headers are SIGNED, so they must be sent exactly as returned.
    function putToR2(item, blob) {
        var headers = item.headers || {};
        var method = item.method || 'PUT';
        if (typeof XMLHttpRequest === 'undefined') {
            return fetch(item.upload_url, { method: method, headers: headers, body: blob })
                .then(function (response) {
                    if (!response.ok) throw new Error('Upload failed (' + response.status + ')');
                    return true;
                });
        }
        return new Promise(function (resolve, reject) {
            var xhr = new XMLHttpRequest();
            xhr.open(method, item.upload_url, true);
            Object.keys(headers).forEach(function (name) {
                try { xhr.setRequestHeader(name, headers[name]); }
                catch (e) { /* a header the browser reserves is already set */ }
            });
            xhr.onload = function () {
                if (xhr.status >= 200 && xhr.status < 300) resolve(true);
                else reject(new Error('Upload failed (' + xhr.status + ')'));
            };
            xhr.onerror = function () { reject(new Error('Upload failed (network error)')); };
            xhr.ontimeout = function () { reject(new Error('Upload failed (timed out)')); };
            xhr.send(blob);
        });
    }

    function pickFile(fileList) {
        if (_busy) return;
        var file = (fileList && fileList[0]) || null;
        if (!file) return;
        if (!WALLET) { requiresLogin(); return; }
        if (!/^image\//.test(file.type || '')) {
            setStatus('Please choose an image file.', true);
            return;
        }
        if (!STORAGE_READY) {
            setStatus('Profile picture uploads are not configured yet.', true);
            return;
        }
        if (file.size > MAX_AVATAR_MB * 1024 * 1024) {
            setStatus('Image is too large (max ' + MAX_AVATAR_MB + 'MB).', true);
            return;
        }
        _busy = true;
        setStatus('Uploading…', false);

        prepareImage(file).then(function (prepared) {
            var contentType = prepared.passthrough
                ? (file.type || 'image/jpeg') : 'image/jpeg';
            return presign(contentType, prepared.blob.size).then(function (item) {
                return putToR2(item, prepared.blob).then(function () { return item; });
            });
        }).then(function (item) {
            return api('/community/api/avatar', {
                method: 'POST',
                body: JSON.stringify({ key: item.key, mime: item.mime }),
            });
        }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error)
                    || 'Could not save your profile picture.');
            }
            setUrl(result.data.avatar_url || '');
            setStatus('Profile picture updated.', false);
            toast('Profile picture updated!', 'ok');
        }).catch(function (error) {
            setStatus((error && error.message)
                || 'Could not save your profile picture.', true);
        }).then(function () { _busy = false; });
    }

    function remove() {
        if (!WALLET) { requiresLogin(); return; }
        setStatus('Removing…', false);
        api('/community/api/avatar', { method: 'DELETE' }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error)
                    || 'Could not remove your profile picture.');
            }
            setUrl('');
            setStatus('Profile picture removed.', false);
        }).catch(function (error) {
            setStatus((error && error.message)
                || 'Could not remove your profile picture.', true);
        });
    }

    function bind() {
        var input = $('cfAvatarInput');
        if (input) {
            input.addEventListener('change', function () {
                pickFile(input.files);
                input.value = '';
            });
        }
        var removeBtn = $('cfAvatarRemove');
        if (removeBtn) removeBtn.addEventListener('click', remove);
        var closeBtn = document.querySelector('[data-action="avatar-close"]');
        if (closeBtn) closeBtn.addEventListener('click', close);
        var editBtn = $('cpAvatarEdit');
        if (editBtn) editBtn.addEventListener('click', open);
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') close();
        });
        paint();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bind);
    } else {
        bind();
    }

    window.GMCommunityAvatar = {
        open: open,
        close: close,
        pickFile: pickFile,
        remove: remove,
        setUrl: function (url) { setUrl(url); },
        getUrl: function () { return _url; },
        setUsername: function (name) { USERNAME = name || ''; paint(); },
        onChange: function (fn) { if (typeof fn === 'function') _listeners.push(fn); },
        _internals: {
            prepareImage: prepareImage,
            presign: presign,
            putToR2: putToR2,
            renderInto: renderInto,
            paint: paint,
        },
    };
})();
