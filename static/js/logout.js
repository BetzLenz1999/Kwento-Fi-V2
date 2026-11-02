/**
 * Shared logout — the ONE place the browser performs a logout.
 *
 * Loaded by every page that can render a logout control (wallet.html,
 * community.html, and the standalone pages). Before this existed each page
 * rendered a bare `<a href="/logout">`, so logout behaviour depended on which
 * bundles that page happened to load — e.g. /community never loaded
 * dapp-connect.js, so wallet-role dApp sessions survived a logout there.
 *
 * The flow, in order:
 *   1. Disconnect any live dApp (wallet-role) sessions, so each dApp is told
 *      the session ended instead of showing a stale "connected" state.
 *   2. POST /logout. The server clears the Flask session, expires the
 *      cookies, and returns `clear_keys` — the list of browser keys to purge.
 *      The server owns that list, so a new key is added in one place.
 *   3. Purge those keys, then navigate.
 *
 * The local keystore (`gm_local_keystore_v1`) is deliberately NOT in the list:
 * it is the user's PIN-encrypted wallet, not a login artifact. Deleting it
 * would destroy their wallet.
 *
 * Every step is best-effort and time-capped. A logout must always complete —
 * a dead relay or a failed fetch must never strand a user logged in.
 *
 * Exposes window.GMLogout with:
 *   perform(opts)        -> run the flow; opts.onDone(url) to navigate yourself
 *   purgeKeys(keys)      -> remove keys from localStorage + sessionStorage
 *   wire()               -> capture clicks on any a[href="/logout"] (idempotent)
 *   isBusy()             -> true while a logout is in flight
 */
