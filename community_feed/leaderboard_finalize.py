"""Close a UTC week into an immutable final recipient ledger.

This is the only code path allowed to freeze a week's payouts. It reads the
*locked* persisted reward configuration (``leaderboard_config``), recomputes the
allocation from the same deterministic function the admin preview used
(``leaderboard_preview.compute_allocation``), and hands the ledger to the
``weekly_exp_finalize_week`` RPC which enforces the budget/funding gate and
writes the ledger immutably.

It never funds, publishes a Merkle root, or pays a claim. Those are separate,
later steps (Phase 9+).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

FINALIZE_RPC = "weekly_exp_finalize_week"
MAX_RECIPIENTS = 10_000


def _admin_client():
    from . import weekly_exp
    return weekly_exp._admin_db()


def _config_to_settings(config: dict) -> dict:
    """Map a stored config payload onto the allocation input schema."""
    return {
        "distribution": config.get("distribution"),
        "ranked_pool_gd": config.get("pool_gd"),
        "fixed_prizes_gd": config.get("fixed_prizes_gd") or [],
        "consolation_gd": config.get("consolation_gd"),
        "top_n": config.get("top_n"),
        "minimum_exp": config.get("minimum_exp"),
        "minimum_substantive_days": config.get("minimum_substantive_days"),
    }


def _load_scores(db, week_start: str) -> list[dict]:
    from . import weekly_exp
    return weekly_exp._bounded_rows(lambda: (
        db.table("leaderboard_weekly_scores")
        .select("wallet_address,rank,total_exp,breakdown")
        .eq("week_start", week_start)
        .order("rank")
    ), maximum=MAX_RECIPIENTS)


def _display_name(wallet: str) -> str:
    return f"{wallet[:6]}…{wallet[-4:]}"


def _build_ledger(allocation: dict, top_n: int) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    for recipient in allocation["ranked_recipients"]:
        rows.append({
            "wallet_address": recipient["wallet_address"],
            "rank": recipient["rank"],
            "category": "ranked",
            "amount_gd": recipient["payout_gd"],
        })
    for user in allocation["consolation_users"]:
        rows.append({
            "wallet_address": user["wallet_address"],
            "rank": user["rank"],
            "category": "consolation",
            "amount_gd": user["payout_gd"],
        })
    rows.sort(key=lambda row: (row["category"] != "ranked",
                               row["rank"] if row["rank"] is not None else 0,
                               row["wallet_address"]))
    standings = [{
        "rank": recipient["rank"],
        "display_name": _display_name(recipient["wallet_address"]),
        "total_exp": recipient["total_exp"],
    } for recipient in allocation["ranked_recipients"][:top_n]]
    return rows, standings


def _ledger_digest(rows: list[dict]) -> str:
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return "0x" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decimal_or_none(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def finalize_week(week_start: date | str, actor: str, *, db=None,
                  budget_raw: str | None = None, funding: dict | None = None,
                  now: datetime | None = None) -> dict:
    """Finalize one week, or return a fail-closed reason it could not be."""
    from . import leaderboard_config, leaderboard_preview

    if not str(actor or "").strip():
        return {"success": False, "error": "actor is required", "code": "invalid_actor"}
    try:
        day = (week_start if isinstance(week_start, date)
               else date.fromisoformat(str(week_start)))
    except (TypeError, ValueError):
        return {"success": False, "error": "week_start must be a date", "code": "invalid_week"}
    if day.weekday() != 0:
        return {"success": False, "error": "week_start must be a Monday UTC boundary",
                "code": "invalid_week"}
    week = day.isoformat()

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if day >= date.fromordinal(now.date().toordinal() - now.weekday()):
        return {"success": False, "error": "the week must be closed before it can be finalized",
                "code": "week_not_closed"}

    db = db if db is not None else _admin_client()
    if db is None:
        return {"success": False, "error": "Reward database is unavailable",
                "code": "configuration_unavailable"}

    config = leaderboard_config.get_config(week, supabase=db)
    if not config.get("success"):
        return config
    if not config.get("configured"):
        return {"success": False, "error": "no reward configuration exists for this period",
                "code": "not_configured"}
    if config.get("status") != "locked":
        return {"success": False,
                "error": "the reward configuration must be locked before finalization",
                "code": "not_locked"}

    try:
        scores = _load_scores(db, week)
    except Exception as exc:  # noqa: BLE001 - surface a usable operator error
        logger.error("weekly finalize score load failed: %s", exc)
        return {"success": False, "error": "Unable to load the final score set",
                "code": "scores_unavailable"}
    if not scores:
        return {"success": False, "error": "no scores exist for this period",
                "code": "no_scores"}

    try:
        allocation = leaderboard_preview.compute_allocation(
            scores, _config_to_settings(config))
    except (leaderboard_preview.PreviewInputError,
            leaderboard_preview.SnapshotDataError) as exc:
        return {"success": False, "error": str(exc), "code": "invalid_configuration"}

    rows, standings = _build_ledger(allocation, int(config.get("top_n") or 0))
    if not rows:
        return {"success": False, "error": "the allocation produced no recipients",
                "code": "empty_allocation"}

    hard_cap = _decimal_or_none(budget_raw if budget_raw is not None
                                else os.getenv("WEEKLY_EXP_MAX_BUDGET_GD"))
    funding = funding if isinstance(funding, dict) else None
    funded = (_decimal_or_none(funding.get("balance_gd"))
              if funding and funding.get("status") == "ready" else None)

    try:
        result = db.rpc(FINALIZE_RPC, {
            "p_week_start": week,
            "p_actor": str(actor).strip(),
            "p_public_standings": standings,
            "p_allocations": rows,
            "p_approved_budget_gd": str(hard_cap) if hard_cap is not None else None,
            "p_funded_balance_gd": str(funded) if funded is not None else None,
            "p_ledger_digest": _ledger_digest(rows),
        }).execute()
    except Exception as exc:  # noqa: BLE001 - finalization must fail closed
        logger.error("weekly finalize RPC failed: %s", exc)
        return {"success": False, "error": "Finalization service is temporarily unavailable",
                "code": "finalization_unavailable"}
    data = getattr(result, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return {"success": False, "error": "Finalization returned an empty response",
                "code": "finalization_unavailable"}
    return data
