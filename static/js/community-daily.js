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
            if (result.completed_new) {
                window.dispatchEvent(new CustomEvent('gm:daily-learn-completed', { detail: { day_utc: result.day_utc } }));
            }
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
                return;
            }
            learn = data;
            card.hidden = false;
            var title = $('cfDailyLearnTitle');
            if (title) title.textContent = data.status === 'completed'
                ? 'Daily Learn complete' : 'Answer today’s 2 questions';
            renderQuestion(data);
        }).catch(function () { card.hidden = true; });
    }

    function init() {
        window.addEventListener('gm:community-post-created', loadPrompt);
        loadPrompt();
        loadLearn();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