(function (global) {
    "use strict";

    if (global.GMLogout) return;

    var FETCH_TIMEOUT_MS = 4000;   // POST /logout
    var DISCONNECT_CAP_MS = 1500;  // dApp session teardown
    var LOGOUT_HREF = "/logout";

    var _busy = false;

    function _log() {
        try {
            if (global.console && console.log) console.log.apply(console, arguments);
        } catch (_) {}
    }

    function _ls() {
        try { return global.localStorage; } catch (_) { return null; }
    }
    function _ss() {
        try { return global.sessionStorage; } catch (_) { return null; }
    }

    // Keys the server told us to clear, plus the ones we know are ours. The
    // local keystore is filtered out defensively even if a future server list
    // included it — losing a wallet is not recoverable, so this is a hard stop.
    var NEVER_CLEAR = ["gm_local_keystore_v1", "gm_local_passkey_v1"];

    // Mirror of routes.py LOGOUT_CLIENT_KEYS, used when the POST fails (offline,
    // blocked storage, a 500). Without this the identity keys survived exactly
    // when the network was down — the moment a user is most likely to hand the
    // device to someone else. A test asserts this list stays in sync with the
    // server's, so it cannot drift.
    var FALLBACK_KEYS = [
        "wc_session_topic",
        "wc_session_address",
        "wc_session_data",
        "wc_session_timestamp",
        "wc_session_chains",
        "gm_reown_appkit_walletconnect_address",
        "privy_wallet_address",
        "gm_pending_buy",
        "gdSelectedCurrency",
        "dismissedBroadcasts",
    ];

    function purgeKeys(keys) {
        var list = keys && keys.length ? keys : [];
        var removed = [];
        var ls = _ls(), ss = _ss();
        list.forEach(function (k) {
            if (!k || NEVER_CLEAR.indexOf(k) !== -1) return;
            try { if (ls) { ls.removeItem(k); } } catch (_) {}
            try { if (ss) { ss.removeItem(k); } } catch (_) {}
            removed.push(k);
        });
        // WalletConnect keys are also cleared through the bridge so its own
        // in-memory state is reset, not just the storage entries.
        try {
            if (global.GMWalletConnect && typeof global.GMWalletConnect.clearStoredSession === "function") {
                global.GMWalletConnect.clearStoredSession();
            }
        } catch (_) {}
        try {
            var appKit = global.GMReownAppKit;
            if (appKit && typeof appKit.disconnect === "function") {
                Promise.resolve(appKit.disconnect()).catch(function () {});
            } else if (typeof global.GM_LOAD_REOWN_APPKIT === "function") {
                global.GM_LOAD_REOWN_APPKIT().then(function (loaded) {
                    if (loaded && typeof loaded.disconnect === "function") return loaded.disconnect();
                }).catch(function () {});
            }
        } catch (_) {}
        return removed;
    }

    // Tell every connected dApp the session ended, then continue. Capped so a
    // dead relay cannot block the logout.
    function _disconnectDapps(done) {
        var finished = false;
        var go = function () { if (!finished) { finished = true; done(); } };
        try {
            var dc = global.GMDappConnect;
            if (!dc || typeof dc.sessions !== "function" || !dc.sessions().length) { go(); return; }
            try {
                if (typeof dc.revokeAll === "function") dc.revokeAll();
                else if (typeof dc._dropForeignSessions === "function") dc._dropForeignSessions();
            } catch (_) {}
        } catch (_) {}
        setTimeout(go, DISCONNECT_CAP_MS);
    }

    function _fetchWithTimeout(url, opts, ms) {
        var controller = null;
        try {
            if (global.AbortController) controller = new global.AbortController();
        } catch (_) {}
        var timer = setTimeout(function () {
            try { if (controller) controller.abort(); } catch (_) {}
        }, ms);
        var o = opts || {};
        if (controller) o.signal = controller.signal;
        var p = global.fetch(url, o);
        // Clear the timer as soon as the request settles, whichever way it goes.
        if (p && typeof p.then === "function") {
            p = p.then(function (r) { clearTimeout(timer); return r; },
                       function (e) { clearTimeout(timer); throw e; });
        }
        return p;
    }

    function perform(opts) {
        opts = opts || {};
        if (_busy) return Promise.resolve(false);
        _busy = true;

        var navigate = function (url) {
            _busy = false;
            if (typeof opts.onDone === "function") { opts.onDone(url); return; }
            try { global.location.href = url || LOGOUT_HREF; } catch (_) {}
        };

        _disconnectDapps(function () {
            var fallback = function () {
                // The server session must still be cleared even if the JSON
                // POST failed (offline, blocked, 500). The GET route is the
                // same implementation server-side, so it is a safe fallback.
                _log("[logout] POST failed — falling back to GET /logout");
                try {
                    purgeKeys(opts.keys && opts.keys.length ? opts.keys : FALLBACK_KEYS);
                    navigate(LOGOUT_HREF);
                } catch (_) { navigate(LOGOUT_HREF); }
            };

            var req;
            try {
                req = _fetchWithTimeout(LOGOUT_HREF, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
                    credentials: "same-origin",
                    body: "{}"
                }, FETCH_TIMEOUT_MS);
            } catch (_) { fallback(); return; }

            if (!req || typeof req.then !== "function") { fallback(); return; }

            req.then(function (res) {
                if (!res || !res.ok) throw new Error("logout POST " + (res ? res.status : "no response"));
                return res.json();
            }).then(function (data) {
                purgeKeys((data && data.clear_keys) || opts.keys || FALLBACK_KEYS);
                _log("[logout] session cleared");
                navigate((data && data.redirect) || LOGOUT_HREF);
            }).catch(function () { fallback(); });
        });

        return Promise.resolve(true);
    }

    // Capture clicks on any logout link so every page gets the full flow
    // without each template hand-wiring it. Capture phase so nothing can stop it.
    var _wired = false;
    function wire() {
        if (_wired || !global.document || !global.document.addEventListener) return;
        _wired = true;
        global.document.addEventListener("click", function (ev) {
            var node = ev.target;
            while (node && node.tagName !== "A") node = node.parentNode;
            if (!node || !node.getAttribute) return;
            var href = node.getAttribute("href") || "";
            if (href.indexOf(LOGOUT_HREF) !== 0) return;
            if (node.getAttribute("target") === "_top" && global.top !== global.self) return; // let the frame escape
            ev.preventDefault();
            perform({});
        }, true);
    }

    function isBusy() { return _busy; }

    global.GMLogout = {
        FALLBACK_KEYS: FALLBACK_KEYS,
        NEVER_CLEAR: NEVER_CLEAR.slice(),
        perform: perform,
        purgeKeys: purgeKeys,
        wire: wire,
        isBusy: isBusy
    };

    if (global.document && global.document.readyState === "loading") {
        global.document.addEventListener("DOMContentLoaded", wire);
    } else {
        wire();
    }
})(typeof window !== "undefined" ? window : this);
