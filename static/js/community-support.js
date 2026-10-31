/* GoodMarket Celo G$ post-support client.
 * The donor signs an exact ERC-20 approval and then a supportPost call; the
 * backend verifies the contract event before it creates a feed announcement.
 */
(function (window, document) {
    'use strict';
    var BOOT = window.GM_COMMUNITY_BOOT || {};
    var CONFIG = BOOT.postSupport || {};
    var busy = false;
    var modal;
    var input;
    var statusNode;
    var submitButton;
    var selectedPost = null;
    var pendingSupportTx = '';
    var pendingSupportPostId = '';
    var MINIMUM = 20;
    var CELO_CHAIN_HEX = '0xa4ec';
    var CELO_RPC = 'https://forno.celo.org';
    var CELO_EXPLORER = 'https://celoscan.io';

    function node(tag, className, text) {
        var el = document.createElement(tag);
        if (className) el.className = className;
        if (text !== undefined && text !== null) el.textContent = String(text);
        return el;
    }
    function ensureModal() {
        if (modal) return modal;
        modal = node('div', 'cf-support-modal');
        modal.hidden = true;
        modal.setAttribute('role', 'presentation');
        var panel = node('section', 'cf-support-panel');
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-modal', 'true');
        panel.setAttribute('aria-labelledby', 'cfSupportTitle');
        var heading = node('h2', null, 'Support this post with G$');
        heading.id = 'cfSupportTitle';
        panel.appendChild(heading);
        var author = node('p', 'cf-support-author');
        author.appendChild(document.createTextNode('Supporting '));
        var authorName = node('strong');
        authorName.id = 'cfSupportAuthor';
        author.appendChild(authorName);
        panel.appendChild(author);
        var route = node('small', 'cf-support-route');
        route.id = 'cfSupportRoute';
        panel.appendChild(route);
        var label = node('label', 'cf-support-label', 'Amount (G$)');
        input = node('input', 'cf-support-input');
        input.id = 'cfSupportAmount';
        input.type = 'text';
        input.inputMode = 'decimal';
        input.autocomplete = 'off';
        input.value = String(MINIMUM);
        input.setAttribute('aria-describedby', 'cfSupportDisclosure cfSupportMinimum');
        label.appendChild(input);
        panel.appendChild(label);
        var presets = node('div', 'cf-support-presets');
        [20, 50, 100].forEach(function (amount) {
            var preset = node('button', 'cf-support-preset', amount + ' G$');
            preset.type = 'button';
            preset.addEventListener('click', function () {
                input.value = String(amount);
                input.focus();
            });
            presets.appendChild(preset);
        });
        panel.appendChild(presets);
        panel.appendChild(node('small', 'cf-support-minimum', 'Minimum: 20 G$.'));
        var disclosure = node('div', 'cf-support-disclosure');
        disclosure.id = 'cfSupportDisclosure';
        disclosure.textContent = 'Your G$ will transfer directly to the post author through GoodMarket’s Community Support smart contract. This requires two wallet confirmations: exact G$ approval, then support. You pay Celo gas. Blockchain transactions are public and transfers are generally irreversible.';
        panel.appendChild(disclosure);
        statusNode = node('p', 'cf-support-status');
        statusNode.setAttribute('role', 'status');
        statusNode.setAttribute('aria-live', 'polite');
        panel.appendChild(statusNode);
        var actions = node('div', 'cf-support-actions');
        submitButton = node('button', 'cf-support-submit', 'Review & Support');
        submitButton.type = 'button';
        submitButton.addEventListener('click', submitSupport);
        var cancel = node('button', 'cf-support-cancel', 'Cancel');
        cancel.type = 'button';
        cancel.addEventListener('click', closeModal);
        actions.appendChild(cancel);
        actions.appendChild(submitButton);
        panel.appendChild(actions);
        modal.appendChild(panel);
        modal.addEventListener('click', function (event) {
            if (event.target === modal && !busy) closeModal();
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && modal && !modal.hidden && !busy) closeModal();
        });
        document.body.appendChild(modal);
        return modal;
    }
    function open(post) {
        if (!CONFIG.enabled || !post || !post.id || post.post_type !== 'user' || post.is_mine) return;
        if (BOOT.isGuest) {
            if (typeof window.openGoodMarketGetStarted === 'function') window.openGoodMarketGetStarted();
            return;
        }
        selectedPost = post;
        ensureModal();
        var author = document.getElementById('cfSupportAuthor');
        if (author) author.textContent = '@' + String(post.username || 'member').replace(/^@/, '');
        var route = document.getElementById('cfSupportRoute');
        if (route) route.textContent = 'Celo Mainnet · G$ · Community Support contract: ' + String(CONFIG.contract_address || '');
        input.value = String(MINIMUM);
        pendingSupportTx = readPendingSupport(post.id);
        pendingSupportPostId = pendingSupportTx ? String(post.id) : '';
        if (pendingSupportTx) {
            submitButton.textContent = 'Verify existing transaction';
            setStatus('A transaction was already sent for this post. Retry verification; no new wallet transaction will be requested.');
        } else {
            submitButton.textContent = 'Review & Support';
            setStatus('Choose an amount to review. Nothing is sent until you confirm in your wallet.');
        }
        modal.hidden = false;
        document.body.classList.add('cf-support-modal-open');
        window.setTimeout(function () { if (input) input.focus(); }, 0);
    }
    function closeModal() {
        if (!modal || busy) return;
        modal.hidden = true;
        document.body.classList.remove('cf-support-modal-open');
        selectedPost = null;
    }
    function setStatus(message, kind) {
        if (!statusNode) return;
        statusNode.textContent = message || '';
        statusNode.classList.toggle('is-error', kind === 'error');
        statusNode.classList.toggle('is-success', kind === 'success');
    }
    function parseAmount(raw) {
        var text = String(raw || '').trim();
        if (!/^(?:\d+)(?:\.\d{1,18})?$/.test(text)) return null;
        var value = Number(text);
        if (!Number.isFinite(value) || value < MINIMUM || value > 1000000) return null;
        return text;
    }
    function isLocal() { return String(BOOT.loginMethod || '').toLowerCase() === 'local'; }
    function isPrivyLogin() { return String(BOOT.loginMethod || '').toLowerCase() === 'privy'; }
    function isWcLogin() {
        return ['walletconnect', 'manual', 'manual_address'].indexOf(String(BOOT.loginMethod || '').toLowerCase()) >= 0;
    }
    // A stored WalletConnect session outranks an injected extension (the user
    // picked WC and it is still live). Same rule as p2p-wallet.js.
    function prefersWc() {
        return !!(window.GMWalletConnect
            && typeof window.GMWalletConnect.prefersWcSigning === 'function'
            && window.GMWalletConnect.prefersWcSigning());
    }
    // The WC bridge exists to recover legacy logins that predate login_method
    // persistence (they report "injected"). A KNOWN non-WC method must never be
    // routed there by omission.
    function wcFallbackAllowed() {
        if (isLocal() || isPrivyLogin()) return false;
        if (isWcLogin()) return true;
        return prefersWc();
    }
    // Providers are exposed in several shapes: a bare EIP-1193 object, a wrapper
    // with `.ethereum`, or an EIP-6963 `{ provider }` entry. Collect them all —
    // a Trust build that only exposes `window.trustwallet.ethereum` used to be
    // invisible here.
    function coerceToRequestProvider(candidate) {
        if (!candidate) return null;
        if (typeof candidate.request === 'function') return candidate;
        if (candidate.ethereum && typeof candidate.ethereum.request === 'function') return candidate.ethereum;
        if (candidate.provider && typeof candidate.provider.request === 'function') return candidate.provider;
        return null;
    }
    function announced6963() {
        var out = [];
        var list = Array.isArray(window.__announced6963Providers) ? window.__announced6963Providers : [];
        for (var i = 0; i < list.length; i++) {
            if (list[i] && list[i].provider) out.push(list[i]);
        }
        return out;
    }
    function looksLikeTrustInfo(info) {
        if (!info) return false;
        var rdns = String(info.rdns || '').toLowerCase();
        var name = String(info.name || '').toLowerCase();
        return rdns.indexOf('trustwallet') >= 0 || name.indexOf('trust') >= 0;
    }
    function isTrustWalletAliasProvider(provider) {
        if (!provider) return false;
        return [window.trustwallet, window.trustWallet].some(function (root) {
            return root && coerceToRequestProvider(root) === provider;
        });
    }
    function isTrustWalletProvider(provider) {
        if (!provider) return false;
        if (provider.isTrust || provider.isTrustWallet) return true;
        // Some Trust builds inject ONLY the branded alias with no `isTrust`
        // flag and no EIP-6963 announcement. Recognise the alias itself so a
        // co-installed extension's provider cannot shadow the authentic one.
        if (isTrustWalletAliasProvider(provider)) return true;
        var announced = announced6963();
        for (var i = 0; i < announced.length; i++) {
            if (announced[i].provider === provider) return looksLikeTrustInfo(announced[i].info);
        }
        return false;
    }
    function injectedCandidates() {
        var all = [];
        function add(candidate) {
            var provider = coerceToRequestProvider(candidate);
            if (provider && all.indexOf(provider) < 0) all.push(provider);
        }
        if (window.ethereum && Array.isArray(window.ethereum.providers)) window.ethereum.providers.forEach(add);
        add(window.ethereum);
        add(window.trustwallet);
        add(window.trustwallet && window.trustwallet.ethereum);
        add(window.trustWallet);
        add(window.trustWallet && window.trustWallet.ethereum);
        announced6963().forEach(function (entry) { add(entry.provider); });
        return all;
    }
    async function accountsOf(provider) {
        var accounts = [];
        try { accounts = await provider.request({ method: 'eth_requestAccounts' }) || []; } catch (_) {}
        if (!accounts.length) {
            try { accounts = await provider.request({ method: 'eth_accounts' }) || []; } catch (_) {}
        }
        return accounts;
    }
    // The provider whose ACTIVE account is the signed-in wallet. When several
    // hold the same address, MiniPay first (it needs CIP-64 fee handling), then
    // Trust (inside its browser the branded provider is the authentic one),
    // then the first match.
    async function matchingInjectedProvider(candidates) {
        var wanted = String(BOOT.wallet || '').toLowerCase();
        var matches = [];
        for (var i = 0; i < candidates.length; i++) {
            var accounts = await accountsOf(candidates[i]);
            if (!wanted || accounts.some(function (a) { return String(a).toLowerCase() === wanted; })) {
                matches.push(candidates[i]);
            }
        }
        if (!matches.length) return null;
        var mini = matches.find(function (p) { return p && p.isMiniPay; });
        if (mini) return mini;
        var trust = matches.find(function (p) { return isTrustWalletProvider(p); });
        return trust || matches[0];
    }
    async function resolveProvider() {
        if (isLocal()) {
            if (!window.GMLocalWallet) throw new Error('Your in-app wallet is still loading. Please try again.');
            if (window.GMEthersLoader && typeof window.GMEthersLoader.load === 'function') {
                await window.GMEthersLoader.load();
            }
            if (!window.GMLocalWallet.isUnlocked() && typeof window._lwOpenUnlockModal === 'function') {
                await window._lwOpenUnlockModal({
                    title: 'Support this post', subtitle: 'Enter your PIN to continue',
                    submitLabel: 'Continue', busyLabel: 'Unlocking…'
                });
            }
            if (!window.GMLocalWallet.isUnlocked()) throw new Error('Unlock your GoodMarket wallet to continue.');
            return window.GMLocalWallet.getProvider();
        }
        if (isWcLogin() && window.GMWalletConnect && typeof window.GMWalletConnect.getProvider === 'function') {
            return await window.GMWalletConnect.getProvider();
        }
        var privy = Array.isArray(window.GMPrivyWallets) ? window.GMPrivyWallets : [];
        for (var p = 0; p < privy.length; p++) {
            if (privy[p] && privy[p].provider && typeof privy[p].provider.request === 'function') return privy[p].provider;
        }
        var candidates = injectedCandidates();
        // A live WC session the user chose must not be shadowed by an extension.
        if (candidates.length && !prefersWc()) {
            var matched = await matchingInjectedProvider(candidates);
            if (matched) return matched;
        }
        if (candidates.length && !wcFallbackAllowed()) {
            throw new Error('Wrong wallet connected. Switch to your GoodMarket wallet and try again.');
        }
        if (isPrivyLogin()) throw new Error('Privy wallet is not ready. Please sign in again.');
        if (window.GMWalletConnect && typeof window.GMWalletConnect.getProvider === 'function') {
            try {
                return await window.GMWalletConnect.getProvider();
            } catch (error) {
                // The bridge's own "No active WalletConnect session." is an
                // internal string; translate it into something actionable.
                if (candidates.length) throw new Error('Wrong wallet connected. Switch to your GoodMarket wallet and try again.');
                throw new Error('No Celo wallet was detected. Open GoodMarket in your wallet and try again.');
            }
        }
        throw new Error(candidates.length ? 'Wrong wallet connected. Switch to your GoodMarket wallet and try again.' : 'No Celo wallet was detected. Open GoodMarket in your wallet and try again.');
    }
    async function ensureCelo(provider) {
        var chain = '';
        try { chain = String(await provider.request({ method: 'eth_chainId' })).toLowerCase(); } catch (_) {}
        if (chain === CELO_CHAIN_HEX) return;
        try {
            await provider.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: CELO_CHAIN_HEX }] });
        } catch (error) {
            if (Number(error && error.code) === 4001) throw new Error('You cancelled the network switch. No transaction was sent.');
            await provider.request({ method: 'wallet_addEthereumChain', params: [{
                chainId: CELO_CHAIN_HEX, chainName: 'Celo Mainnet',
                nativeCurrency: { name: 'CELO', symbol: 'CELO', decimals: 18 },
                rpcUrls: [CELO_RPC], blockExplorerUrls: [CELO_EXPLORER]
            }] });
        }
    }
    async function accountFor(provider) {
        var accounts = [];
        try { accounts = await provider.request({ method: 'eth_requestAccounts' }) || []; } catch (_) {}
        if (!accounts.length) accounts = await provider.request({ method: 'eth_accounts' }) || [];
        var wanted = String(BOOT.wallet || '').toLowerCase();
        for (var i = 0; i < accounts.length; i++) {
            if (String(accounts[i]).toLowerCase() === wanted) return accounts[i];
        }
        if (!wanted && accounts.length) return accounts[0];
        throw new Error('Wrong wallet connected. Use your signed-in GoodMarket wallet.');
    }
    async function sendTransaction(provider, from, to, data, fallbackGas) {
        var tx = { from: from, to: to, data: data, value: '0x0' };
        var gas = BigInt(fallbackGas);
        try {
            var estimated = BigInt(await provider.request({ method: 'eth_estimateGas', params: [tx] }));
            if (estimated > 0n) gas = estimated * 140n / 100n;
        } catch (_) {}
        var gasPrice = 500000000000n;
        try {
            var seen = BigInt(await provider.request({ method: 'eth_gasPrice', params: [] }));
            if (seen > 0n) gasPrice = seen;
        } catch (_) {}
        tx.gas = '0x' + gas.toString(16);
        tx.gasPrice = '0x' + gasPrice.toString(16);
        if (isLocal()) tx.chainId = 42220;
        return await provider.request({ method: 'eth_sendTransaction', params: [tx] });
    }
    function sleep(ms) { return new Promise(function (resolve) { window.setTimeout(resolve, ms); }); }
    function pendingKey(postId) {
        return 'gmCommunityPostSupport_v1:' + String(BOOT.wallet || '').toLowerCase() + ':' + String(postId);
    }
    function readPendingSupport(postId) {
        try {
            var value = JSON.parse(window.localStorage.getItem(pendingKey(postId)) || 'null');
            return value && /^0x[0-9a-f]{64}$/i.test(String(value.tx_hash || ''))
                ? String(value.tx_hash).toLowerCase() : '';
        } catch (_) { return ''; }
    }
    function savePendingSupport(postId, txHash) {
        try { window.localStorage.setItem(pendingKey(postId), JSON.stringify({ tx_hash: txHash })); } catch (_) {}
    }
    function clearPendingSupport(postId) {
        try { window.localStorage.removeItem(pendingKey(postId)); } catch (_) {}
    }
    async function waitReceipt(provider, txHash, maxMs) {
        var deadline = Date.now() + maxMs;
        while (Date.now() < deadline) {
            try {
                var receipt = await provider.request({ method: 'eth_getTransactionReceipt', params: [txHash] });
                if (receipt) {
                    if (parseInt(receipt.status, 16) !== 1) throw new Error('A wallet transaction failed on Celo. No support was recorded.');
                    return receipt;
                }
            } catch (error) {
                if (/failed on Celo/.test(String(error && error.message || ''))) throw error;
            }
            await sleep(2200);
        }
        return null;
    }
    async function requestJson(url, body) {
        var response = await fetch(url, {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        var data = await response.json().catch(function () { return {}; });
        return { ok: response.ok, status: response.status, data: data };
    }
    async function confirmUntilSettled(postId, txHash) {
        var deadline = Date.now() + 90000;
        var last = null;
        while (Date.now() < deadline) {
            var result = await requestJson('/community/api/posts/' + encodeURIComponent(postId) + '/support/confirm', { tx_hash: txHash });
            last = result.data;
            if (result.ok && last && last.success) return last;
            if (result.status !== 202 && !(last && (last.status === 'confirming' || last.status === 'submitted'))) {
                throw new Error((last && last.error) || 'The support transaction could not be verified. Keep the hash and retry verification; do not send again.');
            }
            setStatus((last && last.error) || 'Waiting for Celo confirmation. Do not send another support transaction.');
            await sleep(3500);
        }
        var error = new Error((last && last.error) || 'The transaction is still being confirmed. Keep the transaction hash and check again; do not send another support transaction.');
        error.txHash = txHash;
        throw error;
    }
    function showConfirmed(confirmed, txHash, requestedAmount) {
        if (selectedPost) clearPendingSupport(selectedPost.id);
        pendingSupportTx = '';
        pendingSupportPostId = '';
        var link = document.createElement('a');
        link.href = confirmed.tx_url || ('https://celoscan.io/tx/' + txHash);
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = 'View Celo transaction';
        statusNode.textContent = 'Support confirmed: ' + (confirmed.amount || requestedAmount) + ' G$ sent. The thank-you post is in the feed and is not pinned. ';
        statusNode.appendChild(link);
        statusNode.classList.add('is-success');
        if (window.GMCommunity && typeof window.GMCommunity.refresh === 'function') {
            window.setTimeout(function () { window.GMCommunity.refresh(); }, 1200);
        }
        submitButton.textContent = 'Support confirmed';
    }
    async function submitSupport() {
        if (busy || !selectedPost) return;
        if (pendingSupportTx && pendingSupportPostId === String(selectedPost.id)) {
            busy = true;
            submitButton.disabled = true;
            submitButton.textContent = 'Checking…';
            try {
                setStatus('Checking the transaction already sent. No new wallet transaction will be requested.');
                var prior = await confirmUntilSettled(selectedPost.id, pendingSupportTx);
                showConfirmed(prior, pendingSupportTx, '');
            } catch (pendingError) {
                setStatus(String(pendingError && pendingError.message || 'The transaction is still pending. Do not send again.'), 'error');
            } finally {
                busy = false;
                if (submitButton && submitButton.textContent !== 'Support confirmed') {
                    submitButton.disabled = false;
                    submitButton.textContent = 'Verify existing transaction';
                }
            }
            return;
        }
        var amount = parseAmount(input && input.value);
        if (!amount) {
            setStatus('Enter a valid amount of at least 20 G$ (up to 18 decimal places).', 'error');
            if (input) input.focus();
            return;
        }
        busy = true;
        submitButton.disabled = true;
        submitButton.textContent = 'Preparing…';
        var postId = selectedPost.id;
        var txHash = '';
        try {
            setStatus('Preparing the exact G$ approval and support call…');
            var prepResult = await requestJson('/community/api/posts/' + encodeURIComponent(postId) + '/support/prepare', { amount: amount });
            var prep = prepResult.data;
            if (!prepResult.ok || !prep || !prep.success) throw new Error((prep && prep.error) || 'Could not prepare post support.');
            setStatus('Connect the signed-in wallet and confirm the exact G$ approval. No G$ moves in this step.');
            var provider = await resolveProvider();
            await ensureCelo(provider);
            var from = await accountFor(provider);
            if (isLocal()) {
                from = String(BOOT.wallet || from);
            }
            var approvalHash = await sendTransaction(provider, from, prep.token_address, prep.approval_data, 100000n);
            if (!approvalHash || !/^0x[0-9a-f]{64}$/i.test(String(approvalHash))) throw new Error('The wallet did not return an approval transaction hash. Check wallet activity before retrying.');
            submitButton.textContent = 'Waiting…';
            setStatus('G$ approval submitted. Waiting for Celo confirmation; the support transfer has not been sent yet.');
            var approvalReceipt = await waitReceipt(provider, approvalHash, 90000);
            if (!approvalReceipt) throw new Error('G$ approval is still pending. No support transfer was sent. Check your wallet activity before trying again.');
            setStatus('Approval confirmed. Confirm the second wallet transaction to send support to the post author.');
            var supportHash = await sendTransaction(provider, from, prep.contract_address, prep.support_data, 250000n);
            if (!supportHash || !/^0x[0-9a-f]{64}$/i.test(String(supportHash))) throw new Error('The wallet did not return a support transaction hash. Check wallet activity before retrying.');
            txHash = String(supportHash).toLowerCase();
            pendingSupportTx = txHash;
            pendingSupportPostId = String(postId);
            savePendingSupport(postId, txHash);
            setStatus('Support transaction submitted. Verifying its contract event on Celo…');
            var confirmed = await confirmUntilSettled(postId, txHash);
            showConfirmed(confirmed, txHash, amount);
        } catch (error) {
            var message = String(error && error.message || 'Could not complete support.');
            if (txHash) {
                pendingSupportTx = txHash;
                pendingSupportPostId = String(postId);
                savePendingSupport(postId, txHash);
            }
            setStatus(message, 'error');
            if (error && error.txHash) {
                var txLink = document.createElement('a');
                txLink.href = 'https://celoscan.io/tx/' + error.txHash;
                txLink.target = '_blank';
                txLink.rel = 'noopener noreferrer';
                txLink.textContent = 'View pending transaction';
                statusNode.appendChild(document.createTextNode(' '));
                statusNode.appendChild(txLink);
            }
        } finally {
            busy = false;
            if (submitButton && submitButton.textContent !== 'Support confirmed') {
                submitButton.disabled = false;
                submitButton.textContent = pendingSupportTx ? 'Verify existing transaction' : 'Review & Support';
            }
        }
    }
    window.GMCommunitySupport = { open: open, close: closeModal };
}(window, document));
