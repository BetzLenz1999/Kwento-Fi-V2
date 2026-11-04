"""On-chain Community Garden V2 activity KPIs for the admin data page."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, time as datetime_time, timedelta, timezone

from eth_hash.auto import keccak

from farming import blockchain as shared_chain
from . import garden_sponsors

logger = logging.getLogger(__name__)
CELO_CHAIN_ID = 42220
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Gardener activity excludes SponsorPoolFunded; only gardeners' lifecycle
# actions count as active Community Garden users.
GARDEN_PLANTED_TOPIC = "0x" + keccak(b"GardenPlanted(address,uint256,uint256,uint256,uint256,uint256)").hex()
GARDEN_HARVESTED_TOPIC = "0x" + keccak(b"GardenHarvested(address,uint256,uint256,uint256)").hex()
GARDEN_EXITED_TOPIC = "0x" + keccak(b"GardenExited(address,uint256,uint256)").hex()
GARDEN_ACTIVITY_TOPICS = (
    GARDEN_PLANTED_TOPIC,
    GARDEN_HARVESTED_TOPIC,
    GARDEN_EXITED_TOPIC,
)

_garden_kpi_lock = threading.Lock()
_monthly_activity_cache: dict = {}
_utc_start_block_cache: dict = {}
_total_gardeners_cache: dict = {}
_total_gardeners_refreshing: set[str] = set()
_total_gardeners_errors: dict = {}
MONTHLY_CACHE_TTL = 120
TOTAL_GARDENERS_CACHE_TTL = 300


def _normalize_address(address: str | None) -> str:
    value = str(address or "").strip()
    if not value:
        return ""
    if not _ADDRESS_RE.fullmatch(value):
        raise RuntimeError("COMMUNITY_GARDEN_CONTRACT_ADDRESS is not a valid EVM address")
    return value


def _validated_web3(address: str, web3_instance=None):
    w3 = web3_instance or garden_sponsors._web3()
    if int(w3.eth.chain_id) != CELO_CHAIN_ID:
        raise RuntimeError("Community Garden KPI RPC is not connected to Celo Mainnet")
    if not w3.eth.get_code(address):
        raise RuntimeError("COMMUNITY_GARDEN_CONTRACT_ADDRESS has no contract code on Celo")
    return w3


def get_garden_activity_stats(
    contract_address: str | None = None,
    now: datetime | None = None,
    web3_instance=None,
) -> dict:
    """Count unique V2 gardener wallets in current UTC daily, weekly, and monthly windows."""
    address = _normalize_address(
        contract_address if contract_address is not None
        else os.getenv("COMMUNITY_GARDEN_CONTRACT_ADDRESS", "")
    )
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    period_month = now.strftime("%Y-%m")
    today_utc = now.date()
    if not address:
        return {
            "status": "not_configured",
            "period_month": period_month,
            "daily_active_users": None,
            "weekly_active_users": None,
            "monthly_active_users": None,
            "updated_at_utc": None,
        }
    if shared_chain._get_logs_full is None:
        raise RuntimeError("Shared Celo event-log scanner is unavailable")

    cache_key = (address.lower(), today_utc.isoformat())
    with _garden_kpi_lock:
        cached = _monthly_activity_cache.get(cache_key)
        if cached and cached.get("expires_at", 0) > time.time():
            return dict(cached["stats"])

    w3 = _validated_web3(address, web3_instance)
    latest_block = int(w3.eth.block_number)
    if latest_block <= 0:
        raise RuntimeError("Could not read the latest Celo block")

    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    day_start = datetime.combine(today_utc, datetime_time.min, tzinfo=timezone.utc)
    week_start_day = today_utc - timedelta(days=6)
    week_start = datetime.combine(week_start_day, datetime_time.min, tzinfo=timezone.utc)
    timestamp_cache: dict[int, int] = {}

    def first_block_on_or_after(target_timestamp: int, period_key: str) -> int:
        start_key = (address.lower(), period_key)
        with _garden_kpi_lock:
            cached_block = _utc_start_block_cache.get(start_key)
        if cached_block is not None:
            return int(cached_block)

        def block_timestamp(block_number: int) -> int:
            number = int(block_number)
            if number not in timestamp_cache:
                timestamp_cache[number] = int(w3.eth.get_block(number)["timestamp"])
            return timestamp_cache[number]

        if block_timestamp(latest_block) < target_timestamp:
            first_block = latest_block + 1
        else:
            low, high = 0, latest_block + 1
            while low < high:
                middle = (low + high) // 2
                if block_timestamp(middle) >= target_timestamp:
                    high = middle
                else:
                    low = middle + 1
            first_block = low
        with _garden_kpi_lock:
            if len(_utc_start_block_cache) >= 512:
                _utc_start_block_cache.pop(next(iter(_utc_start_block_cache)))
            _utc_start_block_cache[start_key] = int(first_block)
        return int(first_block)

    monthly_start_block = first_block_on_or_after(
        int(month_start.timestamp()), period_month
    )
    weekly_start_block = first_block_on_or_after(
        int(week_start.timestamp()), week_start_day.isoformat()
    )
    scan_start_block = min(monthly_start_block, weekly_start_block)
    daily_wallets: set[str] = set()
    weekly_wallets: set[str] = set()
    monthly_wallets: set[str] = set()
    if scan_start_block <= latest_block:
        logs = shared_chain._get_logs_full(
            {"address": address, "topics": [list(GARDEN_ACTIVITY_TOPICS)]},
            int(scan_start_block),
            latest_block,
        )
        month_start_ts = int(month_start.timestamp())
        day_start_ts = int(day_start.timestamp())
        week_start_ts = int(week_start.timestamp())
        now_ts = int(now.timestamp())
        timestamp_word_by_topic = {
            GARDEN_PLANTED_TOPIC: 3,
            GARDEN_HARVESTED_TOPIC: 2,
            GARDEN_EXITED_TOPIC: 1,
        }
        for log in logs or []:
            topics = log.get("topics") or []
            topic0 = str(topics[0] or "").lower() if topics else ""
            indexed_gardener = str(topics[1] or "").lower() if len(topics) > 1 else ""
            wallet = "0x" + indexed_gardener[-40:] if len(indexed_gardener) >= 42 else ""
            word_index = timestamp_word_by_topic.get(topic0)
            if word_index is None:
                continue
            raw_data = log.get("data") or "0x"
            if isinstance(raw_data, (bytes, bytearray)):
                raw_data = "0x" + bytes(raw_data).hex()
            data_hex = str(raw_data).lower().removeprefix("0x")
            word_start = int(word_index) * 64 if word_index is not None else -1
            try:
                event_timestamp = int(data_hex[word_start:word_start + 64], 16)
            except (TypeError, ValueError):
                continue
            if not wallet or wallet == "0x" + ("0" * 40) or event_timestamp > now_ts:
                continue
            if event_timestamp >= month_start_ts:
                monthly_wallets.add(wallet)
            if event_timestamp >= week_start_ts:
                weekly_wallets.add(wallet)
            if event_timestamp >= day_start_ts:
                daily_wallets.add(wallet)

    stats = {
        "status": "ready",
        "period_month": period_month,
        "daily_active_users": len(daily_wallets),
        "weekly_active_users": len(weekly_wallets),
        "monthly_active_users": len(monthly_wallets),
        "range_start_block": int(monthly_start_block),
        "scan_start_block": int(scan_start_block),
        "range_end_block": latest_block,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Community Garden V2 GardenPlanted, GardenHarvested, and GardenExited events",
    }
    with _garden_kpi_lock:
        _monthly_activity_cache[cache_key] = {
            "stats": stats,
            "expires_at": time.time() + MONTHLY_CACHE_TTL,
        }
    return stats


def _scan_total_gardeners(contract_address: str) -> dict:
    """Incrementally count unique V2 gardeners from all GardenPlanted events."""
    address = _normalize_address(contract_address)
    if not address or shared_chain._get_logs_full is None:
        raise RuntimeError("Community Garden contract or shared Celo log scanner is unavailable")
    latest_block = shared_chain._get_latest_block_number()
    if not latest_block:
        raise RuntimeError("Could not read the latest Celo block")
    address_key = address.lower()
    with _garden_kpi_lock:
        previous = _total_gardeners_cache.get(address_key)
        previous = dict(previous) if previous else {}
    gardeners = set(previous.get("gardeners") or ())
    deploy_block = previous.get("deploy_block")
    if deploy_block is None:
        deploy_block = shared_chain.get_deploy_block(
            address,
            latest_block=latest_block,
            env_var_name="COMMUNITY_GARDEN_DEPLOY_BLOCK",
        )
    if not deploy_block:
        raise RuntimeError(
            "Could not determine the Community Garden V2 deploy block; set COMMUNITY_GARDEN_DEPLOY_BLOCK"
        )
    last_scanned = previous.get("last_scanned_block")
    from_block = int(deploy_block) if last_scanned is None else int(last_scanned) + 1
    if from_block <= latest_block:
        logs = shared_chain._get_logs_full(
            {"address": address, "topics": [[GARDEN_PLANTED_TOPIC]]},
            from_block,
            latest_block,
        )
        for log in logs or []:
            topics = log.get("topics") or []
            topic0 = str(topics[0] or "").lower() if topics else ""
            indexed_gardener = str(topics[1] or "").lower() if len(topics) > 1 else ""
            wallet = "0x" + indexed_gardener[-40:] if len(indexed_gardener) >= 42 else ""
            if topic0 == GARDEN_PLANTED_TOPIC and wallet and wallet != "0x" + ("0" * 40):
                gardeners.add(wallet)

    stats = {
        "status": "ready",
        "total_gardeners": len(gardeners),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Community Garden V2 GardenPlanted events",
    }
    with _garden_kpi_lock:
        _total_gardeners_cache[address_key] = {
            "stats": stats,
            "gardeners": gardeners,
            "deploy_block": int(deploy_block),
            "last_scanned_block": int(latest_block),
            "expires_at": time.time() + TOTAL_GARDENERS_CACHE_TTL,
        }
        _total_gardeners_errors.pop(address_key, None)
    return stats


def _refresh_total_gardeners(contract_address: str) -> None:
    address_key = (contract_address or "").strip().lower()
    try:
        _scan_total_gardeners(contract_address)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Community Garden total-gardener refresh failed: %s", exc)
        with _garden_kpi_lock:
            _total_gardeners_errors[address_key] = {
                "message": str(exc),
                "retry_after": time.time() + 60,
            }
    finally:
        with _garden_kpi_lock:
            _total_gardeners_refreshing.discard(address_key)


def get_total_gardeners_stats(contract_address: str | None = None) -> dict:
    """Return the cached V2 total and start one incremental background refresh."""
    address = _normalize_address(
        contract_address if contract_address is not None
        else os.getenv("COMMUNITY_GARDEN_CONTRACT_ADDRESS", "")
    )
    if not address:
        return {"status": "not_configured", "total_gardeners": None, "updated_at_utc": None}
    key = address.lower()
    now = time.time()
    with _garden_kpi_lock:
        entry = _total_gardeners_cache.get(key)
        entry = dict(entry) if entry else None
        error = _total_gardeners_errors.get(key)
        refreshing = key in _total_gardeners_refreshing
        fresh = bool(entry and entry.get("expires_at", 0) > now)
        if not fresh and not refreshing and not (error and error.get("retry_after", 0) > now):
            _total_gardeners_refreshing.add(key)
            refreshing = True
            try:
                threading.Thread(
                    target=_refresh_total_gardeners,
                    args=(address,),
                    name="community-garden-kpi-refresh",
                    daemon=True,
                ).start()
            except Exception as exc:  # noqa: BLE001
                _total_gardeners_refreshing.discard(key)
                _total_gardeners_errors[key] = {
                    "message": str(exc),
                    "retry_after": now + 60,
                }
                error = _total_gardeners_errors[key]
        if entry and entry.get("stats"):
            result = dict(entry["stats"])
            result["refreshing"] = refreshing and not fresh
            return result
        if refreshing:
            return {
                "status": "loading",
                "total_gardeners": None,
                "updated_at_utc": None,
                "refreshing": True,
            }
        error = _total_gardeners_errors.get(key)
        if error:
            return {
                "status": "error",
                "total_gardeners": None,
                "updated_at_utc": None,
                "error": error.get("message", "Community Garden history is unavailable"),
                "refreshing": False,
            }
    return {"status": "loading", "total_gardeners": None, "updated_at_utc": None, "refreshing": False}
