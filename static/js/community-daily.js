/* Community Daily Prompt and Daily Learn cards.
   All server-provided content is written with textContent; correctness and
   progress are accepted only from the authenticated server endpoints. */
(function () {
    'use strict';

    var BOOT = window.GM_COMMUNITY_BOOT || {};
    var WALLET = BOOT.wallet || '';
    var prompt = null;
    var learn = null;
    var selectedAnswer = null;
    var lastResult = null;
    var _busy = false;
    var lastReward = null;
    var lastEarningsRefresh = null;

    function $(id) { return document.getElementById(id); }
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }
    function api(path, options) {
        var opts = options || {};
        opts.credentials = 'same-origin';
        opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
        return fetch(path, opts).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                return { ok: response.ok, status: response.status, data: data };
            });
        });
    }
    function connect() {
        if (typeof window.openGoodMarketGetStarted === 'function') window.openGoodMarketGetStarted();
    }

    function loadPrompt() {
        var card = $('cfDailyPrompt');
        if (!card) return Promise.resolve();
        return api('/community/api/daily-prompt').then(function (result) {
            var data = result.data || {};
            if (!data.success || !data.prompt) { card.hidden = true; return; }
            prompt = data.prompt;
            if (window.GMCommunity && window.GMCommunity.setDailyPrompt) {
                window.GMCommunity.setDailyPrompt(prompt);
            }
            var title = $('cfDailyPromptTitle');
            var body = $('cfDailyPromptBody');
            var count = $('cfDailyPromptCount');
            var answer = $('cfAnswerPromptBtn');
            if (title) title.textContent = prompt.title || 'Today’s prompt';
            if (body) body.textContent = prompt.body || '';
            if (count) {
                var n = Number(prompt.response_count) || 0;
                count.textContent = n + ' response' + (n === 1 ? '' : 's');
            }
            if (answer) {
                answer.textContent = prompt.has_responded ? 'You’ve answered' : 'Answer prompt';
                answer.disabled = !!prompt.has_responded;
                answer.setAttribute('aria-disabled', prompt.has_responded ? 'true' : 'false');
            }
            card.hidden = false;
        }).catch(function () { card.hidden = true; });
    }

    function renderLearnMessage(message, actionLabel, action) {
        var root = $('cfDailyLearnContent');
        if (!root) return;
        root.textContent = '';
        root.appendChild(el('p', 'cf-daily-learn-result', message));
        if (actionLabel && action) {
            var button = el('button', 'cf-daily-learn-next', actionLabel);
            button.type = 'button';
            button.onclick = action;
            root.appendChild(button);
        }
    }

    function rewardStatusMessage() {
        if (!lastReward || !lastReward.status) return 'Reward status is not available yet.';
        if (lastReward.status === 'credited') {
            return lastEarningsRefresh === true
                ? 'Your completion reward is now reflected in Community G$ Earnings.'
                : (lastEarningsRefresh === false
                    ? 'Your completion reward was credited, but Community G$ Earnings could not refresh. Reload later to verify your balance.'
                    : 'Your completion reward was credited. Refreshing Community G$ Earnings…');
        }
        if (lastReward.status === 'already_credited') {
            return lastEarningsRefresh === false
                ? 'Your completion reward is already in the ledger, but Community G$ Earnings could not refresh. Reload later to verify your balance.'
                : (lastEarningsRefresh === true
                    ? 'Your completion reward is already included in Community G$ Earnings.'
                    : 'Your completion reward is already in the ledger. Refreshing Community G$ Earnings…');
        }
        if (lastReward.status === 'pending') {
            return lastReward.reason === 'verified_wallet_required'
                ? 'Your quiz is saved, but the remaining reward is pending because this wallet is not eligible to earn Community G$.'
                : 'Your quiz is saved. The remaining question reward is pending and will be retried; check Community G$ Earnings again later.';
        }
        var reason = lastReward.reason;
        var message = reason === 'daily_cap_reached'
            ? 'Your completion is saved, but the separate Daily Learn allowance for this UTC day has been used; no additional reward was added.'
            : (reason === 'no_correct_answers'
                ? 'Your completion is saved. No Community G$ was earned because there were no correct answers.'
                : (reason === 'verified_wallet_required'
                    ? 'Your completion is saved, but only verified wallets can earn Community G$.'
                    : (reason === 'reward_disabled' || reason === 'rewards_disabled'
                        ? 'Your completion is saved, but the Daily Learn reward is currently unavailable.'
                        : 'Your completion is saved, but the reward could not be confirmed. Check Community G$ Earnings again later.')));
        return lastEarningsRefresh === false &&
            lastReward.reason !== 'rewards_disabled' && lastReward.reason !== 'reward_disabled'
            ? message + ' The earnings card could not refresh.' : message;
    }

    function renderRewardStatus() {
        var note = $('cfDailyLearnRewardStatus');
        if (note) note.textContent = rewardStatusMessage();
    }

    function refreshCompletionEarnings(data) {
        lastReward = data && data.reward ? data.reward : null;
        lastEarningsRefresh = null;
        renderRewardStatus();
        // The done strip quotes rewardStatusMessage(), and lastReward only exists
        // from here on — re-render it so the reason is right rather than the
        // "not available yet" placeholder showLearnDone() had to use.
        syncLearnDoneDetail();
        var detail = { day_utc: data && data.day_utc, reward: lastReward };
        window.dispatchEvent(new CustomEvent('gm:daily-learn-completed', { detail: detail }));
        // The Community listener starts the card refresh and attaches its
        // coalesced promise to this event detail. Fall back for isolated embeds.
        var refresh = detail.refreshPromise ||
            (window.GMCommunity && typeof window.GMCommunity.loadRewards === 'function'
                ? window.GMCommunity.loadRewards() : Promise.resolve(false));
        return Promise.resolve(refresh).then(function (ok) {
            lastEarningsRefresh = ok === true;
            renderRewardStatus();
            // The message changes once we know whether the earnings card
            // refreshed ("credited" vs "could not refresh"), so sync again.
            syncLearnDoneDetail();
            return lastEarningsRefresh;
        }).catch(function () {
            lastEarningsRefresh = false;
            renderRewardStatus();
            syncLearnDoneDetail();
            return false;
        });
    }

    function syncLearnDoneDetail() {
        var detail = $('cfLearnDoneDetail');
        if (detail) detail.textContent = learnDoneDetailText();
    }

    function renderCompleted(data) {
        var score = Number(data.score) || 0;
        var total = Number(data.total_questions) || 2;
        var root = $('cfDailyLearnContent');
        if (!root) return;
        root.textContent = '';
        root.appendChild(el('p', 'cf-daily-learn-result',
            'You completed today’s Daily Learn: ' + score + '/' + total + '.'));
        var note = el('p', 'cf-daily-learn-result',
            'Your completion is saved. Daily Learn resets at 00:00 UTC.');
        root.appendChild(note);
        var rewardNote = el('p', 'cf-daily-learn-result', rewardStatusMessage());
        rewardNote.id = 'cfDailyLearnRewardStatus';
        root.appendChild(rewardNote);
        if (WALLET && window.GMCommunity && window.GMCommunity.prefillDailyLearnReflection) {
            var share = el('button', 'cf-daily-learn-share', 'Share to Feed');
            share.type = 'button';
            share.onclick = function () {
                window.GMCommunity.prefillDailyLearnReflection();
            };
            root.appendChild(share);
        }
        if (prompt && !prompt.has_responded && window.GMCommunity) {
            var answerPrompt = el('button', 'cf-daily-learn-next', 'Answer today’s community prompt');
            answerPrompt.type = 'button';
            answerPrompt.onclick = function () {
                window.GMCommunity.setDailyPrompt(prompt);
                window.GMCommunity.answerPrompt();
            };
            root.appendChild(answerPrompt);
        }
    }

    function renderAnswerFeedback(result) {
        lastResult = result;
        var root = $('cfDailyLearnContent');
        if (!root) return;
        root.textContent = '';
        root.appendChild(el('p', 'cf-daily-learn-result', result.correct ? 'Correct.' : 'Not quite.'));
        if (result.explanation) root.appendChild(el('p', 'cf-daily-learn-result', result.explanation));
        if (result.status === 'completed') {
            var rewardNote = el('p', 'cf-daily-learn-result', rewardStatusMessage());
            rewardNote.id = 'cfDailyLearnRewardStatus';
            root.appendChild(rewardNote);
        }
        var progress = $('cfDailyLearnProgress');
        if (progress) progress.textContent = (Number(result.completed_questions) || 0) + '/2 complete';
        var next = el('button', 'cf-daily-learn-next',
            result.status === 'completed' ? 'See today’s result' : 'Continue');
        next.type = 'button';
        next.onclick = function () {
            if (result.status === 'completed') renderCompleted(result);
            else loadLearn();
        };
        root.appendChild(next);
    }

    function submitAnswer(question) {
        if (_busy || selectedAnswer === null) return;
        if (!WALLET) {
            renderLearnMessage('Connect a verified wallet to submit your Daily Learn answers.', 'Sign in', connect);
            return;
        }
        _busy = true;
        var submit = $('cfDailyLearnSubmit');
        if (submit) { submit.disabled = true; submit.textContent = 'Checking…'; }
        api('/community/api/daily-learn/answer', {
            method: 'POST',
            body: JSON.stringify({
                day_utc: learn.day_utc,
                question_id: question.question_id,
                answer: selectedAnswer
            })
        }).then(function (response) {
            var result = response.data || {};
            if (!response.ok || !result.success) {
                if (response.status === 403) {
                    renderLearnMessage(result.error || 'A verified wallet is required to answer.', 'Open wallet', function () {
                        window.location.href = '/wallet';
                    });
                    return;
                }
                if (response.status === 401) {
                    renderLearnMessage('Connect a verified wallet to submit your answers.', 'Sign in', connect);
                    return;
                }
                throw new Error(result.message || result.error || 'Your answer could not be saved. Please try again.');
            }
            if (result.status === 'completed') refreshCompletionEarnings(result);
            renderAnswerFeedback(result);
        }).catch(function (error) {
            renderLearnMessage((error && error.message) || 'Daily Learn is temporarily unavailable.');
        }).then(function () {
            _busy = false;
        });
    }

    function renderQuestion(data) {
        var root = $('cfDailyLearnContent');
        if (!root) return;
        root.textContent = '';
        selectedAnswer = null;
        var question = (data.questions || [])[0];
        var progress = $('cfDailyLearnProgress');
        if (progress) {
            progress.textContent = (Number(data.completed_questions) || 0) + '/2 complete';
        }
        if (data.status === 'completed') {
            renderCompleted(data);
            return;
        }
        if (!question) {
            renderLearnMessage('Today’s Daily Learn questions are not available right now.');
            return;
        }
        if (!WALLET) {
            root.appendChild(el('p', 'cf-daily-learn-reset', 'Preview — connect a verified wallet to answer and save progress.'));
        }
        root.appendChild(el('p', 'cf-daily-learn-question', question.question || ''));
        var options = el('div', 'cf-daily-learn-options');
        options.setAttribute('role', 'group');
        options.setAttribute('aria-label', 'Answer choices');
        var submitButton = null;
        (question.options || []).forEach(function (label, index) {
            var choice = el('button', 'cf-daily-learn-option', String.fromCharCode(65 + index) + '. ' + label);
            choice.type = 'button';
            choice.setAttribute('aria-pressed', 'false');
            choice.onclick = function () {
                selectedAnswer = index;
                Array.prototype.forEach.call(options.children, function (node) {
                    node.setAttribute('aria-pressed', node === choice ? 'true' : 'false');
                });
                if (submitButton) submitButton.disabled = false;
            };
            options.appendChild(choice);
        });
        root.appendChild(options);
        var button = el('button', 'cf-daily-learn-submit', WALLET ? 'Submit answer' : 'Sign in to answer');
        button.id = 'cfDailyLearnSubmit';
        button.type = 'button';
        button.disabled = !!WALLET;
        submitButton = button;
        button.onclick = function () {
            if (!WALLET) { connect(); return; }
            if (selectedAnswer === null) {
                renderLearnMessage('Choose an answer before continuing.', 'Back to question', function () { renderQuestion(data); });
                return;
            }
            submitAnswer(question);
        };
        root.appendChild(button);
    }

    function loadLearn() {
        var card = $('cfDailyLearn');
        if (!card) return Promise.resolve();
        return api('/community/api/daily-learn').then(function (response) {
            var data = response.data || {};
            if (!response.ok || !data.success) {
                card.hidden = true;
                hideLearnDone();
                return;
            }
            learn = data;
            if (data.status === 'completed') {
                // Done for today, so the card goes away instead of sitting on
                // the feed all day. It comes back on its own when the UTC day
                // rolls over: the snapshot is keyed on `day_utc`, so tomorrow's
                // attempt is a fresh, incomplete one and this branch is skipped.
                // Deliberately NOT reopened if an admin republishes the quiz
                // mid-day — re-asking a member a quiz they already finished is a
                // worse experience than waiting for the reset.
                card.hidden = true;
                showLearnDone(data);
                // Still refreshes the Community G$ card and still dispatches
                // gm:daily-learn-completed — do not drop this call, it is the
                // only thing that credits/announces the completion.
                refreshCompletionEarnings(data);
                return;
            }
            hideLearnDone();
            card.hidden = false;
            var title = $('cfDailyLearnTitle');
            if (title) title.textContent = 'Answer today’s 2 questions';
            renderQuestion(data);
        }).catch(function () { card.hidden = true; });
    }

    /* ── "Daily Learn done today" strip ──────────────────────────────────────
       The one-line replacement for the card. It carries the reward reason, which
       is the only place a member can find out why a completion did not pay
       (daily allowance used, no correct answers, wallet not verified, rewards
       off). Collapsed by default so the composer stays clean. */
    function learnDoneDetailText() {
        var score = learn && learn.score != null ? learn.score : null;
        var total = learn && learn.total_questions ? learn.total_questions : 2;
        var head = score != null
            ? 'You scored ' + score + '/' + total + ' today. '
            : 'Today’s Daily Learn is complete. ';
        return head + rewardStatusMessage();
    }

    function showLearnDone(data) {
        var strip = $('cfLearnDone');
        if (!strip) return;
        var text = $('cfLearnDoneText');
        if (text) {
            var score = data && data.score != null ? data.score : null;
            var total = data && data.total_questions ? data.total_questions : 2;
            text.textContent = score != null
                ? 'Daily Learn done today · ' + score + '/' + total
                : 'Daily Learn done today';
        }
        strip.hidden = false;
        var detail = $('cfLearnDoneDetail');
        if (detail) {
            detail.textContent = learnDoneDetailText();
            // Keep it collapsed: the strip is the summary, the detail is opt-in.
            detail.hidden = true;
        }
        var toggle = $('cfLearnDoneToggle');
        if (toggle) toggle.setAttribute('aria-expanded', 'false');
    }

    function hideLearnDone() {
        var strip = $('cfLearnDone');
        if (strip) strip.hidden = true;
        var detail = $('cfLearnDoneDetail');
        if (detail) detail.hidden = true;
        var toggle = $('cfLearnDoneToggle');
        if (toggle) toggle.setAttribute('aria-expanded', 'false');
    }

    function toggleLearnDoneDetail() {
        var detail = $('cfLearnDoneDetail');
        var toggle = $('cfLearnDoneToggle');
        if (!detail || !toggle) return;
        var next = detail.hidden;
        detail.hidden = !next;
        toggle.setAttribute('aria-expanded', next ? 'true' : 'false');
        if (next) detail.textContent = learnDoneDetailText();
    }

    function init() {
        window.addEventListener('gm:community-post-created', loadPrompt);
        loadPrompt();
        loadLearn();
    }

    /* The done strip's Details toggle is wired with an inline onclick, which
       resolves on `window`. community-feed.js loads first and assigns
       `GMCommunity` synchronously, so attaching here is safe — and the guard
       keeps this bundle importable on its own (an isolated embed). */
    if (window.GMCommunity) {
        window.GMCommunity.toggleLearnDoneDetail = toggleLearnDoneDetail;
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
