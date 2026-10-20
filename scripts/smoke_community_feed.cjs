/* Node harness over the REAL static/js/community-feed.js.

   Runs the bundle in a VM under DOM stubs and drives the genuine bodies for:
   - composer Post-button gating (caption / over-limit / whitespace),
   - the caption linkifier (the non-capturing URL_RE rule),
   - optimistic reaction -> reconcile, and rollback on failure,
   - switching a reaction keeping the count flat,
   - the three-hop upload: presign -> PUT straight to R2 with the SIGNED headers,
   - the client-side media type allow-list,
   - the guest guard (a guest can never post or queue an upload),
   - buildCard: caption as TEXT (no HTML injection) and six pressable chips.

   Run: node scripts/smoke_community_feed.cjs
*/
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'js', 'community-feed.js'), 'utf8');

function makeDom() {
  function matches(node, sel) {
    if (!sel || !node || node.nodeType === 3) return false;   // text node
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
      placeholder: '', loading: '', referrerPolicy: '', controls: false,
      preload: '', playsInline: false, poster: '',
      scrollHeight: 400, clientHeight: 100,
      attributes: {}, listeners: {}, style: {}, parent: null,
      setAttribute(k, v) { this.attributes[k] = String(v); },
      getAttribute(k) { return this.attributes[k] === undefined ? null : this.attributes[k]; },
      removeAttribute(k) { delete this.attributes[k]; },
      _attach(c, at) {
        const isText = c && c.nodeType === 3;
        this.childNodes.push(c);
        if (!isText) this.children.push(c);
        if (at !== undefined && at >= 0) {
          this.childNodes.splice(this.childNodes.length - 1, 1);
          this.childNodes.splice(at, 0, c);
          this.children = this.childNodes.filter(n => n.nodeType !== 3);
        }
        c.parent = this;
        return c;
      },
      appendChild(c) { return this._attach(c); },
      insertBefore(c) { return this._attach(c, 0); },
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
      _walk(out) { this.children.forEach(c => { out.push(c); if (c._walk) c._walk(out); }); return out; },
      querySelectorAll(sel) { return this._walk([]).filter(n => matches(n, sel)); },
      querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
      closest() { return null; },
      contains(n) { return this._walk([]).includes(n); },
      getBoundingClientRect() { return { top: 0, bottom: 10, right: 200, left: 20, width: 180, height: 10 }; },
      scrollIntoView() {}, focus() {},
      // insertAdjacentElement('afterend', n) attaches to the SIBLING position,
      // i.e. a child of this node's parent — never of this node.
      insertAdjacentElement(pos, n) {
        const host = (pos === 'afterend' || pos === 'beforebegin') ? (this.parent || this) : this;
        if (pos === 'afterend' || pos === 'beforebegin') {
          const at = host.children.indexOf(this);
          const idx = pos === 'afterend' ? at + 1 : at;
          host.children.splice(idx, 0, n);
          host.childNodes = host.children.slice();
        } else {
          host.children.push(n);
          host.childNodes = host.children.slice();
        }
        n.parent = host;
        return n;
      },
      get firstElementChild() { return this.children[0] || null; },
      set innerHTML(v) { if (v === '') { this.children = []; this.childNodes = []; } },
      get innerHTML() { return ''; },
      // Real DOM semantics: textContent aggregates descendants, so a caption
      // built with createTextNode + appendChild reports its full text.
      get textContent() {
        if (this._text !== undefined) return this._text;
        return this.childNodes.map(c => c.textContent).join('');
      },
      set textContent(v) {
        this._text = v === null || v === undefined ? '' : String(v);
        this.children = [];
        this.childNodes = [];
      },
    };
  }
  return { mk, root: mk('body') };
}

