"""Bounded app-funded Celo gas top-ups for Community Garden purchases.

The sponsor only sends native CELO to the verified signed-in wallet. The user
still signs the exact G$ approval and Garden purchase transactions themselves.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import os

from web3 import Web3

from chain_rpc import make_provider

WEI_PER_CELO = 10**18
CHAIN_ID = 42220
GAS_PRICE_BUFFER_BPS = 13_000
SPONSOR_TX_GAS = 21_000
APPROVAL_GAS_FALLBACK = 100_000
PURCHASE_GAS_FALLBACK = 300_000
POST_TOPUP_WAIT_SECONDS = 30
TOKEN_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
]
GARDEN_V2_ABI = [
    {"inputs": [], "name": "goodDollarToken", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "SEED_PRICE", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "seedPrice", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "MAX_SEEDS", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "maxSeeds", "outputs": [{"type": "uint16"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "seedCount", "type": "uint256"}], "name": "buyAndPlant", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "gardener", "type": "address"}], "name": "gardens", "outputs": [{"type": "tuple", "components": [
        {"name": "principal", "type": "uint256"}, {"name": "bonusBpsAtPlant", "type": "uint16"},
        {"name": "plantedAt", "type": "uint64"},
        {"name": "seedCount", "type": "uint16"}, {"name": "active", "type": "bool"}
    ]}], "stateMutability": "view", "type": "function"},
]
GARDEN_V1_ABI = [
    {"inputs": [], "name": "goodDollarToken", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "gardener", "type": "address"}], "name": "gardens", "outputs": [{"type": "tuple", "components": [
        {"name": "principal", "type": "uint256"}, {"name": "plantedAt", "type": "uint64"},
        {"name": "seedCount", "type": "uint16"}, {"name": "active", "type": "bool"}
    ]}], "stateMutability": "view", "type": "function"},
]


class GardenGasSponsorError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400, detail: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or {}


def _amount_wei(name: str, default: str) -> int:
    try:
        value = Decimal(os.getenv(name, default))
        if not value.is_finite() or value < 0:
            raise InvalidOperation
        return int((value * WEI_PER_CELO).to_integral_value(rounding=ROUND_CEILING))
    except Exception:
        return int(Decimal(default) * WEI_PER_CELO)


def _supabase():
    from supabase_client import get_supabase_admin_client, get_supabase_client
    return get_supabase_admin_client() or get_supabase_client()


def _garden_context(wallet: str):
    from config import GOODDOLLAR_CONTRACT_ADDRESS
    from daily_task_contract_service import get_contract_context, DailyTaskContractError

    if (os.getenv("COMMUNITY_GARDEN_ENABLED", "NO") or "NO").strip().lower() not in {"1", "true", "yes", "on", "enabled"}:
        raise GardenGasSponsorError("Garden purchases are currently unavailable.", status_code=503)
    garden_address_raw = (os.getenv("COMMUNITY_GARDEN_CONTRACT_ADDRESS") or "").strip()
    garden_token_raw = (os.getenv("CELO_GD_TOKEN_CONTRACT") or GOODDOLLAR_CONTRACT_ADDRESS or "").strip()
    try:
        garden_address = Web3.to_checksum_address(garden_address_raw)
        garden_token_address = Web3.to_checksum_address(garden_token_raw)
        wallet = Web3.to_checksum_address(wallet)
    except Exception as exc:
        raise GardenGasSponsorError("Garden or token address configuration is invalid.", status_code=503) from exc

    try:
        sponsor_context = get_contract_context()
    except DailyTaskContractError as exc:
        raise GardenGasSponsorError("App-funded Garden gas is temporarily unavailable.", status_code=503) from exc
    if garden_token_address.lower() != sponsor_context.token_address.lower():
        raise GardenGasSponsorError("Garden and gas-sponsor token configuration do not match.", status_code=503)
    w3 = sponsor_context.w3
    if int(w3.eth.chain_id) != CHAIN_ID or not w3.eth.get_code(garden_address):
        raise GardenGasSponsorError("The configured Garden contract could not be verified on Celo Mainnet.", status_code=503)
    garden = w3.eth.contract(address=garden_address, abi=GARDEN_V2_ABI)
    deployed_token = Web3.to_checksum_address(garden.functions.goodDollarToken().call())
    if deployed_token.lower() != garden_token_address.lower():
        raise GardenGasSponsorError("Garden token configuration does not match the deployed contract.", status_code=503)
    token = w3.eth.contract(address=garden_token_address, abi=TOKEN_ABI)
    return sponsor_context, garden, token, wallet


def _garden_is_active(garden, wallet: str) -> bool:
    record = garden.functions.gardens(wallet).call()
    if isinstance(record, (tuple, list)):
        record_values = record[0] if len(record) == 1 and isinstance(record[0], (tuple, list)) else record
        return bool(record_values[-1])
    return bool(record.get("active"))


def _legacy_garden_is_active(context, wallet: str, v2_address: str, token_address: str) -> bool:
    legacy_raw = (os.getenv(
        "COMMUNITY_GARDEN_V1_CONTRACT_ADDRESS",
        "0xcCD5F87c74d876347aAA9b85ADaF1EEFC75CE3d4",
    ) or "").strip()
    if not legacy_raw:
        raise GardenGasSponsorError("Legacy Garden configuration is invalid.", status_code=503)
    try:
        legacy_address = Web3.to_checksum_address(legacy_raw)
    except Exception as exc:
        raise GardenGasSponsorError("Legacy Garden configuration is invalid.", status_code=503) from exc
    if legacy_address.lower() == v2_address.lower():
        return False
    w3 = context.w3
    if int(w3.eth.chain_id) != CHAIN_ID or not w3.eth.get_code(legacy_address):
        raise GardenGasSponsorError("The configured legacy Garden contract could not be verified on Celo Mainnet.", status_code=503)
    legacy = w3.eth.contract(address=legacy_address, abi=GARDEN_V1_ABI)
    deployed_token = Web3.to_checksum_address(legacy.functions.goodDollarToken().call())
    if deployed_token.lower() != token_address.lower():
        raise GardenGasSponsorError("Legacy Garden token configuration does not match the deployed contract.", status_code=503)
    return _garden_is_active(legacy, wallet)


def _reserve(db, wallet: str, request_id: str, amount: int, daily_cap: int) -> bool:
    day = datetime.now(timezone.utc).date().isoformat()
    try:
        response = db.rpc("reserve_community_garden_gas_sponsorship", {
            "p_wallet": wallet.lower(), "p_day_utc": day, "p_request_id": request_id,
            "p_amount_wei": str(amount), "p_daily_cap_wei": str(daily_cap),
        }).execute()
        data = getattr(response, "data", None)
        if isinstance(data, list):
            data = data[0] if data else False
        if isinstance(data, dict):
            data = data.get("reserve_community_garden_gas_sponsorship", data.get("reserved", False))
        return data is True or str(data).lower() == "true"
    except Exception as exc:
        raise GardenGasSponsorError("Gas support could not be reserved safely. No CELO was sent.", status_code=503) from exc


def _update(db, request_id: str, values: dict) -> None:
    try:
        db.table("community_garden_gas_sponsorships").update(values).eq("request_id", request_id).execute()
    except Exception:
        # Persisted signed bytes remain the source for a safe exact-payload retry.
        pass


def sponsor_for_purchase(wallet: str, seed_count: int, request_id: str) -> dict:
    """Estimate and, only at <= threshold, send a bounded native-gas top-up."""
    if isinstance(seed_count, bool) or not isinstance(seed_count, int) or not 1 <= seed_count <= 1000:
        raise GardenGasSponsorError("Invalid seed count.")
    try:
        request_id = Web3.to_hex(Web3.to_bytes(hexstr=request_id))
        if len(Web3.to_bytes(hexstr=request_id)) != 32:
            raise ValueError("request id must be 32 bytes")
    except Exception as exc:
        raise GardenGasSponsorError("Invalid gas-support request ID.") from exc

    context, garden, token, wallet = _garden_context(wallet)
    try:
        try:
            price = int(garden.functions.seedPrice().call())
        except Exception:
            price = int(garden.functions.SEED_PRICE().call())
        try:
            max_seeds = int(garden.functions.maxSeeds().call())
        except Exception:
            max_seeds = int(garden.functions.MAX_SEEDS().call())
        if seed_count < 1 or seed_count > max_seeds:
            raise GardenGasSponsorError(f"Choose between 1 and {max_seeds} seeds.")
        amount = seed_count * price
        if amount <= 0 or int(token.functions.balanceOf(wallet).call()) < amount:
            raise GardenGasSponsorError("Your wallet needs enough G$ for the selected seeds before gas support can be provided.", status_code=402)
        if _garden_is_active(garden, wallet) or _legacy_garden_is_active(
            context, wallet, garden.address, token.address
        ):
            raise GardenGasSponsorError("You already have an active Garden.", status_code=409)
        allowance = int(token.functions.allowance(wallet, garden.address).call())
        approval_gas = 0
        if allowance < amount:
            try:
                approval_gas = int(token.functions.approve(garden.address, amount).estimate_gas({"from": wallet}))
            except Exception:
                approval_gas = APPROVAL_GAS_FALLBACK
        try:
            purchase_gas = int(garden.functions.buyAndPlant(seed_count).estimate_gas({"from": wallet}))
        except Exception:
            # A first purchase without allowance commonly cannot be estimated.
            purchase_gas = PURCHASE_GAS_FALLBACK
        total_gas = approval_gas + purchase_gas
        gas_price = int(context.w3.eth.gas_price)
        if gas_price <= 0 or total_gas <= 0:
            raise GardenGasSponsorError("Unable to estimate Garden transaction fees.", status_code=503)
        required = (total_gas * gas_price * GAS_PRICE_BUFFER_BPS + 9_999) // 10_000
        balance = int(context.w3.eth.get_balance(wallet))
        threshold = _amount_wei("COMMUNITY_GARDEN_GAS_SPONSOR_THRESHOLD_CELO", "0.3")
        estimated = {
            "estimated_gas": total_gas,
            "estimated_fee_wei": str(required),
            "estimated_fee_celo": f"{required / WEI_PER_CELO:.8f}",
            "user_balance_celo": f"{balance / WEI_PER_CELO:.8f}",
        }
        db = _supabase()
        if not db:
            raise GardenGasSponsorError("Gas support is temporarily unavailable.", status_code=503)

        prior_result = db.table("community_garden_gas_sponsorships").select(
            "request_id,wallet_address,status,amount_wei,sponsor_tx_hash,signed_tx_hex"
        ).eq("request_id", request_id).limit(1).execute()
        prior_rows = getattr(prior_result, "data", None) or []
        if prior_rows:
            prior = prior_rows[0]
            if str(prior.get("wallet_address", "")).lower() != wallet.lower():
                raise GardenGasSponsorError("Gas-support request belongs to another wallet.", status_code=403)
            tx_hash = str(prior.get("sponsor_tx_hash") or "")
            raw_hex = str(prior.get("signed_tx_hex") or "")
            amount_prior = int(prior.get("amount_wei") or 0)
            if prior.get("status") == "sent" and tx_hash:
                return {"success": True, "sponsored": True, "payer": "app", "sponsor_tx_hash": tx_hash,
                        "amount_wei": str(amount_prior), "amount_celo": f"{amount_prior / WEI_PER_CELO:.8f}",
                        "post_topup_wait_seconds": POST_TOPUP_WAIT_SECONDS, **estimated}
            if prior.get("status") == "reserved" and tx_hash and raw_hex:
                raw = Web3.to_bytes(hexstr=raw_hex)
                if Web3.to_hex(Web3.keccak(raw)).lower() != tx_hash.lower():
                    raise GardenGasSponsorError("Stored gas-support transaction failed its integrity check.", status_code=503)
                try:
                    context.w3.eth.get_transaction(tx_hash)
                except Exception:
                    context.w3.eth.send_raw_transaction(raw)
                _update(db, request_id, {"status": "sent", "updated_at": datetime.now(timezone.utc).isoformat()})
                return {"success": True, "sponsored": True, "payer": "app", "sponsor_tx_hash": tx_hash,
                        "amount_wei": str(amount_prior), "amount_celo": f"{amount_prior / WEI_PER_CELO:.8f}",
                        "post_topup_wait_seconds": POST_TOPUP_WAIT_SECONDS, **estimated}
            raise GardenGasSponsorError("A gas-support transaction is being reconciled. Retry shortly.", status_code=409)

        if balance > threshold:
            if balance < required:
                raise GardenGasSponsorError(
                    "Your wallet has more than 0.3 CELO, so it pays its own network fee; the estimated fee exceeds your balance.",
                    status_code=402, detail={**estimated, "sponsored": False, "payer": "user"})
            return {"success": True, "sponsored": False, "payer": "user", **estimated}

        max_tx = _amount_wei("COMMUNITY_GARDEN_GAS_MAX_PER_TX_CELO", "0.02")
        daily_cap = _amount_wei("COMMUNITY_GARDEN_GAS_DAILY_CAP_CELO", "2")
        if required > max_tx:
            raise GardenGasSponsorError("The estimated fee is above the app's Garden gas-support limit. Add CELO and try again later.",
                                        status_code=503, detail={**estimated, "sponsored": False, "payer": "user"})
        if daily_cap <= 0:
            raise GardenGasSponsorError("App-funded Garden gas is paused. Add CELO to continue.", status_code=503)

        sponsor_balance = int(context.w3.eth.get_balance(context.account.address))
        sponsor_gas_price = int(context.w3.eth.gas_price * 1.2)
        if sponsor_balance < required + SPONSOR_TX_GAS * sponsor_gas_price:
            raise GardenGasSponsorError("App gas-support wallet is temporarily underfunded. Please try later.", status_code=503)
        nonce = context.w3.eth.get_transaction_count(context.account.address, "pending")
        tx = {"chainId": CHAIN_ID, "nonce": nonce, "to": wallet, "value": required,
              "gas": SPONSOR_TX_GAS, "gasPrice": sponsor_gas_price}
        signed = context.account.sign_transaction(tx)
        raw = bytes(signed.raw_transaction)
        tx_hash = Web3.to_hex(Web3.keccak(raw))
        if not _reserve(db, wallet, request_id, required, daily_cap):
            raise GardenGasSponsorError("Garden gas support reached its per-wallet or daily limit. Add CELO or try again tomorrow.",
                                        status_code=429, detail={**estimated, "sponsored": False, "payer": "user"})
        try:
            db.table("community_garden_gas_sponsorships").update({
                "sponsor_tx_hash": tx_hash, "signed_tx_hex": Web3.to_hex(raw),
                "amount_wei": str(required), "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("request_id", request_id).execute()
        except Exception as exc:
            try:
                db.rpc("release_community_garden_gas_sponsorship", {"p_wallet": wallet.lower(), "p_request_id": request_id}).execute()
            except Exception:
                pass
            raise GardenGasSponsorError("Gas support was reserved but not broadcast. Retry later; no CELO was sent.", status_code=503) from exc
        try:
            broadcast_hash = Web3.to_hex(context.w3.eth.send_raw_transaction(raw))
            if broadcast_hash.lower() != tx_hash.lower():
                raise GardenGasSponsorError("Celo returned an unexpected gas-support transaction hash.", status_code=503)
        except GardenGasSponsorError:
            raise
        except Exception:
            try:
                context.w3.eth.get_transaction(tx_hash)
            except Exception as exc:
                raise GardenGasSponsorError("Gas support is awaiting Celo RPC confirmation. Retry this step; the same transaction will be reused.",
                                            status_code=503, detail={"sponsored": False, "sponsor_tx_hash": tx_hash}) from exc
        _update(db, request_id, {"status": "sent", "updated_at": datetime.now(timezone.utc).isoformat()})
        return {"success": True, "sponsored": True, "payer": "app", "sponsor_tx_hash": tx_hash,
                "amount_wei": str(required), "amount_celo": f"{required / WEI_PER_CELO:.8f}",
                "post_topup_wait_seconds": POST_TOPUP_WAIT_SECONDS, **estimated}
    except GardenGasSponsorError:
        raise
    except Exception as exc:
        raise GardenGasSponsorError("Unable to prepare Garden gas support. No Garden purchase was submitted.", status_code=503) from exc
