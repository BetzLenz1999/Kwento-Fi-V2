# GoodMarket / GoodDollar Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Development Progress

Repository-specific notes for OpenHands agents.

## Stack
- Flask backend (Python): `routes.py`, `main.py`, `blockchain.py`, `supabase_client.py`.
- Frontend: Jinja templates in `templates/`, vanilla JS in `static/js/`.
- ethers.js **v6.13.4** loaded from cdnjs (swap.html, claim.html, send-link.html). p2p.html/savings.html use 6.7.0. xdc_wallet.html uses 6.13.2.
- Celo mainnet (chainId 42220 / 0xa4ec). Public RPC: `https://forno.celo.org`.

## Wallet provider model
- `static/js/wc-bridge.js`: WalletConnect EIP-1193 bridge. Routes **wallet-scoped** methods (eth_sendTransaction, personal_sign, signTypedData*) to the WC session; routes **read-only** calls (eth_call, eth_estimateGas, eth_getBalance) directly to Celo RPC via `_celoJsonRpc` / `_celoJsonRpcWithFallback`.
- `templates/swap.html` has its OWN inline `_celoJsonRpc` + `_wcBridgeRequest` (a second copy of the bridge logic for the swap page). **Changes to RPC error handling must be mirrored in both wc-bridge.js and swap.html.**
- Signer resolution: `getConnectedSwapSigner()` in swap.html picks Privy / WalletConnect / injected (Trust/MetaMask) in that order based on `IS_PRIVY_LOGIN` / `PREFER_WC_SIGNING`.

## Local self-custodial wallet (GMLocalWallet) signing (2026-08)
Local-wallet users (`login_method === 'local'`, session wallet created in-browser via `static/js/local-wallet.js`) must sign with the **PIN-decrypted in-app wallet** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ never with an injected MetaMask/extension, which is a *different account*. When the wallet auto-locks, prompt the built-in PIN modal (`window._lwOpenUnlockModal()`), then use `GMLocalWallet.getProvider()` (an EIP-1193 provider; wrap in `new ethers.BrowserProvider(...)` + `getSigner()` when callers need a real ethers Signer, e.g. swap.html's `getConnectedSwapSigner()`).

Key invariants when touching signing flows:
- **Celo & XDC only (2026-08 update)** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the in-app wallet signs on a fixed `SUPPORTED_CHAINS` registry in `local-wallet.js` (Celo 0xa4ec + XDC 0x32); everything else (Fuse, etc.) gets a friendly "supports Celo and XDC only" throw from the provider. `wallet_switchEthereumChain` moves an `_activeChainKey` pointer (no user prompt) and `eth_sendTransaction` routes by the tx's own `chainId` or the active chain; `wallet_addEthereumChain` is a no-op for known chains. See the dedicated section below.
- **No injected fall-through** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ every `_getEthProvider()`-style resolver must return null / be bypassed for local logins so a desktop MetaMask can't hijack the account ("Wrong wallet connected").
- **No CIP-64** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ MiniPay `isMiniPay()` checks return false for local logins (in-app wallet pays CELO gas); the `MPGasTopUp.isMiniPay()` preflight gates in swap.html are skipped too.
- Pages wired: `savings.html` (reference implementation), `swap.html` (real ethers signer + Celo-only guards + no-wallet button gate), `reloadly.html` (send + signing-flow gating), `claim.html` (claim branch; also fixed `injectedProvider` block-scope bug), `static/js/p2p/p2p-wallet.js` (`getProvider()` unlock prompt, `_getInjected()`/`isMiniPay()` guards), `wallet.html` (doSend, raffle signer, claim capabilities/preflight, `_getSigningProvider`/stream signing via `signTransaction`, `_getEthProvider` local gate), `send-link.html` (send/cancel/balance), `learn_and_earn.html` (loads ethers+local-wallet.js; L&E deposit `swSendViaWallet` + NFT buy `executeBuyNFT`), `xdc_wallet.html` (XSwap G$Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐÂ¶XDC `_sendXswapTx`, legacy bridge, `sendToken`, `claimGDonXDC` via `_xdcGetSigningProvider()`; `_getEthProvider()` returns null for local; read-only `_xswapReadProvider`/`autoSwitchToXDCIfOnCelo` stay on `_getEthProvider()` so they fall back to public XDC RPC without popping PIN; `switchToCelo` just redirects to /wallet for local).
- **ethers.js is mandatory on any page using GMLocalWallet** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `local-wallet.js` `_assertEthers()` throws "ethers.js is not loaded on this page." from PIN decrypt (`Wallet.fromEncryptedJson`) and `eth_sendTransaction`. reloadly.html was wired for local signing but never loaded ethers, so PIN unlock + G$ pay both failed while savings/swap (which load ethers) worked. Fixed 2026-08: reloadly.html now loads ethers 6.13.4 before local-wallet.js, and `_isMiniPayContext()` returns false for `isGMLocalWallet` providers (no CIP-64 feeCurrency for the in-app wallet). When wiring a NEW page for local signing, always load ethers first.
- Tests: `tests/test_local_wallet_signing_content.py` (content, no deps).

## Local wallet Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ XDC support for local logins (2026-08)
The in-app wallet used to be hard-pinned to Celo (the provider always reported 0xa4ec, XDC/FUSE paths threw "supports Celo only" and the claim UI showed a "Ð â¢Ð¢ÑÐ âÐ¢ÐÐ âÐ¢â Use MetaMask / WalletConnect" banner). EVM keys are chain-agnostic, so XDC was enabled for local logins:
- **`static/js/local-wallet.js`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `SUPPORTED_CHAINS` registry (`celo` 0xa4ec / `xdc` 0x32) with per-chain RPC fan-out (`_chainJsonRpc`, failover + revert-data preservation same as before). `wallet_switchEthereumChain` moves `_activeChainKey` for a supported chain, throws for others; `wallet_addEthereumChain` no-ops for known chains; `eth_sendTransaction` picks the chain from `tx.chainId` (hex or numeric) else the active chain, and builds the ethers signer on that chain's primary RPC. `lock()` resets the pointer to Celo. **Unsupported chains must keep throwing** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ never widen the registry without auditing every same-page flow.
- **Claim UI** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `getClaimWalletCapabilities()` (wallet-main.js) now returns `supportsXdc = isLocal || (...)`; `getClaimProviderAndFrom()` has a local branch (PIN unlock Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `GMLocalWallet.getProvider()` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ address check) so `executeNetworkClaim('xdc')` + `claimXdcInjected` work end-to-end for local logins. The XDC gas preflight (`ensureXdcGasReadyBeforeClaim`) is backend-faucet based, so it needed no change. The "Use MetaMask / WalletConnect" banner no longer appears for local logins; wallet.html note/announcement text updated.
- **XDC token sends** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ wallet-main.js `doSend` gates only Fuse for local now; the XDC send just chain-switches the local provider to 0x32.
- **Swap bridge FROM XDC** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ swap.html `_warmUpXdcWalletProvider` warms the PIN-unlocked local provider (but **never pops the PIN modal at page load** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ returns null when locked; `_sendXdcBridgeTx` re-resolves and prompts at click time via `_swapGetLocalWalletProvider()`). Fuse swap (`getConnectedFuseSigner`) stays blocked with an updated "Celo & XDC only" message.
- **bridgeXdcToCelo provider-resolution fix (2026-08 follow-up):** its account-resolution block used to fall through straight from `_warmUpXdcWalletProvider()` (null when the wallet is locked) to the injected `_awaitEthProvider()` fallback Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so a locked local wallet compared the INJECTED MetaMask address against the session wallet and threw "Ð â¢Ð¢ÑÐ âÐ¢ÑÐ âÐÂ« Wrong wallet connected. Please switch to your GoodMarket wallet." even though the user WAS on their GoodMarket wallet. The block now has an explicit `else if (LOGIN_METHOD === 'local')` branch that re-resolves via `_swapGetLocalWalletProvider()` (PIN unlock prompt) before ever touching the injected fallback. Locked in by `test_swap_xdc_supported_fuse_blocked_for_local`.
- **Same-page-session switch-back guards** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the provider pointer persists within a page, so after an XDC claim/bridge a Celo flow in the SAME page could estimate/broadcast on the wrong chain. Every Celo-bound local flow now switches back first (cheap no-op): wallet-main.js `getRaffleSignerProvider` + `submitGcashCashout` (`_ensureCeloChainForSigning`), swap.html `getConnectedSwapSigner` local branch, wallet-streams.js `signTransaction` (the old `!isLocalProvider` skip was removed). **Any new Celo-bound local flow added to wallet.html/swap.html must switch to 0xa4ec first.**
- **No injected fall-through still holds** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `_getEthProvider()` keeps returning null for local logins; an injected MetaMask on a different account is never prompted, on any chain.
- Tests: `tests/test_local_wallet_signing_content.py` (`test_swap_xdc_supported_fuse_blocked_for_local`, `test_local_wallet_provider_supports_celo_and_xdc`, `test_wallet_claim_capabilities_xdc_supported_for_local`, updated stream/send tests) + `tests/test_local_wallet_content.py` (`test_js_provider_routes_reads_to_chain_and_signs_locally`).

## XDC gas check must precede the faucet request (2026-09)
The XDC claim path used to call `/api/xdc/faucet/gas` as its "gas check". That endpoint performs the top-up, so a wallet that **already held enough XDC gas still requested a GoodDollar faucet entitlement**, and the "waiting for gas to arrive" poll re-hit the faucet API on every tick. Celo was always correct Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `/api/faucet/status` (read-only) first, then `/api/faucet/gas` only when short.
- **`routes.py`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ new read-only `POST /api/xdc/faucet/status` (mirror of `/api/faucet/status`): computes `_get_xdc_gas_status` + `_has_recent_refill`, returns `gas_ready` / `status` / `terminal_status` and **never** calls `GOODDOLLAR_XDC_FAUCET_API_URL`, `_execute_onchain_xdc_faucet_topup` or `_record_recent_refill`. `/api/xdc/faucet/gas` is unchanged and still short-circuits when gas is already ready.
- **`static/js/wallet-main.js`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `ensureXdcGasReadyBeforeClaim()` now does Step A `/api/xdc/faucet/status` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ early return when `gas_ready` (no faucet call), Step B `/api/xdc/faucet/gas` only when short. `pollForXdcGasArrival()` polls the read-only status endpoint instead of the top-up endpoint.
- **`templates/xdc_wallet.html`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the "Checking XDC gasÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£" pre-check uses `/api/xdc/faucet/status`; `/api/xdc/faucet/gas` is requested only inside `if (!preGasData.gas_ready)`. The `recent_refill` retry now re-requests with `force_onchain:true` after the initial `force_onchain:false` top-up (previously the first call *was* the top-up).
- **Rule:** any gas preflight must READ readiness first and only request a faucet top-up when the balance is short Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ never use a top-up endpoint as a status check, and never poll a top-up endpoint while waiting for credit.
- Tests: `tests/test_xdc_gas_check_before_faucet_content.py` (10 tests, no deps). Run with `python -m pytest tests/test_xdc_gas_check_before_faucet_content.py`.

## XDC UBI claim must send an explicit gas limit (2026-10)
The XDC claim never sent a `gas` limit, unlike the Celo claim which was fixed for the same bug (see `tests/test_claim_celo_tx_gas_content.py`). With no gas limit the wallet must estimate the fee through **its own XDC RPC**; when that estimate fails (slow/dead RPC, XDC network added but not fully usable) MetaMask shows a red **"Network fee unavailable"** field and the claim never leaves the wallet Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the exact report from injected/MetaMask users. It worked for the reporter (healthy custom XDC RPC + in-app local wallet, which estimates via ethers) but not for others.
- **`static/js/wallet-main.js` `claimXdcInjected`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ now mirrors `claimCeloInjected`: `eth_estimateGas` on the real `claim()` calldata Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ pad **40%** (`estimated * 140n / 100n`) Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ fall back to `0x7a120` (500 000, the backend cap) Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ always send `to: XDC_UBI_CONTRACT, data: CLAIM_DATA, value: '0x0', gas: gasHex, gasPrice: gasPriceHex`.
- **`gasPrice` MUST be sent too (2026-10 follow-up):** sending `gas` alone was not enough. `eth_estimateGas` returns only the gas **units** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the wallet still has to ask its own RPC for the **price**, and a flaky XDC RPC fails that second round-trip even when the estimate succeeds, so the fee field still went red. We now `eth_gasPrice` and send `gasPrice`, with a generous `0x5d21dba00` (25 gwei) fallback, removing the wallet's fee lookup entirely Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the same proven pattern as `learn_and_earn.html` / savings (which send `gas` + `gasPrice`). **Never drop `gasPrice` from this send.**
- The XDC failure message path now calls `GMTxError.format(err, { nativeSymbol: 'XDC' })` (was the CELO default) and has a dedicated `_isNetworkFeeUnavailableError` message, so a real XDC fee problem is no longer mislabeled as a CELO one.
- **New `_isNetworkFeeUnavailableError(err)`** matches the user-visible strings ("network fee unavailable", "cannot estimate", "invalid network fee", Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£). The send retry now fires the healthy-RPC repair (`_promptAddHealthyXdcRpc`) for **either** an unreachable RPC **or** a fee-estimation failure, then retries once Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ previously a fee error matched nothing and the raw error surfaced.
- **`-32603` now counts as "chain missing"** alongside `4902` in `claimXdcInjected` (some older MetaMask/Trust builds report a missing chain this way), matching `_aiEnsureXdcChain`. Without it the auto-add silently never ran.
- **`templates/xdc_wallet.html`** standalone claim Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ same explicit-gas treatment, and the non-standard `chainId` field is **removed from `eth_sendTransaction` params** (MetaMask Mobile / Trust reject it; the wallet is already on XDC after the switch).
- **Auto-setup already existed and cannot be made silent:** `wallet_addEthereumChain` / `wallet_switchEthereumChain` always require an in-wallet user confirmation (EIP-1193) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the "Add XDC Network?" prompt is expected. Auto-setup only fires when the chain is MISSING (4902/-32603); a user who already has XDC with a bad RPC never sees it, which is why the gas limit is the load-bearing fix and the repair path is the safety net.
- **PROACTIVE wallet-RPC repair (2026-10 follow-up):** the repair used to run ONLY after a failed send Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ by then the red "Network fee unavailable" field had already appeared. `claimXdcInjected` now probes the **wallet's own XDC RPC** (`eth_blockNumber`, via `_walletXdcRpcHealthy`) right after the chain switch and, if it can't answer, offers `_promptAddHealthyXdcRpc` BEFORE building the tx (`_ensureHealthyXdcRpc`). This targets users who added XDC months ago with a now-flaky legacy RPC (`rpc.xinfin.network` / `erpc.xinfin.network` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ both intermittently hang). The post-failure retry is gated by `rpcRepairAttempted` so the prompt never fires twice in one attempt. Declining the prompt is non-fatal (fallback gas/gasPrice still try). Same probe added to `templates/xdc_wallet.html`.
- **No logout / disconnect / reconnect needed.** `wallet_addEthereumChain` for an already-known chain updates its RPC URLs in place and switches to it Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ MetaMask asks the user to approve once, and the GoodMarket session is untouched. `wallet_switchEthereumChain` alone does NOT change RPC URLs (it only moves the active chain pointer), so the repair must always be `wallet_addEthereumChain` with the chainId, even when the chain already exists.
- **"Review alerts" / red fee field is MetaMask SIMULATING a reverting claim (2026-10):** MetaMask runs a simulation (`simulationFails`) before showing the confirm sheet. When the claim would revert on-chain it renders the fee field **red** and replaces the confirm button with a blocking **"Ð â¢Ð¢ÑÐ âÐ¢ÐÐ âÐ¢â Review alerts"** (two or more alerts Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ plural; `footer.tsx` `reviewAlertButtonText`). The user believes it is a fee problem because the whole fee row is red Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ but the fee value is fine. Root cause on our side: `executeNetworkClaim('xdc')` opened the wallet even when the backend already reported `can_claim: false`, so the wallet dutifully simulated a claim() that reverts (already claimed today / not verified). Fixes: (1) **never open the wallet for a claim the backend knows will revert** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `executeNetworkClaim` now returns early when `claims.xdc.success !== false && claims.xdc.can_claim === false`, showing `claims.xdc.reason`; (2) `check_xdc_ubi_entitlement` now returns a `reason` ("not yet face-verified on XDCÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£" vs "may have already claimed today") and `/api/claim/availability` forwards it; (3) the pre-send status line explains the Review-alerts screen and the RPC fix; (4) `_withWalletSignTimeout` (180s) turns a user stuck on that sheet into actionable guidance instead of an infinite spinner.
- **A JSON-RPC revert counts as a HEALTHY wallet RPC.** `_walletXdcRpcHealthy` must return true when `eth_blockNumber` fails with a structured JSON-RPC error (any numeric `code`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ only a transport failure (fetch/timeout/5xx/HTML body) means the endpoint is dead. Treating a revert as "unhealthy" nags every correctly-configured user with a needless network-update prompt. Verified live: `eth_call claim()` on the XDC UBIScheme returns `0x0` (no revert) from a non-claimer, and `eth_estimateGas` returns `0x12cc0` (~77k), so the *chain* is fine Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the revert only appears in MetaMask's own simulation for wallets that genuinely have nothing to claim.
- **MetaMask security alerts run on a fixed supported-network list** (Arbitrum, Avalanche, Base, BSC, Ethereum, Linea, Optimism, Polygon, Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£ Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ **XDC is not on it**). On unsupported chains the simulation/scan returns `ErrorResult`, which is one of the paths that sets `simulationFails`; combined with a would-revert call that yields the blocking Review-alerts state. The app cannot control the alert Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ only avoid triggering it by not sending reverting txs.
- **Do not remove the explicit `gas`/`gasPrice`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ re-introducing the bare send re-creates the red "Network fee unavailable" dead-end. Locked in by `tests/test_claim_xdc_tx_gas_content.py` (13 tests, no deps).

## XDC claim "Ð â¢Ð¢ÑÐ âÐ¢ÐÐ âÐ¢â Review alerts" dead-end Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ stale wallet-RPC balance (2026-10 follow-up)
A second XDC claim report (injected **MetaMask Mobile**) showed the fee row rendered **RED** (`Severity.Danger` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `TextColor.ErrorDefault`) with the button replaced by a blocking **"Ð â¢Ð¢ÑÐ âÐ¢ÐÐ âÐ¢â Review alerts"** and **no way to proceed**. The screenshot's fee *value* (`0.0023 XDC`) was present, which means it is **not** a "Network fee unavailable" case Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ it is the **balance** check failing. On-chain probes of the reported wallet (`0xEE08Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£b25C`) proved the claim was entirely valid: **4.796 XDC** balance, XDC `isWhitelisted = 1`, `checkEntitlement() = 29.87 G$`, `eth_call claim()` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ **`0x1` (no revert)**, `eth_estimateGas` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `0x27b17`, and MetaMask's own Blockaid API returned **`Benign`**. So: **not FV, not whitelist, not a revert, not funds, not Blockaid** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ MetaMask simply could not validate the fee/balance through the **wallet's own XDC RPC**.
- **Fee-row alert mapping (MetaMask mobile, verified in `/tmp/mm-mobile`):** the only `RowAlertKey.EstimatedFee` alerts that are **`Severity.Danger`** are `useInsufficientBalanceAlert` ("Insufficient funds", `isBlocking: true`) and `useGasSponsorshipWarningAlert` (**Monad-only**). `useGasEstimateFailedAlert`/`useGasLimitBelowMinimumAlert` are only *Warning* (amber), and a plain `GasEstimateFailed` is Info/non-blocking. A **red** fee label therefore means the **InsufficientBalance** alert: `balanceWei < gasPrice Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð gas + value`, read from the wallet's RPC. With 4.8 XDC vs 0.0023 XDC needed, the wallet was reading a **stale/zero balance**.
- **Root cause on our side Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ we advertised the dead `https://rpc.xdc.org`** (HTTP **502**, verified repeatedly) in the XDC RPC lists. A wallet that adopts our `rpcUrls` list can end up pinned to it and fail its own balance read Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ red fee row + blocked confirm. Removed from **both** `static/js/wallet-main.js` (`XDC_RPC_URLS`, used by `wallet_addEthereumChain`) and `blockchain.py` (`_build_xdc_rpc_urls`), replaced with the verified-good `https://rpc.xinfin.network`. **Never advertise an unverified endpoint in an RPC list** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a wallet may make it its active node.
- **Probe now reads the BALANCE, not just the block number.** `_walletXdcRpcHealthy(provider, from)` reads `eth_blockNumber` **and** `eth_getBalance`; the JSON-RPC-alive logic moved into a shared `_walletRpcRead` (a structured error still counts as alive). New `_walletXdcBalanceLooksStale(provider, from)` cross-checks a near-zero wallet balance (`< 0.001 XDC`) against a known-good public RPC and flags it, so a node that answers `eth_blockNumber` but returns a stale `0` balance triggers the repair. `_xdcWalletRpcNeedsRepair` combines both; `_ensureHealthyXdcRpc(provider, alreadyAsked, from)` now takes `from`.
- **Pre-send public-RPC simulation.** `_simulateXdcClaimOnPublicRpc(from)` runs `eth_call claim()` on the public RPC list BEFORE the wallet is asked to sign. A revert or a `0x0` result aborts with the real reason (tagged `err.__simulationBlock`, surfaced verbatim by `executeNetworkClaim`) instead of opening a doomed sheet. **Reads go through the public RPC, never `provider.request`** (same rule as the chat bridges); it **fails OPEN** if no RPC answers.
- **Copy fixed.** The old status line told users to *"tap Review alerts Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ Continue"* Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ that control does not exist for a fee/balance block, so it stranded them. It now says the red fee row + "Ð â¢Ð¢ÑÐ âÐ¢ÐÐ âÐ¢â Review alerts" means the wallet could not validate the XDC fee/balance ("a wallet RPC problem, not a funds problem Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£ cannot be tapped past it") and names the concrete remedy: set the XDC Network RPC URL to `https://erpc.xinfin.network`, then retry.
- **The advertised XDC RPC hostname was a TYPO (2026-10 follow-up).** [xdc-rpc-host-guard: allow-bad-host-literal] Everywhere we told a user or a wallet to use `earpc.xinfin.network` it should have been `erpc.xinfin.network` (Enterprise RPC). The bad host does not exist: the parent zone `xinfin.network` is on Cloudflare NS and answers **NXDOMAIN** for `earpc` on both Cloudflare and Google public resolvers, while `erpc` resolves and answers `eth_chainId` = `0x32`. So our own error copy ('set your RPC URL to ...') and our `wallet_addEthereumChain` rpcUrls list (where it was entry **#1**, the one a wallet is most likely to adopt) both pointed at a dead host - which MetaMask renders as a red Network fee row plus a blocking 'Review alerts' button. Origin: `goodmarket_claim_reconciler.py` has a comment naming the correct `erpc.xinfin.network` right above code using the typo. Fixed in 19 files (JS, templates, Python, docs, tests). Also swapped the equally dead `explorer.xinfin.network` (NXDOMAIN) for the live XDCScan explorer (`/tx/<hash>`, since the old `/txs/` path 302-redirects); kept each file's existing `.com`/`.io` convention rather than churning the rest. **Guard:** `tests/test_xdc_rpc_endpoint_hosts_content.py` scans the whole tree for the bad literals (assembled from fragments so the test file itself is clean), asserts the correct host is first in `XDC_RPC_URLS` and present in `blockchain.py`/`goodmarket_claim_reconciler.py`, and does a live `getaddrinfo` check on every advertised endpoint. **Lesson: never advertise an unverified hostname - resolve it first.**
- **Do not re-add `rpc.xdc.org`; keep the balance probe + public-RPC simulation.** Locked in by `tests/test_xdc_review_alerts_deadend_content.py` (12 tests, no deps) and the updated `tests/test_claim_xdc_tx_gas_content.py`.

## Proactive one-tap XDC network repair banner (2026-10)
The claim-time repair only helps a user who already tapped Claim. This surfaces the same fix earlier, on the wallet page, as a dismissible banner with a one-tap button Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so a stale RPC is fixed before the red fee row / "Ð â¢Ð¢ÑÐ âÐ¢ÐÐ âÐ¢â Review alerts" ever appears.
- **Why it cannot be silent (EIP-1193):** a dApp cannot read the wallet's configured RPC URL and cannot change it without a user confirmation. `wallet_addEthereumChain` on an already-known chain updates its RPC URLs **in place** and switches to it, but the wallet always asks the user to approve once. So the design is **detect behaviourally Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ offer one tap**, never a silent switch.
- **Detection (`static/js/wallet-main.js`):** `checkXdcRpcHealthProactively()` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ resolves the **injected** provider only (returns null for `local` / Privy / `_gmPreferWc()`), reads `eth_chainId`, and **only probes when the wallet is ALREADY on XDC** (switching chains unprompted would pop an unexpected dialog). It then calls the existing `_xdcWalletRpcNeedsRepair` (blockNumber + balance probe, stale-balance cross-check). Needs-repair Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ show banner + stamp SEEN; healthy Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ hide.
- **Throttle / no nag:** `gmXdcRpcRepairSeen_v1` stamps at *show* with a **24 h** window (`XDC_RPC_BANNER_THROTTLE_MS`); `gmXdcRpcRepairDone_v1` is set only after a successful repair + clean re-probe and then stops probing until forced. `{force:true}` bypasses both Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ used by the load probe and the claim-open re-probe.
- **One-tap repair:** `repairXdcRpcNow()` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `_promptAddHealthyXdcRpc(provider)` (updates the RPC list in place) Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ wait Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ re-probe. Clean Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ stamp FIXED + "Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐ Ò² XDC network updated". Still broken Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ honest fallback ("still reads slowlyÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£ remove & re-add"). Cancelled (`4001`) Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ no FIXED stamp, retryable. The button is re-enabled in `finally`.
- **Lifecycle wiring:** probe `setTimeout(Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£, 2500)` after load (delayed so late-injecting wallets are seen) + on `visibilitychange` (visible) + `openClaimOrVerify()` forces a fresh probe (the moment the problem bites).
- **UI:** `#xdcRpcRepairBanner` in `templates/wallet.html` (`.xdc-rpc-banner`, `display:none` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `.show`), buttons `Ð âÐ£ÒÐ âÐ£ÐÐ âÐÂ¶Ð âÐ¢Â° Fix XDC network` / `Not now`, status line `#xdcRpcRepairStatus`. CSS uses the theme vars (`--gold`, `--primary-button-text`, `--text-dim`, `--accent`) with the warm-amber warning palette from `.wallet-reminder-banner` (no new literal colours).
- **The banner is unreachable from inside the claim modal (2026-10 follow-up):** .modal-overlay is z-index 200, so the body banner sits behind the open claim sheet. A user who hits the XDC failure IN the modal therefore only ever saw the manual "edit your settings" copy - the one-tap fix was right there but covered. `_offerXdcRpcRepairInClaimModal()` (`static/js/wallet-main.js`) now renders the same one-tap repair as a card in `#ubiClaimStatus`, fired from `executeNetworkClaim`'s catch when `network === 'xdc'` and the error is `_isWalletRpcUnreachableError` OR `_isNetworkFeeUnavailableError` (both mean the wallet's own XDC node could not answer). **Append it AFTER `setStatus` / `_offerAlternateClaimNetwork`** - `setStatus` rewrites `status.innerHTML` and would wipe the card. The card re-resolves the provider via `getClaimProviderAndFrom()` (the catch block has no `provider`/`from` in scope) and stamps `XDC_RPC_BANNER_FIXED_KEY` on success so the body banner stops nagging. Skipped for `LOGIN_METHOD === 'local'` - the in-app wallet fans out over our own RPC list, so `wallet_addEthereumChain` is a no-op there. Tests: `tests/test_xdc_claim_modal_repair_content.py`.
- **Never** switch the user's chain unprompted, probe a local/WC session, or re-show within the throttle window. Locked in by `tests/test_xdc_rpc_repair_banner_content.py` (16 tests Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ functional node harness over the REAL function bodies with stubbed provider/localStorage/DOM: show/hide, chain gating, login-method gating, throttle, force, FIXED, repair/re-probe/cancel, plus markup/CSS/lifecycle wiring).

- **We cannot make a wallet adopt our RPC as its default (2026-10 follow-up):** EIP-3085 only says a re-add of an already-added chain "SHOULD be successful" - MetaMask then APPENDS the new `rpcUrls` and keeps the user's chosen **default** RPC. There is no `wallet_updateEthereumChain` / set-default method, and EIP-1193 forbids changing a network silently, so a dapp can never force the default. PR #487 fixed the *host* (the old `earpc` typo) and the *list order*, which only helps wallets that adopt the list fresh - users who added XDC months ago still point at the old host. The only cure is the user picking the healthy entry as the **Default RPC URL**, so all our copy now names that exact field instead of the vague "set the RPC URL" (which points at a field that is already filled). Guidance lives in `XDC_MANUAL_RPC_STEPS_TEXT` (`static/js/wallet-main.js`) and the two claim-error branches + pre-claim status line; `templates/xdc_wallet.html` setup instructions and the `#xdcRpcRepairBanner` copy (`templates/wallet.html`) carry the same steps. Locked in by `tests/test_xdc_rpc_endpoint_hosts_content.py` (`..._points_at_the_default_rpc_field`) and `tests/test_xdc_rpc_repair_banner_content.py`.


## "missing revert data" Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ root cause & fix (2026-08)
ethers.js v6 throws `"missing revert data in call exception"` whenever an `eth_call` / `eth_estimateGas` reverts and the returned JSON-RPC error object has **no `data` field** (the revert bytes). Public Celo RPC nodes and mobile wallet providers (Trust, MiniPay) are inconsistent Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ some return `error.data`, some don't. The bridge used to forward reverts as `new Error(data.error.message)`, **dropping `error.data` and `error.code`**, which forced ethers into the opaque "missing revert data" path.

Fix layers (all in this repo):
1. `static/js/tx-error.js` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `_decodeRevertData()` decodes `Error(string)` (0x08c379a0), `Panic(uint256)` (0x4e487b71), custom selectors. Exposes `GMTxError.{decodeRevertData, revertReasonFromError, simulateCallCelo}`. `_isReverted()` now matches `missing revert data`. **ABI offset gotcha**: string length word is at hex offset 72 (after selector[8] + offset word[64]), string bytes start at 136 Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ NOT 136/200.
2. `static/js/wc-bridge.js` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `_rpcErrorFromJsonRpcError` preserves `error.code` + `error.data`; `_celoJsonRpcWithFallback` retries the next RPC URL only when `data` is absent (a revert-with-data is deterministic, no point retrying).
3. `templates/swap.html` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ inline `_celoJsonRpc` mirrors the above; `_enrichSwapError(err, simParams, ctx)` re-runs the exact failing calldata as a read-only `eth_call` against several Celo RPCs to recover the revert reason, plus an ERC-20 allowance/balance diagnostic. Wired into `startReserveSwap` (GoodReserve sell/buy) and `startSwap` (Uniswap wallet-signer path).
4. `static/js/minipay-gas-topup.js` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ balance reads (`_ethCall`, `_getCeloBalance`) now fall back to public Celo RPCs when the wallet provider fails (fixes "gas request not working when low balance"); CELOÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶cUSD swap decodes revert bytes via `_decodeRevertData` + `_friendlyGasSwapError`.

## Reloadly refund gas-park (2026-08)
When a Reloadly fulfillment fails, the backend refunds G$ via the `REFUND_KEY` wallet. If that wallet has **no CELO gas**, the refund used to hard-fail with a scary "contact support" message. Now:
- `reloadly/service.py` `refund_gd()` does a preflight CELO balance check + matches `insufficient funds`/gas errors, returning `error_type: "insufficient_gas"`.
  - **Gas budget: fixed 250k (PR #169 estimate-based preflight REVERTED 2026-08):** the preflight demands `REFUND_GAS_LIMIT=250000 Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð gas_price` CELO and the tx is built with `gas: 250000`. The estimate-based preflight from PR #169 (live `estimate_gas`, fallback 80k, cap 150k) was reverted after it broke refunds in production (refund txs reverting / orders hard-failing after the merge). If this is ever re-attempted, the cap/fallback must be Ð â¢Ð¢ÑÐ âÐ²ÒÒ£Ð â¢Ð£Ò· the true gas cost of the deployed G$ token's transfer, and it must be verified against the production RPC.
  - **G$ balance preflight (2026-08 follow-up):** a refund wallet with CELO but **no G$** used to send a transfer that reverted on-chain Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ hard `refund_failed` forever even after admin refilled gas. `refund_gd` now checks `balanceOf(refund_wallet) < amount_wei` first and returns `error_type: "insufficient_balance"` with the exact G$ shortfall. Routes park it as `pending_refund` exactly like gas (auto-retry succeeds once the admin tops up G$); the friendly message reads "topped up by the admin" (covers gas AND balance); refund_retry releases both error types back to `pending_refund`.
  - **Receipt-timeout double-refund hazard (2026-08 follow-up):** `wait_for_transaction_receipt(timeout=60)` raising `TimeExhausted` AFTER `send_raw_transaction` used to hard-fail the order as `refund_failed` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ but the refund tx was already broadcast and could still confirm, so any manual re-send would DOUBLE-refund. Now `_wait_for_receipt_patient` polls for 60s tolerating RPC hiccups and returns None instead of raising; unconfirmed refunds return `error_type: "submitted_unconfirmed"` WITH `tx_hash`, routes park them as `pending_refund` keeping `refund_tx_hash`, and `run_refund_retry_once` checks the prior tx's on-chain receipt (`check_refund_tx_status`: confirmed Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ refunded, reverted Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ refund_failed, pending Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ stay parked) BEFORE any re-send. Hard failures now include the sanitized refund reason in the "contact support" message (URLs stripped) so support can see why; `refund_error` column keeps the full text.
- `reloadly/routes.py` `_process_refund_failure()` (used by both `api_confirm_order` and `api_detect_payment`) parks the order as **`pending_refund`** with a friendly "automatic refund within a few hours once the refund wallet is refilled with gas by the admin" message, instead of `refund_failed`.
- `reloadly/refund_retry.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ env-gated (`RELOADLY_REFUND_RETRY_ENABLED`) background thread (same pattern as `ubi_reminder.py`) that retries `refund_gd` for `pending_refund` orders every `RELOADLY_REFUND_RETRY_INTERVAL_SEC` (default 600s). Succeeds **automatically** once gas is refilled; gas-stalled orders stay `pending_refund`, non-gas failures escalate to `refund_failed`.
  - **Concurrency-safe** via `claim_order_for_refund()` (CAS): atomically flips `pending_refund` -> `refunding` (PostgREST `update(...).eq("id",X).eq("status","pending_refund")`); only the winner sends a refund. Prevents double-refund across gunicorn workers / scheduler-vs-manual endpoint. Gas-stall releases back to `pending_refund`; success -> `refunded`; other failure -> `refund_failed`.
- Wired in `main.py` right after the Reloadly Store init block.
- Frontend `templates/reloadly.html` handles `pending_refund` status (info toast, blue pill, friendly copy).
- Tests: `tests/test_reloadly_refund_gas_content.py` (content, no deps).

### Reloadly refund auto-retry upgrade (2026-10)
A user reported refunds not resuming automatically after the refund wallet was topped up. Root causes found (all fixed):
- **The scheduler defaulted OFF** (`RELOADLY_REFUND_RETRY_ENABLED` had to be explicitly set). A gas/balance top-up could therefore never auto-resume Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the very thing the feature exists for. It is now **ON by default** (`_env_flag(..., True)`); set `RELOADLY_REFUND_RETRY_ENABLED=0` to opt out. `is_refund_retry_enabled()` exposes the state.
- **`reloadly/service.py` never imported `get_supabase_admin_client`** but `claim_order_for_refund` called it Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `NameError` inside the try, caught, and returned `claimed: False` "database unavailable". **Every** refund retry silently skipped its CAS claim and did nothing. Fixed the import (line 10).
- **Stranded `refunding` rows were never retried.** A worker killed mid-refund (gunicorn recycle) left the row in `refunding` forever Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the retry fetch only looked at `pending_refund`. Now `_fetch_refundable_orders` uses an `.or_()` covering `pending_refund`, `refunding` older than `RELOADLY_REFUND_STALE_CLAIM_SEC` (default 300s), and `refund_failed` older than `RELOADLY_REFUND_RETRY_FAILED_AFTER_SEC` (default 900s; 0 disables).
- **`refund_failed` was admin-retry-only** (no scheduler pickup). It is now auto-retried after the delay gate.
- **`claim_order_for_refund(order_id, expected_status="pending_refund")`** generalized so the CAS matches the row we actually fetched (a `refunding`/`refund_failed` row must be claimed from its own status, else the retry would never win).
- **`get_refund_wallet_status()`** (service.py) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ read-only snapshot of the `REFUND_KEY` wallet: address, CELO balance + `celo_ok`, G$ balance + `gd_ok`, `gas_required_celo`, `rpc_ok`. Never raises; ok-flags are False on a failed read so an unreachable RPC can't look funded.
- **`retry_order_refund(order_id)`** (refund_retry.py) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ admin single-order retry that ignores the age/retry-after gates; accepts only `pending_refund`/`refunding`/`refund_failed`, reports `already_refunded` for `refunded`.
- **`wake_refund_retry()`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the scheduler loop waits on a `threading.Event`, so parking a refund (`_process_refund_failure`) and the admin "Process now" both nudge an immediate pass instead of waiting the full interval.
- **Admin endpoints** (`reloadly/routes.py`, admin-gated via `_require_admin`): `GET /reloadly/api/admin/refunds/status` (wallet health + per-status counts + the stuck orders, read through the **service-role** client so RLS can't hide rows), `POST /reloadly/api/admin/refunds/process` (run a pass now), `POST /reloadly/api/admin/refunds/<order_id>/retry` (single order).
- **Admin dashboard "Reloadly Refunds" section** (`templates/admin_dashboard.html`, sidebar + `loadSectionData` case + `loadReloadlyRefunds`/`processReloadlyRefunds`/`retryReloadlyRefund`): shows the wallet's CELO/G$ state with explicit top-up badges, scheduler on/off, queued count, and per-order Retry. All values HTML-escaped.
- **Schema:** `sql/reloadly_refunds.sql` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `ADD COLUMN IF NOT EXISTS refund_tx_hash/refund_error/failure_reason`, a partial index on the three refund statuses, and a documented status set. Env knobs documented in `.env.example`.
- **Rule:** a refund that parks for a funding shortfall must resume automatically; the manual admin trigger is a convenience, never the only path. Do not re-introduce a disabled-by-default scheduler, and keep the CAS `expected_status` in sync with the fetched row.
- Tests: `tests/test_reloadly_refund_autoretry_content.py` (21 tests Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ default-on/opt-out, wallet-status helper, three-status fetch, per-row CAS, single-order retry, wake wiring, admin endpoints/dashboard/migration, plus a functional drive of the real retry module with stubbed supabase/service).

### Reloadly admin Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ view successful refunds (2026-10)
The admin Refunds panel only ever queried the three **stuck** statuses, so successfully refunded orders (the scheduler writes `status='refunded'` + `refund_tx_hash` once the tx confirms) had no audit trail in the UI.
- **`reloadly/routes.py`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `GET /reloadly/api/admin/refunds/status` gained an optional `?status=` filter resolved by the pure helper `_resolve_refund_view(requested) -> (view_statuses, order_desc, normalized)`:
  - no/unknown value Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ the three stuck statuses, oldest-first (**unchanged default**);
  - `refunded` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `["refunded"]`, **newest-first**;
  - `all` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ all four statuses, newest-first;
  - a single stuck status Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ just that state, oldest-first.
  The stuck **counts** are now computed from a dedicated query over `_REFUND_STUCK_STATUSES` (independent of the view), so the "Queued refunds" summary never changes when viewing refunded rows. `refunded` is in `_REFUND_VIEW_STATUSES` but **NOT** in `_REFUND_STUCK_STATUSES`. Response adds `"filter"`.
- **`templates/admin_dashboard.html`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `#reloadlyRefundStatusFilter` dropdown (Awaiting refunds / Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐ Ò² Refunded (successful) / All) passes `?status=` to the endpoint; refunded rows render green (`#34d399`), show the **refunded date** (`updated_at`) and a **"Refund tx Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð â¢Ð¢Ð"** Celoscan link, and get **no Retry button** (nothing left to retry).
- **Do not** add `refunded` to `_REFUND_STUCK_STATUSES` (it would inflate the queued counter) and keep the counts query separate from the view query.
- Tests: `tests/test_reloadly_refund_autoretry_content.py` (+6 Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `_resolve_refund_view` default/refunded/all/stuck, counts independent of view, dashboard filter wiring).

## AI chat agent Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ transaction-hash lookup
`ai_agent/` is the chat-box agent (`/api/ai-agent/chat`). It classifies a message into a safe action preview (send/stream/swap/etc.) and never signs.
- **NEW: `lookup_transaction` action** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ read-only tx-hash lookup. When a user asks "where is my tx hash" / "my Learn & Earn tx" / "reloadly txid" etc., the agent queries the user's own rows across feature tables and replies with the hash + amount + status + Celoscan link. No signing, no fund movement.
- `ai_agent/tx_lookup.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `lookup_transactions(wallet, feature)` queries (per feature): `learnearn_log` (+ `learn_earn_streams`), `reloadly_orders`, `referral_rewards_log`, `twitter_task_log`, `trustpilot_task_log`. supabase is imported **lazily** inside `_query_one` so the module imports/tests without supabase installed.
- Wallet-form gotcha: Learn & Earn quiz logs store the wallet **masked** (`0xabcdÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£1234`) OR full lowercase (depends on writer version) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `_build_wallet_filter` matches both via `or_`. Other tables store lowercase.
- `_is_onchain_hash()` filters to real Celo tx hashes (0x + 64 hex) so Reloadly's numeric `reloadly_transaction_id` and `queued:...` stream placeholders don't get a bogus explorer link.
- Keyword detection is **rules-based** (`is_tx_lookup_request` + `detect_feature`) so it works even without `OPENAI_API_KEY`; the OpenAI classifier is also taught the action via `lookup_feature`.
- Tests: `tests/test_ai_agent_tx_lookup_content.py` (functional via importlib + text-based wiring, no deps).

## Reloadly Mobile Top-Up Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ airtime vs data/bundles are SEPARATE operators (2026-10)
"Find Operators" used to show ONE mixed grid. Reloadly returns every product kind for a country in a **single payload** and labels neither Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ and a data/bundle plan is actually a **separate operator with its own `operatorId`** (e.g. `TM` vs `TM Bundles`). So the user saw airtime and data cards side by side with nothing distinguishing them, and plan descriptions ("5GB Plan, Validity 30days") only appeared when an operator happened to expose them in the country list.
- **`reloadly/catalog.py`** (new, **stdlib-only** so it imports/tests with no deps): `classify_operator()` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `airtime|data|bundle|combo` from Reloadly's own `bundle`/`data`/`combo` flags (accepts `1`/`"true"`/`True`), with a conservative name fallback for older API revisions; `is_data_product()`; `operator_flag_products()` (additive tagging, does not mutate input); `normalize_operator_products()` (per-operator payload Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `{product_type, is_data_bundle, fixed:[{usd,local,description,gd}], suggested, has_fixed, has_range, currency}`). `fixedAmountsDescriptions` is keyed by float-string (`"83.20"`), and a local description (`localFixedAmountsDescriptions["5.00"]`) is the fallback when no USD one exists.
- **Classification is flag/name-derived ONLY Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ never a per-country table** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so it works for every country. `OPERATOR_FAMILIES` (the phone auto-detect map) is unrelated and untouched.
- **`reloadly/routes.py`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `api_topup_operators` tags each operator (`product_type`/`is_data_bundle`, additive) and returns `filters`; new auth-gated `GET /api/topup/operator-products/<int:operator_id>` wraps the previously **UNUSED** `client.get_operator_products()` and converts each fixed amount to `gd`.
- **`templates/reloadly.html`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ filter chips **All products / Airtime / Data & Bundles** with live counts (bar hidden when only one kind exists, so it is never a no-op), a per-card type badge, and a per-operator product fetch on select (`loadOperatorProducts`, cached per operator id) that renders data plans as described rows ("Select Data / Bundle Plan"). A failed/slow fetch **degrades to the summary amounts** instead of blocking; descriptions are HTML-escaped via `_escHtml`. Filter resets on country change.
- **Index-mapping trap (found while implementing):** the grid renders the **product-filtered** subset, so `selectOperator(i)` MUST index that same list (`window._operators = visible`). Indexing the unfiltered family list opens the WRONG operator when a chip is active. `window._familyOperators` keeps the unfiltered list for re-renders.
- **Empty-chip fallback:** if the active chip would render nothing (a brand family with no data plans), the render falls back to `all` rather than stranding the user on an empty grid.
- No SQL migration, no new env vars; the order flow (`send_topup(operator_id, amount)`) is unchanged.
- Tests: `tests/test_reloadly_operator_products_content.py` (40 tests, no deps) + `scripts/smoke_reloadly_products.cjs` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a **node harness over the REAL filter/render functions** with a PH-style payload (chip counts, badges, filtered-index mapping, empty-chip fallback). Wired into pytest (skips without node). Run with `python -m pytest tests/test_reloadly_operator_products_content.py`.

## Reloadly country picker Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ catalog loads without OAuth (2026-10)
The Store's country picker showed "Failed to load countries." because `ReloadlyClient.get_countries()` gated a **public** catalog behind OAuth. `GET https://topups{,-sandbox}.reloadly.com/countries` returns the full 159-country list with **no** `Authorization` header (verified live Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ 200 with empty headers and with a bogus bearer), but the client always called `_topup_headers()` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `_get_token()`, so when `RELOADLY_CLIENT_ID`/`RELOADLY_CLIENT_SECRET` were unset (or the credential expired/was rejected) the token POST to `auth.reloadly.com` returned 400, `get_countries()` raised, `/reloadly/api/countries` answered 500, and the picker stayed empty. Reproduced locally: HTTPError 400 for `auth.reloadly.com/oauth/token` while the anonymous fetch returned 200/159.
- **`reloadly/client.py`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `get_countries()` now tries the authenticated request first (so the configured environment is still honoured), and on any failure (or a non-list payload) falls back to an anonymous request via `_countries_public_headers()`. It raises only when BOTH paths fail, so a genuine outage still surfaces as an error.
- **`reloadly/routes.py`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `api_countries()` logs the failure and returns 502 when the payload is not a list, instead of a success body the frontend can't render.
- **`templates/reloadly.html`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the picker error state has a **Retry** button (`retryCountriesLoad()`), which clears `_countriesCache`/`_countriesLoadingPromise` and re-renders, so a transient backend error recovers without a full page reload.
- **Rule:** a public Reloadly catalog/read endpoint must never be hard-gated on token acquisition. Other `get_*` calls (operators, giftcards, utilities, orders) DO require auth and keep the existing `_topup_headers()`/`_giftcard_headers()`/`_utility_headers()` path.
- Tests: `tests/test_reloadly_countries_content.py` (8 tests Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ functional drive of the REAL client with a stubbed `requests` module: no-creds public fallback, rejected-creds fallback, authenticated preference when creds work, raise when both fail, non-list rejection, and an assertion the fallback request sends no `Authorization`; plus route/template wiring). Run with `python -m pytest tests/test_reloadly_countries_content.py`.

## AI chat agent Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ send/stream + keyword parsing + local wallet (2026-08)
Send/stream execution is frontend-side: after `/api/ai-agent/actions/<id>/confirm`, `static/js/ai-agent.js` `continueWalletFlow` calls `window.GoodMarketAI.handleConfirmedAction` (defined in wallet.html; other pages redirect to `/wallet?ai_action=<id>` where `openAiActionFromUrl` replays it). wallet.html routes to `doSend()` (send) / `handleStartStream()` (stream), so **all login methods Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ including `local` (GMLocalWallet PIN unlock via `_getSigningProvider`/`doSend` local gates) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ sign end-to-end**.
- **Local signing mode:** `confirm_action` now returns `signing_mode: "local"` for `login_method == "local"` (`walletconnect`/`wallet` otherwise); ai-agent.js shows a "Signing: In-app GoodMarket wallet (PIN unlock)" row on the review card.
- **Keyword-based parsing (any phrasing, English+Tagalog):** `_parse_with_rules` triggers send on send/transfer/padala/padalhan substrings; recipient prefixes are to/kay/ni/si/sa/para kay/para sa/@ (`_send_recipient_candidate`) with a `_RECIPIENT_STOP_WORDS` guard (araw/buwan/day/month/Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£) so time words never become usernames. Bare recipients with no prefix are NOT invented Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ they land in `missing_fields`. Stream daily rate accepts per day/daily/kada araw/bawat araw/araw-araw (`_stream_daily_amount`). The OpenAI prompt is told to classify Taglish by keywords; rules fallback needs no OPENAI_API_KEY.
- **Stream-period bug fixed:** wallet.html's AI stream handler must set `streamPeriod` to `/ day` before `handleStartStream()` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the modal defaults to `/ month`, which silently created a 30x smaller stream from the agent's per-day rate.
- **Wallet-page-only widget (2026-08):** the `{% include "_ai_agent.html" %}` widget lives ONLY in `templates/wallet.html` -- it was removed from homepage/swap/reloadly. reloadly.html keeps `openAiReloadlyActionFromUrl` (no widget needed) so mobile_load actions confirmed from the wallet chat still replay via `/reloadly/?ai_action=<id>`.
- **All-login-methods gate (2026-08, REVERTED local-only):** `parse_and_plan` used to hard-gate on `login_method == "local"` (`_LOCAL_ONLY_REPLY` / `not_eligible`); that gate was REMOVED so WalletConnect / injected / Privy / MiniPay sessions can also use the agent. The login method is recorded on each pending action and `confirm_action` maps it to `signing_mode` (`local` / `walletconnect` / `wallet`) so the frontend picks the right signer. The widget ships only on wallet.html, but the backend no longer rejects non-local sessions. Do NOT re-add a local-only gate without also auditing every execution path (send/stream/swap/gcash) for per-provider signer routing.
- **Quick command buttons (2026-08):** `_ai_agent.html` shows exactly THREE quick commands Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `Ð âÐ£ÒÐ âÐ£ÐÐ âÐ¢Â¶Ð âÐ²Òâ Cashout` (`data-gm-ai-command="cashout"` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ auto-starts the GCash cashout request flow), `Ð âÐ£ÒÐ âÐ£ÐÐ âÐ¢Â¶Ð ÐÐ²ÒâÐ²ÒÒ³ Send Money` (`data-gm-ai-command="Send Money"` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ auto-starts send_gd; bot asks for receiver + amount), and `Ð âÐ£ÒÐ âÐ£ÐÐ âÐÂ¶Ð âÐ£Ð Swap` (`data-gm-ai-command="Swap"` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ auto-starts swap; bot asks for amount + both tokens). All old FAQ buttons (What is GoodMarket?/GoodDollar?/How to Send Tokens/etc.) are removed. ai-agent.js keeps the `data-gm-ai-command` click handler; commands are plain messages run through the normal parse flow.
- **GCash cashout via chat (2026-08):** `gcash_cashout` action -- "cashout 5,000 G$" (any order, e.g. "Mag cashout ng 5,000 G$") -> bot asks "Please reply your GCash mobile # and full name registered on GCash" -> user replies "09651234567 Wilbert Lenteria" (name and 11-digit `09xxxxxxxxx` number in ANY order) -> review card -> confirm -> wallet.html `handleConfirmedAction` prefills `gcashAmount/gcashNumber/gcashName` and calls the SAME `submitGcashCashout()` as the modal (PIN unlock -> sign -> receipt wait -> POST /api/gcash/cashout-request), so the row lands in GCash history exactly like the manual flow.
  - Two-turn state: `_PENDING_GCASH` (in-memory per-wallet like `_ACTION_STORE`) stores partially-filled amount/number/name; a follow-up with no command keyword merges missing fields (freshly-typed values win). `cancel` aborts.
  - Parsing helpers: `_gcash_number_candidate` (`^09\d{9}$`), `_gcash_amount_candidate` (phone stripped FIRST so the 11-digit number is never read as the amount), `_gcash_name_candidate` (stopwords removed incl. "G$" via lookarounds -- `\b` after `$` never matches). Full name = >= 2 words. Minimum = `_GCASH_MIN_GD` (default 5000, matches the app-wide floor).
  - tx-lookup guard: `_supplement_intent_from_message` only flips unknown/help -> gcash_cashout on keywords, so "where is my gcash tx hash" stays `lookup_transaction`.
- **No send amount cap + CELO sends (2026-08):** the old `_MAX_SEND_GD` (default 100, env `AI_AGENT_MAX_SEND_GD`) hard cap on `send_gd` was REMOVED -- any positive amount is accepted (balance is enforced on-chain at signing). `_MAX_STREAM_GD_PER_DAY`/`_MAX_MOBILE_LOAD_FIAT` caps still exist. `_normalise_send_token`/`_send_token_candidate` now recognize `celo` -> `CELO` (alongside G$/cUSD/USDT); wallet.html `handleConfirmedAction` maps `{cusd:'CUSD', usdt:'USDT', celo:'CELO'}` onto the send-modal token chips (XDC/XDC G$ stay wallet-page-only).
- **Swap via chat (2026-08):** the `swap` action now executes in-page from the wallet chat (no /swap redirect). Backend `ai_agent/service.py` computes `swap_route`: G$ <-> cUSD -> `goodreserve` (+ `swap_direction` buy/sell; buy = cUSD->G$), everything else -> `uniswap`; `_swap_missing_reply` gives friendly missing-field/unsupported-pair replies. wallet-main.js ships a minimal in-page swap engine (`_aiExecuteSwap`, `_aiSwapReserve`, `_aiSwapUniswap`) that mirrors templates/swap.html's execution path (reserve broker `swapIn` w/ exact approval, UniversalRouter+Permit2 w/ best-of fee-tier + cUSD/USDT two-hop quotes, 0.01 CELO gas buffer) and reports results to the chat via `goodmarket:ai-tx-success/failed` events. `_aiSwapResolveSigner()` resolves the signer PER LOGIN METHOD (local Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ GMLocalWallet; Privy Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `_walletGetPrivyProviderIfPreferred`; WC Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `_walletGetWcProviderIfPreferred` when `_gmPreferWc()`; else injected via `_vAwaitEthProvider` + wrong-account check), with `_aiSwapEnsureCeloChain` switching injected/Privy providers to 0xa4ec Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ same routing as swap.html's `getConnectedSwapSigner()`. Supported tokens mirror the /swap page: GD, CELO, cUSD, USDT (XDC/USDC rejected as `supported_pair`).
- **GCash no-modal revision (2026-08):** the `gcash_cashout` branch in `handleConfirmedAction` no longer opens `gcashModal` -- same style as send_gd (prefill hidden inputs -> `submitGcashCashout()` -> PIN prompt is the review surface).
- **Chat progress + card lifecycle (2026-08):** every chat-driven tx flow (send/stream/gcash/swap) now posts interim Ð â¢Ð¢ÑÐ âÐ²ÒÒ°Ð ÐÐ²ÒÒÐ£Â® processing messages via a new `goodmarket:ai-tx-processing` event (`_gmAiProcessing(message, progressKey)` in wallet-main.js; handler `handleAiProcessing` in ai-agent.js tags each bubble `data-gm-ai-progress`). Result events carry `progressKey` + `actionId` so the widget dismisses the interim bubbles (`_dismissProgress`) and, on success, REPLACES the review card with a "Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐ Ò² Done Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ see the result below." marker (`_markCardSuccess`; failures keep the card). Action-id plumbing: ai-agent.js tracks rendered cards in `_actionCards` (by action.id) + `_activeActionId` fallback; wallet-main.js stamps the confirmed action onto a hidden `#gm-ai-current-action` beacon (`_gmAiSetCurrentAction` in `handleConfirmedAction`) and `_gmAiEventDetail()` merges `{actionId, actionType}` into every doSend/submitGcashCashout/handleStartStream/swap result dispatch Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so manual-flow functions that don't know about chat actions still update the right card.
- **Chat CeloÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶XDC bridge reads-via-wallet fix (2026-08):** `_aiBridgeCeloToXdc` used to bind the read-only contracts (`canBridge`, `allowance`) to `signer.provider`, i.e. through the wallet's injected provider. On MOBILE wallets (MetaMask dApp browser, Trust) every eth_call/eth_estimateGas goes through that wallet app and can show a stale/lagging node Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so a just-confirmed `approve` could still be reported as missing by the next `allowance()` read, and `bridgeTo`'s `safeTransferFrom` reverted with "Token approval is missing or too low" (desktop extensions were fast enough to never hit it). Fix: reads now go through a public Celo `JsonRpcProvider(AI_SWAP_CELO_RPC)`; only signing (`approve`, `bridgeTo`) stays on `signer`. This mirrors `_aiSwapReserve`/`_aiSwapUniswap` (which already read via `AI_SWAP_CELO_RPC`) and `_aiBridgeXdcToCelo` (`_aiXdcReadRpc`). **When adding a Celo-bound chat flow, read on a public RPC, sign on the wallet Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ never read allowance/canBridge through signer.provider.** Locked in by `test_celo_to_xdc_bridge_reads_via_public_rpc_not_wallet`.
- **Chat XDCÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶Celo bridge revert diagnostics + fee buffer (2026-08):** part of the same "Token approval is missing or too low" bug on mobile, but on the XDCÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶Celo side. Two problems: (1) `_aiXdcWaitReceipt` threw a generic `"Transaction reverted on XDC. Check your balance/allowance, then try againÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£"` on ANY revert Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ that string contains "allowance", so `GMTxError._isAllowanceIssue()` mapped EVERY XDC revert (bridge-fee mismatch, paused route, token error) to the misleading approval message. The wait-receipt now decodes the REAL reason via the backend `/api/xdc/tx-revert-reason/<hash>` helper (`_aiXdcRevertReason`, same selectorÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶friendly mapping as the manual /swap page) and the generic fallback no longer mentions allowance. (2) XDCÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶Celo `bridgeTo` is LayerZero fee-sensitive, and the chat sent the backend estimate with NO fee preflight (the manual page preflights via the wallet and bumps to 1.3Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð). `_aiBridgeXdcToCelo` now applies its own 1.3Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð fee buffer (excess is refunded by the bridge) capped against the wallet's real XDC balance via a 0.05 XDC gas reserve. Swaps (XSwap) unaffected Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ they aren't fee-sensitive, which is why they kept working. Locked in by `test_xdc_wait_receipt_decodes_real_revert_reason` + `test_chat_xdc_bridge_applies_fee_buffer`.
- Tests: `tests/test_ai_agent_send_stream_content.py` (functional importlib + content wiring, no deps), `tests/test_ai_agent_swap_content.py` for the swap routing + wiring.

## CeloÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐÂ¶XDC bridge Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ GoodDollar-side pause ("closed") handling (2026-09)
`bridge preflight failed: closed` is **not our bug** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the GoodDollar `MessagePassingBridge` was paused. The bridge (`0xa3247276DbCC76Dd7705273f766eB3E8a5ecF4a5`, identical on Celo/XDC) exposes an owner/guardian kill-switch `isClosed` (set by `pauseBridge`). `canBridge()` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `(false, 'closed')` and every outbound `bridgeTo` reverts `BRIDGE_LIMITS('closed')`. Observed live: Celo `isClosed=1` since block ~76618200 (2026-09-04, the Superfluid-incident pause), XDC still `0`. **Nothing on our side can un-pause it; the user must wait for GoodDollar.** The guard must be **state-driven and self-clearing** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ read `isClosed()` from the contract on every visit, never hardcode a flag, so bridging resumes with no redeploy.
- **CeloÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶XDC (`templates/swap.html`):** `isClosed()` added to `CELO_TO_XDC_BRIDGE_ABI`, read in `_loadCeloBridgeLimitsAndFees()` (soft-fails to `false` so a flaky RPC can't wedge a working route); `_applyCeloBridgePausedState()` disables the bridge + estimate buttons and amount input, shows `CELO_BRIDGE_PAUSED_REASON`, and **re-enables + clears the banner** when it reads `false` again. The `canBridge` reason `'closed'` maps to that dedicated copy instead of the generic "lower the amount". `_celoBridgePausedApplied` stops a mid-flow refresh from clobbering the in-flight button label.
- **XDCÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶Celo destination guard (THE IMPORTANT ONE):** limits/pause are enforced on the **minting (destination)** side Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `_bridgeFrom` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `_enforceLimits` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `canBridge` on CELO Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ while `_bridgeTo` on XDC is gated only by XDC's own `isClosed`. So an XDC-only source preflight passes during a Celo pause, XDC **burns the user's G$**, and the LZ message (`NonblockingLzApp`) is **stored** undeliverable until GoodDollar reopens and a retry succeeds. `_checkCeloDestinationBridge(toAddr, amountWei)` (swap.html + xdc_wallet.html) now reads Celo `isClosed`/`canBridge` **before any approve/burn**, fails **open** on RPC error, and maps `closed`/`minAmount`/`txLimit`/`dailyLimit`/`whitelist` to explicit copy. Same gate in the chat (`wallet-main.js` `_aiBridgeXdcToCelo` via `AI_CELO_DEST_CANBRIDGE_ABI`, read on a public Celo RPC Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ never the wallet provider).
- **`BRIDGE_LIMITS` (0xc5426f8d) is NOT always a pause:** it wraps `closed`, `minAmount`, `txLimit`, `dailyLimit`, `not whitelisted` and `source disabled`. The label/user_reason in `routes.py` + both templates' `selectorMessageMap` were rewritten from "Bridge route is paused" to "rejected under its current policy (may be paused, or over a limit)".
- **Test-harness gotcha:** `tests/test_local_wallet_signing_content.py` sliced `bridgeXdcToCelo()` with a hardcoded `[:6200]` window, so adding the destination guard above the signer call pushed `_xdcGetSigningProvider()` out of the window and produced a false failure. It now uses `_js_function_body()` (slices to the next top-level declaration) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ use that helper for new body assertions instead of fixed offsets.
- Tests: `tests/test_ai_agent_bridge_xdc_swap_content.py` (`test_celo_bridge_reads_is_closed_from_contract`, `test_celo_bridge_pause_disables_controls_and_self_clears`, `test_celo_bridge_closed_reason_has_dedicated_copy`, `test_xdc_to_celo_checks_celo_destination_before_burning`, `test_destination_guard_fails_open_on_rpc_hiccup`, `test_chat_bridge_handles_paused_route`, `test_bridge_limits_selector_label_is_not_always_paused`). No deps.


## Per-user Telegram notify on admin approve/reject (2026-08)
Admin approvals used to be silent Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ users only found out by re-opening the app. Now the linked Telegram bot user gets a DM on every admin decision:
- `telegram_notify.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `notify_user_by_wallet(wallet, text)` reverse-looks the chat id (`get_chat_id_by_wallet` on `telegram_wallet_sessions`) and `send_message`s; `notify_user_by_wallet_async` is the fire-and-forget daemon-thread variant. Both never raise; users who never linked Telegram get nothing.
- `gcash/routes.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `admin_approve` DMs "Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐ Ò² GCash Cashout Approved!" (amount, Ð â¢Ð¢ÑÐ âÐ²ÒÒ£Ð â¢Ð£Â®PHP, Ref #) after the row update; `admin_reject` DMs "Ð â¢Ð¢ÑÐ âÐ¢ÑÐ âÐÂ« GCash Cashout Rejected" + escaped reason + Celoscan refund link (only when the refund succeeded).
- `routes.py` `/api/admin/daily-tasks/approve` + `/reject` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ DM "Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐ Ò² Daily Task Approved!" (reward + tx link) / "Ð â¢Ð¢ÑÐ âÐ¢ÑÐ âÐÂ« Daily Task Rejected" (+ reason). The task-service results don't carry the wallet, so the route fetches `wallet_address`/`reward_amount` from `telegram_task_log`/`twitter_task_log` first.
- All notify calls are best-effort try/except (lazy `from telegram_notify import ...` inside the handler) so a Telegram outage can never break the admin action. Free-text admin notes/reasons are `html.escape`d (parse_mode=HTML).
- Tests: `tests/test_admin_approval_notify_content.py` (functional notify + wiring content, no deps).

## Telegram broadcast Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ durable delivery (2026-08)
Admin broadcasts (`/api/admin/broadcast-message`) push to Telegram bot users. The web dashboard inbox reads the broadcast row directly so it always works, but the Telegram push used to drop silently.

- **Root cause:** the push ran as a fire-and-forget daemon thread spawned inside the admin HTTP request (`telegram_notify.broadcast_message_async`). Under gunicorn (`max_requests=500` recycling + `graceful_timeout`), that thread was killed mid-broadcast, so many users never received the message and the admin endpoint returned `success:true` regardless Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ no signal that delivery failed.
- **Fix Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ durable per-recipient queue + scheduler** (same pattern as `ubi_reminder.py` / `reloadly/refund_retry.py`):
  1. `sql/telegram_broadcast_deliveries.sql` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ adds `telegram_broadcast_deliveries` (one row per recipient: `status` pending|sending|sent|failed|blocked, `attempts`, `last_error`, `delivered_at`) + aggregate columns on `admin_broadcast_messages` (`tg_status`, `tg_total`, `tg_sent`, `tg_failed`, `tg_queued_at`, `tg_delivered_at`). Run this migration in Supabase before enabling the scheduler.
  2. `telegram_notify.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `queue_broadcast_deliveries(broadcast_id, ...)` upserts one row per chat_id (`ON CONFLICT broadcast_id,telegram_chat_id` = idempotent re-queue); `deliver_broadcast_once(broadcast_id)` drains a batch with a **CAS claim** (`update(...).eq('status','pending')` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ only rows we won the flip are sent) so two workers/scheduler runs can't double-send. `classify_send_error` splits Telegram failures into `blocked` (403/chat-not-found Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ permanent, never retry), `rate_limited` (429), `retryable` (5xx/network); transient failures stay `pending` and retry until `_MAX_RETRY_ATTEMPTS` (default 5) then escalate to `failed`. `send_message` signature unchanged so `ubi_reminder` is unaffected.
     - **Silent-failure guards (2026-08 follow-up):** `broadcast_message_async` only takes the durable path when `broadcast_delivery.is_delivery_enabled()` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ with the scheduler off, queueing rows would deliver to nobody, so it uses the legacy best-effort direct send instead. `queue_broadcast_deliveries` **raises** on hard failures (DB down, token missing, upsert/stamp failed = migration not applied) instead of returning a success-looking summary, so the caller falls back to legacy. `_fetch_all_chat_ids(strict=True)` raises on query failure so an unreadable table can't masquerade as "no Telegram users".
     - **Stale-claim reclaim:** a worker killed mid-batch (the very gunicorn recycle this fix targets) leaves rows in `sending`; `deliver_broadcast_once` first runs `_reclaim_stale_sending` (flips `sending` rows with `updated_at` older than `_STALE_CLAIM_SECONDS`, default 300, back to `pending`) so they're redelivered instead of sticking the broadcast at `partially_sent` forever. The CAS claim now stamps `updated_at` so staleness is measurable.
  3. `broadcast_delivery.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ env-gated scheduler (`TELEGRAM_BROADCAST_DELIVERY_ENABLED`, default off). Polls every `TELEGRAM_BROADCAST_DELIVERY_INTERVAL_SEC` (default 30) for broadcasts with `tg_status IN (pending, partially_sent)` and drains a batch each; a `wake_broadcast_delivery()` event triggers near-immediate delivery when a fresh broadcast is queued. If the tg_* schema is missing, `_fetch_due_broadcasts` logs migration instructions once and latches `_schema_missing` instead of erroring every interval.
  4. `routes.py` `send_broadcast_message` passes the freshly-inserted `broadcast_id` to `broadcast_message_async`; `get_broadcast_messages` returns the `tg_*` columns via `select('*')`.
  5. `main.py` starts the scheduler right after the UBI reminder block.
  6. `templates/admin_dashboard.html` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ new "Telegram Delivery" column (`formatTelegramDelivery`) shows `Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐ Ò² Delivered (sent/total)` / `Ð â¢Ð¢ÑÐ âÐ²ÒÒ°Ð ÐÐ²ÒÒÐ£Â® SendingÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£` / `Ð â¢Ð¢ÑÐ âÐ²ÒÒ°Ð ÐÐ²ÒÒÐ£Â® Queued` / `Ð â¢Ð¢ÑÐ âÐ¢ÐÐ âÐ¢âÐ ÐÐ¢âÐ¢Â·Ð âÐ²ÒâÐ âÐ²ÒÒ° Finished Ð ÐÐ²ÒÒÐÂ¬Ð âÐ¢Ñ N failed`; auto-refreshes the table every 15s while any broadcast is still delivering.
- **To enable:** run the SQL migration, then set `TELEGRAM_BROADCAST_DELIVERY_ENABLED=1` (and `TELEGRAM_BOT_TOKEN`). Without either, the admin broadcast degrades to the legacy best-effort direct send Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Telegram users still get the message, just without durability/per-recipient tracking.
- **Diagnostics (2026-08 follow-up):** `GET /api/admin/telegram-diagnostics` (admin-only) runs `telegram_notify.get_broadcast_diagnostics()` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ never-raises health check of every link (bot token, DB, service-role key, recipient count from `telegram_wallet_sessions`, deliveries-table + tg_* column probes, scheduler enabled) with human `hints`. The admin dashboard "Ð âÐ£ÒÐ âÐ£ÐÐ âÐÂ®Ð ÐÐ²ÒÑÐ²ÒÒ³ Check Telegram Delivery Health" button renders it. `send_broadcast_message` response now includes `telegram_recipients` + `telegram_delivery_mode` (`durable_queued`|`legacy_best_effort`) + `telegram_warning` when 0 recipients Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the #1 silent failure is "no Telegram bot users" (users must /start the bot AND save a wallet; only then do they appear in `telegram_wallet_sessions`). `count_broadcast_recipients()` returns -1 for "couldn't read" vs 0 for "no users".
- Tests: `tests/test_broadcast_delivery_content.py` (error classification, idempotent queue, CAS claim, aggregate status, retry cap, scheduler wiring, routes wiring, dashboard rendering Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ no deps, stubs `requests`/`supabase`). Run with `python -m unittest tests.test_broadcast_delivery_content`.

## GCash Cashout (2026-08)
Users cash out G$ Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ PHP via GCash. 100 G$ = Ð â¢Ð¢ÑÐ âÐÂ©Ð ÐÐ²ÒâÐ²ÒÒ·1.00, minimum 5,000 G$ (Ð â¢Ð¢ÑÐ âÐÂ©Ð ÐÐ²ÒâÐ²ÒÒ·50), Philippines-only.
- **Flow:** `templates/wallet.html` `gcashModal` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ user fills amount/number/name Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ signs G$ transfer to `GCASH_ADDRESS` (uses the standard signer routing Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ local/injected/WC) Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `POST /api/gcash/cashout-request` verifies the tx **on-chain** (Transfer event: from=user, to=GCASH_ADDRESS, exact amount) then stores a `pending` row. One pending request per user.
- **Backend:** `gcash/` package (routes.py + service.py + refund_retry.py), blueprint `url_prefix=/api/gcash`. Admin endpoints: `GET /admin/requests`, `POST /admin/requests/<id>/approve` (manual GCash send), `POST .../reject` (auto-refund via `GCASH_KEY`).
- **Auto-refund:** `gcash/refund_retry.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ env-gated (`GCASH_AUTO_REFUND_ENABLED`, interval `GCASH_AUTO_REFUND_INTERVAL_SEC` default 300s). Requests still `pending` after 24h are CAS-claimed (`pending`Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶`refunding`) and refunded. Gas preflight uses the **fixed `_REFUND_GAS_LIMIT = 250_000`** budget (like reloadly's reverted fix), NOT the old estimate-based (fallback 80k / cap 150k) pattern that made admin rejects look successful while the refund tx ran out of gas. The scheduler also re-claims stranded `refunding` rows (worker died mid-refund) after the retry cutoff.
- **Reject-refund robustness (2026-08 follow-up):** "reject succeeds but refund fails" had three causes, all fixed in `gcash/service.py send_refund`: (1) estimate-based gas preflight replaced with fixed 250k (reloadly PR #169 lesson Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ it was reverted there for breaking refunds); (2) added G$ `balanceOf` preflight Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `error_type: insufficient_balance` with the exact shortfall (previously the transfer reverted on-chain); (3) the whole body is wrapped in try/except so it **never raises** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ callers CAS-claim the row first (`pending`Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶`refunding`), and a raise used to strand it in `refunding` with no retry path (admin retry only accepts `refund_failed`; scheduler only read `pending`/`refund_failed`). `_is_insufficient_gas_error` now matches the full reloadly pattern list.
- **Refund-success visibility (2026-08 follow-up):** successful refunds must link Celoscan everywhere Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `admin_reject` response includes full `tx_hash`; the dashboard `rejected`/`refunded` rows render "Refund tx Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð â¢Ð¢Ð" (previously only `refund_failed` linked the hash); reject/retry alerts append the Celoscan URL; wallet.html history shows `refunding`/`refund_failed` progress rows with the link.
- **Env vars:** `GCASH_ADDRESS` (receives user G$), `GCASH_KEY` (signs refunds Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ needs CELO gas), `GCASH_AUTO_REFUND_ENABLED`.
- **DB:** `sql/gcash_cashout.sql` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `gcash_cashout_requests` (statuses: pending/refunding/approved/rejected/refunded/refund_failed; `tx_hash` UNIQUE prevents reuse). Run in Supabase before enabling.
- **Admin UI:** `templates/admin_dashboard.html` "GCash Cashout" section Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ filter by status, approve/reject buttons, elapsed-time badge for pending.
- **Approve = proof required:** approving opens `gcashApproveModal` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ admin must enter the GCash **reference #** and upload a **receipt screenshot** (uploaded to ImgBB via `object_storage_client.upload_to_imgbb`, same helper as developer profiles/p2p). `admin_approve` takes multipart FormData (`reference_number`, `receipt_image`, optional `note`; JSON fallback), stores `reference_number` + `receipt_image_url` on the row, default note "Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐ Ò² Successful Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ GCash payment sent."
- **User history:** inside the cashout modal (`/api/gcash/my-requests`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ approved rows show "Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐ Ò² SUCCESSFUL Ð ÐÐ²ÒÒÐÂ¬Ð âÐ¢Ñ GCash sent! Ref #: Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£ Ð ÐÐ²ÒÒÐÂ¬Ð âÐ¢Ñ View receipt Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð â¢Ð¢Ð"; rejected/refunded rows link the refund tx on Celoscan.
- **On-chain confirmation race (2026-08 follow-up):** the backend used to look up the tx receipt exactly once, right after broadcast Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ "Transaction not found on-chain" even though the G$ had left the user's wallet. Now the frontend `_gcashWaitForReceipt(provider, txHash, 36)` (~90s, same poll pattern as `minipay-gas-topup.js`) waits for the receipt before POSTing, and `verify_payment_tx` polls again (`GCASH_RECEIPT_LOOKUP_ATTEMPTS` Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð `GCASH_RECEIPT_LOOKUP_INTERVAL_SEC`, default 8Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð2.5s) as the safety net. Timeout tells the user to NOT resubmit (would double-send) and to contact support with the tx hash; a reverted receipt aborts cleanly.
- **Locked local-wallet dead button (2026-08 follow-up):** `_lwUnlockIfNeeded`/`_lwIsNeeded` were declared inside the claim IIFE in wallet.html Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ callers outside it (`submitGcashCashout`, `doSend`, raffle signer) got undefined Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ PIN prompt never opened and the button looked dead. Claim worked because it lives inside the same IIFE. Fixed by exposing both on window after their declarations; `submitGcashCashout`'s unlock catch now surfaces non-cancel errors instead of silently returning. **Any new page/modal calling the unlock helpers must rely on the window-exposed globals.**
- **"Could not decode transfer event from transaction." Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ root cause & fix (2026-08 follow-up):** `gcash/service.py`'s `ERC20_ABI` had only the `transfer`/`balanceOf` *functions* Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ no Transfer **event** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so `token.events.Transfer()` raised on EVERY legitimate cashout and the blanket `except` returned "Could not decode transfer eventÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£" even though the user's G$ had reached `GCASH_ADDRESS` (funds stuck, no record). Fixed:
  - `verify_payment_tx` now decodes Transfer logs **manually by topic** (`TRANSFER_TOPIC`, `decode_gd_transfers` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ same ABI-free pattern as reloadly/learn_and_earn) and returns a **3-tuple** `(ok, error, transfer)`; `transfer` (actual received amount) is non-None whenever G$ reached the cashout address, even on amount mismatch.
  - **Auto-refund safety net:** when verification fails but funds arrived, `submit_cashout` calls `auto_refund_failed_cashout()` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ records the row (actual received amount), CAS-claims, and refunds the exact received amount immediately. Duplicate tx_hash Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ 409 (idempotent, no double-refund). Refund failure parks the row `refund_failed` with a friendly "safe Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ will be refunded" message.
  - **Refund robustness (reloadly lessons applied):** `send_refund` uses `_wait_for_receipt_patient` (returns None instead of raising on receipt timeout) Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `submitted_unconfirmed` WITH `tx_hash`; `process_claimed_refund()` checks any prior broadcast refund tx on-chain (`check_refund_tx_status`: confirmedÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶refunded, pendingÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶stay parked, revertedÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶re-send) BEFORE re-sending Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ no double-refund.
  - **refund_failed is retryable:** the auto-refund scheduler `_fetch_refundable` also picks `refund_failed` rows older than `GCASH_REFUND_FAILED_RETRY_AFTER_SEC` (default 3600s), and admins get `POST /api/gcash/admin/requests/<id>/retry-refund` + a "Ð âÐ£ÒÐ âÐ£ÐÐ âÐÂ¶Ð âÐ¡Ò³ Retry Refund" button on refund_failed rows in the dashboard.
  - **SQL migration MUST be re-run:** `sql/gcash_cashout.sql` relaxes `amount_gd` CHECK from `>= 5000` to `> 0` (auto-refund rows record the actual received amount, which can be below the minimum; the 5,000 min is still app-enforced for new cashouts).
- Tests: `tests/test_gcash_cashout_content.py`.

## Human (face) verification gate for feature pages (2026-08)
All login_methods (local / injected / walletconnect / privy / minipay) must be **face-verified on the GoodDollar Identity contract** before entering earning/spending feature pages. Unverified users are bounced to `/wallet?fv_required=1`.
- **Shared helper:** `human_verification.py` (root) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `human_verification_redirect(wallet)` returns a redirect to `FV_REQUIRED_URL` (`/wallet?fv_required=1`) when `blockchain.is_identity_verified(wallet)` is not verified, else None. It **fails closed** (an FV check error blocks access) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ deliberate safety choice. The on-chain check is cached (`IDENTITY_CACHE_TTL = 1800`, i.e. **30 minutes** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ not 5), so a freshly-verified user gets in on the next page load without re-login; after a **de-verify** the cache must be invalidated explicitly (`invalidate_identity_cache`, see below).
- **Gated pages:** `/dashboard` (routes.py + the duplicate main.py rule Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ both gated defensively), `/learn-earn`, `/reloadly/` (reloadly/routes.py), `/minigames/` + `/jumble/` + `/price-prediction/` (Play & Earn umbrella). The gate runs AFTER the session-auth check in each route. The wallet page itself is NOT gated Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ it hosts the FV entry point. **`/swap` and `/savings/` are intentionally EXEMPT** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ unverified users are allowed to swap and save G$ (2026-09 policy: remove the FV gate from those two routes in `routes.py` (`swap_page`) and `savings/routes.py` (`savings_home`); keep the NOTE comments there so a future re-gate is a one-liner).
- **Frontend bounce UX:** `static/js/wallet-main.js` `openFvRequiredFromUrl()` (DOMContentLoaded) reads `fv_required=1`, shows `#walletDashboardFVMsg`, sets `window._walletNeedsFV = true`, calls `window._triggerReVerify()` (flips the Claim button into purple "Re-verify Face ID" mode), scrolls to it, and strips the param via `history.replaceState`.
- **When adding a new earning/spending page, wire the same gate** (import inside the route function like the others).
- Tests: `tests/test_human_verification_gate_content.py` (functional via importlib with stubbed flask/blockchain + wiring content, no deps). Run with `python -m pytest tests/test_human_verification_gate_content.py`.



## Face Verification Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ "De-verify my account" (2026-09)
A verified wallet can remove its own face-verification status from **Settings Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ Face Verification Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ "De-verify my account"**. The browser signs `renounceWhitelisted()` on the GoodDollar Identity contract (Celo); `POST /api/fv/renounce` only **confirms** the on-chain result and drops the caches. No server key is involved.
- **Why the confirmation endpoint exists:** `is_identity_verified` caches for **30 min** and there was no invalidator. Without one, a renounced wallet would keep reading as verified, so the claim gate, the voucher gate and the Settings card would all stay green for up to half an hour. `blockchain.py` gained `invalidate_identity_cache(wallet)` (mirrors `invalidate_fv_expiry_cache`); the route calls **both** plus `invalidate_entitlement_cache`.
- **`/api/fv/renounce` verification is state-based, not purely event-based:** tx succeeded Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `from` == session wallet Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `to` == `GOODDOLLAR_CONTRACTS["IDENTITY"]` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ then `is_identity_verified(wallet)` (after invalidation) must be **False**. The `WhitelistedRemoved(address)` topic (`0x270d9b30Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£`) is checked as corroboration and surfaced as `event_seen`, but an event-shape change on a future Identity revision must not be able to mark a still-verified wallet as de-verified. It polls for a pending receipt (6Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð2s) because the wallet has just broadcast.
- **Frontend** (`static/js/wallet-main.js`): `openFvDeVerifyModal` / `confirmFvDeVerify`, with `_fvResolveSigner` mirroring `getRaffleSignerProvider` (local Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ PIN-unlocked `GMLocalWallet` **switched back to Celo 0xa4ec first**, WalletConnect Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ WC session, else injected bound to the session wallet; a mismatched account throws). A `paused()` preflight refuses before signing (the call reverts on a paused contract); a failed paused *read* must not block the user. On success it sets `_walletNeedsFV`, calls `_triggerReVerify()`, `fetchEntitlement(true)` and `loadFvStatus(true)`, and leaves Settings open showing `#fvDeVerifyNote`.
- **Modal stacking:** `#fvDeVerifyModal` is DOM-ordered **after** `settingsModal` (overlays share z-index 200 and stack by DOM order) and `openFvDeVerifyModal` **bypasses `openModal()`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ that helper closes every other overlay and would dismiss Settings, which the user must return to on Cancel. Same deliberate pattern as `lwUnlockModal`.
- **Button gating:** `renderFvStatus` shows the button only for `state === 'verified' || 'warning'`. `renewal` means the whitelist is already gone (a no-op tx), and `expired`/`unverified`/`error` have nothing to renounce.
- **Copy rule Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ do NOT promise data deletion.** `renounceWhitelisted()` removes ONLY the on-chain whitelist flag. The facial record lives with GoodDollar's FaceTec enrollment service and is removed **by expiry only** (verified 2026-09: `authenticationPeriod` is 180 days; `deleteFaceId` is commented out in GoodServer and `POST /verify/face/:id` 404s). The old intro-modal line "the record can be deleted later if you choose" was **false** and was replaced with the truthful auto-delete-after-180-days wording. The de-verify modal states the record is kept, the daily income stops, and re-verifying with the same wallet remains possible.
- **The 1-account rule is NOT bypassable this way** (verified in GoodServer): it is enforced by a global FaceTec `faceSearch` duplicate check (`ZoomProvider` throws `duplicateFoundMessage` for a different match at/above threshold), not by the on-chain flag. Re-verifying the **same wallet** works (same key Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ same enrollment identifier Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ own record excluded); enrolling a **new wallet with the same face fails** until the record expires (~181 days). So de-verify is safe but is not an "start over on a fresh account" button Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ never imply it is.
- Tests: `tests/test_fv_deverify_content.py` (38 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ cache-invalidator functional drive, route wiring/verification order, per-login-method signer routing, Celo switch-back, paused preflight + fail-open read, receipt-before-confirm ordering, button gating, modal stacking/`openModal` bypass, honest-copy assertions, and a Jinja render). Run with `python -m pytest tests/test_fv_deverify_content.py`.
- **Test-harness gotcha:** `_install_stubs()` patches `sys.modules` **per attribute**, not only-if-absent. Other content tests install partial `requests`/`web3` stubs and leave them behind; a wholesale "if not in sys.modules" guard let a stub lacking `exceptions`/`adapters` through and broke the `blockchain` import when the full suite ran. Use the `_ensure(module, attr, value)` helper for any new stub.
- **CSS literal-color gotcha:** the intro-modal section is scanned by `test_fv_intro_onboarding_content.py::test_intro_css_uses_theme_vars_not_literal_colors`, which fails on ANY `#rrggbb` in that region. The de-verify CSS therefore lives in its own `Ð â¢Ð¢ÑÐ âÐÂ¶Ð âÐ¢Â¶Ð â¢Ð¢ÑÐ âÐÂ¶Ð âÐ¢Â¶ Face Verification de-verify (Settings) Ð â¢Ð¢ÑÐ âÐÂ¶Ð âÐ¢Â¶Ð â¢Ð¢ÑÐ âÐÂ¶Ð âÐ¢Â¶` section AFTER the reduced-motion block, not inside the intro section.

## Username setup prompt after local login (2026-08)
After ANY successful local login (create OR unlock), users with no username land on a username-setup step (`#lwUsernameStep` in `templates/homepage.html`) BEFORE redirecting to the post-login landing (`config.POST_LOGIN_PATH`, default `/community`). The gate is **data-driven, not account-age-driven**: `_lwCompleteLogin(data)` (async) fetches `GET /api/user/username` and shows the step only when `username` is null Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so older accounts created before this flow get prompted on their next login too. A failed lookup must NOT block login (the fetch is wrapped in try/catch and falls through to the normal redirect). The step posts to the existing `POST /api/user/username` (`@auth_required` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ session already exists post-login), validates `^[A-Za-z0-9_]{3,24}$` client-side (same rule as the backend), caches `sessionStorage['username']`, then redirects. A "Skip for now" link (`skipLocalWalletUsername`) redirects without saving Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ skippers get prompted again next login. The wallet page picks the username up automatically via `loadPortfolioCardholder()` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `GET /api/user/username`. `showLocalWalletPanel`/`_lwRenderPhraseStep` must hide `lwUsernameStep` when resetting steps. Tests: `tests/test_local_wallet_username_setup_content.py` (no deps).

## Local-wallet optional WebAuthn (passkey) quick-unlock (2026-10)
The in-app wallet re-prompts for the 8-digit PIN on every page load and after the 15-minute auto-lock. An **opt-in** passkey fast path now lets a supported device approve with its own user verification Ð²Òâ fingerprint, Face ID, **or the device PIN/pattern/passcode** (WebAuthn exposes the same platform authenticator) Ð²Òâ instead of typing the wallet PIN.
- **`static/js/local-wallet-passkey.js`** (new, `window.GMLocalWalletPasskey`). Enrollment creates a WebAuthn credential with the **`prf` extension**; the authenticator returns a 32-byte PRF output after user verification. A wrap key is derived via **HKDF-SHA256**, and the **keystore JSON + the PIN are AES-GCM-256 wrapped** into `localStorage['gm_local_passkey_v1']`. **The raw PIN and the raw private key are NEVER stored** Ð²Òâ without a successful ceremony the blob is opaque. Unlock runs `navigator.credentials.get()` Ð²Ò¶â PRF Ð²Ò¶â HKDF Ð²Ò¶â AES-GCM unwrap Ð²Ò¶â `unlockWithKeystore(keystore, pin)`.
- **Hard fallback everywhere.** `isSupported()` (secure context + `PublicKeyCredential` + `isUserVerifyingPlatformAuthenticatorAvailable()`), `isAvailable()` (supported AND a credential is stored), and every failure (unsupported, cancelled, `no_prf`, `address_mismatch`) return a value the caller turns into the normal PIN prompt Ð²Òâ a passkey can NEVER strand a user or lock them out. The PIN remains a first-class path (and the only path on devices with no screen lock, or in wallet dApp browsers Ð²Òâ embedded WebViews are usually WebAuthn-less).
- **Stale-credential guard:** `unlock()` refuses with `address_mismatch` when the enrolled address differs from the current session wallet (same rule the PIN path enforces before decrypting).
- **Wiring:** `static/js/local-wallet.js` `_lwOpenUnlockModal()` injects a `#lwModalPasskey` button (hidden until `_refreshPasskeyButton`), wired to `unlockViaPasskey()`. `static/js/wallet-main.js` `#lwUnlockModal` gets `#lwUnlockPasskeyBtn` + `_lwUnlockViaPasskey()`; Settings gets a local-only `#passkeySettingsSection` (`_refreshPasskeySettings`), and the enroll sheet is **built in JS** (`_ensurePasskeyEnrollModal`) so **wallet.html stays under its 200 KB no-cache budget** Ð²Òâ never move that sheet back into the template. `_passkeyEnrollSubmit()` re-verifies the PIN (`handleUnlock`) BEFORE wrapping it, so a wrong PIN is never sealed.
- **Option A Ð²Òâ the device prompt is offered AUTOMATICALLY on open (2026-10).** Enrolled passkey Ð²Ò¶â opening the unlock sheet (claim/send/swap, anything that calls `_lwUnlockIfNeeded()` or `_lwOpenUnlockModal()`) runs the device ceremony with **no extra tap**; the sheet's subtitle switches to "Confirm with your fingerprint, Face ID, or device PIN Ð²Òâ or enter your wallet PIN below." Both `_refreshLwPasskeyBtn()` (wallet-main) and `_refreshPasskeyButton()` (local-wallet) **return the availability boolean** so the caller can decide Ð²Òâ do not revert them to `return undefined`. The auto-attempt passes `{silentCancel:true}` (`pkAttempt(true)`), so a **cancelled ceremony is silent** and simply focuses the PIN field Ð²Òâ a cancel means "I'll type my PIN", not an error. **Only the auto-attempt is silent; a manual tap on `#lwUnlockPasskeyBtn` still shows the real reason.** A gesture-less auto-attempt can be rejected by some browsers (PWA/WebView), which is safe: the passkey button stays shown as a one-tap retry.
- **Never make the passkey mandatory.** The PIN field and `#lwUnlockBtn` / `#lwModalSubmit` stay present and focusable in every path; with no enrollment (or no screen lock) the flow is byte-for-byte the old PIN-first one.
- **Failure copy is reason-specific.** `local-wallet-passkey.js` exports `describeReason(reason)` and every `{ok:false, reason}` return now carries a `message`, so a device **without PRF support** (`no_prf`) says so instead of silently looking like a no-op. Add a `REASON_MESSAGES` entry for any new reason.
- **Template contract:** every page that loads `local-wallet.js` also loads `local-wallet-passkey.js` **immediately after it** (`v=ASSET_VERSION`). Add the tag to any new page that can unlock the in-app wallet.
- **Do NOT** store the PIN or the decrypted key, drop the PIN fallback, or make a passkey mandatory. Device-bound by design Ð²Òâ the 12 recovery words remain the only recovery path. Tests: `tests/test_local_wallet_passkey_content.py` (node harness driving the REAL module + a second harness over the real `_lwUnlockIfNeeded` auto-offer for enrolled / no-enrollment / cancelled, plus content wiring). Proposal: `docs/WEBAUTHN_PASSKEY_UNLOCK_PROPOSAL.md`.
- **Enrollment is offered IN the signup/login flow (2026-10).** The Settings-only gate meant most users never enabled it. Two new-user paths now offer it where it belongs, and **both reuse the PIN already in memory** so the user never types it twice:
  - `static/js/get-started.js` Ð²Òâ `_lwCompleteLogin(data, pin)` takes the PIN (`finishLocalWalletSignup` passes `_lwPendingSignup.pin`, `_lwUnlockAccount` passes its own `pin`), stores `_lwOfferPasskey = {pin, keystore, address, redirectTo}` **in memory only**, then `_lwCanOfferPasskey()` Ð²Ò¶â `_lwShowPasskeyStep()`. `submitLocalWalletUsername` and `skipLocalWalletUsername` both fall into the offer before redirecting. `submitLocalWalletPasskey` / `skipLocalWalletPasskey` are exposed on **`window`** (the inline-handler rule Ð²Òâ an unexposed handler is a dead button).
  - `templates/_get_started_modal.html` Ð²Òâ `#lwPasskeyStep` sits **after** `#lwUsernameStep` on purpose: an interrupted device prompt must not cost the user their username.
- **WebAuthn requires TRANSIENT USER ACTIVATION Ð²Òâ this is why the offer is a BUTTON, not an automatic call.** `navigator.credentials.create()`/`.get()` must run inside a real user-gesture task; the browser otherwise logs *"User gesture is not detected"* and refuses. The PR #438 auto-offer runs the ceremony after an `await`, so the activation window has usually expired and the browser silently refuses Ð²Òâ which is why users still reported "same parin". **Never move the enrollment ceremony out of a user-tap handler.**
- **Offer rules (do not weaken):** show only when `isSupported()` reports a platform authenticator (embedded WebViews / no screen-lock devices skip the step entirely); **always skippable**; a decline writes `gm_passkey_offer_declined_v1` so we ask once and never nag; skip when `hasStoredCredential()` already; a **failed enroll never blocks entry** Ð²Òâ it shows the reason and continues the redirect.
- **Flow C Ð²Òâ already-logged-in users** (`static/js/wallet-main.js`): `showPasskeyNudgeOnce()` (2 s delayed) reveals a **JS-built** `#passkeyNudgeBanner` (NOT in the template Ð²Òâ wallet.html is at its 200 KB budget). Gated on `login_method === 'local'` + support + not enrolled + not declined, and stamps `gmPasskeyNudgeSeen_v1` **at reveal** (one-shot promise). Its Enable button opens the existing `openPasskeyEnrollModal()` (that path still re-prompts the PIN Ð²Òâ the PIN is not in memory for an already-unlocked session). `dismissPasskeyNudge()` also writes the shared decline flag.
- **The passkey step is a fast path, never a gate.** Do not make it block the redirect, and do not persist the PIN/keystore into `_lwOfferPasskey` (memory only; only the decline/nudge FLAGS touch storage). Proposal: `docs/PASSKEY_SIGNUP_LOGIN_ENROLLMENT_PROPOSAL.md`.

## Local-wallet face-verification status tracking (2026-10)
Admins could not answer "how many local-wallet users are face-verified?" Ð ÐÐ¢âÐ²ÒÒ `local_wallet_accounts` had no verification state, and `user_data.verified_after_goodmarket` is an *attribution* flag (strict timing rule), not a pure on-chain read. A cached mirror of the on-chain `Identity.isWhitelisted` read now lives on `local_wallet_accounts` so the count/tracing is one cheap query.
- **`sql/local_wallet_verification.sql`** (run in Supabase) Ð ÐÐ¢âÐ²ÒÒ `ADD COLUMN IF NOT EXISTS` `verification_status` ('verified'|'unverified', CHECK-constrained), `is_human_verified` (bool), `human_verified_at` (first confirmation), `last_verified_check` (staleness), + indexes on `is_human_verified` / `verification_status`. Existing rows start `'unverified'` until reconciled.
- **Write-time stamp:** `routes.py` `mark_local_wallet_verified(address)` (`.ilike` address match, preserves the FIRST `human_verified_at`, best-effort Ð ÐÐ¢âÐ²ÒÒ a no-op for non-local wallets / missing row / DB error). Called ONLY after an on-chain confirm, at the `/wallet` FV attribution, `/fv-callback` (main.py `from routes import mark_local_wallet_verified`), `verify-ubi`, and `/api/claims/v2/confirm`.
- **Background reconciler `local_wallet_verification.py`** (env-gated, default ON Ð ÐÐ¢âÐ²ÒÒ `LOCAL_WALLET_VERIFICATION_ENABLED=0` to opt out; interval `LOCAL_WALLET_VERIFICATION_INTERVAL_SEC`, default 3600; batch `LOCAL_WALLET_VERIFICATION_BATCH`, default 200). Backfills existing rows AND refreshes so a de-verified wallet flips back to `unverified`. Orders by `last_verified_check` (nulls first) so a table bigger than the batch is covered across ticks; writes only on change; fails CLOSED on RPC error. Wired in `main.py` right after the referral reconciler. `get_local_wallet_verification_summary()` returns total/verified/unverified/rate without any on-chain calls.
- **Admin:** `GET /api/admin/local-wallets/verification?status=all|verified|unverified` (`@admin_required`, masked wallet + counts) and `POST .../verification/sync` (run a pass now + `log_admin_action`), plus a **"Local Wallet Verification"** dashboard section (sidebar item, `case 'local-wallet-verification'`, stat cards + filterable account list). Reads use `_local_wallet_table()` (service-role preferred). **The wallet address renders as a clickable CeloScan link** (`celoscan_url` built server-side as `https://celoscan.io/address/<full address>`) with a **copy** button (`copyLocalWalletAddress`) so the admin can inspect an account's on-chain history in one tap; the full address stays in the payload, `wallet_short` is the masked display form.
- **Rules:** the on-chain read is the single source of truth Ð ÐÐ¢âÐ²ÒÒ never derive verification from `user_data` flags. Keep `human_verified_at` first-write-only. Keep the reconciler so the cached count can't go stale.
- Tests: `tests/test_local_wallet_verification_content.py` (13 tests, no deps Ð ÐÐ¢âÐ²ÒÒ functional drive of the REAL reconciler against a fake Supabase table + stubbed `blockchain.is_identity_verified`: mark verified/unverified, de-verify flip, preserve first timestamp, fail-closed RPC error, missing client, summary counts; plus SQL/route/main/template/env wiring). Run with `python -m pytest tests/test_local_wallet_verification_content.py`.

## Abandoned local-wallet signups must not count as users (2026-10)
Admin reported "yung ibang users na hindi ginawa ang wallet sa local ay naisali sa localwallet account" Ð²Òâ the Local Wallet Verification counts/table looked like they contained ordinary (WalletConnect / injected / Privy) users.
- **Root cause:** the ONLY writer to `local_wallet_accounts` is `/api/local-wallet/register`, and it inserts the row the moment the browser has generated a keypair Ð²Òâ **before** the user saves their recovery words and completes the first `/api/local-wallet/login`. Every abandoned signup (the 12-word step is where users bail) therefore left a permanent row, and nothing in the table could tell those rows apart from real accounts. So "users who never really made a local wallet" were counted as local-wallet users. (Verified by exhaustively auditing every `insert`/`upsert` in the repo + full git history: no other writer, no DB trigger, no FK/backfill Ð²Òâ this is the cause, not a second writer.)
- **Fix Ð²Òâ record the real completion signal.** `sql/local_wallet_signup_completion.sql` (`ADD COLUMN IF NOT EXISTS signup_completed boolean default false, first_login_at timestamptz`; backfills `signup_completed=true`/`first_login_at=last_login_at` for every row that already has `last_login_at`) + `sql/local_wallet_accounts.sql`. `local_wallet_register` inserts `signup_completed: False`; `local_wallet_login` flips it to `True` and stamps `first_login_at` once. **Both write paths retry without the new columns if the insert/update fails**, so a pre-migration deployment still registers and logs in (never block signup on a schema change).
- **Counting:** `get_local_wallet_verification_summary()` counts completed signups only and adds `completed`/`incomplete`; the reconciler (`_fetch_accounts`) filters `.eq('signup_completed', True)` so it never spends an RPC on an abandoned row. Both **degrade to the pre-migration behaviour** (treat every row as completed / check every row) when the column is missing.
- **Cleanup:** `cleanup_incomplete_local_wallet_accounts(dry_run=True)` + `POST /api/admin/local-wallets/abandoned/cleanup` (`dry_run` defaults TRUE Ð²Òâ a stray click can't delete; `log_admin_action`) and a two-step **ÑÒ¹Â§â Clean up abandoned** dashboard button (dry-run count Ð²Ò¶â confirm Ð²Ò¶â delete). The admin list gained a `status=incomplete` filter and renders abandoned rows with a grey ÑÒ¹ââÐ¿ÑÒ¸ Abandoned badge; `verified`/`unverified`/`all` never surface an unfinished row.
- **Rule:** a "user exists" row written before the flow completes is an artifact Ð²Òâ always separate it from the completed count, and never count a row that never logged in.
- Tests: `tests/test_local_wallet_verification_content.py` (`test_incomplete_signups_excluded_from_counts_and_reconcile`, `test_cleanup_counts_then_deletes_incomplete`, `test_cleanup_reports_missing_column`, plus SQL/route/login/dashboard wiring).

## Second Supabase project - splitting a feature off the free tier (2026-10)
`local_wallet_accounts` can live in its **own** Supabase project so its rows stop counting against the primary project's free-tier DB size. It is the first (and currently only) tenant because it is self-contained: **no foreign keys into any other table**, so it needs no cross-project join.
- **Env:** `SUPABASE_URL2`, `SUPABASE_ANON_KEY2`, `SUPABASE_SERVICE_ROLE_KEY2`. Unset -> every helper returns `None` -> callers fall back to the primary project, so an un-migrated deployment is unchanged and can migrate later with no code change.
- **Helpers:** `supabase_client.get_supabase_client2()` / `get_supabase_admin_client2()` (+ `is_supabase2_configured()`), cached like the primary pair. Service role is required: the table has RLS on with no policies, so the anon client cannot see it.
- **Routing is ONE decision point per module.** `routes._local_wallet_table()` and `local_wallet_verification._get_table()` both resolve project 2 first; the verification module's summary/cleanup used to build their own clients inline and now share `_get_table`, so there is exactly one place to change routing.
- **Rule - any project-2 client wins over any primary client.** Do NOT interleave the two (`admin2() or admin1() or anon2() or anon1()`): if project 2 is configured but its admin key is missing, that order silently falls back to the *primary* project and split-brain accounts into the wrong database. Resolve all project-2 clients, then all primary ones. A test pins this ordering.
- **Failure mode to keep:** if the split is configured but the second project is unreachable, the helper returns `None` (or the caller's existing error) - it must NOT quietly read the primary project. The table is created on the second project by running `sql/local_wallet_accounts.sql` + `sql/local_wallet_signup_completion.sql` + `sql/local_wallet_verification.sql` there.
- **Not covered:** the frontend never touches the table directly (only `/api/local-wallet/*`), so no JS change is needed. `sql/supabase_table_size_report.sql` is still the way to find which tables actually consume the primary project's quota - the local-wallet table is small; the log/history tables are the real weight.
- Tests: `tests/test_supabase_second_project_content.py` (16 - real `supabase_client.py` against a stubbed `supabase` package: unset/placeholder detection, admin+anon creation, caching, anon-only config, primary client untouched; real `_get_table()` selection: prefers project 2, falls back to primary, uses project-2 anon when the admin key is absent, survives a stub with no `*_2` helpers, `None` with no client; plus routes + `.env.example` wiring and the project-2-first ordering). No deps; run with `python -m unittest tests.test_supabase_second_project_content`.

New wallets are created with **8-digit PINs**; legacy wallets keep their **6-digit** PINs and must unlock forever. The rule is split in `static/js/local-wallet.js`:
- `_normalizePin` (unlock/decrypt, lenient): `/^(?:\d{6}|\d{8})$/` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ NEVER tighten this to 8-only, it would brick every legacy account (the digit rule is client-side; ethers decrypts with any string).
- `_normalizeNewPin` (create only, strict): exactly 8 digits + `_isWeakPin` blocklist (all-same-digit, repeated halves like 12341234, 2-digit repeats like 12121212/34343434, straight runs like 12345678/87654321, static `_WEAK_PINS` list). Birthday-style PINs (07161999) are ALLOWED. `create()` uses this; exported as `GMLocalWallet.normalizeNewPin`.
- `GMLocalWallet.pinStrength(pin)` returns `'empty'|'typing'|'weak'|'strong'` for the live meter; `_isWeakPin` only judges all-digit input (incomplete typing is never "weak"). homepage.html `lwPinStrengthHint()` renders it under `#lwPin` (create mode ONLY Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ unlock mode hides it so legacy 6-digit PINs never show "weak"); hidden on mode switch and after `_lwCompleteLogin`.
- `templates/homepage.html` `submitLocalWallet` validates per mode: create Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `normalizeNewPin`, unlock Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `normalizePin`; `#lwPinLabel` flips "8-digit PIN" / "PIN (6 or 8 digits)" in `setLocalWalletMode`.
- Unlock modals (`local-wallet.js` `_lwInjectModal`, `templates/wallet.html` `#lwUnlockModal` + `static/js/wallet-main.js` `_lwUnlockSubmit`) accept 6-or-8, `maxlength="8"`, copy says "Enter your PIN" (no digit count).
- The PIN is never stored anywhere (no hash, no server copy) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the scrypt keystore itself is the verifier; a wrong PIN simply fails decryption.
- Tests: `tests/test_local_wallet_content.py` (`test_js_pin_rules_and_email_normalized`, `test_js_create_uses_strict_new_pin_rule`, `test_js_weak_pin_blocklist`, `test_js_unlock_modal_accepts_legacy_and_new_pins`, `test_homepage_create_validates_strict_unlock_lenient`, `test_homepage_pin_inputs_allow_8_digits`).

## Local wallet PIN Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ 8 digits for new wallets, 6-or-8 for unlock (2026-08)
## Local wallet unlock fix (2026-08)
The unlock modal (`_lwOpenUnlockModal` in `static/js/local-wallet.js`) used to show a blanket "Unlock failed." for every error Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a correct PIN could still fail when the cached keystore belonged to a different wallet (address mismatch) or when the device had no local copy (server keystore only). Fixed:
- **Specific error messages:** `describeUnlockError()` maps the decrypt error ("Incorrect PIN or corrupted wallet backup.") to "Wrong PIN.", surfaces "No wallet found on this deviceÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£" and "Cached wallet belongs to a different addressÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£" instead of the misleading blanket text.
- **Server-keystore fallback:** when no local keystore exists, the modal calls `/api/local-wallet/keystore?email=Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£` (public endpoint Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the keystore is encrypted and useless without the PIN) and tries to unlock with that copy before failing. The login page (`templates/homepage.html` `_lwCompleteLogin`) persists the email in `sessionStorage['lw_session_email']` + `window.GMLocalWalletEmail` so `_getSessionEmail()` can find it.
- **Stale-keystore cleanup:** on address mismatch, `clearLocalKeystore()` removes the wrong cached copy so the next login re-caches the correct one.
- **GCash nav:** the "GCash Cashout" button moved from the Wallet bottom-sheet to the bottom navigation bar, directly after "News" (`templates/wallet.html`).
- **Hidden PIN prompt / "walang nangyayari" (2026-08 follow-up):** every `.modal-overlay` in wallet.html shares `z-index: 200`, and `lwUnlockModal` sits EARLIER in the DOM than sendModal/receiveModal/gcashModal/settingsModal/streamModal Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so the PIN unlock sheet opened INVISIBLE behind the triggering modal and the awaiting flow (e.g. `submitGcashCashout`) hung forever with zero visible feedback. Fixed with `#lwUnlockModal { z-index: 400; }`; the unlock modal must always out-stack any feature modal. Claim worked because claimModal precedes lwUnlockModal in the DOM. Also: unlock-cancel in `submitGcashCashout` now returns quietly instead of re-throwing into an unhandled rejection.
- Tests: `tests/test_local_wallet_unlock_fix_content.py` (12 tests, no deps).

## Referral program Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ local-wallet gate + auto-disbursement (2026-08)
Rewards: referrer 1000 G$, referee 500 G$. Referrals are **recorded ONLY** when the referee creates a brand-new account with the **local (email + PIN) wallet** on the homepage Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `POST /api/local-wallet/register` is the single place `record_referral()` is called. The referral code box exists only on that create form (`#lwReferralCode` in `templates/homepage.html`); the Privy box was removed and the legacy `templates/login.html` referral input was deleted. Wallet logins (`/verify-identity`, `/verify-ubi`) **ignore** any referral_code and reply with a `referral_warning` explaining the local-wallet requirement. The **referrer can use ANY provider** (injected / WalletConnect / Privy / local) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ only the referee's signup method matters.
- **Auto-disburse triggers:** after face verification (`/fv-callback` in main.py), on wallet login when already verified (`/verify-identity`, `/verify-ubi`), and on confirmed UBI claim (`/api/claims/v2/confirm` in routes.py). All go through the shared helper `ReferralService.auto_disburse_pending_referral(wallet, source=...)` (+ `_async` fire-and-forget variant) which **CAS-claims** the referral row (`claim_pending_referral_for_disbursement`) before disbursing Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ concurrent triggers / multi-worker gunicorn can never double-pay. `process_referral_disbursement`'s per-leg reward-log duplicate checks are the second layer.
- **ON-CHAIN FACE-VERIFICATION GATE (2026-09 Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the referee MUST be verified before any payout):** a referee with no on-chain verification used to still get rewards because the payout trusted DB flags. Two defects: (1) `auto_disburse_pending_referral` did NO verification check at all, and (2) `verify_and_disburse_referral` treated `user_data.face_verified`/`verified_after_goodmarket` as "verified" first and only fell back to the chain check Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ and `/wallet?fv_pending=1&src=goodmarket` (routes.py) wrote those flags from **query params alone** (`analytics.track_verification_attempt(wallet, True, face_verified=True)` + `mark_verified_via_goodmarket(require_on_chain_check=False)`), so a failed/cancelled FV or a hand-typed URL marked a wallet verified and paid its referral.
  - `ReferralService.is_referee_onchain_verified(wallet)` is the new **authoritative gate** (`blockchain.is_identity_verified`), and it **FAILS CLOSED** (RPC error / `error` payload Ð â¢Ð¢ÑÐ âÐ¢Â®Ð âÐ¢Â¶ not verified). `auto_disburse_pending_referral` runs it BEFORE claiming and returns `{"reason": "referee_not_onchain_verified", "verification_reason": ...}` without touching the row (a blocked payout must NOT consume the referral). `verify_and_disburse_referral` now does the on-chain check FIRST (the DB-flag-first branch and the attribution fallback were removed; flags are only *recorded* after the chain confirms). `process_pending_disbursements` also refuses legacy `status == 'pending'` reward legs without on-chain proof (only post-verification writes use `pending_disbursed`).
  - `onchain_confirmed=True` on the helper lets a caller that ALREADY proved verification skip the duplicate RPC read: `/verify-identity` (main.py) and `/verify-ubi` (routes.py) pass it because they reach the call only after their own `is_identity_verified` returned verified. **`/fv-callback` and `/api/claims/v2/confirm` deliberately do NOT** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ their trigger conditions are client-supplied query params / payloads, so the helper's chain check must actually run.
  - `/wallet`'s `fv_pending` attribution now calls `is_identity_verified(wallet)` first and only stamps `face_verified`/`verified_after_goodmarket` when the wallet is actually whitelisted on-chain. **Never write `face_verified=True` from a URL param without a chain confirmation** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ that flag is a payout input.
  - The admin approve endpoint (`/api/admin/referrals/approve`) remains the only manual override (admin checks Celoscan, then `onchain_verified=TRUE` + disburses).
  - Tests: `tests/test_referral_onchain_verification_gate_content.py` (18 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ functional drive of the real service with stubbed `supabase_client`/`blockchain`: unverified blocked, verified paid, DB-flags-alone blocked, fail-closed on RPC error/error payload, `onchain_confirmed` skips the read, blocked payout leaves the row `pending_face_verification`, legacy `pending` legs gated, plus wiring checks that `/wallet` verifies before stamping and that fv-callback/claim-confirm stay gated).
  - **PAYEE vs REFEREE (2026-09 follow-up Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the second reported bug):** a `referral_rewards_log.wallet_address` is the **PAYEE**. For the `referrer` leg that is the OLD (usually already-verified) inviter, whose on-chain status says NOTHING about the NEW user this referral is about. `process_pending_disbursements` originally gated on that row's own wallet, so a legacy `pending` **referrer** leg paid out 1000 G$ while the referee was never verified Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ exactly the "old user's verification lets the new user's reward through" report. Fixed with `_resolve_reward_referral(reward)` (resolve the referral via `referral_id`, else code + the payee's leg-appropriate column, **failing closed** when it cannot be attributed unambiguously) + `_reward_referee_verified(reward)` which checks the referral's `referee_wallet` for **both legs**. The gate now covers `pending` AND `pending_disbursed` (a balance/gas shortfall can queue that state BEFORE the referee verifies). Only the human admin override (`referrals.onchain_verified`, set by the approve endpoint after a Celoscan check) bypasses the chain read. **Never gate a disbursement on the reward row's `wallet_address` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ resolve the referral and check its referee.**
  - Tests: `tests/test_referral_onchain_verification_gate_content.py` (30 tests, incl. `TestReferrerLegUsesRefereeVerification`: referrer leg blocked while the referee is unverified, paid once verified, `pending_disbursed` retry gated, admin override honoured, unresolvable referrer leg fails closed).
- **Gas/balance robustness:** `referral_program/blockchain.py` `disburse_referral_reward` preflights the REFERRAL_KEY wallet's G$ balance AND CELO gas (dynamic `estimate_gas` Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð1.3, fallback 250k); low balance or gas returns `pending: True` + `insufficient_balance`/`insufficient_gas` so the referral queues as `pending_disbursed` instead of `failed`. `get_referral_wallet_balance()` also returns `celo_balance`/`celo_balance_wei`; `/api/admin/referral/key-balance` exposes them + `has_gas`.
- **Background reconciler:** `referral_program/referral_reconciler.py` (same pattern as ubi_reminder), env-gated `REFERRAL_RECONCILER_ENABLED` (default ON), `REFERRAL_RECONCILER_INTERVAL_SEC` (900), `REFERRAL_RECONCILER_STUCK_HOURS` (1). Each tick runs `reconcile_stuck_referrals()` (re-checks old `pending_face_verification` rows) + `process_pending_disbursements()` (retries queued legs after a top-up). Wired in main.py with the other schedulers.
- **Nonce-collision fix ("nonce too low: next nonce 40, tx nonce 39", 2026-08):** the referrer leg used to fail with a stale nonce while the referee leg succeeded, because (a) every disbursement call did `get_transaction_count` WITHOUT the `"pending"` tag and (b) auto-trigger threads, the reconciler and the admin retry all sign with the SAME REFERRAL_KEY concurrently. `disburse_referral_reward` now runs the nonce fetchÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶broadcast inside a process-wide `_disburse_lock`, reads the nonce with `"pending"`, and retries up to 4Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð on nonce/mempool collisions by refetching the nonce (`_is_nonce_error` matches "nonce too low/high", "replacement transaction underpriced", "already known"). Any nonce collision that still escapes is classified `pending: True` + `"error": "nonce_collision"` (auto-retried by the reconciler) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ NEVER hard `failed`. The receipt wait uses `_wait_for_receipt_patient` (returns None instead of raising `TimeExhausted`); a broadcast-but-unconfirmed tx returns `pending: True` + `"error": "submitted_unconfirmed"` WITH `tx_hash`, and `process_pending_disbursements` re-checks that hash on-chain via `check_referral_tx_status` BEFORE re-sending (confirmed Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ finalize, pending Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ stay queued, reverted Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ re-send) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a retry can never double-pay.
- **Failed reward legs are auto-retryable (2026-08):** `process_pending_disbursements` now also picks up `status == 'failed'` reward-log legs (gated by `REFERRAL_RETRY_FAILED_AFTER_SEC`, default 900s Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ 0 disables, leaving failed admin-retry-only). This is what auto-pays a referrer whose leg previously failed on a nonce collision: after deploy the reconciler's next tick re-disburses it in place (the `log_reward` pending-query includes `'failed'` so the SAME row flips to completed instead of inserting a duplicate). `process_referral_disbursement`'s pending branch records the SPECIFIC reason (`Referrer: nonce_collision | Referee: pending_disbursed`Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£) instead of the blanket "Insufficient REFERRAL_KEY balance"; only balance/gas pending rows `break` the batch (nonce/mempool pending rows keep processing).
- **Admin dashboard:** referral page explains the auto flow; REFERRAL_KEY status shows a CELO gas card + low-G$/no-gas alerts; pending list shows per-row `error_message`; approve/retry handles the queued (`result.pending`) response; `REFERRAL_RETRYABLE_STATUSES` = pending_face_verification, disbursing, failed, pending_disbursed.
- **The payout gate must check the REFEREE, not the reward row's payee (2026-09):** `referral_rewards_log.wallet_address` is the PAYEE. For the `referrer` leg that is the OLD (usually already-verified) inviter, so gating on it let the referrer's 1,000 G$ through while the NEW user the referral is about was never verified. `_reward_referee_verified(reward)` resolves the referral (`_resolve_reward_referral` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `referral_id` first, else `referral_code` + the leg's wallet column) and checks its **`referee_wallet`** for BOTH legs; an unresolvable referrer leg **fails closed**. This is why legacy `pending` rows + `pending_disbursed` retries are gated too. Only the human admin override (`referrals.onchain_verified`) bypasses the chain read.
- **A blocked payout must correct the stale status (2026-09):** the gate used to `continue` without writing, so a row parked earlier as `pending_disbursed` ("Ð âÐ£ÒÐ âÐ£ÐÐ âÐ¢Â¶Ð âÐ²Òâ Queued (low G$/gas)") kept claiming a **funds** problem while the real blocker was the referee's missing verification Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the admin tops up a wallet that was never the bottleneck. The block now writes `pending_face_verification` + "Referee not on-chain verified (Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£)" (via `update_referral_status_by_id`, which now accepts `error_message`, else `update_referral_status(referee_wallet, Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£)`). The dashboard badge reads "Queued (funds or referee FV)". Hit **`POST /api/admin/referral/process-pending`** to apply it immediately to existing rows instead of waiting for the reconciler tick.
- **Fund-need counts must exclude verification-blocked rows (2026-09):** `get_pending_disbursement_summary()` (drives `/api/admin/referral/key-balance` and the "Low G$ Balance" alert) now splits `pending_disbursed` rows by the referee gate Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `total_pending`/`total_amount` cover only rows that would ACTUALLY pay once funded, while `blocked_by_verification`(+`_amount`) reports the rest. The dashboard shows an informational (indigo) alert for that count instead of a red funds warning. Do NOT reintroduce a raw `.eq('status','pending_disbursed')` count for the balance alert.
- Tests: `tests/test_referral_onchain_verification_gate_content.py` (`TestReferrerLegUsesRefereeVerification::test_blocked_referrer_leg_corrects_stale_low_gas_status`, `test_fund_summary_excludes_verification_blocked_rows`, `test_key_balance_route_counts_only_fund_blocked_rows`, `test_dashboard_surfaces_verification_blocked_queue`).
- **Duplicate referrer payout + stranded `disbursing` status (2026-10):** two reported production defects, both fixed.
  - **Duplicate 1,000 G$ to the referrer.** The referrer-leg duplicate check in `_is_referral_already_disbursed` scoped its query by `referral_id`, so a completed leg recorded WITHOUT one (legacy rows written before `sql/referral_rewards_log_referral_id.sql`, or an id lookup that hit a DB hiccup) was invisible and an admin retry re-sent it. The referee leg deduped on wallet+type+code and was immune - exactly why only the referrer double-paid. `log_reward` had the same blind spot and inserted a second completed row. **Layer 1 (authoritative):** `ReferralBlockchain.reward_already_paid(referral_id, reward_type, wallet)` reads the contract's `rewardPaid(reward_id)` with the SAME reward_id a send would use; `process_referral_disbursement` skips any leg it reports paid, and `admin_approve_referral` pre-checks both legs and finalizes without sending when both are paid. **Layer 1b:** the log fallbacks are scoped `.is_('referral_id', 'null')` - a `referral_code` is reused for every invite, so adopting a row from a DIFFERENT referral would skip a genuine payout. **Layer 2:** `record_referral`'s guard is case-insensitive (`.ilike`) and re-checks after an insert failure; `sql/referral_unique_referee.sql` adds `UNIQUE (lower(referee_wallet))` - **a duplicate `referrals` row is the real root cause**, because `reward_id` is derived from the ROW id (`goodmarket-referral-v1:<chain>:<referral_id>:<type>:<wallet>`), so two rows yield two reward_ids and the contract pays BOTH legitimately. Layer 1's contract check is what catches that case. **Layer 1d:** `verify_and_disburse_referral` now passes `referral_id` to `process_referral_disbursement`.
  - **Stuck "Disbursing (stuck? retry)" with a healthy wallet.** `disbursing` is an intermediate CAS-claim state and nothing guaranteed it was ever left (worker killed mid-flight by gunicorn recycling, or a terminal write swallowed by `_safe`). Nothing picked it up: `reconcile_stuck_referrals` only reads `pending_face_verification` and `process_pending_disbursements` reads `referral_rewards_log`, not `referrals`. **Fix:** `reclaim_stale_disbursing()` (CAS reset back to `pending_face_verification`, cutoff `REFERRAL_STALE_CLAIM_SEC` default 300s, mirrors the reloadly/broadcast reapers) runs FIRST in `referral_reconciler._reconcile_once`; `claim_pending_referral_for_disbursement` stamps `disbursing_at` (`sql/referral_disbursing_at.sql`); the reaper **skips gracefully without the column** rather than resetting live claims. The dashboard badge now reads "Disbursing - in progress (2m ago)" vs "stuck (12m ago)" instead of always claiming stuck.
  - **`onchain_verified` was set on a queued (unsent) payout** - `pending` means a funds/gas shortfall, and that flag is the admin override that later BYPASSES the referee verification gate, so one click during a shortfall authorized every future retry unchecked. The write is now gated on `success`/`already_disbursed` only. Success paths pass `clear_error=True` so a stale "Referee not verified yet" text cannot survive on a completed row.
  - **Rules:** never dedupe a payout on the reward log alone - ask the contract; never adopt an unscoped log row for a specific referral; never set `onchain_verified` without a payout; a stranded intermediate claim state needs a reaper. Tests: `tests/test_referral_duplicate_and_stuck_claim_content.py` (19 tests, no deps). Proposal: `docs/REFERRAL_DUPLICATE_AND_STUCK_CLAIM_PROPOSAL.md`.
- **Origin-aware referral links (2026-08 follow-up, e.g. vercel.app domain):** `referral_program/referral_service.py` `build_referral_link(code)` builds the share link from `flask.request.host_url` when inside a request, falling back to `BASE_URL` (reconciler/schedulers). Used by `/api/referral/my-code` fast path + all `referral_link` fields. Browser templates double-check with a regex origin rewrite: `referralLinkOnCurrentOrigin` (dashboard.html), `withCurrentOrigin` (`_claim_celebration.html`, incl. its localStorage cache). When touching share links use the helper, not `f"{BASE_URL}/..."`.
- Tests: `tests/test_referral_local_wallet_gate_content.py` (45 tests, no deps), `tests/test_referral_onchain_verification_gate_content.py` (35 tests, no deps).

## Daily voucher Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ GoodMarket-users-only gate (2026-08)
The daily voucher (`/api/voucher/claim` in routes.py, banner in `templates/wallet.html`) is claimable ONLY by GoodMarket-created accounts Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ sessions with `login_method == "local"` (the in-app wallet from `static/js/local-wallet.js`). WalletConnect / injected MetaMask / MiniPay / Privy logins get 403 `{not_eligible: true}` from the backend (checked BEFORE the claim mark) plus a client-side fail-fast in `claimDailyVoucher()` showing a "Ð âÐ£ÒÐ âÐ£ÐÐ âÐ¢ÐÐ âÐ¢Ó Not Eligible" modal. Banner copy updated accordingly (no longer "for everyone"). Tests: `tests/test_voucher_local_gate_content.py` (no deps).

## Daily voucher Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ human-verified-only gate (2026-08)
On top of the local-login gate, the voucher is claimable ONLY by **face-verified** wallets (on-chain `Identity.isWhitelisted` via `blockchain.is_identity_verified`, 5-min cached, fails closed on check errors):
- `/api/voucher/claim` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ after the local-login gate, an unverified wallet gets 403 `{not_eligible: true, not_human_verified: true}` BEFORE the voucher slot is taken ("Only human-verified users can claim this voucher. Please complete face verification first.").
- `/api/voucher/confirm` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ same 403 gate BEFORE the CAS mark, so an unverified wallet can never consume the voucher even if it somehow obtained the link.
- `/api/voucher/daily` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ returns `{voucher: None, reason: "not_human_verified"}` for unverified wallets, so the banner is hidden entirely (they never see the voucher payload).
- Frontend `static/js/wallet-voucher.js` `claimDailyVoucher()` fail-fasts on `window._walletNeedsFV` (set by the wallet page's FV status / `openFvRequiredFromUrl`) with the same "Not Eligible" modal; banner copy says "exclusive for human-verified GoodMarket users".
- Tests: `tests/test_voucher_local_gate_content.py` (`test_claim_gated_to_human_verified`, `test_claim_fv_gate_runs_before_voucher_fetch`, `test_claim_fv_gate_fails_closed`, `test_confirm_gated_to_human_verified`, `test_daily_hides_banner_for_unverified`, `test_frontend_failfast_for_unverified`).

## Daily voucher Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ failed attempts don't consume the voucher (2026-08)
`/api/voucher/claim` previously marked the daily voucher `is_claimed=true` the moment a user tapped the button Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so a failed on-chain attempt consumed the voucher. Now the claim endpoint only returns the link; `/api/voucher/confirm` is the ONLY place that marks `is_claimed` (CAS on `is_claimed = False`), after the on-chain withdraw succeeds. Frontend mirrors this: `claimDailyVoucher()` no longer hides the banner on POST success; `voucherDappClaim()` / `wcExecuteClaim()` hide it only after their `/api/voucher/confirm` POST. Also: `voucherDappClaim()` routes `login_method == 'local'` through `GMLocalWallet.getProvider()` after `_lwUnlockIfNeeded()`, and `#lwUnlockModal` z-index is 100000 so the PIN modal out-stacks `voucherResultModal` (inline z-index 99999).

## Face Verification intro onboarding for unverified users (2026-09)
Unverified users (notably new **GoodMarket wallet / local-login** users) landed on `/wallet` with an orb that only said "Tap to start face verification". Nothing explained *why* verification is required or what it unlocks, so the KYC / one-account-per-person rule was invisible and the claim sheet Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ which cannot pay out yet Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ was the only destination.
- **Hero now dispatches.** `#ubiClaimHero` (orb) and `#heroClaimCta` (pill) call `openClaimOrVerify()` instead of a hardcoded `openModal('claimModal')`. Unverified Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `#fvIntroModal`; verified / already-claimed Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ the plain claim sheet. `updateClaimButtonBox()` sets the routing flag (`window._walletNeedsFV` + `window._walletFvReason`) from the same `needsVerification` condition that picks the hero copy, so the copy and the routing can never disagree.
- **Re-verification skips the intro** (`_fvNeedsOnboarding()` returns false when `_walletFvReason === 're_verification_needed'`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ that user already saw the explainer, and the Settings card + expiry banner already cover it. The `?fv_required=1` bounce, `fvStatusCta`, `fvExpiryBannerCta` and the "Re-verify Face ID" hint keep calling the existing flow unchanged.
- **Copy (locked by tests):** hero eyebrow "Verify to start earning", sub "Tap to verify your account so you can start earning money", CTA "Verify my account". The modal has four cards Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ KYC ("Know Your Customer (KYC) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£ performed by the **GoodDollar identity service**, not by GoodMarket"), one person/one account ("linked to **one account only** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£ prevent abuse"), privacy ("anonymized record Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£ never sees or stores"), and what it unlocks ("claim your daily G$") Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ plus a gold **Continue** and a **Not now** escape.
- **Honesty constraint (do not regress):** GoodMarket does **not** run its own KYC Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ GoodDollar's identity service does. Copy must never imply the app performs KYC or collects ID documents. The intro is also **copy only**: the real gate stays `human_verification.py` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `blockchain.is_identity_verified()`; never treat the explainer as the enforcement point.
- **Continue reuses the existing signing flow** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `continueFvIntro()` closes the intro, opens the claim sheet and calls `window._fvBeginOnboarding()` (declared *inside* the claim IIFE, where `startFV`/`needsVerification` are in scope) Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `startFV()`. No signing logic is duplicated, so local-PIN / WalletConnect / MiniPay handoff / Trust Wallet blocker all behave identically. `typeof`-guarded so a missing hook still opens the claim sheet.
- **Modal stacking:** `#fvIntroModal` is a `.modal-overlay` DOM-ordered after `#claimModal` (all overlays share z-index 200 and stack by DOM order); `#lwUnlockModal` keeps its higher z-index so the PIN prompt still stacks over the claim sheet. The intro's CSS uses only theme vars (`--text-dim`, `--accent-secondary`, `--card-soft`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a literal colour would fail the dark-canvas AA test.
- Tests: `tests/test_fv_intro_onboarding_content.py` (21 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ modal copy/KYC honesty/one-account/privacy, hero routing, CSS-var-only rule, plus a functional node harness driving the real `_fvNeedsOnboarding`/`openClaimOrVerify`/`continueFvIntro` bodies for unverified / verified / not-yet-probed / re-verification, and Continue's handoff + missing-hook resilience). `tests/test_wallet_hero_cta_content.py` and `tests/test_claim_button_rework_content.py` updated from the pinned `openModal('claimModal')` + old copy. `scripts/render_wallet_modals.py` force-opens `fvIntroModal` for visual QA. Run with `python -m pytest tests/test_fv_intro_onboarding_content.py`.

## Claim surface Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ readable "hot" state + dead claim-box CSS removed (2026-09)
The claimable state of `#ubiClaimBtn` was an orange/pink "Plasma Ember" gradient with WHITE text. Measured against that gradient's own stops it was **1.86:1** (`#ffae00`), **2.87:1** (`#ff6a00`) and **3.61:1** (`#ff2e63`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ every stop below the 4.5:1 AA floor, so the most eye-catching element on the page was the least readable. It also sat off-brand next to the G$-gold `.btn-primary`.
- **Hot state is now G$ gold with dark text** (`--primary-button-text` `#1f1300` on `#fbbf24 Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ #fde68a Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ #f59e0b`): **8.5:1 Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð â¢Ð¢Ò¡ 14.6:1** across every stop. Keyframes renamed `ubiClaimHeatShift/EmberGlow/FlameWiggle` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `ubiClaimSheen/Glow/SparkTwinkle`; the flame badge is now a Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐÂ© spark. Still opacity/background-position only (no box-shadow keyframes Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ that forces a full repaint every frame).
- **~16 dead claim selectors deleted** (Ð â¢Ð¢ÑÐ âÐ²ÒÒ£Ð â¢Ð£Â®11 KB): `.action-item`, `.action-circle`, `.claim-circle`, `.claim-glyph`, `.claim-badge`, `.claim-button-box*`, `.claim-box-*` (10), `.action-label*`, plus `@keyframes claimHalo/claimBadgeFloat/claimBoxGlow/claimIconGlow/sparkleAnim`. They were the pre-hero claim UI, referenced by **zero** markup and zero JS Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ verified by scanning `class="Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£"`, `classList.*`, `className` across the template and every bundle. The lone survivor was `document.querySelector('.action-item')` as a dead scroll fallback in `handleFvReverifyClick` (now `#ubiClaimHero`). `templates/wallet.html` is down to ~164 KB (budget 200 KB).
- **Hero CTA had no muted state.** `updateClaimButtonBox()` toggles `is-disabled`/`is-claimed` on `#ubiClaimHero`, but the `#heroClaimCta` pill had no rule for either Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so "Verify Face ID" and "View claim details" rendered as the same loud gold pill as an actual claim, and on first load the label was the hardcoded "Claim UBI G$" before any data arrived. Added `.ubi-hero-cta.is-disabled/.is-claimed` (muted `--card-soft` + `--text-dim`, 5.6Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð â¢Ð¢Ò¡5.9:1 composited), a `:focus-visible` ring (it is a `div[role=button]`, so it had none), and JS now mirrors the orb's state classes + `aria-disabled` onto the CTA.
- **The CTA stays clickable when muted** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ deliberate. The claim modal is the only route to Face Verification, so a truly `disabled` CTA would strand unverified users. Muted, not blocked.
- **`prefers-reduced-motion` gap fixed:** `.ubi-hero-orb::before` runs `heroOrbHalo Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£ infinite` with `transform`/`opacity` but was in neither reduced-motion block. Added it (and the now-live `.ubi-hero-cta`); dropped the selectors deleted above. `.spinner` stays exempt on purpose (it signals in-flight work).
- **Hero orb state fix (2026-09):** the orb's halo reset was dead Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `heroOrbHalo` is declared on `.ubi-hero-orb::before`, but the reset was a bare `.ubi-hero-orb { animation: none }` on the element, so a claimed/paused orb kept pulsing the "claim me" cue. The reset now targets `::before`. The state colours were also **inverted**: the claimable orb was purple while the NOT-claimable orb was gold (the brand's "do this now" colour), so the "verify first" state read as the urgent one. Claimable is now gold, claimed green, not-ready muted slate, and the hover glow follows the state hue. Each state has a distinct radial stop Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ guarded by `test_orb_states_are_mutually_distinct` so a re-skin can't collapse them.
- Tests: `tests/test_claim_button_rework_content.py` (16 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ measures the gradient stops against the text colour so a future re-skin that drops below AA fails CI, asserts the dead selectors stay gone, and checks the hero CTA muted/focus/aria wiring + the orb state colours/halo reset). `test_wallet_load_perf_content.py` + `test_wallet_dark_purple_theme_content.py` updated for the renamed keyframes and removed selectors.

## Wallet page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Dark Purple web3 theme (2026-09, supersedes "Soft Sunrise")
`templates/wallet.html` is back on a **dark purple** surface so the wallet hub no longer flips lightÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶dark when the user moves to swap / savings / dashboard / claim (all dark). The Soft Sunrise light theme was replaced, not layered on top.
- **How it's built:** a `Dark Purple Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ web3 theme override` section appended at the END of the main `<style>` block Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the original base rules are untouched and the override wins by cascade order. Same architecture as Soft Sunrise, so the base rules still carry the layout and the override carries the palette. **Re-theming is still a single-block edit.**
- **Palette:** deep indigo canvas `--bg: #0B0F2A` / `--bg2: #130D35` / `#1A103D`, violet accent `--accent: #7c3aed` (`--accent-rgb: 124,58,237`) with `--accent-secondary: #a78bfa`. `--gold`/`--gold-deep` are back to `#fbbf24`/`#f59e0b`. Glass cards use translucent white borders (`rgba(255,255,255,0.1)`); the warm-brown `rgba(67,56,43,Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£)` neutrals are gone (they smudge on indigo).
- **Contrast rule (the load-bearing part):** `#7c3aed` is the accent **surface** colour Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ as *text* on the dark canvas it is only ~2.6:1. Labels use `--accent-secondary` (`#a78bfa`) or `#c4b5fd` instead. `--primary-button-text: #1f1300` keeps the G$-gold primary buttons dark-on-gold (white on amber fails). Locked in by a real WCAG AA check in `tests/test_wallet_dark_purple_theme_content.py` (`test_every_literal_text_color_meets_aa_on_the_dark_canvas`) that measures every literal `color:` against both dark surfaces Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so a future light-theme color copied back in fails CI rather than shipping.
- **Intentional exceptions** (do not "fix" these): the QR plate stays white for scannability, and the voucher banner + G$ badge keep their gold surfaces with dark text. They are allowlisted in the theme test.
- **Shell/PWA:** `<meta name="theme-color">` and `manifest.json` (`background_color #0B0F2A`, `theme_color #7c3aed`) follow the dark canvas; the full-page maintenance takeover owns its own `#0B0F2A` background so it can't flash white over the app.
- **JS bundles carry the palette too** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `wallet-main.js` / `wallet-streams.js` set status colors inline, so they were migrated to the dark status set (`#34d399` success, `#f87171` error, `#fcd34d`/`#fbbf24` warning, `#60a5fa` info, `#67e8f9` cyan). **When adding inline styles to these bundles, use the dark equivalents Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the AA test covers the bundles as well as the template.**
- **Preview harness:** `scripts/render_wallet_preview.py` renders the template with stubbed `url_for`; `scripts/render_wallet_modals.py` additionally force-opens each modal for visual QA. Output goes to `.agent_tmp/render/` (gitignored).
- **Bottom-nav chips must be SOLID, not alpha washes (2026-09 follow-up).** Send / Receive / Earn / More originally rendered as `rgba(accent, 0.16-0.2)` washes over the `rgba(11,15,42,0.96)` bar Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ which measured **1.01:1** against it, i.e. the chips were invisible and the bar read as four flat grey labels. Each chip is now an opaque `linear-gradient` (`--nav-g1` -> `--nav-g2`) carrying a **white bold glyph**, with the label keeping a light tint of the same hue (`--nav-c`). Verified in the rendered page: chip-vs-bar **1.01:1 -> 3.10:1**, white glyph **6.1-6.8:1** on every chip. **Do not "soften" these back to translucent tints** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ an alpha wash over a near-black bar can never stand off it. Locked in by `test_nav_chips_are_solid_not_alpha_washes`, `test_nav_chip_and_glyph_clear_contrast_against_the_bar` (measures both gradient stops vs the real composited bar colour) and `test_nav_labels_are_bold_and_colored`.
- Tests: `tests/test_wallet_dark_purple_theme_content.py` (25 tests, no deps). Run with `python -m pytest tests/test_wallet_dark_purple_theme_content.py`.

## Local-wallet UX copy Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ "Log in" / "Sign this transaction" (2026-08, PR #200)
User-facing copy avoids the word "Unlock": the homepage Get Started toggle says **"Log in with email + PIN"** (submit: "Log in to my account"), and every local-wallet PIN prompt says **"Sign this transaction" / "Sign & Continue" / "SigningÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£"** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the prompt almost always precedes a tx signature, and "unlock" read like a re-login. `window._lwOpenUnlockModal(opts)` in `static/js/local-wallet.js` accepts optional copy overrides (`{title, subtitle, submitLabel, busyLabel}`) for future message-signing (non-tx) prompts; defaults are tx-signing. `templates/wallet.html` has its own richer modal with the same copy. Internal identifiers (`setLocalWalletMode('unlock')`, `isUnlock`, `lwUnlockModal` ids, `unlockWithKeystore`) intentionally kept Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ code-only.

## Local-wallet recovery phrase step Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ copy-required instructions + community link (2026-09)
The 12-word BIP-39 phrase is generated in `GMLocalWallet.create()` (`ethers.Wallet.createRandom()`) and shown ONCE by `_lwRenderPhraseStep()` (`templates/homepage.html`). It is **never stored server-side** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `/api/local-wallet/register` sends only `email, address, keystore, referral_code`, and the server keystore is PIN-encrypted with no mnemonic copy. So the phrase step is the user's only chance to keep the words.
- **Bold, explicit instructions** in a red `.lw-recovery-instruction` box (the only bolded block in the step, by design): you MUST copy and save the 12 words, they are the ONLY way to recover the account/funds, they can be imported into **MetaMask or Trust Wallet**, and losing them means nobody Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ including GoodMarket support Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ can recover the wallet. Every line is wrapped in `<strong>` AND the container sets `font-weight: 700` so the emphasis survives any future wrapper change.
- **Copy button** `#lwCopyWordsBtn` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `lwCopyRecoveryWords(btn)` (`homepage.html` inline JS): copies the phrase via `navigator.clipboard.writeText`, falling back to a hidden `<textarea>` + `execCommand('copy')` (older mobile dApp browsers), with success/failure feedback on the button. The phrase lives in the module-scoped `_lwRecoveryWords` (memory only Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ set in `_lwRenderPhraseStep`, cleared in `_lwCompleteLogin`) so it is never written to localStorage/sessionStorage or sent to the backend.
- **Clickable community link** in the same step: `<a href="https://t.me/GoodMarketCommunity" target="_blank" rel="noopener noreferrer" class="lw-community-link">` ("Need assistance? Join the GoodMarket Telegram community"). The URL matches `GM_JOIN_COMMUNITY_URL` in `static/js/wallet-bottomnav.js` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ one community group, not `t.me/GoodDollarX`.
- **Not wired:** there is still no in-app "view my phrase again" button (`GMLocalWallet.exportMnemonic(pin)` exists but has no caller) and no in-app restore-from-words flow Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the phrase is only usable by importing it into an external wallet. If either is added, keep the phrase out of persistent storage.
- Tests: `tests/test_local_wallet_content.py` (`test_recovery_step_tells_users_they_must_copy_the_phrase`, `test_recovery_instruction_is_rendered_bold`, `test_recovery_copy_button_copies_phrase_to_clipboard`, `test_recovery_step_links_clickable_goodmarket_community`).

## Homepage "Get Started" connect modal reused on guest feature pages (2026-10)
`/swap` (e.g. a shared `?tab=goodswap` link), `/savings`, **`/learn-earn`, `/community-stories`, `/farming` and `/reloadly`** used to bounce a not-logged-in visitor to `/` (or render a dead-end form) Ð²Òâ a shared deep link, the strongest conversion surface we have, died on the homepage. All six pages now **render for guests** and drive them into the **same** connect flow the homepage uses.
- **Single source of truth:** the modal markup/CSS/JS were extracted out of `homepage.html` into `templates/_get_started_modal.html`, `static/css/get-started.css`, `static/js/get-started.js`. The homepage includes the partial with `{% set get_started_boot = {'return_to': ''} %}`. **Do not copy the modal back into a page** Ð²Òâ reuse the partial so connect logic can never drift. The generic card is `templates/_guest_connect_card.html` (`.gm-guest-connect-card`, optional per-page copy via `guest_card_icon/kicker/title/body/button` set in a Jinja `{% with %}`); its CSS lives in `get-started.css`. **Never ship a second connect modal** Ð²Òâ `community_stories.html`'s old bespoke `#walletModal` (manual wallet-address entry + `/verify-ubi` POST) was deleted; `redirectToLogin`/`openWalletModal`/`connectWalletNow`/`setReturnFlag` no longer exist and every connect entry point calls `openGoodMarketGetStarted()`.
- **Include contract:** the parent must load `wc-bridge.js` + `local-wallet.js` BEFORE the partial (the flow uses `GMWalletConnect` / `GMLocalWallet`), set `get_started_boot` (a `return_to` target, usually `request.full_path`), then `{% include "_get_started_modal.html" %}`. The partial emits `window.GM_GET_STARTED_BOOT` (`returnTo`, `postLoginPath` â from the `POST_LOGIN_PATH` context var, `wcSidecarEnabled`) and exposes `window.openGoodMarketGetStarted()`.
- **Guest-safe signing (the hard rule):** a guest gets an empty `wallet`, so every signer resolver (`getConnectedSwapSigner()` / `_resolveSigner()` / `_aiSwapResolveSigner()`) hard-refuses before any prompt Ð²Òâ it never falls through to "use whatever injected account is there" (a different account than the one that would own the funds). Each route sets `is_guest = not (wallet and verified)` and blanks the wallet for guests (`routes.py::learn_earn_page`, `farming/routes.py::farming_home`, `reloadly/routes.py::reloadly_home`, `community_stories/routes.py::community_stories_page`, `routes.py::swap_page`, `savings/routes.py::savings_home`). **The gate is server-side** Ð²Òâ hiding the UI is not the gate.
- **The real feature UI is behind `{% if is_guest %} Ð²ÒÒ° {% else %}`** in each template, so a guest render genuinely omits it (locked by a Jinja render test asserting member markers are absent). Guest pages also short-circuit their member JS: `window.GM_LEARN_GUEST` / `window.GM_RELOADLY_GUEST` flags + `if (!isGuest)` (community stories) + `if (!WALLET) return;` in `static/js/farming.js` (`init`). The Learn & Earn auto-opening "how it works" guide overlay is skipped for guests so it cannot cover the connect card.
- **Return-to, then the post-login landing:** a guest who started on a feature page (e.g. `/swap`) lands back THERE â `postVerifyIdentity`'s response `redirect_to` is rewritten by `_gmApplyReturnTo`, and the injected / WalletConnect / local flows go through `_gmAfterLoginRedirect(delay)` (`_gmReturnTo || _gmPostLoginPath`). With no return-to, everyone lands on the **post-login landing**, `config.POST_LOGIN_PATH` â **`/community` (the Community Feed) by default**, env-overridable â NOT `/wallet`. `config.get_post_login_path()` is the single source of truth: every backend `redirect_to` response (`/api/session-status`, `/verify-identity`, `routes.verify_ubi`, `/api/local-wallet/login`) and `routes.index()`'s logged-in redirect call it, and the template context processor surfaces it as `POST_LOGIN_PATH`, which `_get_started_modal.html` puts into `GM_GET_STARTED_BOOT.postLoginPath` for `_gmPostLoginPath` in `static/js/get-started.js`. **Never reintroduce a hardcoded `'/wallet'` redirect default in a success handler.** Tests: `tests/test_post_login_redirect_content.py`.
- **Guest entry points on the pages:** `/swap` + `/savings` **auto-open the modal on load** (`DOMContentLoaded` Ð²Ò¶â `openGoodMarketGetStarted()`) and every page carries a persistent gold `Get Started` button (`.gs-nav-cta`, `{% if is_guest %}`) so closing the modal doesn't strand the visitor. The four newer pages (`/learn-earn`, `/community-stories`, `/farming`, `/reloadly`) do **not** auto-open Ð²Òâ the connect card occupies the primary action slot and is the single entry point (the swap guest-card pattern).
- **`/community` (the Community Feed) is the seventh guest page** and follows the same
  contract: `community_feed/routes.py::community_feed_home` sets
  `is_guest = not bool(wallet)`, the member composer sits behind
  `{% if is_guest %} ... {% else %}`, and a guest gets both the shared connect card
  **and** the persistent `Get Started` nav button. **Reading is public, writing is
  gated (2026-10)** -- this page deliberately differs from the other guest pages:
  - **The feed section is OUTSIDE the guest branch.** `id="cfFeed"` renders for
    everyone; only `#cfComposer`/`#cfCaption`/`#cfPostBtn`/`#cfUsernameGate` are
    member-only, and the feed bundle loads in BOTH branches. If the feed slides
    back inside the branch, guests silently see zero posts. Locked by
    `test_feed_section_is_outside_the_guest_branch`, which scans the
    `{% if %}`/`{% endif %}` tag stream **by nesting depth** -- the branch nests
    the username gate and the storage warning, so a naive `index("{% endif %}")`
    finds an inner one and passes even when the feed is back inside.
  - **`community_profile.html` / `community-profile.js` are public too**, same
    reason: every author link in the public feed points at
    `/community/u/<username>`, so gating the profile makes every card a dead end
    for a signed-out reader. The view is read-only (media + a link back to the
    feed), so there is no write surface to gate; the bundle is hoisted OUT of the
    branch and loaded once for both audiences.
  - **Every write action raises the connect card.** `init()` no longer returns
    early for guests; post / upload / react / report / comment all call
    `requiresLogin()` (`if (!WALLET) { requiresLogin(); return; }`), which shows
    the toast and calls `openGoodMarketGetStarted()`. Comment is gated **before**
    the network call so a guest never fetches a thread they cannot use. The
    server still rejects every unauthenticated write with a 401 -- the client gate
    is UX, not the security boundary. Locked by
    `scripts/smoke_community_feed.cjs` section 8.
  - **The guest branch MUST load ethers before `local-wallet.js`.** The connect
    modal's in-app wallet needs it: `GMLocalWallet.create()` /
    `unlockWithKeystore()` call `_assertEthers()` and `showLocalWalletPanel()`
    returns early when `ethers` is undefined, so "Create GoodMarket Wallet" and
    "Log in with email + PIN" were both dead ends (the same reloadly.html bug).
    The member branch deliberately loads **no** ethers -- the feed signs nothing --
    so the tag lives inside the guest branch only, and the ethers-free assertion in
    the tests is scoped to the member branch (`test_member_bundle_is_ethers_free`).
    Use the repo-standard 6.13.4 tag with the verified `sha384-6Zl0Pc8zj...` digest;
    a wrong algorithm/length pair makes the browser refuse the script outright.
  - **A connect card alone is not an entry point.** Every other guest page pairs it
    with a nav CTA; without one, a guest who dismisses the modal or scrolls past the
    card has nothing left to tap.
  - **Bottom nav + member search + Earn sheet (2026-10).** `/community` now carries
    the wallet-style thumb bar (`#cfBottomNav`): Feed, Search, Earn, Buy Airtime
    (`/reloadly/#topup` â the Reloadly top-up tab) and Your Wallet (`/wallet`).
    The destination list lives in ONE place, `GM_COMMUNITY_BOOT.nav` (template) /
    `BOOT.nav` (bundle), and `renderNav()` builds the bar from it with
    `textContent` â add a destination there, never in the template markup.
    `guestOk` marks a destination a signed-out visitor may reach: **Feed and
    Search are read-only and stay open to guests; Earn, Buy Airtime and Your
    Wallet call `requiresLogin()`** (toast + shared connect card) and swallow the
    click, so a guest is never dropped on a page they cannot use. Reading stays
    public, writing stays gated â the same contract as the feed itself.
    - **Member search** (`#cfSearchBar`/`#cfSearchInput`/`#cfSearchResults`, inside
      the sticky header so both stick as one unit) hits the new public
      `GET /community/api/users/search` â `svc.search_users()`. The response is
      **usernames only â never a wallet** (the same no-address-oracle rule as the
      feed/profile). `normalize_search_term()` strips to `[A-Za-z0-9_]` and returns
      `None` below `SEARCH_MIN_CHARS` (2), and `_like_pattern()` **escapes** LIKE
      metacharacters (`\\`, `%`, `_`) before the server adds its own `%â¦%` â `_` is a
      legal username char so it cannot be stripped, but unescaped it would match
      names the user never asked for. The client debounces (220ms), refuses a
      1-char term before any request, and guards responses with a sequence counter
      so a slow early reply cannot overwrite a newer one. Results are written with
      `textContent` (a username is user data).
    - **The Earn sheet** (`#cfSheetEarn`/`#cfSheetOverlay`, `openSheet()`/`closeSheet()`)
      mirrors the wallet's `bnSheetEarn` geometry in the community palette. `openSheet()`
      toggles `body.cf-modal-open` (the CSS scroll lock) â without the matching rule the
      page scrolls behind the overlay. Escape closes the sheet **and** the search bar.
      `sheetGo()` re-checks `WALLET` so a stale DOM/console call still gets the connect
      card. A guest tapping Earn must never open the sheet.
    - Below 900px the bar is a bottom bar; at/above 900px it becomes the fixed left
      rail the wallet uses. Every rail rule lives inside the `@media (min-width: 900px)`
      block â unscoped, it steals a permanent left gutter on a phone.
    - Locked by `scripts/smoke_community_feed.cjs` sections 13/13b/13c and
      `tests/test_community_feed_content.py::TestUserSearch`.
  - **Follow buttons + pure-white theme + Instagram-style video (2026-10).**
    Three owner asks on the same page, all in one change. Proposal/revision
    plan: `docs/COMMUNITY_FOLLOW_WHITE_THEME_VIDEO_PROPOSAL.md`.
    - **Follow-to-follow** is a new table `community_feed_follows`
      (`sql/community_feed_follows.sql`), composite PK
      `(follower_wallet, followee_wallet)` + `CHECK (follower_wallet <>
      followee_wallet)`. Same shape as `community_feed_reactions`: the PK makes
      a duplicate follow physically impossible, so a double-tap cannot inflate a
      count and there is no read-modify-write race. Wallets are stored
      **lowercase** and every predicate is `ilike` (the case-mismatch bug
      class). RLS is on with no policies (service-role only), like the other
      feed tables.
    - **The client names a USERNAME, never a wallet.** `svc.follow_user()` /
      `unfollow_user()` resolve the username server-side via
      `wallet_for_username()`. **Follow counts are public; the follow GRAPH is
      never returned**  a follower list is an enumeration surface. Tests
      serialize every response and assert no `0x` leaks. `POST`/`DELETE
      /community/api/users/<username>/follow`; both feature-gated and
      auth-gated (a guest gets the connect card). **Idempotent**: a duplicate
      follow is a SUCCESS, so a retried/double-tapped request converges instead
      of erroring. `FOLLOW_LIMIT` (env `COMMUNITY_FOLLOW_LIMIT`, default 5000)
      caps how many a wallet may follow.
    - **A guest tapping Follow/Like/Comment/Post raises the connect card**  the
      existing read-public/write-gated contract, unchanged.
    - **Follower/following counts silently read 0 (2026-10 fix).** The
      follow edge inserted fine and the button flipped to "Following", but
      every count showed 0. Cause: `community_feed/service.py::_count()`
      selected a literal `"id"` column, and `community_feed_follows` is
      keyed by its **wallet pair** with no `id`; PostgREST fails the whole
      request (`42703 undefined_column`), the `except` in `_count` swallowed
      it, and the count fell back to 0. `_count` now takes a `select=`
      projection that DEFAULTS to `"*"` (the count comes from PostgREST's
      `count="exact"`, not from the returned column); the one caller that
      wants a narrow payload passes `select="id"` explicitly on
      `community_feed_posts`, which really has that column. **Never
      hard-code a column in a generic count helper** -
      a table without it turns every count into a silent 0. The repo's
      `FakeSupabase` hid this twice over (it accepted any column AND
      synthesised an `id` on insert); the regression test drives a new
      `StrictSupabase` that rejects an unknown `select` column and does not
      invent an `id`, so the silent-0 path stays covered
      (`TestFollow::test_counts_work_when_the_table_has_no_id_column`).
    - **The community canvas is PURE WHITE** (`--cf-bg`/`--cf-bg2`/`--cp-bg`
      `#ffffff`, `--cf-field`/`--cp-card-soft` `#f7f7f7`, `theme-color`
      `#ffffff`). This **overrides the older AGENTS.md note** that the feed
      matches the wallet's warm cream "Soft Sunrise" canvas  the owner asked
      for white, so the feed and the shared profile are white; the wallet /
      savings / learn & earn pages are untouched. The body's decorative orange
      radial gradients were removed (a gradient over white is not white). Cards
      keep their `1px` border and inputs keep a visible border + grey field 
      on white, that hairline is what separates the control from the surface.
      Text tokens are unchanged, so every contrast pair that passed AA on cream
      still passes on white (white is lighter, so contrast only rises).
    - **Video is Instagram-style: bounded, rounded, `object-fit: contain`**  a
      `.cf-media .cf-video-frame` / `.cp-media .cp-video-frame` with
      `max-height: min(68dvh, 640px)`, `aspect-ratio: 4 / 5` default, and
      `border-radius: 12px`; the frame is a `<button>` that opens the lightbox.
      **Never `object-fit: cover` on a feed video**  it silently crops the
      user's clip. The bundle sets `style.aspectRatio` from the media row's
      `w`/`h` when present (no letterbox bars) and leaves the CSS default when
      absent. The composer PREVIEW tile legitimately uses `cover` (a square
      thumbnail); the feed video must not.
    - **Multi-photo post previews (2026-10).** Image-only posts with 2–4 photos
      use a bounded, two-column mosaic (2: one row; 3: first tile spans two rows;
      4: a 2 × 2 grid, in upload order) in both the feed and public profile.
      Tiles crop to square thumbnails and open the original image in the
      lightbox. Single photos and any post containing video keep the existing
      media layout; the profile album keeps its separate three-column tiles.
    - Locked by `tests/test_community_feed_content.py::TestFollow` /
      `TestFollowSchema` / the new `TestCss` cases,
      `tests/test_community_profile_content.py::TestFeedTheme` /
      `TestProfileCss`, and `scripts/smoke_community_feed.cjs` sections
      14/14b/14c/14d/15 + `scripts/smoke_community_profile.cjs` sections 17/18.
  - **Profile pictures + reaction breakdown + `@` search (2026-10).** The
    `/community` social upgrade. Proposal/revision plan:
    `docs/COMMUNITY_SOCIAL_UPGRADE_PROPOSAL.md`. Two commits: the
    **re-land of #490** (merged then reverted by #491) kept separate, then the
    new work, so the re-land stays reviewable apart.
    - **Profile pictures** (re-landed): a new `community_feed_avatars` table
      (`sql/community_feed_avatars.sql`), wallet PK, public URL + R2 key + mime
      + bytes. Upload is a **presigned R2 PUT**  bytes never pass through Flask.
      Images only (JPEG/PNG/GIF/WEBP), `MAX_AVATAR_MB` (env
      `COMMUNITY_MAX_AVATAR_MB`, default 5), mime checked **server-side** and
      never trusted from the client. `storage/r2.py::build_key()` is
      `u/<sha256(wallet)[:16]>/<YYYY>/<MM>/<uuid>.<ext>`  the wallet is
      **hashed**, so the public URL cannot be linked back to the address. The
      avatar rides on every feed response via `_avatars_for_wallets()` (ONE
      batched query per page) and renders on cards, comments, search results and
      the public profile, falling back to the initial-letter circle.
      `static/js/community-avatar.js` owns the modal + upload; the feed/profile
      bundles only RENDER and repaint on `GMCommunityAvatar.onChange`.
    - **Public avatar delivery (2026-10 follow-up).** Browser images use
      `GET /community/api/profile/<username>/avatar` instead of requiring the
      R2 CDN URL to be publicly readable. The feature-gated route resolves the
      username and avatar row server-side, reads only a managed R2 object within
      the configured avatar cap (5 MB by default), and rechecks image magic/MIME.
      Responses use `nosniff` and an ETag tied to R2's current object version, with
      short browser/shared-cache lifetimes. Public feed/profile payloads contain
      only same-origin paths, not CDN URLs, wallets or R2 keys; the existing
      owner-only presigned upload flow still carries its upload reference. Feed,
      profile and owner-avatar renderers fall back to the initial on delivery
      failure. The anonymous chatroom profile endpoint also escapes LIKE
      metacharacters and verifies the returned normalized handle; `_` is a valid
      username character but must never act as a wildcard in public lookups.
    - **The reaction BREAKDOWN is decoration; `reaction_count` stays
      AUTHORITATIVE.** `_reaction_breakdowns_for_posts()` tallies which
      reactions a post got in ONE batched query per page (never one per card),
      bounded by `REACTION_SCAN_LIMIT`. `_clean_breakdown()` keeps only known
      keys with positive counts (a stale row for a removed reaction must not
      render a chip the client has no emoji for). **Never sum the breakdown to
      produce the headline number**  the scan is bounded, so a pathological post
      would show a wrong count. The card stacks overlapping badges
      (`buildSummaryEmojis`), most-used first, the viewer's own reaction always
      leading, with a trailing `+N`; the row is `aria-hidden` so the number is
      not announced twice. `react()` keeps the row in step with the optimistic
      count and rolls **chip + count + breakdown back together** on failure,
      then reconciles with the server's response.
    - **The `@` in the search field is CHROME, outside the `<input>`.** A member
      never types it; a typed or pasted `@` is stripped from the field and again
      before the request, so the field never shows `@@`. **A stored `@` is
      tolerated, never required** (the username form validates `@` away, but an
      imported/legacy row can carry one): the `%term%` pattern already spans a
      stored leading `@` (so `bet` finds `@betz` and `@bet` does too  ONE
      pattern, no `or` clause), `username_variants()` lets
      `wallet_for_username()` try both spellings, and the display name is
      `@`-stripped in `search_users`, `_public_post`, `_public_comment` and
      `get_public_profile` so a legacy row never renders `@@betz`.
    - Locked by `tests/test_community_feed_content.py` (breakdown, authoritative
      count, no-wallet-leak, unknown-key drop, a failing scan not breaking the
      feed, the batched-query shape, tolerant search, `@`-stripping, template
      chrome, stacked-summary CSS, bundle source guards) and
      `scripts/smoke_community_feed.cjs` sections 10c/10d/10e.
  - **Instagram-style feed + mobile theme (2026-10).** Owner ask: the `/community`
    feed should "look and feel like a real social platform", with the theme and
    background most closely resembling **instagram.com, especially on mobile**.
    Proposal + rationale: `docs/COMMUNITY_INSTAGRAM_MOBILE_REDESIGN_PROPOSAL.md`.
    - **Instagram is a LAYOUT GRAMMAR, not a palette.** Four traits carry the
      look, all in ONE `@media (max-width: 640px)` block so the desktop rail
      layout is untouched: (1) posts are **sections of one white canvas** — the
      card loses its border/radius/elevation and is separated by a hairline;
      (2) media is **full-bleed**; (3) the action row is an **icon row directly
      under the media**; (4) the caption reads **`author` (bold) + text** on one
      line. The phone block, not 480px, because a 600px window is still a
      one-column phone-shaped feed (the desktop rail only starts at 900px).
    - **`max-width: none` on `.cf-wrap` in the phone block is LOAD-BEARING.** The
      full-bleed media pulls back by exactly `--cf-gutter` (`margin-inline:
      calc(var(--cf-gutter) * -1)`), which only lands on the screen edge when the
      wrap IS the viewport. Between 620px and 640px the 620px `max-width` would
      make the pull-back overshoot, and `overflow-x: clip` would **silently cut
      the photo's edge**. `--cf-gutter` must equal the wrap's own inline padding
      at every breakpoint (0.85rem default, 0.6rem at 480px) or the same thing
      happens — verified in a real browser (`media.touchesEdges === true`).
    - **New tokens:** `--cf-hairline` `#dbdbdb` (IG's separator — one line weight
      for the card edge, section rules and top bar), `--cf-ink-2` `#737373` (IG's
      secondary grey, AA on white), `--cf-elev` (base `var(--cf-shadow)`, set to
      `none` in the phone block so every sibling flattens in ONE place).
      `.cf-card` uses `box-shadow: var(--cf-elev)` — a direct
      `var(--cf-shadow)` would ignore the phone override.
    - **The image path crops like IG; the video path must NOT.** `.cf-media img`
      is `object-fit: cover` in a 4:5 box (IG's portrait default). `buildCard`
      sets the image's own `aspect-ratio` from the media row's `w`/`h` when the
      server reported them, and marks the node **`cf-media-raw`** (`contain`,
      `aspect-ratio: auto`) when it did not — **never guess a crop box**, it cuts
      a face off. The video frame keeps `contain` + `border-radius: 12px` +
      `max-height: min(68dvh, 640px)`; the frame's inline `aspect-ratio` is the
      only ratio source (there is no wrap custom property).
    - **The icon action row's heart IS the `like` reaction** — same optimistic
      path, same rollback, no second write. `react(post, null)` is an explicit
      **REMOVAL**: the heart's un-tap calls it, and `react` now normalises a
      null/undefined key to the previous reaction so it takes the DELETE path.
      **Never POST a null reaction** (the server's check constraint rejects it).
      The heart fires only once per post (`_reactionInFlight`). Report stays in
      the row — it is the moderation path the guest-gate harness drives, so
      moving it behind `⋯` would break a load-bearing test.
    - **Double-tap likes (with a heart burst); single tap still opens the
      lightbox.** A tap arms a `DBL_TAP_MS` (300ms) timer and a second tap
      cancels it and likes instead — so the gesture is distinguishable. `dblclick`
      is wired too: a mouse never fires the two synthetic taps the timer counts,
      so without it the gesture would be phone-only. The burst is
      `transform`/`opacity`-only (`cfBurst`) and `display: none` under
      `prefers-reduced-motion` (without the animation it would sit over the photo
      as a permanent heart).
    - **The "Recent posters" rail** (`#cfRail`) is IG's story rail, honestly
      labelled: this app has no 24-hour story model, so calling it "Stories"
      would promise something the data cannot honour. It is built in
      `renderFeed()` from the authors **already on the feed page**
      (`railAuthors()`) — **no new endpoint, no new table, no migration**, and it
      can never show a member who has not posted. Distinct authors, most recent
      first, **your own row leads** (labelled "You", the composer entry point,
      a `<button>`), capped at `RAIL_MAX` (12). Every row is addressed by
      **username** (`/community/u/<username>`), never a wallet, and a stored `@`
      is stripped. A guest gets the rail too (reading is public). It starts
      `hidden` in the template so an empty feed cannot flash an empty strip.
    - **Brand rule — borrow the grammar, never the trade dress.** AGENTS.md
      carries an impersonation-risk rule (Blockaid flagged the app once), so the
      stylesheet copies **no** Instagram brand colour (its blue), wordmark or
      logo; accents stay GoodMarket orange / G$ gold, and the bottom-nav active
      tab is **ink**, not a brand hue. The brand may appear in a **comment** as
      the reference URL the design follows, never in a CSS RULE — the test strips
      comments before asserting. **Never write a raw selector or token into a
      comment**: the repo's naive CSS scanners split on those literals, so a
      comment naming `@keyframes`/`@media`/a hex colour can break an unrelated
      test (the same rule the CSP test documents).
    - Locked by `tests/test_community_feed_content.py::TestInstagramMobileLayout`
      (grammar rules incl. the `max-width: none` and gutter parity, the crop vs
      `raw` split, the icon row, the null-key DELETE path, the caption shape, the
      double-tap, the rail's dedupe/cap/username-addressing/no-wallet, the brand
      rule, and the theme pins) and `scripts/smoke_community_feed.cjs` sections
      16/16b/17/18/19/19b (the REAL bundle: heart POST/DELETE, double-tap +
      burst + lightbox suppression, the aspect box, the rail). The harness's DOM
      stub's `style` now mirrors a real `CSSStyleDeclaration`
      (`setProperty`/`getPropertyValue`) and it no longer uses descendant
      selectors (`matches()` cannot resolve them) — walk `children` instead.
- **Tests must read the effective homepage:** homepage content tests use `tests/homepage_page_src.py` (`read_homepage_page()`), which concatenates `homepage.html` + `static/js/get-started.js` + `_get_started_modal.html` + `static/css/get-started.css`.
- **IIFE extraction trap Ð²Òâ inline handlers MUST be re-exposed on `window` (2026-10).** `get-started.js` is wrapped in `(function(){ 'use strict'; Ð²ÒÒ° })()`, but `_get_started_modal.html` wires its buttons with inline `onclick`/`oninput`/`onchange` attributes, which resolve the handler name on `window`. When the flow was extracted from the homepage, `submitLocalWallet` / `finishLocalWalletSignup` / `submitLocalWalletUsername` / `skipLocalWalletUsername` / `lwCopyRecoveryWords` / `lwPinStrengthHint` / `syncReferralInputs` were NOT attached to `window`, so after a user filled in email + PIN the **Create my free wallet** and **Log in to my account** buttons threw `ReferenceError: submitLocalWallet is not defined` and appeared completely dead (the rest of the modal Ð²Òâ preflight, toggles Ð²Òâ worked, since those were exposed). Fix: every inline-handler target defined in the bundle is assigned to `window` near the other `window.* =` exports at the bottom. **When moving inline-handler functions into a bundled IIFE, re-expose each one on `window`, and when adding a new inline handler, add the matching `window.<fn> =`.** Locked by `test_every_inline_modal_handler_is_exposed_on_window` (parses the modal's inline handlers, intersects with the bundle's top-level functions, fails if any is missing a `window.` export) + `test_local_wallet_form_handlers_are_global`.
- Tests: `tests/test_get_started_guest_pages_content.py` (swap/savings: shared assets, guest route branches, guest-only includes + `return_to` + auto-open + nav CTA, the return-target redirect guards, and the inline-handler-is-a-global regression guard) and `tests/test_get_started_feature_guest_pages_content.py` (the four newer pages: shared card partial + CSS, all four route gates, per-page card/modal/nav CTA wiring, the server-side gate being `{% else %}`-paired, guest JS short-circuits, and a **Jinja render harness** asserting the member UI is absent for guests and present for members). Run with `python -m pytest tests/test_get_started_guest_pages_content.py tests/test_get_started_feature_guest_pages_content.py`.

### Guest-facing pages must not trip the WalletConnect expiry guard (2026-10)
A guest opening `/savings` (no session / not logged in) was told **"Your WalletConnect session has expired"** and auto-logged-out to the homepage after 5 seconds. Root cause: `savings/routes.py::savings_home()` rendered with `login_method=session.get("login_method", "walletconnect")`. A guest has no session, so it fell through to the **`"walletconnect"` default**; the template then called `GMWalletConnect.configure({loginMethod: "walletconnect", ...})` with an **empty wallet**, and `wc-bridge.js` auto-started its expiry guard for **any** `login_method === "walletconnect"`. With no stored WC session the guard classified the state as `expired` Ð²Ò¶â red banner + `window.location.href = '/logout'` Ð²Ò¶â homepage. (Every other guest-facing page Ð²Òâ `/swap`, `homepage.html` Ð²Òâ already defaults `login_method` to `""`, which is why only `/savings` showed it.)
- **Two-layer fix:** (1) `savings/routes.py` defaults `login_method` to `""` (a guest has no login method Ð²Òâ never default it to `walletconnect`); (2) `wc-bridge.js` gained `_shouldRunExpiryGuard()` = WC login method **AND** a non-empty `_config.walletAddress`, used by BOTH `_guardCheck()` and the `configure()` auto-start. **Never start the WC expiry guard for a session with no wallet** Ð²Òâ a guest-facing page must never show "session expired" or auto-logout.
- **The guard still works for real WC logins:** a genuine `login_method === "walletconnect"` session with a wallet and no stored session still shows the red banner + auto-logout (locked in by a node harness test).
- **Rule for new guest-facing pages:** default `login_method` to `""` (never `"walletconnect"`), and never call `GMWalletConnect.configure()` with a WC login method but no wallet.
- Tests: `tests/test_savings_guest_session_content.py` (6 tests Ð²Òâ a **node harness over the REAL `wc-bridge.js`** driving `configure()`/`_guardCheck()` with a stubbed DOM/localStorage/timers: guest (empty wallet) schedules nothing and shows no banner, empty login_method is exempt, a real WC login still flags expired; plus wiring assertions on the route default and the helper). Run with `python -m pytest tests/test_savings_guest_session_content.py`.

### `/swap` guest "Get Started" card replacing the swap form Ð²Òâ PROPOSAL (2026-10, not implemented)
Requested: on `/swap`, a not-logged-in visitor should see the **Get Started connect card where the "Enter an amount" field/button is**, and the amount input + swap button should appear **only after connecting**. Today the full swap form renders for guests and every action dead-ends on "Ð²Ò¡Â Ð¿ÑÒ¸ No wallet detected". Plan (server-side `{% if is_guest %}` gate per swap/reserve/bridge card, a new `templates/_swap_connect_card.html` partial calling `openGoodMarketGetStarted()`, drop the guest auto-open, keep `getConnectedSwapSigner()`'s session-wallet match untouched) is in **`docs/SWAP_GUEST_CONNECT_CARD_PROPOSAL.md`** Ð²Òâ awaiting review, no code changed.

## Homepage CTA copy Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ "Get Started" (2026-09)
The homepage entry points use **"Get Started"** for the wallet modal Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ header button (`<span>Get Started</span>`) and hero primary button (`Get Started Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶`). The older "Create Account" / "Create Free Account" labels were dropped: on a first visit "create account" reads like a signup wall, while "Get Started" covers both branches the modal offers (create a wallet OR log in with an existing one). The wallet-guide banner (`templates/_wallet_guide_banner.html`) names the button by its current label in its step lists, so it was updated in lockstep; its GoodWallet-panel CTA reads "Get Started Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶". The modal title now covers **both** branches ("Log in or create your GoodMarket account") and the option list leads with a create card **and** a log-in card Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ see the log-in entry point section below; the 2026-09 note that the modal "keeps the account-creation framing" is superseded. Locked in by `tests/test_homepage_loader_profiles_content.py` (`test_ctas_read_get_started`, `test_modal_title_explains_login_or_create`, `test_wallet_guide_banner_uses_get_started_label`); renames must update the banner text too or the step-by-step instructions point at a button that doesn't exist.

## Homepage ÐÒ¢ÐÒ¶ÐÂ¶ log-in entry point for returning GoodMarket wallet users (2026-10)
The shared Get Started modal (`templates/_get_started_modal.html` + `static/js/get-started.js`) used to be **create-only**: the title read "Create your GoodMarket account", the loudest (gold) card was **"Create GoodMarket Wallet"**, and the ONLY route to log in was a small text link at the **bottom of the create form** (`Already have an account? â Log in with email + PIN`). So a returning (email + PIN) user tapped **Get Started**, had to press **"Create"** to find **"Log in"**, then scroll past email/PIN/confirm/referral/Create. `showLocalWalletPanel('unlock')` was never called from anywhere â the unlock plumbing existed with no entry point of its own.
- **New `#loginWalletBtn` card** ("Log in to my wallet") in `#walletOptions`, wired to `openLoginPanel()` â `showLocalWalletPanel('unlock')`. It **deliberately skips the create preflight reminder** â that gate exists to stop people making a SECOND wallet, and a returning user is doing the opposite. `showLocalWalletPanel`'s preflight branch must stay `if (mode === 'create' && !_lwPreflightAcked)`.
- **Welcome-back detection** â `_lwDetectReturningUser()` (get-started.js), called from `_showWalletModalNow()`. A local wallet's **encrypted keystore lives in `localStorage` and SURVIVES logout** (the `/logout` route clears only the Flask session + cookies), so on modal open it reads `GMLocalWallet.getLocalKeystore()` and, when a keystore is present: shows `#walletReturnBanner` ("Welcome back. This device already has your GoodMarket walletâ¦"), **hoists the log-in card above "Create"** (`banner.insertAdjacentElement('afterend', loginBtn)`), **moves the single gold treatment** (`loginBtn.classList.add('primary')` + `createBtn.classList.remove('primary')` â still exactly ONE loud card), and **prefills the email** so the user only types their PIN.
- **Convenience, never a gate.** The log-in card is **always rendered**, so a returning user on a NEW device (no cached keystore) still has a clear way in â they just get the calm card. A **missing or throwing** keystore read is non-fatal (`catch (_) { saved = null }`), and an email-only record (no `.keystore`) is ignored. Do NOT turn this into a gate or a redirect.
- **Copy** â title/subtitle now cover BOTH branches: "Log in or create your GoodMarket account" / "Use your email + PIN, or connect a wallet you already have". The old create-only title is gone (the `test_modal_title_still_explains_account_creation` pin was replaced by `test_modal_title_explains_login_or_create` in `test_homepage_loader_profiles_content.py`).
- **CSS** â `.wallet-return-banner` (starts `display: none`, `.show` reveals; violet `--primary-soft`/`--primary-2` palette) mirrors `.wallet-detect-banner`'s geometry. The log-in card's calm rules are **`:not(.primary)`-guarded** (`static/css/get-started.css`) so they never override the gold card's dark text when the returning-user check moves `.primary` onto it. `--primary-soft` was added to the CSS `:root` block (get-started.css) because guest pages don't define it.
- **Because the modal is shared**, this lands on the homepage AND every guest-facing page (`/swap`, `/savings`, `/learn-earn`, `/community-stories`, `/farming`, `/reloadly`).
- Tests: `tests/test_homepage_login_entry_content.py` (15 tests â content wiring incl. the inline-handler-is-a-global rule, plus a **node harness driving the REAL `_lwDetectReturningUser`/`openLoginPanel` bodies**: keystore found â banner + hoist + gold move + email prefill + unlock mode; no keystore â nothing touched but log-in still works; throwing read non-fatal; email-only record ignored). Mutation-verified (removing the wiring, routing log-in to create, or dropping the gold move all fail). Run with `python -m pytest tests/test_homepage_login_entry_content.py`.

## Homepage theme + create-wallet reminder (2026-10)
The homepage had no design system (no `:root`, ~60 scattered hex literals) and read as cluttered/inconsistent ("nakakalito"), and the **Create GoodMarket Wallet** option let anyone start a second wallet without warning Ð²Òâ a second wallet for a face already verified on GoodDollar cannot claim G$.
- **"Deep Indigo" design system** (`templates/homepage.html`) Ð²Òâ a single `:root` token block (`--bg`, `--surface`, `--primary`, `--action`, `--text-dim`, `--sheet`, Ð²ÒÒ°) appended at the **END** of the main `<style>` so base rules are untouched and the whole restyle is one revertable block. Rules: **one action colour** Ð²Òâ G$ gold (`--action`/`--action-2`/`--action-text: #1f1300`) reserved for real money actions, dark text on gold for AA; **violet is structure only** (links/eyebrows/borders); green/amber/red are **semantic only**. Decorative background layers are capped (loader 4 gradients Ð²Ò¶â 1 radial + 1 linear; `body::before` a single soft radial). The Get Started modal has **exactly one loud card** (the recommended create action is the gold gradient; `.wallet-option-card.wc-option` is a calm `--sheet` card). **Re-theming is a single-block edit Ð²Òâ never edit the base rules, append to the override.** Locked by `tests/test_homepage_theme_signup_content.py` (incl. a brace-matching scan that no `@keyframes` animates `box-shadow`/`left`/`top`, and that `heroDotPulse` is opacity-only).
- **Hero wording** Ð²Òâ two-line headline (`Earn G$ every day.` / `Save, swap & spend.`), the redundant "Learn. Play. Earn." third line and the docs link in the button row removed, one primary + one secondary CTA (`Get Started` / `Explore features`), and the Tagalog line kept **once** as a support line. **The homepage names NO GCash cash-out** (2026-10) Ð²Òâ the hero, hero-check, how-it-works step 3 and the og/twitter meta all had it removed; cash-out is a wallet-page feature. The loader no longer carries a contributor credit line (see the landing reimplementation section below).
- **Create-wallet reminder + smart routing** Ð²Òâ `#localWalletBtn` now calls `openCreatePreflight()` Ð²Ò¶â `showLocalWalletPanel('create')` which, when `!_lwPreflightAcked`, shows `#lwPreflightStep` BEFORE `#lwCreateStep`. The panel states GoodMarket is **one account per person** (GoodDollar face verification), that a **second wallet for the same face cannot be verified / claim G$**, and lists MiniPay / GoodWallet / MetaMask with a **"connect instead"** escape (`cancelCreatePreflight()` Ð²Ò¶â `showWalletOptions()`). `#lwPreflightContinue` is `disabled` until `#lwPreflightAck` is ticked (`onPreflightAckChange`); `ackCreatePreflight()` sets `_lwPreflightAcked` (once per session). **The unlock path is never gated** Ð²Òâ the preflight branch is `mode === 'create'` only. `_lwApplyDetectionNudge(provider)` (called from `detectInjectedWallet`, which clears it when none is found) shows `#walletDetectBanner` and **hoists the connect card above create**, giving MiniPay the gold (`primary`) treatment. Copy + an acknowledgment only Ð²Òâ the create logic and the on-chain verification rule are unchanged; never imply the app deletes/merges a face.
- **Preview:** `scripts/render_homepage_preview.py` renders the page standalone (stubbed Jinja context) to `.agent_tmp/render/` for visual QA.
- Tests: `tests/test_homepage_theme_signup_content.py` (18 tests, no deps Ð²Òâ design-system placement/tokens, AA pairing, one loud card, capped layers, keyframe paint-cost scan, wording, preflight ordering/ack gating/connect-instead, unlock not gated, honest one-face copy, detection banner, plus a **node harness driving the real preflight + nudge bodies**). Run with `python -m pytest tests/test_homepage_theme_signup_content.py`.


## Landing splash + homepage nav/scroll/content viewing (2026-10, PR #444)
Four user reports in one pass: the splash was a multi-second gate, the section panels had no working entry point, the page scrolled sideways / jumped, and certificates could not be read.
- **The loader was a 12Ð²Òâ15 s GATE, not a splash.** `MIN_VISIBLE_MS = 12000` / `MAX_VISIBLE_MS = 15000` held the page long after load, so the app looked frozen on every visit. Now `MIN_VISIBLE_MS = 800`, `MAX_VISIBLE_MS = 3500`, and it hides the moment `readyState === 'complete'` (`loader.classList.add('is-hidden')` Ð²Ò¶â `remove()` after 650 ms). **Never re-introduce a multi-second minimum** Ð²Òâ it is a perceived-hang, not a polish delay.
- **Loader copy:** the `Claim up to 100+ G$ dailyÐ²ÒÒ°` line and the `gm-loader-credit` contributor line (MOB Labs / Betz / Omar) are **removed**. The splash sells the product; contributor names live only in the "Meet our Developers" panel. `test_loader_shows_contributor_credit` was replaced by `test_loader_has_no_contributor_credit_line`.
- **`musicPulse` animated `box-shadow`** (full repaint per frame) Ð²Ò¶â transform-only scale.
- **Dead navigation removed.** `toggleMainMenu()` was **never called**, `.main-menu-btn` is not in the markup, and `#main-menu-submenu` was `display:none` with no opener Ð²Òâ so the 9 `showSection()` panels had **no visible entry point**. The function + its `window.toggleMainMenu` export are deleted.
- **`#main-menu-submenu` is now a working sticky tab bar** ("Explore GoodMarket") directly under the sticky header (`.submenu-wrapper { position: sticky; top: 56px }`), with horizontally scrollable gold pills (`.submenu-nav` is `flex-direction: row`, tabs `border-radius: 999px` / `white-space: nowrap`, active state gold). **Every panel id must keep a matching `#tab-<name>`** Ð²Òâ locked by `test_every_reachable_panel_has_a_tab`.
- **Scrolling:** `html { scroll-padding-top: calc(var(--header-h) + 64px) }` so a section jump never hides a panel title under the header. `body` used `width: 100vw` (includes the scrollbar Ð²Ò¶â forced horizontal scroll) and `min-height: 100vh`; now **`100%` + `100dvh`** so the mobile address bar cannot cause a layout jump.
- **Certificate viewing:** certificates are dense documents and were unreadable at carousel size with **no zoom at all**. Tapping one opens `#certLightbox` (full-screen, `z-index: 100000`, tap-outside / `Ðâ` / Escape to close), and `.cert-slide img` is height-capped `min(70vh, 560px)` with `cursor: zoom-in`. `openCertLightbox`/`closeCertLightbox` are on `window`.
- **Tweet embeds** are viewport-bounded (`height:min(520px, 78dvh)`, `min-height:320px`) instead of a fixed 520 px, and `#community-tweets-section` is `width:100%; max-width:min(100%, 620px)`.
- **Theme completion:** the last competing hues were folded into the design system in the override block Ð²Òâ certificate greens (`#34d399` title/amount/dots) Ð²Ò¶â `var(--action)` gold; `.fs-book-b1` blue gradient Ð²Ò¶â indigo, `.fs-book-b3` green Ð²Ò¶â gold; `.fs-module-pill .dot` Ð²Ò¶â gold; the 8 inline `style="color:#10b981;"` "100 G$" highlights + the `#10b981,#059669` badges Ð²Ò¶â `#fbbf24` / `#fbbf24,#f59e0b`. Verified: **no literal text colour in the override block is below WCAG AA (4.5:1)** on the indigo canvas (gold is 11.27:1 on `#0B0F2A`).
- **Connect modal scroll lock:** `_setBodyScrollLock()` in `static/js/get-started.js` is reference-counted and wired into `_showWalletModalNow` / `closeWalletModal` / `hideWalletModalForPrivy` / `restoreWalletModalAfterPrivy`, so the page behind the sheet no longer scrolls on touch.
- Tests: `tests/test_homepage_landing_nav_content.py` (17 tests, no deps Ð²Òâ loader timing/copy/paint, dead-toggle removal, sticky pill tab bar, header scroll-padding, panelÐ²Ò¶âtab parity, `100vw`/`dvh` guards, lightbox, tweet height, no-GCash). Its `_rule()` helper anchors on **8-space top-level indentation** so `body` cannot resolve to a media-query override Ð²Òâ reuse that helper for new CSS assertions. Proposal: `docs/LANDING_AND_HOMEPAGE_REIMPLEMENTATION_PROPOSAL.md`. Run with `python -m pytest tests/test_homepage_landing_nav_content.py`.


## One-tap "Connect GoodWallet" Ð²Òâ auto-start the QR (2026-10)
Tapping **Connect GoodWallet** used to open a panel whose only control was a *second* button ("Show WalletConnect QR / URI"), so the QR the user asked for was two taps away. `connectWalletConnect()` already emitted `Ð²Ò¸Ñ Processing barcode and URI codeÐ²ÒÒ°` as its first status Ð²Òâ it was simply gated behind that redundant tap.
- **`templates/homepage.html`** Ð²Òâ the `#wcOptionCards` container + its button are **removed**; `#walletConnectPrimaryButton`'s `onclick` is now `openGoodWalletConnect()`, which does `showWalletConnectOptions()` (open panel) **then** `connectWalletConnect()` (start). A `#wcLoading` progress block (spinner + "Processing barcode and URI codeÐ²ÒÒ°", `role="status"` / `aria-live="polite"`, `hidden` by default) sits in the QR slot; `_wcSetLoading(true)` on entry, `false` in `drawWalletConnectQr()` (the moment the QR is ready) and in **every** exit path (success + catch) so a failure never leaves a spinner running.
- **`showWalletConnectOptions()` MUST stay the plain reset** Ð²Òâ it is the "Choose another method" Back target. Auto-starting there would launch a new session on Back. It also hides the loader on the way in. Locked by `test_back_button_does_not_auto_start`.
- **`_wcConnecting` re-entrancy guard** so a double-tap cannot open two concurrent sessions.
- **`setWcQrCompactMode()` was deleted** Ð²Òâ it only toggled the removed `#wcOptionCards`. Never re-add a second "show the QR" button: the card is the single entry point.
- **CSS** Ð²Òâ `.wc-loading` / `.wc-spinner` / `.wc-loading-text` / `.wc-loading-sub` appended in the Deep Indigo override block, `.wc-loading[hidden] { display: none }` (needed because the block is `display:flex`), `@keyframes wcSpin` is **transform-only**, and the spinner is disabled under `prefers-reduced-motion`.
- **`_wallet_guide_banner.html`** GoodWallet tab step now reads `Choose Connect GoodWallet Ð²Òâ the QR code and copy-link appear automatically.` (the old "tap Copy URI and paste it" step described the removed second tap). **Rename/refactor the flow Ð²Ò¶â update the banner step in lockstep.**
- Proposal + rationale: `docs/GOODWALLET_CONNECT_UX_PROPOSAL.md`.
- Tests: `tests/test_homepage_theme_signup_content.py` (Part C Ð²Òâ removed button/container/helper, card starts immediately, Back never starts, loader hidden-by-default + cleared on QR/error, transform-only spin + reduced-motion, banner copy, plus a **node harness** driving the real `openGoodWalletConnect`/`showWalletConnectOptions`/`_wcSetLoading` bodies asserting open-then-start ordering and Back-not-starting). Run with `python -m pytest tests/test_homepage_theme_signup_content.py`.

## WalletConnect panel Ð²Òâ status banner above the fold (2026-10)
The live step message the user needs most (`ÑÒ¹âÐ Awaiting wallet approvalÐ²ÒÒ°`, `Ð²Ò£ÒÐ¿ÑÒ¸ Signature request sentÐ²ÒÒ°`) rendered **after** `#wcQrWrap` Ð²Òâ i.e. below ~526px of QR + Copy + three stacked deep-link buttons Ð²Òâ inside a `max-height:90vh` modal. On a phone it was **below the fold**, so users had to scroll to discover the next action ("very confusing"). Meanwhile the prime spot under the Back button was held by a **static, non-actionable** note ("Generating a secure WalletConnect session for GoodWalletÐ²ÒÒ°") that never changed.
- **`templates/homepage.html`** Ð²Òâ the static `.wc-panel-note` in the WC panel is **deleted**; `#walletConnectPanelStatus` is **moved above `#wcQrWrap`** and becomes `.wc-status-banner` (`role="status"` + `aria-live="polite"`, colour-coded `.info`/`.success`/`.warning`/`.error`). The banner mirrors the flow: Ð²Ò¸Ñ Processing Ð²Ò¶â ÑÒ¹âÐ Awaiting wallet approval Ð²Ò¶â Ð²Ò£ÒÐ¿ÑÒ¸ Approve the signature Ð²Ò¶â Ð²Ò£â¦ Connected. **The ordering IS the fix** Ð²Òâ locked by `test_status_banner_renders_before_the_qr`. Base `.status-message` is `display:none` until a state class lands, so an empty banner costs no vertical space.
- **`showWalletStatus` must PRESERVE the banner class.** It assigned `panel.className = 'status-message ' + type` wholesale, which silently dropped `wc-status-banner` on the first message. Now `'status-message wc-status-banner ' + type`. Locked by `test_banner_class_survives_show_wallet_status`.
- **Redundant QR heading/desc removed** (`#wcInstructions`, `.wc-qr-title`, `.wc-qr-desc` + their CSS) Ð²Òâ the banner carries the instruction now; the QR keeps `aria-label="WalletConnect QR code"`. `.wc-qr-wrap` padding trimmed `1rem Ð²Ò¶â 0.85rem`.
- **Deep links are ONE compact row** (`flex-wrap:wrap`, short labels ÑÒ¹ââ Installed / ÑÒ¹Ó£Ð Trust / ÑÒ¹Ò°Ò  MetaMask) instead of three stacked full-width buttons Ð²Òâ ~120px reclaimed. They are **alternatives, not sequential steps**, and on mobile they are often the *primary* path (a phone cannot scan its own screen), so they stay visible rather than being hidden behind another tap.
- **Two latent bugs fixed in the same block:** (1) the `#wcBackBtn` handler toggled `#wcQrContainer` against `#wcInstructions`, but `#wcInstructions` lived **inside** `#wcQrContainer`, so the toggle was dead; (2) it also set `backBtn.textContent = 'Ð²Ò¶Ò'`, **destroying the "Choose another method" label** on the first click. The toggle is removed; the handler now only reveals the button. Locked by `test_back_button_toggle_bug_is_fixed`.
- Proposal: `docs/GOODWALLET_STATUS_PRIORITY_PROPOSAL.md`.
- Tests: `tests/test_homepage_theme_signup_content.py` Part D (8 tests Ð²Òâ note removed, ordering, aria/hidden-when-empty, class preservation, state styles, dead heading/desc + CSS gone, back-button bug, compact deep-link row, plus a **node harness** driving the REAL `showWalletStatus` asserting the banner class survives all four states). Run with `python -m pytest tests/test_homepage_theme_signup_content.py`.

## Connect GoodWallet QR â pre-QR timeouts too tight for slow networks (2026-10)
On a slow mobile connection the in-browser WalletConnect (SignClient) fallback aborted **before the QR ever rendered**. The two pre-QR stages were timeboxed at only **20s** (`sdk_init` â SDK bundle download + the `wss://relay.walletconnect.org` WebSocket handshake) and **15s** (`connect` â the relay round-trip that returns the pairing URI). A slow link therefore showed the SDK's own `WebSocket connection failed for host: wss://relay.walletconnect.org` and our red **"relay did not respond within 20s"** error instead of a scannable barcode.
- **`static/js/get-started.js`** â the budgets now live in one frozen map, `WC_FALLBACK_TIMEOUT_MS` (`sdk_init: 60000`, `connect: 45000`), used by both `_wcWithTimeout` call sites. The friendly error copy derives its seconds via `Math.round(WC_FALLBACK_TIMEOUT_MS.<stage> / 1000)`, so the message can never drift from the real budget. **Raise these, never shrink them** â they only bound the pre-QR stages; the user-driven scan/approve wait (the 120s session poll) is unbounded and must stay that way.
- **`_wcSetLoading`** swaps the loader sub-line to "Still working â slow connections can take a little longerâ¦" after 12s so the extended wait reads as progress, not a freeze. The notice timer is stored on the loader node (not module scope) so the helper stays self-contained â the node harness in `tests/test_homepage_theme_signup_content.py` executes `_wcSetLoading` standalone.
- Tests: `tests/test_walletconnect_timeout_content.py` (8 tests, no deps â constants centralised + raised, call sites use them, error copy derived, the 120s scan poll still unbounded, loader reassurance, plus a **node harness** over the REAL `_wcWithTimeout` proving a slow-but-successful stage resolves while an unsettled stage rejects with the stage tag and the budget is honoured, not fired early). Run with `python -m pytest tests/test_walletconnect_timeout_content.py`.


## Wallet Settings "Login Method" label Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ "WalletConnect", not "GoodMarket" (2026-10)
The wallet Settings modal's **Login Method** row rendered a WalletConnect login as **"GoodMarket"** (`{{ 'GoodMarket' if login_method == 'walletconnect' else ... }}` in `templates/wallet.html`). Users who tapped **Connect GoodWallet** (the WalletConnect flow) therefore saw "GoodMarket" in their profile, which reads like the in-app (email + PIN) wallet Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a DIFFERENT account and signer. The row must describe the signer the user actually connected with, so it now renders **"WalletConnect"** for every WalletConnect-family login method and humanises the rest.
- **Mapping:** `'WalletConnect' if _lm in ['walletconnect', 'manual', 'manual_address']` (the legacy `manual`/`manual_address` methods are WalletConnect-family Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `wc-bridge.js`/`wallet-main.js` treat all three as the WC session), else `(_lm | replace('_', ' ') | title)`; `_lm` is `(login_method or '')|lower` so a case-variant session still resolves.
- **Do not** label `walletconnect` as "GoodMarket" again. "GoodMarket" describes the in-app wallet (that is what the private-key section is gated on: `login_method == 'local'`).
- Tests: `tests/test_wallet_login_method_label_content.py` (10 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ drives the REAL Jinja template for each login method and pins the label, plus a case-insensitivity guard). Run with `python -m pytest tests/test_wallet_login_method_label_content.py`.

## Injected-wallet connect â no more "Add token?" spam (2026-10)
Connecting an injected wallet (MetaMask) used to fire `wallet_watchAsset` (EIP-747) during the login chain, so users saw an "Add token?" dialog stacked between the Celo chain switch and the login signature â the reported **"parang spamming"**. Root cause was two defects in OUR code, not "just MetaMask": (1) `requestGoodDollarToken()`'s `WATCH_FLAG` (`gmWatchAssetAsked_v1`) had no in-flight reservation, so two concurrent connects both fired watchAsset; (2) `connectInjectedWallet()` had no re-entrancy guard, so a double-tap started a SECOND serial chain (repeat chain prompt + login signature).
- **P4 â the login path never suggests a token.** `static/js/get-started.js` no longer contains `wallet_watchAsset` / `requestGoodDollarToken` / `GOODDOLLAR_TOKEN` at all. The login chain is now accounts â Celo chain â signature only. **There is no EIP-1193 method to ask whether a token is already tracked and `wallet_watchAsset` ALWAYS prompts**, so any auto-suggest on connect adds an unavoidable dialog â never re-add it to a login flow.
- **P1 â `connectInjectedWallet()` is re-entrancy locked.** New module-scope `let _injectedConnecting = false;` with `if (_injectedConnecting) return;`, the `#injectedWalletBtn` disabled on entry, and a `try { â¦ } finally { _injectedConnecting = false; connectBtn.disabled = false; }` so a failure never leaves a dead button. Mirrors `connectWalletConnect()`.
- **G$ is offered ONCE, explicitly, from the wallet Settings modal** via `window.addGdTokenToWallet()` (`static/js/wallet-main.js`) + `GD_WATCH_ASSET`. The section is **built in JS** (`_ensureGdWatchSection`, appended after the `#gdWatchAnchor` Login-Method block) â NOT in `wallet.html`, which is served no-cache and sits on a hard 200 KB budget (`test_wallet_load_perf_content.py::test_template_stays_small_after_extraction`). Only the tiny `#gdWatchAnchor` div is in the template. Gated on `login_method not in ('local', '')` â the in-app wallet has no external token list (a local login is a friendly no-op), and a guest has nothing to sign with.
- Failure copy is honest: a declined/unsupported wallet shows "not added", a throw shows the wallet's own message + a manual-add hint, and the button is always re-enabled in `finally`.
- Tests: `tests/test_wallet_add_gd_token_content.py` (8 tests, no deps â content wiring + login gating + the 200 KB budget guard, plus a **node harness over the REAL `addGdTokenToWallet`** for injected success / decline / throw / local no-op) and `tests/test_desktop_injected_signing_content.py` (`test_node_double_tap_starts_only_one_connect_chain` â a node harness driving the REAL `connectInjectedWallet` for a same-tick double-tap, asserting exactly one `eth_requestAccounts` + one login signature, the button re-enabled, and the lock released for a later retry; mutation-verified). Updated the now-obsolete watch-asset assertions in `tests/test_injected_celo_network_setup_content.py` + `tests/test_desktop_injected_signing_content.py`. Proposal: `docs/INJECTED_CONNECT_ADD_TOKEN_SPAM_PROPOSAL.md`.

## Wallet modal option label Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ "Connect GoodWallet", not "WalletConnect" (2026-09)
The Get Started modal's third option (`#walletConnectPrimaryButton`) is labelled **"Connect GoodWallet"** (Ð âÐ£ÒÐ âÐ£ÐÐ âÐ¢Â¶Ð âÐ¢Ð) with the desc "Already using GoodWallet for G$? Scan the QR or copy the URI link to connect it" Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ most G$ users already hold GoodWallet, so naming the wallet beats naming the protocol. The `onclick` is now `openGoodWalletConnect()` Ð²Òâ see the one-tap section below Ð²Òâ and the panel/QR internals keep their technical "WalletConnect QR" wording for users already inside that flow. The banner's GoodWallet tab step now reads `Choose <strong>Connect GoodWallet</strong>`. **When renaming any modal option, update `_wallet_guide_banner.html`'s matching step in lockstep.** Locked in by `test_walletconnect_option_reads_connect_goodwallet` + `test_wallet_guide_banner_names_connect_goodwallet`.

## Wallet page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ mobile bottom bar vs desktop rail (2026-10)
The wallet nav was a **left vertical rail at every width**. On a 360px phone that rail reserved a permanent 104px (~29%) gutter, rendered **27.8px** rows with **~7px** labels (both far below Apple's 44pt / Material's 48dp), and once the viewport got short it **scrolled and hid destinations** (it needed ~590px of height before items stayed on screen). `html { zoom: 0.7 }` was applied unconditionally, so every tap target and label was scaled down with the page.
- **The nav is ONE element carrying BOTH classes** (`<nav class="wallet-bottom-nav wallet-side-nav">`). It is a bottom bar below 900px and the rail at/above 900px. **Every rail rule must live inside `@media (min-width: 900px)`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ an unscoped `.wallet-side-nav { position:fixed; width:156px }` re-creates the gutter on a phone even though the element is the mobile bar. The mobile hide rule must target a **marker class** (`.wallet-side-nav .bn-desktop-only { display:none }`), never the nav element itself.
- **`zoom: 0.7` is desktop-only** (`@media (min-width: 900px)`, with the Firefox `<126` `transform: scale(0.7)` fallback inside it). Media queries evaluate against the **real** viewport, so this is a true gate. Removing the zoom on mobile makes the page ~61% taller but produces **no horizontal overflow** (verified at 360/390: zero elements wider than the viewport, no clipped text).
- **Mobile bar = 5 or 6 items**: Wallet, Claim G$, Swap, Earn, More, plus the flag-gated **Connect** (dApp-connect's "must stay a primary nav entry" contract). The bar is **`display:flex`** (not `repeat(5,1fr)`), with `.bn-item { flex: 1 1 0; min-width: 0 }`, so it distributes evenly at 5 OR 6 items and still fits at 320px with no clipping. Measured after the change: **62.8px** targets, **30px** icons, **11.5px** labels, no scroll, **0px** gutter.
- **Desktop rail (Ð ÐÐ¢ÒÐ¢Ò³900px)**: 156px wide, `flex-direction: column`, 48px rows, body `padding-left: 156px`. The rail intentionally keeps the 0.7 zoom Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ it is mouse-driven, so WCAG 2.5.8's 24px floor still clears (48 Ð âÐ²Òâ 0.7 = 33.6px).
- **Rail-only destinations are tagged `bn-desktop-only`** (Profile, Bridge, XDC Wallet, Savings, Send, Receive, Services). **A pruned rail item must keep a sheet home or it becomes unreachable.** `bnSheetWallet` was **DEAD markup** (no caller anywhere) yet several rail-only actions (Send / Bridge / XDC Wallet) existed *only* there.
- **The Wallet chip opens the money-hub sheet, it is NOT a link to `/wallet`.** The nav renders only in `wallet.html`, so `href="/wallet"` + `aria-current="page"` was a dead tap that reloaded the page the user was already on. It is now a `<button onclick="openBottomSheet('bnSheetWallet')">` (with `aria-haspopup="dialog"` + `aria-controls`), carrying the Send / GoodSwap / Bridge / Stream G$ / XSwap hub, and the duplicate **"GoodMarket Wallet" row was removed from the More sheet** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ one entry point, in the primary bar. `openBottomSheet()` closes any open sheet first, so tapping it from the More sheet replaces that sheet instead of stacking. **Do not re-add `href="/wallet"` to the nav or a second "GoodMarket Wallet" row to `bnSheetMenu`.**
- **Floating launchers must not branch on the rail class.** `ai-agent.js` / `chatroom-widget.js` computed `classList.contains('wallet-side-nav') ? 24 : height + gap` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ always true in markup, so on mobile they reserved only 24px and the launcher covered the ~78px bar. Both now measure geometry (`isBottomBar = nav.getBoundingClientRect().bottom >= window.innerHeight - 2`) and reserve `height + gap` on mobile, 24px on desktop. **When the nav layout changes again, keep the offset measured, not class-based.**
- Proposal + full measurement record: `docs/WALLET_MOBILE_NAV_PROPOSAL.md`.
- Tests: `tests/test_wallet_mobile_nav_content.py` (9 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ desktop-scoping, marker-class hiding, flex sizing for 5-or-6, tap-target/label floors, rail geometry, reachability of every hidden destination, the revived wallet sheet, the Wallet chip not self-linking, and the geometry-not-class launcher rule). Also updated `test_wallet_claim_navigation_content.py`, `test_ai_agent_launcher_overlap_content.py` and `test_gcash_cashout_content.py`.

### Nav chips were mostly UNCOLOURED Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ two half-wired variable systems (2026-10)
The "make the buttons bold and colourful" report was a **real bug**, not a taste issue. The template carried **two independent, half-wired** per-destination colour systems:
- `--nav-c` / `--nav-bg` (a faint `rgba(hue, 0.12)` wash) were declared on only **4** destinations (send/receive/earn/more) and read by `.bn-item .bn-icon` + `.bn-item .bn-label`.
- `--nav-g1` / `--nav-g2` (the solid gradient) were declared on **9** destinations Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ but **NO rule ever read them**. That whole block was dead.

So the four chips that render on a phone but had no `--nav-c` (**Wallet, Claim G$, Swap, Connect**) fell back to the default icon colour Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ uncoloured grey discs. This also silently **reverted** the earlier "Bottom-nav chips must be SOLID, not alpha washes" fix (above): the `--nav-bg` alpha wash was what actually rendered.
- **Fix:** ONE system. Every destination (all 13) declares the full quadruple `--nav-g1/--nav-g2/--nav-glyph/--nav-c`; `.bn-item .bn-icon` paints `linear-gradient(135deg, var(--nav-g1), var(--nav-g2))` with a bold glyph, and `.bn-item .bn-label` is `font-weight: 800` in `--nav-c`. The duplicate dead gradient block further down was removed; the vars are declared **once** and apply to both the mobile bar and the desktop rail.
- **`--nav-glyph` exists because a white glyph does not work on every chip.** The gold/amber chips (Claim, Earn) carry **dark** `#1f1300`, matching the G$-gold primary-button rule; everything else is white. Locked by a measured WCAG 1.4.11 check (Ð ÐÐ¢ÒÐ¢Ò³3:1) against BOTH gradient stops.
- **`--nav-c` is a dark ink of the hue, never the raw gradient stop.** `#10b981`/`#f59e0b`/`#f97316` are all below 4.5:1 on the light bar, so Wallet uses `#0f766e`, Claim/Earn `#92400e`, Swap `#4338ca`. Locked by a measured Ð ÐÐ¢ÒÐ¢Ò³4.5:1 label check.
- **Do not re-introduce a second colour system or a per-destination `--nav-bg`.** If a new destination is added, give it all four tokens or it will render grey.
- Tests: `tests/test_wallet_nav_button_color_content.py` (12 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ dead-variable guard, solid-not-alpha guard, all 13 destinations complete, dark-ink label rule, measured glyph Ð ÐÐ¢ÒÐ¢Ò³3:1 on both stops, measured label Ð ÐÐ¢ÒÐ¢Ò³4.5:1 on the composited bar, and the 6 mobile chips specifically).

## Wallet page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ modal stacking / hidden-modal dead buttons (2026-08)
All `.modal-overlay` modals in `templates/wallet.html` share `z-index: 200`, and the bottom nav (`.wallet-bottom-nav`) is `z-index: 1200` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so the nav stays tappable while a modal is open, and a second modal triggered from it (via `bnGo` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `openModal`) stacks by DOM order. If the new modal is EARLIER in the DOM than the open one (e.g. `sendModal` vs `gcashModal`), it opens invisibly behind it Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ dead button, zero feedback. Fix: `openModal(id)` closes any other open `.modal-overlay` first, EXCEPT `lwUnlockModal` (the PIN prompt, z-index 100000, stacked deliberately over the triggering modal Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ closing it would strand the awaiting sign flow). New modals opened on top of an existing flow must bypass `openModal` (direct `classList.add('open')`), like `lwUnlockModal` does. Tests: `tests/test_wallet_modal_stacking_content.py` (no deps).

## Swap page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ load performance: inline JS extracted to bundles (2026-09)
`templates/swap.html` was **365 KB**, of which the main inline `<script>` was **~293 KB** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ served inside the `no-cache, no-store` HTML (`main.py` `_add_cache_headers`), so every visit (not just the first) re-downloaded and re-parsed it. That, not the file layout, is why /swap felt slow. It also ran a **page-load Bridge pre-warm** (`loadXdcGdBalance` + `loadXdcNativeBalance` + `estimateBridgeFeeXdcToCelo` + `_warmUpXdcWalletProvider`) for EVERY visitor, including the majority who never open the Bridge tab.
- **Same playbook as wallet.html** (see the wallet load-performance section): the inline JS is now three versioned `/static/` bundles, loaded via `url_for(..., v=ASSET_VERSION)` so the 1-year immutable cache applies:
  - `static/js/swap-core.js` (~128 KB) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ plumbing (EIP-6963 discovery, WC bridge `_wc*`, `_celoJsonRpc`, `_enrichSwapError`, MiniPay fee-retry), signer resolution (`getConnectedSwapSigner`), GoodSwap/Uniswap V3 (`startSwap`, `fetchQuote`) and Fuse/Voltage (`startFuseSwap`).
  - `static/js/swap-reserve.js` (~42 KB) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ GoodReserve (Mento) buy/sell **plus the tab switchers** (`setSwapTab` / `setGoodSwapSubTab` / `setBridgeSubTab`, whose callbacks reach into both other bundles) and the reserve pause probe.
  - `static/js/swap-bridge.js` (~88 KB) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Bridge tab: CeloÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶XDC and XDCÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶Celo legs (`bridgeCeloToXdc`, `bridgeXdcToCelo`, `_checkCeloDestinationBridge`, `_xdcReadProvider`, `_warmUpXdcWalletProvider`).
- **Per-request values via `window.GM_SWAP_BOOT`**, an inline `{...|tojson}` object right before the bundles: `wallet`, `loginMethod`, `walletConnectProjectId`, `assetVersion`, `wcBundleUrl`, `fuseChainId/fuseRpc/fuseGdToken/fuseGdDecimals/fuseWfuse`, `voltageRouter`, `bridgeContract`, `celoGdTokenContract`, `xdcGdTokenContract`, `xdcChainId`, `celoChainId`. **Jinja cannot run inside a static asset** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ any new per-request value must be added to the boot object (the render test fails on leftover `{{ }}` in a bundle). swap.html is now ~71 KB (was ~365 KB).
- **Cross-bundle calls MUST be `typeof`-guarded.** Load order is core Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ reserve Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ bridge; a call from an earlier bundle into a later one is a runtime `ReferenceError` if that bundle fails to load (CDN hiccup, stale cache, blocked request), and the throw **aborts the caller**. Concretely: `setSwapTab('bridge')` lives in swap-reserve.js and calls `updateCeloBridgeBalanceDisplay()` / `_prewarmXdcBridgeTab()` / `updateBridgeFromBalanceXdcToCelo()` in swap-bridge.js Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ unguarded, a missing bridge bundle left the Bridge tab **not switching at all** (`ReferenceError: updateCeloBridgeBalanceDisplay is not defined`). In the original single inline block every one of these was a same-scope declaration, so the split *introduced* this hazard. Guard the same statement (`if (typeof fn === "function") fn();` or a wrapping `try`). Locked in by `test_no_unguarded_cross_bundle_calls` (mechanical scan) and `test_bridge_tab_still_switches_when_the_bridge_bundle_fails_to_load` (loads core+reserve only and asserts the tab still switches).
- **Bridge pre-warm is now LAZY and once-only.** `_prewarmXdcBridgeTab()` (in swap-bridge.js, exposed as `window._prewarmXdcBridgeTab`, guarded by `_xdcBridgePrewarmed`) replaced the eager `DOMContentLoaded` block; `setSwapTab('bridge')` and `setBridgeSubTab('xdc_to_celo')` call it the first time each is opened. **Do not re-add a page-load XDC pre-warm** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ it fires three API/RPC reads + a wallet warm-up for users who never touch the Bridge tab.
- **Bridge deep-link "focused mode" already exists and is correct** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ do not re-implement it. `autoSelectSwapTab()` (swap-reserve.js, runs at load) reads `?tab=` / `?pane=` / `?dir=`; for an explicit section it calls `setSwapTab`/`setGoodSwapSubTab`/`setBridgeSubTab` and then **hides `#swapTabSwitcher` and `display:none`s the inactive top pane**, so a Bridge entry point shows ONLY the bridge UI (no GoodSwap/Uniswap/GoodReserve tabs). All three entry points use it: wallet bottom-sheet `bnGo` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `/swap?tab=bridge`, and xdc_wallet.html Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `/swap?tab=bridge&dir=xdc_to_celo|celo_to_xdc`. Verified in a real browser: `?tab=bridge` and `?tab=bridge&dir=xdc_to_celo` render only the bridge sub-tabs. **Bare `/swap` deliberately keeps both top tabs** (bookmarks/power users). In-page tab clicks do NOT sync the URL (no `pushState`/`replaceState` in any swap bundle) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ that is intentional, but it means a bare-/swap user who taps Bridge and then reloads lands back on GoodSwap. If deep-linking from an in-page tab click is ever wanted, add `history.replaceState` to `setSwapTab`.
- **Content tests must read the effective page**, not the template: `tests/swap_page_src.py` (`append_swap_bundles` / `read_swap_page`), mirroring `wallet_page_src.py`. Tests updated to route `templates/swap.html` through it: `test_goodreserve_sell_gate_content.py`, `test_revert_data_handling.py`, `test_local_wallet_content.py` (`_page`), `test_local_wallet_signing_content.py` (`_tpl`), `test_ai_agent_bridge_xdc_swap_content.py` (`_read`). **When adding a swap bundle, append it to `SWAP_EXTRACTED_JS` there.**
- **Extraction safety net:** the refactor was verified line-by-line (`git show HEAD:templates/swap.html` vs the bundles) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ this caught a real gap where `_ensureReservePauseProbe()` + `window._checkReservePaused` (13 lines between two slice boundaries) were silently dropped. **When slicing a big block, diff every original non-empty line against the union of the outputs**; a boundary off-by-one loses code without a syntax error, and the dropped function is only referenced from another bundle (so a content test that greps the concatenated page still finds the *call*).
- Tests: `tests/test_swap_load_perf_content.py` (12 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ size budget, no-Jinja-in-bundles, load order, boot-key completeness, lazy pre-warm, unguarded-cross-bundle scan, helper). It also drives `scripts/verify_swap_render.py` (Jinja render + `node` parse of the boot object), `scripts/smoke_swap_bundles.cjs` (executes the REAL bundles under DOM stubs to catch load-time throws and assert the pre-warm stays lazy) and `scripts/smoke_swap_degraded_load.cjs` (loads core+reserve only and asserts the Bridge tab still switches) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ all skip cleanly when `node` is absent. Run with `python -m pytest tests/test_swap_load_perf_content.py`.

## Swap page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Uniswap fee tiers + real price impact (2026-10)
Users reported "low liquidity" rates on G$/USDT, G$/cUSD and G$/CELO while app.uniswap.org showed a fair rate for the same pairs. Verified live against Celo mainnet: the app only queried **three** Uniswap V3 fee tiers, missing the **0.01% tier (fee = 100)** Ð²Òâ which is exactly where the deep Celo pools live. Full investigation + numbers: `docs/UNISWAP_LIQUIDITY_ROUTING_PROPOSAL.md`.
- **`FEE_TIERS` must include `100`.** `static/js/swap-core.js` used `[10000, 3000, 500]`; the deep pools are CELO/USDT **0.01%** (886,834 CELO / 125,033 USDT), CUSD/USDT **0.01%** (628,638 USDT / 15,541 cUSD) and CELO/CUSD **0.01%** (26,440 CELO / 3,936 cUSD). The G$ pools are only 1% and thin on the counter-asset side (GD/CUSD: 8.5M G$ but ~475 cUSD; GD/CELO: ~1M G$ but ~84 CELO; **GD/USDT: empty**), so without fee=100 a G$Ð²Ò¶âUSDT quote is throttled by the ~475 cUSD in the 1% GD/cUSD pool. Now `[100, 500, 3000, 10000]`, and `HOP_FEES = FEE_TIERS` (the old hardcoded hop list had the same omission). `wallet-main.js` `AI_SWAP_FEE_TIERS` mirrors it Ð²Òâ **keep the two in sync**. Measured gain: G$Ð²Ò¶âUSDT 1k/10k/100k went 0.1057Ð²Ò¶â0.1101 (+4.1%) / 0.7943Ð²Ò¶â1.1005 (+38.6%) / 2.2765Ð²Ò¶â10.9852 (+382.5%); USDTÐ²Ò¶âCELO +12.2%; CUSDÐ²Ò¶âUSDT +3,484%.
- **`quotePriceImpact` is measured, not hardcoded.** It was `"< 1%"` for every quote, so a bad rate was invisible. `_computePriceImpactPct(quoter, from, to, amountIn, route, bestOut)` quotes a tiny slice of the SAME chosen route (`_quoteRouteOnQuoter`, single-hop or `quoteExactInput` multi-hop) and scales it up as the near-spot reference; impact = `(fair Ð²Ò²â actual) / fair`. Returns `null` when unmeasurable Ð²Ò¶â the UI shows a dash, never a misleading `< 1%`. `showQuote` colours it (`HIGH_IMPACT_WARN_PCT` 5 amber, `HIGH_IMPACT_BLOCK_PCT` 15 red).
- **High-impact guard in `startSwap`.** At/above the block threshold the user must explicitly confirm via `GMTxPreview.confirm` (with a `window.confirm` fallback), and the copy steers G$Ð²Ò¶âcUSD users to the deeper **GoodReserve** route. Early-return is safe because the `finally` resets `_swapInFlight`.
- **`alert-warning` was unstyled** in `templates/swap.html` (both theme blocks) Ð²Òâ added it; `swap-reserve.js` already emitted that class.
- **Not fixed here (Phase 2, still open):** routes are still capped at 2 hops (Uniswap uses 3+), no split routes, and the G$ direct pools remain genuinely thin. For full Uniswap parity adopt their router (Trading API server-side, or `@uniswap/smart-order-router`).
- **Rule:** never drop fee tier `100` from `FEE_TIERS`/`HOP_FEES`/`AI_SWAP_FEE_TIERS`, and never hardcode the price impact again. Tests: `tests/test_swap_liquidity_routing_content.py` (6 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ fee-tier presence/order in both bundles, HOP_FEES reuse, no hardcoded impact, warn+confirm wiring, plus a **node harness** running the REAL `_quoteRouteOnQuoter`/`_computePriceImpactPct` against a stubbed Quoter for 0%/50%/null/25%/clamp). Run with `python -m pytest tests/test_swap_liquidity_routing_content.py`.

## Swap page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ deep-link tab focus resolved SERVER-side (2026-10)
A `?tab=bridge` deep link used to paint GoodSwap (Uniswap V3 + GoodReserve + the `[GoodSwap | Bridge]` switcher) for a visible beat before collapsing to Bridge Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the reported "nakikita parin ang goodswap". The FINAL state was already correct; the bug was a **flash of the wrong content** (and a permanent wrong state if `swap-reserve.js` failed to load).
- **Root cause:** `templates/swap.html` hardcodes GoodSwap visible (`#swapPaneGoodSwap` no `hidden-tab`, `#swapPaneBridge` `hidden-tab`, `#swapTabSwitcher` visible), and the only thing that fixed it on load was `autoSelectSwapTab()` in `static/js/swap-reserve.js` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a 43 KB bundle loaded near the END of `<body>` (after the ethers CDN script). Deep links are a primary path (wallet bottom nav `bn-swap`/`bn-bridge`, xdc_wallet Ð²Ò¶â `/swap?tab=bridge&dir=Ð²ÒÒ°`), so the correct tab must be in the HTML that arrives, not decided after hydration.
- **Fix Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `swap_tab_state.py`** (new, root, dependency-free): `resolve_swap_tab_state(args)` maps `?tab=`/`?pane=`/`?dir=` Ð²Ò¶â `{tab, goodswap_pane, bridge_dir, focused}`. `routes.py::swap_page()` calls it (`resolve_swap_tab_state(request.args)`), applies the render-time fallbacks (`reserve` Ð²Ò¶â `dex` when `reserve_swap_visible` is false; `celo_to_base` Ð²Ò¶â `celo_to_xdc` when LI.FI is off), and passes `swap_tab` / `swap_goodswap_pane` / `swap_bridge_dir` / `swap_focused` to the template. The template now emits the correct `hidden-tab` / `active` / `aria-selected` / `style="display:none"` on first paint.
- **The alias tables in `swap_tab_state.py` MUST stay in sync with `autoSelectSwapTab()`** (`BRIDGE_TAB_ALIASES`/`DEX_TAB_ALIASES`/`CELO_TO_XDC_DIR_ALIASES`/Ð ÐÐ¢âÐ¢Â° mirror the JS literals). A parity test pins every alias on both sides. Unrecognized params leave the client `target`/`pane` unset, so the server also returns `focused: false` for them (do not widen this to `bool(pane)`).
- **Keep `autoSelectSwapTab()`.** It still runs (identical result) and remains the ONLY handler for HASH links (`#bridge`), because the browser never sends the fragment to the server. Its comment now points at the server resolver.
- **LIFI flag read once:** `lifi_enabled = _lifi_bridge_enabled()` feeds BOTH the boot value (`lifi_bridge_enabled=lifi_enabled`) and the tab fallback Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `test_lifi_bridge_content.py::test_swap_page_passes_lifi_boot_values` was updated accordingly.
- Tests: `tests/test_swap_tab_focus_content.py` (21 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ functional drive of the resolver for every alias/fallback/unknown/case, clientÐ ÐÐ¢Â¶Ð²ÒÒserver alias parity, and Jinja renders asserting the exact pane classes for bare/goodswap/bridge/dir/reserve, plus routes context wiring). Run with `python -m pytest tests/test_swap_tab_focus_content.py`. Proposal/rationale: `docs/SWAP_TAB_SERVER_SIDE_FOCUS_PROPOSAL.md`.

## Swap page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ rename "GoodSwap" Ð ÐÐ¢Â¶Ð²ÒÒ· "Swap" + copy simplification (2026-10)
The top tab was named **"GoodSwap"** Ð ÐÐ¢âÐ²ÒÒ an invented `Good*` product name that says nothing to a new user (and collides with an unrelated DeFi project + reads like an official GoodDollar product, against the branding rule above). The page also carried **~250 words of always-visible explainer copy** for a 3-tap task. Both fixed; proposal `docs/SWAP_RENAME_AND_COPY_SIMPLIFICATION_PROPOSAL.md`.
- **Labels (display only):** page `<title>` Ð ÐÐ¢Â¶Ð²ÒÒ· `Swap &amp; Bridge - GoodMarket`, `<h1>` Ð ÐÐ¢Â¶Ð²ÒÒ· `Ð¡ÒÐ¢âÐ²ÒÒ·Ð¢Ò± Swap &amp; Bridge`, subtitle Ð ÐÐ¢Â¶Ð²ÒÒ· "Swap G$ and other tokens, or bridge across chains". Top tabs `[ Swap ] [ Bridge ]`. Sub-tabs Ð ÐÐ¢Â¶Ð²ÒÒ· **"Swap any token"** / **"Buy / Sell G$"** (was `Uniswap V3` / `GoodReserve` Ð ÐÐ¢âÐ²ÒÒ protocol-vs-protocol, no user meaning; the new labels mirror the "Buy G$ with cUSD" / "Sell G$ for cUSD" buttons). Wallet bottom-sheet row Ð ÐÐ¢Â¶Ð²ÒÒ· `Swap` (+ toast "Opening SwapÐ ÐÐ¢âÐ¢Â°"); `xdc_wallet.html` badge Ð ÐÐ¢Â¶Ð²ÒÒ· "Moved to Swap".
- **INTERNAL KEYS ARE UNCHANGED Ð ÐÐ¢âÐ²ÒÒ the deep-link contract.** `?tab=goodswap`, `setSwapTab('goodswap')`, `tabBtnGoodSwap`, `swapPaneGoodSwap`, `setGoodSwapSubTab`, `.goodswap-sub-tab-btn` all stay, so every bookmark, wallet-nav target (`/swap?tab=goodswap`), xdc_wallet redirect and `swap_tab_state.py` alias keeps working. **Never rename these when re-labelling.**
- **Copy: progressive disclosure.** Reference/marketing detail moved behind a collapsed `<details class="swap-more"><summary>How it works|How the bridge works</summary>` (CSS in the base block + light-theme override). The **actionable** warnings stay visible: bridge gas/minimum lines (min 2,000 G$, Ð ÐÐ¢ÒÐ¢Ò³0.20 CELO / Ð ÐÐ¢ÒÐ¢Ò³2.1 XDC for gas) and the conditional `#routeTip`. Removed the obsolete "You need a Web3 wallet (MetaMask, MiniPayÐ ÐÐ¢âÐ¢Â°)" sentence (the in-app wallet is the main path). Visible info-card copy went **~250 Ð ÐÐ¢Â¶Ð²ÒÒ· ~50 words**; the detail is still one tap away.
- **Rules:** the gas/minimum warnings must never be collapsed Ð ÐÐ¢âÐ²ÒÒ they prevent real failed txs. Sub-tab ids/classes/params never change with a label change.
- Tests: `tests/test_swap_rename_and_copy_content.py` (11 tests, no deps Ð ÐÐ¢âÐ²ÒÒ new labels, internal-keys-unchanged, no visible `GoodSwap` label, reference copy collapsed, actionable warnings still visible). Updated `tests/test_wallet_mobile_nav_content.py` (sheet label) and `tests/test_lifi_bridge_content.py` ("2 confirmations" wording). Run with `python -m pytest tests/test_swap_rename_and_copy_content.py`.

## Wallet page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ load performance (2026-08)
Wallet page slowness was NOT the animations or dead code per se Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the two big costs were: (1) the **Privy SDK** (React + `@privy-io/react-auth` via esm.sh module-graph waterfall, several hundred KB of 3rd-party requests) loading on EVERY page load even though every consumer guards on `IS_PRIVY_LOGIN`, and (2) the ~500 KB `wallet.html` (~88 KB CSS + ~345 KB inline JS) being served `no-cache, no-store` (see `main.py` `_add_cache_headers`) so it is re-downloaded and re-parsed on every visit Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ heavy for low-end phones.
- **Privy SDK is now Jinja-gated**: `{% if (login_method or '')|lower == 'privy' %}` wraps `privyWalletMount` + the `<script type="module">` esm.sh imports in wallet.html. All `GMPrivy*` consumers (`_walletGetPrivyProviderIfPreferred` etc.) fast-path on `IS_PRIVY_LOGIN`, so non-Privy sessions never miss the globals. If a NEW Privy consumer is added, it must guard the same way.
- **Inline JS extracted to versioned static bundles (immutable-cached):** the five inline `<script>` blocks (~345 KB) now live in `static/js/wallet-main.js` (270 KB, main logic), `wallet-streams.js` (Superfluid P2P), `wallet-voucher.js` (daily voucher), `wallet-lazyload.js` (idle ethers/QR preload), `wallet-bottomnav.js` (bottom sheets), all loaded via `url_for(..., v=ASSET_VERSION)` so the 1-year immutable `/static/` cache applies. Per-request Jinja values (`wallet`, `login_method`, `gd_token_address`, `raffle_contract_address`, `walletconnect_project_id`, `ASSET_VERSION`) are passed through a tiny inline `window.GM_WALLET_BOOT = {...|tojson}` object right before `wallet-main.js`. wallet.html is now ~152 KB (was ~505 KB); repeat visits only re-download that, not the JS. **Never re-inline big scripts into wallet.html** (a size-budget test guards this); add new wallet JS to the appropriate bundle. Content tests read the effective page via `tests/wallet_page_src.py` (`append_wallet_bundles`/`read_wallet_page`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ if a new bundle is added, append it to `WALLET_EXTRACTED_JS` there.
- **Paint-heavy animations fixed:** never animate `box-shadow` or `left`/`top` in `@keyframes` (full repaint/layout every frame). `claimPulse`/`claimIconPulse`/`voucherPulse`/`ubiClaimEmberPulse`/`heroOrbPulse` were replaced with opacity-only pseudo-element glows (`ubiClaimGlow` on `#ubiClaimBtn.is-hot::before`, `voucherGlow`) or removed; the voucher shine sweep uses `translateX` (475% of the 40%-wide strip == old -60%Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶130% left travel). Remaining cheap loops: `spin`, `heroOrbHalo`, `ubiClaimSheen` (background-position shimmer on the small claim button Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ deliberate), `ubiClaimGlow`, `ubiClaimSparkTwinkle`. A `prefers-reduced-motion: reduce` block (two places: after the claim hot-state CSS and at the end of the Dark Purple override) disables all decorative loops (spinners keep spinning Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ they signal in-flight work).
- **Dead code removed:** `FUSE_PRIVY_CHAIN` (never in `GM_PRIVY_SUPPORTED_CHAINS`), the `loadFuseBalances` no-op stub + its 3 call sites (Fuse is no longer a displayed token; `fuseBal`/`fuseGdBal` stay 0 for the still-reachable `/api/fuse/prepare-send` path), and 264 cascade-dead CSS declarations (~12 KB, mostly the pre-override light-theme colors Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ e.g. the entire Soft Sunrise `:root` light variable block) that the Dark Purple override re-declares with the same selector+property+at-rule context. The dedup was done with a verified one-off script (tinycss2; winner-per-key cascade equality check) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ only exact same-selector/same-property/same-context duplicates with no `!important` were removed, so layout-only base rules remain. When adding override styles, prefer editing the surviving base rule if it's the ONLY remaining definition (the override section is for re-skins, not duplicates).
- Tests: `tests/test_wallet_load_perf_content.py` (no deps).

## Wallet page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ dead CSS/JS removal (2026-09)
A follow-up sweep (same methodology as the homepage cleanup, PR #342) removed wallet-page code that no markup, script, include or test referenced. What was dead, and the guards that keep it out:
- **CSS families removed** (base rules AND their Dark Purple override rules Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a partial removal leaves the override styling non-existent markup): `private-key-export-card` (+ `.warning-title`/`.warning-copy`/`.confirm-phrase`/`.export-status`) and the old `.savings-card`/`.savings-row`/`.savings-label`/`.savings-value`/`.savings-sub`/`.savings-btn` panel. The private-key *export card* (a copy-the-secret panel) was removed from the product; `tests/test_wallet_safety_content.py` asserts the wallet does not offer secret export.

- **`.pk-warning` / `.pk-box` are NOT dead** (restored 2026-09). This sweep deleted their base rules as "unused", but the Settings Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ "reveal private key" flow (PR #338, see the private-key reveal section) re-uses the same family. Its `#pkModal .pk-box { display: block }` override only wins because the base rule is `display: none` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ deleting the base silently hides the revealed key with no error. `test_pk_warning_and_pk_box_selectors_are_used` pins them live.
- **CSS variables removed:** `--serif`, `--sans`, `--blue-dim`, `--blue-glow`, `--green-dim` (defined in `:root`, never read by any `var()`). **`--bg` / `--bg2` look unused by the same measure but MUST stay** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `test_wallet_dark_purple_theme_content.py` pins them as the canvas palette. Do not sweep them as "unused".
- **JS removed:** `SERVER_PRIVY_WALLET_CLIENT_TYPE` (assigned from the boot config, never read Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the privy client type was only ever consumed by the deleted private-key export card), `isMobile()` (no callers), `claimXdcServerSide()` (a stub that only threw "Server signing is disabled"), and `currentStreamInfo` (write-only stream state). The wallet's boot key `privyWalletClientType` went with them (template + `scripts/render_wallet_*.py` render args); the homepage still *sends* `privy_wallet_client_type` and `main.py` still stores it in the session, which is now unused on the wallet page.
- **NOT dead (do not remove):** `_vGetEthProvider()` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ it looks unused from `wallet-main.js` alone but `wallet-voucher.js` calls it (compatibility alias); `id="walletMaintenanceOverlay"` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ asserted by the protocol-maintenance test.
- **Method:** `tinycss2` parse of both `<style>` blocks, every class selector cross-referenced against markup + includes + bundles + the test suite, then a JS function/var scan with the same corpus. `test_wallet_dead_code_content.py` now asserts the removed selectors/symbols stay gone and includes a **live orphan-class check** (every class in the wallet's CSS must appear somewhere in the effective page) so the next dead family is caught mechanically rather than by eye.
- Tests: `tests/test_wallet_dead_code_content.py` (no deps). Run with `python -m pytest tests/test_wallet_dead_code_content.py`.

## Post-UBI-claim savings popup Ð²Òâ REMOVED (2026-10)
The wallet page used to open a `#savingsPopupModal` (iframe -> `/savings?from=wallet_claim`) 900 ms after a **confirmed Celo UBI claim** (`pollReceipt` success branch -> `openSavingsPopupAfterClaim()`). Users reported it as **too disruptive** ("sobrang disturbo"), so it was removed entirely:
- **Deleted:** the `openSavingsPopupAfterClaim()` / `closeSavingsPopup()` functions in `static/js/wallet-main.js`, the `setTimeout(openSavingsPopupAfterClaim, 900)` call, the `#savingsPopupModal` markup in `templates/wallet.html`, and the base + Dark Purple override `.savings-popup-*` CSS blocks.
- **The Celo claim success path is otherwise unchanged** Ð²Òâ it still does `logGoodMarketClaim(..., 'confirmed')`, `startCountdown()`, `loadBalances(true)`, `fetchEntitlement()` and the claim celebration. Only the savings prompt is gone.
- **Savings is still reachable** from the wallet bottom nav (`bn-savings` item + the Services sheet row) and `/savings` directly. Nothing else consumed the popup, the iframe id or the `from=wallet_claim` param.
- **Do not re-add a post-claim savings modal** without an explicit product decision. Guarded by `tests/test_wallet_dead_code_content.py::test_old_savings_card_selectors_are_gone` (now asserts `.savings-popup` is gone too) and `::test_wallet_page_css_has_no_orphan_classes`.


## Wallet page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Referral Program modal (2026-08)
The referral program (previously only on `/dashboard`) is now surfaced directly from the wallet page. A `Ð âÐ£ÒÐ âÐ£ÐÐ â¢Ð¡Ò³Ð âÐ¢Ñ Referral` entry sits in the bottom-nav **"More Ways To Earn G$"** sheet (`bnSheetEarn`), next to Dashboard/G$ Raffle, and opens a `referralModal` (`.modal-overlay`, DOM-ordered AFTER streamModal so it stacks above by the modal-stacking rule).
- **Source of truth:** the modal reuses the same `/api/referral/stats` endpoint the dashboard uses (`referral_service.get_referral_stats`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ link, invited/verified/G$-earned stats, and referral history. No backend change needed.
- **Implementation:** modal HTML lives in `templates/wallet.html`; the JS lives in `static/js/wallet-main.js` (`openReferralModal` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `openModal('referralModal')` + `loadWalletReferral()`, `_referralUrlOnCurrentOrigin` re-anchors the link to the current origin Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ same as dashboard's `referralLinkOnCurrentOrigin` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `copyWalletReferralLink`). Wallet-scoped DOM ids (`wlReferralLinkBox`, `wlReferralLinkInput`, `refStat*`, `referralHistoryList`) avoid colliding with the /dashboard ids (which live on a different page, but keep the `wl` prefix anyway).
- **Theme:** uses the Dark Purple CSS vars (`--text-dim`, `--card-border`, `--card-soft`), not light literals.
- Tests: `tests/test_wallet_referral_modal_content.py` (no deps). Run with `python -m pytest tests/test_wallet_referral_modal_content.py`.

## Wallet page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Daily Task modal (2026-08)
The Daily Task (Twitter/Telegram, 100 G$) feature Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ previously only on `/dashboard` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ is now surfaced directly from the wallet page, mirroring the Referral modal pattern. A `Ð âÐ£ÒÐ âÐ£ÐÐ â¢Ð¢Ò¡Ð ÐÐ²ÒâÐ²ÒÒ· Daily Task` entry sits in the bottom-nav **"More Ways To Earn G$"** sheet (`bnSheetEarn`) next to Referral, and opens a single `dailyTaskModal` (`.modal-overlay`, DOM-ordered after `referralModal` so it stacks above by the modal-stacking rule). The modal is ONLY reachable from that sheet entry (`openDailyTaskModal()` is the sole opener).
- **Single modal contains everything:** status banner (Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐ Ò² Available / Ð â¢Ð¢ÑÐ âÐ²ÒÒ°Ð ÐÐ²ÒÒÐ£Â® Under Review / Ð âÐ£ÒÐ âÐ£ÐÐ âÐÂ¶Ð âÐ¢Â¶ On Cooldown with next-claim time), Twitter/Telegram platform selection, message display + copy, post-URL submission, and the **transaction history section inside the same modal** (loaded on open via `/api/daily-task/history?limit=50`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ same UX as GCash/Referral showing history inline.
- **Source of truth:** reuses the exact `/api/daily-task/status`, `/api/daily-task/claim` and `/api/daily-task/history` endpoints the dashboard uses. No backend change needed.
- **Implementation:** modal HTML lives in `templates/wallet.html`; the JS lives in `static/js/wallet-main.js` (`openDailyTaskModal` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `openModal('dailyTaskModal')` + status fetch + `_wlDailyLoadHistory()`, `selectDailyTaskPlatform`, `loadDailyTaskMessage`, `copyDailyTaskMessage`, `submitWalletDailyTask`). Wallet-scoped DOM ids (`wlPlatformSelection`, `wlTaskSubmissionForm`, `wlTaskUrlInput`, `wlDailyTaskStatusBanner`, `wlDailyTaskHistoryList`, Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£) avoid colliding with the `/dashboard` daily-task ids (which live on a different page). The dashboard section itself is intentionally left intact (same as Referral).
- **Theme:** uses the Dark Purple CSS vars (`--text-dim`, `--card-border`, `--card-soft`), not light literals. Status colors are the dark set: `#34d399`/`#fcd34d`/`#f87171`.
- Tests: `tests/test_wallet_daily_task_modal_content.py` (no deps). Run with `python -m pytest tests/test_wallet_daily_task_modal_content.py`.

## UBI claim Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ per-network choice, no Celo dead-end (2026-09)
The claim modal shows a Celo card and an XDC card, but the flow was **hardcoded Celo-first with no user choice**: `pickRecommendedClaimNetwork()` returned `'celo'` whenever Celo was claimable, `doUbiClaim()` always used that recommendation, and the network cards were display-only `<div>`s (`claimNetworkStatusHtml` had no button). The Celo failure path only *told* the user to "check the network cards above" Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ where there was no control Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so a persistently failing Celo route (gas, RPC busy, reject) stranded the user with no way to reach XDC.
- **`selectedClaimNetwork`** (new, module-scoped in `wallet-main.js`) is an explicit user pick. `pickRecommendedClaimNetwork()` now evaluates it FIRST and keeps honouring it while that network is still claimable, then falls back to the unchanged Celo-first default. The Celo-first recommendation is deliberately preserved for users who have not chosen.
- **Per-network Claim buttons** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ each card renders a `claim-card-btn` (wired to `window.selectClaimNetwork(network)`, which selects the network, re-renders, and starts that network's claim in one tap) only when `canClaim && supported && !needsVerification`. Unsupported / unclaimable / FV-gated cards get no button, and the handler re-checks the same guards.
- **Failure fallback** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `_offerAlternateClaimNetwork(failedNetwork)` re-points the primary button at whichever other network is still claimable after a Celo *or* XDC failure. It **never broadcasts** itself (the user taps once to confirm); with no alternate it says so explicitly. This is what breaks the Celo dead-end loop.
- **Card copy** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the cards now surface the backend's `blocked_by`/`blocked_reason`, so an `celo_identity_verification` block reads "Verify Face ID first" instead of generic copy. Dynamic hint/status text is HTML-escaped (`_claimEsc`) because cards are injected via `innerHTML`.
- **The FV gate is unchanged and must stay** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `routes.py` anchors Face Verification on Celo Identity and zeroes `can_claim` on `celo`/`fuse`/`xdc` alike (`blocked_by = "celo_identity_verification"`). That is an on-chain requirement, NOT "claim Celo before XDC": once face-verified, XDC claimability is an independent `check_xdc_ubi_entitlement` read. Do not weaken this gate when touching the network choice.
- Tests: `tests/test_claim_network_choice_content.py` (17 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ selection precedence, per-network buttons, guard conditions, failure fallback, FV gate intact, CSS/copy wiring). Run with `python -m pytest tests/test_claim_network_choice_content.py`.

## UBI claim Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ unified Celo + XDC amount for local logins (2026-10)
For the in-app GoodMarket wallet (`login_method === 'local'`) the action was already unified (`doUbiClaim` submits Celo, then `_claimXdcAfterLocalCelo` submits XDC from the same PIN-unlocked provider), but the **display** still showed two network cards with two amounts and two `Claim on Celo` / `Claim on XDC` buttons Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so the UI said "two things" while one tap did both. Now the display matches the behaviour **for local logins only**.
- **`routes.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `_combined_claimable(claims)`** (new, module-level) sums only the legs that are actually claimable, and sets **`confident: False` when any leg's read failed (`success: False`) or is `is_available: False`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a failed read must NEVER be silently treated as `0`, which would show a smaller-than-real total. `/api/claim/availability` adds `"combined": {networks, count, total, total_formatted, confident}` (additive; every existing key unchanged).
- **`static/js/wallet-main.js` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `_unifiedLocalClaimTotal()`** returns the `combined` block ONLY when `_isUnifiedLocalUbiClaim()` (local + `GMLocalWallet`) AND `combined.confident` AND `combined.count >= 2`; otherwise `null` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the caller falls back to the per-network display.
- **Unified UI (local only):** `renderClaimNetworks()` renders ONE summary card (`claimUnifiedCardHtml`, `.claim-network-card.is-unified`, `Celo + XDC` + the split in the hint + the total) with **no per-network button**; the primary `#ubiClaimBtn` label becomes `Claim {total} G$ (Celo + XDC)` and `doUbiClaim()` keeps its existing CeloÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶XDC chain. The `is-hot` MutationObserver already matches `^\s*claim\s`, so no change there.
- **Injected / WalletConnect / Privy / MiniPay are untouched** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ for them a second network means a second wallet prompt, so they keep two cards + two `claim-card-btn`s (`window.selectClaimNetwork`). The unified branch is gated on local + 2 legs, so their UI is byte-for-byte the old one.
- **Partial state:** only one network claimable, or a non-confident read Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ falls back to the per-network path (`Claim {amount} G$ on Celo|XDC`), never a guessed sum.
- **The unified total is DISPLAY-ONLY** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐ¢Â¶ the real amounts are still each network's own `checkEntitlement()` at signing; never pass the label as a claim parameter. The FV gate, `_getEthProvider()` local return-null, PIN signing, Celo switch-back and XDC-failure isolation (`_claimXdcAfterLocalCelo` keeps a successful Celo claim a success when XDC fails) all stay.
- Proposal: `docs/UNIFIED_LOCAL_UBI_CLAIM_AMOUNT_PROPOSAL.md`.
- Tests: `tests/test_unified_local_ubi_amount_content.py` (12 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ functional drive of the REAL `_combined_claimable` (both legs / single leg / failed read not zero / unavailable leg / empty) + frontend wiring incl. "unified card has no per-network button" and "injected still gets two cards"). Run with `python -m pytest tests/test_unified_local_ubi_amount_content.py`.

### Local claim modal Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ ALWAYS one card + compact (no scroll) (2026-10)
Follow-up to the section above. Two gaps remained: (1) the single card appeared **only** when BOTH legs were claimable and confidently read Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a local login with one leg claimable (or a failed read) fell back to **two** cards, which still reads as "two actions" even though the wallet does both in one PIN; (2) the modal was **too tall** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ measured **223 px hidden (needed scrolling) at 360Ðâ640**, and 7 px at 390Ðâ844.
- **`static/js/wallet-main.js` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `claimLocalCardHtml(claims)`** (new): ONE adaptive card for every local state. `renderClaimNetworks()` now branches on **`_isUnifiedLocalUbiClaim()`** (the login), not on `unified` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `unified ? claimUnifiedCardHtml(...) : claimLocalCardHtml(...)`. Four states: both claimable (total + split), one claimable (that leg's amount + the other leg NAMED), a leg unreadable (sum of readable legs + "couldn't check XDC Ð²Òâ we'll retry"), neither (dash + `Claimed`/`Paused`/`Not Started`). **Never invents a total** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ an unreadable leg is named, never silently counted as `0`.
- **`_unifiedLocalClaimTotal()` is UNCHANGED** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ it still gates the **button** (`combined.confident && count >= 2`), so card and button always agree: a single-card view with one claimable leg still shows `Claim {amount} G$ on Celo`. Do not weaken it to "make the card unified".
- **Non-local is byte-for-byte unchanged** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ injected / WalletConnect / Privy / MiniPay still get two cards + two `claim-card-btn`s (`claimNetworkStatusHtml`/`selectClaimNetwork` untouched).
- **Compact layout (`templates/wallet.html`):** removed the decorative trust-chips row (Ð²Ó£Òª/ÑÒ¹ââ/Ð²Ò¸Â°) and the **XDC footer block** ("XDC remains availableÐ²ÒÒ°" + "Open XDC claim page Ð²Ò¶â") Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ it contradicted the unified card; `/xdc-wallet` stays reachable from the wallet bottom-sheet's XSwap row + desktop rail. Compressed `.ubi-pool-panel` (padding/gap/cell/value/note), `.modal-title`/`.modal-handle` margins, and the countdown box. The pool note copy was shortened in BOTH the markup and the JS assignment (`loadUbiPoolStats`). **All 4 pool cells + all 9 class names are kept** (the `test_ubi_pool_stats_content.py` orphan-class guard).
- **Short-viewport guard `@media (max-height: 700px)`:** hides `#claimModal .ubi-pool-note`, collapses the countdown to its timer only (label + note hidden), trims the primary button. Only decorative copy is dropped Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ every figure and the claim action stay.
- **Measured (real iframe render over the rendered modal, both states):** 360Ðâ640 claimable 223 px hidden Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ **0**; 360Ðâ640 already-claimed **0**; 390Ðâ844 **0**. `#claimNetworkList` 102 px Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ 89/59 px (one card).
- **Do not** re-add a second network card for local, re-add the XDC footer block, or re-widen the pool panel. **Do not** collapse the pool cells.
- Proposal + measurement record: `docs/CLAIM_MODAL_UNIFIED_COMPACT_PROPOSAL.md`.
- Tests: `tests/test_claim_modal_unified_compact_content.py` (15 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a **node harness** driving the REAL `claimLocalCardHtml`/`claimUnifiedCardHtml`/`_claimEsc` bodies: one card in every local state, "never invents a total" for an unreadable leg, the blocked leg is named, the unified card has no per-network button; plus content wiring for the local render branch, escaping, the unchanged `_unifiedLocalClaimTotal` gate, removed decorative blocks, the short-viewport guard, pool-cell retention, and the signing/FV invariants). Run with `python -m pytest tests/test_claim_modal_unified_compact_content.py`.

### Local claim modal Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the 3 open questions, resolved (2026-10)
The proposal left three choices open; they shipped as follows.
- **Pool panel:** KEPT the 2Ðâ2 grid (all four figures), compressed Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ not collapsed into one line. Hiding figures behind a tap was rejected: the panel is GoodDapp parity, and compression alone was enough to fit.
- **Close button:** KEPT. The ~52 px it costs fits inside the 360Ðâ640 budget now; removing it would leave the backdrop tap as the only exit, which is not discoverable.
- **Single-leg copy:** the card names the surviving network and the sub-line says why the other is out (`XDC already claimed today` / `XDC is paused` / `couldn't check XDC Ð²Òâ we'll retry`).


## Wallet tx history Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Celo was silently empty (2026-08)
Wallet history (`/api/wallet/transaction-history` -> `get_comprehensive_tx_history` in blockchain.py) showed ONLY XDC txs. Root cause: `forno.celo.org` rejects `eth_getLogs` spanning >5000 blocks ("query exceeds range"), and `_get_logs_full` used 10000-block chunks whose errors were silently dropped Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so the entire Celo scan (G$/cUSD/USDT/USDC sends, receives, swaps AND the G$ UBI claim, classified from the Transfer event whose sender is the UBI proxy) returned nothing and only the XDC merge survived.
- Same root cause also silently broke `has_recent_ubi_claim` (its single-shot 7-day getLogs always errored -> "no recent claim"); both its Transfer and UBI-event scans now route through `_get_logs_full`.
- Fix layers: `_get_logs_full` clamps chunks to `GETLOGS_MAX_CHUNK` (env, default 4000) and retries any errored chunk via `_get_logs_range_adaptive` (halves the range on "range/limit/exceed" errors, fails over across `_celo_rpc_urls()` on transient errors like forno's -32011 "no backend healthy"); the batch POST itself also fails over to `CELO_RPC_FALLBACKS` (env-overridable: forno, ankr, drpc). UBI claims need no extra scan Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ they arrive as G$ Transfer events from UBI_PROXY and classify to `claim` / "G$ UBI Claim" once fetching works.
- Tests: `tests/test_tx_history_celo_content.py` (content + functional via stubbed requests/web3, no deps). Run with `python -m unittest tests.test_tx_history_celo_content`.

## Wallet tx history Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ 120s timeout / "Loading transactions..." forever (2026-08 follow-up)
After the Celo fix landed, the history endpoint went from fast-but-empty to >120s (gunicorn `timeout = 120` in gunicorn.conf.py kills the worker Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ frontend spinner never resolves). Measured causes: (1) `_format_timestamp` was called **per raw log** (~500 sequential `eth_getBlockByNumber` round-trips); (2) 8 per-token scans Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð 61 chunks fired ~500 getLogs at forno, which **throttled concurrent 25-item batches to 130+s each**; (3) the scans themselves ran serially (~85s). Fixes in `get_comprehensive_tx_history`: the 4 tokens are queried as ONE `eth_getLogs` address **array** per direction (2 scans instead of 8, token resolved from `log.address` via `addr_to_token`); sent/received scans + XDC fetch run in a ThreadPoolExecutor(3); timestamps are filled AFTER dedupe/trim via `_format_timestamps_batch` (one batched `eth_getBlockByNumber` per Ð â¢Ð¢ÑÐ âÐ²ÒÒ£Ð â¢Ð¡Ò³50 blocks, falls back to the approx block-offset time); the swap-detection `eth_getTransactionByHash` batch fails over across `_celo_rpc_urls()`. `has_recent_ubi_claim` runs its 4 scans in a pool too. Cold fetch for a busy wallet: **~12s** (was 145s). Frontend `loadHistory` (wallet-main.js) has a 90s `AbortController` guard so a stalled request shows a retry message instead of spinning forever. Perf guards live in `tests/test_tx_history_celo_content.py` (`TestHistoryPerformanceGuards`).

## Send-link Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ fresh links flipping to "claimed" via RPC lag (2026-08, PR #222)
Payment links on `/send-link` showed "Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐ Ò² Claimed" seconds after sending. Root cause: `renderHistory()` in `templates/send-link.html` persisted `claimed` (localStorage + DB PATCH) the moment `otp.hasPayment(paymentId)` returned false Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ but forno is load-balanced and the node answering the `eth_call` can lag behind the node that served the deposit receipt, answering `hasPayment=false` for a registered payment. It also mislabelled **cancelled** links as claimed (false is the end state of BOTH exits).
- **OTP escrow contract facts (Celo, `0xB27D247f5C2a61D2Cb6b6E67FEE51d839447e97d`):** deposits via G$ `transferAndCall` (G$ is an ERC1967 proxy Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ check the impl bytecode, not the proxy's). `payments(paymentId)` returns `(bool hasPayment, uint256 paymentAmount, address paymentSender)` and KEEPS sender/amount after settlement Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ only the bool flips Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so `paymentSender == 0` provably means "deposit not visible to this node" (RPC lag), never a real claim. Withdraw event topic0 `0x39ca68a9f5d8038e871ef25a6622a56579cda4a6eedf63813574d23652e94048`, topics = (sender, receiver, **paymentId indexed**), amount in data; deposit event `0x6c9f23eeÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£`; `cancel()` emits NO withdraw event, so "settled + no withdraw event from deposit block to head" Ð â¢Ð¢ÑÐ âÐ¢Â®Ð âÐ¢Â¶ cancelled.
- **Fix:** read `payments()` instead of bare `hasPayment` (zero-sender Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ keep `active`, persist nothing); once settled, `_classifySettledPayment()` scans the withdraw event (topic3-filtered) from the deposit block (via `p.txHash`) in `GETLOGS_CHUNK=4000` chunks (forno rejects >5000) with a `SETTLE_SCAN_MAX_BLOCKS=500000` budget (~5.8 days at 1s blocks); undetermined Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ fallback `claimed` (common case; cancels self-persist `cancelled` from the cancelling device). Backend `/api/payment-links` is a dumb store Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ status is only ever set by the frontend, so all classification lives in send-link.html.
- Tests: `tests/test_send_link_settlement_content.py` (content, no deps).

## Play & Earn Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ 500 G$ weekly withdrawal limit (2026-09)
Play & Earn (`/minigames/`) used to pay out the **entire** `available_balance` in one withdrawal. Now every wallet is capped at **500 G$ per PHT calendar week** (Monday 00:00 Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ Sunday 23:59, UTC+8 Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ no DST so a fixed offset is exact). A 1,000 G$ balance pays out 500 G$; the other 500 G$ stays in the Play & Earn balance for a later week. A second click in the same week is refused with a "withdraw again next week" message naming the exact reset time.
- **`minigames/weekly_limit.py`** (new, dependency-free) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `weekly_withdrawal_limit()` (env `MINIGAME_WEEKLY_WITHDRAWAL_LIMIT`, default 500, falls back on bad/Ð â¢Ð¢ÑÐ âÐ²ÒÒ£Ð â¢Ð¡Ò³0 values so the cap can never be accidentally disabled), `pht_now`, `week_key()` (`2026-W38` ISO week), `week_start_date()`, `week_reset_at()` (next Monday 00:00 PHT), `week_reset_label()`, `weekly_limit_message()`.
- **`sql/minigame_weekly_withdrawal_limit.sql`** (new) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `minigame_weekly_withdrawals`, one row per `(wallet_address, week_key)` holding the week's running total. **Run this migration in Supabase before enabling.** `amount` allows 0 (`CHECK (amount >= 0)`) because the row is *seeded* with the week's existing payout total Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ often 0 Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the first time a wallet is polled or withdraws.
- **Cap logic** (`minigames_manager.withdraw_winnings`): `amount_to_withdraw = min(available_balance, weekly_remaining, MAX_WITHDRAWAL)`; the balance is debited by **only that amount** (`available_balance - amount_to_withdraw`), not zeroed, and `total_withdrawn`/the payout log record the partial amount. `MIN_WITHDRAWAL` (500) still applies Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ if only e.g. 300 G$ of the week's allowance is left, the request is refused as `weekly_limit_reached` rather than paying below the minimum.
- **Concurrency (this is the load-bearing part):** `_claim_week_allowance` is a **CAS** on the week row (`.eq('amount', expected_used)`); only the request that observes the current total wins, so two gunicorn workers racing on the last of the allowance can never both pay out. The loser re-reads and retries (3 attempts). A **failed** payout calls `_release_week_allowance` so the allowance is reusable Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ gas/vault failures must never burn a user's week.
- **Migration-missing safety:** a missing `minigame_weekly_withdrawals` table must never lock users out of their money. `_claim_week_allowance` returns `None` (and latches `_weekly_table_unavailable`) on an infra error, and the withdraw path then enforces the cap from `minigame_withdrawals_log` instead Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ weaker under concurrency, but the 500 G$ rule still holds and legitimate payouts keep working. `get_weekly_withdrawal_status` never raises (it powers `/api/balance` and page loads).
- **Seeding:** `_week_withdrawn_amount` returns the week row when present; otherwise it sums this PHT week's completed rows in `minigame_withdrawals_log` and inserts that as the starting total Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so payouts made before this cap existed (or by any path that bypassed the counter) still count against the allowance.
- **API:** `GET /minigames/api/balance` now also returns `weekly_limit`, `weekly_withdrawn`, `weekly_remaining`, `weekly_limit_reached`, `week_reset_label`, `week_reset_at`, `withdrawable_now`; `can_withdraw` additionally requires `weekly_remaining >= min_withdrawal`. `POST /minigames/api/withdraw` returns `amount_withdrawn`, `remaining_balance`, the `weekly_*` fields and `status: 'weekly_limit_reached'` when blocked.
- **UI (`templates/minigames.html`):** the balance card gained a weekly-allowance panel (limit / withdrawn this week / still available + the Monday-reset note); the withdraw button reads "Withdraw N G$" when the cap is the binding constraint, and a blocked attempt refreshes the card so the labels match the server.
- **When changing this:** keep the debit partial (`available_balance - amount_to_withdraw`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ zeroing the balance was the old behaviour and silently destroys the amount above the cap. Any new payout path for Play & Earn must go through `_claim_week_allowance`.
- Tests: `tests/test_minigames_weekly_withdrawal_limit_content.py` (19 tests Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ functional via importlib with stub supabase/blockchain, incl. 1,000Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶500 payout, same-week block, week rollover, seed-from-log, failure release, CAS race, missing-migration fallback; no deps). Run with `python -m unittest tests.test_minigames_weekly_withdrawal_limit_content`.

## Wallet page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ one-time "Join GoodMarket Community" invite (2026-09)
Every user gets a full-width invite to the Telegram community discussion group (`https://t.me/GoodMarketCommunity`) at the top of the wallet page Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ **shown once per browser and never again**.
- **Markup:** `#joinCommunityBanner` in `templates/wallet.html`, in the page flow right under the top bar (NOT inside a modal, so it needs no tap to appear). Base CSS rule hides it (`display: none`); only `.join-community-banner.show` reveals it, so it cannot flash before the seen-flag is checked.
- **Logic:** `static/js/wallet-bottomnav.js` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `showJoinCommunityBannerOnce()` (on `DOMContentLoaded`) early-returns when `gmJoinCommunityPrompted_v1` is `'1'` in localStorage, else adds `.show` **and writes the flag in the same tick**. The flag is written at *reveal*, not at click (same one-shot pattern as `walletReminderBanner`), so closing it without joining does not bring it back.
- **Fail-closed on blocked storage:** if `localStorage` throws (hardened private mode), `_gmJoinCommunitySeen()` returns **true** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ we cannot remember the prompt was shown, and re-showing it every load would break the one-time promise.
- **Redirect:** `joinGoodMarketCommunity()` hides the banner then `window.open(url, '_blank')`, falling back to `window.location.href` when the popup is blocked (common in mobile dApp browsers) so the tap always lands in the group. `GM_JOIN_COMMUNITY_URL` is the single source of truth for the link.
- **Copy rule:** the banner names the **GoodMarket** community; never describe the app/wallet as GoodDollar (impersonation-risk rule above).
- Tests: `tests/test_wallet_join_community_content.py` (13 tests, no deps). Run with `python -m pytest tests/test_wallet_join_community_content.py`.

## Session lifetime Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ 48h default, env-overridable (2026-09)
`app.permanent_session_lifetime` used to be a hardcoded `timedelta(hours=24)`. Users on unreliable power/connectivity (frequent brownouts) could not always return to the app within a day, so they were logged out and had to do the full email + PIN + signed-login again.
- **`main.py`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `SESSION_LIFETIME_HOURS = get_env_int('SESSION_LIFETIME_HOURS', 48)` (from `env_utils`, so a quoted/placeholder value falls back instead of crashing at import). A `<= 0` value is reset to 48 Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a 0 would expire every cookie instantly. Applied via `app.permanent_session_lifetime = timedelta(hours=SESSION_LIFETIME_HOURS)`.
- **Sliding, not absolute:** `SESSION_REFRESH_EACH_REQUEST` is left at the Flask default (`True`), so the cookie is re-issued on every response Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ any request inside the window keeps a user logged in indefinitely. The window only counts *inactivity*, and it resets on each request. Do not set it to False; that would turn this into a hard absolute cap.
- **Raise it without a deploy:** set `SESSION_LIFETIME_HOURS` in the environment (documented in `.env.example`). Deliberately NOT higher than 48 by default Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a longer session is a longer window for a stolen cookie, and the 15-min GMLocalWallet auto-lock (`AUTO_LOCK_MS`) plus the PIN prompt on every signature is what actually protects signing, independent of session length.
- **Not the same timer as the 15-min wallet lock:** `AUTO_LOCK_MS` in `local-wallet.js` drops the decrypted key after 15 min and re-prompts for the PIN only (cached keystore, no server login). The 48h session governs the Flask cookie Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ homepage bounce.
- **WalletConnect has its own expiry guard** (`static/js/wc-bridge.js`, WC-only) which is unrelated and unchanged.
- Tests: `tests/test_session_lifetime_content.py` (5 tests, no deps). Run with `python -m unittest tests.test_session_lifetime_content`.

## Settings Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ Private key reveal (in-app wallet only) (2026-09)
Local-wallet users can now view their private key from the wallet page Settings modal, behind a hard acknowledgement + PIN re-entry gate.
- **`static/js/local-wallet.js`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ new `getPrivateKey()` (exported as `GMLocalWallet.getPrivateKey`). It is `_requireUnlocked().privateKey`, so a locked session throws `GM_WALLET_LOCKED` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ key material is never reachable without a decrypted session. `create()` still returns only `{email, address, keystore, mnemonic}`; nothing new is persisted or sent.
- **`templates/wallet.html`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `Ð âÐ£ÒÐ âÐ£ÐÐ âÐÂ¶Ð âÐ¢Â° Private Key` section at the bottom of the Settings modal, wrapped in `{% if (login_method or '')|lower == 'local' %}` (an injected/WC/Privy session has no browser-held key to show). The reveal modal `#pkModal` is a second gated block, DOM-ordered AFTER `settingsModal` so it stacks above it (shared `z-index: 200` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ DOM order decides).
- **Two-step gate:** step 1 is the warning + an `#pkAck` checkbox that enables `#pkContinueBtn` (`disabled` on load), step 2 shows the key. **The warning copy ships in English** (the app's other copy is Taglish, but this one is the only thing between a user and a drained wallet, so it stays unambiguous): *"Never share it with anyone"*, funds *"cannot be reversed"*, and support *"will never ask for your private key"*. Keep it English Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `test_warning_copy_is_english` fails on any Tagalog left in the modal.
- **`static/js/wallet-main.js`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `openPkModal` / `closePkModal` / `pkContinue` / `copyPrivateKey` (+ private `_pkReset`). `pkContinue` ALWAYS re-prompts the PIN via `window._lwOpenUnlockModal({title:'Confirm your PIN', Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£})` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ deliberately not an `if (!isUnlocked())` guard, because merely-unlocked is not enough for long-lived key material (same rule `exportMnemonic` uses). Cancelling the PIN prompt reveals nothing.
- **Key hygiene:** the key is written to a DOM text node only and wiped on every exit path (`_pkReset` clears `#pkValue`, unticks the box, resets to step 1). `pkModal` bypasses `openModal` (which would close Settings behind it) and the outside-tap dismiss routes through `closePkModal`. No `fetch`/XHR anywhere in the flow.
- **CSS gotcha:** the legacy `.pk-box` base rule is `display: none` (it was a toggle target), so `#pkModal .pk-box { display: block; }` is required Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ without it the revealed key renders invisibly with no error.
- **Not to be confused with `exportMnemonic(pin)`** (recovery words) which predates this and has no UI caller.
- Tests: `tests/test_private_key_reveal_content.py` (13 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ gating, warning copy, checkbox/PIN gate, DOM wiping, stack order, no-network invariant). The crypto path was additionally verified against real ethers 6.13.4 in a Node VM: `getPrivateKey()` refuses while locked, throws on a wrong PIN, and the revealed key derives the same address as `create()`.

## Chicken Farm â WalletConnect users could not farm (2026-10)
Reported with a screenshot: a WalletConnect-login user tapped a farm action and the page showed
`GMWalletConnect.getProvider(...).then is not a function`. Root cause: **`GMWalletConnect.getProvider()`
returns the EIP-1193 provider SYNCHRONOUSLY** (`wc-bridge.js` builds the object on first call and
returns it), but `static/js/farming.js` chained `.then()` on it â a `TypeError` thrown while building
the promise chain, so the wallet was **never prompted** and every farm action (start / sell / close)
died with that raw error. It hit WalletConnect users only, because the branch is reached only when
`prefersWc()` and no injected provider is present.
- **Fix (`static/js/farming.js` + `static/js/paluwagan.js`):** a small `_wcProviderPromise()` helper
  (`Promise.resolve().then(function () { return GMWalletConnect.getProvider(); })`) tolerates BOTH
  shapes â synchronous today, promise-safe if the bridge ever goes async. **Never call
  `GMWalletConnect.getProvider().then(...)` again.** `paluwagan.js` carried the identical defect.
- **The rest of the app was already correct.** `swap-core.js`, `claim.html`, `lotto.html`,
  `savings.html`, `learn_and_earn.html`, `reloadly.html`, `send-link.html`, `xdc_wallet.html`,
  `wallet-main.js`, `wallet-voucher.js`, `chatroom-widget.js` either `await` the call or `return` it
  from an `async` function â both of which accept a plain value. Only these two call sites chained
  `.then()` directly. The source now documents the sync contract at `wc-bridge.js` `getProvider()`.
- **Rules:** treat `GMWalletConnect.getProvider()` as **synchronous** (a plain provider object, not a
  promise). Wrap it in `Promise.resolve().then(...)`, or `await` it, or `return` it from an `async`
  function â never `.then()` it directly. `farming.js` also exports `startFarm`/`sellEggs`/`closeFarm`
  on `window.GMFarm` so the flow is drivable in tests.
- Tests: `tests/test_farming_tx_history_content.py` â `test_walletconnect_signer_does_not_call_then_on_a_synchronous_provider`,
  `test_paluwagan_walletconnect_signer_has_the_same_sync_guard`,
  `test_no_page_calls_then_on_the_synchronous_wc_get_provider` (repo-wide guard over every
  `static/js/*.js` + `templates/*.html`, comment lines ignored),
  `test_wc_bridge_documents_the_synchronous_contract`, and
  `test_walletconnect_farm_action_reaches_the_wallet_end_to_end` â a **node harness** that drives the
  REAL `resolveSigner()`/`startFarm()` with a synchronous WC provider and NO injected wallet, i.e. the
  exact reported environment. Mutation-verified: reverting the fix makes it fail with the user's exact
  error string. Run with `python -m pytest tests/test_farming_tx_history_content.py`.

## Chicken Farm Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ on-chain tx history + close/sell success UX (2026-09)
Farm actions sign directly from the **user's own wallet**, so the backend never sees a record and the only evidence that "my G$ arrived" is the transaction hash. The old page showed it nowhere Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a successful sell and a silently-dropped one looked identical ("hindi nila alam kung dumating ba ang G$"). The page also confirmed a close with a vague status line and never said the G$ had been sent.
- **`static/js/farming-history.js`** (DOM-free + dependency-free) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ durable localStorage store. **Per-wallet now (2026-09):** the key is namespaced `gm_farming_tx_history_v1::<wallet_lower>` so two accounts on one browser never share/clobber rows; `setWallet()` must be called before any read (farming.js does it at boot), a legacy global-key store is migrated on first contact (adopting rows owned by this wallet or unowned, leaving a foreign wallet's rows for its own migration), and `all()` filters to the active wallet. Every broadcast tx is written the instant a hash exists (`add`), then settled **in place** (`update`/`updateByHash` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ status confirmed|failed, `blockNumber`, `confirmedAt`, `receivedWei`). `pending()` returns only entries with a real hash still awaiting a receipt, so a fresh page load can reconcile them. Retention (**MAX_ITEMS = 200**) is **non-destructive**: pending/failed rows are always kept, only the oldest confirmed rows are trimmed (the old flat 60-cap silently discarded old confirmed transactions Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the "old transaction disappeared" complaint). `mergeOnchain(rows)` folds the wallet's on-chain events (from `/farming/api/history`) into the list Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ upsert by hash, marking confirmed and **inserting when the local copy is gone** (cleared storage / another device), which is what makes history durable. Also owns `isOnchainHash` (0x+64 hex Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the guard that stops a `queued:`/numeric id becoming a bogus explorer link), `celoscanUrl`, `shortHash`, and `formatWei` (exact BigInt G$ formatting). A blocked `localStorage` (hardened private mode) degrades to in-memory Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the page still works, history just does not survive the reload.
- **`farming/blockchain.py`** (new, 2026-09) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ per-wallet on-chain history from the **contract**, the durable source of truth. `fetch_wallet_farm_history(contract, wallet)` scans the user events (`FarmStarted`/`EggsSold`/`FarmClosed`) via `blockchain._get_logs_full` (inherits chunking under forno's 5000-block `eth_getLogs` cap, adaptive range halving, RPC failover) with `topics : [ [3 event sigs], wallet_topic ]` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ topic0 array = OR over actions, topic1 = THIS wallet, so **one wallet can never receive another's rows**. Rows are decoded by slicing topics/data words (no ABI dep) and stamped with the scanning wallet + `source: "onchain"`; block timestamps are batched via `_format_timestamps_batch`. `get_deploy_block()` anchors the scan (never from genesis) via `FARMING_DEPLOY_BLOCK` env, else a one-time `eth_getCode` binary search (archive state is available on forno Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the search is cached per contract), falling back to a bounded `DEFAULT_LOOKBACK_DAYS = 45`. Event topics (verified against ethers 6.13.4 `id()`): `FarmStarted=0x7149cad0Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£`, `EggsSold=0x7d948571Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£`, `FarmClosed=0xbe40ce4cÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£`. 60s result cache; `invalidate_history_cache()` to clear.
- **`farming/routes.py`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `GET /farming/api/history` returns the **SESSION** wallet's on-chain history (never a client-supplied address Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ that would leak another user's rows). Auth-gated (401 without a verified session), returns `[]` (never a 500) when the contract is unset or the RPC fails, and accepts `?force=1` to bypass the cache. Reads only; no signing.
- **`static/js/farming.js`** (new) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ page logic, read from `window.GM_FARM_BOOT` (set inline by the template). `HISTORY.setWallet(WALLET)` runs at module load (before any read). **Farm state loads FIRST (2026-09)** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `init()` calls `refreshFarm()` immediately and the on-chain history scan (`loadOnchainHistory()` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `HISTORY.mergeOnchain(...)` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `reconcilePending()`) runs alongside it, NOT ahead of it. Chaining the history scan before the state read is what made an active farm appear only after a manual Refresh tap (the scan walks contract logs and is slow, so users read "No active farm yet" and assumed their farm had not registered). The "Check on-chain status" button re-fetches with `force=1` (cross-device recovery). Key invariants:
  - **Auto-refresh (`POLL_MS = 15000`):** `startPolling()` registers a 15s interval running `pollOnce()`, which calls `refreshFarm({silent:true})` (and, every `HISTORY_EVERY_TICKS = 4` ticks Ð â¢Ð¢ÑÐ âÐ²ÒÒ£Ð â¢Ð£Â®60s, a non-forced `loadOnchainHistory().then(reconcilePending)` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the backend's 60s cache makes a warm poll a cheap no-op; only the explicit button forces). `pollOnce` **no-ops while `busy` (a tx is in flight), while a read is already running, and while `document.hidden`** (a phone left in the background must not poll all night). `visibilitychange` + `window focus` call `onVisible()` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ immediate `pollOnce()`, so returning to the tab feels instant instead of waiting out the interval.
  - **`refreshFarm(opts)` has a silent mode and is single-flight.** `opts.silent` suppresses the status/error writes (and `applyFarmStatus` additionally refuses to overwrite a status this module does not own Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ see `lastAutoStatus`/`statusOwnedByUs`, so a poll can never clobber an action result or error the user is reading, while a status it *does* own still self-corrects when the farm changes elsewhere). `refreshing` holds the in-flight promise: a silent caller joins it, but an **explicit** refresh (the Refresh button) chains `.then(re-read)` so a user tap is never swallowed by a background poll. `refreshing` is cleared in the final `.then`, and `done(result)` resolves the join promise.
  - **Signer routing mirrors swap/lotto**: `local` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ PIN-unlocked `GMLocalWallet` (injected MetaMask is a DIFFERENT account and is never used); WalletConnect Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `GMWalletConnect`; else injected providers bound to the **session wallet's account** (`getSigner(match)`) across EIP-6963 discovery, so a multi-account wallet cannot sign from the wrong address.
  - **Reads (farm state, receipts) go through a public `JsonRpcProvider`**, never `signer.provider` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ rendering never pops a wallet prompt and never lags behind the phone's wallet node (same rule as the chat bridges).
  - **Every broadcast tx is tracked** (`trackTx` is called AFTER the wallet returns a hash, so a rejected signature leaves no phantom row) and waited on via `waitForReceipt`; success copy names the amount actually received, decoded from the receipt's Transfer logs (`sumGdReceived`) rather than trusting the tx status. The "G$ received" note renders only for payout actions (close/sell) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ start-farm/approve move G$ *to* the contract. Close-farm shows a **congratulations modal** ("your farm is now closed and X G$ has been sent to your wallet") with the Celoscan link; sell-eggs reports "Eggs sold! X G$ was sent to your wallet."
  - `reconcilePending()` runs on load and from the "Check on-chain status" button, so a tab closed mid-flight still settles later.
  - `blockReason()` keeps the older "don't open a doomed wallet prompt" guards (maturity, reward-pool shortfall) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ but a **matured farm still closes with zero egg value** (principal-only payout); only *selling* requires eggs. No copy may tell the user to tap Refresh to see an active farm (`test_no_copy_tells_users_to_tap_refresh`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the button remains only as an escape hatch.
- **`templates/farming.html`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ rebuilt attractively (hero with egg animation, cycle-progress bar, live phase banner, projected profit/payout, in-flight step indicator, toasts, dark gold/green theme) and gained the **transaction-history panel** (status chip + amount + timestamp + Celoscan link + "G$ received"). Scripts load **ethers first** (local-wallet.js asserts it), then tx-error/wc-bridge/local-wallet/farming-history/farming.
  - **Hero farm scene (2026-09, supersedes the single-hen version)** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the hero used to show a static `Ð âÐ£ÒÐ âÐ£ÐÐ âÐ¢ÒÐ âÐÂ¶` watermark (`.hero::after`, now deleted) beside decorative falling eggs that had nothing to do with the user's farm. It is now a **hand-drawn inline SVG farm** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ sky gradient, glowing sun, hills, grass, fence, coop, nest and a **flock of five chickens** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ driven by the real contract state. `renderScene()` in farming.js (called at the end of `renderFarm()`) shows **one bird per chicken owned** (slots are capped at 5; a 10-chicken farm shows 5), fills the nest with the eggs actually ready to sell, and greys the pasture out with no farm (`is-idle`) or lights a pulsing gold halo at maturity (`is-mature`). A visible caption (`#sceneCaption`) and the `role="img"` `aria-label` carry the same numbers for screen readers.
    - **Eggs never tick.** The old `eggDrop` keyframe looped forever, implying eggs accrue in real time, but the contract pays **1 egg/chicken/day** and only if eggs are sold. The drop is gone; `is-pop` fires **once** when the rendered count actually GROWS (`sceneEggsShown` tracks the previous value). A page that loads with 3 eggs already ready shows 3 eggs and **no** pop Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a decorative animation that lies about the farm is worse than none.
    - **CSS:** `.farm-chicken.is-off` and `.chicken-egg.is-hidden` toggle **opacity**, never `display` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a `display:none` egg would kill the `eggPop` keyframe. Animation is `transform`/`opacity` only (`chickenBob`, `chickenPeck`, `eggPop`, `haloPulse`, `sunGlow`), and the `prefers-reduced-motion` block disables every one of them. The scene lives in a `.hero-grid` (`minmax(0,1fr) 320px`) that collapses to one column Ð â¢Ð¢ÑÐ âÐ²ÒÒ£Ð â¢Ð¡Ò³720px.
    - **The scene starts `is-idle` in the markup** so a farm-less visitor never sees a frame of birds, and `is-off`/`is-pop` are applied only by JS (never authored). Changing the flock or nest **slot count breaks the renderer's assumption** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `test_scene_ships_the_slot_counts_the_renderer_assumes` pins both (5/4).
    - Birds are placed by hand-authored `translate`/`scale` pairs in the Jinja `flock` list; `test_scene_geometry_keeps_every_chicken_inside_the_canvas` recomputes each bird's bounds and fails if one leaves the viewBox, floats above the pasture, or merges into its neighbour. Verified against real pixels with cairosvg + PIL (every region sampled in both states) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ see the method note below.
  - The `@keyframes` blocks are **transform/opacity only** (no `box-shadow`/layout props Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the wallet load-perf lesson), and a `prefers-reduced-motion` block disables the decorative loops.
  - Ethers SRI must stay `sha384-6Zl0Pc8zjÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£` (the repo-wide correct hash for 6.13.4) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a wrong hash makes the browser refuse the script, surfacing as "ethers is not defined" the moment an action is tapped.
- **`farming/routes.py`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ passes `login_method`, `walletconnect_project_id`, `celo_rpc`, and `celo_rpc_fallbacks` (env `CELO_RPC_URL` / `CELO_RPC_FALLBACKS`) to the template.
- **When changing this:** keep successes gated on the receipt + decoded amount (not the returned hash), keep `local` off the injected path, and keep reads on the public RPC. Keep the history store **per-wallet** (never revert to the global key) and keep merging the contract's events on load Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ that is what makes old transactions survive cleared storage. Any new farm action must go through `trackTx`/`waitForReceipt` so it appears in history. The orphan-class check (`test_every_css_class_is_actually_used`) will fail if you add a CSS class with no markup/JS reference Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ matching is token-exact, so `action-grid` does not keep a dead `.grid` alive.
- Tests: `tests/test_farming_tx_history_content.py` (68 tests Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ functional node harness over the REAL history store + farming.js logic (per-wallet namespacing/isolation, legacy migration, `mergeOnchain` restore/upsert/idempotence, non-destructive retention, settle-by-receipt, Transfer-log decoding, wei/G$ formatting idempotence, amount validation, refresh-button wiring, **read-provider chain pinning**, **instant farm load ahead of the history scan + 15s auto-poll + hidden-tab skip + visibility refresh + silent-poll status ownership**), functional import of `farming/blockchain.py` with a stubbed `blockchain` pkg (event decoding, per-wallet topic filter, wallet stamping, deploy-block discovery/cache + env override, deploy-block anchoring), plus content wiring for signer routing, public-RPC reads + failover, loading order, SRI, routes, the history/congrats UI, and the orphan-class check, a node harness driving the REAL `renderScene` through `refreshFarm` for no-farm/0-egg/3-egg/mature, the one-shot egg pop (no pop on load, exactly one pop on growth), and **the hero farm scene** (inline SVG + old `.hero::after` watermark gone, scene keyframes transform/opacity only, reduced-motion coverage, flock/nest slot counts, chicken geometry inside the canvas); no deps). Run with `python -m pytest tests/test_farming_tx_history_content.py`.

## UBI pool figures Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ sourced from GoodDapp's own contract reads (2026-09)
The claim modal's "Celo UBI Pool Balance" was `balanceOf(UBI_PROXY)` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the UBIScheme contract's **whole G$ reserve (~59.6M G$)**, which is not the number users see in GoodDapp. GoodDapp's claim screen (`GoodWalletClass.getClaimScreenStats` in the legacy GoodWallet, `protocol-ui` in GoodWeb3-Mono) reads the UBIScheme directly, so the app now does the same and shows the same figures.
- **`UBI_SCHEME_ABI`** gained `dailyCyclePool()` ("Today's G$ Distribution"), `dailyUbi()`, `currentDay()`, `getDailyStats()` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `(claimersToday, claimedTodayWei)`, and `getClaimerCount(uint256 day)`.
- **`blockchain.get_ubi_pool_stats(force=False)`** (new) returns `daily_cycle_pool`, `daily_ubi`, `active_users`, `claimers_today`, `claimed_today`, `current_day`, `period_start`, `paused`, `pool_balance`, `next_claim_ts` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ 60s cached (`POOL_STATS_CACHE_TTL`), serving a stale snapshot on RPC error. **Every read is individually guarded** (`_read()` returns None on revert) because the Celo implementation does NOT expose `activeUsersCount()` (it reverts) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ one unknown method must not blank the panel.
- **`daily_ubi` fallback:** prefer the contract's own `dailyUbi()`; only derive `dailyCyclePool / max(getClaimerCount(currentDay-1), claimersToday)` when it is unreadable. Do NOT compute the division unconditionally Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the contract's value already applies the `minActiveUsers` floor.
- **`next_claim_ts = periodStart + (currentDay + 1) * 86400`** (GoodDapp's logic; 1-day claim cycle).
- **`/api/ubi-pool-balance`** now returns all of the above AND keeps the legacy `balance` / `balance_formatted` keys (falls back to `get_gooddollar_balance(ubi_proxy)` when the snapshot has no `pool_balance`) so no consumer breaks.
- **UI:** `templates/wallet.html` claim modal Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `.ubi-pool-row` replaced by a 2Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð2 `.ubi-pool-panel` grid (Today's G$ Distribution / Per claimer (est.) / Claimers today / Pool balance) with a `#ubiPoolLive` badge that flips to "Paused"/"Offline" (`.is-stale`, amber). `static/js/wallet-main.js` `loadUbiPoolStats()` + `_fmtPoolGd()` (abbreviates Ð â¢Ð¢ÑÐ âÐ²ÒÒ£Ð â¢Ð£Ò·1M as "1.45M G$", Ð â¢Ð¢ÑÐ âÐ²ÒÒ£Ð â¢Ð£Ò·10K as "1.4K G$"). Dark-purple vars only Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a literal light colour fails the AA test.
- **`templates/overview.html`'s `loadUbiPoolBalance()` was already a dead no-op** (no `#ubiPoolBalance` element exists there) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ left alone; the preserved legacy keys would still satisfy it.
- Tests: `tests/test_ubi_pool_stats_content.py` (14 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ drives the REAL `get_ubi_pool_stats` against a fake chain mirroring live Celo values, covering the `dailyUbi` preference + fallback, per-field degradation, caching, and total-failure; plus route/UI/orphan-class/theme wiring). Run with `python -m unittest tests.test_ubi_pool_stats_content`.

## Verifying hand-drawn SVG without eyes (2026-09)
There is no rasteriser or image tool in the base env, and an inline-SVG scene can be
structurally valid while looking wrong. The working method (used for the farm scene):
1. `pip install jinja2 lxml cairosvg pillow` and render the template with a stub `env.globals["url_for"]` (see `scripts/render_wallet_preview.py` for the same trick).
2. Extract the `<svg>`, parse it with lxml, and **check paint order** by walking the top-level children Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ sky must precede hills, grass, coop, nest, flock. z-order in SVG is document order, so this catches a coop painted over its own chickens.
3. Recompute element bounds from their own `translate`/`scale` (not by eye) and assert they stay in the `viewBox`, stand on the ground plane, and do not merge.
4. Rasterise with cairosvg and **sample pixel regions** with PIL Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ assert sky is blue, grass is green, each chicken's comb/head/eye/beak/legs painted the authored colours, and that a no-farm state has ZERO gold pixels in the pasture.
5. Print a coarse ASCII classification of the PNG to actually perceive the composition; that is how a misplaced bird is spotted.
**Gotchas:** inline SVG omits `xmlns` (the HTML5 parser implies it), so lxml sees bare `g`/`path` tags Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ do not filter on the namespaced name. cairosvg **ignores class-driven CSS**, so build each state by adding/removing elements with lxml instead of hoping the stylesheet applies. A maturity halo is gold and will mask a hidden bird's absence Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ strip it when checking the idle state. Sampling a point can land on a shape drawn *over* it (a beak over a tail) or *between* two strokes (a gap between legs), so pick coordinates from the authored geometry rather than eyeballing.

## `ethers.FallbackProvider` 2nd arg is the NETWORK, not the quorum (2026-09)
"Could not load farm status. Network changed: 1 => 42220" was not a Celo/RPC outage Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ it was an argument-position bug. `new ethers.FallbackProvider(providers, 1)` passes `1` as the **network**, so the provider is pinned to **mainnet (chainId 1)** while the sub-providers talk to Celo (42220). Every read then dies inside ethers' `_checkNetwork`: `expected.chainId (1) !== actual.chainId (42220)` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `assert(false, "network changed: 1 => 42220", "NETWORK_ERROR")`. Verified against the real ethers 6.13.4 UMD build Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `Network.from(1)` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ mainnet, and `new FallbackProvider([celoProviders], 1).getNetwork()` rejects with that exact string.
- **Correct signature: `new ethers.FallbackProvider(providers, network, options)`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ quorum belongs in the **3rd** argument: `new FallbackProvider(entries, { chainId: 42220, name: "celo" }, { quorum: 1 })`. With the bug the default quorum also became `Math.ceil(totalWeight/2)`, so failover silently behaved differently than intended.
- **Omitting the 2nd argument is not the bug** (that sets `#anyNetwork = true`, which auto-detects and never throws) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ but it is still wrong here: it hides a misconfigured/wrong-chain RPC instead of failing loudly. Pin the network explicitly.
- **Fixed call sites:** `static/js/farming.js` (`getReadProvider` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ this is what surfaced the report), `templates/savings.html` (`getReadProvider` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ **identical bug**, savings history/receipt reads were dead the same way), `static/js/p2p/p2p-wallet.js` (worked only by accident; now pinned).
- **When adding a FallbackProvider:** never pass a bare number as the 2nd argument. This one is easy to re-introduce because the buggy form looks like a reasonable "quorum" shorthand. Guarded by `test_read_provider_is_pinned_to_celo_not_mainnet` (a functional node harness whose ethers stub reproduces 6.13.4's real coercion + the Contract-call network resolution, so it reproduces the exact user-visible error on the buggy code), `test_quorum_is_passed_as_an_option_not_the_network`, and `test_savings_read_provider_uses_the_same_argument_shape`.

## "Total G$ Distributed" analytics Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ wrong / missing data (2026-09)
The admin overview `Total Disbursed` figure (and the homepage hero stat, which reads the same value) was wrong in both directions. `analytics_service._get_total_disbursements_stats()` had five independent defects:
- **Daily tasks counted unapproved submissions.** `telegram_task_log` / `twitter_task_log` were summed with NO status filter, so every row counted the moment it was created with `status='pending'` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ and `rejected` ones too. Now both filter `status == 'completed'`, which is set only after admin approval + the on-chain transfer.
- **Learn & Earn counted blocked attempts.** The all-time query had no `status` filter (weekly/monthly did). Now filters `status == True` (a **boolean** column, not the string `'completed'`).
- **Play & Earn always reported 0.** It read `minigame_rewards_log` while filtering `reward_type == 'token_withdrawal'` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a column **no writer in this repo sets** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so the filter matched nothing. Real withdrawals live in `minigame_withdrawals_log`; the two ledgers are disjoint, so the total is `minigame_rewards_log.reward_amount` + `minigame_withdrawals_log.amount` (the latter filtered `status == 'completed'`). Breakdown label is now "Play & Earn Payouts" (the `overview.html` icon map matches on both `Play & Earn` and the old `Minigames`).
- **PostgREST ~1000-row truncation.** A single unpaged request stops at the response cap, silently dropping rows. `_read_all_rows` now pages with `.range()` until a short page arrives. `_count_active_earners_across_features` was fixed the same way.
- **RLS hid payout rows.** Reads used the anon client. `_disbursement_client()` prefers `get_supabase_admin_client()` (service-role), falls back to anon with a warning. Same fix in `_count_active_earners_across_features`.
- **Exactly-once rules:** Learn & Earn streams (`learn_earn_streams`) are a separate rail for the SAME reward, so they are NOT summed. Reloadly is G$ **IN** (users paying the store) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ reported but never added. Daily vouchers pay from the OneTimePayments escrow, so they stay a claim COUNT. Forum (`status='completed'`), Task Completion (`status='completed'`), Community Stories (`status IN approved/approved_low/approved_high`), NFT sales (`sell_price`) and NFT burns (`burn_amount_g`) are summed.
- **Column gotchas (verified against the writers):** `learnearn_log.status` is boolean; `minigame_rewards_log` has **no** `status` column (selecting one makes PostgREST error and the safe reader returns `[]`, silently dropping every direct game reward Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the projection must stay amount-only); `minigame_withdrawals_log.status` arrived in a later migration, so `_paginate_optional_status` retries unfiltered *and* drops `status` from the projection when the column is absent.
- **Do not add a table to the total without checking its writer's status semantics** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a pending/rejected row is not a disbursement.
- Tests: `tests/test_total_disbursements_content.py` (27 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ drives the REAL function against a fake Supabase client that reproduces the 1000-row cap, per-column filter errors and RLS-denied anon reads: status filters, exactly-once totals, minigame double-ledger, pagination past the cap, service-role preference, time-windowed status rules, graceful degradation, active-earners paging). Run with `python -m unittest tests.test_total_disbursements_content`.

## LI.FI (Jumper) bridge Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ native CELO Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ ETH on Base (2026-10)
The Bridge tab gained a third direction, **Celo Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ Base**, routed by LI.FI (the engine behind Jumper). Scope is deliberately narrow: **native CELO Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ native ETH on Base ONLY** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ not G$, not Ethereum, not the reverse direction. Ships DARK behind `LIFI_BRIDGE_ENABLED=1`.
- **Module `lifi_bridge.py`** (stdlib only, so it imports/tests without Flask/requests): `build_quote_request`, `get_quote` (10s cache), `normalize_route`, `get_step_transaction`, `get_status`, `bridge_enabled`. Endpoints in `routes.py`: `POST /api/bridge/lifi/quote`, `POST /api/bridge/lifi/step-tx`, `GET /api/bridge/lifi/status` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ all `@auth_required` + feature-gated. **Server-side proxy** so the integrator id/key never reach the browser; the client only calls our endpoints.
- **The route is 2 steps on 2 chains** (verified live 2026-10-07): Glacis (CeloÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶Base) + a destination swap to native ETH Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ **two signatures**. `executeLifiBridge` (swap-bridge.js) signs step 1 on Celo, polls `/status` until the bridge delivers, switches the wallet to Base, then signs step 2.
- **Verified gotchas that make-or-break this Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ do not regress:**
  1. **Use `POST /advanced/routes`, NOT `GET /v1/quote`.** The GET endpoint IGNORES `allowSwitchChain`, so the route is filtered out ("No available quotes"). The POST endpoint with `options.allowSwitchChain=true` (+`allowDestinationCall`) returns it.
  2. **`/advanced/routes` responses contain NO `transactionRequest`.** Tx data must be fetched per step from `POST /advanced/stepTransaction`. `normalize_route` carries each step's `raw` object so the client can post it back to `/api/bridge/lifi/step-tx`.
  3. **Native CELO is its ERC-20 `0x471EcE...`, NOT `0xeeee`** (the pseudo-address is rejected on Celo). Native ETH on Base IS `0xeeee`.
  4. **Cloudflare 403s the default urllib User-Agent** (`Error 1010: browser_signature_banned`). `lifi_bridge._headers()` sends a browser-shaped UA Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ **removing it kills every request.** Locked in by `test_requests_send_a_browser_shaped_user_agent`.
  5. **The source step pulls CELO via `transferFrom`** Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ an exact-amount approval to `estimate.approvalAddress` (the LI.FI diamond) is required first. `_lifiEnsureAllowance` does it (never unlimited).
- **Destination pinned to the session wallet.** The quote endpoint rejects a `to_address` that is not the signed-in wallet; the step-tx endpoint re-stamps `from/to` from the session Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a tampered step cannot redirect funds. Same self-custody rule as the rest of the app.
- **Destination-gas preflight aborts BEFORE step 1.** The 2nd signature is on Base and needs ETH there, so the flow stops before the Celo leg is broadcast (a half-finished bridge is worse than a clean refusal). Reads three Base RPCs (`mainnet.base.org`, `base-rpc.publicnode.com`, `1rpc.io/base`); only a *confirmed* low balance blocks Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ an unreadable RPC is skipped.
- **Resume after a closed tab (`#btnLifiResume` / `resumeLifiBridge`).** Once step 1 broadcasts, the Celo funds are already bridged and can sit on Base Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so the pending leg is persisted to `localStorage` (`gm_lifi_pending_bridge_v1`, per-wallet, 7-day TTL, cleared on success) right after the hash exists. `_lifiShowResumeIfPending()` (called from `_prewarmLifiBridgeTab`) surfaces a "Finish on Base" button that re-fetches the quote (the route is deterministic for from/to/amount) and signs **only** step 2. Without this a user who closed the tab has no in-app way to finish and the funds look lost. `_lifiClearPending`/`_lifiShowResumeIfPending` are try/catch-wrapped Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a DOM hiccup in the cleanup must never turn a *successful* bridge into an error banner (it runs at the end of the success path).
- **GMLocalWallet now includes Base** (`SUPPORTED_CHAINS` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `base` 0x2105/8453) so the in-app wallet can sign step 2. It routes `eth_sendTransaction` by the tx's own `chainId`, so no manual switch is needed for local logins. **The Celo switch-back invariant still holds** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `getConnectedSwapSigner`'s local branch switches to 0xa4ec first, so a Celo-bound flow after a Base step still estimates/broadcasts on Celo.
- **UX (two signatures are the design constraint):** a persistent step bar (`#lifiStepDot1/2` + label), per-step button labels ("Confirm on Celo (1 of 2)" / "Confirm on Base (2 of 2)"), and copy that warns up front about the two confirmations + keeping ETH on Base. The whole pane is inside `{% if lifi_bridge_enabled %}` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a disabled deploy renders neither the sub-tab nor the pane.
- **Liquidity ceiling:** ~1Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð â¢Ð¢Ò¡10 CELO routes cleanly; larger amounts can return no route (Glacis price impact > 10%). The UI surfaces LI.FI's own `reason` verbatim ("try a smaller amount") rather than a generic failure.
- **Cross-bundle guard:** `setBridgeSubTab` lives in swap-reserve.js and calls `window._prewarmLifiBridgeTab` in swap-bridge.js (loaded AFTER) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the call is `typeof`-guarded (same rule as the XDC pre-warm). The LI.FI pre-warm is lazy/once-only, triggered on first opening the Celo Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ Base sub-tab.
- **Integrator fee is 0 by default** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ LI.FI rejects a non-zero `LI_FI_FEE` until the integrator is registered at `https://portal.li.fi/`. `options.fee` is only sent when `LI_FI_FEE > 0`.
- Env: `LIFI_BRIDGE_ENABLED`, `LI_FI_INTEGRATOR`, `LI_FI_API_KEY`, `LI_FI_FEE`, `BASE_CHAIN_ID`, `CELO_ERC20_TOKEN`, `LI_FI_BASE_URL`, `LI_FI_QUOTE_CACHE_TTL`, `LI_FI_HTTP_TIMEOUT` (documented in `.env.example`).
- Tests: `tests/test_lifi_bridge_content.py` (25 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ functional drive of the REAL `lifi_bridge.py` against a stubbed `urllib` (route/step-tx/status shapes, native-token choice, cache, LI.FI reason surfacing, the Cloudflare UA guard), routes/template/boot/local-wallet wiring, and a **node harness** driving the REAL `executeLifiBridge` (both steps signed, CeloÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶Base switch, step bar reaches done, and the dest-gas preflight aborts with 0 txs). Run with `python -m pytest tests/test_lifi_bridge_content.py`.
- Proposal + verified API notes: `docs/JUMPER_LIFI_BRIDGE_PROPOSAL.md`.

### LI.FI CELOÐ ÐÐ¢Â¶Ð²ÒÒ·Base `TransferFromFailed` (2026-10)
Native CELO Ð ÐÐ¢Â¶Ð²ÒÒ· ETH (Base) showed a gas estimate and then failed **only on tap** with `Step simulation failed: TransferFromFailed`. Root cause is **LI.FI-side**: it is LI.FI's own answer from `POST /advanced/stepTransaction` for **step 0** (LI.FI simulates its own calldata). Proven NOT our bug: a wallet with an **infinite** allowance to the LI.FI diamond still fails; amounts 0.5Ð ÐÐ¢âÐ²ÒÒ£100 CELO all fail; every integrator id/slippage fails; but an **ERC-20 source (USDC) builds fine** (`1 USDC Ð ÐÐ¢Â¶Ð²ÒÒ· native ETH` = 2 steps, both OK). The **Glacis** facet cannot simulate a `transferFrom` pull of Celo's native-asset ERC-20 (`0x471EcEÐ ÐÐ¢âÐ¢Â°`).
- **App-side fixes shipped (PR #387):** `get_quote` now **pre-builds step 0** (`preflight_route` Ð ÐÐ¢Â¶Ð²ÒÒ· `get_step_transaction`) before returning a quote, **trying each candidate route** so one broken route cannot hide a working alternative; a definite simulation failure returns a friendly message (`_friendly_preflight_error`) instead of a phantom quote, and a **transient** error (rate limit/network) **fails OPEN** (`_is_transient_error`) so nobody is blocked by a rate limit. `updateLifiBridgeSummary` labels each gas cost with its chain (`Ð ÐÐ¢âÐ¢Â° CELO (Celo) + Ð ÐÐ¢âÐ¢Â° ETH (Base)`) and the dest-gas guard says the Base step's gas **cannot be paid in CELO**.
- **Correction to a wrong earlier conclusion:** destination native gas is *normally not needed* (LI.FI docs + a reporter bridged CELOÐ ÐÐ¢Â¶Ð²ÒÒ·ETH on Jumper with no Base ETH), so "pure CELO is impossible" was WRONG. BUT `/quote`'s `filteredOut` list proves **every** live CELOÐ ÐÐ¢Â¶Ð²ÒÒ·Base candidate needs a destination signature (`glacis` Ð âÐ²Òâ3, `eco` Ð âÐ²Òâ6, `allbridge` Ð âÐ²Òâ1), so 2 signatures are unavoidable for this pair today Ð ÐÐ¢âÐ²ÒÒ label it, do not pretend otherwise.
- **Gotchas:** the anonymous bucket (no `x-lifi-api-key`) rate-limits fast and surfaces as "Rate limit exceeded" Ð ÐÐ¢âÐ²ÒÒ never treat that as a route verdict. `/quote` exposes `errors.filteredOut[].reason` (why each candidate was dropped) Ð ÐÐ¢âÐ²ÒÒ far more diagnostic than `/advanced/routes`, which only returns `routes`/`unavailableRoutes`. `LIFI_BRIDGE_ENABLED` defaults OFF.

### LI.FI CeloÐ ÐÐ¢Â¶Ð²ÒÒ·Base Ð ÐÐ¢âÐ²ÒÒ selectable source token + stablecoin fallback (2026-10)
The `TransferFromFailed` dead-end (above) left the user with no way forward in-app. The pane now takes a **selectable Celo source token** and routes around LI.FI's broken native-CELO simulation. Plan + live evidence: `docs/JUMPER_LIFI_CELO_SOURCE_FIX_PLAN.md`; proposal: `docs/LIFI_SOURCE_TOKENS_AND_WALLET_TOKENS_PROPOSAL.md`.
- **Live-verified (2026-10-07/08):** native CELO Ð ÐÐ¢Â¶Ð²ÒÒ· ETH(Base) fails `TransferFromFailed` at EVERY amount (0.5Ð ÐÐ¢âÐ²ÒÒ£100 CELO), on a funded wallet, for every integrator id; the failure follows the **native-CELO source token**, not the destination swap or the Glacis tool (CELO Ð ÐÐ¢Â¶Ð²ÒÒ· USDC(Base) also fails). **USDC/USDT Ð ÐÐ¢Â¶Ð²ÒÒ· ETH(Base) build cleanly in a SINGLE step** (LayerSwap) Ð ÐÐ¢âÐ²ÒÒ one signature, no Base gas, no chain switch. **USDTÐ ÐÐ¢Â¶Ð²ÒÒ·ETH(Base) confirmed live 2026-10-08.** cUSD routes in 2 steps.
- **DEFAULT SOURCE IS `USDT` (2026-10 follow-up).** A native-CELO default sent every first-time user down the failure + fallback path, so the default moved to the working single-step stablecoin. `SOURCE_TOKENS` is ordered `USDT, USDC, cUSD, CELO` so the picker renders working sources first and the broken native source last. **All four tokens are retained** Ð ÐÐ¢âÐ²ÒÒ cUSD is the Phase-2 pre-swap target and CELO is the "I only hold CELO" entry point; deleting either breaks the pre-swap. Guarded by `test_default_source_is_a_stablecoin_not_native_celo` + `test_source_token_order_puts_working_sources_first` Ð ÐÐ¢âÐ²ÒÒ **never make native CELO the default again.**
- **`lifi_bridge.py`** Ð ÐÐ¢âÐ²ÒÒ `SOURCE_TOKENS` registry (`USDT`/`USDC`/`cUSD`/`CELO`) + `resolve_source_token` (case-insensitive allowlist) + `source_token_list`; `build_quote_request(..., from_token=Ð ÐÐ¢âÐ¢Â°)`; `get_quote_ex` returns `(quote, error, error_code)` (`ERROR_NATIVE_CELO_UNAVAILABLE` / `ERROR_INSUFFICIENT_BALANCE` / `ERROR_ROUTE_UNAVAILABLE`); `get_quote` is a back-compat wrapper. **The quote cache key includes the source token** Ð ÐÐ¢âÐ²ÒÒ a CELO quote must never satisfy a USDC request. Addresses env-overridable (`LIFI_CELO_USDC_TOKEN`, `LIFI_CELO_USDT_TOKEN`, `LIFI_CELO_CUSD_TOKEN`).
- **`routes.py`** Ð ÐÐ¢âÐ²ÒÒ `/api/bridge/lifi/quote` validates `from_token` against the allowlist (unknown Ð ÐÐ¢Â¶Ð²ÒÒ· 400, never a free-form address) and forwards `error_code`; `swap_page` passes `lifi_source_tokens`; the diagnostics endpoint probes **every** source token (`token_probes` via `probe_source_token`).
- **`templates/swap.html` + `static/js/swap-bridge.js`** Ð ÐÐ¢âÐ²ÒÒ source-token selector + picker modal; the step bar **hides for a 1-step route** (`_lifiUpdateStepBarForRoute`); on a native-CELO failure a balance-aware **"Bridge USDC/USDT instead"** button (`_lifiOfferFallbackOnFailure`/`_lifiPickFallbackToken`/`lifiUseFallbackToken`) plus an **"Open in Jumper Ð ÐÐ¢Â¶Ð²Òâ"** last resort; `executeLifiBridge` handles the 1-step path (single signature, NO destination-gas preflight, CeloScan link) and keeps the 2-step path.
- **Phase 2 Ð ÐÐ¢âÐ²ÒÒ in-app `CELO Ð ÐÐ¢Â¶Ð²ÒÒ· stablecoin` pre-swap** (`_lifiMaybePreSwapToSource`): when a non-native source is selected with no balance, swap CELO Ð ÐÐ¢Â¶Ð²ÒÒ· the source with the SAME audited Uniswap V3 UniversalRouter + Permit2 path (`ensureWalletPermit2Allowance` / `buildUniversalRouterV3Swap`), then re-quote and continue. **CELOÐ ÐÐ¢Â¶Ð²ÒÒ·cUSD is the only deep CELO/stable V3 pool** (verified via the on-chain quoter across fee tiers 100/500/3000/10000; CELOÐ ÐÐ¢Â¶Ð²ÒÒ·USDC/USDT have zero liquidity), so if the chosen token has no pool the flow swaps to cUSD and bridges that Ð ÐÐ¢âÐ²ÒÒ the swap-core `TOKENS` key is uppercase (`CUSD`), hence `LIFI_SWAP_TOKEN_KEY`.
- **Do NOT** re-add a native-CELO-only path, hide the source selector, or drop the 1-step branch Ð ÐÐ¢âÐ²ÒÒ and never derive the fallback by string-matching the error copy (use `error_code`).
- Tests: `tests/test_lifi_bridge_content.py` (46 tests Ð ÐÐ¢âÐ²ÒÒ backend request shapes vs a stubbed urllib incl. per-token requests/cache-key/error codes, routes/template/boot wiring, plus **two node harnesses** driving the REAL `executeLifiBridge`: the 2-step flow and the 1-step USDC + native-CELO-fallback flow; plus default-is-a-stablecoin / source-order guards). No deps; run with `python -m pytest tests/test_lifi_bridge_content.py`.

## Wallet tokens Ð ÐÐ¢âÐ²ÒÒ USDC display + send (2026-10)
USDC was already fetched by `/api/wallet/balances` (`blockchain.get_usdc_balance`, 6 decimals) and held in `usdcBal` for MiniPay CIP-64 gas, but it had **no tile and no send path** ("USDC is not displayed as a main wallet tile yet"). USDT already had both. Now both are first-class wallet tokens.
- **`templates/wallet.html`** Ð ÐÐ¢âÐ²ÒÒ USDC balance tile (`#usdcBal` / `#usdcBalUSD`) next to USDT in the Crypto tab, and a `USDC` send chip (`selectToken('USDC', this)`).
- **`static/js/wallet-main.js`** Ð ÐÐ¢âÐ²ÒÒ `loadBalances` renders the USDC tile (was memory-only); `getSelectedTokenMeta` gains `USDC: { label: 'USDC', network: 'celo', chainId: 42220, Ð ÐÐ¢âÐ¢Â° }`; `setMaxSend` includes `USDC: usdcBal`.
- **`blockchain.py`** Ð ÐÐ¢âÐ²ÒÒ new `prepare_usdc_transfer_data(to, amount)` (6 decimals, mirrors `prepare_usdt_transfer_data` exactly; `USDC_CONTRACT` already existed).
- **`routes.py`** Ð ÐÐ¢âÐ²ÒÒ `elif token == "USDC":` branch in `wallet_prepare_send` (Celo path, not XDC).
- **MiniPay:** USDC already had a CIP-64 adapter (`USDC_ADAPTER` / `MINIPAY_FEE_CURRENCY.USDC_ADAPTER`) and is in the stablecoin fee-currency preference list, so a USDC send works with the existing fee-currency logic.
- **Scope:** Celo USDC/USDT only Ð ÐÐ¢âÐ²ÒÒ XDC has its own `XUSDT` path (`/api/xdc/prepare-send`), untouched. USDC is deliberately **not** added to the swap page's `TOKENS` (no CELOÐ ÐÐ¢Â¶Ð²ÒÒUSDC V3 liquidity Ð ÐÐ¢âÐ²ÒÒ pointless).
- Tests: `tests/test_wallet_usdc_token_content.py` (8 tests, no deps Ð ÐÐ¢âÐ²ÒÒ functional drive of the REAL `prepare_usdc_transfer_data` with stubbed web3/eth_abi asserting the `transfer(address,uint256)` selector + 6-decimal encoding, plus tile/chip/meta/max/render wiring). Run with `python -m pytest tests/test_wallet_usdc_token_content.py`.



## "Install app" (PWA) Ð²Òâ revived dead install code + installability fixes (2026-10)
The install code only ever lived inside `templates/login.html`, which is **unreachable** (`/login` Ð²Ò¶â `redirect(url_for("routes.index"))`), so it could never run. It was revived into a shared module and wired to two surfaces. Separately, the app was **not actually installable**: the manifest had one 512px icon (no 192px) and no `scope`/`id`, and the service worker was registered **without an explicit scope** (so it only controlled `/static/`, not the manifest's `start_url` `/`).
- **Installability fixes:** `static/manifest.json` gained a **192px icon** (both sizes are required by Chrome), `"scope": "/"`, `"id": "/"`, `orientation`, and split `any`/`maskable` icon purposes. `main.py` `_dynamic_service_worker` now sends **`Service-Worker-Allowed: /`** (was `/static/`), and every registration passes **`{ scope: '/' }`** (`static/js/pwa-install.js` + `templates/dashboard.html`). **Both are required** Ð²Òâ the header alone doesn't set the scope, the option alone is rejected without the header. Keep `background_color`/`theme_color` (`#f7f1e8`/`#e8720a`) Ð²Òâ `test_wallet_dark_purple_theme_content.py` pins them.
- **`static/js/pwa-install.js`** (`window.GMPwaInstall`) Ð²Òâ `beforeinstallprompt` capture, standalone/iOS detection (`display-mode: standalone|minimal-ui` + `navigator.standalone` + iPadOS-as-Mac touch detection), `promptInstall()`, `handleInstallClick()` (native sheet, else **manual instructions** Ð²Òâ iOS Share menu vs Android browser menu Ð²Òâ so the tap is never a dead end), root-scope SW registration with legacy root-worker cleanup, and **declarative banners** driven by `data-gm-pwa-*` attributes (no inline handlers Ð²Ò¶â CSP-friendly).
- **Two banner modes:** `data-gm-pwa-mode="once"` (seen-flag written **at reveal**, same one-shot rule as the join-community banner) and `"persistent"` (shown until installed or dismissed; dismissal persisted). Both are hidden entirely when the app is already installed.
- **Surfaces:** the homepage uses a **strict full-screen install gate** when `PWA_INSTALL_ENABLED` is on: ordinary browser tabs stay hidden until standalone/install confirmation, while injected-wallet and DApp-browser sessions bypass the gate. The wallet keeps its **once** banner, gated to local logins. Gate detection covers standalone/minimal-ui, iOS standalone, EIP-1193 providers, EIP-6963 announcements, late wallet injection, and common wallet WebView signals. A local-storage prompt-accepted flag alone must never unlock homepage content. Styles live in `static/css/pwa-install.css`; the gate has no dismiss control. PWA flag remains default ON.
- **Do not** re-inline install code into a page, drop the `scope: '/'`, or remove the 192px icon. Tests: `tests/test_pwa_install_content.py` (25 tests, no deps Ð²Òâ installability (icons/scope/id/header), a **Jinja render harness** proving the wallet banner shows only for local logins, and a **node harness** driving the REAL module: once-vs-persistent reveal + persistence, standalone suppression, accepted-prompt Ð²Ò¶â installed + hidden, iOS/Android manual hints, and root-scope SW registration). Run with `python -m pytest tests/test_pwa_install_content.py`.

- **Strict PWA gate regression cases:** keep ordinary-browser content locked until `appinstalled` or standalone display mode; prompt acceptance/localStorage alone does not unlock it. Verify standalone, injected EIP-1193, EIP-6963, late provider, wallet-WebView, and normal-browser paths. The DApp bypass check is intentionally brief and must not flash the homepage first.

## Referral verification gate Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ self-healing (2026-10)
Verified referees were still blocked with **"Referee not verified yet
(not_onchain_verified)"** and the referrer's 1,000 G$ never arrived. The suffix
`not_onchain_verified` means the Identity read returned a clean `false` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ NOT an
RPC error Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so the wallet was *read* as unverified. Four causes, all fixed:
- **A `false` Identity read was cached for the same 30-minute TTL as a positive
  one.** `is_identity_verified` is read the instant the FV callback lands, often
  BEFORE GoodDollar's `addWhitelisted` tx is mined, so that first `false` pinned
  "not verified" for half an hour across every trigger. Now
  `IDENTITY_CACHE_TTL` (1800s) applies only to `verified: true`;
  `IDENTITY_NEGATIVE_CACHE_TTL` (env, default **60s**) applies to `false`. New
  `force=True` bypasses the cache entirely Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ every money-gating payout path
  passes it.
- **The Identity read had no RPC failover** (one forno node). It now fans out
  over `_celo_rpc_urls()`; a transport error on EVERY node returns
  `{"verified": False, "error": ...}` and is **never cached** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so "we could
  not ask" is never persisted as a verdict.
- **A confirmed Celo UBI claim is now accepted as proof of whitelisting**
  (`UBIScheme.claim()` reverts for a non-whitelisted caller). New
  `blockchain.has_confirmed_ubi_claim(wallet, days)` reads
  `goodmarket_claim_events` (`claim_tx_confirmed`, `network='celo'`) and
  **re-verifies each tx hash on-chain** (receipt status 1 AND a G$ Transfer from
  the UBI proxy) before accepting it, because `/api/claims/v2/confirm` takes a
  client-supplied status Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a DB row alone must not unlock a payout.
  `is_referee_onchain_verified` now returns `source: identity | ubi_claim`.
  Window is env-tunable (`REFERRAL_UBI_CLAIM_EVIDENCE_DAYS`, default 30).
- **Stale error text was never cleared.** `update_referral_status` /
  `update_referral_status_by_id` gained `clear_error=True` (writes
  `error_message=None`); `verify_and_disburse_referral` clears it the moment the
  gate passes (while leaving the CAS-claimed row in `disbursing`).
- **An infra failure is no longer reported as "unverified":**
  `auto_disburse_pending_referral` returns
  `referee_verification_unavailable` (not `referee_not_onchain_verified`) on
  `onchain_check_error`, and the blocked-payout note says "check unavailable
  (RPC error) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ will retry".
- **Trigger gap closed:** `verify-identity` used to call the disburser only when
  its OWN cached read said verified, so a stale `false` meant the referral was
  never even retried. It now always calls it (`onchain_confirmed=bool(...)`
  only skips the duplicate RPC read). `fv-callback`'s own read is `force=True`.
- **Admin one-click repair:** `POST /api/admin/referral/recheck-verification`
  (`recheck_verification_blocked_referrals`) forces a fresh read over rows with
  status `pending_face_verification` or the stale error text, disburses the ones
  that now pass, and clears the text. Button "Ð¡ÒÐ¡Ò¹Ð²ÒÒÐ Ò¢ Re-check Verification" in the
  Admin dashboard referral section.
- **Do NOT** re-unify the positive/negative TTL, remove `force` from a payout
  path, accept a `goodmarket_claim_events` row without re-verifying the tx, or
  cache a transport error. Tests:
  `tests/test_referral_verification_self_healing_content.py` (32 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶
  functional drive of the REAL `blockchain` + `ReferralService` with stubbed
  requests/web3/supabase; verified it fails against the pre-fix code). Proposal:
  `docs/REFERRAL_VERIFICATION_SELF_HEALING_PROPOSAL.md`.

## Community Rotating Savings â Paluwagan (2026-10)
A 5-member rotating savings group ("paluwagan"): each member contributes a fixed
amount every week and one member receives the pooled contribution each week in a
fixed payout order; the rotation repeats for the whole commitment period. This
is a SAVINGS-GROUP arrangement â there is **no borrowing, lending, interest or
loan pool**, and no reward pool (no approved funding source exists).

**FULLY ON-CHAIN.** The contract is the sole authoritative record. There is NO
Supabase table, no SQL migration, no scheduler, no server-side key and no
off-chain financial logic. The Flask blueprint is a thin READ-ONLY bridge; every
state-changing action is signed by the user's own wallet in the browser.

### Separation from the existing G$ Savings feature (the core requirement)
- **A separate standalone contract** `contracts/GoodMarketPaluwagan.sol` â not a
  module of `GDSavings`, not a shared vault. It holds ONLY its own group
  contributions, in its own account, and has **no reference to the savings
  contract**.
- **No privileged control surface:** no owner, no pause, no emergency
  withdrawal, no fee, no upgrade path. That is deliberate: an admin who could
  move group funds would break the "funds reserved for payouts are never
  diverted" guarantee. A standalone contract is the strongest separation here
  precisely BECAUSE it removes the ability to touch the existing system â a
  shared/modular design would put the savings vault's storage and its privileged
  functions one bug away from the new feature.
- `savings/`, `templates/savings.html` and the `GDSavings` contract are left
  **byte-for-byte untouched** (asserted by tests). The savings backend contains
  no reference to paluwagan and vice versa.
- Group balances are NEVER merged into the personal savings balance in the UI â
  `/paluwagan` is a distinct page with its own teal accent, and the wallet nav
  entry is a separate row from "Savings".

### Contract model (`contracts/GoodMarketPaluwagan.sol`)
- `MEMBERS = 5` (constant). `createGroup` takes the token, weekly amount, weeks
  and a `uint8[5]` payout order; `weeks` must be a **positive multiple of 5** so
  every round has exactly one recipient.
- **`weeks` is a reserved Solidity time-unit keyword** â the field/param is
  named `periodWeeks`. Never rename it back.
- **Group names (2026-10):** `Group.name` is contract state set at creation;
  `mapping(bytes32 => uint256) _groupIdByName` is a **case-insensitive** registry
  (`_lowercase` â keccak) so `TeamBetz` and `teambetz` collide on purpose.
  `getGroupIdByName(string)` resolves any casing (returns `0`, never reverts â
  it uses the non-validating `_slugToKey` path, because a deep link is user
  input). `getGroupName(uint256)` returns the display name. Rules: 3â32 bytes,
  `a-z A-Z 0-9 -` only (`bad_name_length` / `bad_name_char`); the restricted
  alphabet keeps slugs URL-safe. `GroupCreated` carries the name so history
  stays rebuildable from events alone.
- **The numeric group id remains the financial identity.** Every money-moving
  call (`joinGroup`/`contribute`/`coverFor`/`claimPayout`) keys off the id; the
  name is only a lookup alias resolved once at the edge. Never make a payout
  depend on a name.
- **Commitment periods:** 50 weeks (~12 months) or 100 weeks (~24 months). 50
  weeks = 10 rotations, 100 weeks = 20 rotations. 50 is recommended for v1.
- `joinGroup` fills the next free slot (max 5); `activateGroup` starts the
  rotation and is only callable once all five members have joined.
- `contribute` pulls the fixed weekly amount via `transferFrom` (exact-amount
  approval; no unlimited approval needed). `coverFor` lets a member pay another
  member's round (recorded as `ContributionCovered` with the payer).
- **Payout safety:** `claimPayout` requires the **full pool** collected for that
  round (`roundCollected == pool`), requires the caller to be that round's
  recipient, and sets `claimed = true` BEFORE the transfer
  (checks-effects-interactions + `nonReentrant`) so a payout can never be
  duplicated or released early.
- `markMissed` records a miss only after the round deadline + grace period;
  three consecutive misses set a `defaulted` flag. **Funds already contributed
  are NOT confiscated** â they stay in the round accounting; defaulting blocks
  that member's future payouts and is visible on-chain. What a contract CANNOT
  do is force a defaulting member to keep paying: that risk is disclosed, not
  papered over.
- `advanceRound` is permissionless once the round deadline + grace has passed
  (the rotation cannot stall on one absent member).
- `proposeEarlySettle` / `approveEarlySettle` require **unanimous** (5/5) member
  approval â never an admin decision.
- Uses OpenZeppelin `SafeERC20` + `ReentrancyGuard`.

### Backend (`paluwagan/`, read-only)
- `blockchain.py` â Celo RPC access: `get_group`, `get_group_overview`,
  `get_my_groups` (from `MemberJoined` logs), `get_contributions` (merges
  `ContributionMade` + `ContributionCovered`), `get_payout_history`
  (`PayoutClaimed`), token/native balances. Event scans anchor at
  `PALUWAGAN_DEPLOYMENT_BLOCK` and route through `blockchain._get_logs_full`
  (inherits the 5000-block `eth_getLogs` cap handling + RPC failover). Every
  read is individually guarded and degrades to `[]`/`None` rather than raising.
- `routes.py` â `/paluwagan` page + read-only JSON (`/api/config`,
  `/api/groups`, `/api/group/<id>`, `/api/group/<id>/contributions`,
  `/api/group/<id>/payouts`, `/api/balances`). **No route signs anything.**
  Also the shareable link `GET /paluwagan/<name>` (renders the page with the
  group pre-filled; the frontend resolves the slug on-chain) and
  `GET /paluwagan/api/resolve/<name>` (404 unknown, 400 malformed slug via
  `NAME_RE`). **Flask ranks the concrete `/api/...` rules above the `<name>`
  converter, so the API cannot be shadowed** â verified; keep the API routes as
  concrete rules, never fold them into `<name>`.
- `__init__.py` â `init_paluwagan(app)` registers the blueprint (wired in
  `main.py` right after G$ Savings).

### Frontend
- `templates/paluwagan.html` + `static/js/paluwagan.js`. Signer routing mirrors
  the rest of the app: **local logins PIN-unlock `GMLocalWallet`** (never an
  injected extension â a different account), WalletConnect via the WC bridge,
  else injected bound to the account matching the session wallet. Reads use a
  public `FallbackProvider` (network object, not a bare number) so they never pop
  a wallet prompt.
- **Every tx sends explicit `gas` + `gasPrice`** (estimate â 40% pad â fallback
  gasPrice) â the repo-wide lesson that a bare `eth_sendTransaction` can surface
  a red "network fee unavailable" and strand the flow.
- The UI shows the **group amount and the network gas fee as two separate
  numbers**, states there is no relayer/gas sponsorship, and requires the user's
  own CELO for gas.
- Guests reuse the shared `_get_started_modal.html` connect modal; the page is
  intentionally NOT behind the face-verification gate (like `/savings`).
- **The wallet nav entry is injected from `wallet-bottomnav.js`**, not inlined
  into `wallet.html` â that template is served no-cache and sits on a hard
  200 KB budget. It is gated on the public `paluwagan_visible` flag.

### Feature flag + config
- Admin â Feature Visibility â **Community Savings (Paluwagan)**
  (`paluwagan_feature`), exposed on both the admin and public
  `/api/feature-visibility` endpoints (default visible).
- Env: `PALUWAGAN_CONTRACT`, `PALUWAGAN_DEPLOYMENT_BLOCK`, `PALUWAGAN_ENABLED`
  (kill-switch, default ON), `PALUWAGAN_LOOKBACK_BLOCKS`. Deploy with
  `PALUWAGAN_DEPLOY_KEY=... python contracts/deploy_paluwagan_contract.py`.
- **Requires a contract deploy** before the page is functional; without
  `PALUWAGAN_CONTRACT` the routes return `not_configured` and the UI says so.

### Token scope
v1 is **G$ (Celo) only** â `SUPPORTED_TOKENS` has one entry. CELO/cUSD/USDT are
deliberately deferred: the contract itself is token-agnostic, but a v1 launch on
one audited 18-decimal token removes the 6-decimal (USDT) class of bugs. Adding
a token means extending the registry + verifying the address and decimals.

### Rules (do not regress)
- Never give the paluwagan contract an owner, pause, emergency withdrawal or
  upgrade path, and never let it reference the savings contract.
- Never add an off-chain database/table for group or financial state.
- Never sign server-side or add a relayer/gas-sponsor for this feature.
- Never release a payout before the full pool is collected, and never allow a
  second claim for a round.
- Never make early settlement an admin decision â it is 5/5 unanimous.
- Never merge group balances into the personal savings balance in the UI.
- Tests: `tests/test_paluwagan_content.py` (39 tests, no deps â functional drive
  of the REAL `paluwagan/blockchain.py` with stubbed web3: group decoding,
  token decimals, per-member standing, contribution/payout history from events,
  my-groups filtering, on-chain name resolution, graceful degradation when
  unconfigured; plus contract safety invariants, the savings-separation
  assertions both ways, no-off-chain checks, feature-flag/nav/admin wiring, the
  named deep-link route + resolve API, and the wallet.html budget guard).
  Run with `python -m pytest tests/test_paluwagan_content.py`.
- Proposal: `docs/PALUWAGAN_COMMUNITY_SAVINGS_PROPOSAL.md`;
  mockups: `docs/mockups/paluwagan_mockup.html`.

### Paluwagan â "Please log in to sign this transaction." (2026-10)
A logged-in user filled in the create-group form, tapped Create, and got
**"Please log in to sign this transaction."** Two independent defects, both in
`templates/paluwagan.html`:

1. **`window.GM_PALUWAGAN_BOOT` was emitted AFTER `paluwagan.js`.** The bundle
   snapshotted `boot.wallet` / `boot.loginMethod` into `WALLET` /
   `LOGIN_METHOD` at **parse time**, so both were `""` forever and
   `resolveSigner()` bailed on its first guard â **for every login method**.
   This is the exact landmine documented for `dapp-connect.js`. Fixes:
   - The boot `<script>` now precedes the bundle (matching swap/wallet/farming),
     so the config is present when the bundle is evaluated.
   - **Defence in depth:** `paluwagan.js` gained `_syncBoot()`, which re-reads
     the live boot object and is called at the top of **`resolveSigner()`** and
     `_boot()`. **Never snapshot `GM_PALUWAGAN_BOOT` into module-scope vars
     again** â read it through `_syncBoot()` (or a lazy accessor) so a future
     reordering can't silently break signing. Only overwrites a value when the
     boot config actually carries one, so a guest (empty wallet) still hits the
     guard.
2. **The ethers `<script>` had a broken SRI.** It requested **6.7.0** while
   carrying the **sha512 of 6.13.4 mislabelled as `sha384-`** (88-char digest
   under a 64-char algorithm), so the browser refused ethers entirely and
   `ethers` was `undefined` â no transaction could be signed at all. Now
   **6.13.4 + the repo-standard verified `sha384-6Zl0Pc8zjâ¦`**, matching
   swap/claim/savings-siblings. **A digest body must match its declared
   algorithm's length** (sha256 44 / sha384 64 / sha512 88 chars).

Both bugs are independent: fixing only the ordering still leaves a page where
`ethers` never loads. Tests (`tests/test_paluwagan_content.py` Part 4 â 4 new,
**all fail on the pre-fix code**): a **node harness over the REAL
`window.createGroup()`** that loads the bundle with NO boot object, installs it
afterwards, and asserts the injected / local / WalletConnect paths each reach
the wallet and sign with the **right** signer while a real guest is still
refused (it reproduces the exact reported string on the buggy code); plus
ordering, lazy-boot and SRI-hash guards. Run with
`python -m pytest tests/test_paluwagan_content.py`.

### Paluwagan â a created group was invisible in "My Groups" (2026-10)
Report: "after mag create ng group at magbayad ng gas yung group hindi nakikita"
â but `https://goodmarketph.live/paluwagan/goodmarketteam` DID load the group
with the creator already shown as joined, and members who joined later never
appeared in the count. The contract was fine the whole time; the **event scan
that powers "My Groups" was silently failing**, while the deep link used a
direct view call (`getGroupIdByName` â `getGroup`) that needs no log scan.

- **Root cause â the chunk math in `paluwagan/blockchain.py::_chunked_logs`.**
  The initial step was `max(10_000, GETLOGS_MAX_CHUNK)` (10000) and the adaptive
  retry only fired when `step > 10_000`, so on a provider capping at ~5000
  blocks (forno: *"query exceeds range"*) the **first chunk was already too
  large and the retry could never shrink**. Every scan raised, every caller
  swallowed it, and `/paluwagan/api/groups` returned `[]` â "No groups yet".
  Fix: initial step is now `max(GETLOGS_MIN_CHUNK, GETLOGS_MAX_CHUNK)` (4000)
  and the halving floor is the new **`GETLOGS_MIN_CHUNK` (default 500)**, so the
  scan actually adapts instead of giving up. **Never set the initial step above
  `GETLOGS_MAX_CHUNK` and never hardcode a floor the retry cannot cross** â
  this is the same class of bug the wallet tx-history fix hit.
- **Deploy-block anchoring (`get_deploy_block`).** `_from_block()` now prefers
  `PALUWAGAN_DEPLOYMENT_BLOCK`, else discovers it once via an `eth_getCode`
  binary search (cached), and only then falls back to
  `PALUWAGAN_LOOKBACK_BLOCKS`. Membership discovery scans **four** event types
  now, so an unanchored scan would walk hundreds of thousands of blocks on every
  request. **Set `PALUWAGAN_DEPLOYMENT_BLOCK` in production** â it is the
  difference between one chunk and a hundred.
- **Discovery unions every member-indexed event, not just `MemberJoined`**
  (`_MEMBER_EVENT_FILTERS`): `MemberJoined`/`ContributionMade`/
  `ContributionLate` on `member`, `PayoutClaimed` on `recipient`. A wallet that
  created, contributed or claimed is found from the first action it took, so a
  group cannot be missed because the join log fell outside the scan window.
  Each event scan is isolated (`_groups_for_wallet_event`), so **one unreadable
  event type can never blank the whole list**.
- **The group view now polls (`static/js/paluwagan.js`).** The member list and
  contribution progress change when OTHER wallets join or pay, and the page only
  ever rendered the state from the moment it was opened â which is why "kapag
  may sasali iba diko na makikita kung ilan na nagjoined". `refreshGroup({silent})`
  gained a silent mode (no spinner flash over the history tabs, no error toast)
  and a 15s `setInterval` re-reads the open group, **paused while
  `document.hidden` or a tx is in flight**, plus an immediate refresh on
  `visibilitychange` so a returning tab feels instant.
- Tests: `tests/test_paluwagan_content.py` (`test_chunked_logs_survives_providers_range_cap`,
  `test_my_groups_works_against_a_range_capped_provider` â a `_RangeCappedEvents`
  harness that reproduces forno's 5000-block cap end-to-end,
  `test_membership_is_discovered_from_contributions_and_payouts`,
  `test_one_broken_event_scan_does_not_blank_the_list`,
  `test_js_polls_the_open_group_so_new_members_appear`). The first three **fail
  on the pre-fix code** (verified by mutation). 48 tests, no deps; run with
  `python -m pytest tests/test_paluwagan_content.py`.

### Paluwagan â G$ balance read + freeing an inactive member's slot (2026-10)
Two reported bugs, one of which needed a contract change. Proposal:
`docs/PALUWAGAN_BALANCE_AND_MEMBER_REMOVAL_PROPOSAL.md`.

- **"G$ balance is not fetching, so the G$ token cannot be seen" â the ABI was
  the ethers string form.** `paluwagan/blockchain.py` declared `ERC20_ABI` as
  human-readable strings (`'function balanceOf(address owner) view returns
  (uint256)'`). That is an **ethers.js** convenience; **web3.py v7 raises**
  `ValueError: Could not format invalid value [...] as field 'abi'`, so
  `get_token_balance()` raised, logged a warning and returned **0** â while CELO
  (read via `eth_getBalance`, no ABI) displayed correctly. Fixed to a JSON ABI.
  Verified live: `0 G$` â `344,792.15 G$`. **Never use the human-readable string
  form in Python** â only the JS bundles may.
- **web3.py v7 REJECTS a non-checksummed address** (`InvalidAddress`) instead of
  coercing, so a lowercase session wallet raised inside the contract call and
  `get_my_groups()` silently returned `[]` (logged "failed to load group N").
  New `_to_checksum(addr)` (returns the input unchanged when it cannot checksum)
  is applied to the per-member standing reads, the balance reads and the event
  topic filters. Sessions are checksummed today but nothing guarantees it, so
  **normalise at the boundary**.
- **Removing an inactive member required a contract change â it was impossible
  before.** The deployed `0xa22ac994â¦` has **no** `leaveGroup`/`removeMember`/
  `replaceMember`/`kickMember`, and cannot be upgraded (no owner, no proxy, no
  pause â by design). `contracts/GoodMarketPaluwagan.sol` is now **v2**:
  - `leaveGroup(groupId)` â self-service, **Forming only**.
  - `removeMember(groupId, member)` â **creator only, Forming only, and only
    after `REMOVAL_GRACE_PERIOD` (2 days)** since that member joined; the
    creator cannot remove themselves.
  - `getGroupCreator` / `memberJoinedAt` views, and `MemberLeft`/`MemberRemoved`
    events (so history stays rebuildable from logs alone).
  - **Forming-only is the load-bearing scope:** contributions begin at
    activation, so by construction no funds exist and no rotation can be broken.
    After `Active` the only exit is unanimous `approveEarlySettle`. The creator
    still gets **no fund access, no rule change, no pause** â the contract keeps
    its no-owner/no-pause/no-emergency-withdraw/no-upgrade design.
  - **Two subtleties that make slot reuse work:** `joinGroup` must fill the
    **lowest free slot** (`_firstFreeSlot()`), not append at `memberCount`, and
    `_isMember` must **scan all five slots** â once a hole exists `memberCount`
    is no longer a valid upper bound. `_freeSlot` also clears the member's stale
    standing (`paid`/`missedRounds`/`consecutiveMisses`/`isDefaulted`/
    `memberJoinedAt`) so the next occupant does not inherit it.
  - Verified by compiling (solc 0.8.21, 12,716 bytes) and driving a **real EVM**
    (eth-tester/PyEVM): 21 scenarios pass, including grace-period refusal,
    non-creator refusal, self-removal refusal, lowest-free-slot reuse, rejoin
    after removal, and `not_forming` reverts once Active.
- **`supports_member_removal()` is a BYTECODE selector scan of the DEPLOYED
  contract** (`eth_getCode`, selectors pinned as literals), cached per address
  and **failing closed** on an RPC error. It is the authoritative gate: the UI
  must never offer a button that would revert on the old deployment. Verified
  live â `False` for `0xa22ac994â¦` (v1), `True` for a v2 contract on an
  in-memory chain. `get_group_overview()` adds `removal_supported`, `creator`,
  `member_joined_at`, `can_leave`, `can_remove`, each v2 read individually
  guarded so the overview still returns on v1. Membership discovery now unions
  `MemberLeft`/`MemberRemoved` too.
- **UI:** a gold **Creator** badge, a **Leave** button on your own slot, and a
  **Remove** button on a removable member's slot â all hidden when
  `removal_supported` is false, so a v1 deployment shows a clean lobby with no
  dead controls. Both handlers go through the shared `_sendContractCall`
  (explicit `gas` + `gasPrice`).
- **A v2 deploy is required for removal.** Until then the UI correctly shows no
  controls. `PALUWAGAN_REMOVAL_GRACE_PERIOD` (env, default 172800) must match the
  contract's constant.
- **v1 keeps working with NO v2 deploy â this is the whole point of the probe,
  and it is verified live.** Merging this change does not break a deploy that
  leaves `PALUWAGAN_CONTRACT` pointing at v1 (`0xa22ac994â¦`): the overview still
  returns with every pre-existing field, `get_token_balance()` reads the real
  balance (346,480 G$ verified live), `get_my_groups()` finds the wallet's groups
  from a lowercase wallet, and **no Remove/Leave control is offered**. Three
  independent safety layers: (1) `_add_removal_info` **early-returns** on
  `removal_supported is False`, so the v2-only `getGroupCreator`/`memberJoinedAt`
  calls are never even attempted; (2) the probe **fails closed** on any RPC error;
  (3) the JS gate is `boot.removalSupported === true`, so an absent/undefined flag
  (old cached HTML + new JS) is **false**, never a dead button. Do not remove the
  early return â the v2 reads would raise on v1 (mutation-verified: removing it
  fails 3 tests).
- Tests: `tests/test_paluwagan_content.py` (+26, 74 total, no deps â ABI shape +
  a real balance value, wallet normalisation, lowercase-wallet group discovery,
  the bytecode probe incl. one-of-two/RPC-error/unconfigured, pinned selectors,
  overview removal states, `MemberRemoved` discovery, and the contract
  invariants). Mutation-verified: reverting the ABI fails 26 tests, dropping the
  JS gate fails the gate test, dropping the creator check fails the invariant
  test. A **node harness** drives the REAL `renderGroup` lobby markup for six
  scenarios (creator/member/grace/active/v1/v2). Run with
  `python -m pytest tests/test_paluwagan_content.py`.


### Paluwagan â on-chain fetch, loading locks, intent-aware invite link (2026-10)
Three reported problems: slow on-chain fetching, buttons staying live while a
request is in flight, and the invite deep link dumping members on the generic
join form. Proposal: `docs/PALUWAGAN_ONCHAIN_FETCH_AND_DEEPLINK_PROPOSAL.md`.

- **Fetch was unbounded and uncached.** `PALUWAGAN_LOOKBACK_BLOCKS` defaulted to
  **500_000** â one list load fired ~750 serial `eth_getLogs` calls. Now
  `PALUWAGAN_LOOKBACK_BLOCKS_DEFAULT = 120_000` (~7 days of Celo blocks) and the
  `eth_getCode` deploy-block search is bounded by `PALUWAGAN_DEPLOY_SEARCH_SPAN`
  (2M blocks) so a missing env var can never walk the chain. **Set
  `PALUWAGAN_DEPLOYMENT_BLOCK` in production** â it is the difference between one
  chunk and a hundred.
- **Read caching (`_cache_get`/`_cache_set`)**, namespaced per wallet/group, with
  `invalidate_wallet_cache`/`invalidate_group_cache`. TTLs:
  `PALUWAGAN_CACHE_TTL_{GROUPS,OVERVIEW,HISTORY,BALANCE}` (20/10/30/15s). **A
  failed read and a deadline-truncated read are NEVER cached** â otherwise a bad
  or partial value would be pinned for the whole TTL.
- **Soft read deadline** (`PALUWAGAN_READ_DEADLINE`, 12s). `_Deadline` is
  thread-local; `_carry_deadline(fn)` propagates it into pool workers (they are
  different threads â an unwrapped call would run without a budget). On expiry
  `_chunked_logs` stops issuing RPC calls and the route returns `partial: true`
  instead of running into gunicorn's 120s worker kill. `_timed_read` in
  `routes.py` installs the deadline and reports `partial`/`cached`/`took_ms`.
- **Concurrency:** the 6 member-event scans and the per-group overviews run in
  `ThreadPoolExecutor`s (`get_my_groups`, `get_group_overview`).
- **`POST /api/invalidate`** is session-scoped (keys off `_session_wallet()`,
  never a client address). The page calls it after every confirmed write
  (`createGroup`/`joinGroup`/`leaveGroup`/`removeMember`/`activateGroup`/
  `doContribute`/`claimPayout`) so the next poll is fresh.
- **Loading locks (Part B):** `lockButton(btn, label)` disables + shows a spinner
  and returns a `restore()` called in every handler's `finally`; `_lockSection`
  remembers `data-was-disabled` so unlock restores the exact prior state (some
  buttons are legitimately disabled by their own logic). `_fetch` counts
  in-flight requests and `_pollGroupOnce` skips while `busy || _inFlight > 0`, so
  a background poll can never disable a control the user is about to tap.
  Skeleton loaders (`.skeleton`/`.skel`, transform/opacity-free shimmer,
  reduced-motion safe) replace the spinner text.
- **Intent-aware invite link (Part C):** `/paluwagan/<name>` and `?join=` render
  `#secInvite` as the ACTIVE pane server-side (`paluwagan_invite` in
  `_render_paluwagan`) so there is no flash of the generic join form. `_routeInvite`
  resolves the group on-chain and: an **existing member** â `openGroup(gid)` (the
  team dashboard); **non-member** â the invite card + Join action; each blocked
  state gets its own honest copy (`_inviteMessage`: not_found / full / started /
  guest). A guest on a deep link does NOT get the connect modal auto-opened.
- **Do not** raise the lookback default, remove the deadline/caching, cache a
  partial read, or route a deep link back to the generic join form.
- Tests: `tests/test_paluwagan_content.py` (+23, 97 total, no deps â caching,
  bounded lookback/search, deadline propagation + no-cache-on-partial,
  concurrency, invalidate scoping, loading-lock/invalidate wiring, invite
  routing content, and a **node harness** driving the REAL `_boot()`/`_routeInvite()`
  across member/non-member/guest page loads; mutation-verified). Run with
  `python -m pytest tests/test_paluwagan_content.py`.

### Paluwagan â onboarding guide overlay (Notion embed) (2026-10)
A tutorial guide (a Notion page) shown in a **full-screen overlay before the page
content**, then re-openable from a "How it works" button in the overview card.
Module: `static/js/paluwagan-guide.js` (`window.openPaluwaganGuide` /
`closePaluwaganGuide`), kept OUT of `paluwagan.js` on purpose â that bundle is
the signing/navigation logic driven by a node harness; the guide is pure
presentation.
- **`<iframe ... />` is INVALID HTML and must never be used.** `<iframe>` is not
  a void element, so a self-closing slash is ignored and **every following
  sibling** â including the boot config and all the `<script>` tags â is parsed
  as an iframe CHILD and never renders. Proved with html5lib: the page came out
  with an empty body after the frame. Always `<iframe ...></iframe>`, and
  `test_guide_overlay_renders_without_swallowing_siblings` parses the RENDERED
  page and asserts the iframe has zero children â a static grep passes on broken
  HTML, a parse does not.
- **CSP:** `notion.site` had to be added to `main.py` `frame-src`
  (`https://*.notion.site`) or the frame renders as a blank box with only a
  console error. Notion serves `frame-ancestors https: http:`, so embedding is
  permitted â only our own CSP blocked it. The test **strips comments** before
  asserting: the explanatory comment also names the host, and a comment must
  never satisfy the check (found by mutation testing â the first version of the
  test passed with the directive removed).
- **The iframe is LAZY:** `data-src` only, copied to `src` on first open. The
  Notion embed pulls ~11 external scripts, so a member who never opens the guide
  never pays for it. Never put an eager `src` on the frame.
- **Auto-open is gated three ways:** (1) once per browser â
  `localStorage['gm_paluwagan_guide_seen_v1']`, **written at reveal, not at
  close**, so closing it without reading does not bring it back (same one-shot
  rule as the L&E guide and the join-community banner); (2) **never for a
  guest** (the shared connect modal owns that moment â two stacked overlays hide
  both); (3) **never on an invite deep link** (`/paluwagan/<name>`, `?join=`) â
  someone who tapped an invite wants the join card immediately.
- **Fail-closed storage:** an unreadable `localStorage` (hardened private mode)
  is treated as **already seen** â we cannot remember the prompt was shown, and
  re-showing it every load would break the one-time promise.
- **z-index 99990** â above the page chrome (navbar 100) but **below the
  local-wallet PIN prompt** (99999 injected / 100000 wallet page) so signing
  always stacks over the guide. `Escape` also closes it.
- **Env-configurable:** `PALUWAGAN_GUIDE_URL` (default the GoodDollar PH Notion
  page, `GUIDE_URL_DEFAULT` in `paluwagan/routes.py`) is passed to the template
  as `paluwagan_guide_url`; an **empty value removes the overlay AND its button
  entirely**, so the feature can be turned off without a deploy.
- Tests: `tests/test_paluwagan_content.py` Part 5 (+12, 109 total, no deps â
  closed-tag/lazy-src/reopen-button/CSP/auto-open-gating/seen-flag/storage-fail-
  closed/z-index/unconfigured content checks, an **html5lib parse of the
  rendered page**, and a **node harness** driving the REAL module across member
  /second-visit/guest/invite/manual-open/Escape/unconfigured/blocked-storage).
  Mutation-verified: all 6 mutations (self-closing iframe, CSP removal, eager
  src, guest auto-open, seen-flag on close, z-index above the PIN) fail.

## Learn & Earn â module (study) view revision â IMPLEMENTED (2026-10)
The screen shown after **Start Quiz** (`displayModuleLinks()` in `templates/learn_and_earn.html`) was **missed by the Soft Sunrise light-theme migration**: it hardcoded the old dark-theme palette (`#667eea`, `#6366f1`, `#8b5cf6`, `rgba(241,245,249,â¦)`, `#fbbf24`) and assumed a dark translucent card, all rendered on the light cream canvas. Measured in a real browser: sub-line and description **1.10:1**, progress chip **1.19:1**, next button **1.49:1**, headings 3.66:1 â six text elements failed WCAG AA outright, including the instruction sentence. The content was also **destroyed** (`innerHTML = 'â Reading time completeâ¦'`) the moment the timer ended, the plate was `max-height:300px` inside a `transform:scale(0.7)` body (â8.2px effective body text), the timer turned red as a threat for a rule with **no server-side enforcement**, and the copy was administrative ("Required Study Materials", "content will hide").
- **Fix (shipped):** the renderer's inline-style wall was replaced by a `.lem-*` CSS block appended to the existing Soft Sunrise override (base rules untouched) + new `:root` tokens (`--action`, `--action-2`, `--action-text`, `--accent-ink`, `--accent-chip-ink`, `--timer-bg`). Measured AFTER (real browser, composited surfaces): heading/card-title **21.0**, sub-line/card-desc **6.39**, progress chip **8.25**, timer label/value **7.86**, timer hint **5.36**, next button **5.21**, next-ready **5.02** â every pair clears AA.
- **Two defects found BY the measurement, not by reading the code:** the "ready" button's white-on-`#16a34a` was only **3.3:1** (â `#15803d`â`#166534`); and `makeModuleContentLinksClickable()` set an **inline** `#2563eb` on every link that beat the `.lem-content a` class rule (â `#b45309`, 7.4:1).
- **Behaviour:** content is **never destroyed** when the countdown ends; only `MODULE_MIN_READING_SECONDS` (**30s**) is enforced and the admin's `reading_time_minutes` is shown as guidance ("5 min read") â the quiz API never sees the reading time, which is *why* a long forced wait was pure friction; the red `#dc3545` danger state is gone; a slim progress bar fills the countdown and the timer block turns green (`is-done`) with "Reading time complete". `startModuleReadingTimer()` / `_updateModuleTimerUI()` / `_finishModuleReading()` were extracted so initial paint, each tick and the finished state share one implementation; `moduleReadingTotal` gives the bar a denominator.
- **Copy:** administrative wording replaced with learner-facing ("Study first, then take the quiz", "N min read", "Take your time â the quiz unlocks when the timer ends"); the progress chip became a "Module N of M" kicker.
- **Not changed (deliberately):** the admin Module Links editor + `learn_earn_module_links` table (per the request), the quiz question/results screens, and the backend.
- **Template gotcha:** `tests/test_learn_earn_cooldown_module_content.py::test_web_module_content_preserves_lines_like_jumble` asserted `white-space: pre-wrap` **inline** in the 300 chars after `id="module-content"`; moving it onto `.lem-content` broke it. The intent is unchanged, so the test now checks the class the card renders with and the class's own rule. **When moving an inline style onto a class, grep the tests for the property before assuming nothing reads it.**
- Tests: `tests/test_learn_earn_module_view_content.py` (26 tests, no deps â a **contrast test that parses the template's REAL `.lem-*` rules and `:root` tokens** and computes WCAG AA per pair, so a future re-skin that drops below AA fails CI; a guard-the-guard test proving the metric catches the old palette; copy assertions; and a **node harness** driving the REAL `displayModuleLinks`/`startModuleReadingTimer`/`_updateModuleTimerUI`/`_finishModuleReading` bodies against a DOM stub â render, tick to zero, assert content survives + button enabled + timer done + step indicator + "Start quiz" on the last module). Mutation-verified: reintroducing content destruction, reverting `.lem-sub` to `#f1f5f9`, and removing the 30s cap each fail the suite. Run with `python -m pytest tests/test_learn_earn_module_view_content.py`.
- Proposal + BEFORE/AFTER mockup: `docs/LEARN_EARN_MODULE_VIEW_REVISION_PROPOSAL.md`, `docs/mockups/learn_earn_module_view_mockup.html`.


## G$ Raffle â redeploy with new economics (2026-10)
The raffle was redeployed to change the round shape: **10 participants, 500 G$ entry,
4 winners paid 40/30/20/10** (2,000 / 1,500 / 1,000 / 500 G$, pool pays out 100%).
The old contract's constants were all `constant` (`MAX_PARTICIPANTS=400`,
`ENTRY_FEE=250`, `WINNER_COUNT=10`, `PRIZE_PER_WINNER=10_000`), so nothing could be
changed in place â a redeploy was the only option. Proposal + measurements:
`docs/RAFFLE_REDEPLOY_PROPOSAL.md`.

**The old contract `0x6e3Fa13306dbB67CFEE4711Ac9c4b6218F1145A0` is abandoned.**
Verified live on Celo: round 1 stalled at **58 of 400** participants holding
**14,500 G$**, and the contract has **no owner, no pause, no `withdraw`, no
`emergencyWithdraw`** â so those deposits are **permanently unrecoverable**. Do not
point `GOODMARKET_RAFFLE_CONTRACT_ADDRESS` at it and do not send users there.
(This is the concrete cost of the no-admin-withdrawal design; the new contract
keeps that design and adds a user-initiated refund instead.)

- **`contracts/GoodMarketRaffle.sol`** â `WINNER_COUNT` is now derived from
  `prizeTiers.length` (4); the constructor requires the tiers to **sum exactly to
  `MAX_PARTICIPANTS * ENTRY_FEE`** (`pool_mismatch`) and the tier count to match
  (`tier_count_mismatch`), so the contract can never be deployed unable to pay.
  New **`refundEntry()`** â open ONLY while the round is `Open`, so a round that
  never fills cannot lock funds. It closes the instant the 10th wallet joins
  (`status` flips to `Drawing`), which is the property that keeps a committed draw
  funded; `_removeParticipant` is swap-and-pop so a freed slot is reusable.
  Winner selection is a **partial FisherâYates shuffle**.
- **Honest note on the shuffle:** an earlier draft of the proposal claimed the old
  linear probe biased selection toward later joiners *measurably* at 10
  participants. **That was wrong** â 20,000 simulated rounds put every slot within
  2Ï of uniform (`0.979x`â`1.009x`), and it was equally negligible at 400/10. The
  shuffle is kept because it is correct-by-construction and costs nothing, **not**
  because users were hitting a bias. Do not re-assert the bias claim.
- **No face-verification gate** on entry â **declined by the product owner**. The
  exposure is recorded in the proposal (Â§4.3 / risk R2): at 10 slots one actor with
  N wallets holds `N/10` of the expected value. Mitigate by monitoring the public
  `RaffleJoined` logs and holding a suspicious round (nothing is lost while a round
  waits in `Drawing`). Adding the gate is a one-line `isWhitelisted` require plus a
  redeploy â do not add it without an explicit decision.
- **`contracts/deploy_goodmarket_raffle.py`** (new) â mirrors the paluwagan deploy
  pattern; **no hardcoded private key** (`GOODMARKET_RAFFLE_DEPLOY_KEY`). Deploy
  args: `gdToken = 0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A`,
  `randomnessProvider = <keeper>`, `tiers = [2000e18, 1500e18, 1000e18, 500e18]`.
- **`contracts/draw_goodmarket_raffle.py`** â the keeper now reads
  `MAX_PARTICIPANTS`/`ENTRY_FEE`/`WINNER_COUNT` **from the contract** instead of
  hardcoding 400. **Never re-hardcode the economics** â the same literals were
  duplicated in the frontend and would silently desync after any redeploy.
- **`static/js/wallet-main.js`** â `GOODMARKET_RAFFLE_ABI` updated; `refreshGdRaffle`
  reads live values; `setRaffleButtons` gained the refund button;
  `refundGdRaffleEntry()` added. The page degrades to "not deployed yet" when the
  address is empty and every read is `try`-guarded, so a stale cache cannot pair new
  JS with an old deployment.
- **Randomness trust (unchanged, worth restating):** `drawWinners(bytes32 seed)`
  takes the seed **from the randomness provider**, so whoever holds
  `GOODMARKET_RAFFLE_RANDOMNESS_KEY` chooses the outcome. Acceptable for a
  team-run community raffle; Phase 2 is Chainlink VRF or commitâreveal. Keep that
  key non-incentivized.
- **`templates/wallet.html` is on a hard 200 KB budget** â the raffle copy had to be
  trimmed twice to fit (`199,961` bytes). Add any further raffle copy to
  `wallet-main.js` or a static asset, **not** the template.
- Tests: `tests/test_raffle_contract_content.py` (52 tests â content wiring plus a
  **real-EVM drive** of the compiled `.sol` under eth-tester: pool/tier-count deploy
  guards, 10-fill â `Drawing`, 11th refused, tier order, refund frees the slot and
  closes on fill, provider-only draw, non-zero seed, claim-once, full payout to a
  zero balance, and uniformity over 300 rounds). Run with
  `python -m pytest tests/test_raffle_contract_content.py`.
- **EVM test-harness gotchas (both cost real debugging time):**
  1. **`eth-tester` returns a status-0 receipt on revert instead of raising.** A
     fixture that doesn't assert `receipt.status == 1` will silently skip every
     reverting transaction and then "measure" stale state â the first version of the
     uniformity test passed on a round that never advanced. `chain.send()` now
     asserts status. **Any new EVM test must do the same.**
  2. **`PyEVMBackend` ships only 10 accounts**, and a round needs 10 players plus a
     funder and an 11th. Register extras with `tester.add_account("0x" + hex)` (the
     key must be a **hex string**, not raw bytes) and fund them; they are then
     web3-managed and can sign.
  3. Fund/approve players for the **whole** statistical run â a budget of 4 rounds
     with a 300-round test makes joins revert from round 5 on.

## Testing
- `tests/test_raffle_contract_content.py` â G$ Raffle redeploy (content wiring + a real-EVM drive of the compiled contract under eth-tester: deploy guards, 10-fill â Drawing, refund, tier order, claim-once, full payout to zero balance, uniformity). Needs `solcx` + `web3` + `eth-tester` for Part 2 (skips cleanly without). Run with `python -m pytest tests/test_raffle_contract_content.py`.
- `tests/test_local_wallet_passkey_content.py` Ð²Òâ optional WebAuthn (passkey) quick-unlock (node harness over the REAL `static/js/local-wallet-passkey.js`: PIN/keystore never in the stored blob, wrong PRF rejected, cancelled ceremony + unsupported device fall back, address guard, revoke; plus manifest/details ignored, reason-specific failure copy). A second harness drives the REAL `_lwUnlockIfNeeded` auto-offer: enrolled Ð²Ò¶â device ceremony runs with no tap and unlocks without a PIN; no enrollment Ð²Ò¶â PIN focused, no ceremony; cancelled auto-attempt Ð²Ò¶â silent, PIN takes over; plus content checks that the PIN controls survive and the auto-offer is wired in both unlock sheets.
- `tests/test_reloadly_refund_autoretry_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Reloadly refund auto-retry upgrade (scheduler default-ON/opt-out, `get_refund_wallet_status`, three-status fetch + per-row CAS, single-order admin retry, wake wiring, admin endpoints/dashboard, SQL migration; plus a functional drive of the real retry module with stubbed supabase/service). No deps; run with `python -m pytest tests/test_reloadly_refund_autoretry_content.py`.
- `tests/test_lifi_bridge_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ LI.FI (Jumper) Celo Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ Base bridge (backend request shapes vs a stubbed urllib, the Cloudflare UA guard, routes/template/boot/local-wallet wiring, node harness over the real two-step execute). No deps; run with `python -m pytest tests/test_lifi_bridge_content.py`.
- `tests/test_farming_tx_history_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Chicken Farm tx-history + close/sell success UX (localStorage store, receipt settlement, Transfer-log G$ decoding, signer routing, public-RPC reads, history panel + congrats modal, orphan-class check, hero SVG chicken scene). No deps; run with `python -m pytest tests/test_farming_tx_history_content.py`.
- `tests/test_revert_data_handling.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ content/behavior tests locking in the fix. Run with `python -m pytest tests/test_revert_data_handling.py`.
- `tests/test_local_wallet_signing_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ content tests locking in GMLocalWallet (local self-custodial) signing routing across swap/reloadly/claim/p2p/wallet/send-link/learn_and_earn. No deps; run with `python -m pytest tests/test_local_wallet_signing_content.py`.
- `tests/test_gcash_cashout_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ content tests for the GCash cashout feature (SQL, gcash/ package, wallet.html modal, admin dashboard, main.py wiring). No deps; run with `python -m pytest tests/test_gcash_cashout_content.py`.
- `tests/test_ubi_reminder_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ content tests for the Telegram UBI reminder (message builders + per-wallet processing). Run with `python -m unittest tests.test_ubi_reminder_content`.
- `tests/test_wallet_load_perf_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ content tests locking in the wallet load-performance fixes (Privy SDK Jinja gate, no box-shadow/`left` keyframe animations, Fuse dead code removal, JS bundle extraction + boot config, CSS dedup). No deps; run with `python -m pytest tests/test_wallet_load_perf_content.py`.
- `tests/test_swap_load_perf_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ content tests locking in the swap load-performance fix (inline ~293 KB JS extracted to `swap-core`/`swap-reserve`/`swap-bridge` bundles, `GM_SWAP_BOOT` boot object, lazy once-only bridge pre-warm, size budget, render + runtime smoke checks). No deps; run with `python -m pytest tests/test_swap_load_perf_content.py`.
- `tests/test_swap_tab_focus_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ server-side /swap deep-link tab focus (functional drive of `swap_tab_state.resolve_swap_tab_state`, clientÐ ÐÐ¢Â¶Ð²ÒÒserver alias parity, Jinja renders asserting the exact pane classes for bare/goodswap/bridge/dir/reserve). No deps; run with `python -m pytest tests/test_swap_tab_focus_content.py`.
- `tests/test_get_started_guest_pages_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ homepage "Get Started" connect modal reused on `/swap` + `/savings` for guests (shared partial/CSS/JS, guest route branches + wallet blanking, guest-only includes/auto-open/nav CTA, return-to redirect guards). No deps; run with `python -m pytest tests/test_get_started_guest_pages_content.py`.
- `tests/wallet_page_src.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ shared helper, NOT a test file: wallet.html content tests must read the template plus the extracted `static/js/wallet-*.js` bundles (`append_wallet_bundles` / `read_wallet_page`).
- `tests/swap_page_src.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ shared helper, NOT a test file: swap.html content tests must read the template plus the extracted `static/js/swap-*.js` bundles (`append_swap_bundles` / `read_swap_page`). Bundles listed in `SWAP_EXTRACTED_JS`.
- `scripts/verify_swap_render.py` + `scripts/smoke_swap_bundles.cjs` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ dev helpers driven by `tests/test_swap_load_perf_content.py`: render swap.html with stubs (Jinja + node-parse the `GM_SWAP_BOOT` object, assert no Jinja leaked into a bundle) and execute the real swap bundles under DOM stubs (load-time throw detection + lazy pre-warm assertion).
- `tests/test_ai_agent_tx_lookup_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ functional (loads `ai_agent/tx_lookup.py` via importlib, no deps) + text-based wiring tests for the agent tx-hash lookup. Run with `python -m pytest tests/test_ai_agent_tx_lookup_content.py`.
- `tests/test_broadcast_delivery_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ durable Telegram broadcast delivery (error classification, queue, CAS claim, aggregates, retry cap, scheduler, routes, dashboard). Run with `python -m unittest tests.test_broadcast_delivery_content`.
- `tests/test_referral_local_wallet_gate_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ referral program revision (local-wallet-only recording, shared auto-disburse helper + CAS, gas preflight/queue, reconciler, admin dashboard, frontend gating). Run with `python -m pytest tests/test_referral_local_wallet_gate_content.py`.
- `tests/test_claim_network_choice_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ UBI claim per-network choice (user pick outranks Celo-first, per-network claim buttons, guard conditions, failure fallback to the other network, FV gate intact, CSS/copy). No deps; run with `python -m pytest tests/test_claim_network_choice_content.py`.
- `tests/test_send_link_settlement_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ send-link settlement-status fix (payments()-struct lag guard, withdraw-event claimed/cancelled classification, getLogs chunking). No deps; run with `python -m pytest tests/test_send_link_settlement_content.py`.
- `tests/test_xdc_gas_check_before_faucet_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ XDC gas readiness is checked BEFORE any faucet request (read-only `/api/xdc/faucet/status`, top-up gated on `!gas_ready`, polling uses the status endpoint, xdc_wallet.html ordering). No deps; run with `python -m pytest tests/test_xdc_gas_check_before_faucet_content.py`.
- `tests/test_wallet_join_community_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ wallet-page one-time Telegram community invite (banner markup + hidden default, seen-flag written at reveal, fail-closed blocked storage, popup-blocked same-tab fallback, single URL constant, GoodMarket-not-GoodDollar copy). No deps; run with `python -m pytest tests/test_wallet_join_community_content.py`.
- `tests/test_minigames_weekly_withdrawal_limit_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Play & Earn 500 G$/week withdrawal cap (PHT week helpers, partial payout, same-week block + next-week copy, week rollover, seed-from-log, failed-payout release, CAS race, missing-migration fallback, balance-endpoint fields, page wiring). No deps; run with `python -m unittest tests.test_minigames_weekly_withdrawal_limit_content`.
- `tests/test_wallet_usdc_token_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ USDC as a first-class wallet token (functional drive of the REAL `prepare_usdc_transfer_data` with stubbed web3/eth_abi, plus tile/chip/meta/max/render/route wiring). No deps; run with `python -m pytest tests/test_wallet_usdc_token_content.py`.
- `tests/test_wallet_daily_task_modal_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ content tests for the Daily Task modal on the wallet page (surfaced only from the More Ways To Earn sheet; status/history/submission inside one modal). No deps; run with `python -m pytest tests/test_wallet_daily_task_modal_content.py`.
- `tests/test_ubi_pool_stats_content.py` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ claim-screen UBI pool figures matching GoodDapp (contract-stat ABI, `get_ubi_pool_stats` functional drive + fallbacks/degradation/caching, route legacy-key compatibility, panel markup/JS/theme wiring). No deps; run with `python -m unittest tests.test_ubi_pool_stats_content`.
- Many tests need `flask` / `requests` (not installed in the base env). Content tests (`test_*_content.py`) run without deps.
- JS syntax: `node --check static/js/<file>.js`. For template inline JS, strip Jinja `{{ }}`/`{% %}` first (see tests for the regex approach).

## Conventions
- No build step for frontend JS Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ edit files directly, bump `?v={{ ASSET_VERSION }}` is handled by template caching.
- Comments explain *why*, not *what*. Existing style uses section banners (Ð â¢Ð¢ÑÐ âÐÂ¶Ð âÐ¢Â¶Ð â¢Ð¢ÑÐ âÐÂ¶Ð âÐ¢Â¶ / Ð â¢Ð¢ÑÐ âÐ¡Ò»Ð âÐ¢ÒÐ â¢Ð¢ÑÐ âÐ¡Ò»Ð âÐ¢ÒÐ â¢Ð¢ÑÐ âÐ¡Ò»Ð âÐ¢Ò).

## Twitter/Telegram daily-task message pools Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ origin re-anchoring (2026-08)
The message pools in `twitter_task/twitter_task.py` and `telegram_task/telegram_task.py` hardcode `goodmarket.live` in their templates. Like the referral program, served messages are now re-anchored to the origin the requesting client actually used (Vercel preview, custom domain), so no hardcoded domain leaks into user-facing copy.
- `referral_program/referral_service.py` exposes a shared resolver: `_origin_base()` (flask `request.host_url` on-request, `BASE_URL` off-request), `build_referral_link(code)`, and `current_origin_domain()` (host without scheme for text re-anchoring).
- Twitter task: referral URL injected via `build_referral_link`, then any remaining bare `goodmarket.live` is replaced via a case-insensitive `re.sub` against `current_origin_domain()` (lambda repl, so digit-containing domains stay safe).
- Telegram task: same case-insensitive re-sub on `current_origin_domain()`, non-fatal try/except guard.
- When a new task/message pool mentions a domain, re-anchor the same way; never hardcode `goodmarket.live`.
- Tests: `tests/test_task_message_origin_content.py` (functional importlib + content wiring, no deps). Run with `python -m unittest tests.test_task_message_origin_content`.

## Twitter/Telegram daily-task message pools Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ copy rewrite (2026-09)
The pools are **user-signed posts published from real personal accounts**, so the copy must read as if a person wrote it. The old pools were brand taglines ("Join the financial revolutionÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£", "Join thousands earning G$ daily!") published verbatim, which reads as coordinated spam to X/Telegram spam heuristics and makes the poster look like a bot Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the worst outcome for new users.
- **Copy rules (do not regress):** first-person plain English; no brand taglines; no affiliation claims (`official`); no `free`/`guaranteed`/`join thousands`; **exactly one link per post** (the injected referral link Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the old pools carried three: bare domain + closing URL + referral); single closing punctuation only.
- **Shared generator helpers** in BOTH modules: `_coprime_stride(total, count)` + `_build_message_pool(openers, middles, closers, render, count=1000)`. Each of the 1000 messages is a unique (opener, middle, closer) triple. A stride coprime with `total` sweeps the whole space.
- **Dead-code bug fixed:** the old `i % n` / `(i//10) % n` / `(i//100) % n` layout exposed only a tenth of the closer pool Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ 10 of 20 closers were **unreachable**, including all five that advertised a CELO check-in reward that **does not exist in this codebase**. Those are gone; every phrase in every pool is now reachable (guarded by `test_every_phrase_entry_is_reachable`).
- **Rotation is keyed on (wallet, UTC day) ONLY.** The old index also mixed in `hour_of_day`, so the message shown at 7:59 differed from the one the user actually posted at 8:01 Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the admin reviewing the submission saw text that did not match the post. Do not re-add an hourly term. Pool size is 1000 distinct messages per platform, so ~5/100 users in a day share text (was ~29/100 with reused sentence triples).
- **Mentions reduced to one** (`@gooddollarorg`, a factual project reference) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the second `@GoodDollarTeam` mention plus three URLs read as a campaign.
- Do NOT put links in the opening/middle pools: only the closing phrase carries the URL, so the referral injection yields exactly one link.
- Tests: `tests/test_daily_task_message_quality_content.py` (30 tests Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ 1000-distinct, single-link, X-weighted length, reachability, banned phrases, first-person ratio, hourly-drift regression, origin re-anchor preserved; no deps). Run with `python -m unittest tests.test_daily_task_message_quality_content`.

## Telegram bot Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Daily Task integration (2026-08)
The web app unified daily task (dashboard.html modal + `/api/daily-task/*`) is now fully usable from the Telegram bot with the SAME wallet, SAME tables, SAME cooldown, and SAME admin-dashboard approval Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ no backend route changes were needed.
- **All in `telegram_bot.py`** (section `Ð â¢Ð¢ÑÐ âÐ¡Ò»Ð âÐ¢ÒÐ â¢Ð¢ÑÐ âÐ¡Ò»Ð âÐ¢ÒÐ â¢Ð¢ÑÐ âÐ¡Ò»Ð âÐ¢Ò Daily Task (Twitter / Telegram) Ð â¢Ð¢ÑÐ âÐ¡Ò»Ð âÐ¢ÒÐ â¢Ð¢ÑÐ âÐ¡Ò»Ð âÐ¢ÒÐ â¢Ð¢ÑÐ âÐ¡Ò»Ð âÐ¢Ò`): `/dailytask` command + "Ð âÐ£ÒÐ âÐ£ÐÐ â¢Ð¢Ò¡Ð âÐ Ò² Daily Task" main-menu button + callbacks `daily_task`, `daily_task_status`, `daily_task_twitter`, `daily_task_telegram`, `daily_task_history`, `daily_task_cancel`; text submissions via `handle_daily_task_text` in the webhook chain (after trustpilot/community-stories handlers, BEFORE `handle_wallet_text`).
- **Same wallet guarantee:** wallet comes from `_get_saved_wallet()` (`telegram_wallet_sessions`, normalized lowercase) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ bot submissions land in `twitter_task_log`/`telegram_task_log` keyed by that wallet, so web app status/history and the admin dashboard pending list (`/api/admin/daily-tasks/pending|approve|reject`) reflect them automatically. Admin approve/decline stays the ONLY approval point.
- **Unified status mirrors `/api/daily-task/status` in routes.py:** `_get_daily_task_unified_status()` runs both services' `check_eligibility` + direct pending-row DB check on BOTH tables; pending on EITHER platform blocks BOTH, cooldown picks `min(next_claim_time)`. The 72h cooldown is shared across platforms (rejected Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ immediate resubmit; completed Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ 72h block).
- **Submission path reuses the SAME services:** `handle_daily_task_text` does the unified shared-cooldown gate first, then `_run_async(service.claim_task_reward(wallet, post_url))` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ URL validation (Telegram posts must be `t.me/GoodDollarX/<msg_id>`), uniqueness checks, and pending-row insert all behave exactly as the web app. Custom message comes from the service's `get_custom_message_for_user` (wallet-rotated, origin re-anchored Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ inside the webhook request context `current_origin_domain()` uses the app host).
- **Session hygiene:** `_TELEGRAM_DAILY_TASK_SESSIONS` per-user with `TELEGRAM_DAILY_TASK_SESSION_TTL_SECONDS` (default 900) TTL; `handle_daily_task` clears stale sessions; platform buttons only render in the "ready" state (pending/cooldown show only Status/History).
- Tests: `tests/test_telegram_daily_task_content.py` (25 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ stubs flask/requests/config/supabase_client/news_feed/itsdangerous, imports telegram_bot via importlib; functional unified-status/handler tests + wiring content). Run with `python -m unittest tests.test_telegram_daily_task_content`.

## Daily task Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ wallet case-mismatch fix (2026-08)
Bug: web sessions store **checksummed** wallets (`Web3.to_checksum_address` in verify-identity / verify-ubi / fv-callback) while the Telegram bot stores **lowercase** (`_normalize_wallet`). Every daily-task query used case-sensitive `.eq('wallet_address', ...)`, so a bot-submitted pending row (lowercase) was invisible to the web dashboard status (checksummed session wallet) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ "submitted sa bot pero hindi nag-update sa web app" Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ and vice versa.
- **Fix Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ case-insensitive matching everywhere in the daily-task paths:** `_wallet_filter(query, wallet)` helper (`.ilike('wallet_address', wallet)` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ safe because addresses are hex-only, no ilike wildcards) in BOTH `telegram_task/telegram_task.py` and `twitter_task/twitter_task.py`, applied to `check_eligibility` (pending + recent-claims), `get_task_stats`, `get_transaction_history`; `.ilike` also in `routes.py` `/api/daily-task/status` pending checks and `telegram_bot.py` `_get_daily_task_unified_status`.
- **Writes normalized:** both services insert `wallet_address.lower()` so new rows are uniform; duplicate-URL guards compare `str(previous_wallet).lower() == wallet_address.lower()`.
- **When adding a wallet-scoped query to these tables, use `_wallet_filter`/ilike, never `.eq('wallet_address', ...)`.**
- Tests: `tests/test_daily_task_wallet_case_content.py` (functional Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ loads both services via importlib with stubbed supabase/cache/reward/maintenance/requests, drives fake tables with mixed-case wallets; plus wiring content checks). Run with `python -m unittest tests.test_daily_task_wallet_case_content`.

## Community Stories Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ wallet case-mismatch fix (2026-08)
Same bug class as the daily task: web routes pass `session.get('wallet')` (checksummed) while the Telegram bot passes the lowercase saved wallet, and `community_stories/community_stories_service.py` used case-sensitive `.eq('wallet_address', ...)` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so a bot submission did NOT block the web app during the participation window (and vice versa), risking duplicate reward submissions.
- **Fix:** `_wallet_filter(query, wallet)` helper (`.ilike`) applied to `check_user_cooldown`, `has_pending_submission`, `get_user_submissions` (submissions + cooldowns), and the approve-path cooldown read/update; all inserts (`submit_tweet`, `submit_screenshot`, `create_screenshot_entry`, approve-cooldown insert) normalize `wallet_address` to lowercase.
- **When adding a wallet-scoped query here, use `_wallet_filter`/ilike, never `.eq('wallet_address', ...)`.**
- Tests: `tests/test_community_stories_wallet_case_content.py` (functional Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ loads the real service via importlib with stubbed supabase/env/web3/config, drives fake tables with mixed-case wallets; plus wiring content checks). Run with `python -m unittest tests.test_community_stories_wallet_case_content`.

## Community Stories Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ admin dashboard hidden pending submissions (2026-08)
The admin dashboard's Community Stories section (`admin_dashboard.html` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `/community-stories/api/admin/notifications` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `CommunityStoriesService.get_admin_notifications`) showed "Ð â¢Ð¢ÑÐ âÐ Ò²Ð âÐ Ò² No pending submissions" even when users had `pending` rows. Root causes, both silently dropping the entire list:
- The embedded-resource join `.select('*, community_stories_submissions(*)')` on `community_stories_admin_notifications` resolves to `null` unless the two tables have a foreign-key relationship (the column filter then reads `{}.get('status')` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ filtered out). `community_stories_admin_notifications` is created outside the SQL migrations, so the FK is often missing.
- The join was gated by a **case-sensitive** `.eq('admin_wallet', admin_wallet)`; Telegram-login admins get lowercase rows while web sessions are checksummed Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the same case-mismatch bug class as the user-wallet fixes above.
- **Fix:** `get_admin_notifications` now reads `community_stories_submissions` directly (`status == 'pending'`, ordered by `submitted_at`) as the source of truth and builds the same `{submission_id, community_stories_submissions}` shape the dashboard expects Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ no FK relation needed, no admin-wallet case gating. The approve/reject "mark notification as read" updates also use `.ilike('admin_wallet', admin_wallet)` for case-insensitivity. Mirrors the duplicate `/api/admin/community-stories-notifications` endpoint in routes.py.
- **When adding an admin Community-Stories query, query `community_stories_submissions` directly and never rely on the admin-notifications join or a case-sensitive `admin_wallet` lookup.**
- Tests: `TestCommunityStoriesAdminNotifications` in `tests/test_community_stories_wallet_case_content.py`.

## Learn & Earn quiz history Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ Telegram vs web wallet case (2026-08)
Telegram-bot quizzes used to be invisible in the web quiz history (`/learn-earn/quiz-history`). Root cause: `learnearn_log.wallet_address` stores a MASKED display address (`0xabc...xyz`), and the case depends on the writer Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ web sessions (checksummed) vs the Telegram bot (lowercase `session_data["wallet"]` from `_get_saved_wallet`). `QuizManager.get_quiz_history` built a case-sensitive `.eq` OR filter over (masked, full-lower, full-raw), so Telegram rows never matched. On-chain transfers still showed in the wallet overview tx history (blockchain scan), so it looked like "reward received but not recorded".
- **Fix:** `QuizManager._wallet_history_filter(wallet)` (learn_and_earn.py) returns the canonical case-insensitive `ilike` OR over the masked and full forms Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ same pattern as `_latest_attempt_query` (cooldown). Used by `get_quiz_history`, both NFT mint gate queries, and as the model for routes.py `get_profile` + `notifications_service._get_learn_earn_notifications`. The daily-ranking endpoint now compares masks with `.lower()` on both sides.
- **Writes normalized:** `save_quiz_attempt`/`log_quiz_attempt` store `mask(wallet.lower())`. **When adding any wallet-scoped query on `learnearn_log`, use the helper / ilike, never `.eq('wallet_address', ...)`.**
- Tests: `tests/test_learn_earn_quiz_history_wallet_case.py` (functional importlib with flask/web3/supabase stubbed + wiring content checks, no deps). Run with `python -m unittest tests.test_learn_earn_quiz_history_wallet_case`.

## Branding Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ GoodMarket vs GoodDollar impersonation-risk rule (2026-08)
MetaMask/Blockaid flagged the app as a suspected GoodDollar impersonator (eth-phishing-detect issue #286646 Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the reply: "is this some kind of testing deployment? the official domains are gooddollar.org"). Root cause: copy like "Earn Free GoodDollar Daily", "GoodDollar UBI Portal", "GoodDollar Portfolio" on a non-gooddollar.org (vercel.app) domain reads as a textbook phishing pattern.
- **Rule for user-facing identity surfaces** (page `<title>`, meta/og/twitter description, WalletConnect `dappDescription`/SignClient `metadata.description`, PWA `manifest.json`, brand headers): the app is **GoodMarket**; describe it as a "community wallet for the G$ token (GoodDollar protocol)". Never call the app/wallet itself "GoodDollar" (e.g. no "GoodDollar Wallet", "GoodDollar Portal") and never promise "free GoodDollar" in titles/meta.
- **Factual references stay**: "G$", the GoodDollar Reserve / OneTimePayments / Identity contracts, "about GoodDollar" quiz/task copy, and links to docs.gooddollar.org / t.me/GoodDollarX / @gooddollarorg are fine.
- The footer disclaimer ("independent community project Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ not an official product of, or affiliated with, GoodDollar") must stay prominent.
- Note: the domain was never on the eth-phishing-detect static list Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the "Review alert" comes from **Blockaid**; appeals go to report.blockaid.io, not just the GitHub repo.

## Canonical domain redirect Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ goodmarketph.live (2026-08)
The app must be served ONLY on the canonical domain `goodmarketph.live` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the `*.vercel.app` origin is a phishing risk signal with wallet security vendors (Blockaid; see the branding section above), and the old custom domain `goodmarket.live` EXPIRED.
- **`canonical_host.py`** (root, no deps) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `canonical_redirect_target(host, path_qs)` returns the 308 target or None. `CANONICAL_HOST` env (default `goodmarketph.live`). Redirects ONLY allowlisted aliases: any `*.vercel.app` host, `goodmarket.live`/`www.goodmarket.live`, and `www.{canonical}`. Localhost / dev / runtime preview hosts are NEVER redirected (allowlist, not a blanket rule).
- **Exempt paths:** `/telegram/webhook` (Telegram treats a 3xx webhook response as failed delivery Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ re-register via `/telegram/setup-webhook` after cutover) and `/health`.
- **main.py** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `@app.before_request` hook `_redirect_alias_hosts_to_canonical` runs BEFORE `_block_scanner_paths`, uses `request.host` + `request.full_path`, 308 (permanent, method-preserving).
- **Defaults moved:** `config.py` `PRODUCTION_DOMAIN` and `referral_program/referral_service.py` `BASE_URL` now default to `https://goodmarketph.live`. The twitter/telegram message pools still hardcode `goodmarket.live` as static text Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the existing origin re-anchoring (`current_origin_domain()`) rewrites them at serve time, so no pool change was needed.
- User-facing copy updated: `templates/_wallet_guide_banner.html`, `templates/security.html`.
- When moving domains again: set `CANONICAL_HOST` (+ `PRODUCTION_DOMAIN`/`BASE_URL` for off-request contexts) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ no code change needed.
- Tests: `tests/test_canonical_domain_redirect_content.py` (functional importlib + wiring, no deps). Run with `python -m unittest tests.test_canonical_domain_redirect_content`.

## Public Community Chatroom (2026-09)
A **public room** where every signed-in user can chat and **everyone sees every message**, each attributed to the poster's **username** (never the wallet address). It is its **OWN floating widget** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ deliberately separate from the GoodMarket Agent Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ which **auto-opens on load** and shows new messages within about a second. **Hidden by default** until an admin turns it on. The GoodMarket Agent, in turn, is now **hidden by default** behind its own flag so the room is what users land on.
- **Module:** `chatroom/` (`service.py` = logic, `routes.py` = `/chatroom` blueprint). SQL: `sql/community_chat.sql` (tables `community_chat_messages`, `community_chat_reports`, `community_chat_bans`; seeds `chatroom_feature` as `is_maintenance = TRUE`). Register with `init_chatroom(app)` in `main.py`.
- **Hidden by default, enforced server-side.** `service.is_enabled()` reads the `chatroom_feature` row from `maintenance_settings` and **fails CLOSED** (missing row / DB error Ð â¢Ð¢ÑÐ âÐ¢Â®Ð âÐ¢Â¶ OFF). `routes.py` returns 403 `{enabled: false}` on every `/chatroom/api/*` call and redirects the page while OFF Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ hiding the UI is not the gate. Turn it ON from **Admin Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ Feature Visibility Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ Community Chatroom** (the `chatroom_feature` id is in the `routes.py` whitelist + `set_feature_visibility` validation + `get_feature_visibility` response, and the `chatroom_visible` context processor in `main.py` drives the templates).
- **Privacy rule (load-bearing):** the browser NEVER receives a wallet address. `service._public_row()` drops `wallet_address` and exposes only `{id, username, message, created_at, is_me}`; ownership is a boolean `is_me`. `username` is a **snapshot** stored at post time (no join at render); a user without a username shows `short_wallet()` (`0x1234Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£cdef`). **Do not add `wallet_address` to a response.**
- **Reads use the service-role client** (`get_supabase_admin_client() or get_supabase_client()`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the RLS lesson from lotto/gcash.
- **Moderation:** users can report a message (`POST /api/report`, UNIQUE per message+reporter Ð â¢Ð¢ÑÐ âÐ¢Â®Ð âÐ¢Â¶ duplicate is idempotent success); admins get soft-delete (`is_deleted` + `deleted_by`/`deleted_at`), resolve/dismiss reports, and ban/unban wallets (banned wallets get `403 banned` on post). Admin UI: **Admin Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ Chatroom Moderation** (`chatroom-moderation-section` in `admin_dashboard.html`).
- **Rate limit + sanitizing:** `CHATROOM_RATE_LIMIT_SECONDS` (default 3s, per wallet, checked before insert) and `CHATROOM_MAX_MESSAGE_LENGTH` (default 500). `sanitize_message()` strips control/zero-width chars and collapses whitespace.
- **Frontend Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a SEPARATE widget, not an agent tab:** `templates/_chatroom.html` (its own launcher + panel, `{% if chatroom_visible %}`), `static/js/chatroom-widget.js` + `static/js/chatroom-widget.css`, exposed as `window.GMChatroom.{mount,buildChatRow}`. It was **deliberately removed** from `_ai_agent.html`/`ai-agent.js` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ do not re-add a `Ð âÐ£ÒÐ âÐ£ÐÐ âÐ¢Â¶Ð âÐ¡Ò³ Chatroom` tab to the agent panel (a content test fails if the symbols reappear there).
  - **Auto-open:** `mount()` defaults `opts.autoOpen !== false` and the `DOMContentLoaded` self-mount calls `start()`, so the panel is open on load. The standalone `/chatroom` page mounts manually (`data-gm-chat-manual`) with `autoOpen: true`.
  - **Mobile launcher is a compact icon + caption chip, not a wide pill (2026-10).** The collapsed launcher was a ~150px purple pill ("ÑÒ¹âÂ¬ Community Chat") Ð²Òâ about 40% of a 360px viewport Ð²Òâ so it sat over the portfolio balance card and the token rows. Below 640px it is now a **64Ðâ68** chip: the ÑÒ¹âÂ¬ icon with a one-word **"Chat"** caption under it (the caption is what keeps an icon-only button from reading as a mystery circle Ð²Òâ the user asked for "icon lang siya" but also "alam din nila na chatroom iyon"). Desktop keeps the full labelled pill. The `<strong>` became `class="gm-chat-label"` and the icon/caption spans were added for the responsive swap; `aria-label="Open community chatroom"` + `title` carry the accessible name when the visible label is hidden. **Do not widen the mobile chip back into a pill** Ð²Òâ it is the only thing overlapping the balance card. Locked by `test_launcher_is_an_icon_caption_chip_on_mobile_not_a_wide_pill` in `tests/test_chatroom_content.py` and `test_launcher_is_compact_and_keeps_the_caption_on_mobile` / `test_launcher_desktop_pill_is_unchanged` in `tests/test_wallet_nav_button_color_content.py`.
  - **Bottom-sheet circles are coloured by default (2026-10).** `.bn-sheet-item .bn-sheet-circle` used to be a pale `#faf7f2` disc, so every row without an inline gradient (Savings, Send, GoodSwap, XSwap, Virtual Card, Mobile Top-Up) rendered grey. It now carries an orange gradient + white glyph (rows with an inline gradient keep theirs Ð²Òâ the inline style wins).
  - **Instant delivery = fast adaptive polling, NOT SSE/WebSocket.** The app runs gunicorn with `gthread` (4 workers Ð ÐÐ²ÒÒÐ¢ÐÐ â¢Ð¢Ð 4 threads = 16 slots); a held-open SSE connection would pin one slot per visitor and stall the app at ~16 users, so polling is the correct primitive here. `POLL_FAST_MS = 1000` while the panel is open, `POLL_IDLE_MS = 5000` collapsed (badge only), doubling to `POLL_MAX_MS = 15000` on failures, paused while `document.hidden`, refreshed on `visibilitychange`. Each tick uses the **`after_id` cursor** (`?after_id=<last>`), never a full refetch.
  - **Unread badge** counts only messages that arrive while the panel is **collapsed** (`if (!open)`) Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a message the user is already reading is not "new".
  - **Message bodies are written with `textContent` only Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ never `innerHTML`** (untrusted input).
- **GoodMarket Agent is now hidden by default too:** `goodmarket_agent_feature` (seeded `is_maintenance = TRUE`) drives the `goodmarket_agent_visible` context processor; `templates/wallet.html` wraps `{% include "_ai_agent.html" %}` in `{% if goodmarket_agent_visible %}`. Flip it ON from **Admin Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ Feature Visibility Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ GoodMarket Agent**. (The include is inside the `if`, so the agent's JS never loads while OFF; `_chatroom.html` gates itself internally so its include is not wrapped.)
- **Launcher stacking:** both widgets are `position: fixed` (chatroom `z-index: 9998`, agent `9999`). `positionChatroomLauncher()` lifts the chatroom above the wallet bottom nav AND above the agent toggle when the agent is on screen, so the two floating buttons never overlap.
- **Entry point:** a `Ð âÐ£ÒÐ âÐ£ÐÐ âÐ¢Â¶Ð âÐ¡Ò³ Community Chat` row in the wallet bottom-nav "More Ways To Earn G$" sheet, wrapped in `{% if chatroom_visible %}`. The chatroom CSS colors in `wallet.html` must stay within the dark-purple palette (the wallet theme test bans `#16a34a`/`#15803d` literals outside the override).
- **Not FV-gated (deliberate):** the room is for ALL signed-in users, so the `human_verification_redirect` gate was removed; abuse is handled by report/ban. Re-adding an FV gate is a product decision, not a bug fix.
- Tests: `tests/test_chatroom_content.py` (38 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ functional drive of `chatroom/service.py` with a stubbed supabase client (fail-closed flag, sanitizing, rate limit, bans, soft-delete, cursor pagination, and a hard assertion that no wallet address is ever returned), a **node harness** driving the REAL `GMChatroom.mount`/`buildChatRow` (renders `@username`, message stays literal text, report button only on others' rows, no `0x` in the DOM, auto-open, and the unread-only-when-collapsed rule), plus SQL/blueprint/feature-flag/template/admin-dashboard wiring). Run with `python -m pytest tests/test_chatroom_content.py`.
- **Node-harness gotcha:** the widget's polling now uses real `setTimeout`, so a harness **must call `room.stop()` and `process.exit(0)`** or node never exits and the test hangs. The old `setInterval` stub exited on its own.
- **Test-harness gotcha:** the service lazy-imports `supabase_client` at call time, so the test installs its stub into `sys.modules` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ an **autouse fixture restores the prior module** afterwards, otherwise other content tests (which install their own stub only-if-absent) import a `supabase_client` missing `safe_supabase_operation` and error out. Any new content test that stubs `supabase_client` must restore it.

## Community Chatroom Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ user-to-user tipping (2026-09)
Any member can tip another member the tokens the app supports Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ **G$ (Celo), CELO, G$ (XDC), XDC** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ straight from the public room, addressed by **@username** (or the Ð âÐ£ÒÐ âÐ£ÐÐ âÐ¢Â¶Ð âÐ²Òâ Tip button). The tip is a **REAL on-chain transfer signed by the sender's own wallet**; the app never holds a key and never takes custody. Every successful tip returns its **TX hash to the room** as a gold tip card with a block-explorer link, plus a public announcement: `Ð âÐ£ÒÐ âÐ£ÐÐ âÐ²ÒÒ»Ð âÐ²ÒÒ£ Congrats @betz! You received 5 G$ from @alice Ð âÐ£ÒÐ âÐ£ÐÐ âÐ²ÒÒ»Ð âÐ¡Ò³`.
- **Module:** `chatroom/tips.py` (`prepare_tip` / `record_tip` / `verify_tip_tx`), SQL `sql/community_chat_tips.sql` (new `community_chat_tips` table + `message_type`/`tip_token`/`tip_amount`/`tip_tx_hash` columns on `community_chat_messages`). **Run the migration before enabling tipping.**
- **Flow (3 hops, no server signing):** `POST /chatroom/api/tip/prepare` resolves the `@username` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ wallet and returns unsigned transfer params (`to`/`data`/`value`/`chain_id`) Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ the browser signs with the user's own wallet Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `POST /chatroom/api/tip/confirm` with the `tx_hash`. The backend then VERIFIES the tx on-chain (from == sender, to == recipient, exact amount, correct token contract, receipt status 1) **before** recording it and posting the announcement. A client claiming success is never enough.
- **The tx hash is the only evidence, and it is UNIQUE.** `community_chat_tips.tx_hash` is UNIQUE, so a replayed confirm returns the already-recorded tip (`duplicate: true`) instead of recording/announcing twice Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the replay is checked **before** the rate-limit and verification steps.
- **Verification is ABI-free.** ERC-20 transfers are decoded from the receipt logs by topic (`TRANSFER_TOPIC`) and filtered to the tip token's own contract address (logs from other contracts are ignored); native CELO/XDC tips check `tx.from`/`tx.to`/`tx.value`. `transfer()` calldata is hand-encoded (`a9059cbb` + two 32-byte words) so the module imports and tests without `web3`/`eth_abi`.
- **Announcements bypass the chat cooldown deliberately.** The public tip message is inserted directly (not via `service.post_message`) because the money has already moved on-chain Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a chat rate limit must never drop the record or the announcement. Bans are still enforced before signing.
- **Privacy rule still holds:** the tip row stores both wallets, but `_public_row` only exposes `{token, amount, tx_hash}` for `message_type == "tip"` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ **never a wallet address**. The recipient's address is returned to the SENDER's own browser only (so their wallet can sign it); it is never echoed into the room. Tip lookups (`find_wallet_by_username`) resolve through `user_data.username` (case-insensitive `ilike`), which is unique by construction.
- **Signer routing mirrors the rest of the app:** `local` Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ PIN-unlocked `GMLocalWallet` (`_lwOpenUnlockModal`, never an injected extension); WalletConnect Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `GMWalletConnect` when `_gmPreferWc()`; Privy Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ `window.GMPrivyWallets[].provider`; else injected, **bound to the session wallet's account** (`eth_requestAccounts` then match) so a multi-account wallet cannot sign from the wrong address ("Wrong wallet connected"). Local logins sign the tx with its own `chainId` (the in-app wallet routes by it); injected/WC get a `wallet_switchEthereumChain` (with `wallet_addEthereumChain` on 4902) to the token's chain first.
- **ethers on demand:** `GMLocalWallet` needs ethers, but the standalone `/chatroom` page has no other reason to load it, so `_ensureEthers()` lazily injects it (cdnjs SRI `sha384-6Zl0Pc8zjÐ â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£`, jsdelivr fallback) before a local PIN unlock. wallet.html already ships ethers, so it is a no-op there.
- **Templates:** the tip sheet is a shared partial `templates/_chatroom_tip_modal.html`, included by BOTH `_chatroom.html` (floating widget) and `chatroom.html` (standalone page). Per-request values (`wallet`, `loginMethod`) come through `window.GM_CHAT_BOOT`; the token list + viewer wallet are also returned by `/chatroom/api/state` (the server is the source of truth for the offered tokens).
- **Modal stacking:** `.gm-chat-tip-modal` is `z-index: 99990` Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ deliberately BELOW both PIN prompts (99999 injected / 100000 wallet page) so the local-wallet unlock sheet always stacks above the tip it is signing.
- **Clickable usernames:** a `.gm-chat-name` on someone else's message is a `role="button"` that opens the tip sheet (`openTipModal`); own messages are not tippable. The tip card renders `message` with `textContent` like every other message Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ **never `innerHTML`**.
- **Config:** `CHATROOM_TIP_RATE_LIMIT_SECONDS` (default 10s per sender), `CHATROOM_MAX_TIP_AMOUNT` (default 1,000,000), token contracts env-overridable (`CELO_GD_TOKEN_CONTRACT`, `XDC_GD_TOKEN_CONTRACT`), RPCs via `CELO_RPC_URL` / `XDC_RPC_URL`.
- **When extending this:** keep verification mandatory (never trust a client-claimed success), keep `tx_hash` UNIQUE, keep the announcement out of the cooldown path, and never add a wallet address to a public row. Any new token must be added to `TOKENS` (with its contract + explorer) and to the `CHECK` in `sql/community_chat_tips.sql` + the modal's `<select>`.
- Tests: `tests/test_chatroom_tips_content.py` (37 tests, no deps Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ functional drive of `chatroom/tips.py` with a stubbed supabase client (username resolution, prepare incl. self/token/amount/disabled, ABI-free ERC-20 + native verification, amount/recipient/contract mismatches, idempotent replay, rate limit, banned sender, the "Congrats Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£" copy, and a hard assertion that no wallet address is returned), a **node harness** driving the REAL `buildChatRow`/`sendTip` (tip card + explorer link, tip affordance only on others' rows, clickable @username, the prepareÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶signÐ â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶confirm hash round-trip, and the wrong-wallet refusal), plus SQL/route/modal/boot/z-index wiring). Run with `python -m pytest tests/test_chatroom_tips_content.py`.

### Desktop injected (MetaMask) tip signing + Get Started connect repair (2026-10)
Two reported issues, both root-caused in code. Proposal: `docs/DESKTOP_INJECTED_SIGNING_AND_CONNECT_REPAIR_PROPOSAL.md`.
- **Chatroom tip on desktop MetaMask: "hindi na gumana ang signing".** `_tipSendTransaction()` sent a **bare** `eth_sendTransaction` (**no `gas`, no `gasPrice`**) Ð ÐÐ¢âÐ²ÒÒ the only money-moving send in the app that still did. Without a gas limit the wallet must estimate the fee through **its own configured RPC**; a slow/rate-limited node renders a red **"Network fee unavailable"** and the sheet never settles. Fixed: `_tipGasParams()` runs `eth_estimateGas` Ð ÐÐ¢Â¶Ð²ÒÒ· pad **40%** Ð ÐÐ¢Â¶Ð²ÒÒ· cap at `TIP_GAS_CEILING` Ð ÐÐ¢Â¶Ð²ÒÒ· fall back to `TIP_GAS_FALLBACK` (celo 120k / xdc 500k), plus `eth_gasPrice` Ð ÐÐ¢Â¶Ð²ÒÒ· `CHAIN_META[net].fallbackGasPrice`, and the injected send carries **both** fields. **Never send only `gas`** Ð ÐÐ¢âÐ²ÒÒ `eth_estimateGas` returns units, so the wallet would still ask its RPC for the price (same lesson as `claimXdcInjected`).
- **Nested error codes.** MetaMask wraps the real code under `err.cause.code` / `err.data.originalError.code`, so the old bare `err.code === 4902` check missed an "Unrecognized chain" and `wallet_addEthereumChain` never ran. New `_tipErrCode()` / `_tipErrMessage()` / `_tipIsUserRejection()` unwrap all shapes; a **4001 switch decline** is now reported as "You cancelled the network switch" (a normal user action), not a raw wallet error.
- **False tip success fixed.** `sendTip()` did `if (tipBusy) return null;` Ð ÐÐ¢âÐ²ÒÒ a duplicate submit resolved `null` and the caller rendered **"Ð¡ÒÐ¢âÐ¢ÓÐ¢Ò Tip sent!"** with no tx hash. It now **throws** "A tip is already in progress", and `_submitTipFromModal` disables the Send button for the duration (`finally` re-enables).
- **Sign timeout.** Both the local and injected tip sends (and the Get Started login `personal_sign`) go through a 120s timeout helper (`_withSignTimeout` / `withInjectedSignTimeout`) so an asleep extension service worker or stuck mobile dApp browser yields actionable copy instead of an infinite spinner.
- **Tip failure diagnostic:** the send catch logs provider / chain / gas / gasPrice / code / message to the console (shortens support loops).
- **Get Started "minsan delay".** `connectInjectedWallet()` is a strictly serial chain of 2Ð ÐÐ¢âÐ²ÒÒ£4 wallet dialogs. Two fixes: (1) **`requestGoodDollarToken` (`wallet_watchAsset`) asked on EVERY login** Ð ÐÐ¢âÐ²ÒÒ it now runs **once per browser session** (`sessionStorage gmWatchAssetAsked_v1`, set in a `finally`), so it no longer inserts an "Add token?" dialog between the network switch and the login signature; (2) **`openWalletModal()` awaited `/api/session-status` before painting** Ð ÐÐ¢âÐ²ÒÒ it now races the probe against a 400ms gate and shows the sheet via `_showWalletModalNow()`, redirecting post-paint if the probe says authenticated.
- **Faster desktop detection.** `_walletDetectionMaxDepth()` desktop depth 6 Ð ÐÐ¢Â¶Ð²ÒÒ· **2** and `awaitPreferredInjectedProvider()` desktop budget 800ms Ð ÐÐ¢Â¶Ð²ÒÒ· **300ms** (desktop extensions inject synchronously; the long budgets are for late-injecting mobile wallets). The `visibilitychange`/`pageshow` warm-up re-discovery was **already** Trust-mobile-gated Ð ÐÐ¢âÐ²ÒÒ not a desktop cost.
- **Invariants kept:** local logins never fall through to an injected extension; the tip is still verified on-chain server-side (a client hash is never trusted); no silent chain switching; the chatroom privacy rule (no wallet address in any payload).
- **Audit note:** a repo-wide scan of `eth_sendTransaction` sites found many pre-existing bare sends (mostly helper functions receiving `txParams`, and the `wc-bridge.js` method router). Those are out of scope here; the **reported flows** are fixed. When touching any injected send, send `gas` + `gasPrice`.
- Tests: `tests/test_desktop_injected_signing_content.py` (16 tests, no deps Ð ÐÐ¢âÐ²ÒÒ content wiring + a **node harness** driving the REAL `sendTip`/`_tipEnsureChain` against a stubbed MetaMask provider: explicit gas+gasPrice from estimate/gasPrice, fallback when the estimate fails, nested-4902 Ð ÐÐ¢Â¶Ð²ÒÒ· `wallet_addEthereumChain`, 4001 Ð ÐÐ¢Â¶Ð²ÒÒ· cancellation copy, and a busy tip throwing instead of a false success). Verified the harness fails when the `gas` field is removed. Run with `python -m pytest tests/test_desktop_injected_signing_content.py`.


## Community Chatroom Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ admin badge + in-room admin delete (2026-10)
A wallet flagged admin in the database (`user_data.is_admin = TRUE`) now gets a **gold `ADMIN` badge** next to its username in the room, and admins get an **in-room Ð âÐ£ÒÐ âÐ£ÐÐ â¢Ð¢ÐÐ âÐ¢Â° Delete** button on every message (not just in the Admin dashboard's Chatroom Moderation panel). Deletes are **soft** (kept as an audit trail) and **prune live from every other viewer's screen** within a poll tick.
- **Admin source of truth:** `supabase_client.is_admin()` reads `user_data.is_admin` (this is the same flag the Admin dashboard's "set admin" action toggles). The chatroom mirrors it Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ it never invents a second admin list.
- **`chatroom/service.py`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ new `is_admin_wallet(wallet)` (single lookup, used by the state endpoint) and `_admin_wallets(wallets)` (ONE batched `or_` of `wallet_address.ilike.Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð ÐÐ¢âÐ¢Ó£` clauses Ð â¢Ð¢ÑÐ âÐ¢Ò³Ð âÐ¢Â¶ a lowercased set, so badge resolution is **never N+1** and stays case-insensitive across the checksummed/lowercase split). `_public_row(row, viewer, admin_wallets)` adds **`is_admin` as a BOOLEAN** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ the wallet address is still never sent to the browser. `get_messages` resolves the set once per page and stamps every row. New `get_deleted_ids(since, limit)` backs the deletion cursor.
- **Deletion propagation (the load-bearing part):** polling uses the `after_id` cursor, which **never re-reads old rows** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ so a message deleted by an admin would linger on other users' screens until a manual reload. Fix: `GET /chatroom/api/messages` accepts **`?deleted_after=<ISO ts>`** and returns `deleted_ids` + `deleted_cursor` (from `get_deleted_ids`), plus a `server_time` on every response. The widget asks with its stored `deletedCursor`, calls `pruneDeleted(ids)` to remove those rows, and seeds the cursor from `server_time` (never the device clock Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ skew would miss deletions). `deletedCursor` is only sent once set, so the **first** poll stays a plain `limit=50` read. The service orders deletions **ascending** so the cursor advances monotonically (a burst > `limit` drains over successive polls).
- **`chatroom/routes.py`** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `api_state` returns `is_admin` for the viewer (drives the delete affordance); the delete endpoint (`DELETE /api/admin/messages/<id>`, already admin-gated + `log_admin_action`) is unchanged and still re-checks `is_admin` server-side Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ a non-admin calling it directly gets 403 regardless of the UI.
- **Widget (`static/js/chatroom-widget.js`)** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `buildChatRow` adds `data-id`, an `.is-admin` row class, a `.gm-chat-admin-badge` span, and (only when `options.isAdmin`) a `.gm-chat-delete` button wired to `deleteChatMessage()`. `mount()` tracks `viewerIsAdmin` (from `/api/state`) and `deletedCursor`, and `removeMessageRow()`/`pruneDeleted()` handle both the admin's own optimistic removal and other viewers' pruning. Message bodies remain `textContent`-only.
- **SQL (`sql/community_chat.sql`)** Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ partial index `idx_community_chat_deleted_at ON community_chat_messages(deleted_at) WHERE is_deleted` for the cursor query.
- **Soft, not hard, delete is deliberate:** keeps the `deleted_by`/`deleted_at` audit trail, avoids cascading `community_chat_reports` (`ON DELETE CASCADE`) and nulling replies (`ON DELETE SET NULL`), and every read already filters `.eq("is_deleted", False)` so the message is gone from all listings. **Do not switch to a hard delete** without re-handling reports/replies/audit.
- Tests: `tests/test_chatroom_content.py` (+7 Ð â¢Ð¢ÑÐ âÐ¢Â¶Ð âÐÂ¶ `test_admin_badge_is_a_boolean_and_never_leaks_the_wallet`, `test_admin_wallets_lookup_is_batched_and_case_insensitive`, `test_deleted_ids_are_returned_for_the_polling_cursor`, `test_admin_delete_is_soft_and_hides_from_every_listing`, `test_admin_badge_and_delete_wiring_is_present`, `test_widget_builds_admin_badge_and_delete_only_for_admin`, `test_widget_prunes_deleted_messages_on_poll`). The FakeTable stub gained `or_`/`gte`/`count` support. Run with `python -m pytest tests/test_chatroom_content.py`.


## Connect a dApp Ð ÐÐ¢âÐ²ÒÒ WalletConnect WALLET role, Phase 1 (2026-10)
GoodMarket already played the **dApp** role (wc-bridge.js opens sessions OUT to the user's wallet). This adds the **inverse**: the app acts as the *wallet*, so a user copies the WalletConnect link from an external dApp, pastes it into GoodMarket, and signs for that dApp from inside the app. Phase 1 is **local-login (GMLocalWallet) only** Ð ÐÐ¢âÐ²ÒÒ the only method where the app holds the key, so signing is on-device + PIN-gated with no external round trip. Proposal: `docs/DAPP_CONNECT_PROPOSAL.md`.
- **Module:** `static/js/dapp-connect.js` (`window.GMDappConnect`) + `static/js/dapp-connect.css`. Feature flag `dapp_connect_feature` Ð ÐÐ¢âÐ²ÒÒ **hidden by default**; enable from Admin Ð ÐÐ¢Â¶Ð²ÒÒ· Feature Visibility Ð ÐÐ¢Â¶Ð²ÒÒ· Connect a dApp. **No SQL is required:** the feature has NO tables and NO endpoints (sessions live in SignClient browser storage; every signature is local), a MISSING `maintenance_settings` row already reads as hidden in both read paths, and `set_feature_visibility` insert-if-absents the row itself. `sql/dapp_connect.sql` is optional Ð ÐÐ¢âÐ²ÒÒ it only pre-seeds an explicit hidden row. **Do not describe the SQL as a prerequisite** (an earlier wording did; `test_no_database_setup_is_actually_required` now pins the honest copy).
- **Entry points (2026-10 update):** a **primary bottom-nav button** `Ð¡ÒÐ¢âÐ²ÒÒÐ²Òâ Connect` (`class="bn-item bn-dappconnect"`, blue chip, between Services and More) so it is as discoverable as Send / Receive / Services Ð ÐÐ¢âÐ²ÒÒ it was previously buried in the Services sub-sheet and users never noticed it. It is `{% if dapp_connect_visible %}`-gated so a disabled deploy shows nothing. The `Ð¡ÒÐ¢âÐ²ÒÒÐ²Òâ Connect dApp` row in the **Services** sheet stays (redundant but harmless), and a `#dappConnectStrip` status strip appears whenever Ð ÐÐ¢ÒÐ¢Ò³1 session is live. **Do not** demote it back into a sub-sheet Ð ÐÐ¢âÐ²ÒÒ the whole point of the move was discoverability. Modals `#dappConnectModal` + `#dappRequestModal` are DOM-ordered after `settingsModal`; `#lwUnlockModal` keeps its higher z-index so the PIN prompt stacks above the approval sheet.
- **Method dispatch (the load-bearing part).** `eth_accounts`/`eth_chainId` answer silently; a `READ_METHODS` list (`eth_call`, `eth_estimateGas`, `eth_getBalance`, `eth_getCode`, `eth_getTransactionCount`, `eth_getBlockBy*`, `eth_getTransactionBy*`, `eth_getTransactionReceipt`, `eth_getLogs`, `eth_blockNumber`, `eth_gasPrice`, `eth_feeHistory`, `eth_syncing`, Ð ÐÐ¢âÐ¢Â°) is **forwarded to the public RPC with no prompt and no signature** Ð ÐÐ¢âÐ²ÒÒ a dApp cannot render a balance or estimate gas without them, so rejecting them makes the wallet look broken the moment it connects. `wallet_switchEthereumChain` moves the in-app chain pointer (unknown chain Ð ÐÐ¢Â¶Ð²ÒÒ· `4902`). Signing methods (`personal_sign`, `eth_signTypedData[_v3|_v4]`, `eth_sendTransaction`) open the approval sheet. Everything else, **including `eth_sign`**, is auto-rejected with `4200`.
- **Never auto-sign.** Only the two silent methods skip the prompt. The sheet shows the dApp name, real origin, chain, action, decoded message / typed data, and the native value in both wei and a human-readable amount; an `eth_sendTransaction` whose `from` is not the session wallet gets a blocking **"DIFFERENT address"** warning.
- **Signing always goes through `GMLocalWallet.getProvider()`** after `_lwUnlockIfNeeded()` Ð ÐÐ¢âÐ²ÒÒ never an injected provider (a different account). Non-local logins get an explanatory message and the **SignClient is never loaded** for them (a test asserts zero `SignClient.init` calls).
- **Session lifetime:** cap 10, revoke from the strip/modal. **Logout clears sessions** Ð ÐÐ¢âÐ²ÒÒ logout is a plain `<a href="/logout">` full-page navigation, so a capturing click listener disconnects every session first (1.5 s cap) and then navigates; on boot `_dropForeignSessions()` disconnects any persisted session whose accounts do not include the current session wallet, so a session can never survive into the next user's account.
- **Disconnect must propagate to the dApp (2026-10).** WC `disconnect()` publishes `wc_sessionDelete` to the peer over the relay BEFORE deleting locally, and the SDK's relayer `sendRequest` uses `throwOnFailedPublish: true` Ð ÐÐ¢âÐ²ÒÒ so if the socket is not open the publish throws and the local session is dropped while the dApp keeps showing "connected" (the exact confusion reported). `_disconnectSession(topic, msg, retries)` now (1) `_ensureRelayer(client)` opens the relay websocket (capped, best-effort) and (2) reports `{delivered}` so `revoke()` can show an honest `is-warn` message when delivery fails ("removed from GoodMarket but the notice could not reach it Ð ÐÐ¢âÐ²ÒÒ reopen the dApp and use its Disconnect"). `_getClient()` also best-effort opens the relayer so an already-connected session can be cleanly disconnected; `_disconnectAllThen` (logout) opens it too. Inbound: on `session_delete` (the dApp disconnected us) we clear any dead approval sheet (`_clearRequestForTopic`) + toast; a `session_request` for a topic we no longer hold is rejected with `4100` instead of popping a sheet. `refreshStatusStrip` appends a relay-offline hint only when the SDK reports `transportExplicitlyClosed`. **Never revert to the bare `client.disconnect()`** Ð ÐÐ¢âÐ²ÒÒ silent failure re-creates the stale-connection bug. Locked by `test_disconnect_opens_relayer_and_reports_delivery` + `test_disconnect_wiring_and_copy_are_present`.
- **`customStoragePrefix: "gmdapp"`** keeps wallet-role sessions in a separate storage namespace from the dApp-role login session managed by `wc-bridge.js` (different encryption keys, same origin). **Do not remove it** or the two roles collide.
- **Read `GM_WALLET_BOOT` LAZILY Ð ÐÐ¢âÐ²ÒÒ never snapshot it at load time (2026-10).** `dapp-connect.js` is loaded in `<head>` (before `local-wallet.js`), but `window.GM_WALLET_BOOT` is an inline object emitted LATE in `<body>`. The module originally captured `var LOGIN_METHOD = BOOT.loginMethod || ""` at load time, so it was always `""` Ð ÐÐ¢Â¶Ð²ÒÒ· `isEligible()` returned **false for everyone**, including real in-app (email + PIN) users, who then saw "Connect a dApp signs with your in-app GoodMarket wallet Ð ÐÐ¢âÐ¢Â° Log in with your GoodMarket wallet to use it." The boot config is now read through lazy accessors (`_boot()` / `_loginMethod()` / `_walletAddr()` / `_projectId()` / `_assetVersion()`). **Any new value from `GM_WALLET_BOOT` must go through an accessor, never a captured `var`.** Same landmine that previously bit `local-wallet.js`'s `_bootEth` Ethereum override.
- **Never** widen the chain/method allowlist, drop the read-method forwarding, or re-introduce an injected-provider fallback without re-auditing every signing path.
- **Theme: `dapp-connect.css` must use the SOFT SUNRISE light palette (2026-10).** The wallet is on the light theme (white `#ffffff` modal sheets, cream `#f7f1e8` canvas), but this sheet originally shipped light-on-dark colours (`#e5e7eb`/`#9ca3af`/`#bfdbfe`) Ð ÐÐ¢âÐ²ÒÒ light grey on white is ~1.1:1, so the connect modal was **effectively unreadable**. All text is now dark-on-light, mostly via the theme vars (`--text`, `--text-dim`, `--text-muted`, `--card-soft`, `--card-border`). The AA test (`test_dapp_text_colors_meet_aa_on_the_light_canvas`) measures every literal `color:` against BOTH light surfaces. **When the wallet theme changes again, re-measure this sheet.**
- **The Connect button must DISAPPEAR after a successful connect.** The connect modal shows exactly ONE of three views (`_applyView()` in `dapp-connect.js`): **REVIEW** (a `session_proposal` is pending Ð ÐÐ¢âÐ²ÒÒ the proposal + Approve/Decline sit directly under the title, the paste form + session list are hidden), **CONNECT** (nothing connected: paste a `wc:` link), **SESSIONS** (Ð ÐÐ¢ÒÐ¢Ò³1 connected: the list + "Connect another dApp"). The DOM order in `wallet.html` is `#dappProposalBox`/`#dappProposalActions` Ð ÐÐ¢Â¶Ð²ÒÒ· `#dappConnectForm` Ð ÐÐ¢Â¶Ð²ÒÒ· `#dappSessionsWrap` Ð ÐÐ¢Â¶Ð²ÒÒ· actions. **Previously all sections stacked at once**, so on mobile the Approve/Decline buttons fell below the fold (users had to scroll past the paste form + connected list) and the Connect button appeared to "still be there" during review Ð ÐÐ¢âÐ²ÒÒ the exact report. `startNewConnection()` sets `_state.connectFormForced` so "Connect another dApp" can re-open the paste form even when a session exists. `_scrollSheetTop()` scrolls the sheet back to the top when a proposal arrives / the form re-opens (defensive: no-op if the element stub lacks `querySelector`). **Do not flatten the modal back into one stack** Ð ÐÐ¢âÐ²ÒÒ the review actions must stay above the fold. The paste-a-link form is `#dappConnectForm`; `_renderSessions()` calls `_showConnectForm(all.length === 0)` so any live session hides the form/button and reveals the `#dappConnectAgainBtn` ("Connect another dApp", hidden by default) escape hatch + the session list. `startNewConnection()` re-reveals the paste form. `approveProposal()` clears `#dappProposalBox` on success and re-renders. Locked by `test_connect_button_is_hidden_after_a_successful_connect` + the functional `test_connect_form_hides_after_a_session_exists` node harness.
- Tests: `tests/test_dapp_connect_content.py` (26 tests, no deps Ð ÐÐ¢âÐ²ÒÒ flag/admin-dashboard/route wiring, primary-nav discoverability, strip + modal stacking, connect-form hidden after connect, light-theme AA, allowlist, and **node harnesses** driving the REAL module: lazy boot read (eligibility flips falseÐ ÐÐ¢Â¶Ð²ÒÒ·true when the body boot config arrives), the connect form hiding on a live session, unsupported-chain and `eth_sign` rejection, approval sheet, read forwarding + chain switch (4902 for unknown), wrong-`from` warning, silent `eth_accounts`, revoke, foreign-session drop, the non-local no-SDK-load assertion, plus the AppKit/optionalNamespaces harness). Run with `python -m pytest tests/test_dapp_connect_content.py`.

### Connect a dApp Ð ÐÐ¢âÐ²ÒÒ real-dApp compatibility: optionalNamespaces + Ethereum (2026-10)
The first real-world report: a user opened `claim.superfluid.org`, copied its WalletConnect link, pasted it into GoodMarket, and it failed. Two independent defects, both fixed:
- **Defect 1 Ð ÐÐ¢âÐ²ÒÒ `optionalNamespaces` was ignored.** The parser read chains/methods ONLY from `requiredNamespaces`. Reown **AppKit** dApps (and most modern ones) send `requiredNamespaces: {}` (empty) plus a multi-chain `optionalNamespaces`, so the proposal looked empty Ð ÐÐ¢Â¶Ð²ÒÒ· Approve stayed **permanently disabled** with a "GoodMarket does not supportÐ ÐÐ¢âÐ¢Â°" warning. Fix: `_proposalChains`/`_proposalMethods` now **union** `requiredNamespaces` + `optionalNamespaces` (via `_nsUnion`). The dApp's chain list is treated as a **superset** we intersect Ð ÐÐ¢âÐ²ÒÒ we grant the supported subset instead of rejecting the whole proposal. The `bip122` (Bitcoin) namespace that Superfluid also lists is simply ignored (not an eip155 namespace).
- **Defect 2 Ð ÐÐ¢âÐ²ÒÒ approve blocked a superset.** `_proposalSupported` used to require EVERY requested chain/method to be known. Now it blocks only when a **required** chain is unsupported (`_requiredChains`), or when the compiled **granted subset is empty** (nothing we can do at all). An entirely-optional proposal with a supported subset is approvable; the withheld chains/methods are listed on the card as "Ð ÐÐ¢âÐ¢Â° will not be granted".
- **Ethereum added** to `ALLOWED_CHAINS` (`eip155:1`) and to the in-app wallet's `SUPPORTED_CHAINS` (`local-wallet.js`) so a dApp that lists `eip155:1` can be granted. The ETH RPC list is overridable per deploy via `ETHEREUM_RPC_URL` / `ETHEREUM_RPC_FALLBACKS` (boot keys `ethereumRpc`/`ethereumRpcFallbacks` in `wallet.html`; the override is applied **lazily** because `local-wallet.js` loads BEFORE `GM_WALLET_BOOT`, so it runs on first `_chainJsonRpc`/send). Public RPCs with failover, never a single hardcoded endpoint.
- **Benign wallet reads answered silently** Ð ÐÐ¢âÐ²ÒÒ `BENIGN_WALLET_READS` (`wallet_getCapabilities`, `wallet_getPermissions`, `wallet_getCallsStatus`, `wallet_getAssets`): read-only, no key, no funds moved, so `_answerBenignWalletRead` answers without a prompt. **`wallet_sendCalls` / `wallet_showCallsStatus` are NOT in that list** Ð ÐÐ¢âÐ²ÒÒ batched fund-moving calls stay auto-rejected with `4200` (never auto-sign).
- **Signing stays local-only** (2d decision): `isEligible()` still returns true only for `LOGIN_METHOD === "local"`. Relaying to injected/WalletConnect/Privy signers is out of scope. Cleaner copy: *"Connect a dApp signs with your in-app GoodMarket wallet (email + PIN) Ð ÐÐ¢âÐ¢Â° a MetaMask, WalletConnect or Privy login signs in its own wallet, not here."*
- **Why GoodWallet "works on web" but this needed the app:** a normal WalletConnect **wallet** (MetaMask/Trust/GoodWallet) is a full EIP-1193 provider that implements the `wallet_*` namespace and holds the key in the wallet app; here the *key lives in the browser* (GMLocalWallet), so the app must implement that namespace itself and the allowlist must cover the chains the dApp asks for. It is not "needs to be installed" Ð ÐÐ¢âÐ²ÒÒ it is the app implementing the wallet side correctly.
- **Do not** re-narrow the proposal parser to `requiredNamespaces`, re-block superset proposals, or drop `wallet_sendCalls` from the reject list. Tests: `test_appkit_optional_namespaces_are_read_and_intersected`, `test_required_unsupported_chain_still_blocks`, `test_wallet_read_methods_silent_sendcalls_rejected`, `test_ethereum_in_allowlist_and_local_wallet_registry`, `test_eligibility_is_local_only`.

## Telegram bot Ð ÐÐ¢âÐ²ÒÒ admin-only `/tip` command (2026-10)
Admins can send real tokens to a registered member straight from the bot: `/tip 100 G$`, `/tip 1 XDC`, `/tip 1 CELO`, `/tip 1 USDT` (also USDC + XDC G$). Real on-chain transfer signed by a **dedicated `TIP_KEY` hot wallet** Ð ÐÐ¢âÐ²ÒÒ the server never custodies user funds, only its own treasury. Proposal + design: `docs/TELEGRAM_TIP_PROPOSAL.md`.
- **Module `telegram_tip/`** Ð ÐÐ¢âÐ²ÒÒ `tokens.py` (dependency-free registry: G$/CELO/USDT/USDC on Celo, XDC/XDC G$ on XDC; per-token decimals, aliases), `blockchain.py` (`TipBlockchainService` Ð ÐÐ¢âÐ²ÒÒ direct transfer, ERC-20 **and native**, mirrors `telegram_task/blockchain.py`'s `DAILYTASK_KEY` pattern), `service.py` (parse, admin gate, recipient resolution, limits, ledger Ð ÐÐ¢âÐ²ÒÒ importable without web3/supabase).
- **`telegram_bot.py`** Ð ÐÐ¢âÐ²ÒÒ `/tip` branch in `webhook()` (BEFORE the `handle_wallet_text` fallback) Ð ÐÐ¢Â¶Ð²ÒÒ· `handle_tip(chat_id, telegram_user, text, message)` Ð ÐÐ¢Â¶Ð²ÒÒ· `execute_tip(...)`; `_tip_reply_telegram_user_id()` reads `reply_to_message.from.id` (ignores bot replies). Best-effort recipient DM via `_notify_tip_recipient`.
- **Recipient resolution order:** explicit raw `0x` address Ð ÐÐ¢Â¶Ð²ÒÒ· explicit `@username` (case-insensitive `user_data.username`) Ð ÐÐ¢Â¶Ð²ÒÒ· **replied-to member's saved wallet** (`telegram_wallet_sessions`) Ð ÐÐ¢Â¶Ð²ÒÒ· self (private-chat testing). A member must be **registered** (`/start` + saved wallet) or the tip is refused with a friendly prompt.
- **Admin gate (server-side, every call):** `TIP_ADMIN_TELEGRAM_IDS` (comma-separated) is a **strict allowlist** when set; otherwise the caller's saved wallet must have `user_data.is_admin` (the SAME flag the Admin dashboard toggles Ð ÐÐ¢âÐ²ÒÒ one source of truth). Check runs BEFORE any balance read or tx build.
- **Limits bound a compromised admin:** per-tip cap (`TIP_MAX_PER_TIP_*`) + **rolling UTC daily cap** (`TIP_DAILY_CAP_*`) + per-admin rate limit (`TIP_RATE_LIMIT_SECONDS`). Daily cap only counts `sending`/`confirmed` rows. `check_limits` **fails CLOSED** when the ledger is unreadable.
- **Ledger `sql/telegram_tips.sql`** Ð ÐÐ¢âÐ²ÒÒ append-only audit (admin id/wallet, recipient, token, amount, status, tx_hash, error), `tx_hash UNIQUE` (idempotency Ð ÐÐ¢âÐ²ÒÒ a retried confirm can never pay twice). Reads/writes use the **service-role** client. Run the migration before enabling (a missing table surfaces as honest "ledger unavailable" refusals).
- **Safety invariants (repo-wide lessons):** decimals come from the registry Ð ÐÐ¢âÐ²ÒÒ **USDT/USDC are 6 decimals**, never hardcode `10**18`; preflight BOTH native gas and token balance with `insufficient_gas`/`insufficient_balance` error types; nonce read with the `"pending"` tag + retry on collisions (never hard-fail); patient receipt wait Ð ÐÐ¢Â¶Ð²ÒÒ· `submitted_unconfirmed` **with** `tx_hash` and **never auto-resend** (double-pay hazard).
- **Supported tokens include the Celo stablecoins, with PER-TOKEN decimals (2026-10).** `telegram_tip/tokens.py` `TOKENS` = GD / CELO / **USDT / USDC (6 dp)** / **CUSD (18 dp)** on Celo + XDC / XDC G$ on XDC. cUSD (`0x765DE816845861e75A25fCA122bb6898B8B1282a`, env `CUSD_TOKEN_ADDRESS`) is an **18-decimal** token Ð ÐÐ¢âÐ²ÒÒ unlike USDT/USDC at 6 Ð ÐÐ¢âÐ²ÒÒ so it uses the standard `10**18` path, NOT the 6-dp conversion. Both cUSD and USDT/USDC sit in the stablecoin cap tier (`TIP_MAX_PER_TIP_CUSD` 100 / `TIP_DAILY_CAP_CUSD` 500). The supported-token list in the bot's usage message is generated from `TOKENS`, so adding a registry entry surfaces it automatically. **Never hardcode `10**18` for a stablecoin** Ð ÐÐ¢âÐ²ÒÒ read decimals from the registry (a hardcoded value over-sends USDT/USDC by 10**12). The `telegram_tips.token` column is plain TEXT (no CHECK), so adding a token needs no SQL migration.
- **Receipt confirmation must poll across RPC fallbacks (2026-10).** A tip reported `submitted_unconfirmed` even though the G$ actually arrived, because `send()` relied on `w3.eth.wait_for_transaction_receipt(timeout=60)` on the SINGLE primary RPC. A broadcast tx is a fact; the only thing that can fail afterwards is the node we read from Ð ÐÐ¢âÐ²ÒÒ forno both rate-limits and lags on receipts. `TipBlockchainService._wait_for_receipt_patient(meta, tx_hash)` now polls the primary RPC then `CELO_RPC_FALLBACKS` / `XDC_RPC_FALLBACKS` for up to `TIP_RECEIPT_TIMEOUT_SEC` (default **120s**, poll `TIP_RECEIPT_POLL_SEC` 2.5s) and only then reports `submitted_unconfirmed`. **Never revert to a single-RPC `wait_for_transaction_receipt`** for a hot-wallet payout Ð ÐÐ¢âÐ²ÒÒ a slow node must not look like a failed tip.
- **Unregistered-recipient errors need their OWN actionable copy (2026-10).** A tip whose recipient is not linked to the bot surfaced as a vague failure. `_recipient_message(error, source)` now splits the cases: `recipient_unknown` (a typed `@handle` resolved to no wallet) Ð ÐÐ¢Â¶Ð²ÒÒ· "No registered wallet found for that @username", check the spelling / use a `0x` address, plus how a member becomes tippable (open the bot, `/start`, save a wallet); `recipient_not_registered` (the replied-to member has no linked wallet) Ð ÐÐ¢Â¶Ð²ÒÒ· "That member hasn't linked a wallet to the bot yet" + ask them to `/start` & save, or tip a `0x` address directly; `no_recipient` lists both options with an example. **Never collapse "not registered" into the generic send-error / "check the server logs" message** Ð ÐÐ¢âÐ²ÒÒ it is a user-fixable state, not an incident.
- **Every on-chain step must try ALL RPCs, not just the primary (2026-10).** A generic "Tip failed. Please try again or check the server logs." came from `_send_error_message`'s catch-all, which fired for any `error_type` it did not name AND for an infra failure on the single primary RPC. Two fixes: (1) `preflight()` now iterates `_rpc_urls_for(meta)` (primary then `CELO_RPC_FALLBACKS`/`XDC_RPC_FALLBACKS`); a dead/rate-limited node can no longer make a funded wallet look unfunded, and when ALL nodes fail it returns `rpc_down`/`gas_check_failed`/`balance_check_failed` (infra) instead of a wrong funds verdict; (2) `send()` likewise cycles URPs on broadcast failure before giving up. `_send_error_message(error_type, meta, ref)` now has a specific, actionable message for EVERY error_type (`no_key`/`invalid_key`/`rpc_down`/`gas_check_failed`/`balance_check_failed`/`send_failed`/`send_exception`/Ð ÐÐ¢âÐ¢Â°) and appends a `Ref: TIP-<ledger id>` so the exact row/log line is one search away; the failure path also logs `telegram_tip: send failed ref=Ð ÐÐ¢âÐ¢Â° error_type=Ð ÐÐ¢âÐ¢Â°`. **Never let a new error_type fall through to the vague catch-all, and never broadcast/preflight on a single RPC.**
- **The bot is SILENT in groups except for an admin tip (2026-10).** `webhook()` branches on `_is_group_chat(message)` (type group/supergroup/channel) BEFORE the private-chat dispatch: in a group it acts ONLY when `_is_tip_message(text)` AND `_tip_authorized(telegram_user)` (admin pre-gate, so a non-admin attempt is ignored rather than answered). Everything else Ð ÐÐ¢âÐ²ÒÒ members' chatter, wallet submissions, stray links, and ALL other commands/buttons Ð ÐÐ¢âÐ²ÒÒ produces **no reply**. This is what stopped `handle_wallet_text` from answering every member message in the group. The private-chat command dispatch is unchanged. Do NOT let a group message fall through to `handle_wallet_text`/`handle_start`/etc. again.
- **Honest caveat:** `TIP_KEY` is a **hot wallet in env** Ð ÐÐ¢âÐ²ÒÒ anyone with server/deploy access can read it. The caps + audit only bound what a *Telegram* admin can do through the bot. Keep the balance low + top up on demand; if budgets grow, migrate to the pull-based vault model (`GoodMarketLotto`/`GoodMarketRaffle` `grantWinners` + user `claim`).
- **Configurable trigger (dodges GroupHelp):** group-management bots delete unrecognised `/` commands even for admins, so the trigger is env-driven Ð ÐÐ¢âÐ²ÒÒ `TIP_COMMAND_ALIASES` (default `tip,gift`), `TIP_COMMAND_PREFIXES` (default `/!`), and `TIP_REPLY_TRIGGER=1` for the **no-keyword** form (an admin replying to a member with just `<amount> <token>`, e.g. `100 XDC`, is treated as a tip). `is_tip_command()` / `parse_tip_command()` build the regex from these; `telegram_bot._is_tip_message()` ORs the triggered form with the reply form. **There is no such thing as a truly keyword-less bot** Ð ÐÐ¢âÐ²ÒÒ it receives every group message, so a bare `<amount> <token>` must NEVER fire outside an explicit reply. `execute_tip_parsed()` runs the pre-parsed reply form through the SAME admin gate/limits/ledger as `execute_tip()`.
- **Public announcement:** on success the bot posts `Ð¡ÒÐ¢âÐ¢ÓÐ¢Ò Congrats @user! You received N TOKEN from @admin Ð¡ÒÐ¢âÐ¢ÓÐ¢â` + explorer link in the SAME chat (usernames only Ð ÐÐ¢âÐ²ÒÒ **never a wallet address**; falls back to the masked confirmation card when the recipient has no username). Controlled by `TIP_PUBLIC_ANNOUNCE` (default ON). When it posts, the recipient DM is suppressed to avoid a duplicate; otherwise `_notify_tip_recipient` DMs them. Names resolve via `username_for_telegram` (telegram_wallet_sessions.username) Ð ÐÐ¢Â¶Ð²ÒÒ· `username_for_wallet` (user_data.username).
- **`@username` resolution must check BOTH username tables (2026-10 fix).** A reported "recipient is not registered yet" for a user who HAD saved their wallet came from `find_wallet_by_username()` querying only `user_data.username` (the GoodMarket app username). A bot-only user's Telegram `@handle` is stored in **`telegram_wallet_sessions.username`** (written by `_save_wallet_session`), NOT in `user_data`, so tipping by `@handle` found nothing. `find_wallet_by_username` now prefers `user_data.username` and **falls back to `telegram_wallet_sessions.username`** (case-insensitive `ilike`, ignores NULL/blank handles, each table guarded independently). **When resolving a Telegram `@handle`, never rely on `user_data` alone** Ð ÐÐ¢âÐ²ÒÒ the bot table is the source of truth for Telegram-only registrations.
- **Recipient resolution hardening + diagnostics (2026-10 follow-up).** The same "not registered" reply had several independent causes, all addressed:
  - **`_MENTION_RE` was 3Ð ÐÐ¢âÐ²ÒÒ£24 chars** Ð ÐÐ¢âÐ²ÒÒ Telegram handles are 5Ð ÐÐ¢âÐ²ÒÒ£32, so a long `@handle` failed mention parsing entirely. Widened to **3Ð ÐÐ¢âÐ²ÒÒ£32**.
  - **A failed wallet-store read was indistinguishable from "not registered"** (`get_saved_wallet` collapsed both to `""`). New `lookup_saved_wallet()` returns `{found, wallet, error, rows}`; `resolve_recipient` now returns **`recipient_lookup_unavailable`** (new friendly "temporary server/database issue Ð ÐÐ¢âÐ¢Â° try again" message) on a DB error instead of the misleading "not registered".
  - **Reply-target fallback by @handle:** `resolve_recipient(..., reply_username=...)` Ð ÐÐ¢âÐ²ÒÒ when the replied-to member has no saved-wallet row (e.g. their row predates username capture), their **Telegram @handle** (from `reply_to_message.from.username` via `telegram_bot._tip_reply_username`) is tried as a last resort against both username tables. New source `reply_username`.
  - **Visibility:** `handle_tip` logs every tip with `chat_id`/`admin_id`/`reply_user_id`/`reply_username`/text, and `_execute_parsed` logs the exact resolution inputs (`error`, `source`, explicit, reply ids) on failure Ð ÐÐ¢âÐ²ÒÒ the user-facing message is intentionally vague, so the log is the only way to tell `recipient_unknown` (bad `@handle`) from `recipient_not_registered` (no row) from `recipient_lookup_unavailable` (DB). The webhook also logs every incoming `message` (`chat_type`, `from_id`, `is_bot`, text) so a Privacy-mode gap is immediately visible.
  - **Admin diagnostics endpoint `GET /telegram/tip-diagnostics`** (session + `is_admin` gated, read-only): returns `getMe` (**`can_read_all_group_messages`** = group Privacy check), `getWebhookInfo`, tip env presence (`tip_key_set` + public `tip_key_address`, never the key; prefixes/aliases/reply_trigger/allowlist count), and optional `?user_id=<telegram_id>` Ð ÐÐ¢Â¶Ð²ÒÒ· `lookup_saved_wallet` result. Use it to prove whether the group bot is actually receiving the trigger before touching code.
- **Do NOT** gate on `/tip` with client-supplied data, reuse another treasury key, or add a token without a registry row + the SQL `token` value. Tests: `tests/test_telegram_tip_content.py` (43 tests, no deps Ð ÐÐ¢âÐ²ÒÒ functional drive of the REAL service + blockchain with stubbed supabase/web3/eth_account, plus wiring; includes `test_find_wallet_by_username_falls_back_to_telegram_sessions` and `test_find_wallet_by_username_prefers_user_data_over_telegram`). Run with `python -m pytest tests/test_telegram_tip_content.py`.

## Public Community Chatroom â messages ON-CHAIN (2026-10)
The room's **text and image messages now live in the `GoodMarketChat` contract as events**, so the app owner stops paying per-message database cost. The **USER pays the gas** â that is the whole point: posting is a wallet signature, the server holds no key for this room and never writes a message row.

### Hybrid model (what moved, what did NOT)
Four things cannot leave the database, so the room is hybrid by design:
- **Messages (text/image/replies) â ON-CHAIN.** The contract is the record.
- **Images â still ImgBB.** Only the resulting URL goes on-chain (the contract stores no bytes). An on-chain body is whatever the author signed, so an image URL must pass `_attachment_url_allowed` (the `CHATROOM_ATTACHMENT_HOSTS` allow-list) or it is **downgraded to text** rather than rendered.
- **Tips â the tips table stays as the LEDGER** (idempotency + on-chain verification need a UNIQUE `tx_hash`). The public *announcement* is now an on-chain `KIND_TIP` post signed by the sender. **The transfer has already moved by then, so a declined announcement signature must NEVER fail the tip** â `record_tip` returns it as a separate `announcement` step for the browser to sign.
- **Reports â still the database** (no contract concept; admin-only workflow).
- **Bans â ON-CHAIN** (the contract's `banned` mapping). `service.is_banned` reads the contract first and **fails OPEN on a chain error** (a moderation outage must not lock everyone out), then falls back to the DB list so a pre-migration ban still applies.

### The three-hop post (no server signing)
`static/js/chatroom-widget.js` `postMessage()`:
1. `POST /chatroom/api/prepare-post` â the server builds the **unsigned** calldata (one implementation of the encoding) and returns `{to, data, value, chain_id, network}`.
2. the **user's own wallet signs and broadcasts it** â the user pays the gas.
3. `POST /chatroom/api/posted` â drops the read cache so the next poll renders it. **The event IS the record; this endpoint writes nothing.** A failed cache-drop must NOT fail a broadcast post.

Signer routing mirrors the tip flow (`_postSendTransaction`): `local` â the PIN-unlocked in-app wallet (sent the tx's own `chainId`, **no gas fields** â it estimates via ethers); WalletConnect â the WC session; else injected, **bound to the account matching the session wallet** (`Wrong wallet connected`), with an explicit `gas` + `gasPrice` (`_postGasParams`, 150k fallback / 300k ceiling â deliberately NOT the tip's 120k, a post is a different call). `eth_estimateGas` returns units only, so **`gasPrice` must be sent too** or the wallet asks its own RPC and can show the red "Network fee unavailable" dead-end.

### Read path (`chatroom/onchain.py`)
Raw JSON-RPC (never web3) so it imports and tests without deps:
- **Calldata is hand-encoded** and verified byte-for-byte against web3's ABI encoder in the tests. `POST_SELECTOR`/`POSTED_TOPIC` are **computed from the signature at import** (pinned literals only as a fallback), so a stale constant cannot silently post nothing.
- **Calldata head offset is 96; the EVENT data offset is 128** (3 words after the selector vs 4 head words). Confusing the two decodes an empty body â the test fixture originally had this wrong.
- `decode_posted_log` accepts **both** `HexBytes` (web3/tests) and hex strings (JSON-RPC). `int(str(HexBytes))` raises and silently collapsed every id to 0.
- `_fetch_logs` **halves the range down to a SINGLE block** on a refusal. A fixed floor above the provider's real cap is the exact defect the paluwagan scan shipped: the first chunk is already too large, the "adaptive" retry can never shrink below its floor, every scan fails, and the room looks empty. (Locked by a test parameterised over caps 1/2/5/4000.)
- The scan is **anchored at `CHAT_DEPLOYMENT_BLOCK`**, else a bounded `CHATROOM_LOOKBACK_BLOCKS` (120k). Without an anchor it would walk from genesis on every page load.
- An **RPC failure returns `success: False`** â never a false-empty room. The route then falls back to the DB so an outage shows the last known room.

### Routes
`GET /api/messages` serves on-chain when configured, else the DB. `POST /api/messages` returns **409 `onchain_required`** when on-chain â otherwise a post could be written off-chain and be invisible to everyone reading the contract. `POST /api/prepare-post` + `POST /api/posted` are new. `/api/state` reports `onchain`, and every new endpoint keeps the `svc.is_enabled()` feature-flag gate.

### Frontend
`onchainMode` (from `/api/state`) switches `send()` to the signing path, and **hides the admin Delete button** â the contract has no delete, only bans, so the control would only ever fail. Admin badges still work (`_admin_wallets` batched lookup, `is_admin` as a BOOLEAN; **the wallet address is still never sent to the browser**).

### Config + deploy
`CHAT_CONTRACT`, `CHAT_DEPLOYMENT_BLOCK` (the deploy script prints both), `CHATROOM_MAX_MESSAGE_BYTES` (500, mirrors the contract's `MAX_LEN`), `CHATROOM_LOOKBACK_BLOCKS`. **A contract deploy is required** â without `CHAT_CONTRACT` the room behaves exactly as before (DB-only), which is why the whole migration is backward compatible.

### Rules (do not regress)
- **Never sign server-side or add a relayer/gas-sponsor for this room** â the user pays, by design.
- **Never write a message row while on-chain** â the event is the only record.
- **Never drop `gas`/`gasPrice` from an injected post** (red "Network fee unavailable"), and never reuse the tip's gas numbers.
- **Never cache or report a chain failure as an empty room.**
- **Never re-add a fixed chunk floor** to the log scan.
- Tests: `tests/test_chat_contract_content.py` (39 â contract safety + gas budget, drives the REAL contract on a local EVM) and `tests/test_chatroom_onchain_content.py` (75 â encoding vs web3's encoder, decode for both wire shapes, adaptive chunking across provider caps, privacy rule, admin/ban resolution, route/widget/template/deploy wiring, plus `scripts/smoke_chatroom_onchain.cjs`, a node harness over the REAL widget functions: correct signer per login method, gas+gasPrice sent, wrong-wallet refusal, the three-hop order, and cache-drop tolerance). No deps; run with `python -m pytest tests/test_chat_contract_content.py tests/test_chatroom_onchain_content.py`.
- **Stub-pollution gotcha:** both test files resolve `solcx`/`web3`/`eth_tester` through `_real_module()`, which drops a cached entry that has no `__file__`. Other content tests install a partial `web3` stub and never restore it, which otherwise makes the EVM tests error **only when the whole suite runs** (`Web3 has no attribute EthereumTesterProvider`).

## Community Chatroom - links-with-text, line breaks, image sharing (2026-10)
Three user-reported bugs, all fixed:
- **"link lang ang naipadala"** - `static/js/chatroom-widget.js` `URL_RE` had a **capturing group** (`/(https?...)/`), which shifts `String.replace`'s callback signature: the 2nd arg is the group value, not the offset, so every plain-text span around a link was dropped. Fixed by making the regex **non-capturing** and reading the offset from `arguments[arguments.length - 2]` (defensive). The logic is now the named `linkifyInto(node, text, createLink)` helper. **Never re-add a capturing group to `URL_RE`.**
- **Line breaks / spaces lost** - the loss was **server-side**: `service.sanitize_message` stripped `_CONTROL_CHARS = \x00-\x1f`, which includes `\n` and `\t`. Now CRLF/CR normalize to `\n`, control chars are stripped **except `\n`**, tabs/spaces collapse per line, and 3+ blank lines collapse to 2. The CSS (`white-space: pre-wrap`) was already correct.
- **Image sharing (images only, hosted on ImgBB)** - `POST /chatroom/api/upload` (multipart `file`) validates MIME (`image/png|jpeg|gif|webp`) + size (`CHATROOM_MAX_UPLOAD_MB`, default 5) via the existing `object_storage_client.upload_to_imgbb` (now takes an optional `expiration`). The widget has a paperclip button, **paste** and **drag-drop**, an upload chip/preview, and renders `message_type='image'` rows as an `<img>` built with the DOM API (`img.src`, never markup injection; `loading=lazy`, `referrerpolicy=no-referrer`). New columns on `community_chat_messages`: `attachment_url/kind/mime/name/bytes`; `normalize_attachment` re-validates on write (https + host allow-list `CHATROOM_ATTACHMENT_HOSTS`, kind must be `image`). An image post may have an empty caption.
- **No Replit / no non-image files** - the deployment is not on Replit and there is no other object store, so only images are supported. **ImgBB's API v1 has NO delete endpoint**, so admin soft-delete hides the row but cannot purge the hosted copy; retention is the optional `CHATROOM_IMAGE_EXPIRATION_DAYS` (upload-time `expiration`, 0 = keep). Do not promise programmatic deletion.
- Tests: `tests/test_chatroom_media_content.py` (21 tests). Proposal: `docs/CHATROOM_LINKS_TEXT_IMAGES_PROPOSAL.md`.

## Community Chatroom - smaller phone panel, manual open, dismiss affordances (2026-10)
The floating panel was a **94-95% of viewport width** takeover on a phone that **auto-opened on load** and had **no way out** except the small `Ðâ` in the header. Users reported it covering the wallet balance card. Fixed:
- **Phone panel is capped and keeps a gutter.** `static/js/chatroom-widget.css` `@media (max-width: 640px)` used `width: calc(100vw - 20px)` (a 10px gutter on each side). Now `width: min(320px, calc(100vw - 88px))` and `max-height: min(440px, calc(100dvh - 216px - env(safe-area-inset-bottom, 0px)))`; `.gm-chat-messages` is `min(220px, ...)`. Measured: 360x640 340x460 (94% w) -> **272x402 (76% w)**; 390x844 370x460 (95% w) -> **302x395 (77% w)**; desktop unchanged (266x352). The panel is anchored to the RIGHT, so the reclaimed space is the **left strip** (~78px) where the balance card/token rows stay visible and tappable. **Do not re-introduce a full-bleed phone width.**
- **Why the width is the load-bearing fix, not the height:** the panel is bottom-anchored, so a shorter panel just moves its top edge DOWN and covers the same middle content (sweeping `max-height` 460->260px left balance-card coverage at 59% regardless). And real rendered rows are **69-88px**, so the 220px list is only ~3 messages Ð²Òâ going lower makes the room awkward. Keep the list at 220px.
- **The panel no longer auto-opens.** `mount()` was `setOpen(opts.autoOpen !== false)` (default ON); it is now `setOpen(opts.autoOpen === true && !(hasLauncher && readCollapsed()))`. The **floating widget** (wallet page, `_chatroom.html`) mounts with no `autoOpen`, so it starts CLOSED and opens only when the user taps the launcher; the panel markup carries `hidden` and the launcher starts `aria-expanded="false"` so it cannot paint open before the widget mounts.
- **Dismiss affordances (launcher-based widget only):** a `document` `keydown` **Escape** handler and a `document` `pointerdown` **tap-outside** handler (ignores taps inside `.gm-chat-panel`, on `.gm-chat-toggle`, and inside `[data-gm-tip-modal]`). `setOpen` now also keeps `aria-expanded` truthful. Previously the only close control was the header `Ðâ`.
- **The standalone `/chatroom` page is deliberately exempt.** It owns the whole viewport, has **no launcher and no close button** (`templates/chatroom.html` has zero `data-gm-chat-toggle` / `data-gm-chat-close`), so a remembered collapse or an Escape press would blank the page. Both the collapse memory and the dismiss handlers are gated on `hasLauncher` (`!!toggle`). Its `{ autoOpen: true }` still opens it directly. **Any new page mounting the widget without a launcher must get the same treatment** Ð²Òâ the guard is behavioural, not a page check.
- **Collapsed state is remembered** in `sessionStorage['gmChatCollapsed_v1']` (written on every `setOpen(false)`), so closing the room does not re-open it over the wallet on the next page load. Only consulted when a launcher exists.
- Tests: `tests/test_chatroom_content.py` (+5 Ð²Òâ mobile width cap/gutter, phone-width arithmetic, collapsed-on-load via DOMContentLoaded then tap-to-open, Escape + tap-outside (inside/launcher taps keep it open), remembered collapse, and the standalone-page exemption; the node harness gained a `sessionStorage` stub and a default `closest()`). `tests/test_wallet_nav_button_color_content.py::test_unread_badge_and_setopen_behaviour_are_untouched` updated from the old `opts.autoOpen !== false`. Proposal: `docs/CHATROOM_PANEL_MOBILE_SIZE_PROPOSAL.md` (status IMPLEMENTED). Run with `python -m pytest tests/test_chatroom_content.py`.

## Community Chatroom - network fee notice + CIP-64 fee currencies (2026-10)
Posting is an **on-chain transaction the USER signs**, but the room never said so â so the wallet signature prompt looked arbitrary, and a user holding only stablecoins could not post at all. Two user reports, one root. Proposal: `docs/CHATROOM_FEE_CURRENCY_PROPOSAL.md`.
- **CELO is ALWAYS the headline figure.** The fee is priced in CELO (the chain's unit of account â the one number comparable across wallets); the chosen token only *settles* it. `estimate_post_fee()` returns `celo_cost`/`celo_cost_formatted`/`usd_cost` for every currency. The notice above the composer reads `Network fee: 0.01451626 CELO (â $â¦), paid in CELO. Posting is a blockchain transaction, so your wallet will ask you to sign.` â the second sentence is the answer to "bakit need ng mag sign sa wallet?", not decoration. For a stablecoin it reads `about <CELO amount> Â· paid in USDT`. **Never price the notice in the settling token** â a user comparing wallets needs the CELO number.
- **BILL GAS USED, NOT THE TX LIMIT (2026-10 correction â a user caught this in production).** The first version quoted `POST_GAS_LIMIT` (150,000) Ã gas price and showed **0.0305 CELO**; the real post cost **just over 0.01 CELO**. The limit is *insurance* â unused gas is refunded, so it is never the cost. The contract's measured worst case is **71,728 gas** (cold: a new wallet pays a cold `SSTORE` for `lastPostAt`); warm is ~49k. Billing 150k over-stated the fee **~2Ã**. `estimate_post_fee()` now bills `POST_GAS_USED` (72,000, the measured cold worst case, so a new user's first post is never understated) and returns `gas_limit` SEPARATELY. Verified live at ~201 gwei: **0.01451626 CELO**. **Never bill the limit again** â `tests/test_chat_contract_content.py` already measures warm/cold and fails on a gas regression, so re-derive `POST_GAS_USED` from that if the contract changes.
- **A CIP-64 fee currency costs MORE, and the copy must say "about".** Celo charges extra intrinsic gas whenever `feeCurrency` is set â measured live via `eth_estimateGas`: bare transfer 21,000 native vs **71,865** with cUSD; ~500 B calldata 42,204 vs **80,230**. `FEE_CURRENCY_INTRINSIC_GAS` (default 50,000, the conservative end of the 38kâ51k range) is added to the billed units for any non-native choice. The stablecoin notice therefore says "about *X* CELO Â· paid in USDT" and **never** "same value as CELO" (an earlier draft claimed that; it was false).
- **`chatroom/onchain.py` fee registry:** `CELO` (native â *omit* the field), `CUSD` (18 dp, token address), `USDT`/`USDC` (**6 dp â the Celo ADAPTER address**, `0x0E2A3e05â¦` / `0x2F25deB3â¦`). USDT/USDC sent as their own token address would be wrong â the adapter normalises to 18 decimals. All env-overridable (`CELO_CUSD_TOKEN`, `CELO_USDT_ADAPTER`, `CELO_USDC_ADAPTER`). `FEE_CURRENCY_SUPPORTED` is Celo-only; an XDC room offers native XDC alone.
- **A fee preference can NEVER block a post** (the load-bearing rule), for **every** login method: an unknown key falls back to CELO in the route; `_feeCurrencyCandidates()` always appends `null` (native) and BOTH the injected and the **local (in-app)** branch walk that candidate list, retrying without the field; and a wallet that ignores CIP-64 just pays CELO. Retry happens **only** on a fee-currency error â a **4001 user rejection (or a cancelled PIN prompt) ends the flow** and is never re-prompted with another token.
- **A background state poll must not reset the user's choice.** `/api/state` always reports the CELO default, so `feeChoiceChosen` latches the moment the selector changes and the poll then stops adopting the server default. Without it the picker silently flipped back to CELO every few seconds.
- **Injected/WC keep explicit `gas` + `gasPrice`** (40% pad, node gasPrice, fallbacks) â the repo-wide "Network fee unavailable" lesson: `eth_estimateGas` returns units only, so without `gasPrice` the wallet asks its own RPC for the price.
- **The in-app wallet signs type-0x7b by hand.** ethers 6.13.4 THROWS `unsupported transaction type` for types 122/123/124, so `_sendFeeCurrencyTx()` builds `0x7b || RLP([chainId, nonce, maxPriorityFeePerGas, maxFeePerGas, gasLimit, to, value, data, accessList, feeCurrency, yParity, r, s])`. Verified against Celo mainnet (node decodes `types.CeloDynamicFeeTxV2`). **Two gotchas:** (1) RLP integers must be canonical â zero is the EMPTY STRING and a leading zero byte gets the whole tx rejected as `non-canonical integer (leading zero bytes)`; (2) the `eth_estimateGas` call must include `feeCurrency`, since Celo adds a token-specific intrinsic cost. Native CELO keeps the plain ethers signer byte-for-byte.
- **Wiring:** `/chatroom/api/fee?currency=` (flag+auth gated), `fee`/`fee_currencies`/`fee_supported` on `/api/state`, `fee_currency` accepted by `prepare-post` and echoed back as `fee_currency_address`/`_native`. Markup `data-gm-chat-fee-bar` sits **above** `data-gm-chat-form` in BOTH `_chatroom.html` and `chatroom.html`, `hidden` by default (so a DB-only room never flashes it), and the bar is hidden entirely when `onchain` is false. The selector only renders when there is a real choice (`supported && list.length >= 2`).
- Tests: `tests/test_chatroom_fee_content.py` (47 tests, no deps â functional drive of the REAL fee helpers: CELO-first/default, case-insensitive resolution, adapter vs token addresses, **gas-used-not-the-limit billing + the live 203 gwei â 0.0145 CELO check + the CIP-64 surcharge**, CELO-denominated pricing for every currency, unreadable-price â `estimated:false` + "varies" (never a wrong number), gas-price + USD caching, market-outage swallow; plus routes/templates/CSS/widget/local-wallet wiring (incl. the poll-must-not-reset-the-choice latch, the local CIP-64 fallback, and the no-"same value as CELO" copy rule), and TWO node harnesses: `scripts/smoke_chatroom_onchain.cjs` (extended with the local + injected CIP-64 send/retry/user-rejection cases over the REAL `_postSendTransaction`) and an inline harness that drives the REAL `_sendFeeCurrencyTx`, decodes the signed type-0x7b tx and checks the signature recovers). **Mutation-verified: billing the limit again fails 3 tests, dropping the CIP-64 surcharge fails 1, and dropping the native candidate / `feeCurrency` from the estimate / the CELO cost / the local fallback each fail.** Run with `python -m pytest tests/test_chatroom_fee_content.py`.

## Chatroom tip - desktop extension never got the signature request (2026-10)
A desktop MetaMask user tapped Tip, the sheet said "Confirm in your wallet...", and **nothing ever appeared in MetaMask**. Root cause was a **provider-ordering bug in `static/js/chatroom-widget.js` `_tipProvider()`**: it checked the WalletConnect session BEFORE the extension, and `_prefersWc()` is true for any *stored* WC session (not only a WC login). So a leftover session from an earlier WalletConnect sign-in hijacked the tip, the request went to the (dead) WC relay, and the extension was never asked. Proven with a node harness over the REAL widget: the old code produced `wcProviderUsed:true, extensionCalls:[]` and "No active WalletConnect session".
- **P1 Ð²Òâ `_tipProvider()` rewritten.** Order is now: local (in-app, PIN) -> Privy -> **WC only when `_isWcLogin()`** (the strict login-method signal) -> injected via `_tipResolveInjected()` -> WC-session fallback only when **no** injected provider exists. `_tipResolveInjected()` runs EIP-6963 discovery (`window.__announced6963Providers` + a fresh `eip6963:requestProvider` dispatch), asks EVERY candidate for accounts (`eth_requestAccounts` then `eth_accounts`) and binds to the one holding `TIP_BOOT.wallet` Ð²Òâ so with two extensions installed (or a late-injecting one) the extension holding the funds is the one prompted. `window.ethereum` is last-writer-wins and is no longer trusted alone.
- **P2 Ð²Òâ account before chain.** `_tipSendTransaction()` resolves and matches the account FIRST, then `_tipEnsureChain()`. A rejected/unsupported chain switch can no longer abort before the wallet was asked for an account (which is also what unlocks a locked extension).
- **P3 Ð²Òâ the sheet names the signer.** `TIP_SIGNER` + `_tipSetSigner()`/`_tipProviderLabel()` render "Signing with MetaMask." / "Trust Wallet" / "WalletConnect" / "In-app wallet" so a failure is never a mystery. A prediction shows on open, the real label once the provider is resolved.
- **P4 Ð²Òâ per-wallet WC cleanup.** `wc-bridge.js` gained `_clearStoredSessionFor(wallet)` (exported as `GMWalletConnect.clearStoredSessionFor`) Ð²Òâ clears a stored WC session ONLY when it belongs to that wallet. `get-started.js` calls it after an injected login instead of the blanket `clearStoredSession()`, so a session for a DIFFERENT wallet is preserved.
- **P5 Ð²Òâ `wallet-main.js` conflation removed.** `_getEthProvider()` used `if (_gmPreferWc()) return null`, which let a leftover WC session suppress a genuinely injected login on **every** page (claim/send/stream/raffle). New login-aware `_gmShouldUseWcSession()` = WC-family login OR (no injected provider holds the session wallet AND a WC session exists). All four signing call sites (`getClaimSigner`, `_aiSwapResolveSigner`, raffle signer, stream signer) use it; `_xdcBannerProvider()` dropped its redundant guard.
- **Wrong-wallet copy keeps the recognizable phrase:** "Wrong wallet connected. MetaMask has a different account selected Ð²Òâ ...".
- **Do not** re-order WC ahead of the injected branch, re-add the bare `window.ethereum` shortcut, or gate the injected path on `_gmPreferWc()`.
- Tests: `tests/test_desktop_injected_signing_content.py` Part 3 (10 new: content wiring + a node harness over the REAL widget for injected-with-stale-WC -> extension signs, two extensions -> matching one signs, no-extension -> WC fallback, WC login -> WC only, account-before-chain). Updated expectations in `test_ai_agent_swap_content.py` (`_gmShouldUseWcSession()`) and `test_xdc_rpc_repair_banner_content.py` (faithful `_getEthProvider` stub). Run with `python -m pytest tests/test_desktop_injected_signing_content.py`.


## Responsive design - mobile/desktop audit + the three shipped fixes (2026-10)

The app had **no shared responsive contract**: each page invented its own
breakpoints, and nine pages scaled the whole document with `html { zoom: 0.7 }`
at EVERY width. A real-browser audit (headless Chromium, 320/360/390/412/768/
1280) found 4 defects; the 3 highest-value ones are fixed. Full write-up:
`docs/RESPONSIVE_MOBILE_DESKTOP_PROPOSAL.md`.

### Shipped

- **D1 - `/send-link` was 2x the screen width on a phone.** `templates/send-link.html`
  had **NO media query at all**; its desktop navbar (`.nav-links` measures ~503px
  on one unwrappable row) pushed the page **351px** past a 320px viewport. Added
  `@media (max-width: 640px)`: `.nav-links` becomes a swipeable strip
  (`overflow-x:auto` + `min-width:0` + `flex-shrink:0` anchors), `.wallet-tag`
  hidden (the body already shows the wallet), plus `body { overflow-x: clip }`
  as a backstop. Measured 351px -> **0px**. All 5 destinations stay reachable.
- **`overflow-x: clip`, NOT `hidden`.** `hidden` makes the body a scroll
  container, which **kills the sticky navbar** (`position:sticky`). Verified
  `position:sticky` still pins after scrolling. Any future overflow backstop on
  a page with a sticky header must use `clip`.
- **D2 - homepage feature card escaped its card at 320px.** `.fs-game-pad` was a
  fixed **92px**; now `clamp(64px, 20vw, 92px)`. `.fs-mult-row` gained
  `flex-wrap: wrap` (~167px unwrapped) and `.fs-game-multiplier` gained
  `min-width: 0` (a flex child needs it to shrink below its content width).
  Measured multiplier right edge 332px -> **263px**, inside its own card.
- **Rule 2 - pinch-zoom restored on 9 pages.** Removed
  `maximum-scale=1.0, user-scalable=no` (`admin_dashboard`, `chatroom`,
  `community_stories`, `jumble_game`, `learn_and_earn`, `lotto`) and
  `maximum-scale=1.0` alone (`farming`, `wallet`, `xdc_wallet`). All now ship
  `width=device-width, initial-scale=1.0`. **WCAG 1.4.4** (Resize Text) - locking
  the scale blocks low-vision users from magnifying a wallet they use to check
  amounts. **Never re-add `maximum-scale`/`user-scalable=no`.**

### Tests

`tests/test_responsive_contract_content.py` (28 tests, no deps). Mutation-verified:
**16 fail on the pre-fix code**, all 28 pass after. Two are deliberate
**ratchets** guarding work this pass did NOT do:

- `test_every_full_page_has_a_media_query_or_explains_why_not` pins the pages
  that still have no media query (set may only shrink; a NEW page must have one).
- `test_desktop_zoom_never_applies_on_phones` pins the 9 pages still applying
  `zoom: 0.7` at every width and fails if a new one appears.

**Do not "fix" the ratchet lists by deleting entries** - they are the honest
baseline, and the zoom unification is an open product decision (below).

### Deferred (deliberate - needs a product call)

- **The `zoom: 0.7` split.** 10 pages apply it at every width, 6 do not, so the
  same icon is **78px on wallet vs 34px on another page**. Unifying it changes
  how the app FEELS on desktop, so it is proposal open-question 1 (Option A
  natural scale everywhere vs Option B compact everywhere).
- **One desktop content column.** Currently wallet 1180px, swap/savings 1300px,
  minigames 1400px, reloadly 900px, lotto 820px, homepage 1024px.
- **`100vh` -> `dvh`** (33 sites). On mobile `100vh` is the LARGEST viewport
  height, so a `min-height:100vh` page is taller than the visible area.
- Lower-severity leftovers (no page-level sideways scroll): `reloadly`,
  `learn_and_earn` (6 offenders at 320px only), `xdc_wallet`, `dashboard`.

### Audit method (reusable)

`scripts/render_page_preview.py` renders any template standalone (stubbed Jinja
context incl. `homepage_stats`); `scripts/render_wallet_preview.py` /
`render_homepage_preview.py` are the page-specific ones. Then load the HTML in
Playwright and read `scrollWidth` vs `clientWidth` + `getBoundingClientRect()`
per element. **Do not trust a static CSS read** - the send-link overflow was
invisible in the source and obvious in the measurement.

**Gotcha:** never name a scratch script `inspect.py` - it shadows the stdlib
`inspect` module and Playwright then fails with a confusing circular-import error.

### Regression check

Full suite before vs after: **1964 passed, failure list byte-for-byte identical
(44 items both times)** - those 44 are pre-existing sandbox issues (the
`supabase` module is not installed), unrelated to these edits. Re-verify with a
before/after diff of `pytest ... | grep -E '^(FAILED|ERROR)'` rather than
trusting an absolute count.

## G$ Raffle - injected wallet detection (Trust Wallet mobile) (2026-10)
Reported: a Trust Wallet mobile user tapped the G$ Raffle and was told **"No wallet detected"** while the same injected login worked on claim/send. Injected wallets (MetaMask / Trust / MiniPay / Rabby) all share `login_method === 'injected'` - the difference was that the **raffle's signer resolver was less capable than every other flow's**. Three defects in `static/js/wallet-main.js`; proposal `docs/RAFFLE_WALLET_DETECTION_PROPOSAL.md`.
- **`_awaitEthProvider()` keyed its wait budget on a Trust-ONLY signal.** `const trustBudget = _isTrustWalletMobileContext() ? 4200 : 800;` - but `_isTrustWalletMobileContext()` needs a Trust-specific marker (UA contains `trust`, or a `window.trustwallet` / `window.ethereum.isTrust` alias) AND a touch-mobile UA. Trust Wallet's in-app browser frequently ships a **plain Chrome/Safari UA with no alias**, so the check was `false`, the budget fell to the **800 ms desktop** value, and the resolver gave up before Trust injected (~1.2-2.5 s). Fix: new `_isLikelyMobileWalletContext()` (the regex `swap-core.js` / `get-started.js` already use) as the middle tier - `_isTrustWalletMobileContext() ? 4200 : (_isLikelyMobileWalletContext() ? 2500 : 800)`. **Desktop deliberately keeps the 800 ms budget** (extensions inject synchronously; a user with none must fail fast). Measured with the real bodies: plain-UA Trust at 1200/2500 ms went fail -> pass, desktop unchanged.
- **`getRaffleSignerProvider()` only tried the injected provider.** `getClaimSigner()` / `getClaimProviderAndFrom()` / the chat swap resolver all try **Privy -> WalletConnect session -> injected**; the raffle went straight to `_vAwaitEthProvider()`. A WalletConnect login has **no injected provider at all**, so the raffle threw "No Web3 wallet detected" - while a live WC session existed, and the copy told the user to "open this page in WalletConnect", i.e. exactly what they had done. Now it mirrors the standard order, with the **local (in-app) branch still first** so an extension can never hijack an in-app account, plus a stored-WC-session last resort for legacy logins.
- **`GMTxError.toFriendlyMessage` DOES NOT EXIST** - `tx-error.js` exposes `format` / `isUserRejection` / `asFriendly` / `decodeRevertData` / `revertReasonFromError`. All three raffle handlers called it, so **every** handled failure threw a second `TypeError: ... is not a function` inside the `catch`, the alert never rendered, the panel was never refreshed, and the user was left with a **permanently disabled button reading "Preparing..."**. Reproduced by driving the REAL `joinGdRaffle()` (before: `alertText:""`, `refreshCalls:0`, uncaught TypeError; after: alert shown + refresh + no throw). Fixed to `GMTxError.format(err)`. **Rule: a throw inside a `catch` is invisible - only call methods that exist on `GMTxError`, and always surface the reason + re-read state.**
- **Rules:** never key a mobile wait budget on a Trust-only signal; every signer resolver must try Privy -> WC -> injected (local first), and "no injected provider" must never be reported as "no wallet".
- Tests: `tests/test_raffle_wallet_detection_content.py` (15 tests, no deps - node harnesses over the REAL `getRaffleSignerProvider` / `_awaitEthProvider` / `joinGdRaffle` bodies: late injection at 1200/2500 ms on a plain-UA Trust build, self-identifying Trust UA, synchronous injection, desktop fails fast, WC-login signs via the session, stored-session last resort, actionable no-wallet copy, local-only signing, plus `GMTxError` surface checks). Mutation-verified (reverting each of the three fixes fails the suite). Run with `python -m pytest tests/test_raffle_wallet_detection_content.py`.

## GoodMarket Community Feed - Facebook/Instagram-style posts, media in Cloudflare R2 (2026-10)
A separate module where users post their daily life - **images and videos** -
with reactions and comments. Called **GoodMarket Community** (`/community`).
Media bytes live in **Cloudflare R2**; Supabase stores metadata only. Proposal +
as-built record: `docs/COMMUNITY_FEED_R2_PROPOSAL.md`.
- **Module:** `community_feed/` (`service.py` = rules, `routes.py` = blueprint at
  `/community`, `storage.py` = the R2 glue) + **`storage/r2.py`**, a new
  stdlib-only S3 SigV4 client (no boto3 - it would fight the existing
  `requests`/`httpx` pins). Templates: `templates/community.html`,
  `static/css/community-feed.css`, `static/js/community-feed.js`. SQL:
  `sql/community_feed.sql`. Register with `init_community_feed(app)` in `main.py`.
- **HIDDEN by default, and the gate is server-side.** `service.is_enabled()` reads
  the `community_feed_feature` row from `maintenance_settings` and **fails
  CLOSED** (missing row / DB error / `is_maintenance = TRUE` all mean OFF). The
  page redirects and every `/community/api/*` call returns 403 while OFF. Turn it
  ON from **Admin -> Feature Visibility -> Community Feed**. `COMMUNITY_FEED_ENABLED=0`
  is an env kill switch that **wins over** the admin flag; leaving it UNSET means
  "defer to the admin flag" (not "off") - do not change that, or the toggle could
  never turn the feed on.
- **Uploads go BROWSER -> R2 directly with a presigned PUT.** No bytes pass
  through Flask: with gthread + 4 workers a proxied 100MB video would pin one of
  ~16 slots for the whole upload. `POST /api/upload/presign` validates the
  declared MIME + size and returns the signed URL; the browser PUTs and then
  `POST /api/posts` references the object. **There is no route that accepts raw
  bytes** (a test asserts `request.files` never appears).
- **The upload throttle admits ONE POST's worth of presigns, not one.** A post
  may carry `MAX_MEDIA_PER_POST` (4) files and every video also uploads a poster
  frame, so the presign route calls
  `_throttle(..., allow=UPLOAD_BURST_ALLOWANCE)` where the allowance is
  `MAX_MEDIA_PER_POST * 2`. `_throttle` still defaults to `allow=1`, so the post
  and comment limits are unchanged. **Do not drop the `allow=` argument**: with a
  single-slot window a 2+ image post failed from the second file on with "Please
  wait before uploading again" (HTTP 429) while a caption-only post worked - the
  "images don't upload" symptom. `COMMUNITY_UPLOAD_RATE_LIMIT_SECONDS` sets the
  window (default 10s).
- **A presigned PUT is not trust - the object is re-verified.** `storage.verify_upload`
  HEADs the object for its real size and reads the first 32 bytes to sniff the
  magic bytes. **The sniff is authoritative and a mismatch is rejected**: an HTML
  document uploaded as `image/jpeg` must never be storable (that was a real hole -
  the first version only rejected a *positive* mismatch, so readable-but-unidentified
  bytes passed). Only a genuinely unreadable head fails **open** (some R2 setups
  refuse ranged GETs). `ALLOWED_MIME_TYPES` = JPEG/PNG/GIF/WEBP + MP4/WEBM;
  **SVG is deliberately not allowed** (it can carry script).
  **WEBP needs its own check, not a prefix match**: the RIFF header puts a length
  at bytes 4..8 before the `WEBP` form type, so `r2._is_webp` tests
  `head[:4] == b"RIFF" and head[8:12] == b"WEBP"`. A prefix-only table silently
  rejected every WEBP upload ("does not look like the type it claims to be")
  while the allow-list advertised it; the form-type check also keeps RIFF/AVI and
  RIFF/WAV out.
- **Object keys are server-generated and owner-scoped.** `r2.build_key` embeds a
  SHA-256 **hash** of the wallet (the path leaks no address) plus a uuid, and
  `verify_upload` checks the key starts with `u/<owner_hash>/` - so a client
  cannot reference, or later delete, another user's file. `is_managed_key`
  rejects traversal, absolute paths and `//`. **Never accept a client-supplied
  key.**
- **The wallet address NEVER reaches the browser.** `_public_post` /
  `_public_comment` drop it and expose `is_mine` as a **boolean** (same rule as
  the chatroom). A test asserts the serialized response contains no wallet.
- **Username requirement (2026-10) - the feed shows a NAME, never an address.**
  The fallback for a missing username used to be `short_wallet(owner)`, so a user
  who never picked one published `0x1234Écdef` - which reads as spam and partly
  undoes the privacy rule above. Now:
  - `REQUIRE_USERNAME` (env `COMMUNITY_REQUIRE_USERNAME`, default **on**) makes
    `create_post` and `add_comment` return
    `{success: false, code: "username_required"}` when the author has no username.
    Enforced in the **service**, so a direct API call cannot bypass the UI gate.
  - The gate runs **before** media validation, so a user is not asked to
    re-upload a 100 MB video only to be refused at the end.
  - `ANON_LABEL` ("Community member") is the fallback for **legacy rows** written
    before this requirement (and for a deploy with `COMMUNITY_REQUIRE_USERNAME=0`).
    It is a fixed string, **deliberately not `short_wallet()`** - a shortened
    address is still an address leak.
  - The composer renders `#cfUsernameGate` server-side (`{% if username_required
    and not username %}`) with an input wired to `GMCommunity.saveUsername()`,
    which POSTs to the existing app-wide `POST /api/user/username` - so the same
    rules (3-24 chars `[A-Za-z0-9_]`, uniqueness, once-per-year edit) apply here
    and on `/wallet`. On success the composer unlocks **in place** (the gate node
    is removed and `updatePostButton()` re-runs), so a caption already typed is
    not lost. Enter submits, and a client-side regex refuses a bad name before any
    request.
  - `needsUsername()` gates `#cfPostBtn` (`updatePostButton`) and is re-checked in
    `submitPost`, so a console/keyboard path is told to pick a name instead of
    failing later with a server error.
  - `/api/state` exposes `needs_username`; the template boot object carries
    `username` / `usernameRequired`, and the route passes both.
  - **Do not** restore a wallet-derived fallback, and do not move the gate after
    media validation.
- **Reactions: Messenger model, NOT Facebook's.** Six types
  (`like/love/haha/wow/sad/angry`), **one per wallet per post**, switchable, and
  tapping the active chip removes it. The composite PK `(post_id, wallet_address)`
  makes a second row physically impossible, so a switch is a single UPDATE and a
  double-tap can never inflate the count. The count is adjusted on the write path
  and is **never taken from the client**; `recount_reaction_count` can rebuild a
  drifted counter. (Facebook's six denormalised counters race and wrap on a 360px
  card - see the proposal Â§4.1.)
- **Caption + engagement placement (2026-10): ONE caption per post.** It is
  `community_feed_posts.body`; the media table has **no** caption column on
  purpose (per-file captions are how carousels get confusing). Typed in the
  composer textarea, rendered **below the media**. Directly after it, one
  `.cf-engagement` group contains the action row, reaction/comment counts, the
  heart-anchored reaction picker and any expanded comment thread. The six
  reaction choices are hidden until a 400ms press-and-hold or keyboard
  `ArrowDown`; tapping the heart toggles the `love` reaction. Order is pinned in
  the node harness: `cf-media -> cf-caption -> cf-card-actions -> cf-summary`;
  the picker is inside the action row and the comment thread stays inside
  `.cf-engagement`. Caption is optional when there is media, required when
  there is none.
  Rendered with `textContent` + a **non-capturing** `URL_RE` linkifier (a
  capturing group silently drops all surrounding text - the chatroom bug).
  Editable afterwards via `PATCH /api/posts/<id>` (stamps `edited_at`); **media
  is never editable** (swapping a video after reactions would change what people
  reacted to) - that flow is delete + re-post. Long captions clamp with a real
  "See more" toggle, never `-webkit-line-clamp` alone.
- **Sanitising:** `sanitize_caption` keeps `\n` (line breaks are legitimate),
  normalises CRLF/CR, strips control/zero-width chars, collapses runs. Bounded by
  **both** characters (`COMMUNITY_MAX_CAPTION`, default 2200) and **UTF-8 bytes**
  - an emoji costs 4 bytes, so a char-blind check lets ~8KB through.
- **Deletion really deletes.** Own-post delete and admin delete are **soft** in
  the DB (audit trail) but **hard-delete the R2 objects**, because a reported
  video must actually stop being served. `delete_objects` re-checks
  `is_managed_key` itself rather than relying on the transport guard.
- **Reads use the service-role client** (the lotto/gcash lesson: anon + RLS
  silently hides rows), and the migration **enables RLS with no policies** on all
  six tables as defence in depth - anon/authenticated can read/write nothing.
- **Pagination is keyset (`id < cursor`), never OFFSET** - offset paging over a
  live feed duplicates and skips rows as new posts land (locked by a test that
  inserts a post between pages).
- **Moderation:** report (one per wallet per post, idempotent), admin hide /
  delete+purge / ban-unban, and a **Community Feed** section in the admin
  dashboard (sidebar + `case 'community-feed-moderation'` in `loadSectionData` +
  a Feature Visibility toggle). **Report reasons are HTML-escaped** before
  rendering. **`_applyVisibilityBadge` silently no-ops on a missing element** -
  the badge id must exist in the markup; a test asserts every
  `_applyVisibilityBadge('X-status-badge')` call has a matching `id="X-status-badge"`
  element (this caught two badges that never rendered).
- **Not face-verification gated (deliberate):** like the chatroom, the feed is for
  all signed-in users and **guests may read it**; they get the shared connect card.
  Abuse is handled by the report/ban path. Re-adding an FV gate is a product
  decision.
- **Isolation:** this module does not import the chatroom or community stories,
  does not use ImgBB, and `object_storage_client.py` is untouched (tests assert
  all four both ways). Group/feed state is its own tables - nothing is merged into
  another feature's data.
- Tests: `tests/test_community_feed_content.py` (197 tests, no deps - functional
  drives of the REAL `service.py` / `storage.py` / `r2.py` against a fake Supabase
  and the AWS SigV4 published vectors: flag fail-closed + kill-switch semantics,
  caption sanitising/byte cap, MIME allow-list and the two size caps, **magic-byte
  verification incl. the stored-XSS rejection and WEBP's RIFF form-type check**,
  **the upload-throttle burst allowance (a whole post's presigns pass, an abuse
  burst is still blocked, and post/comment stay single-shot)**, key
  scoping/hashing, feed
  keyset pagination + no-wallet-leak, post/delete/edit ownership, reactions
  (switch/toggle/one-row/recount), comments, moderation, admin stats degradation,
  presign shape + signature re-derivation + expiry clamp + path encoding, and the
  SQL/route/template/CSS/admin/env wiring) plus `scripts/smoke_community_feed.cjs`
  (56 checks over the REAL widget JS: post-button gating, the username gate + save flow, the linkifier, caption
  placement + textContent, video controls/poster, the six chips, the presign
  round-trip with the signed Content-Type, **a 4-file burst making 4 presigns + 4
  PUTs with distinct keys**, upload refusals, and the guest guard).
  Mutation-verified (wallet leak, reaction double-count, flag fail-open, `%2F`
  path encoding, caption reorder, a badge-id mismatch, **the single-slot upload
  throttle, and the missing WEBP sniff** each fail the suite).
  Run with `python -m pytest tests/test_community_feed_content.py`.
- **Enabling it:** run `sql/community_feed.sql`, set the `R2_*` env vars, apply
  the bucket **CORS** policy (a missing rule is the #1 cause of "upload failed"),
  then flip the Feature Visibility toggle.
- **Community G$ rewards (2026-10) - `docs/COMMUNITY_REWARDS_LEDGER_PROPOSAL.md`.**
  **Four UNEQUAL rates** (owner's numbers, 2026-10-21), each an env var:
  comment received **2.5** (`COMMUNITY_REWARD_COMMENT_OUT`, the largest),
  comment given **1.5** (`..._COMMENT_IN`), reaction received **0.5**
  (`..._REACTION_OUT`), reaction given **0.1** (`..._REACTION_IN`, the smallest).
  Code in `community_feed/rewards.py`, SQL in `sql/community_reward_ledger.sql`,
  hooks in `service.py` (`set_reaction` / `clear_reaction` / `add_comment` /
  `delete_comment`), read endpoint `GET /community/api/rewards`.
  - **Only a FACE-VERIFIED wallet earns - and the check is NOT the session.**
    `is_eligible()` in `rewards.py` gates `_credit` on
    `blockchain.is_identity_verified` (the on-chain read). Do NOT gate on
    `session["verified"]`: that key is a LOGIN flag the login path sets for
    EVERY wallet ("UBI check temporarily disabled - allow all wallets in"), so
    gating on it lets every unverified wallet earn. A test asserts `rewards.py`
    never reads the session. The gate sits at the single credit choke point so
    it covers BOTH sides of an interaction (an unverified POST OWNER must not be
    paid when a verified user reacts); gating at the route would cover only the
    actor. It FAILS CLOSED - an unreadable chain means not-eligible, never
    assume-verified. The verdict is cached 300s per process (the read is an RPC
    on the hot path), but a chain ERROR is NOT cached, so an outage is retried
    instead of pinned for the TTL. `COMMUNITY_REWARD_REQUIRE_VERIFIED=0` is the
    escape hatch for a chain outage. This is an EARNING gate, not an access
    gate: unverified wallets still post, react and comment.
  - **Rates are DB-backed with an env fallback, resolved in ONE place.**
    `_resolve_rates` in `rewards.py` is the only source: precedence is an admin
    override (`maintenance_settings` / `community_reward_rates`) -> env var ->
    shipped default. The ledger, the caps and the card all read it, so they
    cannot disagree. The admin editor
    (`/community/api/admin/reward-rates`, GET + POST, admin-only) writes that
    config blob and NOTHING else - it edits the rate, never a balance. A write
    needs a reason, is logged with the admin wallet, and is clamped server-side
    (`RATE_BOUNDS`, ceiling `COMMUNITY_REWARD_RATE_MAX`, floor 0 = "switch it
    off"). A corrupt override falls back to the env value, not to 0, so a bad
    config row cannot silently kill the economy. The cache is PER PROCESS
    (60s): the worker that wrote invalidates immediately, the others catch up
    within the TTL. Do NOT add a balance editor - a test asserts the admin
    routes never touch `delta_gd`.
  - **The ordering IS the anti-farm design - do not flatten the rates.**
    `reaction_in` is the farm vector: the only rate that pays for tapping a
    stranger's post, and the only one with **no content left behind to audit**.
    `reaction_out` is the creator signal (a photo earns without a comment);
    `comment_out` is the largest because a comment is the strongest signal.
    Paying them equally makes the cheapest action the most profitable one.
  - **Both comment sides earn, from TWO separate ledger rows**
    (`comment_in:<id>` and `comment_out:<id>`), so a cap that refuses one does
    not block the other, and each reverses on its own. A comment reversal must
    therefore resolve the post owner (`_comment_post_owner` in `service.py`) -
    reversing only the commenter's row would leave the owner's credit standing.
  - **The owner's rule: "balance of the members never change from database".**
    No trigger stops a superuser, so the guarantee is STRUCTURAL: there is **no
    stored balance column at all** - the balance is `SUM(delta_gd)` over an
    append-only ledger (a VIEW). `UPDATE`/`DELETE` raise via trigger. Do not add
    a `balance` column; a test asserts it is absent.
  - **The card lives ONLY in `/community`** (never the wallet page), and the
    label is **"Community G$"** - NOT "G$ Earned", because the wallet page
    already uses "G$ Earned" for the referral figure (`#refStatEarned`). Two
    different numbers under one name is the exact confusion the proposal avoids.
  - **Idempotency is the anti-farm property.** `idempotency_key` is unique, so
    one source event credits exactly once *forever*: un-react then re-react nets
    zero, and a reaction switch (`love` -> `haha`) does not re-credit.
    `reverses_ledger_id` is unique (partial), so a replayed undo is a no-op.
  - **v1 has NO payout path.** No withdraw route, no button, `can_withdraw` is
    always `False`. The 500 G$ figure is a displayed goal, not a transfer. Ship
    earning first, measure, then wire `batchDisburseRewards` (proposal §12).
  - **A rewards failure must never fail the user's action.** Every hook is
    wrapped in `try/except` on BOTH sides (inside `rewards.py` and at the call
    site in `service.py`); a reaction/comment still succeeds if rewards explode.
  - **Flag:** `community_rewards_feature`, seeded HIDDEN, fails closed
    (missing row / DB error / `is_maintenance = TRUE` all mean OFF).
    `COMMUNITY_REWARDS_ENABLED=0` is an env kill switch that wins over the admin
    flag; UNSET means "defer to the admin flag". Turn ON from **Admin ->
    Feature Visibility -> Community G$ Rewards**. Caps:
    `COMMUNITY_REWARD_DAILY_CAP` (50), `COMMUNITY_REWARD_POST_CAP` (100),
    `COMMUNITY_REWARD_MIN_PAYOUT_GD` (500), `COMMUNITY_REWARD_START_AT` (empty =
    ungated; only matters if a backfill is ever run). **The caps are ceilings on
    the SUM, not on a row count** - with fractional rates a "row count" test is
    wrong; assert the total. **A rate change affects only FUTURE credits**: the
    ledger is append-only, so past earnings can be raised but never clawed back.
    A bad rate env var falls back to the default; a negative one clamps to 0.
  - **Tests:** `TestReward*` / `TestBalanceCannotBeManipulated` in
    `tests/test_community_feed_content.py` (91 rewards tests: `TestRewardEligibility`, `TestReward*`,
    `TestAdminEditableRates`, `TestAdminRewardRateRoutes`,
    `TestBalanceCannotBeManipulated`). The four rates are pinned, their ORDER is
    asserted, and the admin editor is covered for override precedence, partial
    updates, clamping, unknown-name refusal, cache invalidation, and the
    admin-only gate - so a well-meaning "simplify to one rate" or a loosened
    admin check cannot land silently. The test module
    **memoises `_load_packages()`** so the `service` and `rewards` fixtures share
    one module object - `service` does `from . import rewards`, and a fresh load
    per fixture would silently detach a `monkeypatch.setattr` on one from the
    other.
- **Card revamp + visible member earnings (2026-10 follow-up).** The owner first
  requested a hidden balance, then asked to restore the default-visible member
  balance and collapse the rates. This follow-up supersedes the original
  visibility direction. Owner report: *"yung card na
  community G$ rewards sobrang pangit walang ka buhay buhay, at dapat yung
  nakalagay na community earnings sa card dapat e hide ... dapat may color at
  background sa card."* Proposal + measurements:
  `docs/COMMUNITY_REWARDS_CARD_REVAMP_PROPOSAL.md`.
  - **The member personal balance is visible by default.**
    `COMMUNITY_REWARD_SHOW_BALANCE` defaults to `1`, is read **per call** by
    `rewards.show_balance()`, and is returned on both guest and member summaries.
    An explicit `0` remains the operator opt-out. The balance is not spendable or
    on-chain; the card must keep its no-withdrawal disclaimer. Guests never see
    the personal balance even when the server flag is true.
  - **It is still a presentation switch.** `#cfEarnAmount` / `#cfEarnGoal` /
    `#cfEarnBar` / `#cfEarnBarFill` stay in the DOM and are **still written to**;
    `#cfEarnBalance` starts hidden until data arrives, then is revealed only when
    `data.show_balance === true` and `data.is_guest` is false.
  - **The always-visible `#cfEarnLead` carries the state** ("Verify your identity
    to start earning G$." / "Sign in to earn G$ in the community." / "Earn G$
    when the community reacts to your posts and comments."). It lives OUTSIDE
    `#cfEarnBalance` and remains visible while the balance is hidden for guests.
  - **Rate details start collapsed.** `#cfEarnRatesToggle` is a native button
    controlling server-sourced `#cfEarnRates`; keep `aria-expanded` synchronized
    with the list's `hidden` state. Expanded state survives a rewards refresh.
  - **The card has colour: a warm G$ gold gradient** (`--cf-earn-bg1`
    `#fff7e6` → `--cf-earn-bg2` `#ffdfae`), a gold coin mark, solid status chips
    and a 2×2 grid of rate tiles. It is the ONE money surface on the page, so it
    **deliberately does NOT follow the feed's phone grammar** — it keeps its
    gradient/radius/shadow on a phone instead of flattening into a bare section.
    All colours live in the `--cf-earn-*` tokens.
  - **Contrast (WCAG AA, measured in a test, on the WORST gradient stop
    `#ffdfae` — a gradient is a range, so the lightest end is the test):**
    `--cf-earn-ink` 10.12, `--cf-earn-ink-2` 8.28, `--cf-earn-accent` 5.32. The
    rate values moved OFF `--cf-gold-deep` because it is only **2.37:1** there —
    the number that matters would have been the least readable element. Every
    chip state is measured too (6.38 / 5.91 / 8.62 / 6.76). The gold coin mark
    carries `--cf-gold-ink` (dark), per the wallet page's gold rule.
  - **One decorative loop, `cfEarnGlow` on `.cf-earn::before`**, animating
    `transform`/`opacity` only (no `box-shadow`/`left`/`top`/`width`/`height` —
    those force a full repaint every frame). It is disabled in the
    `prefers-reduced-motion` block together with the progress bar's transition.
  - Tests: `TestRewardsCardRevamp` in `tests/test_community_feed_content.py`
    (visible member default + per-call read + payload, guest privacy, initially
    hidden markup, accessible collapsed-rate toggle, always-visible lead,
    gradient from tokens, measured AA, rate values not gold-deep, chip solidity,
    tile grid, keyframe paint cost, reduced motion) plus `scripts/smoke_community_feed.cjs`
    sections 20 / 20a-ii / 20c. Changing the default back to `0`, exposing the
    balance to guests, or expanding rates by default must fail regression checks.
- **Upload feedback (2026-10) - the user must SEE it working.** "May processing
  man lang habang nag-a-upload" was the report: a big video queued with no visible
  movement reads as "hindi gumana", so the composer now narrates every stage.
  - **The R2 PUT goes through `XMLHttpRequest`, not `fetch`.** `fetch` cannot
    report request progress; `xhr.upload.onprogress` gives `loaded/total`, so
    `putToR2(item, blob, onProgress)` drives a real percentage. `fetch` stays as
    the fallback when `XMLHttpRequest` is undefined (embedded webviews). The
    SIGNED `Content-Type` is still sent verbatim - the signature covers it.
  - **Every attachment tile shows a busy state.** `renderPreviews` adds
    `.is-busy` + a `.cf-preview-spinner` overlay while `state` is `preparing`
    (canvas re-encode / video decode) or `uploading`, so a tile with no poster
    yet is not a frozen black square. The bottom bar (`entry._fill`) is a REAL
    width % during the PUT (0% while preparing), not an indeterminate stub.
  - **One live status line** (`#cfUploadStatus`, `role=status aria-live=polite`):
    "Processing image/video..." while preparing, "Uploading... NN%" while the
    bytes leave, the error text on failure. `updateUploadStatus()` owns it and
    **bails out while `_posting`**, because `submitPost` writes "Publishing..." to
    the same node - do not let the two fight over it.
  - **The Post button says "Uploading..."** (`updatePostButton`) instead of going
    silently dead, and `addFiles` calls it immediately on queue so the flip is
    instant rather than waiting for the first progress event.
  - `setUploadStatus(message, isError)` now adds **`.is-busy`** for a non-error
    line (its spinner is a CSS `::before`), and `.is-error` on failure.
  - Spinners are `transform: rotate()` only and are switched OFF under
    `prefers-reduced-motion` (the ring stays visible as a static busy cue).
  - Pinned by `scripts/smoke_community_feed.cjs` 6c/6d/6e (an XHR-shaped PUT,
    the byte fraction, the done tile at 100%, a non-2xx failure line, and the
    "Uploading..."/"Processing..." labels) + `TestUploadProgressWiring` in
    `tests/test_community_feed_content.py`.
- **A feed video MUST be allowed by `media-src` (2026-10).** Reported as "na
  upload at na post na ang video kaso walang laman, ayaw ma play" - the upload
  and the post both succeeded, but the player rendered empty. The cause was
  **our own CSP, not Cloudflare and not CORS**: `<video>`/`<audio>` are governed
  by **`media-src`**, which read `'self' data: blob:` with no `https:`, so the
  R2 public origin (`https://media.goodmarketph.live/...`) was refused. Images
  kept working the whole time because **`img-src` already allowed `https:`** -
  that asymmetry is why it looked like a video-specific bug, and why the
  chatroom (ImgBB, images-only) never hit it.
  - Symptom is a **blank, unplayable `<video>`** with only a console error
    ("Refused to load media ... violates media-src"). The upload progress bar
    runs to 100%, the post publishes, and the DB row is correct.
  - `media-src` now reads `'self' data: blob: https:`. Pinned by
    `TestVideoIsPlayableUnderCsp` in `tests/test_community_feed_content.py`,
    which **strips comments** before matching (the explanatory comment names
    `media-src` too) and is mutation-verified: reverting the directive fails
    `test_media_src_allows_remote_https`.
  - Generalisation: **any remote media host added later needs BOTH `img-src`
    (images) and `media-src` (audio/video)** in `main.py`'s `_CSP_DIRECTIVES`.
    `connect-src` already allows `https:` so the presign/PUT never needed a
    change.


## Community Feed theme + shared public profile & album (2026-10)
The feed was the last **dark** island in an otherwise light app, and the
chatroom profile was a stub. Requested (2026-10-20): redesign the feed
theme/background, **share + upgrade** the chatroom profile so the feed uses it
too, and show a member's **posts** and an **album** on their profile. Proposal:
`docs/COMMUNITY_PROFILE_AND_ALBUM_PROPOSAL.md` (status IMPLEMENTED).

- **Theme: the feed is now LIGHT.** `static/css/community-feed.css` moved from
  the navy palette to the app's **Soft Sunrise** tokens (`--cf-bg: #f7f4ef`,
  white cards, `--cf-accent: #e8720a`), and `community.html`'s
  `<meta name="theme-color">` is `#f7f1e8`. Reaching `/community` from the wallet
  no longer flips light -> dark -> light. Keep the AA rules: gold surfaces carry
  DARK ink (`--cf-gold-ink`), and `@keyframes` must not animate `box-shadow` /
  `left` / `top` / `width` / `height` (a test scans every keyframe body).
- **ONE profile page, two entry points.** `templates/community_profile.html` is
  the shared view; `chatroom_profile.html` is now `{% extends
  "community_profile.html" %}` and only swaps the back-link. Routes:
  `/community/u/<username>` (feed, **guests OK**, 404 for an unknown member) and
  `/chatroom/u/<username>` (chatroom, signed in). `static/js/community-profile.js`
  + `static/css/community-profile.css` are the shared bundle/styles.
- **A profile is addressed by USERNAME, never by wallet.** `wallet_for_username`
  resolves the wallet **server-side only** to key the queries; it is never placed
  in HTML or JSON. Tests serialize every response and assert the wallet substring
  is absent.
- **Isolation is preserved - the two modules still do not read each other.** The
  feed owns posts/albums (`/community/api/profile/<u>`, `.../posts`, `.../album`);
  the chatroom owns the message count (`/chatroom/api/profile/<u>`, returns ONLY
  `{username, joined_at, message_count}` and fails CLOSED with 403 when the
  chatroom is OFF). The page hydrates each stat from its owning endpoint. A test
  asserts neither module imports or greps the other's tables.
- **The message stat starts HIDDEN in the markup** and is revealed only when the
  chatroom confirms a count - so the feed never reads the chatroom's feature
  flag, and a disabled chatroom shows no misleading `0`.
- **The album needs NO new table and NO new migration** (`sql/community_feed.sql`
  is unchanged): an album is a **post that carries media** - the caption is the
  album title, the images/videos are its items (the media schema already has
  `post_id`/`position` and no per-file caption). `get_user_album` pages by the
  post id (keyset). The scan is bounded (`size * 3 + 1` posts) because
  caption-only posts are dropped; the cursor advances past the **last post
  scanned**, not the last album kept, or a run of caption-only posts would loop
  forever. The album tab is **lazy** - fetched on first open.
- **Feed cards link to the profile.** The avatar + name in `community-feed.js`
  `buildCard` are one `<a href="/community/u/<username>">` (the username is
  `encodeURIComponent`-ed; no wallet in the URL) with an `aria-label`.
- Tests: `tests/test_community_profile_content.py` (80 tests, no deps - functional
  drives of the REAL `service.py` against a fake Supabase + the REAL bundle via
  `scripts/smoke_community_profile.cjs` (40+ checks: guest guard, keyset paging,
  caption-as-TEXT, the non-capturing linkifier, video poster, lazy album, the
  chatroom-owned count reveal/hide, lightbox, empty + error states) + route/
  template/CSS/isolation wiring). `tests/test_chatroom_profiles_content.py`'s
  privacy-boundary assertion now reads the shared template (the copy moved).
  Run with `python -m pytest tests/test_community_profile_content.py`.

## Community feed â Buy Airtime opens the store in a modal (2026-10)
- **The ask:** tapping Buy Airtime on `/community` must NOT navigate to the
  Reloadly page. The store now opens in an in-page dialog, the same in-place
  behaviour as the wallet's G$ Raffle / Daily Task modals.
- **Community side** (`templates/community.html`, `static/js/community-feed.js`,
  `static/css/community-feed.css`): the nav entry is
  `{ action: 'airtime', href: '/reloadly/?embed=1#topup' }`. `openAirtime()`
  copies the iframe's `data-src` onto `src` **on the first open only**, so the
  store's catalog scripts never load for a member who never taps the button;
  the frame is left mounted on close (reloading would refetch the whole store).
  The guest gate (`if (!WALLET) { requiresLogin(); return; }`) runs **before**
  `openAirtime()`, so a guest gets the connect card and never the store.
  Escape closes it; `body.cf-modal-open` locks the page scroll. The href stays
  real, so middle-click / no-JS still reaches the standalone page.
- **The 0.8 shrink:** the store is a wide desktop layout. `.cf-airtime-body
  iframe` is `width/height: 125%` + `transform: scale(0.8); transform-origin:
  top left`, so the store lays itself out for a viewport 25% wider than the
  visible area and the whole catalogue fits without its own `max-width: 500px`
  media query kicking in. `.cf-airtime-card` needs a **definite** `height`, not
  just `max-height` â with only a max-height the flex body collapsed to the
  iframe's 150px default and the store was unreadable.
- **Store side** (`reloadly/routes.py`, `templates/reloadly.html`):
  `?embed=1` sets `is_embed` (adds `body.gm-embed`, which hides `.top-bar`) and
  `embed_blocked`. **The FV gate must not redirect inside the frame** â a
  top-level redirect would load the whole `/wallet` page into the dialog, so
  embed renders an inline notice whose CTA is `target="_top"`.
- **Escape hatch:** the modal header carries an `Open full page â`
  (`href="/reloadly/#topup"`, `target="_top"`). Some signing paths only work
  top-level (an injected MiniPay/Trust wallet may not expose `window.ethereum`
  to a frame; Privy opens its own popups), so the dialog must not trap the user.
- Tests: `tests/test_community_buy_airtime_modal_content.py` (wiring + CSS +
  the four reloadly render modes) and `scripts/smoke_community_feed.cjs`
  section 13d (drives the real bundle: guest gate, lazy src, scroll lock,
  no-reload on reopen). Run with
  `python -m pytest tests/test_community_buy_airtime_modal_content.py`.

## Community feed — "Claim Free G$" opens the claim sheet IN the feed (2026-10)
The `/community` bottom nav carried a **Feed** destination pointing at
`/community` â the page the user was already on â and it also hard-coded
`is-active`, so the loudest tab in the bar was the one that did nothing. Its
slot now carries **Claim Free G$**, the same action as the wallet page's Claim
chip. Proposal + code: `docs/COMMUNITY_CLAIM_ENTRY_POINT_PROPOSAL.md`.
- **The destination list is the boot object** (`GM_COMMUNITY_BOOT.nav` in
  `templates/community.html`) â the bar is built from it in
  `static/js/community-feed.js` `buildNavItem()`, so a destination is added in
  ONE place. Keep the count at **5**: the bar is `flex: 1 1 0`, and the measured
  360px-phone tap target depends on that count (`WALLET_MOBILE_NAV_PROPOSAL.md`).
- **The claim action OPENS THE SHEET IN THE FEED (framed), not a navigation.**
  It first shipped as a deep link to `/wallet?claim=1`, which navigated the
  member off the feed; the owner asked for the sheet itself to be reachable from
  `/community`. `action: 'claim'` opens `#cfClaimModal`, whose iframe loads
  **`/wallet?embed=claim&claim=1`** - the wallet page in embed mode, where the
  deep link opens the sheet. Proposal:
  `docs/COMMUNITY_CLAIM_MODAL_INLINE_PROPOSAL.md` (supersedes section 2 #2 of
  `COMMUNITY_CLAIM_ENTRY_POINT_PROPOSAL.md`).
- **The sheet is the wallet page's ONE implementation - never a second copy.**
  The claim sheet is a ~3,900-line IIFE in `wallet-main.js` whose transitive
  dependency closure is **47 more top-level declarations** (~1,470 lines:
  EIP-6963 discovery, Privy/WC resolvers, MiniPay fee helpers, the XDC RPC
  repair probe, `openModal`/`closeModal`, the balance readers, `renderFvStatus`,
  the passkey helpers, the PIN flow). Extracting it for the feed would be a
  money-code refactor where one missed declaration breaks a claim. **Frame it
  instead.**
- **`?embed=claim` hides the wallet's own chrome.** `routes.wallet_page` reads
  `embed`, the body gets `gm-embed-claim`, and CSS hides every top-level child
  **except `.modal-overlay`, `#gmCeleOverlay` and `#walletMaintenanceOverlay`**.
  The maintenance overlay is deliberately kept (it out-stacks everything; a
  member must see the notice, not a dead sheet). The agent + chatroom includes
  are **skipped entirely** in embed mode (fixed launchers would sit over the
  sheet, and skipping the include means their bundles never load).
- **The `href` stays the real `/wallet?claim=1`, never the embed URL.** So
  middle-click / no-JS still reach the wallet page with the sheet open. The
  iframe `src` is parked on `data-src` (lazy) - the framed page is the whole
  wallet bundle, so a member who never taps Claim must not pay for it. A second
  open must not refetch (`_claimOpened`).
- **Full-page flows must leave the frame** (`_gmNavigateTop(url)`,
  `wallet-main.js`): Face Verification runs on `goodid.gooddollar.org`, which
  refuses to be framed, so inside the dialog a local navigation leaves a blank
  box. Both FV exits use it and the three `/logout` blockers carry
  `target="_top"`. **Never re-introduce a bare `window.location.href = link`
  in the FV path.**
- **The boot flag is a JS boolean** (`"embed": {{ 'true' if embed_claim else
  'false' }}`) - a quoted `"false"` is truthy and would route every navigation
  to the top window on the normal wallet page. No `zoom` reset is needed: the
  0.7 scale is inside `@media (min-width: 900px)`, and a media query in a frame
  evaluates against the FRAME's viewport.
- **The open is DEFERRED until the first availability response**
  (`static/js/wallet-main.js` claim IIFE: `_claimDeepLinkPending` +
  `_openClaimDeepLink()`, called in `fetchEntitlement`'s `.then` AFTER
  `applyClaimAvailabilityData`/`applyEntitlementData`). `openClaimOrVerify()`
  reads `window._walletNeedsFV`, which those apply functions write - opening
  earlier shows the "Checking networks..." placeholder AND can route an
  unverified user into a claim sheet that cannot pay out. **Never move the call
  above the apply functions.**
- **The param is stripped on read and the open is one-shot**, so a refresh and
  the 60s paused-pool availability poller cannot re-open the sheet. A missing
  `openClaimOrVerify` is `typeof`-guarded (a stale cached bundle must not throw).
- **`is-active` is data-driven now** (`item.active`). Nothing on the feed page
  is "the current page" any more, so no tab may hard-code the class. A future
  page reusing the bar marks its own destination with `active: true`.
- **The chip is the GOLD money chip** (`.cf-nav-item.cf-nav-gold`, `gold: true`)
  and gold carries **DARK ink** - white on `#f5a623` is 2.03:1 and fails AA;
  `--cf-gold-ink` measures 6.0-9.0:1. The rule styles the **icon disc**, not the
  tab: the disc is a fixed 30px circle in both the phone bar and the desktop
  rail, so one rule works in both layouts.
- **Gated for guests** (`guestOk: false`): claiming needs a wallet, so a guest
  gets the shared connect card instead of a dead trip to `/wallet` - the same
  contract as Earn / Buy Airtime / Your Wallet.
- **Do not re-add a Feed tab**, and do not move claim logic into the feed
  bundle. Tests: `tests/test_community_claim_entry_content.py` (49 - nav swap,
  gold ink, the framed dialog (lazy iframe, `data-src`, Escape, scroll lock,
  guest gate before the open), embed mode (route flag, body class, the hide rule
  keeping the three overlays, the boolean boot flag, Jinja renders both ways),
  frame escape (`_gmNavigateTop` wiring + a **node harness** driving the real
  body for embedded / normal / cross-origin), a **node harness over the REAL
  extracted deep-link body**, and a scoped scan proving nothing else in
  production code reads a `claim` param) and `scripts/smoke_community_feed.cjs`
  sections 13/13b/13e. Mutation-verified. Run with
  `python -m pytest tests/test_community_claim_entry_content.py`.



## Optional environment configuration safety (2026-10)
- Runtime numeric settings that use environment values MUST go through `env_utils.get_env_int`, `get_env_float`, or `get_env_decimal`; do not cast `os.getenv()`/`os.environ.get()` directly at module import or inside request paths. Unset, blank, quoted, malformed, and non-finite numeric values fall back to the documented default, keeping unrelated app routes available.
- The shared parser logs the variable name and chosen default only; never log the raw environment value, which may accidentally contain a credential.
- Missing credentials disable only their dependent integration/feature according to its existing guard; they must not prevent Flask from importing or serving unrelated pages.
- Regression coverage: `python -m pytest tests/test_env_utils.py tests/test_session_lifetime_content.py -q`.

## Vercel deploy - `builds` drops `static/` + `templates/`, so the SW route 500s (2026-10)
Reported as "500 internal server error" in the deployment logs for
`/static/service-worker.js` and `/telegram/webhook`.

- **Root cause 1 - the assets were never in the function bundle.** `vercel.json`
  used the legacy form (`"version": 2`, `builds: [{src: main.py, use: @vercel/python}]`,
  `routes`). Vercel's own docs: *"When at least one `builds` item is specified,
  only the outputs of the build processes will be included in the resulting
  deployment as a security precaution."* So **only `main.py` shipped** - `static/`
  and `templates/` did not. `_dynamic_service_worker()` read the worker from disk,
  caught `FileNotFoundError` (an `OSError`) and answered **500**. A 500 makes
  `navigator.serviceWorker.register()` reject, which leaves the already-installed
  worker in control indefinitely - so the endpoint looked permanently broken and
  a stale worker could never be retired.
  - **Fix:** dropped `builds`/`routes`/`version` for `"framework": "flask"` +
    `functions["main.py"].includeFiles = "{static/**,templates/**}"`. **Never
    re-add a `builds` array** - it silently removes runtime-read assets again.
    `.vercelignore` must never list `static/` or `templates/` either.
- **Root cause 2 - a missing file must never be a 500.** `_read_service_worker_source()`
  now tries `app.static_folder` **and** `__file__`-anchored candidates (`__file__`
  is the only anchor that survives a serverless CWD), and on total failure returns
  **200** with `_SW_SELF_UNREGISTER_BODY` (a worker that unregisters itself) while
  logging at ERROR. `_service_worker_response()` is the single place that sets
  `Service-Worker-Allowed: /` + `no-store`; the root `/service-worker.js` shim uses
  it too. **Do not reintroduce `status=500` on this route.**
- **Root cause 3 - the webhook 500 was a *config* error being retried forever.**
  `webhook()` returned `500` when `TELEGRAM_BOT_TOKEN` was unset. Telegram treats
  any non-2xx as a failed delivery and re-queues the update, so one missing env var
  produced an endless retry storm of 500s in the logs. It now answers **200**
  `{ok: false, error: "bot_not_configured"}` with an ERROR log (delivery is
  acknowledged and dropped, cause is still loud). Payload access was hardened too:
  `isinstance(update, dict)` guard, and `message["chat"]["id"]` became
  `(message.get("chat") or {}).get("id")` with an explicit chat-less early return -
  the old direct indexing raised `KeyError` that the broad `except` swallowed,
  dropping the update silently. The 403 shared-secret gate is unchanged.
- **Rule:** a *configuration* problem must never be signalled with a 5xx to a
  webhook provider that retries on 5xx. Acknowledge, log at ERROR, and fix the env.
- Tests: `tests/test_service_worker_and_webhook_500_content.py` (20 tests - AST-extracts
  the REAL bodies of `_read_service_worker_source` / `_dynamic_service_worker` /
  `webhook` and drives them: normal SW serves the build version, missing file ->
  200 + self-unregister + ERROR log, relative `static_folder` still resolves via
  `__file__`, no `status=500` in either view, unset token -> 200 `bot_not_configured`,
  eight odd payload shapes never 500, secret gate still 403, plus vercel.json
  `includeFiles`/no-`builds` and `.vercelignore` keeps the runtime assets).
  Mutation-verified: reverting either fix fails 4 of them.
  **Stub-pollution gotcha:** other content tests install a partial `flask` stub into
  `sys.modules` and never restore it, so `app.test_client()` later dies with
  `ModuleNotFoundError: No module named 'flask.testing'; 'flask' is not a package`.
  The file resolves the real package through a `flask_mod` fixture that snapshots
  and restores `sys.modules` - use that pattern for any new flask-driving test.
  Run with `python -m pytest tests/test_service_worker_and_webhook_500_content.py`.

## Cold start must never touch the network - the real cause of the "500 with no logs" (2026-10)

PR #505 fixed the *deployment config* (legacy `builds` dropping `static/` +
`templates/`) and the SW/webhook 500s. Those fixes are on `main` (2a97e883), but
the site still returned a 500 with **nothing red in the logs**. That second
symptom is the tell: if Flask's logger never fires, the request never reached
Flask - the module never finished **importing**.

### Root cause

Eight blockchain-backed services are instantiated at **module import** time, and
each constructor built a provider and called `is_connected()` - a real JSON-RPC
round-trip - with a 30s timeout:

| module | singleton |
|---|---|
| `trustpilot_task/blockchain.py` | `trustpilot_blockchain_service` |
| `discourse_task/blockchain.py` | `discourse_blockchain_service` |
| `community_stories/blockchain.py` | `community_stories_blockchain` |
| `learn_and_earn/blockchain.py` | `learn_blockchain_service` |
| `learn_and_earn/contract_service.py` | `learn_earn_contract_service` |
| `learn_and_earn/nft_service.py` | `achievement_nft_service` |
| `minigames/blockchain.py` | `minigames_blockchain` |
| `twitter_task/blockchain.py`, `telegram_task/blockchain.py` | same pattern |

plus `main.py`'s own `initialize_blockchain()` (also called at import).

Measured with a slow RPC, `import main` scaled **linearly with latency**: 13.1s
at 2s, 25.1s at 4s, 49.1s at 8s - six blocking calls, one after another. Vercel's
default function limit is 10s (Hobby), so the function was killed **mid-import**,
which is why the 500 had no application log and why the live probe showed a TLS
handshake followed by **0 bytes and a hang** rather than a Flask error page.

### Fix (`chain_rpc.py`)

One shared helper with three rules:

1. **Never touch the network at import time.** `is_connected()` is a network
   call; do it lazily, on first real use.
2. **Always bound the call.** A provider with no `request_kwargs` inherits
   `requests`' "no timeout" default, so a throttled RPC becomes a cold-start
   timeout instead of a fast failure. `make_provider()` always passes a
   `(connect, read)` timeout pair (defaults 3s/8s, env `RPC_CONNECT_TIMEOUT` /
   `RPC_READ_TIMEOUT`).
3. **Probe each chain at most once per process** (`is_reachable`, cached), so
   eight services do not each pay for it.

`RPC_LAZY_CONNECT=0` restores the old eager probe (not recommended).

Result: `import main` is **1.1s with 0 socket connections and 0 DNS lookups**,
flat regardless of RPC latency. A dead RPC now fails in **3.0s** instead of
hanging 30s+. `vercel.json` also sets `maxDuration: 60` explicitly.

Also: `beautifulsoup4` was imported (lazily) by 4 modules but was **never in
`requirements.txt`**, so it was absent from the deployed environment. Added.

### Rules (do not regress)

- **Never call `is_connected()` (or any RPC) at module scope or in a constructor
  that runs at import time.** Build the provider with `chain_rpc.make_provider`;
  let the first real call decide whether the chain is up.
- Never build a bare `Web3.HTTPProvider(url)` in an import-time path.
- Runtime env floats go through `env_utils.get_env_float` - the repo guard
  `tests/test_env_utils.py::test_app_runtime_does_not_cast_environment_values_directly`
  fails on a direct `float(os.getenv(...))` (it caught the first draft of
  `chain_rpc.py`).

### Tests

`tests/test_cold_start_no_network_content.py` (16 tests + 36 subtests, no deps
required for the source-scan half):

- `chain_rpc` helper: timeout always present and < 30s, lazy default on, probe
  cache consulted before probing.
- Every import-time service: still instantiated at module level (guards the
  premise), **no `is_connected()` in `__init__`**, uses `chain_rpc`, no unbounded
  provider. Docstrings are stripped via `_code_without_strings` because the fix
  comments legitimately *name* `is_connected`.
- No module-level `is_connected()` anywhere in the app.
- **End-to-end**: importing the app with a 4s-slow RPC must stay under 10s
  (skips if no interpreter has `web3`+`requests`).
- `vercel.json` valid, `maxDuration` present, `static/`+`templates/` included,
  legacy `builds` gone, `beautifulsoup4` declared.

Mutation-verified: restoring the old `HTTPProvider(...)` + `is_connected()` in
one constructor fails 3 tests.

Run with `python -m pytest tests/test_cold_start_no_network_content.py`.

### Diagnosis method (reusable)

A 500 with **no application log** means the process died before it could log.
Check import cost, not request handlers:

```bash
python -c "import time; t=time.time(); import main; print(time.time()-t)"
```

To reproduce a serverless cold-start failure locally, patch
`requests.Session.request` to sleep then raise, and time `import main`. If the
time tracks the injected latency, the import path is network-bound.

## Community withdrawal vault - a config failure must name the fixable reason (2026-10)

Every vault misconfiguration collapsed into ONE sentence - "Community
withdrawals are not enabled or the vault configuration is not ready." - so an
operator could not tell a disabled feature from a wrong chain ID from an
undeployed contract. `community_feed/withdrawals.py` now raises
`WithdrawalConfigError(message, code=..., hint=...)` and `build_runtime`
classifies all 22 raises; `operator_detail(exc)` returns `message + hint`.

**The split is the design, not a detail:**

- **Admin surfaces** (`/community/api/admin/withdrawal-vault/config` and
  `.../fund/confirm`) return the reason as `detail` plus a stable `code`. The
  dashboard prefers `data.detail || data.error`, so the operator is told which
  env var to change.
- **Member surfaces** (eligibility, authorize, confirm) return the shared
  `MEMBER_CONFIG_ERROR` - deliberately generic. A member cannot act on an
  operator's configuration, and naming an env var or a contract getter leaks
  deployment detail without helping them.
- **Every member-facing failure is still LOGGED** via `operator_detail(exc)`, so
  a member-reported failure is diagnosable from the logs. Losing that log line
  would re-create the original defect for the common case.

**The withdrawal MINIMUM / MAXIMUM / DAILY CAP are NOT env vars.** They are
`immutable` in the vault bytecode (`minimumWithdrawalAmount`,
`maximumWithdrawalAmount`, `dailyOutflowCap`), read from the contract into
`Runtime.min_amount` / `max_amount` / `daily_cap`. No env var, restart or
redeploy of the APP changes them - **only deploying a new vault does**. There is
deliberately no fallback to an env value, so the contract is the single source
of truth. This is a security property: immutable limits bound the damage even if
`COMMUNITY_ISSUER_PRIVATE_KEY` is compromised, which is why the app validates
`onchain["minimum"] <= 0 or maximum < minimum or daily_cap < maximum` and
refuses to run rather than trusting a setting. The Admin Dashboard now renders
the three live immutable limits (base units -> G$ via a **string-based**
formatter; `Number()` would lose precision past 2^53 on an 18-decimal value) so
an operator can see that the CONTRACT governs the minimum.

**`COMMUNITY_WITHDRAWAL_ENABLED` and vault FUNDING are separate switches.**
Funding the vault does not enable withdrawals; the disabled message says so
explicitly, because that was a real source of confusion.

**Rules:** any new `raise WithdrawalConfigError` must pass a `code=` (a test
scans for unclassified raises - it caught one the first pass missed); keep
member copy free of env-var names; keep the operator log line on every
member-facing catch.

Tests: `tests/test_withdrawal_config_error_detail_content.py` (25 tests, no deps
- drives the REAL `build_runtime` for each classified failure, asserts the
admin/member split, the dashboard detail + immutable limits + formatter
precision, and that `.env.example` documents all nine vault variables and warns
that the limits are immutable). 16 of them fail on the unfixed code. All nine
vault env vars are now documented in `.env.example` (they were undocumented,
which is how this got misconfigured). Run with
`python -m pytest tests/test_withdrawal_config_error_detail_content.py`.
