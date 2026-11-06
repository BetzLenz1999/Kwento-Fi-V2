/* Permanent Community video listing + paid-download wallet transactions.
 * The server prepares authoritative calldata and verifies receipts. The source
 * video URLs are public, so payments do not prevent direct URL access.
 */
(function (window, document) {
    'use strict';
    var BOOT = window.GM_COMMUNITY_BOOT || {};
    var CONFIG = BOOT.videoDownloads || {};
    var BUSY = Object.create(null);
    var CELO_CHAIN_HEX = '0xa4ec';
    var CELO_RPC = 'https://forno.celo.org';

    function jsonRequest(url, body) {
        return fetch(url, {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {})
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                if (!response.ok || !data.success) throw new Error(data.error || 'The request failed.');
                return data;
            });
        });
    }
    async function providerForWallet() {
        var method = String(BOOT.loginMethod || '').toLowerCase();
        if (method === 'local') {
            if (!window.GMLocalWallet) throw new Error('Your KwentoFi wallet is still loading.');
            if (!window.GMLocalWallet.isUnlocked() && typeof window._lwOpenUnlockModal === 'function') {
                await window._lwOpenUnlockModal({
                    title: 'Confirm Community transaction', subtitle: 'Enter your wallet PIN to continue',
                    submitLabel: 'Continue', busyLabel: 'Unlocking…'
                });
            }
            if (!window.GMLocalWallet.isUnlocked()) throw new Error('Unlock your wallet to continue.');
            return window.GMLocalWallet.getProvider();
        }
        if (['walletconnect', 'manual', 'manual_address'].indexOf(method) >= 0
                && window.GMWalletConnect && typeof window.GMWalletConnect.getProvider === 'function') {
            return await window.GMWalletConnect.getProvider();
        }
        // MiniPay is an injected wallet. Its eth_accounts may be empty until
        // eth_requestAccounts is called from a user action; use the shared
        // account-bound resolver rather than silently falling through to WC.
        var miniPayContext = /minipay|opera\s*mini/i.test(String(window.navigator && window.navigator.userAgent || ''))
            || !!(window.ethereum && (window.ethereum.isMiniPay || window.ethereum.isMiniPayWallet
                || (Array.isArray(window.ethereum.providers) && window.ethereum.providers.some(function (p) {
                    return p && (p.isMiniPay || p.isMiniPayWallet || p.isOperaMiniPay);
                }))));
        if ((!method || method === 'injected' || (method === 'minipay' && miniPayContext))
                && window.GMCommunityWalletDetection
                && typeof window.GMCommunityWalletDetection.resolveInjectedProvider === 'function') {
            var detected = await window.GMCommunityWalletDetection.resolveInjectedProvider({
                loginMethod: method === 'minipay' ? 'injected' : (method || 'injected'), wallet: BOOT.wallet
            });
            if (detected) return detected;
        }
        var privy = Array.isArray(window.GMPrivyWallets) ? window.GMPrivyWallets : [];
        for (var i = 0; i < privy.length; i++) {
            if (privy[i] && privy[i].provider && typeof privy[i].provider.request === 'function') {
                return privy[i].provider;
            }
        }
        var candidates = [];
        if (window.GMCommunityWalletDetection && typeof window.GMCommunityWalletDetection.collect === 'function') {
            candidates = window.GMCommunityWalletDetection.collect();
        } else if (window.ethereum) candidates = [window.ethereum];
        var wanted = String(BOOT.wallet || '').toLowerCase();
        for (var j = 0; j < candidates.length; j++) {
            var candidate = candidates[j];
            var p = candidate && candidate.provider ? candidate.provider : candidate;
            if (!p || typeof p.request !== 'function') continue;
            var accounts = await p.request({ method: 'eth_accounts' }).catch(function () { return []; });
            if (!wanted || accounts.some(function (a) { return String(a).toLowerCase() === wanted; })) return p;
        }
        if (window.GMWalletConnect && typeof window.GMWalletConnect.getProvider === 'function') {
            return await window.GMWalletConnect.getProvider();
        }
        throw new Error('Connect the same wallet you used to sign in.');
    }
    async function accountFor(provider) {
        var accounts = await withTimeout(provider.request({ method: 'eth_requestAccounts' }),
            15000, 'Wallet account request timed out. Reopen the page inside your wallet and retry.');
        var wallet = String(BOOT.wallet || '').toLowerCase();
        var account = accounts.find(function (a) { return String(a).toLowerCase() === wallet; });
        if (!account) throw new Error('The connected wallet does not match your KwentoFi wallet.');
        return account;
    }
    async function ensureCelo(provider) {
        var chain = String(await withTimeout(provider.request({ method: 'eth_chainId' }),
            8000, 'Could not read the wallet network. Reopen the page inside your wallet and retry.')).toLowerCase();
        if (/^\d+$/.test(chain)) chain = '0x' + Number(chain).toString(16);
        if (chain === CELO_CHAIN_HEX) return;
        try {
            await provider.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: CELO_CHAIN_HEX }] });
        } catch (error) {
            if (Number(error && error.code) === 4001) throw new Error('Network switch cancelled. No transaction was sent.');
            await provider.request({ method: 'wallet_addEthereumChain', params: [{
                chainId: CELO_CHAIN_HEX, chainName: 'Celo Mainnet',
                nativeCurrency: { name: 'CELO', symbol: 'CELO', decimals: 18 },
                rpcUrls: [CELO_RPC], blockExplorerUrls: ['https://celoscan.io']
            }] });
        }
    }
    function isMiniPay(provider) {
        if (provider && (provider.isMiniPay || provider.isMiniPayWallet || provider.isOperaMiniPay)) return true;
        var ua = String(window.navigator && window.navigator.userAgent || '');
        return /minipay|opera\s*mini/i.test(ua)
            || !!(window.MPGasTopUp && typeof window.MPGasTopUp.isMiniPay === 'function'
                && window.MPGasTopUp.isMiniPay());
    }
    function withTimeout(promise, ms, message) {
        return Promise.race([promise, new Promise(function (_, reject) {
            window.setTimeout(function () { reject(new Error(message)); }, ms);
        })]);
    }
    async function sendTx(provider, from, to, data, fallbackGas, statusNode, actionLabel) {
        var tx = { from: from, to: to, data: data, value: '0x0' };
        var gas = BigInt(fallbackGas);
        var miniPay = isMiniPay(provider);
        if (miniPay) {
            // MiniPay's CIP-64 transactions use feeCurrency rather than the
            // standard gasPrice path. A bounded estimate is optional; never
            // let a slow MiniPay RPC stall before opening its signing prompt.
            try {
                var miniEstimate = BigInt(await withTimeout(provider.request({
                    method: 'eth_estimateGas', params: [tx]
                }), 3500, 'MiniPay gas estimate timed out'));
                if (miniEstimate > 0n) gas = miniEstimate * 140n / 100n;
            } catch (_) {}
            tx.gas = '0x' + gas.toString(16);
            var currencies;
            try {
                var feeHelper = window.GMMinipayFeeCurrencies;
                if (feeHelper && typeof feeHelper.orderByBalances === 'function') {
                    currencies = await withTimeout(feeHelper.orderByBalances(provider, from, { includePlain: false }),
                        4500, 'MiniPay fee-currency lookup timed out');
                }
            } catch (_) {}
            if (!Array.isArray(currencies) || !currencies.length) {
                currencies = ['0x765DE816845861e75A25fCA122bb6898B8B1282a',
                    '0x0E2A3e05bc9A16F5292A6170456A710cb89C6f72',
                    '0x2F25deB3848C207fc8E0c34035B3Ba7fC157602B'];
            }
            var lastMiniError;
            for (var c = 0; c < currencies.length; c++) {
                tx.feeCurrency = currencies[c];
                if (statusNode) statusNode.textContent = 'Approve ' + (actionLabel || 'the transaction')
                    + ' in your MiniPay wallet. Select a supported gas currency if prompted…';
                try {
                    return await provider.request({ method: 'eth_sendTransaction', params: [tx] });
                } catch (miniError) {
                    lastMiniError = miniError;
                    var miniMessage = String(miniError && (miniError.message || miniError.data && miniError.data.message) || '');
                    if (Number(miniError && miniError.code) === 4001 || /reject|denied|cancel/i.test(miniMessage)) {
                        throw new Error('Transaction cancelled in MiniPay. No transaction was sent.');
                    }
                    if (/revert/i.test(miniMessage)) throw miniError;
                }
            }
            if (lastMiniError && Number(lastMiniError.code) === -32601) {
                delete tx.feeCurrency;
                if (statusNode) statusNode.textContent = 'Approve the transaction in your MiniPay wallet…';
                return provider.request({ method: 'eth_sendTransaction', params: [tx] });
            }
            throw lastMiniError || new Error('MiniPay could not prepare transaction gas. Check that you have a supported gas token and retry.');
        }
        try {
            var estimate = BigInt(await withTimeout(provider.request({ method: 'eth_estimateGas', params: [tx] }),
                5000, 'Gas estimate timed out'));
            if (estimate > 0n) gas = estimate * 140n / 100n;
        } catch (_) {}
        tx.gas = '0x' + gas.toString(16);
        var gasPrice = 500000000000n;
        try {
            var seenGasPrice = BigInt(await withTimeout(provider.request({ method: 'eth_gasPrice', params: [] }),
                3500, 'Gas price lookup timed out'));
            if (seenGasPrice > 0n) gasPrice = seenGasPrice;
        } catch (_) {}
        tx.gasPrice = '0x' + gasPrice.toString(16);
        if (BOOT.loginMethod === 'local') tx.chainId = 42220;
        return provider.request({ method: 'eth_sendTransaction', params: [tx] });
    }
    function sleep(ms) { return new Promise(function (resolve) { window.setTimeout(resolve, ms); }); }
    async function waitReceipt(provider, hash, timeoutMs) {
        var until = Date.now() + (timeoutMs || 120000);
        while (Date.now() < until) {
            var receipt = await provider.request({ method: 'eth_getTransactionReceipt', params: [hash] }).catch(function () { return null; });
            if (receipt) {
                if (parseInt(receipt.status, 16) !== 1) throw new Error('The wallet transaction failed on Celo.');
                return receipt;
            }
            await sleep(2200);
        }
        var error = new Error('Transaction is still pending. Keep its hash and verify it before retrying.');
        error.txHash = hash;
        throw error;
    }
    function randomRef() {
        var bytes = new Uint8Array(32);
        window.crypto.getRandomValues(bytes);
        return '0x' + Array.prototype.map.call(bytes, function (x) { return x.toString(16).padStart(2, '0'); }).join('');
    }
    function pendingKey(postId) {
        return 'gmCommunityVideoDownload_v1:' + String(BOOT.wallet || '').toLowerCase() + ':' + String(postId);
    }
    function readPending(postId) {
        try {
            var value = JSON.parse(window.localStorage.getItem(pendingKey(postId)) || 'null');
            if (value && /^0x[0-9a-f]{64}$/i.test(String(value.purchase_ref || ''))
                    && /^0x[0-9a-f]{64}$/i.test(String(value.tx_hash || ''))) return value;
        } catch (_) {}
        return null;
    }
    function savePending(postId, value) {
        try { window.localStorage.setItem(pendingKey(postId), JSON.stringify(value)); } catch (_) {}
    }
    function clearPending(postId) {
        try { window.localStorage.removeItem(pendingKey(postId)); } catch (_) {}
    }
    function approveData(spender, amount) {
        return '0x095ea7b3' + String(spender).replace(/^0x/, '').toLowerCase().padStart(64, '0')
            + BigInt(amount).toString(16).padStart(64, '0');
    }
    async function waitServerConfirmation(url, body, statusNode) {
        var expires = Date.now() + 90000;
        var last;
        while (Date.now() < expires) {
            try {
                last = await jsonRequest(url, body);
                return last;
            } catch (error) {
                if (!/confirm|pending|still/i.test(String(error.message || ''))) throw error;
                if (statusNode) statusNode.textContent = 'Waiting for Celo transaction verification…';
            }
            await sleep(3000);
        }
        throw new Error((last && last.error) || 'The transaction is still confirming. Keep its hash and retry verification; do not submit a second payment.');
    }

    async function registerListing(post, statusNode) {
        if (!CONFIG.enabled) throw new Error('Paid download contract is not configured yet. Your post is published, but its listing is not active.');
        var key = 'listing:' + post.id;
        if (BUSY[key]) return;
        BUSY[key] = true;
        try {
            if (statusNode) statusNode.textContent = 'Preparing the on-chain listing (gas only; no G$ posting fee)…';
            var prep = await jsonRequest('/community/api/posts/' + encodeURIComponent(post.id) + '/download/listing/prepare', {});
            if (prep.already_registered) {
                post.download_listing_status = 'active';
                return post;
            }
            if (statusNode) statusNode.textContent = 'Connecting to your author wallet… approve the connection request if MiniPay asks.';
            var provider = await providerForWallet();
            await ensureCelo(provider);
            var from = await accountFor(provider);
            if (statusNode) statusNode.textContent = 'Check your wallet and approve the listing transaction. You will pay network gas only.';
            var txHash = await sendTx(provider, from, prep.contract_address, prep.data, 180000n,
                statusNode, 'the paid-download listing');
            if (statusNode) statusNode.textContent = 'Confirming the permanent listing on Celo…';
            await waitReceipt(provider, txHash);
            var confirmed = await waitServerConfirmation(
                '/community/api/posts/' + encodeURIComponent(post.id) + '/download/listing/confirm',
                { tx_hash: txHash }, statusNode);
            post.download_listing_status = confirmed.status || 'active';
            return post;
        } finally { BUSY[key] = false; }
    }

    async function purchase(post, statusNode) {
        if (!CONFIG.enabled) throw new Error('Paid video downloads are not configured yet.');
        if (BOOT.isGuest || !BOOT.wallet) {
            if (typeof window.openGoodMarketGetStarted === 'function') window.openGoodMarketGetStarted();
            throw new Error('Connect a KwentoFi wallet to continue.');
        }
        var amount = Number(post.download_price_gd);
        if (!Number.isInteger(amount) || amount < 1000 || amount > 10000) throw new Error('This post has an invalid download price.');
        var author = String(post.username || 'the author').replace(/^@/, '');
        var key = 'purchase:' + post.id;
        if (BUSY[key]) return;
        BUSY[key] = true;
        try {
            var pending = readPending(post.id);
            if (pending) {
                if (statusNode) statusNode.textContent = 'Checking the payment already sent. No second payment will be requested.';
                var pendingProvider = await providerForWallet();
                await ensureCelo(pendingProvider);
                await accountFor(pendingProvider);
                try {
                    await waitReceipt(pendingProvider, pending.tx_hash);
                } catch (pendingError) {
                    if (/failed on Celo/i.test(String(pendingError && pendingError.message || ''))) clearPending(post.id);
                    else throw pendingError;
                }
                var recovered = await waitServerConfirmation(
                    '/community/api/posts/' + encodeURIComponent(post.id) + '/download/confirm',
                    { purchase_ref: pending.purchase_ref, tx_hash: pending.tx_hash }, statusNode);
                clearPending(post.id);
                if (recovered.download_url) {
                    openDownload(recovered, post, amount, statusNode);
                    return recovered;
                }
                throw new Error('Payment verified, but the public video link was not returned. Transaction: ' + pending.tx_hash);
            }
        var disclosure = 'You are about to pay ' + amount.toLocaleString('en-US') + ' G$ directly to @' + author + ' to download this video. You will first approve the exact G$ amount, then confirm the purchase. Network gas is separate.\n\nImportant: this video is hosted at a public URL. Paying unlocks this button flow but cannot prevent someone from accessing the public URL directly. Blockchain payments are generally irreversible.\n\nAgree and continue?';
        if (!window.confirm(disclosure)) return;
            var ref = randomRef();
            if (statusNode) statusNode.textContent = 'Preparing payment…';
            var prep = await jsonRequest('/community/api/posts/' + encodeURIComponent(post.id) + '/download/prepare', { purchase_ref: ref });
            var provider = await providerForWallet();
            await ensureCelo(provider);
            var from = await accountFor(provider);
            if (statusNode) statusNode.textContent = 'Approve exactly ' + amount.toLocaleString('en-US') + ' G$ in your wallet. No payment is taken in this step.';
            var approvalHash = await sendTx(provider, from, prep.token_address,
                approveData(prep.contract_address, prep.price_base_units), 100000n, statusNode, 'the exact G$ approval');
            await waitReceipt(provider, approvalHash);
            if (statusNode) statusNode.textContent = 'Confirm ' + amount.toLocaleString('en-US') + ' G$ payment to @' + author + ' in your wallet…';
            var paymentHash = await sendTx(provider, from, prep.contract_address, prep.data, 240000n,
                statusNode, 'the G$ payment');
            savePending(post.id, { purchase_ref: ref, tx_hash: paymentHash });
            try {
                await waitReceipt(provider, paymentHash);
            } catch (paymentError) {
                if (/failed on Celo/i.test(String(paymentError && paymentError.message || ''))) clearPending(post.id);
                throw paymentError;
            }
            if (statusNode) statusNode.textContent = 'Verifying your payment on Celo…';
            var confirmed = await waitServerConfirmation(
                '/community/api/posts/' + encodeURIComponent(post.id) + '/download/confirm',
                { purchase_ref: ref, tx_hash: paymentHash }, statusNode);
            clearPending(post.id);
            if (!confirmed.download_url) throw new Error('Payment verified, but the public video link was not returned. Save transaction ' + paymentHash + ' and contact support.');
            openDownload(confirmed, post, amount, statusNode);
            return confirmed;
        } finally { BUSY[key] = false; }
    }

    function openDownload(confirmed, post, amount, statusNode) {
        var a = document.createElement('a');
        a.href = confirmed.download_url;
        a.download = 'kwentofi-community-video-' + post.id + '.mp4';
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        document.body.appendChild(a);
        a.click();
        a.remove();
        if (statusNode) {
            statusNode.textContent = 'Payment confirmed (' + amount.toLocaleString('en-US') + ' G$). The public video link opened; the browser may play it instead of saving it, depending on the media host.';
        }
    }

    window.GMCommunityVideoDownloads = {
        enabled: !!CONFIG.enabled,
        registerListing: registerListing,
        purchase: purchase
    };
})(window, document);
