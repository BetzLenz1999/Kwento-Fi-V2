/* Shared ethers.js loader for claim/signing pages.
 *
 * The loader is deliberately small and dependency-free. It loads one pinned
 * ethers version, tries a verified fallback source when the primary CDN fails,
 * and exposes one Promise so callers never race or append duplicate scripts.
 */
(function (global, document) {
    'use strict';

    if (global.GMEthersLoader) return;

    var VERSION = '6.13.4';
    var INTEGRITY = 'sha384-6Zl0Pc8zjSz8KvmNeXRvUQgY4ryFb+BwDvKCmLYcBME0joAaru491tQgi9B7zsMM';
    var SOURCES = [
        'https://cdnjs.cloudflare.com/ajax/libs/ethers/' + VERSION + '/ethers.umd.min.js',
        'https://cdn.jsdelivr.net/npm/ethers@' + VERSION + '/dist/ethers.umd.min.js'
    ];
    var TIMEOUT_MS = 10000;
    var promise = null;
    var state = 'idle';
    var sourceIndex = -1;
    var listeners = [];

    function hasRequiredApi() {
        var e = global.ethers;
        return !!(e && typeof e.BrowserProvider === 'function' &&
            typeof e.JsonRpcProvider === 'function' &&
            typeof e.Contract === 'function' && typeof e.Wallet === 'function');
    }

    function publish(next, error) {
        state = next;
        var detail = { state: next, sourceIndex: sourceIndex };
        if (error) detail.error = error;
        listeners.slice().forEach(function (listener) {
            try { listener(detail); } catch (_) {}
        });
        try { global.dispatchEvent(new CustomEvent('gm:ethers-state', { detail: detail })); } catch (_) {}
    }

    function loadSource(index) {
        sourceIndex = index;
        return new Promise(function (resolve, reject) {
            var script = document.createElement('script');
            var settled = false;
            var timer = setTimeout(function () {
                if (settled) return;
                settled = true;
                script.remove();
                reject(new Error('Ethers CDN timed out.'));
            }, TIMEOUT_MS);

            script.src = SOURCES[index];
            script.async = true;
            script.integrity = INTEGRITY;
            script.crossOrigin = 'anonymous';
            script.referrerPolicy = 'no-referrer';
            script.dataset.gmEthersSource = String(index);
            script.onload = function () {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                if (hasRequiredApi()) resolve(global.ethers);
                else reject(new Error('Ethers loaded but its API is incomplete.'));
            };
            script.onerror = function () {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                reject(new Error('Ethers CDN failed.'));
            };
            (document.head || document.body || document.documentElement).appendChild(script);
        });
    }

    function start() {
        if (hasRequiredApi()) {
            publish('ready');
            return Promise.resolve(global.ethers);
        }
        publish('loading');
        return loadSource(0).catch(function (primaryError) {
            return loadSource(1).catch(function (fallbackError) {
                var error = new Error('Wallet tools could not be loaded. Check your connection and try again.');
                error.code = 'ETHERS_LOAD_FAILED';
                error.primary = primaryError;
                error.fallback = fallbackError;
                throw error;
            });
        }).then(function (e) {
            publish('ready');
            return e;
        }).catch(function (error) {
            publish('failed', error);
            throw error;
        });
    }

    global.GMEthersLoader = {
        version: VERSION,
        sources: SOURCES.slice(),
        integrity: INTEGRITY,
        load: function () {
            if (hasRequiredApi()) {
                publish('ready');
                return Promise.resolve(global.ethers);
            }
            if (!promise || state === 'failed') {
                promise = start();
            }
            return promise;
        },
        retry: function () {
            promise = null;
            sourceIndex = -1;
            return this.load();
        },
        getState: function () { return state; },
        onState: function (listener) {
            if (typeof listener !== 'function') return function () {};
            listeners.push(listener);
            return function () {
                listeners = listeners.filter(function (item) { return item !== listener; });
            };
        }
    };
}(window, document));
