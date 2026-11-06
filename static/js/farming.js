/* GoodMarket Chicken Farm — page logic.
 *
 * Extracted from templates/farming.html so the markup stays readable and the
 * logic can be reasoned about (and tested) on its own. Per-request values come
 * from window.GM_FARM_BOOT, set inline by the template.
 *
 * Signing rules follow the rest of the app:
 *   - local logins sign with the PIN-decrypted in-app wallet (GMLocalWallet);
 *     an injected MetaMask is a DIFFERENT account and must never be used.
 *   - WalletConnect logins sign through the WC bridge.
 *   - everyone else uses the injected provider, bound to the account that
 *     matches the session wallet (multi-account wallets otherwise sign with
 *     whatever is selected).
 * Reads (balances, farm state, history reconciliation) go through a public
 * Celo RPC so they never pop a wallet prompt and never lag behind the phone's
 * node.
 */
(function () {
    "use strict";
    var DISPLAY_BRAND = String(window.GM_DISPLAY_BRAND || "GoodMarket");

    var boot = window.GM_FARM_BOOT || {};
    var WALLET = String(boot.wallet || "");
    var LOGIN_METHOD = String(boot.loginMethod || "").toLowerCase();
    var FARMING_CONTRACT = String(boot.farmingContract || "");
    var GD_CONTRACT = String(boot.gdContract || "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A");
    var CHAIN_ID = Number(boot.chainId || 42220);
    var CHAIN_HEX = "0x" + CHAIN_ID.toString(16);
    var CELO_RPC = String(boot.celoRpc || "https://forno.celo.org");
    var CELO_RPC_FALLBACKS = Array.isArray(boot.celoRpcFallbacks) && boot.celoRpcFallbacks.length
        ? boot.celoRpcFallbacks
        : [CELO_RPC, "https://rpc.ankr.com/celo", "https://celo.drpc.org"];
    var CFG = boot.config || {};
    var MIN_FARM = Number(CFG.min_farm_gd || 1000);
    var CHICKEN_PRICE = Number(CFG.chicken_price_gd || 100);
    var FARM_DAYS = Number(CFG.farm_days || 30);
    var MONTHLY_PROFIT_PERCENT = Number(CFG.monthly_profit_percent || 10);

    var GD_DECIMALS = 18;
    var HISTORY = window.GMFarmHistory;
    // Scope the history store to THIS wallet before anything reads it — a
    // shared browser must never mix two accounts' farm transactions.
    if (HISTORY && typeof HISTORY.setWallet === "function") {
        HISTORY.setWallet(WALLET);
    }
    var readProvider = null;
    var farmState = null;
    var tickTimer = null;
    var busy = false;

    // The farm state is polled so an active farm shows up on its own — waiting
    // for a manual Refresh tap made users think their farm had not registered.
    // 15s is a read-only public-RPC eth_call, cheap enough to keep the page
    // honest without hammering the node.
    var POLL_MS = 15000;
    var HISTORY_EVERY_TICKS = 4;   // reconcile the on-chain history every ~60s
    var pollTimer = null;
    var pollTicks = 0;
    var refreshing = null;   // promise while a read is in flight

    // getFarm() returns the full farm state, so the per-field view helpers
    // (pendingEggs / pendingEggValue / maxMonthlyProfit) are not declared here.
    var FARM_ABI = [
        "function startFarm(uint256 amount)",
        "function sellEggs()",
        "function closeFarm()",
        "function rewardPool() view returns (uint256)",
        "function getFarm(address user) view returns (uint256 principal, uint256 chickens, uint256 startedAt, uint256 lastEggSoldAt, uint256 unlocksAt, uint256 claimedProfit, bool active, bool isMature, uint256 eggsReady, uint256 eggValueGd, uint256 totalValueAtMaturity)",
    ];
    var GD_ABI = [
        "function approve(address spender, uint256 amount) returns (bool)",
        "function allowance(address owner, address spender) view returns (uint256)",
        "function balanceOf(address account) view returns (uint256)",
    ];

    var $ = function (id) { return document.getElementById(id); };
    var _toLower = function (v) { return String(v || "").toLowerCase(); };

    // ── pure helpers (exported for tests) ───────────────────────────────────

    // A farm amount is valid when it clears the minimum and buys whole
    // chickens — the contract rejects anything else, so catch it before the
    // wallet prompt instead of letting a revert look like a network error.
    function validateFarmAmount(raw) {
        var amount = Number(raw);
        if (!isFinite(amount) || amount <= 0) {
            return { ok: false, error: "Enter how many G$ you want to farm." };
        }
        if (amount < MIN_FARM) {
            return { ok: false, error: "Minimum farm is " + MIN_FARM + " G$ (" + Math.floor(MIN_FARM / CHICKEN_PRICE) + " chickens)." };
        }
        if (amount % CHICKEN_PRICE !== 0) {
            return { ok: false, error: "Amount must be a multiple of " + CHICKEN_PRICE + " G$ so it buys whole chickens." };
        }
        return { ok: true, amount: amount, chickens: Math.floor(amount / CHICKEN_PRICE) };
    }

    function projectedMonthlyProfit(principalGd) {
        var p = Number(principalGd);
        if (!isFinite(p) || p <= 0) return 0;
        return Math.round(p * (MONTHLY_PROFIT_PERCENT / 100) * 100) / 100;
    }

    // Copy shown after a successful close. The user's core question is "did my
    // G$ arrive?" — so the message names the exact amount sent and the hash.
    function describeCloseSuccess(amountGd, hash) {
        return "🎉 Congratulations — your farm is now closed and " +
            HISTORY.formatGd(amountGd) + " G$ has been sent to your wallet.";
    }

    function describeSellSuccess(amountGd) {
        return "✅ Eggs sold! " + HISTORY.formatGd(amountGd) + " G$ was sent to your wallet.";
    }

    function eggValueLabel(eggValue) {
        return HISTORY.formatWei(eggValue) + " G$";
    }

    // ── read provider (public RPC, no wallet prompt) ────────────────────────

    function getReadProvider() {
        if (readProvider) return readProvider;
        if (typeof ethers === "undefined") return null;
        var urls = [];
        CELO_RPC_FALLBACKS.forEach(function (u) {
            if (u && urls.indexOf(u) === -1) urls.push(u);
        });
        if (!urls.length) return null;

        // Real failover: forno regularly returns transient errors, and a
        // receipt read that silently fails is exactly what makes a user think
        // their G$ never arrived. quorum 1 keeps a single fast provider in
        // normal operation while allowing a switch on failure.
        //
        // The 2nd FallbackProvider argument is the NETWORK, not the quorum —
        // passing a bare `1` silently pins the provider to mainnet, so every
        // Celo read dies with "network changed: 1 => 42220". Quorum belongs in
        // the 3rd options argument.
        if (typeof ethers.FallbackProvider === "function" && urls.length > 1) {
            try {
                var entries = urls.map(function (u, i) {
                    return {
                        provider: new ethers.JsonRpcProvider(u),
                        priority: i + 1,
                        weight: 1,
                        stallTimeout: 2000,
                    };
                });
                readProvider = new ethers.FallbackProvider(
                    entries,
                    { chainId: CHAIN_ID, name: "celo" },
                    { quorum: 1 }
                );
                return readProvider;
            } catch (_) {
                readProvider = null;
            }
        }
        try {
            readProvider = new ethers.JsonRpcProvider(urls[0]);
        } catch (_) {
            readProvider = null;
        }
        return readProvider;
    }

    // ── signer resolution (mirrors swap.html / lotto.html) ──────────────────

    var isLocal = function () { return LOGIN_METHOD === "local"; };
    // The STRICT WalletConnect signal. `prefersWc()` below is broader — it is
    // also true for any *stored* WC session — so it must not decide routing on
    // its own (see _awaitProviders / resolveSigner).
    var isWcFamilyLogin = function () {
        return ["walletconnect", "manual", "manual_address"].indexOf(LOGIN_METHOD) >= 0;
    };
    var prefersWc = function () {
        return typeof GMWalletConnect !== "undefined" &&
            typeof GMWalletConnect.prefersWcSigning === "function" &&
            GMWalletConnect.prefersWcSigning();
    };

    // GMWalletConnect.getProvider() returns the EIP-1193 provider
    // SYNCHRONOUSLY (wc-bridge.js builds it on first call and returns the
    // object). Calling .then() on it threw
    // "GMWalletConnect.getProvider(...).then is not a function" and every
    // WalletConnect farm action died before the wallet was ever prompted.
    // Tolerate both shapes so a future async getProvider can't re-break this.
    function _wcProviderPromise() {
        return Promise.resolve().then(function () {
            return GMWalletConnect.getProvider();
        });
    }

    window.__farmEip6963Providers = window.__farmEip6963Providers || [];
    (function _initEip6963() {
        try {
            window.addEventListener("eip6963:announceProvider", function (event) {
                var detail = event && event.detail;
                if (!detail || !detail.provider) return;
                var uuid = detail.info && detail.info.uuid;
                var already = window.__farmEip6963Providers.find(function (d) {
                    return (d.info && d.info.uuid && uuid && d.info.uuid === uuid) || d.provider === detail.provider;
                });
                if (!already) window.__farmEip6963Providers.push(detail);
            });
            window.dispatchEvent(new Event("eip6963:requestProvider"));
        } catch (_) {}
    })();

    function _coerceProvider(candidate) {
        if (!candidate) return null;
        if (typeof candidate.request === "function") return candidate;
        if (candidate.ethereum && typeof candidate.ethereum.request === "function") return candidate.ethereum;
        if (candidate.provider && typeof candidate.provider.request === "function") return candidate.provider;
        return null;
    }

    function _collectProviders() {
        var shared = window.GMCommunityWalletDetection;
        if (shared && typeof shared.collect === "function") return shared.collect();
        var out = [];
        var push = function (p) {
            var provider = _coerceProvider(p);
            if (provider && out.indexOf(provider) === -1) out.push(provider);
        };
        if (window.ethereum) {
            if (Array.isArray(window.ethereum.providers)) window.ethereum.providers.forEach(push);
            push(window.ethereum);
        }
        if (window.trustwallet) push(window.trustwallet);
        if (window.trustWallet) push(window.trustWallet);
        (window.__farmEip6963Providers || []).forEach(function (d) { push(d.provider); });
        _parentProviders().forEach(push);
        return out;
    }

    // When /farming is embedded (Community → Menu → Earn More → Farming), a
    // mobile dApp browser injects its provider into the TOP frame only. The
    // iframe's own window has no window.ethereum, so the page reported "No
    // wallet detected" while the wallet was right there in the parent. Borrow
    // the parent's injected provider — same-origin only (a cross-origin parent
    // throws on property access) and never for a local/WC login, which have
    // their own signer.
    function _parentProviders() {
        var out = [];
        if (isLocal() || isWcFamilyLogin()) return out;
        try {
            var parent = window.parent;
            if (!parent || parent === window) return out;
            var pe = parent.ethereum;
            if (pe) {
                if (Array.isArray(pe.providers)) {
                    pe.providers.forEach(function (p) {
                        var c = _coerceProvider(p);
                        if (c && out.indexOf(c) === -1) out.push(c);
                    });
                }
                var own = _coerceProvider(pe);
                if (own && out.indexOf(own) === -1) out.push(own);
            }
            var pt = _coerceProvider(parent.trustwallet) || _coerceProvider(parent.trustWallet);
            if (pt && out.indexOf(pt) === -1) out.push(pt);
            if (Array.isArray(parent.__announced6963Providers)) {
                parent.__announced6963Providers.forEach(function (d) {
                    var c = d && _coerceProvider(d.provider);
                    if (c && out.indexOf(c) === -1) out.push(c);
                });
            }
        } catch (_) { /* cross-origin parent — its provider is not reachable */ }
        return out;
    }

    function _isMobile() {
        var ua = (navigator.userAgent || "").toLowerCase();
        if (/android|iphone|ipad|ipod|minipay|metamask|trust|trustwallet|mobile/i.test(ua)) return true;
        // iPadOS 13+ asks for the desktop site by default, so its UA is
        // indistinguishable from macOS and none of the tokens above match. A
        // Trust Wallet user on an iPad therefore got the SHORT desktop wait and
        // a spurious "No wallet detected". `maxTouchPoints > 1` is the accepted
        // tell — same fix get-started.js applies.
        try {
            if (/macintosh|mac os x/.test(ua) && Number(navigator.maxTouchPoints || 0) > 1) return true;
        } catch (_) { /* no navigator.maxTouchPoints */ }
        return false;
    }

    function _awaitProviders(timeoutMs) {
        // Only a WC-FAMILY login bypasses injected discovery. The broad
        // `prefersWc()` (true for any stored WC session) must NOT decide this:
        // a genuine injected/Trust login with a leftover session would then be
        // hidden from its own extension and routed to the dead WC relay
        // ("No active WalletConnect session."). A legacy "injected" login with
        // no matching provider still reaches WC via resolveSigner's fallback.
        if (isWcFamilyLogin() || isLocal()) return Promise.resolve([]);
        var shared = window.GMCommunityWalletDetection;
        if (shared && typeof shared.resolveInjectedProvider === "function") {
            return shared.resolveInjectedProvider({
                wallet: WALLET,
                loginMethod: LOGIN_METHOD,
                timeoutMs: timeoutMs
            }).then(function (provider) { return provider ? [provider] : []; });
        }
        var budget = typeof timeoutMs === "number" ? timeoutMs : (_isMobile() ? 3000 : 900);
        var start = Date.now();
        var providers = _collectProviders();
        if (providers.length) return Promise.resolve(providers);
        return new Promise(function (resolve) {
            (function poll() {
                try { window.dispatchEvent(new Event("eip6963:requestProvider")); } catch (_) {}
                providers = _collectProviders();
                if (providers.length) return resolve(providers);
                if (Date.now() - start >= budget) return resolve([]);
                setTimeout(poll, 120);
            })();
        });
    }

    function _isUserRejection(err) {
        var code = err && err.code;
        if (code === 4001 || code === 5000) return true;
        var msg = String((err && (err.shortMessage || err.message)) || "").toLowerCase();
        return msg.indexOf("user rejected") !== -1 || msg.indexOf("user denied") !== -1 ||
            msg.indexOf("cancelled") !== -1 || msg.indexOf("canceled") !== -1;
    }

    function _localProvider() {
        if (!isLocal() || typeof GMLocalWallet === "undefined") return Promise.resolve(null);
        if (!GMLocalWallet.isUnlocked()) {
            if (typeof window._lwOpenUnlockModal !== "function") {
                return Promise.reject(new Error("Please unlock your wallet and try again."));
            }
            return Promise.resolve(window._lwOpenUnlockModal()).then(function () {
                if (!GMLocalWallet.isUnlocked()) {
                    throw new Error("Your wallet is still locked. Please enter your PIN to sign.");
                }
                return GMLocalWallet.getProvider();
            });
        }
        return Promise.resolve(GMLocalWallet.getProvider());
    }

    function _signWithWc() {
        return _wcProviderPromise().then(function (wcProvider) {
            var p = new ethers.BrowserProvider(wcProvider);
            return p.getSigner().then(function (signer) {
                if (_toLower(signer.address) !== _toLower(WALLET)) {
                    throw new Error("Wrong WalletConnect wallet connected. Please switch to your " + DISPLAY_BRAND + " wallet.");
                }
                return { signer: signer, provider: wcProvider };
            });
        });
    }

    function resolveSigner() {
        if (isLocal()) {
            return _localProvider().then(function (provider) {
                var p = new ethers.BrowserProvider(provider);
                return p.getSigner().then(function (signer) {
                    if (_toLower(signer.address) !== _toLower(WALLET)) {
                        throw new Error("Wrong wallet connected. Please use your " + DISPLAY_BRAND + " wallet.");
                    }
                    return { signer: signer, provider: provider };
                });
            });
        }

        return _awaitProviders().then(function (providers) {
            if (!providers.length) {
                if (prefersWc()) return _signWithWc();
                throw new Error("No wallet detected. Open this page in MetaMask, Trust Wallet or another Celo wallet.");
            }

            // Bind to the account that matches the session wallet — a
            // multi-account wallet otherwise signs with whatever is selected.
            var wanted = _toLower(WALLET);

            return (function tryProvider(index, lastErr) {
                if (index >= providers.length) {
                    throw lastErr || new Error("Could not connect to your wallet. Please try again.");
                }
                var provider = providers[index];
                return Promise.resolve()
                    .then(function () {
                        return provider.request({ method: "eth_requestAccounts" })
                            .catch(function (reqErr) {
                                if (_isUserRejection(reqErr)) throw reqErr;
                                return provider.request({ method: "eth_accounts" }).catch(function () { return []; });
                            });
                    })
                    .then(function (accounts) {
                        accounts = accounts || [];
                        var match = accounts.find(function (a) { return _toLower(a) === wanted; });
                        if (!match) {
                            // Try the next discovered provider before reporting a
                            // wrong-wallet error — mobile dApp browsers inject
                            // several (MetaMask + Trust + MiniPay) at once.
                            var next = accounts.length
                                ? new Error("Wrong wallet connected. Please switch to your " + DISPLAY_BRAND + " wallet (" +
                                    WALLET.slice(0, 6) + "…) before continuing.")
                                : (lastErr || new Error("No wallet account available. Please unlock your wallet and try again."));
                            return tryProvider(index + 1, next);
                        }
                        var p = new ethers.BrowserProvider(provider);
                        return p.getSigner(match).then(function (signer) {
                            return { signer: signer, provider: provider };
                        });
                    })
                    .catch(function (err) {
                        if (_isUserRejection(err) || String(err.message || "").indexOf("Wrong wallet") === 0) throw err;
                        return tryProvider(index + 1, err);
                    });
            })(0, null);
        });
    }

    function ensureCelo(provider) {
        if (!provider || !provider.request) return Promise.resolve();
        return provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CHAIN_HEX }] })
            .catch(function (err) {
                if (err && (err.code === 4902 || String(err.message || "").indexOf("Unrecognized chain") !== -1)) {
                    return provider.request({
                        method: "wallet_addEthereumChain",
                        params: [{
                            chainId: CHAIN_HEX,
                            chainName: "Celo Mainnet",
                            rpcUrls: [CELO_RPC],
                            nativeCurrency: { name: "CELO", symbol: "CELO", decimals: 18 },
                            blockExplorerUrls: ["https://celoscan.io"],
                        }],
                    });
                }
                // 4001 here means the user declined the switch; the signature
                // itself will surface it. Not fatal to the flow.
                return null;
            });
    }

    // ── transaction tracking ────────────────────────────────────────────────

    // Record a transaction that has actually been broadcast. Called AFTER the
    // wallet returns a hash so a rejected signature never leaves a phantom row
    // — the history is a record of on-chain activity, not of attempted clicks.
    function trackTx(action, hash, opts) {
        opts = opts || {};
        var entry = HISTORY.add({
            hash: hash,
            action: action,
            actionLabel: opts.actionLabel || HISTORY.ACTION_LABELS[action] || action,
            amountLabel: opts.amountLabel || "",
            wallet: WALLET,
            status: "pending",
            note: opts.note || "",
        });
        renderHistory();
        return entry;
    }

    // Sum the G$ delivered TO this wallet in a receipt's Transfer logs. This is
    // what actually answers "did the G$ arrive?" — an ERC-20 transfer emits
    // Transfer(from, to, value) and the wallet is topic 2.
    function sumGdReceived(receipt, wallet) {
        var total = 0n;
        if (!receipt || !Array.isArray(receipt.logs)) return "0";
        var transferTopic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
        var walletTopic = "0x" + _toLower(wallet).replace(/^0x/, "").padStart(64, "0");
        var gd = _toLower(GD_CONTRACT);
        receipt.logs.forEach(function (log) {
            try {
                if (_toLower(log.address) !== gd) return;
                if (!log.topics || _toLower(log.topics[0]) !== transferTopic) return;
                if (_toLower(log.topics[2]) !== walletTopic) return;
                total += BigInt(log.data);
            } catch (_) {}
        });
        return total.toString();
    }

    // Mark a tracked tx from its receipt. Shared by the live flow and the
    // page-load reconciliation pass so both settle history identically.
    function settleFromReceipt(entry, receipt) {
        if (!receipt) return;
        if (receipt.status === 0 || receipt.status === "0x0") {
            HISTORY.update(entry.id, {
                status: "failed",
                error: "Transaction reverted on-chain.",
                blockNumber: receipt.blockNumber != null ? receipt.blockNumber : null,
            });
            return;
        }
        var received = sumGdReceived(receipt, WALLET);
        HISTORY.update(entry.id, {
            status: "confirmed",
            confirmedAt: Date.now(),
            blockNumber: receipt.blockNumber != null ? receipt.blockNumber : null,
            receivedWei: received,
            error: "",
        });
    }

    function waitForReceipt(hash, onTick) {
        var rp = getReadProvider();
        if (!rp || !HISTORY.isOnchainHash(hash)) return Promise.resolve(null);
        var tries = 0;
        return (function poll() {
            return rp.getTransactionReceipt(hash).then(function (receipt) {
                if (receipt) return receipt;
                tries += 1;
                if (tries >= 50) return null; // ~2.5 min then leave it pending
                if (typeof onTick === "function") onTick(tries);
                return new Promise(function (r) { setTimeout(r, 3000); }).then(poll);
            }).catch(function () {
                tries += 1;
                if (tries >= 50) return null;
                return new Promise(function (r) { setTimeout(r, 3000); }).then(poll);
            });
        })();
    }

    // Page load: any entry left "pending" (the user closed the tab, or the
    // RPC was busy) is re-checked against the chain so history never lies.
    function reconcilePending() {
        var rp = getReadProvider();
        if (!rp) return Promise.resolve();
        var pending = HISTORY.pending();
        if (!pending.length) return Promise.resolve();
        return Promise.all(pending.map(function (entry) {
            return rp.getTransactionReceipt(entry.hash).then(function (receipt) {
                if (receipt) settleFromReceipt(entry, receipt);
            }).catch(function () {});
        })).then(renderHistory);
    }

    // ── on-chain history (the durable, per-wallet source of truth) ──────────
    //
    // The browser-local list is a convenience cache; the CONTRACT holds the
    // permanent record. The backend scans the farm events for the SESSION
    // wallet and returns them, so clearing site data / switching device no
    // longer loses old transactions — they are re-merged here on every load.
    function loadOnchainHistory(opts) {
        opts = opts || {};
        var url = "/farming/api/history" + (opts.force ? "?force=1" : "");
        return fetch(url, { credentials: "same-origin" })
            .then(function (res) {
                if (!res.ok) throw new Error("HTTP " + res.status);
                return res.json();
            })
            .then(function (data) {
                var rows = (data && data.transactions) || [];
                var result = HISTORY.mergeOnchain(rows);
                renderHistory();
                return result;
            })
            .catch(function (err) {
                // Never fatal: the locally tracked rows are still shown.
                if (typeof console !== "undefined" && console.warn) {
                    console.warn("[farm] on-chain history unavailable:", err && err.message);
                }
                return { added: 0, updated: 0, error: true };
            });
    }

    // ── contract calls ──────────────────────────────────────────────────────

    function farmContract(signer) {
        return new ethers.Contract(FARMING_CONTRACT, FARM_ABI, signer);
    }

    function gdContract(runner) {
        return new ethers.Contract(GD_CONTRACT, GD_ABI, runner);
    }

    function _errMessage(err) {
        var formatted = (window.GMTxError && typeof GMTxError.format === "function")
            ? GMTxError.format(err) : "";
        if (formatted) return formatted;
        return (err && (err.shortMessage || err.message)) || "Something went wrong.";
    }

    function setStatus(message, type) {
        var el = $("farmStatus");
        if (!el) return;
        el.textContent = message;
        el.className = "status" + (type ? " " + type : "");
    }

    // The status text this module last wrote. A background poll may only
    // replace a status we own — never the result of an action the user just
    // signed, or an error they still need to read.
    var lastAutoStatus = null;

    function statusOwnedByUs() {
        if (lastAutoStatus === null) return false;
        var el = $("farmStatus");
        return Boolean(el) && el.textContent === lastAutoStatus;
    }

    function farmStatusMessage() {
        if (!farmState) return { text: "", type: "" };
        if (!farmState.active) {
            return { text: "No active farm yet. Start one above to begin earning egg rewards.", type: "" };
        }
        if (farmState.pool < farmState.eggValue) {
            return {
                text: "Active farm (" + HISTORY.formatWei(farmState.principal) + " G$). Rewards are temporarily unavailable — please try again later.",
                type: "error",
            };
        }
        return {
            text: "Active farm: " + HISTORY.formatWei(farmState.principal) + " G$ principal, " +
                farmState.chickens.toString() + " chickens. " +
                (farmState.mature ? "Mature — ready to close." : "Still growing."),
            type: farmState.active ? "ok" : "",
        };
    }

    function applyFarmStatus(silent) {
        if (silent && !statusOwnedByUs()) return;
        var msg = farmStatusMessage();
        if (!msg.text) return;
        setStatus(msg.text, msg.type);
        lastAutoStatus = msg.text;
    }

    function setProgress(step, total) {
        var bar = $("txProgress");
        if (!bar) return;
        if (!step) { bar.classList.remove("show"); bar.textContent = ""; return; }
        bar.classList.add("show");
        bar.textContent = "";
        var span = document.createElement("span");
        span.className = "spinner";
        bar.appendChild(span);
        var label = document.createElement("span");
        label.textContent = "Step " + step + " of " + total;
        bar.appendChild(label);
    }

    function toast(message, type) {
        var t = $("farmToast");
        if (!t) return;
        t.textContent = message;
        t.className = "farm-toast " + (type || "info") + " show";
        clearTimeout(t._delay);
        t._delay = setTimeout(function () { t.classList.remove("show"); }, 3600);
    }

    function updateActionState() {
        var active = Boolean(farmState && farmState.active);
        var legacyUi = document.querySelectorAll("[data-legacy-v1-ui]");
        for (var i = 0; i < legacyUi.length; i += 1) legacyUi[i].hidden = !active;
        var hasContract = Boolean(FARMING_CONTRACT);
        var eggsReady = active && farmState.eggValue > 0n;
        var poolCovers = active && farmState.pool >= farmState.eggValue;
        var canSell = hasContract && eggsReady && poolCovers;
        var canClose = hasContract && active && farmState.mature && poolCovers;

        var sellBtn = $("sellEggsBtn");
        var closeBtn = $("closeFarmBtn");
        if (sellBtn) sellBtn.disabled = !canSell || busy;
        if (closeBtn) closeBtn.disabled = !canClose || busy;
        // Starting legacy V1 farms is permanently disabled; only the verified
        // V2 panel can start a new position.
        var startBtn = $("startFarmBtn");
        if (startBtn) startBtn.disabled = true;

        var banner = $("farmStatusBanner");
        if (banner) {
            if (!active) banner.textContent = "No active legacy V1 farm.";
            else if (!farmState.mature) banner.textContent = "Your chickens are laying eggs. You can sell eggs any time.";
            else banner.textContent = "Your farm is mature! Close it to get your principal + egg rewards. 💰";
        }
    }

    function blockReason(action) {
        if (!FARMING_CONTRACT) return "The farming contract is not configured yet. Please try again later.";
        if (busy) return "Please wait for the current transaction to finish.";
        if (!farmState || !farmState.active) return "No active farm found yet. Please wait a moment for your farm to load.";
        if (action === "close" && !farmState.mature) return "This farm is still growing and cannot be closed yet.";
        // A matured farm whose profit was already sold off still closes — the
        // contract returns the principal with a 0 final profit. Only selling
        // needs eggs to exist.
        if (action === "sell" && farmState.eggValue === 0n) {
            return "There are no egg rewards ready yet. Chickens produce 1 egg per day.";
        }
        if (farmState.pool < farmState.eggValue) {
            return "This action is temporarily unavailable. Please try again later.";
        }
        return "";
    }

    // ── actions ─────────────────────────────────────────────────────────────

    function startFarm() {
        // Defensive guard for legacy markup, scripts, and stale browser state.
        // Never connect a signer or request a V1 approval from this action.
        setStatus("Starting new V1 farms is disabled. Use the separate verified Farming V2 panel; no V1 approval will be requested.", "error");
        return Promise.resolve();
    }

    function sellEggs() {
        if (busy) return Promise.resolve();
        var reason = blockReason("sell");
        if (reason) return Promise.resolve(setStatus(reason, "error"));
        var soldEggs = farmState.eggs;
        var soldValue = farmState.eggValue;

        busy = true; updateActionState();
        v2Processing("Confirm egg sale", "Review and confirm selling your ready eggs in your wallet.");
        setStatus("Confirm selling your eggs in your wallet…");
        setProgress(1, 1);

        var resolved;
        return resolveSigner().then(function (r) {
            resolved = r;
            return ensureCelo(r.provider);
        }).then(function () {
            var farm = farmContract(resolved.signer);
            return farm.sellEggs().then(function (tx) {
                var entry = trackTx("sellEggs", tx.hash, {
                    amountLabel: HISTORY.formatWei(soldValue) + " G$",
                    note: soldEggs.toString() + " eggs",
                });
                v2Processing("Egg sale submitted", "Waiting for the G$ transfer to confirm on Celo.");
                setStatus("Eggs sale sent. Waiting for confirmation… Tx: " + HISTORY.shortHash(tx.hash));
                return waitForReceipt(tx.hash).then(function (receipt) {
                    setProgress(0, 0);
                    if (!receipt) {
                        setStatus("Eggs sale submitted. Confirmation is still pending — it will update in your history below.", "info");
                        return renderHistory();
                    }
                    settleFromReceipt(entry, receipt);
                    renderHistory();
                    if (receipt.status === 0 || receipt.status === "0x0") {
                        setStatus("The sale reverted on-chain. Your eggs are still safe — see history below.", "error");
                        return refreshFarm();
                    }
                    var received = sumGdReceived(receipt, WALLET);
                    var paidGd = HISTORY.formatWei(received);
                    setStatus(describeSellSuccess(paidGd) + " Tx: " + HISTORY.shortHash(tx.hash), "ok");
                    toast("🥚 " + paidGd + " G$ received!", "success");
                    return refreshFarm();
                });
            });
        }).catch(function (err) {
            setProgress(0, 0);
            setStatus(_isUserRejection(err) ? "You cancelled the wallet request." : _errMessage(err), "error");
        }).then(function () {
            v2Processing("", "");
            busy = false;
            updateActionState();
        });
    }

    function closeFarm() {
        if (busy) return Promise.resolve();
        var reason = blockReason("close");
        if (reason) return Promise.resolve(setStatus(reason, "error"));
        var expectedPrincipal = farmState.principal;
        var expectedProfit = farmState.eggValue;
        var expectedTotal = expectedPrincipal + expectedProfit;

        busy = true; updateActionState();
        v2Processing("Confirm farm close", "Review and confirm closing your matured farm and withdrawing its balance.");
        setStatus("Confirm closing your farm in your wallet…");
        setProgress(1, 1);

        var resolved;
        return resolveSigner().then(function (r) {
            resolved = r;
            return ensureCelo(r.provider);
        }).then(function () {
            var farm = farmContract(resolved.signer);
            return farm.closeFarm().then(function (tx) {
                var entry = trackTx("closeFarm", tx.hash, {
                    amountLabel: HISTORY.formatWei(expectedTotal) + " G$",
                    note: "Principal + egg rewards",
                });
                v2Processing("Farm close submitted", "Waiting for the principal and rewards withdrawal to confirm on Celo.");
                setStatus("Farm closing sent. Waiting for confirmation… Tx: " + HISTORY.shortHash(tx.hash));
                return waitForReceipt(tx.hash).then(function (receipt) {
                    setProgress(0, 0);
                    if (!receipt) {
                        setStatus("Farm close submitted. Confirmation is still pending — it will update in your history below.", "info");
                        return renderHistory();
                    }
                    settleFromReceipt(entry, receipt);
                    renderHistory();
                    if (receipt.status === 0 || receipt.status === "0x0") {
                        setStatus("The close transaction reverted on-chain. Your farm is still active — see history below.", "error");
                        return refreshFarm();
                    }
                    var received = sumGdReceived(receipt, WALLET);
                    var paidGd = HISTORY.formatWei(received);
                    HISTORY.update(entry.id, { amountLabel: paidGd + " G$", note: "Paid out to wallet" });
                    setStatus(describeCloseSuccess(paidGd) + " Tx: " + HISTORY.shortHash(tx.hash), "ok");
                    showCloseModal(paidGd, tx.hash);
                    return refreshFarm();
                });
            });
        }).catch(function (err) {
            setProgress(0, 0);
            setStatus(_isUserRejection(err) ? "You cancelled the wallet request." : _errMessage(err), "error");
        }).then(function () {
            v2Processing("", "");
            busy = false;
            updateActionState();
        });
    }

    // ── rendering ───────────────────────────────────────────────────────────

    function showCloseModal(amountGd, hash) {
        var modal = $("closeSuccessModal");
        if (!modal) return;
        var amountEl = $("closeSuccessAmount");
        if (amountEl) amountEl.textContent = HISTORY.formatGd(amountGd) + " G$";
        var linkEl = $("closeSuccessTx");
        if (linkEl) {
            var url = HISTORY.celoscanUrl(hash);
            if (url) {
                linkEl.href = url;
                linkEl.textContent = HISTORY.shortHash(hash, 14);
                linkEl.parentElement.style.display = "";
            } else {
                linkEl.parentElement.style.display = "none";
            }
        }
        modal.classList.add("open");
    }

    function closeCloseModal() {
        var modal = $("closeSuccessModal");
        if (modal) modal.classList.remove("open");
    }

    function renderHistory() {
        var list = $("farmHistoryList");
        if (!list) return;
        var items = HISTORY.all();
        var panel = $("legacyV1HistoryPanel");
        if (panel) panel.hidden = items.length === 0;
        var empty = $("farmHistoryEmpty");
        if (!items.length) {
            list.innerHTML = "";
            if (empty) empty.style.display = "";
            return;
        }
        if (empty) empty.style.display = "none";
        list.innerHTML = items.map(function (e) {
            var status = e.status;
            var chip, chipClass;
            if (status === "confirmed") { chip = "✅ Confirmed"; chipClass = "ok"; }
            else if (status === "failed") { chip = "❌ Failed"; chipClass = "error"; }
            else { chip = "⏳ Pending"; chipClass = "pending"; }

            var url = HISTORY.celoscanUrl(e.hash);
            var hashHtml = url
                ? '<a class="tx-hash" href="' + url + '" target="_blank" rel="noopener">' + HISTORY.shortHash(e.hash, 12) + ' ↗</a>'
                : '<span class="tx-hash muted">No hash yet</span>';

            var receivedNote = "";
            // Only show "G$ received" for actions that pay the user. Start-farm
            // and approve move G$ TO the contract, so a 0 there would read as a
            // failure when nothing is wrong.
            var paysUser = e.action === "closeFarm" || e.action === "sellEggs";
            if (status === "confirmed" && paysUser && e.receivedWei) {
                receivedNote = '<div class="tx-received">G$ received: ' + HISTORY.formatWei(e.receivedWei) + ' G$</div>';
            }
            var errorNote = e.error ? '<div class="tx-error">' + escapeHtml(e.error) + '</div>' : "";

            return '<div class="tx-row ' + chipClass + '">' +
                '<div class="tx-main">' +
                    '<div class="tx-top"><span class="tx-action">' + escapeHtml(e.actionLabel) + '</span>' +
                    '<span class="tx-status ' + chipClass + '">' + chip + '</span></div>' +
                    '<div class="tx-meta">' +
                        (e.amountLabel ? '<span class="tx-amount">' + escapeHtml(e.amountLabel) + '</span>' : '') +
                        '<span class="tx-time">' + escapeHtml(e.timeLabel || HISTORY.formatTime(e.confirmedAt || e.createdAt)) + '</span>' +
                    '</div>' +
                    receivedNote + errorNote +
                    '<div class="tx-foot">' + hashHtml + '</div>' +
                '</div>' +
            '</div>';
        }).join("");
    }

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function updateChickensToBuy() {
        var input = $("farmAmount");
        var out = $("chickensToBuy");
        if (!input || !out) return;
        var amount = Number(input.value || 0);
        var chickens = Math.floor(amount / CHICKEN_PRICE);
        out.textContent = isFinite(chickens) && chickens > 0 ? chickens.toLocaleString() : "0";
        var projected = $("projectedProfit");
        if (projected) {
            var profit = projectedMonthlyProfit(amount);
            projected.textContent = HISTORY.formatGd(profit) + " G$";
        }
        var payout = $("projectedPayout");
        if (payout) {
            payout.textContent = HISTORY.formatGd(amount + projectedMonthlyProfit(amount)) + " G$";
        }
    }

    // The farm scene reflects the REAL contract, never a decorative count: one
    // visible bird per chicken owned (capped at the drawn flock), the nest
    // fills with the eggs actually ready to sell, and an empty pasture before
    // the user starts. Eggs pop ONCE when the count grows — an endless trickle
    // would imply eggs accrue in real time, but the contract pays 1/chicken/day.
    var sceneEggsShown = null;

    function renderScene() {
        var scene = $("farmScene");
        if (!scene) return;
        var v1Active = Boolean(farmState && farmState.active);
        var v2Position = v2 && v2.position && v2.position.active ? v2.position : null;
        var useV2 = !v1Active && Boolean(v2Position);
        var active = v1Active || Boolean(v2Position);
        var v2Maturity = v2Position ? Number(v2Position.matures_at || 0) : 0;
        var mature = v1Active ? Boolean(farmState.mature) : Boolean(v2Position && v2Maturity && Date.now() >= v2Maturity * 1000);
        var v2Principal = useV2 ? Number(v2Position.principal_gd || v2Position.principal || 0) : 0;
        // V2 stores principal, not chicken NFTs: show the legacy 100 G$ per
        // chicken visual convention, and map claimable reward progress to
        // illustrative reward eggs. These are visuals, not V2 contract units.
        var chickens = v1Active ? Number(farmState.chickens) : (useV2 ? Math.floor(v2Principal / 100) : 0);
        var eggsReady = v1Active ? Number(farmState.eggs) : 0;
        if (useV2) {
            var p = v2Params(), cap = v2Principal * p.bps / 10000;
            var claimable = Number(v2Position.claimable_reward || 0);
            var rewardPerIllustratedEgg = chickens > 0 && p.days > 0 ? cap / p.days / chickens : 0;
            eggsReady = rewardPerIllustratedEgg > 0 ? Math.floor(claimable / rewardPerIllustratedEgg) : 0;
        }

        scene.classList.toggle("is-idle", !active);
        scene.classList.toggle("is-mature", mature);

        var flock = scene.querySelectorAll(".farm-chicken");
        for (var i = 0; i < flock.length; i++) {
            flock[i].classList.toggle("is-off", i >= chickens);
        }

        var nestEggs = scene.querySelectorAll(".chicken-egg");
        var grew = sceneEggsShown !== null && eggsReady > sceneEggsShown;
        var newest = Math.min(eggsReady, nestEggs.length) - 1;
        for (var j = 0; j < nestEggs.length; j++) {
            var shown = j < eggsReady;
            nestEggs[j].classList.toggle("is-hidden", !shown);
            if (!shown) {
                nestEggs[j].classList.remove("is-pop");
            } else if (grew && j === newest) {
                nestEggs[j].classList.remove("is-pop");
                void nestEggs[j].offsetWidth;   // force a reflow so it re-triggers
                nestEggs[j].classList.add("is-pop");
            }
        }
        sceneEggsShown = eggsReady;

        var caption = $("sceneCaption");
        if (caption) {
            if (!active) caption.textContent = "Start a farm to see your chickens";
            else if (useV2) caption.textContent = chickens.toLocaleString() + " V2 chickens · " + eggsReady + " reward eggs (visual)" + (mature ? " · cycle complete — collect G$ rewards" : "");
            else if (mature) caption.textContent = chickens.toLocaleString() + " chickens · cycle complete — close to collect";
            else caption.textContent = chickens.toLocaleString() + " chickens · " + eggsReady + " eggs ready";
        }
        scene.setAttribute("aria-label", !active
            ? "Your farm — start a farm to see your chickens"
            : (useV2 ? "V2 visual representation: " : "") + chickens.toLocaleString() + " chickens with " + eggsReady +
              (useV2 ? " illustrative reward eggs" : " eggs ready") + (mature ? ", cycle complete" : ""));
    }

    function renderFarm() {
        var active = Boolean(farmState && farmState.active);
        var principalEl = $("principalValue");
        var chickenEl = $("chickenCount");
        var eggsEl = $("eggsReady");
        var eggValueEl = $("eggValue");
        var statusEl = $("farmPhaseValue");
        var progressFill = $("cycleProgressFill");
        var progressLabel = $("cycleProgressLabel");

        if (chickenEl) chickenEl.textContent = active ? farmState.chickens.toString() : "—";
        if (principalEl) principalEl.textContent = active ? HISTORY.formatWei(farmState.principal) + " G$" : "—";
        if (eggsEl) eggsEl.textContent = active ? farmState.eggs.toString() : "—";
        if (eggValueEl) eggValueEl.textContent = active ? eggValueLabel(farmState.eggValue) : "—";

        if (statusEl) {
            if (!active) statusEl.textContent = "No farm";
            else if (farmState.mature) statusEl.textContent = "Mature 🌾";
            else statusEl.textContent = "Growing 🌱";
        }

        if (progressFill && progressLabel) {
            if (!active) {
                progressFill.style.width = "0%";
                progressLabel.textContent = "Start a farm to begin your " + FARM_DAYS + "-day cycle.";
            } else {
                var now = Math.floor(Date.now() / 1000);
                var span = farmState.unlocksAt - farmState.startedAt;
                var done = Math.min(Math.max(now - farmState.startedAt, 0), span);
                var pct = span > 0 ? Math.round((done / span) * 100) : 100;
                progressFill.style.width = pct + "%";
                var left = Math.max(farmState.unlocksAt - now, 0);
                var days = Math.floor(left / 86400);
                var hours = Math.floor((left % 86400) / 3600);
                progressLabel.textContent = farmState.mature
                    ? "Cycle complete — ready to close!"
                    : pct + "% · " + days + "d " + hours + "h until maturity";
            }
        }

        renderScene();
    }

    function refreshFarm(opts) {
        opts = opts || {};
        var silent = Boolean(opts.silent);
        // A background poll that is still running when the user taps Refresh
        // must not swallow the tap: join the in-flight read and then re-read,
        // so the explicit request always observes a fresh value.
        if (refreshing) {
            if (silent) return refreshing;
            return refreshing.then(function () { return refreshFarm(opts); });
        }
        if (!FARMING_CONTRACT) {
            if (!silent) setStatus("The farming contract is not configured yet. Please try again later.", "error");
            return Promise.resolve();
        }
        var reader = getReadProvider();
        if (!reader) {
            if (!silent) setStatus("Could not reach the Celo network. Please check your connection.", "error");
            return Promise.resolve();
        }
        var farm = farmContract(reader);
        var done;
        refreshing = new Promise(function (resolve) { done = resolve; });
        return farm.getFarm(WALLET).then(function (result) {
            return farm.rewardPool().then(function (pool) {
                farmState = {
                    principal: BigInt(result[0]),
                    chickens: BigInt(result[1]),
                    startedAt: Number(result[2]),
                    unlocksAt: Number(result[4]),
                    claimedProfit: BigInt(result[5]),
                    active: Boolean(result[6]),
                    mature: Boolean(result[7]),
                    eggs: BigInt(result[8]),
                    eggValue: BigInt(result[9]),
                    pool: BigInt(pool),
                };
                renderFarm();
                updateActionState();
                // A background poll may only replace a status this module
                // owns; it must never overwrite the outcome of the action the
                // user just signed, or an error they still need to read.
                applyFarmStatus(silent);
                return farmState;
            });
        }).catch(function (err) {
            if (!silent) setStatus("Could not load farm status. " + _errMessage(err), "error");
            return farmState;
        }).then(function (result) {
            refreshing = null;
            done(result);
            return result;
        });
    }

    // ── boot ────────────────────────────────────────────────────────────────

    function startTick() {
        if (tickTimer) clearInterval(tickTimer);
        tickTimer = setInterval(function () {
            if (farmState && farmState.active) renderFarm();
        }, 1000);
    }

    // Background refresh: the farm state (and periodically the on-chain
    // history) is re-read on its own so an active farm is visible the moment
    // the page opens and stays current without a manual Refresh tap. Skipped
    // while a transaction is in flight (busy) so the poll can't fight the
    // action's own state, and skipped while the tab is hidden so a phone left
    // in the background isn't making RPC calls all night.
    function pollOnce() {
        if (busy || refreshing) return Promise.resolve();
        if (typeof document !== "undefined" && document.hidden) return Promise.resolve();
        pollTicks += 1;
        var tasks = [refreshFarm({ silent: true })];
        if (pollTicks % HISTORY_EVERY_TICKS === 0) {
            // Not forced: the backend caches the log scan for ~60s, so a poll
            // that lands on a warm cache is a cheap no-op. The explicit "Check
            // on-chain status" button is what forces a fresh scan.
            tasks.push(loadOnchainHistory().then(reconcilePending));
        }
        return Promise.all(tasks);
    }

    function startPolling() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(pollOnce, POLL_MS);
    }

    // Returning to the tab (or the window regaining focus) should feel instant:
    // refresh right away instead of waiting out the rest of the interval.
    function onVisible() {
        if (typeof document !== "undefined" && document.hidden) return;
        pollOnce();
    }

    // ── Farming V2 (isolated contract; never reuse the V1 address) ──────────
    var v2MaturityTimer = null;
    var v2 = { enabled: false, busy: false, address: "", token: GD_CONTRACT, position: null, positionChecked: false, explorer: "https://celoscan.io" };
    var V2_ABI = [
        "function startFarm(uint256 amount)", "function claimReward()", "function closeFarm()",
        "function fundRewardPool(uint256 amount)",
        "function farms(address) view returns (uint256 principal,uint256 maxReward,uint256 claimedReward,uint64 startedAt,bool active)",
        "function accruedReward(address) view returns (uint256)", "function claimableReward(address) view returns (uint256)",
    ];
    function v2Element(id) { return $(id); }
    function v2Status(message, kind) {
        var el = v2Element("farmingV2Status"); if (el) { el.textContent = message; el.className = "status " + (kind || "info"); }
    }
    function v2TxStatus(message, kind, hash) {
        var el = v2Element("v2TxStatus"); if (!el) return;
        el.hidden = false; el.className = "status " + (kind || "info"); el.textContent = message;
        if (hash) { var a = document.createElement("a"); a.href = v2.explorer.replace(/\/$/, "") + "/tx/" + hash; a.target = "_blank"; a.rel = "noopener"; a.textContent = " View transaction"; el.appendChild(a); }
    }
    var v2GasRequestMemory = null;
    function v2Processing(title, detail) {
        var overlay = v2Element("farmProcessingOverlay");
        if (!overlay) return;
        if (!title) { overlay.classList.remove("show"); overlay.hidden = true; overlay.setAttribute("aria-hidden", "true"); overlay.setAttribute("aria-busy", "false"); return; }
        var titleEl = v2Element("farmProcessingTitle"), detailEl = v2Element("farmProcessingDetail");
        if (titleEl) titleEl.textContent = title;
        if (detailEl) detailEl.textContent = detail || "Please keep this page open while the request completes.";
        overlay.hidden = false; overlay.setAttribute("aria-hidden", "false"); overlay.setAttribute("aria-busy", "true"); overlay.classList.add("show");
    }
    function v2GasRequestId(amountWei) {
        var principal = amountWei.toString();
        var storageKey = "kwentofiFarmingV2GasRequest:" + String(WALLET || "").toLowerCase();
        var record = v2GasRequestMemory;
        try { record = JSON.parse(window.sessionStorage.getItem(storageKey) || "null") || record; } catch (_) {}
        if (record && record.request_id) {
            if (String(record.principal_wei) !== principal) throw new Error("A previous gas-support request is still being reconciled. Retry the same farm amount first.");
            v2GasRequestMemory = record;
            return record.request_id;
        }
        if (!window.crypto || typeof window.crypto.getRandomValues !== "function") throw new Error("Secure request IDs are unavailable. Refresh the page and try again.");
        var bytes = new Uint8Array(32); window.crypto.getRandomValues(bytes);
        var requestId = "0x" + Array.from(bytes).map(function (value) { return value.toString(16).padStart(2, "0"); }).join("");
        v2GasRequestMemory = { principal_wei: principal, request_id: requestId };
        try { window.sessionStorage.setItem(storageKey, JSON.stringify(v2GasRequestMemory)); } catch (_) {}
        return requestId;
    }
    function v2ClearGasRequest(requestId) {
        var storageKey = "kwentofiFarmingV2GasRequest:" + String(WALLET || "").toLowerCase();
        if (v2GasRequestMemory && v2GasRequestMemory.request_id === requestId) v2GasRequestMemory = null;
        try {
            var saved = JSON.parse(window.sessionStorage.getItem(storageKey) || "null");
            if (saved && saved.request_id === requestId) window.sessionStorage.removeItem(storageKey);
        } catch (_) {}
    }
    function v2PrepareGasSupport(amountWei) {
        var requestId = v2GasRequestId(amountWei), reader = getReadProvider();
        if (!reader || typeof reader.waitForTransaction !== "function") throw new Error("Celo read provider is unavailable. Please retry before starting the farm.");
        v2Processing("Checking gas support…", "Estimating the approval and farm-start gas. No farm transaction is sent until you confirm it in your wallet.");
        return fetch("/api/farming/v2/gas-sponsor", {
            method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ principal_wei: amountWei.toString(), request_id: requestId })
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                var hash = String(data.sponsor_tx_hash || "");
                if ((!response.ok || !data.success) && hash) data.sponsored = true;
                if (!response.ok || !data.success) {
                    if (hash) return { data: data, hash: hash };
                    v2TxStatus("Gas sponsorship is unavailable. Your wallet can continue only if it has enough CELO to pay the transaction fees. " + String(data.error || ""), "info");
                    v2Processing("Gas check complete", "Automatic gas support is unavailable. Your wallet will need enough CELO for approval and farm start.");
                    return null;
                }
                if (!data.sponsored) {
                    v2ClearGasRequest(requestId);
                    v2Processing("Gas check complete", "Your wallet will pay its own estimated Celo transaction fees.");
                    return null;
                }
                if (!/^0x[0-9a-fA-F]{64}$/.test(hash)) throw new Error("The gas top-up response did not include a valid Celo transaction hash.");
                return { data: data, hash: hash };
            });
        }).then(function (pending) {
            if (!pending) return null;
            var amountText = String(pending.data.amount_celo || pending.data.estimated_fee_celo || "");
            v2Processing("Gas top-up sent", "Waiting for the " + (amountText ? amountText + " CELO " : "") + "top-up to confirm on Celo before asking you to sign.");
            v2TxStatus("Gas top-up sent. Waiting for Celo confirmation…", "info", pending.hash);
            return reader.waitForTransaction(pending.hash, 1, 120000).then(function (receipt) {
                if (!receipt || Number(receipt.status) !== 1) throw new Error("The gas top-up is not confirmed yet. Retry the same farm amount; do not start another transaction.");
                v2ClearGasRequest(requestId);
                v2Processing("Gas top-up confirmed", "Now checking G$ approval and preparing your farm-start transaction.");
                return receipt;
            });
        });
    }
    function v2Normalize(data) {
        // Backend contract expected: {v2:{enabled,verified,contract/address,token,chain_id,
        // min_farm_gd,max_farm_gd,step_gd,duration_seconds,reward_bps,explorer_url,
        // funding_status,active_farm:{principal,started_at,matures_at,accrued_reward,
        // claimable_reward,claimed_reward,active,mature}}}. Accept config.v2/farming_v2 too.
        var cfg = data && (data.v2 || data.farming_v2 || (data.config && (data.config.v2 || data.config.farming_v2)) ||
            (String(data.version || "").toLowerCase() === "v2" ? data : null));
        if (!cfg || typeof cfg !== "object") return null;
        return cfg;
    }
    function v2Params() {
        var c = v2.config || {}, raw = c.params || c;
        var decimals = Number(c.token_decimals || 18), unit = Math.pow(10, decimals);
        return {
            min: Number(c.min_farm_gd || c.min_principal || (raw.min_principal_wei ? Number(raw.min_principal_wei) / unit : 1000)),
            max: Number(c.max_farm_gd || c.max_principal || (raw.max_principal_wei ? Number(raw.max_principal_wei) / unit : 20000)),
            step: Number(c.step_gd || c.amount_increment || (raw.principal_increment_wei ? Number(raw.principal_increment_wei) / unit : 100)),
            days: Number(c.farm_days || c.duration_days || (raw.duration_seconds ? Number(raw.duration_seconds) / 86400 : 7)),
            bps: Number(raw.reward_bps || c.reward_bps || (Number(c.reward_percent || 5) * 100)),
        };
    }
    function v2Estimate() {
        var input = v2Element("v2FarmAmount"), amount = Number(input && input.value), p = v2Params();
        var valid = Number.isFinite(amount) && amount >= p.min && amount <= p.max && p.step > 0 && amount % p.step === 0;
        var reward = valid ? Math.floor(amount * p.bps / 10000) : 0;
        var est = v2Element("v2RewardEstimate"), total = v2Element("v2TotalEstimate");
        if (est) est.textContent = "Up to " + (valid ? reward.toLocaleString() : "—") + " G$";
        if (total) total.textContent = (valid ? (amount + reward).toLocaleString() : "—") + " G$";
        var start = v2Element("v2StartBtn"); if (start) start.disabled = !v2.enabled || !v2.positionChecked || !valid || Boolean(v2.position && v2.position.active) || v2.busy;
        return valid ? { amount: amount, reward: reward } : null;
    }
    function v2UnavailableMessage(cfg) {
        if (Number(cfg && cfg.chain_id || CHAIN_ID) !== CHAIN_ID) {
            return "Farming V2 is configured for a different Celo chain.";
        }
        var messages = {
            disabled_or_unconfigured: "The server has not enabled or fully configured Farming V2.",
            chain_mismatch: "The configured V2 deployment is on a different chain.",
            contract_code_missing: "No V2 contract code was found at the configured Celo address.",
            token_mismatch: "The V2 contract's G$ token does not match the configured token.",
            signer_mismatch: "The V2 contract signer does not match the configured signer.",
            parameters_mismatch: "The on-chain V2 terms do not match the reviewed app settings.",
            admin_key_mismatch: "The server-side V2 signing key does not match the configured signer.",
            verification_failed: "The backend could not verify the V2 contract over Celo RPC."
        };
        return messages[String(cfg && cfg.reason || "")] || "The backend has not verified the Farming V2 contract.";
    }
    function v2ApplyConfig(data) {
        var cfg = v2Normalize(data), badge = v2Element("farmingV2Badge"), fund = v2Element("v2FundBtn");
        if (!cfg) { v2.enabled = false; v2.position = null; v2.positionChecked = false;
            if (badge) badge.textContent = "V2 · unavailable / unverified";
            if (fund) fund.disabled = true;
            v2Status("V2 status unavailable or unverified. New farms and contributions are disabled until the backend verifies V2.", "info");
            v2RenderPosition(); v2Estimate(); return; }
        v2.config = cfg;
        // Require positive backend verification and explicit enablement. No V1 fallback.
        v2.enabled = cfg.enabled === true && cfg.verified === true && Boolean(cfg.contract_address || cfg.contract || cfg.address) && Number(cfg.chain_id || CHAIN_ID) === CHAIN_ID;
        v2.address = String(cfg.contract_address || cfg.contract || cfg.address || "");
        v2.token = String(cfg.token_address || cfg.gd_contract || cfg.token || GD_CONTRACT);
        v2.explorer = String(cfg.explorer_url || "https://celoscan.io");
        v2.position = cfg.active_farm || cfg.position || null;
        v2.positionChecked = Boolean(v2.position);
        if (badge) badge.textContent = v2.enabled ? "V2 · verified and enabled" : "V2 · unavailable / unverified";
        var notice = v2Element("v2ContractNotice");
        if (notice) {
            notice.textContent = v2.address ? "V2 destination contract: " + v2.address : "V2 destination contract: unavailable until backend verification.";
            if (v2.address && (cfg.explorer_url || cfg.contract_explorer_url)) {
                var link = document.createElement("a"); link.href = String(cfg.contract_explorer_url || cfg.explorer_url); link.target = "_blank"; link.rel = "noopener"; link.textContent = " View contract"; notice.appendChild(link);
            }
        }
        var funding = String(cfg.funding_status || cfg.sponsor_funding_status || cfg.reason || "").toLowerCase();
        var status = v2.enabled ? (funding.indexOf("insufficient") >= 0 ? "New V2 farms are temporarily unavailable." : funding.indexOf("unverified") >= 0 || funding.indexOf("unknown") >= 0 ? "V2 availability could not be confirmed; new farms are unavailable." : "V2 is ready. New farms are available when rewards can be reserved.") : "V2 is unavailable or still being verified. " + v2UnavailableMessage(cfg) + " New farms are disabled.";
        v2Status(status, v2.enabled ? "info" : "error");
        if (fund) fund.disabled = !v2.enabled || v2.busy;
        var p = v2Params(), amountInput = v2Element("v2FarmAmount");
        if (amountInput) { amountInput.min = p.min; amountInput.max = p.max; amountInput.step = p.step; }
        v2RenderPosition(); v2Estimate();
    }
    function v2RenderPosition() {
        var pos = v2.position, card = v2Element("v2Position");
        renderScene();
        if (!card) return;
        if (v2MaturityTimer !== null) { clearTimeout(v2MaturityTimer); v2MaturityTimer = null; }
        card.hidden = !(pos && pos.active);
        if (!pos || !pos.active) { ["v2ClaimBtn", "v2CloseBtn"].forEach(function (id) { var b = v2Element(id); if (b) { b.disabled = true; if (id === "v2CloseBtn") b.hidden = true; } }); return; }
        var set = function (id, value) { var el = v2Element(id); if (el) el.textContent = value; };
        var maturity = Number(pos.matures_at || pos.maturity_timestamp || pos.unlocks_at || 0);
        var reward = Number(pos.claimable_reward !== undefined ? pos.claimable_reward : (pos.accrued_reward !== undefined ? pos.accrued_reward : (pos.pending_reward || 0)));
        set("v2Principal", String(pos.principal_gd || pos.principal || "—") + " G$");
        set("v2Accrued", reward.toLocaleString() + " G$"); set("v2Claimed", String(pos.claimed_reward_gd || pos.claimed_reward || "0") + " G$");
        set("v2Maturity", maturity ? new Date(maturity * 1000).toLocaleString() : "Not verified");
        var mature = pos.mature === true || (maturity > 0 && Date.now() >= maturity * 1000);
        set("v2FarmPhase", mature ? "Mature" : "Active · no early withdrawal");
        var claim = v2Element("v2ClaimBtn"), close = v2Element("v2CloseBtn");
        if (claim) claim.disabled = !v2.enabled || reward <= 0 || v2.busy;
        if (close) { close.hidden = !mature; close.disabled = !v2.enabled || !mature || v2.busy; }
        if (!mature && maturity > 0) {
            var untilMaturity = Math.max(0, maturity * 1000 - Date.now());
            v2MaturityTimer = setTimeout(function () { v2MaturityTimer = null; v2RenderPosition(); }, Math.min(untilMaturity, 2147480000));
        }
    }
    function v2RenderHistory(rows, unavailable) {
        var list = v2Element("v2HistoryList"), empty = v2Element("v2HistoryEmpty");
        if (!list || !empty) return;
        list.textContent = "";
        rows = Array.isArray(rows) ? rows : [];
        empty.hidden = rows.length > 0 && !unavailable;
        empty.textContent = unavailable ? "V2 history is unavailable until the backend verifies the deployed contract." :
            (rows.length ? "" : "No V2 transactions found for this wallet yet.");
        rows.forEach(function (entry) {
            var card = document.createElement("div"); card.className = "tx-row ok";
            var top = document.createElement("div"); top.className = "tx-top";
            var label = document.createElement("span"); label.className = "tx-action"; label.textContent = String(entry.actionLabel || entry.action || "V2 transaction");
            var badge = document.createElement("span"); badge.className = "tx-status ok"; badge.textContent = "V2 · on-chain";
            top.appendChild(label); top.appendChild(badge); card.appendChild(top);
            var meta = document.createElement("div"); meta.className = "tx-meta";
            if (entry.amountLabel) { var amount = document.createElement("span"); amount.className = "tx-amount"; amount.textContent = String(entry.amountLabel); meta.appendChild(amount); }
            var block = Number(entry.blockNumber || 0); var time = document.createElement("span"); time.className = "tx-time";
            time.textContent = entry.timeLabel || (block > 0 ? "Block #" + block : "On-chain"); meta.appendChild(time); card.appendChild(meta);
            var hash = String(entry.hash || "");
            if (/^0x[0-9a-fA-F]{64}$/.test(hash)) {
                var foot = document.createElement("div"); foot.className = "tx-foot";
                var link = document.createElement("a"); link.className = "tx-hash"; link.href = v2.explorer.replace(/\/$/, "") + "/tx/" + hash;
                link.target = "_blank"; link.rel = "noopener"; link.textContent = HISTORY.shortHash(hash, 12) + " ↗";
                foot.appendChild(link); card.appendChild(foot);
            }
            list.appendChild(card);
        });
    }
    function v2LoadHistory() {
        if (!WALLET || !v2.enabled) { v2RenderHistory([], true); return Promise.resolve([]); }
        return fetch("/farming/api/v2/history", { credentials: "same-origin" }).then(function (response) {
            if (!response.ok) throw new Error("V2 history unavailable.");
            return response.json();
        }).then(function (data) {
            if (data.version !== "v2" || data.verified !== true) throw new Error("V2 history is not verified.");
            v2RenderHistory(data.transactions || [], false); return data.transactions || [];
        }).catch(function () { v2RenderHistory([], true); return []; });
    }
    function v2Refresh() { return fetch("/farming/api/v2/config", { credentials: "same-origin" }).then(function (r) { if (!r.ok) throw new Error("V2 configuration request failed."); return r.json(); }).then(function (data) {
        v2ApplyConfig(data);
        // Read only this wallet's V2 mapping and accrual from the verified V2 contract.
        // Never call reward-pool getters or render an exact sponsor balance.
        if (!v2.enabled || !WALLET || !ethers || !v2.address) { v2RenderHistory([], true); return; }
        v2LoadHistory();
        var reader = getReadProvider(); if (!reader) return;
        var c = new ethers.Contract(v2.address, V2_ABI, reader);
        return Promise.all([c.farms(WALLET), c.accruedReward(WALLET), c.claimableReward(WALLET)]).then(function (r) {
            var f = r[0], active = Boolean(f.active !== undefined ? f.active : f[4]);
            if (!active) { v2.position = null; v2.positionChecked = true; v2RenderPosition(); v2Estimate(); return; }
            var started = Number(f.startedAt !== undefined ? f.startedAt : f[3]);
            var claimable = Number(ethers.formatUnits(r[2], Number(v2.config.token_decimals || 18)));
            var accrued = Number(ethers.formatUnits(r[1], Number(v2.config.token_decimals || 18)));
            var principal = Number(ethers.formatUnits(f.principal !== undefined ? f.principal : f[0], Number(v2.config.token_decimals || 18)));
            var claimed = Number(ethers.formatUnits(f.claimedReward !== undefined ? f.claimedReward : f[2], Number(v2.config.token_decimals || 18)));
            v2.position = { active: true, principal_gd: principal, accrued_reward: accrued, claimable_reward: claimable,
                claimed_reward_gd: claimed, started_at: started, matures_at: started + v2Params().days * 86400 };
            v2.positionChecked = true;
            v2RenderPosition(); v2Estimate();
        });
    }).catch(function () { v2.enabled = false; v2Status("V2 status unavailable or unverified. Actions are disabled; no V1 fallback is used.", "error"); v2Estimate(); }); }
    function v2Send(action) {
        if (v2.busy || !v2.enabled || !v2.address) return Promise.resolve();
        var estimate = action === "start" ? v2Estimate() : null;
        if (action === "start" && (!estimate || !v2.positionChecked || (v2.position && v2.position.active))) { v2TxStatus("Enter a valid amount and wait for the V2 contract to confirm you have no active farm.", "error"); return Promise.resolve(); }
        var fundAmount = Number((v2Element("v2FundAmount") || {}).value);
        if (action === "fund" && (!Number.isFinite(fundAmount) || fundAmount <= 0)) { v2TxStatus("Enter a valid contribution amount.", "error"); return Promise.resolve(); }
        v2.busy = true; ["v2StartBtn", "v2ClaimBtn", "v2CloseBtn", "v2FundBtn"].forEach(function (id) { var b = v2Element(id); if (b) b.disabled = true; });
        var amount = action === "start" ? estimate.amount : fundAmount;
        var amountWei = amount ? ethers.parseUnits(String(amount), GD_DECIMALS) : null;
        var resolved, token;
        var actionLabels = { start: "farm start", claim: "reward claim", close: "mature farm close", fund: "sponsor contribution" };
        v2Processing("Preparing your V2 " + actionLabels[action] + "…", "Checking your wallet connection and Celo network.");
        v2TxStatus(action === "fund" ? "Review contribution " + amount + " G$. Non-refundable; no personal reward. Approve in your wallet." : "Waiting for wallet confirmation…", "info");
        return resolveSigner().then(function (r) { resolved = r; return ensureCelo(r.provider); }).then(function () {
            if (action !== "start") return null;
            return v2PrepareGasSupport(amountWei);
        }).then(function () {
            var contract = new ethers.Contract(v2.address, V2_ABI, resolved.signer);
            if (action === "start" || action === "fund") {
                token = new ethers.Contract(v2.token, GD_ABI, resolved.signer);
                return token.allowance(WALLET, v2.address).then(function (allowance) {
                    if (BigInt(allowance) >= BigInt(amountWei)) return null;
                    v2Processing("Approve G$ spending", action === "start"
                        ? "Confirm the exact approval in your wallet. The farm will start only after this approval confirms."
                        : "Confirm the exact approval in your wallet. The sponsor contribution will continue after it confirms.");
                    v2TxStatus("Approve G$ spending for verified V2 only. Confirm approval in your wallet…", "info");
                    return token.approve(v2.address, amountWei).then(function (tx) {
                        v2TxStatus("Approval pending: " + HISTORY.shortHash(tx.hash), "info", tx.hash);
                        v2Processing("G$ approval submitted", "Waiting for Celo to confirm the approval transaction.");
                        return tx.wait().then(function (receipt) {
                            if (!receipt || Number(receipt.status) !== 1) throw new Error("G$ approval failed or could not be confirmed.");
                            v2Processing("Approval confirmed", action === "start" ? "Preparing the V2 farm-start transaction." : "Preparing the sponsor contribution transaction.");
                            return receipt;
                        });
                    });
                }).then(function () {
                    v2Processing(action === "start" ? "Confirm farm start" : "Confirm sponsor contribution",
                        action === "start" ? "Review and confirm the V2 farm transaction in your wallet."
                            : "Review and confirm the non-refundable sponsor contribution in your wallet.");
                    v2TxStatus(action === "fund" ? "Confirm sponsor funding in your wallet…" : "Confirm V2 farm start in your wallet…", "info");
                    return action === "fund" ? contract.fundRewardPool(amountWei) : contract.startFarm(amountWei);
                });
            }
            v2Processing(action === "claim" ? "Confirm reward claim" : "Confirm mature farm close",
                action === "claim" ? "Review and confirm your accrued V2 reward claim in your wallet."
                    : "Review and confirm closing your mature V2 farm in your wallet.");
            v2TxStatus("Confirm " + (action === "claim" ? "reward claim" : "mature farm close") + " in your wallet…", "info");
            return action === "claim" ? contract.claimReward() : contract.closeFarm();
        }).then(function (tx) {
            if (!tx || !tx.hash) return;
            v2Processing(action === "start" ? "Farm transaction submitted"
                : action === "claim" ? "Reward claim submitted"
                : action === "close" ? "Farm close submitted" : "Sponsor contribution submitted",
                "Waiting for the transaction to confirm on Celo.");
            v2TxStatus("Transaction pending confirmation: " + HISTORY.shortHash(tx.hash), "info", tx.hash);
            return tx.wait().then(function (receipt) {
                if (!receipt || Number(receipt.status) !== 1) throw new Error("Transaction failed or receipt could not be verified.");
                v2TxStatus("Transaction confirmed on-chain.", "ok", tx.hash);
                v2Processing("Transaction confirmed on Celo", "Refreshing your verified Farming V2 position.");
                return v2Refresh();
            });
        }).catch(function (err) {
            v2TxStatus(_isUserRejection(err) ? "Wallet request cancelled." : _errMessage(err), "error");
        }).then(function () {
            v2Processing("", "");
            v2.busy = false; ["v2FundBtn"].forEach(function (id) { var b = v2Element(id); if (b) b.disabled = !v2.enabled; }); v2RenderPosition(); v2Estimate();
        });
    }

    function init() {
        if (typeof ethers === "undefined") {
            setStatus("Wallet libraries are still loading. Please refresh the page.", "error");
            return;
        }
        // Guest view: the page renders the shared "Get Started" connect card
        // instead of the play panel, so there is no farm to read and no wallet
        // to poll with. Skip all reads (an empty wallet would query the
        // contract for address "").
        if (!WALLET) return;
        if (typeof GMWalletConnect !== "undefined") {
            GMWalletConnect.configure({
                walletAddress: WALLET,
                loginMethod: LOGIN_METHOD,
                projectId: boot.walletconnectProjectId || "",
                dappName: DISPLAY_BRAND + " — Farming",
                dappDescription: "Farm chickens and earn G$ egg rewards on Celo",
                assetVersion: boot.assetVersion || "",
            });
        }

        var input = $("farmAmount");
        if (input) {
            input.addEventListener("input", updateChickensToBuy);
            if (!input.value) input.value = String(MIN_FARM);
        }
        document.querySelectorAll("[data-farm-amount]").forEach(function (button) {
            button.addEventListener("click", function () {
                if (!input) return;
                input.value = button.dataset.farmAmount;
                updateChickensToBuy();
            });
        });
        updateChickensToBuy();
        var v2Amount = $("v2FarmAmount"); if (v2Amount) v2Amount.addEventListener("input", v2Estimate);
        [["v2StartBtn", "start"], ["v2ClaimBtn", "claim"], ["v2CloseBtn", "close"], ["v2FundBtn", "fund"]].forEach(function (pair) { var b = $(pair[0]); if (b) b.addEventListener("click", function () { return v2Send(pair[1]); }); });
        var v2HistoryBtn = $("v2RefreshHistoryBtn"); if (v2HistoryBtn) v2HistoryBtn.addEventListener("click", v2LoadHistory);

        var startBtn = $("startFarmBtn");
        if (startBtn) startBtn.addEventListener("click", startFarm);
        var refreshBtn = $("refreshFarmBtn");
        if (refreshBtn) refreshBtn.addEventListener("click", function () {
            setStatus("Refreshing farm status…");
            return refreshFarm();
        });
        var sellBtn = $("sellEggsBtn");
        if (sellBtn) sellBtn.addEventListener("click", sellEggs);
        var closeBtn = $("closeFarmBtn");
        if (closeBtn) closeBtn.addEventListener("click", closeFarm);
        var refreshHistoryBtn = $("refreshHistoryBtn");
        if (refreshHistoryBtn) refreshHistoryBtn.addEventListener("click", function () {
            toast("Checking on-chain status of pending transactions…", "info");
            // Re-read the contract too, so transactions made on another device
            // (or before the browser history was cleared) reappear here.
            return loadOnchainHistory({ force: true }).then(function () {
                return reconcilePending();
            }).then(function () {
                toast("Transaction history is up to date.", "success");
            });
        });
        var closeModalBtn = $("closeSuccessClose");
        if (closeModalBtn) closeModalBtn.addEventListener("click", closeCloseModal);

        // Auto-refresh: returning to the tab refreshes immediately, and the
        // interval keeps the farm state (and history) current from then on.
        document.addEventListener("visibilitychange", onVisible);
        window.addEventListener("focus", onVisible);

        renderHistory();
        startTick();
        startPolling();
        // Read the farm FIRST so an active farm is visible on load. The
        // on-chain history scan is the slow part (it walks contract logs), so
        // chaining it ahead of the state read is what forced users to tap
        // Refresh — they saw "no active farm" until the scan finished.
        refreshFarm();
        // Do not let the separate V2 config request delay visibility of a
        // user's V1 legacy position on initial page load.
        v2Refresh();
        loadOnchainHistory().then(reconcilePending);
    }

    window.GMFarm = {
        init: init,
        startFarm: startFarm,
        sellEggs: sellEggs,
        closeFarm: closeFarm,
        refreshFarm: refreshFarm,
        renderHistory: renderHistory,
        validateFarmAmount: validateFarmAmount,
        projectedMonthlyProfit: projectedMonthlyProfit,
        describeCloseSuccess: describeCloseSuccess,
        describeSellSuccess: describeSellSuccess,
        sumGdReceived: sumGdReceived,
        settleFromReceipt: settleFromReceipt,
        reconcilePending: reconcilePending,
        loadOnchainHistory: loadOnchainHistory,
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
