"""Private one-to-one Community messaging backed by protected Supabase tables.

The browser receives usernames and avatar paths only. Wallet addresses are used
as server-side participant keys and are never returned in API payloads.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone

from community_feed import service as profile_service

logger = logging.getLogger(__name__)
MAX_MESSAGE = max(1, int(os.getenv("COMMUNITY_DM_MAX_MESSAGE", "2000")))
MAX_PAGE_SIZE = max(1, min(50, int(os.getenv("COMMUNITY_DM_PAGE_SIZE", "40"))))
MAX_ATTACHMENT_BYTES = max(1, int(os.getenv("COMMUNITY_DM_MAX_ATTACHMENT_BYTES", str(10 * 1024 * 1024))))
ATTACHMENT_UPLOAD_TTL = max(60, min(900, int(os.getenv("COMMUNITY_DM_ATTACHMENT_TTL", "600"))))
ATTACHMENT_MIME_TYPES = {
    "image/jpeg": ("image", "jpg"),
    "image/png": ("image", "png"),
    "image/gif": ("image", "gif"),
    "image/webp": ("image", "webp"),
    "application/pdf": ("document", "pdf"),
}

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


def _r2():
    from storage import r2
    return r2


def _clean_filename(value: str | None) -> str:
    name = os.path.basename(str(value or "attachment"))
    name = _CONTROL.sub("", name).strip().replace("/", "_").replace("\\", "_")
    return name[:160] or "attachment"


def _attachment_plan(mime: str | None, byte_size=None):
    clean = (mime or "").split(";", 1)[0].strip().lower()
    plan = ATTACHMENT_MIME_TYPES.get(clean)
    if not plan:
        return False, "Unsupported file type. Please upload a JPEG, PNG, GIF, WEBP or PDF file.", {}
    try:
        size = int(byte_size)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return False, "Please choose a non-empty file.", {}
    if size > MAX_ATTACHMENT_BYTES:
        return False, f"File is too large (max {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB).", {}
    return True, "", {"mime": clean, "kind": plan[0], "ext": plan[1], "bytes": size}


def _magic_matches(mime: str, head: bytes) -> bool:
    if not head:
        return False
    if mime == "application/pdf":
        return head.startswith(b"%PDF-")
    try:
        return _r2().sniff_mime(head) == mime
    except Exception:  # noqa: BLE001
        return False


def _verify_r2_attachment(key: str, plan: dict) -> tuple[bool, str, dict]:
    r2 = _r2()
    if not r2.is_managed_key(key) or not key.startswith("dm/"):
        return False, "Invalid upload reference.", {}
    head = r2.object_head(key)
    if not head or not head.get("exists"):
        return False, "The upload could not be found. Please try again.", {}
    size = head.get("bytes")
    if not isinstance(size, int) or size <= 0 or size > MAX_ATTACHMENT_BYTES:
        return False, "The uploaded file size is invalid.", {}
    magic = r2.read_prefix(key, 32)
    if not _magic_matches(plan["mime"], magic):
        r2.delete_object(key)
        return False, "That file does not match its declared type.", {}
    return True, "", {"bytes": size, "mime": plan["mime"], "kind": plan["kind"]}


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


def presign_attachment(viewer_wallet: str, conversation_id: str,
                       filename: str, mime: str, byte_size) -> dict:
    db = _db()
    viewer = _wallet(viewer_wallet)
    if not db or not viewer:
        return {"success": False, "code": "db_unavailable", "error": "Messaging is temporarily unavailable."}
    ok, error, plan = _attachment_plan(mime, byte_size)
    if not ok:
        return {"success": False, "code": "attachment_type", "error": error}
    try:
        if not _authorized_participant(db, conversation_id, viewer):
            return {"success": False, "code": "forbidden", "error": "Conversation not found."}
        r2 = _r2()
        if not r2.credentials_configured():
            return {"success": False, "code": "storage_unconfigured",
                    "error": "File uploads are temporarily unavailable."}
        upload_id = str(uuid.uuid4())
        key = r2.build_key(f"{conversation_id}:{viewer}:{upload_id}", plan["ext"], prefix="dm")
        signed = r2.presign_put(key, plan["mime"], max_bytes=MAX_ATTACHMENT_BYTES,
                                ttl=ATTACHMENT_UPLOAD_TTL)
        if not signed:
            return {"success": False, "code": "storage_error",
                    "error": "Could not prepare the upload. Please try again."}
        db.table("community_dm_upload_reservations").insert({
            "upload_id": upload_id,
            "conversation_id": conversation_id,
            "uploader_wallet": viewer,
            "r2_key": key,
            "original_name": _clean_filename(filename),
            "mime_type": plan["mime"],
            "byte_size": plan["bytes"],
            "expires_at": datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + ATTACHMENT_UPLOAD_TTL,
                tz=timezone.utc).isoformat(),
        }).execute()
        return {"success": True, "upload_id": upload_id, "key": key,
                "upload_url": signed["url"], "method": signed["method"],
                "headers": signed["headers"], "expires_in": signed["expires_in"],
                "name": _clean_filename(filename), "mime": plan["mime"],
                "kind": plan["kind"], "max_bytes": MAX_ATTACHMENT_BYTES}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_messages: attachment presign failed: %s", exc)
        return {"success": False, "code": "db_error", "error": "Could not prepare the upload."}


def _verified_attachment(viewer_wallet: str, conversation_id: str,
                         descriptor: dict) -> tuple[bool, str, dict]:
    if not isinstance(descriptor, dict):
        return False, "Invalid attachment.", {}
    upload_id = str(descriptor.get("upload_id") or "").strip()
    if not upload_id:
        return False, "The attachment upload is missing.", {}
    db = _db()
    viewer = _wallet(viewer_wallet)
    try:
        rows = _rows(db.table("community_dm_upload_reservations")
                     .select("upload_id,conversation_id,uploader_wallet,r2_key,original_name,mime_type,byte_size,expires_at,consumed_at")
                     .eq("upload_id", upload_id).limit(1).execute())
        reservation = rows[0] if rows else None
        if not reservation or reservation.get("conversation_id") != conversation_id \
                or _wallet(reservation.get("uploader_wallet")) != viewer:
            return False, "Invalid attachment upload.", {}
        if reservation.get("consumed_at"):
            return False, "This attachment upload was already used.", {}
        expires = str(reservation.get("expires_at") or "")
        if expires:
            try:
                expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if expiry <= datetime.now(timezone.utc):
                    return False, "This attachment upload expired. Please try again.", {}
            except ValueError:
                return False, "Invalid attachment upload.", {}
        ok, error, plan = _attachment_plan(reservation.get("mime_type"), reservation.get("byte_size"))
        if not ok:
            return False, error, {}
        ok, error, verified = _verify_r2_attachment(reservation.get("r2_key") or "", plan)
        if not ok:
            return False, error, {}
        return True, "", {"upload_id": upload_id, "key": reservation["r2_key"],
                           "name": reservation["original_name"], "mime": verified["mime"],
                           "kind": verified["kind"], "bytes": verified["bytes"]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_messages: attachment verify failed: %s", exc)
        return False, "The attachment could not be verified.", {}


def _attachments_for_messages(db, message_ids: list[str]) -> dict:
    if not message_ids:
        return {}
    try:
        rows = _rows(db.table("community_dm_attachments")
                     .select("id,message_id,original_name,mime_type,byte_size,attachment_kind")
                     .in_("message_id", message_ids).is_("deleted_at", "null").execute())
        return {str(row.get("message_id")): row for row in rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_messages: attachment metadata unavailable: %s", exc)
        return {}


def _public_attachment(row: dict | None) -> dict | None:
    if not row:
        return None
    return {"id": row.get("id"), "name": row.get("original_name") or "attachment",
            "mime": row.get("mime_type") or "application/octet-stream",
            "bytes": row.get("byte_size") or 0, "kind": row.get("attachment_kind") or "document",
            "download_url": f"/api/messages/attachments/{row.get('id')}/download"}


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
        attachments = _attachments_for_messages(db, [str(m.get("id")) for m in messages])
        return {"success": True, "conversation": {"id": conversation_id, **person},
                "messages": [{"id": m.get("id"), "body": m.get("body") or "", "created_at": m.get("created_at"),
                              "is_me": _wallet(m.get("sender_wallet")) == viewer,
                              "attachment": _public_attachment(attachments.get(str(m.get("id"))))}
                         for m in messages]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_messages: thread failed: %s", exc)
        return {"success": False, "code": "db_error", "error": "Could not load conversation."}


def send_message(viewer_wallet: str, conversation_id: str, body: str,
                 client_message_id: str | None = None,
                 attachment: dict | None = None) -> dict:
    db = _db()
    viewer = _wallet(viewer_wallet)
    text = normalize_body(body)
    verified_attachment = None
    if attachment is not None:
        ok, error, verified_attachment = _verified_attachment(viewer, conversation_id, attachment)
        if not ok:
            return {"success": False, "code": "attachment_invalid", "error": error}
    if not text and not verified_attachment:
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
        attachment_row = None
        stored_attachment = None
        if verified_attachment:
            attachment_row = {
                "message_id": message.get("id"),
                "conversation_id": conversation_id,
                "r2_key": verified_attachment["key"],
                "original_name": verified_attachment["name"],
                "mime_type": verified_attachment["mime"],
                "byte_size": verified_attachment["bytes"],
                "attachment_kind": verified_attachment["kind"],
            }
            stored_attachment = (_rows(db.table("community_dm_attachments")
                                   .insert(attachment_row).execute()) or [None])[0]
            if not stored_attachment:
                _r2().delete_object(verified_attachment["key"])
                db.table("community_dm_messages").delete().eq("id", message.get("id")).execute()
                return {"success": False, "code": "attachment_store_failed",
                        "error": "The file could not be attached. Please try again."}
            db.table("community_dm_upload_reservations").update({
                "consumed_at": datetime.now(timezone.utc).isoformat()
            }).eq("upload_id", verified_attachment["upload_id"]).execute()
        now = datetime.now(timezone.utc).isoformat()
        preview = text[:160] if text else "Attachment"
        db.table("community_dm_conversations").update({"last_message_at": now, "last_message_preview": preview}).eq("id", conversation_id).execute()
        return {"success": True, "message": {"id": message.get("id"), "body": text,
                "created_at": message.get("created_at") or now, "is_me": True,
                "attachment": _public_attachment(stored_attachment)}}
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


def attachment_download(viewer_wallet: str, attachment_id: str) -> dict:
    db = _db()
    viewer = _wallet(viewer_wallet)
    if not db or not viewer:
        return {"success": False, "code": "db_unavailable", "error": "Messaging is temporarily unavailable."}
    try:
        rows = _rows(db.table("community_dm_attachments")
                     .select("id,conversation_id,r2_key,original_name,mime_type,byte_size,attachment_kind")
                     .eq("id", attachment_id).is_("deleted_at", "null").limit(1).execute())
        row = rows[0] if rows else None
        if not row or not _authorized_participant(db, row.get("conversation_id"), viewer):
            return {"success": False, "code": "forbidden", "error": "Attachment not found."}
        key = row.get("r2_key") or ""
        if not key.startswith("dm/"):
            return {"success": False, "code": "not_found", "error": "Attachment not found."}
        url = _r2().presign_get(key, ttl=300)
        if not url:
            return {"success": False, "code": "storage_error", "error": "File download is temporarily unavailable."}
        return {"success": True, "url": url, "name": row.get("original_name"),
                "mime": row.get("mime_type"), "bytes": row.get("byte_size")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_messages: attachment download failed: %s", exc)
        return {"success": False, "code": "db_error", "error": "Could not open the attachment."}


def cancel_attachment(viewer_wallet: str, upload_id: str) -> dict:
    db = _db()
    viewer = _wallet(viewer_wallet)
    if not db or not viewer:
        return {"success": False, "code": "db_unavailable", "error": "Messaging is temporarily unavailable."}
    try:
        rows = _rows(db.table("community_dm_upload_reservations")
                     .select("upload_id,r2_key,uploader_wallet,consumed_at")
                     .eq("upload_id", upload_id).limit(1).execute())
        row = rows[0] if rows else None
        if not row or _wallet(row.get("uploader_wallet")) != viewer:
            return {"success": False, "code": "forbidden", "error": "Upload not found."}
        if row.get("consumed_at"):
            return {"success": False, "code": "already_used", "error": "Attachment is already part of a message."}
        _r2().delete_object(row.get("r2_key") or "")
        db.table("community_dm_upload_reservations").delete().eq("upload_id", upload_id).execute()
        return {"success": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_messages: attachment cancel failed: %s", exc)
        return {"success": False, "code": "db_error", "error": "Could not cancel the upload."}
