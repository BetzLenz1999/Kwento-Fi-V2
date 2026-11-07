"""Phase 1 foundation for the Community weekly EXP leaderboard.

All points are off-chain and evidence-backed. The feature is disabled unless
WEEKLY_EXP_LEADERBOARD_ENABLED=1. No wallet-supplied score or chain event is
accepted through a public API.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Iterable

from env_utils import get_env_int

logger = logging.getLogger(__name__)

CHECKIN_EXP = 1
DAILY_TASK_EXP = 3
GARDEN_EXP = 3
SEVEN_DAY_STREAK_EXP = 5
BASE_DAILY_CAP = 7

# Top N and pool amounts are admin-configurable with no fixed 10/20 product cap.
# The only Top N bound is system capacity, guarded here so a single snapshot or
# proof bundle can never be asked to carry an unbounded recipient set.
DEFAULT_TOP_N = 5
MAX_TOP_N_CAPACITY = 10_000

_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def is_enabled() -> bool:
    """Fail-closed feature flag; live scoring is opt-in after migration."""
    return (os.getenv("WEEKLY_EXP_LEADERBOARD_ENABLED", "0") or "0").strip().lower() in {
        "1", "true", "yes", "on", "enabled"
    }


def max_top_n() -> int:
    """Admin-configurable Top N ceiling, bounded only by system capacity.

    There is deliberately no 10/20 product cap. ``WEEKLY_EXP_MAX_TOP_N`` may
    raise or lower the practical ceiling; it can never exceed the supported
    capacity that a single snapshot/proof bundle can safely carry.
    """
    capacity = MAX_TOP_N_CAPACITY
    value = get_env_int("WEEKLY_EXP_MAX_TOP_N", capacity)
    return max(1, min(capacity, value))


def utc_date(value: datetime | date | str | None = None) -> date:
    """Normalize a timestamp to its UTC calendar date."""
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date()


def week_bounds(day: date) -> tuple[date, date]:
    """Return Monday-inclusive, next-Monday-exclusive UTC week bounds."""
    start = day.fromordinal(day.toordinal() - day.weekday())
    return start, date.fromordinal(start.toordinal() + 7)


def normalize_wallet(wallet: str) -> str:
    wallet = str(wallet or "").strip()
    if not _ADDRESS_RE.fullmatch(wallet):
        raise ValueError("A valid EVM wallet address is required")
    return wallet.lower()


def calculate_daily_points(participation: Iterable[dict], start: date | None = None,
                           end: date | None = None) -> dict:
    """Deduplicate source credits, apply the 7 EXP base cap, and add streaks.

    A substantive streak day requires an approved task or verified Garden
    action; check-in contributes its own EXP but never preserves the streak.
    Input rows must already represent verified evidence from the server ledger.
    """
    by_day: dict[date, set[str]] = {}
    for item in participation:
        if (item.get("verified") is not True or item.get("reversed") is True
                or item.get("reversed_at")):
            continue
        source = str(item.get("source") or "")
        if source not in {"checkin", "daily_task", "garden"}:
            continue
        try:
            day = utc_date(item.get("activity_day") or item.get("created_at"))
        except (TypeError, ValueError):
            continue
        by_day.setdefault(day, set()).add(source)

    day_totals: dict[date, dict] = {}
    for day, sources in by_day.items():
        checkin = CHECKIN_EXP if "checkin" in sources else 0
        task = DAILY_TASK_EXP if "daily_task" in sources else 0
        garden = GARDEN_EXP if "garden" in sources else 0
        base = min(BASE_DAILY_CAP, checkin + task + garden)
        day_totals[day] = {
            "activity_day": day.isoformat(),
            "checkin_exp": checkin,
            "daily_task_exp": task,
            "garden_exp": garden,
            "base_exp": base,
            "streak_exp": 0,
            "total_exp": base,
            "substantive": bool({"daily_task", "garden"} & sources),
        }

    substantive_dates = {day for day, sources in by_day.items()
                         if {"daily_task", "garden"} & sources}
    run = 0
    previous = None
    streak_awards = 0
    for day in sorted(substantive_dates):
        run = run + 1 if previous and (day - previous).days == 1 else 1
        if run >= 7 and run % 7 == 0:
            day_totals[day]["streak_exp"] += SEVEN_DAY_STREAK_EXP
            day_totals[day]["total_exp"] += SEVEN_DAY_STREAK_EXP
            streak_awards += 1
        previous = day

    rows = [day_totals[day] for day in sorted(day_totals)
            if (start is None or day >= start) and (end is None or day < end)]
    return {
        "total_exp": sum(row["total_exp"] for row in rows),
        "checkin_exp": sum(row["checkin_exp"] for row in rows),
        "daily_task_exp": sum(row["daily_task_exp"] for row in rows),
        "garden_exp": sum(row["garden_exp"] for row in rows),
        "streak_exp": sum(row["streak_exp"] for row in rows),
        "substantive_days": sum(1 for row in rows if row["substantive"]),
        "daily": rows,
    }


def _admin_db():
    """Require service-role access; never fall back to the anon client."""
    from supabase_client import get_supabase_admin_client
    db = get_supabase_admin_client()
    if not db:
        raise RuntimeError("Weekly EXP service-role database client is unavailable")
    return db


def _bounded_rows(query_factory, maximum: int = 5000) -> list[dict]:
    """Read a query in bounded pages and fail rather than silently truncate."""
    page_size = 500
    collected: list[dict] = []
    for offset in range(0, maximum + page_size, page_size):
        page = query_factory().range(offset, offset + page_size - 1).execute()
        rows = getattr(page, "data", None) or []
        if offset >= maximum and rows:
            raise RuntimeError(f"Weekly EXP query exceeded {maximum} rows")
        collected.extend(rows)
        if len(rows) < page_size:
            return collected
    return collected


def _hex_text(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    text = str(value or "").lower()
    return text if text.startswith("0x") else "0x" + text


def record_participation(wallet: str, day: date, source: str,
                         source_key: str, evidence: dict | None = None) -> dict:
    """Idempotently persist an evidence row using the restricted SQL RPC."""
    wallet = normalize_wallet(wallet)
    if source not in {"checkin", "daily_task", "garden"}:
        raise ValueError("Unsupported participation source")
    if not source_key or len(str(source_key)) > 200:
        raise ValueError("Invalid participation evidence key")
    result = _admin_db().rpc("weekly_exp_record_participation", {
        "p_wallet": wallet,
        "p_activity_day": day.isoformat(),
        "p_source": source,
        "p_source_key": str(source_key),
        "p_evidence": evidence or {},
    }).execute()
    data = getattr(result, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict):
        return data
    if isinstance(data, bool):
        return {"recorded": data}
    return {"recorded": False}


def reverse_participation(source: str, source_key: str, reason: str) -> None:
    """Audibly reverse one evidence row after its authoritative status changes."""
    if source not in {"daily_task", "garden"}:
        raise ValueError("This evidence source cannot be reversed here")
    if not source_key or len(source_key) > 200:
        raise ValueError("Invalid participation evidence key")
    _admin_db().rpc("weekly_exp_reverse_participation", {
        "p_source": source,
        "p_source_key": source_key,
        "p_reason": str(reason or "source_no_longer_qualifies")[:240],
    }).execute()


def record_checkin(wallet: str, now: datetime | None = None) -> dict:
    """Record the authenticated user's explicit daily check-in, never a page view."""
    day = utc_date(now)
    normalized = normalize_wallet(wallet)
    result = record_participation(
        normalized, day, "checkin", f"checkin:{normalized}:{day.isoformat()}",
        {"verified_by": "authenticated_same_origin_post"},
    )
    result["activity_day"] = day.isoformat()
    return result


