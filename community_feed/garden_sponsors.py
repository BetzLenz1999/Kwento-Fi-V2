"""Verified Garden sponsor contributions and pinned Community acknowledgements.

Garden sponsorship is signed directly by the member's wallet. This module only
accepts an acknowledgement after verifying the mined Celo transaction, the
Garden ``SponsorPoolFunded`` event, and the matching G$ token transfer. It
reuses the existing Community Vault donation/post tables so no new migration is
needed; the donation row is marked with ``garden-sponsor-v1`` and the Garden
contract address.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import post_support
from . import vault_donations

logger = logging.getLogger(__name__)

CELO_CHAIN_ID = 42220
TOKEN_DECIMALS = 18
_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CONSENT_VERSION = "garden-sponsor-v1"
GARDEN_POST_TYPE = "system_vault_donation"


def _rows(result) -> list[dict]:
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    if isinstance(data, dict):
        return [data]
    return [row for row in (data or []) if isinstance(row, dict)]


def _hex(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        value = "0x" + bytes(value).hex()
    text = str(value or "")
    return text if text.startswith("0x") else "0x" + text


def _topic_address(value) -> str:
    return "0x" + _hex(value)[-40:]


def _garden_address() -> str:
    address = (os.getenv("COMMUNITY_GARDEN_CONTRACT_ADDRESS") or "").strip()
    if not _ADDRESS_RE.fullmatch(address):
        raise ValueError("Garden contract address is not configured")
    return address


def _token_address() -> str:
    """Resolve the G$ token, treating a blank setting as "use the default".

    Precedence is preserved (``CELO_GD_TOKEN_CONTRACT`` first) but a blank
    variable no longer shadows the fallback.
    """
    from env_utils import get_env_str

    address = (get_env_str("CELO_GD_TOKEN_CONTRACT")
               or get_env_str("GOODDOLLAR_TOKEN_ADDRESS")
               or post_support.GD_TOKEN_ADDRESS)
    if not _ADDRESS_RE.fullmatch(address):
        raise ValueError("G$ token address is not configured")
    return address


def _web3():
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(post_support.CELO_RPC_URL, request_kwargs={"timeout": 15}))
    if int(w3.eth.chain_id) != CELO_CHAIN_ID:
        raise RuntimeError("The configured verification RPC is not Celo Mainnet")
    garden = _garden_address()
    if not w3.eth.get_code(Web3.to_checksum_address(garden)):
        raise RuntimeError("The configured Garden contract is not deployed")
    return w3


def _verified_transaction(wallet: str, tx_hash: str) -> dict:
    if not _TX_RE.fullmatch(tx_hash or ""):
        return {"pending": False, "ok": False, "error": "Invalid Celo transaction hash."}
    try:
        from web3 import Web3

        w3 = _web3()
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if not receipt:
            return {"pending": True, "error": "Transaction is still confirming on Celo."}
        if int(receipt.get("status") or 0) != 1:
            return {"pending": False, "ok": False, "error": "The wallet transaction failed on Celo."}
        tx = w3.eth.get_transaction(tx_hash)
        garden = Web3.to_checksum_address(_garden_address())
        token = Web3.to_checksum_address(_token_address())
        sender = wallet.lower()
        if str(tx.get("to") or "").lower() != garden.lower() or str(tx.get("from") or "").lower() != sender:
            return {"pending": False, "ok": False, "error": "This transaction was not sent from your wallet to the Garden contract."}

        tx_input = _hex(tx.get("input") or tx.get("data") or "0x")
        selector = Web3.keccak(text="fundSponsorPool(uint256)")[:4].hex()
        if not tx_input.lower().startswith("0x" + selector) or len(tx_input) != 2 + 8 + 64:
            return {"pending": False, "ok": False, "error": "This is not a Garden sponsor contribution transaction."}
        amount = int(tx_input[10:], 16)
        if amount <= 0:
            return {"pending": False, "ok": False, "error": "The Garden contribution amount is invalid."}

        event_topic = "0x" + bytes(Web3.keccak(text="SponsorPoolFunded(address,uint256,uint256)")).hex()
        matching_events = []
        transfer_topic = "0x" + bytes(Web3.keccak(text="Transfer(address,address,uint256)")).hex()
        matching_transfers = []
        for log in receipt.get("logs") or []:
            log_address = str(log.get("address") or "").lower()
            topics = log.get("topics") or []
            if log_address == garden.lower() and len(topics) == 2 and _hex(topics[0]).lower() == event_topic.lower():
                sponsor = _topic_address(topics[1]).lower()
                event_data = _hex(log.get("data") or "0x")[2:]
                if len(event_data) >= 128 and sponsor == sender and int(event_data[:64], 16) == amount:
                    matching_events.append(amount)
            if log_address == token.lower() and len(topics) == 3 and _hex(topics[0]).lower() == transfer_topic.lower():
                from_address = _topic_address(topics[1]).lower()
                to_address = _topic_address(topics[2]).lower()
                transferred = int(_hex(log.get("data") or "0x"), 16)
                if from_address == sender and to_address == garden.lower() and transferred == amount:
                    matching_transfers.append(amount)
        if len(matching_events) != 1:
            return {"pending": False, "ok": False, "error": "No unique matching Garden sponsor event was found."}
        if len(matching_transfers) != 1:
            return {"pending": False, "ok": False, "error": "No matching G$ transfer to the Garden contract was found."}
        amount_decimal = Decimal(amount) / (Decimal(10) ** TOKEN_DECIMALS)
        return {
            "pending": False,
            "ok": True,
            "amount_base_units": amount,
            "amount": amount_decimal,
            "amount_text": format(amount_decimal.normalize(), "f"),
            "block_number": int(receipt.get("blockNumber") or 0),
            "garden_address": garden,
            "token_address": token,
        }
    except Exception as exc:  # noqa: BLE001
        logger.info("Garden sponsor receipt not available for %s: %s", tx_hash, exc)
        return {"pending": True, "error": "Transaction is still confirming. Keep this page open or retry verification."}


def _display_name(wallet: str) -> str:
    db = vault_donations._admin_db()
    try:
        rows = _rows(db.table("user_data").select("community_display_name").ilike("wallet_address", wallet).limit(1).execute())
        if rows:
            name = str(rows[0].get("community_display_name") or "").strip()
            if name:
                return name[:80]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Garden sponsor display-name lookup failed: %s", exc)
    try:
        from . import service as svc
        username = str(svc.get_username(wallet) or "").strip().lstrip("@")
        if username:
            return username[:80]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Garden sponsor username lookup failed: %s", exc)
    return "Community member"


def acknowledge(wallet: str | None, tx_hash: str | None) -> dict:
    wallet = str(wallet or "").strip()
    tx_hash = str(tx_hash or "").strip().lower()
    if not _WALLET_RE.fullmatch(wallet):
        return {"success": False, "error": "Connect your KwentoFi wallet first.", "code": "wallet"}
    if not _TX_RE.fullmatch(tx_hash):
        return {"success": False, "error": "Enter a valid Celo transaction hash.", "code": "tx_hash"}
    try:
        db = vault_donations._admin_db()
        prior = _rows(db.table("community_vault_donations").select("id,feed_post_id,wallet_address,tx_hash,status").ilike("tx_hash", tx_hash).limit(1).execute())
        if prior:
            row = prior[0]
            if str(row.get("wallet_address") or "").lower() != wallet.lower() or row.get("status") != "confirmed":
                return {"success": False, "error": "This transaction hash has already been used for another activity.", "code": "replay"}
            if row.get("feed_post_id"):
                return {"success": True, "duplicate": True, "feed_post_id": row["feed_post_id"], "tx_hash": tx_hash, "tx_url": post_support.CELO_EXPLORER_TX + tx_hash}

        verified = _verified_transaction(wallet, tx_hash)
        if verified.get("pending"):
            return {"success": False, "status": "confirming", "error": verified["error"], "code": "pending"}
        if not verified.get("ok"):
            return {"success": False, "error": verified.get("error") or "Garden contribution could not be verified.", "code": "verification"}

        donor_name = _display_name(wallet)
        now = datetime.now(timezone.utc)
        donation_id = str((prior[0] if prior else {}).get("id") or uuid.uuid4())
        body = f"{donor_name} contributed {verified['amount_text']} G$ to the KwentoFi Garden sponsor pool."
        post_result = db.table("community_feed_posts").insert({
            "wallet_address": "system",
            "username": "KwentoFi",
            "body": body,
            "post_type": GARDEN_POST_TYPE,
            "is_pinned": True,
            "pinned_at": now.isoformat(),
            "created_at": now.isoformat(),
        }).execute()
        post_rows = _rows(post_result)
        if not post_rows or not post_rows[0].get("id"):
            raise RuntimeError("Could not create the Garden acknowledgement post")
        post_id = int(post_rows[0]["id"])
        try:
            donation_row = {
                "id": donation_id,
                "wallet_address": wallet.lower(),
                "donor_name": donor_name,
                "linkedin_url": "",
                "amount_base_units": str(verified["amount_base_units"]),
                "token_decimals": TOKEN_DECIMALS,
                "chain_id": CELO_CHAIN_ID,
                "token_address": verified["token_address"].lower(),
                "vault_address": verified["garden_address"].lower(),
                "status": "confirmed",
                "tx_hash": tx_hash,
                "block_number": verified["block_number"],
                "feed_post_id": post_id,
                "consent_version": CONSENT_VERSION,
                "consented_at": now.isoformat(),
                "created_at": now.isoformat(),
                "submitted_at": now.isoformat(),
                "confirmed_at": now.isoformat(),
                "expires_at": (now + timedelta(days=3650)).isoformat(),
            }
            if prior:
                db.table("community_vault_donations").update(donation_row).eq("id", donation_id).execute()
            else:
                db.table("community_vault_donations").insert(donation_row).execute()
        except Exception:
            try:
                db.table("community_feed_posts").delete().eq("id", post_id).execute()
            except Exception:
                logger.exception("Garden sponsor orphan acknowledgement cleanup failed for post %s", post_id)
            raise
        return {"success": True, "duplicate": False, "feed_post_id": post_id, "tx_hash": tx_hash, "tx_url": post_support.CELO_EXPLORER_TX + tx_hash}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Garden sponsor acknowledgement failed")
        return {"success": False, "error": "The contribution is confirmed, but the acknowledgement is still processing. Retry this same confirmation; do not contribute again.", "code": "retryable"}