function run(opts) {
  opts = opts || {};
  const dom = makeDom();
  const registry = {};
  ['cfCaption', 'cfCounter', 'cfPostBtn', 'cfFileInput', 'cfPreviews',
   'cfUploadStatus', 'cfFeed', 'cfEmpty', 'cfLoadMore', 'cfToast',
   'cfLightbox', 'cfLightboxBody', 'cfEditModal', 'cfEditCaption',
   'cfEditSaveBtn', 'cfComposer',
   'cfUsernameGate', 'cfUsernameInput', 'cfUsernameSave', 'cfUsernameHint'
  ].forEach(id => {
    const node = dom.mk(
      id === 'cfCaption' || id === 'cfEditCaption' || id === 'cfUsernameInput'
        ? 'textarea' : 'div');
    node.id = id;
    registry[id] = node;
  });
  registry.cfPreviews.hidden = true;
  registry.cfUsernameInput.value = '';

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

  const sandbox = {
    document: doc, console,
    setTimeout: fn => { Promise.resolve().then(fn); return 0; },
    clearTimeout: () => {}, requestAnimationFrame: fn => fn(),
    fetch: opts.fetchImpl || (() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })),
    navigator: { userAgent: 'node' },
    Image: function () { this.onload = null; this.onerror = null; },
    URL: { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} },
    Promise, Object, Array, JSON, Math, Date, Number, String, RegExp, Error,
    isNaN, parseInt, parseFloat, encodeURIComponent,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.GM_COMMUNITY_BOOT = opts.boot || {};
  sandbox.prompt = () => '';
  sandbox.confirm = () => true;
  sandbox.location = { origin: 'https://goodmarketph.live', hash: '' };

  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return { api: sandbox.GMCommunity, store: registry, sandbox };
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
  isGuest: false, wallet: '0xAbCdEf1234567890abcdef1234567890AbCdEf12',
  username: 'betz', usernameRequired: true,
  loginMethod: 'local', maxCaption: 2200, maxMedia: 4,
  maxImageMb: 10, maxVideoMb: 100, storageReady: true,
  reactions: { like: '👍', love: '❤️', haha: '😂', wow: '😮', sad: '😢', angry: '😡' },
};

// Same as MEMBER but with no username yet — the gate must block posting.
const NO_USERNAME = Object.assign({}, MEMBER, {
  username: '', usernameRequired: true,
});

function makePost(over) {
  return Object.assign({
    id: 5, username: 'betz', body: 'hello', media: [],
    reaction_count: 16, comment_count: 0, my_reaction: null,
    is_mine: false, created_at: new Date().toISOString(),
  }, over || {});
}

const tick = () => new Promise(r => setTimeout(r, 5));

/* The bundle's init() runs loadFeed() on load, so every stub must answer the
   feed endpoint with a valid (empty) page. `handler` handles everything else. */
function feedFetch(handler, feedRows) {
  return function (url, opts) {
    if (String(url).indexOf('/community/api/feed') === 0) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          success: true, posts: feedRows || [], next_cursor: null, has_more: false,
        }),
      });
    }
    return handler(url, opts);
  };
}

