"""Read-only Community leaderboard card backed by persisted weekly snapshots.

Page/API reads never scan Garden logs or rebuild rankings. A separately
credentialed worker refreshes snapshots after both evidence sources reconcile.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone

from . import weekly_exp

logger = logging.getLogger(__name__)
SNAPSHOT_TABLE = "leaderboard_weekly_snapshots"
PRIVATE_SCORE_TABLE = "leaderboard_weekly_scores"
STALE_AFTER = timedelta(hours=36)
MAX_PARTICIPANTS = 10_000
_SHORT_WALLET_LABEL = re.compile(r"^0x[a-fA-F0-9]{4}…[a-fA-F0-9]{4}$")


def is_enabled() -> bool:
    return (os.getenv("COMMUNITY_LEADERBOARD_CARD_ENABLED", "0") or "0").strip().lower() in {
        "1", "true", "yes", "on", "enabled"
    }


def top_n() -> int:
    """Configured public Top N with no fixed 10/20 cap (bounded by capacity)."""
    try:
        requested = int(os.getenv("COMMUNITY_LEADERBOARD_TOP_N", str(weekly_exp.DEFAULT_TOP_N)))
    except (TypeError, ValueError):
        requested = weekly_exp.DEFAULT_TOP_N
    return max(1, min(weekly_exp.max_top_n(), requested))


def _period(now: datetime | None = None) -> tuple[date, date, str]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    start, end = weekly_exp.week_bounds(now.astimezone(timezone.utc).date())
    iso = start.isocalendar()
    return start, end, f"{iso.year}-W{iso.week:02d}"


def _dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        import json
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []
    return []


def _public_rows(value) -> list[dict]:
    """Whitelist public fields; never serialize wallet, evidence, or proof data."""
    output = []
    for row in _list(value)[:top_n()]:
        if not isinstance(row, dict):
            continue
        try:
            rank = int(row.get("rank"))
            exp = int(row.get("total_exp"))
        except (TypeError, ValueError):
            continue
        if not (1 <= rank <= 10_000 and 0 <= exp <= 100_000_000):
            continue
        label = str(row.get("display_name") or "")[:48]
        if not _SHORT_WALLET_LABEL.fullmatch(label):
            label = "Community member"
        output.append({
            "rank": rank,
            "display_name": label,
            "total_exp": exp,
        })
    return output


def _private_viewer_row(db, week_start: date, wallet: str | None) -> dict | None:
    if not wallet:
        return None
    try:
        wallet = weekly_exp.normalize_wallet(wallet)
        result = (db.table(PRIVATE_SCORE_TABLE)
                  .select("rank,total_exp,breakdown")
                  .eq("week_start", week_start.isoformat())
                  .eq("wallet_address", wallet)
                  .limit(1).execute())
        rows = getattr(result, "data", None) or []
        if not rows:
            return None
        row = rows[0]
        breakdown = _dict(row.get("breakdown"))
        allowed = ("checkin_exp", "daily_task_exp", "garden_exp", "streak_exp",
                   "substantive_days", "daily_task_days", "daily")
        safe_breakdown = {key: breakdown[key] for key in allowed if key in breakdown}
        return {"rank": int(row.get("rank") or 0),
                "total_exp": int(row.get("total_exp") or 0),
                "breakdown": safe_breakdown}
    except Exception as exc:  # noqa: BLE001 - private detail must fail independently
        logger.warning("weekly leaderboard private viewer lookup failed: %s", exc)
        return None


def current_board(viewer: str | None = None, now: datetime | None = None) -> dict:
    """Return only the persisted public snapshot and the requesting user's row."""
    start, end, label = _period(now)
    base = {"enabled": is_enabled(), "available": False,
            "week_start": start.isoformat(), "week_end_exclusive": end.isoformat(),
            "week_label": label}
    if not is_enabled():
        return base
    try:
        db = weekly_exp._admin_db()
        result = (db.table(SNAPSHOT_TABLE).select(
                    "week_start,week_end_exclusive,data_status,generated_at,top_n,public_standings"
                  ).eq("week_start", start.isoformat()).limit(1).execute())
        rows = getattr(result, "data", None) or []
        if not rows:
            return {**base, "status": "awaiting_snapshot", "standings": [],
                    "prize_terms": {"status": "pending_admin_review",
                                    "message": "Prize terms are pending admin review."}}
        row = rows[0]
        stale = row.get("data_status") == "stale"
        if row.get("generated_at"):
            try:
                parsed = datetime.fromisoformat(str(row["generated_at"]).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
                stale = stale or age > STALE_AFTER or age < timedelta(minutes=-5)
            except (TypeError, ValueError):
                stale = True
        else:
            stale = True
        status = "stale" if stale else "provisional"
        own = _private_viewer_row(db, start, viewer)
        return {
            **base,
            "available": True,
            "status": status,
            "last_updated_at": row.get("generated_at"),
            "top_n": max(1, min(weekly_exp.max_top_n(), int(row.get("top_n") or top_n()))),
            "standings": _public_rows(row.get("public_standings")),
            "viewer": own,
            "prize_terms": {"status": "pending_admin_review",
                            "message": "Prize terms are pending admin review; no payout is promised."},
            "points_guide": {
                "checkin_exp": weekly_exp.CHECKIN_EXP,
                "daily_task_exp": weekly_exp.DAILY_TASK_EXP,
                "garden_exp": weekly_exp.GARDEN_EXP,
                "streak_exp": weekly_exp.SEVEN_DAY_STREAK_EXP,
                "base_daily_cap": weekly_exp.BASE_DAILY_CAP,
            },
        }
    except Exception as exc:  # noqa: BLE001 - never compute scores in request path
        logger.warning("weekly leaderboard snapshot unavailable: %s", exc)
        return {**base, "status": "unavailable", "standings": [],
                "prize_terms": {"status": "pending_admin_review",
                                "message": "Prize terms are pending admin review."}}


def _mark_stale(db, week_start: date) -> None:
    try:
        db.table(SNAPSHOT_TABLE).update({"data_status": "stale"})\
            .eq("week_start", week_start.isoformat()).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("weekly leaderboard stale marker failed: %s", exc)


def refresh_current_snapshot(now: datetime | None = None) -> dict:
    """Worker entry point: reconcile evidence then atomically persist current board."""
    if not is_enabled() or not weekly_exp.is_enabled():
        return {"success": False, "status": "disabled"}
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    start, end, _label = _period(now)
    db = weekly_exp._admin_db()
    tasks = weekly_exp.reconcile_approved_daily_tasks(now.date())
    garden = weekly_exp.reconcile_garden_events(now.date())
    if not tasks.get("success") or not garden.get("success"):
        _mark_stale(db, start)
        return {"success": False, "status": "stale", "task_sources": tasks.get("sources"),
                "garden_status": garden.get("status")}

    evidence_start = date.fromordinal(max(1, start.toordinal() - 6))
    records = weekly_exp._bounded_rows(lambda: (
        db.table("leaderboard_participation")
        .select("wallet_address,activity_day,source,verified,reversed_at")
        .gte("activity_day", evidence_start.isoformat())
        .lt("activity_day", end.isoformat())
        .eq("verified", True).is_("reversed_at", "null")
    ), maximum=MAX_PARTICIPANTS)
    grouped: dict[str, list[dict]] = {}
    for row in records:
        try:
            wallet = weekly_exp.normalize_wallet(row.get("wallet_address"))
        except ValueError:
            continue
        grouped.setdefault(wallet, []).append(row)

    ranked = []
    for wallet, items in grouped.items():
        score = weekly_exp.calculate_daily_points(items, start=start, end=end)
        if not score["total_exp"]:
            continue
        task_days = sum(1 for item in score["daily"] if item["daily_task_exp"] > 0)
        ranked.append({"wallet_address": wallet, "score": score,
                       "daily_task_days": task_days})
    ranked.sort(key=lambda row: (-row["score"]["total_exp"],
                                 -row["score"]["substantive_days"],
                                 -row["daily_task_days"],
                                 row["wallet_address"].lower()))
    private_scores = []
    public_standings = []
    for position, item in enumerate(ranked, 1):
        score = item["score"]
        private_scores.append({
            "wallet_address": item["wallet_address"],
            "rank": position,
            "total_exp": score["total_exp"],
            "breakdown": {"checkin_exp": score["checkin_exp"],
                          "daily_task_exp": score["daily_task_exp"],
                          "garden_exp": score["garden_exp"],
                          "streak_exp": score["streak_exp"],
                          "substantive_days": score["substantive_days"],
                          "daily_task_days": item["daily_task_days"],
                          "daily": score["daily"]},
        })
        if position <= top_n():
            public_standings.append({
                "rank": position,
                "display_name": f"{item['wallet_address'][:6]}…{item['wallet_address'][-4:]}",
                "total_exp": score["total_exp"],
            })

    generated_at = datetime.now(timezone.utc).isoformat()
    source_status = {"daily_task": "ready", "garden_v2": "ready"}
    db.rpc("weekly_leaderboard_save_snapshot", {
        "p_week_start": start.isoformat(),
        "p_week_end_exclusive": end.isoformat(),
        "p_generated_at": generated_at,
        "p_top_n": top_n(),
        "p_public_standings": public_standings,
        "p_scores": private_scores,
        "p_source_status": source_status,
    }).execute()
    from . import leaderboard_posts
    post = leaderboard_posts.publish_weekly_post(
        db, week_start=start, standings=public_standings, status="provisional")
    return {"success": True, "status": "provisional", "week_start": start.isoformat(),
            "generated_at": generated_at, "participant_count": len(private_scores),
            "public_count": len(public_standings), "post_id": post["post_id"]}
