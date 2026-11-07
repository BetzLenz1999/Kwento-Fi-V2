(function () {
    'use strict';

    var ENDPOINT = '/api/admin/weekly-exp/preview';
    var ADDRESS_RE = /^0x[a-fA-F0-9]{40}$/;
    var PAGE_SIZE = 100;
    var rankedData = [];
    var consolationData = [];
    var rankedPage = 0;
    var consolationPage = 0;

    function byId(id) { return document.getElementById(id); }
    function textNode(tag, value, className) {
        var node = document.createElement(tag);
        node.textContent = value == null ? '' : String(value);
        if (className) node.className = className;
        return node;
    }
    function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
    function formatNumber(value) {
        var number = Number(value);
        return Number.isFinite(number) ? number.toLocaleString() : '—';
    }
    function setStatus(message, isError) {
        var node = byId('weeklyExpPreviewStatus');
        if (!node) return;
        node.textContent = message;
        node.classList.toggle('is-error', !!isError);
    }
    function addressCell(wallet) {
        var td = document.createElement('td');
        td.className = 'weekly-exp-wallet';
        if (!ADDRESS_RE.test(String(wallet || ''))) {
            td.textContent = 'Invalid wallet';
            return td;
        }
        var link = document.createElement('a');
        link.href = 'https://celoscan.io/address/' + encodeURIComponent(wallet);
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = wallet;
        td.appendChild(link);
        return td;
    }
    function renderChecks(checks) {
        var list = byId('weeklyExpPreviewChecks');
        clear(list);
        (Array.isArray(checks) ? checks : []).forEach(function (check) {
            var item = textNode('li', (check.passed ? 'PASS — ' : 'BLOCK — ') + String(check.message || check.key || 'Check'));
            if (check.passed) item.className = 'passed';
            list.appendChild(item);
        });
    }
    function addMetric(container, label, value) {
        var card = document.createElement('div');
        card.className = 'weekly-exp-metric';
        card.appendChild(textNode('span', label));
        card.appendChild(textNode('strong', value == null || value === '' ? '—' : value));
        container.appendChild(card);
    }
    function renderMetrics(data) {
        var container = byId('weeklyExpPreviewMetrics');
        var allocation = data.allocation || {};
        var snapshot = data.snapshot || {};
        clear(container);
        addMetric(container, 'Preview state', data.preview_status === 'ready_for_admin_review' ? 'Ready for review' : 'Blocked');
        addMetric(container, 'UTC week', snapshot.week_start || '—');
        addMetric(container, 'Snapshot updated', snapshot.generated_at || '—');
        addMetric(container, 'Participants', formatNumber(snapshot.participant_count));
        addMetric(container, 'Ranked liability', (allocation.ranked_liability_gd || '0') + ' G$');
        addMetric(container, 'Consolation liability', (allocation.consolation_liability_gd || '0') + ' G$');
        addMetric(container, 'Total liability', (allocation.total_liability_gd || '0') + ' G$');
        addMetric(container, 'Configured max budget', allocation.maximum_budget_gd == null ? 'Not configured' : allocation.maximum_budget_gd + ' G$');
        addMetric(container, 'Verified treasury balance', allocation.funded_balance_gd == null ? 'Unavailable' : allocation.funded_balance_gd + ' G$');
        addMetric(container, 'Treasury balance read at block', allocation.treasury_block == null ? 'Unavailable' : formatNumber(allocation.treasury_block));
        addMetric(container, 'Budget headroom', allocation.budget_headroom_gd == null ? 'Unavailable' : allocation.budget_headroom_gd + ' G$');
        addMetric(container, 'Consolation recipients', formatNumber(allocation.consolation_recipient_count));
        var cap = byId('weeklyExpMaxBudget');
        if (cap) cap.textContent = allocation.maximum_budget_gd == null
            ? 'Not configured' : allocation.maximum_budget_gd + ' G$';
    }
    function updatePager(prefix, rows, page) {
        var pageCount = Math.ceil(rows.length / PAGE_SIZE);
        var info = byId(prefix + 'PageInfo');
        var previous = byId(prefix + 'Prev');
        var next = byId(prefix + 'Next');
        if (info) info.textContent = pageCount ? 'Page ' + (page + 1) + ' of ' + pageCount + ' · ' + formatNumber(rows.length) + ' recipients' : 'Page 0 of 0';
        if (previous) previous.disabled = pageCount < 2 || page <= 0;
        if (next) next.disabled = pageCount < 2 || page >= pageCount - 1;
    }
    function renderRanked(rows) {
        rankedData = Array.isArray(rows) ? rows : [];
        rankedPage = 0;
        drawRankedPage();
    }
    function drawRankedPage() {
        var body = byId('weeklyExpRankedRows');
        clear(body);
        if (!rankedData.length) {
            var empty = document.createElement('tr');
            var cell = textNode('td', 'No ranked recipients in this snapshot.', 'weekly-exp-empty');
            cell.colSpan = 9;
            empty.appendChild(cell);
            body.appendChild(empty);
            updatePager('weeklyExpRanked', rankedData, rankedPage);
            return;
        }
        var fragment = document.createDocumentFragment();
        rankedData.slice(rankedPage * PAGE_SIZE, (rankedPage + 1) * PAGE_SIZE).forEach(function (row) {
            var tr = document.createElement('tr');
            tr.appendChild(textNode('td', formatNumber(row.rank)));
            tr.appendChild(addressCell(row.wallet_address));
            tr.appendChild(textNode('td', formatNumber(row.total_exp)));
            var parts = row.exp_breakdown || {};
            ['checkin_exp', 'daily_task_exp', 'garden_exp', 'streak_exp'].forEach(function (key) {
                tr.appendChild(textNode('td', formatNumber(parts[key])));
            });
            tr.appendChild(textNode('td', formatNumber(row.substantive_days)));
            tr.appendChild(textNode('td', String(row.payout_gd || '0') + ' G$'));
            fragment.appendChild(tr);
        });
        body.appendChild(fragment);
        updatePager('weeklyExpRanked', rankedData, rankedPage);
    }
    function renderConsolation(rows) {
        consolationData = Array.isArray(rows) ? rows : [];
        consolationPage = 0;
        drawConsolationPage();
    }
    function drawConsolationPage() {
        var body = byId('weeklyExpConsolationRows');
        clear(body);
        if (!consolationData.length) {
            var empty = document.createElement('tr');
            var cell = textNode('td', 'No eligible consolation recipients for these settings.', 'weekly-exp-empty');
            cell.colSpan = 5;
            empty.appendChild(cell);
            body.appendChild(empty);
            updatePager('weeklyExpConsolation', consolationData, consolationPage);
            return;
        }
        var fragment = document.createDocumentFragment();
        consolationData.slice(consolationPage * PAGE_SIZE, (consolationPage + 1) * PAGE_SIZE).forEach(function (row) {
            var tr = document.createElement('tr');
            tr.appendChild(textNode('td', formatNumber(row.rank)));
            tr.appendChild(addressCell(row.wallet_address));
            tr.appendChild(textNode('td', formatNumber(row.total_exp)));
            tr.appendChild(textNode('td', formatNumber(row.substantive_days)));
            tr.appendChild(textNode('td', String(row.payout_gd || '0') + ' G$'));
            fragment.appendChild(tr);
        });
        body.appendChild(fragment);
        updatePager('weeklyExpConsolation', consolationData, consolationPage);
    }
    function renderTieNotes(tieResolution) {
        var node = byId('weeklyExpTieNotes');
        clear(node);
        var policy = String((tieResolution && tieResolution.policy) || 'Tie policy unavailable.');
        var groups = (tieResolution && Array.isArray(tieResolution.groups)) ? tieResolution.groups : [];
        node.appendChild(textNode('p', policy + ' Final policy approved: ' + (tieResolution && tieResolution.final_policy_approved ? 'yes' : 'no') + '.'));
        node.appendChild(textNode('p', groups.length
            ? 'Tie groups intersecting the configured Top N: ' + groups.map(function (group) { return (group.ranks || []).join(' / '); }).join('; ')
            : 'No tied rank group intersects the configured Top N.'));
    }
    function render(data) {
        var container = byId('weeklyExpPreviewResult');
        if (container) container.hidden = false;
        renderMetrics(data);
        renderChecks(data.checks);
        renderRanked(data.ranked_recipients);
        renderConsolation(data.consolation_users);
        renderTieNotes(data.tie_resolution);
        setStatus(data.preview_status === 'ready_for_admin_review'
            ? 'Preview passed the configured checks. This is not final approval.'
            : 'Preview is blocked. Review each failed check before proceeding.',
        data.preview_status !== 'ready_for_admin_review');
    }
    function inputValue(id) { return byId(id) ? byId(id).value : ''; }
    function readSettings() {
        var topNInput = byId('weeklyExpTopN');
        var topNMax = Number((topNInput && topNInput.getAttribute('data-max')) || 10000);
        var topN = Math.floor(Number(inputValue('weeklyExpTopN')));
        if (!isFinite(topN) || topN < 1) { topN = 1; }
        if (topN > topNMax) { topN = topNMax; }
        var fixed = inputValue('weeklyExpFixedPrizes').split(',').map(function (value) {
            return value.trim();
        }).filter(Boolean);
        return {
            top_n: topN,
            distribution: inputValue('weeklyExpDistribution'),
            ranked_pool_gd: inputValue('weeklyExpRankedPool').trim(),
            fixed_prizes_gd: fixed,
            minimum_exp: Number(inputValue('weeklyExpMinimumExp')),
            minimum_substantive_days: Number(inputValue('weeklyExpMinimumDays')),
            consolation_gd: inputValue('weeklyExpConsolation').trim()
        };
    }
    async function preview() {
        var button = byId('weeklyExpPreviewButton');
        if (button) { button.disabled = true; button.textContent = 'Checking snapshot and balance…'; }
        setStatus('Loading the current-week snapshot and verifying Celo funding…', false);
        try {
            var response = await fetch(ENDPOINT, {
                method: 'POST',
                credentials: 'same-origin',
                cache: 'no-store',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify(readSettings())
            });
            var data = await response.json();
            if (!response.ok || !data || data.success !== true) {
                throw new Error((data && data.error) || 'Admin preview request failed (' + response.status + ').');
            }
            render(data);
        } catch (error) {
            setStatus((error && error.message) || 'Unable to load the preview.', true);
        } finally {
            if (button) { button.disabled = false; button.textContent = 'Preview allocation & budget'; }
        }
    }
    function setConfigStatus(message, isError) {
        var node = byId('weeklyExpConfigStatus');
        if (!node) return;
        node.textContent = message;
        node.classList.toggle('is-error', !!isError);
    }
    function configWeek() {
        var value = inputValue('weeklyExpConfigWeek');
        return value ? value : undefined;
    }
    function collectSettings() {
        var settings = readSettings();
        return {
            distribution: settings.distribution,
            pool_gd: settings.ranked_pool_gd,
            fixed_prizes_gd: settings.fixed_prizes_gd,
            consolation_gd: settings.consolation_gd,
            top_n: settings.top_n,
            minimum_exp: settings.minimum_exp,
            minimum_substantive_days: settings.minimum_substantive_days
        };
    }
    async function configRequest(method, body, url) {
        var response = await fetch(url || '/api/admin/weekly-exp/config', {
            method: method,
            credentials: 'same-origin',
            cache: 'no-store',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
            body: body ? JSON.stringify(body) : undefined
        });
        var data = await response.json();
        if (!response.ok || !data || data.success !== true) {
            throw new Error((data && data.error) || 'Configuration request failed (' + response.status + ').');
        }
        return data;
    }
    async function saveConfig() {
        var button = byId('weeklyExpSaveConfigButton');
        if (button) button.disabled = true;
        try {
            var body = {
                week_start: configWeek(),
                reason: inputValue('weeklyExpConfigReason'),
                settings: collectSettings()
            };
            var data = await configRequest('POST', body);
            setConfigStatus('Scheduled for ' + (data.week_start || '') + ' at version ' + (data.config_version || '') + '.', false);
        } catch (error) {
            setConfigStatus((error && error.message) || 'Unable to save the configuration.', true);
        } finally {
            if (button) button.disabled = false;
        }
    }
    async function lockConfig() {
        var button = byId('weeklyExpLockConfigButton');
        if (button) button.disabled = true;
        try {
            var data = await configRequest('POST', { week_start: configWeek() },
                '/api/admin/weekly-exp/config/lock');
            setConfigStatus(data.already_locked
                ? 'Period already locked.'
                : 'Period locked for ' + (data.week_start || '') + '.', false);
        } catch (error) {
            setConfigStatus((error && error.message) || 'Unable to lock the configuration.', true);
        } finally {
            if (button) button.disabled = false;
        }
    }
    async function loadConfig() {
        var week = configWeek();
        try {
            var url = '/api/admin/weekly-exp/config' + (week ? '?week_start=' + encodeURIComponent(week) : '');
            var data = await configRequest('GET', null, url);
            if (!data.configured) {
                setConfigStatus('No configuration saved for that period yet.', false);
                return;
            }
            if (byId('weeklyExpTopN')) byId('weeklyExpTopN').value = data.top_n;
            if (byId('weeklyExpDistribution')) byId('weeklyExpDistribution').value = data.distribution;
            if (byId('weeklyExpRankedPool')) byId('weeklyExpRankedPool').value = data.pool_gd;
            if (byId('weeklyExpFixedPrizes')) {
                byId('weeklyExpFixedPrizes').value = (data.fixed_prizes_gd || []).join(',');
            }
            if (byId('weeklyExpConsolation')) byId('weeklyExpConsolation').value = data.consolation_gd;
            if (byId('weeklyExpMinimumExp')) byId('weeklyExpMinimumExp').value = data.minimum_exp;
            if (byId('weeklyExpMinimumDays')) byId('weeklyExpMinimumDays').value = data.minimum_substantive_days;
            updateDistributionFields();
            setConfigStatus('Loaded ' + data.status + ' configuration (version ' + data.config_version + ').', false);
        } catch (error) {
            setConfigStatus((error && error.message) || 'Unable to load the configuration.', true);
        }
    }
    function setFinalizeStatus(message, isError) {
        var node = byId('weeklyExpFinalizeStatus');
        if (!node) return;
        node.textContent = message;
        node.classList.toggle('is-error', !!isError);
    }
    async function finalizeWeek() {
        var button = byId('weeklyExpFinalizeButton');
        var week = inputValue('weeklyExpFinalizeWeek');
        if (!week) {
            setFinalizeStatus('Choose the closed week to finalize first.', true);
            return;
        }
        if (button) button.disabled = true;
        try {
            var response = await fetch('/api/admin/weekly-exp/finalize', {
                method: 'POST',
                credentials: 'same-origin',
                cache: 'no-store',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({ week_start: week })
            });
            var data = await response.json();
            if (!response.ok || !data || data.success !== true) {
                throw new Error((data && data.error) || 'Finalization failed (' + response.status + ').');
            }
            setFinalizeStatus(data.already_finalized
                ? 'That week was already finalized; the existing ledger is unchanged.'
                : 'Finalized ' + (data.week_start || week) + ' with ' + (data.recipient_count || 0)
                    + ' recipients and a total liability of ' + (data.total_liability_gd || '') + ' G$.',
                false);
        } catch (error) {
            setFinalizeStatus((error && error.message) || 'Unable to finalize the week.', true);
        } finally {
            if (button) button.disabled = false;
        }
    }
    function setPublishStatus(message, isError) {
        var node = byId('weeklyExpPublishStatus');
        if (!node) return;
        node.textContent = message;
        node.classList.toggle('is-error', !!isError);
    }
    async function publishPeriod() {
        var button = byId('weeklyExpPublishButton');
        var week = inputValue('weeklyExpPublishWeek');
        var tx = inputValue('weeklyExpPublishTx');
        var block = inputValue('weeklyExpPublishBlock');
        if (!week) {
            setPublishStatus('Choose the finalized week to publish first.', true);
            return;
        }
        if (button) button.disabled = true;
        try {
            var response = await fetch('/api/admin/weekly-exp/publish', {
                method: 'POST',
                credentials: 'same-origin',
                cache: 'no-store',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                body: JSON.stringify({
                    week_start: week,
                    root_tx_hash: tx || undefined,
                    root_block_number: block === '' ? undefined : Number(block)
                })
            });
            var data = await response.json();
            if (!response.ok || !data || data.success !== true) {
                throw new Error((data && data.error) || 'Publication failed (' + response.status + ').');
            }
            setPublishStatus(data.already_published
                ? 'That period is already published.'
                : 'Published ' + (data.period_id || '') + ' for ' + (data.recipient_count || 0)
                    + ' recipients.', false);
        } catch (error) {
            setPublishStatus((error && error.message) || 'Unable to publish the period.', true);
        } finally {
            if (button) button.disabled = false;
        }
    }
    function updateDistributionFields() {
        var fixed = inputValue('weeklyExpDistribution') === 'fixed_by_rank';
        var poolField = byId('weeklyExpPoolField');
        var fixedField = byId('weeklyExpFixedField');
        if (poolField) poolField.hidden = fixed;
        if (fixedField) fixedField.hidden = !fixed;
    }
    function init() {
        var button = byId('weeklyExpPreviewButton');
        var distribution = byId('weeklyExpDistribution');
        if (button) button.addEventListener('click', preview);
        if (distribution) distribution.addEventListener('change', updateDistributionFields);
        var rankedPrev = byId('weeklyExpRankedPrev');
        var rankedNext = byId('weeklyExpRankedNext');
        var consolationPrev = byId('weeklyExpConsolationPrev');
        var consolationNext = byId('weeklyExpConsolationNext');
        if (rankedPrev) rankedPrev.addEventListener('click', function () { if (rankedPage > 0) { rankedPage -= 1; drawRankedPage(); } });
        if (rankedNext) rankedNext.addEventListener('click', function () { if (rankedPage < Math.ceil(rankedData.length / PAGE_SIZE) - 1) { rankedPage += 1; drawRankedPage(); } });
        if (consolationPrev) consolationPrev.addEventListener('click', function () { if (consolationPage > 0) { consolationPage -= 1; drawConsolationPage(); } });
        if (consolationNext) consolationNext.addEventListener('click', function () { if (consolationPage < Math.ceil(consolationData.length / PAGE_SIZE) - 1) { consolationPage += 1; drawConsolationPage(); } });
        var saveButton = byId('weeklyExpSaveConfigButton');
        var lockButton = byId('weeklyExpLockConfigButton');
        var loadButton = byId('weeklyExpLoadConfigButton');
        if (saveButton) saveButton.addEventListener('click', saveConfig);
        if (lockButton) lockButton.addEventListener('click', lockConfig);
        if (loadButton) loadButton.addEventListener('click', loadConfig);
        var finalizeButton = byId('weeklyExpFinalizeButton');
        if (finalizeButton) finalizeButton.addEventListener('click', finalizeWeek);
        var publishButton = byId('weeklyExpPublishButton');
        if (publishButton) publishButton.addEventListener('click', publishPeriod);
        updateDistributionFields();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());
