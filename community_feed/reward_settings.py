"""DB-authoritative Community reward configuration facade.

The SQL owner exposes the only supported settings read/write API.  This module
intentionally has no environment/default fallback: a missing migration or a
malformed RPC response must be visible to operators rather than silently
changing reward economics.
"""
from __future__ import annotations

import json
import logging
import math
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

RPC_GET = "community_reward_settings_get"
RPC_UPDATE = "community_reward_settings_update"
SETTING_KEYS = frozenset({
    "rates", "daily_cap", "post_cap", "daily_learn_rate",
    "daily_learn_rate_max", "daily_learn_cap", "combined_cap",
    "rate_max", "max", "min", "names", "config_version",
})
RATE_NAMES = ("comment_out", "comment_in", "reaction_out", "reaction_in")
UPDATE_KEYS = frozenset({
    "comment_out", "comment_in", "reaction_out", "reaction_in",
    "rates", "daily_cap", "post_cap", "daily_learn_rate",
    "daily_learn_rate_max", "daily_learn_cap",
})


def _admin_client():
    try:
        from supabase_client import get_supabase_admin_client
        return get_supabase_admin_client()
    except Exception as exc:  # pragma: no cover - import/runtime dependent
        logger.warning("community reward settings admin client unavailable: %s", exc)
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


def get_settings(supabase=None) -> dict:
    """Read the authoritative merged settings through the SQL RPC.

    No cache, environment value, or Python default is consulted.  The returned
    error is deliberately useful for an admin dashboard diagnosing migrations.
    """
    db = supabase if supabase is not None else _admin_client()
    if db is None:
        return _failure("Reward configuration database is unavailable",
                        code="configuration_unavailable")
    try:
        result = db.rpc(RPC_GET, {}).execute()
    except Exception as exc:  # noqa: BLE001 - fail closed at the facade boundary
        logger.warning("community reward settings read failed: %s", exc)
        return _failure("Reward configuration RPC is unavailable",
                        code="configuration_unavailable")
    payload = _payload(result)
    if payload is None:
        return _failure("Reward configuration RPC returned malformed data",
                        code="configuration_malformed")
    if payload.get("success") is False:
        return payload
    if payload.get("success") is not True:
        return _failure("Reward configuration RPC returned no success marker",
                        code="configuration_malformed")
    return payload


def _number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return number.is_finite() and number >= 0 and math.isfinite(float(number))


def _validate_patch(values: Any) -> dict | None:
    if not isinstance(values, dict) or not values:
        return {"success": False, "error": "No reward settings supplied",
                "code": "invalid_settings"}
    unknown = set(values) - UPDATE_KEYS
    if unknown:
        return {"success": False,
                "error": "Unknown reward setting(s): " + ", ".join(sorted(unknown)),
                "code": "invalid_settings"}
    patch = dict(values)
    rates = patch.get("rates")
    if rates is not None:
        if not isinstance(rates, dict):
            return {"success": False, "error": "rates must be an object",
                    "code": "invalid_settings"}
        unknown_rates = set(rates) - set(RATE_NAMES)
        if unknown_rates:
            return {"success": False, "error": "Unknown rate(s): " + ", ".join(sorted(unknown_rates)),
                    "code": "invalid_settings"}
        if any(not _number(value) for value in rates.values()):
            return {"success": False, "error": "Rates must be finite non-negative numbers",
                    "code": "invalid_settings"}
    for key in UPDATE_KEYS - {"rates"}:
        if key in patch and not _number(patch[key]):
            return {"success": False,
                    "error": f"{key} must be a finite non-negative number",
                    "code": "invalid_settings"}
    # Accept the legacy flat admin payload as a compatibility wrapper.
    flat_rates = {key: patch.pop(key) for key in RATE_NAMES if key in patch}
    if flat_rates:
        merged_rates = dict(patch.get("rates") or {})
        merged_rates.update(flat_rates)
        patch["rates"] = merged_rates
    return patch


def update_settings(values, *, actor, reason, supabase=None,
                    expected_version=None) -> dict:
    """Atomically update a partial settings patch through the SQL RPC."""
    if not isinstance(actor, str) or not actor.strip():
        return _failure("An actor is required", code="invalid_actor")
    if not isinstance(reason, str) or not reason.strip():
        return _failure("A reason is required", code="invalid_reason")
    patch_result = _validate_patch(values)
    if patch_result is not None and patch_result.get("success") is False:
        return patch_result
    patch = patch_result
    if expected_version is not None:
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            return _failure("config_version must be an integer", code="invalid_version")
    db = supabase if supabase is not None else _admin_client()
    if db is None:
        return _failure("Reward configuration database is unavailable",
                        code="configuration_unavailable")
    args = {
        "p_settings": patch,
        "p_actor": actor.strip(),
        "p_reason": reason.strip(),
        "p_expected_version": expected_version,
    }
    try:
        result = db.rpc(RPC_UPDATE, args).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("community reward settings update failed: %s", exc)
        return _failure("Reward configuration update is unavailable",
                        code="configuration_unavailable")
    payload = _payload(result)
    if payload is None:
        return _failure("Reward configuration RPC returned malformed data",
                        code="configuration_malformed")
    if payload.get("success") is not True:
        return payload
    return payload
