/**
 * P2PWallet — login-method-aware signing helper for the P2P trading page.
 *
 * Mirrors the savings / reloadly approach: it routes signing through whatever
 * login method the user used.
 *   - injected login  -> the injected provider (MiniPay / Trust / MetaMask)
 *   - walletconnect /  -> the GMWalletConnect EIP-1193 bridge provider
 *     goodmarket/manual    (static/js/wc-bridge.js, configured by the page)
 *
 * P2P on-chain user actions (createListing/approve, openOrder,
 * releaseOrder=Approve, cancel, raiseDispute) go through sendTx(). Mark-paid
 * is an off-chain seller notification handled by the backend. The owner
 * (P2P_KEY) admin-review release/refund is server-signed and NOT handled here.
 *
 * Requires ethers v6 and wc-bridge.js to be loaded first.
 */
window.P2PWallet = (function () {
    "use strict";

    var cfg = { wallet: "", chainId: 42220, loginMethod: "" };
    var CELO_RPC_URLS = [
        "https://forno.celo.org",
        "https://1rpc.io/celo",
        "https://celo.publicnode.com",
    ];
    // Match the existing Community transaction fallback used by chatroom-widget.js.
    var CELO_FALLBACK_GAS_PRICE = 500000000000n;

    // Celo Mainnet fee-currency adapters MiniPay accepts on eth_sendTransaction.
    var MINIPAY_FEE_CURRENCY = {
        USDM: "0x765DE816845861e75A25fCA122bb6898B8B1282a",
        USDT_ADAPTER: "0x0E2A3e05bc9A16F5292A6170456A710cb89C6f72",
        USDC_ADAPTER: "0x2F25deB3848C207fc8E0c34035B3Ba7fC157602B",
    };

    // Read the page's boot config LAZILY. It is an inline object emitted late in
    // <body>, so snapshotting it at load time would always read "". A page that
    // forgets to call configure() (the Community Garden did) therefore used to
    // leave loginMethod empty, which made every local (in-app) wallet sign fall
    // through to the WalletConnect bridge and fail with "No active WalletConnect
    // session." — and let an installed MetaMask extension sign for an in-app
    // account. Explicit configure() values still win.
    function _bootConfig() {
        var boot = window.GM_WALLET_BOOT || window.GM_COMMUNITY_BOOT || {};
        return {
            wallet: boot.wallet || "",
            chainId: Number(boot.celoChainId || boot.chainId || 42220),
            loginMethod: boot.loginMethod || "",
        };
    }

    function configure(o) {
        Object.assign(cfg, _bootConfig(), o || {});
    }
    // Seed the defaults so a caller that never configures still signs with the
    // right method. A guest render has no wallet, so the guards below still hold.
    configure();

    function _isPrivyLogin() {
        return String(cfg.loginMethod || "").toLowerCase() === "privy";
    }

    async function _getPrivyProvider(options) {
        if (!_isPrivyLogin()) return null;
        options = options || {};
        var timeoutMs = typeof options.timeoutMs === "number" ? options.timeoutMs : 4000;
        var start = Date.now();
        var wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
        while (Date.now() - start < timeoutMs) {
            try {
                var wallets = Array.isArray(window.GMPrivyWallets) ? window.GMPrivyWallets : [];
                var sessionWallet = (cfg.wallet || "").toLowerCase();
                var wallet = wallets.find(function (w) { return (w.address || "").toLowerCase() === sessionWallet; })
                    || wallets.find(function (w) { return w.walletClientType === "privy"; })
                    || wallets[0];
                if (wallet && typeof wallet.getEthereumProvider === "function") {
                    var provider = await wallet.getEthereumProvider();
                    if (provider && typeof provider.request === "function") {
                        provider.__gmPrivyProvider = true;
                        return provider;
                    }
                }
                if (window.GMPrivyReady && !window.GMPrivyAuthenticated && typeof window.GMPrivyLogin === "function" && options.promptLogin) {
                    await window.GMPrivyLogin();
                }
            } catch (err) {
                if (err && (err.code === 4001 || /reject|cancel/i.test(String(err.message || "")))) throw err;
                console.warn("[privy] provider lookup failed:", err);
            }
            await wait(150);
        }
        return null;
    }

    function _preferWc() {
        return (
            typeof GMWalletConnect !== "undefined" &&
            typeof GMWalletConnect.prefersWcSigning === "function" &&
            GMWalletConnect.prefersWcSigning()
        );
    }

    function _announced6963Providers() {
        var out = [];
        var seen = [];
        try {
            var list = window.__announced6963Providers;
            if (Array.isArray(list)) {
                for (var i = 0; i < list.length; i++) {
                    var entry = list[i];
                    if (!entry || !entry.provider) continue;
                    if (seen.indexOf(entry.provider) >= 0) continue;
                    seen.push(entry.provider);
                    out.push(entry);
                }
            }
        } catch (_) { /* older browsers / strict CSPs */ }
        return out;
    }

    function _coerceToRequestProvider(candidate) {
        if (!candidate) return null;
        if (typeof candidate.request === "function") return candidate;
        if (candidate.ethereum && typeof candidate.ethereum.request === "function") return candidate.ethereum;
        if (candidate.provider && typeof candidate.provider.request === "function") return candidate.provider;
        return null;
    }

    function _looksLikeTrustInfo(info) {
        if (!info) return false;
        var rdns = String(info.rdns || "").toLowerCase();
        var name = String(info.name || "").toLowerCase();
        return rdns.indexOf("trustwallet") >= 0 || name.indexOf("trust") >= 0;
    }

    function _isTrustWalletAliasProvider(provider) {
        if (!provider) return false;
        var roots = [window.trustwallet, window.trustWallet];
        for (var i = 0; i < roots.length; i++) {
            if (roots[i] && _coerceToRequestProvider(roots[i]) === provider) return true;
        }
        return false;
    }

    function _isTrustWalletProvider(provider) {
        if (!provider) return false;
        if (provider.isTrust || provider.isTrustWallet) return true;
        // Some Trust builds inject ONLY the branded alias with no `isTrust`
        // flag and no EIP-6963 announcement. Recognise the alias itself so a
        // co-installed extension's provider cannot shadow the authentic one.
        if (_isTrustWalletAliasProvider(provider)) return true;
        var announced = _announced6963Providers();
        for (var i = 0; i < announced.length; i++) {
            if (announced[i].provider === provider) return _looksLikeTrustInfo(announced[i].info);
        }
        return false;
    }

    function _collectInjected() {
        var out = [];
        var push = function (p) {
            var provider = _coerceToRequestProvider(p);
            if (provider && out.indexOf(provider) < 0) out.push(provider);
        };
        if (window.ethereum) {
            if (window.ethereum.providers && window.ethereum.providers.length) {
                window.ethereum.providers.forEach(push);
            }
            push(window.ethereum);
        }
        // Trust Wallet's branded injection. The alias casing varies across builds
        // and platforms, and recent mobile builds expose ONLY this (no
        // window.ethereum), so both spellings must be collected.
        if (window.trustwallet) push(window.trustwallet);
        if (window.trustwallet && window.trustwallet.ethereum) push(window.trustwallet.ethereum);
        if (window.trustWallet) push(window.trustWallet);
        if (window.trustWallet && window.trustWallet.ethereum) push(window.trustWallet.ethereum);
        var announced = _announced6963Providers();
        for (var i = 0; i < announced.length; i++) push(announced[i].provider);
        return out;
    }

    function _getInjected() {
        // WalletConnect / manual / local logins must never use an injected
        // wallet — its account differs from the logged-in GoodMarket wallet.
        // Keyed on the STRICT `_wcFamilyLogin()`, never the broad `_preferWc()`
        // (true for any stored session, which hid a genuine injected/Trust login).
        if (_wcFamilyLogin() || _isPrivyLogin() || _isLocalLogin()) return null;
        try { window.dispatchEvent(new Event("eip6963:requestProvider")); } catch (_) {}
        var providers = _collectInjected();
        if (!providers.length) return null;
        var mini = providers.find(function (p) { return p && p.isMiniPay; });
        if (mini) return mini;
        // Inside Trust Wallet's dApp browser the authentic provider may only be
        // exposed via window.trustwallet or an EIP-6963 announcement, and its UA
        // is often plain Chrome/Safari — so prefer a Trust provider whenever one
        // is present rather than falling through to a co-installed extension.
        var trust = providers.find(function (p) { return _isTrustWalletProvider(p); });
        if (trust) return trust;
        var mm = providers.find(function (p) { return p && p.isMetaMask && !p.isBraveWallet; });
        return mm || providers[0];
    }

    // An injected provider whose ACTIVE account is the session wallet. Without
    // this, a multi-extension phone can sign from an unrelated account (the
    // "Wrong wallet connected" report). Only used for the injected path — a
    // WalletConnect/Privy login must never prompt a stray extension.
    async function _injectedMatchingWallet() {
        if (_wcFamilyLogin() || _isPrivyLogin() || _isLocalLogin()) return null;
        var candidates = _collectInjected();
        if (!candidates.length) return null;
        var want = String(cfg.wallet || "").toLowerCase();
        if (!want) return null;
        var matches = [];
        for (var i = 0; i < candidates.length; i++) {
            try {
                var accounts = await candidates[i].request({ method: "eth_requestAccounts" });
                if (!accounts || !accounts.length) accounts = await candidates[i].request({ method: "eth_accounts" });
                var hit = (accounts || []).some(function (a) { return String(a).toLowerCase() === want; });
                if (hit) matches.push(candidates[i]);
            } catch (_) { /* locked / rejected — try the next provider */ }
        }
        if (!matches.length) return null;
        // Both may hold the same address. MiniPay first (it is the user's actual
        // wallet and needs CIP-64 fee handling), then Trust Wallet (inside its
        // browser the branded provider is the authentic one), then the first.
        var mini = matches.find(function (p) { return p && p.isMiniPay; });
        if (mini) return mini;
        var trust = matches.find(function (p) { return _isTrustWalletProvider(p); });
        return trust || matches[0];
    }

    function isMiniPay() {
        // The in-app wallet pays gas in CELO — CIP-64 feeCurrency params would
        // break its plain eth_sendTransaction path.
        if (_isLocalLogin()) return false;
        var ep = _getInjected();
        if (ep && ep.isMiniPay) return true;
        if (window.ethereum && window.ethereum.isMiniPay) return true;
        return typeof navigator !== "undefined" && /minipay/i.test(navigator.userAgent || "");
    }

    function _isLocalLogin() {
        return String(cfg.loginMethod || "").toLowerCase() === "local";
    }

    function _wcFamilyLogin() {
        return ["walletconnect", "manual", "manual_address"].indexOf(
            String(cfg.loginMethod || "").toLowerCase()
        ) >= 0;
    }

    // Legacy "injected" login guard: a leftover stored WC session must not
    // hijack a genuine injected/Trust login (the request would go to the dead WC
    // relay and the extension would never be asked). A WC-family login is
    // unaffected — it always keeps its own session.
    async function _wcSessionHasNoInjectedCompetitor() {
        if (_wcFamilyLogin()) return true;
        return (await _injectedMatchingWallet()) === null;
    }

    // Should the WalletConnect fallback be allowed at all? It exists to recover
    // legacy logins that predate login_method persistence (they report
    // "injected"), so a KNOWN non-WC method must never be routed there by
    // omission — that is how a local login ended up asking for a WC session.
    function _wcFallbackAllowed() {
        if (_isLocalLogin() || _isPrivyLogin()) return false;
        if (_wcFamilyLogin()) return true;
        return _preferWc();
    }

    async function getProvider() {
        // Local self-custodial accounts sign with the browser wallet (PIN-decrypted).
        // The wallet auto-locks after 15 min — prompt for the PIN instead of
        // failing with "Wallet is locked" deep inside a transaction flow.
        if (_isLocalLogin() && typeof GMLocalWallet !== "undefined") {
            if (!GMLocalWallet.isUnlocked()) {
                if (typeof window._lwOpenUnlockModal === "function") {
                    await window._lwOpenUnlockModal();
                } else {
                    throw new Error("Please unlock your GoodMarket wallet to continue.");
                }
            }
            return GMLocalWallet.getProvider();
        }
        var privy = await _getPrivyProvider({ promptLogin: true, timeoutMs: 10000 });
        if (privy) return privy;
        // Prefer the injected provider that actually holds the session wallet, so
        // a multi-extension phone cannot sign from an unrelated account.
        var matched = await _injectedMatchingWallet();
        if (matched) return matched;
        var ep = _getInjected();
        if (ep) return ep;
        if (_wcFallbackAllowed() && await _wcSessionHasNoInjectedCompetitor() && typeof GMWalletConnect !== "undefined") {
            try { return await GMWalletConnect.getProvider(); } catch (_) {}
        }
        throw new Error(
            _isPrivyLogin()
                ? "Privy wallet is not ready. Please sign in again."
                : "No wallet available. Open in a dApp browser (MiniPay / Trust / MetaMask) or log in with WalletConnect."
        );
    }

    function getReadProvider() {
        // Pin the network explicitly: with no 2nd argument the provider
        // auto-detects (anyNetwork), which hides a wrong-chain RPC instead of
        // failing loudly. Never pass a bare quorum number here — it is read as
        // the network and breaks every Celo read with
        // "network changed: 1 => 42220".
        //
        // quorum MUST be 1. ethers defaults it to ceil(providers/2) = 2, but G$
        // is a rebasing token whose balanceOf differs at every block, so three
        // RPCs sitting at three heights never agree. Every G$ read then dies
        // with SERVER_ERROR "quorum not met" — including the balanceOf guard
        // that runs before any signing prompt.
        return new ethers.FallbackProvider(
            CELO_RPC_URLS.map(function (u, i) {
                return {
                    provider: new ethers.JsonRpcProvider(u, { chainId: cfg.chainId, name: "celo" }),
                    priority: i + 1,
                    weight: 1,
                    stallTimeout: 2000,
                };
            }),
            { chainId: cfg.chainId, name: "celo" },
            { quorum: 1 }
        );
    }

    async function _waitReceipt(ep, txHash, maxAttempts) {
        maxAttempts = maxAttempts || 60;
        for (var i = 0; i < maxAttempts; i++) {
            try {
                var r = await ep.request({ method: "eth_getTransactionReceipt", params: [txHash] });
                if (r) {
                    if (r.status === "0x0") throw new Error("Transaction reverted on-chain.");
                    return r;
                }
            } catch (e) {
                if (e && e.message && /reverted/i.test(e.message)) throw e;
            }
            await new Promise(function (res) { return setTimeout(res, 2000); });
        }
        return null; // may still land; caller can link to CeloScan
    }

    function _parseChainId(value) {
        if (typeof value === "number") return Number.isSafeInteger(value) ? value : null;
        var raw = String(value || "").trim();
        var parsed = /^0x[0-9a-f]+$/i.test(raw)
            ? parseInt(raw, 16)
            : (/^\d+$/.test(raw) ? parseInt(raw, 10) : NaN);
        return Number.isSafeInteger(parsed) ? parsed : null;
    }

    async function _readChainId(ep) {
        var reported;
        try {
            reported = await ep.request({ method: "eth_chainId" });
        } catch (_) {
            throw new Error("Could not verify your wallet's active network. No transaction was sent.");
        }
        var chainId = _parseChainId(reported);
        if (chainId === null) {
            throw new Error("Your wallet did not report its active network. No transaction was sent.");
        }
        return chainId;
    }

    async function _assertCelo(ep) {
        if (ep.isGoodMarketWcBridge) return; // bridge routes wallet requests to its configured Celo session
        if (await _readChainId(ep) !== Number(cfg.chainId)) {
            throw new Error("Your wallet is not on Celo Mainnet. Switch to Celo and try again. No transaction was sent.");
        }
    }

    function _walletErrorDetails(err) {
        var queue = [err];
        var seen = [];
        var codes = [];
        var messages = [];
        while (queue.length) {
            var item = queue.shift();
            if (!item) continue;
            if (typeof item === "string") {
                messages.push(item);
                continue;
            }
            if (typeof item !== "object" || seen.indexOf(item) >= 0) continue;
            seen.push(item);
            if (item.code !== undefined && item.code !== null) {
                var code = Number(item.code);
                if (Number.isFinite(code)) codes.push(code);
                if (String(item.code).toUpperCase() === "ACTION_REJECTED") codes.push(4001);
            }
            if (typeof item.message === "string") messages.push(item.message);
            ["cause", "data", "error", "originalError", "info"].forEach(function (key) {
                if (item[key] && typeof item[key] === "object") queue.push(item[key]);
                else if (typeof item[key] === "string") messages.push(item[key]);
            });
        }
        return { codes: codes, message: messages.join(" ").toLowerCase() };
    }

    function _isWalletUserRejection(err) {
        var details = _walletErrorDetails(err);
        return details.codes.indexOf(4001) >= 0
            || /user (?:rejected|denied)|rejected by (?:the )?user|request was rejected|cancelled by user|canceled by user/.test(details.message);
    }

    function _isUnknownChainError(err) {
        if (_isWalletUserRejection(err)) return false;
        var details = _walletErrorDetails(err);
        return details.codes.indexOf(4902) >= 0
            || details.codes.indexOf(-32603) >= 0
            || /unrecognized chain|unknown chain|chain (?:is )?(?:not added|not found|not recognized|unknown|missing)/.test(details.message);
    }

    async function _ensureCelo(ep) {
        if (ep.isGoodMarketWcBridge) return; // bridge is Celo-scoped
        var expectedChainId = Number(cfg.chainId);
        if (!Number.isSafeInteger(expectedChainId) || expectedChainId <= 0) {
            throw new Error("Celo network configuration is invalid. No transaction was sent.");
        }
        if (await _readChainId(ep) === expectedChainId) return;

        var hex = "0x" + expectedChainId.toString(16);
        try {
            await ep.request({ method: "wallet_switchEthereumChain", params: [{ chainId: hex }] });
        } catch (switchErr) {
            if (_isWalletUserRejection(switchErr)) {
                throw new Error("Celo network switch was cancelled. No transaction was sent.");
            }
            if (!_isUnknownChainError(switchErr)) {
                throw new Error("Could not switch your wallet to Celo Mainnet. No transaction was sent.");
            }
            try {
                await ep.request({
                    method: "wallet_addEthereumChain",
                    params: [{
                        chainId: hex, chainName: "Celo Mainnet",
                        nativeCurrency: { name: "CELO", symbol: "CELO", decimals: 18 },
                        rpcUrls: CELO_RPC_URLS.slice(), blockExplorerUrls: ["https://celoscan.io"],
                    }],
                });
                // Adding a network does not guarantee that the wallet activated it.
                await ep.request({ method: "wallet_switchEthereumChain", params: [{ chainId: hex }] });
            } catch (networkErr) {
                if (_isWalletUserRejection(networkErr)) {
                    throw new Error("Celo network setup was cancelled. No transaction was sent.");
                }
                throw new Error("Could not activate Celo Mainnet in your wallet. No transaction was sent.");
            }
        }
        await _assertCelo(ep);
    }


    function _isPrivyEmbeddedProvider(ep, from) {
        if (!ep || !ep.__gmPrivyProvider || !from) return false;
        var wallets = Array.isArray(window.GMPrivyWallets) ? window.GMPrivyWallets : [];
        return wallets.some(function (w) {
            return w
                && w.walletClientType === "privy"
                && (w.address || "").toLowerCase() === String(from).toLowerCase();
        });
    }

    function _privyActionLabel(fn) {
        switch (fn) {
            case "approve": return "Approve G$ spending";
            case "createListing": return "Create P2P sell ad";
            case "openOrder": return "Open P2P order";
            case "cancelListing": return "Cancel P2P sell ad";
            case "cancelOrder": return "Cancel P2P order";
            case "releaseOrder": return "Release escrowed G$";
            case "raiseDispute": return "Raise P2P dispute";
            default: return "Confirm P2P transaction";
        }
    }

    function _notifyTxOption(options, name, value) {
        if (!options || typeof options[name] !== "function") return;
        try { options[name](value); } catch (_) {
            // Observers must not turn a submitted wallet transaction into a send failure.
        }
    }

    async function _privySendTx(to, data, valueHex, from, fn, options) {
        if (typeof window.GMPrivySendTransaction !== "function") {
            throw new Error("Privy transaction signing is not ready. Please refresh and try again.");
        }
        var label = _privyActionLabel(fn);
        _notifyTxOption(options, "onBroadcastStarted");
        var receipt = await window.GMPrivySendTransaction(
            {
                to: to,
                data: data,
                value: valueHex || "0x0",
                chainId: cfg.chainId,
            },
            {
                address: from,
                uiOptions: {
                    description: label + " on GoodMarket P2P. Review the details before signing.",
                    buttonText: label,
                    transactionInfo: {
                        title: "GoodMarket P2P",
                        action: label,
                    },
                },
            }
        );
        var txHash = (receipt && (receipt.hash || receipt.transactionHash)) || (typeof receipt === "string" ? receipt : null);
        if (!txHash) throw new Error("Privy did not return a transaction hash.");
        _notifyTxOption(options, "onTransactionHash", txHash);
        return txHash;
    }

    async function _miniPayTx(ep, to, data, valueHex, options) {
        var value = valueHex || "0x0";
        var accounts = await ep.request({ method: "eth_requestAccounts" });
        var from = (accounts && accounts[0]) || cfg.wallet;
        var gasHex;
        try {
            var est = await ep.request({ method: "eth_estimateGas", params: [{ from: from, to: to, data: data, value: value }] });
            gasHex = "0x" + (BigInt(est) * 140n / 100n).toString(16);
        } catch (_) { gasHex = "0x7A120"; }
        var attempts = (window.GMMinipayFeeCurrencies && window.GMMinipayFeeCurrencies.orderByBalances)
            ? await window.GMMinipayFeeCurrencies.orderByBalances(ep, from, { includePlain: true })
            : [MINIPAY_FEE_CURRENCY.USDM, MINIPAY_FEE_CURRENCY.USDT_ADAPTER, MINIPAY_FEE_CURRENCY.USDC_ADAPTER, null];
        var lastErr;
        for (var k = 0; k < attempts.length; k++) {
            var p = { from: from, to: to, data: data, value: value, gas: gasHex };
            if (attempts[k]) p.feeCurrency = attempts[k];
            var txHash = null;
            var sendRequestStarted = false;
            try {
                await _assertCelo(ep);
                sendRequestStarted = true;
                _notifyTxOption(options, "onBroadcastStarted");
                txHash = await ep.request({ method: "eth_sendTransaction", params: [p] });
                if (!txHash) throw new Error("Transaction failed");
                _notifyTxOption(options, "onTransactionHash", txHash);
                if (!options || options.waitForReceipt !== false) await _waitReceipt(ep, txHash);
                return txHash;
            } catch (err) {
                lastErr = err;
                // Once a provider has returned a hash, never retry with another
                // fee currency: that could submit a duplicate transfer.
                if (txHash) throw err;
                if (sendRequestStarted && options && options.noRetryAfterSendRequest) throw err;
                var msg = ((err && (err.message || "")) + "").toLowerCase();
                if ((err && err.code === 4001) || /reject|denied|revert/i.test(msg)) throw err;
            }
        }
        throw lastErr || new Error("MiniPay transaction failed");
    }

    /**
     * Send a single contract call.
     * @param {string} to           contract address
     * @param {string} abiFragment  e.g. "function openOrder(uint256 listingId, uint256 amount) returns (uint256)"
     * @param {Array}  args         function args
     * @param {Object} options      optional submission callbacks and receipt-wait policy
     * @returns {Promise<string>}   tx hash (after confirmation when possible by default)
     */
    async function sendTx(to, abiFragment, args, valueHex, options) {
        options = options || {};
        var ep = await getProvider();
        var accounts = await ep.request({ method: "eth_requestAccounts" });
        var from = (accounts && accounts[0]) || cfg.wallet;
        if (cfg.wallet && from && from.toLowerCase() !== cfg.wallet.toLowerCase()) {
            throw new Error(
                "Wrong wallet. Switch to " + cfg.wallet.slice(0, 6) + "…" + cfg.wallet.slice(-4) + " and try again."
            );
        }
        var iface = new ethers.Interface([abiFragment]);
        var fn = abiFragment.match(/function (\w+)/)[1];
        var data = iface.encodeFunctionData(fn, args);

        await _ensureCelo(ep);
        if (_isPrivyEmbeddedProvider(ep, from)) {
            var privyTxHash = await _privySendTx(to, data, valueHex, from, fn, options);
            if (options.waitForReceipt !== false) await _waitReceipt(ep, privyTxHash);
            return privyTxHash;
        }
        if (isMiniPay()) return await _miniPayTx(ep, to, data, valueHex, options);

        var gasHex;
        try {
            var est = await ep.request({ method: "eth_estimateGas", params: [{ from: from, to: to, data: data, value: valueHex || "0x0" }] });
            gasHex = "0x" + (BigInt(est) * 130n / 100n).toString(16);
        } catch (_) { gasHex = "0x7A120"; }

        var gasPrice = CELO_FALLBACK_GAS_PRICE;
        try {
            var estimatedGasPrice = BigInt(await ep.request({ method: "eth_gasPrice", params: [] }));
            if (estimatedGasPrice > 0n) gasPrice = estimatedGasPrice;
        } catch (_) { /* keep the proven Celo fallback instead of making the wallet estimate fees */ }

        // The wallet can be switched while gas is being prepared; verify again
        // immediately before signing. Do not include a non-standard chainId
        // transaction field because some injected wallets (including Trust) reject it.
        await _assertCelo(ep);

        _notifyTxOption(options, "onBroadcastStarted");
        var txHash = await ep.request({
            method: "eth_sendTransaction",
            params: [{ from: from, to: to, data: data, value: valueHex || "0x0", gas: gasHex, gasPrice: "0x" + gasPrice.toString(16) }],
        });
        if (!txHash) throw new Error("Transaction failed");
        _notifyTxOption(options, "onTransactionHash", txHash);
        if (options.waitForReceipt !== false) await _waitReceipt(ep, txHash);
        return txHash;
    }

    /**
     * Fetch a tx receipt via public RPC and decode an event by name from a
     * given contract ABI. Returns the decoded args object, or null.
     */
    async function decodeEvent(txHash, abi, eventName) {
        try {
            var rp = getReadProvider();
            var receipt = await rp.getTransactionReceipt(txHash);
            if (!receipt) return null;
            var iface = new ethers.Interface(abi);
            for (var i = 0; i < receipt.logs.length; i++) {
                try {
                    var parsed = iface.parseLog(receipt.logs[i]);
                    if (parsed && parsed.name === eventName) return parsed.args;
                } catch (_) {}
            }
        } catch (_) {}
        return null;
    }

    return {
        configure: configure,
        sendTx: sendTx,
        getProvider: getProvider,
        getReadProvider: getReadProvider,
        decodeEvent: decodeEvent,
        isMiniPay: isMiniPay,
    };
})();
