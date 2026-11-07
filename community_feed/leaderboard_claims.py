"""Deterministic, versioned Merkle proof construction for finalized leaderboard claims.

This module only builds a private proof bundle from an already-reviewed allocation.
It does not calculate rankings, decide prizes, register roots, or mark claims paid.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Any

CELO_CHAIN_ID = 42220
LEAF_VERSION = 1
TOKEN_DECIMALS = 18
MAX_RECIPIENTS = 10_000
_UINT256_MAX = (1 << 256) - 1
_BYTES32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _crypto():
    try:
        from eth_abi import encode
        from web3 import Web3
    except ImportError as exc:  # pragma: no cover - deployment environment dependency
        raise RuntimeError("web3 and eth_abi are required to build claim proofs") from exc
    return encode, Web3


def _bytes32(value: Any, label: str) -> bytes:
    text = str(value or "")
    if not _BYTES32_RE.fullmatch(text):
        raise ValueError(f"{label} must be a 32-byte 0x-prefixed hex value")
    return bytes.fromhex(text[2:])


def _checksum_address(value: Any, label: str) -> str:
    _encode, Web3 = _crypto()
    if not isinstance(value, str) or not Web3.is_address(value):
        raise ValueError(f"{label} must be a valid EVM address")
    address = Web3.to_checksum_address(value)
    if address == "0x0000000000000000000000000000000000000000":
        raise ValueError(f"{label} cannot be the zero address")
    return address


def amount_to_units(value: Any, decimals: int = TOKEN_DECIMALS) -> int:
    """Convert an exact decimal token amount to base units without float rounding."""
    if isinstance(value, float):
        raise ValueError("float token amounts are not supported; pass an exact decimal string")
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("token amount must be an exact decimal string, integer, or Decimal")
    if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 36:
        raise ValueError("invalid token decimals")
    try:
        decimal_amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid token amount") from exc
    if not decimal_amount.is_finite() or decimal_amount <= 0:
        raise ValueError("token amount must be finite and greater than zero")
    with localcontext() as context:
        context.prec = 100
        scaled = decimal_amount * (Decimal(10) ** decimals)
        integral = scaled.to_integral_value()
        if scaled != integral:
            raise ValueError(f"token amount exceeds {decimals} decimal places")
        units = int(integral)
    if units <= 0 or units > _UINT256_MAX:
        raise ValueError("token amount is outside the uint256 range")
    return units


def _leaf(chain_id: int, contract_address: str, period_id: bytes, wallet: str, amount_units: int) -> bytes:
    encode, Web3 = _crypto()
    encoded_claim = encode(
        ["uint256", "address", "bytes32", "address", "uint256"],
        [chain_id, contract_address, period_id, wallet, amount_units],
    )
    # Match Solidity: keccak256(bytes.concat(keccak256(abi.encode(...)))).
    return bytes(Web3.keccak(Web3.keccak(encoded_claim)))


def _parent(left: bytes, right: bytes) -> bytes:
    _encode, Web3 = _crypto()
    first, second = (left, right) if left <= right else (right, left)
    return bytes(Web3.keccak(first + second))


def _hex(value: bytes) -> str:
    return "0x" + value.hex()


def build_claim_bundle(
    *,
    period_id: str,
    contract_address: str,
    allocations: list[dict[str, Any]],
    chain_id: int = CELO_CHAIN_ID,
) -> dict[str, Any]:
    """Return a reproducible sorted-pair root plus wallet/amount/proof entries.

    Each input row must contain exactly a wallet and integer `amount_units`; the
    builder intentionally does not infer a reward from EXP or accept client proofs.
    """
    if chain_id != CELO_CHAIN_ID:
        raise ValueError("leaderboard claims are restricted to Celo chain 42220")
    period = _bytes32(period_id, "period_id")
    contract = _checksum_address(contract_address, "contract_address")
    if not isinstance(allocations, list) or not 1 <= len(allocations) <= MAX_RECIPIENTS:
        raise ValueError(f"allocations must contain 1 to {MAX_RECIPIENTS} recipients")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for row in allocations:
        if not isinstance(row, dict) or set(row) != {"wallet", "amount_units"}:
            raise ValueError("each allocation must contain only wallet and amount_units")
        wallet = _checksum_address(row.get("wallet"), "wallet")
        wallet_key = wallet.lower()
        if wallet_key in seen:
            raise ValueError("duplicate wallet allocation")
        seen.add(wallet_key)
        amount = row.get("amount_units")
        if isinstance(amount, bool) or not isinstance(amount, (int, str)):
            raise ValueError("amount_units must be a positive integer or digit string")
        if isinstance(amount, str) and not amount.isascii():
            raise ValueError("amount_units must contain ASCII digits only")
        if isinstance(amount, str) and not amount.isdigit():
            raise ValueError("amount_units must contain digits only")
        amount = int(amount)
        if amount <= 0 or amount > _UINT256_MAX:
            raise ValueError("amount_units is outside the positive uint256 range")
        total += amount
        if total > _UINT256_MAX:
            raise ValueError("total allocation exceeds uint256")
        normalized.append({"wallet": wallet, "amount_units": amount})

    normalized.sort(key=lambda row: row["wallet"].lower())
    leaves = [
        _leaf(chain_id, contract, period, row["wallet"], row["amount_units"])
        for row in normalized
    ]
    layers = [leaves]
    while len(layers[-1]) > 1:
        current = layers[-1]
        next_level = []
        for index in range(0, len(current), 2):
            if index + 1 < len(current):
                next_level.append(_parent(current[index], current[index + 1]))
            else:
                # Promote an odd final node unchanged; do not duplicate it.
                next_level.append(current[index])
        layers.append(next_level)

    claims = []
    for claim_index, row in enumerate(normalized):
        proof: list[str] = []
        cursor = claim_index
        for level in layers[:-1]:
            sibling = cursor ^ 1
            if sibling < len(level):
                proof.append(_hex(level[sibling]))
            cursor //= 2
        claims.append({
            "wallet": row["wallet"],
            "amount_units": str(row["amount_units"]),
            "leaf": _hex(leaves[claim_index]),
            "proof": proof,
        })

    return {
        "leaf_version": LEAF_VERSION,
        "chain_id": CELO_CHAIN_ID,
        "contract_address": contract,
        "period_id": "0x" + period.hex(),
        "merkle_root": _hex(layers[-1][0]),
        "recipient_count": len(claims),
        "total_allocation_units": str(total),
        "claims": claims,
    }


def verify_claim_proof(bundle: dict[str, Any], claim: dict[str, Any]) -> bool:
    """Verify one generated proof using the same sorted-pair rules as OZ MerkleProof."""
    _encode, Web3 = _crypto()
    try:
        period = _bytes32(bundle["period_id"], "period_id")
        contract = _checksum_address(bundle["contract_address"], "contract_address")
        wallet = _checksum_address(claim["wallet"], "wallet")
        amount = int(claim["amount_units"])
        if bundle.get("leaf_version") != LEAF_VERSION or bundle.get("chain_id") != CELO_CHAIN_ID:
            return False
        node = _leaf(CELO_CHAIN_ID, contract, period, wallet, amount)
        for sibling_hex in claim["proof"]:
            node = _parent(node, _bytes32(sibling_hex, "proof sibling"))
        return _hex(node).lower() == str(bundle["merkle_root"]).lower()
    except (KeyError, TypeError, ValueError, RuntimeError):
        return False


def period_id_for_week(week_start: Any) -> str:
    """Derive a stable bytes32 period ID from a Monday UTC date."""
    from datetime import date, datetime

    if isinstance(week_start, datetime):
        raise ValueError("week_start must be a date, not a datetime")
    if isinstance(week_start, str):
        try:
            week_start = date.fromisoformat(week_start)
        except ValueError as exc:
            raise ValueError("week_start must be an ISO date") from exc
    if not isinstance(week_start, date) or week_start.isoweekday() != 1:
        raise ValueError("week_start must be the Monday UTC date")
    _encode, Web3 = _crypto()
    value = Web3.keccak(text=f"kwentofi.weekly-exp.period.v1|{week_start.isoformat()}")
    return _hex(bytes(value))


def store_draft_bundle(
    db,
    *,
    bundle: dict[str, Any],
    week_start: Any,
    token_address: str,
    claim_deadline: str,
) -> dict[str, Any]:
    """Atomically persist a verified private draft bundle through its service RPC.

    The SQL procedure leaves the period in `draft`; it cannot publish a root or
    finalize prize policy. This helper is intentionally server-side only.
    """
    from datetime import date, datetime, timedelta, timezone

    if not isinstance(bundle, dict) or bundle.get("leaf_version") != LEAF_VERSION:
        raise ValueError("unsupported claim leaf version")
    if bundle.get("chain_id") != CELO_CHAIN_ID:
        raise ValueError("claim bundle must target Celo chain 42220")
    if isinstance(week_start, datetime):
        raise ValueError("week_start must be a date, not a datetime")
    if isinstance(week_start, str):
        try:
            week_start = date.fromisoformat(week_start)
        except ValueError as exc:
            raise ValueError("week_start must be an ISO date") from exc
    if not isinstance(week_start, date) or week_start.isoweekday() != 1:
        raise ValueError("week_start must be the Monday UTC date")

    period = _bytes32(bundle.get("period_id"), "period_id")
    expected_period = period_id_for_week(week_start)
    if "0x" + period.hex() != expected_period.lower():
        raise ValueError("period ID does not match the selected UTC week")
    contract = _checksum_address(bundle.get("contract_address"), "contract_address")
    token = _checksum_address(token_address, "token_address")
    if str(bundle.get("contract_address", "")).lower() != contract.lower():
        raise ValueError("invalid contract address")

    claims = bundle.get("claims")
    if not isinstance(claims, list) or not 1 <= len(claims) <= MAX_RECIPIENTS:
        raise ValueError("claim bundle recipient count is invalid")
    if bundle.get("recipient_count") != len(claims):
        raise ValueError("claim bundle recipient count mismatch")
    if not _BYTES32_RE.fullmatch(str(bundle.get("merkle_root") or "")):
        raise ValueError("claim bundle root is invalid")

    amount_total = 0
    entitlements = []
    seen: set[str] = set()
    for row in claims:
        if not isinstance(row, dict) or not verify_claim_proof(bundle, row):
            raise ValueError("claim bundle contains an invalid proof")
        wallet = _checksum_address(row.get("wallet"), "wallet")
        if wallet.lower() in seen:
            raise ValueError("claim bundle contains a duplicate wallet")
        seen.add(wallet.lower())
        amount_text = row.get("amount_units")
        if not isinstance(amount_text, str) or not amount_text.isascii() or not amount_text.isdigit():
            raise ValueError("claim amount must be an integer-unit string")
        amount = int(amount_text)
        if amount <= 0 or amount > _UINT256_MAX:
            raise ValueError("claim amount is outside the positive uint256 range")
        amount_total += amount
        if amount_total > _UINT256_MAX:
            raise ValueError("claim total exceeds uint256")
        entitlements.append({
            "wallet": wallet.lower(),
            "amount_units": str(amount),
            "leaf": str(row.get("leaf") or "").lower(),
            "proof": [str(node).lower() for node in row.get("proof", [])],
        })

    if str(bundle.get("total_allocation_units")) != str(amount_total):
        raise ValueError("claim bundle total does not equal entitlement sum")

    try:
        deadline = datetime.fromisoformat(str(claim_deadline).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("claim_deadline must be an ISO timestamp") from exc
    if deadline.tzinfo is None:
        raise ValueError("claim_deadline must include a timezone")
    now = datetime.now(timezone.utc)
    deadline = deadline.astimezone(timezone.utc)
    if deadline <= now or deadline > now + timedelta(days=365):
        raise ValueError("claim_deadline must be within the next 365 days")

    params = {
        "p_period_id": expected_period.lower(),
        "p_week_start": week_start.isoformat(),
        "p_chain_id": CELO_CHAIN_ID,
        "p_contract_address": contract.lower(),
        "p_token_address": token.lower(),
        "p_merkle_root": str(bundle["merkle_root"]).lower(),
        "p_leaf_version": LEAF_VERSION,
        "p_total_allocation_units": str(amount_total),
        "p_claim_deadline": deadline.isoformat().replace("+00:00", "Z"),
        "p_entitlements": entitlements,
    }
    result = db.rpc("weekly_exp_store_claim_bundle", params).execute()
    data = getattr(result, "data", None)
    return {"success": True, "status": "draft", "period_id": expected_period.lower(),
            "recipient_count": len(entitlements), "total_allocation_units": str(amount_total),
            "database_result": data}


_CLAIM_READ_ABI = [
    {"inputs": [], "name": "goodDollarToken", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "paused", "outputs": [{"type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "periodId", "type": "bytes32"}, {"name": "recipient", "type": "address"}],
     "name": "claimed", "outputs": [{"type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "", "type": "bytes32"}], "name": "periods",
     "outputs": [
         {"name": "merkleRoot", "type": "bytes32"},
         {"name": "claimDeadline", "type": "uint64"},
         {"name": "registeredAt", "type": "uint64"},
         {"name": "totalAllocation", "type": "uint256"},
         {"name": "claimedAmount", "type": "uint256"},
         {"name": "claimCount", "type": "uint32"},
         {"name": "registered", "type": "bool"},
         {"name": "recovered", "type": "bool"},
     ], "stateMutability": "view", "type": "function"},
]


def read_onchain_claim_state(*, rpc_url: str, contract_address: str, period_id: str, wallet: str) -> dict[str, Any]:
    """Verify claim status and the stored period domain against a live Celo RPC."""
    try:
        from web3 import Web3
    except ImportError as exc:  # pragma: no cover - deployment environment dependency
        raise RuntimeError("web3 is required to verify on-chain claim state") from exc
    if not isinstance(rpc_url, str) or not rpc_url.startswith("https://"):
        raise ValueError("a trusted HTTPS Celo RPC URL is required")
    contract_address = _checksum_address(contract_address, "contract_address")
    wallet = _checksum_address(wallet, "wallet")
    _bytes32(period_id, "period_id")
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 6}))
    if not web3.is_connected():
        raise RuntimeError("Celo RPC is unavailable")
    if int(web3.eth.chain_id) != CELO_CHAIN_ID:
        raise RuntimeError("configured RPC is not Celo mainnet")
    if not web3.eth.get_code(contract_address):
        raise RuntimeError("configured claim contract has no code")
    contract = web3.eth.contract(address=contract_address, abi=_CLAIM_READ_ABI)
    values = contract.functions.periods(period_id).call()
    token_address = Web3.to_checksum_address(contract.functions.goodDollarToken().call())
    latest = web3.eth.get_block("latest")
    return {
        "chain_id": CELO_CHAIN_ID,
        "block_number": int(latest["number"]),
        "block_timestamp": int(latest["timestamp"]),
        "token_address": token_address,
        "contract_address": contract_address,
        "paused": bool(contract.functions.paused().call()),
        "claimed": bool(contract.functions.claimed(period_id, wallet).call()),
        "period_root": Web3.to_hex(values[0]).lower(),
        "claim_deadline": int(values[1]),
        "total_allocation_units": str(values[3]),
        "registered": bool(values[6]),
        "recovered": bool(values[7]),
    }


def wallet_claims(
    wallet: str,
    *,
    db=None,
    now: Any = None,
    state_reader=None,
) -> dict[str, Any]:
    """Return proofs only for the supplied authenticated wallet and active published periods."""
    import os
    from datetime import datetime, timezone

    if (os.getenv("WEEKLY_EXP_CLAIMS_ENABLED", "0") or "0").strip().lower() not in {
        "1", "true", "yes", "on", "enabled"
    }:
        return {"success": True, "enabled": False, "claims": []}
    wallet = _checksum_address(wallet, "wallet")
    configured_contract = os.getenv("WEEKLY_EXP_CLAIM_CONTRACT_ADDRESS", "")
    configured_token = os.getenv("GOODDOLLAR_CONTRACT_ADDRESS", "")
    rpc_url = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
    if not configured_contract or not configured_token:
        raise RuntimeError("claim contract/token configuration is missing")
    configured_contract = _checksum_address(configured_contract, "configured contract")
    configured_token = _checksum_address(configured_token, "configured token")
    if state_reader is None:
        state_reader = read_onchain_claim_state
    if db is None:
        from . import weekly_exp
        db = weekly_exp._admin_db()

    result = (db.table("leaderboard_claim_periods")
              .select("period_id,week_start,chain_id,contract_address,token_address,merkle_root,leaf_version,status,total_allocation_units,claim_deadline")
              .in_("status", ["published", "paused"])
              .order("week_start", desc=True).limit(20).execute())
    period_rows = getattr(result, "data", None) or []
    if len(period_rows) > 20:
        raise RuntimeError("claim period query exceeded its bound")

    claims = []
    for period in period_rows:
        if not isinstance(period, dict):
            continue
        if int(period.get("chain_id") or 0) != CELO_CHAIN_ID:
            continue
        try:
            period_contract = _checksum_address(period.get("contract_address"), "period contract")
            period_token = _checksum_address(period.get("token_address"), "period token")
        except ValueError:
            continue
        if period_contract.lower() != configured_contract.lower() or period_token.lower() != configured_token.lower():
            continue
        entitlement_result = (db.table("leaderboard_claim_entitlements")
                              .select("period_id,wallet_address,amount_units,leaf_hash,proof,claim_status,claim_tx_hash,claimed_at")
                              .eq("period_id", period.get("period_id"))
                              .eq("wallet_address", wallet.lower()).limit(1).execute())
        rows = getattr(entitlement_result, "data", None) or []
        if not rows:
            continue
        row = rows[0]
        try:
            state = state_reader(rpc_url=rpc_url, contract_address=period_contract,
                                 period_id=period["period_id"], wallet=wallet)
            amount = int(row.get("amount_units"))
            period_deadline = datetime.fromisoformat(str(period["claim_deadline"]).replace("Z", "+00:00"))
            if period_deadline.tzinfo is None:
                raise ValueError("stored claim deadline is missing timezone")
            proof = row.get("proof")
            if isinstance(proof, str):
                import json
                proof = json.loads(proof)
            claim = {"wallet": wallet, "amount_units": str(amount), "proof": proof}
            proof_bundle = {
                "leaf_version": int(period.get("leaf_version") or 0),
                "chain_id": int(period.get("chain_id") or 0),
                "contract_address": period_contract,
                "period_id": period["period_id"],
                "merkle_root": str(period.get("merkle_root") or "").lower(),
            }
            if state.get("chain_id") != CELO_CHAIN_ID or state.get("contract_address", "").lower() != period_contract.lower():
                raise ValueError("on-chain claim domain mismatch")
            if state.get("token_address", "").lower() != period_token.lower():
                raise ValueError("on-chain token mismatch")
            if (not state.get("registered") or state.get("recovered")
                    or state.get("period_root", "").lower() != proof_bundle["merkle_root"]
                    or int(state.get("claim_deadline", 0)) != int(period_deadline.timestamp())
                    or int(state.get("total_allocation_units", 0)) != int(period.get("total_allocation_units") or 0)):
                raise ValueError("stored claim period does not match the published contract state")
            if not isinstance(proof, list) or any(not _BYTES32_RE.fullmatch(str(node)) for node in proof):
                raise ValueError("stored claim proof is malformed")
            expected_leaf = _hex(_leaf(
                CELO_CHAIN_ID, period_contract, _bytes32(period["period_id"], "period_id"), wallet, amount
            )).lower()
            if str(row.get("leaf_hash") or "").lower() != expected_leaf:
                raise ValueError("stored claim leaf does not match the entitlement")
            claimable_proof = verify_claim_proof(proof_bundle, claim)
            if not claimable_proof:
                raise ValueError("stored claim proof does not match its root")
        except Exception:  # noqa: BLE001 - fail closed, never return an unverified proof
            claims.append({"period_id": period.get("period_id"), "status": "unavailable",
                           "message": "Claim details could not be verified."})
            continue

        if state["claimed"]:
            status = "claimed" if row.get("claim_status") == "claimed" else "pending_confirmation"
        elif row.get("claim_status") != "unclaimed":
            status = "reconciliation_required"
        elif period.get("status") == "paused" or state.get("paused"):
            status = "paused"
        else:
            chain_now = int(state.get("block_timestamp") or 0)
            if now is None:
                current = datetime.now(timezone.utc)
            elif isinstance(now, datetime) and now.tzinfo is not None:
                current = now.astimezone(timezone.utc)
            else:
                raise ValueError("now must be a timezone-aware datetime")
            current_time = int(current.timestamp())
            if chain_now > int(state["claim_deadline"]) or current_time > int(state["claim_deadline"]):
                status = "expired"
            else:
                status = "claimable"

        response_row = {
            "period_id": period["period_id"],
            "week_start": str(period.get("week_start") or ""),
            "wallet": wallet,
            "amount_units": str(amount),
            "chain_id": CELO_CHAIN_ID,
            "contract_address": period_contract,
            "claim_deadline": period_deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": status,
            "claimable": status == "claimable",
        }
        if status == "claimable":
            response_row["proof"] = proof
        if status in {"claimed", "pending_confirmation"}:
            response_row["transaction_hash"] = row.get("claim_tx_hash")
        claims.append(response_row)

    claims.extend(_pending_period_claims(db, wallet))
    return {"success": True, "enabled": True, "claims": claims}


def _pending_period_claims(db, wallet: str) -> list[dict[str, Any]]:
    """Rows for periods that are finalized but not yet funded/published.

    A draft period means the Merkle bundle is stored but the contract root is not
    registered yet (awaiting funding). A finalized period has an approved ledger
    and root but is still unpublished. Neither exposes a proof or a Claim button;
    they only let the member's post panel explain why a reward is not claimable.
    """
    try:
        result = (db.table("leaderboard_claim_periods")
                  .select("period_id,week_start,status")
                  .in_("status", ["draft", "finalized"])
                  .order("week_start", desc=True).limit(20).execute())
        periods = getattr(result, "data", None) or []
    except Exception:  # noqa: BLE001 - extra states are advisory only
        return []
    rows: list[dict[str, Any]] = []
    for period in periods:
        if not isinstance(period, dict):
            continue
        try:
            entitlement_result = (db.table("leaderboard_claim_entitlements")
                                  .select("period_id,wallet_address,amount_units")
                                  .eq("period_id", period.get("period_id"))
                                  .eq("wallet_address", wallet.lower()).limit(1).execute())
            entitlements = getattr(entitlement_result, "data", None) or []
            if not entitlements:
                continue
            amount = int(entitlements[0].get("amount_units"))
        except Exception:  # noqa: BLE001
            continue
        rows.append({
            "period_id": period.get("period_id"),
            "week_start": str(period.get("week_start") or ""),
            "wallet": wallet,
            "amount_units": str(amount),
            "chain_id": CELO_CHAIN_ID,
            "status": "awaiting_funding" if period.get("status") == "draft" else "reward_finalized",
            "claimable": False,
        })
    return rows


def claims_enabled() -> bool:
    import os
    return (os.getenv("WEEKLY_EXP_CLAIMS_ENABLED", "0") or "0").strip().lower() in {
        "1", "true", "yes", "on", "enabled"
    }
