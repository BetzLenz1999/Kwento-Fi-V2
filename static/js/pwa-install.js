/**
 * GoodMarket — shared PWA install + service-worker module.
 *
 * This revives the install logic that only ever lived inside the now-dead
 * templates/login.html (the /login route is a redirect, so its inline
 * `beforeinstallprompt` + service-worker code could never run). It is a
 * single reusable module so the homepage, the wallet page and any future
 * surface share one implementation.
 *
 * Responsibilities:
 *   - Capture `beforeinstallprompt` (Chromium) so a tap can trigger the real
 *     native install sheet at a moment of our choosing.
 *   - Detect "already installed" (standalone display mode / iOS navigator
 *     flag) so we never nag a user who already has the app.
 *   - Register the service worker at the ROOT scope so it controls "/" (the
 *     manifest's start_url) — required for installability. Without the
 *     explicit scope the worker would only control /static/.
 *   - Drive declarative install banners via data attributes (no inline
 *     handlers needed), in two modes:
 *       data-gm-pwa-mode="once"       → shown once per browser, then never
 *       data-gm-pwa-mode="persistent" → shown until installed or dismissed
 *
 * Markup contract (see templates/homepage.html and templates/wallet.html):
 *   <div id="..." data-gm-pwa-banner
 *        data-gm-pwa-mode="once|persistent"
 *        data-gm-pwa-key="wallet|home">           (seen/dismissed key suffix)
 *     <button data-gm-pwa-dismiss>×</button>
 *     <button data-gm-pwa-install>Install app</button>
 *     <div data-gm-pwa-status role="status" aria-live="polite"></div>
 *   </div>
 *
 * Exposes window.GMPwaInstall for programmatic use / tests.
 */
