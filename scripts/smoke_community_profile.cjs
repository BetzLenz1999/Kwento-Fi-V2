/* Node harness over the REAL static/js/community-profile.js.

   Runs the bundle in a VM under DOM stubs and drives the genuine bodies for:
   - the guest guard (a guest never fetches anything),
   - posts loading + keyset "Load more",
   - the caption linkifier (the non-capturing URL_RE rule),
   - buildPostCard: caption as TEXT (no HTML injection),
   - the album: lazy-loaded on first tab open, tiles + video badge,
   - the stats: public message count from the CHATROOM endpoint, hidden when
     the chatroom is off,
   - the lightbox.

   Run: node scripts/smoke_community_profile.cjs
*/
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'js', 'community-profile.js'), 'utf8');

function makeDom() {
  function matches(node, sel) {
    if (!sel || !node || node.nodeType === 3) return false;
    if (sel.startsWith('#')) return node.id === sel.slice(1);
    if (sel.startsWith('.')) return (node.className || '').split(/\s+/).includes(sel.slice(1));
    if (sel.startsWith('[data-')) {
      const m = /\[data-([a-z-]+)="?([^"\]]*)"?\]/.exec(sel);
      return m ? node.attributes['data-' + m[1]] === m[2] : false;
    }
    return node.tagName === sel.toUpperCase();
  }
  function mk(tag) {
    return {
      tagName: String(tag).toUpperCase(), children: [], childNodes: [],
      className: '', id: '', textContent: '', hidden: false, value: '',
      src: '', alt: '', href: '', type: '', disabled: false, maxLength: 0,
      loading: '', referrerPolicy: '', controls: false, autoplay: false,
      preload: '', muted: false, playsInline: false, poster: '', title: '',
      attributes: {}, listeners: {}, style: {}, parent: null,
      get parentNode() { return this.parent; },
      setAttribute(k, v) { this.attributes[k] = String(v); },
      getAttribute(k) { return this.attributes[k] === undefined ? null : this.attributes[k]; },
      removeAttribute(k) { delete this.attributes[k]; },
      appendChild(c) {
        this.childNodes.push(c);
        if (c && c.nodeType !== 3) this.children.push(c);
        if (c) c.parent = this;
        return c;
      },
      removeChild(c) {
        this.childNodes = this.childNodes.filter(x => x !== c);
        this.children = this.children.filter(x => x !== c);
        return c;
      },
      remove() { if (this.parent) this.parent.removeChild(this); },
      addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
      dispatch(ev, arg) { (this.listeners[ev] || []).forEach(fn => fn(arg || {})); },
      get classList() {
        const self = this;
        return {
          add(c) { if (!self.className.split(/\s+/).includes(c)) self.className = (self.className + ' ' + c).trim(); },
          remove(c) { self.className = self.className.split(/\s+/).filter(x => x && x !== c).join(' '); },
          toggle(c, on) {
            const has = self.className.split(/\s+/).includes(c);
            const want = on === undefined ? !has : !!on;
            if (want) this.add(c); else this.remove(c);
            return want;
          },
          contains(c) { return self.className.split(/\s+/).includes(c); },
        };
      },
      querySelectorAll(sel) { return this._walk([]).filter(n => matches(n, sel)); },
      querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
      _walk(out) { this.children.forEach(c => { out.push(c); if (c._walk) c._walk(out); }); return out; },
      focus() {},
      get firstElementChild() { return this.children[0] || null; },
      set innerHTML(v) { if (v === '') { this.children = []; this.childNodes = []; } },
      get innerHTML() { return ''; },
      get textContent() {
        if (this.childNodes.length) return this.childNodes.map(c => c.textContent).join('');
        return this._text === undefined ? '' : this._text;
      },
      set textContent(v) {
        this._text = v === null || v === undefined ? '' : String(v);
        this.children = []; this.childNodes = [];
      },
    };
  }
  return { mk, root: mk('body') };
}

const IDS = [
  'cpPosts', 'cpPostsEmpty', 'cpPostsMore', 'cpAlbums', 'cpAlbumsEmpty',
  'cpAlbumsMore', 'cpTabPosts', 'cpTabAlbum', 'cpPanelPosts', 'cpPanelAlbum',
  'cpMessageCount', 'cpMessageStat', 'cpPostCount', 'cpAlbumCount',
  'cpMediaCount', 'cpLightbox', 'cpLightboxBody',
];

