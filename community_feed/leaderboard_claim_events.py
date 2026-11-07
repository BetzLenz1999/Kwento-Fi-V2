"""Reconcile on-chain weekly leaderboard claims into the private ledger (M5).

A backend-only worker: it scans confirmed ``LeaderboardRewardClaimed`` logs for
the configured claim contract, verifies each receipt, block hash and
confirmation count, then confirms the event through a service-role RPC. It also
detects reorgs and reverts affected events. It never runs from a feed request
and fails closed on any RPC or receipt error.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from env_utils import get_env_int

logger = logging.getLogger(__name__)

CELO_CHAIN_ID = 42220
CLAIM_EVENT_SIGNATURE = (
    "LeaderboardRewardClaimed(bytes32,address,uint256,bytes32)")
_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_BYTES32_RE = re.compile(r"^0x[0-9a-f]{64}$")
DEFAULT_MIN_CONFIRMATIONS = 12
MAX_REORG_DEPTH = 200


def _topic_hash() -> str:
    from web3 import Web3
    return Web3.to_hex(Web3.keccak(text=CLAIM_EVENT_SIGNATURE)).lower()


def _hex(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return str(value or "")


def _admin_db():
    from . import weekly_exp
    return weekly_exp._admin_db()


def _enabled() -> bool:
    return (os.getenv("WEEKLY_EXP_CLAIMS_ENABLED", "0") or "0").strip().lower() in {
        "1", "true", "yes", "on", "enabled"}


def reconcile_claim_events(*, web3_instance=None, db=None,
                           now: datetime | None = None) -> dict:
    """Scan and confirm/revert weekly claim events for the claim contract."""
    contract = (os.getenv("WEEKLY_EXP_CLAIM_CONTRACT_ADDRESS", "") or "").strip()
    if not _enabled() or not _ADDRESS_RE.fullmatch(contract.lower()):
        return {"success": False, "status": "not_configured", "confirmed": 0, "reorged": 0}

    from farming import blockchain as shared_chain
    if shared_chain._get_logs_full is None:
        return {"success": False, "status": "scanner_unavailable", "confirmed": 0, "reorged": 0}

    try:
        from . import garden_kpis
        w3 = web3_instance or garden_kpis.garden_sponsors._web3()
        if int(w3.eth.chain_id) != CELO_CHAIN_ID or not w3.eth.get_code(contract):
            raise RuntimeError("Configured claim contract is not on Celo Mainnet")
        latest = int(w3.eth.block_number)
        confirmations = max(1, min(1000, get_env_int(
            "WEEKLY_EXP_CLAIM_CONFIRMATIONS", DEFAULT_MIN_CONFIRMATIONS)))
        end_block = latest - confirmations + 1
        if end_block < 0:
            raise RuntimeError("Insufficient confirmed Celo blocks")

        db = db if db is not None else _admin_db()
        if db is None:
            return {"success": False, "status": "unavailable", "confirmed": 0, "reorged": 0}

        cursor = _read_cursor(db, contract)
        from_block = int(cursor.get("last_scanned_block") or 0) + 1 if cursor else 0
        if from_block > end_block:
            from_block = end_block + 1

        topic0 = _topic_hash()
        logs = []
        if from_block <= end_block:
            logs = shared_chain._get_logs_full(
                {"address": contract, "topics": [topic0]}, from_block, end_block) or []

        confirmed = 0
        pending = 0
        for log in logs:
            decoded = _decode_claim_log(w3, log, contract, latest, confirmations, end_block)
            result = _submit(db, decoded)
            if result.get("success") and result.get("confirmed"):
                confirmed += 1
                _post_claim_receipt(db, decoded)
            else:
                pending += 1

        reorged = _revert_reorged(db, w3, contract, end_block, confirmations)
        block_hash = _hex(w3.eth.get_block(end_block)["hash"]).lower()
        _write_cursor(db, contract, end_block, block_hash)

        return {"success": True, "status": "ready", "chain_id": CELO_CHAIN_ID,
                "contract_address": contract.lower(), "scanned_from_block": from_block,
                "scanned_to_block": end_block, "confirmed": confirmed,
                "pending": pending, "reorged": reorged,
                "updated_at": (now or datetime.now(timezone.utc)).isoformat()}
    except Exception as exc:  # noqa: BLE001 - reconciliation must fail closed
        logger.warning("weekly_exp: claim reconciliation failed: %s", exc)
        return {"success": False, "status": "stale", "confirmed": 0, "reorged": 0,
                "error": "Claim event evidence could not be fully verified."}


def _decode_claim_log(w3, log: dict, contract: str, latest: int,
                      confirmations: int, end_block: int) -> dict:
    topics = log.get("topics") or []
    if len(topics) < 4:
        raise RuntimeError("claim log is missing indexed topics")
    if str(log.get("address") or "").lower() != contract.lower():
        raise RuntimeError("claim log does not match the configured contract")
    period_id = _hex(topics[1]).lower()
    recipient_word = _hex(topics[2]).lower()
    merkle_root = _hex(topics[3]).lower()
    if not _BYTES32_RE.fullmatch(period_id) or not _BYTES32_RE.fullmatch(merkle_root):
        raise RuntimeError("claim log has a malformed bytes32 topic")
    recipient = "0x" + recipient_word[-40:]
    if not _ADDRESS_RE.fullmatch(recipient):
        raise RuntimeError("claim log has an invalid recipient")

    raw_data = log.get("data") or "0x"
    data_hex = _hex(raw_data).lower().removeprefix("0x")
    if len(data_hex) < 64:
        raise RuntimeError("claim log is missing the amount")
    amount = int(data_hex[:64], 16)

    block_number = int(log.get("blockNumber"), 16) if isinstance(log.get("blockNumber"), str) else int(log.get("blockNumber"))
    log_index = int(log.get("logIndex"), 16) if isinstance(log.get("logIndex"), str) else int(log.get("logIndex"))
    tx_hash = _hex(log.get("transactionHash")).lower()
    block_hash = _hex(log.get("blockHash")).lower()
    if not _BYTES32_RE.fullmatch(tx_hash) or not _BYTES32_RE.fullmatch(block_hash):
        raise RuntimeError("claim log has a malformed transaction or block hash")

    receipt = w3.eth.get_transaction_receipt(tx_hash)
    if int(receipt.get("status", 0)) != 1:
        raise RuntimeError("claim transaction reverted")
    if int(receipt.get("blockNumber")) != block_number:
        raise RuntimeError("claim receipt block mismatch")
    if _hex(receipt.get("blockHash")).lower() != block_hash:
        raise RuntimeError("claim receipt block hash mismatch")
    receipt_match = any(
        str(receipt_log.get("address") or "").lower() == contract.lower()
        and int(receipt_log.get("logIndex")) == log_index
        and _hex(receipt_log.get("transactionHash")).lower() == tx_hash
        for receipt_log in (receipt.get("logs") or []))
    if not receipt_match:
        raise RuntimeError("claim log is not present in its receipt")
    if latest - block_number + 1 < confirmations or block_number > end_block:
        raise RuntimeError("claim log is not yet confirmed")

    return {"chain_id": CELO_CHAIN_ID, "contract_address": contract.lower(),
            "transaction_hash": tx_hash, "log_index": log_index,
            "period_id": period_id, "recipient": recipient, "amount_units": str(amount),
            "block_number": block_number, "block_hash": block_hash,
            "confirmations": latest - block_number + 1, "receipt_status": 1,
            "merkle_root": merkle_root}


def _submit(db, decoded: dict) -> dict:
    result = db.rpc("weekly_exp_record_claim_event", {
        "p_chain_id": decoded["chain_id"],
        "p_contract_address": decoded["contract_address"],
        "p_transaction_hash": decoded["transaction_hash"],
        "p_log_index": decoded["log_index"],
        "p_period_id": decoded["period_id"],
        "p_recipient": decoded["recipient"],
        "p_amount_units": decoded["amount_units"],
        "p_block_number": decoded["block_number"],
        "p_block_hash": decoded["block_hash"],
        "p_confirmations": decoded["confirmations"],
        "p_receipt_status": decoded["receipt_status"],
        "p_merkle_root": decoded["merkle_root"],
    }).execute()
    data = getattr(result, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else {"success": False}


def _revert_reorged(db, w3, contract: str, end_block: int, confirmations: int) -> int:
    """Revert confirmed events whose stored block hash no longer matches chain."""
    try:
        rows = db.table("leaderboard_claim_events").select(
            "transaction_hash,log_index,block_number,block_hash,event_status"
        ).eq("contract_address", contract.lower()).eq("event_status", "confirmed").limit(
            MAX_REORG_DEPTH).execute()
    except Exception:  # noqa: BLE001
        return 0
    data = getattr(rows, "data", None)
    if not isinstance(data, list):
        return 0
    reorged = 0
    for row in data:
        try:
            block_number = int(row.get("block_number"))
            if end_block - block_number > MAX_REORG_DEPTH:
                continue
            chain_hash = _hex(w3.eth.get_block(block_number)["hash"]).lower()
            if chain_hash == str(row.get("block_hash") or "").lower():
                continue
            db.rpc("weekly_exp_reorg_claim_event", {
                "p_chain_id": CELO_CHAIN_ID,
                "p_transaction_hash": str(row.get("transaction_hash")),
                "p_log_index": int(row.get("log_index")),
            }).execute()
            reorged += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("weekly_exp: reorg revert skipped: %s", type(exc).__name__)
    return reorged


def _post_claim_receipt(db, decoded: dict) -> None:
    """Post one idempotent public receipt comment for a confirmed claim.

    The receipt intentionally omits the wallet and the amount: the weekly post is
    public, and the proposal keeps claim amounts and entitlements private. It
    only confirms that a verified on-chain claim happened for the period.
    """
    week = _period_week(db, decoded.get("period_id"))
    label = f" for the week of {week}" if week else ""
    body = f"A weekly reward claim{label} was verified on-chain."
    try:
        db.rpc("weekly_exp_post_claim_receipt", {
            "p_chain_id": decoded["chain_id"],
            "p_transaction_hash": decoded["transaction_hash"],
            "p_log_index": decoded["log_index"],
            "p_body": body,
        }).execute()
    except Exception as exc:  # noqa: BLE001 - receipt failure must not block reconciliation
        logger.warning("weekly_exp: claim receipt comment skipped: %s", type(exc).__name__)


def _period_week(db, period_id: str | None) -> str | None:
    """Best-effort week label from a stored period id, without a DB round-trip."""
    try:
        rows = db.table("leaderboard_claim_periods").select("week_start").eq(
            "period_id", period_id).limit(1).execute()
    except Exception:  # noqa: BLE001
        return None
    data = getattr(rows, "data", None)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return str(data[0].get("week_start") or "") or None
    return None


def _read_cursor(db, contract: str) -> dict:
    try:
        rows = db.table("leaderboard_claim_scan_cursors").select(
            "last_scanned_block,last_scanned_block_hash"
        ).eq("chain_id", CELO_CHAIN_ID).eq("contract_address", contract.lower()).limit(1).execute()
    except Exception:  # noqa: BLE001
        return {}
    data = getattr(rows, "data", None)
    if isinstance(data, list) and data:
        return data[0] if isinstance(data[0], dict) else {}
    return {}


def _write_cursor(db, contract: str, block_number: int, block_hash: str) -> None:
    try:
        db.table("leaderboard_claim_scan_cursors").upsert({
            "chain_id": CELO_CHAIN_ID, "contract_address": contract.lower(),
            "last_scanned_block": block_number, "last_scanned_block_hash": block_hash,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="chain_id,contract_address").execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("weekly_exp: claim cursor update skipped: %s", type(exc).__name__)
