#!/usr/bin/env python3
"""Finalize a full GoodMarket G$ Raffle round.

This script is the automation/keeper side of the raffle. The smart contract
cannot wake itself up when the last participant joins; a trusted/verifiable
randomness provider must submit the seed by calling `drawWinners(bytes32)`.

Round economics (participants, entry fee, prize tiers) are READ FROM THE
CONTRACT, never hardcoded, so this script keeps working across redeploys.
Current deployment: 10 participants per round, 500 G$ entry, 4 winners paid
40/30/20/10.

Run from cron, a worker, or a keeper service every few minutes:

    export CELO_RPC_URL=https://forno.celo.org
    export GOODMARKET_RAFFLE_CONTRACT_ADDRESS=0x...
    export GOODMARKET_RAFFLE_RANDOMNESS_KEY=0x...
    uv run python contracts/draw_goodmarket_raffle.py

The wallet that owns GOODMARKET_RAFFLE_RANDOMNESS_KEY must match the
`randomnessProvider` address configured when GoodMarketRaffle was deployed.

NOTE: whoever holds that key chooses the seed, and therefore can influence the
outcome. Use a non-incentivized key, or migrate to a VRF/commit-reveal source.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any

from eth_account import Account
from web3 import Web3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CELO_RPC_URL = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CHAIN_ID = int(os.getenv("CHAIN_ID", "42220"))
RAFFLE_ADDRESS = (os.getenv("GOODMARKET_RAFFLE_CONTRACT_ADDRESS") or "").strip()
RANDOMNESS_KEY = (os.getenv("GOODMARKET_RAFFLE_RANDOMNESS_KEY") or "").strip()

ROUND_STATUS_OPEN = 0
ROUND_STATUS_DRAWING = 1

# The participant count is NOT hardcoded: it is read from the deployed contract
# so the keeper keeps working after a redeploy with different round economics.
FALLBACK_MAX_PARTICIPANTS = 10

RAFFLE_ABI: list[dict[str, Any]] = [
    {
        "inputs": [],
        "name": "currentRoundId",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "MAX_PARTICIPANTS",
        "outputs": [{"internalType": "uint16", "name": "", "type": "uint16"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getPrizeTiers",
        "outputs": [{"internalType": "uint256[]", "name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "roundId", "type": "uint256"}],
        "name": "getRound",
        "outputs": [
            {"internalType": "enum GoodMarketRaffle.RoundStatus", "name": "status", "type": "uint8"},
            {"internalType": "uint256", "name": "participantCount", "type": "uint256"},
            {"internalType": "uint256", "name": "winnerCount", "type": "uint256"},
            {"internalType": "bytes32", "name": "randomnessSeed", "type": "bytes32"},
            {"internalType": "uint256", "name": "openedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "completedAt", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "bytes32", "name": "seed", "type": "bytes32"}],
        "name": "drawWinners",
        "outputs": [{"internalType": "address[]", "name": "winners", "type": "address[]"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def require_env() -> None:
    if not RAFFLE_ADDRESS:
        raise RuntimeError("Missing GOODMARKET_RAFFLE_CONTRACT_ADDRESS")
    if not RANDOMNESS_KEY:
        raise RuntimeError("Missing GOODMARKET_RAFFLE_RANDOMNESS_KEY")


def main() -> None:
    require_env()

    key = RANDOMNESS_KEY if RANDOMNESS_KEY.startswith("0x") else f"0x{RANDOMNESS_KEY}"
    account = Account.from_key(key)

    w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise RuntimeError(f"Failed to connect to CELO_RPC_URL={CELO_RPC_URL}")

    raffle = w3.eth.contract(
        address=Web3.to_checksum_address(RAFFLE_ADDRESS),
        abi=RAFFLE_ABI,
    )
    round_id = raffle.functions.currentRoundId().call()
    try:
        max_participants = int(raffle.functions.MAX_PARTICIPANTS().call())
    except Exception as exc:
        logger.warning("MAX_PARTICIPANTS() read failed (%s); using %s", exc, FALLBACK_MAX_PARTICIPANTS)
        max_participants = FALLBACK_MAX_PARTICIPANTS
    status, participant_count, winner_count, _seed, _opened_at, _completed_at = raffle.functions.getRound(round_id).call()

    try:
        tiers = [int(t) for t in raffle.functions.getPrizeTiers().call()]
        prizes = ", ".join(f"{t / 1e18:.0f} G$" for t in tiers)
    except Exception:
        prizes = "n/a"

    logger.info(
        "Round #%s status=%s participants=%s/%s winners=%s prizes=[%s]",
        round_id,
        status,
        participant_count,
        max_participants,
        winner_count,
        prizes,
    )

    if status != ROUND_STATUS_DRAWING or participant_count < max_participants:
        logger.info("No draw needed yet. The script exits without sending a transaction.")
        return

    seed = "0x" + secrets.token_bytes(32).hex()
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    gas_price = int(w3.eth.gas_price * 1.15)
    draw_call = raffle.functions.drawWinners(seed)
    tx = draw_call.build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "chainId": CHAIN_ID,
            "gasPrice": gas_price,
        }
    )

    try:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    except Exception as exc:
        logger.warning("estimate_gas failed (%s); falling back to 650000", exc)
        tx["gas"] = 650_000

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hash_hex = tx_hash.hex()
    if not tx_hash_hex.startswith("0x"):
        tx_hash_hex = "0x" + tx_hash_hex

    logger.info("Submitted drawWinners for round #%s: %s", round_id, tx_hash_hex)
    logger.info("Explorer: https://celoscan.io/tx/%s", tx_hash_hex)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)

    if receipt.status != 1:
        raise RuntimeError(f"drawWinners transaction reverted: {tx_hash_hex}")

    logger.info("Winners finalized for round #%s. Gas used: %s", round_id, receipt.gasUsed)


if __name__ == "__main__":
    main()
