/**
 * Node harness over the REAL chatroom widget on-chain posting functions.
 *
 * Content tests can prove a string exists; they cannot prove the post is
 * signed by the RIGHT wallet, that the fee fields are actually sent, or that
 * the three-hop flow calls the endpoints in the right order. This executes the
 * real function bodies extracted from static/js/chatroom-widget.js.
 *
 * Exits non-zero on the first failed assertion so pytest reports it.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const SRC = fs.readFileSync(path.join(ROOT, 'static', 'js', 'chatroom-widget.js'), 'utf8');

function slice(startMarker, endMarker) {
  const start = SRC.indexOf(startMarker);
  if (start < 0) throw new Error('could not find ' + startMarker);
  const end = SRC.indexOf(endMarker, start);
  if (end < 0) throw new Error('could not find ' + endMarker);
  return SRC.slice(start, end);
}

const WALLET = '0x' + 'a'.repeat(40);
const OTHER = '0x' + 'b'.repeat(40);

function build() {
  const ctx = {
    _tipHex: (v) => '0x' + BigInt(v).toString(16),
    _isLocalLogin: () => ctx.__local,
    _withSignTimeout: (p) => p,
    TIP_BOOT: { wallet: WALLET, loginMethod: '' },
    TIP_SIGNER: 'MetaMask',
    CHAIN_META: {
      celo: { fallbackGasPrice: 500000000000n },
      xdc: { fallbackGasPrice: 25000000000n }
    },
    POST_GAS_FALLBACK: 150000n,
    POST_GAS_CEILING: 300000n,
    FEE_CURRENCY_NATIVE: 'CELO',
    TIP_SIGN_TIMEOUT_MS: 5000,
    _tipEnsureChain: async () => { ctx.__chainSwitches.push(1); },
    _tipProvider: async () => ctx.__provider,
    _explorerUrl: () => 'https://celoscan.io/tx/0xdead',
    __local: false,
    __provider: null,
    __chainSwitches: []
  };
  const body = [
    slice('function _tipErrCode', 'function configureTip'),
    slice('async function postMessage', 'function _tipModal')
  ].join('\n');
  const factory = new Function('ctx', 'with (ctx) { ' + body +
    '; return { _postGasParams, _postSendTransaction, postMessage, _feeCurrencyCandidates }; }');
  const api = factory(ctx);
  return { ctx, api };
}

let failures = 0;
function check(label, condition, detail) {
  if (condition) {
    console.log('  ok   ' + label);
  } else {
    failures += 1;
    console.log('  FAIL ' + label + (detail ? ' :: ' + detail : ''));
  }
}

function makeProvider(opts) {
  opts = opts || {};
  const sent = [];
  return {
    sent,
    async request(payload) {
      if (payload.method === 'eth_requestAccounts') return opts.accounts || [WALLET];
      if (payload.method === 'eth_accounts') return opts.accounts || [WALLET];
      if (payload.method === 'eth_estimateGas') {
        if (opts.estimateFails) throw new Error('estimate failed');
        return '0x' + (70000).toString(16);
      }
      if (payload.method === 'eth_gasPrice') {
        if (opts.gasPriceFails) throw new Error('no price');
        return '0x' + (100000000000).toString(16);
      }
      if (payload.method === 'eth_sendTransaction') {
        sent.push(payload.params[0]);
        return '0x' + 'c'.repeat(64);
      }
      return null;
    }
  };
}

async function main() {
  // ── 1. local login signs with the in-app wallet ──────────────────────────────
  console.log('local (in-app wallet) post:');
  {
    const { ctx, api } = build();
    ctx.__local = true;
    const provider = makeProvider();
    const prep = { to: '0x' + '1'.repeat(40), data: '0x1c9d6ab3', value: '0x0',
                   chain_id: 42220, network: 'celo' };
    const hash = await api._postSendTransaction(provider, prep).then((h) => h, (e) => 'ERR:' + e.message);
    const tx = provider.sent[0];
    check('broadcast happens', provider.sent.length === 1, hash);
    check('from is the session wallet', tx && tx.from.toLowerCase() === WALLET, tx && tx.from);
    check('carries the post calldata', tx && tx.data === '0x1c9d6ab3');
    check('carries the chainId (in-app wallet routes by it)', tx && tx.chainId === 42220);
    // The in-app wallet estimates via ethers, so it must NOT be sent a fee.
    check('no gas field (in-app wallet estimates locally)', tx && tx.gas === undefined);
    check('no feeCurrency for a native-CELO choice', tx && tx.feeCurrency === undefined);
  }

  // ── 1b. the in-app wallet carries the chosen CIP-64 fee currency ─────────────
  console.log('in-app wallet with a fee currency:');
  {
    const CUSD = '0x765DE816845861e75A25fCA122bb6898B8B1282a';
    const { ctx, api } = build();
    ctx.__local = true;
    const provider = makeProvider();
    await api._postSendTransaction(provider, {
      to: '0x' + '1'.repeat(40), data: '0x1c9d6ab3', value: '0x0',
      chain_id: 42220, network: 'celo',
      fee_currency: 'CUSD', fee_currency_address: CUSD, fee_currency_native: false
    }).catch(() => {});
    const tx = provider.sent[0];
    check('carries the chosen feeCurrency', tx && String(tx.feeCurrency).toLowerCase() === CUSD.toLowerCase(),
      tx && tx.feeCurrency);
    check('still no gas fields', tx && tx.gas === undefined && tx.gasPrice === undefined);

    // A cancelled PIN prompt must NOT be retried with another token.
    const cancel = build();
    cancel.ctx.__local = true;
    let localAttempts = 0;
    const cancelling = {
      async request() {
        localAttempts += 1;
        const e = new Error('User cancelled');
        e.code = 4001;
        throw e;
      }
    };
    await cancel.api._postSendTransaction(cancelling, {
      to: '0x' + '1'.repeat(40), data: '0x1c9d6ab3', value: '0x0',
      chain_id: 42220, network: 'celo',
      fee_currency: 'CUSD', fee_currency_address: CUSD, fee_currency_native: false
    }).catch(() => {});
    check('a cancelled PIN prompt is not retried', localAttempts === 1, String(localAttempts));

    // A fee-currency rejection IS retried with native CELO.
    const retry = build();
    retry.ctx.__local = true;
    let retryTxs = [];
    const feeRejecting = {
      async request(payload) {
        retryTxs.push(payload.params[0]);
        if (payload.params[0].feeCurrency) {
          const e = new Error('feeCurrency not supported');
          e.code = -32602;
          throw e;
        }
        return '0x' + 'd'.repeat(64);
      }
    };
    const ok = await retry.api._postSendTransaction(feeRejecting, {
      to: '0x' + '1'.repeat(40), data: '0x1c9d6ab3', value: '0x0',
      chain_id: 42220, network: 'celo',
      fee_currency: 'CUSD', fee_currency_address: CUSD, fee_currency_native: false
    }).catch((e) => 'ERR:' + e.message);
    check('the in-app wallet retries without the token', retryTxs.length === 2, String(retryTxs.length));
    check('the retry dropped the field', retryTxs[1] && retryTxs[1].feeCurrency === undefined);
    check('the in-app retry succeeded', ok === '0x' + 'd'.repeat(64), ok);
  }

  // ── 2. injected wallet gets explicit gas + gasPrice ──────────────────────────
  console.log('injected (MetaMask) post:');
  {
    const { ctx, api } = build();
    ctx.__local = false;
    const provider = makeProvider({ accounts: [OTHER, WALLET] });
    const prep = { to: '0x' + '1'.repeat(40), data: '0x1c9d6ab3', value: '0x0',
                   chain_id: 42220, network: 'celo' };
    await api._postSendTransaction(provider, prep).catch(() => {});
    const tx = provider.sent[0];
    check('broadcast happens', provider.sent.length === 1);
    check('gas is sent', tx && typeof tx.gas === 'string', tx && tx.gas);
    check('gasPrice is sent', tx && typeof tx.gasPrice === 'string', tx && tx.gasPrice);
    check('gas is the padded estimate (70000*1.4 = 98000)',
      tx && BigInt(tx.gas) === 98000n, tx && tx.gas);
    check('gasPrice comes from the node', tx && BigInt(tx.gasPrice) === 100000000000n);
    check('binds to the session wallet, not accounts[0]',
      tx && tx.from.toLowerCase() === WALLET, tx && tx.from);
    check('switches to the room chain first', ctx.__chainSwitches.length === 1);
  }

  // ── 3. a wrong account is refused, never silently used ───────────────────────
  console.log('wrong wallet:');
  {
    const { ctx, api } = build();
    ctx.__local = false;
    const provider = makeProvider({ accounts: [OTHER] });
    const prep = { to: '0x' + '1'.repeat(40), data: '0x1c9d6ab3', value: '0x0', network: 'celo' };
    const err = await api._postSendTransaction(provider, prep).then(() => null, (e) => e.message);
    check('throws instead of signing', typeof err === 'string' && err.length > 0);
    check('says the wallet is wrong', /Wrong wallet connected/i.test(err || ''), err);
    check('nothing was broadcast', provider.sent.length === 0);
  }

  // ── 4. fee fallback when the node cannot estimate ────────────────────────────
  console.log('fee fallback:');
  {
    const { ctx, api } = build();
    ctx.__local = false;
    const provider = makeProvider({ estimateFails: true, gasPriceFails: true });
    const prep = { to: '0x' + '1'.repeat(40), data: '0x1c9d6ab3', value: '0x0', network: 'celo' };
    await api._postSendTransaction(provider, prep).catch(() => {});
    const tx = provider.sent[0];
    check('still sends a gas limit', tx && BigInt(tx.gas) === 150000n, tx && tx.gas);
    check('still sends a gas price', tx && BigInt(tx.gasPrice) === 500000000000n, tx && tx.gasPrice);
  }

  // ── 5. the three-hop flow ────────────────────────────────────────────────────
  console.log('three-hop postMessage:');
  {
    const { ctx, api } = build();
    ctx.__local = true;
    ctx.__provider = makeProvider();
    const calls = [];
    global.fetch = async (url, opts) => {
      calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
      if (url.indexOf('prepare-post') >= 0) {
        return { ok: true, json: async () => ({ success: true, to: '0x' + '1'.repeat(40),
          data: '0x1c9d6ab3', value: '0x0', chain_id: 42220, network: 'celo' }) };
      }
      if (url.indexOf('posted') >= 0) {
        return { ok: true, json: async () => ({ success: true,
          explorer_url: 'https://celoscan.io/tx/0xdead' }) };
      }
      throw new Error('unexpected url ' + url);
    };
    const out = await api.postMessage('hello room', { replyToId: 3 }).catch((e) => ({ err: e.message }));
    check('prepare-post called first', calls[0] && calls[0].url.indexOf('prepare-post') >= 0);
    check('the reply target is forwarded', calls[0] && calls[0].body.reply_to_id === 3);
    check('posted called last', calls[calls.length - 1].url.indexOf('posted') >= 0);
    check('the tx hash is reported back', calls[calls.length - 1].body.tx_hash.startsWith('0x'));
    check('returns the tx hash', out.tx_hash && out.tx_hash.startsWith('0x'), JSON.stringify(out));
    check('signing happened between the two calls', ctx.__provider.sent.length === 1);
  }

  // ── 6. a failed cache-drop must not fail a broadcast post ────────────────────
  console.log('cache-drop failure tolerance:');
  {
    const { ctx, api } = build();
    ctx.__local = true;
    ctx.__provider = makeProvider();
    global.fetch = async (url) => {
      if (url.indexOf('prepare-post') >= 0) {
        return { ok: true, json: async () => ({ success: true, to: '0x' + '1'.repeat(40),
          data: '0x1c9d6ab3', value: '0x0', chain_id: 42220, network: 'celo' }) };
      }
      throw new Error('network down');
    };
    const out = await api.postMessage('hello').then((r) => r, (e) => ({ err: e.message }));
    check('still resolves (the message IS on-chain)', !out.err, JSON.stringify(out));
    check('returns the hash', out.tx_hash && out.tx_hash.startsWith('0x'));
  }

  // ── 7. CIP-64: a chosen fee currency is sent, and CELO is the fallback ───────
  console.log('CIP-64 fee currency:');
  {
    const CUSD = '0x765DE816845861e75A25fCA122bb6898B8B1282a';
    // 7a. The chosen token is attached to both the estimate and the send.
    const { ctx, api } = build();
    ctx.__local = false;
    const provider = makeProvider({ accounts: [WALLET] });
    const prep = { to: '0x' + '1'.repeat(40), data: '0x1c9d6ab3', value: '0x0',
                   chain_id: 42220, network: 'celo',
                   fee_currency: 'CUSD', fee_currency_address: CUSD, fee_currency_native: false };
    await api._postSendTransaction(provider, prep).catch(() => {});
    const tx = provider.sent[0];
    check('the chosen feeCurrency is sent', tx && String(tx.feeCurrency).toLowerCase() === CUSD.toLowerCase(),
      tx && tx.feeCurrency);
    check('gas is still explicit', tx && typeof tx.gas === 'string' && typeof tx.gasPrice === 'string');

    // 7b. Native CELO omits the field entirely (every wallet can honour it).
    const native = build();
    native.ctx.__local = false;
    const p2 = makeProvider({ accounts: [WALLET] });
    await native.api._postSendTransaction(p2, {
      to: '0x' + '1'.repeat(40), data: '0x1c9d6ab3', value: '0x0',
      chain_id: 42220, network: 'celo', fee_currency: 'CELO', fee_currency_native: true
    }).catch(() => {});
    check('native CELO sends no feeCurrency field', p2.sent[0] && p2.sent[0].feeCurrency === undefined);
    check('candidates are [native] for CELO',
      JSON.stringify(api._feeCurrencyCandidates({ fee_currency: 'CELO' })) === '[null]');

    // 7c. A wallet that rejects the fee currency is retried with native CELO.
    const retry = build();
    retry.ctx.__local = false;
    let attempts = [];
    const rejecting = {
      sent: attempts,
      async request(payload) {
        if (payload.method === 'eth_requestAccounts' || payload.method === 'eth_accounts') return [WALLET];
        if (payload.method === 'eth_estimateGas') return '0x' + (70000).toString(16);
        if (payload.method === 'eth_gasPrice') return '0x' + (100000000000).toString(16);
        if (payload.method === 'eth_sendTransaction') {
          attempts.push(payload.params[0]);
          if (payload.params[0].feeCurrency) {
            const e = new Error('feeCurrency not whitelisted');
            e.code = -32602;
            throw e;
          }
          return '0x' + 'c'.repeat(64);
        }
        return null;
      }
    };
    const hash = await retry.api._postSendTransaction(rejecting, {
      to: '0x' + '1'.repeat(40), data: '0x1c9d6ab3', value: '0x0',
      chain_id: 42220, network: 'celo',
      fee_currency: 'CUSD', fee_currency_address: CUSD, fee_currency_native: false
    }).catch((e) => 'ERR:' + e.message);
    check('retried after the fee-currency rejection', attempts.length === 2, JSON.stringify(attempts.length));
    check('first attempt carried the token', attempts[0] && attempts[0].feeCurrency);
    check('retry dropped the field (native CELO)', attempts[1] && attempts[1].feeCurrency === undefined);
    check('the retry succeeded', hash === '0x' + 'c'.repeat(64), hash);

    // 7d. A USER REJECTION is never retried with another token.
    const cancel = build();
    cancel.ctx.__local = false;
    let cancels = 0;
    const cancelling = {
      async request(payload) {
        if (payload.method === 'eth_requestAccounts' || payload.method === 'eth_accounts') return [WALLET];
        if (payload.method === 'eth_estimateGas') return '0x' + (70000).toString(16);
        if (payload.method === 'eth_gasPrice') return '0x' + (100000000000).toString(16);
        if (payload.method === 'eth_sendTransaction') {
          cancels += 1;
          const e = new Error('User rejected the request');
          e.code = 4001;
          throw e;
        }
        return null;
      }
    };
    const cancelErr = await cancel.api._postSendTransaction(cancelling, {
      to: '0x' + '1'.repeat(40), data: '0x1c9d6ab3', value: '0x0',
      chain_id: 42220, network: 'celo',
      fee_currency: 'CUSD', fee_currency_address: CUSD, fee_currency_native: false
    }).then(() => null, (e) => e.message);
    check('a user rejection stops the flow', cancels === 1, String(cancels));
    check('the rejection reason is surfaced', /reject/i.test(cancelErr || ''), cancelErr);
  }

  // ── 8. a rejected prepare is surfaced ────────────────────────────────────────
  console.log('prepare rejection:');
  {
    const { ctx, api } = build();
    ctx.__local = true;
    ctx.__provider = makeProvider();
    global.fetch = async () => ({
      ok: false, json: async () => ({ success: false, error: 'You cannot post right now.' })
    });
    const err = await api.postMessage('hi').then(() => null, (e) => e.message);
    check('throws the server reason', /cannot post/i.test(err || ''), err);
    check('nothing was signed', ctx.__provider.sent.length === 0);
  }

  console.log('');
  if (failures) {
    console.log('FAILED: ' + failures + ' assertion(s)');
    process.exit(1);
  }
  console.log('All chatroom on-chain harness assertions passed.');

}

main().catch((e) => { console.error(e); process.exit(1); });
