"""Admin-only, read-only weekly EXP allocation previews and budget checks.

No function in this module writes configuration, finalizes a week, publishes a
root, or transfers G$. The proposed G$ budget cap is server-configured; the
funded balance is read from the configured Celo G$ token and treasury address.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from urllib.parse import urlsplit

from env_utils import get_env_int

logger = logging.getLogger(__name__)
_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_AMOUNT_RE = re.compile(r"^\d{1,18}(?:\.\d{1,18})?$")
_TOKEN_DECIMALS = 18
_TOKEN_UNIT = Decimal("0.000000000000000001")
_MAX_GD_AMOUNT = Decimal("1000000000000000000")
_MAX_PREVIEW_ROWS = 10_000
_DEFAULT_MAX_SNAPSHOT_AGE_HOURS = 36


def _max_top_n() -> int:
    """Admin-configurable Top N ceiling (no fixed 10/20 cap).

    Falls back to the environment/default when imported outside the package
    (for example, by a standalone test loader) so the bound stays consistent
    with ``weekly_exp.max_top_n``.
    """
    try:
        from . import weekly_exp
        return weekly_exp.max_top_n()
    except Exception:  # noqa: BLE001 - standalone import context
        return max(1, min(10_000, get_env_int("WEEKLY_EXP_MAX_TOP_N", 10_000)))


class PreviewInputError(ValueError):
    """A bounded, user-correctable allocation input error."""


class SnapshotDataError(RuntimeError):
    """Corrupt or incomplete persisted snapshot data; fail the preview closed."""


def _amount(value, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PreviewInputError(f"{name} must be a decimal string or integer.")
    text = str(value).strip()
    if not _AMOUNT_RE.fullmatch(text):
        raise PreviewInputError(f"{name} must be a non-negative G$ amount with at most 18 decimals.")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise PreviewInputError(f"{name} is not a valid G$ amount.") from exc
    if not parsed.is_finite() or parsed < 0 or parsed > _MAX_GD_AMOUNT:
        raise PreviewInputError(f"{name} is outside the allowed range.")
    return parsed


def _format_gd(value: Decimal) -> str:
    with localcontext() as ctx:
        ctx.prec = 100
        text = format(value.quantize(_TOKEN_UNIT, rounding=ROUND_DOWN), ".18f")
    return text.rstrip("0").rstrip(".") or "0"


def _integer(value, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise PreviewInputError(f"{name} must be an integer from {minimum} to {maximum}.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PreviewInputError(f"{name} must be an integer from {minimum} to {maximum}.") from exc
    if str(value).strip() != str(parsed) or not minimum <= parsed <= maximum:
        raise PreviewInputError(f"{name} must be an integer from {minimum} to {maximum}.")
    return parsed


def _parse_snapshot_time(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validated_scores(rows: list[dict]) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > _MAX_PREVIEW_ROWS:
        raise SnapshotDataError("Snapshot score rows exceed the preview limit.")
    output = []
    seen_wallets = set()
    seen_ranks = set()
    for row in rows:
        if not isinstance(row, dict):
            raise SnapshotDataError("Snapshot contains an invalid score row.")
        wallet = str(row.get("wallet_address") or "")
        if not _ADDRESS_RE.fullmatch(wallet):
            raise SnapshotDataError("Snapshot contains an invalid wallet address.")
        wallet = wallet.lower()
        try:
            rank = int(row.get("rank"))
            exp = int(row.get("total_exp"))
        except (TypeError, ValueError) as exc:
            raise SnapshotDataError("Snapshot contains invalid rank or EXP values.") from exc
        breakdown = row.get("breakdown")
        if not isinstance(breakdown, dict):
            raise SnapshotDataError("Snapshot is missing private EXP breakdown data.")
        try:
            substantive_days = int(breakdown.get("substantive_days"))
            daily_task_days = int(breakdown.get("daily_task_days"))
            exp_breakdown = {
                "checkin_exp": int(breakdown.get("checkin_exp")),
                "daily_task_exp": int(breakdown.get("daily_task_exp")),
                "garden_exp": int(breakdown.get("garden_exp")),
                "streak_exp": int(breakdown.get("streak_exp")),
            }
        except (TypeError, ValueError) as exc:
            raise SnapshotDataError("Snapshot contains an invalid EXP breakdown.") from exc
        if (rank < 1 or exp < 1 or exp > 100_000_000
                or not 0 <= substantive_days <= 7 or not 0 <= daily_task_days <= 7
                or any(value < 0 for value in exp_breakdown.values())
                or sum(exp_breakdown.values()) != exp
                or wallet in seen_wallets or rank in seen_ranks):
            raise SnapshotDataError("Snapshot contains out-of-range, inconsistent, or duplicate score data.")
        seen_wallets.add(wallet)
        seen_ranks.add(rank)
        output.append({
            "wallet_address": wallet,
            "rank": rank,
            "total_exp": exp,
            "substantive_days": substantive_days,
            "daily_task_days": daily_task_days,
            "exp_breakdown": exp_breakdown,
        })
    output.sort(key=lambda item: item["rank"])
    if output and [item["rank"] for item in output] != list(range(1, len(output) + 1)):
        raise SnapshotDataError("Snapshot ranks are incomplete; refresh the source snapshot before previewing.")
    return output


def _settings(data: dict) -> dict:
    if not isinstance(data, dict):
        raise PreviewInputError("Preview settings must be a JSON object.")
    top_n = _integer(data.get("top_n"), "Top N", 1, _max_top_n())
    distribution = data.get("distribution")
    if distribution not in {"equal_pool", "fixed_by_rank"}:
        raise PreviewInputError("Choose equal_pool or fixed_by_rank distribution.")
    pool = _amount(data.get("ranked_pool_gd", "0"), "Ranked pool")
    consolation = _amount(data.get("consolation_gd", "0"), "Consolation amount")
    minimum_exp = _integer(data.get("minimum_exp", 0), "Minimum EXP", 0, 100_000_000)
    minimum_days = _integer(data.get("minimum_substantive_days", 0),
                            "Minimum substantive days", 0, 7)
    fixed = data.get("fixed_prizes_gd", [])
    if not isinstance(fixed, list) or len(fixed) > _max_top_n():
        raise PreviewInputError(
            f"Fixed prize amounts must be a list of at most {_max_top_n()} values.")
    prizes = [_amount(value, f"Fixed prize #{index + 1}")
              for index, value in enumerate(fixed)]
    if distribution == "fixed_by_rank" and len(prizes) != top_n:
        raise PreviewInputError("Enter exactly one fixed G$ prize for each configured rank.")
    return {
        "top_n": top_n,
        "distribution": distribution,
        "ranked_pool": pool,
        "fixed_prizes": prizes,
        "consolation": consolation,
        "minimum_exp": minimum_exp,
        "minimum_substantive_days": minimum_days,
    }


def compute_allocation(score_rows: list[dict], settings: dict) -> dict:
    """Deterministically allocate a validated score set under a config.

    This is the single source of truth shared by the admin preview and the
    finalization workflow, so a finalized ledger can never disagree with what
    the admin reviewed. It never reads or writes any store.
    """
    scores = _validated_scores(score_rows)
    config = _settings(settings)
    winners = scores[:config["top_n"]]
    if config["distribution"] == "equal_pool" and winners:
        with localcontext() as ctx:
            ctx.prec = 60
            each = (config["ranked_pool"] / Decimal(len(winners))).quantize(
                _TOKEN_UNIT, rounding=ROUND_DOWN)
            ranked_prizes = [each for _row in winners]
            ranked_pool_unallocated = config["ranked_pool"] - each * len(winners)
    elif config["distribution"] == "fixed_by_rank":
        ranked_prizes = config["fixed_prizes"][:len(winners)]
        with localcontext() as ctx:
            ctx.prec = 60
            ranked_pool_unallocated = sum(config["fixed_prizes"][len(winners):], Decimal("0"))
    else:
        ranked_prizes = []
        ranked_pool_unallocated = config["ranked_pool"]

    ranked_recipients = []
    for row, prize in zip(winners, ranked_prizes):
        ranked_recipients.append({
            **row,
            "payout_gd": _format_gd(prize),
            "tie_key": [row["total_exp"], row["substantive_days"], row["daily_task_days"]],
        })
    winner_wallets = {row["wallet_address"] for row in winners}
    consolation_users = []
    for row in scores:
        if row["wallet_address"] in winner_wallets:
            continue
        if (row["total_exp"] >= config["minimum_exp"]
                and row["substantive_days"] >= config["minimum_substantive_days"]):
            consolation_users.append({
                "wallet_address": row["wallet_address"],
                "rank": row["rank"],
                "total_exp": row["total_exp"],
                "substantive_days": row["substantive_days"],
                "payout_gd": _format_gd(config["consolation"]),
            })

    with localcontext() as ctx:
        ctx.prec = 60
        ranked_liability = sum(ranked_prizes, Decimal("0"))
        consolation_liability = config["consolation"] * len(consolation_users)
        total_liability = ranked_liability + consolation_liability

    return {
        "config": config,
        "scores": scores,
        "ranked_recipients": ranked_recipients,
        "consolation_users": consolation_users,
        "ranked_liability": ranked_liability,
        "consolation_liability": consolation_liability,
        "total_liability": total_liability,
        "ranked_pool_unallocated": ranked_pool_unallocated,
    }


def build_admin_preview(snapshot: dict, score_rows: list[dict], settings: dict,
                        max_budget_raw: str | None, funding: dict,
                        now: datetime | None = None,
                        max_snapshot_age_hours: int = _DEFAULT_MAX_SNAPSHOT_AGE_HOURS) -> dict:
    """Build a non-persistent allocation preview with fail-closed budget checks."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    start = date.fromordinal(now.date().toordinal() - now.weekday())
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    snapshot_start = str(snapshot.get("week_start") or "")
    generated_at = _parse_snapshot_time(snapshot.get("generated_at"))
    snapshot_status = str(snapshot.get("data_status") or "")
    try:
        snapshot_is_current = date.fromisoformat(snapshot_start) == start
    except ValueError:
        snapshot_is_current = False
    max_snapshot_age_hours = max(1, min(int(max_snapshot_age_hours), 168))
    snapshot_is_fresh = bool(
        generated_at and generated_at <= now + timedelta(minutes=5)
        and generated_at >= now - timedelta(hours=max_snapshot_age_hours)
    )
    source_status = snapshot.get("source_status")
    source_status = source_status if isinstance(source_status, dict) else {}
    sources_ready = (source_status.get("daily_task") == "ready"
                     and source_status.get("garden_v2") == "ready")
    config = _settings(settings)

    alloc = compute_allocation(score_rows, settings)
    ranked_recipients = alloc["ranked_recipients"]
    consolation_users = alloc["consolation_users"]
    ranked_liability = alloc["ranked_liability"]
    consolation_liability = alloc["consolation_liability"]
    total_liability = alloc["total_liability"]
    ranked_pool_unallocated = alloc["ranked_pool_unallocated"]
    try:
        hard_cap = _amount(max_budget_raw, "Configured maximum budget") if max_budget_raw is not None else None
    except PreviewInputError:
        hard_cap = None
    funding = funding if isinstance(funding, dict) else {}
    try:
        funded_balance = (_amount(funding.get("balance_gd"), "Verified funded balance")
                          if funding.get("status") == "ready" else None)
    except PreviewInputError:
        funded_balance = None
    snapshot_checks = {
        "snapshot_current": snapshot_is_current,
        "snapshot_provisional": snapshot_status == "provisional",
        "snapshot_fresh": snapshot_is_fresh,
        "sources_ready": sources_ready,
    }
    checks = [
        {"key": "snapshot_current", "passed": snapshot_is_current,
         "message": "Snapshot is for the current UTC week." if snapshot_is_current else "No current-UTC-week snapshot."},
        {"key": "snapshot_provisional", "passed": snapshot_status == "provisional",
         "message": "Snapshot is provisional and editable." if snapshot_status == "provisional" else "Snapshot is not in provisional status."},
        {"key": "snapshot_fresh", "passed": snapshot_is_fresh,
         "message": f"Snapshot is within {max_snapshot_age_hours} hours." if snapshot_is_fresh else "Snapshot is missing, future-dated, or too old."},
        {"key": "sources_ready", "passed": sources_ready,
         "message": "Daily Task and Garden sources report ready." if sources_ready else "One or more evidence sources are not marked ready."},
        {"key": "budget_cap_configured", "passed": hard_cap is not None,
         "message": "Server maximum budget is configured." if hard_cap is not None else "WEEKLY_EXP_MAX_BUDGET_GD is missing or invalid."},
        {"key": "within_budget_cap", "passed": hard_cap is not None and total_liability <= hard_cap,
         "message": "Total liability is within the configured maximum." if hard_cap is not None and total_liability <= hard_cap else "Total liability exceeds or cannot be compared with the configured maximum."},
        {"key": "funding_verified", "passed": funded_balance is not None,
         "message": "Celo G$ treasury balance was verified on-chain." if funded_balance is not None else "Celo G$ treasury balance is unavailable or unconfigured."},
        {"key": "within_funded_balance", "passed": funded_balance is not None and total_liability <= funded_balance,
         "message": "Total liability is within the verified treasury balance." if funded_balance is not None and total_liability <= funded_balance else "Total liability exceeds or cannot be compared with the verified treasury balance."},
    ]
    budget_check_keys = {"budget_cap_configured", "within_budget_cap",
                         "funding_verified", "within_funded_balance"}
    budget_passed = all(check["passed"] for check in checks
                        if check["key"] in budget_check_keys)
    ready = all(check["passed"] for check in checks)
    if hard_cap is not None:
        with localcontext() as ctx:
            ctx.prec = 60
            budget_headroom = hard_cap - total_liability
    else:
        budget_headroom = None

    tie_groups_map: dict[tuple[int, int, int], list[dict]] = {}
    for row in alloc["scores"]:
        key = (row["total_exp"], row["substantive_days"], row["daily_task_days"])
        tie_groups_map.setdefault(key, []).append(row)
    tie_groups = []
    for key, group in tie_groups_map.items():
        if len(group) > 1 and any(row["rank"] <= config["top_n"] for row in group):
            tie_groups.append({
                "ranks": [row["rank"] for row in group],
                "total_exp": key[0],
                "substantive_days": key[1],
                "daily_task_days": key[2],
                "wallets": [row["wallet_address"] for row in group],
                "provisional_order": "lowercase wallet address ascending",
            })

    return {
        "preview_status": "ready_for_admin_review" if ready else "blocked",
        "can_finalize": False,
        "finalization_available": False,
        "snapshot": {
            "week_start": snapshot_start,
            "generated_at": snapshot.get("generated_at"),
            "data_status": snapshot_status or "missing",
            "source_status": source_status,
            "participant_count": len(alloc["scores"]),
            "freshness_hours_limit": max_snapshot_age_hours,
            **snapshot_checks,
        },
        "allocation": {
            "top_n": config["top_n"],
            "distribution": config["distribution"],
            "ranked_pool_gd": _format_gd(config["ranked_pool"]),
            "consolation_gd": _format_gd(config["consolation"]),
            "minimum_exp": config["minimum_exp"],
            "minimum_substantive_days": config["minimum_substantive_days"],
            "ranked_recipient_count": len(ranked_recipients),
            "consolation_recipient_count": len(consolation_users),
            "ranked_liability_gd": _format_gd(ranked_liability),
            "ranked_pool_unallocated_gd": _format_gd(ranked_pool_unallocated),
            "unassigned_fixed_prizes_gd": (_format_gd(ranked_pool_unallocated)
                                            if config["distribution"] == "fixed_by_rank" else "0"),
            "consolation_liability_gd": _format_gd(consolation_liability),
            "total_liability_gd": _format_gd(total_liability),
            "maximum_budget_gd": _format_gd(hard_cap) if hard_cap is not None else None,
            "budget_headroom_gd": _format_gd(budget_headroom) if budget_headroom is not None else None,
            "funded_balance_gd": _format_gd(funded_balance) if funded_balance is not None else None,
            "treasury_address": funding.get("treasury_address") if funded_balance is not None else None,
            "chain_id": funding.get("chain_id") if funded_balance is not None else None,
            "treasury_block": funding.get("block_number") if funded_balance is not None else None,
            "budget_passed": budget_passed,
        },
        "checks": checks,
        "tie_resolution": {
            "policy": "Preview only: total EXP, substantive days, Daily Task days, then lowercase wallet address ascending.",
            "final_policy_approved": False,
            "groups": tie_groups,
        },
        "ranked_recipients": ranked_recipients,
        "consolation_users": consolation_users,
        "notice": "Preview only. No allocation is saved or finalized and no G$ is transferred. Provisional tie ordering is not an approved winner policy.",
    }


