
"""Community reward facade.

Positive reward grants are deliberately delegated to service-role SQL RPCs.  The
append-only ledger remains the historical balance source, but Python never
read-modify-writes a positive ledger row and never substitutes environment
rates/caps when the database/RPC is unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from env_utils import get_env_int

from . import reward_settings

logger = logging.getLogger(__name__)

FEATURE_NAME = "community_rewards_feature"
LEDGER = "community_reward_ledger"
RATE_NAMES = reward_settings.RATE_NAMES
RATES_FEATURE = "community_reward_rates"  # compatibility name; no table fallback
_ENV_RATE_DEFAULTS = {
    "comment_out": "2.5", "comment_in": "1.5",
    "reaction_out": "0.5", "reaction_in": "0.1",
}
# These are schema/safety bounds, not runtime settings or defaults.
RATE_MAX = Decimal("100")
RATE_BOUNDS = {name: (Decimal("0"), RATE_MAX) for name in RATE_NAMES}
RATE_MAX_EFFECTIVE = RATE_MAX  # legacy introspection; live ceiling is settings.rate_max
DAILY_CAP = Decimal("250")  # compatibility constant only; live values come from DB
POST_CAP = Decimal("100")   # compatibility constant only; live values come from DB
SHARED_CAP_EVENT_TYPES = ("reaction_in", "reaction_out", "comment_in", "comment_out")
MIN_PAYOUT_GD = Decimal(os.getenv("COMMUNITY_REWARD_MIN_PAYOUT_GD", "500"))
DAILY_LEARN_RATE_MAX_DEFAULT = Decimal("50")
DAILY_LEARN_RATE_DB_BOUND = Decimal("500")
DAILY_LEARN_EVENT = "daily_learn"
DAILY_LEARN_CLAIM_RPC = "community_daily_learn_claim_uncredited_questions"
BUDGET_SUMMARY_RPC = "community_reward_budget_summary"

_ELIGIBILITY_TTL = get_env_int("COMMUNITY_REWARD_ELIGIBILITY_TTL", 300)
_eligibility_cache: dict[str, tuple[bool, float]] = {}


def _env_flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip() != "0"


def rewards_enabled_env() -> bool:
    return _env_flag("COMMUNITY_REWARDS_ENABLED", "1")


def _get_supabase():
    try:
        from supabase_client import get_supabase_admin_client, get_supabase_client
        return get_supabase_admin_client() or get_supabase_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: database client unavailable: %s", exc)
        return None


def _rows(result) -> list[dict]:
    if not result:
        return []
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    if isinstance(data, dict):
        return [data]
    return [row for row in (data or []) if isinstance(row, dict)]


def _payload(result) -> dict | None:
    rows = _rows(result)
    return rows[0] if rows else None


def _as_decimal(value) -> Decimal:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else Decimal("0")
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _same_wallet(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and str(a).strip().lower() == str(b).strip().lower()


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def start_at() -> datetime | None:
    raw = (os.getenv("COMMUNITY_REWARD_START_AT") or "").strip()
    if not raw:
        return None
    parsed = _parse_dt(raw)
    if parsed is None:
        logger.error("community_rewards: invalid COMMUNITY_REWARD_START_AT=%r", raw)
        return datetime.max.replace(tzinfo=timezone.utc)
    return parsed


def is_rewardable(created_at) -> bool:
    fence = start_at()
    if fence is None:
        return True
    parsed = _parse_dt(created_at)
    return parsed is not None and parsed >= fence


def is_enabled(*, _supabase=None) -> bool:
    """Read the admin kill switch; missing/unreadable configuration is OFF."""
    if not rewards_enabled_env():
        return False
    db = _supabase or _get_supabase()
    if db is None:
        return False
    try:
        rows = _rows(db.table("maintenance_settings").select("is_maintenance")
                     .eq("feature_name", FEATURE_NAME).limit(1).execute())
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: feature flag read failed: %s", exc)
        return False
    return bool(rows) and rows[0].get("is_maintenance") in (False, 0, "false", "f")


def _identity_verified(wallet: str) -> bool | None:
    try:
        from blockchain import is_identity_verified
        result = is_identity_verified(wallet) or {}
        if result.get("verified") is True:
            return True
        return None if result.get("error") else False
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: identity check failed: %s", exc)
        return None


def is_eligible(wallet: str) -> bool:
    if not wallet:
        return False
    if not _env_flag("COMMUNITY_REWARD_REQUIRE_VERIFIED", "1"):
        return True
    key = str(wallet).strip().lower()
    now = time.time()
    cached = _eligibility_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    verdict = _identity_verified(wallet)
    if verdict is None:
        return False
    _eligibility_cache[key] = (verdict, now + _ELIGIBILITY_TTL)
    return verdict


def invalidate_eligibility_cache() -> None:
    _eligibility_cache.clear()


# ── DB configuration wrappers ────────────────────────────────────────────────

def _settings(_supabase=None) -> dict:
    return reward_settings.get_settings(_supabase)


def _setting_decimal(settings: dict, key: str) -> Decimal:
    value = settings.get(key) if isinstance(settings, dict) else None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return parsed if parsed.is_finite() and parsed >= 0 else Decimal("0")


def effective_rates(supabase=None, *, use_cache: bool = False) -> dict[str, Decimal]:
    """Return rates from one authoritative settings RPC; unavailable is zero."""
    settings = _settings(supabase)
    rates = settings.get("rates") if settings.get("success") else None
    if not isinstance(rates, dict):
        return {name: Decimal("0") for name in RATE_NAMES}
    return {name: _setting_decimal(rates, name) for name in RATE_NAMES}


def daily_learn_rate_max(_supabase=None) -> Decimal:
    settings = _settings(_supabase)
    return _setting_decimal(settings, "daily_learn_rate_max") if settings.get("success") else Decimal("0")


def daily_learn_daily_cap(_supabase=None) -> Decimal:
    settings = _settings(_supabase)
    return _setting_decimal(settings, "daily_learn_cap") if settings.get("success") else Decimal("0")


def daily_learn_window_start(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def daily_learn_budget(_supabase=None) -> tuple[Decimal, datetime, tuple]:
    return (daily_learn_daily_cap(_supabase), daily_learn_window_start(), (DAILY_LEARN_EVENT,))


def _configured_daily_learn_rate(db) -> Decimal | None:
    """Compatibility helper: only read the authoritative settings RPC."""
    settings = _settings(db)
    if not settings.get("success"):
        return None
    value = _setting_decimal(settings, "daily_learn_rate")
    return value


def _env_rate(name: str) -> Decimal:
    """Compatibility accessor exposing shipped display defaults only.

    It is intentionally not used to grant, summarize, or save rewards.  Runtime
    rates are DB-only under the approved contract.
    """
    return Decimal(_ENV_RATE_DEFAULTS[name])


def _rate(name: str, default: str) -> Decimal:
    return Decimal(str(default))


def _clamp(name: str, value: Decimal) -> Decimal:
    low, high = RATE_BOUNDS[name]
    return max(low, min(high, value))


def _parse_rate(name: str, raw) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() and value >= 0 else None


def _read_overrides(_supabase) -> dict:
    return {}


def invalidate_rates_cache() -> None:
    return None


def set_rates(values: dict, *, actor: str, reason: str, supabase=None) -> dict:
    """Compatibility wrapper for the old admin API; writes only through RPC."""
    return reward_settings.update_settings({"rates": values}, actor=actor,
                                           reason=reason, supabase=supabase)


# ── Ledger reads (historical aggregate remains immutable) ────────────────────

def _balance_read(wallet: str, supabase) -> tuple[Decimal, bool]:
    if not wallet:
        return Decimal("0"), True
    if supabase is None:
        return Decimal("0"), False
    total = Decimal("0")
    offset = 0
    page = 1000
    while True:
        try:
            result = (supabase.table(LEDGER).select("delta_gd")
                      .ilike("wallet_address", wallet).order("id")
                      .range(offset, offset + page - 1).execute())
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_rewards: balance read failed: %s", exc)
            return total, False
        rows = _rows(result)
        total += sum((_as_decimal(row.get("delta_gd")) for row in rows), Decimal("0"))
        if len(rows) < page:
            return total, True
        offset += page


def balance(wallet: str, *, _supabase=None) -> Decimal:
    return _balance_read(wallet, _supabase or _get_supabase())[0]


def _find_by_key(supabase, key: str) -> dict | None:
    try:
        rows = _rows(supabase.table(LEDGER).select("*").eq(
            "idempotency_key", key).limit(1).execute())
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: ledger lookup failed: %s", exc)
        return None


def _insert(supabase, row: dict) -> bool:
    """Compatibility insert used only for negative reversal rows."""
    row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    try:
        supabase.table(LEDGER).insert(row).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: reversal insert skipped: %s", exc)
        return False


# ── Atomic positive credit RPC ────────────────────────────────────────────────

def _rpc_outcome(result, *, default_reason="configuration_unavailable") -> dict:
    payload = _payload(result)
    if not payload:
        return {"status": "not_credited", "reason": default_reason}
    status = str(payload.get("status") or "not_credited")
    outcome = {"status": status}
    if payload.get("reason") is not None:
        outcome["reason"] = payload["reason"]
    if payload.get("credited_gd") is not None:
        outcome["credited_gd"] = payload["credited_gd"]
    return outcome


def _credit(supabase, wallet: str, *, key: str, event_type: str,
            amount: Decimal | None = None, post_id=None, comment_id=None,
            event_created_at=None, source_question_id=None, day_utc=None,
            actor_wallet=None) -> dict:
    """Call the atomic grant RPC; ``amount`` is compatibility-only and ignored."""
    if not wallet:
        return {"status": "not_credited", "reason": "invalid_event"}
    if not is_rewardable(event_created_at):
        return {"status": "not_credited", "reason": "invalid_event"}
    if not is_eligible(wallet):
        return {"status": "not_credited", "reason": "verified_wallet_required"}
    if not is_enabled(_supabase=supabase):
        return {"status": "not_credited", "reason": "reward_disabled"}
    if supabase is None:
        return {"status": "not_credited", "reason": "configuration_unavailable"}
    args = {
        "p_wallet_address": str(wallet).strip().lower(),
        "p_event_type": event_type,
        "p_idempotency_key": key,
        "p_post_id": int(post_id) if post_id not in (None, "") else None,
        "p_comment_id": int(comment_id) if comment_id not in (None, "") else None,
        "p_source_question_id": source_question_id,
        "p_day_utc": day_utc,
        "p_actor_wallet": str(actor_wallet or wallet).strip().lower(),
    }
    try:
        result = supabase.rpc("community_reward_credit", args).execute()
        return _rpc_outcome(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: atomic credit RPC failed: %s", exc)
        return {"status": "not_credited", "reason": "configuration_unavailable"}


# ── Daily Learn snapshots and retries ────────────────────────────────────────

def _daily_learn_attempt(db, wallet: str, day_utc: str) -> dict | None:
    try:
        rows = _rows(db.table("community_daily_learn_attempts").select(
            "status,score,reward_rate_gd,reward_cap_gd,reward_budget_day_utc,"
            "completed_at,question_1_id,correct_1,question_2_id,correct_2"
        ).ilike("wallet_address", wallet).eq("day_utc", day_utc).limit(1).execute())
        return rows[0] if rows and rows[0].get("status") == "completed" else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: Daily Learn attempt read failed: %s", exc)
        return None


def _daily_learn_ledger_row(db, key: str) -> dict | None:
    try:
        rows = _rows(db.table(LEDGER).select("*").eq("idempotency_key", key).limit(1).execute())
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: Daily Learn ledger lookup failed: %s", exc)
        raise


def _daily_learn_ledger_row_matches(row: dict, wallet: str, amount: Decimal,
                                   question_id: str) -> bool:
    return (_same_wallet(row.get("wallet_address"), wallet)
            and row.get("event_type") == DAILY_LEARN_EVENT
            and _as_decimal(row.get("delta_gd")) == amount
            and str(row.get("source_question_id") or "") == question_id)


def daily_learn_completion_status(wallet: str, day_utc: str, *, _supabase=None,
                                  _attempt: dict | None = None) -> dict:
    try:
        identity = (wallet or "").strip().lower()
        parsed_day = datetime.fromisoformat(str(day_utc)).date().isoformat()
        if not identity or parsed_day != str(day_utc):
            return {"status": "not_credited", "reason": "invalid_completion"}
        db = _supabase or _get_supabase()
        if db is None:
            return {"status": "not_credited", "reason": "configuration_unavailable"}
        if not is_enabled(_supabase=db):
            return {"status": "not_credited", "reason": "reward_disabled"}
        attempt = _attempt or _daily_learn_attempt(db, identity, parsed_day)
        if not attempt:
            return {"status": "not_credited", "reason": "completion_not_found"}
        try:
            score = int(attempt.get("score") or 0)
        except (TypeError, ValueError):
            return {"status": "not_credited", "reason": "invalid_event"}
        if score <= 0:
            return {"status": "not_credited", "reason": "no_correct_answers"}
        if score > 2:
            return {"status": "not_credited", "reason": "invalid_event"}
        correct_questions = []
        for suffix in ("1", "2"):
            if attempt.get(f"correct_{suffix}") is True:
                question_id = str(attempt.get(f"question_{suffix}_id") or "").strip()
                if not question_id:
                    return {"status": "not_credited", "reason": "invalid_event"}
                correct_questions.append(question_id)
        if len(correct_questions) != score:
            return {"status": "not_credited", "reason": "invalid_event"}

        legacy_key = f"daily_learn:{parsed_day}:{identity}"
        legacy = _daily_learn_ledger_row(db, legacy_key)
        if legacy:
            if (_same_wallet(legacy.get("wallet_address"), identity)
                    and legacy.get("event_type") == DAILY_LEARN_EVENT
                    and _as_decimal(legacy.get("delta_gd")) > 0
                    and not legacy.get("source_question_id")):
                return {"status": "already_credited", "credited_gd": legacy.get("delta_gd")}
            return {"status": "not_credited", "reason": "ledger_integrity_error"}

        raw_rate = attempt.get("reward_rate_gd")
        # A NULL historical snapshot is not permission to reprice at today's
        # settings.  The SQL owner may repair such a row explicitly; until then
        # surface a diagnostic rather than minting at a new rate.
        if raw_rate is None:
            return {"status": "not_credited", "reason": "legacy_snapshot_required"}
        rate = _as_decimal(raw_rate)
        if rate < 0 or not rate.is_finite():
            return {"status": "not_credited", "reason": "invalid_event"}
        if rate == 0:
            return {"status": "not_credited", "reason": "reward_disabled"}

        question_keys = {
            q: f"daily_learn:{parsed_day}:{identity}:{q}" for q in correct_questions
        }
        existing = {}
        for q, key in question_keys.items():
            row = _daily_learn_ledger_row(db, key)
            if row:
                if not _daily_learn_ledger_row_matches(row, identity, rate, q):
                    return {"status": "not_credited", "reason": "ledger_integrity_error"}
                existing[q] = row
        missing = [q for q in correct_questions if q not in existing]
        if not missing:
            return {"status": "already_credited", "credited_gd": str(rate * len(correct_questions))}

        completed_at = attempt.get("completed_at") or attempt.get("reward_completed_at")
        newly = Decimal("0")
        credited_count = len(existing)
        for question_id in missing:
            outcome = _credit(
                db, identity, key=question_keys[question_id],
                event_type=DAILY_LEARN_EVENT, amount=rate,
                event_created_at=completed_at, source_question_id=question_id,
                day_utc=attempt.get("reward_budget_day_utc") or parsed_day,
                actor_wallet=identity)
            status = outcome.get("status")
            if status == "credited":
                credited_count += 1
                newly += _as_decimal(outcome.get("credited_gd", rate))
                continue
            if status == "already_credited":
                credited_count += 1
                newly += _as_decimal(outcome.get("credited_gd", rate))
                continue
            result = {"status": "pending" if credited_count else "not_credited",
                      "reason": outcome.get("reason", "configuration_unavailable"),
                      "credited_questions": credited_count}
            if newly:
                result["credited_gd"] = float(newly)
            return result
        return {"status": "credited" if newly else "already_credited",
                "credited_questions": credited_count,
                "credited_gd": float(newly or rate * len(correct_questions))}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: Daily Learn credit skipped: %s", exc)
        return {"status": "not_credited", "reason": "configuration_unavailable"}


def on_daily_learn_completed(wallet: str, day_utc: str, *, _supabase=None) -> bool:
    return daily_learn_completion_status(wallet, day_utc, _supabase=_supabase).get("status") == "credited"


def reconcile_uncredited_daily_learn_attempts(*, _supabase=None, batch_size: int = 100) -> dict:
    db = _supabase or _get_supabase()
    if db is None:
        return {"claimed": 0, "credited": 0, "already_credited": 0,
                "not_credited": 0, "error": "database_unavailable"}
    if not is_enabled(_supabase=db):
        return {"claimed": 0, "credited": 0, "already_credited": 0,
                "not_credited": 0, "skipped": "rewards_disabled"}
    try:
        attempts = _rows(db.rpc(DAILY_LEARN_CLAIM_RPC, {
            "p_limit": max(1, min(int(batch_size), 500))}).execute())
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: Daily Learn claim failed: %s", exc)
        return {"claimed": 0, "credited": 0, "already_credited": 0,
                "not_credited": 0, "error": "claim_failed"}
    summary = {"claimed": len(attempts), "credited": 0,
               "already_credited": 0, "not_credited": 0}
    for attempt in attempts:
        # The claim RPC deliberately returns only claim metadata.  Re-read the
        # full persisted attempt so completion timestamps and immutable reward
        # snapshots remain authoritative for the start fence and retry pricing.
        result = daily_learn_completion_status(
            str(attempt.get("attempt_wallet_address") or ""),
            str(attempt.get("attempt_day_utc") or ""), _supabase=db,
        )
        key = result.get("status")
        summary["credited" if key == "credited" else
                "already_credited" if key == "already_credited" else "not_credited"] += 1
    return summary


# ── Negative reversals ────────────────────────────────────────────────────────

def _reverse(supabase, original: dict, *, actor_wallet: str) -> bool:
    original_id = original.get("id")
    delta = _as_decimal(original.get("delta_gd"))
    if original_id in (None, "") or delta == 0:
        return False
    try:
        if _rows(supabase.table(LEDGER).select("id").eq(
                "reverses_ledger_id", int(original_id)).limit(1).execute()):
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: reversal lookup failed: %s", exc)
        return False
    return _insert(supabase, {
        "wallet_address": str(original.get("wallet_address") or actor_wallet).lower(),
        "delta_gd": str(-delta), "event_type": original.get("event_type") or "admin_adjust",
        "idempotency_key": f"undo:{original_id}", "reverses_ledger_id": int(original_id),
        "reason": "reversed: source event removed", "actor": actor_wallet.lower(),
    })


# ── Public social hooks ───────────────────────────────────────────────────────

def _hook_unavailable(reason="configuration_unavailable"):
    return {"status": "not_credited", "reason": reason}


def on_reaction_added(wallet: str, post_id, post_owner: str | None, *,
                      event_created_at=None, _supabase=None) -> dict:
    try:
        db = _supabase or _get_supabase()
        if not wallet:
            return _hook_unavailable("invalid_event")
        if db is None:
            return _hook_unavailable()
        if _same_wallet(wallet, post_owner):
            return _hook_unavailable("invalid_event")
        actor_result = _credit(
            db, wallet, key=f"reaction_in:{post_id}:{wallet.lower()}",
            event_type="reaction_in", post_id=post_id,
            event_created_at=event_created_at, actor_wallet=wallet)
        if post_owner:
            _credit(db, post_owner, key=f"reaction_out:{post_id}:{wallet.lower()}",
                    event_type="reaction_out", post_id=post_id,
                    event_created_at=event_created_at, actor_wallet=wallet)
        return actor_result
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: reaction credit failed: %s", exc)
        return _hook_unavailable()


def on_reaction_removed(wallet: str, post_id, post_owner: str | None, *,
                        _supabase=None) -> dict:
    try:
        db = _supabase or _get_supabase()
        if db is None:
            return _hook_unavailable()
        if _same_wallet(wallet, post_owner):
            return _hook_unavailable("invalid_event")
        reversed_any = False
        for owner, key in ((wallet, f"reaction_in:{post_id}:{wallet.lower()}"),
                           (post_owner, f"reaction_out:{post_id}:{wallet.lower()}")):
            if owner:
                original = _find_by_key(db, key)
                if original:
                    reversed_any = _reverse(db, original, actor_wallet=wallet) or reversed_any
        return {"status": "reversed" if reversed_any else "already_credited"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: reaction reversal failed: %s", exc)
        return _hook_unavailable()


def on_comment_added(wallet: str, comment_id, post_id, post_owner: str | None, *,
                     event_created_at=None, _supabase=None) -> dict:
    try:
        db = _supabase or _get_supabase()
        if not wallet:
            return _hook_unavailable("invalid_event")
        if db is None:
            return _hook_unavailable()
        if _same_wallet(wallet, post_owner):
            return _hook_unavailable("invalid_event")
        actor_result = _credit(
            db, wallet, key=f"comment_in:{comment_id}", event_type="comment_in",
            post_id=post_id, comment_id=comment_id, event_created_at=event_created_at,
            actor_wallet=wallet)
        if post_owner:
            _credit(db, post_owner, key=f"comment_out:{comment_id}", event_type="comment_out",
                    post_id=post_id, comment_id=comment_id,
                    event_created_at=event_created_at, actor_wallet=wallet)
        return actor_result
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: comment credit failed: %s", exc)
        return _hook_unavailable()


def on_comment_removed(wallet: str, comment_id, post_owner: str | None = None, *,
                       _supabase=None) -> dict:
    try:
        db = _supabase or _get_supabase()
        if db is None:
            return _hook_unavailable()
        reversed_any = False
        for owner, key in ((wallet, f"comment_in:{comment_id}"),
                           (post_owner, f"comment_out:{comment_id}")):
            if owner:
                original = _find_by_key(db, key)
                if original:
                    reversed_any = _reverse(db, original, actor_wallet=wallet) or reversed_any
        return {"status": "reversed" if reversed_any else "already_credited"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: comment reversal failed: %s", exc)
        return _hook_unavailable()


# ── Display ───────────────────────────────────────────────────────────────────

def _rate_payload(_supabase=None) -> dict:
    settings = _settings(_supabase)
    rates = effective_rates(_supabase)
    payload = {name: float(rates[name]) for name in RATE_NAMES}
    payload["daily_learn_per_correct"] = float(_setting_decimal(settings, "daily_learn_rate")) if settings.get("success") else 0.0
    payload["daily_learn_rate_max"] = float(daily_learn_rate_max(_supabase))
    return payload


def _budget_summary(wallet: str, db) -> dict | None:
    if not wallet or db is None:
        return None
    try:
        payload = _payload(db.rpc(BUDGET_SUMMARY_RPC, {
            "p_wallet_address": wallet.lower()}).execute())
        return payload if payload and payload.get("success") is True else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: budget summary unavailable: %s", exc)
        return None


def summary(wallet: str | None, *, _supabase=None) -> dict:
    db = _supabase or _get_supabase()
    settings = _settings(db)
    config_available = settings.get("success") is True
    enabled = is_enabled(_supabase=db)
    rates = _rate_payload(db)
    daily_cap = _setting_decimal(settings, "daily_cap") if config_available else Decimal("0")
    quiz_cap = _setting_decimal(settings, "daily_learn_cap") if config_available else Decimal("0")
    quiz_ceiling = _setting_decimal(settings, "daily_learn_rate_max") if config_available else Decimal("0")
    if not wallet:
        return {
            "success": True, "enabled": enabled, "is_guest": True,
            "balance_gd": 0.0, "min_payout_gd": float(MIN_PAYOUT_GD),
            "progress": 0.0, "status": "guest", "can_withdraw": False,
            "eligible": False, "balance_available": True, "rates": rates,
            "daily_cap_gd": float(daily_cap), "daily_learn_cap_gd": float(quiz_cap),
            "daily_learn_rate_max": float(quiz_ceiling), "show_balance": show_balance(),
            "budgets": None, "configuration_available": config_available,
        }
    current, balance_available = _balance_read(wallet, db)
    minimum = MIN_PAYOUT_GD
    progress = float(min(current, minimum) / minimum * 100) if minimum > 0 else 0.0
    eligible = is_eligible(wallet)
    budget = _budget_summary(wallet, db) if config_available else None
    member_config_available = config_available and budget is not None
    return {
        "success": True, "enabled": enabled, "is_guest": False,
        "balance_gd": float(current), "min_payout_gd": float(minimum),
        "progress": round(progress, 1),
        "status": ("ready" if current >= minimum else "pending") if eligible else "unverified",
        "eligible": eligible, "balance_available": balance_available,
        "can_withdraw": False, "rates": rates,
        "reward_per_action": float(max(rates.values())) if rates else 0.0,
        "daily_cap_gd": float(daily_cap), "daily_learn_cap_gd": float(quiz_cap),
        "daily_learn_rate_max": float(quiz_ceiling), "show_balance": show_balance(),
        "budgets": budget, "configuration_available": member_config_available,
    }


def show_balance() -> bool:
    return _env_flag("COMMUNITY_REWARD_SHOW_BALANCE", "1")
