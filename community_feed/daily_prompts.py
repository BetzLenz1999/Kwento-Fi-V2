"""Community Daily Prompt lifecycle and read/write helpers.

Routes remain responsible for admin authorization, audit logging, and deriving
``created_by``/viewer wallet identities from the server-side session. This module
is dependency-light and accepts an injected Supabase-compatible client for tests.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

PROMPT_TABLE = "community_daily_prompts"
POST_TABLE = "community_feed_posts"
FEATURE_NAME = "community_daily_prompt_feature"
VALID_STATUSES = frozenset({"draft", "scheduled", "active", "archived", "disabled"})
VALID_THEMES = frozenset({
    "reflection", "helpful_tip", "build_in_public", "community_question", "celebration"
})
MAX_TITLE = 80
MAX_BODY = 240
MAX_PLACEHOLDER = 120
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029]")
_SENSITIVE_REQUEST = re.compile(
    r"\b(seed\s+phrase|private\s+key|password|passphrase|phone\s+number|"
    r"exact\s+(?:financial\s+)?balance|bank\s+account\s+number)\b",
    re.IGNORECASE,
)
_SAFE_COVER_KEYS = frozenset({"sparkles", "lightbulb", "heart", "wrench", "party", "leaf"})
_SAFE_COVER_EMOJI = frozenset({"✨", "💡", "❤️", "🛠️", "🎉", "🌱", "🌟", "🤝"})


def _db(client=None):
    if client is not None:
        return client
    try:
        from supabase_client import get_supabase_admin_client, get_supabase_client

        return get_supabase_admin_client() or get_supabase_client()
    except Exception as exc:  # keep imports dependency-light and fail closed
        logger.warning("Daily Prompt database unavailable: %s", exc)
        return None


def _rows(result) -> list[dict[str, Any]]:
    if not result:
        return []
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    return list(data or [])


def _utc_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_prompt_date(value: date | datetime | str | None = None) -> str:
    """Return an ISO date in UTC; naive datetimes are interpreted as UTC."""
    if value is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(value, datetime):
        return _utc_now(value).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def _clean_text(value: Any, field: str, limit: int, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    if required and not cleaned:
        raise ValueError(f"{field} is required")
    if len(cleaned) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return cleaned or None


def _parse_timestamp(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO timestamp") from exc
    else:
        raise ValueError(f"{field} must be an ISO timestamp")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_prompt(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """Validate and normalize prompt fields, rejecting unknown/server-owned keys."""
    if not isinstance(payload, dict):
        raise ValueError("prompt must be an object")
    allowed = {
        "prompt_date", "title", "body", "composer_placeholder", "theme",
        "cover_emoji", "status", "starts_at", "ends_at",
    }
    extra = set(payload) - allowed
    if extra:
        raise ValueError(f"unsupported prompt fields: {', '.join(sorted(extra))}")
    normalized: dict[str, Any] = {}
    required_fields = {"prompt_date", "title", "body", "starts_at", "ends_at"}
    for key in ("prompt_date", "title", "body", "composer_placeholder", "theme", "cover_emoji", "status", "starts_at", "ends_at"):
        if key not in payload:
            if not partial and key in required_fields:
                raise ValueError(f"{key} is required")
            continue
        value = payload[key]
        if key == "prompt_date":
            try:
                normalized[key] = utc_prompt_date(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("prompt_date must be a valid YYYY-MM-DD date") from exc
        elif key == "title":
            normalized[key] = _clean_text(value, key, MAX_TITLE, required=True)
        elif key == "body":
            normalized[key] = _clean_text(value, key, MAX_BODY, required=True)
        elif key == "composer_placeholder":
            normalized[key] = _clean_text(value, key, MAX_PLACEHOLDER, required=False)
        elif key == "theme":
            theme = _clean_text(value, key, 40, required=True)
            if theme not in VALID_THEMES:
                raise ValueError("theme is not supported")
            normalized[key] = theme
        elif key == "cover_emoji":
            cover = _clean_text(value, key, 24, required=False)
            if cover and cover not in _SAFE_COVER_KEYS and cover not in _SAFE_COVER_EMOJI:
                raise ValueError("cover_emoji must be a safe server-selected icon")
            normalized[key] = cover
        elif key == "status":
            status = _clean_text(value, key, 16, required=True)
            if status not in VALID_STATUSES:
                raise ValueError("status is not supported")
            normalized[key] = status
        elif key in {"starts_at", "ends_at"}:
            normalized[key] = _parse_timestamp(value, key).isoformat()
    if not partial:
        normalized.setdefault("composer_placeholder", None)
        normalized.setdefault("theme", "reflection")
        normalized.setdefault("status", "draft")
        start = _parse_timestamp(normalized["starts_at"], "starts_at")
        end = _parse_timestamp(normalized["ends_at"], "ends_at")
        if end <= start:
            raise ValueError("ends_at must be later than starts_at")
    elif "starts_at" in normalized and "ends_at" in normalized:
        if _parse_timestamp(normalized["ends_at"], "ends_at") <= _parse_timestamp(normalized["starts_at"], "starts_at"):
            raise ValueError("ends_at must be later than starts_at")
    title = normalized.get("title")
    body = normalized.get("body")
    if title is not None and _SENSITIVE_REQUEST.search(title) or body is not None and _SENSITIVE_REQUEST.search(body):
        raise ValueError("prompt must not request sensitive personal or financial information")
    return normalized


def is_valid_active_window(row: dict[str, Any], *, now: datetime | None = None, day: date | str | None = None) -> bool:
    """Pure predicate for today's prompt once its active/scheduled window is live."""
    try:
        current = _utc_now(now)
        prompt_day = utc_prompt_date(day if day is not None else current)
        return (
            row.get("status") in {"active", "scheduled"}
            and utc_prompt_date(row.get("prompt_date")) == prompt_day
            and _parse_timestamp(row.get("starts_at"), "starts_at") <= current
            and _parse_timestamp(row.get("ends_at"), "ends_at") > current
        )
    except (TypeError, ValueError, OverflowError):
        return False


