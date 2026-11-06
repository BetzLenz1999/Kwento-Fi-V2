/* GoodMarket shared public profile — a member's posts + media ALBUM.

   Used by BOTH the Community Feed profile (`/community/u/<username>`) and the
   chatroom profile (`/chatroom/u/<username>`), so there is one implementation.

   Security rules kept from the rest of the app:
   - User-supplied text is written with textContent ONLY, never innerHTML.
   - The wallet address is never fetched, never rendered, never stored: the
     page is addressed by USERNAME and the server strips the wallet.
   - Each stat comes from its feature's OWN endpoint (posts/albums from the
     Community Feed, messages from the chatroom), so no module reads another's
     tables. A feature that is OFF simply contributes no stat.

   The album is a member's POSTS THAT CARRY MEDIA: the caption is the album
   title and the images/videos are its items. Nothing new is uploaded — this is
   already-posted, already-moderated media. */

(function () {
    'use strict';

    var BOOT = window.GM_PROFILE_BOOT || {};
    var USERNAME = BOOT.username || '';
    var FEED_ENABLED = BOOT.feedEnabled !== false;

    var _posts = [];
    var _postsCursor = null;
    var _postsLoading = false;
    var _albums = [];
    var _albumsCursor = null;
    var _albumsLoading = false;
    var _albumLoaded = false;
    var _lightboxOpener = null;
    var _messageReturnFocus = null;
    var _messageConversationId = '';
    var _messageModalBound = false;

    // ── Small helpers ──────────────────────────────────────────────────────

    function $(id) { return document.getElementById(id); }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
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

    // Follow is a write, so a guest is sent to the shared connect flow instead
    // of silently failing — the same contract as the feed page.
    function requiresLogin() {
        toast('Please log in to do that.', 'error');
        if (typeof window.openGoodMarketGetStarted === 'function') {
            window.openGoodMarketGetStarted();
        }
        return false;
    }

    function openMessage(event) {
        if (!BOOT.wallet) {
            if (event && event.preventDefault) event.preventDefault();
            requiresLogin();
            return false;
        }
        if (!BOOT.messageTarget) return true;
        if (event && event.preventDefault) event.preventDefault();
        var modal = $('cpMessageModal');
        if (!modal || !FEED_ENABLED) {
            window.location.href = '/messages?with=' + encodeURIComponent(BOOT.messageTarget);
            return false;
        }
        openDirectMessageModal(event && event.currentTarget ? event.currentTarget : $('cpMessageBtn'));
        return false;
    }

    function messageModalStatus(text, isError) {
        var status = $('cpMessageModalStatus');
        if (!status) return;
        status.textContent = text || '';
        status.className = 'cp-message-modal-status' + (isError ? ' is-error' : '');
    }

    function closeDirectMessageModal() {
        var modal = $('cpMessageModal');
        if (!modal || modal.hidden) return;
        modal.hidden = true;
        modal.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('cp-message-modal-open');
        var opener = _messageReturnFocus;
        _messageReturnFocus = null;
        _messageConversationId = '';
        if (opener && opener.focus) opener.focus();
    }

    function renderDirectMessageThread(messages) {
        var thread = $('cpMessageThread');
        if (!thread) return;
        thread.textContent = '';
        if (!messages.length) {
            thread.appendChild(el('p', 'cp-message-thread-state', 'This is a private conversation. Say hello.'));
            return;
        }
        messages.forEach(function (message) {
            var bubble = el('article', 'cp-dm-bubble ' + (message.is_me ? 'is-mine' : 'is-theirs'));
            if (message.transfer) {
                var transfer = message.transfer;
                bubble.appendChild(el('p', 'cp-dm-bubble-text',
                    (transfer.amount || '') + ' ' + (transfer.token_label || transfer.token || '') +
                    ' sent to @' + (transfer.recipient_username || 'member')));
                if (transfer.explorer_url && /^https:\/\/celoscan\.io\/tx\//.test(transfer.explorer_url)) {
                    var txLink = el('a', 'cp-dm-attachment-link', 'View transaction');
                    txLink.href = transfer.explorer_url;
                    txLink.target = '_blank';
                    txLink.rel = 'noopener noreferrer';
                    bubble.appendChild(txLink);
                }
            } else if (message.body) {
                bubble.appendChild(el('p', 'cp-dm-bubble-text', message.body));
            }
            if (message.attachment && message.attachment.download_url &&
                /^\/api\/messages\/attachments\/[A-Za-z0-9_-]+\/download$/.test(message.attachment.download_url)) {
                var file = el('a', 'cp-dm-attachment-link', '📎 ' + (message.attachment.name || 'Attachment'));
                file.href = message.attachment.download_url;
                file.rel = 'nofollow';
                bubble.appendChild(file);
            }
            if (message.created_at) {
                var when = new Date(message.created_at);
                bubble.appendChild(el('time', 'cp-dm-bubble-time',
                    isNaN(when.getTime()) ? '' : when.toLocaleString(undefined, { hour: 'numeric', minute: '2-digit', month: 'short', day: 'numeric' })));
            }
            thread.appendChild(bubble);
        });
        thread.scrollTop = thread.scrollHeight;
    }

    function loadDirectMessageThread(conversationId) {
        var thread = $('cpMessageThread');
        if (thread) {
            thread.textContent = '';
            thread.appendChild(el('p', 'cp-message-thread-state', 'Loading conversation…'));
        }
        return api('/api/messages/conversations/' + encodeURIComponent(conversationId)).then(function (result) {
            if (!result.ok || !result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not load this conversation.');
            }
            renderDirectMessageThread(result.data.messages || []);
            var send = $('cpMessageSend');
            if (send) send.disabled = false;
            messageModalStatus('Messages are private between you and this member.');
            api('/api/messages/conversations/' + encodeURIComponent(conversationId) + '/read', {
                method: 'POST', body: '{}'
            }).catch(function () {});
            var input = $('cpMessageInput');
            if (input && input.focus) input.focus();
        });
    }

    function openDirectMessageModal(trigger) {
        var modal = $('cpMessageModal');
        if (!modal) return false;
        _messageReturnFocus = trigger || document.activeElement;
        _messageConversationId = '';
        modal.hidden = false;
        modal.setAttribute('aria-hidden', 'false');
        document.body.classList.add('cp-message-modal-open');
        var closeButton = $('cpMessageModalClose');
        if (closeButton && closeButton.focus) closeButton.focus();
        var title = $('cpMessageModalTitle');
        var subtitle = $('cpMessageModalSubtitle');
        var avatar = $('cpMessageModalAvatar');
        var input = $('cpMessageInput');
        var send = $('cpMessageSend');
        var thread = $('cpMessageThread');
        if (title) title.textContent = '@' + (BOOT.messageTarget || USERNAME);
        if (subtitle) subtitle.textContent = 'Private conversation';
        if (avatar) avatar.textContent = String(BOOT.messageTarget || USERNAME || 'M').charAt(0).toUpperCase();
        if (input) input.value = '';
        if (send) send.disabled = true;
        if (thread) {
            thread.textContent = '';
            thread.appendChild(el('p', 'cp-message-thread-state', 'Opening your private conversation…'));
        }
        messageModalStatus('Loading conversation…');
        api('/api/messages/start', {
            method: 'POST',
            body: JSON.stringify({ username: BOOT.messageTarget })
        }).then(function (result) {
            var conversation = result.data && result.data.conversation;
            if (!result.ok || !result.data || !result.data.success || !conversation || !conversation.id) {
                throw new Error((result.data && result.data.error) || 'Messaging is temporarily unavailable.');
            }
            _messageConversationId = conversation.id;
            if (title) title.textContent = '@' + (conversation.username || BOOT.messageTarget || USERNAME);
            if (avatar) avatar.textContent = String(conversation.username || BOOT.messageTarget || 'M').charAt(0).toUpperCase();
            return loadDirectMessageThread(conversation.id);
        }).catch(function (error) {
            if (thread) {
                thread.textContent = '';
                thread.appendChild(el('p', 'cp-message-thread-state is-error', error.message || 'Could not open this conversation.'));
            }
            messageModalStatus('You can close this window and try again.', true);
        });
        return true;
    }

    function sendDirectMessage(event) {
        if (event && event.preventDefault) event.preventDefault();
        var input = $('cpMessageInput');
        var send = $('cpMessageSend');
        var body = input ? input.value.trim() : '';
        if (!body || !_messageConversationId || (send && send.disabled)) return false;
        if (send) send.disabled = true;
        messageModalStatus('Sending…');
        var clientId = (window.crypto && window.crypto.randomUUID)
            ? window.crypto.randomUUID() : ('profile-' + Date.now() + '-' + Math.random().toString(36).slice(2));
        api('/api/messages/conversations/' + encodeURIComponent(_messageConversationId) + '/messages', {
            method: 'POST',
            body: JSON.stringify({ body: body, client_message_id: clientId })
        }).then(function (result) {
            if (!result.ok || !result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Message was not sent.');
            }
            if (input) input.value = '';
            messageModalStatus('Message sent.');
            return loadDirectMessageThread(_messageConversationId);
        }).catch(function (error) {
            messageModalStatus(error.message || 'Message was not sent. Please try again.', true);
            if (send) send.disabled = false;
        });
        return false;
    }

    function bindDirectMessageModal() {
        var modal = $('cpMessageModal');
        if (!modal || _messageModalBound) return;
        _messageModalBound = true;
        var close = $('cpMessageModalClose');
        var form = $('cpMessageComposer');
        var input = $('cpMessageInput');
        if (close) close.addEventListener('click', closeDirectMessageModal);
        if (form) form.addEventListener('submit', sendDirectMessage);
        if (input) input.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendDirectMessage(event);
            }
        });
        modal.addEventListener('click', function (event) {
            if (event.target === modal) closeDirectMessageModal();
        });
        document.addEventListener('keydown', function (event) {
            if (modal.hidden) return;
            if (event.key === 'Escape') {
                closeDirectMessageModal();
                return;
            }
            if (event.key === 'Tab') {
                var focusable = modal.querySelectorAll('button:not(:disabled), textarea:not(:disabled), a[href]');
                if (!focusable.length) return;
                var first = focusable[0];
                var last = focusable[focusable.length - 1];
                if (event.shiftKey && document.activeElement === first) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault();
                    first.focus();
                }
            }
        });
    }

    function toast(message, kind) {
        var node = $('cpToast');
        if (!node) return;
        node.textContent = message;
        node.className = 'cp-toast' + (kind ? ' is-' + kind : '');
        node.hidden = false;
        if (node._timer) clearTimeout(node._timer);
        node._timer = setTimeout(function () { node.hidden = true; }, 2600);
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

    // ── Posts ──────────────────────────────────────────────────────────────

    // ── Follow ─────────────────────────────────────────────────────────────
    // The profile hero's follow button. A write, so a guest gets the shared
    // connect card; your own profile never shows it. Counts are public; the
    // follow graph is never returned.

    function followButton() {
        var host = $('cpFollowWrap');
        if (!host) return;
        host.textContent = '';
        var profile = BOOT.profile || {};
        // Re-sync the counts from the boot object: the template renders them
        // for no-JS, but the API value is authoritative (it is computed with
        // the viewer in mind) and keeps the hero correct after a follow.
        var followerEl = $('cpFollowerCount');
        if (followerEl && typeof profile.followers === 'number') {
            followerEl.textContent = String(profile.followers);
        }
        var followingEl = $('cpFollowingCount');
        if (followingEl && typeof profile.following === 'number') {
            followingEl.textContent = String(profile.following);
        }
        if (profile.is_self || !USERNAME) return;
        var btn = el('button', 'cp-follow' + (profile.is_following ? ' is-following' : ''),
            profile.is_following ? 'Following' : 'Follow');
        btn.type = 'button';
        btn.id = 'cpFollowBtn';
        btn.setAttribute('aria-pressed', profile.is_following ? 'true' : 'false');
        btn.setAttribute('aria-label',
            (profile.is_following ? 'Unfollow @' : 'Follow @') + USERNAME);
        btn.onclick = function () { toggleFollow(btn); };
        host.appendChild(btn);
    }

    function toggleFollow(btn) {
        if (!BOOT.wallet) { requiresLogin(); return Promise.resolve(); }
        var following = btn.classList.contains('is-following');
        setFollowButton(btn, !following);
        return api('/community/api/users/' + encodeURIComponent(USERNAME) + '/follow', {
            method: following ? 'DELETE' : 'POST',
        }).then(function (result) {
            if (!result.ok || !result.data || result.data.success === false) {
                setFollowButton(btn, following);
                toast((result.data && result.data.error) || 'Could not update follow.',
                    'error');
                return;
            }
            setFollowButton(btn, !!result.data.following);
            var count = $('cpFollowerCount');
            if (count && typeof result.data.follower_count === 'number') {
                count.textContent = String(result.data.follower_count);
            }
        }).catch(function () {
            setFollowButton(btn, following);
            toast('Could not update follow.', 'error');
        });
    }

    function setFollowButton(btn, following) {
        btn.classList.toggle('is-following', following);
        btn.textContent = following ? 'Following' : 'Follow';
        btn.setAttribute('aria-pressed', following ? 'true' : 'false');
        btn.setAttribute('aria-label',
            (following ? 'Unfollow @' : 'Follow @') + USERNAME);
    }

    // A video is shown Instagram-style: a BOUNDED, rounded frame with
    // `object-fit: contain` so nothing is cropped; tapping opens the lightbox.
    function buildVideoFrame(item, username) {
        var frame = el('button', 'cp-video-frame');
        frame.type = 'button';
        frame.setAttribute('aria-label', 'Play video by ' + (username || 'this member'));
        if (item.w && item.h) {
            frame.style.aspectRatio = item.w + ' / ' + item.h;
        }
        var video = document.createElement('video');
        video.controls = true;
        video.setAttribute('controlslist', 'nodownload');
        video.preload = 'metadata';
        video.playsInline = true;
        if (item.poster) video.poster = item.poster;
        video.src = item.url;
        video.onclick = function (event) { event.stopPropagation(); };
        frame.appendChild(video);
        frame.onclick = function () { openLightbox(item.url, 'video'); };
        return frame;
    }

    function avatarImageUrl(url, username) {
        var name = String(username || '').trim().replace(/^@+/, '');
        if (!String(url || '').trim() || !/^[A-Za-z0-9_]{3,24}$/.test(name)) return '';
        return '/community/api/profile/' + encodeURIComponent(name) + '/avatar';
    }

    // A member's profile picture, or their initial. Same element shape as the
    // feed's `cf-avatar`, so both views read as one product.
    function avatarNode(url, username, className) {
        var node = el('span', className || 'cp-avatar cp-avatar-sm');
        node.setAttribute('aria-hidden', 'true');
        var imageSrc = avatarImageUrl(url, username);
        if (imageSrc) {
            var img = document.createElement('img');
            img.className = 'cp-avatar-img';
            img.src = imageSrc;
            img.alt = '';
            img.loading = 'lazy';
            img.decoding = 'async';
            img.referrerPolicy = 'no-referrer';
            img.onerror = function () {
                node.textContent = (String(username || 'A'))[0].toUpperCase();
            };
            node.appendChild(img);
        } else {
            node.textContent = (String(username || 'A'))[0].toUpperCase();
        }
        return node;
    }

    function buildPostCard(post) {
        var card = el('article', 'cp-card cp-post');
        card.setAttribute('data-post-id', post.id);

        var head = el('div', 'cp-post-head');
        head.appendChild(avatarNode(post.avatar_url, post.username,
            'cp-avatar cp-avatar-sm'));
        var who = el('div', 'cp-post-who');
        who.appendChild(el('strong', null, post.username || 'Community member'));
        who.appendChild(el('span', null, timeAgo(post.created_at)
            + (post.is_edited ? ' · edited' : '')));
        var postBadges = Array.isArray(post.badges) ? post.badges.slice(0, 3) : [];
        if (postBadges.length) {
            var inlineBadges = el('span', 'cp-inline-badges');
            postBadges.forEach(function (badge) {
                if (!badge || !badge.name) return;
                var chip = el('span', 'cp-inline-badge', badge.name);
                chip.title = badge.description || badge.name;
                chip.setAttribute('aria-label', badge.name + ': ' + (badge.description || 'Community badge'));
                inlineBadges.appendChild(chip);
            });
            if (inlineBadges.childNodes.length) who.appendChild(inlineBadges);
        }
        head.appendChild(who);
        card.appendChild(head);

        if (post.media && post.media.length) {
            var photoGridCount = post.media.length >= 2 && post.media.length <= 4
                && post.media.every(function (item) {
                    return item && item.type !== 'video';
                }) ? post.media.length : 0;
            var mediaWrap = el('div', 'cp-media' + (photoGridCount
                ? ' cp-media-grid cp-media-grid-' + photoGridCount : ''));
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

        var body = el('div', 'cp-post-body');
        if (post.body) {
            var caption = el('p', 'cp-caption');
            linkifyInto(caption, post.body);
            body.appendChild(caption);
        }
        var meta = el('div', 'cp-post-meta');
        meta.appendChild(el('span', null, (countLabel(post.reaction_count) || '0') + ' reactions'));
        meta.appendChild(el('span', null, (countLabel(post.comment_count) || '0') + ' comments'));
        body.appendChild(meta);

        // A feed post links back to its place in the feed (the feed renders the
        // interactive card; the profile view is read-only).
        var link = el('a', 'cp-post-link', 'View in feed →');
        link.href = '/community#post-' + post.id;
        body.appendChild(link);
        card.appendChild(body);
        return card;
    }

    function renderPosts() {
        var container = $('cpPosts');
        if (!container) return;
        container.textContent = '';
        container.setAttribute('aria-busy', 'false');

        var empty = $('cpPostsEmpty');
        if (!_posts.length) {
            if (empty) empty.hidden = false;
            updatePostsMore();
            return;
        }
        if (empty) empty.hidden = true;
        _posts.forEach(function (post) { container.appendChild(buildPostCard(post)); });
        updatePostsMore();
    }

    function updatePostsMore() {
        var button = $('cpPostsMore');
        if (button) button.hidden = !_postsCursor;
    }

    function loadPosts(reset) {
        if (_postsLoading || !USERNAME || !FEED_ENABLED) {
            if (!FEED_ENABLED) renderPostsUnavailable();
            return Promise.resolve();
        }
        _postsLoading = true;
        var url = '/community/api/profile/' + encodeURIComponent(USERNAME) + '/posts?limit=10';
        if (!reset && _postsCursor) url += '&cursor=' + encodeURIComponent(_postsCursor);

        return api(url).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not load posts.');
            }
            _postsCursor = result.data.next_cursor || null;
            var rows = result.data.posts || [];
            _posts = reset ? rows : _posts.concat(rows);
            renderPosts();
        }).catch(function (error) {
            var container = $('cpPosts');
            if (container) {
                container.textContent = '';
                container.setAttribute('aria-busy', 'false');
                container.appendChild(el('p', 'cp-empty',
                    (error && error.message) || 'Could not load posts.'));
            }
        }).then(function () {
            _postsLoading = false;
        });
    }

    function renderPostsUnavailable() {
        var container = $('cpPosts');
        if (!container) return;
        container.textContent = '';
        container.setAttribute('aria-busy', 'false');
        container.appendChild(el('p', 'cp-empty', 'Posts are not available right now.'));
    }

    function loadMorePosts() {
        var button = $('cpPostsMore');
        if (button) { button.disabled = true; button.textContent = 'Loading…'; }
        loadPosts(false).then(function () {
            if (button) { button.disabled = false; button.textContent = 'Load more posts'; }
        });
    }

    // ── Album ──────────────────────────────────────────────────────────────

    function buildAlbumCard(album) {
        var card = el('article', 'cp-album');
        card.setAttribute('data-album-id', album.post_id);

        var head = el('div', 'cp-album-head');
        var title = el('strong', 'cp-album-title');
        title.textContent = album.caption ? album.caption : 'Untitled album';
        head.appendChild(title);
        head.appendChild(el('span', 'cp-album-count',
            album.item_count + ' item' + (album.item_count === 1 ? '' : 's')));
        card.appendChild(head);

        var grid = el('div', 'cp-album-grid');
        (album.items || []).forEach(function (item) {
            var tile = el('button', 'cp-album-tile');
            tile.type = 'button';
            tile.setAttribute('aria-label', item.type === 'video'
                ? 'Play video' : 'View photo');
            if (item.type === 'video') {
                var video = document.createElement('video');
                video.preload = 'metadata';
                video.muted = true;
                video.playsInline = true;
                if (item.poster) video.poster = item.poster;
                video.src = item.url;
                tile.appendChild(video);
                tile.appendChild(el('span', 'cp-album-badge', '▶'));
            } else {
                var img = document.createElement('img');
                img.loading = 'lazy';
                img.decoding = 'async';
                img.referrerPolicy = 'no-referrer';
                img.src = item.url;
                img.alt = album.caption ? album.caption.slice(0, 120) : 'Photo';
                tile.appendChild(img);
            }
            tile.onclick = function () { openLightbox(item.url, item.type); };
            grid.appendChild(tile);
        });
        card.appendChild(grid);
        return card;
    }

    function renderAlbums() {
        var container = $('cpAlbums');
        if (!container) return;
        container.textContent = '';
        container.setAttribute('aria-busy', 'false');

        var empty = $('cpAlbumsEmpty');
        if (!_albums.length) {
            if (empty) empty.hidden = false;
            updateAlbumsMore();
            return;
        }
        if (empty) empty.hidden = true;
        _albums.forEach(function (album) { container.appendChild(buildAlbumCard(album)); });
        updateAlbumsMore();
    }

    function updateAlbumsMore() {
        var button = $('cpAlbumsMore');
        if (button) button.hidden = !_albumsCursor;
    }

    function loadAlbums(reset) {
        if (_albumsLoading || !USERNAME || !FEED_ENABLED) return Promise.resolve();
        _albumsLoading = true;
        var url = '/community/api/profile/' + encodeURIComponent(USERNAME) + '/album?limit=10';
        if (!reset && _albumsCursor) url += '&cursor=' + encodeURIComponent(_albumsCursor);

        return api(url).then(function (result) {
            if (!result.data || !result.data.success) {
                throw new Error((result.data && result.data.error) || 'Could not load the album.');
            }
            _albumsCursor = result.data.next_cursor || null;
            var rows = result.data.albums || [];
            _albums = reset ? rows : _albums.concat(rows);
            renderAlbums();
        }).catch(function (error) {
            var container = $('cpAlbums');
            if (container) {
                container.textContent = '';
                container.setAttribute('aria-busy', 'false');
                container.appendChild(el('p', 'cp-empty',
                    (error && error.message) || 'Could not load the album.'));
            }
        }).then(function () {
            _albumsLoading = false;
        });
    }

    function loadMoreAlbums() {
        var button = $('cpAlbumsMore');
        if (button) { button.disabled = true; button.textContent = 'Loading…'; }
        loadAlbums(false).then(function () {
            if (button) { button.disabled = false; button.textContent = 'Load more albums'; }
        });
    }

    // ── Tabs ───────────────────────────────────────────────────────────────

    function showTab(name) {
        var isAlbum = name === 'album';
        var postsTab = $('cpTabPosts');
        var albumTab = $('cpTabAlbum');
        var postsPanel = $('cpPanelPosts');
        var albumPanel = $('cpPanelAlbum');

        if (postsTab) {
            postsTab.classList.toggle('is-active', !isAlbum);
            postsTab.setAttribute('aria-selected', isAlbum ? 'false' : 'true');
        }
        if (albumTab) {
            albumTab.classList.toggle('is-active', isAlbum);
            albumTab.setAttribute('aria-selected', isAlbum ? 'true' : 'false');
        }
        if (postsPanel) postsPanel.hidden = isAlbum;
        if (albumPanel) albumPanel.hidden = !isAlbum;

        // The album is only fetched the first time it is opened, so a profile
        // view does not pay for a media query nobody asked for.
        if (isAlbum && !_albumLoaded) {
            _albumLoaded = true;
            loadAlbums(true);
        }
    }

    // ── Stats ──────────────────────────────────────────────────────────────

    function setText(id, value) {
        var node = $(id);
        if (node) node.textContent = String(value);
    }

    /* Public message count comes from the CHATROOM's own endpoint — the feed
       never reads the chatroom's tables or its feature flag (and vice versa).
       The stat starts hidden and is revealed only when the chatroom confirms
       the count, so a chatroom that is OFF shows no misleading zero. */
    function loadMessageCount() {
        if (!USERNAME) return;
        var stat = $('cpMessageStat');
        api('/chatroom/api/profile/' + encodeURIComponent(USERNAME)).then(function (result) {
            if (result.data && result.data.success) {
                setText('cpMessageCount', countLabel(result.data.message_count) || '0');
                if (stat) stat.hidden = false;
            }
        }).catch(function () {
            // Chatroom unavailable: leave the stat hidden.
        });
    }

    function bindProfileAvatar() {
        var avatar = $('cpProfileAvatar');
        if (!avatar || avatar.tagName !== 'BUTTON') return;

        function setAvatarUnavailable() {
            avatar.textContent = (USERNAME || 'A')[0].toUpperCase();
            avatar.disabled = true;
            avatar.setAttribute('aria-label',
                'Profile picture unavailable for @' + USERNAME);
        }

        function syncAvatar(url) {
            var image = avatar.querySelector('img');
            var available = !!url && !!image;
            avatar.disabled = !available;
            avatar.setAttribute('aria-label', available
                ? 'View profile picture of @' + USERNAME
                : 'No profile picture for @' + USERNAME);
            if (image) {
                // The hero is above the fold; don't lazy-load the image the
                // visitor came to this page to see.
                image.loading = 'eager';
                image.fetchPriority = 'high';
                image.referrerPolicy = 'no-referrer';
                image.onerror = setAvatarUnavailable;
            }
        }

        avatar.onclick = function () {
            if (avatar.disabled) return;
            var image = avatar.querySelector('img');
            var src = image && (image.currentSrc || image.src);
            if (!src) return;
            openLightbox(src, 'image', 'Profile picture of @' + USERNAME, avatar);
        };

        syncAvatar(BOOT.avatarUrl || '');
        if (window.GMCommunityAvatar
                && typeof window.GMCommunityAvatar.onChange === 'function') {
            window.GMCommunityAvatar.onChange(syncAvatar);
        }
    }

    // ── Lightbox ───────────────────────────────────────────────────────────

    function openLightbox(url, kind, label, opener) {
        var box = $('cpLightbox');
        var body = $('cpLightboxBody');
        if (!box || !body) return;
        _lightboxOpener = opener || (document.activeElement !== document.body
            ? document.activeElement : null);
        box.setAttribute('aria-label', label ? label + ' viewer' : 'Media viewer');
        body.textContent = '';
        if (kind === 'video') {
            var video = document.createElement('video');
            video.controls = true;
            video.setAttribute('controlslist', 'nodownload');
            video.autoplay = true;
            video.playsInline = true;
            video.src = url;
            body.appendChild(video);
        } else {
            var img = document.createElement('img');
            img.src = url;
            img.alt = label || '';
            img.onerror = function () {
                body.textContent = '';
                var fallback = el('p', 'cp-lightbox-error',
                    'This image could not be loaded.');
                fallback.setAttribute('role', 'status');
                body.appendChild(fallback);
            };
            body.appendChild(img);
        }
        box.hidden = false;
        var closeButton = box.querySelector('.cp-lightbox-close');
        if (closeButton && closeButton.focus) closeButton.focus();
    }

    function closeLightbox() {
        var box = $('cpLightbox');
        var body = $('cpLightboxBody');
        if (body) body.textContent = '';
        if (box) box.hidden = true;
        if (box) box.setAttribute('aria-label', 'Media viewer');
        var opener = _lightboxOpener;
        _lightboxOpener = null;
        if (opener && !opener.disabled && opener.focus) opener.focus();
    }

    // ── Send Money / Profile tip ──────────────────────────────────────────
    var tipBusy = false;
    var tipPendingHash = null;
    var tipPendingMeta = null;
    var TIP_PENDING_KEY = 'gm_profile_tip_pending_v1';
    function tipStatus(text, error) { var node=$('cpTipStatus'); if (!node) return; node.textContent=text||''; node.className='cp-money-status'+(error?' is-error':''); }
    function resetTipSuccess() {
        var panel = $('cpTipSuccess');
        if (panel) panel.hidden = true;
        var summary = $('cpTipSuccessSummary');
        if (summary) summary.textContent = '';
        var link = $('cpTipSuccessLink');
        if (link) { link.hidden = false; link.removeAttribute('href'); }
        var pending = $('cpTipPending');
        if (pending) pending.hidden = true;
        var pendingLink = $('cpTipPendingLink');
        if (pendingLink) pendingLink.removeAttribute('href');
    }
    function savePendingTip(meta, hash) {
        meta.hash = hash;
        tipPendingMeta = meta;
        tipPendingHash = hash;
        try { sessionStorage.setItem(TIP_PENDING_KEY, JSON.stringify({ meta: meta, hash: hash })); } catch (_) {}
    }
    function clearPendingTip() {
        tipPendingMeta = null;
        tipPendingHash = null;
        try { sessionStorage.removeItem(TIP_PENDING_KEY); } catch (_) {}
    }
    function showTipPending(hash, message) {
        var panel = $('cpTipPending');
        var summary = $('cpTipPendingSummary');
        var link = $('cpTipPendingLink');
        if (summary) summary.textContent = message || 'Waiting for Celo confirmation. Your funds were not sent again.';
        if (link) { link.href = 'https://celoscan.io/tx/' + hash; link.hidden = false; }
        if (panel) panel.hidden = false;
    }
    function restorePendingTip() {
        try {
            var raw = sessionStorage.getItem(TIP_PENDING_KEY);
            var saved = raw ? JSON.parse(raw) : null;
            if (saved && saved.hash && saved.meta && saved.meta.username === USERNAME) {
                savePendingTip(saved.meta, saved.hash);
                showTipPending(saved.hash, 'A previous transfer is still awaiting confirmation.');
                return true;
            }
        } catch (_) {}
        return false;
    }
    function showTipSuccess(tip) {
        var panel = $('cpTipSuccess');
        var summary = $('cpTipSuccessSummary');
        var link = $('cpTipSuccessLink');
        var label = (tip && tip.token_label) || (tip && tip.token) || '';
        var amount = (tip && tip.amount) || '';
        var recipient = (tip && tip.recipient_username) || USERNAME;
        if (summary) summary.textContent = amount + ' ' + label + ' sent to @' + recipient + '.';
        if (link && tip && tip.explorer_url) { link.href = tip.explorer_url; link.hidden = false; }
        else if (link) link.hidden = true;
        if (panel) panel.hidden = false;
        tipStatus('');
        toast('Transfer confirmed on Celo.', 'success');
    }
    function openSendMoney() {
        if (!BOOT.wallet) { requiresLogin(); return; }
        var m = $('cpSendMoneyModal');
        if (m) {
            m.hidden = false;
            document.body.classList.add('cp-money-open');
            resetTipSuccess();
            restorePendingTip();
        }
    }
    function closeSendMoney() {
        var m = $('cpSendMoneyModal');
        if (m) m.hidden = true;
        document.body.classList.remove('cp-money-open');
        tipStatus(''); resetTipSuccess();
    }
    var profileEip6963Providers = [];
    try {
        window.addEventListener('eip6963:announceProvider', function (event) {
            var detail = event && event.detail;
            if (!detail || !detail.provider) return;
            if (!profileEip6963Providers.some(function (item) {
                return item.provider === detail.provider ||
                    (item.info && detail.info && item.info.uuid &&
                     item.info.uuid === detail.info.uuid);
            })) profileEip6963Providers.push(detail);
        });
    } catch (_) {}
    function profilePrimeProviderDiscovery() {
        if (window.__gmProfileDiscoveryPromise) return window.__gmProfileDiscoveryPromise;
        window.__gmProfileDiscoveryPromise = Promise.resolve().then(function () {
            try { window.dispatchEvent(new Event('eip6963:requestProvider')); } catch (_) {}
            return true;
        }).finally(function () { window.__gmProfileDiscoveryPromise = null; });
        return window.__gmProfileDiscoveryPromise;
    }
    function profileBindProviderLifecycle() {
        if (window.__gmProfileProviderLifecycleBound) return;
        window.__gmProfileProviderLifecycleBound = true;
        var rediscover = function () {
            if (typeof document !== 'undefined' &&
                document.visibilityState && document.visibilityState !== 'visible') return;
            profilePrimeProviderDiscovery().catch(function () {});
        };
        try {
            window.addEventListener('pageshow', rediscover);
            window.addEventListener('TrustWallet#initialized', rediscover);
            if (typeof document !== 'undefined' && document.addEventListener) {
                document.addEventListener('visibilitychange', rediscover);
            }
        } catch (_) {}
    }
    profilePrimeProviderDiscovery();
    profileBindProviderLifecycle();
    function profileProviderInfo(p) {
        var item = profileEip6963Providers.find(function (entry) { return entry.provider === p; });
        return (item && item.info) || {};
    }
    function isProfileTrustProvider(p) {
        var info = profileProviderInfo(p);
        return !!(p && (p.isTrust || p.isTrustWallet)) ||
            /trustwallet|trust/i.test(String(info.rdns || '') + ' ' + String(info.name || ''));
    }
    function profileProviders() {
        var a=[], add=function(p){
            if(p&&p.provider)p=p.provider;
            if(p&&p.ethereum&&typeof p.ethereum.request==='function')p=p.ethereum;
            if(p&&typeof p.request==='function'&&a.indexOf(p)<0)a.push(p);
        };
        var e=window.ethereum;
        if(e&&Array.isArray(e.providers))e.providers.forEach(add);
        add(e); add(window.trustwallet); add(window.trustWallet);
        profileEip6963Providers.forEach(function (entry) { add(entry.provider); });
        return a;
    }
    function profileProviderOrder(providers) {
        return providers.slice().sort(function (a, b) {
            function score(p) {
                if (isProfileTrustProvider(p)) return 40;
                if (p && p.isMiniPay) return 30;
                if (p && p.isMetaMask && !p.isBraveWallet) return 20;
                return 10;
            }
            return score(b) - score(a);
        });
    }
    async function profileSignerProvider() {
        var method=String(BOOT.loginMethod||'').toLowerCase();
        if(method==='local'){ if(!window.GMLocalWallet)throw new Error('Your in-app wallet is still loading.'); if(!GMLocalWallet.isUnlocked()&&window._lwOpenUnlockModal)await window._lwOpenUnlockModal({title:'Sign transfer',subtitle:'Enter your PIN to send money',submitLabel:'Sign & Send',busyLabel:'Signing…'}); if(!GMLocalWallet.isUnlocked())throw new Error('Please unlock your in-app wallet first.'); return GMLocalWallet.getProvider(); }
        if(Array.isArray(window.GMPrivyWallets)){var pr=window.GMPrivyWallets.find(function(x){return x&&x.provider&&typeof x.provider.request==='function';});if(pr)return pr.provider;}
        if((method==='walletconnect'||(window.GMWalletConnect&&GMWalletConnect.isPreferred&&GMWalletConnect.isPreferred()))&&window.GMWalletConnect)return await GMWalletConnect.getProvider();
        var wanted=String(BOOT.wallet||'').toLowerCase(), ps=[];
        // Trust Wallet mobile may inject after this bundle parsed, so rediscover
        // for a short bounded window instead of failing on the first tap.
        var mobile=/android|iphone|ipad|ipod/i.test(String(navigator.userAgent||''));
        var deadline=Date.now()+(mobile?4200:800);
        await profilePrimeProviderDiscovery();
        do {
            ps=profileProviderOrder(profileProviders());
            for(var i=0;i<ps.length;i++){
                var ac=[];
                try{ac=await ps[i].request({method:'eth_accounts'})||[]}catch(_){}
                if(ac.some(function(x){return String(x).toLowerCase()===wanted;}))return ps[i];
            }
            if(!ps.length || Date.now()<deadline) await new Promise(function(resolve){setTimeout(resolve,120);});
        } while(Date.now()<deadline);
        // Only prompt the preferred provider once; probing every injected wallet
        // can open multiple account sheets on phones with several providers.
        if(ps.length){
            var prompted=[];
            try{prompted=await ps[0].request({method:'eth_requestAccounts'})||[]}catch(_){}
            if(prompted.some(function(x){return String(x).toLowerCase()===wanted;}))return ps[0];
        }
        throw new Error(ps.length?'Wrong wallet connected. Please switch to your GoodMarket wallet.':'No wallet detected. Please open MetaMask, Trust Wallet, MiniPay, or use WalletConnect.');
    }
    async function profileSendTransaction(provider,prep){
        var method=String(BOOT.loginMethod||'').toLowerCase(), ac=[];
        if(method!=='local') ac=await provider.request({method:'eth_requestAccounts'})||[];
        var from=method==='local'?BOOT.wallet:ac.find(function(x){return String(x).toLowerCase()===String(BOOT.wallet).toLowerCase();}); if(!from)throw new Error('Wrong wallet connected. Please switch to your GoodMarket wallet.');
        if(method!=='local'){var chain=await provider.request({method:'eth_chainId'}).catch(function(){return null;});if(String(chain||'').toLowerCase()!=='0xa4ec'){try{await provider.request({method:'wallet_switchEthereumChain',params:[{chainId:'0xa4ec'}]});}catch(e){if(e&&e.code===4902)await provider.request({method:'wallet_addEthereumChain',params:[{chainId:'0xa4ec',chainName:'Celo Mainnet',nativeCurrency:{name:'CELO',symbol:'CELO',decimals:18},rpcUrls:['https://forno.celo.org'],blockExplorerUrls:['https://celoscan.io']}]});else throw e;}}}
        var tx={from:from,to:prep.to,data:prep.data||'0x',value:prep.value||'0x0'}; if(method==='local')tx.chainId=prep.chain_id; if(provider.isMiniPay&&method!=='local'&&prep.minipay_fee_currencies)tx.feeCurrency=prep.minipay_fee_currencies.cusd; return provider.request({method:'eth_sendTransaction',params:[tx]});
    }
    async function confirmTipOnce(meta, hash) {
        var result = await api('/community/api/profile/' + encodeURIComponent(USERNAME) + '/tip/confirm', {
            method: 'POST', body: JSON.stringify({ token: meta.token, amount: meta.amount, tx_hash: hash })
        });
        if (result.data && result.data.success) return { kind: 'confirmed', data: result.data };
        var status = result.data && result.data.status;
        if (status === 'pending' || status === 'rpc_unavailable' || (result.data && result.data.retryable)) {
            return { kind: 'retry', message: (result.data && result.data.error) || 'Waiting for Celo confirmation.' };
        }
        var error = new Error((result.data && result.data.error) || 'Transfer verification failed.');
        error.permanent = status === 'reverted' || status === 'mismatch' || status === 'verify_failed';
        throw error;
    }
    async function saveConfirmedTip(meta, result) {
        var saved = await api('/api/messages/profile/' + encodeURIComponent(USERNAME) + '/onchain-transfer', {
            method: 'POST', body: JSON.stringify({ token: meta.token, amount: meta.amount, tx_hash: meta.hash })
        }).catch(function () { return { ok: false }; });
        clearPendingTip();
        showTipSuccess(result.data.tip);
        if (!saved.ok) tipStatus('Transfer confirmed, but it could not be added to Messages.', true);
        if ($('cpTipAmount')) $('cpTipAmount').value = '';
    }
    async function pollTipConfirmation(meta, hash) {
        var delays = [0, 2000, 4000, 7000, 12000, 20000];
        for (var i = 0; i < delays.length; i++) {
            if (delays[i]) await new Promise(function (resolve) { setTimeout(resolve, delays[i]); });
            tipStatus(i ? 'Still checking Celo…' : 'Checking Celo confirmation…');
            var result = await confirmTipOnce(meta, hash);
            if (result.kind === 'confirmed') return result;
            showTipPending(hash, result.message);
        }
        showTipPending(hash, 'Confirmation is taking longer than usual. Your funds were not sent again.');
        tipStatus('Transaction submitted. You can check again without sending another transfer.');
        return { kind: 'pending' };
    }
    async function checkTipStatus() {
        if (tipBusy || !tipPendingHash || !tipPendingMeta) return;
        tipBusy = true;
        var button = $('cpTipCheck');
        if (button) button.disabled = true;
        try {
            var result = await pollTipConfirmation(tipPendingMeta, tipPendingHash);
            if (result.kind === 'confirmed') await saveConfirmedTip(tipPendingMeta, result);
        } catch (e) {
            if (e && e.permanent) {
                clearPendingTip();
                resetTipSuccess();
                tipStatus(e.message || 'Transfer verification failed.', true);
            } else {
                showTipPending(tipPendingHash, (e && e.message) || 'Confirmation is still unavailable.');
            }
        } finally {
            tipBusy = false;
            if (button) button.disabled = false;
        }
    }
    async function sendMoney() {
        if (tipBusy) return;
        if (tipPendingHash) { checkTipStatus(); return; }
        if (!BOOT.wallet) { requiresLogin(); return; }
        var token = $('cpTipToken') && $('cpTipToken').value;
        var amount = $('cpTipAmount') && $('cpTipAmount').value.trim();
        var button = $('cpTipSubmit');
        if (!amount || Number(amount) <= 0) { tipStatus('Enter a valid amount.', true); return; }
        tipBusy = true;
        if (button) button.disabled = true;
        try {
            tipStatus('Preparing transfer…');
            var pr = await api('/community/api/profile/' + encodeURIComponent(USERNAME) + '/tip/prepare', {
                method: 'POST', body: JSON.stringify({ token: token, amount: amount })
            });
            if (!pr.ok || !pr.data.success) throw new Error((pr.data && pr.data.error) || 'Could not prepare transfer.');
            tipStatus('Confirm the transaction in your wallet…');
            var provider = await profileSignerProvider();
            var hash = await profileSendTransaction(provider, pr.data);
            if (!hash) throw new Error('Your wallet did not return a transaction hash.');
            var meta = { token: token, amount: amount, username: USERNAME };
            savePendingTip(meta, hash);
            showTipPending(hash, 'Transaction submitted. Waiting for Celo confirmation.');
            var result = await pollTipConfirmation(meta, hash);
            if (result.kind === 'confirmed') await saveConfirmedTip(meta, result);
        } catch (e) {
            if (e && e.permanent) { clearPendingTip(); resetTipSuccess(); }
            tipStatus((e && e.message) || 'Transfer failed. Please try again.', true);
        } finally {
            tipBusy = false;
            if (button) button.disabled = false;
        }
    }
    // ── Init ───────────────────────────────────────────────────────────────

    function renderBadges(rows) {
        var container = $('cpBadges');
        if (!container) return;
        container.textContent = '';
        if (!Array.isArray(rows) || !rows.length) {
            container.appendChild(el('p', 'cp-badge-empty', 'No Community achievements yet.'));
            return;
        }
        var icons = {
            first_post: '✍', first_reaction: '♥', community_voice: '🎤',
            helpful_member: '✦', consistent_contributor: '✓',
            community_builder: '◈', daily_learner: '▣'
        };
        rows.forEach(function (badge) {
            if (!badge || !badge.name) return;
            var item = el('article', 'cp-badge-card');
            item.appendChild(el('span', 'cp-badge-icon', icons[badge.key] || '✦'));
            var text = el('span', 'cp-badge-copy');
            text.appendChild(el('strong', 'cp-badge-name', badge.name));
            if (badge.description) text.appendChild(el('span', 'cp-badge-description', badge.description));
            if (badge.awarded_at) {
                var date = new Date(badge.awarded_at);
                if (!isNaN(date.getTime())) text.appendChild(el('span', 'cp-badge-date', 'Earned ' + date.toLocaleDateString()));
            }
            item.appendChild(text);
            container.appendChild(item);
        });
    }

    function loadBadges() {
        var section = $('cpAchievements');
        if (!section || !FEED_ENABLED || !USERNAME) return Promise.resolve();
        var url = '/community/api/profile/' + encodeURIComponent(USERNAME) + '/badges';
        return api(url).then(function (result) {
            if (!result.data || !result.data.success) throw new Error('Badges unavailable');
            renderBadges(result.data.badges || []);
            section.hidden = false;
        }).catch(function () {
            section.hidden = true;
        });
    }

    function init() {
        // Guests read profiles too — profiles are public, only writing is
        // gated (and this view is read-only: media + a link back to the feed).
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') closeLightbox();
        });
        bindProfileAvatar();
        bindDirectMessageModal();
        followButton();
        loadPosts(true);
        loadBadges();
        loadMessageCount();
    }

    window.GMProfile = {
        openMessage: openMessage,
        closeMessage: closeDirectMessageModal,
        sendDirectMessage: sendDirectMessage,
        openSendMoney: openSendMoney,
        closeSendMoney: closeSendMoney,
        sendMoney: sendMoney,
        checkTipStatus: checkTipStatus,
        init: init,
        showTab: showTab,
        loadMorePosts: loadMorePosts,
        loadMoreAlbums: loadMoreAlbums,
        openLightbox: openLightbox,
        closeLightbox: closeLightbox,
        // Exposed for the test harness (drives the real bodies).
        _internals: {
            timeAgo: timeAgo,
            countLabel: countLabel,
            linkifyInto: linkifyInto,
            buildPostCard: buildPostCard,
            buildAlbumCard: buildAlbumCard,
            buildVideoFrame: buildVideoFrame,
            avatarNode: avatarNode,
            followButton: followButton,
            toggleFollow: toggleFollow,
            setFollowButton: setFollowButton,
            renderPosts: renderPosts,
            renderAlbums: renderAlbums,
            loadPosts: loadPosts,
            loadAlbums: loadAlbums,
            loadMessageCount: loadMessageCount,
            bindProfileAvatar: bindProfileAvatar,
            getPosts: function () { return _posts; },
            setPosts: function (rows) { _posts = rows; },
            getAlbums: function () { return _albums; },
            setAlbums: function (rows) { _albums = rows; },
            setPostsCursor: function (v) { _postsCursor = v; },
            setAlbumsCursor: function (v) { _albumsCursor = v; },
        },
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
