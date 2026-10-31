/* Node harness over the REAL static/js/community-feed.js.

   Runs the bundle in a VM under DOM stubs and drives the genuine bodies for:
   - composer Post-button gating (caption / over-limit / whitespace),
   - the caption linkifier (the non-capturing URL_RE rule),
   - optimistic reaction -> reconcile, and rollback on failure,
   - switching a reaction keeping the count flat,
   - the three-hop upload: presign -> PUT straight to R2 with the SIGNED headers,
   - the client-side media type allow-list,
   - the guest guard (a guest can never post or queue an upload),
   - buildCard: caption as TEXT (no HTML injection) and a hidden reaction picker,
   - the Community Menu Wallet modal's Celo-only read/refresh, token picker,
     review/confirmation, and guest guard;
   - the authenticated Daily Task menu action opening the shared modal in place.

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
      attributes: {}, listeners: {}, parent: null, parentNode: null,
      // A real CSSStyleDeclaration has setProperty/getPropertyValue as well as
      // direct property assignment; the stub mirrors that so a bundle call to
      // either form works instead of throwing "not a function".
      style: {
        _vars: {},
        setProperty(name, value) { this._vars[name] = String(value); this[name] = String(value); },
        getPropertyValue(name) { return this._vars[name] || ''; },
      },
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
        c.parentNode = this;
        return c;
      },
      appendChild(c) { return this._attach(c); },
      insertBefore(c) { return this._attach(c, 0); },
      removeChild(c) {
        this.childNodes = this.childNodes.filter(x => x !== c);
        this.children = this.children.filter(x => x !== c);
        if (c && c.parent === this) { c.parent = null; c.parentNode = null; }
        return c;
      },
      replaceChild(next, old) {
        const at = this.children.indexOf(old);
        if (at !== -1) {
          this.children.splice(at, 1, next);
          this.childNodes = this.children.slice();
        }
        if (old) { old.parent = null; old.parentNode = null; }
        next.parent = this;
        next.parentNode = this;
        return old;
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
      scrollIntoView() {}, focus() { this.focused = true; },
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
        n.parentNode = host;
        return n;
      },
      get firstElementChild() { return this.children[0] || null; },
      set innerHTML(v) { if (v === '') { this.children = []; this.childNodes = []; } },
      get innerHTML() { return ''; },
      // Real DOM semantics: textContent aggregates descendants, so a caption
      // built with createTextNode + appendChild reports its full text. Assigning
      // textContent clears the children, so a later appendChild must still show
      // up here — hence `_text` is a PREFIX, not a replacement.
      get textContent() {
        const own = this._text === undefined ? '' : this._text;
        return own + this.childNodes.map(c => c.textContent).join('');
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
   'cfUsernameGate', 'cfUsernameInput', 'cfUsernameSave', 'cfUsernameHint',
   'cfBottomNav', 'cfSearchBar', 'cfSearchInput', 'cfSearchResults',
   'cfCommunityCardsModal', 'cfCommunityCardsClose', 'cfCommunityCardsContent',
   'cfCommunityCardsDescription', 'cfVaultDonationCard',
   'cfReactionUsersModal', 'cfReactionUsersClose', 'cfReactionUsersFilters',
   'cfReactionUsersStatus', 'cfReactionUsersList', 'cfReactionUsersRetry', 'cfReactionUsersMore',
   'cfSheetEarn', 'cfSheetOverlay', 'cfMenuCommunityCards',
   'cfMenuEarnMoreToggle', 'cfMenuEarnMore', 'cfMenuWallet',
   'cfAirtimeModal', 'cfAirtimeFrame',
   'cfEarnPageModal', 'cfEarnPageFrame', 'cfEarnPageTitle', 'cfEarnPageClose',
   'cfCommunityWalletModal', 'cfCommunityWalletAddress', 'cfCommunityWalletStatus',
   'cfCommunityWalletRefresh', 'cfCommunityWalletClose', 'cfCommunityWalletSendOpen',
   'cfWalletOverview', 'cfWalletSendTokenPicker', 'cfWalletSendForm', 'cfWalletSendReview',
   'cfWalletSendError', 'cfWalletReviewToken', 'cfWalletReviewAmount', 'cfWalletReviewRecipient',
   'cfWalletSendBalanceCelo', 'cfWalletSendBalanceGd', 'cfWalletSendBalanceCusd',
   'cfWalletSendBalanceUsdt', 'cfWalletSendBalanceUsdc', 'cfWalletSendBalancePhpm',
   'sendTo', 'sendAmount', 'sendTokenLabel', 'sendAlert', 'sendBtn', 'dailyTaskModal',
   'cfWalletBalanceCelo', 'cfWalletBalanceGd', 'cfWalletBalanceCusd',
   'cfWalletBalanceUsdt', 'cfWalletBalanceUsdc', 'cfWalletBalancePhpm',
   'cfMessagePushPrompt', 'cfMessagePushStatus', 'cfMessagePushCopy',
   'cfMessagePushEnable', 'cfMessagePushLater',
   'claimModal',
   'cfRail',
   'cfSuggested', 'cfSuggestedList', 'cfSuggestedStatus', 'cfTodayHub',
   'cfPinnedDonations', 'cfPinnedDonationFeed', 'cfPinnedDonationMore',
   'cfPinnedDonationBody', 'cfPinnedDonationsToggle',
   'cfEarn', 'cfEarnChip', 'cfEarnAmount', 'cfEarnGoal', 'cfEarnBar',
   'cfEarnBarFill', 'cfEarnNote', 'cfEarnRates', 'cfEarnRatesToggle',
   'cfEarnBalance', 'cfEarnLead'
  ].forEach(id => {
    const node = dom.mk(
      id === 'cfCaption' || id === 'cfEditCaption' || id === 'cfUsernameInput'
        || id === 'cfSearchInput'
        ? 'textarea' : 'div');
    node.id = id;
    registry[id] = node;
  });
  registry.cfPreviews.hidden = true;
  // Mirror the markup: the card and the balance block both start hidden, so a
  // "revealed" assertion cannot pass on the stub's default `false`.
  registry.cfEarn.hidden = true;
  registry.cfEarnBalance.hidden = true;
  registry.cfEarnRates.hidden = true;
  registry.cfEarnRatesToggle.hidden = true;
  registry.cfEarnRatesToggle.setAttribute('aria-expanded', 'false');
  registry.cfUsernameInput.value = '';
  registry.cfSearchInput.value = '';
  // The Buy Airtime iframe ships with the modal: `hidden` until opened, and
  // its src parked on data-src so the store never loads until a tap.
  registry.cfAirtimeModal.hidden = true;
  registry.cfEarnPageModal.hidden = true;
  registry.cfCommunityWalletModal.hidden = true;
  registry.cfWalletSendTokenPicker.hidden = true;
  registry.cfWalletSendForm.hidden = true;
  registry.cfWalletSendReview.hidden = true;
  registry.dailyTaskModal.className = '';
  registry.cfCommunityCardsModal.hidden = true;
  // The suggestions panel ships hidden (revealed only on a successful
  // response) and the side hub with it, matching the markup.
  registry.cfSuggested.hidden = true;
  registry.cfSuggestedStatus.hidden = true;
  registry.cfTodayHub.hidden = true;
  // Mirror the markup's nesting so the Community Cards relocation has a real
  // source parent to move from: the G$ card and the suggestions panel start in
  // the side hub, and the vault card starts in the primary column.
  registry.cfEarn.parent = registry.cfTodayHub;
  registry.cfEarn.parentNode = registry.cfTodayHub;
  registry.cfSuggested.parent = registry.cfTodayHub;
  registry.cfSuggested.parentNode = registry.cfTodayHub;
  registry.cfVaultDonationCard.parent = registry.cfComposer;
  registry.cfVaultDonationCard.parentNode = registry.cfComposer;
  // The pinned donor section ships hidden (revealed only when there are pinned
  // posts) AND its body ships collapsed, so a "revealed" assertion cannot pass
  // on the stub's default `false`.
  registry.cfPinnedDonations.hidden = true;
  registry.cfPinnedDonationBody.hidden = true;
  registry.cfPinnedDonationsToggle.setAttribute('aria-expanded', 'false');
  registry.cfMenuEarnMore.hidden = true;
  registry.cfSheetEarn.setAttribute('aria-hidden', 'true');
  registry.cfSheetEarn.setAttribute('inert', '');
  registry.cfReactionUsersModal.hidden = true;
  registry.cfReactionUsersRetry.hidden = true;
  registry.cfReactionUsersMore.hidden = true;
  registry.cfMessagePushPrompt.hidden = true;
  registry.cfAirtimeFrame.setAttribute('data-src', '/reloadly/?embed=1#topup');
  registry.cfEarnPageFrame.setAttribute('data-farming-src', '/farming/?embed=1');
  registry.cfEarnPageFrame.setAttribute('data-savings-src', '/savings/?embed=1');
  // The shared claim dialog is rendered directly and starts closed.
  registry.claimModal.className = '';
  registry.claimModal.setAttribute('aria-hidden', 'true');

  // Record document-level listeners so the real keydown handler can be driven
  // (Escape must close every dialog, including the claim modal).
  const docListeners = {};
  const doc = {
    readyState: 'complete',
    body: dom.root,
    createElement: dom.mk,
    createTextNode: t => ({ nodeType: 3, textContent: String(t) }),
    // The Community Cards relocation marks each card's original slot with a
    // comment node so it can be restored exactly. A bare object is enough: the
    // bundle only reads `.parentNode` and passes it to insertBefore/removeChild.
    createComment: () => ({ nodeType: 8, parent: null }),
    getElementById: id => registry[id] || null,
    querySelector: sel => registry[String(sel).replace('#', '')] || dom.root.querySelector(sel),
    querySelectorAll: sel => dom.root.querySelectorAll(sel),
    addEventListener: (name, fn) => { (docListeners[name] = docListeners[name] || []).push(fn); },
    removeEventListener: () => {},
    dispatch: (name, event) => (docListeners[name] || []).forEach(fn => fn(event)),
  };

  let nextTimerId = 1;
  const timeoutCallbacks = new Map();
  const windowListeners = {};
  const sandbox = {
    document: doc, console,
    addEventListener: (name, fn) => { (windowListeners[name] = windowListeners[name] || []).push(fn); },
    dispatchWindowEvent: (name, event) => (windowListeners[name] || []).forEach(fn => fn(event)),
    setInterval: () => 1,
    clearInterval: () => {},
    // Honour the delay with a real timer. A "flush on the next microtask"
    // stub would fire a long cleanup timer (e.g. the 900ms double-tap heart
    // burst removal) before the assertion that observes it, hiding real
    // behaviour. `tick()` (5ms) still flushes 0ms callbacks.
    setTimeout: (fn, delay) => {
      const id = nextTimerId++;
      const handle = setTimeout(() => { timeoutCallbacks.delete(id); fn(); }, delay || 0);
      timeoutCallbacks.set(id, handle);
      return id;
    },
    clearTimeout: id => {
      if (timeoutCallbacks.has(id)) { clearTimeout(timeoutCallbacks.get(id)); timeoutCallbacks.delete(id); }
    },
    requestAnimationFrame: fn => fn(),
    fetch: opts.fetchImpl || (() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })),
    navigator: { userAgent: 'node' },
    Image: function () { this.onload = null; this.onerror = null; },
    URL: { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} },
    Promise, Object, Array, JSON, Math, Date, Number, String, RegExp, Error,
    isNaN, parseInt, parseFloat, encodeURIComponent,
  };
  sandbox.window = sandbox;
  sandbox.innerWidth = opts.innerWidth === undefined ? 1280 : opts.innerWidth;
  // The bundle remembers the pinned-donor toggle for the session. The stub is
  // per-run so one run cannot leak its choice into the next.
  const sessionStore = new Map();
  sandbox.sessionStorage = {
    getItem: key => (sessionStore.has(key) ? sessionStore.get(key) : null),
    setItem: (key, value) => { sessionStore.set(key, String(value)); },
    removeItem: key => { sessionStore.delete(key); },
  };
  sandbox.globalThis = sandbox;
  sandbox.GM_COMMUNITY_BOOT = opts.boot || {};
  // The shared wallet boot payload. `fvVerified` drives the Garden and Daily
  // Task face-verification gates, so the harness must set it exactly like the
  // server-rendered templates do. Default: a verified member.
  sandbox.GM_WALLET_BOOT = opts.walletBoot || { fvVerified: true };
  sandbox.GMWalletCommunitySend = opts.communitySend || {
    selected: null,
    selectToken(token, button) {
      this.selected = token;
      if (button && button.classList) button.classList.add('active');
      const labels = { CELO: 'CELO', GD: 'G$', CUSD: 'cUSD', USDT: 'USDT', USDC: 'USDC', PHPM: 'PHPm' };
      registry.sendTokenLabel.textContent = labels[token] || token;
      return true;
    },
    submit() { return Promise.resolve(true); },
  };
  sandbox.openModal = function (id) {
    if (registry[id]) registry[id].classList.add('open');
    dom.root.style.overflow = 'hidden';
    dom.root.classList.add('gm-modal-open');
  };
  sandbox.openDailyTaskModal = function () { sandbox.openModal('dailyTaskModal'); };
  sandbox.closeModal = function (id) {
    if (registry[id]) registry[id].classList.remove('open');
    dom.root.style.overflow = '';
    dom.root.classList.remove('gm-modal-open');
    dom.root.classList.remove('cf-modal-open');
  };
  sandbox.prompt = () => '';
  sandbox.confirm = () => true;
  sandbox.location = { origin: 'https://goodmarketph.live', hash: '' };
  if (opts.XMLHttpRequest) sandbox.XMLHttpRequest = opts.XMLHttpRequest;

  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return { api: sandbox.GMCommunity, store: registry, sandbox, document: doc };
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
  // The same destination list the template emits (GM_COMMUNITY_BOOT.nav).
  // Wallet balances are accessed from inside Menu; no /wallet destination is
  // exposed in the Community navigation.
  nav: [
    { id: 'claim', icon: '🪙', label: 'Claim Free G$', href: '/wallet?claim=1', action: 'claim', gold: true, guestOk: false },
    { id: 'search', icon: '🔍', label: 'Search', action: 'search', guestOk: true },
    { id: 'messages', icon: '💬', label: 'Messages', href: '/messages', action: 'messages', guestOk: false },
    { id: 'airtime', icon: '📱', label: 'Buy Airtime', href: '/reloadly/?embed=1#topup', action: 'airtime', guestOk: false },
    { id: 'menu', icon: '☰', label: 'Menu', action: 'menu', guestOk: true },
  ],
  reactions: { like: '👍', love: '❤️', haha: '😂', wow: '😮', sad: '😢', angry: '😡' },
};

// Same as MEMBER but with no username yet — the gate must block posting.
const NO_USERNAME = Object.assign({}, MEMBER, {
  username: '', usernameRequired: true,
});

// A signed-out visitor: no wallet, no username, and the connect hook is what
// every write destination must reach.
const GUEST = Object.assign({}, MEMBER, {
  isGuest: true, wallet: '', username: '',
  usernameRequired: false, loginMethod: '',
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
    // The bundle's init() also fetches the rewards card; scope the assertion
    // to the reaction request itself rather than to the call count.
    const reactionCalls = () => calls.filter(c => /\/reaction$/.test(c.url));
    calls.length = 0;
    api.react(p, 'love');
    const posted = reactionCalls();
    ok('reaction POSTs to the post endpoint',
       posted.length === 1 && posted[0].url === '/community/api/posts/5/reaction'
         && posted[0].method === 'POST', JSON.stringify(calls));
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

  // ── 6c. Progress: the PUT goes through XHR so bytes-sent is reportable ────
  // fetch() cannot report upload progress, so a 100MB video showed a frozen
  // tile and no "uploading" cue — the "did it even start?" symptom. The real
  // body must build an XHR, keep the SIGNED headers, and surface fractions.
  {
    const xhrLog = { opened: null, headers: {}, sent: null };
    function FakeXHR() {
      this.upload = {};
      this.status = 0;
      this._listeners = {};
      this.open = (method, url) => { xhrLog.opened = { method, url }; };
      this.setRequestHeader = (k, v) => { xhrLog.headers[k] = v; };
      this.send = (blob) => {
        xhrLog.sent = blob;
        // Emulate the browser reporting a half-sent body, then a clean 200.
        if (this.upload.onprogress) {
          this.upload.onprogress({ lengthComputable: true, loaded: 50, total: 100 });
        }
        this.status = 200;
        if (this.onload) this.onload();
      };
    }
    const { api, store } = run({
      boot: MEMBER,
      XMLHttpRequest: FakeXHR,
      fetchImpl: feedFetch((url) => Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          success: true,
          upload_url: 'https://acct.r2.cloudflarestorage.com/bucket/u/abc/key.jpg',
          method: 'PUT', headers: { 'Content-Type': 'image/jpeg' },
          key: 'u/abc/key.jpg', public_url: 'https://media.example/u/abc/key.jpg',
          media_type: 'image', mime: 'image/jpeg',
        }),
      })),
    });

    const fractions = [];
    const item = await api._internals.presign('image/jpeg', 100);
    await api._internals.putToR2(item, 'BLOB', f => fractions.push(f));
    ok('the PUT is issued as an XHR (progress is reportable)',
       xhrLog.opened && xhrLog.opened.method === 'PUT', JSON.stringify(xhrLog.opened));
    ok('the XHR targets the presigned R2 URL',
       !!xhrLog.opened && xhrLog.opened.url.startsWith('https://acct.r2.cloudflarestorage.com'),
       JSON.stringify(xhrLog.opened));
    ok('the SIGNED Content-Type is still sent verbatim',
       xhrLog.headers['Content-Type'] === 'image/jpeg', JSON.stringify(xhrLog.headers));
    ok('the progress callback receives a byte fraction',
       fractions.length === 1 && fractions[0] === 0.5, JSON.stringify(fractions));

    // The live status line and the tile bar must actually move.
    const entry = { contentType: 'image/jpeg', blob: { type: 'image/jpeg', size: 100 } };
    api._internals.setPending([entry]);
    await api._internals.uploadAttachment(entry);
    ok('a finished attachment ends done with a full progress value',
       entry.state === 'done' && entry.progress === 1, JSON.stringify(entry.state));
    ok('a done tile paints a full progress bar',
       entry._fill && entry._fill.style.width === '100%', JSON.stringify(entry._fill && entry._fill.style));
  }

  // ── 6d. A failed PUT clears the spinner and reports the error line ────────
  {
    function FailingXHR() {
      this.upload = {};
      this.status = 500;
      this.open = () => {};
      this.setRequestHeader = () => {};
      this.send = () => { if (this.onload) this.onload(); };
    }
    const { api, store } = run({
      boot: MEMBER,
      XMLHttpRequest: FailingXHR,
      fetchImpl: feedFetch(() => Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          success: true,
          upload_url: 'https://acct.r2.cloudflarestorage.com/x',
          method: 'PUT', headers: { 'Content-Type': 'image/jpeg' },
          key: 'u/abc/key.jpg', public_url: 'https://media.example/u/abc/key.jpg',
          media_type: 'image', mime: 'image/jpeg',
        }),
      })),
    });
    const entry = { contentType: 'image/jpeg', blob: { type: 'image/jpeg', size: 100 } };
    api._internals.setPending([entry]);
    let threw = false;
    try { await api._internals.uploadAttachment(entry); } catch (e) { threw = true; }
    ok('a non-2xx PUT rejects', threw === true);
    ok('the failed attachment is marked failed', entry.state === 'failed', entry.state);
    ok('the status line reports the failure',
       store.cfUploadStatus.hidden === false
       && /Upload failed/i.test(store.cfUploadStatus.textContent || ''),
       store.cfUploadStatus.textContent);
    ok('the failure line is styled as an error, not busy',
       store.cfUploadStatus.className.indexOf('is-error') !== -1,
       store.cfUploadStatus.className);
  }

  // ── 6e. "Uploading…" is shown on the Post button while a PUT is in flight ─
  {
    const { api, store } = run({ boot: MEMBER });
    store.cfCaption.value = 'Look at this';
    api._internals.setPending([{ state: 'uploading', progress: 0.4, isVideo: false }]);
    api._internals.updatePostButton();
    ok('the Post button reads "Uploading…" during an upload',
       store.cfPostBtn.textContent === 'Uploading…', store.cfPostBtn.textContent);
    ok('the Post button is disabled while uploading',
       store.cfPostBtn.disabled === true);

    // A preparing file gets its own "Processing…" line.
    api._internals.setPending([{ state: 'preparing', isVideo: true, progress: 0 }]);
    api._internals.updateUploadStatus();
    ok('a preparing video shows a Processing line',
       /Processing video/i.test(store.cfUploadStatus.textContent || ''),
       store.cfUploadStatus.textContent);
    ok('the Processing line is styled as busy',
       store.cfUploadStatus.className.indexOf('is-busy') !== -1,
       store.cfUploadStatus.className);
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

  // ── 8. Guest: reads the feed, but every write raises the connect card ─────
  {
    const seen = [];
    let connectCalls = 0;
    // A bespoke fetchImpl (not feedFetch) so EVERY url is recorded — feedFetch
    // answers the feed internally and would never surface it in `seen`.
    const { api, store, sandbox } = run({
      // storageReady: true so addFiles reaches the WALLET gate rather than
      // stopping at the (unrelated) "uploads not configured" guard.
      boot: { isGuest: true, wallet: '', storageReady: true, reactions: {} },
      fetchImpl: function (url) {
        seen.push(String(url));
        if (String(url).indexOf('/community/api/feed') === 0) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({
            success: true, posts: [makePost({ id: 7, body: 'public post' })],
            next_cursor: null, has_more: false }) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      },
    });
    sandbox.window.openGoodMarketGetStarted = function () { connectCalls++; };
    await tick();

    // Reading is public: the guest's init() loads the feed like a member's.
    ok('a guest fetches the feed',
       seen.some(u => u.indexOf('/community/api/feed') === 0), JSON.stringify(seen));
    ok('a guest renders the posts it fetched',
       api._internals.getFeed().length === 1);
    ok('the cards are really in the DOM',
       !!store.cfFeed.querySelector('[data-post-id="7"]'));

    // Writing is gated, and the gate is the shared connect card. Reset the
    // counter per action so one assertion cannot mask another's failure.
    function gate(label, fn) {
      const before = connectCalls;
      fn();
      ok(label, connectCalls === before + 1, String(connectCalls));
    }

    gate('posting as a guest opens the connect card', function () {
      api.submitPost();
    });
    ok('a guest cannot post', api._internals.getFeed().length === 1);
    gate('queueing an upload as a guest opens the connect card', function () {
      api.addFiles([{ type: 'image/jpeg', size: 10, name: 'a.jpg' }]);
    });
    ok('a guest cannot queue an upload', api._internals.getPending().length === 0);
    gate('reacting as a guest opens the connect card', function () {
      api.react(makePost({ id: 7 }), 'like');
    });
    // Report is driven through the real card button (the user's actual path)
    // rather than an internal export, so the test cannot drift from the UI.
    const reportCard = api._internals.buildCard(makePost({ id: 9 }));
    const reportBtn = reportCard.querySelector('.cf-card-actions').children
      .filter(b => /Report/.test(b.textContent))[0];
    gate('reporting as a guest opens the connect card', function () {
      reportBtn.onclick();
    });
    ok('a gated report posts nothing',
       !seen.some(u => /\/api\/report/.test(u)), JSON.stringify(seen));

    // Comment is gated BEFORE the network: a guest must not even fetch the
    // thread, or they would see a comment box they cannot use.
    const card = api._internals.buildCard(makePost({ id: 8 }));
    const commentBtn = card.querySelector('.cf-card-actions').children
      .filter(b => /Comment/.test(b.textContent))[0];
    gate('the comment button opens the connect card for a guest', function () {
      commentBtn.onclick();
    });
    ok('a gated comment fetches no thread',
       !seen.some(u => /comments/.test(u)), JSON.stringify(seen));
  }

  // ── 9. buildCard renders the caption as TEXT, never markup ────────────────
  // Instagram's caption shape is `author + text`. The author is a real ANCHOR
  // (one element), and the body is raw TEXT nodes — so the injection guarantee
  // is now asserted on the text nodes specifically, not on the whole caption.
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      body: '<img src=x onerror=alert(1)> hello',
    }));
    const caption = card.querySelector('.cf-caption');
    ok('the caption node exists', !!caption);
    ok('the caption text ends with the raw body as TEXT (no HTML injection)',
       caption.textContent.endsWith('<img src=x onerror=alert(1)> hello'),
       JSON.stringify(caption.textContent));
    ok('the injected markup created no element',
       !caption.querySelector('img'),
       JSON.stringify((caption.children || []).map(c => c.tagName)));
    // The ONLY element child is the author link — the body added none.
    ok('the caption holds exactly the author link as its element child',
       caption.children.length === 1
         && caption.children[0].className === 'cf-caption-author',
       JSON.stringify((caption.children || []).map(c => c.className)));
    // The body itself is a bare text node, which is what makes the injection
    // impossible: markup in a text node cannot become an element.
    ok('the body is written as a raw TEXT node',
       caption.childNodes.some(n => n.nodeType === 3
         && n.textContent === '<img src=x onerror=alert(1)> hello'),
       JSON.stringify(caption.childNodes.map(n => n.nodeType)));
  }

  // ── 9b. Caption + engagement PLACEMENT ────────────────────────────────────
  // The caption is below the media; actions, counts, the hidden picker and the
  // expanded thread are grouped together immediately after it.
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      body: 'Unang araw ng paluwagan namin!',
      media: [{ type: 'image', url: 'https://media.example/a.jpg' }],
    }));
    const order = [];
    (function walk(node) {
      for (const child of node.children || []) {
        for (const cls of ['cf-media', 'cf-caption', 'cf-card-actions',
                           'cf-summary']) {
          if ((child.className || '').split(/\s+/).includes(cls)) order.push(cls);
        }
        walk(child);
      }
    })(card);
    const at = (name) => order.indexOf(name);
    ok('the card renders media, caption and grouped engagement controls',
       order.length === 4, JSON.stringify(order));
    ok('the caption sits BELOW the media', at('cf-media') < at('cf-caption'),
       JSON.stringify(order));
    ok('summary and actions follow the caption in order',
       at('cf-caption') < at('cf-summary')
         && at('cf-summary') < at('cf-card-actions'),
       JSON.stringify(order));
    const picker = card.querySelector('.cf-reaction-picker');
    ok('the picker is inside the action row and hidden by default',
       !!picker && picker.hidden
         && card.querySelector('.cf-card-actions').contains(picker));
    ok('there is no permanent reaction toolbar row',
       !card.querySelector('.cf-reactions'));
    const engagement = card.querySelector('.cf-engagement');
    const commentBtn = card.querySelector('.cf-card-actions').children
      .filter(b => /Comment/.test(b.textContent))[0];
    commentBtn.onclick();
    ok('the expanded comment thread stays in the engagement group',
       !!engagement.querySelector('.cf-comments'));
  }

  // ── 9c. Multi-level replies retain parent mentions and render as text ──────
  {
    const { api, document } = run({ boot: MEMBER });
    const post = { id: 77, comment_count: 1 };
    const wrap = document.createElement('div');
    api._internals.setComments(77, [
      { id: 1, parent_comment_id: null, username: 'wewe', body: 'Root', is_mine: false, is_post_author: false },
      { id: 2, parent_comment_id: 1, reply_to_username: 'wewe', username: 'betz', body: 'Thanks', is_mine: true, is_post_author: true },
      { id: 3, parent_comment_id: 2, reply_to_username: 'betz', username: 'wewe', body: '<img src=x onerror=alert(1)>', is_mine: false, is_post_author: false },
    ]);
    api._internals.renderComments(post, wrap);
    const nodes = wrap.querySelectorAll('.cf-comment-node');
    const nodeFor = (id) => nodes.find(n => n.getAttribute('data-comment-id') === String(id));
    ok('three comment nodes render from the nested thread data', nodes.length === 3,
       String(nodes.length));
    ok('reply-to-reply keeps the direct @mention',
       nodeFor(3) && nodeFor(3).querySelector('.cf-reply-mention')
         && nodeFor(3).querySelector('.cf-reply-mention').textContent === '@betz');
    ok('the post author\'s reply receives the Author badge',
       nodeFor(2) && nodeFor(2).querySelector('.cf-author-badge')
         && nodeFor(2).querySelector('.cf-author-badge').textContent === 'Author');
    ok('reply content is text and cannot create an image node',
       !wrap.querySelector('img') && wrap.textContent.includes('<img src=x onerror=alert(1)>'));
    ok('thread shows a direct replies collapse control',
       nodeFor(1) && nodeFor(1).querySelector('.cf-replies-toggle')
         && nodeFor(1).querySelector('.cf-replies-toggle').textContent.includes('1 reply'));
    const rootReply = nodeFor(1) && nodeFor(1).querySelector('.cf-comment-action');
    if (rootReply && rootReply.onclick) rootReply.onclick();
    ok('reply action opens an inline composer naming its direct target',
       wrap.querySelector('.cf-replying-to')
         && wrap.querySelector('.cf-replying-to').textContent === 'Replying to @wewe');
  }

  // ── 9d. A caption-only post still renders its caption (no media branch) ───
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      body: 'Walang litrato, salita lang.', media: [],
    }));
    const caption = card.querySelector('.cf-caption');
    ok('a text-only post renders its caption',
       !!caption && caption.textContent.endsWith('Walang litrato, salita lang.'),
       caption ? JSON.stringify(caption.textContent) : 'missing');
    ok('the caption leads with the author link (Instagram shape)',
       !!caption && caption.children[0].className === 'cf-caption-author'
         && caption.children[0].textContent === 'betz',
       caption ? JSON.stringify(caption.children[0] && caption.children[0].className) : 'missing');
    ok('the caption author links to the public profile by USERNAME',
       caption.children[0].href === '/community/u/betz',
       caption.children[0].href);
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

  // ── 10. buildCard: six reaction choices inside the hidden picker ─────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({ my_reaction: 'love' }));
    const picker = card.querySelector('.cf-reaction-picker');
    const chips = picker ? picker.querySelectorAll('.cf-chip') : [];
    ok('all six choices are in the reaction picker', chips.length === 6, String(chips.length));
    ok('the six choices are hidden until the picker opens', !!picker && picker.hidden);
    const pressed = chips.filter(c => c.getAttribute('aria-pressed') === 'true');
    ok('exactly the viewer reaction is marked pressed',
       pressed.length === 1 && pressed[0].textContent === '❤️',
       JSON.stringify(pressed.map(c => c.textContent)));
    ok('every chip has an accessible label',
       chips.every(c => /^React with /.test(c.getAttribute('aria-label') || '')));
  }

  // ── 10b. buildCard: the author's profile picture, or their initial ────────
  {
    const { api } = run({ boot: MEMBER });
    const withPic = api._internals.buildCard(makePost({
      username: 'betz', avatar_url: 'https://media.example/u/deadbeef/pic.jpg',
    }));
    const img = withPic.querySelector('.cf-avatar-img');
    ok('an author with a picture shows the photo',
       !!img && img.src === '/community/api/profile/betz/avatar',
       img ? img.src : 'no img');
    ok('the avatar link still points at the profile',
       withPic.querySelector('.cf-profile-link').href === '/community/u/betz');

    const withoutPic = api._internals.buildCard(makePost({ username: 'betz' }));
    const initial = withoutPic.querySelector('.cf-avatar');
    ok('an author without a picture falls back to the initial',
       !!initial && initial.textContent === 'B', initial ? initial.textContent : 'none');
    ok('the fallback renders no <img>',
       !withoutPic.querySelector('.cf-avatar-img'));

    const legacyName = api._internals.buildCard(makePost({
      username: 'Community member',
      avatar_url: 'https://media.example/u/deadbeef/legacy.jpg',
    }));
    ok('an invalid username never falls back to the raw R2 URL',
       !legacyName.querySelector('.cf-avatar-img'));
  }

  // ── 10c. buildCard: the stacked reaction summary ──────────────────────────
  // A social feed shows WHICH reactions a post got, not one anonymous emoji.
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      reaction_count: 12,
      reaction_breakdown: { love: 7, like: 4, haha: 1 },
    }));
    const emojis = card.querySelectorAll('.cf-summary-emoji')
      .map(n => n.textContent);
    ok('the summary stacks the most-used reactions first',
       emojis[0] === '❤️' && emojis[1] === '👍', JSON.stringify(emojis));
    ok('the summary shows at most three reaction kinds',
       emojis.length === 3, String(emojis.length));
    ok('the headline number is still the authoritative total',
       card.querySelector('.cf-count').textContent === '12',
       card.querySelector('.cf-count').textContent);
    ok('the summary emojis are decorative (not announced twice)',
       card.querySelector('.cf-summary-emojis')
         .getAttribute('aria-hidden') === 'true');

    // Four kinds: the fourth is collapsed into a "+1" chip.
    const many = api._internals.buildCard(makePost({
      reaction_count: 9,
      reaction_breakdown: { like: 4, love: 3, haha: 1, wow: 1 },
    }));
    ok('a fourth reaction kind collapses into a +N chip',
       many.querySelector('.cf-summary-more').textContent === '+1',
       many.querySelector('.cf-summary-more').textContent);

    // No breakdown (an old row / un-migrated table): one 👍, never a gap.
    const bare = api._internals.buildCard(makePost({ reaction_count: 3 }));
    const bareEmojis = bare.querySelectorAll('.cf-summary-emoji');
    ok('a missing breakdown falls back to a single 👍',
       bareEmojis.length === 1 && bareEmojis[0].textContent === '👍',
       JSON.stringify(bareEmojis.map(n => n.textContent)));

  // The viewer's own reaction always gets a seat, even when it is the
  // least-used kind.
  const mine = api._internals.buildCard(makePost({
      reaction_count: 20,
      my_reaction: 'sad',
      reaction_breakdown: { like: 19, sad: 1 },
    }));
    ok('the viewer reaction is always shown first',
       mine.querySelector('.cf-summary-emoji').textContent === '😢',
       mine.querySelector('.cf-summary-emoji').textContent);
  }

  // ── 10c-1. Comment names link only when a profile username is available ──
  {
    const { api } = run({ boot: MEMBER });
    const post = makePost({ id: 41 });
    const live = api._internals.renderCommentNode(post, {
      id: 7, username: 'ana', profile_username: 'ana', body: 'hello',
      is_deleted: false, avatar_url: null,
    }, {}, 0, {});
    const authorLink = live.querySelector('.cf-comment-profile-link');
    const avatarLink = live.querySelector('.cf-comment-profile-avatar-link');
    ok('a live commenter name links to the username profile',
       !!authorLink && authorLink.href === '/community/u/ana', authorLink && authorLink.href);
    ok('a live commenter avatar links to the same profile',
       !!avatarLink && avatarLink.href === '/community/u/ana', avatarLink && avatarLink.href);

    const anonymous = api._internals.renderCommentNode(post, {
      id: 8, username: 'Community member', profile_username: null, body: 'legacy',
      is_deleted: false, avatar_url: null,
    }, {}, 0, {});
    ok('a legacy anonymous commenter is not turned into a profile link',
       !anonymous.querySelector('.cf-comment-profile-link'));
    const deleted = api._internals.renderCommentNode(post, {
      id: 9, username: 'Deleted comment', profile_username: 'ana', body: 'deleted',
      is_deleted: true, avatar_url: null,
    }, {}, 0, {});
    ok('a deleted commenter remains non-clickable',
       !deleted.querySelector('.cf-comment-profile-link')
       && !deleted.querySelector('.cf-comment-profile-avatar-link'));
  }

  // ── 10c-2. The reaction summary opens a paginated/filterable public list ──
  {
    const requests = [];
    const { api, store, document } = run({
      boot: GUEST,
      fetchImpl: url => {
        const requestUrl = String(url);
        if (requestUrl.indexOf('/reactions?') === -1) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({
            success: true, posts: [], next_cursor: null, has_more: false,
          }) });
        }
        requests.push(requestUrl);
        const payload = requests.length === 1 ? {
          success: true,
          reactors: [
            { username: 'ana', profile_username: 'ana', avatar_url: null, reaction: 'love' },
            { username: 'Community member', profile_username: null, avatar_url: null, reaction: 'haha' },
          ],
          next_cursor: 'opaque.cursor.token', has_more: true,
        } : requests.length === 2 ? {
          success: true,
          reactors: [{ username: 'teo', profile_username: 'teo', avatar_url: null, reaction: 'like' }],
          next_cursor: null, has_more: false,
        } : {
          success: true,
          reactors: [{ username: 'ana', profile_username: 'ana', avatar_url: null, reaction: 'love' }],
          next_cursor: null, has_more: false,
        };
        return Promise.resolve({ ok: true, json: () => Promise.resolve(payload) });
      },
    });
    const card = api._internals.buildCard(makePost({ reaction_count: 3 }));
    const trigger = card.querySelector('.cf-reaction-count-trigger');
    ok('reaction count is a button with the accessible dialog target',
       !!trigger && trigger.tagName === 'BUTTON'
       && trigger.getAttribute('aria-controls') === 'cfReactionUsersModal');
    ok('reaction button visibly tells users they can see who reacted',
       trigger && trigger.textContent.indexOf('See who reacted') >= 0);
    trigger.onclick();
    await tick();
    ok('tapping reaction total opens the users modal for guests',
       store.cfReactionUsersModal.hidden === false && requests.length === 1);
    ok('the reactor request is deferred until the summary is tapped',
       requests[0] === '/community/api/posts/5/reactions?limit=25', requests[0]);
    ok('named reactors link to public profiles',
       store.cfReactionUsersList.querySelector('.cf-reactor-name').href === '/community/u/ana');
    ok('reactor fallback is generic and contains no wallet address',
       store.cfReactionUsersList.textContent.indexOf('Community member') >= 0
       && !/0x[0-9a-f]{8,}/i.test(store.cfReactionUsersList.textContent));
    ok('load-more appears only when the API supplies a continuation',
       store.cfReactionUsersMore.hidden === false);
    store.cfReactionUsersMore.dispatch('click');
    await tick();
    ok('load-more sends the opaque keyset cursor',
       requests[1].indexOf('cursor=opaque.cursor.token') >= 0, requests[1]);
    const loveFilter = store.cfReactionUsersFilters.querySelectorAll('.cf-reaction-filter')
      .find(button => button.getAttribute('data-reaction-filter') === 'love');
    loveFilter.onclick();
    await tick();
    ok('reaction filter resets pagination and requests only that reaction',
       requests[2].indexOf('reaction=love') >= 0 && requests[2].indexOf('cursor=') === -1,
       requests[2]);
    store.cfReactionUsersClose.dispatch('click');
    ok('closing the reactor dialog restores focus and body scroll',
       store.cfReactionUsersModal.hidden === true
       && trigger.getAttribute('aria-expanded') === 'false'
       && !document.body.classList.contains('cf-modal-open'));
    trigger.onclick();
    await tick();
    let escapePrevented = false;
    document.dispatch('keydown', { key: 'Escape', preventDefault() { escapePrevented = true; } });
    ok('Escape closes the reactor dialog without leaving the page locked',
       escapePrevented && store.cfReactionUsersModal.hidden === true
       && !document.body.classList.contains('cf-modal-open'));
  }

  // ── 10c-3. A stale zero total cannot disable known reaction evidence ─────
  {
    const requests = [];
    const { api, store } = run({
      boot: GUEST,
      fetchImpl: url => {
        const requestUrl = String(url);
        if (requestUrl.indexOf('/reactions?') !== -1) requests.push(requestUrl);
        return Promise.resolve({ ok: true, json: () => Promise.resolve({
          success: true, reactors: [], next_cursor: null, has_more: false,
        }) });
      },
    });
    const card = api._internals.buildCard(makePost({
      reaction_count: 0, reaction_breakdown: { love: 2 },
    }));
    const trigger = card.querySelector('.cf-reaction-count-trigger');
    ok('a nonzero reaction breakdown overrides a stale zero-count disabled state',
       !!trigger && trigger.disabled === false
       && trigger.textContent.indexOf('See who reacted') >= 0);
    trigger.onclick();
    await tick();
    ok('the stale-count control still opens and requests the reactor list',
       store.cfReactionUsersModal.hidden === false && requests.length === 1);
    store.cfReactionUsersClose.dispatch('click');
  }

  // ── 10d. An optimistic reaction keeps the stacked summary in step ────────
  {
    const { api } = run({
      boot: MEMBER,
      fetchImpl: () => Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          success: true, reaction_count: 4, my_reaction: 'haha',
          reaction_breakdown: { like: 3, haha: 1 },
        }),
      }),
    });
    const post = makePost({
      id: 5, reaction_count: 4, my_reaction: 'like',
      reaction_breakdown: { like: 4 },
    });
    api.react(post, 'haha');
    ok('the optimistic summary moves the viewer reaction',
       post.my_reaction === 'haha' && post.reaction_breakdown.haha === 1
         && post.reaction_breakdown.like === 3,
       JSON.stringify(post.reaction_breakdown));
    ok('a switch keeps the optimistic total flat',
       post.reaction_count === 4, String(post.reaction_count));
    await tick();
    ok('the summary reconciles with the server tally',
       post.reaction_breakdown.haha === 1 && post.reaction_breakdown.like === 3
         && post.reaction_count === 4,
       JSON.stringify(post.reaction_breakdown));
  }

  // ── 10e. Search: the `@` is chrome, never typed ───────────────────────────
  {
    const seen = [];
    const { api, store } = run({
      boot: MEMBER,
      fetchImpl: feedFetch(function (url) {
        seen.push(String(url));
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            success: true, users: [{ username: 'betz' }],
          }),
        });
      }),
    });
    // A pasted `@betz` is stripped from the FIELD (the row draws its own `@`).
    store.cfSearchInput.value = '@betz';
    api.searchInput();
    // The field is cleaned synchronously, but the search itself is debounced
    // (220ms), so wait past the debounce before asserting the request.
    ok('a typed @ is stripped from the field', store.cfSearchInput.value === 'betz',
       store.cfSearchInput.value);
    await new Promise(r => setTimeout(r, 260));
    ok('the request carries the bare username',
       seen.some(u => /users\/search\?q=betz(&|$)/.test(u)), JSON.stringify(seen));

    // A stored `@` on the row is stripped before display, so no `@@betz`.
    api._internals.renderSearchResults([{ username: '@betz' }]);
    ok('a stored @ is not doubled in the results',
       store.cfSearchResults.querySelector('.cf-search-name').textContent === '@betz',
       store.cfSearchResults.querySelector('.cf-search-name').textContent);
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

  // ── 13. Bottom nav: destinations, guest gate, active item ─────────────────
  {
    const balanceRequests = [];
    const walletPayload = {
      success: true,
      celo: { success: true, balance_formatted: '2.000000 CELO' },
      gd: { success: true, balance_formatted: '12.500000 G$' },
      cusd: { success: true, balance_formatted: '3.000000 cUSD' },
      usdt: { success: true, balance_formatted: '4.000000 USDT' },
      usdc: { success: true, balance_formatted: '5.000000 USDC' },
      phpm: { success: true, balance_formatted: '42.000000 PHPm' },
    };
    const { api, store, sandbox } = run({
      boot: MEMBER,
      fetchImpl: feedFetch(function (url) {
        if (String(url).indexOf('/api/wallet/balances') === 0) {
          balanceRequests.push(String(url));
          return Promise.resolve({ ok: true, json: () => Promise.resolve(walletPayload) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ success: true }) });
      }),
    });
    const items = store.cfBottomNav.children;
    ok('the bottom nav renders every Community destination', items.length === 5,
       JSON.stringify(items.length));
    const labels = items.map(n => n.querySelector('.cf-nav-label').textContent);
    ok('the nav carries Claim Free G$, Search, Messages, Buy Airtime and Menu',
       JSON.stringify(labels) ===
       JSON.stringify(['Claim Free G$', 'Search', 'Messages', 'Buy Airtime', 'Menu']),
       JSON.stringify(labels));
    // The Feed tab is gone: this page IS the feed, so a tab pointing back at
    // /community was a dead tap. Nothing claims to be the active tab now.
    ok('no nav tab links back to /community',
       items.every(n => n.href !== '/community'));
    ok('no nav tab claims to be active on the feed page',
       items.every(n => !n.className.split(/\s+/).includes('is-active')));

    // Claim Free G$ opens the wallet's claim sheet IN the feed (see 13e); the
    // href stays the real page so middle-click / no-JS still work.
    const claim = items.filter(n => /Claim Free G\$/.test(n.textContent))[0];
    ok('Claim Free G$ keeps the real /wallet?claim=1 href',
       claim.href === '/wallet?claim=1', claim.href);
    ok('the Claim Free G$ chip is the gold money chip',
       claim.className.split(/\s+/).includes('cf-nav-gold'), claim.className);

    // Buy Airtime links to the Reloadly store's top-up tab (and opens the
    // in-page modal for a member — see 13d).
    const airtime = items.filter(n => /Buy Airtime/.test(n.textContent))[0];
    ok('Buy Airtime links to the embedded Reloadly top-up tab',
       airtime.href === '/reloadly/?embed=1#topup', airtime.href);
    const menu = items.filter(n => /Menu/.test(n.textContent))[0];
    ok('Menu is an accessible dialog button',
       menu.tagName === 'BUTTON'
         && menu.getAttribute('aria-haspopup') === 'dialog'
         && menu.getAttribute('aria-controls') === 'cfSheetEarn');
    menu.onclick({ preventDefault() {} });
    ok('Menu opens the community menu sheet', store.cfSheetEarn.className.includes('open'));
    ok('the menu marks its trigger expanded', menu.getAttribute('aria-expanded') === 'true');
    ok('opening the menu also shows the overlay',
       store.cfSheetOverlay.className.includes('open'));
    ok('opening the menu locks the page scroll',
       sandbox.document.body.classList.contains('cf-modal-open'));
    api.toggleMenuEarnMore(store.cfMenuEarnMoreToggle);
    ok('Earn More expands its submenu',
       store.cfMenuEarnMore.hidden === false
         && store.cfMenuEarnMoreToggle.getAttribute('aria-expanded') === 'true');
    api.openEarnPage('farming');
    ok('Farming opens in the in-community modal',
       store.cfEarnPageModal.hidden === false
         && store.cfEarnPageFrame.src === '/farming/?embed=1'
         && store.cfEarnPageTitle.textContent === 'Farming');
    ok('opening Farming stays on the Community page', !sandbox.location.href);
    api.closeEarnPage();
    ok('closing the Farming modal releases the page scroll',
       store.cfEarnPageModal.hidden === true
         && !sandbox.document.body.classList.contains('cf-modal-open'));
    api.openEarnPage('savings');
    ok('Savings also opens in the in-community modal',
       store.cfEarnPageModal.hidden === false
         && store.cfEarnPageFrame.src === '/savings/?embed=1'
         && store.cfEarnPageTitle.textContent === 'Savings');
    api.closeEarnPage();
    api.closeSheet();
    ok('closing the menu sheet hides it again',
       !store.cfSheetEarn.className.includes('open'));
    ok('closing the menu sheet unlocks the page scroll',
       !sandbox.document.body.classList.contains('cf-modal-open'));

    menu.onclick({ preventDefault() {} });
    ok('Wallet is offered inside the Community Menu', !!store.cfMenuWallet);
    api.openWalletModal(store.cfMenuWallet);
    ok('Wallet opens a Celo wallet modal without leaving Community',
       store.cfCommunityWalletModal.hidden === false && !sandbox.location.href);
    await tick();
    ok('the Wallet modal renders the Celo-native balance',
       store.cfWalletBalanceCelo.textContent === '2.000000 CELO');
    ok('the Wallet modal renders the PHPm balance',
       store.cfWalletBalancePhpm.textContent === '42.000000 PHPm');
    ok('the send token picker mirrors the live PHPm balance',
       store.cfWalletSendBalancePhpm.textContent === '42.000000 PHPm');
    ok('opening Wallet reads the authenticated Celo balance endpoint',
       balanceRequests.length === 1 && balanceRequests[0] === '/api/wallet/balances');
    api.refreshWalletBalances();
    await tick();
    ok('manual refresh requests a fresh Celo balance read',
       balanceRequests[1] === '/api/wallet/balances?force=1');
    api.openWalletSend();
    ok('Send opens the token picker inside the wallet modal',
       store.cfWalletSendTokenPicker.hidden === false && store.cfWalletOverview.hidden === true);
    const phpButton = store.cfWalletSendTokenPicker.querySelector('.cf-wallet-send-token') || {
      className: '', classList: { add(c) { this.owner.className += ' ' + c; }, owner: null },
      setAttribute() {}, focus() {},
    };
    phpButton.classList.owner = phpButton;
    ok('PHPm can be selected for a Community send', api.selectWalletSendToken('PHPM', phpButton));
    ok('selecting a token opens the recipient and amount form',
       store.cfWalletSendForm.hidden === false && store.cfWalletSendTokenPicker.hidden === true
         && store.sendTokenLabel.textContent === 'PHPm');
    store.sendTo.value = '0x1111111111111111111111111111111111111111';
    store.sendAmount.value = '1.25';
    ok('valid recipient and amount advance to transfer review', api.reviewWalletSend()
       && store.cfWalletSendReview.hidden === false
       && store.cfWalletReviewAmount.textContent === '1.25 PHPm'
       && store.sendBtn.textContent === 'Confirm send');
    await api.confirmWalletSend();
    ok('the review confirmation invokes the established wallet signing bridge',
       sandbox.GMWalletCommunitySend.selected === 'PHPM');
    api.backToWalletOverview();
    ok('Back to wallet returns to the balance overview', store.cfWalletOverview.hidden === false);
    api.closeWalletModal();
    ok('closing the Wallet modal restores page scrolling',
       store.cfCommunityWalletModal.hidden === true
         && !sandbox.document.body.classList.contains('cf-modal-open'));
    api.openDailyTask();
    ok('Daily Task opens the shared modal inside Community for members',
       store.dailyTaskModal.classList.contains('open')
         && sandbox.document.body.classList.contains('cf-modal-open'));
    api.closeDailyTask();
    ok('closing Daily Task restores the Community page scroll',
       !store.dailyTaskModal.classList.contains('open')
         && !sandbox.document.body.classList.contains('cf-modal-open'));

    // Daily Task is verified-only: an unverified member must NOT reach the
    // payment modal — the tap opens the shared Claim G$ verification flow
    // instead (the server refuses the payment endpoints for them anyway, and
    // the 20 G$ fee is non-refundable).
    {
      let fvOpens = 0;
      const gated = run({
        boot: MEMBER,
        walletBoot: { fvVerified: false },
        innerWidth: 1280,
        fetchImpl: feedFetch(() => Promise.resolve({
          ok: true, json: () => Promise.resolve({ success: true }) })),
      });
      await tick();
      gated.sandbox.window.openClaimOrVerify = function () { fvOpens++; };
      gated.api.openDailyTask();
      ok('an unverified member is sent to verification, not the payment modal',
         fvOpens === 1 && !gated.store.dailyTaskModal.classList.contains('open'),
         'fvOpens=' + fvOpens);
      ok('the verification bounce seeds the shared FV router flags',
         gated.sandbox.window._walletNeedsFV === true
           && gated.sandbox.window._walletFvReason === 'not_verified');
    }

    // …and Search opens the read-only member search for a member too.
    store.cfSearchBar.hidden = true;
    api.openSearch();
    ok('Search opens the member search bar', store.cfSearchBar.hidden === false);
    api.closeSearch();
    ok('closing search hides the bar', store.cfSearchBar.hidden === true);
  }

  // ── 13b. The nav is gated for a guest, exactly like the write actions ─────
  {
    let connectCalls = 0;
    const { api, store, sandbox } = run({
      boot: GUEST,
      innerWidth: 390,
      fetchImpl: feedFetch(() => Promise.resolve({
        ok: true, json: () => Promise.resolve({ success: true }) })),
    });
    // The shared connect card is a global hook the bundle calls; count the
    // calls exactly like the write-gate block above.
    sandbox.window.openGoodMarketGetStarted = function () { connectCalls++; };
    await tick();

    ok('a guest still gets the whole nav (reading is public)',
       store.cfBottomNav.children.length === 5);

    const items = store.cfBottomNav.children;
    const airtime = items.filter(n => /Buy Airtime/.test(n.textContent))[0];
    const search = items.filter(n => /Search/.test(n.textContent))[0];
    const claim = items.filter(n => /Claim Free G\$/.test(n.textContent))[0];
    const messages = items.filter(n => /Messages/.test(n.textContent))[0];
    const menu = items.filter(n => /Menu/.test(n.textContent))[0];

    messages.onclick({ preventDefault() {} });
    ok('a guest tapping Messages opens the connect card', connectCalls === 1,
       String(connectCalls));

    menu.onclick({ preventDefault() {} });
    ok('a guest may open the Menu without a login gate',
       store.cfSheetEarn.className.includes('open') && connectCalls === 1);
    api.menuOpenCommunityCards();
    ok('a guest may open Community Cards from the Menu without a login gate',
       store.cfCommunityCardsModal.hidden === false && connectCalls === 1);
    ok('opening Community Cards from Menu locks page scroll',
       sandbox.document.body.classList.contains('cf-modal-open'));
    api.closeCommunityCards();
    ok('closing Community Cards restores the page and modal state',
       store.cfCommunityCardsModal.hidden === true
         && !sandbox.document.body.classList.contains('cf-modal-open'));

    api.openWalletModal(store.cfMenuWallet);
    ok('a guest opening Wallet gets the connect card',
       connectCalls === 2, String(connectCalls));
    ok('a guest cannot fetch private wallet balances',
       store.cfCommunityWalletModal.hidden === true);

    // Buy Airtime is also signed-in only: the tap is swallowed and the card
    // shows, so the guest is never dropped on a page they cannot use — and the
    // store's modal must NOT open.
    let airtimeNavigated = false;
    api.closeAirtime();
    airtime.onclick({ preventDefault() { airtimeNavigated = true; } });
    ok('a guest tapping Buy Airtime opens the connect card',
       connectCalls === 3, String(connectCalls));
    ok('a guest tapping Buy Airtime is not navigated away', airtimeNavigated);
    ok('a guest tapping Buy Airtime never opens the store modal',
       store.cfAirtimeModal.hidden === true);
    ok('a guest tapping Buy Airtime never loads the store iframe',
       !store.cfAirtimeFrame.src, String(store.cfAirtimeFrame.src));

    // Claim Free G$ is a signed-in destination too: claiming needs a wallet, so
    // the guest gets the connect card instead of a dead trip to /wallet.
    let claimNavigated = false;
    claim.onclick({ preventDefault() { claimNavigated = true; } });
    ok('a guest tapping Claim Free G$ opens the connect card',
       connectCalls === 4, String(connectCalls));
    ok('a guest tapping Claim Free G$ is not navigated away', claimNavigated);
    ok('a guest tapping Claim Free G$ never opens the claim modal',
       !store.claimModal.className.includes('open'));
    ok('a guest tapping Claim Free G$ never opens the shared claim modal',
       !store.claimModal.className.includes('open'), String(store.claimModal.className));

    // Reading stays public: Search is NOT gated for a guest.
    api.closeSearch();
    search.onclick({ preventDefault() {} });
    ok('a guest may open the member search', store.cfSearchBar.hidden === false);
    ok('the read-only Search tap raised no connect card', connectCalls === 4,
       String(connectCalls));
  }

  // ── 13b-2. Desktop Menu -> Community Cards hosts the G$ card ──────────────
  // On desktop the Community G$ card is RELOCATED into the dialog (reachable
  // through Menu -> Community Cards) while "Suggested for you" stays in the
  // side column. The relocation is a move, not a clone: one element per id.
  {
    const { api, store, document: doc } = run({
      boot: MEMBER,
      innerWidth: 1280,
      fetchImpl: feedFetch(() => Promise.resolve({
        ok: true, json: () => Promise.resolve({ success: true }) })),
    });
    await tick();

    const content = store.cfCommunityCardsContent;
    ok('desktop moves the G$ card into the Community Cards dialog',
       store.cfEarn.parentNode === content);
    ok('desktop keeps "Suggested for you" in the side column (not the dialog)',
       store.cfSuggested.parentNode !== content);
    ok('desktop does not move the vault card into the dialog',
       !store.cfVaultDonationCard || store.cfVaultDonationCard.parentNode !== content);

    // The dialog starts closed, so the relocated card is not on screen until the
    // Menu entry opens it.
    ok('the Community Cards dialog starts closed on desktop',
       store.cfCommunityCardsModal.hidden === true);
    api.menuOpenCommunityCards();
    ok('desktop Menu -> Community Cards opens the dialog',
       store.cfCommunityCardsModal.hidden === false);
    ok('the opened dialog still holds the G$ card',
       store.cfEarn.parentNode === content);
    ok('opening the desktop dialog locks page scroll',
       doc.body.classList.contains('cf-modal-open'));
    ok('the desktop dialog describes the G$ card, not suggested members',
       /Community G\$ balance/.test(store.cfCommunityCardsDescription.textContent)
         && !/suggested members/.test(store.cfCommunityCardsDescription.textContent),
       store.cfCommunityCardsDescription.textContent);
    api.closeCommunityCards();
    ok('closing the desktop dialog restores the page',
       store.cfCommunityCardsModal.hidden === true
         && !doc.body.classList.contains('cf-modal-open'));
    // Closing the dialog on desktop must NOT return the card to the side column.
    ok('the G$ card stays out of the side column after closing on desktop',
       store.cfEarn.parentNode === content);
  }

  // ── 13c. Member search: min length, results, and text-not-markup ─────────
  {
    const seen = [];
    const { api, store } = run({
      boot: MEMBER,
      fetchImpl: feedFetch(function (url) {
        seen.push(String(url));
        if (String(url).indexOf('/community/api/users/search') === 0) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({
              success: true,
              users: [{ username: 'betz' }, { username: 'betz_2' }],
            }),
          });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      }),
    });

    // A one-character term is refused before any request.
    store.cfSearchInput.value = 'b';
    api._internals.runSearch('b');
    await tick();
    ok('a one-character search makes no request',
       !seen.some(u => /users\/search/.test(u)), JSON.stringify(seen));
    ok('the short-term hint is shown',
       /at least 2/.test(store.cfSearchResults.textContent || ''),
       store.cfSearchResults.textContent);

    api._internals.runSearch('bet');
    await tick();
    ok('a valid term queries the search endpoint',
       seen.some(u => /users\/search\?q=bet/.test(u)), JSON.stringify(seen));
    const rows = store.cfSearchResults.children;
    ok('the results render one row per member', rows.length === 2,
       JSON.stringify(rows.length));
    ok('a result links to the public profile by USERNAME',
       rows[0].href === '/community/u/betz', rows[0].href);
    ok('a result shows the @handle, never a wallet',
       rows[0].textContent.indexOf('@betz') !== -1, rows[0].textContent);
    ok('no wallet address is rendered in the results',
       !/0x/i.test(store.cfSearchResults.textContent || ''));

    // An empty result set renders the "no members" hint, not a broken list.
    api._internals.renderSearchResults([]);
    ok('an empty result set explains itself',
       /No members found/.test(store.cfSearchResults.textContent || ''));

    // A username is written as TEXT: markup cannot be injected through it.
    api._internals.renderSearchResults([{ username: '<img src=x>' }]);
    const injected = store.cfSearchResults.querySelector('.cf-search-name');
    ok('a username is rendered as TEXT, never markup',
       injected.textContent === '@<img src=x>', JSON.stringify(injected.textContent));
  }

  // ── 13d. Buy Airtime opens the Reloadly store IN the feed, not a page load ─
  {
    const { api, store, sandbox } = run({ boot: MEMBER });
    const items = store.cfBottomNav.children;
    const airtime = items.filter(n => /Buy Airtime/.test(n.textContent))[0];

    ok('the store modal starts hidden', store.cfAirtimeModal.hidden === true);
    ok('the store iframe ships with no src (lazy load)',
       !store.cfAirtimeFrame.src, String(store.cfAirtimeFrame.src));

    // A member tap opens the modal and loads the store ONCE.
    let navigated = false;
    airtime.onclick({ preventDefault() { navigated = true; } });
    ok('a member tapping Buy Airtime is not navigated away', navigated);
    ok('a member tapping Buy Airtime opens the store modal',
       store.cfAirtimeModal.hidden === false);
    ok('the store iframe loads the embedded top-up URL',
       store.cfAirtimeFrame.src === '/reloadly/?embed=1#topup',
       String(store.cfAirtimeFrame.src));
    ok('opening the store locks the page scroll',
       sandbox.document.body.classList.contains('cf-modal-open'));

    // A second open must not reload the store (the src is set once).
    store.cfAirtimeFrame.src = 'sentinel';
    api.closeAirtime();
    ok('closing the store hides the modal', store.cfAirtimeModal.hidden === true);
    ok('closing the store unlocks the page scroll',
       !sandbox.document.body.classList.contains('cf-modal-open'));
    airtime.onclick({ preventDefault() {} });
    ok('reopening the store does not reload the iframe',
       store.cfAirtimeFrame.src === 'sentinel', String(store.cfAirtimeFrame.src));
    api.closeAirtime();

    // The href is still a real link, so a no-JS/middle-click still reaches the
    // standalone store page.
    ok('the Buy Airtime item is still an anchor',
       airtime.tagName === 'A' && airtime.href === '/reloadly/?embed=1#topup',
       airtime.tagName + ' ' + airtime.href);

    const childReplies = [];
    const childWindow = { postMessage: (message, origin) => childReplies.push({ message, origin }) };
    store.cfAirtimeFrame.contentWindow = childWindow;
    let verificationOpens = 0;
    let modalWasClosedBeforeVerification = false;
    sandbox.openClaimOrVerify = () => {
      verificationOpens++;
      modalWasClosedBeforeVerification = store.cfAirtimeModal.hidden === true;
    };
    const verificationRequest = {
      channel: 'gm-reloadly-embed', version: 1,
      type: 'request-community-verification', requestId: 'airtime-fv-123-abc'
    };
    api.openAirtime();
    sandbox.dispatchWindowEvent('message', {
      origin: 'https://untrusted.example', source: childWindow, data: verificationRequest,
    });
    sandbox.dispatchWindowEvent('message', {
      origin: sandbox.location.origin, source: { postMessage() {} }, data: verificationRequest,
    });
    sandbox.dispatchWindowEvent('message', {
      origin: sandbox.location.origin, source: childWindow,
      data: Object.assign({}, verificationRequest, { version: 2 }),
    });
    api.closeAirtime();
    sandbox.dispatchWindowEvent('message', {
      origin: sandbox.location.origin, source: childWindow, data: verificationRequest,
    });
    ok('invalid Airtime iframe messages are ignored',
       verificationOpens === 0 && store.cfAirtimeModal.hidden === true);

    api.openAirtime();
    sandbox._walletFvReason = undefined;
    sandbox.dispatchWindowEvent('message', {
      origin: sandbox.location.origin, source: childWindow, data: verificationRequest,
    });
    ok('a valid Airtime verification request opens the shared Community modal',
       verificationOpens === 1 && modalWasClosedBeforeVerification
         && sandbox._walletNeedsFV === true && sandbox._walletFvReason === 'not_verified'
         && store.cfAirtimeModal.hidden === true);
    ok('the parent acknowledges the handled request and stays on Community',
       childReplies.length === 1
         && childReplies[0].message.type === 'community-verification-ack'
         && childReplies[0].message.requestId === verificationRequest.requestId
         && !String(sandbox.location.href || '').includes('/wallet'));

    // Preserve a distinct re-verification state instead of rewriting it as first-time.
    api.openAirtime();
    sandbox._walletFvReason = 're_verification_needed';
    const reverifyRequest = Object.assign({}, verificationRequest, { requestId: 'airtime-fv-124-def' });
    sandbox.dispatchWindowEvent('message', {
      origin: sandbox.location.origin, source: childWindow, data: reverifyRequest,
    });
    ok('the handoff preserves the existing re-verification reason',
       verificationOpens === 2 && sandbox._walletFvReason === 're_verification_needed');
  }

  // ── 13e. Claim Free G$ opens the wallet claim sheet IN the feed ───────────
  {
    const { api, store, sandbox } = run({ boot: MEMBER });
    const items = store.cfBottomNav.children;
    const claim = items.filter(n => /Claim Free G\$/.test(n.textContent))[0];

    ok('the claim modal starts hidden', !store.claimModal.className.includes('open'));
    // A member tap opens the shared claim sheet directly in the feed — the
    // member is never navigated off the feed and no iframe is involved.
    let claimNavigated = false;
    claim.onclick({ preventDefault() { claimNavigated = true; } });
    ok('a member tapping Claim Free G$ is not navigated away', claimNavigated);
    ok('a member tapping Claim Free G$ opens the claim modal',
       store.claimModal.className.includes('open'));
    ok('the shared claim modal renders in the feed',
       store.claimModal.className.includes('open'));
    ok('opening the claim modal locks the page scroll',
       sandbox.document.body.classList.contains('cf-modal-open'));

    // A second open reuses the same direct-rendered modal.
    api.closeClaim();
    ok('closing the claim modal hides it', !store.claimModal.className.includes('open'));
    ok('closing the claim modal unlocks the page scroll',
       !sandbox.document.body.classList.contains('cf-modal-open'));
    claim.onclick({ preventDefault() {} });
    ok('reopening the claim modal reuses the shared modal',
       store.claimModal.className.includes('open'));
    api.closeClaim();

    // Escape closes it, like every other dialog on the page — drive the REAL
    // document keydown handler rather than calling closeClaim() directly.
    claim.onclick({ preventDefault() {} });
    ok('the claim modal is open before Escape', store.claimModal.className.includes('open'));
    sandbox.document.dispatch('keydown', { key: 'Escape' });
    ok('Escape closes the claim modal', !store.claimModal.className.includes('open'));

    // The href is still a real link for a no-JS/middle-click fallback.
    ok('the Claim Free G$ item is still an anchor',
       claim.tagName === 'A' && claim.href === '/wallet?claim=1',
       claim.tagName + ' ' + claim.href);
  }

  // ── 13f. Suggested for you fills the empty right column ──────────────────
  // Owner report: the grid reserved a 300-340px right track that was
  // essentially empty. Two things had to be true: the panel renders the most
  // ACTIVE members with Follow buttons, and when the whole side column is
  // empty the grid COLLAPSES to one column instead of leaving the gap.
  {
    const { api, store } = run({ boot: MEMBER });

    // A failed/empty response must leave the panel hidden AND collapse the grid
    // (the stub has no .cf-wrap, so syncLayoutColumns must not throw).
    api.renderSuggestedMembers([]);
    ok('an empty result hides the panel', store.cfSuggested.hidden === true);

    api.renderSuggestedMembers(null);
    ok('a failed read hides the panel', store.cfSuggested.hidden === true);

    api.renderSuggestedMembers([
      { username: 'alice', avatar_url: null, active_days: 21, window_days: 30 },
      { username: 'bob', avatar_url: null, active_days: 0, window_days: 30 },
    ]);
    ok('a successful result reveals the panel', store.cfSuggested.hidden === false);
    ok('every suggested member gets a row', store.cfSuggestedList.children.length === 2);

    const first = store.cfSuggestedList.children[0];
    ok('the row links to the public profile by username',
       first.textContent.includes('@alice'));
    const link = first.querySelectorAll('a')[0];
    ok('the profile link carries the username, never a wallet',
       !!link && link.href === '/community/u/alice' && !/0x/i.test(first.textContent));

    const follow = first.children.filter(n => n.className.indexOf('cf-follow') === 0)[0];
    ok('the row has a Follow button', !!follow && follow.textContent === 'Follow');
    ok('the follow button names the member for screen readers',
       follow.getAttribute('aria-label') === 'Follow @alice');
    ok('the follow button carries the username as its data attribute',
       follow.getAttribute('data-follow') === 'alice');

    // The meta line explains WHY they are suggested, and is honest about a
    // cold-start fallback row (no activity figure).
    ok('an active member shows their active days',
       first.textContent.includes('Active 21 of the last 30 days'),
       first.textContent);
    const second = store.cfSuggestedList.children[1];
    ok('a fallback member is labelled as new, not as active 0 days',
       second.textContent.includes('New member') && !second.textContent.includes('Active 0'),
       second.textContent);

    // The panel is a set of names: no wallet address may reach the DOM.
    ok('no wallet address is rendered in the panel',
       !/0x[0-9a-f]{4}/i.test(store.cfSuggestedList.textContent));

    // A stored `@` (a legacy row) must not render `@@name`.
    api.renderSuggestedMembers([{ username: '@carol', active_days: 5, window_days: 30 }]);
    ok('a stored @ is stripped so the row never shows @@',
       store.cfSuggestedList.children[0].textContent.includes('@carol')
         && !store.cfSuggestedList.children[0].textContent.includes('@@'));

    // Following from the panel uses the SHARED follow path (one implementation).
    const { api: api2, store: store2, sandbox: sandbox2 } = run({
      boot: MEMBER,
      fetchImpl: (url, options) => {
        if (String(url).indexOf('/follow') !== -1) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({ success: true, following: true }) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      },
    });
    api2.renderSuggestedMembers([{ username: 'alice', active_days: 9, window_days: 30 }]);
    const followBtn = store2.cfSuggestedList.children[0]
      .children.filter(n => n.className.indexOf('cf-follow') === 0)[0];
    followBtn.onclick();
    ok('tapping Follow flips the button optimistically',
       followBtn.textContent === 'Following');
    await tick();
    ok('a successful follow keeps the button in the Following state',
       followBtn.classList.contains('is-following'));
    ok('a follow from the panel does not navigate away',
       sandbox2.location.href === undefined);
  }

  // ── 13g. Pinned donor acknowledgments are collapsed by default ───────────
  // Owner request: the pinned donor posts should not push the real feed down;
  // they appear only after tapping "Show donors". The toggle moves the `hidden`
  // flag on the already-rendered body, so it never refetches.
  {
    const { api, store } = run({ boot: MEMBER });

    // Nothing pinned: the section stays hidden and the toggle is never shown.
    api._internals.setPinnedDonations([]);
    api._internals.renderPinnedDonations();
    ok('no pinned posts keeps the whole section hidden',
       store.cfPinnedDonations.hidden === true);

    // With pinned posts the section is revealed, but the CARDS stay collapsed.
    api._internals.setPinnedDonations([
      { id: 1, post_type: 'system_vault_donation', donor_name: 'Betz', amount_gd: '1000', is_pinned: true },
      { id: 2, post_type: 'system_support', donor_username: 'ana', author_username: 'betz', amount_gd: '5', is_pinned: true },
    ]);
    api._internals.renderPinnedDonations();
    ok('pinned posts reveal the section', store.cfPinnedDonations.hidden === false);
    ok('the cards are collapsed by default', store.cfPinnedDonationBody.hidden === true);
    ok('the cards were still rendered while collapsed',
       store.cfPinnedDonationFeed.children.length === 2);
    ok('the toggle reads "Show donors" while collapsed',
       store.cfPinnedDonationsToggle.textContent === 'Show donors');
    ok('the toggle reports a collapsed state to screen readers',
       store.cfPinnedDonationsToggle.getAttribute('aria-expanded') === 'false');

    // Tapping reveals the already-loaded cards without another request.
    api.togglePinnedDonations();
    ok('tapping the toggle reveals the cards',
       store.cfPinnedDonationBody.hidden === false);
    ok('the toggle now reads "Hide donors"',
       store.cfPinnedDonationsToggle.textContent === 'Hide donors');
    ok('the toggle reports an expanded state to screen readers',
       store.cfPinnedDonationsToggle.getAttribute('aria-expanded') === 'true');

    // Tapping again collapses them.
    api.togglePinnedDonations();
    ok('tapping again collapses the cards',
       store.cfPinnedDonationBody.hidden === true);
    ok('the toggle returns to "Show donors"',
       store.cfPinnedDonationsToggle.textContent === 'Show donors');

    // A re-render (a feed refresh) must not force the panel back open.
    api.togglePinnedDonations();
    api._internals.renderPinnedDonations();
    ok('a re-render keeps the member\'s expanded choice',
       store.cfPinnedDonationBody.hidden === false);

    // An empty list can never stay "expanded" with nothing to show.
    api._internals.setPinnedDonations([]);
    api._internals.renderPinnedDonations();
    ok('an empty list collapses the toggle state again',
       store.cfPinnedDonationBody.hidden === true
         && store.cfPinnedDonationsToggle.getAttribute('aria-expanded') === 'false');
  }

  // ── 14. Follow pill: another author gets one, your own post does not ──────
  {
    const { api } = run({ boot: MEMBER });
    const other = api._internals.buildCard(makePost({ username: 'pedro', is_mine: false }));
    const own = api._internals.buildCard(makePost({ username: 'betz', is_mine: true }));
    ok('another member\'s card carries a Follow button',
       !!other.querySelector('.cf-follow'));
    ok('the Follow button reads "Follow" when not following',
       other.querySelector('.cf-follow').textContent === 'Follow');
    ok('your own card carries no Follow button',
       !own.querySelector('.cf-follow'));

    const following = api._internals.buildCard(makePost({
      username: 'pedro', is_following: true,
    })).querySelector('.cf-follow');
    ok('a followed author shows "Following"',
       following.textContent === 'Following');
    ok('a followed author is aria-pressed',
       following.getAttribute('aria-pressed') === 'true');
  }

  // ── 14b. toggleFollow: POST then flip, rollback on failure ───────────────
  {
    const seen = [];
    const { api } = run({
      boot: MEMBER,
      fetchImpl: feedFetch(function (url, opts) {
        seen.push({ url: String(url), method: (opts && opts.method) || 'GET' });
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ success: true, following: true, follower_count: 3 }),
        });
      }),
    });
    const btn = api._internals.buildFollowButton('pedro', false);
    await api._internals.toggleFollow(btn, 'pedro');
    const call = seen.filter(c => /users\/pedro\/follow/.test(c.url))[0];
    ok('following POSTs to the username-scoped follow route',
       !!call && call.method === 'POST' && call.url === '/community/api/users/pedro/follow',
       JSON.stringify(call));
    ok('the button flips to "Following" after success',
       btn.textContent === 'Following' && btn.classList.contains('is-following'));
    ok('the request never carries a wallet address',
       !seen.some(c => /0x/i.test(c.url)), JSON.stringify(seen));

    // Unfollowing DELETEs.
    const seen2 = [];
    const r2 = run({
      boot: MEMBER,
      fetchImpl: feedFetch(function (url, opts) {
        seen2.push({ url: String(url), method: (opts && opts.method) || 'GET' });
        return Promise.resolve({
          ok: true, json: () => Promise.resolve({ success: true, following: false }),
        });
      }),
    });
    const btn2 = r2.api._internals.buildFollowButton('pedro', true);
    await r2.api._internals.toggleFollow(btn2, 'pedro');
    const call2 = seen2.filter(c => /users\/pedro\/follow/.test(c.url))[0];
    ok('unfollowing DELETEs the follow route',
       !!call2 && call2.method === 'DELETE', JSON.stringify(call2));
    ok('the button flips back to "Follow"',
       btn2.textContent === 'Follow' && !btn2.classList.contains('is-following'));
  }

  // ── 14c. A failed follow rolls the button back ───────────────────────────
  {
    const { api } = run({
      boot: MEMBER,
      fetchImpl: feedFetch(function () {
        return Promise.resolve({
          ok: false, json: () => Promise.resolve({ success: false, error: 'nope' }),
        });
      }),
    });
    const btn = api._internals.buildFollowButton('pedro', false);
    await api._internals.toggleFollow(btn, 'pedro');
    ok('a failed follow rolls back to "Follow"',
       btn.textContent === 'Follow' && !btn.classList.contains('is-following'),
       btn.textContent);
  }

  // ── 14d. A guest tap raises the connect card and issues no request ───────
  {
    const seen = [];
    let gated = 0;
    const { api, sandbox } = run({
      boot: GUEST,
      fetchImpl: feedFetch(function (url) { seen.push(String(url)); return Promise.resolve({ ok: true, json: () => Promise.resolve({}) }); }),
    });
    sandbox.openGoodMarketGetStarted = function () { gated++; };
    const btn = api._internals.buildFollowButton('pedro', false);
    await api._internals.toggleFollow(btn, 'pedro');
    ok('a guest following opens the connect card', gated === 1, String(gated));
    ok('a guest following posts no follow request',
       !seen.some(u => /follow/.test(u)), JSON.stringify(seen));
  }

  // ── 15. Video is Instagram-style: a bounded frame, not full screen ───────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      media: [{ type: 'video', url: 'https://media.example/v.mp4',
                poster: 'https://media.example/v.jpg', w: 1080, h: 1350 }],
    }));
    const frame = card.querySelector('.cf-video-frame');
    ok('a video renders inside a frame', !!frame);
    ok('the frame is a button (tap to expand)', frame && frame.tagName === 'BUTTON');
    ok('the frame takes its aspect ratio from the clip',
       frame && frame.style.aspectRatio === '1080 / 1350', frame ? frame.style.aspectRatio : 'n/a');
    const video = frame && frame.querySelector('video');
    ok('the video keeps its controls', video && video.controls === true);
    ok('the video still has the stored poster',
       video && video.poster === 'https://media.example/v.jpg');
  }

  // ── 15b. Tapping a video frame opens the lightbox with a <video> ─────────
  {
    const { api, store } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      media: [{ type: 'video', url: 'https://media.example/v.mp4' }],
    }));
    card.querySelector('.cf-video-frame').onclick();
    ok('the lightbox opens', store.cfLightbox.hidden === false);
    ok('the lightbox holds a playable video',
       !!store.cfLightboxBody.querySelector('video'));
  }

  // ── 15c. An unknown clip size falls back to the IG portrait ratio ────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({
      media: [{ type: 'video', url: 'https://media.example/v.mp4' }],
    }));
    const frame = card.querySelector('.cf-video-frame');
    ok('a clip with no reported size leaves the CSS default ratio',
       !frame.style.aspectRatio, JSON.stringify(frame.style.aspectRatio));
  }

  // ── 16. Instagram-style action row: heart · comment · share · report ─────
  // Tapping the heart toggles ❤️ (`love`); the picker handles other reactions.
  {
    const seen = [];
    const { api } = run({
      boot: MEMBER,
      fetchImpl: feedFetch(function (url, opts) {
        seen.push({ url: String(url), method: (opts && opts.method) || 'GET', body: opts && opts.body });
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            success: true, reaction_count: 5, my_reaction: 'love',
          }),
        });
      }),
    });

    const card = api._internals.buildCard(makePost({ id: 21, is_mine: true }));
    const row = card.querySelector('.cf-card-actions');
    const trigger = card.querySelector('.cf-reaction-trigger');
    ok('the action row renders', !!row);
    const labels = (row.children || []).map(n => n.textContent);
    ok('the row carries heart, Comment and Share',
       labels.some(l => /Comment/.test(l)) && labels.some(l => /Share/.test(l)),
       JSON.stringify(labels));
    ok('the heart has no text label (it is an icon only)',
       trigger.querySelector('.cf-act-icon').textContent === '🤍',
       JSON.stringify(trigger.querySelector('.cf-act-icon').textContent));
    ok('the heart starts un-liked', !trigger.classList.contains('is-liked'),
       trigger.className);
    ok('the heart carries an aria-pressed state',
       trigger.getAttribute('aria-pressed') === 'false');

    // Tap it: an optimistic love reaction goes out, and the heart fills in.
    trigger.onclick();
    await tick();
    ok('tapping the heart POSTs a love reaction',
       seen.some(s => s.method === 'POST' && /reaction/.test(s.url)
         && JSON.parse(s.body).reaction === 'love'),
       JSON.stringify(seen));
    const liked = api._internals.buildCard(makePost({ id: 21, is_mine: true, my_reaction: 'love' }));
    const likedTrigger = liked.querySelector('.cf-reaction-trigger');
    ok('a liked post fills the heart red',
       likedTrigger.className.includes('is-liked'), likedTrigger.className);
    ok('a liked heart reads ❤️',
       likedTrigger.querySelector('.cf-act-icon').textContent === '❤️',
       likedTrigger.querySelector('.cf-act-icon').textContent);
    ok('a liked heart is aria-pressed',
       likedTrigger.getAttribute('aria-pressed') === 'true');

    // Un-tap: the same heart REMOVES love (DELETE, never a null POST).
    const before = seen.length;
    likedTrigger.onclick();
    await tick();
    const after = seen.slice(before);
    ok('un-liking DELETEs the reaction',
       after.some(s => s.method === 'DELETE'), JSON.stringify(after));
    ok('un-liking never POSTs a null reaction',
       !after.some(s => s.method === 'POST'), JSON.stringify(after));
  }

  // ── 16a. Press-and-hold, keyboard access, and picker dismissal ────────────
  {
    const pickerWrites = [];
    const { api, store, document } = run({
      boot: MEMBER,
      fetchImpl: (url, opts) => {
        const isReaction = /\/reaction$/.test(String(url));
        const key = isReaction && opts && opts.body
          ? JSON.parse(opts.body).reaction : null;
        if (isReaction && key) pickerWrites.push(key);
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            success: true, posts: [], has_more: false, reaction_count: 1,
            my_reaction: key, reaction_breakdown: key ? { [key]: 1 } : {},
          }),
        });
      },
    });
    const reactionPost = makePost({ id: 22 });
    const card = api._internals.buildCard(reactionPost);
    const trigger = card.querySelector('.cf-reaction-trigger');
    const picker = card.querySelector('.cf-reaction-picker');
    const keyEvent = { key: 'ArrowDown', prevented: false,
      preventDefault() { this.prevented = true; } };
    trigger.dispatch('keydown', keyEvent);
    ok('ArrowDown opens the picker from the heart',
       !picker.hidden && trigger.getAttribute('aria-expanded') === 'true');
    ok('keyboard opening focuses the first reaction choice',
       !!picker.querySelector('.cf-chip').focused);
    const escape = { key: 'Escape', prevented: false,
      preventDefault() { this.prevented = true; } };
    document.dispatch('keydown', escape);
    ok('Escape closes the picker and returns focus to the heart',
       picker.hidden && trigger.getAttribute('aria-expanded') === 'false'
         && trigger.focused && escape.prevented);

    const quickPost = makePost({ id: 25 });
    const quickCard = api._internals.buildCard(quickPost);
    const quickTrigger = quickCard.querySelector('.cf-reaction-trigger');
    const quickPicker = quickCard.querySelector('.cf-reaction-picker');
    quickTrigger.dispatch('pointerdown', { pointerType: 'touch', clientX: 12, clientY: 8 });
    quickTrigger.dispatch('pointerup', {});
    await Promise.resolve();
    ok('a short press cancels the long-press picker', quickPicker.hidden);
    quickTrigger.onclick();
    await tick();
    ok('a short press still toggles the heart reaction', quickPost.my_reaction === 'love');

    trigger.dispatch('pointerdown', { pointerType: 'touch', clientX: 20, clientY: 10 });
    // The hold-to-open is a 400ms timer, so wait past it (a microtask tick is
    // not enough once the harness honours real delays).
    await new Promise(r => setTimeout(r, 450));
    ok('holding the heart opens the picker', !picker.hidden);
    trigger.dispatch('pointerup', {});
    trigger.onclick({ preventDefault() {} });
    ok('the synthetic click after a hold does not toggle a reaction',
       trigger.getAttribute('aria-pressed') === 'false');

    const haha = picker.querySelectorAll('.cf-chip').filter(
      chip => chip.getAttribute('aria-label') === 'React with haha')[0];
    haha.onclick();
    await tick();
    ok('choosing a picker option closes the picker', picker.hidden);
    ok('the selected emoji is sent through the existing reaction API',
       pickerWrites.includes('haha') && reactionPost.my_reaction === 'haha',
       JSON.stringify({ pickerWrites, my_reaction: reactionPost.my_reaction }));

    const outsideCard = api._internals.buildCard(makePost({ id: 23 }));
    const outsideTrigger = outsideCard.querySelector('.cf-reaction-trigger');
    const outsidePicker = outsideCard.querySelector('.cf-reaction-picker');
    outsideTrigger.oncontextmenu({ preventDefault() {} });
    document.dispatch('pointerdown', { target: store.cfFeed });
    ok('a pointer press outside the picker dismisses it', outsidePicker.hidden);
  }

  // ── 16b. Scroll gesture cancels the hold instead of reacting ──────────────
  {
    const { api } = run({ boot: MEMBER });
    const card = api._internals.buildCard(makePost({ id: 24 }));
    const trigger = card.querySelector('.cf-reaction-trigger');
    const picker = card.querySelector('.cf-reaction-picker');
    trigger.dispatch('pointerdown', { pointerType: 'touch', clientX: 20, clientY: 10 });
    trigger.dispatch('pointermove', { clientX: 20, clientY: 40 });
    await Promise.resolve();
    ok('moving to scroll cancels the picker hold', picker.hidden);
    trigger.dispatch('pointerup', {});
    let prevented = false;
    trigger.onclick({ preventDefault() { prevented = true; } });
    ok('a cancelled scroll gesture does not fire a heart reaction', prevented);

    trigger.dispatch('pointerdown', { pointerType: 'mouse', button: 0, clientX: 5, clientY: 5 });
    trigger.dispatch('pointerleave', {});
    await Promise.resolve();
    ok('leaving the heart cancels a pending hold', picker.hidden);
  }

  // ── 16c. The report control stays in the row for another member ──────────
  {
    const { api } = run({ boot: MEMBER });
    const mine = api._internals.buildCard(makePost({ is_mine: true }));
    ok('your own post has no Report in the action row',
       !/Report/.test(mine.querySelector('.cf-card-actions').textContent));
    const theirs = api._internals.buildCard(makePost({ is_mine: false }));
    ok('another member\'s post keeps a Report control',
       /Report/.test(theirs.querySelector('.cf-card-actions').textContent),
       theirs.querySelector('.cf-card-actions').textContent);
  }

  // ── 17. Double-tap the photo to like (with the heart burst) ──────────────
  {
    const seen = [];
    const { api, store } = run({
      boot: MEMBER,
      fetchImpl: feedFetch(function (url, opts) {
        seen.push({ url: String(url), method: (opts && opts.method) || 'GET' });
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ success: true, reaction_count: 1, my_reaction: 'like' }),
        });
      }),
    });
    const card = api._internals.buildCard(makePost({
      id: 31, media: [{ type: 'image', url: 'https://media.example/a.jpg' }],
    }));
    const img = card.querySelector('.cf-media').children[0];

    // A single tap opens the lightbox (after the double-tap window closes).
    img.onclick();
    await new Promise(r => setTimeout(r, 350));
    ok('a single tap opens the lightbox',
       store.cfLightbox.hidden === false, String(store.cfLightbox.hidden));
    api.closeLightbox();

    // Two taps inside the window: the lightbox must NOT open, and the post is
    // liked instead. Reset the tap state so the previous tap cannot pair up.
    api._internals.onMediaTap(makePost({ id: 31, my_reaction: null }),
      card.querySelector('.cf-media'), 'https://media.example/a.jpg', 'image', function () {});
    const burstCard = api._internals.buildCard(makePost({
      id: 32, media: [{ type: 'image', url: 'https://media.example/b.jpg' }],
    }));
    const burstImg = burstCard.querySelector('.cf-media').children[0];
    burstImg.onclick();
    burstImg.onclick();
    await tick();
    ok('a double tap likes the post',
       seen.some(s => s.method === 'POST' && /reaction/.test(s.url)),
       JSON.stringify(seen));
    const burst = burstCard.querySelector('.cf-media').children
      .filter(n => (n.className || '').split(/\s+/).includes('cf-burst'))[0];
    ok('a double tap leaves a heart burst over the photo', !!burst, 'no .cf-burst');
    ok('the burst is decorative (aria-hidden)',
       !!burst && burst.getAttribute('aria-hidden') === 'true');
    ok('a double tap does NOT open the lightbox',
       store.cfLightbox.hidden === true, String(store.cfLightbox.hidden));

    // `dblclick` is wired too: a mouse never fires the two synthetic taps.
    const mouseCard = api._internals.buildCard(makePost({
      id: 33, media: [{ type: 'image', url: 'https://media.example/c.jpg' }],
    }));
    ok('the media carries a dblclick handler for mouse users',
       typeof mouseCard.querySelector('.cf-media').children[0].ondblclick === 'function');
  }

  // ── 18. Instagram's media box: the crop box comes from the real size ─────
  {
    const { api } = run({ boot: MEMBER });
    const sized = api._internals.buildCard(makePost({
      media: [{ type: 'image', url: 'https://media.example/a.jpg', w: 1080, h: 1350 }],
    }));
    const sizedImg = sized.querySelector('.cf-media').children[0];
    ok('a sized image gets an explicit aspect box',
       sizedImg.style.aspectRatio === '1080 / 1350', sizedImg.style.aspectRatio);
    ok('a sized image is not marked raw (so it crops like IG)',
       sizedImg.className !== 'cf-media-raw', sizedImg.className);

    // No dimensions reported: we must NOT guess a box, because a guessed crop
    // cuts a face off. Mark it `raw` so the CSS shows the whole photo.
    const unsized = api._internals.buildCard(makePost({
      media: [{ type: 'image', url: 'https://media.example/d.jpg' }],
    }));
    const unsizedImg = unsized.querySelector('.cf-media').children[0];
    ok('an unsized image is marked raw instead of being cropped',
       unsizedImg.className.includes('cf-media-raw'), unsizedImg.className);
    ok('an unsized image sets no aspect ratio',
       !unsizedImg.style.aspectRatio, String(unsizedImg.style.aspectRatio));
    ok('a single photo keeps the existing media layout',
       !unsized.querySelector('.cf-media').className.includes('cf-media-grid'));

    const four = api._internals.buildCard(makePost({ media: [
      { type: 'image', url: 'https://media.example/1.jpg', w: 1200, h: 800 },
      { type: 'image', url: 'https://media.example/2.jpg', w: 800, h: 1200 },
      { type: 'image', url: 'https://media.example/3.jpg' },
      { type: 'image', url: 'https://media.example/4.jpg' },
    ] }));
    const gallery = four.querySelector('.cf-media');
    ok('four photos use the compact 2x2 grid',
       gallery.className.includes('cf-media-grid-4'), gallery.className);
    ok('the four grid tiles keep upload order',
       gallery.children.slice(0, 4).map(n => n.src).join('|')
         === 'https://media.example/1.jpg|https://media.example/2.jpg|https://media.example/3.jpg|https://media.example/4.jpg');
    ok('grid thumbnails ignore source aspect ratios',
       !gallery.children[0].style.aspectRatio && !gallery.children[0].className.includes('cf-media-raw'));
    ok('a grid thumbnail retains its tap-to-view handler',
       typeof gallery.children[0].onclick === 'function');

    const two = api._internals.buildCard(makePost({ media: [
      { type: 'image', url: 'https://media.example/a.jpg' },
      { type: 'image', url: 'https://media.example/b.jpg' },
    ] }));
    ok('two photos use one compact row',
       two.querySelector('.cf-media').className.includes('cf-media-grid-2'));
    const three = api._internals.buildCard(makePost({ media: [
      { type: 'image', url: 'https://media.example/a.jpg' },
      { type: 'image', url: 'https://media.example/b.jpg' },
      { type: 'image', url: 'https://media.example/c.jpg' },
    ] }));
    ok('three photos use the split-height mosaic',
       three.querySelector('.cf-media').className.includes('cf-media-grid-3'));
    const mixed = api._internals.buildCard(makePost({ media: [
      { type: 'image', url: 'https://media.example/a.jpg' },
      { type: 'video', url: 'https://media.example/v.mp4' },
    ] }));
    ok('mixed photos and video keep the existing media layout',
       !mixed.querySelector('.cf-media').className.includes('cf-media-grid'));

    // A video's ratio lands on its own frame, which is what the full-bleed
    // phone layout and the lightbox both read.
    const video = api._internals.buildCard(makePost({
      media: [{ type: 'video', url: 'https://media.example/v.mp4', w: 720, h: 1280 }],
    }));
    const frame = video.querySelector('.cf-video-frame');
    ok('a sized video carries its own ratio',
       frame.style.aspectRatio === '720 / 1280', String(frame.style.aspectRatio));
  }

  // ── 19. The "Recent posters" rail ────────────────────────────────────────
  {
    const { api, store } = run({ boot: MEMBER });
    api._internals.setFeed([
      makePost({ id: 1, username: 'ana' }),
      makePost({ id: 2, username: 'betz', is_mine: true }),
      makePost({ id: 3, username: 'ana' }),        // duplicate author
      makePost({ id: 4, username: 'ces' }),
      makePost({ id: 5, username: '@legacy' }),    // stored @ must not double up
    ]);
    api._internals.renderFeed();

    const items = store.cfRail.children;
    ok('the rail renders one row per DISTINCT author', items.length === 4,
       JSON.stringify(items.length));
    ok('the rail is revealed when there are authors', store.cfRail.hidden === false);

    const names = items.map(n => n.querySelector('.cf-rail-name').textContent);
    ok('your own row leads the rail', names[0] === 'You', JSON.stringify(names));
    ok('a stored @ is not doubled in the rail label',
       names.includes('legacy') && !names.includes('@@legacy'), JSON.stringify(names));
    ok('another author links to their public profile by USERNAME',
       items[1].href === '/community/u/ana', String(items[1].href));
    ok('no wallet address reaches the rail',
       !/0x/i.test(store.cfRail.textContent || ''), store.cfRail.textContent);

    // Your own row is the composer entry point, not a profile link.
    ok('your own row is a button, not a link',
       items[0].tagName === 'BUTTON' && !items[0].href, items[0].tagName);
    ok('your own row is marked is-self', items[0].className.includes('is-self'));

    // The ring is present on every row (the Instagram look).
    ok('every row carries a gradient ring',
       items.every(n => !!n.querySelector('.cf-rail-ring')));

    // An empty feed hides the rail instead of leaving a bare strip.
    api._internals.setFeed([]);
    api._internals.renderFeed();
    ok('an empty feed hides the rail', store.cfRail.hidden === true);

    // The rail caps its rows so a busy feed cannot grow an endless strip.
    api._internals.setFeed(Array.from({ length: 30 }, (_, i) =>
      makePost({ id: 100 + i, username: 'member' + i })));
    api._internals.renderFeed();
    ok('the rail is capped', store.cfRail.children.length === 12,
       JSON.stringify(store.cfRail.children.length));
  }

  // ── 19b. A guest gets the rail too (reading is public) ──────────────────
  {
    const { api, store } = run({ boot: GUEST });
    api._internals.setFeed([makePost({ id: 1, username: 'ana', is_mine: false })]);
    api._internals.renderFeed();
    ok('a guest sees the rail', store.cfRail.children.length === 1,
       JSON.stringify(store.cfRail.children.length));
    ok('a guest row is a plain profile link (nothing behind it is gated)',
       store.cfRail.children[0].href === '/community/u/ana',
       String(store.cfRail.children[0].href));
  }

  // ── 20. The G$ Earned card ───────────────────────────────────────────────
  // The balance is the server's SUM; the card must render it, keep the status
  // chip, and NEVER offer a withdraw control in v1.
  //
  // Signed-in members see the balance by default, while guests never see
  // personal figures. Rate details are collapsed behind an accessible toggle.
  // The card still states that community earnings are not withdrawable. See
  // docs/COMMUNITY_REWARDS_CARD_REVAMP_PROPOSAL.md.
  {
    const { api, store } = run({ boot: MEMBER });
    api._internals.renderRewards({
      enabled: true, is_guest: false, balance_gd: 47,
      min_payout_gd: 500, progress: 9.4, status: 'pending', can_withdraw: false,
      show_balance: true,
      rates: { comment_in: 1.5, comment_out: 2.5, reaction_out: 0.5, reaction_in: 0.1, daily_learn_per_correct: 1.5 },
    });
    ok('the earn card is revealed when enabled', store.cfEarn.hidden === false);
    ok('the earn card shows the server balance',
       store.cfEarnAmount.textContent === '47', String(store.cfEarnAmount.textContent));
    ok('the earn card keeps a status chip',
       store.cfEarnChip.textContent === 'Pending to withdraw',
       String(store.cfEarnChip.textContent));
    ok('the earn card names the threshold',
       /500 G\$/.test(store.cfEarnGoal.textContent),
       String(store.cfEarnGoal.textContent));

    // The server defaults show_balance to true for signed-in members. The
    // amount, goal, progress and note still come from the server payload.
    ok('the balance block is visible by default for a member',
       store.cfEarnBalance.hidden === false,
       String(store.cfEarnBalance.hidden));
    ok('the lead line is always visible',
       store.cfEarnLead.hidden === false && store.cfEarnLead.textContent.length > 0,
       String(store.cfEarnLead.textContent));
    ok('the lead line says what you can earn',
       /earn g\$/i.test(store.cfEarnLead.textContent),
       String(store.cfEarnLead.textContent));

    // The rates are rendered from the server payload and collapsed until
    // requested; a wrong value is a support ticket.
    ok('rate details start collapsed', store.cfEarnRates.hidden === true);
    ok('the accessible rate toggle is available',
       store.cfEarnRatesToggle.hidden === false
         && store.cfEarnRatesToggle.getAttribute('aria-expanded') === 'false');
    store.cfEarnRatesToggle.onclick();
    ok('the rate toggle expands the list and updates its state',
       store.cfEarnRates.hidden === false
         && store.cfEarnRatesToggle.getAttribute('aria-expanded') === 'true');
    const rateRows = store.cfEarnRates.children.map(li => li.textContent);
    ok('the earn card lists all five rates', rateRows.length === 5,
       JSON.stringify(rateRows));
    ok('the commenter rate is 1.5',
       rateRows.some(t => /Comment on a post/.test(t) && /\+1\.5 G\$/.test(t)),
       JSON.stringify(rateRows));
    ok('the post-owner comment rate is 2.5',
       rateRows.some(t => /comments on your post/.test(t) && /\+2\.5 G\$/.test(t)),
       JSON.stringify(rateRows));
    ok('the received-reaction rate is 0.5',
       rateRows.some(t => /reacts to your post/.test(t) && /\+0\.5 G\$/.test(t)),
       JSON.stringify(rateRows));
    ok('the given-reaction rate is 0.1',
       rateRows.some(t => /You react to a post/.test(t) && /\+0\.1 G\$/.test(t)),
       JSON.stringify(rateRows));
    ok('the Daily Learn rate is per correct answer',
       rateRows.some(t => /Daily Learn \(per correct answer\)/.test(t) && /\+1\.5 G\$/.test(t)),
       JSON.stringify(rateRows));
    ok('the rates list remains revealed after opening',
       store.cfEarnRates.hidden === false);
    ok('the progress bar reflects the server progress',
       store.cfEarnBarFill.style.width === '9.4%',
       String(store.cfEarnBarFill.style.width));
    ok('the progress bar is announced to assistive tech',
       store.cfEarnBar.attributes['aria-valuenow'] === '9',
       String(store.cfEarnBar.attributes['aria-valuenow']));
    ok('the pending state does not offer a withdrawal action',
       !/ready to withdraw/i.test(store.cfEarnNote.textContent),
       String(store.cfEarnNote.textContent));
    ok('a member is told withdrawals require the threshold and an enabled vault',
       /full balance becomes withdrawable after the minimum is reached and the vault is enabled/i.test(store.cfEarnNote.textContent),
       String(store.cfEarnNote.textContent));
  }

  // ── 20a-ii. The explicit flag can hide the member balance ─────────────────
  {
    const { api, store } = run({ boot: MEMBER });
    api._internals.renderRewards({
      enabled: true, is_guest: false, balance_gd: 47,
      min_payout_gd: 500, progress: 9.4, status: 'pending', can_withdraw: false,
      show_balance: false,
      rates: { comment_in: 1.5, comment_out: 2.5, reaction_out: 0.5, reaction_in: 0.1, daily_learn_per_correct: 1.5 },
    });
    ok('an explicit opt-out hides the balance block',
       store.cfEarnBalance.hidden === true,
       String(store.cfEarnBalance.hidden));

    // Only the exact boolean `true` counts; a truthy string does not reveal it.
    api._internals.renderRewards({
      enabled: true, is_guest: false, balance_gd: 47,
      min_payout_gd: 500, progress: 9.4, status: 'pending', can_withdraw: false,
      show_balance: 'yes',
    });
    ok('a non-boolean show_balance does not reveal the balance',
       store.cfEarnBalance.hidden === true,
       String(store.cfEarnBalance.hidden));
  }

  // ── 20b. Reaching the threshold does not imply withdrawals are live ──────
  {
    const { api, store } = run({ boot: MEMBER });
    api._internals.renderRewards({
      enabled: true, is_guest: false, balance_gd: 512,
      min_payout_gd: 500, progress: 100, status: 'ready', can_withdraw: false,
    });
    ok('a balance at the threshold remains pending while withdrawals are unavailable',
       store.cfEarnChip.textContent === 'Pending to withdraw',
       String(store.cfEarnChip.textContent));
    ok('a reached goal says so',
       /reached/i.test(store.cfEarnGoal.textContent),
       String(store.cfEarnGoal.textContent));
    ok('the bar is capped at 100%',
       store.cfEarnBarFill.style.width === '100%',
       String(store.cfEarnBarFill.style.width));
  }

  // ── 20b-ii. An unverified wallet is told why it earns nothing ────────────
  //
  // The server sends eligible:false / status:'unverified' and a zero balance.
  // The card must NOT show a silent zero — that reads as "rewards are broken"
  // rather than "you have not verified yet" — and it must not imply the wallet
  // is locked out, because the feed stays fully usable.
  {
    const { api, store } = run({ boot: MEMBER });
    api._internals.renderRewards({
      enabled: true, is_guest: false, balance_gd: 0,
      min_payout_gd: 500, progress: 0, status: 'unverified',
      eligible: false, can_withdraw: false,
    });
    ok('an unverified wallet gets its own chip, not a bare Pending',
       store.cfEarnChip.textContent === 'Verify to earn',
       String(store.cfEarnChip.textContent));
    ok('the chip carries the unverified state for styling',
       store.cfEarnChip.attributes['data-state'] === 'unverified',
       String(store.cfEarnChip.attributes['data-state']));
    ok('the goal line says the balance is zero because of verification',
       /verify/i.test(store.cfEarnGoal.textContent),
       String(store.cfEarnGoal.textContent));
    ok('the note says the feed still works, so it does not read as a lockout',
       /still post/i.test(store.cfEarnNote.textContent),
       String(store.cfEarnNote.textContent));
    ok('the unverified-wallet note does not promise withdrawal access',
       !/withdraw/i.test(store.cfEarnNote.textContent),
       String(store.cfEarnNote.textContent));
  }

  // ── 20c. A guest is invited to sign in, and a disabled feature hides ─────
  {
    const { api, store } = run({ boot: GUEST });
    api._internals.renderRewards({
      enabled: true, is_guest: true, balance_gd: 0,
      min_payout_gd: 500, progress: 0, status: 'guest', can_withdraw: false,
      show_balance: true,
    });
    ok('a guest is told to sign in to earn',
       /sign in/i.test(store.cfEarnGoal.textContent),
       String(store.cfEarnGoal.textContent));
    // The lead line carries the sign-in instruction; even a true server flag
    // must never reveal a guest's personal balance.
    ok('a guest sees the sign-in instruction on the always-visible lead line',
       /sign in/i.test(store.cfEarnLead.textContent),
       String(store.cfEarnLead.textContent));
    ok('a guest does not get the personal balance block',
       store.cfEarnBalance.hidden === true,
       String(store.cfEarnBalance.hidden));
    ok('a guest is told withdrawals require the threshold and an enabled vault',
       /full balance becomes withdrawable after the minimum is reached and the vault is enabled/i.test(store.cfEarnNote.textContent),
       String(store.cfEarnNote.textContent));

    api._internals.renderRewards({ enabled: false });
    ok('a disabled rewards feature hides the card', store.cfEarn.hidden === true);
  }

  // ── 20d. No wallet address ever reaches the earn card ────────────────────
  {
    const { api, store } = run({ boot: MEMBER });
    api._internals.renderRewards({
      enabled: true, is_guest: false, balance_gd: 47, min_payout_gd: 500,
      progress: 9.4, status: 'pending', can_withdraw: false,
      rates: { comment_in: 1.5, comment_out: 2.5, reaction_out: 0.5, reaction_in: 0.1, daily_learn_per_correct: 1.5 },
    });
    const text = store.cfEarn.textContent;
    ok('no wallet address reaches the earn card',
       !/0x/i.test(text), text);
  }

  // ── 20e. A refresh during an in-flight fetch is not dropped ──────────────
  // The bug this pins: a react fires loadRewards() while the boot fetch is
  // still open, and a naive `if (busy) return` would leave the card showing
  // the PRE-react balance — which reads as "my reaction did not count".
  {
    const gate = [];
    const balances = [10, 11];
    let call = 0;
    const { api, store } = run({
      boot: MEMBER,
      fetchImpl: feedFetch(function (url) {
        if (String(url).indexOf('/community/api/rewards') === 0) {
          const idx = call++;
          return new Promise(resolve => {
            gate.push(() => resolve({
              ok: true,
              json: () => Promise.resolve({
                success: true, enabled: true, is_guest: false,
                balance_gd: balances[idx] !== undefined ? balances[idx] : 11,
                min_payout_gd: 500, progress: 2.0, status: 'pending',
                can_withdraw: false,
                rates: { comment_in: 1.5, comment_out: 2.5, reaction_out: 0.5, reaction_in: 0.1, daily_learn_per_correct: 1.5 },
              }),
            }));
          });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      }),
    });

    // Drive the promise chain without depending on its shape: `loadRewards`
    // chains the follow-up fetch into the first call's promise, so awaiting the
    // first call before releasing the second response would deadlock.
    const tick = () => new Promise(r => setTimeout(r, 0));

    const first = api._internals.loadRewards();
    // The second call arrives while the first is still open.
    api._internals.loadRewards();
    await tick();
    ok('only one rewards fetch is in flight at a time', gate.length === 1,
       String(gate.length));

    gate[0]();                      // release the first response
    await tick();
    ok('the coalesced refresh issues a second fetch', gate.length === 2,
       String(gate.length));
    gate[1]();                      // release the refresh
    await first;
    await tick();
    ok('the card ends on the freshest balance',
       store.cfEarnAmount.textContent === '11',
       String(store.cfEarnAmount.textContent));
  }

  // ── N. Admin rate editor: the static contract ──────────────────────────────
  //
  // The editor's JS is inline in templates/admin_dashboard.html, so it cannot be
  // driven through the bundle's VM the way the feed is. What is checkable — and
  // what actually breaks — is the WIRING: that the labels cover every rate the
  // server can send, that the endpoint path is the one the blueprint serves, and
  // that the editor cannot be used to write a balance. Each of these was
  // mutation-checked by breaking the template and watching this fail.
  {
    const fs2 = require('fs');
    const tpl = fs2.readFileSync(
      path.join(__dirname, '..', 'templates', 'admin_dashboard.html'), 'utf8');
    const rw = fs2.readFileSync(
      path.join(__dirname, '..', 'community_feed', 'rewards.py'), 'utf8');
    const settings = fs2.readFileSync(
      path.join(__dirname, '..', 'community_feed', 'reward_settings.py'), 'utf8');

    // The server's rate vocabulary. The template must not invent a name.
    const names = /RATE_NAMES\s*=\s*\(([^)]*)\)/.exec(settings);
    const serverNames = names
      ? names[1].split(',').map(x => x.trim().replace(/["']/g, '')).filter(Boolean)
      : [];
    ok('the server declares the four rates', serverNames.length === 4,
       serverNames.join(','));

    const labelBlock = /_REWARD_RATE_LABELS\s*=\s*\{([^}]*)\}/.exec(tpl);
    const labelKeys = labelBlock
      ? (labelBlock[1].match(/([a-z_]+)\s*:/g) || [])
          .map(x => x.replace(/[:\s]/g, ''))
      : [];
    ok('the editor labels every server rate',
       serverNames.every(n => labelKeys.includes(n)),
       'server=' + serverNames.join(',') + ' labels=' + labelKeys.join(','));

    ok('the editor posts to the endpoint the blueprint serves',
       tpl.includes("'/community/api/admin/reward-rates'"));
    ok('the editor reads the rates from the server, never hard-coded',
       tpl.includes('fetch(') && tpl.includes('loadRewardRates'));

    // The reason is mandatory on both sides. Client-side is UX; the server
    // refusal is the control, and the test suite covers that separately.
    ok('the editor requires a reason before saving',
       /if\s*\(\s*!reason\s*\)/.test(tpl));
    ok('the server refuses a write without an actor and a reason',
       settings.includes('not actor.strip()') &&
       settings.includes('not reason.strip()'));

    // An atomic DB settings RPC validates the whole rate/cap combination;
    // silent rate clamping is deliberately no longer the live write policy.
    ok('the server validates settings through the atomic update RPC',
       settings.includes('community_reward_settings_update') &&
       settings.includes('def _validate_patch') &&
       settings.includes('db.rpc(RPC_UPDATE, args)'));

    // The whole point: this writes config, and there is still no balance column.
    ok('the rate write touches only the config table',
       settings.includes('RPC_UPDATE') &&
       !/update\([^)]*delta_gd/.test(settings) &&
       !/set_balance/.test(settings));

    // The card copy must not advertise a balance the admin can edit.
    ok('the admin copy still promises the balance cannot be edited',
       tpl.includes("there is no admin control to change anyone's balance"));

    // The visibility blurb must state the real rates, not the old flat 1 G$.
    ok('the admin blurb states the weighted rates',
       tpl.includes('<strong>2.5</strong> per comment received') &&
       tpl.includes('<strong>0.1</strong> per reaction given'));
  }

  // ── O. Messages button: opt-in is tap-triggered and never blocks Messages ──
  {
    const boot = Object.assign({}, MEMBER, {
      messagesEnabled: true,
      nav: [{ id: 'messages', icon: '💬', label: 'Messages', href: '/messages',
        action: 'messages', guestOk: false }],
    });
    const { api, store, sandbox } = run({ boot });
    sandbox.Notification = { permission: 'default' };
    sandbox.isSecureContext = true;
    sandbox.PushManager = function () {};
    sandbox.navigator.serviceWorker = {};
    let enableCalls = 0;
    sandbox.GMCommunityPush = {
      inspect: () => Promise.resolve({ state: 'needs-setup' }),
      enable: () => { enableCalls++; return Promise.resolve({ state: 'enabled' }); },
    };
    api._internals.renderNav();
    const messageButton = store.cfBottomNav.children.find(n => n.getAttribute('data-nav') === 'messages');
    messageButton.onclick({ preventDefault() {} });
    ok('tapping Messages opens the in-app permission-choice dialog',
       store.cfMessagePushPrompt.hidden === false);
    ok('tapping Messages itself does not request browser permission', enableCalls === 0);
    await tick();
    ok('the Enable control becomes available after readiness passes',
       store.cfMessagePushEnable.disabled === false
         && store.cfMessagePushEnable.textContent === 'Enable notifications');
    api.enableMessageNotifications();
    ok('the explicit Enable tap starts push setup', enableCalls === 1);
    await tick();
    ok('successful setup proceeds to Messages', sandbox.location.href === '/messages', sandbox.location.href);

    const declined = run({ boot });
    declined.sandbox.GMCommunityPush = {
      inspect: () => Promise.resolve({ state: 'needs-setup' }),
      enable: () => { throw new Error('Not now must not attempt enable'); },
    };
    declined.api._internals.openMessages();
    await tick();
    declined.api.declineMessageNotifications();
    ok('Not now opens Messages without calling enable', declined.sandbox.location.href === '/messages');

    const alreadyGranted = run({ boot });
    alreadyGranted.sandbox.Notification = { permission: 'granted' };
    let inspectCalls = 0;
    alreadyGranted.sandbox.GMCommunityPush = {
      inspect: () => { inspectCalls++; return Promise.resolve({ state: 'enabled' }); },
    };
    alreadyGranted.api._internals.renderNav();
    const grantedButton = alreadyGranted.store.cfBottomNav.children.find(
      n => n.getAttribute('data-nav') === 'messages');
    grantedButton.onclick({ preventDefault() {} });
    ok('granted permission opens Messages immediately without the opt-in popup',
       alreadyGranted.sandbox.location.href === '/messages'
         && alreadyGranted.store.cfMessagePushPrompt.hidden === true);
    ok('the fast path does not re-run the push readiness check', inspectCalls === 0);

    const permissionBlocked = run({ boot });
    permissionBlocked.sandbox.Notification = { permission: 'denied' };
    permissionBlocked.sandbox.GMCommunityPush = {
      inspect: () => { throw new Error('Blocked permission must not reopen the prompt'); },
    };
    permissionBlocked.api._internals.openMessages();
    ok('blocked permission opens Messages directly for settings guidance',
       permissionBlocked.sandbox.location.href === '/messages'
         && permissionBlocked.store.cfMessagePushPrompt.hidden === true);
  }

  console.log('');
  if (failures) {
    console.error(failures + ' CHECK(S) FAILED');
    process.exit(1);
  }
  console.log('ALL COMMUNITY FEED HARNESS CHECKS PASSED');
})();
