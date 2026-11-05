"""Read-only on-chain KPI aggregation for the GoodMarket Chicken Farm V2 contract.

The /data dashboard uses the public contract address and deployment block only.
It never needs or returns the Farming V2 admin or gas-sponsor private keys.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from farming import blockchain

logger = logging.getLogger("farming.v2_kpis")

FARM_STARTED_TOPIC = "0x7149cad02ed72d4294d82633c1daeb9025185ce693f5988f3933a931a17cbda2"
REWARD_CLAIMED_TOPIC = "0x812be816db82c66cd18ca8457005cd84689642d8ac4d38599cc6af444a2dc72a"
FARM_CLOSED_TOPIC = "0xbe40ce4ce3eec04f7b492e9df5b191110e2860b5a423176001e015f94786f78a"
REWARD_POOL_FUNDED_TOPIC = "0xd3c7d1587d8f93bae0fcfc5a820d2187e755a75ec28a06c20a9da047e1b01eb5"

EVENT_TOPICS = {
    "farm_started": FARM_STARTED_TOPIC,
    "reward_claimed": REWARD_CLAIMED_TOPIC,
    "farm_closed": FARM_CLOSED_TOPIC,
    "reward_pool_funded": REWARD_POOL_FUNDED_TOPIC,
}
ALL_EVENT_TOPICS = tuple(EVENT_TOPICS.values())
FARMER_EVENT_TOPICS = frozenset((FARM_STARTED_TOPIC, REWARD_CLAIMED_TOPIC, FARM_CLOSED_TOPIC))
CACHE_TTL_SECONDS = 300

_kpi_lock = threading.Lock()
_kpi_cache: dict[str, dict] = {}
_kpi_refreshing: set[str] = set()
_kpi_errors: dict[str, dict] = {}
_month_start_block_cache: dict[tuple[str, str], int] = {}


def _empty_result(status: str, period_month: str, *, error: str | None = None) -> dict:
    result = {
        "status": status,
        "period_month": period_month,
        "active_farmers": None,
        "total_farmers": None,
        "monthly_active_farmers": None,
        "total_transactions": None,
        "monthly_transactions": None,
        "transactions_by_type": None,
        "updated_at_utc": None,
        "refreshing": status == "loading",
        "source": "GoodMarketChickenFarmV2 on-chain events",
    }
    if error:
        result["error"] = error
    return result


def _as_int(value) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except (TypeError, ValueError):
        return 0


def _wallet_from_log(log: dict) -> str:
    topics = log.get("topics") or []
    if len(topics) < 2:
        return ""
    indexed = str(topics[1] or "").lower()
    if indexed.startswith("0x"):
        indexed = indexed[2:]
    if len(indexed) < 40:
        return ""
    wallet = "0x" + indexed[-40:]
    return "" if wallet == "0x" + "0" * 40 else wallet


def _event_name(topic: str) -> str | None:
    for name, event_topic in EVENT_TOPICS.items():
        if topic == event_topic:
            return name
    return None


def _log_order(log: dict) -> tuple[int, int, int]:
    return (
        _as_int(log.get("blockNumber")),
        _as_int(log.get("transactionIndex")),
        _as_int(log.get("logIndex")),
    )


def _get_web3(web3_instance=None):
    if web3_instance is not None:
        return web3_instance
    from web3 import Web3
    from web3.providers.rpc import HTTPProvider

    rpc_urls = blockchain._celo_rpc_urls() if blockchain._celo_rpc_urls else []
    rpc_url = os.getenv("CELO_RPC_URL", "").strip() or (rpc_urls[0] if rpc_urls else "https://forno.celo.org")
    return Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))


def _month_start_block(contract_key: str, now: datetime, latest_block: int, web3_instance) -> int:
    period_month = now.strftime("%Y-%m")
    cache_key = (contract_key, period_month)
    with _kpi_lock:
        cached = _month_start_block_cache.get(cache_key)
    if cached is not None:
        return cached

    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    target_timestamp = int(month_start.timestamp())
    timestamps: dict[int, int] = {}

    def block_timestamp(block_number: int) -> int:
        if block_number not in timestamps:
            block = web3_instance.eth.get_block(block_number)
            timestamps[block_number] = int(block["timestamp"] if isinstance(block, dict) else block.timestamp)
        return timestamps[block_number]

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
    with _kpi_lock:
        _month_start_block_cache[cache_key] = int(first_block)
    return int(first_block)


def _scan_farming_v2_kpi_stats(contract_address: str, now: datetime | None = None, web3_instance=None) -> dict:
    """Incrementally scan V2 events; active status is derived from start/close transitions."""
    contract = (contract_address or "").strip()
    contract_key = contract.lower()
    if not contract or not blockchain._get_logs_full:
        raise RuntimeError("Farming V2 contract or shared Celo log scanner is unavailable")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", contract):
        raise RuntimeError("FARMINGV2_CONTRACT_ADDRESS is not a valid EVM address")

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    period_month = now.strftime("%Y-%m")

    w3 = _get_web3(web3_instance)
    if int(w3.eth.chain_id) != 42220:
        raise RuntimeError("Farming V2 KPI RPC is not connected to Celo mainnet")
    latest_block = int(w3.eth.block_number)
    if latest_block <= 0:
        raise RuntimeError("Could not get the latest Celo block")
    if not w3.eth.get_code(contract):
        raise RuntimeError("FARMINGV2_CONTRACT_ADDRESS has no contract code on Celo")

    with _kpi_lock:
        previous = _kpi_cache.get(contract_key)
        previous = dict(previous) if previous else {}
    farmers = set(previous.get("farmers") or ())
    active_farmers = set(previous.get("active_farmers") or ())
    event_counts = {name: int((previous.get("event_counts") or {}).get(name, 0)) for name in EVENT_TOPICS}

    deploy_block = previous.get("deploy_block")
    if deploy_block is None:
        deploy_block = blockchain.get_deploy_block(
            contract, latest_block=latest_block, env_var_name="FARMINGV2_DEPLOY_BLOCK"
        )
    if not deploy_block:
        raise RuntimeError("Could not determine the V2 deployment block; set FARMINGV2_DEPLOY_BLOCK")
    last_scanned_block = previous.get("last_scanned_block")
    from_block = int(deploy_block) if last_scanned_block is None else int(last_scanned_block) + 1

    if from_block <= latest_block:
        logs = blockchain._get_logs_full(
            {"address": contract, "topics": [list(ALL_EVENT_TOPICS)]},
            from_block,
            latest_block,
        ) or []
        for log in sorted(logs, key=_log_order):
            topics = log.get("topics") or []
            topic = str(topics[0] or "").lower() if topics else ""
            name = _event_name(topic)
            wallet = _wallet_from_log(log)
            if not name or not wallet:
                continue
            event_counts[name] += 1
            if topic == FARM_STARTED_TOPIC:
                farmers.add(wallet)
                active_farmers.add(wallet)
            elif topic == FARM_CLOSED_TOPIC:
                active_farmers.discard(wallet)

    month_first_block = _month_start_block(contract_key, now, latest_block, w3)
    monthly_wallets: set[str] = set()
    monthly_transactions = 0
    if month_first_block <= latest_block:
        month_logs = blockchain._get_logs_full(
            {"address": contract, "topics": [list(ALL_EVENT_TOPICS)]},
            month_first_block,
            latest_block,
        ) or []
        for log in month_logs:
            topics = log.get("topics") or []
            topic = str(topics[0] or "").lower() if topics else ""
            if topic not in EVENT_TOPICS.values():
                continue
            monthly_transactions += 1
            if topic in FARMER_EVENT_TOPICS:
                wallet = _wallet_from_log(log)
                if wallet:
                    monthly_wallets.add(wallet)

    stats = {
        "status": "ready",
        "period_month": period_month,
        "active_farmers": len(active_farmers),
        "total_farmers": len(farmers),
        "monthly_active_farmers": len(monthly_wallets),
        "total_transactions": sum(event_counts.values()),
        "monthly_transactions": monthly_transactions,
        "transactions_by_type": dict(event_counts),
        "range_start_block": int(deploy_block),
        "range_end_block": latest_block,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "refreshing": False,
        "source": "GoodMarketChickenFarmV2 FarmStarted, RewardClaimed, FarmClosed, and RewardPoolFunded events",
    }
    with _kpi_lock:
        _kpi_cache[contract_key] = {
            "stats": stats,
            "farmers": farmers,
            "active_farmers": active_farmers,
            "event_counts": event_counts,
            "period_month": period_month,
            "deploy_block": int(deploy_block),
            "last_scanned_block": latest_block,
            "expires_at": time.time() + CACHE_TTL_SECONDS,
        }
        _kpi_errors.pop(contract_key, None)
    return stats


def _refresh_farming_v2_kpi_stats(contract_address: str) -> None:
    contract_key = (contract_address or "").strip().lower()
    try:
        _scan_farming_v2_kpi_stats(contract_address)
    except Exception as exc:
        logger.warning("Farming V2 KPI refresh failed (%s)", type(exc).__name__)
        with _kpi_lock:
            _kpi_errors[contract_key] = {"message": "Farming V2 on-chain event data is temporarily unavailable", "retry_after": time.time() + 60}
    finally:
        with _kpi_lock:
            _kpi_refreshing.discard(contract_key)


def get_farming_v2_kpi_stats(contract_address: str | None = None) -> dict:
    """Return cached V2 metrics immediately and scan stale history in a daemon thread."""
    contract = (contract_address if contract_address is not None else os.getenv("FARMINGV2_CONTRACT_ADDRESS", "")).strip()
    now = datetime.now(timezone.utc)
    period_month = now.strftime("%Y-%m")
    if not contract:
        return _empty_result("not_configured", period_month)
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", contract):
        return _empty_result("error", period_month, error="FARMINGV2_CONTRACT_ADDRESS is not a valid EVM address")

    contract_key = contract.lower()
    with _kpi_lock:
        entry = _kpi_cache.get(contract_key)
        error = _kpi_errors.get(contract_key)
        refreshing = contract_key in _kpi_refreshing
        same_month = bool(entry and entry.get("period_month") == period_month)
        fresh = bool(same_month and entry.get("expires_at", 0) > time.time())
        if not fresh and not refreshing and not (error and error.get("retry_after", 0) > time.time()):
            _kpi_refreshing.add(contract_key)
            refreshing = True
            try:
                threading.Thread(
                    target=_refresh_farming_v2_kpi_stats,
                    args=(contract,),
                    name="farming-v2-kpi-refresh",
                    daemon=True,
                ).start()
            except Exception as exc:
                _kpi_refreshing.discard(contract_key)
                _kpi_errors[contract_key] = {"message": "Farming V2 on-chain event data is temporarily unavailable", "retry_after": time.time() + 60}
                error = _kpi_errors[contract_key]

        if same_month and entry and entry.get("stats"):
            result = dict(entry["stats"])
            result["refreshing"] = refreshing and not fresh
            if error:
                result["status"] = "error"
                result["error"] = error.get("message", "Farming V2 event refresh failed")
            return result
        if refreshing:
            result = _empty_result("loading", period_month)
            result["refreshing"] = True
            if entry and entry.get("stats"):
                for key in ("active_farmers", "total_farmers", "total_transactions", "transactions_by_type", "updated_at_utc"):
                    result[key] = entry["stats"].get(key)
            return result
        error = _kpi_errors.get(contract_key)
        if error:
            result = _empty_result("error", period_month, error=error.get("message"))
            if entry and entry.get("stats"):
                for key in ("active_farmers", "total_farmers", "total_transactions", "transactions_by_type", "updated_at_utc"):
                    result[key] = entry["stats"].get(key)
            return result
        return _empty_result("loading", period_month)
