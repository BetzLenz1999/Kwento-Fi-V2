#!/usr/bin/env python3
"""Backfill user_activity_daily from the durable community tables.

Why
---
``user_sessions`` is purged to a rolling 7-day window, so it cannot supply the
history retention needs. The community_* tables are NOT purged and each carries
a wallet address plus a timestamp, so they can reconstruct a usable activity
history: one (wallet, UTC day) row per wallet per day it did something visible.

Sources (all lowercased to match the rollup):
    community_reward_ledger        created_at   (feed reactions/comments, daily learn)
    community_daily_learn_attempts day_utc      (quiz participation)
    community_feed_posts           created_at   (posting)
    community_chat_messages        created_at   (chat)
    community_user_badges          awarded_at   (badge awards)

This is a PROXY for full-platform activity: it covers the community surfaces,
not on-chain garden/farming/paluwagan actions. Say so when reporting a number.

Usage:
    python scripts/backfill_user_activity_daily.py --dry-run
    python scripts/backfill_user_activity_daily.py --since 2026-01-01
    python scripts/backfill_user_activity_daily.py --apply

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY, and that
``sql/user_activity_daily.sql`` has been applied. Exit codes:
    0 — success, 1 — nothing to do / partial failure, 2 — configuration error
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retention_service import normalize_wallet, to_utc_date  # noqa: E402

# (table, timestamp column) pairs that carry per-wallet dated activity.
SOURCES = (
    ("community_reward_ledger", "created_at"),
    ("community_daily_learn_attempts", "day_utc"),
    ("community_feed_posts", "created_at"),
    ("community_chat_messages", "created_at"),
    ("community_user_badges", "awarded_at"),
)

PAGE_SIZE = 1000


def _paged_days(client, table: str, column: str, since: date) -> list[tuple[str, str]]:
    """Return distinct (wallet, day) pairs for one source table."""
    pairs: set[tuple[str, str]] = set()
    offset = 0
    while True:
        query = (
            client.table(table)
            .select(f"wallet_address,{column}")
            .gte(column, since.isoformat())
            .order(column)
            .range(offset, offset + PAGE_SIZE - 1)
        )
        batch = getattr(query.execute(), "data", None) or []
        for row in batch:
            wallet = normalize_wallet(row.get("wallet_address"))
            day = to_utc_date(row.get(column))
            if wallet and day and day >= since:
                pairs.add((wallet, day.isoformat()))
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return sorted(pairs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=None,
                        help="Earliest day to backfill (YYYY-MM-DD). Default: 365 days ago.")
    parser.add_argument("--apply", action="store_true",
                        help="Write the rows. Without this the run is a dry run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Explicitly do nothing but report (default behaviour).")
    args = parser.parse_args()

    since = (date.fromisoformat(args.since) if args.since
             else datetime.now(timezone.utc).date() - timedelta(days=365))

    try:
        from supabase_client import get_supabase_admin_client

        client = get_supabase_admin_client()
    except Exception as exc:  # noqa: BLE001
        print(f"configuration error: could not build the admin client: {exc}")
        return 2

    if client is None:
        print("configuration error: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")
        return 2

    print(f"Backfilling user_activity_daily since {since.isoformat()} "
          f"({'APPLY' if args.apply else 'DRY RUN'})")

    all_pairs: set[tuple[str, str]] = set()
    for table, column in SOURCES:
        try:
            pairs = _paged_days(client, table, column, since)
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
            print(f"  ! {table}.{column}: skipped ({exc})")
            continue
        all_pairs.update(pairs)
        print(f"  {table}.{column}: {len(pairs)} (wallet, day) pairs")

    print(f"Total distinct (wallet, day) pairs: {len(all_pairs)}")
    if not args.apply:
        print("Dry run complete. Re-run with --apply to write.")
        return 0

    written = failed = 0
    for wallet, day_text in sorted(all_pairs):
        try:
            client.rpc("user_activity_daily_touch",
                       {"p_wallet_address": wallet, "p_day_utc": day_text}).execute()
            written += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if failed <= 5:
                print(f"  ! {wallet} {day_text}: {exc}")
        if (written + failed) % 500 == 0:
            print(f"  ... {written} written, {failed} failed")

    print(f"Done: {written} written, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
