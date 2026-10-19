"""
GoodMarketRaffle Contract Deployment Script for Celo Mainnet.

Deploys the round-based G$ raffle with tiered prizes:
  - 10 participants per round
  - 500 G$ entry fee
  - 4 winners, paid 40/30/20/10 (2,000 / 1,500 / 1,000 / 500 G$)

The constructor enforces `sum(tiers) == ENTRY_FEE * MAX_PARTICIPANTS`, so a
mis-configured deployment reverts instead of shipping a contract that cannot pay
its winners. The pool is a pure pass-through (100% payout), so the contract
needs NO external funding and has no house edge.

There is no owner, no pause and no admin withdrawal: prizes are pull-based via
withdrawReward(), and a participant can refundEntry() only while the round is
still Open (before the draw commits).

REQUIRED ENV VARS:
    GOODMARKET_RAFFLE_DEPLOY_KEY       — Wallet that pays deployment gas (no ongoing role)

OPTIONAL ENV VARS:
    CELO_RPC_URL                       — default https://forno.celo.org
    CHAIN_ID                           — default 42220
    GOODDOLLAR_CONTRACT_ADDRESS        — default G$ on Celo
    GOODMARKET_RAFFLE_RANDOMNESS_KEY   — if set, its address becomes the
                                         randomnessProvider (the wallet allowed
                                         to submit the draw seed)
    GOODMARKET_RAFFLE_RANDOMNESS_PROVIDER — explicit provider address, used when
                                         the key above is not set

AFTER DEPLOYMENT:
    Set env vars:
        GOODMARKET_RAFFLE_CONTRACT_ADDRESS=<deployed_address>
        GOODMARKET_RAFFLE_DEPLOYMENT_BLOCK=<receipt block>
    Keep GOODMARKET_RAFFLE_RANDOMNESS_KEY matching the deployed randomnessProvider.

Usage:
    uv run python contracts/deploy_goodmarket_raffle.py
"""

import os
import json
import logging

from web3 import Web3
from eth_account import Account
from solcx import compile_standard, install_solc

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

CELO_RPC_URL = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
CHAIN_ID = int(os.getenv('CHAIN_ID', 42220))
GOODDOLLAR_CONTRACT = os.getenv(
    'GOODDOLLAR_CONTRACT_ADDRESS',
    '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A',
)

ENTRY_FEE_WEI = 500 * 10**18
MAX_PARTICIPANTS = 10
# 40 / 30 / 20 / 10 of a 5,000 G$ pool.
PRIZE_TIERS_WEI = [
    2000 * 10**18,
    1500 * 10**18,
    1000 * 10**18,
    500 * 10**18,
]

SOURCE = open(os.path.join(os.path.dirname(__file__), 'GoodMarketRaffle.sol')).read()


def _resolve_randomness_provider() -> str | None:
    """Provider address from the keeper key, else an explicit env override."""
    raw = (os.getenv('GOODMARKET_RAFFLE_RANDOMNESS_KEY') or '').strip()
    if raw:
        key = raw if raw.startswith('0x') else '0x' + raw
        try:
            return Account.from_key(key).address
        except Exception as exc:
            logger.error(f"GOODMARKET_RAFFLE_RANDOMNESS_KEY is not a valid key: {exc}")
            return None
    explicit = (os.getenv('GOODMARKET_RAFFLE_RANDOMNESS_PROVIDER') or '').strip()
    return explicit or None


def compile_contract():
    logger.info("Installing Solidity compiler v0.8.21...")
    install_solc('0.8.21')
    logger.info("Compiling GoodMarketRaffle contract...")

    compiled = compile_standard({
        "language": "Solidity",
        "sources": {
            "GoodMarketRaffle.sol": {"content": SOURCE}
        },
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "outputSelection": {
                "*": {
                    "*": ["abi", "metadata", "evm.bytecode", "evm.deployedBytecode"]
                }
            }
        }
    }, solc_version='0.8.21')

    contract_data = compiled["contracts"]["GoodMarketRaffle.sol"]["GoodMarketRaffle"]
    return {
        "abi": contract_data["abi"],
        "bytecode": contract_data["evm"]["bytecode"]["object"]
    }


