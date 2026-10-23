
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
        window.location.href = '/messages?with=' + encodeURIComponent(BOOT.messageTarget);
        return false;
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
    function tipStatus(text, error) { var node=$('cpTipStatus'); if (!node) return; node.textContent=text||''; node.className='cp-money-status'+(error?' is-error':''); }
    function resetTipSuccess() {
        var panel = $('cpTipSuccess');
        if (panel) panel.hidden = true;
        var summary = $('cpTipSuccessSummary');
        if (summary) summary.textContent = '';
        var link = $('cpTipSuccessLink');
        if (link) { link.hidden = false; link.removeAttribute('href'); }
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
        if (m) { m.hidden = false; document.body.classList.add('cp-money-open'); resetTipSuccess(); }
    }
    function closeSendMoney() {
        var m = $('cpSendMoneyModal');
        if (m) m.hidden = true;
        document.body.classList.remove('cp-money-open');
        tipStatus(''); resetTipSuccess();
    }
    function profileProviders() { var a=[], add=function(p){if(p&&p.provider)p=p.provider;if(p&&typeof p.request==='function'&&a.indexOf(p)<0)a.push(p);}; var e=window.ethereum; if(e&&Array.isArray(e.providers))e.providers.forEach(add); add(e); add(window.trustwallet); add(window.trustWallet); return a; }
    async function profileSignerProvider() {
        var method=String(BOOT.loginMethod||'').toLowerCase();
        if(method==='local'){ if(!window.GMLocalWallet)throw new Error('Your in-app wallet is still loading.'); if(!GMLocalWallet.isUnlocked()&&window._lwOpenUnlockModal)await window._lwOpenUnlockModal({title:'Sign transfer',subtitle:'Enter your PIN to send money',submitLabel:'Sign & Send',busyLabel:'Signing…'}); if(!GMLocalWallet.isUnlocked())throw new Error('Please unlock your in-app wallet first.'); return GMLocalWallet.getProvider(); }
        if(Array.isArray(window.GMPrivyWallets)){var pr=window.GMPrivyWallets.find(function(x){return x&&x.provider&&typeof x.provider.request==='function';});if(pr)return pr.provider;}
        if((method==='walletconnect'||(window.GMWalletConnect&&GMWalletConnect.isPreferred&&GMWalletConnect.isPreferred()))&&window.GMWalletConnect)return await GMWalletConnect.getProvider();
        var ps=profileProviders(), wanted=String(BOOT.wallet||'').toLowerCase();
        for(var i=0;i<ps.length;i++){var ac=[];try{ac=await ps[i].request({method:'eth_requestAccounts'})||[]}catch(_){} if(!ac.length)try{ac=await ps[i].request({method:'eth_accounts'})||[]}catch(_){} if(ac.some(function(x){return String(x).toLowerCase()===wanted;}))return ps[i];}
        throw new Error(ps.length?'Wrong wallet connected. Please switch to your GoodMarket wallet.':'No wallet detected. Please open MetaMask, Trust Wallet, MiniPay, or use WalletConnect.');
    }
    async function profileSendTransaction(provider,prep){
        var method=String(BOOT.loginMethod||'').toLowerCase(), ac=[];
        if(method!=='local') ac=await provider.request({method:'eth_requestAccounts'})||[];
        var from=method==='local'?BOOT.wallet:ac.find(function(x){return String(x).toLowerCase()===String(BOOT.wallet).toLowerCase();}); if(!from)throw new Error('Wrong wallet connected. Please switch to your GoodMarket wallet.');
        if(method!=='local'){var chain=await provider.request({method:'eth_chainId'}).catch(function(){return null;});if(String(chain||'').toLowerCase()!=='0xa4ec'){try{await provider.request({method:'wallet_switchEthereumChain',params:[{chainId:'0xa4ec'}]});}catch(e){if(e&&e.code===4902)await provider.request({method:'wallet_addEthereumChain',params:[{chainId:'0xa4ec',chainName:'Celo Mainnet',nativeCurrency:{name:'CELO',symbol:'CELO',decimals:18},rpcUrls:['https://forno.celo.org'],blockExplorerUrls:['https://celoscan.io']}]});else throw e;}}}
        var tx={from:from,to:prep.to,data:prep.data||'0x',value:prep.value||'0x0'}; if(method==='local')tx.chainId=prep.chain_id; if(provider.isMiniPay&&method!=='local'&&prep.minipay_fee_currencies)tx.feeCurrency=prep.minipay_fee_currencies.cusd; return provider.request({method:'eth_sendTransaction',params:[tx]});
    }
    async function sendMoney(){if(tipBusy)return;if(!BOOT.wallet){requiresLogin();return;}var token=$('cpTipToken')&&$('cpTipToken').value,amount=$('cpTipAmount')&&$('cpTipAmount').value.trim(),button=$('cpTipSubmit');if(!amount||Number(amount)<=0){tipStatus('Enter a valid amount.',true);return;}tipBusy=true;if(button)button.disabled=true;try{tipStatus('Preparing transfer…');var pr=await api('/community/api/profile/'+encodeURIComponent(USERNAME)+'/tip/prepare',{method:'POST',body:JSON.stringify({token:token,amount:amount})});if(!pr.ok||!pr.data.success)throw new Error((pr.data&&pr.data.error)||'Could not prepare transfer.');tipStatus('Confirm the transaction in your wallet…');var provider=await profileSignerProvider(),hash=await profileSendTransaction(provider,pr.data);if(!hash)throw new Error('Your wallet did not return a transaction hash.');tipStatus('Verifying on Celo…');var cf=await api('/community/api/profile/'+encodeURIComponent(USERNAME)+'/tip/confirm',{method:'POST',body:JSON.stringify({token:token,amount:amount,tx_hash:hash})});if(!cf.ok||!cf.data.success)throw new Error((cf.data&&cf.data.error)||'Transfer verification failed.');var saved=await api('/api/messages/profile/'+encodeURIComponent(USERNAME)+'/onchain-transfer',{method:'POST',body:JSON.stringify({token:token,amount:amount,tx_hash:hash})}).catch(function(){return {ok:false};});showTipSuccess(cf.data.tip);if(!saved.ok)tipStatus('Transfer confirmed, but it could not be added to Messages.',true);if($('cpTipAmount'))$('cpTipAmount').value='';}catch(e){tipStatus((e&&e.message)||'Transfer failed. Please try again.',true);}finally{tipBusy=false;if(button)button.disabled=false;}}
    // ── Init ───────────────────────────────────────────────────────────────

    function init() {
        // Guests read profiles too — profiles are public, only writing is
        // gated (and this view is read-only: media + a link back to the feed).
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') closeLightbox();
        });
        bindProfileAvatar();
        followButton();
        loadPosts(true);
        loadMessageCount();
    }

    window.GMProfile = {
        openMessage: openMessage,
        openSendMoney: openSendMoney,
        closeSendMoney: closeSendMoney,
        sendMoney: sendMoney,
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
