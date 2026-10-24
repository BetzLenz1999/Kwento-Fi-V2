"""Atomic Community Daily Learn attempt persistence and scoring.

The database RPCs are the trust boundary: they read only today's published,
immutable snapshot, serialize concurrent answers per normalized wallet/day, and
compute correctness/score from that snapshot. This module never writes rewards
or badges; callers may use ``completed_new`` after this persisted result to run
those existing integrations exactly once.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)
ATTEMPT_TABLE = "community_daily_learn_attempts"
SUBMIT_RPC = "community_daily_learn_submit_answer"
GET_RPC = "community_daily_learn_get_attempt"
_UNAVAILABLE = {
    "success": False,
    "status": "unavailable",
    "message": "Daily Learn is temporarily unavailable.",
}


def _db(client=None):
    """Get a service-role client; anon access is intentionally insufficient."""
    if client is not None:
        return client
    try:
        from supabase_client import get_supabase_admin_client
        return get_supabase_admin_client()
    except Exception as exc:  # noqa: BLE001 - persistence fails closed
        logger.warning("Daily Learn attempt DB client unavailable: %s", exc)
        return None


def _feature_enabled() -> bool:
    """Honor the Community-specific kill switch, failing closed on uncertainty."""
    try:
        from .daily_learn import _community_enabled
        enabled, _message = _community_enabled()
        return bool(enabled)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("Daily Learn attempt feature flag unavailable: %s", exc)
        return False


def _wallet(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value or None


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _current_day(value: Any) -> str | None:
    """Return canonical YYYY-MM-DD only when the supplied day is today in UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    canonical = parsed.isoformat()
    return canonical if value == canonical and canonical == _today_utc() else None


def _question_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _answer_index(value: Any) -> int | None:
    """Normalize A-D or the snapshot's zero-based option index to 0..3."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 3 else None
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in {"A", "B", "C", "D"}:
            return ord(normalized) - ord("A")
        if normalized in {"0", "1", "2", "3"}:
            return int(normalized)
    return None


def _payload(result) -> dict[str, Any] | None:
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return None
    return data if isinstance(data, dict) else None


def _public_result(payload: dict[str, Any], *, submitted: bool) -> dict[str, Any]:
    """Allowlist response fields so answer keys or internal DB values cannot leak."""
    output = {
        key: payload[key]
        for key in (
            "success", "status", "day_utc", "score", "completed_questions",
            "total_questions", "next_question_available", "completed_new", "code",
        )
        if key in payload
    }
    if submitted:
        for key in ("correct", "question_id", "explanation"):
            if key in payload:
                output[key] = payload[key]
        if payload.get("completed_new") is True and isinstance(payload.get("completion"), dict):
            completion = payload["completion"]
            output["completion"] = {
                key: completion[key]
                for key in ("day_utc", "score", "total_questions")
                if key in completion
            }
    if not output.get("success"):
        output.setdefault("status", "unavailable")
        output.setdefault("message", _UNAVAILABLE["message"])
    return output


def _invalid_day() -> dict[str, Any]:
    return {"success": False, "status": "expired", "code": "day_not_current",
            "message": "Daily Learn answers are accepted only for today's UTC cycle."}


def submit_answer(wallet: str, day_utc: str, question_id: str, answer: Any, *, client=None) -> dict[str, Any]:
    """Persist one answer for today's expected question and return its result.

    The explicit ``day_utc`` is a consistency guard for the route and must equal
    today's UTC date. The database checks it again against its own UTC clock.
    Score, reward, and correctness are not accepted as inputs. An identical
    retry is successful but has ``completed_new=False``; a changed answer or
    out-of-order question is rejected without mutation.
    """
    wallet_address = _wallet(wallet)
    qid = _question_id(question_id)
    index = _answer_index(answer)
    if not wallet_address or not qid or index is None:
        return {"success": False, "status": "rejected", "code": "invalid_request",
                "message": "A wallet, question, and valid answer are required."}
    day = _current_day(day_utc)
    if day is None:
        return _invalid_day()
    if not _feature_enabled():
        return dict(_UNAVAILABLE)
    db = _db(client)
    if db is None:
        return dict(_UNAVAILABLE)
    try:
        result = db.rpc(SUBMIT_RPC, {
            "p_wallet_address": wallet_address,
            "p_day_utc": day,
            "p_question_id": qid,
            "p_answer": index,
        }).execute()
        payload = _payload(result)
        if payload is None:
            return dict(_UNAVAILABLE)
        return _public_result(payload, submitted=True)
    except Exception as exc:  # noqa: BLE001 - no answer accepted without DB commit
        logger.warning("Daily Learn answer persistence unavailable: %s", exc)
        return dict(_UNAVAILABLE)


def get_attempt(wallet: str, day_utc: str, *, client=None) -> dict[str, Any]:
    """Recover today's sanitized server-side progress without answer keys."""
    wallet_address = _wallet(wallet)
    if not wallet_address:
        return {"success": False, "status": "rejected", "code": "invalid_wallet",
                "message": "A wallet is required."}
    day = _current_day(day_utc)
    if day is None:
        return _invalid_day()
    if not _feature_enabled():
        return dict(_UNAVAILABLE)
    db = _db(client)
    if db is None:
        return dict(_UNAVAILABLE)
    try:
        result = db.rpc(GET_RPC, {
            "p_wallet_address": wallet_address,
            "p_day_utc": day,
        }).execute()
        payload = _payload(result)
        if payload is None:
            return dict(_UNAVAILABLE)
        return _public_result(payload, submitted=False)
    except Exception as exc:  # noqa: BLE001 - fail closed on persistence errors
        logger.warning("Daily Learn attempt recovery unavailable: %s", exc)
        return dict(_UNAVAILABLE)