(async function main() {
  // ── 1. Post button gating ─────────────────────────────────────────────────
  {
    const { api, store } = run({ boot: MEMBER });
    ok('post button starts disabled', store.cfPostBtn.disabled === true);

    store.cfCaption.value = 'Hello world';
    api._internals.updatePostButton();
    ok('a caption alone enables Post', store.cfPostBtn.disabled === false);

    store.cfCaption.value = 'x'.repeat(2201);
    api._internals.updatePostButton();
    ok('an over-limit caption disables Post', store.cfPostBtn.disabled === true);

    store.cfCaption.value = '   ';
    api._internals.updatePostButton();
    ok('a whitespace-only caption keeps Post disabled', store.cfPostBtn.disabled === true);
  }

  // ── 2. Caption linkifier keeps the surrounding text ───────────────────────
  {
    const { api } = run({ boot: MEMBER });
    const node = { children: [], appendChild(c) { this.children.push(c); } };
    api._internals.linkifyInto(node, 'before https://x.com/a after');
    const parts = node.children.map(c => c.textContent);
    ok('linkify keeps the text BEFORE the link', parts[0] === 'before ', JSON.stringify(parts));
    ok('linkify produced the anchor', parts[1] === 'https://x.com/a', JSON.stringify(parts));
    ok('linkify keeps the text AFTER the link', parts[2] === ' after', JSON.stringify(parts));
  }

  // ── 3. Reaction: optimistic, then reconcile ───────────────────────────────
  {
    const calls = [];
    const fetchImpl = (url, o) => {
      calls.push({ url, method: (o && o.method) || 'GET' });
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ success: true, reaction_count: 17, my_reaction: 'love' }),
      });
    };
    const { api } = run({ boot: MEMBER, fetchImpl: feedFetch(fetchImpl) });
    const p = makePost();
    api._internals.setFeed([p]);
    api.react(p, 'love');
    ok('reaction POSTs to the post endpoint',
       calls.length === 1 && calls[0].url === '/community/api/posts/5/reaction'
         && calls[0].method === 'POST', JSON.stringify(calls));
    ok('reaction is optimistic immediately',
       p.reaction_count === 17 && p.my_reaction === 'love', JSON.stringify(p));
    await tick();
    ok('reaction reconciles with the server count',
       p.reaction_count === 17 && p.my_reaction === 'love', JSON.stringify(p));
  }

  // ── 4. Reaction: a failure rolls BOTH the chip and the count back ─────────
  {
    const fetchImpl = () => Promise.resolve({
      ok: false, json: () => Promise.resolve({ success: false, error: 'nope' }),
    });
    const { api } = run({ boot: MEMBER, fetchImpl: feedFetch(fetchImpl) });
    const p = makePost();
    api._internals.setFeed([p]);
    api.react(p, 'love');
    await tick();
    ok('a failed reaction rolls the count back', p.reaction_count === 16, JSON.stringify(p));
    ok('a failed reaction rolls the chip back', p.my_reaction === null, JSON.stringify(p));
  }

  // ── 5. Switching a reaction does NOT move the count ───────────────────────
  {
    const fetchImpl = () => Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ success: true, reaction_count: 16, my_reaction: 'haha' }),
    });
    const { api } = run({ boot: MEMBER, fetchImpl: feedFetch(fetchImpl) });
    const p = makePost({ my_reaction: 'love', reaction_count: 16 });
    api._internals.setFeed([p]);
    api.react(p, 'haha');
    ok('switching a reaction keeps the count flat', p.reaction_count === 16, JSON.stringify(p));
    ok('switching a reaction moves the chip', p.my_reaction === 'haha', JSON.stringify(p));
    await tick();
  }

  // ── 6. Upload: presign -> PUT straight to R2 with the SIGNED headers ──────
  {
    const seen = [];
    const fetchImpl = (url, o) => {
      seen.push({ url, method: (o && o.method) || 'GET', headers: (o && o.headers) || {} });
      if (url === '/community/api/upload/presign') {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            success: true,
            upload_url: 'https://acct.r2.cloudflarestorage.com/bucket/u/abc/key.jpg',
            method: 'PUT', headers: { 'Content-Type': 'image/jpeg' },
            key: 'u/abc/key.jpg', public_url: 'https://media.example/u/abc/key.jpg',
            media_type: 'image', mime: 'image/jpeg',
          }),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    };
    const { api } = run({ boot: MEMBER, fetchImpl: feedFetch(fetchImpl) });
    const item = await api._internals.presign('image/jpeg', 1234);
    ok('presign returns the server-generated key', item.key === 'u/abc/key.jpg');
    await api._internals.putToR2(item, 'BLOB');
    const put = seen.find(s => s.method === 'PUT');
    ok('the bytes go straight to R2, not through the app',
       !!put && put.url.startsWith('https://acct.r2.cloudflarestorage.com'), JSON.stringify(seen));
    ok('the SIGNED Content-Type header is sent back verbatim',
       put.headers['Content-Type'] === 'image/jpeg', JSON.stringify(put.headers));
  }

  // ── 6b. Multi-file: every attachment gets its OWN presign + PUT ───────────
  // A post may carry up to 4 files. The client must not reuse one presigned URL
  // for several files (an R2 key is single-object), and the server throttle must
  // admit the whole burst — a regression here showed "upload failed" from the
  // second image on while the caption posted fine.
  {
    const seen = [];
    const fetchImpl = (url, o) => {
      const method = (o && o.method) || 'GET';
      seen.push({ url, method });
      if (url === '/community/api/upload/presign') {
        const n = seen.filter(s => s.url === '/community/api/upload/presign').length;
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            success: true,
            upload_url: 'https://acct.r2.cloudflarestorage.com/bucket/u/abc/k' + n + '.jpg',
            method: 'PUT', headers: { 'Content-Type': 'image/jpeg' },
            key: 'u/abc/k' + n + '.jpg', public_url: 'https://media.example/u/abc/k' + n + '.jpg',
            media_type: 'image', mime: 'image/jpeg',
          }),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    };
    const { api } = run({ boot: MEMBER, fetchImpl: feedFetch(fetchImpl) });
    const entries = [1, 2, 3, 4].map(i => ({
      contentType: 'image/jpeg', blob: { type: 'image/jpeg', size: 1000, name: 'p' + i + '.jpg' },
    }));
    const done = await Promise.all(entries.map(e => api._internals.uploadAttachment(e)));
    const presigns = seen.filter(s => s.url === '/community/api/upload/presign').length;
    const puts = seen.filter(s => s.method === 'PUT').length;
    ok('4 attachments make 4 presign requests', presigns === 4, String(presigns));
    ok('4 attachments make 4 PUTs to R2', puts === 4, String(puts));
    ok('every attachment ends in the done state',
       done.every(e => e.state === 'done'), JSON.stringify(done.map(e => e.state)));
    ok('each attachment keeps a distinct object key',
       new Set(done.map(e => e.upload.key)).size === 4,
       JSON.stringify(done.map(e => e.upload.key)));
  }

  // ── 7. Client-side media type allow-list ──────────────────────────────────
  {
    const { api } = run({ boot: MEMBER });
    api.addFiles([{ type: 'application/x-msdownload', size: 10, name: 'evil.exe' }]);
    ok('an unknown media type never reaches the upload queue',
       api._internals.getPending().length === 0,
       JSON.stringify(api._internals.getPending()));
    api.addFiles([{ type: 'text/html', size: 10, name: 'x.html' }]);
    ok('an HTML file is refused too (stored-XSS guard)',
       api._internals.getPending().length === 0);
  }

  // ── 8. Guest guard ────────────────────────────────────────────────────────
  {
    const { api } = run({ boot: { isGuest: true, wallet: '', reactions: {} } });
    api.submitPost();
    ok('a guest cannot post', api._internals.getFeed().length === 0);
    api.addFiles([{ type: 'image/jpeg', size: 10, name: 'a.jpg' }]);
    ok('a guest cannot queue an upload', api._internals.getPending().length === 0);
  }

  // ── 9. buildCard renders the caption as TEXT, never markup ────────────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      body: '<img src=x onerror=alert(1)> hello',
    }));
    const caption = card.querySelector('.cf-caption');
    ok('the caption node exists', !!caption);
    ok('the caption keeps the raw text as TEXT (no HTML injection)',
       caption.textContent === '<img src=x onerror=alert(1)> hello',
       JSON.stringify(caption.textContent));
    ok('the caption created no element children',
       caption.children.length === 0, JSON.stringify(caption.children.length));
  }

  // ── 9b. Caption PLACEMENT: media → summary → caption → reactions ──────────
  // The card order is the spec (docs/COMMUNITY_FEED_R2_PROPOSAL.md §4.2): the
  // caption sits BELOW the media and ABOVE the reaction bar. A refactor that
  // reorders these is a silent regression, so it is pinned by index.
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      body: 'Unang araw ng paluwagan namin!',
      media: [{ type: 'image', url: 'https://media.example/a.jpg' }],
    }));
    const order = [];
    (function walk(node) {
      for (const child of node.children || []) {
        for (const cls of ['cf-media', 'cf-summary', 'cf-caption',
                           'cf-reactions', 'cf-card-actions']) {
          if ((child.className || '').split(/\s+/).includes(cls)) order.push(cls);
        }
        walk(child);
      }
    })(card);
    const at = (name) => order.indexOf(name);
    ok('the card renders media, summary, caption, reactions and actions',
       order.length === 5, JSON.stringify(order));
    ok('the caption sits BELOW the media', at('cf-media') < at('cf-caption'),
       JSON.stringify(order));
    ok('the caption sits ABOVE the reaction bar',
       at('cf-caption') < at('cf-reactions'), JSON.stringify(order));
    ok('the reaction summary sits above the caption',
       at('cf-summary') < at('cf-caption'), JSON.stringify(order));
  }

  // ── 9c. A caption-only post still renders its caption (no media branch) ───
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      body: 'Walang litrato, salita lang.', media: [],
    }));
    const caption = card.querySelector('.cf-caption');
    ok('a text-only post renders its caption',
       !!caption && caption.textContent === 'Walang litrato, salita lang.',
       caption ? JSON.stringify(caption.textContent) : 'missing');
  }

  // ── 9d. An image-only post renders media with NO caption node ────────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      body: '', media: [{ type: 'image', url: 'https://media.example/b.jpg' }],
    }));
    ok('an image-only post renders its media',
       !!card.querySelector('.cf-media'));
    ok('an empty caption adds no empty node', !card.querySelector('.cf-caption'));
  }

  // ── 9e. A video gets controls + the stored poster frame ──────────────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      body: 'clip', media: [{
        type: 'video', url: 'https://media.example/v.mp4',
        poster: 'https://media.example/v.jpg',
      }],
    }));
    const video = card.querySelector('video');
    ok('a video element is rendered', !!video);
    ok('the video has controls', video && video.controls === true);
    ok('the video is preload="metadata" (no eager 100MB download)',
       video && video.preload === 'metadata', video ? video.preload : 'n/a');
    ok('the poster frame is the stored R2 URL',
       video && video.poster === 'https://media.example/v.jpg');
  }

  // ── 10. buildCard: six reaction chips, only the viewer's is pressed ───────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({ my_reaction: 'love' }));
    const chips = card.querySelectorAll('.cf-chip');
    ok('all six reaction chips are rendered', chips.length === 6, String(chips.length));
    const pressed = chips.filter(c => c.getAttribute('aria-pressed') === 'true');
    ok('exactly the viewer reaction is marked pressed',
       pressed.length === 1 && pressed[0].textContent === '❤️',
       JSON.stringify(pressed.map(c => c.textContent)));
    ok('every chip has an accessible label',
       chips.every(c => /^React with /.test(c.getAttribute('aria-label') || '')));
  }

  // ── 11. Username gate ─────────────────────────────────────────────────────
  {
    // No username yet: a caption is not enough to enable Post.
    const { api, store } = run({ boot: NO_USERNAME });
    ok('needsUsername() is true without a username',
       api._internals.needsUsername() === true);
    store.cfCaption.value = 'Hello world';
    api._internals.updatePostButton();
    ok('a caption does NOT enable Post without a username',
       store.cfPostBtn.disabled === true);

    // The gate is lifted in place once a name is saved, with no reload.
    api._internals.setUsername('betz');
    api._internals.updatePostButton();
    ok('saving a username unlocks Post',
       store.cfPostBtn.disabled === false);
    ok('needsUsername() is false once a username exists',
       api._internals.needsUsername() === false);

    // A username present from the start is never gated.
    const named = run({ boot: MEMBER });
    named.store.cfCaption.value = 'Hello world';
    named.api._internals.updatePostButton();
    ok('a named member posts without any gate',
       named.store.cfPostBtn.disabled === false);

    // The requirement can be switched off by config.
    const relaxed = run({ boot: Object.assign({}, MEMBER, {
      username: '', usernameRequired: false }) });
    relaxed.store.cfCaption.value = 'Hello world';
    relaxed.api._internals.updatePostButton();
    ok('COMMUNITY_REQUIRE_USERNAME=0 disables the gate',
       relaxed.store.cfPostBtn.disabled === false);
  }

  // ── 12. saveUsername validates and posts to the app-wide endpoint ─────────
  {
    let called = null;
    const { api, store } = run({
      boot: NO_USERNAME,
      fetchImpl: feedFetch(function (url, opts) {
        if (String(url) === '/api/user/username') {
          called = { url: url, body: JSON.parse(opts.body) };
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ success: true, username: 'betz' }),
          });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      }),
    });

    // A too-short name is refused client-side, without a network call.
    store.cfUsernameInput.value = 'ab';
    api.saveUsername();
    ok('a too-short username is refused before any request', called === null);
    ok('the hint explains the rule',
       /3-24/.test(store.cfUsernameHint.textContent || ''));

    store.cfUsernameInput.value = 'betz';
    api.saveUsername();
    await tick();
    ok('a valid username posts to /api/user/username',
       called && called.url === '/api/user/username', JSON.stringify(called));
    ok('the username is sent in the body',
       called && called.body.username === 'betz');
    ok('the gate element is removed after saving',
       store.cfUsernameGate.parent === null
       || store.cfUsernameGate.parent === undefined);
    // Post still needs content — the username only removes the gate.
    store.cfCaption.value = 'Hello world';
    api._internals.updatePostButton();
    ok('the composer unlocks after saving a username',
       store.cfPostBtn.disabled === false);

    // A taken name surfaces the server's message.
    const taken = run({
      boot: NO_USERNAME,
      fetchImpl: feedFetch(function () {
        return Promise.resolve({
          ok: false, status: 409,
          json: () => Promise.resolve({
            success: false, error: 'Username is already taken' }),
        });
      }),
    });
    taken.store.cfUsernameInput.value = 'taken';
    taken.api.saveUsername();
    await tick();
    ok('a taken username shows the server error',
       /already taken/i.test(taken.store.cfUsernameHint.textContent || ''),
       taken.store.cfUsernameHint.textContent);
    ok('the Save button is re-enabled after a failure',
       taken.store.cfUsernameSave.disabled === false);
  }

  console.log('');
  if (failures) {
    console.error(failures + ' CHECK(S) FAILED');
    process.exit(1);
  }
  console.log('ALL COMMUNITY FEED HARNESS CHECKS PASSED');
})();