def reconcile_approved_daily_tasks(day: date | None = None) -> dict:
    """Import only completed, paid/grandfathered Task rows for a UTC date.

    The original submission timestamp is retained for late approvals. Querying
    each source is bounded to that date and any missing table fails closed.
    """
    day = day or utc_date()
    start, end = week_bounds(day)
    # Backfill the active week so late moderation approvals retain their
    # original created_at day without scanning the full task history.
    db = _admin_db()
    imported = 0
    reversed_count = 0
    source_status: dict[str, bool] = {}
    tables = ("twitter_task_log", "telegram_task_log")
    for table in tables:
        try:
            table_rows = _bounded_rows(lambda: (
                db.table(table)
                .select("id,wallet_address,created_at,status,payment_status")
                .gte("created_at", datetime.combine(start, datetime.min.time(), timezone.utc).isoformat())
                .lt("created_at", datetime.combine(end, datetime.min.time(), timezone.utc).isoformat())
            ))
        except Exception as exc:
            # A missing/unavailable source must not be mistaken for an empty
            # verified result, nor block reconciliation of the other table.
            logger.warning("weekly_exp: %s evidence source unavailable: %s", table, exc)
            source_status[table] = False
            continue
        source_status[table] = True
        for row in table_rows:
            try:
                source_key = f"daily_task:{table}:{row['id']}"
                qualified = (str(row.get("status") or "").lower() == "completed"
                             and str(row.get("payment_status") or "").lower()
                             in {"paid", "grandfathered"})
                if not qualified:
                    reverse_participation("daily_task", source_key,
                                          "task_not_completed_or_payment_unverified")
                    reversed_count += 1
                    continue
                wallet = normalize_wallet(row.get("wallet_address"))
                created = row.get("created_at")
                activity_day = utc_date(created)
                record_participation(
                    wallet, activity_day, "daily_task",
                    source_key,
                    {"source_table": table, "source_row_id": str(row["id"]),
                     "approval_status": "completed", "payment_status": row.get("payment_status"),
                     "submitted_at": created},
                )
                imported += 1
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("weekly_exp: invalid %s evidence row skipped: %s", table, exc)
    return {"success": all(source_status.values()) and len(source_status) == len(tables),
            "sources": source_status, "week_start": start.isoformat(),
            "week_end_exclusive": end.isoformat(), "imported_rows": imported,
            "reversed_rows": reversed_count}