def deploy_contract():
    deploy_key = os.getenv('GOODMARKET_RAFFLE_DEPLOY_KEY')

    if not deploy_key:
        logger.error("GOODMARKET_RAFFLE_DEPLOY_KEY not set!")
        return None

    randomness_provider = _resolve_randomness_provider()
    if not randomness_provider:
        logger.error(
            "No randomness provider. Set GOODMARKET_RAFFLE_RANDOMNESS_KEY (its "
            "address is used) or GOODMARKET_RAFFLE_RANDOMNESS_PROVIDER."
        )
        return None

    w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))
    if not w3.is_connected():
        logger.error("Failed to connect to Celo network")
        return None

    logger.info(f"Connected to Celo Mainnet (Chain ID: {CHAIN_ID})")

    key = deploy_key if deploy_key.startswith('0x') else '0x' + deploy_key
    account = Account.from_key(key)
    logger.info(f"Deploying from GOODMARKET_RAFFLE_DEPLOY_KEY address: {account.address}")
    logger.info(f"  G$ token:            {GOODDOLLAR_CONTRACT}")
    logger.info(f"  Randomness provider: {randomness_provider}")
    logger.info(f"  Entry fee:           {ENTRY_FEE_WEI / 1e18:.0f} G$")
    logger.info(f"  Participants/round:  {MAX_PARTICIPANTS}")
    logger.info(f"  Prize tiers:         {[int(t / 1e18) for t in PRIZE_TIERS_WEI]} G$")

    celo_balance = w3.eth.get_balance(account.address)
    celo_human = w3.from_wei(celo_balance, 'ether')
    logger.info(f"CELO balance: {celo_human} CELO")

    if celo_balance < w3.to_wei(0.05, 'ether'):
        logger.error(
            f"Insufficient CELO for gas (need ~0.05, have {celo_human}). "
            "Top up the GOODMARKET_RAFFLE_DEPLOY_KEY address."
        )
        return None

    compiled = compile_contract()

    contract = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])

    nonce = w3.eth.get_transaction_count(account.address, 'pending')
    gas_price = int(w3.eth.gas_price * 1.2)

    ctor_call = contract.constructor(
        Web3.to_checksum_address(GOODDOLLAR_CONTRACT),
        Web3.to_checksum_address(randomness_provider),
        PRIZE_TIERS_WEI,
    )
    try:
        gas_estimate = ctor_call.estimate_gas({'from': account.address})
    except Exception as e:
        logger.warning(f"estimate_gas failed ({e}); falling back to 4_000_000")
        gas_estimate = 4_000_000
    gas_limit = int(gas_estimate * 1.15)
    logger.info(f"Gas estimate: {gas_estimate} (using limit: {gas_limit})")
    logger.info(f"Gas price:    {gas_price} wei (~{gas_price / 1e9:.2f} gwei)")
    logger.info(f"Max tx cost:  {gas_limit * gas_price} wei (~{gas_limit * gas_price / 1e18:.4f} CELO)")

    constructor_txn = ctor_call.build_transaction({
        'chainId':  CHAIN_ID,
        'gas':      gas_limit,
        'gasPrice': gas_price,
        'nonce':    nonce,
    })

    signed_txn = w3.eth.account.sign_transaction(constructor_txn, key)
    tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)
    tx_hash_hex = tx_hash.hex()
    if not tx_hash_hex.startswith('0x'):
        tx_hash_hex = '0x' + tx_hash_hex

    logger.info(f"Tx hash: {tx_hash_hex}")
    logger.info(f"Explorer: https://celoscan.io/tx/{tx_hash_hex}")
    logger.info("Waiting for confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)

    if receipt.status == 1:
        contract_address = receipt.contractAddress
        logger.info(f"Contract deployed: {contract_address}")
        logger.info(f"   CeloScan: https://celoscan.io/address/{contract_address}")
        logger.info(f"   Gas used: {receipt.gasUsed}")

        deployment_info = {
            "contract_name": "GoodMarketRaffle",
            "version": "2",
            "contract_address": contract_address,
            "tx_hash": tx_hash_hex,
            "deployer": account.address,
            "owner": None,
            "gooddollar_token": GOODDOLLAR_CONTRACT,
            "randomness_provider": randomness_provider,
            "entry_fee": str(ENTRY_FEE_WEI),
            "max_participants": MAX_PARTICIPANTS,
            "winner_count": len(PRIZE_TIERS_WEI),
            "prize_tiers": [str(t) for t in PRIZE_TIERS_WEI],
            "prize_pool": str(sum(PRIZE_TIERS_WEI)),
            "chain_id": CHAIN_ID,
            "network": "Celo Mainnet",
            "block_number": receipt.blockNumber,
            "gas_used": receipt.gasUsed,
            "compiler_version": "v0.8.21+commit.d9974bed",
            "optimization": True,
            "optimization_runs": 200,
            "supersedes": os.getenv('GOODMARKET_RAFFLE_SUPERSEDES', ''),
            "notes": (
                "Round-based G$ raffle with tiered prizes. 10 participants per round at "
                "500 G$ entry, 4 winners paid 40/30/20/10 (2000/1500/1000/500 G$). The "
                "constructor requires the prize tiers to sum exactly to the pool, so the "
                "contract is always solvent with no external funding and no house edge. "
                "No owner, no pause, no admin withdrawal: prizes are pull-based, and "
                "refundEntry() is available only while a round is Open. Winner selection "
                "uses a partial Fisher-Yates shuffle of the slot indexes, so no join order "
                "is favoured. The randomness provider chooses the seed and can therefore "
                "influence the outcome."
            ),
            "abi": compiled["abi"]
        }

        out = os.path.join(os.path.dirname(__file__), 'raffle_deployment_info.json')
        with open(out, 'w') as f:
            json.dump(deployment_info, f, indent=2)

        logger.info(f"Deployment info saved to: {out}")
        logger.info(
            f"✅ Next steps: set GOODMARKET_RAFFLE_CONTRACT_ADDRESS={contract_address} "
            f"and GOODMARKET_RAFFLE_DEPLOYMENT_BLOCK={receipt.blockNumber}."
        )
    else:
        logger.error("❌ Deployment transaction failed on-chain.")

    return contract_address


if __name__ == "__main__":
    deploy_contract()
