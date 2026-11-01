/*
 * Canonical injected-wallet discovery and selection for wallet signing flows.
 * Discovery is passive. resolveInjectedProvider() is called only from an
 * explicit user action; it may request account access and always binds the
 * result to the authenticated session wallet before returning a provider.
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

    function parentWindow() {
        try {
            var parent = window.parent;
            if (parent && parent !== window && parent.document && parent.location.origin === window.location.origin) return parent;
        } catch (_) { /* Cross-origin parent is intentionally ignored. */ }
        return null;
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
        var parent = parentWindow();
        if (parent) {
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
        return out;
    }

    function announcementEntries() {
        var entries = announced.concat(window.__announced6963Providers || []);
        var parent = parentWindow();
        if (parent) entries = entries.concat(parent.__gmCommunityEip6963Providers || [], parent.__announced6963Providers || []);
        return entries;
    }

    function trustMetadata(provider) {
        return announcementEntries().some(function (entry) {
            if (!entry || entry.provider !== provider) return false;
            var info = entry.info || {};
            return /trustwallet|trust/i.test(String(info.rdns || "") + " " + String(info.name || ""));
        });
    }

    function isTrustProvider(provider) {
        if (!provider) return false;
        if (provider.isTrust || provider.isTrustWallet) return true;
        var parent = parentWindow();
        var roots = [window.trustwallet, window.trustWallet];
        if (parent) roots = roots.concat([parent.trustwallet, parent.trustWallet]);
        for (var i = 0; i < roots.length; i++) {
            if (roots[i] && (coerce(roots[i]) === provider || coerce(roots[i].ethereum) === provider)) return true;
        }
        return trustMetadata(provider);
    }

    function isMiniPayProvider(provider) {
        return !!(provider && (provider.isMiniPay || provider.isMiniPayWallet));
    }

    function isMobileContext() {
        var ua = "";
        try { ua = String(window.navigator && window.navigator.userAgent || "").toLowerCase(); } catch (_) {}
        if (/android|iphone|ipad|ipod|mobile|minipay|trust|metamask/i.test(ua)) return true;
        try { return /macintosh|mac os x/.test(ua) && Number(window.navigator.maxTouchPoints || 0) > 1; }
        catch (_) { return false; }
    }

    function isTrustContext(providers) {
        var ua = "";
        try { ua = String(window.navigator && window.navigator.userAgent || "").toLowerCase(); } catch (_) {}
        return /trustwallet|trust\//.test(ua) || (providers || collect()).some(isTrustProvider);
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

    function preferredProvider(providers) {
        var miniPay = providers.find(isMiniPayProvider);
        if (miniPay) return miniPay;
        var trust = providers.find(isTrustProvider);
        if (trust) return trust;
        var metaMask = providers.find(function (p) { return p && p.isMetaMask && !p.isBraveWallet; });
        if (metaMask) return metaMask;
        return providers[0] || null;
    }

    function accountList(provider, method) {
        return Promise.resolve(provider.request({ method: method })).then(function (accounts) {
            return Array.isArray(accounts) ? accounts : [];
        }).catch(function () { return []; });
    }

    async function resolveInjectedProvider(options) {
        options = options || {};
        var loginMethod = String(options.loginMethod || "").toLowerCase();
        if (loginMethod && loginMethod !== "injected") return null;
        var wallet = String(options.wallet || "").toLowerCase();
        if (!/^0x[a-f0-9]{40}$/.test(wallet)) throw new Error("The signed-in wallet address is unavailable.");

        var providers = requestDiscovery();
        var mobile = isMobileContext();
        var started = Date.now();
        var trustBudget = typeof options.timeoutMs === "number" ? options.timeoutMs
            : (isTrustContext(providers) ? 4200 : (mobile ? 2500 : 800));
        var knownPreferred = function (list) {
            return list.some(function (p) { return isMiniPayProvider(p) || isTrustProvider(p) ||
                (p && (p.isMetaMask || p.isBraveWallet || p.isCoinbaseWallet)); });
        };
        // On mobile, keep discovering after a generic ethereum shim appears.
        // Return early only for a recognized wallet, or on desktop.
        if (!mobile && providers.length) return bindProvider(providers, wallet);
        while (Date.now() - started < trustBudget) {
            if (providers.length && knownPreferred(providers)) return bindProvider(providers, wallet);
            await new Promise(function (resolve) { setTimeout(resolve, 120); });
            providers = requestDiscovery();
        }
        if (!providers.length) return null;
        return bindProvider(providers, wallet);
    }

    async function bindProvider(providers, wallet) {
        var ordered = [];
        var preferred = preferredProvider(providers);
        if (preferred) ordered.push(preferred);
        providers.forEach(function (p) { if (ordered.indexOf(p) < 0) ordered.push(p); });
        // First use already-authorized accounts: this avoids prompting wallets
        // that do not own the authenticated session address.
        for (var i = 0; i < ordered.length; i++) {
            var existing = await accountList(ordered[i], "eth_accounts");
            if (existing.some(function (account) { return String(account).toLowerCase() === wallet; })) return ordered[i];
        }
        // The action is user-initiated. Request access only from the canonical
        // preferred provider; never cascade prompts across unrelated wallets.
        var selected = preferred || ordered[0];
        if (!selected) return null;
        var requested;
        try {
            requested = await selected.request({ method: "eth_requestAccounts" });
        } catch (error) {
            if (error && (error.code === 4001 || /reject|denied|cancel/i.test(String(error.message || "")))) throw error;
            throw new Error("Could not connect to the selected wallet. Open this page in your wallet and try again.");
        }
        if (!(Array.isArray(requested) && requested.some(function (account) { return String(account).toLowerCase() === wallet; }))) {
            throw new Error("The connected wallet account does not match your signed-in account. Switch accounts or sign in again.");
        }
        return selected;
    }

    try {
        window.addEventListener("eip6963:announceProvider", function (event) {
            var detail = event && event.detail;
            if (!detail || !detail.provider) return;
            var exists = announced.some(function (item) {
                return item.provider === detail.provider ||
                    (item.info && detail.info && item.info.uuid && detail.info.uuid && item.info.uuid === detail.info.uuid);
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
        waitForProviders: waitForProviders,
        resolveInjectedProvider: resolveInjectedProvider
    };
})(window);
