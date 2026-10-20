/* GoodMarket Community Feed — feed rendering, composer, upload, reactions,
   comments, report, delete.

   Media never passes through Flask: the browser asks for a presigned PUT and
   uploads straight to Cloudflare R2 (see community_feed/storage.py). That is
   what keeps a 100MB video from holding one of the app's ~16 gthread slots.

   Security rules kept from the rest of the app:
   - User-supplied text is written with textContent ONLY, never innerHTML.
   - Images are re-encoded through a canvas before upload, which also STRIPS
     EXIF — a phone photo otherwise publishes the user's GPS coordinates.
   - The reaction count is optimistic for feel, then reconciled with the
     server's number; a failure rolls both the chip and the count back. */

(function () {
    'use strict';

    var BOOT = window.GM_COMMUNITY_BOOT || {};
    var WALLET = BOOT.wallet || '';
    var USERNAME = BOOT.username || '';
    var USERNAME_REQUIRED = BOOT.usernameRequired !== false;
    var REACTIONS = BOOT.reactions || {};
    var MAX_CAPTION = BOOT.maxCaption || 2200;
    var MAX_MEDIA = BOOT.maxMedia || 4;
    var MAX_IMAGE_MB = BOOT.maxImageMb || 10;
    var MAX_VIDEO_MB = BOOT.maxVideoMb || 100;
    var STORAGE_READY = !!BOOT.storageReady;

    var LONG_EDGE = 2048;      // downscale target for images
    var JPEG_QUALITY = 0.85;

    // Fail fast client-side on a type the server will refuse anyway, so the
    // user is not told "upload failed" after waiting for a large file.
    var ALLOWED_TYPES = {
        'image/jpeg': 1, 'image/png': 1, 'image/gif': 1, 'image/webp': 1,
        'video/mp4': 1, 'video/webm': 1
    };

    var _feed = [];            // rendered posts, newest first
    var _nextCursor = null;
    var _loading = false;
    var _pending = [];         // composer attachments awaiting upload
    var _posting = false;
    var _reactionInFlight = {};  // post_id -> true (serialise per post)
    var _commentsOpen = {};    // post_id -> true
    var _commentCache = {};    // post_id -> [comments]

    // ── Small helpers ──────────────────────────────────────────────────────

    function $(id) { return document.getElementById(id); }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function esc(text) { return String(text === undefined || text === null ? '' : text); }

    function toast(message, kind) {
        var node = $('cfToast');
        if (!node) return;
        node.textContent = esc(message);
        node.className = 'cf-toast' + (kind ? ' is-' + kind : '');
        node.hidden = false;
        clearTimeout(node._timer);
        node._timer = setTimeout(function () { node.hidden = true; }, 4200);
    }

    function timeAgo(iso) {
        if (!iso) return '';
        var then = new Date(iso).getTime();
        if (isNaN(then)) return '';
        var secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
        if (secs < 60) return 'just now';
        var mins = Math.floor(secs / 60);
        if (mins < 60) return mins + 'm ago';
        var hours = Math.floor(mins / 60);
        if (hours < 24) return hours + 'h ago';
        var days = Math.floor(hours / 24);
        if (days < 7) return days + 'd ago';
        return new Date(then).toLocaleDateString();
    }

    function countLabel(n) {
        n = Number(n) || 0;
        if (n <= 0) return '';
        return n > 999 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K' : String(n);
    }

    // Non-capturing group on purpose: a capturing group shifts String.replace's
    // callback arguments and silently drops the surrounding text (the chatroom
    // shipped exactly that bug).
    var URL_RE = /(https?:\/\/[^\s<]+)/g;

    function linkifyInto(node, text) {
        var source = String(text === undefined || text === null ? '' : text);
        var last = 0;
        var match;
        URL_RE.lastIndex = 0;
        while ((match = URL_RE.exec(source)) !== null) {
            if (match.index > last) {
                node.appendChild(document.createTextNode(source.slice(last, match.index)));
            }
            var href = match[0];
            var link = document.createElement('a');
            link.href = href;
            link.target = '_blank';
            link.rel = 'noopener noreferrer nofollow';
            link.textContent = href;
            node.appendChild(link);
            last = match.index + href.length;
        }
        if (last < source.length) {
            node.appendChild(document.createTextNode(source.slice(last)));
        }
    }

    function api(path, options) {
        var opts = options || {};
        opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
        opts.credentials = 'same-origin';
        return fetch(path, opts).then(function (response) {
            return response.json().catch(function () { return {}; })
                .then(function (data) {
                    return { ok: response.ok, status: response.status, data: data };
                });
        });
    }

    function requiresLogin() {
        toast('Please log in to do that.', 'error');
        if (typeof window.openGoodMarketGetStarted === 'function') {
            window.openGoodMarketGetStarted();
        }
        return false;
    }

    // ── Composer: image processing ─────────────────────────────────────────

    function isImage(file) { return /^image\//.test(file.type || ''); }

    function fileToImage(file) {
        return new Promise(function (resolve, reject) {
            var url = URL.createObjectURL(file);
            var img = new Image();
            img.onload = function () { resolve({ img: img, url: url }); };
            img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('Could not read that image.')); };
            img.src = url;
        });
    }

    /* Re-encode through a canvas: downscales a huge camera photo AND strips
       EXIF (including GPS). Animated GIFs are passed through untouched — the
       canvas would flatten them to a single frame. */
    function prepareImage(file) {
        if (file.type === 'image/gif') {
            return Promise.resolve({ blob: file, width: null, height: null, preview: null, passthrough: true });
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
                    resolve({
                        blob: blob || file,
                        width: width,
                        height: height,
                        preview: canvas.toDataURL('image/jpeg', 0.6),
                        passthrough: false,
                    });
                }, 'image/jpeg', JPEG_QUALITY);
            });
        }).catch(function () {
            // A decode failure must not block the upload — send the original.
            return { blob: file, width: null, height: null, preview: null, passthrough: true };
        });
    }

    /* Videos are NOT transcoded (no ffmpeg in this deployment). We read the
       duration to reject an over-long clip early and grab a poster frame so the
       feed does not show a black rectangle before playback. */
    function prepareVideo(file) {
        return new Promise(function (resolve) {
            var url = URL.createObjectURL(file);
            var video = document.createElement('video');
            video.preload = 'metadata';
            video.muted = true;
            var done = false;
            function finish(payload) {
                if (done) return;
                done = true;
                resolve(payload);
            }
            video.onloadeddata = function () {
                var canvas = document.createElement('canvas');
                canvas.width = video.videoWidth || 320;
                canvas.height = video.videoHeight || 240;
                try {
                    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
                } catch (e) { /* a tainted/blocked frame just means no poster */ }
                finish({
                    blob: file,
                    width: video.videoWidth || null,
                    height: video.videoHeight || null,
                    duration: video.duration || null,
                    poster: canvas.width ? canvas.toDataURL('image/jpeg', 0.7) : null,
                });
            };
            video.onerror = function () {
                finish({ blob: file, width: null, height: null, duration: null, poster: null });
            };
            video.src = url;
            setTimeout(function () {
                finish({ blob: file, width: null, height: null, duration: null, poster: null });
            }, 6000);
        });
    }

    // ── Composer: upload ───────────────────────────────────────────────────

    function presign(contentType, bytes) {
        return api('/community/api/upload/presign', {
            method: 'POST',
            body: JSON.stringify({ content_type: contentType, bytes: bytes }),
        }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not start the upload.');
            }
            return result.data;
        });
    }

    /* fetch() cannot report upload progress, and a 100MB video with a dead
       progress bar is exactly the "did it even work?" moment. XHR's
       upload.onprogress gives real bytes-sent fractions, so the tile and the
       status line move while the PUT is in flight. fetch stays as the fallback
       for an environment without XHR. */
    function putToR2(item, blob, onProgress) {
        var headers = item.headers || {};
        var method = item.method || 'PUT';

        if (typeof XMLHttpRequest === 'undefined') {
            return fetch(item.upload_url, {
                method: method,
                // The Content-Type is SIGNED: sending a different value is a
                // SignatureDoesNotMatch, so it must be exactly what we got back.
                headers: headers,
                body: blob,
            }).then(function (response) {
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
            if (xhr.upload && onProgress) {
                xhr.upload.onprogress = function (event) {
                    if (!event.lengthComputable || !event.total) return;
                    onProgress(Math.min(1, event.loaded / event.total));
                };
            }
            xhr.onload = function () {
                if (xhr.status >= 200 && xhr.status < 300) resolve(true);
                else reject(new Error('Upload failed (' + xhr.status + ')'));
            };
            xhr.onerror = function () { reject(new Error('Upload failed (network error)')); };
            xhr.ontimeout = function () { reject(new Error('Upload failed (timed out)')); };
            xhr.send(blob);
        });
    }

    function uploadAttachment(entry) {
        entry.state = 'uploading';
        entry.progress = 0;
        renderPreviews();
        updateUploadStatus();
        return presign(entry.contentType, entry.blob.size)
            .then(function (item) {
                entry.upload = item;
                return putToR2(item, entry.blob, function (fraction) {
                    entry.progress = fraction;
                    if (entry._fill) {
                        entry._fill.style.width = Math.round(fraction * 100) + '%';
                    }
                    var pct = Math.round(fraction * 100);
                    if (pct !== entry._pct) { entry._pct = pct; updateUploadStatus(); }
                });
            })
            .then(function () {
                entry.state = 'done';
                entry.progress = 1;
                renderPreviews();
                updateUploadStatus();
                return entry;
            })
            .catch(function (error) {
                entry.state = 'failed';
                entry.error = error && error.message ? error.message : 'Upload failed.';
                renderPreviews();
                updateUploadStatus();
                throw error;
            });
    }

    function addFiles(fileList) {
        var files = Array.prototype.slice.call(fileList || []);
        if (!files.length) return;
        if (!STORAGE_READY) {
            toast('Media uploads are not configured yet.', 'error');
            return;
        }
        if (!WALLET) { requiresLogin(); return; }

        files.forEach(function (file) {
            if (_pending.length >= MAX_MEDIA) {
                toast('You can attach up to ' + MAX_MEDIA + ' files.', 'error');
                return;
            }
            var mime = (file.type || '').split(';')[0].trim().toLowerCase();
            if (!ALLOWED_TYPES[mime]) {
                toast('Please choose a JPEG, PNG, GIF, WEBP, MP4 or WEBM file.', 'error');
                return;
            }
            var isVid = /^video\//.test(mime);
            var limitMb = isVid ? MAX_VIDEO_MB : MAX_IMAGE_MB;
            if (file.size > limitMb * 1024 * 1024) {
                toast('That file is too large (max ' + limitMb + 'MB).', 'error');
                return;
            }
            var entry = { file: file, state: 'preparing', contentType: mime, isVideo: isVid, progress: 0 };
            _pending.push(entry);
            renderPreviews();
            updateUploadStatus();
            updatePostButton();   // flips the button to "Uploading…" right away

            var prepared = isVid ? prepareVideo(file) : prepareImage(file);
            prepared.then(function (out) {
                entry.blob = out.blob;
                entry.width = out.width;
                entry.height = out.height;
                entry.duration = out.duration || null;
                entry.posterDataUrl = out.poster || out.preview || null;
                if (out.blob && out.blob.type) entry.contentType = out.blob.type;
                if (!entry.contentType) entry.contentType = isVid ? 'video/mp4' : 'image/jpeg';
                return uploadAttachment(entry);
            }).catch(function (error) {
                entry.state = 'failed';
                entry.error = (error && error.message) || 'Could not prepare that file.';
                renderPreviews();
                updateUploadStatus();
            }).then(updatePostButton);
        });
    }

    function renderPreviews() {
        var wrap = $('cfPreviews');
        if (!wrap) return;
        wrap.textContent = '';
        if (!_pending.length) { wrap.hidden = true; return; }
        wrap.hidden = false;

        _pending.forEach(function (entry, index) {
            var busy = entry.state === 'preparing' || entry.state === 'uploading';
            var box = el('div', 'cf-preview' + (entry.state === 'done' ? ' is-done' : '')
                + (entry.state === 'failed' ? ' is-failed' : '')
                + (busy ? ' is-busy' : ''));

            if (entry.posterDataUrl) {
                var img = document.createElement('img');
                img.src = entry.posterDataUrl;
                img.alt = '';
                box.appendChild(img);
            } else if (entry.isVideo) {
                box.appendChild(el('div', 'cf-preview-bar'));
            }

            // A tile with no poster yet (or a video still being read) would sit
            // as a black square, which reads as "nothing happened".
            if (busy) {
                box.appendChild(el('div', 'cf-preview-spinner'));
            }

            var remove = el('button', 'cf-preview-remove', '×');
            remove.type = 'button';
            remove.setAttribute('aria-label', 'Remove attachment');
            remove.onclick = function () {
                _pending.splice(index, 1);
                renderPreviews();
                updateUploadStatus();
                updatePostButton();
            };
            box.appendChild(remove);

            var bar = el('div', 'cf-preview-bar');
            var fill = el('i', null, '');
            // The overlay + a real width % read as "working" on a slow phone;
            // the indeterminate shimmer keeps a preparing tile from looking
            // frozen while the canvas/decoder runs.
            fill.style.width = entry.state === 'done' ? '100%'
                : (entry.state === 'uploading' ? Math.round((entry.progress || 0) * 100) + '%' : '0');
            entry._fill = fill;
            bar.appendChild(fill);
            box.appendChild(bar);
            wrap.appendChild(box);
        });
    }

    /* One live line under the tiles so the user is never guessing: which file
       is being processed, what percentage has actually left the device, or that
       the post itself is publishing. */
    function updateUploadStatus() {
        if (_posting) return;   // "Publishing…" owns the line
        var busy = _pending.filter(function (e) {
            return e.state === 'preparing' || e.state === 'uploading';
        });
        var failed = _pending.filter(function (e) { return e.state === 'failed'; });
        if (busy.length) {
            var entry = busy[0];
            if (entry.state === 'preparing') {
                setUploadStatus('Processing ' + (entry.isVideo ? 'video' : 'image') + '…', false);
            } else {
                var pct = Math.round((entry.progress || 0) * 100);
                setUploadStatus('Uploading… ' + pct + '%', false);
            }
            return;
        }
        if (failed.length) {
            setUploadStatus(failed[0].error || 'Upload failed.', true);
            return;
        }
        setUploadStatus('', false);
    }

    function setUploadStatus(message, isError) {
        var node = $('cfUploadStatus');
        if (!node) return;
        if (!message) { node.hidden = true; node.textContent = ''; node.className = 'cf-upload-status'; return; }
        node.hidden = false;
        // A busy line ("Uploading… 42%") gets the spinner via CSS ::before, so
        // the text stays a single textContent write (never innerHTML).
        node.className = 'cf-upload-status' + (isError ? ' is-error' : ' is-busy');
        node.textContent = esc(message);
    }

    function updateCounter() {
        var input = $('cfCaption');
        var node = $('cfCounter');
        if (!input || !node) return;
        var length = input.value.length;
        node.textContent = length + ' / ' + MAX_CAPTION;
        node.className = 'cf-counter'
            + (length > MAX_CAPTION ? ' is-over' : (length > MAX_CAPTION * 0.9 ? ' is-near' : ''));
    }

    // A username is required before posting: a post without one renders as a
    // wallet address. Kept as a function so the gate can be re-evaluated after
    // the inline prompt saves a name, without a page reload.
    function needsUsername() {
        return USERNAME_REQUIRED && !!WALLET && !USERNAME;
    }

    function focusUsernameInput() {
        var field = $('cfUsernameInput');
        if (!field) return;
        if (field.focus) field.focus();
        if (field.scrollIntoView) {
            try { field.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (e) { /* older browsers */ }
        }
    }

    function setUsernameHint(text, isError) {
        var hint = $('cfUsernameHint');
        if (!hint) return;
        hint.textContent = text;
        hint.className = 'cf-username-hint' + (isError ? ' is-error' : '');
    }

    // Saves the username through the app-wide endpoint, so the same rules
    // (3-24 chars, uniqueness, once-per-year edit) apply here and on /wallet.
    // On success the composer unlocks in place — no reload, so the user does
    // not lose a caption they already typed.
    function saveUsername() {
        var field = $('cfUsernameInput');
        var button = $('cfUsernameSave');
        if (!field) return;
        var value = (field.value || '').trim();

        if (!/^[A-Za-z0-9_]{3,24}$/.test(value)) {
            setUsernameHint('Use 3-24 letters, numbers or _ (no spaces).', true);
            if (field.focus) field.focus();
            return;
        }

        if (button) { button.disabled = true; button.textContent = 'Saving…'; }
        setUsernameHint('Saving…', false);

        api('/api/user/username', {
            method: 'POST',
            body: JSON.stringify({ username: value }),
        }).then(function (result) {
            var data = result.data || {};
            if (!data.success) {
                throw new Error(data.error || 'Could not save that username.');
            }
            USERNAME = data.username || value;
            var gate = $('cfUsernameGate');
            if (gate && gate.parentNode) gate.parentNode.removeChild(gate);
            toast('Username saved! You can post now.', 'ok');
            updatePostButton();
        }).catch(function (error) {
            if (button) { button.disabled = false; button.textContent = 'Save'; }
            setUsernameHint((error && error.message) || 'Could not save that username.', true);
        });
    }

    function updatePostButton() {
        var button = $('cfPostBtn');
        var input = $('cfCaption');
        if (!button || !input) return;
        var uploading = _pending.some(function (e) { return e.state === 'uploading' || e.state === 'preparing'; });
        var failed = _pending.some(function (e) { return e.state === 'failed'; });
        var hasText = input.value.trim().length > 0;
        var overLimit = input.value.length > MAX_CAPTION;
        var canPost = !_posting && !uploading && !failed
            && !needsUsername()
            && _pending.length <= MAX_MEDIA
            && !overLimit
            && (hasText || _pending.some(function (e) { return e.state === 'done'; }));
        button.disabled = !canPost;
        // Say WHY the button is dead: "Uploading…" answers the user's "did it
        // even start?" far better than a disabled button with no explanation.
        button.textContent = _posting ? 'Posting…' : (uploading ? 'Uploading…' : 'Post');
    }

    function resetComposer() {
        _pending = [];
        var input = $('cfCaption');
        if (input) input.value = '';
        var fileInput = $('cfFileInput');
        if (fileInput) fileInput.value = '';
        renderPreviews();
        setUploadStatus('');
        updateCounter();
        updatePostButton();
    }

    function submitPost() {
        if (!WALLET) { requiresLogin(); return; }
        if (_posting) return;
        if (needsUsername()) {
            // The button is disabled in this state, so this only fires from a
            // keyboard/console path — point at the prompt instead of failing
            // later with a server error.
            focusUsernameInput();
            toast('Please choose a username first.', 'error');
            return;
        }
        var input = $('cfCaption');
        var caption = input ? input.value : '';

        var done = _pending.filter(function (e) { return e.state === 'done' && e.upload; });
        if (_pending.some(function (e) { return e.state !== 'done'; })) {
            toast('Please wait for the uploads to finish.', 'error');
            return;
        }
        if (!caption.trim() && !done.length) {
            toast('Please write something or attach a photo/video.', 'error');
            return;
        }

        _posting = true;
        updatePostButton();
        setUploadStatus('Publishing…', false);

        var media = done.map(function (entry) {
            return {
                key: entry.upload.key,
                media_type: entry.upload.media_type,
                mime: entry.upload.mime,
                width: entry.width,
                height: entry.height,
                duration: entry.duration,
                poster_url: null,   // the poster is uploaded as its own object below
            };
        });

        // Upload any poster frames first so they become managed objects, then
        // attach their public URLs to the media descriptors.
        uploadPosters(done).then(function (posters) {
            posters.forEach(function (url, index) { media[index].poster_url = url; });
            return api('/community/api/posts', {
                method: 'POST',
                body: JSON.stringify({ caption: caption, media: media }),
            });
        }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not publish your post.');
            }
            _posting = false;
            setUploadStatus('');
            resetComposer();
            // Prepend from the local object — no refetch needed.
            _feed.unshift(result.data.post);
            renderFeed();
            toast('Posted!', 'ok');
        }).catch(function (error) {
            _posting = false;
            setUploadStatus('');
            updatePostButton();
            toast((error && error.message) || 'Could not publish your post.', 'error');
        });
    }

    function uploadPosters(entries) {
        var jobs = entries.map(function (entry) {
            if (!entry.posterDataUrl || !entry.isVideo) return Promise.resolve(null);
            return dataUrlToBlob(entry.posterDataUrl).then(function (blob) {
                return presign('image/jpeg', blob.size).then(function (item) {
                    return putToR2(item, blob).then(function () { return item.public_url; });
                });
            }).catch(function () { return null; });
        });
        return Promise.all(jobs);
    }

    function dataUrlToBlob(dataUrl) {
        return fetch(dataUrl).then(function (response) { return response.blob(); });
    }

    // ── Feed rendering ─────────────────────────────────────────────────────

    function renderFeed() {
        var container = $('cfFeed');
        if (!container) return;
        container.textContent = '';
        container.setAttribute('aria-busy', 'false');

        if (!_feed.length) {
            var empty = $('cfEmpty');
            if (empty) empty.hidden = false;
            updateLoadMore();
            return;
        }
        var emptyNode = $('cfEmpty');
        if (emptyNode) emptyNode.hidden = true;

        _feed.forEach(function (post) { container.appendChild(buildCard(post)); });
        updateLoadMore();
    }

    function updateLoadMore() {
        var button = $('cfLoadMore');
        if (!button) return;
        button.hidden = !_nextCursor;
    }

    // ── Follow ─────────────────────────────────────────────────────────────
    // A follow is a WRITE, so a guest gets the shared connect card and issues
    // no request — the same contract as react/comment. The server stores the
    // edge by WALLET, resolved from the username; the browser only ever names
    // a username and only ever receives counts.

    function buildFollowButton(username, isFollowing) {
        var btn = el('button', 'cf-follow' + (isFollowing ? ' is-following' : ''),
            isFollowing ? 'Following' : 'Follow');
        btn.type = 'button';
        btn.setAttribute('data-follow', username);
        btn.setAttribute('aria-pressed', isFollowing ? 'true' : 'false');
        btn.setAttribute('aria-label',
            (isFollowing ? 'Unfollow @' : 'Follow @') + username);
        btn.onclick = function () { toggleFollow(btn, username); };
        return btn;
    }

    function toggleFollow(btn, username) {
        if (!WALLET) { requiresLogin(); return Promise.resolve(); }
        var following = btn.classList.contains('is-following');
        // Optimistic flip so the tap feels instant; rolled back on failure.
        setFollowButton(btn, !following);
        return api('/community/api/users/' + encodeURIComponent(username) + '/follow', {
            method: following ? 'DELETE' : 'POST',
        }).then(function (result) {
            if (!result.ok || !result.data || result.data.success === false) {
                setFollowButton(btn, following);
                toast((result.data && result.data.error) || 'Could not update follow.',
                    'error');
                return;
            }
            setFollowButton(btn, !!result.data.following);
        }).catch(function () {
            setFollowButton(btn, following);
            toast('Could not update follow.', 'error');
        });
    }

    function setFollowButton(btn, following) {
        btn.classList.toggle('is-following', following);
        btn.textContent = following ? 'Following' : 'Follow';
        btn.setAttribute('aria-pressed', following ? 'true' : 'false');
        var name = btn.getAttribute('data-follow') || '';
        btn.setAttribute('aria-label',
            (following ? 'Unfollow @' : 'Follow @') + name);
    }

    // ── Media ──────────────────────────────────────────────────────────────
    // A video is shown Instagram-style: a BOUNDED, rounded frame (never the
    // full screen) with `object-fit: contain`, so nothing is ever cropped. The
    // frame is a button that opens the lightbox for the full-size view.

    function buildVideoFrame(item, username) {
        var frame = el('button', 'cf-video-frame');
        frame.type = 'button';
        frame.setAttribute('aria-label', 'Play video by ' + (username || 'this member'));
        // Match the frame to the clip when the uploader reported its size, so
        // there are no letterbox bars; fall back to the IG portrait default.
        if (item.w && item.h) {
            frame.style.aspectRatio = item.w + ' / ' + item.h;
        }
        var video = document.createElement('video');
        video.controls = true;
        video.preload = 'metadata';
        video.playsInline = true;
        if (item.poster) video.poster = item.poster;
        video.src = item.url;
        // The controls need the taps; the frame handles the rest.
        video.onclick = function (event) { event.stopPropagation(); };
        frame.appendChild(video);
        frame.onclick = function () { openLightbox(item.url, 'video'); };
        return frame;
    }

    function buildCard(post) {
        var card = el('article', 'cf-card');
        card.setAttribute('data-post-id', post.id);

        // Head. The avatar + name link to the member's PUBLIC PROFILE
        // (`/community/u/<username>`), where their posts and album live. The
        // link carries only the username — a profile is never addressed by
        // wallet, and the address never reaches the browser.
        var head = el('div', 'cf-card-head');
        var profile = el('a', 'cf-profile-link');
        profile.href = '/community/u/' + encodeURIComponent(
            String(post.username || '').replace(/^@/, ''));
        profile.title = 'View @' + post.username + "'s profile";
        profile.setAttribute('aria-label', 'View @' + post.username + "'s profile");
        profile.appendChild(el('div', 'cf-avatar', (esc(post.username) || 'A')[0].toUpperCase()));
        var who = el('div', 'cf-card-who');
        who.appendChild(el('strong', null, post.username));
        var stamp = timeAgo(post.created_at) + (post.is_edited ? ' · edited' : '');
        who.appendChild(el('span', null, stamp));
        profile.appendChild(who);
        head.appendChild(profile);
        // Follow pill: only for ANOTHER member's post. Your own post never
        // offers it (you cannot follow yourself) and a guest never gets a
        // working one — the tap raises the shared connect card instead.
        if (!post.is_mine && post.username) {
            head.appendChild(buildFollowButton(post.username, post.is_following));
        }
        if (post.is_mine) {
            var menu = el('button', 'cf-menu-btn', '⋯');
            menu.type = 'button';
            menu.setAttribute('aria-label', 'Post options');
            menu.onclick = function () { openPostMenu(post, menu); };
            head.appendChild(menu);
        }
        card.appendChild(head);

        // Media
        if (post.media && post.media.length) {
            var mediaWrap = el('div', 'cf-media');
            post.media.forEach(function (item) {
                if (item.type === 'video') {
                    mediaWrap.appendChild(buildVideoFrame(item, post.username));
                } else {
                    var img = document.createElement('img');
                    img.loading = 'lazy';
                    img.decoding = 'async';
                    img.referrerPolicy = 'no-referrer';
                    img.src = item.url;
                    img.alt = post.body ? post.body.slice(0, 120) : 'Photo shared by ' + post.username;
                    img.onclick = function () { openLightbox(item.url); };
                    mediaWrap.appendChild(img);
                }
            });
            card.appendChild(mediaWrap);
        }

        var body = el('div', 'cf-card-body');

        // Reaction + comment summary
        var summary = el('div', 'cf-summary');
        var left = el('div', 'cf-summary-left');
        var emoji = post.my_reaction && REACTIONS[post.my_reaction]
            ? REACTIONS[post.my_reaction] : '👍';
        var emojiNode = el('span', 'cf-summary-emoji', emoji);
        emojiNode.setAttribute('aria-hidden', 'true');
        left.appendChild(emojiNode);
        var countNode = el('span', 'cf-count', countLabel(post.reaction_count) || '0');
        // Announce the change without re-reading the whole card.
        countNode.setAttribute('aria-live', 'polite');
        left.appendChild(countNode);
        summary.appendChild(left);
        summary.appendChild(el('span', null, countLabel(post.comment_count)
            ? countLabel(post.comment_count) + ' comment' + (post.comment_count === 1 ? '' : 's')
            : ''));
        body.appendChild(summary);

        // Caption (media above, caption below — the IG/FB convention)
        if (post.body) {
            var caption = el('p', 'cf-caption');
            linkifyInto(caption, post.body);
            body.appendChild(caption);
            requestAnimationFrame(function () {
                // Clamp only when it actually overflows, and always leave a way
                // to read the whole thing.
                if (caption.scrollHeight > caption.clientHeight + 4 && !caption.classList.contains('is-clamped')) {
                    caption.classList.add('is-clamped');
                    var more = el('button', 'cf-see-more', 'See more');
                    more.type = 'button';
                    more.onclick = function () {
                        var clamped = caption.classList.toggle('is-clamped');
                        more.textContent = clamped ? 'See more' : 'See less';
                    };
                    caption.insertAdjacentElement('afterend', more);
                }
            });
        }

        // Reaction bar — always visible, one tap.
        var bar = el('div', 'cf-reactions');
        bar.setAttribute('role', 'group');
        bar.setAttribute('aria-label', 'Reactions');
        Object.keys(REACTIONS).forEach(function (key) {
            var chip = el('button', 'cf-chip', REACTIONS[key]);
            chip.type = 'button';
            // The emoji IS the label text, but aria-label gives screen readers
            // the word ("React with love") instead of the glyph name.
            chip.setAttribute('aria-label', 'React with ' + key);
            chip.setAttribute('aria-pressed', post.my_reaction === key ? 'true' : 'false');
            chip.onclick = function () { react(post, key); };
            bar.appendChild(chip);
        });
        body.appendChild(bar);

        // Actions
        var actions = el('div', 'cf-card-actions');
        var commentBtn = el('button', 'cf-action-btn', '💬 Comment');
        commentBtn.type = 'button';
        // Reading a post is public; joining the thread is not. A guest gets the
        // shared connect card instead of an empty comment box.
        commentBtn.onclick = function () {
            if (!WALLET) { requiresLogin(); return; }
            toggleComments(post, body);
        };
        actions.appendChild(commentBtn);

        var shareBtn = el('button', 'cf-action-btn', '↗ Share');
        shareBtn.type = 'button';
        shareBtn.onclick = function () { sharePost(post); };
        actions.appendChild(shareBtn);

        if (!post.is_mine) {
            var reportBtn = el('button', 'cf-action-btn', '⚑ Report');
            reportBtn.type = 'button';
            reportBtn.onclick = function () { reportPost(post); };
            actions.appendChild(reportBtn);
        }
        body.appendChild(actions);

        card.appendChild(body);
        return card;
    }

    function findPost(id) {
        for (var i = 0; i < _feed.length; i++) {
            if (String(_feed[i].id) === String(id)) return _feed[i];
        }
        return null;
    }

    function rerenderPost(post) {
        var card = document.querySelector('[data-post-id="' + post.id + '"]');
        if (!card || !card.parentNode) { renderFeed(); return; }
        card.parentNode.replaceChild(buildCard(post), card);
    }

    // ── Reactions ──────────────────────────────────────────────────────────

    function react(post, key) {
        if (!WALLET) { requiresLogin(); return; }
        if (_reactionInFlight[post.id]) return;   // a fast double-tap fires once
        _reactionInFlight[post.id] = true;

        var previous = post.my_reaction;
        var previousCount = Number(post.reaction_count) || 0;

        // Optimistic: switch keeps the count, add bumps it, un-react drops it.
        if (previous === key) {
            post.my_reaction = null;
            post.reaction_count = Math.max(0, previousCount - 1);
        } else if (previous) {
            post.my_reaction = key;
        } else {
            post.my_reaction = key;
            post.reaction_count = previousCount + 1;
        }
        rerenderPost(post);

        var removing = previous === key;
        var path = '/community/api/posts/' + post.id + '/reaction';
        var request = removing
            ? api(path, { method: 'DELETE' })
            : api(path, { method: 'POST', body: JSON.stringify({ reaction: key }) });

        request.then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not save your reaction.');
            }
            // Reconcile with the authoritative count.
            post.reaction_count = Number(result.data.reaction_count);
            post.my_reaction = result.data.my_reaction;
            rerenderPost(post);
        }).catch(function (error) {
            // Roll back both the chip and the count.
            post.my_reaction = previous;
            post.reaction_count = previousCount;
            rerenderPost(post);
            toast((error && error.message) || 'Could not save your reaction.', 'error');
        }).then(function () {
            _reactionInFlight[post.id] = false;
        });
    }

    // ── Comments ───────────────────────────────────────────────────────────

    function toggleComments(post, body) {
        var existing = body.querySelector('.cf-comments');
        if (existing) { existing.remove(); delete _commentsOpen[post.id]; return; }
        _commentsOpen[post.id] = true;

        var wrap = el('div', 'cf-comments');
        wrap.setAttribute('aria-label', 'Comments');
        wrap.appendChild(el('div', 'cf-comment', 'Loading comments…'));
        body.appendChild(wrap);

        api('/community/api/posts/' + post.id + '/comments').then(function (result) {
            _commentCache[post.id] = (result.data && result.data.comments) || [];
            renderComments(post, wrap);
        }).catch(function () {
            wrap.textContent = '';
            wrap.appendChild(el('div', 'cf-comment', 'Could not load comments.'));
        });
    }

    function renderComments(post, wrap) {
        wrap.textContent = '';
        var comments = _commentCache[post.id] || [];
        comments.forEach(function (comment) {
            var row = el('div', 'cf-comment');
            row.appendChild(el('div', 'cf-comment-avatar', (esc(comment.username) || 'A')[0].toUpperCase()));
            var bubble = el('div', 'cf-comment-body');
            bubble.appendChild(el('strong', null, comment.username));
            bubble.appendChild(document.createTextNode(comment.body));
            row.appendChild(bubble);
            if (comment.is_mine) {
                var remove = el('button', 'cf-action-btn', '×');
                remove.type = 'button';
                remove.setAttribute('aria-label', 'Delete comment');
                remove.onclick = function () { deleteComment(post, comment, wrap); };
                row.appendChild(remove);
            }
            wrap.appendChild(row);
        });

        if (!WALLET) {
            wrap.appendChild(el('div', 'cf-comment', 'Log in to comment.'));
            return;
        }

        var form = el('div', 'cf-comment-form');
        var input = document.createElement('input');
        input.type = 'text';
        input.placeholder = 'Write a comment…';
        input.maxLength = 500;
        input.setAttribute('aria-label', 'Write a comment');
        var submit = el('button', null, 'Send');
        submit.type = 'button';
        submit.disabled = true;
        input.oninput = function () { submit.disabled = !input.value.trim(); };
        input.onkeydown = function (event) {
            if (event.key === 'Enter' && input.value.trim()) { submit.click(); }
        };
        submit.onclick = function () { addComment(post, input, submit, wrap); };
        form.appendChild(input);
        form.appendChild(submit);
        wrap.appendChild(form);
    }

    function addComment(post, input, submit, wrap) {
        var body = input.value.trim();
        if (!body) return;
        submit.disabled = true;
        submit.textContent = '…';
        api('/community/api/posts/' + post.id + '/comments', {
            method: 'POST',
            body: JSON.stringify({ body: body }),
        }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not post your comment.');
            }
            input.value = '';
            _commentCache[post.id] = (_commentCache[post.id] || []).concat([result.data.comment]);
            post.comment_count = (Number(post.comment_count) || 0) + 1;
            renderComments(post, wrap);
            rerenderPost(post);
        }).catch(function (error) {
            toast((error && error.message) || 'Could not post your comment.', 'error');
        }).then(function () {
            submit.disabled = false;
            submit.textContent = 'Send';
        });
    }

    function deleteComment(post, comment, wrap) {
        api('/community/api/comments/' + comment.id, { method: 'DELETE' }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not delete the comment.');
            }
            _commentCache[post.id] = (_commentCache[post.id] || []).filter(function (row) {
                return String(row.id) !== String(comment.id);
            });
            post.comment_count = Math.max(0, (Number(post.comment_count) || 1) - 1);
            renderComments(post, wrap);
            rerenderPost(post);
        }).catch(function (error) {
            toast((error && error.message) || 'Could not delete the comment.', 'error');
        });
    }

    // ── Post menu / edit / delete / report ─────────────────────────────────

    function openPostMenu(post, anchor) {
        closePostMenu();
        var menu = el('div', 'cf-menu');
        menu.id = 'cfPostMenu';
        menu.setAttribute('role', 'menu');

        var edit = el('button', 'cf-menu-item', '✏️ Edit caption');
        edit.type = 'button';
        edit.setAttribute('role', 'menuitem');
        edit.onclick = function () { closePostMenu(); openEdit(post); };
        menu.appendChild(edit);

        var remove = el('button', 'cf-menu-item is-danger', '🗑 Delete post');
        remove.type = 'button';
        remove.setAttribute('role', 'menuitem');
        remove.onclick = function () { closePostMenu(); deletePost(post); };
        menu.appendChild(remove);

        document.body.appendChild(menu);
        var rect = anchor.getBoundingClientRect();
        menu.style.top = (rect.bottom + window.scrollY + 4) + 'px';
        menu.style.left = Math.max(8, rect.right + window.scrollX - 180) + 'px';

        setTimeout(function () {
            document.addEventListener('click', _menuOutsideClick, { once: true });
        }, 0);
    }

    function _menuOutsideClick(event) {
        var menu = $('cfPostMenu');
        if (menu && menu.contains(event.target)) return;
        closePostMenu();
    }

    function closePostMenu() {
        var menu = $('cfPostMenu');
        if (menu) menu.remove();
    }

    var _editing = null;

    function openEdit(post) {
        _editing = post;
        var modal = $('cfEditModal');
        var input = $('cfEditCaption');
        if (!modal || !input) return;
        input.value = post.body || '';
        modal.hidden = false;
        input.focus();
    }

    function closeEdit() {
        var modal = $('cfEditModal');
        if (modal) modal.hidden = true;
        _editing = null;
    }

    function saveEdit() {
        if (!_editing) return;
        var input = $('cfEditCaption');
        var button = $('cfEditSaveBtn');
        var body = input ? input.value : '';
        if (button) { button.disabled = true; button.textContent = 'Saving…'; }
        api('/community/api/posts/' + _editing.id, {
            method: 'PATCH',
            body: JSON.stringify({ caption: body }),
        }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not update the post.');
            }
            _editing.body = result.data.body;
            _editing.is_edited = true;
            rerenderPost(_editing);
            closeEdit();
            toast('Caption updated.', 'ok');
        }).catch(function (error) {
            toast((error && error.message) || 'Could not update the post.', 'error');
        }).then(function () {
            if (button) { button.disabled = false; button.textContent = 'Save'; }
        });
    }

    function deletePost(post) {
        if (!window.confirm('Delete this post? The photos and videos will be removed too.')) return;
        api('/community/api/posts/' + post.id, { method: 'DELETE' }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not delete the post.');
            }
            _feed = _feed.filter(function (row) { return String(row.id) !== String(post.id); });
            renderFeed();
            toast('Post deleted.', 'ok');
        }).catch(function (error) {
            toast((error && error.message) || 'Could not delete the post.', 'error');
        });
    }

    function reportPost(post) {
        if (!WALLET) { requiresLogin(); return; }
        var reason = window.prompt('Why are you reporting this post? (optional)', '');
        if (reason === null) return;
        api('/community/api/report', {
            method: 'POST',
            body: JSON.stringify({ post_id: post.id, reason: reason }),
        }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not send the report.');
            }
            toast('Thanks — our team will review this.', 'ok');
        }).catch(function (error) {
            toast((error && error.message) || 'Could not send the report.', 'error');
        });
    }

    function sharePost(post) {
        var url = window.location.origin + '/community#post-' + post.id;
        var text = post.body ? post.body.slice(0, 120) : 'Check out this post on GoodMarket Community';
        if (navigator.share) {
            navigator.share({ title: 'GoodMarket Community', text: text, url: url }).catch(function () {});
            return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(url).then(function () {
                toast('Link copied.', 'ok');
            }).catch(function () { toast(url); });
            return;
        }
        window.prompt('Copy this link:', url);
    }

    // ── Lightbox ───────────────────────────────────────────────────────────

    function openLightbox(url, kind) {
        var box = $('cfLightbox');
        var body = $('cfLightboxBody');
        if (!box || !body) return;
        body.textContent = '';
        if (kind === 'video') {
            // The full-size view of a bounded in-feed frame.
            var video = document.createElement('video');
            video.controls = true;
            video.autoplay = true;
            video.playsInline = true;
            video.src = url;
            body.appendChild(video);
        } else {
            var img = document.createElement('img');
            img.src = url;
            img.alt = '';
            body.appendChild(img);
        }
        box.hidden = false;
    }

    function closeLightbox() {
        var box = $('cfLightbox');
        var body = $('cfLightboxBody');
        if (body) body.textContent = '';
        if (box) box.hidden = true;
    }

    // ── Feed loading ───────────────────────────────────────────────────────

    function loadFeed(reset) {
        if (_loading) return Promise.resolve();
        _loading = true;
        var url = '/community/api/feed?limit=10';
        if (!reset && _nextCursor) url += '&cursor=' + encodeURIComponent(_nextCursor);

        return api(url).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not load the feed.');
            }
            _nextCursor = result.data.next_cursor || null;
            // Guard a malformed payload: a server bug must not break the page.
            var rows = result.data.posts || [];
            _feed = reset ? rows : _feed.concat(rows);
            renderFeed();
        }).catch(function (error) {
            var container = $('cfFeed');
            if (container) {
                container.textContent = '';
                container.setAttribute('aria-busy', 'false');
                container.appendChild(el('p', 'cf-empty',
                    (error && error.message) || 'Could not load the feed.'));
            }
        }).then(function () {
            _loading = false;
        });
    }

    function refresh() { loadFeed(true); }

    function loadMore() {
        var button = $('cfLoadMore');
        if (button) { button.disabled = true; button.textContent = 'Loading…'; }
        loadFeed(false).then(function () {
            if (button) { button.disabled = false; button.textContent = 'Load more'; }
        });
    }

    // ── Bottom nav / member search / Earn sheet ────────────────────────────
    // The bar is built from `BOOT.nav` so the destination list lives in ONE
    // place (the template's GM_COMMUNITY_BOOT). A destination marked `guestOk`
    // is read-only (the feed, member search); every other one raises the shared
    // connect card for a guest, exactly like the write actions above. Labels and
    // usernames are written with textContent — user data never reaches markup.

    var NAV = (BOOT.nav && BOOT.nav.length) ? BOOT.nav : [];
    var SEARCH_MIN = 2;
    var _searchTimer = null;
    var _searchSeq = 0;

    function buildNavItem(item) {
        var isLink = !!item.href;
        var node = document.createElement(isLink ? 'a' : 'button');
        node.className = 'cf-nav-item' + (item.id === 'feed' ? ' is-active' : '');
        node.setAttribute('data-nav', item.id || '');
        if (isLink) node.href = item.href;
        else node.type = 'button';
        var icon = el('span', 'cf-nav-icon', item.icon || '');
        icon.setAttribute('aria-hidden', 'true');
        node.appendChild(icon);
        node.appendChild(el('span', 'cf-nav-label', item.label || ''));
        node.onclick = function (event) {
            if (item.action === 'search') {
                if (event && event.preventDefault) event.preventDefault();
                openSearch();
                return;
            }
            if (item.action === 'earn') {
                if (event && event.preventDefault) event.preventDefault();
                if (!WALLET) { requiresLogin(); return; }
                openSheet();
                return;
            }
            // A plain link. A guest may follow it only when it is read-only.
            if (!item.guestOk && !WALLET) {
                if (event && event.preventDefault) event.preventDefault();
                requiresLogin();
            }
        };
        return node;
    }

    function renderNav() {
        var bar = $('cfBottomNav');
        if (!bar) return;
        bar.textContent = '';
        NAV.forEach(function (item) { bar.appendChild(buildNavItem(item)); });
    }

    function openSearch() {
        var bar = $('cfSearchBar');
        if (!bar) return;
        bar.hidden = false;
        var input = $('cfSearchInput');
        if (input && input.focus) input.focus();
    }

    function closeSearch() {
        var bar = $('cfSearchBar');
        if (bar) bar.hidden = true;
        var input = $('cfSearchInput');
        if (input) input.value = '';
        var results = $('cfSearchResults');
        if (results) results.textContent = '';
        if (_searchTimer) { clearTimeout(_searchTimer); _searchTimer = null; }
    }

    function searchInput() {
        if (_searchTimer) clearTimeout(_searchTimer);
        _searchTimer = setTimeout(function () {
            var input = $('cfSearchInput');
            runSearch(input ? input.value : '');
        }, 220);
    }

    function searchKeydown(event) {
        if (event && event.key === 'Escape') closeSearch();
    }

    function runSearch(term) {
        var cleaned = String(term === undefined || term === null ? '' : term).trim();
        var results = $('cfSearchResults');
        if (cleaned.length < SEARCH_MIN) {
            if (results) {
                results.textContent = '';
                results.appendChild(el('p', 'cf-search-hint',
                    'Type at least ' + SEARCH_MIN + ' characters to search members.'));
            }
            return Promise.resolve();
        }
        // Sequence guard: a slow early response must not overwrite a newer one.
        var seq = ++_searchSeq;
        return api('/community/api/users/search?q=' + encodeURIComponent(cleaned))
            .then(function (result) {
                if (seq !== _searchSeq) return;
                renderSearchResults((result.data && result.data.users) || []);
            })
            .catch(function () {
                if (seq !== _searchSeq) return;
                renderSearchResults(null);
            });
    }

    function renderSearchResults(users) {
        var results = $('cfSearchResults');
        if (!results) return;
        results.textContent = '';
        if (users === null) {
            results.appendChild(el('p', 'cf-search-hint',
                'Could not search right now. Please try again.'));
            return;
        }
        if (!users.length) {
            results.appendChild(el('p', 'cf-search-hint', 'No members found.'));
            return;
        }
        users.forEach(function (user) {
            var name = esc(user && user.username).trim();
            if (!name) return;
            var row = document.createElement('a');
            row.className = 'cf-search-result';
            row.href = '/community/u/' + encodeURIComponent(name);
            row.setAttribute('role', 'option');
            row.appendChild(el('span', 'cf-search-avatar', name[0].toUpperCase()));
            row.appendChild(el('span', 'cf-search-name', '@' + name));
            results.appendChild(row);
        });
    }

    function openSheet() {
        var sheet = $('cfSheetEarn');
        if (!sheet) return;
        var overlay = $('cfSheetOverlay');
        if (overlay) overlay.classList.add('open');
        sheet.classList.add('open');
        document.body.classList.add('cf-modal-open');
    }

    function closeSheet() {
        var sheet = $('cfSheetEarn');
        if (sheet) sheet.classList.remove('open');
        var overlay = $('cfSheetOverlay');
        if (overlay) overlay.classList.remove('open');
        document.body.classList.remove('cf-modal-open');
    }

    function sheetGo(href, message) {
        closeSheet();
        if (!href) return;
        // The sheet's destinations are all signed-in features, so a guest that
        // reached here (a stale DOM, a console call) still gets the connect card.
        if (!WALLET) { requiresLogin(); return; }
        if (message) toast(message, 'ok');
        setTimeout(function () { window.location.href = href; }, 180);
    }

    // ── Wiring ─────────────────────────────────────────────────────────────

    function bind() {
        var input = $('cfCaption');
        if (input) {
            input.addEventListener('input', function () {
                updateCounter();
                updatePostButton();
            });
            input.addEventListener('keydown', function (event) {
                if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
                    event.preventDefault();
                    submitPost();
                }
            });
        }
        var fileInput = $('cfFileInput');
        if (fileInput) {
            fileInput.addEventListener('change', function () {
                addFiles(fileInput.files);
                fileInput.value = '';
            });
        }
        // Enter saves the username, so the gate is one keystroke to clear.
        var usernameInput = $('cfUsernameInput');
        if (usernameInput) {
            usernameInput.addEventListener('keydown', function (event) {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    saveUsername();
                }
            });
        }
        // Paste an image straight into the composer.
        document.addEventListener('paste', function (event) {
            if (!event.clipboardData || !event.clipboardData.files.length) return;
            var target = event.target;
            if (!target || target.id !== 'cfCaption') return;
            addFiles(event.clipboardData.files);
        });
        // Drag & drop onto the composer.
        var composer = $('cfComposer');
        if (composer) {
            ['dragover', 'drop'].forEach(function (name) {
                composer.addEventListener(name, function (event) {
                    event.preventDefault();
                    if (name === 'drop' && event.dataTransfer && event.dataTransfer.files) {
                        addFiles(event.dataTransfer.files);
                    }
                });
            });
        }
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') { closeLightbox(); closeEdit(); closeSheet(); closeSearch(); }
        });
        updateCounter();
        updatePostButton();
    }

    function init() {
        // Guests read the feed too — reading is public, only writing is gated.
        // The composer is absent from a guest's DOM, so bind()'s null-checks
        // make that half a no-op; the feed wiring is what matters here.
        bind();
        renderNav();
        loadFeed(true);
        if (window.location.hash && /^#post-\d+$/.test(window.location.hash)) {
            var id = window.location.hash.replace('#post-', '');
            var post = findPost(id);
            if (post) {
                var card = document.querySelector('[data-post-id="' + id + '"]');
                if (card && card.scrollIntoView) card.scrollIntoView({ block: 'center' });
            }
        }
    }

    window.GMCommunity = {
        init: init,
        refresh: refresh,
        loadMore: loadMore,
        submitPost: submitPost,
        react: react,
        toggleComments: toggleComments,
        openLightbox: openLightbox,
        closeLightbox: closeLightbox,
        openEdit: openEdit,
        closeEdit: closeEdit,
        saveEdit: saveEdit,
        deletePost: deletePost,
        reportPost: reportPost,
        sharePost: sharePost,
        addFiles: addFiles,
        saveUsername: saveUsername,
        openSearch: openSearch,
        closeSearch: closeSearch,
        searchInput: searchInput,
        searchKeydown: searchKeydown,
        openSheet: openSheet,
        closeSheet: closeSheet,
        sheetGo: sheetGo,
        // Exposed for the test harness (drives the real bodies).
        _internals: {
            timeAgo: timeAgo,
            countLabel: countLabel,
            linkifyInto: linkifyInto,
            buildCard: buildCard,
            buildNavItem: buildNavItem,
            buildFollowButton: buildFollowButton,
            buildVideoFrame: buildVideoFrame,
            toggleFollow: toggleFollow,
            setFollowButton: setFollowButton,
            renderNav: renderNav,
            runSearch: runSearch,
            renderSearchResults: renderSearchResults,
            prepareImage: prepareImage,
            updatePostButton: updatePostButton,
            needsUsername: needsUsername,
            saveUsername: saveUsername,
            getUsername: function () { return USERNAME; },
            setUsername: function (value) { USERNAME = value || ''; },
            presign: presign,
            putToR2: putToR2,
            uploadAttachment: uploadAttachment,
            updateUploadStatus: updateUploadStatus,
            renderPreviews: renderPreviews,
            renderFeed: renderFeed,
            getFeed: function () { return _feed; },
            setFeed: function (rows) { _feed = rows; },
            getPending: function () { return _pending; },
            setPending: function (rows) { _pending = rows; },
            getNextCursor: function () { return _nextCursor; },
            setNextCursor: function (value) { _nextCursor = value; },
        },
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
