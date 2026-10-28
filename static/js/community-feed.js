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
    var _pinnedDonations = [];
    var _pinnedDonationCursor = null;
    var _pinnedDonationLoading = false;
    var _vaultDonationConfig = null;
    var _vaultDonationIntent = null;
    var _vaultDonationBusy = false;
    var _activeVaultDonation = null;
    var _loading = false;
    var _pending = [];         // composer attachments awaiting upload
    var _posting = false;
    var _reactionInFlight = {};  // post_id -> true (serialise per post)
    var _activeReactionPicker = null;
    var _reactionUsersOpen = false;
    var _reactionUsersBound = false;
    var _reactionUsersPostId = null;
    var _reactionUsersFilter = 'all';
    var _reactionUsersCursor = null;
    var _reactionUsersBusy = false;
    var _reactionUsersRequestId = 0;
    var _reactionUsersReturnFocus = null;
    var _reactionUsersBodyWasLocked = false;
    var _commentsOpen = {};    // post_id -> true
    var _commentCache = {};    // post_id -> [comments]
    var _replyingTo = {};      // post_id -> comment/reply id
    var _collapsedReplies = {}; // post_id -> {comment_id: true}
    var _earnBusy = false;     // one in-flight rewards fetch at a time
    var _earnDirty = false;    // a refresh arrived while one was in flight
    var _earnPromise = null;   // callers can await the coalesced latest read
    // Reconciliation is deliberately page-memory-only: the cursor is a
    // bounded scan continuation, never a durable withdrawal authorization.
    var _reconciliation = {
        withdrawalId: null,
        beforeBlock: null,
        busy: false,
        skipRevealOnce: false,
        statusLoaded: false,
        canWithdraw: false,
        resumeAuthorizationId: null,
    };
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

    function communityProfilePath(username) {
        var name = esc(username).trim().replace(/^@+/, '');
        if (!/^[A-Za-z0-9_]{3,24}$/.test(name)) return '';
        return '/community/u/' + encodeURIComponent(name);
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

    function hasReactionEvidence(post, count) {
        if (count > 0 || (post && post.my_reaction && REACTIONS[post.my_reaction])) return true;
        var breakdown = post && post.reaction_breakdown;
        return !!(breakdown && typeof breakdown === 'object'
            && Object.keys(breakdown).some(function (key) {
                return !!REACTIONS[key] && Number(breakdown[key]) > 0;
            }));
    }

    function buildReactionUsersTrigger(post) {
        var count = Number(post && post.reaction_count) || 0;
        var hasReactions = hasReactionEvidence(post, count);
        var trigger = el('button', 'cf-reaction-count-trigger');
        trigger.type = 'button';
        trigger.disabled = !hasReactions;
        trigger.setAttribute('aria-haspopup', 'dialog');
        trigger.setAttribute('aria-controls', 'cfReactionUsersModal');
        trigger.setAttribute('aria-expanded', 'false');
        trigger.setAttribute('aria-label', hasReactions
            ? (count > 0 ? 'See who reacted; ' + count + ' reactions' : 'See who reacted')
            : 'No reactions yet');
        trigger.appendChild(buildSummaryEmojis(post));
        if (count > 0 || !hasReactions) {
            var countNode = el('span', 'cf-count', countLabel(count) || '0');
            countNode.setAttribute('aria-live', 'polite');
            trigger.appendChild(countNode);
        }
        if (hasReactions) trigger.appendChild(el('span', 'cf-reaction-view-label', 'See who reacted'));
        trigger.onclick = function () { openReactionUsers(post, trigger); };
        return trigger;
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

    var REACTION_USERS_PAGE_SIZE = 25;

    function renderReactionUserFilters() {
        var filters = $('cfReactionUsersFilters');
        if (!filters) return;
        filters.textContent = '';
        var choices = [{ key: 'all', label: 'All' }];
        Object.keys(REACTIONS).forEach(function (key) {
            choices.push({ key: key, label: REACTIONS[key] + ' ' + key.charAt(0).toUpperCase() + key.slice(1) });
        });
        choices.forEach(function (choice) {
            var button = el('button', 'cf-reaction-filter', choice.label);
            button.type = 'button';
            button.setAttribute('data-reaction-filter', choice.key);
            button.setAttribute('aria-pressed', _reactionUsersFilter === choice.key ? 'true' : 'false');
            button.onclick = function () {
                if (_reactionUsersFilter === choice.key) return;
                _reactionUsersFilter = choice.key;
                renderReactionUserFilters();
                loadReactionUsers(true);
            };
            filters.appendChild(button);
        });
    }

    function appendReactionUser(item) {
        var list = $('cfReactionUsersList');
        if (!list || !item || typeof item !== 'object') return;
        var displayName = String(item.username || 'Community member').trim() || 'Community member';
        var itemReaction = String(item.reaction || '').toLowerCase();
        var emoji = REACTIONS[itemReaction] || '';
        var row = el('li', 'cf-reactor-row');
        var path = communityProfilePath(item.profile_username);
        var avatar = avatarNode(item.avatar_url, item.profile_username || displayName,
            'cf-avatar cf-reactor-avatar');
        if (path) {
            var avatarLink = el('a', 'cf-comment-profile-avatar-link');
            avatarLink.href = path;
            avatarLink.setAttribute('aria-label', 'View @' + item.profile_username + "'s profile");
            avatarLink.appendChild(avatar);
            row.appendChild(avatarLink);
        } else {
            row.appendChild(avatar);
        }
        var copy = el('span', 'cf-reactor-copy');
        if (path) {
            var nameLink = el('a', 'cf-reactor-name', displayName);
            nameLink.href = path;
            copy.appendChild(nameLink);
        } else {
            copy.appendChild(el('span', 'cf-reactor-name', displayName));
        }
        copy.appendChild(el('span', 'cf-reactor-type', emoji ? 'Reacted with ' + itemReaction : 'Reacted'));
        row.appendChild(copy);
        if (emoji) {
            var icon = el('span', 'cf-reactor-emoji', emoji);
            icon.setAttribute('aria-hidden', 'true');
            row.appendChild(icon);
        }
        list.appendChild(row);
    }

    function loadReactionUsers(reset) {
        if (!_reactionUsersOpen || !_reactionUsersPostId) return Promise.resolve(false);
        if (!reset && (_reactionUsersBusy || !_reactionUsersCursor)) return Promise.resolve(false);
        var list = $('cfReactionUsersList');
        var status = $('cfReactionUsersStatus');
        var more = $('cfReactionUsersMore');
        var retry = $('cfReactionUsersRetry');
        if (reset) {
            _reactionUsersRequestId += 1;
            _reactionUsersBusy = false;
            _reactionUsersCursor = null;
            if (list) list.textContent = '';
            if (more) more.hidden = true;
            if (retry) retry.hidden = true;
            if (status) status.textContent = 'Loading reactions…';
        }
        var requestId = ++_reactionUsersRequestId;
        var query = 'limit=' + REACTION_USERS_PAGE_SIZE;
        if (_reactionUsersFilter !== 'all') query += '&reaction=' + encodeURIComponent(_reactionUsersFilter);
        if (_reactionUsersCursor) query += '&cursor=' + encodeURIComponent(_reactionUsersCursor);
        _reactionUsersBusy = true;
        if (more) more.disabled = true;
        if (retry) retry.hidden = true;
        return api('/community/api/posts/' + encodeURIComponent(_reactionUsersPostId)
            + '/reactions?' + query, { cache: 'no-store' }).then(function (result) {
            if (requestId !== _reactionUsersRequestId || !_reactionUsersOpen) return false;
            if (!result.ok || !result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not load reactions.');
            }
            var reactors = Array.isArray(result.data.reactors) ? result.data.reactors : [];
            reactors.forEach(appendReactionUser);
            _reactionUsersCursor = typeof result.data.next_cursor === 'string'
                && result.data.next_cursor ? result.data.next_cursor : null;
            var hasMore = result.data.has_more === true && !!_reactionUsersCursor;
            if (status) {
                status.textContent = list && list.children.length
                    ? '' : 'No reactions yet.';
            }
            if (more) {
                more.hidden = !hasMore;
                more.disabled = false;
            }
            return true;
        }).catch(function (error) {
            if (requestId !== _reactionUsersRequestId || !_reactionUsersOpen) return false;
            if (status) status.textContent = (error && error.message) || 'Could not load reactions.';
            if (retry) retry.hidden = false;
            if (more) { more.hidden = true; more.disabled = false; }
            return false;
        }).then(function (result) {
            if (requestId === _reactionUsersRequestId) _reactionUsersBusy = false;
            return result;
        });
    }

    function reactionUsersKeydown(event) {
        if (!_reactionUsersOpen || !event) return;
        if (event.key === 'Escape') {
            if (event.preventDefault) event.preventDefault();
            closeReactionUsers();
            return;
        }
        if (event.key !== 'Tab') return;
        var modal = $('cfReactionUsersModal');
        if (!modal) return;
        var focusable = Array.prototype.slice.call(modal.querySelectorAll(
            'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )).filter(function (node) {
            return !node.hidden && (!node.getClientRects || node.getClientRects().length > 0);
        });
        if (!focusable.length) {
            if (event.preventDefault) event.preventDefault();
            if (modal.focus) modal.focus();
            return;
        }
        var first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
            if (event.preventDefault) event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            if (event.preventDefault) event.preventDefault();
            first.focus();
        }
    }

    function bindReactionUsersModal() {
        if (_reactionUsersBound) return;
        var modal = $('cfReactionUsersModal');
        if (!modal) return;
        _reactionUsersBound = true;
        modal.addEventListener('click', function (event) {
            if (event && event.target === modal) closeReactionUsers();
        });
        var close = $('cfReactionUsersClose');
        if (close) close.addEventListener('click', function () { closeReactionUsers(); });
        var more = $('cfReactionUsersMore');
        if (more) more.addEventListener('click', function () { loadReactionUsers(false); });
        var retry = $('cfReactionUsersRetry');
        if (retry) retry.addEventListener('click', function () { loadReactionUsers(true); });
        document.addEventListener('keydown', reactionUsersKeydown);
    }

    function openReactionUsers(post, trigger) {
        var modal = $('cfReactionUsersModal');
        var postId = post && String(post.id || '');
        if (!modal || !/^\d+$/.test(postId) || Number(postId) <= 0) return false;
        bindReactionUsersModal();
        _reactionUsersReturnFocus = trigger || document.activeElement || null;
        _reactionUsersBodyWasLocked = document.body.classList.contains('cf-modal-open');
        _reactionUsersPostId = postId;
        _reactionUsersFilter = 'all';
        _reactionUsersOpen = true;
        modal.hidden = false;
        modal.setAttribute('aria-hidden', 'false');
        if (_reactionUsersReturnFocus && _reactionUsersReturnFocus.setAttribute) {
            _reactionUsersReturnFocus.setAttribute('aria-expanded', 'true');
        }
        document.body.classList.add('cf-modal-open');
        renderReactionUserFilters();
        var status = $('cfReactionUsersStatus');
        if (status) status.textContent = 'Loading reactions…';
        var list = $('cfReactionUsersList');
        if (list) list.textContent = '';
        var close = $('cfReactionUsersClose');
        if (close && close.focus) close.focus();
        loadReactionUsers(true);
        return true;
    }

    function closeReactionUsers(restoreFocus) {
        if (!_reactionUsersOpen) return true;
        var modal = $('cfReactionUsersModal');
        if (modal) {
            modal.hidden = true;
            modal.setAttribute('aria-hidden', 'true');
        }
        _reactionUsersRequestId += 1;
        _reactionUsersBusy = false;
        _reactionUsersCursor = null;
        var trigger = _reactionUsersReturnFocus;
        if (trigger && trigger.setAttribute) trigger.setAttribute('aria-expanded', 'false');
        _reactionUsersReturnFocus = null;
        _reactionUsersPostId = null;
        _reactionUsersOpen = false;
        if (!_reactionUsersBodyWasLocked) document.body.classList.remove('cf-modal-open');
        _reactionUsersBodyWasLocked = false;
        if (restoreFocus !== false && trigger && trigger.focus) trigger.focus();
        return true;
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
            renderRewardOutcome(result.data.reward);
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

    function renderPinnedDonations() {
        var section = $('cfPinnedDonations');
        var container = $('cfPinnedDonationFeed');
        if (!section || !container) return;
        container.textContent = '';
        section.hidden = !_pinnedDonations.length;
        _pinnedDonations.forEach(function (post) {
            container.appendChild(buildDonationCard(post));
        });
        var more = $('cfPinnedDonationMore');
        if (more) more.hidden = !_pinnedDonationCursor;
    }

    function renderFeed() {
        var container = $('cfFeed');
        if (!container) return;
        container.textContent = '';
        container.setAttribute('aria-busy', 'false');
        buildRail();
        renderPinnedDonations();

        if (!_feed.length) {
            var empty = $('cfEmpty');
            if (empty) {
                empty.hidden = false;
                empty.textContent = _pinnedDonations.length
                    ? 'No recent member posts yet.'
                    : (WALLET ? 'No posts yet. Be the first to share something!' : 'No posts yet.');
            }
            updateLoadMore();
            return;
        }
        var emptyNode = $('cfEmpty');
        if (emptyNode) {
            emptyNode.hidden = true;
            emptyNode.textContent = 'No posts yet. Be the first to share something!';
        }

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

    function buildDonationCard(post) {
        var card = el('article', 'cf-donation-card');
        card.setAttribute('data-post-id', post.id);
        var head = el('div', 'cf-donation-card-head');
        var author = el('div', 'cf-donation-system-author');
        author.appendChild(el('span', 'cf-donation-system-mark', 'S'));
        author.appendChild(el('strong', null, 'Sytem'));
        head.appendChild(author);
        head.appendChild(el('span', 'cf-donation-pin-label', 'Pinned'));
        card.appendChild(head);

        var body = el('div', 'cf-card-body');
        var donor = String(post.donor_name || '').trim() || 'A Community member';
        var amount = String(post.amount_gd || '').trim() || '0';
        body.appendChild(el('p', 'cf-donation-card-copy',
            donor + ' donated ' + amount + ' G$ to the Community Withdrawal Vault.'));
        body.appendChild(el('small', 'cf-donation-card-meta',
            'Thank you for supporting the Community.'));

        var links = el('div', 'cf-donation-card-links');
        var linkedIn = String(post.linkedin_url || '');
        if (/^https:\/\/(?:www\.)?linkedin\.com\/in\/[A-Za-z0-9_%.-]{1,120}\/?$/i.test(linkedIn)) {
            var profileLink = el('a', null, 'LinkedIn Profile');
            profileLink.href = linkedIn;
            profileLink.target = '_blank';
            profileLink.rel = 'noopener noreferrer';
            links.appendChild(profileLink);
        }
        var txUrl = String(post.tx_url || '');
        if (/^https:\/\/celoscan\.io\/tx\/0x[0-9a-f]{64}$/i.test(txUrl)) {
            var txLink = el('a', null, 'View Celo Transaction');
            txLink.href = txUrl;
            txLink.target = '_blank';
            txLink.rel = 'noopener noreferrer';
            links.appendChild(txLink);
        }
        if (links.childNodes.length) body.appendChild(links);
        var stamp = timeAgo(post.created_at);
        if (stamp) body.appendChild(el('time', 'cf-donation-card-time', stamp));

        var summary = el('div', 'cf-summary');
        var summaryLeft = el('div', 'cf-summary-left');
        summaryLeft.appendChild(buildReactionUsersTrigger(post));
        summary.appendChild(summaryLeft);
        summary.appendChild(el('span', null, countLabel(post.comment_count)
            ? countLabel(post.comment_count) + ' comment' + (post.comment_count === 1 ? '' : 's')
            : ''));
        body.appendChild(summary);

        var engagement = el('div', 'cf-engagement');
        engagement.appendChild(buildActionRow(post, engagement));
        body.appendChild(engagement);
        card.appendChild(body);
        return card;
    }

    function buildCard(post) {
        if (post && post.post_type === 'system_vault_donation') {
            return buildDonationCard(post);
        }
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
        left.appendChild(buildReactionUsersTrigger(post));
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
            renderRewardOutcome(result.data.reward);
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
        var profilePath = !comment.is_deleted
            ? communityProfilePath(comment.profile_username) : '';
        var commenterName = profilePath ? comment.profile_username : comment.username;
        if (!comment.is_deleted) {
            var commentAvatar = avatarNode(comment.avatar_url, commenterName,
                'cf-comment-avatar');
            if (profilePath) {
                var avatarLink = el('a', 'cf-comment-profile-avatar-link');
                avatarLink.href = profilePath;
                avatarLink.setAttribute('aria-label', 'View @' + comment.profile_username + "'s profile");
                avatarLink.appendChild(commentAvatar);
                row.appendChild(avatarLink);
            } else {
                row.appendChild(commentAvatar);
            }
        } else {
            row.appendChild(el('span', 'cf-comment-avatar cf-comment-deleted-avatar', '…'));
        }
        var bubble = el('div', 'cf-comment-body');
        var authorLine = el('div', 'cf-comment-author-line');
        if (profilePath) {
            var profileLink = el('a', 'cf-comment-profile-link');
            profileLink.href = profilePath;
            profileLink.setAttribute('aria-label', 'View @' + comment.profile_username + "'s profile");
            profileLink.appendChild(el('strong', null, commenterName));
            authorLine.appendChild(profileLink);
        } else {
            authorLine.appendChild(el('strong', null, comment.username));
        }
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
            renderRewardOutcome(result.data.reward);
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
        var title = post.post_type === 'system_vault_donation'
            ? 'Community Vault donation · Sytem'
            : '@' + (post.username || 'member') + ' on GoodMarket Community';
        if (navigator.share) {
            navigator.share({
                title: title,
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

    function renderBudgetDisplay(data) {
        var budgetsEl = $('cfEarnBudgets');
        if (!budgetsEl || !data || data.is_guest || data.configuration_available === false) {
            if (budgetsEl) budgetsEl.hidden = true;
            ['cfEarnSocialBudgetValue', 'cfEarnSocialBudgetNote',
                'cfEarnQuizBudgetValue', 'cfEarnQuizBudgetNote',
                'cfEarnCombinedBudgetValue', 'cfEarnCombinedBudgetNote'].forEach(function (id) {
                var node = $(id);
                if (node) node.textContent = '';
            });
            return;
        }
        var budgets = data.budgets;
        if (!budgets || typeof budgets !== 'object') {
            budgetsEl.hidden = true;
            return;
        }
        var resetEl = $('cfEarnBudgetReset');
        if (resetEl && budgets.reset_at) {
            var reset = new Date(String(budgets.reset_at));
            if (!isNaN(reset.getTime())) {
                try {
                    resetEl.textContent = 'Next reset: ' + reset.toLocaleString('en-PH', {
                        timeZone: 'Asia/Manila', month: 'short', day: 'numeric',
                        hour: 'numeric', minute: '2-digit', hour12: true
                    }) + ' PH';
                } catch (e) {
                    resetEl.textContent = 'Next reset: ' + reset.toISOString();
                }
            }
        }
        var value = function (item, key) {
            if (!item || item[key] === undefined || item[key] === null) return null;
            var number = Number(item[key]);
            return Number.isFinite(number) ? number : null;
        };
        var render = function (name, item, label) {
            var used = value(item, 'used_gd');
            var remaining = value(item, 'remaining_gd');
            var cap = value(item, 'cap_gd');
            var valueEl = $(name + 'BudgetValue');
            var noteEl = $(name + 'BudgetNote');
            if (used === null || remaining === null || cap === null) return false;
            if (valueEl) valueEl.textContent = formatGd(used) + ' used / ' + formatGd(remaining) + ' remaining';
            if (noteEl) noteEl.textContent = label + ' limit ' + formatGd(cap) + ' G$'
                + (item.reached ? ' · limit reached' : '');
            return true;
        };
        var socialOk = render('cfEarnSocial', budgets.social, 'Social');
        var quizOk = render('cfEarnQuiz', budgets.quiz, 'Daily Learn');
        var combinedOk = render('cfEarnCombined', budgets.combined, 'Combined');
        budgetsEl.hidden = !(socialOk && quizOk && combinedOk);
    }

    function renderRewardOutcome(reward) {
        if (!reward || !reward.status) return;
        var status = String(reward.status);
        if (status === 'credited' || status === 'already_credited') return;
        var reason = String(reward.reason || 'reward_unavailable');
        var message = reason === 'daily_cap_reached'
            ? 'Your action was saved, but today’s reward budget has been used.'
            : (reason === 'post_cap_reached'
                ? 'Your action was saved, but this post has reached its reward limit.'
                : (reason === 'combined_cap_reached'
                    ? 'Your action was saved, but the combined daily reward cap has been reached.'
                    : (reason === 'configuration_unavailable'
                        ? 'Your action was saved, but Community G$ configuration is temporarily unavailable. No reward was credited; try again later.'
                        : (reason === 'legacy_snapshot_required'
                            ? 'Your action was saved, but this older quiz attempt needs manual review before any reward can be credited.'
                            : (reason === 'verified_wallet_required'
                                ? 'Your action was saved, but this wallet is not eligible for Community G$.'
                                : 'Your action was saved, but its Community G$ credit could not be confirmed.')))));
        toast(message, 'error');
    }

    // ── Community G$ card ──────────────────────────────────────────────────
    // The balance lives on THIS page only (never the wallet page — see
    // docs/COMMUNITY_REWARDS_LEDGER_PROPOSAL.md §0). It is the SUM of an
    // append-only ledger, fetched from the server; the client never computes
    // or stores it. Refreshed after every earning action so the number moves
    // as the user reacts and comments.
    var _withdrawalHistory = { items: [], hasMore: false, cursor: null, loaded: false, busy: false };

    function renderWithdrawalHistoryItems(items) {
        var list = $('cfEarnHistoryList');
        if (!list) return;
        items.forEach(function (item) {
            var li = el('li', 'cf-earn-history-item');
            var detail = el('div', 'cf-earn-history-detail');
            var amount = Number(item.amount_gd);
            detail.appendChild(el('strong', 'cf-earn-history-amount',
                (Number.isFinite(amount) ? formatGd(amount) : String(item.amount_gd || '')) + ' G$'));
            var timestamp = item.timestamp_utc ? new Date(item.timestamp_utc) : null;
            var dateText = timestamp && !Number.isNaN(timestamp.getTime())
                ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(timestamp)
                : 'Date unavailable';
            detail.appendChild(el('time', 'cf-earn-history-date', dateText));
            li.appendChild(detail);
            var state = item.status === 'confirmed' ? 'Confirmed' : 'Confirming';
            li.appendChild(el('span', 'cf-earn-history-state', state));
            var hash = String(item.tx_hash || '');
            if (/^0x[0-9a-fA-F]{64}$/.test(hash)) {
                var link = el('a', 'cf-earn-history-link', 'View transaction');
                link.href = 'https://celoscan.io/tx/' + hash;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                link.setAttribute('aria-label', 'View transaction ' + hash + ' on CeloScan (opens in a new tab)');
                li.appendChild(link);
            }
            list.appendChild(li);
        });
    }

    function loadWithdrawalHistory(older) {
        var message = $('cfEarnHistoryMessage');
        var list = $('cfEarnHistoryList');
        var more = $('cfEarnHistoryMore');
        if (_withdrawalHistory.busy || !WALLET) return;
        _withdrawalHistory.busy = true;
        if (more) { more.disabled = true; more.textContent = 'Loading…'; }
        if (message) message.textContent = older ? 'Loading older transactions…' : 'Loading transaction history…';
        var url = '/community/api/rewards/withdrawals/history?limit=20';
        if (older && _withdrawalHistory.cursor) {
            url += '&before_block=' + encodeURIComponent(_withdrawalHistory.cursor.block_number)
                + '&before_log_index=' + encodeURIComponent(_withdrawalHistory.cursor.log_index);
        }
        api(url).then(function (result) {
            var data = result && result.data;
            if (!result.ok || !data || !data.success || !Array.isArray(data.items)) {
                throw new Error((data && data.error) || 'Could not load transaction history.');
            }
            if (!older) list.textContent = '';
            renderWithdrawalHistoryItems(data.items);
            _withdrawalHistory.loaded = true;
            _withdrawalHistory.hasMore = data.has_more === true;
            _withdrawalHistory.cursor = data.next_cursor || null;
            _withdrawalHistory.retryOlder = false;
            if (message) {
                message.textContent = !data.items.length
                    ? (data.has_more
                        ? 'No withdrawals found in this scanned range. Load older to continue.'
                        : (older ? 'No older withdrawals.' : 'No confirmed withdrawals yet.'))
                    : '';
            }
            if (more) {
                more.hidden = !_withdrawalHistory.hasMore;
                more.disabled = false;
                more.textContent = 'Load older';
            }
        }).catch(function (error) {
            if (message) message.textContent = (error && error.message ? error.message : 'Could not load transaction history.') + ' Select to retry.';
            if (more) { more.hidden = false; more.disabled = false; more.textContent = older ? 'Retry loading older' : 'Retry'; }
            // Retry button shares this action; remember whether this was a next-page request.
            _withdrawalHistory.retryOlder = !!older;
        }).then(function () { _withdrawalHistory.busy = false; });
    }

    function isWithdrawalId(value) {
        return /^0x[0-9a-f]{64}$/i.test(String(value || ''));
    }
    function isTransactionHash(value) {
        return /^0x[0-9a-f]{64}$/i.test(String(value || ''));
    }
    function reconciliationStatus(message, kind) {
        var status = $('cfEarnReconcileStatus');
        if (!status) return;
        status.textContent = message || '';
        status.className = 'cf-earn-reconcile-status' + (kind ? ' is-' + kind : '');
        status.hidden = !message;
    }
    function reconciliationButton(disabled, label) {
        var button = $('cfEarnReconcileBtn');
        if (!button) return;
        button.disabled = !!disabled;
        if (label) button.textContent = label;
    }
    function syncWithdrawalActionVisibility() {
        var row = $('cfEarnWithdrawRow');
        if (!row) return;
        row.hidden = !WALLET || !_reconciliation.statusLoaded
            || !_reconciliation.canWithdraw || !!_reconciliation.withdrawalId;
    }
    function showReconciliationStatusUnavailable() {
        var prompt = $('cfEarnReconcile');
        var copy = $('cfEarnReconcileCopy');
        var button = $('cfEarnReconcileBtn');
        var hasKnownReservation = isWithdrawalId(_reconciliation.withdrawalId);
        if (prompt) prompt.hidden = false;
        if (copy) {
            copy.textContent = hasKnownReservation
                ? 'A previous payout is awaiting confirmation. We could not refresh its status; retry the reconciliation check or contact support.'
                : 'We could not check whether a previous withdrawal is awaiting confirmation. Refresh this page before starting another withdrawal.';
        }
        if (button) {
            button.hidden = !hasKnownReservation;
            button.disabled = !hasKnownReservation;
        }
        reconciliationStatus(hasKnownReservation
            ? 'The status check could not complete. No balance change was made; retry or contact support.'
            : 'Refresh the page before starting a withdrawal so we can safely check for an earlier request.', 'error');
        syncWithdrawalActionVisibility();
    }
    function hideReconciliationPrompt() {
        var prompt = $('cfEarnReconcile');
        var copy = $('cfEarnReconcileCopy');
        var button = $('cfEarnReconcileBtn');
        if (prompt) prompt.hidden = true;
        _reconciliation.withdrawalId = null;
        _reconciliation.beforeBlock = null;
        reconciliationStatus('');
        if (copy) copy.textContent = 'A previous payout was submitted but may need checking. This checks and reconciles that submitted payout only; it will not send another payment or ask you to sign.';
        if (button) button.hidden = false;
        reconciliationButton(false, 'Reconcile previous withdrawal');
        syncWithdrawalActionVisibility();
        syncTodayHub();
    }
    function loadReconciliationStatus() {
        // The member-only markup and this guard keep guests from learning even
        // that a wallet-specific reservation exists.
        if (BOOT.isGuest || !WALLET || !$('cfEarnReconcile')) return Promise.resolve(false);
        return api('/community/api/rewards/withdrawals/reconciliation', {
            headers: { 'Cache-Control': 'no-cache' },
        }).then(function (result) {
            var skipReveal = _reconciliation.skipRevealOnce;
            _reconciliation.skipRevealOnce = false;
            var data = result && result.data;
            if (!result.ok || !data || data.success !== true
                    || typeof data.needs_reconciliation !== 'boolean') {
                _reconciliation.statusLoaded = false;
                showReconciliationStatusUnavailable();
                return false;
            }
            _reconciliation.statusLoaded = true;
            var withdrawal = data.withdrawal || {};
            if (data.needs_reconciliation === true && !isWithdrawalId(withdrawal.withdrawal_id)) {
                _reconciliation.statusLoaded = false;
                showReconciliationStatusUnavailable();
                return false;
            }
            if (data.needs_reconciliation !== true) {
                _reconciliation.resumeAuthorizationId = null;
                hideReconciliationPrompt();
                return false;
            }
            _reconciliation.withdrawalId = String(withdrawal.withdrawal_id).toLowerCase();
            _reconciliation.beforeBlock = null;
            _reconciliation.resumeAuthorizationId = null;
            if (skipReveal) {
                hideReconciliationPrompt();
                return false;
            }
            var prompt = $('cfEarnReconcile');
            var copy = $('cfEarnReconcileCopy');
            var button = $('cfEarnReconcileBtn');
            if (prompt) prompt.hidden = false;
            if (copy) copy.textContent = 'A previous payout was submitted but may need checking. This checks and reconciles that submitted payout only; it will not send another payment or ask you to sign.';
            if (button) button.hidden = false;
            reconciliationStatus('');
            reconciliationButton(false, 'Reconcile previous withdrawal');
            syncWithdrawalActionVisibility();
            syncTodayHub();
            return true;
        }).catch(function () {
            _reconciliation.skipRevealOnce = false;
            _reconciliation.statusLoaded = false;
            showReconciliationStatusUnavailable();
            return false;
        });
    }
    function refreshWithdrawalHistoryAfterConfirmation() {
        if (!_withdrawalHistory.loaded) return Promise.resolve();
        _withdrawalHistory.loaded = false;
        var historyList = $('cfEarnHistoryList');
        var historyMore = $('cfEarnHistoryMore');
        if (historyList) historyList.textContent = '';
        if (historyMore) {
            historyMore.hidden = true;
            historyMore.textContent = 'Load older';
        }
        var historyToggle = $('cfEarnHistoryToggle');
        if (historyToggle && historyToggle.getAttribute('aria-expanded') === 'true') {
            loadWithdrawalHistory(false);
        }
        return Promise.resolve();
    }
    async function refreshWithdrawalViewsAfterConfirmation() {
        _reconciliation.statusLoaded = false;
        _reconciliation.resumeAuthorizationId = null;
        hideReconciliationPrompt();
        await refreshWithdrawalHistoryAfterConfirmation();
        await loadRewards();
        // Refresh the private reservation status without allowing a stale
        // claimed response to flash the prompt back after completion.
        _reconciliation.skipRevealOnce = true;
        await loadReconciliationStatus();
    }
    async function confirmWithdrawalTransaction(withdrawalId, txHash, setStatus) {
        _reconciliation.withdrawalId = isWithdrawalId(withdrawalId)
            ? String(withdrawalId).toLowerCase() : null;
        _reconciliation.resumeAuthorizationId = null;
        _reconciliation.statusLoaded = true;
        var prompt = $('cfEarnReconcile');
        var button = $('cfEarnReconcileBtn');
        var copy = $('cfEarnReconcileCopy');
        if (prompt && _reconciliation.withdrawalId) prompt.hidden = false;
        if (button && _reconciliation.withdrawalId) button.hidden = false;
        if (copy && _reconciliation.withdrawalId) {
            copy.textContent = 'A previous payout was submitted but may need checking. This checks and reconciles that submitted payout only; it will not send another payment or ask you to sign.';
        }
        syncWithdrawalActionVisibility();
        var key = 'gm-community-withdrawal:' + String(WALLET).toLowerCase();
        try { localStorage.setItem(key, JSON.stringify({ withdrawal_id: withdrawalId, tx_hash: txHash })); } catch (_) {}
        var wait = function (ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); };
        for (var attempt = 0; attempt < 12; attempt += 1) {
            var check = await api('/community/api/rewards/withdrawals/confirm', {
                method: 'POST',
                body: JSON.stringify({ withdrawal_id: withdrawalId, tx_hash: txHash }),
            });
            var confirmed = check && check.data;
            if (confirmed && confirmed.status === 'completed') {
                try { localStorage.removeItem(key); } catch (_) {}
                if (setStatus) setStatus('Withdrawal confirmed. Refreshing your Community balance and history…', 'ok');
                await refreshWithdrawalViewsAfterConfirmation();
                return true;
            }
            if (confirmed && confirmed.status === 'failed') {
                try { localStorage.removeItem(key); } catch (_) {}
                var failedMessage = confirmed.error || 'The withdrawal transaction reverted; your ledger balance was not debited.';
                _reconciliation.statusLoaded = false;
                _reconciliation.resumeAuthorizationId = null;
                hideReconciliationPrompt();
                await loadRewards();
                await loadReconciliationStatus();
                var withdrawStatus = $('cfEarnWithdrawStatus');
                if (withdrawStatus) {
                    withdrawStatus.textContent = failedMessage;
                    withdrawStatus.hidden = false;
                }
                throw new Error(failedMessage);
            }
            if (!check.ok && check.status !== 202) {
                throw new Error((confirmed && confirmed.error) || 'Could not verify the transaction. Keep its hash and retry confirmation.');
            }
            if (setStatus) setStatus(confirmed && confirmed.status === 'confirming'
                ? 'The submitted payout was found; waiting for the required confirmation depth…'
                : 'The submitted payout was found; waiting for confirmation…');
            await wait(5000);
        }
        if (setStatus) setStatus('The submitted payout is still awaiting final confirmation. No balance change was made; retry reconciliation later or contact support.', 'error');
        return false;
    }
    async function reconcilePreviousWithdrawal() {
        if (BOOT.isGuest || !WALLET) { requiresLogin(); return; }
        var prompt = $('cfEarnReconcile');
        if (!prompt || prompt.hidden || !isWithdrawalId(_reconciliation.withdrawalId)) return;
        if (_reconciliation.busy) return;
        _reconciliation.busy = true;
        reconciliationButton(true, 'Checking…');
        reconciliationStatus('Checking the previously submitted payout. No new payment will be sent and no signature is requested.');
        try {
            var body = { withdrawal_id: _reconciliation.withdrawalId };
            if (_reconciliation.beforeBlock !== null) body.before_block = _reconciliation.beforeBlock;
            var result = await api('/community/api/rewards/withdrawals/reconciliation', {
                method: 'POST',
                body: JSON.stringify(body),
            });
            var data = result && result.data;
            var status = data && data.status;
            if (result.ok && data && status === 'transaction_found'
                    && String(data.withdrawal_id || '').toLowerCase() === _reconciliation.withdrawalId
                    && isTransactionHash(data.tx_hash)) {
                reconciliationStatus('The previously submitted transaction was found. Checking it with the existing confirmation process…');
                var confirmed = await confirmWithdrawalTransaction(
                    _reconciliation.withdrawalId, String(data.tx_hash).toLowerCase(),
                    function (message, kind) { reconciliationStatus(message, kind); });
                if (!confirmed && !prompt.hidden) reconciliationButton(false, 'Reconcile previous withdrawal');
                return;
            }
            if (result.ok && data && status === 'scanning'
                    && /^\d+$/.test(String(data.next_before_block))) {
                _reconciliation.beforeBlock = String(data.next_before_block);
                reconciliationStatus('This bounded page is complete. Continue search to check older blocks; no new payment will be sent and no signature is requested.');
                reconciliationButton(false, 'Continue search');
                return;
            }
            if (result.ok && data && status === 'not_found') {
                var scannedWithdrawalId = _reconciliation.withdrawalId;
                _reconciliation.beforeBlock = null;
                _reconciliation.resumeAuthorizationId = scannedWithdrawalId;
                hideReconciliationPrompt();
                var withdrawStatus = $('cfEarnWithdrawStatus');
                if (withdrawStatus) {
                    withdrawStatus.textContent = 'No matching payout was found across the configured history. No balance change was made; you can retry the normal withdrawal or contact support.';
                    withdrawStatus.hidden = false;
                }
                syncWithdrawalActionVisibility();
                return;
            }
            if (status === 'manual_review' || result.status === 409) {
                reconciliationStatus('The previous payout could not be matched exactly. No balance change was made. Please contact support for manual review.', 'error');
                reconciliationButton(false, 'Retry reconciliation');
                return;
            }
            reconciliationStatus('The reconciliation check could not complete. No balance change was made. Please retry, or contact support if the problem continues.', 'error');
            reconciliationButton(false, 'Retry reconciliation');
        } catch (_) {
            reconciliationStatus('The reconciliation check could not complete. No balance change was made. Please retry, or contact support if the problem continues.', 'error');
            reconciliationButton(false, 'Retry reconciliation');
        } finally {
            _reconciliation.busy = false;
            if (!prompt.hidden && $('cfEarnReconcileBtn') && $('cfEarnReconcileBtn').disabled) {
                reconciliationButton(false, _reconciliation.beforeBlock !== null ? 'Continue search' : 'Reconcile previous withdrawal');
            }
        }
    }

    function syncTodayHub() {
        var hub = $('cfTodayHub');
        if (!hub) return;
        var cardIds = ['cfDailyPrompt', 'cfDailyLearn', 'cfLearnDone'];
        var hasVisibleCard = cardIds.some(function (id) {
            var card = $(id);
            return !!(card && !card.hidden);
        });
        var earnCard = $('cfEarn');
        var cardsContent = $('cfCommunityCardsContent');
        var earnStillBelongsInHub = !isCommunityCardsMobileLayout()
            || !cardsContent || (earnCard && earnCard.parentNode !== cardsContent);
        if (earnCard && !earnCard.hidden && earnStillBelongsInHub) hasVisibleCard = true;
        hub.hidden = !hasVisibleCard;
    }

    function isCommunityCardsMobileLayout() {
        if (window && typeof window.matchMedia === 'function') {
            return window.matchMedia('(max-width: 899px)').matches;
        }
        return typeof window.innerWidth === 'number' && window.innerWidth < 900;
    }

    function moveCommunityCardsIntoModal() {
        var content = $('cfCommunityCardsContent');
        if (!content || typeof document.createComment !== 'function') return;
        ['cfEarn', 'cfVaultDonationCard', 'cfDailyHeading', 'cfRail'].forEach(function (id) {
            var node = $(id);
            if (!node || !node.parentNode || node.parentNode === content) return;
            var marker = document.createComment('cf-community-card-slot:' + id);
            node.parentNode.insertBefore(marker, node);
            _communityCardsSlots.push({ node: node, marker: marker });
            content.appendChild(node);
        });
    }

    function restoreCommunityCardsToFeed() {
        _communityCardsSlots.forEach(function (slot) {
            var marker = slot.marker;
            var parent = marker && marker.parentNode;
            if (!parent || !slot.node) return;
            parent.insertBefore(slot.node, marker);
            parent.removeChild(marker);
        });
        _communityCardsSlots = [];
    }

    function communityCardsKeydown(event) {
        if (!_communityCardsOpen || !event) return;
        if (event.key === 'Escape') {
            if (event.preventDefault) event.preventDefault();
            closeCommunityCards();
            return;
        }
        if (event.key !== 'Tab') return;
        var modal = $('cfCommunityCardsModal');
        if (!modal) return;
        var focusable = Array.prototype.slice.call(modal.querySelectorAll(
            'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )).filter(function (node) {
            return !node.hidden && (!node.getClientRects || node.getClientRects().length > 0);
        });
        if (!focusable.length) {
            if (event.preventDefault) event.preventDefault();
            if (modal.focus) modal.focus();
            return;
        }
        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
            if (event.preventDefault) event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            if (event.preventDefault) event.preventDefault();
            first.focus();
        }
    }

    function bindCommunityCardsModal() {
        var modal = $('cfCommunityCardsModal');
        if (!modal || _communityCardsModalBound) return;
        _communityCardsModalBound = true;
        modal.addEventListener('click', function (event) {
            if (event && event.target === modal) closeCommunityCards();
        });
        var closeButton = $('cfCommunityCardsClose');
        if (closeButton) closeButton.addEventListener('click', function () {
            closeCommunityCards();
        });
        document.addEventListener('keydown', communityCardsKeydown);
    }

    function syncCommunityCardsPlacement() {
        if (isCommunityCardsMobileLayout()) {
            moveCommunityCardsIntoModal();
        } else {
            if (_communityCardsOpen && !closeCommunityCards(false)) return;
            restoreCommunityCardsToFeed();
        }
        syncTodayHub();
    }

    function setupCommunityCardsLayout() {
        if (_communityCardsLayoutBound) return;
        _communityCardsLayoutBound = true;
        bindCommunityCardsModal();
        var media = typeof window.matchMedia === 'function'
            ? window.matchMedia('(max-width: 899px)') : null;
        syncCommunityCardsPlacement();
        if (media && typeof media.addEventListener === 'function') {
            media.addEventListener('change', syncCommunityCardsPlacement);
        } else if (media && typeof media.addListener === 'function') {
            media.addListener(syncCommunityCardsPlacement);
        } else if (typeof window.addEventListener === 'function') {
            window.addEventListener('resize', syncCommunityCardsPlacement);
        }
    }

    function openCommunityCards(trigger) {
        if (!isCommunityCardsMobileLayout()) return false;
        var modal = $('cfCommunityCardsModal');
        if (!modal) return false;
        bindCommunityCardsModal();
        moveCommunityCardsIntoModal();
        if (_communityCardsOpen) return true;
        _communityCardsReturnFocus = trigger || document.activeElement || null;
        _communityCardsBodyWasLocked = document.body.classList.contains('cf-modal-open');
        modal.hidden = false;
        modal.setAttribute('aria-hidden', 'false');
        if (_communityCardsReturnFocus && _communityCardsReturnFocus.setAttribute) {
            _communityCardsReturnFocus.setAttribute('aria-expanded', 'true');
        }
        document.body.classList.add('cf-modal-open');
        _communityCardsOpen = true;
        var closeButton = $('cfCommunityCardsClose');
        if (closeButton && closeButton.focus) closeButton.focus();
        return true;
    }

    function closeCommunityCards(restoreFocus) {
        if (!_communityCardsOpen) return true;
        var recovery = $('cfVaultDonationRecovery');
        if (recovery && !recovery.hidden) {
            donationStatus('Keep Community Cards open to verify this donation. Do not send another transfer.', 'error');
            return false;
        }
        var modal = $('cfCommunityCardsModal');
        if (modal) {
            modal.hidden = true;
            modal.setAttribute('aria-hidden', 'true');
        }
        var trigger = _communityCardsReturnFocus;
        if (trigger && trigger.setAttribute) trigger.setAttribute('aria-expanded', 'false');
        _communityCardsReturnFocus = null;
        _communityCardsOpen = false;
        if (!_communityCardsBodyWasLocked) document.body.classList.remove('cf-modal-open');
        _communityCardsBodyWasLocked = false;
        if (!isCommunityCardsMobileLayout()) restoreCommunityCardsToFeed();
        syncTodayHub();
        if (restoreFocus !== false && trigger && trigger.focus) trigger.focus();
        return true;
    }

    function renderRewards(data) {
        var card = $('cfEarn');
        if (!card) return;
        if (!data || !data.enabled) {
            card.hidden = true;
            syncTodayHub();
            return;
        }

        var amount = Number(data.balance_gd) || 0;
        var minimum = Number(data.min_payout_gd) || 0;
        var progress = Number(data.progress) || 0;
        // The server flag defaults on for signed-in members; the guest check
        // below still prevents personal balance details from being disclosed.
        var showBalance = data.show_balance === true;
        var configurationAvailable = data.configuration_available !== false;

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
            var canVerify = state === 'unverified' && !data.is_guest && !!WALLET;
            chip.disabled = !canVerify;
            chip.onclick = canVerify ? function () {
                // The rewards endpoint has already identified this member as
                // ineligible. Seed the shared router so its existing
                // Claim Free G$ entry point opens the verification onboarding
                // even if the separate claim-availability request is still
                // finishing. This only opens UI; it does not claim or sign.
                window._walletNeedsFV = true;
                if (window._walletFvReason !== 're_verification_needed') {
                    window._walletFvReason = 'not_verified';
                }
                openClaim();
            } : null;
            if (canVerify) {
                chip.setAttribute('aria-label', 'Verify to earn. Open Claim Free G$ verification options.');
            } else {
                chip.removeAttribute('aria-label');
            }
        }

        // Keep the optional server-sourced rate renderer/data contract intact;
        // the current Community template intentionally mounts no detail nodes.
        var ratesEl = $('cfEarnRates');
        var ratesToggle = $('cfEarnRatesToggle');
        if (ratesEl) {
            var rates = configurationAvailable ? (data.rates || null) : null;
            if (rates) {
                var rows = [
                    ['Comment on a post', rates.comment_in],
                    ['Someone comments on your post', rates.comment_out],
                    ['Someone reacts to your post', rates.reaction_out],
                    ['You react to a post', rates.reaction_in],
                    ['Daily Learn (per correct answer)', rates.daily_learn_per_correct != null
                        ? rates.daily_learn_per_correct : data.daily_learn_rate],
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
        // Keep the server-controlled budget/rate payload and renderer intact;
        // this card intentionally no longer mounts those optional detail panels.
        renderBudgetDisplay(data);

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
        _reconciliation.canWithdraw = !data.is_guest && data.eligible !== false
            && showBalance && !!WALLET;
        if (withdrawRow) {
            withdrawRow.hidden = !_reconciliation.canWithdraw
                || !_reconciliation.statusLoaded || !!_reconciliation.withdrawalId;
        }
        if (withdrawStatus) withdrawStatus.hidden = true;
        syncWithdrawalActionVisibility();
        var history = $('cfEarnHistory');
        var historyToggle = $('cfEarnHistoryToggle');
        var historyPanel = $('cfEarnHistoryPanel');
        if (history) history.hidden = !!data.is_guest || !WALLET;
        if (historyToggle) {
            historyToggle.hidden = !!data.is_guest || !WALLET;
            historyToggle.onclick = function () {
                var expanded = historyToggle.getAttribute('aria-expanded') === 'true';
                historyToggle.setAttribute('aria-expanded', expanded ? 'false' : 'true');
                historyPanel.hidden = expanded;
                if (!expanded && !_withdrawalHistory.loaded) loadWithdrawalHistory(false);
            };
        }
        var historyMore = $('cfEarnHistoryMore');
        if (historyMore) historyMore.onclick = function () {
            if (_withdrawalHistory.retryOlder) {
                _withdrawalHistory.retryOlder = false;
                loadWithdrawalHistory(true);
            } else loadWithdrawalHistory(_withdrawalHistory.loaded);
        };
        var hasBalance = !!(balanceWrap && !balanceWrap.hidden);
        var hasActions = !!((withdrawRow && !withdrawRow.hidden)
            || (history && !history.hidden));
        var actionColumn = $('cfEarnActions');
        if (actionColumn) actionColumn.hidden = !hasActions;
        var overview = $('cfEarnOverview');
        if (overview) {
            overview.classList.toggle('cf-earn-overview-no-balance', !hasBalance);
            overview.hidden = !hasBalance && !hasActions;
        }
        card.hidden = false;
        syncTodayHub();
    }

    async function checkWithdrawalEligibility() {
        if (!WALLET) { requiresLogin(); return Promise.resolve(); }
        var button = $('cfEarnWithdrawBtn');
        var status = $('cfEarnWithdrawStatus');
        var submissionAttempted = false;
        if (button) { button.disabled = true; button.textContent = 'Checking…'; }
        if (status) { status.hidden = true; status.textContent = ''; }
        var setStatus = function (message) {
            if (status) { status.textContent = message; status.hidden = false; }
        };
        try {
            var pendingKey = 'gm-community-withdrawal:' + String(WALLET).toLowerCase();
            var pending = null;
            try { pending = JSON.parse(localStorage.getItem(pendingKey) || 'null'); } catch (_) {}
            if (pending && pending.withdrawal_id && pending.tx_hash) {
                setStatus('Resuming confirmation for your submitted withdrawal…');
                await confirmWithdrawalTransaction(pending.withdrawal_id, pending.tx_hash, function (message) {
                    setStatus(message);
                });
                return;
            }

            if (_reconciliation.withdrawalId) {
                setStatus('A previous withdrawal is awaiting reconciliation. Use the reconciliation control before starting another wallet flow.');
                return;
            }
            if (!isWithdrawalId(_reconciliation.resumeAuthorizationId)) {
                await loadReconciliationStatus();
                if (_reconciliation.withdrawalId) {
                    setStatus('A previous withdrawal is awaiting reconciliation. Use the reconciliation control before starting another wallet flow.');
                    return;
                }
                if (!_reconciliation.statusLoaded) {
                    setStatus('The app could not safely check for an earlier withdrawal. Refresh the page before retrying.');
                    return;
                }
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
                await confirmWithdrawalTransaction(withdrawalId, txHash, function (message) {
                    setStatus(message);
                });
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
            submissionAttempted = true;
            _reconciliation.resumeAuthorizationId = null;
            txHash = await window.P2PWallet.sendTx(authData.vault_address,
                'function withdraw((address recipient,uint256 amount,bytes32 withdrawalId,uint256 nonce,uint256 deadline,bytes32 projectId) authorization,bytes issuerAuthorization)',
                [[auth.recipient, String(auth.amount), auth.withdrawalId, String(auth.nonce), String(auth.deadline), auth.projectId], authData.signature]);
            if (!txHash) throw new Error('The wallet did not return a transaction hash. Check your wallet activity before retrying.');
            await confirmWithdrawalTransaction(withdrawalId, txHash, function (message) {
                setStatus(message);
            });
        } catch (error) {
            setStatus((error && error.message) || 'Withdrawal could not be completed. Check your wallet activity before retrying.');
        } finally {
            if (button) { button.disabled = false; button.textContent = 'Withdraw'; }
            if (submissionAttempted) await loadReconciliationStatus();
        }
    }

    function formatGd(value) {
        var n = Number(value);
        if (!Number.isFinite(n)) return '';
        if (n % 1 === 0) return String(Math.round(n));
        return n.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
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
            if (reset) {
                _pinnedDonations = Array.isArray(result.data.pinned_posts)
                    ? result.data.pinned_posts : [];
                _pinnedDonationCursor = result.data.pinned_next_cursor || null;
            }
            // Guard a malformed payload: a server bug must not break the page.
            var rows = result.data.posts || [];
            if (reset && FOCUS_POST && FOCUS_POST.post_type !== 'system_vault_donation'
                    && FOCUS_POST.id != null
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

    function refresh() {
        loadFeed(true);
        loadMessageUnread();
    }

    function loadMore() {
        var button = $('cfLoadMore');
        if (button) { button.disabled = true; button.textContent = 'Loading…'; }
        loadFeed(false).then(function () {
            if (button) { button.disabled = false; button.textContent = 'Load more'; }
        });
    }

    function loadMorePinnedDonations() {
        if (_pinnedDonationLoading || !_pinnedDonationCursor) return Promise.resolve();
        _pinnedDonationLoading = true;
        var button = $('cfPinnedDonationMore');
        if (button) { button.disabled = true; button.textContent = 'Loading…'; }
        return api('/community/api/feed/pinned-donations?limit=10&cursor='
            + encodeURIComponent(_pinnedDonationCursor)).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not load pinned donations.');
            }
            _pinnedDonations = _pinnedDonations.concat(result.data.posts || []);
            _pinnedDonationCursor = result.data.next_cursor || null;
            renderPinnedDonations();
        }).catch(function (error) {
            toast((error && error.message) || 'Could not load pinned donations.', 'error');
        }).then(function () {
            _pinnedDonationLoading = false;
            if (button) { button.disabled = false; button.textContent = 'Load more pinned donations'; }
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
    var _messageUnreadLoading = false;
    var _messageUnreadPollTimer = null;
    var _messageUnreadLifecycleInitialized = false;
    var _messagePushPromptOpen = false;
    var _messagePushPromptBusy = false;
    var _messagePushPromptReady = false;
    var _messagePushPromptDismissKey = 'gmCommunityMessagePushPromptDismissedUntil_v1';
    var _notifications = [];
    var _notificationCursor = null;
    var _notificationUnread = 0;
    var _notificationLoading = false;
    var _notificationPanelOpen = false;
    var _notificationPollTimer = null;
    var _communityCardsSlots = [];
    var _communityCardsLayoutBound = false;
    var _communityCardsModalBound = false;
    var _communityCardsOpen = false;
    var _communityCardsReturnFocus = null;
    var _communityCardsBodyWasLocked = false;

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
        if (item.action === 'community-cards') {
            node.setAttribute('aria-haspopup', 'dialog');
            node.setAttribute('aria-controls', 'cfCommunityCardsModal');
            node.setAttribute('aria-expanded', 'false');
        }
        if (isLink) node.href = item.href;
        else node.type = 'button';
        var icon = el('span', 'cf-nav-icon', item.icon || '');
        icon.setAttribute('aria-hidden', 'true');
        node.appendChild(icon);
        node.appendChild(el('span', 'cf-nav-label', item.label || ''));
        node.onclick = function (event) {
            if (item.action === 'community-cards') {
                if (event && event.preventDefault) event.preventDefault();
                openCommunityCards(node);
                return;
            }
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
                openMessages();
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
        if (!WALLET || !NAV.some(function (item) { return item.action === 'messages'; })
                || _messageUnreadLoading) return Promise.resolve(false);
        _messageUnreadLoading = true;
        return api('/api/messages/conversations').then(function (result) {
            var data = result && result.data;
            var count = data && Number(data.unread_conversation_count);
            if (!result.ok || !data || data.success !== true || !isFinite(count) || count < 0) {
                return false;
            }
            setMessageUnread(count);
            return true;
        }).catch(function () {
            // A failed background read is not proof there are no unread threads.
            return false;
        }).then(function (success) {
            _messageUnreadLoading = false;
            return success;
        });
    }
    function initMessageUnreadRefresh() {
        if (!WALLET || _messageUnreadLifecycleInitialized
                || !NAV.some(function (item) { return item.action === 'messages'; })) return;
        _messageUnreadLifecycleInitialized = true;
        function refreshIfVisible() {
            if (!document.visibilityState || document.visibilityState === 'visible') {
                loadMessageUnread();
            }
        }
        document.addEventListener('visibilitychange', refreshIfVisible);
        window.addEventListener('focus', refreshIfVisible);
        _messageUnreadPollTimer = window.setInterval(refreshIfVisible, 5 * 60 * 1000);
    }

    function messagePromptWasDismissed() {
        try {
            var until = Number(localStorage.getItem(_messagePushPromptDismissKey) || 0);
            if (until > Date.now()) return true;
            localStorage.removeItem(_messagePushPromptDismissKey);
        } catch (_) {}
        return false;
    }

    function messagePromptStatus(message, isError) {
        var status = $('cfMessagePushStatus');
        if (!status) return;
        status.textContent = message || '';
        status.hidden = !message;
        status.classList.toggle('is-error', !!isError);
    }

    function browserCanRequestMessageNotifications() {
        return !!window.Notification
            && window.Notification.permission === 'default'
            && window.isSecureContext !== false
            && !!(window.navigator && window.navigator.serviceWorker)
            && !!window.PushManager;
    }

    function openMessages() {
        if (!WALLET) { requiresLogin(); return; }
        var prompt = $('cfMessagePushPrompt');
        if (!BOOT.messagesEnabled || !prompt || !window.GMCommunityPush
                || messagePromptWasDismissed()
                || !browserCanRequestMessageNotifications()) {
            window.location.href = '/messages';
            return;
        }
        _messagePushPromptOpen = true;
        _messagePushPromptBusy = false;
        _messagePushPromptReady = false;
        prompt.hidden = false;
        document.body.classList.add('cf-push-optin-open');
        var enable = $('cfMessagePushEnable');
        var later = $('cfMessagePushLater');
        var copy = $('cfMessagePushCopy');
        if (copy) copy.textContent = 'Enable notifications on this device so you’ll know when someone sends you a Community message. You can change this later.';
        if (enable) { enable.hidden = false; enable.disabled = true; enable.textContent = 'Checking…'; }
        if (later) { later.disabled = false; later.textContent = 'Not now'; }
        messagePromptStatus('Checking notification permission and device setup…', false);
        if (later && later.focus) later.focus();

        window.GMCommunityPush.inspect().then(function (result) {
            if (!_messagePushPromptOpen) return;
            result = result || { state: 'unavailable' };
            if (result.state === 'enabled') {
                window.location.href = '/messages';
                return;
            }
            if (result.state === 'needs-setup') {
                _messagePushPromptReady = true;
                if (enable) {
                    enable.disabled = false;
                    enable.textContent = window.Notification && window.Notification.permission === 'granted'
                        ? 'Finish setup' : 'Enable notifications';
                    enable.focus();
                }
                messagePromptStatus('', false);
                return;
            }

            _messagePushPromptReady = false;
            if (enable) { enable.hidden = true; enable.disabled = true; }
            if (later) later.textContent = 'Continue to Messages';
            var message = result.message || 'You can still open Messages without notifications.';
            if (result.state === 'permission-blocked') {
                message = 'Notifications are blocked for this site. Allow them in your browser settings, then retry from Messages. You can continue without enabling them.';
            } else if (result.state === 'unsupported') {
                message = 'This browser or connection does not support push notifications. You can still open Messages.';
            } else if (result.state === 'unconfigured') {
                message = 'Message notifications are not available right now. You can still open Messages.';
            }
            messagePromptStatus(message, result.state === 'storage-unavailable');
            if (later && later.focus) later.focus();
        }).catch(function (error) {
            if (!_messagePushPromptOpen) return;
            _messagePushPromptReady = false;
            if (enable) { enable.hidden = true; enable.disabled = true; }
            if (later) { later.textContent = 'Continue to Messages'; if (later.focus) later.focus(); }
            messagePromptStatus((error && error.message)
                || 'Notification setup could not be checked. You can still open Messages.', true);
        });
    }

    function declineMessageNotifications() {
        if (!_messagePushPromptOpen) { window.location.href = '/messages'; return; }
        _messagePushPromptOpen = false;
        _messagePushPromptReady = false;
        var prompt = $('cfMessagePushPrompt');
        if (prompt) prompt.hidden = true;
        document.body.classList.remove('cf-push-optin-open');
        try { localStorage.setItem(_messagePushPromptDismissKey, String(Date.now() + 30 * 86400000)); }
        catch (_) {}
        window.location.href = '/messages';
    }

    function enableMessageNotifications() {
        if (!_messagePushPromptOpen || !_messagePushPromptReady || _messagePushPromptBusy
                || !window.GMCommunityPush) return;
        _messagePushPromptBusy = true;
        var enable = $('cfMessagePushEnable');
        var later = $('cfMessagePushLater');
        if (enable) { enable.disabled = true; enable.textContent = 'Setting up…'; }
        if (later) later.disabled = true;
        messagePromptStatus('Allow notifications in the browser prompt to continue.', false);

        // The shared helper calls Notification.requestPermission immediately,
        // synchronously within this click handler, before any network request.
        window.GMCommunityPush.enable().then(function () {
            if (!_messagePushPromptOpen) return;
            messagePromptStatus('Notifications are enabled on this device. Opening Messages…', false);
            window.location.href = '/messages';
        }).catch(function (error) {
            if (!_messagePushPromptOpen) return;
            var denied = error && error.code === 'permission_denied';
            _messagePushPromptBusy = false;
            _messagePushPromptReady = !denied;
            if (enable) {
                enable.hidden = !!denied;
                enable.disabled = !!denied;
                enable.textContent = denied ? 'Enable notifications' : 'Retry setup';
            }
            if (later) { later.disabled = false; later.textContent = 'Continue to Messages'; }
            messagePromptStatus((error && error.message)
                || 'Could not finish notification setup. You can still open Messages and retry later.', true);
            if (denied && later && later.focus) later.focus();
        });
    }

    function initMessagePushPrompt() {
        if (!WALLET || !$('cfMessagePushPrompt')) return;
        document.addEventListener('keydown', function (event) {
            if (!_messagePushPromptOpen) return;
            if (event.key === 'Escape') {
                if (event.preventDefault) event.preventDefault();
                declineMessageNotifications();
                return;
            }
            if (event.key !== 'Tab') return;
            var enable = $('cfMessagePushEnable');
            var later = $('cfMessagePushLater');
            var focusable = [enable, later].filter(function (node) {
                return node && !node.hidden && !node.disabled;
            });
            if (!focusable.length) return;
            var first = focusable[0], last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault(); last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault(); first.focus();
            }
        });
    }

    // ── Private in-app Community notifications ──────────────────────────────
    function setNotificationUnread(count) {
        var button = $('cfNotificationButton');
        var badge = $('cfNotificationBadge');
        if (!button || !badge) return;
        _notificationUnread = Math.max(0, Number(count) || 0);
        badge.textContent = _notificationUnread > 99 ? '99+' : String(_notificationUnread);
        badge.hidden = _notificationUnread === 0;
        button.setAttribute('aria-label', _notificationUnread
            ? 'Notifications, ' + _notificationUnread + ' unread'
            : 'Notifications');
        var markAll = $('cfNotificationMarkAll');
        if (markAll) markAll.disabled = _notificationUnread === 0;
    }

    function notificationMessage(item) {
        var actor = esc(item && item.actor_username || 'Community member')
            .trim().replace(/^@+/, '').slice(0, 80) || 'Community member';
        var type = String(item && item.notification_type || '');
        if (type === 'reaction') {
            var reaction = {
                like: '👍', love: '❤️', haha: '😂', wow: '😮', sad: '😢', angry: '😡'
            }[String(item.reaction || '')] || '';
            return actor + ' reacted' + (reaction ? ' ' + reaction : '') + ' to your post.';
        }
        if (type === 'message') return actor + ' sent you a message.';
        if (type === 'comment') return actor + ' commented on your post.';
        if (type === 'reply') return actor + ' replied to your comment.';
        if (type === 'post_reply') return actor + ' replied in your post.';
        return actor + ' interacted with your post.';
    }

    function notificationIcon(item) {
        if (item && item.notification_type === 'message') return '✉';
        if (item && item.notification_type === 'reaction') {
            return ({ like: '👍', love: '❤️', haha: '😂', wow: '😮', sad: '😢', angry: '😡' })[
                String(item.reaction || '')] || '🔔';
        }
        if (item && item.notification_type === 'reply') return '↩';
        return '💬';
    }

    function renderNotifications(message) {
        var list = $('cfNotificationList');
        var more = $('cfNotificationMore');
        if (!list) return;
        list.textContent = '';
        if (message) {
            list.appendChild(el('p', 'cf-notification-empty', message));
            if (more) more.hidden = true;
            return;
        }
        if (!_notifications.length) {
            list.appendChild(el('p', 'cf-notification-empty', 'You’re all caught up.'));
        } else {
            _notifications.forEach(function (item) {
                var wrapper = el('div', 'cf-notification-row');
                wrapper.setAttribute('role', 'listitem');
                var button = el('button', 'cf-notification-item'
                    + (item.read_at ? '' : ' is-unread'));
                button.type = 'button';
                button.setAttribute('aria-label', notificationMessage(item)
                    + (item.read_at ? '' : ' Unread'));
                var icon = el('span', 'cf-notification-icon', notificationIcon(item));
                icon.setAttribute('aria-hidden', 'true');
                var copy = el('span', 'cf-notification-copy');
                copy.appendChild(el('span', 'cf-notification-message', notificationMessage(item)));
                copy.appendChild(el('span', 'cf-notification-time',
                    item.created_at ? timeAgo(item.created_at) : 'Recently'));
                var dot = el('span', 'cf-notification-unread-dot');
                dot.setAttribute('aria-hidden', 'true');
                dot.hidden = !!item.read_at;
                button.appendChild(icon);
                button.appendChild(copy);
                button.appendChild(dot);
                button.onclick = function () { openNotification(item); };
                wrapper.appendChild(button);
                list.appendChild(wrapper);
            });
        }
        if (more) more.hidden = !_notificationCursor;
        var markAll = $('cfNotificationMarkAll');
        if (markAll) markAll.disabled = _notificationUnread === 0;
    }

    function loadNotifications(append) {
        if (!WALLET || _notificationLoading) return Promise.resolve(false);
        var cursor = append ? _notificationCursor : null;
        if (append && !cursor) return Promise.resolve(false);
        _notificationLoading = true;
        var list = $('cfNotificationList');
        var more = $('cfNotificationMore');
        if (list) list.setAttribute('aria-busy', 'true');
        if (more && append) { more.disabled = true; more.textContent = 'Loading…'; }
        var query = '/community/api/notifications?limit=20'
            + (cursor ? '&cursor=' + encodeURIComponent(cursor) : '');
        return api(query).then(function (result) {
            if (!result.ok || !result.data || !result.data.success) {
                throw new Error((result.data && result.data.error)
                    || 'Notifications are temporarily unavailable.');
            }
            var incoming = result.data.notifications || [];
            _notifications = append ? _notifications.concat(incoming) : incoming;
            _notificationCursor = result.data.next_cursor || null;
            setNotificationUnread(result.data.unread_count);
            renderNotifications();
            return true;
        }).catch(function () {
            if (!_notifications.length) {
                renderNotifications('Notifications are temporarily unavailable. Please try again.');
            }
            return false;
        }).then(function (success) {
            _notificationLoading = false;
            if (list) list.setAttribute('aria-busy', 'false');
            if (more) { more.disabled = false; more.textContent = 'Load older notifications'; }
            return success;
        });
    }

    function openNotifications() {
        if (!WALLET) { requiresLogin(); return; }
        var panel = $('cfNotificationPanel');
        var button = $('cfNotificationButton');
        if (!panel || !button) return;
        _notificationPanelOpen = true;
        panel.hidden = false;
        button.setAttribute('aria-expanded', 'true');
        loadNotifications(false);
        var close = panel.querySelector('.cf-notification-close');
        if (close && close.focus) close.focus();
    }

    function closeNotifications(restoreFocus) {
        var panel = $('cfNotificationPanel');
        var button = $('cfNotificationButton');
        _notificationPanelOpen = false;
        if (panel) panel.hidden = true;
        if (button) {
            button.setAttribute('aria-expanded', 'false');
            if (restoreFocus && button.focus) button.focus();
        }
    }

    function toggleNotifications() {
        if (_notificationPanelOpen) closeNotifications(true);
        else openNotifications();
    }

    function openNotification(item) {
        if (!item || !/^\d+$/.test(String(item.id || ''))) return;
        var destination;
        if (item.notification_type === 'message') {
            if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
                    .test(String(item.conversation_id || ''))) return;
            destination = '/messages?conversation=' + encodeURIComponent(item.conversation_id);
        } else {
            if (!/^\d+$/.test(String(item.post_id || ''))) return;
            destination = '/community/p/' + encodeURIComponent(item.post_id)
                + '#post-' + encodeURIComponent(item.post_id);
        }
        if (!item.read_at) {
            item.read_at = new Date().toISOString();
            setNotificationUnread(_notificationUnread - 1);
            renderNotifications();
            // Keep the private read receipt alive while the browser navigates.
            fetch('/community/api/notifications/' + encodeURIComponent(item.id) + '/read', {
                method: 'POST', credentials: 'same-origin', keepalive: true,
                headers: { 'Content-Type': 'application/json' }, body: '{}'
            }).catch(function () {});
        }
        closeNotifications(false);
        window.location.href = destination;
    }

    function markAllNotificationsRead() {
        if (!WALLET || !_notificationUnread) return Promise.resolve(false);
        var button = $('cfNotificationMarkAll');
        if (button) button.disabled = true;
        return api('/community/api/notifications/read-all', { method: 'POST', body: '{}' })
            .then(function (result) {
                if (!result.ok || !result.data || !result.data.success) {
                    throw new Error((result.data && result.data.error)
                        || 'Could not update notifications.');
                }
                var now = new Date().toISOString();
                _notifications.forEach(function (item) { item.read_at = item.read_at || now; });
                setNotificationUnread(0);
                renderNotifications();
                return true;
            }).catch(function () {
                if (button) button.disabled = _notificationUnread === 0;
                toast('Could not mark notifications as read. Please try again.', 'error');
                return false;
            });
    }

    function loadMoreNotifications() { return loadNotifications(true); }

    function initNotifications() {
        if (!WALLET || !$('cfNotificationButton')) return;
        loadNotifications(false);
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && _notificationPanelOpen) closeNotifications(true);
        });
        document.addEventListener('click', function (event) {
            if (!_notificationPanelOpen) return;
            var panel = $('cfNotificationPanel');
            var button = $('cfNotificationButton');
            if (panel && button && !panel.contains(event.target)
                    && !button.contains(event.target)) closeNotifications(false);
        });
        var refreshIfVisible = function () {
            if (!_notificationPanelOpen
                    && (!document.visibilityState || document.visibilityState === 'visible')) {
                loadNotifications(false);
            }
        };
        document.addEventListener('visibilitychange', refreshIfVisible);
        window.addEventListener('focus', refreshIfVisible);
        _notificationPollTimer = window.setInterval(refreshIfVisible, 5 * 60 * 1000);
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

    var _airtimeVerificationBridgeBound = false;
    function bindAirtimeVerificationBridge() {
        if (_airtimeVerificationBridgeBound || typeof window.addEventListener !== 'function') return;
        _airtimeVerificationBridgeBound = true;
        window.addEventListener('message', function (event) {
            var frame = $('cfAirtimeFrame');
            var modal = $('cfAirtimeModal');
            var data = event && event.data;
            if (!frame || !modal || modal.hidden || !event
                    || event.origin !== window.location.origin
                    || event.source !== frame.contentWindow
                    || !data || typeof data !== 'object'
                    || data.channel !== 'gm-reloadly-embed' || data.version !== 1
                    || data.type !== 'request-community-verification'
                    || typeof data.requestId !== 'string'
                    || !/^airtime-fv-[a-z0-9-]{1,80}$/i.test(data.requestId)
                    || typeof window.openClaimOrVerify !== 'function') return;

            closeAirtime();
            window._walletNeedsFV = true;
            if (window._walletFvReason !== 're_verification_needed') {
                window._walletFvReason = 'not_verified';
            }
            openClaim();
            try {
                event.source.postMessage({
                    channel: 'gm-reloadly-embed',
                    version: 1,
                    type: 'community-verification-ack',
                    requestId: data.requestId
                }, event.origin);
            } catch (_) {}
        });
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

    function bind() {
        bindAirtimeVerificationBridge();
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
            if (event.key === 'Escape') {
                if (_activeReactionPicker) {
                    closeReactionPicker(true);
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

    function donationStatus(message, kind) {
        var status = $('cfVaultDonationStatus');
        if (!status) return;
        status.hidden = !message;
        status.textContent = message || '';
        status.classList.toggle('is-error', kind === 'error');
        status.classList.toggle('is-success', kind === 'success');
    }

    function donationStorageKey() {
        return 'gm-community-vault-donation:' + String(WALLET || '').toLowerCase();
    }

    function readPendingVaultDonation() {
        if (!WALLET) return null;
        if (_activeVaultDonation && /^[0-9a-f-]{36}$/i.test(String(_activeVaultDonation.donation_id || ''))
                && /^0x[0-9a-f]{64}$/i.test(String(_activeVaultDonation.tx_hash || ''))) {
            return _activeVaultDonation;
        }
        try {
            var value = JSON.parse(localStorage.getItem(donationStorageKey()) || 'null');
            if (value && /^[0-9a-f-]{36}$/i.test(String(value.donation_id || ''))
                    && /^0x[0-9a-f]{64}$/i.test(String(value.tx_hash || ''))) return value;
        } catch (_) {}
        return null;
    }

    function savePendingVaultDonation(value) {
        _activeVaultDonation = value;
        try { localStorage.setItem(donationStorageKey(), JSON.stringify(value)); } catch (_) {}
    }

    function clearPendingVaultDonation() {
        _activeVaultDonation = null;
        try { localStorage.removeItem(donationStorageKey()); } catch (_) {}
    }

    function openVaultDonation() {
        if (!WALLET) { requiresLogin(); return Promise.resolve(false); }
        var panel = $('cfVaultDonationFormWrap');
        if (panel) panel.hidden = false;
        var opener = $('cfVaultDonationOpen');
        if (opener) opener.setAttribute('aria-expanded', 'true');
        var name = $('cfVaultDonationName');
        if (name && name.focus) name.focus();
        var address = $('cfVaultDonationAddress');
        var retry = $('cfVaultDonationRetry');
        if (retry) retry.hidden = true;
        donationStatus('Verifying the shared Community Withdrawal Vault on Celo…');
        return api('/community/api/vault-donations/config').then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Donations are not available right now.');
            }
            _vaultDonationConfig = result.data;
            if (name && !name.value && result.data.community_display_name) {
                name.value = result.data.community_display_name;
            }
            if (address) {
                address.hidden = false;
                address.textContent = 'Verified recipient: ' + result.data.vault_address
                    + ' · G$ token: ' + result.data.token_address
                    + ' · Celo Mainnet (42220)';
            }
            if (retry) retry.hidden = true;
            donationStatus('Vault verified. Complete the form and review the exact donation before opening your wallet.');
            return true;
        }).catch(function (error) {
            donationStatus((error && error.message) || 'Could not verify the Community Vault.', 'error');
            if (retry) retry.hidden = false;
            return false;
        });
    }

    function retryVaultDonationConfig() {
        return openVaultDonation();
    }

    function closeVaultDonation() {
        if (_vaultDonationBusy) return;
        var recovery = $('cfVaultDonationRecovery');
        if (_vaultDonationIntent && recovery && !recovery.hidden) {
            donationStatus('Keep this verification panel open until you enter the transaction hash. Do not send another transfer.', 'error');
            return;
        }
        var intent = _vaultDonationIntent;
        _vaultDonationIntent = null;
        if (intent && intent.donation_id) {
            api('/community/api/vault-donations/cancel', {
                method: 'POST', body: JSON.stringify({ donation_id: intent.donation_id })
            });
        }
        var entry = $('cfVaultDonationEntry');
        var review = $('cfVaultDonationReview');
        if (entry) entry.hidden = false;
        if (review) review.hidden = true;
        if (recovery) recovery.hidden = true;
        var recoveryHash = $('cfVaultDonationTxHash');
        if (recoveryHash) { recoveryHash.disabled = true; recoveryHash.value = ''; }
        var panel = $('cfVaultDonationFormWrap');
        if (panel) panel.hidden = true;
        var button = $('cfVaultDonationOpen');
        if (button) button.setAttribute('aria-expanded', 'false');
        var statusClose = $('cfVaultDonationStatusClose');
        if (statusClose) statusClose.hidden = true;
        donationStatus('');
        if (button && button.focus) button.focus();
    }

    function donationAmountBaseUnits(value, decimals) {
        var text = String(value || '').trim();
        if (!/^\d+(?:\.\d+)?$/.test(text)) throw new Error('Enter a valid G$ amount.');
        var parts = text.split('.');
        var places = Number(decimals);
        var fraction = parts[1] || '';
        if (!Number.isInteger(places) || places < 0 || places > 36 || fraction.length > places) {
            throw new Error('Amount has too many decimal places for G$.');
        }
        var scale = 10n ** BigInt(places);
        var base = BigInt(parts[0]) * scale
            + BigInt((fraction + '0'.repeat(places)).slice(0, places) || '0');
        if (base < 1000n * scale) throw new Error('The minimum donation is 1,000 G$.');
        if (base > ((1n << 256n) - 1n)) throw new Error('Donation amount exceeds the token transaction limit.');
        return base.toString();
    }

    function checkPendingVaultDonation(explicit) {
        if (_vaultDonationBusy) return Promise.resolve(false);
        var pending = explicit || readPendingVaultDonation();
        if (!pending) {
            donationStatus('No submitted donation transaction is saved on this device.', 'error');
            return Promise.resolve(false);
        }
        _vaultDonationBusy = true;
        var check = $('cfVaultDonationCheck');
        if (check) check.hidden = true;
        var statusClose = $('cfVaultDonationStatusClose');
        if (statusClose) statusClose.hidden = false;
        donationStatus('Checking the same submitted transaction. This will not send another transfer.');
        function wait(ms) { return new Promise(function (resolve) { window.setTimeout(resolve, ms); }); }
        function poll(attempt) {
            return api('/community/api/vault-donations/confirm', {
                method: 'POST',
                body: JSON.stringify({ donation_id: pending.donation_id, tx_hash: pending.tx_hash })
            }).then(function (result) {
                var data = result.data || {};
                if (data.success && data.status === 'confirmed') {
                    clearPendingVaultDonation();
                    _vaultDonationIntent = null;
                    var entry = $('cfVaultDonationEntry');
                    var review = $('cfVaultDonationReview');
                    var recovery = $('cfVaultDonationRecovery');
                    if (entry) entry.hidden = false;
                    if (review) review.hidden = true;
                    if (recovery) recovery.hidden = true;
                    var recoveryHash = $('cfVaultDonationTxHash');
                    if (recoveryHash) { recoveryHash.disabled = true; recoveryHash.value = ''; }
                    donationStatus('Donation confirmed: ' + (data.amount_gd || '')
                        + ' G$ sent to the Community Withdrawal Vault. Sytem has pinned your acknowledgment.', 'success');
                    if (check) check.hidden = true;
                    if (statusClose) statusClose.hidden = !!entry;
                    loadFeed(true);
                    return true;
                }
                if (data.status === 'pending' || data.status === 'confirming'
                        || data.status === 'processing' || data.retryable) {
                    var detail = data.confirmations != null
                        ? ' (' + data.confirmations + '/' + data.required_confirmations + ' confirmations)'
                        : '';
                    donationStatus((data.message || 'Waiting for donation confirmation') + detail
                        + '. Do not submit another transfer.');
                    if (attempt < 19) return wait(3000).then(function () { return poll(attempt + 1); });
                    donationStatus('The transaction is still being processed. Keep this hash and use “Check donation status”; do not send again.');
                    if (check) check.hidden = false;
                    return false;
                }
                donationStatus(data.error || 'Could not verify this transaction. Keep the hash and contact support before trying another transfer.', 'error');
                if (check) check.hidden = false;
                return false;
            }).catch(function () {
                if (attempt < 19) return wait(3000).then(function () { return poll(attempt + 1); });
                donationStatus('Network confirmation is delayed. Use “Check donation status” later; do not send again.');
                if (check) check.hidden = false;
                return false;
            });
        }
        return poll(0).then(function (confirmed) {
            _vaultDonationBusy = false;
            var submit = $('cfVaultDonationSubmit');
            if (submit) submit.disabled = !!readPendingVaultDonation();
            return confirmed;
        });
    }

    function renderVaultDonationReview(intent) {
        var entry = $('cfVaultDonationEntry');
        var review = $('cfVaultDonationReview');
        if (!entry || !review || !intent) return false;
        $('cfVaultDonationReviewAmount').textContent = String(intent.amount_gd || '') + ' G$';
        $('cfVaultDonationReviewName').textContent = String(intent.public_name || '');
        $('cfVaultDonationReviewToken').textContent = String(intent.token_address || '');
        $('cfVaultDonationReviewRecipient').textContent = String(intent.vault_address || '');
        var linkedinCell = $('cfVaultDonationReviewLinkedIn');
        var linkedin = String(intent.linkedin_url || '');
        if (linkedinCell) {
            linkedinCell.textContent = '';
            if (/^https:\/\/(?:www\.)?linkedin\.com\/in\/[A-Za-z0-9_%.-]{1,120}\/?$/i.test(linkedin)) {
                var link = el('a', null, linkedin);
                link.href = linkedin;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                linkedinCell.appendChild(link);
            } else {
                linkedinCell.textContent = linkedin;
            }
        }
        var network = $('cfVaultDonationReviewNetwork');
        if (network) network.textContent = 'Celo Mainnet (' + String(intent.chain_id || 42220) + ')';
        entry.hidden = true;
        review.hidden = false;
        var recovery = $('cfVaultDonationRecovery');
        if (recovery) recovery.hidden = true;
        donationStatus('Review every detail below. No transfer has been sent yet.');
        var title = $('cfVaultDonationReviewTitle');
        if (title && title.focus) title.focus();
        return true;
    }

    function submitVaultDonation(event) {
        if (event && event.preventDefault) event.preventDefault();
        if (!WALLET) { requiresLogin(); return false; }
        if (_vaultDonationBusy) return false;
        if (readPendingVaultDonation()) {
            donationStatus('A prior donation transaction is still being checked. Check its status before starting another donation.', 'error');
            var check = $('cfVaultDonationCheck');
            if (check) check.hidden = false;
            return false;
        }
        if (_vaultDonationIntent) {
            renderVaultDonationReview(_vaultDonationIntent);
            return false;
        }
        var form = $('cfVaultDonationForm');
        if (!form || !form.reportValidity()) return false;
        var name = String(($('cfVaultDonationName') || {}).value || '').trim();
        var linkedin = String(($('cfVaultDonationLinkedIn') || {}).value || '').trim();
        var amount = String(($('cfVaultDonationAmount') || {}).value || '').trim();
        var consent = $('cfVaultDonationConsent');
        if (!consent || !consent.checked) {
            donationStatus('Consent to the public acknowledgment is required.', 'error');
            return false;
        }
        _vaultDonationBusy = true;
        var submit = $('cfVaultDonationSubmit');
        if (submit) submit.disabled = true;
        var work = _vaultDonationConfig ? Promise.resolve(_vaultDonationConfig) : openVaultDonation().then(function (ok) {
            if (!ok) throw new Error('Vault verification failed. No transfer was sent.');
            return _vaultDonationConfig;
        });
        work.then(function (config) {
            var amountBase = donationAmountBaseUnits(amount, config.decimals);
            donationStatus('Preparing your review. No transfer has been sent yet.');
            return api('/community/api/vault-donations/intent', {
                method: 'POST',
                body: JSON.stringify({ name: name, linkedin_url: linkedin, amount_gd: amount, consent: true })
            }).then(function (result) {
                if (!result.data || !result.data.success) {
                    throw new Error((result.data && result.data.error) || 'Could not prepare the donation.');
                }
                var intent = result.data;
                if (String(intent.amount_base_units) !== amountBase) {
                    api('/community/api/vault-donations/cancel', {
                        method: 'POST', body: JSON.stringify({ donation_id: intent.donation_id })
                    });
                    throw new Error('The prepared G$ amount changed. No transfer was sent; reload the verified vault details and try again.');
                }
                _vaultDonationIntent = intent;
                if (!renderVaultDonationReview(intent)) {
                    _vaultDonationIntent = null;
                    throw new Error('Could not show the donation review. No transfer was sent.');
                }
            });
        }).catch(function (error) {
            _vaultDonationBusy = false;
            donationStatus((error && error.message) || 'Could not prepare the donation. No transfer was sent.', 'error');
            var retry = $('cfVaultDonationRetry');
            if (_vaultDonationConfig && retry) retry.hidden = true;
        }).then(function () {
            _vaultDonationBusy = false;
            if (submit) submit.disabled = !!readPendingVaultDonation();
        });
        return false;
    }

    function editVaultDonation() {
        if (_vaultDonationBusy) return;
        var intent = _vaultDonationIntent;
        _vaultDonationIntent = null;
        if (intent && intent.donation_id) {
            api('/community/api/vault-donations/cancel', {
                method: 'POST', body: JSON.stringify({ donation_id: intent.donation_id })
            });
        }
        var entry = $('cfVaultDonationEntry');
        var review = $('cfVaultDonationReview');
        var recovery = $('cfVaultDonationRecovery');
        if (entry) entry.hidden = false;
        if (review) review.hidden = true;
        if (recovery) recovery.hidden = true;
        var recoveryHash = $('cfVaultDonationTxHash');
        if (recoveryHash) { recoveryHash.disabled = true; recoveryHash.value = ''; }
        donationStatus('You can edit the donation details. No transfer has been sent.');
        var name = $('cfVaultDonationName');
        if (name && name.focus) name.focus();
    }

    function setDonationReviewActionsDisabled(disabled) {
        ['cfVaultDonationReviewEdit', 'cfVaultDonationReviewCancel', 'cfVaultDonationContinue']
            .forEach(function (id) {
                var button = $(id);
                if (button) button.disabled = !!disabled;
            });
    }

    function isVaultDonationUserRejection(error) {
        var queue = [error];
        var seen = [];
        var messages = [];
        var rejected = false;
        while (queue.length) {
            var item = queue.shift();
            if (!item) continue;
            if (typeof item === 'string') {
                messages.push(item);
                continue;
            }
            if (typeof item !== 'object' || seen.indexOf(item) >= 0) continue;
            seen.push(item);
            var code = String(item.code == null ? '' : item.code).toUpperCase();
            if (code === '4001' || code === 'ACTION_REJECTED') rejected = true;
            if (typeof item.message === 'string') messages.push(item.message);
            ['cause', 'data', 'error', 'originalError', 'info'].forEach(function (key) {
                if (item[key] && typeof item[key] === 'object') queue.push(item[key]);
                else if (typeof item[key] === 'string') messages.push(item[key]);
            });
        }
        return rejected || /user\s+(?:rejected|denied|cancelled|canceled)|(?:cancelled|canceled)\s+by\s+user|request\s+(?:was\s+)?rejected|rejected\s+by\s+(?:the\s+)?user/i.test(messages.join(' '));
    }

    function recordVaultDonationHash(intent, rawHash) {
        var txHash = String(rawHash || '').toLowerCase();
        if (!/^0x[0-9a-f]{64}$/.test(txHash)) return null;
        var pending = { donation_id: intent.donation_id, tx_hash: txHash };
        savePendingVaultDonation(pending);
        return pending;
    }

    function continueVaultDonationToWallet() {
        if (!WALLET) { requiresLogin(); return Promise.resolve(false); }
        if (_vaultDonationBusy) return Promise.resolve(false);
        if (readPendingVaultDonation()) {
            donationStatus('A prior transaction is still being checked. Do not send another transfer.', 'error');
            return Promise.resolve(false);
        }
        var intent = _vaultDonationIntent;
        if (!intent) {
            donationStatus('Prepare and review a donation before opening your wallet.', 'error');
            return Promise.resolve(false);
        }
        if (intent.expires_at && Date.now() >= Date.parse(intent.expires_at)) {
            api('/community/api/vault-donations/cancel', {
                method: 'POST', body: JSON.stringify({ donation_id: intent.donation_id })
            });
            _vaultDonationIntent = null;
            var entry = $('cfVaultDonationEntry');
            var review = $('cfVaultDonationReview');
            if (entry) entry.hidden = false;
            if (review) review.hidden = true;
            donationStatus('The review request expired before wallet approval. No transfer was sent; review the details again.', 'error');
            return Promise.resolve(false);
        }
        if (!window.P2PWallet || typeof window.P2PWallet.sendTx !== 'function') {
            donationStatus('Wallet signing is unavailable. No transfer was sent.', 'error');
            return Promise.resolve(false);
        }
        _vaultDonationBusy = true;
        setDonationReviewActionsDisabled(true);
        try {
            window.P2PWallet.configure({
                wallet: WALLET,
                chainId: Number(intent.chain_id || 42220),
                loginMethod: BOOT.loginMethod || ''
            });
        } catch (error) {
            _vaultDonationBusy = false;
            setDonationReviewActionsDisabled(false);
            donationStatus((error && error.message) || 'Wallet signing is unavailable. No transfer was sent.', 'error');
            return Promise.resolve(false);
        }
        donationStatus('Review and approve the exact transfer in your wallet.');
        var broadcastAttempted = false;
        var submittedPending = null;
        return Promise.resolve().then(function () {
            return window.P2PWallet.sendTx(
                intent.token_address,
                'function transfer(address to,uint256 value) returns (bool)',
                [intent.vault_address, String(intent.amount_base_units)],
                '0x0',
                {
                    waitForReceipt: false,
                    noRetryAfterSendRequest: true,
                    onBroadcastStarted: function () { broadcastAttempted = true; },
                    onTransactionHash: function (txHash) {
                        submittedPending = recordVaultDonationHash(intent, txHash);
                    }
                }
            );
        }).then(function (txHash) {
            txHash = String(txHash || '').toLowerCase();
            if (!/^0x[0-9a-f]{64}$/.test(txHash)) {
                throw new Error('Wallet did not return a valid transaction hash.');
            }
            var pending = submittedPending || recordVaultDonationHash(intent, txHash);
            if (!pending) throw new Error('The submitted transaction hash could not be saved for verification.');
            _vaultDonationIntent = null;
            var entry = $('cfVaultDonationEntry');
            var review = $('cfVaultDonationReview');
            var recovery = $('cfVaultDonationRecovery');
            if (entry) entry.hidden = true;
            if (review) review.hidden = true;
            if (recovery) recovery.hidden = true;
            var recoveryHash = $('cfVaultDonationTxHash');
            if (recoveryHash) { recoveryHash.disabled = true; recoveryHash.value = ''; }
            _vaultDonationBusy = false;
            return checkPendingVaultDonation(pending);
        }).catch(function (error) {
            _vaultDonationBusy = false;
            var message = String(error && error.message || '');
            if (submittedPending) {
                _vaultDonationIntent = null;
                var savedEntry = $('cfVaultDonationEntry');
                var savedReview = $('cfVaultDonationReview');
                var savedRecovery = $('cfVaultDonationRecovery');
                if (savedEntry) savedEntry.hidden = true;
                if (savedReview) savedReview.hidden = true;
                if (savedRecovery) savedRecovery.hidden = true;
                var savedHash = $('cfVaultDonationTxHash');
                if (savedHash) { savedHash.disabled = true; savedHash.value = ''; }
                donationStatus('A transaction hash was saved. Checking that same transfer now; no second transfer will be sent.');
                return checkPendingVaultDonation(submittedPending);
            }
            if (isVaultDonationUserRejection(error)) {
                setDonationReviewActionsDisabled(false);
                donationStatus('You cancelled the wallet request. No transfer was sent; you can continue or edit these details.', 'error');
                return false;
            }
            if (broadcastAttempted) {
                var recovery = $('cfVaultDonationRecovery');
                if (recovery) recovery.hidden = false;
                var recoveryHash = $('cfVaultDonationTxHash');
                if (recoveryHash) { recoveryHash.disabled = false; recoveryHash.focus(); }
                setDonationReviewActionsDisabled(true);
                donationStatus('The wallet did not return a transaction hash.', 'error');
            } else {
                setDonationReviewActionsDisabled(false);
                var noSendSuffix = /no transaction was sent|no transfer was sent/i.test(message)
                    ? '' : ' No transfer was sent.';
                donationStatus((message || 'Wallet setup or preflight failed.') + noSendSuffix
                    + ' You can retry or edit these details.', 'error');
            }
            return false;
        });
    }

    function resumeVaultDonationFromHash() {
        if (_vaultDonationBusy) return Promise.resolve(false);
        if (!_vaultDonationIntent) {
            donationStatus('There is no prepared donation request to match this transaction.', 'error');
            return Promise.resolve(false);
        }
        var input = $('cfVaultDonationTxHash');
        var txHash = String((input && input.value) || '').trim().toLowerCase();
        if (!/^0x[0-9a-f]{64}$/.test(txHash)) {
            if (input && input.reportValidity) input.reportValidity();
            donationStatus('Enter a valid Celo transaction hash from your wallet activity.', 'error');
            return Promise.resolve(false);
        }
        var pending = { donation_id: _vaultDonationIntent.donation_id, tx_hash: txHash };
        savePendingVaultDonation(pending);
        _vaultDonationIntent = null;
        var entry = $('cfVaultDonationEntry');
        var review = $('cfVaultDonationReview');
        var recovery = $('cfVaultDonationRecovery');
        if (entry) entry.hidden = true;
        if (review) review.hidden = true;
        if (recovery) recovery.hidden = true;
        if (input) { input.disabled = true; input.value = ''; }
        return checkPendingVaultDonation(pending);
    }

    function init() {
        // Guests read the feed too — reading is public, only writing is gated.
        // The composer is absent from a guest's DOM, so bind()'s null-checks
        // make that half a no-op; the feed wiring is what matters here.
        setupCommunityCardsLayout();
        bindReactionUsersModal();
        bind();
        renderNav();
        initMessagePushPrompt();
        loadMessageUnread();
        initMessageUnreadRefresh();
        initNotifications();
        loadRewards().then(function () {
            if (!WALLET) return;
            var pending = null;
            try {
                pending = JSON.parse(localStorage.getItem(
                    'gm-community-withdrawal:' + String(WALLET).toLowerCase()) || 'null');
            } catch (_) {}
            // Reconcile only an already-submitted member transaction. The
            // confirm endpoint verifies its exact authorization and receipt;
            // this never signs or submits another on-chain withdrawal.
            if (pending && /^0x[0-9a-f]{64}$/i.test(String(pending.withdrawal_id || ''))
                    && /^0x[0-9a-f]{64}$/i.test(String(pending.tx_hash || ''))) {
                checkWithdrawalEligibility();
            }
            loadReconciliationStatus();
        });
        if (typeof window.addEventListener === 'function') {
            window.addEventListener('gm:daily-learn-completed', function (event) {
                var refresh = loadRewards();
                if (event && event.detail) event.detail.refreshPromise = refresh;
            });
        }
        var pendingDonation = readPendingVaultDonation();
        if (pendingDonation) {
            var donationPanel = $('cfVaultDonationFormWrap');
            var checkDonation = $('cfVaultDonationCheck');
            var donationOpener = $('cfVaultDonationOpen');
            var donationEntry = $('cfVaultDonationEntry');
            var donationReview = $('cfVaultDonationReview');
            var donationRecovery = $('cfVaultDonationRecovery');
            if (donationPanel) donationPanel.hidden = false;
            if (donationOpener) donationOpener.setAttribute('aria-expanded', 'true');
            if (checkDonation) checkDonation.hidden = false;
            if (donationEntry) donationEntry.hidden = true;
            if (donationReview) donationReview.hidden = true;
            if (donationRecovery) donationRecovery.hidden = true;
            var donationHash = $('cfVaultDonationTxHash');
            if (donationHash) { donationHash.disabled = true; donationHash.value = ''; }
            checkPendingVaultDonation(pendingDonation);
        }
        loadFeed(true);
    }

    window.GMCommunity = {
        init: init,
        refresh: refresh,
        loadRewards: loadRewards,
        syncTodayHub: syncTodayHub,
        loadMore: loadMore,
        loadMorePinnedDonations: loadMorePinnedDonations,
        openVaultDonation: openVaultDonation,
        retryVaultDonationConfig: retryVaultDonationConfig,
        closeVaultDonation: closeVaultDonation,
        submitVaultDonation: submitVaultDonation,
        editVaultDonation: editVaultDonation,
        continueVaultDonationToWallet: continueVaultDonationToWallet,
        resumeVaultDonationFromHash: resumeVaultDonationFromHash,
        checkPendingVaultDonation: checkPendingVaultDonation,
        submitPost: submitPost,
        setDailyPrompt: setDailyPrompt,
        answerPrompt: answerPrompt,
        seePromptResponses: seePromptResponses,
        cancelPromptMode: cancelPromptMode,
        prefillDailyLearnReflection: prefillDailyLearnReflection,
        checkWithdrawalEligibility: checkWithdrawalEligibility,
        reconcilePreviousWithdrawal: reconcilePreviousWithdrawal,
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
        openCommunityCards: openCommunityCards,
        closeCommunityCards: closeCommunityCards,
        openReactionUsers: openReactionUsers,
        closeReactionUsers: closeReactionUsers,
        openSheet: openSheet,
        closeSheet: closeSheet,
        sheetGo: sheetGo,
        openAirtime: openAirtime,
        closeAirtime: closeAirtime,
        openClaim: openClaim,
        closeClaim: closeClaim,
        openMessages: openMessages,
        enableMessageNotifications: enableMessageNotifications,
        declineMessageNotifications: declineMessageNotifications,
        setMessageUnread: setMessageUnread,
        toggleNotifications: toggleNotifications,
        closeNotifications: closeNotifications,
        markAllNotificationsRead: markAllNotificationsRead,
        loadMoreNotifications: loadMoreNotifications,
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
            renderCommentNode: renderCommentNode,
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
            openMessages: openMessages,
            enableMessageNotifications: enableMessageNotifications,
            declineMessageNotifications: declineMessageNotifications,
            messagePromptWasDismissed: messagePromptWasDismissed,
            setMessageUnread: setMessageUnread,
            loadMessageUnread: loadMessageUnread,
            setNotificationUnread: setNotificationUnread,
            notificationMessage: notificationMessage,
            notificationIcon: notificationIcon,
            renderNotifications: renderNotifications,
            loadNotifications: loadNotifications,
            openNotification: openNotification,
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
            loadReconciliationStatus: loadReconciliationStatus,
            reconcilePreviousWithdrawal: reconcilePreviousWithdrawal,
            confirmWithdrawalTransaction: confirmWithdrawalTransaction,
            formatGd: formatGd,
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
