/* Shared Community Web Push setup. Permission is requested only from an explicit user click. */
(function (window, navigator) {
  'use strict';

  var SW_URL = '/static/service-worker.js';
  var SW_SCOPE = '/';
  var ACTIVE_TIMEOUT_MS = 15000;
  var ENABLED_CACHE_PREFIX = 'gmMessagePushEnabled_v1:';
  var ENABLED_CACHE_TTL_MS = 30 * 24 * 60 * 60 * 1000;

  function enabledCacheKey(wallet) {
    var boot = window.GM_MESSAGES_BOOT || window.GM_COMMUNITY_BOOT || {};
    var address = String(wallet || boot.wallet || '').trim().toLowerCase();
    return address ? ENABLED_CACHE_PREFIX + address : '';
  }

  function rememberEnabled() {
    var key = enabledCacheKey();
    if (!key) return;
    try {
      if (!window.localStorage) return;
      window.localStorage.setItem(key, JSON.stringify({ checkedAt: Date.now() }));
    }
    catch (_) {}
  }

  function clearRememberedEnabled(wallet) {
    var key = enabledCacheKey(wallet);
    if (!key) return;
    try { if (window.localStorage) window.localStorage.removeItem(key); } catch (_) {}
  }

  function hasCachedEnabled(wallet) {
    var key = enabledCacheKey(wallet);
    if (!key || !window.Notification
        || window.Notification.permission !== 'granted') return false;
    try {
      var storage = window.localStorage;
      if (!storage) return false;
      var value = JSON.parse(storage.getItem(key) || 'null');
      var age = value && Date.now() - Number(value.checkedAt);
      if (!value || !isFinite(age) || age < 0 || age > ENABLED_CACHE_TTL_MS) {
        storage.removeItem(key);
        return false;
      }
      return true;
    } catch (_) { return false; }
  }

  function makeError(message, code) {
    var error = new Error(message || 'Could not set up message notifications.');
    error.code = code || 'push_setup_failed';
    return error;
  }

  function api(path, options) {
    var opts = options || {};
    opts.credentials = 'same-origin';
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    return window.fetch(path, opts).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          throw makeError(data.error || 'The notification service could not complete setup.',
            data.code || 'push_server_error');
        }
        return data;
      });
    });
  }

  function supported() {
    return window.isSecureContext !== false && !!window.Notification
      && !!(navigator && navigator.serviceWorker)
      && !!window.PushManager;
  }

  function permissionRequest() {
    if (!supported()) return Promise.reject(makeError(
      'This browser or connection does not support message notifications.', 'unsupported'));
    if (window.Notification.permission === 'granted') return Promise.resolve('granted');
    if (window.Notification.permission === 'denied') return Promise.reject(makeError(
      'Notifications are blocked for this site. Allow them in your browser settings, then try again.',
      'permission_denied'));

    // Call synchronously when enable() is invoked by the Enable button's click
    // handler. Browsers require a user gesture for the native permission prompt.
    return new Promise(function (resolve, reject) {
      var settled = false;
      function finish(value) {
        if (settled) return;
        settled = true;
        resolve(value);
      }
      var result;
      try {
        result = window.Notification.requestPermission(finish);
      } catch (error) {
        reject(error || makeError('The browser could not request notification permission.',
          'permission_request_failed'));
        return;
      }
      if (result && typeof result.then === 'function') result.then(finish, reject);
      else if (typeof result === 'string') finish(result);
    });
  }

  function base64ToBytes(value) {
    if (typeof value !== 'string' || !value.trim()) {
      throw makeError('Message notifications are not configured on the server.', 'not_configured');
    }
    var padding = '='.repeat((4 - value.length % 4) % 4);
    var raw;
    try {
      raw = window.atob((value + padding).replace(/-/g, '+').replace(/_/g, '/'));
    } catch (_) {
      throw makeError('The notification server key is invalid. Please contact support.',
        'invalid_server_key');
    }
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    if (bytes.length !== 65 || bytes[0] !== 4) {
      throw makeError('The notification server key is invalid. Please contact support.',
        'invalid_server_key');
    }
    return bytes;
  }

  function workerPath(worker) {
    if (!worker || !worker.scriptURL) return '';
    try { return new window.URL(worker.scriptURL, window.location.href).pathname; }
    catch (_) { return ''; }
  }

  function hasCanonicalWorker(registration) {
    var workers = registration
      ? [registration.active, registration.waiting, registration.installing].filter(Boolean)
      : [];
    if (!workers.length) return false;
    var active = registration.active;
    if (active && active.state === 'activated') return workerPath(active) === SW_URL;
    return workers.some(function (worker) { return workerPath(worker) === SW_URL; });
  }

  function registerCanonicalWorker() {
    if (!supported()) return Promise.reject(makeError(
      'This browser or connection does not support message notifications.', 'unsupported'));
    return navigator.serviceWorker.getRegistration(SW_SCOPE).then(function (existing) {
      if (existing && hasCanonicalWorker(existing)) return existing;
      var removeOld = existing && typeof existing.unregister === 'function'
        ? Promise.resolve(existing.unregister()) : Promise.resolve(false);
      return removeOld.then(function () {
        return navigator.serviceWorker.register(SW_URL, { scope: SW_SCOPE });
      });
    }).then(function (registration) {
      if (!registration || !registration.pushManager) {
        throw makeError('The app service worker is not ready for notifications.', 'worker_unavailable');
      }
      return waitForActive(registration).then(function () { return registration; });
    });
  }

  function waitForActive(registration) {
    if (registration.active && registration.active.state === 'activated') return Promise.resolve();
    return new Promise(function (resolve, reject) {
      var done = false;
      var timer = window.setInterval(check, 50);
      var timeout = window.setTimeout(function () {
        finish(makeError('The app service worker did not become ready. Please retry.',
          'worker_timeout'));
      }, ACTIVE_TIMEOUT_MS);
      function finish(error) {
        if (done) return;
        done = true;
        window.clearInterval(timer);
        window.clearTimeout(timeout);
        if (error) reject(error);
        else resolve();
      }
      function check() {
        if (registration.active && registration.active.state === 'activated') finish();
        else if (registration.installing && registration.installing.state === 'redundant') {
          finish(makeError('The app service worker could not be installed. Please retry.',
            'worker_install_failed'));
        }
      }
      check();
    });
  }

  function serverStatus(endpoint) {
    var options = endpoint ? { method: 'POST', body: JSON.stringify({ endpoint: endpoint }) } : undefined;
    return api('/api/messages/push/status', options).then(function (data) {
      if (!data || data.success !== true) {
        throw makeError((data && data.error) || 'Could not check notification setup.',
          (data && data.code) || 'push_status_failed');
      }
      return data;
    });
  }

  function inspect() {
    var result;
    if (!supported()) result = Promise.resolve({ state: 'unsupported' });
    else if (window.Notification.permission === 'denied') {
      result = Promise.resolve({ state: 'permission-blocked' });
    } else {
      result = serverStatus().then(function (config) {
        if (!config.enabled || !config.public_key) return { state: 'unconfigured' };
        // Catch malformed keys before the user grants browser permission.
        base64ToBytes(config.public_key);
        if (config.storage_ready !== true) {
          return { state: 'storage-unavailable', message: config.error ||
            'Message notifications cannot be enabled right now. You can still open Messages.' };
        }
        return registerCanonicalWorker().then(function (registration) {
          return registration.pushManager.getSubscription().then(function (subscription) {
            if (window.Notification.permission !== 'granted' || !subscription) {
              return { state: 'needs-setup' };
            }
            return serverStatus(subscription.endpoint).then(function (status) {
              return status.enabled && status.storage_ready === true && status.subscribed === true
                ? { state: 'enabled' } : { state: 'needs-setup' };
            });
          });
        });
      }).catch(function (error) {
        return { state: 'storage-unavailable', message: (error && error.message)
          || 'Could not check notification setup. You can still open Messages.' };
      });
    }
    return result.then(function (status) {
      if (status.state === 'enabled') rememberEnabled();
      else if (status.state !== 'storage-unavailable') clearRememberedEnabled();
      return status;
    });
  }

  function enable() {
    // This must be the first side effect and is intentionally called before
    // any await/fetch, while the user is clicking the in-app Enable button.
    return permissionRequest().then(function (permission) {
      if (permission !== 'granted' || window.Notification.permission !== 'granted') {
        throw makeError(permission === 'denied'
          ? 'Notifications are blocked for this site. Allow them in your browser settings, then try again.'
          : 'Notification permission was not granted.',
        permission === 'denied' ? 'permission_denied' : 'permission_not_granted');
      }
      return serverStatus().then(function (config) {
        if (!config.enabled || !config.public_key) {
          throw makeError('Message notifications are not configured on the server yet.', 'not_configured');
        }
        if (config.storage_ready !== true) {
          throw makeError(config.error || 'Notification registration is temporarily unavailable. Please try later.',
            'storage_unavailable');
        }
        return registerCanonicalWorker().then(function (registration) {
          return registration.pushManager.getSubscription().then(function (current) {
            return current || registration.pushManager.subscribe({
              userVisibleOnly: true,
              applicationServerKey: base64ToBytes(config.public_key)
            });
          });
        });
      }).then(function (subscription) {
        if (!subscription || window.Notification.permission !== 'granted') {
          throw makeError('The browser did not finish creating this device subscription.',
            'subscription_missing');
        }
        var payload = subscription.toJSON();
        if (!payload || !payload.endpoint || !payload.keys
            || !payload.keys.p256dh || !payload.keys.auth) {
          throw makeError('The browser returned an incomplete device subscription.',
            'invalid_subscription');
        }
        return api('/api/messages/push/subscribe', {
          method: 'POST', body: JSON.stringify({ subscription: payload })
        }).then(function (saved) {
          if (!saved || saved.success !== true || saved.enabled !== true || saved.subscribed !== true) {
            throw makeError((saved && saved.error) || 'The server could not save this device subscription.',
              (saved && saved.code) || 'subscription_save_failed');
          }
          return serverStatus(subscription.endpoint).then(function (verified) {
            if (!verified || verified.success !== true || !verified.enabled
                || verified.storage_ready !== true || verified.subscribed !== true) {
              throw makeError((verified && verified.error)
                || 'The server could not confirm this device subscription. Please retry.',
              (verified && verified.code) || 'subscription_verify_failed');
            }
            return { state: 'enabled', subscription: subscription };
          });
        });
      });
    }).then(function (result) {
      if (result && result.state === 'enabled') rememberEnabled();
      return result;
    });
  }

  function disable() {
    return registerCanonicalWorker().then(function (registration) {
      return registration.pushManager.getSubscription().then(function (subscription) {
        if (!subscription) { clearRememberedEnabled(); return { state: 'disabled' }; }
        return api('/api/messages/push/unsubscribe', {
          method: 'POST', body: JSON.stringify({ endpoint: subscription.endpoint })
        }).then(function (result) {
          if (!result || result.success !== true) {
            throw makeError((result && result.error) || 'The server could not disable this device.',
              (result && result.code) || 'unsubscribe_failed');
          }
          return subscription.unsubscribe().catch(function () { return false; })
            .then(function () { clearRememberedEnabled(); return { state: 'disabled' }; });
        });
      });
    });
  }

  window.GMCommunityPush = {
    supported: supported,
    inspect: inspect,
    enable: enable,
    disable: disable,
    register: registerCanonicalWorker,
    hasCachedEnabled: hasCachedEnabled,
    clearCachedEnabled: clearRememberedEnabled
  };
})(window, navigator);