def reconcile_garden_events(day: date | None = None, web3_instance=None) -> dict:
    """Reconcile confirmed Garden V2 lifecycle logs for one UTC week.

    This is a backend-only worker function. It never runs from a feed request.
    A complete successful scan is required; RPC/receipt errors fail closed.
    """
    from . import garden_kpis
    from farming import blockchain as shared_chain

    day = day or utc_date()
    start, end = week_bounds(day)
    address = (os.getenv("COMMUNITY_GARDEN_CONTRACT_ADDRESS", "") or "").strip()
    enabled = (os.getenv("COMMUNITY_GARDEN_ENABLED", "NO") or "NO").strip().lower() in {
        "1", "true", "yes", "on", "enabled"
    }
    if not enabled or not _ADDRESS_RE.fullmatch(address):
        return {"success": False, "status": "not_configured", "imported_logs": 0}
    if shared_chain._get_logs_full is None:
        return {"success": False, "status": "scanner_unavailable", "imported_logs": 0}
    try:
        w3 = web3_instance or garden_kpis.garden_sponsors._web3()
        if int(w3.eth.chain_id) != 42220 or not w3.eth.get_code(address):
            raise RuntimeError("Configured Garden is not a contract on Celo Mainnet")
        latest = int(w3.eth.block_number)
        confirmations = max(1, min(100, int(os.getenv("WEEKLY_EXP_GARDEN_CONFIRMATIONS", "12"))))
        end_block = latest - confirmations + 1
        if end_block < 0:
            raise RuntimeError("Insufficient confirmed Celo blocks")

        timestamp_cache: dict[int, int] = {}
        def block_ts(number: int) -> int:
            if number not in timestamp_cache:
                timestamp_cache[number] = int(w3.eth.get_block(number)["timestamp"])
            return timestamp_cache[number]

        def first_block(target: int) -> int:
            if block_ts(end_block) < target:
                return end_block + 1
            low, high = 0, end_block + 1
            while low < high:
                middle = (low + high) // 2
                if block_ts(middle) >= target:
                    high = middle
                else:
                    low = middle + 1
            return low

        start_ts = int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp())
        end_ts = int(datetime.combine(end, datetime.min.time(), timezone.utc).timestamp())
        from_block = first_block(start_ts)
        if from_block <= end_block:
            logs = shared_chain._get_logs_full(
                {"address": address, "topics": [list(garden_kpis.GARDEN_ACTIVITY_TOPICS)]},
                from_block, end_block,
            ) or []
        else:
            logs = []

        timestamp_word = {
            garden_kpis.GARDEN_PLANTED_TOPIC: 3,
            garden_kpis.GARDEN_HARVESTED_TOPIC: 2,
            garden_kpis.GARDEN_EXITED_TOPIC: 1,
        }
        validated: list[dict] = []
        for log in logs:
            topics = log.get("topics") or []
            topic0 = str(topics[0] or "").lower() if topics else ""
            if topic0 not in timestamp_word:
                continue
            log_address = str(log.get("address") or "").lower()
            if log_address != address.lower() or len(topics) < 2:
                raise RuntimeError("Garden event log does not match configured contract")
            indexed = str(topics[1] or "").lower().removeprefix("0x")
            wallet = "0x" + indexed[-40:] if len(indexed) >= 40 else ""
            if not _ADDRESS_RE.fullmatch(wallet) or wallet == "0x" + "0" * 40:
                raise RuntimeError("Garden event has invalid indexed wallet")
            raw_data = log.get("data") or "0x"
            if isinstance(raw_data, (bytes, bytearray)):
                raw_data = "0x" + bytes(raw_data).hex()
            data_hex = str(raw_data).lower().removeprefix("0x")
            offset = timestamp_word[topic0] * 64
            try:
                event_ts = int(data_hex[offset:offset + 64], 16)
                block_number = int(log.get("blockNumber"), 16) if isinstance(log.get("blockNumber"), str) else int(log.get("blockNumber"))
                log_index = int(log.get("logIndex"), 16) if isinstance(log.get("logIndex"), str) else int(log.get("logIndex"))
                block_time = block_ts(block_number)
                tx_value = log.get("transactionHash")
                tx_hash = ("0x" + bytes(tx_value).hex() if isinstance(tx_value, (bytes, bytearray))
                           else str(tx_value or ""))
                if not tx_hash:
                    raise ValueError("missing transaction hash")
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                receipt_status = int(receipt.get("status", 0))
                receipt_block = int(receipt.get("blockNumber"))
                receipt_match = any(
                    str(receipt_log.get("address") or "").lower() == address.lower()
                    and int(receipt_log.get("logIndex")) == log_index
                    and [_hex_text(value) for value in (receipt_log.get("topics") or [])]
                    == [_hex_text(value) for value in topics]
                    for receipt_log in (receipt.get("logs") or [])
                )
            except Exception as exc:
                raise RuntimeError("Could not verify Garden event receipt") from exc
            if (receipt_status != 1 or receipt_block != block_number or not receipt_match
                    or latest - block_number + 1 < confirmations
                    or event_ts <= 0 or abs(event_ts - block_time) > 300
                    or not (start_ts <= block_time < end_ts)):
                raise RuntimeError("Garden event failed receipt, time or confirmation checks")
            validated.append({
                "wallet": wallet,
                "day": datetime.fromtimestamp(block_time, timezone.utc).date(),
                "source_key": f"garden:{tx_hash.lower()}:{log_index}",
                "evidence": {"contract_address": address.lower(), "chain_id": 42220,
                             "event_topic": topic0, "transaction_hash": tx_hash.lower(),
                             "log_index": log_index, "block_number": block_number,
                             "confirmations": latest - block_number + 1,
                             "block_timestamp": block_time, "event_timestamp": event_ts},
            })

        db = _admin_db()
        existing = _bounded_rows(lambda: (
            db.table("leaderboard_participation")
            .select("source_key")
            .eq("source", "garden")
            .gte("activity_day", start.isoformat())
            .lt("activity_day", end.isoformat())
        ))
        live_keys = {entry["source_key"] for entry in validated}
        for entry in validated:
            record_participation(entry["wallet"], entry["day"], "garden",
                                 entry["source_key"], entry["evidence"])
        reversed_count = 0
        for row in existing:
            key = row.get("source_key")
            if key and key not in live_keys:
                reverse_participation("garden", key, "event_missing_from_confirmed_reconciliation")
                reversed_count += 1
        return {"success": True, "status": "ready", "week_start": start.isoformat(),
                "week_end_exclusive": end.isoformat(), "scanned_from_block": from_block,
                "scanned_to_block": end_block, "confirmed_logs": len(validated),
                "imported_logs": len(validated), "reversed_logs": reversed_count,
                "updated_at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:  # noqa: BLE001 - reconciliation must fail closed
        logger.warning("weekly_exp: Garden reconciliation failed: %s", exc)
        return {"success": False, "status": "stale", "imported_logs": 0,
                "error": "Garden event evidence could not be fully verified."}


def score_wallet(wallet: str, start: date | None = None, end: date | None = None) -> dict:
    """Return one wallet's private evidence breakdown for a date range."""
    wallet = normalize_wallet(wallet)
    if start is None:
        start, end = week_bounds(utc_date())
    end = end or date.fromordinal(start.toordinal() + 7)
    evidence_start = date.fromordinal(max(1, start.toordinal() - 6))
    db = _admin_db()
    rows = _bounded_rows(lambda: (
        db.table("leaderboard_participation")
        .select("activity_day,source,verified,reversed_at")
        .eq("wallet_address", wallet)
        .gte("activity_day", evidence_start.isoformat())
        .lt("activity_day", end.isoformat())
        .eq("verified", True).is_("reversed_at", "null")
    ))
    return calculate_daily_points(rows, start=start, end=end)
