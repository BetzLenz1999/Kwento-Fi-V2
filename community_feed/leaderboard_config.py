"""Durable, versioned weekly reward configuration facade.

The SQL owner (``weekly_exp_*_reward_config`` RPCs) is the only supported
read/write API. There is intentionally no environment or Python default for the
reward economics: a missing migration or a malformed RPC response must surface
to the operator instead of silently changing what a period pays out.

This module never scores, allocates, finalizes, funds or publishes anything. It
only schedules, locks and voids the admin's per-week rule set.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

RPC_GET = "weekly_exp_get_reward_config"
RPC_SAVE = "weekly_exp_save_reward_config"
RPC_LOCK = "weekly_exp_lock_reward_config"
RPC_VOID = "weekly_exp_void_reward_config"

DISTRIBUTIONS = frozenset({"equal_pool", "fixed_by_rank"})
TIE_POLICIES = frozenset({"deterministic_activity", "shared_rank"})
CONFIG_STATUSES = frozenset({"draft", "scheduled", "locked", "void"})

# A mutable draft/scheduled configuration may be changed; a locked one is frozen
# for the period. Only a future week may be edited once the current one locks.
EDITABLE_STATUSES = frozenset({"draft", "scheduled"})


def _admin_client():
    try:
        from supabase_client import get_supabase_admin_client
        return get_supabase_admin_client()
    except Exception as exc:  # pragma: no cover - import/runtime dependent
        logger.warning("weekly reward config admin client unavailable: %s", exc)
        return None


def _payload(result: Any) -> dict[str, Any] | None:
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


def _failure(error: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"success": False, "error": error}
    result.update(extra)
    return result


def _monday(value: date | datetime | str) -> str | None:
    """Normalize to an ISO Monday date; return None when not a valid boundary."""
    if isinstance(value, datetime):
        day = value.date()
    elif isinstance(value, date):
        day = value
    else:
        try:
            day = date.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if day.weekday() != 0:
        return None
    return day.isoformat()


def _call(db, rpc_name: str, params: dict) -> dict:
    try:
        result = db.rpc(rpc_name, params).execute()
    except Exception as exc:  # noqa: BLE001 - surface a usable admin error
        logger.error("weekly reward config RPC %s failed: %s", rpc_name, exc)
        return _failure("Reward configuration service is temporarily unavailable",
                        code="configuration_unavailable")
    payload = _payload(result)
    if payload is None:
        return _failure("Reward configuration service returned an empty response",
                        code="configuration_unavailable")
    return payload


def get_config(week_start: date | str, supabase=None) -> dict:
    """Read the effective configuration for a period (never invents defaults)."""
    boundary = _monday(week_start)
    if boundary is None:
        return _failure("week_start must be a Monday UTC boundary",
                        code="invalid_week")
    db = supabase if supabase is not None else _admin_client()
    if db is None:
        return _failure("Reward configuration database is unavailable",
                        code="configuration_unavailable")
    return _call(db, RPC_GET, {"p_week_start": boundary})


def save_config(week_start: date | str, settings: dict, actor: str,
                reason: str = "", expected_version: int | None = None,
                supabase=None) -> dict:
    """Create or update a draft/scheduled configuration for a future period."""
    boundary = _monday(week_start)
    if boundary is None:
        return _failure("week_start must be a Monday UTC boundary",
                        code="invalid_week")
    if not isinstance(settings, dict):
        return _failure("settings must be an object", code="invalid_settings")
    if not str(actor or "").strip():
        return _failure("actor is required", code="invalid_actor")
    db = supabase if supabase is not None else _admin_client()
    if db is None:
        return _failure("Reward configuration database is unavailable",
                        code="configuration_unavailable")
    params = {
        "p_week_start": boundary,
        "p_settings": settings,
        "p_actor": str(actor).strip(),
        "p_reason": reason or "",
        "p_expected_version": expected_version,
    }
    return _call(db, RPC_SAVE, params)


def lock_config(week_start: date | str, actor: str,
                expected_version: int | None = None, supabase=None) -> dict:
    """Freeze a period's configuration so scoring/allocation uses it as-is."""
    boundary = _monday(week_start)
    if boundary is None:
        return _failure("week_start must be a Monday UTC boundary",
                        code="invalid_week")
    if not str(actor or "").strip():
        return _failure("actor is required", code="invalid_actor")
    db = supabase if supabase is not None else _admin_client()
    if db is None:
        return _failure("Reward configuration database is unavailable",
                        code="configuration_unavailable")
    return _call(db, RPC_LOCK, {
        "p_week_start": boundary,
        "p_actor": str(actor).strip(),
        "p_expected_version": expected_version,
    })


def void_config(week_start: date | str, actor: str, reason: str,
                supabase=None) -> dict:
    """Void a configuration with an audit reason; a locked period is never edited."""
    boundary = _monday(week_start)
    if boundary is None:
        return _failure("week_start must be a Monday UTC boundary",
                        code="invalid_week")
    if not str(actor or "").strip():
        return _failure("actor is required", code="invalid_actor")
    if not str(reason or "").strip():
        return _failure("a reason is required to void a configuration",
                        code="reason_required")
    db = supabase if supabase is not None else _admin_client()
    if db is None:
        return _failure("Reward configuration database is unavailable",
                        code="configuration_unavailable")
    return _call(db, RPC_VOID, {
        "p_week_start": boundary,
        "p_actor": str(actor).strip(),
        "p_reason": str(reason).strip(),
    })