function run(opts) {
  opts = opts || {};
  const dom = makeDom();
  const registry = {};
  IDS.forEach(id => {
    const node = dom.mk('div');
    node.id = id;
    registry[id] = node;
  });
  // The message-count stat starts hidden in the markup; the bundle reveals it.
  const messageStat = registry.cpMessageStat;
  messageStat.className = 'cp-stat';
  messageStat.hidden = true;
  messageStat.appendChild(registry.cpMessageCount);
  dom.root.appendChild(messageStat);

  const calls = [];
  const doc = {
    readyState: 'complete',
    body: dom.root,
    createElement: dom.mk,
    createTextNode: t => ({ nodeType: 3, textContent: String(t) }),
    getElementById: id => registry[id] || null,
    querySelector: sel => registry[String(sel).replace('#', '')] || dom.root.querySelector(sel),
    querySelectorAll: sel => dom.root.querySelectorAll(sel),
    addEventListener: () => {},
    removeEventListener: () => {},
  };

  const fetchImpl = opts.fetchImpl || function (url) {
    calls.push(String(url));
    if (String(url).indexOf('/posts') !== -1) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        { success: true, posts: [], next_cursor: null, has_more: false }) });
    }
    if (String(url).indexOf('/album') !== -1) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        { success: true, albums: [], next_cursor: null, has_more: false }) });
    }
    if (String(url).indexOf('/chatroom/api/profile/') === 0) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        { success: true, message_count: 7 }) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
  };

  const sandbox = {
    document: doc, console,
    setTimeout: fn => { Promise.resolve().then(fn); return 0; },
    clearTimeout: () => {},
    fetch: function (url, opts2) { calls.push(String(url)); return fetchImpl(url, opts2); },
    navigator: { userAgent: 'node' },
    Promise, Object, Array, JSON, Math, Date, Number, String, RegExp, Error,
    isNaN, parseInt, parseFloat, encodeURIComponent,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.GM_PROFILE_BOOT = opts.boot || {};

  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return { api: sandbox.GMProfile, store: registry, sandbox, calls, messageStat };
}

let failures = 0;
function ok(label, condition, extra) {
  if (!condition) {
    failures++;
    console.error('FAIL: ' + label + (extra ? ' :: ' + extra : ''));
  } else {
    console.log('ok - ' + label);
  }
}

const MEMBER = {
  isGuest: false, username: 'betz', viewerUsername: 'viewer',
  feedEnabled: true, chatroomEnabled: true, assetVersion: '1',
};

const tick = () => new Promise(r => setTimeout(r, 5));

function makePost(over) {
  return Object.assign({
    id: 5, username: 'betz', body: 'hello', media: [],
    reaction_count: 3, comment_count: 1, is_mine: false,
    created_at: new Date().toISOString(),
  }, over || {});
}

function makeAlbum(over) {
  return Object.assign({
    post_id: 9, username: 'betz', caption: 'Paluwagan day 1',
    created_at: new Date().toISOString(), item_count: 2, has_video: true,
    items: [
      { type: 'image', url: 'https://media.example/a.jpg' },
      { type: 'video', url: 'https://media.example/v.mp4',
        poster: 'https://media.example/v.jpg' },
    ],
  }, over || {});
}

