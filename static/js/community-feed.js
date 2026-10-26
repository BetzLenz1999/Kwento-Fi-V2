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
     server's number; a failure rolls the selected reaction and count back. */

(function () {
    'use strict';

    var BOOT = window.GM_COMMUNITY_BOOT || {};
    var WALLET = BOOT.wallet || '';
    var USERNAME = BOOT.username || '';
    var AVATAR_URL = BOOT.avatarUrl || '';
    var USERNAME_REQUIRED = BOOT.usernameRequired !== false;
    var REACTIONS = BOOT.reactions || {};
    var MAX_CAPTION = BOOT.maxCaption || 2200;
    var MAX_MEDIA = BOOT.maxMedia || 4;
    var MAX_IMAGE_MB = BOOT.maxImageMb || 10;
    var MAX_VIDEO_MB = BOOT.maxVideoMb || 100;
    var MAX_AVATAR_MB = BOOT.maxAvatarMb || 5;
    var STORAGE_READY = !!BOOT.storageReady;
    var FOCUS_POST_ID = String(BOOT.focusPostId || '');
    var FOCUS_POST = BOOT.focusPost || null;

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
    var _activeReactionPicker = null;
    var _commentsOpen = {};    // post_id -> true
    var _commentCache = {};    // post_id -> [comments]
    var _replyingTo = {};      // post_id -> comment/reply id
    var _collapsedReplies = {}; // post_id -> {comment_id: true}
    var _earnBusy = false;     // one in-flight rewards fetch at a time
    var _earnDirty = false;    // a refresh arrived while one was in flight
    var _earnPromise = null;   // callers can await the coalesced latest read
    var _dailyPrompt = null;
    var _promptModeId = null;
    var _promptFilterId = null;

    // ── Small helpers ──────────────────────────────────────────────────────

    function $(id) { return document.getElementById(id); }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function esc(text) { return String(text === undefined || text === null ? '' : text); }

    /* ── Avatars ──────────────────────────────────────────────────────────
       The response carries only the same-origin image path; it never exposes
       an R2 URL. A failed read falls back to the member's initial. */
    function avatarImageUrl(url, username) {
        var name = esc(username).trim().replace(/^@+/, '');
        if (!esc(url).trim() || !/^[A-Za-z0-9_]{3,24}$/.test(name)) return '';
        return '/community/api/profile/' + encodeURIComponent(name) + '/avatar';
    }

    function avatarNode(url, username, className) {
        var node = el('span', className || 'cf-avatar');
        node.setAttribute('aria-hidden', 'true');
        renderAvatarInto(node, url, username);
        return node;
    }

    function renderAvatarInto(node, url, username) {
        if (!node) return;
        node.textContent = '';
        var imageSrc = avatarImageUrl(url, username);
        if (imageSrc) {
            var img = document.createElement('img');
            img.className = 'cf-avatar-img';
            img.src = imageSrc;
            img.alt = '';
            img.loading = 'lazy';
            img.decoding = 'async';
            img.referrerPolicy = 'no-referrer';
            img.onerror = function () {
                node.textContent = (esc(username) || 'A')[0].toUpperCase();
            };
            node.appendChild(img);
            return;
        }
        node.textContent = (esc(username) || 'A')[0].toUpperCase();
    }

    /* ── Reaction summary ─────────────────────────────────────────────────
       The stacked emoji row a social feed shows above the caption: the
       reactions a post actually received, most-used first, then a trailing
       "+N" when more kinds were used than fit. Falls back to a single 👍 when
       the server sent no breakdown (an old row, or a table that was not yet
       migrated) so the summary never renders an empty gap.

       The breakdown is DECORATION. The number beside it is always
       `reaction_count`, the authoritative total — so a bounded breakdown scan
       can never make the headline count wrong. */
    var SUMMARY_EMOJI_MAX = 3;

    function summaryEmojiKeys(post) {
        var breakdown = post && post.reaction_breakdown;
        var keys = [];
        if (breakdown && typeof breakdown === 'object') {
            Object.keys(breakdown).forEach(function (key) {
                var count = Number(breakdown[key]) || 0;
                if (count > 0 && REACTIONS[key]) keys.push(key);
            });
        }
        // Most-used first; ties keep REACTIONS order, which is stable.
        keys.sort(function (a, b) {
            var diff = (Number(breakdown[b]) || 0) - (Number(breakdown[a]) || 0);
            return diff !== 0 ? diff
                : Object.keys(REACTIONS).indexOf(a) - Object.keys(REACTIONS).indexOf(b);
        });
        // The viewer's own reaction always leads: it is the one they care
        // about, and a post with a single reaction must show that reaction
        // rather than a stranger's more popular one.
        var mine = post && post.my_reaction;
        if (mine && REACTIONS[mine]) {
            keys = keys.filter(function (key) { return key !== mine; });
            keys.unshift(mine);
        }
        if (!keys.length) keys.push('like');
        return keys;
    }

    function buildSummaryEmojis(post) {
        var wrap = el('span', 'cf-summary-emojis');
        wrap.setAttribute('aria-hidden', 'true');
        var keys = summaryEmojiKeys(post);
        keys.slice(0, SUMMARY_EMOJI_MAX).forEach(function (key) {
            wrap.appendChild(el('span', 'cf-summary-emoji', REACTIONS[key]));
        });
        var extra = keys.length - SUMMARY_EMOJI_MAX;
        if (extra > 0) wrap.appendChild(el('span', 'cf-summary-more', '+' + extra));
        return wrap;
    }

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

    function timeTitle(iso) {
        if (!iso) return '';
        var then = new Date(iso);
        if (isNaN(then.getTime())) return '';
        return then.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
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
            var raw = match[0];
            var href = raw;
            var trailing = '';
            // Sentence punctuation should remain text, not become part of the
            // destination. Keep this conservative so valid URL path/query
            // characters are not rewritten.
            while (/[.,!?;:)\]]$/.test(href)) {
                trailing = href.slice(-1) + trailing;
                href = href.slice(0, -1);
            }
            var link = document.createElement('a');
            link.href = href;
            link.target = '_blank';
            link.rel = 'noopener noreferrer nofollow';
            link.className = 'cf-inline-link';
            link.textContent = href;
            link.onclick = function (event) { event.stopPropagation(); };
            node.appendChild(link);
            if (trailing) node.appendChild(document.createTextNode(trailing));
            last = match.index + raw.length;
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

    // ── Profile picture (avatar) ───────────────────────────────────────────
    // The upload + modal live in the SHARED `community-avatar.js` (loaded by
    // both this page and the public profile, so the picture has one owner).
    // This bundle only RENDERS avatars and repaints its own cards when the
    // shared module reports a change.

    function onAvatarChanged(url) {
        AVATAR_URL = url || '';
        // Repaint the viewer's own cards so the new picture shows without a
        // full feed refetch. Other members' cards are untouched.
        _feed.forEach(function (post) {
            if (post.is_mine) post.avatar_url = AVATAR_URL || null;
        });
        renderFeed();
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
        // The post is published, so there is nothing left to protect: fold the
        // composer back to its resting row.
        setComposerCollapsed(true);
    }

    function setDailyPrompt(prompt) { _dailyPrompt = prompt || null; }

    function answerPrompt() {
        if (!_dailyPrompt || !_dailyPrompt.id) return;
        if (!WALLET) { requiresLogin(); return; }
        if (needsUsername()) {
            focusUsernameInput();
            toast('Please choose a username before answering.', 'error');
            return;
        }
        if (_dailyPrompt.has_responded) {
            toast('You have already answered today’s prompt.', 'error');
            return;
        }
        _promptModeId = String(_dailyPrompt.id);
        var context = $('cfPromptContext');
        var contextText = $('cfPromptContextText');
        var input = $('cfCaption');
        if (context) context.hidden = false;
        if (contextText) contextText.textContent = 'Answering: ' + (_dailyPrompt.title || 'today’s prompt');
        if (input) {
            input.placeholder = _dailyPrompt.composer_placeholder || 'Write your response to today’s prompt…';
            input.focus();
            input.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }

    function cancelPromptMode() {
        _promptModeId = null;
        var context = $('cfPromptContext');
        var input = $('cfCaption');
        if (context) context.hidden = true;
        if (input) input.placeholder = 'What happened today?';
    }

    function seePromptResponses() {
        if (!_dailyPrompt || !_dailyPrompt.id) return;
        _promptFilterId = _promptFilterId === String(_dailyPrompt.id)
            ? null : String(_dailyPrompt.id);
        var button = $('cfSeePromptBtn');
        if (button) button.textContent = _promptFilterId ? 'Show all posts' : 'See responses';
        loadFeed(true);
        var feed = $('cfFeed');
        if (feed) feed.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function prefillDailyLearnReflection() {
        if (!WALLET) { requiresLogin(); return; }
        // Sharing a learning reflection is not an official Daily Prompt
        // response, even if the member was previously composing one.
        cancelPromptMode();
        var input = $('cfCaption');
        if (!input) { toast('Open the Community feed to share your result.', 'error'); return; }
        var reflection = 'I completed today’s Daily Learn. One thing I’ll remember: ';
        input.value = input.value.trim()
            ? input.value.trimEnd() + '\n\n' + reflection
            : reflection;
        updateCounter();
        updatePostButton();
        input.focus();
        input.scrollIntoView({ behavior: 'smooth', block: 'center' });
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
            var postBody = { caption: caption, media: media };
            if (_promptModeId) postBody.prompt_id = _promptModeId;
            return api('/community/api/posts', {
                method: 'POST',
                body: JSON.stringify(postBody),
            });
        }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not publish your post.');
            }
            _posting = false;
            setUploadStatus('');
            resetComposer();
            cancelPromptMode();
            // Prepend from the local object — no refetch needed.
            _feed.unshift(result.data.post);
            renderFeed();
            window.dispatchEvent(new CustomEvent('gm:community-post-created', {
                detail: { prompt_id: result.data.post && result.data.post.prompt_id }
            }));
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

    /* ── Recent posters rail ───────────────────────────────────────────────
       Instagram's story rail, honestly labelled: this app has no 24-hour story
       model, so calling it "Stories" would promise something the data cannot
       honour. It is built from the authors ALREADY on this feed page — no new
       endpoint, no new table — so it can never show a member who has not posted
       and it costs no extra request.

       Rows are addressed by USERNAME (never a wallet) and link to the same
       public profile the cards do. A guest gets the rail too: reading is
       public. */
    var RAIL_MAX = 12;

    function railAuthors() {
        var seen = {};
        var out = [];
        _feed.forEach(function (post) {
            var name = esc(post && post.username).trim().replace(/^@+/, '');
            if (!name) return;
            var key = name.toLowerCase();
            if (seen[key]) return;
            seen[key] = true;
            out.push({
                username: name,
                avatar_url: post.avatar_url,
                is_self: !!post.is_mine,
            });
        });
        // Your own row leads, the way Instagram pins "Your story" first.
        out.sort(function (a, b) {
            return (b.is_self ? 1 : 0) - (a.is_self ? 1 : 0);
        });
        return out.slice(0, RAIL_MAX);
    }

    function buildRailItem(entry) {
        var isSelf = entry.is_self;
        var node;
        if (isSelf) {
            // Your own row is the composer entry point, not a profile link.
            node = el('button', 'cf-rail-item is-self');
            node.type = 'button';
            node.setAttribute('aria-label', 'Create a post');
            node.onclick = function () {
                var input = $('cfCaption');
                if (input && input.focus) input.focus();
                var composer = $('cfComposer');
                if (composer && composer.scrollIntoView) composer.scrollIntoView({ block: 'start' });
            };
        } else {
            node = el('a', 'cf-rail-item');
            node.href = '/community/u/' + encodeURIComponent(entry.username);
            node.setAttribute('aria-label', 'View @' + entry.username + "'s profile");
        }
        var ring = el('span', 'cf-rail-ring');
        ring.appendChild(avatarNode(entry.avatar_url, entry.username, 'cf-avatar'));
        node.appendChild(ring);
        node.appendChild(el('span', 'cf-rail-name',
            isSelf ? 'You' : entry.username));
        return node;
    }

    function buildRail() {
        var rail = $('cfRail');
        if (!rail) return;
        var heading = $('cfDailyHeading');
        rail.textContent = '';
        var authors = railAuthors();
        if (!authors.length) {
            rail.hidden = true;
            if (heading) heading.hidden = true;
            return;
        }
        rail.hidden = false;
        if (heading) heading.hidden = false;
        authors.forEach(function (entry) {
            rail.appendChild(buildRailItem(entry));
        });
    }

    function renderFeed() {
        var container = $('cfFeed');
        if (!container) return;
        container.textContent = '';
        container.setAttribute('aria-busy', 'false');
        buildRail();

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

    function focusCanonicalPost() {
        var id = FOCUS_POST_ID;
        if (!id && window.location.hash && /^#post-\d+$/.test(window.location.hash)) {
            id = window.location.hash.replace('#post-', '');
        }
        if (!id) return;
        var card = document.querySelector('[data-post-id="' + id + '"]');
        if (!card) return;
        card.classList.add('is-focus');
        window.setTimeout(function () {
            if (card && card.scrollIntoView) card.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }, 40);
        window.setTimeout(function () { card.classList.remove('is-focus'); }, 2400);
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
        var videoLabel = 'Video shared by ' + (username || 'this member');
        frame.setAttribute('aria-label', 'Open ' + videoLabel);
        frame.title = 'Open video';
        // Match the frame to the clip when the uploader reported its size, so
        // there are no letterbox bars; fall back to the IG portrait default.
        if (item.w && item.h) {
            frame.style.aspectRatio = item.w + ' / ' + item.h;
        }
        var video = document.createElement('video');
        video.controls = true;
        video.preload = 'metadata';
        video.playsInline = true;
        video.muted = true;
        video.setAttribute('aria-label', videoLabel);
        if (item.poster) video.poster = item.poster;
        video.src = item.url;
        video.addEventListener('loadeddata', function () {
            frame.classList.remove('is-loading');
            frame.classList.remove('is-error');
        });
        video.addEventListener('error', function () {
            frame.classList.remove('is-loading');
            frame.classList.add('is-error');
            frame.setAttribute('aria-label', 'Video unavailable from ' + (username || 'this member'));
        });
        frame.classList.add('is-loading');
        // The controls need the taps; the frame handles the rest.
        video.onclick = function (event) { event.stopPropagation(); };
        frame.appendChild(video);
        frame.onclick = function () { openLightbox(item.url, 'video'); };
        return frame;
    }

    /* ── Double-tap to like ────────────────────────────────────────────────
       Instagram's primary gesture. A single tap still opens the lightbox
       (genuinely useful here — it is how a member reads a small photo), so the
       two have to be told apart: a tap arms a short timer, and a second tap
       inside it cancels the lightbox and likes the post instead.

       `dblclick` is wired too, because a mouse never fires the two synthetic
       taps this timer counts — without it the gesture would be phone-only. */
    var DBL_TAP_MS = 300;
    var _lastTap = { id: null, at: 0 };

    function heartBurst(mediaWrap) {
        if (!mediaWrap) return;
        var burst = el('span', 'cf-burst', '❤️');
        burst.setAttribute('aria-hidden', 'true');
        mediaWrap.appendChild(burst);
        // Remove it after the animation so a re-render is not needed and the
        // node cannot pile up on repeated taps.
        setTimeout(function () {
            if (burst.parentNode) burst.parentNode.removeChild(burst);
        }, 900);
    }

    function closeReactionPicker(returnFocus) {
        if (!_activeReactionPicker) return;
        var active = _activeReactionPicker;
        active.picker.hidden = true;
        active.trigger.setAttribute('aria-expanded', 'false');
        _activeReactionPicker = null;
        if (returnFocus && active.trigger.focus) active.trigger.focus();
    }

    function openReactionPicker(trigger, picker, post, focusFirst) {
        if (_activeReactionPicker
                && _activeReactionPicker.picker !== picker) {
            closeReactionPicker(false);
        }
        picker.hidden = false;
        trigger.setAttribute('aria-expanded', 'true');
        _activeReactionPicker = { trigger: trigger, picker: picker, postId: post.id };
        if (focusFirst) {
            var first = picker.querySelector('.cf-chip');
            if (first && first.focus) first.focus();
        }
    }

    // `onSingle` runs when the tap turned out NOT to be a double tap.
    function onMediaTap(post, mediaWrap, url, kind, onSingle) {
        var now = Date.now();
        var isDouble = _lastTap.id === post.id && (now - _lastTap.at) < DBL_TAP_MS;
        _lastTap = { id: post.id, at: isDouble ? 0 : now };
        if (!isDouble) {
            setTimeout(function () {
                if (_lastTap.at === now) onSingle();
            }, DBL_TAP_MS);
            return;
        }
        // Double tap: like (never un-like — Instagram only ever adds here), and
        // show the burst. A guest gets the connect card through react().
        if (post.my_reaction !== 'like') react(post, 'like');
        heartBurst(mediaWrap);
    }

    /* Action row: heart · comment · share, with report pushed to the right.
       Tapping the heart toggles ❤️; holding it opens the existing reaction
       choices. Every choice reuses react() and the same optimistic API path. */
    function buildActionRow(post, body) {
        var actions = el('div', 'cf-card-actions');

        var hearted = post.my_reaction === 'love';
        var reactionControl = el('div', 'cf-reaction-control');
        var heartBtn = el('button', 'cf-action-btn cf-reaction-trigger'
            + (hearted ? ' is-liked' : ''));
        heartBtn.type = 'button';
        heartBtn.appendChild(el('span', 'cf-act-icon', hearted ? '❤️' : '🤍'));
        heartBtn.setAttribute('aria-pressed', hearted ? 'true' : 'false');
        heartBtn.setAttribute('aria-expanded', 'false');
        heartBtn.setAttribute('aria-controls', 'cf-reaction-picker-' + post.id);
        heartBtn.setAttribute('aria-keyshortcuts', 'ArrowDown');
        heartBtn.setAttribute('aria-label', hearted
            ? 'Remove heart reaction; press and hold or press ArrowDown to choose another reaction'
            : 'React with love; press and hold or press ArrowDown to choose a reaction');
        heartBtn.title = 'Tap for a heart; press and hold or press ArrowDown to choose a reaction';

        var picker = el('div', 'cf-reaction-picker');
        picker.id = 'cf-reaction-picker-' + post.id;
        picker.hidden = true;
        picker.setAttribute('role', 'group');
        picker.setAttribute('aria-label', 'Choose a reaction');
        Object.keys(REACTIONS).forEach(function (key) {
            var option = el('button', 'cf-chip', REACTIONS[key]);
            option.type = 'button';
            option.setAttribute('aria-label', 'React with ' + key);
            option.setAttribute('aria-pressed', post.my_reaction === key ? 'true' : 'false');
            option.onclick = function () {
                closeReactionPicker(false);
                react(post, key);
            };
            picker.appendChild(option);
        });

        var holdTimer = null;
        var holdOpened = false;
        var suppressNextClick = false;
        var startX = 0;
        var startY = 0;
        function cancelHoldTimer() {
            if (holdTimer !== null) {
                clearTimeout(holdTimer);
                holdTimer = null;
            }
        }
        heartBtn.addEventListener('pointerdown', function (event) {
            if (event.pointerType === 'mouse' && event.button !== 0) return;
            cancelHoldTimer();
            suppressNextClick = false;
            holdOpened = false;
            startX = Number(event.clientX) || 0;
            startY = Number(event.clientY) || 0;
            holdTimer = setTimeout(function () {
                holdTimer = null;
                holdOpened = true;
                suppressNextClick = true;
                openReactionPicker(heartBtn, picker, post, false);
            }, 400);
        });
        heartBtn.addEventListener('pointermove', function (event) {
            if (holdTimer === null) return;
            var movedX = Math.abs((Number(event.clientX) || 0) - startX);
            var movedY = Math.abs((Number(event.clientY) || 0) - startY);
            if (movedX > 12 || movedY > 12) {
                cancelHoldTimer();
                suppressNextClick = true;
            }
        });
        heartBtn.addEventListener('pointerleave', function () {
            if (holdTimer === null) return;
            cancelHoldTimer();
            suppressNextClick = true;
        });
        heartBtn.addEventListener('pointerup', cancelHoldTimer);
        heartBtn.addEventListener('pointercancel', function () {
            cancelHoldTimer();
            if (holdOpened) closeReactionPicker(false);
            holdOpened = false;
            suppressNextClick = true;
        });
        heartBtn.oncontextmenu = function (event) {
            if (event && event.preventDefault) event.preventDefault();
            cancelHoldTimer();
            holdOpened = true;
            suppressNextClick = true;
            openReactionPicker(heartBtn, picker, post, false);
            return false;
        };
        heartBtn.addEventListener('keydown', function (event) {
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                openReactionPicker(heartBtn, picker, post, true);
            }
        });
        heartBtn.onclick = function (event) {
            if (suppressNextClick) {
                suppressNextClick = false;
                holdOpened = false;
                if (event && event.preventDefault) event.preventDefault();
                return;
            }
            closeReactionPicker(false);
            react(post, hearted ? null : 'love');
        };
        reactionControl.appendChild(heartBtn);
        reactionControl.appendChild(picker);
        actions.appendChild(reactionControl);

        var commentBtn = el('button', 'cf-action-btn');
        commentBtn.type = 'button';
        commentBtn.classList.add('cf-comment-action');
        commentBtn.appendChild(el('span', 'cf-act-icon', '💬'));
        commentBtn.appendChild(el('span', null, 'Comment'));
        commentBtn.setAttribute('aria-label', 'View comments' + (post.comment_count ? ' (' + post.comment_count + ')' : ''));
        commentBtn.setAttribute('aria-controls', 'cf-comments-' + post.id);
        commentBtn.setAttribute('aria-expanded', 'false');
        // Reading a post is public; joining the thread is not. A guest gets the
        // shared connect card instead of an empty comment box.
        commentBtn.onclick = function () {
            if (!WALLET) { requiresLogin(); return; }
            var isOpen = !!body.querySelector('.cf-comments');
            commentBtn.setAttribute('aria-expanded', isOpen ? 'false' : 'true');
            toggleComments(post, body);
        };
        actions.appendChild(commentBtn);

        var shareBtn = el('button', 'cf-action-btn');
        shareBtn.type = 'button';
        shareBtn.classList.add('cf-share-action');
        shareBtn.appendChild(el('span', 'cf-act-icon', '↗'));
        shareBtn.appendChild(el('span', null, 'Share'));
        shareBtn.setAttribute('aria-label', 'Share post');
        shareBtn.onclick = function () { sharePost(post); };
        actions.appendChild(shareBtn);

        // Report stays in the row rather than moving behind `⋯`: it is the
        // moderation path the guest-gate harness drives through this exact
        // button, and it is the only report affordance a visitor has.
        if (!post.is_mine) {
            var reportBtn = el('button', 'cf-action-btn');
            reportBtn.type = 'button';
            reportBtn.appendChild(el('span', 'cf-act-icon', '⚑'));
            reportBtn.appendChild(el('span', null, 'Report'));
            reportBtn.onclick = function () { reportPost(post); };
            actions.appendChild(reportBtn);
        }
        return actions;
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
        // Facebook-style circular picture: the member's uploaded photo when
        // there is one, their initial otherwise. `cf-avatar-ring` draws the
        // gradient ring around it.
        profile.appendChild(avatarNode(post.avatar_url, post.username,
            'cf-avatar cf-avatar-ring'));
        var who = el('div', 'cf-card-who');
        who.appendChild(el('strong', null, post.username));
        var stamp = timeAgo(post.created_at) + (post.is_edited ? ' · edited' : '');
        var time = el('time', null, stamp || 'Posted');
        time.className = 'cf-post-time';
        if (post.created_at) {
            time.dateTime = post.created_at;
            time.setAttribute('datetime', post.created_at);
            var exactTime = timeTitle(post.created_at);
            if (exactTime) time.title = exactTime;
        }
        who.appendChild(time);
        var earnedBadges = Array.isArray(post.badges) ? post.badges.slice(0, 3) : [];
        if (earnedBadges.length) {
            var badgeRow = el('span', 'cf-badge-row');
            var badgeIcons = {
                first_post: '✍', first_reaction: '♥', community_voice: '🎤',
                helpful_member: '✦', consistent_contributor: '✓',
                community_builder: '◈', daily_learner: '▣'
            };
            earnedBadges.forEach(function (badge) {
                if (!badge || !badge.name) return;
                var chip = el('span', 'cf-badge-chip');
                chip.title = badge.description || badge.name;
                chip.setAttribute('aria-label', badge.name + ': ' + (badge.description || 'Community badge'));
                chip.appendChild(el('span', 'cf-badge-icon', badgeIcons[badge.key] || '✦'));
                chip.appendChild(el('span', 'cf-badge-name', badge.name));
                badgeRow.appendChild(chip);
            });
            if (badgeRow.childNodes.length) who.appendChild(badgeRow);
        }
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

        // Media. The wrap is kept in scope: the double-tap burst is appended to
        // it, and the tap handler needs it to place the heart over the photo.
        var mediaWrap = null;
        if (post.media && post.media.length) {
            var photoGridCount = post.media.length >= 2 && post.media.length <= 4
                && post.media.every(function (item) {
                    return item && item.type !== 'video';
                }) ? post.media.length : 0;
            mediaWrap = el('div', 'cf-media' + (photoGridCount
                ? ' cf-media-grid cf-media-grid-' + photoGridCount : ''));
            post.media.forEach(function (item) {
                if (item.type === 'video') {
                    mediaWrap.appendChild(buildVideoFrame(item, post.username));
                } else {
                    var img = document.createElement('img');
                    img.loading = 'lazy';
                    img.decoding = 'async';
                    img.referrerPolicy = 'no-referrer';
                    img.className = 'cf-media-image is-loading';
                    img.src = item.url;
                    img.alt = 'Photo shared by ' + (post.username || 'this member');
                    img.onload = function () { img.classList.remove('is-loading'); };
                    img.onerror = function () {
                        img.classList.remove('is-loading');
                        img.classList.add('is-error');
                        img.alt = 'Photo unavailable from ' + (post.username || 'this member');
                    };
                    // Gallery thumbnails use a fixed square crop; the tap still
                    // opens the original image. Single/mixed posts keep their
                    // existing source-ratio or unsized-image behavior.
                    if (!photoGridCount) {
                        if (item.w && item.h) {
                            img.style.aspectRatio = item.w + ' / ' + item.h;
                        } else {
                            img.classList.add('cf-media-raw');
                        }
                    }
                    img.onclick = function () {
                        onMediaTap(post, mediaWrap, item.url, 'image',
                            function () { openLightbox(item.url); });
                    };
                    img.ondblclick = function () { onMediaTap(post, mediaWrap, item.url, 'image', function () {}); };
                    mediaWrap.appendChild(img);
                }
            });
            card.appendChild(mediaWrap);
        }

        var body = el('div', 'cf-card-body');

        // Caption — Instagram's shape: the author in BOLD, then the text, on the
        // same line. The author is a real link to the public profile.
        if (post.body) {
            var caption = el('p', 'cf-caption');
            var author = el('a', 'cf-caption-author', post.username);
            author.href = '/community/u/' + encodeURIComponent(
                String(post.username || '').replace(/^@/, ''));
            author.setAttribute('aria-label', 'View @' + post.username + "'s profile");
            caption.appendChild(author);
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

        // Reaction + comment counts are post metadata and sit ABOVE the action
        // row, keeping the separate engagement controls directly below them.
        var summary = el('div', 'cf-summary');
        var left = el('div', 'cf-summary-left');
        left.appendChild(buildSummaryEmojis(post));
        var countNode = el('span', 'cf-count', countLabel(post.reaction_count) || '0');
        // Announce the change without re-reading the whole card.
        countNode.setAttribute('aria-live', 'polite');
        left.appendChild(countNode);
        summary.appendChild(left);
        summary.appendChild(el('span', null, countLabel(post.comment_count)
            ? countLabel(post.comment_count) + ' comment' + (post.comment_count === 1 ? '' : 's')
            : ''));
        body.appendChild(summary);

        // The comment button appends its expanded thread into this same group.
        var engagement = el('div', 'cf-engagement');
        engagement.appendChild(buildActionRow(post, engagement));
        body.appendChild(engagement);

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
        if (_activeReactionPicker
                && String(_activeReactionPicker.postId) === String(post.id)) {
            closeReactionPicker(false);
        }
        if (!card || !card.parentNode) { renderFeed(); return; }
        card.parentNode.replaceChild(buildCard(post), card);
    }

    // ── Reactions ──────────────────────────────────────────────────────────

    function react(post, key) {
        if (!WALLET) { requiresLogin(); return; }
        var previous = post.my_reaction;
        // A null/undefined key is an explicit REMOVAL, not a no-op: the heart
        // calls `react(post, null)` to remove love, and it must take the DELETE
        // path rather than POST a reaction of `null`.
        if (key === null || key === undefined) {
            if (!previous) return;
            key = previous;
        }
        if (_reactionInFlight[post.id]) return;   // a fast double-tap fires once
        _reactionInFlight[post.id] = true;

        var previousCount = Number(post.reaction_count) || 0;
        var previousBreakdown = cloneBreakdown(post.reaction_breakdown);

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
        // Keep the stacked summary in step with the optimistic count so the
        // emoji row does not jump when the server's tally arrives.
        post.reaction_breakdown = optimisticBreakdown(
            previousBreakdown, previous, post.my_reaction);
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
            // Reconcile with the authoritative count and the server's tally.
            post.reaction_count = Number(result.data.reaction_count);
            post.my_reaction = result.data.my_reaction;
            if (result.data.reaction_breakdown) {
                post.reaction_breakdown = result.data.reaction_breakdown;
            }
            rerenderPost(post);
            loadRewards();
        }).catch(function (error) {
            // Roll back the chip, the count and the summary together.
            post.my_reaction = previous;
            post.reaction_count = previousCount;
            post.reaction_breakdown = previousBreakdown;
            rerenderPost(post);
            toast((error && error.message) || 'Could not save your reaction.', 'error');
        }).then(function () {
            _reactionInFlight[post.id] = false;
        });
    }

    function cloneBreakdown(raw) {
        var out = {};
        if (raw && typeof raw === 'object') {
            Object.keys(raw).forEach(function (key) {
                var count = Number(raw[key]) || 0;
                if (count > 0) out[key] = count;
            });
        }
        return out;
    }

    // Move one reaction from `from` to `to` without touching the headline
    // count: a switch is a decrement plus an increment, an add is an increment,
    // and removing is a decrement. `to` is null when the viewer un-reacted.
    function optimisticBreakdown(breakdown, from, to) {
        var out = cloneBreakdown(breakdown);
        if (from && out[from]) {
            out[from] -= 1;
            if (out[from] <= 0) delete out[from];
        }
        if (to) out[to] = (out[to] || 0) + 1;
        return out;
    }

    // ── Comments ───────────────────────────────────────────────────────────

    function toggleComments(post, body) {
        var existing = body.querySelector('.cf-comments');
        if (existing) {
            existing.remove();
            delete _commentsOpen[post.id];
            delete _replyingTo[post.id];
            return;
        }
        _commentsOpen[post.id] = true;

        var wrap = el('div', 'cf-comments');
        wrap.id = 'cf-comments-' + post.id;
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
        var byId = {};
        var children = {};
        var roots = [];
        comments.forEach(function (comment) { byId[String(comment.id)] = comment; });
        comments.forEach(function (comment) {
            var parentId = comment.parent_comment_id == null
                ? '' : String(comment.parent_comment_id);
            if (parentId && byId[parentId]) {
                if (!children[parentId]) children[parentId] = [];
                children[parentId].push(comment);
            } else {
                roots.push(comment);
            }
        });
        roots.sort(function (a, b) { return Number(a.id) - Number(b.id); });
        var thread = el('div', 'cf-comment-thread');
        roots.forEach(function (comment) {
            thread.appendChild(renderCommentNode(post, comment, children, 0, wrap));
        });
        wrap.appendChild(thread);

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
        submit.onclick = function () { addComment(post, input, submit, wrap, null); };
        form.appendChild(input);
        form.appendChild(submit);
        wrap.appendChild(form);
    }

    function renderCommentNode(post, comment, children, depth, wrap) {
        var node = el('div', 'cf-comment-node');
        node.setAttribute('data-comment-id', String(comment.id));
        var row = el('div', 'cf-comment');
        if (!comment.is_deleted) {
            row.appendChild(avatarNode(comment.avatar_url, comment.username,
                'cf-comment-avatar'));
        } else {
            row.appendChild(el('span', 'cf-comment-avatar cf-comment-deleted-avatar', '…'));
        }
        var bubble = el('div', 'cf-comment-body');
        var authorLine = el('div', 'cf-comment-author-line');
        authorLine.appendChild(el('strong', null, comment.username));
        if (comment.is_post_author) authorLine.appendChild(el('span', 'cf-author-badge', 'Author'));
        bubble.appendChild(authorLine);
        if (comment.reply_to_username) {
            bubble.appendChild(el('span', 'cf-reply-mention', '@' + comment.reply_to_username));
            bubble.appendChild(document.createTextNode(' '));
        }
        bubble.appendChild(document.createTextNode(comment.body || ''));
        row.appendChild(bubble);

        var actions = el('div', 'cf-comment-actions');
        if (WALLET && !comment.is_deleted) {
            var reply = el('button', 'cf-comment-action', 'Reply');
            reply.type = 'button';
            reply.setAttribute('aria-label', 'Reply to @' + (comment.username || 'member'));
            reply.onclick = function () {
                _replyingTo[post.id] = String(comment.id);
                renderComments(post, wrap);
            };
            actions.appendChild(reply);
        }
        var replies = children[String(comment.id)] || [];
        var collapsed = !!((_collapsedReplies[post.id] || {})[String(comment.id)]);
        if (replies.length) {
            var toggle = el('button', 'cf-comment-action cf-replies-toggle',
                replies.length + (replies.length === 1 ? ' reply · ' : ' replies · ') +
                (collapsed ? 'Expand' : 'Collapse'));
            toggle.type = 'button';
            toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            toggle.onclick = function () {
                if (!_collapsedReplies[post.id]) _collapsedReplies[post.id] = {};
                _collapsedReplies[post.id][String(comment.id)] = !collapsed;
                renderComments(post, wrap);
            };
            actions.appendChild(toggle);
        }
        if (comment.is_mine && !comment.is_deleted) {
            var remove = el('button', 'cf-comment-action cf-comment-delete', 'Delete');
            remove.type = 'button';
            remove.setAttribute('aria-label', 'Delete comment');
            remove.onclick = function () { deleteComment(post, comment, wrap); };
            actions.appendChild(remove);
        }
        if (actions.children.length) row.appendChild(actions);
        node.appendChild(row);

        if (String(_replyingTo[post.id] || '') === String(comment.id)) {
            node.appendChild(buildReplyComposer(post, comment, wrap));
        }
        if (replies.length && !collapsed) {
            var nested = el('div', 'cf-comment-children');
            nested.setAttribute('data-depth', String(Math.min(depth + 1, 4)));
            replies.sort(function (a, b) { return Number(a.id) - Number(b.id); });
            replies.forEach(function (replyComment) {
                nested.appendChild(renderCommentNode(post, replyComment, children, depth + 1, wrap));
            });
            node.appendChild(nested);
        }
        return node;
    }

    function buildReplyComposer(post, parent, wrap) {
        var form = el('div', 'cf-comment-form cf-reply-form');
        form.appendChild(el('span', 'cf-replying-to', 'Replying to @' + (parent.username || 'member')));
        var input = document.createElement('input');
        input.type = 'text';
        input.placeholder = 'Write a reply…';
        input.maxLength = 500;
        input.setAttribute('aria-label', 'Reply to @' + (parent.username || 'member'));
        var cancel = el('button', 'cf-comment-cancel', 'Cancel');
        cancel.type = 'button';
        cancel.onclick = function () { delete _replyingTo[post.id]; renderComments(post, wrap); };
        var submit = el('button', 'cf-comment-submit', 'Reply');
        submit.type = 'button';
        submit.disabled = true;
        input.oninput = function () { submit.disabled = !input.value.trim(); };
        input.onkeydown = function (event) {
            if (event.key === 'Enter' && input.value.trim()) submit.click();
            if (event.key === 'Escape') cancel.click();
        };
        submit.onclick = function () { addComment(post, input, submit, wrap, parent.id); };
        form.appendChild(input);
        form.appendChild(cancel);
        form.appendChild(submit);
        return form;
    }

    function addComment(post, input, submit, wrap, parentCommentId) {
        var body = input.value.trim();
        if (!body) return;
        submit.disabled = true;
        var submitLabel = parentCommentId == null ? 'Send' : 'Reply';
        submit.textContent = '…';
        api('/community/api/posts/' + post.id + '/comments', {
            method: 'POST',
            body: JSON.stringify({ body: body, parent_comment_id: parentCommentId }),
        }).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not post your comment.');
            }
            input.value = '';
            _commentCache[post.id] = (_commentCache[post.id] || []).concat([result.data.comment]);
            if (parentCommentId == null) {
                post.comment_count = (Number(post.comment_count) || 0) + 1;
            }
            delete _replyingTo[post.id];
            renderComments(post, wrap);
            rerenderPost(post);
            loadRewards();
        }).catch(function (error) {
            toast((error && error.message) || 'Could not post your comment.', 'error');
        }).then(function () {
            submit.disabled = false;
            submit.textContent = submitLabel;
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
            loadRewards();
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
        var url = window.location.origin + '/community/p/' + encodeURIComponent(post.id);
        var text = post.body ? post.body.slice(0, 120) : 'Check out this post on GoodMarket Community';
        if (navigator.share) {
            navigator.share({
                title: '@' + (post.username || 'member') + ' on GoodMarket Community',
                text: text,
                url: url,
            }).catch(function () {});
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

    // ── Community G$ card ──────────────────────────────────────────────────
    // The balance lives on THIS page only (never the wallet page — see
    // docs/COMMUNITY_REWARDS_LEDGER_PROPOSAL.md §0). It is the SUM of an
    // append-only ledger, fetched from the server; the client never computes
    // or stores it. Refreshed after every earning action so the number moves
    // as the user reacts and comments.
    function renderRewards(data) {
        var card = $('cfEarn');
        if (!card) return;
        if (!data || !data.enabled) { card.hidden = true; return; }

        var amount = Number(data.balance_gd) || 0;
        var minimum = Number(data.min_payout_gd) || 0;
        var progress = Number(data.progress) || 0;
        // The server flag defaults on for signed-in members; the guest check
        // below still prevents personal balance details from being disclosed.
        var showBalance = data.show_balance === true;

        var amountEl = $('cfEarnAmount');
        if (amountEl) amountEl.textContent = formatGd(amount);

        // The lead line is ALWAYS visible, and carries the state — so the card
        // still tells the user what to do while the balance is hidden.
        var leadEl = $('cfEarnLead');
        if (leadEl) {
            if (data.is_guest) {
                leadEl.textContent = 'Sign in to earn G$ in the community.';
            } else if (data.eligible === false) {
                leadEl.textContent = 'Verify your identity to start earning G$.';
            } else if (showBalance && minimum > 0 && amount >= minimum) {
                leadEl.textContent = 'Your full balance may be ready to withdraw. The vault and identity checks run before signing.';
            } else {
                leadEl.textContent = 'Earn G$ from reactions, comments and the '
                    + 'daily quiz.';
            }
        }

        var goalEl = $('cfEarnGoal');
        if (goalEl) {
            if (data.is_guest) {
                goalEl.textContent = 'Sign in to earn G$ in the community.';
            } else if (data.eligible === false) {
                // Say WHY the balance is zero. A silent 0 reads as "rewards are
                // broken"; the real reason is that the wallet is not verified.
                goalEl.textContent = 'Verify your identity to start earning G$.';
            } else if (minimum > 0) {
                var remaining = Math.max(0, minimum - amount);
                goalEl.textContent = remaining > 0
                    ? formatGd(remaining) + ' G$ to go until ' + formatGd(minimum) + ' G$'
                    : formatGd(minimum) + ' G$ reached';
            }
        }

        var chip = $('cfEarnChip');
        if (chip) {
            // The chip is ALWAYS present: a bare number invites "I can send
            // this", which is false — it is not spendable and not on-chain.
            var state = data.is_guest ? 'guest'
                : (data.eligible === false ? 'unverified'
                    : (showBalance && minimum > 0 && amount >= minimum ? 'ready' : 'pending'));
            chip.setAttribute('data-state', state);
            chip.textContent = state === 'ready' ? 'Threshold reached'
                : (state === 'guest' ? 'Guest'
                    : (state === 'unverified' ? 'Verify to earn' : 'Pending to withdraw'));
        }

        // The rates come from the server payload, never hard-coded, so the page
        // cannot quote a rate the ledger does not pay. Keep them collapsed until
        // the accessible toggle is activated, preserving that choice on refresh.
        var ratesEl = $('cfEarnRates');
        var ratesToggle = $('cfEarnRatesToggle');
        if (ratesEl) {
            var rates = data.rates || null;
            if (rates) {
                var rows = [
                    ['Comment on a post', rates.comment_in],
                    ['Someone comments on your post', rates.comment_out],
                    ['Someone reacts to your post', rates.reaction_out],
                    ['You react to a post', rates.reaction_in],
                    ['Daily Learn (per correct answer)', rates.daily_learn_per_correct],
                ];
                ratesEl.textContent = '';
                rows.forEach(function (row) {
                    // Build the children explicitly: `el()`'s third argument is
                    // textContent, so passing a node would stringify it.
                    var li = el('li');
                    li.appendChild(el('span', 'cf-earn-rate-label', row[0]));
                    li.appendChild(el('span', 'cf-earn-rate-value',
                        '+' + formatGd(row[1]) + ' G$'));
                    ratesEl.appendChild(li);
                });
                if (ratesToggle) {
                    ratesToggle.hidden = false;
                    var expanded = ratesToggle.getAttribute('aria-expanded') === 'true';
                    ratesEl.hidden = !expanded;
                    ratesToggle.textContent = expanded
                        ? 'Hide earning rates' : 'Show earning rates';
                    ratesToggle.onclick = function () {
                        var next = ratesToggle.getAttribute('aria-expanded') !== 'true';
                        ratesToggle.setAttribute('aria-expanded', next ? 'true' : 'false');
                        ratesEl.hidden = !next;
                        ratesToggle.textContent = next
                            ? 'Hide earning rates' : 'Show earning rates';
                    };
                } else {
                    ratesEl.hidden = true;
                }
            } else {
                ratesEl.textContent = '';
                ratesEl.hidden = true;
                if (ratesToggle) {
                    ratesToggle.hidden = true;
                    ratesToggle.setAttribute('aria-expanded', 'false');
                    ratesToggle.textContent = 'Show earning rates';
                }
            }
        }

        var balanceWrap = $('cfEarnBalance');
        if (balanceWrap) balanceWrap.hidden = !showBalance || !!data.is_guest;

        var bar = $('cfEarnBar');
        var fill = $('cfEarnBarFill');
        if (bar) bar.setAttribute('aria-valuenow', String(Math.round(progress)));
        if (fill) fill.style.width = Math.max(0, Math.min(100, progress)) + '%';

        var note = $('cfEarnNote');
        if (note) {
            note.hidden = false;
            note.textContent = data.eligible === false
                ? 'Only currently verified wallets earn Community G$. You can still post, comment and react.'
                : (showBalance && minimum > 0 && amount >= minimum
                    ? 'Withdraw sends G$ directly to your logged-in wallet. You pay Celo gas; the ledger is debited only after the vault payout is confirmed.'
                    : 'Your Community G$ accrues in the ledger. The full balance becomes withdrawable after the minimum is reached and the vault is enabled.');
        }
        var withdrawRow = $('cfEarnWithdrawRow');
        var withdrawStatus = $('cfEarnWithdrawStatus');
        if (withdrawRow) {
            withdrawRow.hidden = !!data.is_guest || data.eligible === false || !showBalance;
        }
        if (withdrawStatus) withdrawStatus.hidden = true;
        card.hidden = false;
    }

    async function checkWithdrawalEligibility() {
        if (!WALLET) { requiresLogin(); return Promise.resolve(); }
        var button = $('cfEarnWithdrawBtn');
        var status = $('cfEarnWithdrawStatus');
        if (button) { button.disabled = true; button.textContent = 'Checking…'; }
        if (status) { status.hidden = true; status.textContent = ''; }
        var setStatus = function (message) {
            if (status) { status.textContent = message; status.hidden = false; }
        };
        var wait = function (ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); };
        var confirmTx = async function (withdrawalId, txHash) {
            var key = 'gm-community-withdrawal:' + String(WALLET).toLowerCase();
            try { localStorage.setItem(key, JSON.stringify({ withdrawal_id: withdrawalId, tx_hash: txHash })); } catch (_) {}
            for (var attempt = 0; attempt < 12; attempt += 1) {
                var check = await api('/community/api/rewards/withdrawals/confirm', {
                    method: 'POST',
                    body: JSON.stringify({ withdrawal_id: withdrawalId, tx_hash: txHash })
                });
                var confirmed = check && check.data;
                if (confirmed && confirmed.status === 'completed') {
                    try { localStorage.removeItem(key); } catch (_) {}
                    setStatus('Withdrawal confirmed. Your Community ledger balance is updating.');
                    await loadRewards();
                    return true;
                }
                if (confirmed && confirmed.status === 'failed') {
                    try { localStorage.removeItem(key); } catch (_) {}
                    throw new Error(confirmed.error || 'The withdrawal transaction reverted; your ledger balance was not debited.');
                }
                if (!check.ok && check.status !== 202) {
                    throw new Error((confirmed && confirmed.error) || 'Could not verify the transaction. Keep its hash and retry confirmation.');
                }
                setStatus(confirmed && confirmed.status === 'confirming'
                    ? 'Celo transaction confirmed; waiting for the required finality confirmations…'
                    : 'Withdrawal transaction submitted; waiting for Celo confirmation…');
                await wait(5000);
            }
            setStatus('The transaction is still awaiting final confirmation. Your request is reserved; click Withdraw again to resume safely.');
            return false;
        };

        try {
            var pendingKey = 'gm-community-withdrawal:' + String(WALLET).toLowerCase();
            var pending = null;
            try { pending = JSON.parse(localStorage.getItem(pendingKey) || 'null'); } catch (_) {}
            if (pending && pending.withdrawal_id && pending.tx_hash) {
                setStatus('Resuming confirmation for your submitted withdrawal…');
                await confirmTx(pending.withdrawal_id, pending.tx_hash);
                return;
            }

            var eligibility = await api('/community/api/rewards/withdrawal-eligibility');
            var ready = eligibility && eligibility.data;
            if (!eligibility.ok || !ready || !ready.success) {
                throw new Error((ready && ready.error) || 'Could not check withdrawal readiness.');
            }
            if (!ready.can_request) {
                setStatus(ready.message || 'This wallet is not ready to withdraw yet.');
                return;
            }
            var amount = String(ready.amount_gd || 'your full available balance');
            if (!window.confirm('Withdraw ' + amount + ' G$ to your logged-in wallet? You will pay Celo gas. Confirm the recipient, vault address and network in your wallet before signing.')) return;

            if (button) button.textContent = 'Preparing…';
            var authResult = await api('/community/api/rewards/withdrawals/authorize', {
                method: 'POST', body: '{}'
            });
            var authData = authResult && authResult.data;
            var withdrawalId = authData && authData.withdrawal_id;
            var txHash = authData && authData.tx_hash;
            if ((!authResult.ok || !authData || !authData.success)
                    && !(authData && authData.status === 'transaction_submitted' && txHash)) {
                throw new Error((authData && authData.error) || 'Could not issue the withdrawal authorization.');
            }
            if (txHash && withdrawalId && authData.status === 'transaction_submitted') {
                await confirmTx(withdrawalId, txHash);
                return;
            }
            if (button) button.textContent = 'Confirm in wallet…';
            if (!window.P2PWallet) throw new Error('Your wallet signing module is not ready. Refresh the page and retry.');
            if (window.GMEthersLoader && typeof window.GMEthersLoader.load === 'function') {
                await window.GMEthersLoader.load();
            }
            var boot = window.GM_COMMUNITY_BOOT || {};
            window.P2PWallet.configure({ wallet: WALLET, chainId: Number(boot.celoChainId || 42220), loginMethod: boot.loginMethod || '' });
            var auth = authData.authorization;
            if (!auth || !auth.recipient || !auth.withdrawalId || !auth.projectId || !authData.signature) {
                throw new Error('The server returned an incomplete withdrawal authorization. No transaction was sent.');
            }
            var expectedProjectId = '0xe4772aeb1ac8e78ecc6c0f9255e6c828ef90f0f411ba199aa090efd81271de5e';
            if (String(auth.recipient).toLowerCase() !== String(WALLET).toLowerCase()) {
                throw new Error('The authorization recipient does not match your logged-in wallet. No transaction was sent.');
            }
            if (String(auth.projectId).toLowerCase() !== expectedProjectId
                    || Number(authData.chain_id) !== 42220
                    || !authData.vault_address || !authData.token_address) {
                throw new Error('The authorization project, Celo network, or vault configuration did not match. No transaction was sent.');
            }
            var authDecimals = Number(authData.token_decimals);
            var authUnits = BigInt(String(auth.amount));
            var authScale = 10n ** BigInt(authDecimals);
            var authWhole = (authUnits / authScale).toString();
            var authFraction = (authUnits % authScale).toString().padStart(authDecimals, '0').replace(/0+$/, '');
            var authAmountGd = authWhole + (authFraction ? '.' + authFraction : '');
            if (!window.confirm('Final review before your wallet opens:\n\n'
                    + 'Amount: ' + authAmountGd + ' G$\n'
                    + 'Recipient (your wallet): ' + auth.recipient + '\n'
                    + 'GoodDollar token: ' + authData.token_address + '\n'
                    + 'Community vault: ' + authData.vault_address + '\n'
                    + 'Network: Celo Mainnet (42220)\n'
                    + 'Gas: paid by you\n\nContinue to your wallet?')) return;
            txHash = await window.P2PWallet.sendTx(authData.vault_address,
                'function withdraw((address recipient,uint256 amount,bytes32 withdrawalId,uint256 nonce,uint256 deadline,bytes32 projectId) authorization,bytes issuerAuthorization)',
                [[auth.recipient, String(auth.amount), auth.withdrawalId, String(auth.nonce), String(auth.deadline), auth.projectId], authData.signature]);
            if (!txHash) throw new Error('The wallet did not return a transaction hash. Check your wallet activity before retrying.');
            await confirmTx(withdrawalId, txHash);
        } catch (error) {
            setStatus((error && error.message) || 'Withdrawal could not be completed. Check your wallet activity before retrying.');
        } finally {
            if (button) { button.disabled = false; button.textContent = 'Withdraw'; }
        }
    }

    function formatGd(value) {
        var n = Number(value) || 0;
        // Whole numbers read cleanly; a fractional balance keeps one decimal.
        return n % 1 === 0 ? String(Math.round(n)) : n.toFixed(1);
    }

    function loadRewards() {
        // Coalesce rather than drop. A react fires a refresh while the boot
        // fetch may still be in flight; dropping it would leave the number
        // showing the pre-react balance, which reads as "my reaction did not
        // count". One extra pass after the current one settles is enough: the
        // server is the source of truth, so the latest response wins.
        if (_earnBusy) { _earnDirty = true; return _earnPromise || Promise.resolve(false); }
        _earnBusy = true;
        _earnPromise = api('/community/api/rewards').then(function (result) {
            var responseOk = !!(result.ok && result.data && result.data.success);
            var refreshed = !!(responseOk && result.data.enabled &&
                result.data.balance_available !== false);
            if (responseOk && (!result.data.enabled || result.data.balance_available !== false)) {
                renderRewards(result.data);
            }
            // A partial ledger read is not a fresh balance: keep the last good
            // card value instead of replacing it with a misleading zero.
            return refreshed;
        }).catch(function () {
            // A rewards read failure must not disturb the feed; the card just
            // stays as it was (hidden on first load).
            return false;
        }).then(function (refreshed) {
            _earnBusy = false;
            if (_earnDirty) { _earnDirty = false; return loadRewards(); }
            return refreshed;
        });
        return _earnPromise;
    }

    function loadFeed(reset) {
        if (_loading) return Promise.resolve();
        _loading = true;
        var url = '/community/api/feed?limit=10';
        if (_promptFilterId) url += '&prompt_id=' + encodeURIComponent(_promptFilterId);
        if (!reset && _nextCursor) url += '&cursor=' + encodeURIComponent(_nextCursor);

        return api(url).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not load the feed.');
            }
            _nextCursor = result.data.next_cursor || null;
            // Guard a malformed payload: a server bug must not break the page.
            var rows = result.data.posts || [];
            if (reset && FOCUS_POST && FOCUS_POST.id != null
                    && !rows.some(function (post) { return String(post.id) === String(FOCUS_POST.id); })) {
                rows = [FOCUS_POST].concat(rows);
            }
            _feed = reset ? rows : _feed.concat(rows);
            renderFeed();
            focusCanonicalPost();
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
        // `active` is data-driven: this bar only renders on /community, and
        // /community IS the feed, so no tab claims to be the current page any
        // more (the Feed tab that did is gone — it was a dead tap back to the
        // page you were already on). A future page reusing this bar marks its
        // own destination with `active: true`.
        node.className = 'cf-nav-item'
            + (item.active ? ' is-active' : '')
            + (item.gold ? ' cf-nav-gold' : '');
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
            if (item.action === 'messages') {
                if (event && event.preventDefault) event.preventDefault();
                if (!WALLET) { requiresLogin(); return; }
                window.location.href = '/messages';
                return;
            }
            if (item.action === 'airtime') {
                // Buy Airtime opens the Reloadly store in the in-page modal
                // instead of navigating: a member stays in the feed, exactly
                // like the wallet's Raffle / Daily Task dialogs. The href is
                // kept so the item is still a real link (middle-click, and a
                // no-JS fallback to the store page).
                if (event && event.preventDefault) event.preventDefault();
                if (!WALLET) { requiresLogin(); return; }
                openAirtime();
                return;
            }
            if (item.action === 'claim') {
                // Claim Free G$ opens the shared claim controller directly in
                // this page's DOM. The href remains the no-JS/middle-click
                // fallback, but the normal path no longer loads a wallet iframe.
                if (event && event.preventDefault) event.preventDefault();
                if (!WALLET) { requiresLogin(); return; }
                openClaim();
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
    function setMessageUnread(count) {
        var item = document.querySelector('[data-nav="messages"]');
        if (!item) return;
        var badge = item.querySelector('.cf-nav-unread');
        if (!badge) {
            badge = el('span', 'cf-nav-unread', '');
            item.appendChild(badge);
        }
        var total = Math.max(0, Number(count) || 0);
        badge.textContent = total > 99 ? '99+' : String(total);
        badge.hidden = total === 0;
        item.setAttribute('aria-label', total ? 'Messages, ' + total + ' unread' : 'Messages');
    }
    function loadMessageUnread() {
        if (!WALLET || !NAV.some(function (item) { return item.action === 'messages'; })) return;
        fetch('/api/messages/conversations', { credentials: 'same-origin' })
            .then(function (response) { return response.ok ? response.json() : null; })
            .then(function (data) { if (data && data.success) setMessageUnread(data.unread_total); })
            .catch(function () {});
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
        var input = $('cfSearchInput');
        // The `@` is drawn beside the field, so a typed or pasted one would
        // show `@@betz`. Drop it from the field itself; the search still works.
        if (input && /^\s*@/.test(input.value || '')) {
            input.value = String(input.value).replace(/^\s*@+/, '');
        }
        if (_searchTimer) clearTimeout(_searchTimer);
        _searchTimer = setTimeout(function () {
            runSearch(input ? input.value : '');
        }, 220);
    }

    function searchKeydown(event) {
        if (event && event.key === 'Escape') closeSearch();
    }

    function runSearch(term) {
        // The `@` is chrome (see the template), but a member may still paste or
        // type one. Strip it here so the request is the same either way — the
        // server matches a username with or without a stored `@`.
        var cleaned = String(term === undefined || term === null ? '' : term)
            .trim().replace(/^@+/, '');
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
            // Strip a stored `@` (a legacy row) — the row draws its own prefix.
            var name = esc(user && user.username).trim().replace(/^@+/, '');
            if (!name) return;
            var row = document.createElement('a');
            row.className = 'cf-search-result';
            row.href = '/community/u/' + encodeURIComponent(name);
            row.setAttribute('role', 'option');
            row.appendChild(avatarNode(user && user.avatar_url, name,
                'cf-search-avatar'));
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

    /* ── Buy Airtime modal ──────────────────────────────────────────────────
       The Reloadly store in an iframe, so a member never leaves the feed (the
       owner's ask: same in-place behaviour as the wallet's Raffle / Daily Task).
       `src` is copied from `data-src` on the FIRST open only, so the store's
       catalog scripts do not load for someone who never taps the button. The
       frame is left mounted on close — reloading it on every open would refetch
       the whole store, and the store keeps the member's country/operator choice
       while they read the feed. */
    var _airtimeOpened = false;

    function openAirtime() {
        var modal = $('cfAirtimeModal');
        if (!modal) return;
        var frame = $('cfAirtimeFrame');
        if (frame && !_airtimeOpened) {
            var src = frame.getAttribute('data-src');
            if (src) frame.src = src;
            _airtimeOpened = true;
        }
        modal.hidden = false;
        document.body.classList.add('cf-modal-open');
    }

    function closeAirtime() {
        var modal = $('cfAirtimeModal');
        if (modal) modal.hidden = true;
        // Only release the scroll lock when no other dialog wants it.
        var sheet = $('cfSheetEarn');
        var sheetOpen = sheet && sheet.classList.contains('open');
        if (!sheetOpen) document.body.classList.remove('cf-modal-open');
    }

    function openClaim() {
        if (typeof window.openClaimOrVerify === 'function') {
            window.openClaimOrVerify();
            return;
        }
        var modal = $('claimModal');
        if (!modal) return;
        modal.classList.add('open');
        modal.setAttribute('aria-hidden', 'false');
        document.body.classList.add('cf-modal-open');
    }

    function closeClaim() {
        if (typeof window.closeModal === 'function') {
            window.closeModal('claimModal');
            return;
        }
        var modal = $('claimModal');
        if (modal) {
            modal.classList.remove('open');
            modal.setAttribute('aria-hidden', 'true');
        }
        document.body.classList.remove('cf-modal-open');
    }

    // ── Wiring ─────────────────────────────────────────────────────────────

    // ── Collapsed composer ────────────────────────────────────────────────
    // The composer rests as one row and expands on an intent-to-post signal.
    // The collapse state is the `is-collapsed` class on #cfComposer — the CSS
    // and this code therefore share ONE source of truth and cannot disagree.

    function isComposerCollapsed() {
        var composer = $('cfComposer');
        return !!(composer && composer.classList.contains('is-collapsed'));
    }

    /* Collapsing with a typed caption or a queued attachment would silently
       destroy the member's work — a queued video is minutes of uploading. So
       the composer only auto-collapses when it is genuinely empty. */
    function composerHasContent() {
        var input = $('cfCaption');
        if (input && String(input.value || '').trim()) return true;
        return _pending.length > 0;
    }

    function setComposerCollapsed(collapsed) {
        var composer = $('cfComposer');
        if (!composer) return;
        var summary = $('cfComposerSummary');
        // Never hide work in progress, whichever direction we were asked to go.
        if (collapsed && composerHasContent()) return;
        composer.classList.toggle('is-collapsed', !!collapsed);
        if (summary) summary.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    }

    function expandComposer() {
        if (!isComposerCollapsed()) return;
        setComposerCollapsed(false);
        // Focus the field the member just asked for, so posting is one tap and
        // then typing — not tap, then tap again.
        var input = $('cfCaption');
        if (input && typeof input.focus === 'function') input.focus();
    }

    function collapseComposer() {
        setComposerCollapsed(true);
    }

    function bind() {
        var input = $('cfCaption');
        if (input) {
            // Typing is itself an intent to post; make sure the composer is open.
            input.addEventListener('input', function () {
                expandComposer();
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
        var avatarInput = $('cfAvatarInput');
        if (avatarInput && window.GMCommunityAvatar) {
            avatarInput.addEventListener('change', function () {
                window.GMCommunityAvatar.pickFile(avatarInput.files);
                avatarInput.value = '';
            });
        }
        // The shared avatar module owns the modal + upload; the feed only needs
        // to repaint its own cards when the picture changes.
        if (window.GMCommunityAvatar) {
            window.GMCommunityAvatar.onChange(onAvatarChanged);
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
            expandComposer();
            addFiles(event.clipboardData.files);
        });
        document.addEventListener('pointerdown', function (event) {
            if (!_activeReactionPicker) return;
            var target = event && event.target;
            if (target && (target === _activeReactionPicker.picker
                    || target === _activeReactionPicker.trigger
                    || _activeReactionPicker.picker.contains(target)
                    || _activeReactionPicker.trigger.contains(target))) return;
            closeReactionPicker(false);
        });
        document.addEventListener('focusin', function (event) {
            if (!_activeReactionPicker) return;
            var target = event && event.target;
            if (target && (target === _activeReactionPicker.picker
                    || target === _activeReactionPicker.trigger
                    || _activeReactionPicker.picker.contains(target)
                    || _activeReactionPicker.trigger.contains(target))) return;
            closeReactionPicker(false);
        });
        // Drag & drop onto the composer. A drag is an intent to post, so it
        // opens the composer too — otherwise the drop would land on a hidden
        // body and appear to do nothing.
        var composer = $('cfComposer');
        if (composer) {
            ['dragover', 'drop'].forEach(function (name) {
                composer.addEventListener(name, function (event) {
                    event.preventDefault();
                    if (name === 'drop' && event.dataTransfer && event.dataTransfer.files) {
                        expandComposer();
                        addFiles(event.dataTransfer.files);
                    }
                });
            });
        }
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                if (_activeReactionPicker) {
                    closeReactionPicker(true);
                    event.preventDefault();
                    return;
                }
                // An open-but-empty composer closes before the dialogs do: it is
                // the innermost surface. It refuses while it holds work, so
                // Escape can never discard a caption.
                if (!isComposerCollapsed()) {
                    collapseComposer();
                    event.preventDefault();
                    return;
                }
                closeLightbox(); closeEdit(); closeSheet(); closeSearch();
                closeAirtime();
                closeClaim();
                if (window.GMCommunityAvatar) window.GMCommunityAvatar.close();
            }
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
        loadMessageUnread();
        loadRewards();
        if (typeof window.addEventListener === 'function') {
            window.addEventListener('gm:daily-learn-completed', function (event) {
                var refresh = loadRewards();
                if (event && event.detail) event.detail.refreshPromise = refresh;
            });
        }
        loadFeed(true);
    }

    window.GMCommunity = {
        init: init,
        refresh: refresh,
        loadRewards: loadRewards,
        loadMore: loadMore,
        submitPost: submitPost,
        expandComposer: expandComposer,
        collapseComposer: collapseComposer,
        setDailyPrompt: setDailyPrompt,
        answerPrompt: answerPrompt,
        seePromptResponses: seePromptResponses,
        cancelPromptMode: cancelPromptMode,
        prefillDailyLearnReflection: prefillDailyLearnReflection,
        checkWithdrawalEligibility: checkWithdrawalEligibility,
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
        openAvatarModal: function () {
            if (window.GMCommunityAvatar) window.GMCommunityAvatar.open();
        },
        closeAvatarModal: function () {
            if (window.GMCommunityAvatar) window.GMCommunityAvatar.close();
        },
        removeAvatar: function () {
            if (window.GMCommunityAvatar) window.GMCommunityAvatar.remove();
        },
        openSearch: openSearch,
        closeSearch: closeSearch,
        searchInput: searchInput,
        searchKeydown: searchKeydown,
        openSheet: openSheet,
        closeSheet: closeSheet,
        sheetGo: sheetGo,
        openAirtime: openAirtime,
        closeAirtime: closeAirtime,
        openClaim: openClaim,
        closeClaim: closeClaim,
        setMessageUnread: setMessageUnread,
        // Exposed for the test harness (drives the real bodies).
        _internals: {
            timeAgo: timeAgo,
            countLabel: countLabel,
            linkifyInto: linkifyInto,
            buildCard: buildCard,
            buildNavItem: buildNavItem,
            buildFollowButton: buildFollowButton,
            buildVideoFrame: buildVideoFrame,
            buildActionRow: buildActionRow,
            buildRail: buildRail,
            buildRailItem: buildRailItem,
            railAuthors: railAuthors,
            onMediaTap: onMediaTap,
            avatarNode: avatarNode,
            renderAvatarInto: renderAvatarInto,
            onAvatarChanged: onAvatarChanged,
            getAvatarUrl: function () { return AVATAR_URL; },
            setAvatarUrl: function (value) { AVATAR_URL = value || ''; },
            toggleFollow: toggleFollow,
            setFollowButton: setFollowButton,
            renderNav: renderNav,
            setMessageUnread: setMessageUnread,
            loadMessageUnread: loadMessageUnread,
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
            renderComments: renderComments,
            setComments: function (postId, rows) { _commentCache[postId] = rows || []; },
            loadRewards: loadRewards,
            renderRewards: renderRewards,
            formatGd: formatGd,
            getFeed: function () { return _feed; },
            setFeed: function (rows) { _feed = rows; },
            getPending: function () { return _pending; },
            setPending: function (rows) { _pending = rows; },
            getNextCursor: function () { return _nextCursor; },
            setNextCursor: function (value) { _nextCursor = value; },
            isComposerCollapsed: isComposerCollapsed,
            composerHasContent: composerHasContent,
            setComposerCollapsed: setComposerCollapsed,
            expandComposer: expandComposer,
            collapseComposer: collapseComposer,
            resetComposer: resetComposer,
        },
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
