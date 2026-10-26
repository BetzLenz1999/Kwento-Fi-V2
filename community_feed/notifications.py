"""Private in-app notifications for Community Feed interactions.

Notifications are created only by server service code after a reaction/comment
has been persisted. This keeps recipient selection tied to the validated post
and reply parent and prevents browser-supplied wallet targets. The public list
serializer is an explicit allow-list and never returns either wallet address.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)
_WALLET_RE = re.compile(r"^0x[0-9a-f]{40}$")
_REACTIONS = {"like", "love", "haha", "wow", "sad", "angry"}
_PUBLIC_TYPES = {"reaction", "comment", "reply", "post_reply"}
_DEFAULT_ACTOR = "Community member"


def _wallet(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if _WALLET_RE.fullmatch(candidate) else ""


def _actor_name(value: Any) -> str:
    name = str(value or "").strip().lstrip("@").strip()
    name = " ".join(name.split())[:80]
    return name or _DEFAULT_ACTOR


def _db_admin():
    """Return only the service-role client; anon/RLS fallback must fail closed."""
    try:
        from supabase_client import get_supabase_admin_client

        return get_supabase_admin_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_notifications: service client unavailable: %s", exc)
        return None


def _rows(result: Any) -> list[dict]:
    data = getattr(result, "data", None)
    return data if isinstance(data, list) else []


def _insert(db: Any, row: dict) -> bool:
    if db is None:
        return False
    try:
        db.table("community_feed_notifications").insert(row).execute()
        return True
    except Exception as exc:  # noqa: BLE001 - never fail an already-saved interaction
        logger.info("community_notifications: insert skipped: %s", exc)
        return False


def notify_reaction(db: Any, *, actor_wallet: str, actor_username: str,
                    post_owner_wallet: str | None, post_id: int,
                    reaction: str) -> bool:
    """Persist a notification for a newly inserted reaction, never a switch."""
    actor = _wallet(actor_wallet)
    recipient = _wallet(post_owner_wallet)
    if (not actor or not recipient or actor == recipient
            or reaction not in _REACTIONS):
        return False
    try:
        post_id = int(post_id)
        if post_id <= 0:
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    return _insert(db, {
        "recipient_wallet": recipient,
        "actor_wallet": actor,
        "actor_username_snapshot": _actor_name(actor_username),
        "notification_type": "reaction",
        "post_id": post_id,
        "comment_id": None,
        "reaction": reaction,
        "dedupe_key": f"reaction:{post_id}:{actor}",
    })


def notify_comment(db: Any, *, actor_wallet: str, actor_username: str,
                   post_owner_wallet: str | None, post_id: int,
                   comment_id: int, parent: dict | None = None) -> int:
    """Notify the post author and/or direct parent commenter after a comment.

    A top-level comment notifies the post author. A reply notifies its parent
    commenter and, if distinct, the post author. Self-notifications and the
    synthetic System donation post owner are omitted by the caller/validation.
    """
    actor = _wallet(actor_wallet)
    if not actor:
        return 0
    try:
        post_id = int(post_id)
        comment_id = int(comment_id)
        if post_id <= 0 or comment_id <= 0:
            return 0
    except (TypeError, ValueError, OverflowError):
        return 0

    recipients: dict[str, str] = {}
    parent_wallet = _wallet((parent or {}).get("wallet_address"))
    post_owner = _wallet(post_owner_wallet)
    if parent_wallet and parent_wallet != actor:
        recipients[parent_wallet] = "reply"
    if post_owner and post_owner != actor:
        recipients.setdefault(post_owner, "post_reply" if parent else "comment")

    saved = 0
    for recipient, notification_type in recipients.items():
        if notification_type not in _PUBLIC_TYPES:
            continue
        if _insert(db, {
            "recipient_wallet": recipient,
            "actor_wallet": actor,
            "actor_username_snapshot": _actor_name(actor_username),
            "notification_type": notification_type,
            "post_id": post_id,
            "comment_id": comment_id,
            "reaction": None,
            "dedupe_key": f"comment:{comment_id}",
        }):
            saved += 1
    return saved


def _public_notification(row: dict) -> dict | None:
    """Build an allow-listed response object; never copy raw database rows."""
    try:
        notification_id = int(row.get("id"))
        post_id = int(row.get("post_id"))
    except (TypeError, ValueError, OverflowError):
        return None
    kind = str(row.get("notification_type") or "")
    if notification_id <= 0 or post_id <= 0 or kind not in _PUBLIC_TYPES:
        return None
    comment_id = row.get("comment_id")
    if comment_id is not None:
        try:
            comment_id = int(comment_id)
            if comment_id <= 0:
                comment_id = None
        except (TypeError, ValueError, OverflowError):
            comment_id = None
    reaction = row.get("reaction") if kind == "reaction" else None
    if reaction not in _REACTIONS:
        reaction = None
    return {
        "id": str(notification_id),
        "notification_type": kind,
        "actor_username": _actor_name(row.get("actor_username_snapshot")),
        "post_id": post_id,
        "comment_id": comment_id,
        "reaction": reaction,
        "created_at": row.get("created_at"),
        "read_at": row.get("read_at"),
    }


def list_for_wallet(wallet: str, *, limit: int = 20,
                    before_id: int | None = None) -> dict:
    recipient = _wallet(wallet)
    if not recipient:
        return {"success": False, "code": "auth", "error": "Not authenticated"}
    try:
        limit = max(1, min(int(limit), 50))
        if before_id is not None:
            before_id = int(before_id)
            if before_id <= 0:
                return {"success": False, "code": "cursor", "error": "Invalid notification cursor."}
    except (TypeError, ValueError, OverflowError):
        return {"success": False, "code": "cursor", "error": "Invalid notification cursor."}

    db = _db_admin()
    if db is None:
        return {"success": False, "code": "db", "error": "Notifications are temporarily unavailable."}
    try:
        query = (db.table("community_feed_notifications")
                 .select("id,notification_type,actor_username_snapshot,post_id,comment_id,reaction,created_at,read_at")
                 .eq("recipient_wallet", recipient)
                 .order("id", desc=True)
                 .limit(limit + 1))
        if before_id is not None:
            query = query.lt("id", before_id)
        rows = _rows(query.execute())
        has_more = len(rows) > limit
        page = rows[:limit]
        public_rows = [item for row in page
                       if (item := _public_notification(row)) is not None]

        count_result = (db.table("community_feed_notifications")
                        .select("id", count="exact")
                        .eq("recipient_wallet", recipient)
                        .is_("read_at", "null")
                        .limit(1)
                        .execute())
        unread_count = getattr(count_result, "count", None)
        if unread_count is None:
            raise RuntimeError("Supabase did not return an exact unread count")
        cursor = str(page[-1].get("id")) if has_more and page else None
        return {
            "success": True,
            "notifications": public_rows,
            "unread_count": max(0, int(unread_count)),
            "next_cursor": cursor,
            "has_more": has_more,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_notifications: list failed: %s", exc)
        return {"success": False, "code": "db", "error": "Notifications are temporarily unavailable."}


def mark_one_read(wallet: str, notification_id: int) -> dict:
    recipient = _wallet(wallet)
    if not recipient:
        return {"success": False, "code": "auth", "error": "Not authenticated"}
    try:
        notification_id = int(notification_id)
        if not 1 <= notification_id <= 9_223_372_036_854_775_807:
            raise ValueError("invalid id")
    except (TypeError, ValueError, OverflowError):
        return {"success": False, "code": "id", "error": "Invalid notification ID."}
    db = _db_admin()
    if db is None:
        return {"success": False, "code": "db", "error": "Notifications are temporarily unavailable."}
    try:
        (db.table("community_feed_notifications")
         .update({"read_at": datetime.now(timezone.utc).isoformat()})
         .eq("id", notification_id)
         .eq("recipient_wallet", recipient)
         .is_("read_at", "null")
         .execute())
        # Idempotent and deliberately does not disclose whether an ID owned by
        # another wallet exists.
        return {"success": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_notifications: mark-one-read failed: %s", exc)
        return {"success": False, "code": "db", "error": "Could not update notifications."}


def mark_all_read(wallet: str) -> dict:
    recipient = _wallet(wallet)
    if not recipient:
        return {"success": False, "code": "auth", "error": "Not authenticated"}
    db = _db_admin()
    if db is None:
        return {"success": False, "code": "db", "error": "Notifications are temporarily unavailable."}
    try:
        (db.table("community_feed_notifications")
         .update({"read_at": datetime.now(timezone.utc).isoformat()})
         .eq("recipient_wallet", recipient)
         .is_("read_at", "null")
         .execute())
        return {"success": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_notifications: mark-all-read failed: %s", exc)
        return {"success": False, "code": "db", "error": "Could not update notifications."}