(function () {
    'use strict';

    var SW_URL = '/static/service-worker.js';
    var SW_SCOPE = '/';
    var INSTALLED_KEY = 'gmPwaInstalled_v1';
    var INSTALL_BASELINE_PENDING_KEY = 'gmPwaInstallBaselinePending_v1';
    var INSTALL_BASELINE_INTENT_TTL_MS = 7 * 24 * 60 * 60 * 1000;
    var MODULE_BUILD_VERSION = _moduleBuildVersion();
    var UPDATE_POLL_MS = 60000;
    var DAPP_DETECTION_MS = 900;

    var _deferredPrompt = null;
    var _installedFlag = _readFlag(INSTALLED_KEY);
    var _swRegistered = false;
    var _homepageGateReleased = false;
    var _homepageInstallState = 'checking';
    var _homepageProgressTimer = null;
    var _homepageInstallCompleted = false;
    var _homepageProgressStageIndex = -1;
    var HOMEPAGE_PROGRESS_STAGES = [
        { percent: 10, message: 'Preparing GoodMarket…' },
        { percent: 20, message: 'Preparing app resources…' },
        { percent: 30, message: 'Setting up the app shell…' },
        { percent: 40, message: 'Registering GoodMarket for your device…' },
        { percent: 50, message: 'Still installing GoodMarket…' },
        { percent: 60, message: 'Checking the app setup…' },
        { percent: 70, message: 'Finalizing the installation…' },
        { percent: 80, message: 'Almost ready…' },
        { percent: 90, message: 'Finishing installation — keep this page open…' },
    ];

    // ── storage helpers (fail closed on blocked storage) ────────────────
    function _readFlag(key) {
        try { return localStorage.getItem(key) === '1'; }
        // localStorage blocked (hardened private mode): we cannot remember,
        // so report "already installed" and never nag on every load.
        catch (_) { return true; }
    }

    function _writeFlag(key) {
        try { localStorage.setItem(key, '1'); } catch (_) { /* private mode */ }
    }

    function _moduleBuildVersion() {
        try {
            var script = document.currentScript;
            var source = script && script.src || '';
            var match = source.match(/[?&]v=([^&#]+)/);
            return match ? decodeURIComponent(match[1]) : '';
        } catch (_) {
            return '';
        }
    }

    function _recordInstallBaselineIntent(force) {
        if (!MODULE_BUILD_VERSION || (_installedFlag && !force)) return;
        try {
            localStorage.setItem(INSTALL_BASELINE_PENDING_KEY, JSON.stringify({
                build: MODULE_BUILD_VERSION,
                createdAt: Date.now(),
            }));
        } catch (_) { /* blocked storage leaves the first launch as a safe review */ }
    }

    function _readSeen(key) {
        try { return localStorage.getItem(key) === '1'; }
        catch (_) { return true; }
    }

    function _writeSeen(key) {
        try { localStorage.setItem(key, '1'); } catch (_) { /* private mode */ }
    }

    // ── install-state detection ─────────────────────────────────────────
    function isStandalone() {
        try {
            if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) {
                return true;
            }
            if (window.matchMedia && window.matchMedia('(display-mode: minimal-ui)').matches) {
                return true;
            }
        } catch (_) { /* matchMedia unavailable */ }
        // iOS Safari exposes navigator.standalone instead of display-mode.
        return window.navigator && window.navigator.standalone === true;
    }

    function isIos() {
        var ua = (window.navigator && window.navigator.userAgent) || '';
        var iosDevice = /iPad|iPhone|iPod/.test(ua);
        // iPadOS 13+ reports a desktop UA; detect it via touch points.
        var iPadOs = /Macintosh/.test(ua) && (window.navigator.maxTouchPoints || 0) > 1;
        return iosDevice || iPadOs;
    }

    // The homepage gate is bypassed for wallet/DApp browsers. Check the
    // injected EIP-1193 provider synchronously first, then wait briefly for
    // late provider injection and EIP-6963 announcements before blocking.
    function _hasInjectedWallet() {
        try {
            var eth = window.ethereum;
            if (eth && (typeof eth.request === 'function' || eth.isMetaMask
                    || eth.isMiniPay || eth.isTrust || eth.isTrustWallet
                    || eth.isCoinbaseWallet || eth.isBraveWallet
                    || (Array.isArray(eth.providers) && eth.providers.length))) return true;
            if (window.celo && typeof window.celo.request === 'function') return true;
        } catch (_) { /* cross-origin or guarded provider getter */ }
        return false;
    }

    function _hasDappBrowserSignal() {
        if (_hasInjectedWallet()) return true;
        var ua = (window.navigator && window.navigator.userAgent) || '';
        if (/MiniPay|MetaMaskMobile|TrustWallet|CoinbaseWallet|TokenPocket|imToken|OKApp|Rainbow|Telegram/i.test(ua)) {
            return true;
        }
        try {
            var webApp = window.Telegram && window.Telegram.WebApp;
            if (webApp && (webApp.initData || webApp.initDataUnsafe
                    && Object.keys(webApp.initDataUnsafe).length)) return true;
        } catch (_) { /* optional SDK */ }
        return false;
    }

    function _waitForDappBrowser() {
        if (_hasDappBrowserSignal()) return Promise.resolve(true);
        return new Promise(function (resolve) {
            var settled = false;
            var pollTimer = null;
            var timeoutTimer = null;
            function cleanup() {
                if (pollTimer !== null) clearTimeout(pollTimer);
                if (timeoutTimer !== null) clearTimeout(timeoutTimer);
                try { window.removeEventListener('eip6963:announceProvider', onProvider); } catch (_) {}
                try { window.removeEventListener('ethereum#initialized', check); } catch (_) {}
            }
            function finish(found) {
                if (settled) return;
                settled = true;
                cleanup();
                resolve(found);
            }
            function onProvider(event) {
                // Any EIP-6963 provider is an explicit injected-wallet signal.
                if (event && event.detail && event.detail.provider) finish(true);
            }
            function check() {
                if (_hasDappBrowserSignal()) { finish(true); return; }
                pollTimer = setTimeout(check, 100);
            }
            try { window.addEventListener('eip6963:announceProvider', onProvider); } catch (_) {}
            try { window.addEventListener('ethereum#initialized', check); } catch (_) {}
            try { window.dispatchEvent(new Event('eip6963:requestProvider')); } catch (_) {}
            pollTimer = setTimeout(check, 100);
            timeoutTimer = setTimeout(function () { finish(_hasDappBrowserSignal()); }, DAPP_DETECTION_MS);
        });
    }

    function _homepageGate() {
        return document.getElementById('gmPwaInstallGate');
    }

    function _homepageGateStatus() {
        var gate = _homepageGate();
        return gate ? gate.querySelector('[data-gm-pwa-status]') : null;
    }

    function _homepageGateButton() {
        var gate = _homepageGate();
        return gate ? gate.querySelector('[data-gm-pwa-install]') : null;
    }

    function _homepageProgress() {
        var gate = _homepageGate();
        return gate ? gate.querySelector('[data-gm-pwa-progress]') : null;
    }

    function _homepageProgressValue() {
        var gate = _homepageGate();
        return gate ? gate.querySelector('[data-gm-pwa-progress-value]') : null;
    }

    function _homepageProgressMessage() {
        var gate = _homepageGate();
        return gate ? gate.querySelector('[data-gm-pwa-progress-message]') : null;
    }

    function _setHomepageState(state, message) {
        _homepageInstallState = state;
        var gate = _homepageGate();
        if (!gate) return;
        gate.setAttribute('data-state', state);
        var status = _homepageGateStatus();
        if (status && message) status.textContent = message;
        var progressWrap = gate.querySelector('[data-gm-pwa-progress-wrap]');
        if (progressWrap && state !== 'prompting' && state !== 'installing'
                && state !== 'installed-awaiting-launch') {
            progressWrap.hidden = true;
        }
        var button = _homepageGateButton();
        if (button) {
            var busy = state === 'prompting' || state === 'installing'
                || state === 'installed-awaiting-launch';
            button.disabled = busy;
            button.hidden = state === 'installed-awaiting-launch';
            button.setAttribute('aria-busy', busy ? 'true' : 'false');
        }
    }

    function _setHomepageProgress(percent, message) {
        var progress = _homepageProgress();
        var value = _homepageProgressValue();
        var gate = _homepageGate();
        var progressWrap = gate ? gate.querySelector('[data-gm-pwa-progress-wrap]') : null;
        if (progressWrap) progressWrap.hidden = false;
        if (progress) {
            progress.value = percent;
            progress.setAttribute('aria-valuenow', String(percent));
            progress.setAttribute('aria-valuetext', (message || 'Installing GoodMarket')
                + ' Approximately ' + String(percent) + ' percent.');
            progress.hidden = false;
        }
        if (value) value.textContent = String(percent) + '%';
        var progressMessage = _homepageProgressMessage();
        if (progressMessage && message) progressMessage.textContent = message;
        if (gate) gate.setAttribute('data-progress', String(percent));
        if (message) {
            var status = _homepageGateStatus();
            if (status) status.textContent = message;
        }
    }

    function _stopHomepageProgress() {
        if (_homepageProgressTimer !== null) {
            clearInterval(_homepageProgressTimer);
            _homepageProgressTimer = null;
        }
    }

    function _startHomepageProgress() {
        _stopHomepageProgress();
        _homepageProgressStageIndex = 0;
        var firstStage = HOMEPAGE_PROGRESS_STAGES[0];
        _setHomepageState('installing', firstStage.message);
        _setHomepageProgress(firstStage.percent, firstStage.message);
        _homepageProgressTimer = setInterval(function () {
            // Browsers do not expose byte-level PWA installation progress.
            // Stop at 90% and reserve 100% for a real completion signal.
            if (_homepageInstallCompleted) return;
            if (_homepageProgressStageIndex >= HOMEPAGE_PROGRESS_STAGES.length - 1) {
                _stopHomepageProgress();
                return;
            }
            _homepageProgressStageIndex += 1;
            var stage = HOMEPAGE_PROGRESS_STAGES[_homepageProgressStageIndex];
            _setHomepageProgress(stage.percent, stage.message);
        }, 700);
    }

    function _confirmHomepageInstall() {
        if (_homepageInstallCompleted) return;
        _homepageInstallCompleted = true;
        _stopHomepageProgress();
        _homepageProgressStageIndex = HOMEPAGE_PROGRESS_STAGES.length - 1;
        _markInstalled();
        var message = 'GoodMarket is installed. Open it from your home screen or app launcher to continue.';
        _setHomepageState('installed-awaiting-launch', message);
        _setHomepageProgress(100, message);
    }

    function _releaseHomepageGate(reason) {
        var isDappBypass = reason === 'dapp';
        var gate = _homepageGate();
        // The ordinary browser gate may only release in standalone mode. The
        // existing DApp-browser bypass remains an explicit separate reason.
        if (gate && !isDappBypass && !isStandalone()) return false;
        _stopHomepageProgress();
        _homepageGateReleased = true;
        var root = document.documentElement;
        if (root && root.classList) {
            root.classList.remove('gm-pwa-gate-checking');
            root.classList.remove('gm-pwa-gate-required');
        }
        if (gate) {
            gate.hidden = true;
            gate.setAttribute('data-state', 'released');
            gate.setAttribute('aria-hidden', 'true');
        }
        return true;
    }

    function _requireHomepageInstall() {
        if (_homepageGateReleased) return;
        _setHomepageState('required', 'Install GoodMarket, then open it from your home screen or app launcher.');
        var root = document.documentElement;
        var gate = _homepageGate();
        if (root && root.classList) {
            root.classList.remove('gm-pwa-gate-checking');
            root.classList.add('gm-pwa-gate-required');
        }
        if (gate) {
            gate.hidden = false;
            gate.setAttribute('data-state', 'required');
            gate.removeAttribute('aria-hidden');
            var installButton = gate.querySelector('[data-gm-pwa-install]');
            if (installButton && installButton.focus) installButton.focus();
        }
    }

    function _initHomepageGate() {
        if (!_homepageGate()) return Promise.resolve('not-applicable');
        // Do not trust a local-storage marker for the gate: it only proves that
        // a prompt was accepted at some point, not that this tab is standalone.
        if (isStandalone()) {
            _homepageInstallCompleted = true;
            _releaseHomepageGate('standalone');
            return Promise.resolve('installed');
        }
        if (_hasDappBrowserSignal()) {
            _releaseHomepageGate('dapp');
            return Promise.resolve('dapp');
        }
        var gate = _homepageGate();
        if (gate) {
            gate.hidden = false;
            gate.setAttribute('data-state', 'checking');
        }
        return _waitForDappBrowser().then(function (isDapp) {
            if (isDapp) {
                _releaseHomepageGate('dapp');
                return 'dapp';
            }
            if (isStandalone()) {
                _homepageInstallCompleted = true;
                _releaseHomepageGate('standalone');
                return 'installed';
            }
            _requireHomepageInstall();
            return 'install-required';
        });
    }

    function isInstalled() {
        return _installedFlag || isStandalone();
    }

    function canPrompt() {
        return !!_deferredPrompt;
    }

    function shouldOfferInstall() {
        return !isInstalled();
    }

    function _markInstalled() {
        _installedFlag = true;
        _writeFlag(INSTALLED_KEY);
        _recordInstallBaselineIntent(true);
    }

    // ── native prompt ───────────────────────────────────────────────────
    function promptInstall(opts) {
        opts = opts || {};
        var deferred = _deferredPrompt;
        if (!deferred) {
            return Promise.resolve({
                outcome: 'unavailable',
                ios: isIos(),
                standalone: isStandalone(),
            });
        }
        // The deferred event can only be used once.
        _deferredPrompt = null;
        return Promise.resolve()
            .then(function () { return deferred.prompt(); })
            .then(function () { return deferred.userChoice; })
            .then(function (choice) {
                var outcome = (choice && choice.outcome) || 'dismissed';
                if (outcome === 'accepted') {
                    if (!opts.homepage) {
                        _markInstalled();
                        hideAllBanners();
                    }
                }
                _emit('gm-pwa-install-choice', { outcome: outcome });
                return { outcome: outcome };
            })
            .catch(function () {
                return { outcome: 'error' };
            });
    }

    /**
     * Wire an "Install app" button: uses the native sheet when available and
     * falls back to explicit manual instructions (iOS Share menu / browser
     * menu) so the tap is never a dead end.
     */
    function handleInstallClick(opts) {
        opts = opts || {};
        var statusEl = opts.statusId ? document.getElementById(opts.statusId) : null;
        var setStatus = function (msg) {
            if (statusEl) statusEl.textContent = msg;
        };

        if (isStandalone()) {
            setStatus('GoodMarket is already installed on this device.');
            return Promise.resolve({ outcome: 'already-installed' });
        }

        var homepage = !!_homepageGate();
        if (homepage) {
            _setHomepageState('prompting', 'Opening the browser install request…');
            _setHomepageProgress(1, 'Starting the install request…');
        }

        return promptInstall({ homepage: homepage }).then(function (res) {
            if (res.outcome === 'accepted') {
                if (homepage) {
                    _startHomepageProgress();
                } else {
                    setStatus('Installing GoodMarket…');
                }
                return res;
            }
            if (res.outcome === 'dismissed') {
                if (homepage) _setHomepageState('cancelled', 'Installation cancelled — you can try again anytime.');
                setStatus('Install cancelled — you can try again anytime.');
                return res;
            }
            // No native prompt available (Firefox/Safari, or before the
            // engagement threshold) — guide the user to the manual path.
            _recordInstallBaselineIntent(false);
            if (homepage) _setHomepageState('manual', manualInstallHint());
            setStatus(manualInstallHint());
            return res;
        });
    }

    function manualInstallHint() {
        if (isIos()) {
            return 'To install: tap the Share button (⬆️), then choose “Add to Home Screen”.';
        }
        return 'To install: open your browser menu (⋮) and choose “Install app” or “Add to Home screen”.';
    }

    // ── service worker (root scope so it controls start_url "/") ────────
    function registerServiceWorker(opts) {
        opts = opts || {};
        if (!('serviceWorker' in navigator)) return Promise.resolve(null);
        if (_swRegistered) return Promise.resolve(navigator.serviceWorker.controller || null);
        _swRegistered = true;

        var register = function () {
            return navigator.serviceWorker
                .register(SW_URL, { scope: SW_SCOPE })
                .then(function (reg) {
                    // Mirror the previous login/dashboard behaviour: poll for
                    // updates so a new deployment reaches installed users.
                    try {
                        setInterval(function () {
                            try { reg.update(); } catch (_) {}
                        }, UPDATE_POLL_MS);
                    } catch (_) {}
                    return reg;
                });
        };

        // A stale root-scoped worker registered from the OLD "/service-worker.js"
        // path can linger. The server shim unregisters itself, but we also
        // clean up any other root-scoped registration here so the new worker
        // (same scope, different script URL) takes over cleanly.
        return _unregisterLegacyRootWorkers()
            .then(register)
            .catch(function (err) {
                console.warn('PWA: service worker registration failed', err);
                return null;
            });
    }

    function _unregisterLegacyRootWorkers() {
        if (!navigator.serviceWorker.getRegistrations) return Promise.resolve();
        return navigator.serviceWorker.getRegistrations()
            .then(function (regs) {
                var jobs = [];
                (regs || []).forEach(function (reg) {
                    try {
                        var scopePath = new URL(reg.scope).pathname;
                        // Only touch the old root-scoped script; never the
                        // /static/service-worker.js we are about to register.
                        if (scopePath === '/' && reg.active &&
                            reg.active.scriptURL &&
                            reg.active.scriptURL.indexOf('/service-worker.js') !== -1 &&
                            reg.active.scriptURL.indexOf('/static/') === -1) {
                            jobs.push(reg.unregister());
                        }
                    } catch (_) {}
                });
                return Promise.all(jobs);
            })
            .catch(function () { /* best effort */ });
    }

    function _listenForUpdates() {
        if (!('serviceWorker' in navigator)) return;
        navigator.serviceWorker.addEventListener('message', function (event) {
            var data = event && event.data;
            if (data && data.type === 'SW_UPDATED') {
                _showUpdateToast();
                _emit('gm-pwa-updated', { version: data.version });
            }
        });
    }

    /**
     * A new deployment is live. Show a small "refresh" toast (revives the
     * update banner the old login page had) so installed users actually get
     * the new build instead of a stale cache.
     */
    function _showUpdateToast() {
        try {
            if (document.getElementById('gmPwaUpdateToast')) return;
            var toast = document.createElement('div');
            toast.id = 'gmPwaUpdateToast';
            toast.className = 'gm-pwa-update-toast';
            toast.setAttribute('role', 'status');
            toast.setAttribute('aria-live', 'polite');

            var text = document.createElement('span');
            text.textContent = 'A new version of GoodMarket is available.';
            toast.appendChild(text);

            var btn = document.createElement('button');
            btn.type = 'button';
            btn.textContent = 'Refresh';
            btn.addEventListener('click', function () { window.location.reload(); });
            toast.appendChild(btn);

            var close = document.createElement('button');
            close.type = 'button';
            close.className = 'gm-pwa-update-close';
            close.setAttribute('aria-label', 'Dismiss');
            close.textContent = '×';
            close.addEventListener('click', function () { toast.remove(); });
            toast.appendChild(close);

            document.body.appendChild(toast);
        } catch (_) { /* non-fatal */ }
    }

    // ── banner management ───────────────────────────────────────────────
    function _banners() {
        var out = [];
        var nodes = document.querySelectorAll('[data-gm-pwa-banner]');
        for (var i = 0; i < nodes.length; i++) out.push(nodes[i]);
        return out;
    }

    function hideBanner(elOrId) {
        var el = typeof elOrId === 'string' ? document.getElementById(elOrId) : elOrId;
        if (el) el.classList.remove('show');
    }

    function hideAllBanners() {
        _banners().forEach(function (el) { el.classList.remove('show'); });
    }

    function _seenKey(el) {
        return 'gmPwaPrompted_' + (el.getAttribute('data-gm-pwa-key') || 'default') + '_v1';
    }

    function _dismissKey(el) {
        return 'gmPwaDismissed_' + (el.getAttribute('data-gm-pwa-key') || 'default') + '_v1';
    }

    function _initBanner(el) {
        if (!shouldOfferInstall()) { hideBanner(el); return; }
        var mode = el.getAttribute('data-gm-pwa-mode') || 'persistent';
        if (mode === 'once') {
            var seen = _seenKey(el);
            if (_readSeen(seen)) return;
            el.classList.add('show');
            // Write the flag at REVEAL, not at click — the same one-shot rule
            // as the join-community banner, so closing it never re-prompts.
            _writeSeen(seen);
            return;
        }
        // persistent: shown until installed or explicitly dismissed
        if (_readSeen(_dismissKey(el))) return;
        el.classList.add('show');
    }

    function initBanners() {
        _banners().forEach(_initBanner);
    }

    function dismissBanner(el) {
        if (!el) return;
        el.classList.remove('show');
        var mode = el.getAttribute('data-gm-pwa-mode') || 'persistent';
        // Persistent banners remember the dismissal; "once" banners already
        // wrote their seen-flag at reveal, so nothing more is needed.
        if (mode !== 'once') _writeSeen(_dismissKey(el));
    }

    // ── events ──────────────────────────────────────────────────────────
    function _emit(name, detail) {
        try {
            document.dispatchEvent(new CustomEvent(name, { detail: detail || {} }));
        } catch (_) { /* older engines */ }
    }

    function _wireGlobalListeners() {
        window.addEventListener('beforeinstallprompt', function (e) {
            e.preventDefault();
            _deferredPrompt = e;
            _emit('gm-pwa-installable', {});
            // Re-evaluate banners so a button that was previously in "manual"
            // mode now offers the real native sheet.
            initBanners();
        });

        window.addEventListener('appinstalled', function () {
            if (_homepageGate()) {
                _confirmHomepageInstall();
            } else {
                _markInstalled();
                hideAllBanners();
            }
            _emit('gm-pwa-installed', {});
        });
        // Some platforms return from the native install flow into the browser
        // tab; reveal only if the browser now reports installed display mode.
        function checkInstalledMode() {
            if (!isStandalone()) return;
            if (_homepageGate()) {
                if (_homepageInstallState === 'installing' || _homepageInstallState === 'prompting'
                        || _homepageInstallState === 'installed-awaiting-launch') {
                    _confirmHomepageInstall();
                }
                _homepageInstallCompleted = true;
                _releaseHomepageGate('standalone');
            } else {
                _releaseHomepageGate('standalone');
            }
        }
        window.addEventListener('pageshow', checkInstalledMode);
        window.addEventListener('focus', checkInstalledMode);
        document.addEventListener('visibilitychange', checkInstalledMode);

        // Declarative click handling (no inline handlers → CSP-friendly).
        document.addEventListener('click', function (event) {
            var target = event.target;
            if (!target || !target.closest) return;

            var dismissBtn = target.closest('[data-gm-pwa-dismiss]');
            if (dismissBtn) {
                event.preventDefault();
                dismissBanner(dismissBtn.closest('[data-gm-pwa-banner]'));
                return;
            }

            var installBtn = target.closest('[data-gm-pwa-install]');
            if (installBtn) {
                event.preventDefault();
                var banner = installBtn.closest('[data-gm-pwa-banner]')
                    || installBtn.closest('[data-gm-pwa-gate]');
                var statusEl = banner ? banner.querySelector('[data-gm-pwa-status]') : null;
                handleInstallClick({ statusId: statusEl ? statusEl.id : null });
            }
        });
    }

    // ── boot ────────────────────────────────────────────────────────────
    function init() {
        _wireGlobalListeners();
        _listenForUpdates();
        registerServiceWorker();
        _initHomepageGate();
        initBanners();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.GMPwaInstall = {
        init: init,
        isStandalone: isStandalone,
        isIos: isIos,
        isInstalled: isInstalled,
        canPrompt: canPrompt,
        shouldOfferInstall: shouldOfferInstall,
        promptInstall: promptInstall,
        handleInstallClick: handleInstallClick,
        manualInstallHint: manualInstallHint,
        registerServiceWorker: registerServiceWorker,
        initBanners: initBanners,
        hideBanner: hideBanner,
        hideAllBanners: hideAllBanners,
        dismissBanner: dismissBanner,
        // Test seams (no-op in production unless called explicitly).
        _setDeferredPrompt: function (p) { _deferredPrompt = p; },
        _resetInstalled: function () { _installedFlag = false; },
    };
})();
