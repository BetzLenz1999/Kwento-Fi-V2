"""Payment-gated Daily Task submission service (post URL is stored only after payment)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import os
import re
from typing import Any
from urllib.parse import urlsplit

from web3 import Web3

from daily_task_contract_service import (
    DailyTaskContractError,
    commitment_for,
    get_contract_context,
    issue_payment_authorization,
    reward_to_wei,
    verify_payment_receipt,
)

PLATFORM_TABLES = {
    "twitter": ("twitter_task_log", "twitter_url"),
    "telegram": ("telegram_task_log", "telegram_url"),
}


class DailyTaskSubmissionError(RuntimeError):
    pass


def _platform_service(platform: str):
    if platform == "twitter":
        from twitter_task.twitter_task import twitter_task_service
        return twitter_task_service
    if platform == "telegram":
        from telegram_task.telegram_task import telegram_task_service
        return telegram_task_service
    raise DailyTaskSubmissionError("Choose Twitter/X or Telegram.")


def _normal_url_key(platform: str, url: str) -> str:
    parsed = urlsplit((url or "").strip())
    if platform == "twitter":
        match = re.search(r"/status/(\d+)", parsed.path)
        if not match:
            raise DailyTaskSubmissionError("Enter a valid X/Twitter post URL.")
        canonical = match.group(1)
    else:
        if (parsed.hostname or "").lower() not in {"t.me", "telegram.me"}:
            raise DailyTaskSubmissionError("Enter a valid Telegram post URL.")
        canonical = (parsed.path or "").rstrip("/").lower()
        if not canonical:
            raise DailyTaskSubmissionError("Enter a valid Telegram post URL.")
    secret = (os.getenv("DAILY_TASK_POST_HASH_SECRET") or "").encode("utf-8")
    if len(secret) < 32:
        raise DailyTaskSubmissionError("Daily Task post-hash secret is not configured securely.")
    return hmac.new(secret, f"{platform}:{canonical}".encode("utf-8"), hashlib.sha256).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_supabase():
    from supabase_client import get_supabase_admin_client, get_supabase_client
    return get_supabase_admin_client() or get_supabase_client()


def _validate_and_check_eligibility(platform: str, wallet: str, post_url: str, service=None) -> tuple[Any, str]:
    service = service or _platform_service(platform)
    if not wallet:
        raise DailyTaskSubmissionError("Sign in with your wallet to submit a task.")
    if not isinstance(post_url, str) or not post_url.strip() or len(post_url) > 2048:
        raise DailyTaskSubmissionError("Enter a valid post URL.")
    validator = service._validate_twitter_url if platform == "twitter" else service._validate_telegram_url
    validation = validator(post_url.strip())
    if not validation.get("valid"):
        raise DailyTaskSubmissionError(validation.get("error") or "Invalid post URL.")
    try:
        import asyncio
        eligibility = asyncio.run(service.check_eligibility(wallet))
    except RuntimeError:
        # Flask routes are synchronous, but tests or an embedding host can
        # already have a running loop; isolate the async eligibility call.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            eligibility = pool.submit(asyncio.run, service.check_eligibility(wallet)).result()
    if not eligibility.get("can_claim"):
        raise DailyTaskSubmissionError(eligibility.get("reason") or "You are not eligible to submit a Daily Task yet.")
    return service, _normal_url_key(platform, post_url.strip())


def _active_duplicate(supabase, platform: str, url_column: str, url_key: str, post_url: str) -> dict | None:
    table = PLATFORM_TABLES[platform][0]
    try:
        query = supabase.table(table).select("id,wallet_address,status," + url_column)
        try:
            match = query.eq("post_url_hash", url_key).limit(1).execute()
            if match.data:
                return match.data[0]
        except Exception:
            # Existing production rows may predate the hash column until the
            # additive migration is applied; the legacy URL scan remains safe.
            pass
        rows = supabase.table(table).select("id,wallet_address,status," + url_column).execute()
        wanted = _normal_url_key(platform, post_url)
        for row in rows.data or []:
            try:
                if _normal_url_key(platform, row.get(url_column) or "") == wanted:
                    return row
            except DailyTaskSubmissionError:
                continue
    except Exception as exc:
        raise DailyTaskSubmissionError("Unable to check whether this post link was already used. Please try again.") from exc
    return None


def _reward_snapshot(service) -> tuple[str, int]:
    reward_value = service.get_task_reward()
    reward_wei = reward_to_wei(reward_value)
    return format(Decimal(str(reward_value)), "f"), reward_wei


def create_payment_intent(wallet: str, platform: str, post_url: str, salt_hex: str) -> dict:
    if platform not in PLATFORM_TABLES:
        raise DailyTaskSubmissionError("Choose Twitter/X or Telegram.")
    service, url_key = _validate_and_check_eligibility(platform, wallet, post_url)
    supabase = _get_supabase()
    if not supabase:
        raise DailyTaskSubmissionError("The Daily Task database is unavailable. Please try again later.")
    # A signed voucher is short-lived. Mark expired reservations before the
    # unique active-post index is evaluated so an abandoned attempt cannot
    # block that post link indefinitely.
    try:
        supabase.table("daily_task_payment_intents").update({
            "status": "expired",
            "updated_at": _now_iso(),
        }).eq("status", "issued").lt("voucher_deadline", int(datetime.now(timezone.utc).timestamp())).execute()
    except Exception as exc:
        raise DailyTaskSubmissionError("Unable to refresh expired payment attempts. Please try again shortly.") from exc
    duplicate = _active_duplicate(supabase, platform, PLATFORM_TABLES[platform][1], url_key, post_url)
    if duplicate:
        if str(duplicate.get("wallet_address", "")).lower() == wallet.lower():
            raise DailyTaskSubmissionError("This post link was already submitted. Check your Daily Task history.")
        raise DailyTaskSubmissionError("This post link has already been used by another wallet.")

    reward_text, reward_wei = _reward_snapshot(service)
    context = get_contract_context()
    import secrets
    intent_bytes = secrets.token_bytes(32)
    commitment = commitment_for(wallet, platform, post_url.strip(), salt_hex, intent_bytes)
    authorization = issue_payment_authorization(
        wallet, platform, commitment, reward_wei, context=context, intent_id=intent_bytes
    )
    now = datetime.now(timezone.utc)
    row = {
        "intent_id": authorization["intent_id"],
        "wallet_address": Web3.to_checksum_address(wallet).lower(),
        "platform": platform,
        "post_commitment": authorization["post_commitment"],
        "post_url_hash": url_key,
        "reward_amount": reward_text,
        "reward_amount_wei": str(reward_wei),
        "owner_signature": authorization["owner_signature"],
        "voucher_deadline": authorization["deadline"],
        "status": "issued",
        "created_at": now.isoformat(),
        "expires_at": datetime.fromtimestamp(authorization["deadline"], timezone.utc).isoformat(),
    }
    try:
        # Reserve the salted URL hash and intent only. The actual post URL is
        # deliberately not written until the chain payment has been verified.
        inserted = supabase.table("daily_task_payment_intents").insert(row).execute()
        if not getattr(inserted, "data", None):
            raise DailyTaskSubmissionError("Could not reserve this Daily Task payment. Please retry.")
    except DailyTaskSubmissionError:
        raise
    except Exception as exc:
        raise DailyTaskSubmissionError("This post already has an active payment attempt, or the payment service is unavailable.") from exc
    return {
        **authorization,
        "reward_amount": reward_text,
        "post_url_storage": "after confirmed 20 G$ payment only",
        "gas_sponsorship_threshold_wei": str(300_000_000_000_000_000),
        "gas_sponsorship_threshold_celo": "0.3",
        "non_refundable": True,
    }


def confirm_paid_submission(wallet: str, intent_id: str, post_url: str, salt_hex: str, payment_tx_hash: str) -> dict:
    if not wallet:
        raise DailyTaskSubmissionError("Sign in with your wallet to continue.")
    if not isinstance(intent_id, str) or not isinstance(payment_tx_hash, str):
        raise DailyTaskSubmissionError("Missing payment authorization or transaction hash.")
    try:
        intent_id = Web3.to_hex(Web3.to_bytes(hexstr=intent_id))
        payment_tx_hash = Web3.to_hex(Web3.to_bytes(hexstr=payment_tx_hash))
        if len(Web3.to_bytes(hexstr=intent_id)) != 32:
            raise ValueError
    except Exception as exc:
        raise DailyTaskSubmissionError("Invalid payment authorization or transaction hash.") from exc
    supabase = _get_supabase()
    if not supabase:
        raise DailyTaskSubmissionError("The Daily Task database is unavailable. Your confirmed payment is safe; retry this confirmation later.")
    existing = supabase.table("daily_task_payment_intents").select("*").eq("intent_id", intent_id).limit(1).execute()
    if not existing.data:
        raise DailyTaskSubmissionError("Payment intent not found. Please contact support with your transaction hash.")
    intent = existing.data[0]
    if str(intent.get("wallet_address", "")).lower() != wallet.lower():
        raise DailyTaskSubmissionError("This payment intent belongs to a different wallet.")
    # A user may broadcast just before voucher expiry and return after the
    # expiry sweeper runs. The receipt verifier below accepts that transaction
    # only if its mined block timestamp was within the signed deadline.
    if intent.get("status") not in {"issued", "paid", "submitted", "expired"}:
        raise DailyTaskSubmissionError("This payment intent is not available for submission.")
    platform = intent.get("platform")
    if platform not in PLATFORM_TABLES:
        raise DailyTaskSubmissionError("Payment intent contains an unsupported platform.")
    try:
        expected_commitment = commitment_for(wallet, platform, post_url.strip(), salt_hex, Web3.to_bytes(hexstr=intent_id))
    except Exception as exc:
        raise DailyTaskSubmissionError(str(exc)) from exc
    if Web3.to_hex(expected_commitment).lower() != str(intent.get("post_commitment", "")).lower():
        raise DailyTaskSubmissionError("Post URL does not match the payment authorization. No submission was stored.")
    if intent.get("status") == "submitted":
        if str(intent.get("payment_tx_hash", "")).lower() != payment_tx_hash.lower():
            raise DailyTaskSubmissionError("This intent was already submitted with a different payment transaction.")
        return {"success": True, "pending": True, "status": "pending_approval", "already_recorded": True}

    try:
        url_key = _normal_url_key(platform, post_url.strip())
    except DailyTaskSubmissionError:
        raise
    if url_key != intent.get("post_url_hash"):
        raise DailyTaskSubmissionError("Post link does not match the reserved payment intent.")
    expected = {
        "wallet": wallet,
        "intent_id": intent_id,
        "platform_hash": Web3.to_hex(Web3.keccak(text=platform)),
        "post_commitment": intent["post_commitment"],
        "reward_amount_wei": intent["reward_amount_wei"],
        "deadline": int(intent["voucher_deadline"]),
    }
    try:
        verified = verify_payment_receipt(payment_tx_hash, expected)
    except DailyTaskContractError as exc:
        raise DailyTaskSubmissionError(str(exc)) from exc
    # Reserve this URL against new intents before creating its task-log row.
    # The signed receipt is verified first, so an unverified client cannot lock
    # a link or update the intent's audit hash.
    try:
        supabase.table("daily_task_payment_intents").update({
            "status": "paid",
            "payment_tx_hash": verified["tx_hash"],
            "updated_at": _now_iso(),
        }).eq("intent_id", intent_id).execute()
    except Exception as exc:
        raise DailyTaskSubmissionError("Payment was confirmed on Celo but its reservation could not be updated. Retry confirmation; do not pay again.") from exc
    table, url_column = PLATFORM_TABLES[platform]
    existing_row = supabase.table(table).select("id,status,payment_tx_hash").eq("payment_intent_id", intent_id).limit(1).execute()
    if existing_row.data:
        row_tx = str(existing_row.data[0].get("payment_tx_hash") or "").lower()
        if row_tx and row_tx != verified["tx_hash"].lower():
            raise DailyTaskSubmissionError("This intent was already recorded with a different confirmed payment.")
        supabase.table("daily_task_payment_intents").update({"status": "submitted", "payment_tx_hash": verified["tx_hash"], "submitted_at": _now_iso(), "updated_at": _now_iso()}).eq("intent_id", intent_id).execute()
        return {"success": True, "pending": True, "status": "pending_approval", "already_recorded": True}

    duplicate = _active_duplicate(supabase, platform, url_column, url_key, post_url)
    if duplicate:
        raise DailyTaskSubmissionError("This post link was claimed before confirmation. The 20 G$ payment is non-refundable; contact support with your transaction hash.")

    row = {
        "wallet_address": Web3.to_checksum_address(wallet).lower(),
        url_column: post_url.strip(),
        "post_url_hash": url_key,
        "reward_amount": str(intent["reward_amount"]),
        "status": "pending",
        "transaction_hash": None,
        "payment_tx_hash": verified["tx_hash"],
        "payment_intent_id": intent_id,
        "payment_fee": "20",
        "payment_status": "paid",
        "created_at": _now_iso(),
    }
    try:
        inserted = supabase.table(table).insert(row).execute()
    except Exception as exc:
        # A retry after an uncertain DB response is safe: payment_intent_id is
        # unique, and the verified chain receipt can be submitted again.
        retry = supabase.table(table).select("id,status").eq("payment_intent_id", intent_id).limit(1).execute()
        if retry.data:
            inserted = retry
        else:
            raise DailyTaskSubmissionError("Payment was confirmed, but the submission could not yet be saved. Retry confirmation; do not pay again.") from exc
    if not getattr(inserted, "data", None):
        retry = supabase.table(table).select("id,status").eq("payment_intent_id", intent_id).limit(1).execute()
        if not retry.data:
            raise DailyTaskSubmissionError("Payment was confirmed, but the submission could not yet be saved. Retry confirmation; do not pay again.")
    supabase.table("daily_task_payment_intents").update({
        "status": "submitted",
        "payment_tx_hash": verified["tx_hash"],
        "submitted_at": _now_iso(),
        "updated_at": _now_iso(),
    }).eq("intent_id", intent_id).execute()
    return {
        "success": True,
        "pending": True,
        "status": "pending_approval",
        "message": "20 G$ payment confirmed. Your post is now submitted for admin review.",
        "payment_tx_hash": verified["tx_hash"],
    }
