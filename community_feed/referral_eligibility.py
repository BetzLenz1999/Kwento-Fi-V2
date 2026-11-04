"""Server-side eligibility for the Community Referral Program entry point.

A user must visit an authenticated Community page on three consecutive UTC
calendar days. Eligibility is permanent once unlocked. State transitions are
performed by a database RPC so concurrent tabs/workers cannot double-count or
lose a day.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

MIN_STREAK_DAYS = 3
_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _wallet_key(wallet_address: str) -> str:
    value = str(wallet_address or "").strip()
    if not _WALLET_RE.fullmatch(value):
        raise ValueError("A valid EVM wallet address is required")
    return value.lower()


def _database():
    from supabase_client import get_supabase_admin_client

    client = get_supabase_admin_client()
    if client is None:
        raise RuntimeError("Service-role database access is unavailable")
    return client


def _normalize_status(row: dict | None) -> dict:
    row = row or {}
    try:
        streak_days = max(0, min(MIN_STREAK_DAYS, int(row.get("streak_days") or 0)))
    except (TypeError, ValueError):
        streak_days = 0
    unlocked_at = row.get("unlocked_at")
    return {
        "eligible": bool(unlocked_at),
        "streak_days": MIN_STREAK_DAYS if unlocked_at else streak_days,
        "days_required": MIN_STREAK_DAYS,
        "days_remaining": 0 if unlocked_at else max(0, MIN_STREAK_DAYS - streak_days),
        "last_active_date": row.get("last_active_date"),
        "unlocked_at": unlocked_at,
    }


def record_community_referral_visit(wallet_address: str) -> dict:
    """Record one authenticated Community visit for the current UTC date."""
    wallet = _wallet_key(wallet_address)
    active_date = datetime.now(timezone.utc).date().isoformat()
    response = _database().rpc(
        "record_community_referral_daily_visit",
        {"p_wallet_address": wallet, "p_active_date": active_date},
    ).execute()
    data = getattr(response, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        raise RuntimeError("Referral eligibility RPC returned an invalid response")
    return _normalize_status(data)


def get_community_referral_status(wallet_address: str) -> dict:
    """Read eligibility without recording activity (used by the protected API)."""
    wallet = _wallet_key(wallet_address)
    response = (
        _database().table("community_referral_eligibility")
        .select("streak_days,last_active_date,unlocked_at")
        .eq("wallet_address", wallet)
        .limit(1)
        .execute()
    )
    rows = getattr(response, "data", None) or []
    return _normalize_status(rows[0] if rows else None)
