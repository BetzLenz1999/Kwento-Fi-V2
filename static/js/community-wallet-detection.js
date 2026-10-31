/*
 * Shared injected-wallet discovery for Community wallet features.
 * This module discovers providers only: it never requests accounts, switches
 * chains, or signs. Individual flows retain their own account and login-method
 * authorization checks before using a provider.
 */
(function (window) {
    "use strict";
    if (!window || window.GMCommunityWalletDetection) return;

    var announced = window.__gmCommunityEip6963Providers || [];
    window.__gmCommunityEip6963Providers = announced;

    function coerce(candidate) {
        if (!candidate) return null;
        if (typeof candidate.request === "function") return candidate;
        if (candidate.ethereum && typeof candidate.ethereum.request === "function") return candidate.ethereum;
        if (candidate.provider && typeof candidate.provider.request === "function") return candidate.provider;
        var legacy = candidate.ethereum || candidate.provider || candidate;
        if (legacy && typeof legacy.request !== "function") {
            var send = typeof legacy.sendAsync === "function" ? legacy.sendAsync.bind(legacy)
                : (typeof legacy.send === "function" ? legacy.send.bind(legacy) : null);
            if (send) {
                try {
                    legacy.request = function (payload) {
                        return new Promise(function (resolve, reject) {
                            send({ jsonrpc: "2.0", id: Date.now(), method: payload.method,
                                params: Array.isArray(payload.params) ? payload.params : [] }, function (error, response) {
                                if (error) return reject(error);
                                if (response && response.error) return reject(response.error);
                                resolve(response && Object.prototype.hasOwnProperty.call(response, "result")
                                    ? response.result : response);
                            });
                        });
                    };
                    if (typeof legacy.request === "function") return legacy;
                } catch (_) { /* frozen legacy provider: skip it safely */ }
            }
        }
        return null;
    }

    function addUnique(out, candidate) {
        var provider = coerce(candidate);
        if (provider && out.indexOf(provider) < 0) out.push(provider);
    }

    function collect() {
        var out = [];
        var ethereum = window.ethereum;
        if (ethereum && Array.isArray(ethereum.providers)) ethereum.providers.forEach(function (p) { addUnique(out, p); });
        addUnique(out, ethereum);
        [window.trustwallet, window.trustWallet].forEach(function (root) {
            addUnique(out, root);
            if (root) addUnique(out, root.ethereum);
        });
        announced.forEach(function (item) { if (item) addUnique(out, item.provider); });
        // The Community menu embeds some tools in same-origin frames. A wallet
        // dApp browser may inject only in the top frame; do not cross origins.
        try {
            var parent = window.parent;
            if (parent && parent !== window) {
                var pe = parent.ethereum;
                if (pe && Array.isArray(pe.providers)) pe.providers.forEach(function (p) { addUnique(out, p); });
                addUnique(out, pe);
                [parent.trustwallet, parent.trustWallet].forEach(function (root) {
                    addUnique(out, root);
                    if (root) addUnique(out, root.ethereum);
                });
                var parentAnnounced = parent.__gmCommunityEip6963Providers || parent.__announced6963Providers || [];
                if (Array.isArray(parentAnnounced)) parentAnnounced.forEach(function (item) { if (item) addUnique(out, item.provider); });
            }
        } catch (_) { /* cross-origin parent is intentionally ignored */ }
        return out;
    }

    function trustMetadata(provider) {
        var entries = announced.concat(window.__announced6963Providers || []);
        return entries.some(function (entry) {
            if (!entry || entry.provider !== provider) return false;
            var info = entry.info || {};
            return /trustwallet|trust/i.test(String(info.rdns || "") + " " + String(info.name || ""));
        });
    }

    function isTrustProvider(provider) {
        if (!provider) return false;
        if (provider.isTrust || provider.isTrustWallet) return true;
        var roots = [window.trustwallet, window.trustWallet];
        for (var i = 0; i < roots.length; i++) {
            if (roots[i] && (coerce(roots[i]) === provider || coerce(roots[i].ethereum) === provider)) return true;
        }
        return trustMetadata(provider);
    }

    function isMobileContext() {
        var ua = "";
        try { ua = String(window.navigator && window.navigator.userAgent || "").toLowerCase(); } catch (_) {}
        if (/android|iphone|ipad|ipod|mobile|minipay|trust|metamask/i.test(ua)) return true;
        try { return /macintosh|mac os x/.test(ua) && Number(window.navigator.maxTouchPoints || 0) > 1; }
        catch (_) { return false; }
    }

    function requestDiscovery() {
        try { window.dispatchEvent(new Event("eip6963:requestProvider")); } catch (_) {}
        return collect();
    }

    function waitForProviders(timeoutMs) {
        var mobile = isMobileContext();
        var budget = typeof timeoutMs === "number" ? timeoutMs : (mobile ? 3200 : 700);
        var start = Date.now();
        var found = requestDiscovery();
        if (found.length || budget <= 0) return Promise.resolve(found);
        return new Promise(function (resolve) {
            (function poll() {
                found = requestDiscovery();
                if (found.length || Date.now() - start >= budget) return resolve(found);
                setTimeout(poll, 120);
            })();
        });
    }

    try {
        window.addEventListener("eip6963:announceProvider", function (event) {
            var detail = event && event.detail;
            if (!detail || !detail.provider) return;
            var exists = announced.some(function (item) {
                return item.provider === detail.provider ||
                    (item.info && detail.info && item.info.uuid && item.info.uuid === detail.info.uuid);
            });
            if (!exists) announced.push(detail);
        });
        window.addEventListener("ethereum#initialized", requestDiscovery);
        window.addEventListener("TrustWallet#initialized", requestDiscovery);
        window.addEventListener("pageshow", requestDiscovery);
        if (window.document && window.document.addEventListener) {
            window.document.addEventListener("visibilitychange", function () {
                if (window.document.visibilityState === "visible") requestDiscovery();
            });
        }
        requestDiscovery();
    } catch (_) {}

    window.GMCommunityWalletDetection = {
        collect: collect,
        isTrustProvider: isTrustProvider,
        isMobileContext: isMobileContext,
        requestDiscovery: requestDiscovery,
        waitForProviders: waitForProviders
    };
})(window);