def read_treasury_balance(env=None) -> dict:
    """Read the configured Celo G$ treasury via eth_call; fail closed on any issue."""
    env = env or os.environ
    treasury = str(env.get("WEEKLY_EXP_PRIZE_TREASURY_ADDRESS", "")).strip()
    if not _ADDRESS_RE.fullmatch(treasury):
        return {"status": "unconfigured"}
    rpc_url = str(env.get("CELO_RPC_URL") or "https://forno.celo.org").strip()
    parsed_rpc = urlsplit(rpc_url)
    loopback = (parsed_rpc.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed_rpc.scheme != "https" and not (parsed_rpc.scheme == "http" and loopback):
        return {"status": "unconfigured"}
    try:
        from config import GOODDOLLAR_CONTRACT_ADDRESS
        from web3 import Web3
        token = str(GOODDOLLAR_CONTRACT_ADDRESS or "").strip()
        if not Web3.is_address(token):
            return {"status": "unconfigured"}
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 12}))
        if not w3.is_connected() or int(w3.eth.chain_id) != 42220:
            return {"status": "rpc_unavailable"}
        block_number = int(w3.eth.block_number)
        checksum_token = Web3.to_checksum_address(token)
        checksum_treasury = Web3.to_checksum_address(treasury)
        if not w3.eth.get_code(checksum_token):
            return {"status": "token_unavailable"}
        contract = w3.eth.contract(address=checksum_token, abi=[
            {"name": "balanceOf", "type": "function", "stateMutability": "view",
             "inputs": [{"name": "account", "type": "address"}],
             "outputs": [{"name": "", "type": "uint256"}]},
            {"name": "decimals", "type": "function", "stateMutability": "view",
             "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
        ])
        decimals = int(contract.functions.decimals().call(block_identifier=block_number))
        if decimals != _TOKEN_DECIMALS:
            return {"status": "token_decimals_mismatch"}
        raw_balance = int(contract.functions.balanceOf(checksum_treasury)
                          .call(block_identifier=block_number))
        if raw_balance < 0:
            return {"status": "invalid_balance"}
        with localcontext() as ctx:
            ctx.prec = 100
            balance = Decimal(raw_balance) / (Decimal(10) ** decimals)
        return {"status": "ready", "balance_gd": _format_gd(balance),
                "treasury_address": checksum_treasury, "chain_id": 42220,
                "block_number": block_number}
    except Exception as exc:  # noqa: BLE001 - never treat an RPC error as funds
        logger.warning("weekly EXP treasury balance check unavailable: %s", exc)
        return {"status": "rpc_unavailable"}
