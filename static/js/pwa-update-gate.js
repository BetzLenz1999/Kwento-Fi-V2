(function (window, document, navigator) {
  'use strict';

  var CHECK_TIMEOUT_MS = 12000;
  var CHECK_OPERATION_TIMEOUT_MS = 22000;
  var WORKER_VERSION_TIMEOUT_MS = 3000;
  var ACTIVE_WORKER_TIMEOUT_MS = 5000;
  var ACTIVATION_TIMEOUT_MS = 15000;
  var ACCEPTED_BUILD_KEY = 'gmPwaAcceptedBuild_v1';
  var INSTALL_BASELINE_PENDING_KEY = 'gmPwaInstallBaselinePending_v1';
  var INSTALL_BASELINE_INTENT_TTL_MS = 7 * 24 * 60 * 60 * 1000;

  function isBuildAcknowledged(buildVersion) {
    if (!buildVersion) return true;
    try { return window.localStorage.getItem(ACCEPTED_BUILD_KEY) === buildVersion; }
    catch (_) { return false; } // Require an explicit confirmation if storage is unavailable.
  }

  function rememberBuild(buildVersion) {
    if (!buildVersion) return false;
    try {
      window.localStorage.setItem(ACCEPTED_BUILD_KEY, buildVersion);
      return true;
    } catch (_) {
      return false;
    }
  }

  function hasInstallBaselineIntent(buildVersion) {
    try {
      var raw = window.localStorage.getItem(INSTALL_BASELINE_PENDING_KEY);
      if (!raw) return false;
      var intent = JSON.parse(raw);
      return !!intent && intent.build === buildVersion
        && Date.now() >= intent.createdAt
        && Date.now() - intent.createdAt <= INSTALL_BASELINE_INTENT_TTL_MS;
    } catch (_) {
      return false;
    }
  }

  function clearInstallBaselineIntent() {
    try { window.localStorage.removeItem(INSTALL_BASELINE_PENDING_KEY); }
    catch (_) { /* Storage may be restricted; never break the app flow. */ }
  }

  function withTimeout(promise, timeoutMs, message) {
    return new Promise(function (resolve, reject) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        reject(new Error(message || 'The operation took too long.'));
      }, timeoutMs);
      Promise.resolve(promise).then(function (value) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      }, function (error) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(error);
      });
    });
  }

  function readWorkerBuildVersion(worker, timeoutMs) {
    timeoutMs = timeoutMs || WORKER_VERSION_TIMEOUT_MS;
    if (!worker || typeof worker.postMessage !== 'function' || typeof window.MessageChannel !== 'function') {
      return Promise.reject(new Error('Could not verify the installed app version.'));
    }
    return new Promise(function (resolve, reject) {
      var channel;
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        if (channel) {
          channel.port1.close();
          channel.port2.close();
        }
        reject(new Error('The app version check took too long.'));
      }, timeoutMs);

      function finish(error, version) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (channel) {
          channel.port1.close();
          channel.port2.close();
        }
        if (error) reject(error);
        else resolve(version);
      }

      try {
        channel = new window.MessageChannel();
        channel.port1.onmessage = function (event) {
          var data = event && event.data;
          if (!data || data.type !== 'BUILD_VERSION' || typeof data.version !== 'string') {
            finish(new Error('The app returned an invalid version response.'));
            return;
          }
          finish(null, data.version);
        };
        worker.postMessage({ type: 'GET_BUILD_VERSION' }, [channel.port2]);
      } catch (error) {
        finish(error || new Error('Could not verify the installed app version.'));
      }
    });
  }

  function isStandalone(win, nav) {
    try {
      if (nav && nav.standalone === true) return true; // iOS home-screen apps
      return !!(win.matchMedia && win.matchMedia('(display-mode: standalone)').matches);
    } catch (_) {
      return false;
    }
  }

  function waitForActiveWorker(registration, timeoutMs) {
    timeoutMs = timeoutMs || 5000;
    if (registration && registration.active) return Promise.resolve(registration.active);
    return new Promise(function (resolve, reject) {
      var finished = false;
      var interval = setInterval(check, 50);
      var timer = setTimeout(function () {
        finish(new Error('The app service worker did not become active in time.'));
      }, timeoutMs);

      function finish(error, worker) {
        if (finished) return;
        finished = true;
        clearInterval(interval);
        clearTimeout(timer);
        if (error) reject(error);
        else resolve(worker);
      }

      function check() {
        if (registration && registration.active) finish(null, registration.active);
      }

      check();
    });
  }

  function targetPath(body) {
    var target = body && body.getAttribute('data-next-url');
    // The post-login destination is same-origin and rooted. Fail closed on a
    // malformed template value rather than turning the gate into an open redirect.
    if (!target || target.charAt(0) !== '/' || target.indexOf('//') === 0
        || target.indexOf('\\') !== -1 || /[\u0000-\u0020\u007f-\u009f]/.test(target) || /\s/.test(target)) {
      return '/community';
    }
    return target;
  }

  function registrationHasCurrentScript(registration) {
    var workers = registration
      ? [registration.active, registration.waiting, registration.installing].filter(Boolean)
      : [];
    if (!workers.length || typeof window.URL !== 'function') return false;
    return workers.every(function (worker) {
      try {
        var script = new window.URL(worker.scriptURL, window.location.href);
        return script.origin === window.location.origin
          && script.pathname === '/static/service-worker.js';
      } catch (_) {
        return false;
      }
    });
  }

  function checkForUpdate(registration, timeoutMs) {
    timeoutMs = timeoutMs || CHECK_TIMEOUT_MS;
    if (!registration || typeof registration.update !== 'function') {
      return Promise.reject(new Error('Service worker update check is unavailable.'));
    }

    return new Promise(function (resolve, reject) {
      var done = false;
      var sawInstallingWorker = false;
      var currentWorker = null;
      var timer = setTimeout(function () {
        finish(null, new Error('The update check took too long.'));
      }, timeoutMs);

      function cleanup() {
        clearTimeout(timer);
        if (registration.removeEventListener) registration.removeEventListener('updatefound', onUpdateFound);
        if (currentWorker && currentWorker.removeEventListener) currentWorker.removeEventListener('statechange', onStateChange);
      }

      function finish(worker, error) {
        if (done) return;
        done = true;
        cleanup();
        if (error) reject(error);
        else resolve(worker ? { worker: worker } : null);
      }

      function onStateChange() {
        if (!currentWorker) return;
        if (currentWorker.state === 'installed') {
          // No active worker means this is the first install, not an app update.
          // Initial installs must not be trapped behind an update prompt.
          if (registration.active && registration.waiting) finish(registration.waiting);
          else finish(null);
        } else if (currentWorker.state === 'redundant') {
          finish(null);
        } else if (currentWorker.state === 'activated') {
          // A first install activates directly instead of waiting. It is not
          // an update and must not strand the user behind the root gate.
          if (registration.waiting && registration.active) finish(registration.waiting);
          else finish(null);
        }
      }

      function watchInstallingWorker() {
        currentWorker = registration.installing;
        if (!currentWorker) return;
        sawInstallingWorker = true;
        if (currentWorker.addEventListener) currentWorker.addEventListener('statechange', onStateChange);
        onStateChange();
      }

      function onUpdateFound() {
        watchInstallingWorker();
      }

      if (registration.addEventListener) registration.addEventListener('updatefound', onUpdateFound);
      if (registration.installing) watchInstallingWorker();

      Promise.resolve().then(function () { return registration.update(); }).then(function () {
        if (done) return;
        if (registration.installing) {
          watchInstallingWorker();
          return;
        }
        if (registration.waiting && registration.active) {
          finish(registration.waiting);
          return;
        }
        if (!sawInstallingWorker) finish(null); // Current service worker is already up to date.
      }).catch(function (error) {
        finish(null, error || new Error('Could not check for an app update.'));
      });
    });
  }

  function start() {
    var body = document.body;
    if (!body) return;
    var target = targetPath(body);
    var buildVersion = body.getAttribute('data-build-version') || '';

    if (!isStandalone(window, navigator)) {
      window.location.replace(target);
      return;
    }
    var installBaselinePending = hasInstallBaselineIntent(buildVersion);

    var title = document.getElementById('gm-pwa-update-title');
    var copy = document.getElementById('gm-pwa-update-copy');
    var status = document.getElementById('gm-pwa-update-status');
    var progress = document.getElementById('gm-pwa-startup-progress');
    var progressFill = document.getElementById('gm-pwa-progress-fill');
    var progressPercent = document.getElementById('gm-pwa-progress-percent');
    var progressStage = document.getElementById('gm-pwa-progress-stage');
    var updateButton = document.getElementById('gm-pwa-update-now');
    var retryButton = document.getElementById('gm-pwa-update-retry');
    var continueButton = document.getElementById('gm-pwa-continue');
    var registration = null;
    var waitingWorker = null;
    var navigating = false;
    var checkGeneration = 0;
    var progressTimer = null;
    var progressStartedAt = 0;
    var progressValue = 1;

    function stopProgress() {
      if (progressTimer !== null) {
        clearInterval(progressTimer);
        progressTimer = null;
      }
    }

    function setProgress(value, stageText) {
      progressValue = Math.max(progressValue, Math.min(99, Math.max(1, Math.floor(value))));
      if (progress) progress.setAttribute('aria-valuenow', String(progressValue));
      if (progressPercent) progressPercent.textContent = progressValue + '%';
      if (progressFill) progressFill.style.width = progressValue + '%';
      if (stageText && progressStage) progressStage.textContent = stageText;
    }

    function startProgress() {
      stopProgress();
      progressValue = 1;
      progressStartedAt = Date.now();
      if (progress) progress.hidden = false;
      setProgress(1, 'Starting KwentoFi…');
      progressTimer = setInterval(function () {
        var elapsed = Date.now() - progressStartedAt;
        // Estimated progress follows elapsed work but asymptotically stops at
        // 99%; only actual startup checkpoints or navigation can finish it.
        var estimate = Math.min(99, 1 + Math.floor(98 * (1 - Math.exp(-elapsed / 4500))));
        setProgress(estimate);
      }, 250);
    }

    document.documentElement.classList.remove('gm-pwa-update-gate-pending');

    function goToApp() {
      if (navigating) return;
      navigating = true;
      checkGeneration += 1;
      setProgress(99, 'Opening your KwentoFi page…');
      if (status) status.textContent = 'App checks complete. Opening your page…';
      stopProgress();
      window.location.replace(target);
    }

    function showChecking() {
      if (title) title.textContent = 'Checking KwentoFi';
      if (copy) copy.textContent = 'We’ll check for an app update before opening your page.';
      if (status) status.textContent = 'Preparing your app…';
      startProgress();
      if (updateButton) updateButton.hidden = true;
      if (retryButton) retryButton.hidden = true;
      if (continueButton) continueButton.hidden = true;
    }

    function showUpdate(worker) {
      stopProgress();
      if (progress) progress.hidden = true;
      waitingWorker = worker;
      if (title) title.textContent = worker ? 'Update required' : 'Confirm app version';
      if (copy) copy.textContent = worker
        ? 'A new KwentoFi app version is ready. Update now to continue.'
        : 'This KwentoFi version is installed. Confirm it before opening your page.';
      if (status) status.textContent = worker
        ? 'The new version is ready to install.'
        : 'Confirm this app version to continue.';
      if (updateButton) {
        updateButton.hidden = false;
        updateButton.disabled = false;
        updateButton.textContent = worker ? 'Update app' : 'Confirm version';
      }
      if (retryButton) retryButton.hidden = true;
      if (continueButton) continueButton.hidden = true;
    }

    function showCheckError(error) {
      stopProgress();
      if (progress) progress.hidden = true;
      if (title) title.textContent = 'Could not check for updates';
      if (copy) copy.textContent = 'Connect to the internet and try again, or continue with the version already on this device.';
      if (status) status.textContent = (error && error.message) || 'The update check is unavailable.';
      if (updateButton) updateButton.hidden = true;
      if (retryButton) retryButton.hidden = false;
      if (continueButton) continueButton.hidden = false;
    }

    function acknowledgeVerifiedBuild(onVerified) {
      setProgress(92, 'Verifying the installed app…');
      var candidates = [];
      if (registration && registration.active) candidates.push(registration.active);
      var controller = navigator.serviceWorker.controller;
      if (controller && candidates.indexOf(controller) === -1) candidates.push(controller);
      function verifyCandidate(index) {
        if (index >= candidates.length) {
          return Promise.reject(new Error('The active app worker does not match this release. Please retry the update check.'));
        }
        return readWorkerBuildVersion(candidates[index], WORKER_VERSION_TIMEOUT_MS).then(function (version) {
          if (version === buildVersion) return version;
          return verifyCandidate(index + 1);
        }, function () {
          return verifyCandidate(index + 1);
        });
      }
      verifyCandidate(0).then(function () {
        if (rememberBuild(buildVersion)) clearInstallBaselineIntent();
        onVerified();
      }).catch(showCheckError);
    }

    function check() {
      var generation = ++checkGeneration;
      showChecking();
      if (!navigator.serviceWorker || typeof navigator.serviceWorker.register !== 'function') {
        showCheckError(new Error('This browser cannot check for app updates.'));
        return;
      }

      var checkOperation = Promise.resolve().then(function () {
        return typeof navigator.serviceWorker.getRegistration === 'function'
          ? navigator.serviceWorker.getRegistration('/')
          : null;
      }).then(function (existing) {
        setProgress(18, 'Preparing the app update check…');
        if (navigator.onLine === false) throw new Error('You are offline. Connect to the internet to check for an update.');
        function registerCurrentWorker() {
          return navigator.serviceWorker.register('/static/service-worker.js', { scope: '/' })
            .then(function (reg) { return { registration: reg, worker: null }; });
        }
        if (existing && !registrationHasCurrentScript(existing)) {
          if (typeof existing.unregister !== 'function') {
            throw new Error('A legacy app worker could not be migrated. Please retry.');
          }
          return Promise.resolve(existing.unregister()).then(registerCurrentWorker);
        }
        if (existing) return { registration: existing, worker: null };
        return registerCurrentWorker();
      }).then(function (reg) {
        if (!reg || reg.worker) return reg;
        setProgress(38, 'Checking for a newer KwentoFi version…');
        return checkForUpdate(reg.registration, CHECK_TIMEOUT_MS).then(function (result) {
          setProgress(68, 'Verifying the app version…');
          var worker = result && result.worker;
          var versionSourcePromise = worker
            ? Promise.resolve(worker)
            : waitForActiveWorker(reg.registration, ACTIVE_WORKER_TIMEOUT_MS);
          return versionSourcePromise.then(function (versionSource) {
            return readWorkerBuildVersion(versionSource, WORKER_VERSION_TIMEOUT_MS);
          }).then(function (version) {
            if (version !== buildVersion) {
              throw new Error('The available app worker does not match this release. Please retry the update check.');
            }
            setProgress(90, 'Getting your page ready…');
            return { registration: reg.registration, worker: worker, workerVersion: version };
          });
        });
      });

      withTimeout(
        checkOperation,
        CHECK_OPERATION_TIMEOUT_MS,
        'The service worker check did not finish in time.'
      ).then(function (result) {
        if (generation !== checkGeneration) return;
        registration = result.registration;
        if (result.worker) showUpdate(result.worker);
        else if (installBaselinePending) acknowledgeVerifiedBuild(goToApp);
        else if (!isBuildAcknowledged(buildVersion)) showUpdate(null);
        else goToApp();
      }).catch(function (error) {
        if (generation !== checkGeneration) return;
        showCheckError(error);
      });
    }

    if (updateButton) updateButton.addEventListener('click', function () {
      if (!waitingWorker || !registration) {
        if (!waitingWorker && registration) {
          acknowledgeVerifiedBuild(goToApp);
        } else {
          check();
        }
        return;
      }
      updateButton.disabled = true;
      updateButton.textContent = 'Installing update…';
      if (status) status.textContent = 'Installing the new version. Please keep the app open…';

      var completed = false;
      var activationTimer = setTimeout(function () {
        if (completed) return;
        completed = true;
        showCheckError(new Error('The update did not finish. You can retry or continue with the current version.'));
      }, ACTIVATION_TIMEOUT_MS);

      function updated() {
        if (completed) return;
        completed = true;
        clearTimeout(activationTimer);
        acknowledgeVerifiedBuild(goToApp);
      }

      if (navigator.serviceWorker.addEventListener) {
        navigator.serviceWorker.addEventListener('controllerchange', updated, { once: true });
      }
      if (waitingWorker.addEventListener) {
        waitingWorker.addEventListener('statechange', function () {
          // controllerchange is not guaranteed in every standalone launch
          // context. The activated worker is authoritative once it becomes the
          // registration's active worker, even if an older controller remains.
          if (waitingWorker.state === 'activated'
              && (!registration.active || registration.active === waitingWorker)) updated();
        });
      }
      try {
        waitingWorker.postMessage({ type: 'SKIP_WAITING' });
      } catch (error) {
        clearTimeout(activationTimer);
        completed = true;
        showCheckError(error || new Error('Could not activate the update.'));
      }
    });

    if (retryButton) retryButton.addEventListener('click', check);
    if (continueButton) continueButton.addEventListener('click', goToApp);
    check();
  }

  window.GMPwaUpdateGate = {
    start: start,
    isStandalone: isStandalone,
    targetPath: targetPath,
    checkForUpdate: checkForUpdate,
    withTimeout: withTimeout,
    readWorkerBuildVersion: readWorkerBuildVersion,
    isBuildAcknowledged: isBuildAcknowledged,
    rememberBuild: rememberBuild,
    hasInstallBaselineIntent: hasInstallBaselineIntent,
    clearInstallBaselineIntent: clearInstallBaselineIntent,
    registrationHasCurrentScript: registrationHasCurrentScript,
    waitForActiveWorker: waitForActiveWorker
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})(window, document, navigator);