(async function main() {
  // ── 1. A guest READS the profile (public), and never writes ──────────────
  {
    const { api, store, calls } = run({
      boot: { isGuest: true, username: 'betz', feedEnabled: true },
      fetchImpl: function (url) {
        if (String(url).indexOf('/posts') !== -1) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({
            success: true, posts: [makePost({ body: 'public' })],
            next_cursor: null, has_more: false }) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve(
          { success: true, message_count: 2 }) });
      },
    });
    await tick();
    // Profiles are public: the bundle loads the same data for a guest.
    ok('a guest loads the profile posts',
       calls.some(c => /\/posts/.test(c)), JSON.stringify(calls));
    ok('a guest renders the posts it fetched',
       !!store.cpPosts.querySelector('.cp-post'));
    ok('the profile view exposes no write affordance',
       !store.cpPosts.querySelector('form, input, button'));
  }

  // ── 2. Posts load on init ─────────────────────────────────────────────────
  {
    const { api, store, calls } = run({
      boot: MEMBER,
      fetchImpl: function (url) {
        if (String(url).indexOf('/posts') !== -1) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({
            success: true, posts: [makePost({ body: 'first' })],
            next_cursor: null, has_more: false }) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve(
          { success: true, message_count: 4 }) });
      },
    });
    await tick();
    ok('the posts endpoint is fetched',
       calls.some(u => u.indexOf('/community/api/profile/betz/posts') === 0),
       JSON.stringify(calls));
    ok('one post is rendered', api._internals.getPosts().length === 1);
    ok('the post card is in the DOM',
       !!store.cpPosts.querySelector('.cp-post'));
    ok('no "no posts" empty state while posts exist', store.cpPostsEmpty.hidden === true);
  }

  // ── 3. Keyset "Load more" ─────────────────────────────────────────────────
  {
    let page = 0;
    const { api, calls } = run({
      boot: MEMBER,
      fetchImpl: function (url) {
        if (String(url).indexOf('/posts') !== -1) {
          page += 1;
          return Promise.resolve({ ok: true, json: () => Promise.resolve(
            page === 1
              ? { success: true, posts: [makePost({ id: 20 })], next_cursor: 20, has_more: true }
              : { success: true, posts: [makePost({ id: 10 })], next_cursor: null, has_more: false }) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      },
    });
    await tick();
    ok('the first page sets a cursor',
       api._internals.getPosts().length === 1);
    await api.loadMorePosts();
    await tick();
    ok('the second page is appended', api._internals.getPosts().length === 2,
       String(api._internals.getPosts().length));
    ok('the cursor is sent as a query param',
       calls.some(u => u.indexOf('cursor=20') !== -1), JSON.stringify(calls));
  }

  // ── 4. buildPostCard: caption is TEXT, never markup ───────────────────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildPostCard(makePost({
      body: '<img src=x onerror=alert(1)> hello',
    }));
    const caption = card.querySelector('.cp-caption');
    ok('the caption node exists', !!caption);
    ok('the caption keeps the raw text as TEXT (no HTML injection)',
       caption.textContent === '<img src=x onerror=alert(1)> hello',
       JSON.stringify(caption.textContent));
    ok('the caption created no element children',
       caption.children.length === 0, JSON.stringify(caption.children.length));
  }

  // ── 4b. The caption linkifier keeps the surrounding text ──────────────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildPostCard(makePost({
      body: 'see https://goodmarketph.live now',
    }));
    const caption = card.querySelector('.cp-caption');
    ok('the linkifier keeps the text before AND after the URL',
       caption.textContent === 'see https://goodmarketph.live now',
       JSON.stringify(caption.textContent));
    const link = caption.querySelector('a');
    ok('the URL becomes a safe link',
       !!link && link.href === 'https://goodmarketph.live'
       && link.rel.indexOf('noopener') !== -1);
  }

  // ── 4c. A video post gets controls + the stored poster ────────────────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildPostCard(makePost({
      body: 'clip', media: [{
        type: 'video', url: 'https://media.example/v.mp4',
        poster: 'https://media.example/v.jpg',
      }],
    }));
    const video = card.querySelector('video');
    ok('a video element is rendered', !!video);
    ok('the video has controls', video && video.controls === true);
    ok('the video is preload="metadata"', video && video.preload === 'metadata');
    ok('the poster frame is the stored URL',
       video && video.poster === 'https://media.example/v.jpg');
  }

  // ── 4d. A post links back to its place in the feed ────────────────────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildPostCard(makePost({ id: 42 }));
    const link = card.querySelector('.cp-post-link');
    ok('the card links back to the feed post', !!link && link.href === '/community#post-42',
       link ? link.href : 'missing');
  }

  // ── 5. Album is lazy-loaded on the first tab open ─────────────────────────
  {
    const { api, calls, store } = run({
      boot: MEMBER,
      fetchImpl: function (url) {
        if (String(url).indexOf('/album') !== -1) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({
            success: true, albums: [makeAlbum()], next_cursor: null, has_more: false }) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve(
          { success: true, posts: [], next_cursor: null }) });
      },
    });
    await tick();
    ok('the album is NOT fetched before the tab is opened',
       !calls.some(u => u.indexOf('/album') !== -1), JSON.stringify(calls));
    api.showTab('album');
    await tick();
    ok('opening the album tab fetches it',
       calls.some(u => u.indexOf('/community/api/profile/betz/album') === 0),
       JSON.stringify(calls));
    ok('the album panel is shown', store.cpPanelAlbum.hidden === false);
    ok('the posts panel is hidden', store.cpPanelPosts.hidden === true);
    ok('the album tab is marked active',
       store.cpTabAlbum.className.split(/\s+/).includes('is-active'));
    ok('the album tab is aria-selected', store.cpTabAlbum.getAttribute('aria-selected') === 'true');
  }

  // ── 6. buildAlbumCard: tiles + a video badge ──────────────────────────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildAlbumCard(makeAlbum());
    const tiles = card.querySelectorAll('.cp-album-tile');
    ok('one tile per album item', tiles.length === 2, String(tiles.length));
    ok('the image tile renders an img', !!tiles[0].querySelector('img'));
    ok('the video tile renders a video', !!tiles[1].querySelector('video'));
    ok('the video tile carries a play badge', !!card.querySelector('.cp-album-badge'));
    ok('the album title is the post caption',
       card.querySelector('.cp-album-title').textContent === 'Paluwagan day 1');
    ok('the item count is shown',
       card.querySelector('.cp-album-count').textContent === '2 items',
       card.querySelector('.cp-album-count').textContent);
  }

  // ── 6b. An untitled album still renders ───────────────────────────────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildAlbumCard(makeAlbum({ caption: '' }));
    ok('an empty caption falls back to "Untitled album"',
       card.querySelector('.cp-album-title').textContent === 'Untitled album');
  }

  // ── 7. Public message count comes from the chatroom endpoint ──────────────
  {
    const { api, calls, store } = run({
      boot: MEMBER,
      fetchImpl: function (url) {
        if (String(url).indexOf('/chatroom/api/profile/') === 0) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve(
            { success: true, message_count: 1234 }) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ success: true, posts: [] }) });
      },
    });
    await tick();
    ok('the chatroom profile endpoint is used',
       calls.some(u => u === '/chatroom/api/profile/betz'), JSON.stringify(calls));
    ok('the message count is rendered',
       store.cpMessageCount.textContent === '1.2K', store.cpMessageCount.textContent);
    ok('the message stat is revealed once the chatroom confirms a count',
       store.cpMessageStat.hidden === false);
  }

  // ── 7b. With the chatroom OFF the stat stays hidden, not a fake zero ──────
  {
    const { calls, messageStat } = run({
      boot: MEMBER,
      fetchImpl: function (url) {
        if (String(url).indexOf('/chatroom/api/profile/') === 0) {
          // The real endpoint returns 403 + enabled:false when the chatroom is
          // switched off (it fails CLOSED), never a count.
          return Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve(
            { success: false, enabled: false, error: 'The chatroom is not available yet.' }) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve(
          { success: true, posts: [] }) });
      },
    });
    await tick();
    ok('the chatroom endpoint is still consulted (it owns its own flag)',
       calls.some(u => u.indexOf('/chatroom/api/profile/') === 0), JSON.stringify(calls));
    ok('the message stat stays hidden when the chatroom is off',
       messageStat.hidden === true);
  }

  // ── 8. Lightbox ───────────────────────────────────────────────────────────
  {
    const { api, store } = run({ boot: MEMBER });
    api.openLightbox('https://media.example/a.jpg', 'image');
    ok('the lightbox opens with an image',
       store.cpLightbox.hidden === false
       && !!store.cpLightboxBody.querySelector('img'));
    api.closeLightbox();
    ok('the lightbox closes and clears its body',
       store.cpLightbox.hidden === true && store.cpLightboxBody.children.length === 0);

    api.openLightbox('https://media.example/v.mp4', 'video');
    ok('a video opens in a playable lightbox element',
       !!store.cpLightboxBody.querySelector('video'));
  }

  // ── 9. An empty profile shows the empty states ────────────────────────────
  {
    const { api, store } = run({ boot: MEMBER });
    await tick();
    ok('an empty posts list shows the empty state', store.cpPostsEmpty.hidden === false);
    api.showTab('album');
    await tick();
    ok('an empty album shows the empty state', store.cpAlbumsEmpty.hidden === false);
  }

  // ── 10. A feed error surfaces instead of a blank panel ────────────────────
  {
    const { api, store } = run({
      boot: MEMBER,
      fetchImpl: function (url) {
        if (String(url).indexOf('/posts') !== -1) {
          return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve(
            { success: false, error: 'Could not load posts.' }) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      },
    });
    await tick();
    ok('a failed posts load reports the error',
       /Could not load posts/.test(store.cpPosts.textContent),
       JSON.stringify(store.cpPosts.textContent));
  }

  console.log('');
  if (failures) {
    console.error(failures + ' CHECK(S) FAILED');
    process.exit(1);
  }
  console.log('ALL COMMUNITY PROFILE HARNESS CHECKS PASSED');
})();