def public_prompt(row: dict[str, Any], *, response_count: int = 0, has_responded: bool = False) -> dict[str, Any]:
    """Shape only approved public fields; in particular never return created_by."""
    return {
        "id": row.get("id"),
        "prompt_date": utc_prompt_date(row.get("prompt_date")),
        "title": row.get("title"),
        "body": row.get("body"),
        "composer_placeholder": row.get("composer_placeholder"),
        "theme": row.get("theme") or "reflection",
        "cover_emoji": row.get("cover_emoji"),
        "response_count": max(0, int(response_count)),
        "has_responded": bool(has_responded),
    }


def is_enabled(*, client=None) -> bool:
    """Daily Prompt fail-closed feature gate, with a deployment kill switch."""
    if os.getenv("COMMUNITY_DAILY_PROMPTS_ENABLED", "1").strip() == "0":
        return False
    db = _db(client)
    if db is None:
        return False
    try:
        result = (db.table("maintenance_settings").select("is_maintenance")
                  .eq("feature_name", FEATURE_NAME).limit(1).execute())
        rows = _rows(result)
        if not rows:
            return False
        value = rows[0].get("is_maintenance", True)
        return value is False or value == 0 or value == "false" or value == "f"
    except Exception as exc:
        logger.warning("Daily Prompt feature flag read failed: %s", exc)
        return False


def _visible_response_count(db, prompt_id: Any) -> int:
    query = (db.table(POST_TABLE).select("id", count="exact")
             .eq("prompt_id", prompt_id).eq("visibility", "public")
             .eq("is_hidden", False).eq("is_deleted", False))
    result = query.execute()
    count = getattr(result, "count", None)
    return max(0, int(count if count is not None else len(_rows(result))))


def filter_prompt_posts(query, prompt_id: Any):
    """Add the safe public response predicates to an existing feed query.

    The caller can then reuse the Community feed's normal pagination and post
    serializer instead of introducing a separate prompt-post type.
    """
    if prompt_id is None or not str(prompt_id).strip():
        raise ValueError("prompt_id is required")
    return (query.eq("prompt_id", prompt_id).eq("visibility", "public")
            .eq("is_hidden", False).eq("is_deleted", False))


def _wallet_has_response(db, prompt_id: Any, wallet: str | None) -> bool:
    wallet = (wallet or "").strip()
    if not wallet:
        return False
    result = (db.table(POST_TABLE).select("id").eq("prompt_id", prompt_id)
              .ilike("wallet_address", wallet).limit(1).execute())
    return bool(_rows(result))


def get_active_prompt(viewer: str | None = None, *, client=None, now: datetime | None = None) -> dict[str, Any]:
    """Return today's single valid prompt or ``prompt: None``; failures are non-fatal."""
    current = _utc_now(now)
    day = current.date().isoformat()
    try:
        db = _db(client)
        if db is None or not is_enabled(client=db):
            return {"success": False, "prompt": None, "status": "unavailable"}
        result = (db.table(PROMPT_TABLE).select("*").eq("prompt_date", day)
                  .eq("status", "active").lte("starts_at", current.isoformat())
                  .gt("ends_at", current.isoformat()).limit(2).execute())
        candidates = [row for row in _rows(result) if is_valid_active_window(row, now=current, day=day)]
        if not candidates:
            scheduled_result = (db.table(PROMPT_TABLE).select("*").eq("prompt_date", day)
                                .eq("status", "scheduled").lte("starts_at", current.isoformat())
                                .gt("ends_at", current.isoformat()).limit(2).execute())
            candidates = [row for row in _rows(scheduled_result)
                          if is_valid_active_window(row, now=current, day=day)]
            if candidates:
                row = candidates[0]
                try:
                    db.table(PROMPT_TABLE).update({"status": "active"}).eq(
                        "id", row.get("id")).eq("status", "scheduled").execute()
                    row["status"] = "active"
                except Exception as exc:
                    # A failed promotion does not hide a prompt whose approved
                    # time window is live; future reads can retry the transition.
                    logger.warning("Daily Prompt scheduled activation deferred: %s", exc)
        if len(candidates) > 1:
            logger.error("Multiple active Daily Prompts found for %s", day)
            return {"success": False, "prompt": None, "status": "unavailable"}
        if not candidates:
            return {"success": True, "prompt": None}
        row = candidates[0]
        count = _visible_response_count(db, row.get("id"))
        responded = _wallet_has_response(db, row.get("id"), viewer)
        return {"success": True, "prompt": public_prompt(row, response_count=count, has_responded=responded)}
    except Exception as exc:
        logger.warning("Daily Prompt lookup unavailable for %s: %s", day, exc)
        return {"success": False, "prompt": None, "status": "unavailable"}


