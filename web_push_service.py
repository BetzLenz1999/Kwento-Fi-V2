"""Best-effort Web Push delivery for authenticated GoodMarket users.

Private message bodies are intentionally never included in push payloads. The
service stores one subscription per wallet/endpoint and removes subscriptions
that the push provider reports as permanently expired.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
_TABLE = "community_push_subscriptions"
_MAX_ENDPOINT = 2048


def _db():
    try:
        from supabase_client import get_supabase_admin_client, get_supabase_client
        return get_supabase_admin_client() or get_supabase_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("web push: database unavailable: %s", exc)
        return None


def _clean_wallet(value: str | None) -> str:
    return (value or "").strip().lower()


def public_key() -> str:
    return (os.getenv("VAPID_PUBLIC_KEY") or "").strip()


def configured() -> bool:
    return bool(public_key() and os.getenv("VAPID_PRIVATE_KEY") and os.getenv("VAPID_SUBJECT"))


def _valid_endpoint(value: str | None) -> bool:
    try:
        parsed = urlparse(str(value or ""))
        return parsed.scheme == "https" and bool(parsed.netloc) and len(parsed.geturl()) <= _MAX_ENDPOINT
    except ValueError:
        return False


def _subscription_payload(subscription: dict) -> tuple[dict | None, str]:
    if not isinstance(subscription, dict):
        return None, "Invalid push subscription."
    endpoint = str(subscription.get("endpoint") or "").strip()
    keys = subscription.get("keys") if isinstance(subscription.get("keys"), dict) else {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    if not _valid_endpoint(endpoint) or not p256dh or not auth:
        return None, "Invalid push subscription."
    return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}, ""


def save_subscription(wallet: str, subscription: dict, user_agent: str | None = None) -> dict:
    viewer = _clean_wallet(wallet)
    payload, error = _subscription_payload(subscription)
    if not viewer or not payload:
        return {"success": False, "code": "invalid_subscription", "error": error or "Invalid push subscription."}
    db = _db()
    if not db:
        return {"success": False, "code": "db_unavailable", "error": "Notifications are temporarily unavailable."}
    try:
        db.table(_TABLE).upsert({
            "wallet_address": viewer,
            "endpoint": payload["endpoint"],
            "p256dh": payload["p256dh"],
            "auth": payload["auth"],
            "user_agent": str(user_agent or "")[:500],
            "revoked_at": None,
        }, on_conflict="wallet_address,endpoint").execute()
        return {"success": True, "enabled": configured()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("web push: subscription save failed: %s", exc)
        return {"success": False, "code": "db_error", "error": "Could not enable notifications."}


def remove_subscription(wallet: str, endpoint: str | None) -> dict:
    viewer = _clean_wallet(wallet)
    if not viewer or not _valid_endpoint(endpoint):
        return {"success": False, "code": "invalid_subscription", "error": "Invalid push subscription."}
    db = _db()
    if not db:
        return {"success": False, "code": "db_unavailable", "error": "Notifications are temporarily unavailable."}
    try:
        db.table(_TABLE).update({"revoked_at": datetime.now(timezone.utc).isoformat()}).eq("wallet_address", viewer).eq("endpoint", endpoint).execute()
        return {"success": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("web push: subscription revoke failed: %s", exc)
        return {"success": False, "code": "db_error", "error": "Could not disable notifications."}


def _recipient_subscriptions(wallet: str) -> list[dict]:
    db = _db()
    if not db:
        return []
    try:
        result = (db.table(_TABLE).select("id,endpoint,p256dh,auth")
                  .eq("wallet_address", _clean_wallet(wallet)).is_("revoked_at", "null").execute())
        return list(getattr(result, "data", None) or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("web push: subscription lookup failed: %s", exc)
        return []


def _revoke_id(subscription_id) -> None:
    db = _db()
    if not db or not subscription_id:
        return
    try:
        db.table(_TABLE).update({"revoked_at": datetime.now(timezone.utc).isoformat()}).eq("id", subscription_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("web push: failed to revoke expired subscription: %s", exc)


def notify_new_message(recipient_wallet: str, sender_username: str, conversation_id: str) -> dict:
    """Notify all active recipient devices; never raises into message sending."""
    if not configured():
        return {"sent": 0, "skipped": "not_configured"}
    subscriptions = _recipient_subscriptions(recipient_wallet)
    if not subscriptions:
        return {"sent": 0, "skipped": "no_subscriptions"}
    try:
        from pywebpush import WebPushException, webpush
    except Exception as exc:  # noqa: BLE001
        logger.warning("web push: pywebpush unavailable: %s", exc)
        return {"sent": 0, "skipped": "dependency_unavailable"}

    sender = str(sender_username or "Community member").strip().lstrip("@")[:80] or "Community member"
    payload = json.dumps({
        "title": "New GoodMarket message",
        "body": f"@{sender} sent you a new message.",
        "url": f"/messages?conversation={conversation_id}",
        "tag": f"community-message-{conversation_id}",
    }, separators=(",", ":"))
    sent = 0
    for item in subscriptions:
        subscription_info = {"endpoint": item.get("endpoint"), "keys": {
            "p256dh": item.get("p256dh"), "auth": item.get("auth")}}
        try:
            webpush(subscription_info=subscription_info, data=payload,
                    vapid_private_key=os.getenv("VAPID_PRIVATE_KEY"),
                    vapid_claims={"sub": os.getenv("VAPID_SUBJECT")})
            sent += 1
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if status in (404, 410):
                _revoke_id(item.get("id"))
            logger.info("web push: delivery failed status=%s", status)
        except Exception as exc:  # noqa: BLE001
            logger.warning("web push: delivery failed: %s", exc)
    return {"sent": sent, "attempted": len(subscriptions)}
