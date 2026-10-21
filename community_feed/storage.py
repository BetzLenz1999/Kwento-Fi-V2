"""Community Feed — media storage glue.

Thin layer between the feed's business rules and ``storage/r2.py``:

    plan_upload()   -> what the client is allowed to send (MIME + size)
    sign_upload()   -> a presigned PUT the browser uses to upload straight to R2
    verify_upload() -> server-side confirmation the object really landed

The browser uploads directly to R2, so ``verify_upload`` is the trust boundary:
it HEADs the object for its real size and sniffs the magic bytes so a file that
claims to be a JPEG but is actually an HTML document is refused.
"""

from __future__ import annotations

import logging

from . import service as svc

logger = logging.getLogger(__name__)


def _r2():
    from storage import r2

    return r2


def storage_available() -> bool:
    """True when R2 is fully configured, so the composer can say so honestly."""
    try:
        return _r2().is_configured()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: storage check failed: %s", exc)
        return False


def sign_upload(owner_wallet: str, content_type: str | None,
                declared_bytes: int | None = None) -> dict:
    """Validate an upload request and return a presigned PUT.

    Returns ``{success, upload_url, method, headers, key, public_url, expires_in,
    media_type, max_bytes}`` or ``{success: False, error}``. Never raises.
    """
    ok, error, plan = svc.plan_upload(content_type, declared_bytes)
    if not ok:
        return {"success": False, "error": error, "code": "upload_type"}

    if not storage_available():
        return {
            "success": False,
            "code": "storage_unconfigured",
            "error": "Media uploads are not configured yet.",
        }

    r2 = _r2()
    key = r2.build_key(owner_wallet, plan["ext"])
    signed = r2.presign_put(key, plan["mime"], max_bytes=plan["max_bytes"])
    if not signed:
        return {
            "success": False,
            "code": "storage_error",
            "error": "Could not prepare the upload. Please try again.",
        }

    return {
        "success": True,
        "upload_url": signed["url"],
        "method": signed["method"],
        "headers": signed["headers"],
        "key": key,
        "public_url": signed["public_url"],
        "expires_in": signed["expires_in"],
        "media_type": plan["media_type"],
        "mime": plan["mime"],
        "max_bytes": plan["max_bytes"],
    }


def sign_avatar_upload(owner_wallet: str, content_type: str | None,
                       declared_bytes: int | None = None) -> dict:
    """Presigned PUT for a PROFILE PICTURE (images only, tighter size cap).

    Same shape as ``sign_upload`` but plans through ``plan_avatar_upload`` so a
    video (or an oversized image) is refused before any signature exists. The
    key is built the same way, so ``verify_avatar`` can re-check ownership.
    """
    ok, error, plan = svc.plan_avatar_upload(content_type, declared_bytes)
    if not ok:
        return {"success": False, "error": error, "code": "upload_type"}

    if not storage_available():
        return {
            "success": False,
            "code": "storage_unconfigured",
            "error": "Profile picture uploads are not configured yet.",
        }

    r2 = _r2()
    key = r2.build_key(owner_wallet, plan["ext"])
    signed = r2.presign_put(key, plan["mime"], max_bytes=plan["max_bytes"])
    if not signed:
        return {
            "success": False,
            "code": "storage_error",
            "error": "Could not prepare the upload. Please try again.",
        }

    return {
        "success": True,
        "upload_url": signed["url"],
        "method": signed["method"],
        "headers": signed["headers"],
        "key": key,
        "public_url": signed["public_url"],
        "expires_in": signed["expires_in"],
        "media_type": plan["media_type"],
        "mime": plan["mime"],
        "max_bytes": plan["max_bytes"],
    }


def verify_upload(owner_wallet: str, item: dict) -> tuple[bool, str, dict]:
    """Confirm an uploaded object before a post may reference it.

    ``item`` is the client's descriptor: ``{key, media_type, mime, width, height,
    duration, poster_url}``. The key must be one WE generated (``is_managed_key``
    plus the owner's own prefix), so a client cannot reference — or later delete —
    another user's object.
    """
    if not isinstance(item, dict):
        return False, "Invalid media descriptor.", {}

    key = (item.get("key") or "").strip()
    r2 = _r2()
    if not r2.is_managed_key(key):
        return False, "Invalid upload reference.", {}

    # Ownership check: the key embeds a hash of the owner's wallet, so a client
    # cannot point at someone else's file even with a valid key.
    if not key.startswith(f"u/{_owner_hash(owner_wallet)}/"):
        return False, "That upload does not belong to you.", {}

    declared_type = item.get("media_type")
    mime = (item.get("mime") or "").split(";")[0].strip().lower()
    ok, error, plan = svc.plan_upload(mime)
    if not ok:
        return False, error, {}
    if declared_type and declared_type != plan["media_type"]:
        return False, "Invalid media type.", {}

    head = r2.object_head(key)
    magic = r2.read_prefix(key, 32) if head else b""
    ok, error, verified = svc.verify_uploaded_object(plan, head, magic)
    if not ok:
        # The object exists but is unacceptable — clean it up so a rejected
        # upload does not sit in the bucket forever.
        if head:
            r2.delete_object(key)
        return False, error, {}

    return True, "", {
        "key": key,
        "public_url": r2.public_url(key),
        "media_type": verified["media_type"],
        "mime": verified.get("mime") or plan["mime"],
        "bytes": verified["bytes"],
        "width": _int_or_none(item.get("width")),
        "height": _int_or_none(item.get("height")),
        "duration": _float_or_none(item.get("duration")),
        "poster_url": _safe_poster(item.get("poster_url")),
    }


def verify_avatar(owner_wallet: str, item: dict) -> tuple[bool, str, dict]:
    """Confirm an uploaded PROFILE PICTURE before it may be stored.

    ``item`` is the client's descriptor: ``{key, mime, width, height}``. The key
    must be one WE generated AND under the owner's own prefix, so a member
    cannot point at (or later delete) another member's object. The bytes are
    re-sniffed, so an HTML document uploaded as ``image/jpeg`` is refused.
    """
    if not isinstance(item, dict):
        return False, "Invalid image descriptor.", {}

    key = (item.get("key") or "").strip()
    r2 = _r2()
    if not r2.is_managed_key(key):
        return False, "Invalid upload reference.", {}

    if not key.startswith(f"u/{_owner_hash(owner_wallet)}/"):
        return False, "That upload does not belong to you.", {}

    mime = (item.get("mime") or "").split(";")[0].strip().lower()
    ok, error, plan = svc.plan_avatar_upload(mime)
    if not ok:
        return False, error, {}

    head = r2.object_head(key)
    magic = r2.read_prefix(key, 32) if head else b""
    ok, error, verified = svc.verify_uploaded_object(plan, head, magic)
    if not ok:
        # Rejected upload: clean it up so a bad picture does not sit in R2.
        if head:
            r2.delete_object(key)
        return False, error, {}

    return True, "", {
        "key": key,
        "public_url": r2.public_url(key),
        "mime": verified.get("mime") or plan["mime"],
        "bytes": verified["bytes"],
    }


def _owner_hash(wallet: str) -> str:
    import hashlib

    return hashlib.sha256((wallet or "anon").encode("utf-8")).hexdigest()[:16]


def _int_or_none(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _float_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _safe_poster(url) -> str | None:
    """A poster frame must be one of our own public URLs, never a client URL."""
    url = (url or "").strip()
    if not url:
        return None
    try:
        r2 = _r2()
    except Exception:  # noqa: BLE001
        return None
    base = r2.public_base_url()
    if base and url.startswith(base + "/"):
        return url
    return None