def create_prompt(data: dict[str, Any], actor: str, *, client=None) -> dict[str, Any]:
    """Insert a normalized prompt; caller must enforce admin authorization/audit."""
    try:
        creator = (actor or "").strip()
        if not creator:
            return {"success": False, "error": "server-side creator identity required"}
        db = _db(client)
        if db is None:
            return {"success": False, "error": "Daily Prompt storage unavailable"}
        row = validate_prompt(data)
        row["created_by"] = creator
        result = db.table(PROMPT_TABLE).insert(row).select("*").execute()
        rows = _rows(result)
        return {"success": True, "prompt": public_prompt(rows[0] if rows else row)}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("Daily Prompt create failed: %s", exc)
        return {"success": False, "error": "Daily Prompt could not be saved"}


def update_prompt(prompt_id: Any, data: dict[str, Any], actor: str, *, client=None) -> dict[str, Any]:
    """Update explicitly supplied editable fields; caller owns auth and audit."""
    try:
        if not (actor or "").strip():
            return {"success": False, "error": "server-side actor identity required"}
        updates = validate_prompt(data, partial=True)
        if not updates:
            return {"success": False, "error": "no prompt fields supplied"}
        db = _db(client)
        if db is None:
            return {"success": False, "error": "Daily Prompt storage unavailable"}
        result = db.table(PROMPT_TABLE).update(updates).eq("id", prompt_id).select("*").execute()
        rows = _rows(result)
        if not rows:
            return {"success": False, "error": "prompt not found"}
        return {"success": True, "prompt": public_prompt(rows[0])}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("Daily Prompt update failed: %s", exc)
        return {"success": False, "error": "Daily Prompt could not be updated"}


def archive_prompt(prompt_id: Any, actor: str, *, client=None) -> dict[str, Any]:
    """Archive rather than delete a prompt; caller must authorize and audit."""
    return update_prompt(prompt_id, {"status": "archived"}, actor, client=client)


def validate_prompt_response(prompt_id: Any, wallet: str | None, *, client=None, now: datetime | None = None) -> dict[str, Any]:
    """Server-side response preflight; the database unique index closes races."""
    identity = (wallet or "").strip()
    if not identity:
        return {"success": False, "error": "authentication required"}
    current = _utc_now(now)
    try:
        db = _db(client)
        if db is None or not is_enabled(client=db):
            return {"success": False, "error": "Daily Prompt is unavailable"}
        result = db.table(PROMPT_TABLE).select("*").eq("id", prompt_id).limit(1).execute()
        rows = _rows(result)
        if not rows or not is_valid_active_window(rows[0], now=current):
            return {"success": False, "error": "prompt is not accepting responses"}
        if _wallet_has_response(db, prompt_id, identity):
            return {"success": False, "error": "one official response per prompt is allowed"}
        return {"success": True, "prompt_id": prompt_id}
    except Exception as exc:
        logger.warning("Daily Prompt response validation failed: %s", exc)
        return {"success": False, "error": "Daily Prompt is unavailable"}


def list_prompts(*, prompt_date: str | None = None, status: str | None = None,
                 limit: int = 30, client=None) -> dict[str, Any]:
    """Bounded admin listing; creator wallets and internal row fields are omitted."""
    db = _db(client)
    if db is None:
        return {"success": False, "error": "Daily Prompt storage unavailable", "prompts": []}
    try:
        size = max(1, min(100, int(limit)))
        query = db.table(PROMPT_TABLE).select(
            "id,prompt_date,title,body,composer_placeholder,theme,cover_emoji,status,starts_at,ends_at,created_at,updated_at"
        )
        if prompt_date:
            query = query.eq("prompt_date", utc_prompt_date(prompt_date))
        if status:
            if status not in VALID_STATUSES:
                return {"success": False, "error": "status is not supported", "prompts": []}
            query = query.eq("status", status)
        result = query.order("prompt_date", desc=True).limit(size).execute()
        return {"success": True, "prompts": _rows(result)}
    except Exception as exc:
        logger.warning("Daily Prompt admin list failed: %s", exc)
        return {"success": False, "error": "Daily Prompt list is unavailable", "prompts": []}
