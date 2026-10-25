# Community Withdrawal Vault — local tests and gated deployment

This folder contains a draft Solidity vault, an isolated Hardhat test project, and a gated Celo mainnet deployment script. **No contract has been deployed.** Hardhat's in-memory chain is for local tests only; the Celo network requires explicit RPC and signer configuration.

## Run local tests

```bash
cd contracts/community_withdrawal_vault
pnpm install
pnpm test
```

These commands compile and deploy only to Hardhat's local in-memory chain. They do not connect to Celo or any public network. `MockGovernance` is a local call harness, **not a production multisig**.

## Community ID

The owner-approved ID text is `betzlenz@goodmarketph.v2`. `deployment-config.cjs` derives the exact `bytes32` value using `ethers.id("betzlenz@goodmarketph.v2")`; the tests and deployment script import that same value. It is a public identifier, not a secret. The value is constructor-immutable, so verify it before deployment.

## Celo mainnet deployment

The deployment script is `deploy.cjs`; it only targets Celo mainnet (chain ID `42220`). It refuses the local Hardhat network, checks the live chain ID, verifies token and governance contract code, compares the GoodDollar token's live decimals against the independently approved expected decimals, validates issuer/governance separation and integer limit inputs, estimates gas, prints nonsecret configuration for review, and requires the explicit `CONFIRM_CELO_MAINNET_DEPLOYMENT=YES` gate before broadcasting. After two confirmations it reads the immutable getters back and checks them against the intended configuration.

Supply these settings through a protected environment/secret manager—not in chat, source files, command-line arguments, or committed `.env` files:

- `CELO_RPC_URL` — HTTPS Celo mainnet RPC URL.
- `DEPLOYER_PRIVATE_KEY` — deployer EOA key; use an account separate from the issuer and with only the native gas funds needed. The script never prints the key. Never send it to Manus or commit it.
- `GOODDOLLAR_TOKEN_ADDRESS` — independently verified production token address.
- `GOODDOLLAR_EXPECTED_DECIMALS` — independently verified token decimals; the script compares this with the live token contract.
- `COMMUNITY_ISSUER_SIGNER_ADDRESS` — issuer address that signs backend withdrawal authorizations.
- `COMMUNITY_GOVERNANCE_MULTISIG_ADDRESS` — already deployed, independently reviewed multisig contract address.
- `COMMUNITY_MIN_WITHDRAWAL_BASE_UNITS`, `COMMUNITY_MAX_WITHDRAWAL_BASE_UNITS`, `COMMUNITY_DAILY_CAP_BASE_UNITS` — approved positive integer amounts in token base units; no production defaults are supplied.

Only after verifying every value and the multisig threshold should the authorized deployer environment run:

```bash
pnpm deploy:celo
```

The script will stop without submitting a transaction unless the deployment environment also explicitly sets `CONFIRM_CELO_MAINNET_DEPLOYMENT=YES`. I did not set this confirmation or run the deployment. Do not retry blindly if a transaction hash has been submitted but its result is uncertain.

## Contract behavior and production gates

- A member calls `withdraw` directly from the wallet that receives G$ and pays transaction gas; there is no relayer or gas sponsor.
- The contract checks a Community EIP-712 authorization bound to chain ID, vault address, exact recipient/amount, immutable project ID, member nonce, unique withdrawal ID, and short deadline. `msg.sender` must equal the recipient.
- The issuer signer and governance addresses are fixed and must be different. Governance must be a reviewed deployed multisig contract and can only pause/unpause; it has no issuer-rotation, token-sweep, or arbitrary-call function. Different addresses do not by themselves prove independent operators or key custody.
- The issuer signer is immutable. If its signing key is compromised, forged authorizations may be possible up to on-chain limits; the contract cannot independently verify the off-chain Community ledger. Governance can pause, but changing the issuer requires a separately reviewed migration.
- Anyone may fund by direct ERC-20 transfer. Funding creates pooled liquidity only; it creates no earnings or depositor claim.
- `dailyOutflowCap` is a UTC calendar-day cap, not a rolling 24-hour cap. Near midnight, close to two daily caps could be paid within a short interval. Approve this behavior and conservative cap values or require a rolling-window change before production.
- The tests use an 18-decimal mock token. Confirm actual GoodDollar address/semantics, chain, project ID, decimals, issuer, multisig threshold, and every limit. The script does not verify multisig owners/threshold, backend ledger integration, or production-token transfer behavior.

This is a contract draft, not a complete withdrawal product. Backend ledger validation/signing, application receipt reconciliation, independent security review, and explicit owner approval remain production requirements. Do not deploy or fund until they are complete.
