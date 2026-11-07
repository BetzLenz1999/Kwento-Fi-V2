"""Publish a finalized week's claim period once it is funded and rooted.

Flow: read the immutable final ledger, rebuild the deterministic Merkle bundle,
refuse to proceed unless the on-chain treasury balance covers the full
allocation and the registered root matches the rebuilt bundle, then store the
private proof bundle and flip the claim period to ``published``.

Root registration and the funding transfer are performed by the separately
reviewed on-chain tooling; this module never handles a private key. It only
verifies the on-chain result before exposing claims.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

MAX_RECIPIENTS = 10_000


def _admin_client():
    from . import weekly_exp
    return weekly_exp._admin_db()


def _rows(result) -> list[dict]:
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    return data if isinstance(data, list) else []


def _payload(result) -> dict | None:
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def _deadline_for(config: dict, now: datetime) -> str:
    """Claim deadline from the locked config, defaulting to 60 days after close."""
    raw = config.get("claim_deadline")
    if raw:
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            if value > now:
                return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            pass
    return (now + timedelta(days=60)).isoformat().replace("+00:00", "Z")


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def publish_period(week_start: date | str, actor: str, *, db=None,
                   root_tx_hash: str | None = None,
                   root_block_number: int | None = None,
                   funding: dict | None = None,
                   now: datetime | None = None) -> dict:
    """Publish the funded, rooted claim period for a finalized week."""
    from . import leaderboard_claims, leaderboard_config
    from .leaderboard_preview import read_treasury_balance

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

    contract = str(os.getenv("WEEKLY_EXP_CLAIM_CONTRACT_ADDRESS", "")).strip()
    token = str(os.getenv("GOODDOLLAR_CONTRACT_ADDRESS", "")).strip()
    if not contract or not token:
        return {"success": False, "error": "claim contract/token configuration is missing",
                "code": "not_configured"}

    db = db if db is not None else _admin_client()
    if db is None:
        return {"success": False, "error": "Reward database is unavailable",
                "code": "configuration_unavailable"}

    try:
        ledger = _rows(db.table("leaderboard_final_allocations")
                       .select("wallet_address,rank,category,amount_gd")
                       .eq("week_start", week).order("rank").execute())
    except Exception as exc:  # noqa: BLE001
        logger.error("publish ledger read failed: %s", exc)
        return {"success": False, "error": "Unable to load the final ledger",
                "code": "ledger_unavailable"}
    if not ledger:
        return {"success": False,
                "error": "the week is not finalized; no ledger exists to publish",
                "code": "not_finalized"}

    config = leaderboard_config.get_config(week, supabase=db)
    if not config.get("success") or config.get("status") != "locked":
        return {"success": False, "error": "the reward configuration must be locked",
                "code": "not_locked"}

    try:
        allocations = [{"wallet": row["wallet_address"],
                        "amount_units": leaderboard_claims.amount_to_units(row["amount_gd"])}
                       for row in ledger]
    except (KeyError, TypeError, ValueError) as exc:
        return {"success": False, "error": f"invalid ledger amount: {exc}",
                "code": "invalid_ledger"}

    try:
        period_id = leaderboard_claims.period_id_for_week(day)
        bundle = leaderboard_claims.build_claim_bundle(
            period_id=period_id, contract_address=contract, allocations=allocations)
    except ValueError as exc:
        return {"success": False, "error": str(exc), "code": "invalid_bundle"}

    total_units = int(bundle["total_allocation_units"])

    funding = funding if isinstance(funding, dict) else read_treasury_balance()
    funded_gd = _decimal(funding.get("balance_gd")) if funding.get("status") == "ready" else None
    funded_units = (int(leaderboard_claims.amount_to_units(funded_gd))
                    if funded_gd is not None else None)
    if funded_units is None or funded_units < total_units:
        return {"success": False,
                "error": "verified treasury funding does not cover the full allocation",
                "code": "funding_insufficient",
                "required_units": str(total_units),
                "funded_units": str(funded_units) if funded_units is not None else None}

    if not root_tx_hash or root_block_number is None:
        return {"success": False,
                "error": "the period root must be registered on-chain before publishing",
                "code": "root_unregistered"}

    deadline = _deadline_for(config, now)
    probe_wallet = str(funding.get("treasury_address") or "").strip() or contract
    try:
        state = leaderboard_claims.read_onchain_claim_state(
            rpc_url=str(os.getenv("CELO_RPC_URL", "https://forno.celo.org")),
            contract_address=contract, period_id=period_id, wallet=probe_wallet)
    except Exception as exc:  # noqa: BLE001 - publishing must fail closed
        logger.warning("publish on-chain verification failed: %s", type(exc).__name__)
        return {"success": False, "error": "the registered root could not be verified on-chain",
                "code": "root_unverified"}
    deadline_ts = int(datetime.fromisoformat(deadline.replace("Z", "+00:00")).timestamp())
    if (not state.get("registered") or state.get("recovered")
            or str(state.get("period_root", "")).lower() != bundle["merkle_root"].lower()
            or int(state.get("total_allocation_units", 0)) != total_units
            or int(state.get("claim_deadline", 0)) != deadline_ts):
        return {"success": False,
                "error": "on-chain period does not match the finalized ledger",
                "code": "root_mismatch"}

    try:
        stored = leaderboard_claims.store_draft_bundle(
            db, bundle=bundle, week_start=day, token_address=token, claim_deadline=deadline)
    except Exception as exc:  # noqa: BLE001
        if "duplicate" not in str(exc).lower() and "already exists" not in str(exc).lower():
            logger.error("publish bundle store failed: %s", exc)
            return {"success": False, "error": "Unable to store the private claim bundle",
                    "code": "store_failed"}
        stored = {"period_id": period_id}

    try:
        db.rpc("weekly_exp_finalize_claim_period", {
            "p_period_id": period_id, "p_actor": str(actor).strip()}).execute()
        publish_result = db.rpc("weekly_exp_publish_claim_period", {
            "p_period_id": period_id,
            "p_actor": str(actor).strip(),
            "p_root_tx_hash": str(root_tx_hash).lower(),
            "p_root_block_number": int(root_block_number),
            "p_funded_balance_units": str(funded_units),
            "p_funding_tx_hash": funding.get("funding_tx_hash"),
            "p_verified_block": funding.get("block_number"),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("publish RPC failed: %s", exc)
        return {"success": False, "error": "Publication service is temporarily unavailable",
                "code": "publish_unavailable"}
    payload = _payload(publish_result)
    if not isinstance(payload, dict) or not payload.get("success"):
        return payload if isinstance(payload, dict) else {
            "success": False, "error": "Publication returned an empty response",
            "code": "publish_unavailable"}
    payload.setdefault("period_id", stored.get("period_id", period_id))
    payload["recipient_count"] = bundle["recipient_count"]
    payload["merkle_root"] = bundle["merkle_root"]
    payload["total_allocation_units"] = str(total_units)
    return payload
