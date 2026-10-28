"""Member-funded Community Vault donations.

When the verified Community Support contract is enabled, the wallet approves
the exact G$ amount and calls its donateToVault method; otherwise the legacy
direct G$ transfer remains available. The browser never chooses the token,
chain, or recipient. Public system posts are created only by the service-role
finalization RPC after exact receipt verification.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from . import withdrawals
from . import post_support

logger = logging.getLogger(__name__)
CONSENT_VERSION = "community-vault-donation-v1"
MINIMUM_GD = 1000
INTENT_TTL_MINUTES = 30
_TX_RE = re.compile(r"^0x[0-9a-f]{64}$")
_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def donations_enabled() -> bool:
    return os.getenv("COMMUNITY_VAULT_DONATIONS_ENABLED", "").strip().upper() == "YES"


def _admin_db():
    from supabase_client import get_supabase_admin_client
    db = get_supabase_admin_client()
    if not db:
        raise RuntimeError("Service-role database client is unavailable")
    return db


def _rows(result) -> list[dict]:
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    if isinstance(data, dict):
        return [data]
    return [row for row in (data or []) if isinstance(row, dict)]


def _runtime(*, for_confirmation: bool = False):
    if not for_confirmation and not donations_enabled():
        raise withdrawals.WithdrawalConfigError(
            "Community Vault donations are not enabled.", code="donations_disabled")
    runtime = withdrawals.build_runtime(
        require_enabled=not for_confirmation, require_signer_key=False)
    # New requests are refused when payouts are paused. A transfer already sent
    # to the vault must still be verifiable/finalizable while the vault is
    # paused or the intake flag is switched off.
    if not for_confirmation and bool(runtime.vault.functions.paused().call()):
        raise withdrawals.WithdrawalPolicyError(
            "Community Vault withdrawals are paused; donations are temporarily unavailable.")
    return runtime


def public_config(wallet: str | None = None) -> dict:
    """Return only the verified network/token/recipient values needed by the UI."""
    runtime = _runtime()
    display_name = None
    if wallet and _WALLET_RE.fullmatch(str(wallet)):
        rows = _rows(_admin_db().table("user_data")
                     .select("community_display_name")
                     .ilike("wallet_address", wallet).limit(1).execute())
        display_name = _clean_name(rows[0].get("community_display_name")) if rows else None
    return {
        "success": True,
        "chain_id": withdrawals.CELO_CHAIN_ID,
        "token_address": runtime.token_address,
        "vault_address": runtime.vault_address,
        "decimals": runtime.decimals,
        "minimum_gd": str(MINIMUM_GD),
        "community_display_name": display_name,
    }


def _clean_name(value) -> str | None:
    if not isinstance(value, str):
        return None
    name = unicodedata.normalize("NFC", value).strip()
    if not name or len(name) > 80 or len(name.encode("utf-8")) > 320:
        return None
    if _CONTROL_RE.search(name):
        return None
    return name


def _clean_linkedin(value) -> str | None:
    if not isinstance(value, str) or len(value.strip()) > 500:
        return None
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except Exception:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme.lower() != "https" or host not in {"linkedin.com", "www.linkedin.com"}
            or parsed.username or parsed.password or port not in (None, 443)
            or not parsed.path.startswith("/in/") or len(parsed.path.split("/")) < 3
            or not parsed.path.split("/")[2]):
        return None
    # Keep only a standard profile URL; discard tracking query/fragment content.
    path = "/" + parsed.path.strip("/") + "/"
    if not re.fullmatch(r"/in/[A-Za-z0-9_%.-]{1,120}/", path):
        return None
    return urlunsplit(("https", "www.linkedin.com", path, "", ""))


def _parse_amount(value, decimals: int) -> tuple[str, int] | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    try:
        decimals = int(decimals)
    except (TypeError, ValueError):
        return None
    if decimals < 0 or decimals > 36:
        return None
    whole_text, _, fraction = text.partition(".")
    if len(fraction) > decimals or len(whole_text) > 78:
        return None
    whole_text = whole_text.lstrip("0") or "0"
    whole = int(whole_text)
    if whole < MINIMUM_GD:
        return None
    scale = 10 ** int(decimals)
    base_units = whole * scale + (int(fraction.ljust(decimals, "0") or "0"))
    if base_units <= 0 or base_units > (2 ** 256 - 1):
        return None
    normalized_fraction = fraction.rstrip("0")
    amount_text = whole_text + ("." + normalized_fraction if normalized_fraction else "")
    return amount_text, base_units


def create_intent(wallet: str | None, payload: dict) -> dict:
    if not wallet or not _WALLET_RE.fullmatch(str(wallet)):
        return {"success": False, "error": "A verified wallet session is required.", "code": "auth"}
    if not isinstance(payload, dict):
        payload = {}
    raw_name = payload.get("name")
    name = _clean_name(raw_name)
    if isinstance(raw_name, str) and raw_name.strip() and not name:
        return {"success": False, "error": "Enter a valid public name (up to 80 characters).", "code": "name"}
    linkedin = _clean_linkedin(payload.get("linkedin_url"))
    if not linkedin:
        return {"success": False, "error": "Enter a valid HTTPS LinkedIn profile URL.", "code": "linkedin"}
    if payload.get("consent") is not True:
        return {"success": False, "error": "Consent to the public acknowledgment is required.", "code": "consent"}
    try:
        runtime = _runtime()
    except withdrawals.WithdrawalConfigError as exc:
        return {"success": False, "error": str(exc), "code": exc.code}
    except withdrawals.WithdrawalPolicyError as exc:
        return {"success": False, "error": str(exc), "code": "vault_unavailable"}
    if post_support.is_available():
        try:
            post_support.validate_deployed_contract(
                expected_token=runtime.token_address,
                expected_vault=runtime.vault_address,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("community vault donation: support contract validation failed: %s", exc)
            return {"success": False, "error": "The Community Support contract does not match the configured Celo G$ token and vault. No donation intent was created.", "code": "contract"}
    parsed = _parse_amount(payload.get("amount_gd"), runtime.decimals)
    if not parsed:
        return {"success": False,
                "error": f"Enter at least 1,000 G$ with no more than {runtime.decimals} decimal places.",
                "code": "amount"}
    amount_text, amount_base = parsed
    now = datetime.now(timezone.utc)
    try:
        db = _admin_db()
        profile_rows = _rows(db.table("user_data").select("id,community_display_name")
                             .ilike("wallet_address", wallet).limit(1).execute())
        if not profile_rows:
            return {"success": False, "error": "Community profile not found. Please reload and try again.", "code": "profile"}
        name = name or _clean_name(profile_rows[0].get("community_display_name"))
        if not name:
            return {"success": False, "error": "Enter a public Community name (up to 80 characters).", "code": "name"}
        db.table("user_data").update({
            "community_display_name": name,
            "updated_at": now.isoformat(),
        }).eq("id", profile_rows[0]["id"]).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("community display name save failed: %s", exc)
        return {"success": False, "error": "Could not save your Community display name. No transfer was sent.", "code": "profile"}
    donation_id = str(uuid.uuid4())
    support_contract = post_support.contract_address() if post_support.is_available() else None
    row = {
        "id": donation_id,
        "wallet_address": wallet.lower(),
        "donor_name": name,
        "linkedin_url": linkedin,
        "amount_base_units": str(amount_base),
        "token_decimals": int(runtime.decimals),
        "chain_id": withdrawals.CELO_CHAIN_ID,
        "token_address": runtime.token_address.lower(),
        "vault_address": runtime.vault_address.lower(),
        "support_contract_address": support_contract.lower() if support_contract else None,
        "status": "pending",
        "consent_version": CONSENT_VERSION,
        "consented_at": now.isoformat(),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=INTENT_TTL_MINUTES)).isoformat(),
    }
    try:
        result = db.table("community_vault_donations").insert(row).execute()
        if not _rows(result):
            # Some PostgREST deployments omit insert representations. Confirm
            # persistence by reading the exact server-generated UUID.
            saved = _rows(db.table("community_vault_donations").select("id")
                          .eq("id", donation_id).limit(1).execute())
            if not saved:
                raise RuntimeError("Donation intent insert was not confirmed")
    except Exception as exc:  # noqa: BLE001
        logger.error("community vault donation intent insert failed: %s", exc)
        return {"success": False, "error": "Could not prepare the donation. No transfer was sent.", "code": "db"}
    return {
        "success": True,
        "donation_id": donation_id,
        "amount_gd": amount_text,
        "amount_base_units": str(amount_base),
        "public_name": name,
        "linkedin_url": linkedin,
        "chain_id": withdrawals.CELO_CHAIN_ID,
        "token_address": runtime.token_address,
        "vault_address": runtime.vault_address,
        "support_contract_address": support_contract,
        "decimals": runtime.decimals,
        "explorer_url": "https://celoscan.io/tx/",
        "expires_at": row["expires_at"],
    }


def _load_intent(db, wallet: str, donation_id: str) -> dict | None:
    try:
        rows = _rows(db.table("community_vault_donations").select("*")
                     .eq("id", donation_id).ilike("wallet_address", wallet)
                     .limit(1).execute())
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("community vault donation intent lookup failed: %s", exc)
        return None


def cancel_intent(wallet: str | None, donation_id: str) -> dict:
    if not wallet or not _WALLET_RE.fullmatch(str(wallet)):
        return {"success": False, "error": "A verified wallet session is required.", "code": "auth"}
    try:
        parsed_id = str(uuid.UUID(str(donation_id)))
        db = _admin_db()
        db.table("community_vault_donations").update({"status": "cancelled"}) \
            .eq("id", parsed_id).ilike("wallet_address", wallet) \
            .eq("status", "pending").execute()
        return {"success": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community vault donation cancellation failed: %s", exc)
        return {"success": False, "error": "Could not cancel the donation request.", "code": "db"}


def confirm_transaction(wallet: str | None, donation_id: str, tx_hash: str) -> dict:
    """Verify exact submitted transfer and atomically record/finalize its post."""
    if not wallet or not _WALLET_RE.fullmatch(str(wallet)):
        return {"success": False, "error": "A verified wallet session is required.", "code": "auth"}
    tx_hash = str(tx_hash or "").strip().lower()
    if not _TX_RE.fullmatch(tx_hash):
        return {"success": False, "error": "A valid transaction hash is required.", "code": "tx_hash"}
    try:
        donation_id = str(uuid.UUID(str(donation_id)))
    except (ValueError, TypeError, AttributeError):
        return {"success": False, "error": "Invalid donation request.", "code": "donation_id"}
    try:
        runtime = _runtime(for_confirmation=True)
        db = _admin_db()
    except withdrawals.WithdrawalConfigError as exc:
        return {"success": False, "status": "processing", "retryable": True,
                "error": "Vault configuration is temporarily unavailable for donation verification. Keep the transaction hash and retry; do not send again.",
                "code": exc.code}
    except withdrawals.WithdrawalPolicyError as exc:
        return {"success": False, "error": str(exc), "code": "vault_unavailable"}
    except Exception as exc:  # noqa: BLE001
        logger.error("community vault donation runtime/database unavailable: %s", exc)
        return {"success": False, "error": "Donation confirmation is temporarily unavailable.", "code": "unavailable"}
    row = _load_intent(db, wallet, donation_id)
    if not row:
        return {"success": False, "error": "Donation request not found.", "code": "not_found"}
    if row.get("status") == "confirmed":
        if str(row.get("tx_hash") or "").lower() != tx_hash:
            return {"success": False, "error": "This donation request is already linked to another transaction.", "code": "tx_conflict"}
        return {"success": True, "status": "confirmed", "duplicate": True,
                "tx_hash": tx_hash, "feed_post_id": row.get("feed_post_id")}
    if row.get("status") not in {"pending", "submitted", "confirming"}:
        return {"success": False, "error": "This donation request is no longer open.", "code": "closed"}
    # The client checks the intent TTL before opening the wallet, but a TTL
    # cannot invalidate a real direct token transfer. This endpoint always
    # re-verifies the exact submitted hash on-chain, so a late browser retry
    # remains safe and can recover an already-sent donation.
    if (int(row.get("chain_id") or 0) != withdrawals.CELO_CHAIN_ID
            or str(row.get("token_address") or "").lower() != runtime.token_address.lower()
            or str(row.get("vault_address") or "").lower() != runtime.vault_address.lower()):
        return {"success": False, "error": "Vault configuration changed after the donation request. No post was created.", "code": "config_changed"}
    old_hash = str(row.get("tx_hash") or "").lower()
    if old_hash and old_hash != tx_hash:
        return {"success": False, "error": "A different transaction is already linked to this donation request.", "code": "tx_conflict"}
    try:
        db.table("community_vault_donations").update({
            "tx_hash": tx_hash,
            "status": "submitted",
            "submitted_at": row.get("submitted_at") or datetime.now(timezone.utc).isoformat(),
        }).eq("id", donation_id).ilike("wallet_address", wallet).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("community vault donation tx hash save failed: %s", exc)
        return {"success": False, "error": "Could not save transaction status. Keep the hash and retry; do not send again.", "code": "db"}
    try:
        tx = runtime.web3.eth.get_transaction(tx_hash)
        receipt = runtime.web3.eth.get_transaction_receipt(tx_hash)
    except Exception:
        return {"success": True, "status": "pending", "tx_hash": tx_hash,
                "message": "Transaction submitted. Waiting for Celo confirmation."}
    try:
        if row.get("support_contract_address"):
            amount_base = _verify_custom_vault_donation(
                runtime, receipt, tx, wallet, row
            )
        else:
            amount_base = withdrawals.verify_funding_receipt(runtime, receipt, tx, wallet)
    except withdrawals.WithdrawalPolicyError as exc:
        if int(receipt.get("status", 0)) == 0:
            try:
                db.table("community_vault_donations").update({"status": "failed"}) \
                    .eq("id", donation_id).eq("tx_hash", tx_hash).execute()
            except Exception:
                pass
        return {"success": False, "error": str(exc), "code": "verification"}
    if str(amount_base) != str(row.get("amount_base_units")):
        return {"success": False, "error": "Confirmed G$ amount does not match this donation request. No feed post was created.", "code": "amount_mismatch"}
    confirmations = int(runtime.web3.eth.block_number) - int(receipt["blockNumber"]) + 1
    if confirmations < int(runtime.confirmations):
        try:
            db.table("community_vault_donations").update({
                "status": "confirming", "block_number": int(receipt["blockNumber"])
            }).eq("id", donation_id).eq("tx_hash", tx_hash).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("community vault donation confirmation-depth save failed: %s", exc)
        return {"success": True, "status": "confirming", "tx_hash": tx_hash,
                "confirmations": confirmations,
                "required_confirmations": int(runtime.confirmations)}
    try:
        amount_gd = str(withdrawals.base_units_to_gd(amount_base, runtime.decimals))
        result = db.rpc("community_vault_donation_finalize", {
            "p_donation_id": donation_id,
            "p_wallet_address": wallet.lower(),
            "p_tx_hash": tx_hash,
            "p_amount_base_units": str(amount_base),
            "p_amount_gd": amount_gd,
            "p_block_number": int(receipt["blockNumber"]),
            "p_token_address": runtime.token_address,
            "p_vault_address": runtime.vault_address,
        }).execute()
        data = getattr(result, "data", None)
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict) or data.get("success") is not True:
            raise RuntimeError("Donation finalization RPC did not confirm success")
        return {"success": True, "status": "confirmed", "tx_hash": tx_hash,
                "amount_gd": amount_gd, "feed_post_id": data.get("feed_post_id"),
                "duplicate": bool(data.get("duplicate"))}
    except Exception as exc:  # noqa: BLE001
        # The on-chain transfer may already be final. Keep the intent and tx
        # hash so the same request can safely retry without sending again.
        logger.error("community vault donation finalization failed: %s", exc)
        return {"success": False, "status": "processing", "retryable": True,
                "tx_hash": tx_hash,
                "error": "The transfer is confirmed, but the acknowledgment is still processing. Retry this same confirmation; do not send again.",
                "code": "finalization_pending"}


def _verify_custom_vault_donation(runtime, receipt, tx, wallet: str, row: dict) -> int:
    """Verify both the CommunitySupport event and the resulting G$ token transfer."""
    try:
        from web3 import Web3

        support_contract = str(row.get("support_contract_address") or "").lower()
        vault = str(row.get("vault_address") or "").lower()
        token = str(row.get("token_address") or "").lower()
        if not _WALLET_RE.fullmatch(support_contract):
            raise ValueError("The support contract address in the donation intent is invalid.")
        if int(receipt.get("status") or 0) != 1:
            raise ValueError("The Community Vault donation transaction failed on Celo.")
        if str(tx.get("to") or "").lower() != support_contract:
            raise ValueError("The transaction was not sent to the Community Support contract.")
        if str(tx.get("from") or "").lower() != wallet.lower():
            raise ValueError("The transaction was not sent from the donating wallet.")

        tx_input = tx.get("input") or tx.get("data") or "0x"
        if isinstance(tx_input, (bytes, bytearray)):
            tx_input = "0x" + bytes(tx_input).hex()
        tx_input = str(tx_input).lower()
        selector = "0x" + bytes(Web3.keccak(text="donateToVault(uint256)")[:4]).hex()
        if not tx_input.startswith(selector) or len(tx_input) != 74:
            raise ValueError("The call is not a donateToVault transaction.")
        call_amount = int(tx_input[10:74], 16)
        expected_amount = int(row.get("amount_base_units") or 0)
        if call_amount != expected_amount:
            raise ValueError("The contract call amount does not match the reviewed donation.")

        vault_event = "0x" + bytes(Web3.keccak(text="VaultDonation(address,address,uint256)")).hex()
        # Compute the standard ERC-20 event topic rather than relying on a
        # copied literal.
        transfer_event = "0x" + bytes(Web3.keccak(text="Transfer(address,address,uint256)")).hex()
        contract_events = []
        token_transfers = []
        for log in receipt.get("logs") or []:
            address = str(log.get("address") or "").lower()
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            topic0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
            if not topic0.startswith("0x"):
                topic0 = "0x" + topic0
            topic0 = topic0.lower()
            data = log.get("data") or "0x"
            if isinstance(data, (bytes, bytearray)):
                data = "0x" + bytes(data).hex()
            data = str(data)
            if not data.startswith("0x"):
                data = "0x" + data
            if address == support_contract and topic0 == vault_event.lower():
                donor_topic = topics[1].hex() if hasattr(topics[1], "hex") else str(topics[1])
                vault_topic = topics[2].hex() if hasattr(topics[2], "hex") else str(topics[2])
                donor_topic = donor_topic if donor_topic.startswith("0x") else "0x" + donor_topic
                vault_topic = vault_topic if vault_topic.startswith("0x") else "0x" + vault_topic
                event_amount = int(data, 16)
                if ("0x" + donor_topic[-40:]).lower() == wallet.lower() \
                        and ("0x" + vault_topic[-40:]).lower() == vault \
                        and event_amount == expected_amount:
                    contract_events.append(event_amount)
            if address == token and topic0 == transfer_event.lower():
                from_topic = topics[1].hex() if hasattr(topics[1], "hex") else str(topics[1])
                to_topic = topics[2].hex() if hasattr(topics[2], "hex") else str(topics[2])
                from_topic = from_topic if from_topic.startswith("0x") else "0x" + from_topic
                to_topic = to_topic if to_topic.startswith("0x") else "0x" + to_topic
                value = int(data, 16)
                if ("0x" + from_topic[-40:]).lower() == wallet.lower() \
                        and ("0x" + to_topic[-40:]).lower() == vault \
                        and value == expected_amount:
                    token_transfers.append(value)
        if len(contract_events) != 1:
            raise ValueError("No unique matching VaultDonation event was found.")
        if len(token_transfers) != 1:
            raise ValueError("No matching G$ transfer to the configured Community Vault was found.")
        return contract_events[0]
    except Exception as exc:  # noqa: BLE001
        raise withdrawals.WithdrawalPolicyError(str(exc)) from exc
