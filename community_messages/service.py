"""Private one-to-one Community messaging backed by protected Supabase tables.

The browser receives usernames and avatar paths only. Wallet addresses are used
as server-side participant keys and are never returned in API payloads.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

from community_feed import service as profile_service

logger = logging.getLogger(__name__)
MAX_MESSAGE = max(1, int(os.getenv("COMMUNITY_DM_MAX_MESSAGE", "2000")))
MAX_PAGE_SIZE = max(1, min(50, int(os.getenv("COMMUNITY_DM_PAGE_SIZE", "40"))))

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029]")


def _db():
    try:
        from supabase_client import get_supabase_admin_client, get_supabase_client
        return get_supabase_admin_client() or get_supabase_client()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_messages: database unavailable: %s", exc)
        return None


def _rows(result) -> list[dict]:
    return list(getattr(result, "data", None) or []) if result else []


def _wallet(value: str | None) -> str:
    return (value or "").strip().lower()


def normalize_body(value: str | None) -> str:
    text = _CONTROL.sub("", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text.strip()[:MAX_MESSAGE]


def _pair_key(first: str, second: str) -> str:
    return ":".join(sorted((_wallet(first), _wallet(second))))


def _profile(username: str | None) -> tuple[str | None, str | None]:
    name = profile_service.normalize_username(username)
    if not name:
        return None, None
    wallet = profile_service.wallet_for_username(name)
    return name, _wallet(wallet)


def _avatar(username: str | None) -> str | None:
    try:
        return profile_service.public_avatar_path(username)
    except Exception:  # noqa: BLE001
        return None


def _person(wallet: str, username: str | None = None) -> dict:
    name = username or profile_service.get_username(wallet) or "Community member"
    return {"username": name.lstrip("@"), "avatar_url": _avatar(name)}


def _authorized_participant(db, conversation_id: str, wallet: str):
    rows = _rows(db.table("community_dm_participants").select("conversation_id,wallet_address,last_read_at")
              .eq("conversation_id", conversation_id).eq("wallet_address", _wallet(wallet)).limit(1).execute())
    return rows[0] if rows else None


def _conversation(db, conversation_id: str) -> dict | None:
    rows = _rows(db.table("community_dm_conversations").select("id,pair_key,status,created_at,last_message_at,last_message_preview")
              .eq("id", conversation_id).limit(1).execute())
    return rows[0] if rows else None


def start_conversation(viewer_wallet: str, username: str) -> dict:
    db = _db()
    viewer = _wallet(viewer_wallet)
    name, target = _profile(username)
    if not db:
        return {"success": False, "code": "db_unavailable", "error": "Messaging is temporarily unavailable."}
    if not viewer or not target:
        return {"success": False, "code": "not_found", "error": "Member not found."}
    if viewer == target:
        return {"success": False, "code": "self_message", "error": "You cannot message yourself."}
    pair = _pair_key(viewer, target)
    try:
        existing = _rows(db.table("community_dm_conversations").select("id,pair_key,status,created_at,last_message_at,last_message_preview")
                         .eq("pair_key", pair).limit(1).execute())
        if existing:
            conversation = existing[0]
        else:
            created = db.table("community_dm_conversations").insert({"pair_key": pair}).execute()
            conversation = (_rows(created) or [None])[0]
            if not conversation:
                existing = _rows(db.table("community_dm_conversations").select("id,pair_key,status,created_at,last_message_at,last_message_preview")
                                 .eq("pair_key", pair).limit(1).execute())
                conversation = (existing or [None])[0]
        if not conversation:
            return {"success": False, "code": "create_failed", "error": "Could not open the conversation."}
        cid = conversation["id"]
        participants = _rows(db.table("community_dm_participants").select("wallet_address")
                              .eq("conversation_id", cid).execute())
        known = {_wallet(row.get("wallet_address")) for row in participants}
        missing = []
        for address in (viewer, target):
            if address not in known:
                missing.append({"conversation_id": cid, "wallet_address": address,
                                "username_snapshot": profile_service.get_username(address)})
        if missing:
            db.table("community_dm_participants").insert(missing).execute()
        return {"success": True, "conversation": _public_conversation(db, conversation, viewer)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_messages: start failed: %s", exc)
        return {"success": False, "code": "db_error", "error": "Could not open the conversation."}


def _public_conversation(db, row: dict, viewer_wallet: str) -> dict:
    cid = row.get("id")
    participants = _rows(db.table("community_dm_participants").select("wallet_address,username_snapshot")
                          .eq("conversation_id", cid).execute())
    viewer = _wallet(viewer_wallet)
    other = next((p for p in participants if _wallet(p.get("wallet_address")) != viewer), None)
    person = _person(_wallet(other.get("wallet_address")), other.get("username_snapshot")) if other else {
        "username": "Community member", "avatar_url": None}
    unread = 0
    participant = next((p for p in participants if _wallet(p.get("wallet_address")) == viewer), None)
    if participant and participant.get("last_read_at"):
        try:
            unread_result = (db.table("community_dm_messages").select("id", count="exact")
                             .eq("conversation_id", cid).neq("sender_wallet", viewer)
                             .is_("deleted_at", "null").gt("created_at", participant["last_read_at"]).execute())
            unread = int(getattr(unread_result, "count", None) or len(_rows(unread_result)))
        except Exception:  # noqa: BLE001
            unread = 0
    return {"id": cid, "username": person["username"], "avatar_url": person["avatar_url"],
            "last_message": row.get("last_message_preview") or "", "last_message_at": row.get("last_message_at"),
            "unread_count": unread}


def list_conversations(viewer_wallet: str) -> dict:
    db = _db()
    viewer = _wallet(viewer_wallet)
    if not db or not viewer:
        return {"success": False, "code": "db_unavailable", "error": "Messaging is temporarily unavailable.", "conversations": []}
    try:
        links = _rows(db.table("community_dm_participants").select("conversation_id")
                      .eq("wallet_address", viewer).execute())
        ids = [row.get("conversation_id") for row in links if row.get("conversation_id")]
        if not ids:
            return {"success": True, "conversations": [], "unread_total": 0}
        rows = _rows(db.table("community_dm_conversations").select("id,pair_key,status,created_at,last_message_at,last_message_preview")
                     .in_("id", ids).eq("status", "active").order("last_message_at", desc=True).execute())
        conversations = [_public_conversation(db, row, viewer) for row in rows]
        return {"success": True, "conversations": conversations,
                "unread_total": sum(item["unread_count"] for item in conversations)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_messages: list failed: %s", exc)
        return {"success": False, "code": "db_error", "error": "Could not load messages.", "conversations": []}


def get_thread(viewer_wallet: str, conversation_id: str, limit: int | None = None) -> dict:
    db = _db()
    viewer = _wallet(viewer_wallet)
    if not db or not viewer:
        return {"success": False, "code": "db_unavailable", "error": "Messaging is temporarily unavailable."}
    try:
        row = _conversation(db, conversation_id)
        participant = _authorized_participant(db, conversation_id, viewer) if row else None
        if not row or not participant:
            return {"success": False, "code": "forbidden", "error": "Conversation not found."}
        people = _rows(db.table("community_dm_participants").select("wallet_address,username_snapshot")
                       .eq("conversation_id", conversation_id).execute())
        other = next((p for p in people if _wallet(p.get("wallet_address")) != viewer), None)
        person = _person(_wallet(other.get("wallet_address")), other.get("username_snapshot")) if other else {"username": "Community member", "avatar_url": None}
        size = max(1, min(MAX_PAGE_SIZE, int(limit or MAX_PAGE_SIZE)))
        messages = _rows(db.table("community_dm_messages").select("id,sender_wallet,body,created_at")
                         .eq("conversation_id", conversation_id).is_("deleted_at", "null")
                         .order("created_at", desc=True).limit(size).execute())
        messages.reverse()
        return {"success": True, "conversation": {"id": conversation_id, **person},
                "messages": [{"id": m.get("id"), "body": m.get("body") or "", "created_at": m.get("created_at"),
                              "is_me": _wallet(m.get("sender_wallet")) == viewer} for m in messages]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_messages: thread failed: %s", exc)
        return {"success": False, "code": "db_error", "error": "Could not load conversation."}


def send_message(viewer_wallet: str, conversation_id: str, body: str, client_message_id: str | None = None) -> dict:
    db = _db()
    viewer = _wallet(viewer_wallet)
    text = normalize_body(body)
    if not text:
        return {"success": False, "code": "empty", "error": "Message cannot be empty."}
    if len(text.encode("utf-8")) > MAX_MESSAGE * 4:
        return {"success": False, "code": "too_long", "error": f"Message is too long (max {MAX_MESSAGE} characters)."}
    if not db or not viewer:
        return {"success": False, "code": "db_unavailable", "error": "Messaging is temporarily unavailable."}
    try:
        conversation = _conversation(db, conversation_id)
        if not conversation or conversation.get("status") != "active" or not _authorized_participant(db, conversation_id, viewer):
            return {"success": False, "code": "forbidden", "error": "Conversation not found."}
        row = {"conversation_id": conversation_id, "sender_wallet": viewer, "body": text}
        if client_message_id:
            row["client_message_id"] = str(client_message_id)[:100]
        result = db.table("community_dm_messages").insert(row).execute()
        message = (_rows(result) or [None])[0]
        if not message:
            return {"success": False, "code": "insert_failed", "error": "Message was not sent."}
        now = datetime.now(timezone.utc).isoformat()
        db.table("community_dm_conversations").update({"last_message_at": now, "last_message_preview": text[:160]}).eq("id", conversation_id).execute()
        return {"success": True, "message": {"id": message.get("id"), "body": text,
                "created_at": message.get("created_at") or now, "is_me": True}}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_messages: send failed: %s", exc)
        return {"success": False, "code": "db_error", "error": "Message was not sent."}


def mark_read(viewer_wallet: str, conversation_id: str) -> dict:
    db = _db()
    viewer = _wallet(viewer_wallet)
    if not db or not viewer:
        return {"success": False, "code": "db_unavailable", "error": "Messaging is temporarily unavailable."}
    try:
        if not _authorized_participant(db, conversation_id, viewer):
            return {"success": False, "code": "forbidden", "error": "Conversation not found."}
        db.table("community_dm_participants").update({"last_read_at": datetime.now(timezone.utc).isoformat()}) \
          .eq("conversation_id", conversation_id).eq("wallet_address", viewer).execute()
        return {"success": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_messages: mark read failed: %s", exc)
        return {"success": False, "code": "db_error", "error": "Could not update unread state."}
