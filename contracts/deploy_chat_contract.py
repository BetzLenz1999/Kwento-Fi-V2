"""
GoodMarketChat Contract Deployment Script for Celo Mainnet.

Deploys the public community chatroom contract. Message bodies live in EVENTS
ONLY (no storage), which measured 86% cheaper than a storage ring buffer.

THE CONTRACT HOLDS NO FUNDS AND HAS NO OWNER, no pause, no upgrade path and no
withdraw function. The deployer pays the one-time deployment gas and then has
exactly one role: it is seeded as the FIRST MODERATOR (it can ban/unban and add
other moderators). It cannot move funds because there are none, and it cannot
delete a message because nothing is stored.

REQUIRED ENV VARS:
    CHAT_DEPLOY_KEY        — Wallet that pays deployment gas

OPTIONAL ENV VARS:
    CHAT_MODERATORS        — Comma-separated extra moderator addresses, seeded
                             alongside the deployer. Up to 25 total.
    CELO_RPC_URL           — default https://forno.celo.org
    CHAIN_ID               — default 42220

AFTER DEPLOYMENT:
    Set env vars:
        CHAT_CONTRACT=<deployed_address>
        CHAT_DEPLOYMENT_BLOCK=<receipt block>
    The block number lets the frontend's eth_getLogs scan start at the right
    place instead of walking from genesis.

Usage:
    CHAT_DEPLOY_KEY=0x... python contracts/deploy_chat_contract.py
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

SOURCE = open(os.path.join(os.path.dirname(__file__), 'GoodMarketChat.sol')).read()

# Measured deploy cost on a local EVM at 202.5 gwei: 817,687 gas (~0.166 CELO).
# Keep a healthy floor so a price spike does not fail the deploy.
MIN_DEPLOY_CELO = 0.05


def compile_contract():
    logger.info("Installing Solidity compiler v0.8.21...")
    install_solc('0.8.21')
    logger.info("Compiling GoodMarketChat contract...")

    compiled = compile_standard({
        "language": "Solidity",
        "sources": {"GoodMarketChat.sol": {"content": SOURCE}},
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "outputSelection": {
                "*": {"*": ["abi", "metadata", "evm.bytecode", "evm.deployedBytecode"]}
            },
        },
    }, solc_version='0.8.21')

    data = compiled["contracts"]["GoodMarketChat.sol"]["GoodMarketChat"]
    return {"abi": data["abi"], "bytecode": data["evm"]["bytecode"]["object"]}


def _moderator_list(deployer: str):
    """Deployer first, then any CHAT_MODERATORS, de-duplicated, checksummed.

    WARNING -- a lost moderator key is UNRECOVERABLE. The contract has no owner
    and no upgrade path, so if EVERY moderator key is lost, nobody can ever ban
    a spammer again and the ban list is frozen permanently. There is no reset.

    Seed AT LEAST TWO moderators (ideally three, on separate devices) so one
    lost key does not end moderation. The deployer wallet is always included,
    so keep its key safe or add others before you stop using it.
    """
    raw = (os.getenv('CHAT_MODERATORS') or '').strip()
    addrs = [deployer]
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            addr = Web3.to_checksum_address(part)
        except Exception:
            logger.warning(f"Skipping invalid moderator address: {part}")
            continue
        if addr not in addrs:
            addrs.append(addr)

    # The contract caps the set at 25 so moderators() can never run out of gas.
    if len(addrs) > 25:
        logger.warning(f"{len(addrs)} moderators given; trimming to the first 25")
        addrs = addrs[:25]

    if len(addrs) < 2:
        logger.warning(
            "Only ONE moderator is being seeded. If that key is lost, the ban "
            "list is frozen forever -- the contract has no owner and no reset. "
            "Set CHAT_MODERATORS with at least one more address you control."
        )
    return addrs


def deploy_contract():
    deploy_key = os.getenv('CHAT_DEPLOY_KEY')
    if not deploy_key:
        logger.error("CHAT_DEPLOY_KEY not set!")
        return None

    w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))
    if not w3.is_connected():
        logger.error("Failed to connect to Celo network")
        return None
    logger.info(f"Connected to Celo Mainnet (Chain ID: {CHAIN_ID})")

    key = deploy_key if deploy_key.startswith('0x') else '0x' + deploy_key
    account = Account.from_key(key)
    logger.info(f"Deploying from address: {account.address}")

    balance = w3.eth.get_balance(account.address)
    celo_human = w3.from_wei(balance, 'ether')
    logger.info(f"CELO balance: {celo_human} CELO")
    if balance < w3.to_wei(MIN_DEPLOY_CELO, 'ether'):
        logger.error(
            f"Insufficient CELO for gas (need ~{MIN_DEPLOY_CELO}, have {celo_human}). "
            "Top up the CHAT_DEPLOY_KEY address."
        )
        return None

    moderators = _moderator_list(account.address)
    logger.info(f"Seeding {len(moderators)} moderator(s):")
    for m in moderators:
        logger.info(f"   {m}")

    compiled = compile_contract()
    contract = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])

    nonce = w3.eth.get_transaction_count(account.address, 'pending')
    gas_price = int(w3.eth.gas_price * 1.2)

    ctor = contract.constructor(moderators)
    try:
        gas_estimate = ctor.estimate_gas({'from': account.address})
    except Exception as e:
        logger.warning(f"estimate_gas failed ({e}); falling back to 1_500_000")
        gas_estimate = 1_500_000
    gas_limit = int(gas_estimate * 1.15)
    logger.info(f"Gas estimate: {gas_estimate} (limit {gas_limit})")
    logger.info(f"Gas price: {gas_price} wei (~{gas_price / 1e9:.2f} gwei)")
    logger.info(f"Max tx cost: ~{gas_limit * gas_price / 1e18:.4f} CELO")

    txn = ctor.build_transaction({
        'chainId': CHAIN_ID,
        'gas': gas_limit,
        'gasPrice': gas_price,
        'nonce': nonce,
    })

    signed = w3.eth.account.sign_transaction(txn, key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hash_hex = tx_hash.hex()
    if not tx_hash_hex.startswith('0x'):
        tx_hash_hex = '0x' + tx_hash_hex

    logger.info(f"Tx hash: {tx_hash_hex}")
    logger.info(f"Explorer: https://celoscan.io/tx/{tx_hash_hex}")
    logger.info("Waiting for confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    if receipt.status != 1:
        logger.error("Deployment transaction failed on-chain.")
        return None

    address = receipt.contractAddress
    logger.info(f"Contract deployed: {address}")
    logger.info(f"   CeloScan: https://celoscan.io/address/{address}")
    logger.info(f"   Gas used: {receipt.gasUsed}")
    logger.info(f"   Cost:     {receipt.gasUsed * gas_price / 1e18:.4f} CELO")
    # These two values ARE the production config. Without the deployment block
    # the read path cannot anchor its log scan and falls back to a bounded
    # lookback, which is slower and can miss the room's very first messages.
    logger.info("")
    logger.info("Set these in the environment to switch the room on-chain:")
    logger.info(f"   CHAT_CONTRACT={address}")
    logger.info(f"   CHAT_DEPLOYMENT_BLOCK={receipt.blockNumber}")
    logger.info("")

    info = {
        "contract_name": "GoodMarketChat",
        "version": "1",
        "contract_address": address,
        "tx_hash": tx_hash_hex,
        "deployer": account.address,
        "owner": None,
        "moderators": moderators,
        "chain_id": CHAIN_ID,
        "network": "Celo Mainnet",
        "block_number": receipt.blockNumber,
        "gas_used": receipt.gasUsed,
        "compiler_version": "v0.8.21+commit.d9974bed",
        "optimization": True,
        "optimization_runs": 200,
        "notes": (
            "GoodMarket public community chatroom. Message bodies live in EVENTS "
            "ONLY (no message storage) -- measured 86% cheaper than a storage ring "
            "buffer. The contract holds no funds and has no owner, pause, upgrade "
            "path or withdraw function. Messages are IMMUTABLE: there is no delete "
            "and no edit. Moderation is a ban list -- a banned wallet cannot post "
            "again, but nothing already posted is removed. The last moderator "
            "cannot be removed, so the ban list can never become unmanageable. "
            "Every poster pays their own gas; there is no relayer or sponsorship."
        ),
        "abi": compiled["abi"],
    }

    out = os.path.join(os.path.dirname(__file__), 'chat_deployment_info.json')
    with open(out, 'w') as f:
        json.dump(info, f, indent=2)
    logger.info(f"Deployment info saved to: {out}")
    logger.info(
        f"Next steps: set CHAT_CONTRACT={address} and "
        f"CHAT_DEPLOYMENT_BLOCK={receipt.blockNumber}."
    )
    return address


if __name__ == "__main__":
    deploy_contract()
