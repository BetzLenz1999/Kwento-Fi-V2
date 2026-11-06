"""Bounded backend-funded CELO gas for Farming V2 farm starts only.

The sponsor transfers native CELO to the authenticated farmer's wallet. The
farmer still signs the G$ approval and startFarm transactions in their wallet.
This module intentionally uses dedicated FARMING_GAS_SPONSOR_* configuration;
it does not reuse the Daily Task signer or Community Garden budget.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import logging
import os
import re

logger = logging.getLogger(__name__)

CHAIN_ID = 42220
WEI_PER_CELO = 10**18
WEI_PER_GD = 10**18
GAS_PRICE_BUFFER_BPS = 13_000
SPONSOR_TX_GAS = 21_000
POST_TOPUP_WAIT_SECONDS = 30

TOKEN_ABI = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view", "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view", "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable", "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "decimals", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
]
FARM_V2_ABI = [
    {"type": "function", "name": "goodDollarToken", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "availableRewards", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "REWARD_BPS", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "newFarmsPaused", "stateMutability": "view", "inputs": [], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "farms", "stateMutability": "view", "inputs": [{"name": "farmer", "type": "address"}], "outputs": [{"type": "tuple", "components": [
        {"name": "principal", "type": "uint256"}, {"name": "maxReward", "type": "uint256"},
        {"name": "claimedReward", "type": "uint256"}, {"name": "startedAt", "type": "uint64"},
        {"name": "active", "type": "bool"},
    ]}]},
    {"type": "function", "name": "startFarm", "stateMutability": "nonpayable", "inputs": [{"name": "amount", "type": "uint256"}], "outputs": []},
]


class FarmingGasSponsorError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400, detail: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or {}


def _amount_wei(name: str, default: str) -> int:
    """Parse a non-negative CELO amount without float math; malformed config fails closed."""
    try:
        raw = os.getenv(name)
        value = Decimal(default if raw is None or not raw.strip() else raw.strip())
        if not value.is_finite() or value < 0:
            raise InvalidOperation
        return int((value * WEI_PER_CELO).to_integral_value(rounding=ROUND_CEILING))
    except Exception as exc:
        raise FarmingGasSponsorError(f"Invalid Farming gas-sponsor setting: {name}.", status_code=503) from exc


def _gas_fallback(name: str, default: str) -> int:
    try:
        value = int(os.getenv(name, default))
        if value <= 0:
            raise ValueError("must be positive")
        return value
    except Exception as exc:
        raise FarmingGasSponsorError(f"Invalid Farming gas-sponsor gas estimate: {name}.", status_code=503) from exc


def _principal_wei(value) -> int:
    """Accept only a decimal integer in wei and enforce the deployed V2 terms."""
    if isinstance(value, bool):
        raise FarmingGasSponsorError("Invalid farm principal.")
    try:
        text = str(value)
        if not re.fullmatch(r"[0-9]+", text):
            raise ValueError("principal must be a decimal integer")
        amount = int(text, 10)
    except (TypeError, ValueError) as exc:
        raise FarmingGasSponsorError("Invalid farm principal.") from exc
    minimum, maximum, increment = 1_000 * WEI_PER_GD, 20_000 * WEI_PER_GD, 100 * WEI_PER_GD
    if amount < minimum or amount > maximum or amount % increment:
        raise FarmingGasSponsorError("Farm principal must be 1,000–20,000 G$ in 100 G$ increments.")
    return amount


def _request_id(value) -> str:
    request_id = str(value or "").strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{64}", request_id):
        raise FarmingGasSponsorError("Invalid gas-support request ID.")
    return request_id


def _wallet_address(value) -> str:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", str(value or "")):
        raise FarmingGasSponsorError("Your authenticated wallet is unavailable.", status_code=401)
    return str(value)


def _should_sponsor(balance_wei: int, threshold_wei: int) -> bool:
    """Strict threshold: exactly 0.2 CELO and above is user-paid."""
    return int(balance_wei) < int(threshold_wei)


def _supabase():
    # The migration grants the sponsorship RPC/table only to service_role.
    from supabase_client import get_supabase_admin_client
    return get_supabase_admin_client()


def _rpc_urls() -> list[str]:
    """Return the primary Celo RPC followed by configured fallbacks."""
    values = [os.getenv("CELO_RPC_URL", "https://forno.celo.org")]
    values.extend((os.getenv("CELO_RPC_FALLBACKS", "") or "").split(","))
    urls = []
    for value in values:
        url = str(value or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls or ["https://forno.celo.org"]


def _context():
    """Verify V2 deployment and the independent gas-sponsor signing key."""
    from eth_account import Account
    from web3 import Web3
    from chain_rpc import make_provider
    from farming.blockchain import farming_v2_runtime

    enabled = (os.getenv("FARMING_GAS_SPONSOR_ENABLED", "false") or "false").strip().lower()
    if enabled not in {"1", "true", "yes", "on", "enabled"}:
        raise FarmingGasSponsorError("Farming gas sponsorship is not enabled.", status_code=503)

    # Do not let a stale or rate-limited primary RPC make a low-balance wallet
    # look ineligible. Pick one endpoint that can both verify V2 and read the
    # wallet before preparing the sponsor transaction.
    runtime = None
    w3 = None
    last_reason = "Farming V2 is unavailable or unverified; no gas was sent."
    for candidate_rpc in _rpc_urls():
        try:
            candidate_runtime = farming_v2_runtime(rpc_url=candidate_rpc)
            if not candidate_runtime.get("verified") or not candidate_runtime.get("enabled"):
                continue
            candidate_w3 = Web3(make_provider(candidate_rpc, timeout=15))
            if int(candidate_w3.eth.chain_id) != CHAIN_ID:
                last_reason = "Gas sponsorship is available only on Celo Mainnet."
                continue
            runtime, w3 = candidate_runtime, candidate_w3
            break
        except Exception as exc:
            logger.warning("Farming gas sponsor RPC unavailable (%s)", type(exc).__name__)
            last_reason = "Farming gas support is temporarily unavailable."
    if runtime is None or w3 is None:
        raise FarmingGasSponsorError(last_reason, status_code=503)

    private_key = (os.getenv("FARMING_GAS_SPONSOR_PRIVATE_KEY") or "").strip()
    expected_signer = (os.getenv("FARMING_GAS_SPONSOR_SIGNER_ADDRESS") or "").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", private_key) or not Web3.is_address(expected_signer):
        raise FarmingGasSponsorError("Farming gas sponsor signer is not configured safely.", status_code=503)
    try:
        account = Account.from_key(private_key)
        expected_signer = Web3.to_checksum_address(expected_signer)
    except Exception as exc:
        raise FarmingGasSponsorError("Farming gas sponsor signer configuration is invalid.", status_code=503) from exc
    if account.address.lower() != expected_signer.lower():
        raise FarmingGasSponsorError("Farming gas sponsor key does not match its configured address.", status_code=503)
    if account.address.lower() == str(runtime.get("admin_signer") or "").lower():
        raise FarmingGasSponsorError("Use a dedicated Farming gas-sponsor key, not the Farming V2 admin signer.", status_code=503)

    contract_address = Web3.to_checksum_address(runtime["contract"])
    token_address = Web3.to_checksum_address(runtime["token"])
    if not w3.eth.get_code(contract_address):
        raise FarmingGasSponsorError("Verified Farming V2 contract code is unavailable.", status_code=503)
    farm = w3.eth.contract(address=contract_address, abi=FARM_V2_ABI)
    deployed_token = Web3.to_checksum_address(farm.functions.goodDollarToken().call())
    if deployed_token.lower() != token_address.lower():
        raise FarmingGasSponsorError("Farming V2 token configuration does not match the deployed contract.", status_code=503)
    token = w3.eth.contract(address=token_address, abi=TOKEN_ABI)
    if int(token.functions.decimals().call()) != 18:
        raise FarmingGasSponsorError("Farming gas sponsorship requires the verified 18-decimal G$ token.", status_code=503)
    return {"Web3": Web3, "w3": w3, "account": account, "farm": farm, "token": token,
            "wallet_contract": contract_address, "token_address": token_address, "sponsor_address": expected_signer}


def _result_rows(result) -> list[dict]:
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    if isinstance(data, dict):
        return [data]
    return [row for row in (data or []) if isinstance(row, dict)]


def _sponsor_response(row: dict) -> dict:
    amount = int(row.get("amount_wei") or 0)
    estimated_fee = int(row.get("estimated_fee_wei") or amount)
    return {"success": True, "sponsored": True, "payer": "app",
            "sponsor_tx_hash": str(row.get("sponsor_tx_hash") or ""),
            "amount_wei": str(amount), "amount_celo": f"{amount / WEI_PER_CELO:.8f}",
            "estimated_fee_wei": str(estimated_fee), "estimated_fee_celo": f"{estimated_fee / WEI_PER_CELO:.8f}",
            "post_topup_wait_seconds": POST_TOPUP_WAIT_SECONDS}


def _update(db, request_id: str, values: dict) -> None:
    try:
        db.table("farming_gas_sponsorships").update(values).eq("request_id", request_id).execute()
    except Exception:
        # The persisted signed tx is the source of truth for safe idempotent retry.
        logger.warning("Farming gas sponsorship status update failed")


def _resume_existing(context: dict, db, row: dict, wallet: str, principal: int) -> dict:
    Web3 = context["Web3"]
    if str(row.get("wallet_address") or "").lower() != wallet.lower():
        raise FarmingGasSponsorError("Gas-support request belongs to another wallet.", status_code=403)
    if int(row.get("principal_wei") or 0) != principal:
        raise FarmingGasSponsorError("Gas-support request does not match this farm amount.", status_code=409)
    tx_hash = str(row.get("sponsor_tx_hash") or "")
    raw_hex = str(row.get("signed_tx_hex") or "")
    if row.get("status") == "sent" and tx_hash:
        return _sponsor_response(row)
    if row.get("status") != "reserved" or not tx_hash or not raw_hex:
        raise FarmingGasSponsorError("A gas-support transaction is being reconciled. Retry with the same request ID.", status_code=409)
    raw = Web3.to_bytes(hexstr=raw_hex)
    if Web3.to_hex(Web3.keccak(raw)).lower() != tx_hash.lower():
        raise FarmingGasSponsorError("Stored gas-support transaction failed its integrity check.", status_code=503)
    try:
        context["w3"].eth.get_transaction(tx_hash)
    except Exception:
        try:
            context["w3"].eth.send_raw_transaction(raw)
        except Exception:
            try:
                context["w3"].eth.get_transaction(tx_hash)
            except Exception as exc:
                raise FarmingGasSponsorError("Gas support is awaiting Celo RPC confirmation. Retry with the same request ID.",
                                             status_code=503, detail={"sponsor_tx_hash": tx_hash}) from exc
    _update(db, str(row["request_id"]), {"status": "sent", "updated_at": datetime.now(timezone.utc).isoformat()})
    row["status"] = "sent"
    return _sponsor_response(row)


def _reserve(db, *, wallet: str, request_id: str, principal: int, amount: int,
             per_wallet_cap: int, daily_cap: int, daily_cost: int, daily_tx_cap: int,
             signer: str, nonce: int) -> bool:
    day = datetime.now(timezone.utc).date().isoformat()
    try:
        result = db.rpc("reserve_farming_gas_sponsorship", {
            "p_wallet": wallet.lower(), "p_day_utc": day, "p_request_id": request_id,
            "p_principal_wei": str(principal), "p_amount_wei": str(amount),
            "p_wallet_daily_cap_wei": str(per_wallet_cap), "p_daily_cap_wei": str(daily_cap),
            "p_daily_cost_wei": str(daily_cost), "p_daily_tx_cap": int(daily_tx_cap),
            "p_sponsor_address": signer.lower(), "p_sponsor_nonce": int(nonce),
        }).execute()
        value = getattr(result, "data", None)
        if isinstance(value, list):
            value = value[0] if value else False
        if isinstance(value, dict):
            value = value.get("reserve_farming_gas_sponsorship", value.get("reserved", False))
        return value is True or str(value).lower() == "true"
    except Exception as exc:
        raise FarmingGasSponsorError("Farming gas support could not be reserved safely. No CELO was sent.", status_code=503) from exc


def _release_unbroadcast(db, wallet: str, request_id: str) -> None:
    try:
        db.rpc("release_farming_gas_sponsorship", {
            "p_wallet": wallet.lower(), "p_request_id": request_id,
        }).execute()
    except Exception:
        # Keep a reservation unless the database proves no signed payload exists.
        pass


def sponsor_for_start(wallet_value, principal_value, request_id_value) -> dict:
    """Sponsor estimated approval+startFarm gas only below the configured threshold."""
    wallet = _wallet_address(wallet_value)
    principal = _principal_wei(principal_value)
    request_id = _request_id(request_id_value)
    context = _context()
    Web3 = context["Web3"]
    wallet = Web3.to_checksum_address(wallet)
    db = _supabase()
    if db is None:
        raise FarmingGasSponsorError("Farming gas support is temporarily unavailable.", status_code=503)

    try:
        required = principal
        if int(context["token"].functions.balanceOf(wallet).call()) < required:
            raise FarmingGasSponsorError("Your wallet needs enough G$ for this farm before gas support can be provided.", status_code=402)
        farm_record = context["farm"].functions.farms(wallet).call()
        if bool(farm_record.active if hasattr(farm_record, "active") else farm_record[4]):
            raise FarmingGasSponsorError("You already have an active V2 farm.", status_code=409)
        if bool(context["farm"].functions.newFarmsPaused().call()):
            raise FarmingGasSponsorError("New V2 farms are temporarily paused.", status_code=503)
        reward_bps = int(context["farm"].functions.REWARD_BPS().call())
        reward_cap = (principal * reward_bps) // 10_000
        if int(context["farm"].functions.availableRewards().call()) < reward_cap:
            raise FarmingGasSponsorError("Sponsor rewards cannot currently cover this farm's full reward cap.", status_code=503)

        allowance = int(context["token"].functions.allowance(wallet, context["wallet_contract"]).call())
        approval_gas = 0
        if allowance < principal:
            try:
                approval_gas = int(context["token"].functions.approve(context["wallet_contract"], principal).estimate_gas({"from": wallet}))
            except Exception:
                approval_gas = _gas_fallback("FARMING_GAS_APPROVAL_GAS_FALLBACK", "100000")
        try:
            # Works when the user already has allowance. Without it, startFarm
            # simulation reverts and the configured conservative fallback is used.
            start_gas = int(context["farm"].functions.startFarm(principal).estimate_gas({"from": wallet}))
        except Exception:
            start_gas = _gas_fallback("FARMING_GAS_START_GAS_FALLBACK", "300000")
        if approval_gas < 0 or start_gas <= 0:
            raise FarmingGasSponsorError("Unable to estimate Farming transaction fees.", status_code=503)
        total_gas = approval_gas + start_gas
        gas_price = int(context["w3"].eth.gas_price)
        if gas_price <= 0:
            raise FarmingGasSponsorError("Unable to read the current Celo gas price.", status_code=503)
        estimated_fee = (total_gas * gas_price * GAS_PRICE_BUFFER_BPS + 9_999) // 10_000
        balance = int(context["w3"].eth.get_balance(wallet))
        threshold = _amount_wei("FARMING_GAS_SPONSOR_THRESHOLD_CELO", "0.2")
        estimate = {"estimated_gas": total_gas, "estimated_fee_wei": str(estimated_fee),
                    "estimated_fee_celo": f"{estimated_fee / WEI_PER_CELO:.8f}",
                    "user_balance_celo": f"{balance / WEI_PER_CELO:.8f}"}

        # Match Daily Task's recovery point: check for an existing signed
        # sponsorship after estimating the fee but before the threshold branch.
        # A retry must reuse the persisted top-up even if that earlier credit
        # has since moved the wallet above the sponsorship threshold.
        try:
            prior = _result_rows(db.table("farming_gas_sponsorships").select(
                "request_id,wallet_address,principal_wei,amount_wei,estimated_fee_wei,sponsor_tx_hash,signed_tx_hex,status"
            ).eq("request_id", request_id).limit(1).execute())
            if prior:
                return _resume_existing(context, db, prior[0], wallet, principal)
        except FarmingGasSponsorError:
            raise
        except Exception:
            # The atomic reservation below remains the final duplicate guard.
            pass

        if not _should_sponsor(balance, threshold):
            if balance < estimated_fee:
                threshold_label = format((Decimal(threshold) / Decimal(WEI_PER_CELO)).normalize(), "f")
                raise FarmingGasSponsorError(
                    f"Your wallet has {threshold_label} CELO or more, so it pays its own gas; add CELO because the estimated fee exceeds the balance.",
                    status_code=402, detail={**estimate, "sponsored": False, "payer": "user"})
            return {"success": True, "sponsored": False, "payer": "user", **estimate}

        max_tx = _amount_wei("FARMING_GAS_SPONSOR_MAX_PER_TX_CELO", "0.02")
        per_wallet_daily = _amount_wei("FARMING_GAS_SPONSOR_WALLET_DAILY_CAP_CELO", "0.02")
        daily_cap = _amount_wei("FARMING_GAS_SPONSOR_DAILY_CAP_CELO", "2")
        try:
            daily_tx_cap = int(os.getenv("FARMING_GAS_SPONSOR_DAILY_TX_CAP", "100"))
        except Exception as exc:
            raise FarmingGasSponsorError("Invalid Farming gas-sponsor daily transaction limit.", status_code=503) from exc
        if daily_cap <= 0 or per_wallet_daily <= 0 or max_tx <= 0 or daily_tx_cap <= 0:
            raise FarmingGasSponsorError("Farming gas sponsorship is paused by its configured spending limits.", status_code=503)
        if estimated_fee > max_tx or estimated_fee > per_wallet_daily:
            raise FarmingGasSponsorError("The estimated fee is above the Farming gas-support limit. Add CELO and try again later.",
                                         status_code=503, detail={**estimate, "sponsored": False, "payer": "user"})

        sponsor_balance = int(context["w3"].eth.get_balance(context["account"].address))
        sponsor_gas_price = max(gas_price, int(gas_price * 1.2))
        sponsor_tx_cost = SPONSOR_TX_GAS * sponsor_gas_price
        daily_cost = estimated_fee + sponsor_tx_cost
        if sponsor_balance < daily_cost:
            raise FarmingGasSponsorError("Farming gas-support wallet is temporarily underfunded. Please try later.", status_code=503)
        nonce = int(context["w3"].eth.get_transaction_count(context["account"].address, "pending"))
        signed_tx = context["account"].sign_transaction({
            "chainId": CHAIN_ID, "nonce": nonce, "to": wallet, "value": estimated_fee,
            "gas": SPONSOR_TX_GAS, "gasPrice": sponsor_gas_price,
        })
        raw = bytes(signed_tx.raw_transaction)
        tx_hash = Web3.to_hex(Web3.keccak(raw))
        if not _reserve(db, wallet=wallet, request_id=request_id, principal=principal, amount=estimated_fee,
                        per_wallet_cap=per_wallet_daily, daily_cap=daily_cap,
                        daily_cost=daily_cost, daily_tx_cap=daily_tx_cap,
                        signer=context["sponsor_address"], nonce=nonce):
            raise FarmingGasSponsorError("Farming gas support reached its per-wallet or global UTC-day limit, or the sponsor nonce is busy. Add CELO or retry shortly.",
                                         status_code=429, detail={**estimate, "sponsored": False, "payer": "user"})
        try:
            db.table("farming_gas_sponsorships").update({
                "sponsor_tx_hash": tx_hash, "signed_tx_hex": Web3.to_hex(raw),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("request_id", request_id).execute()
        except Exception as exc:
            _release_unbroadcast(db, wallet, request_id)
            raise FarmingGasSponsorError("Gas support was reserved but not broadcast. Retry with the same request ID; no CELO was sent.",
                                         status_code=503) from exc
        try:
            broadcast_hash = Web3.to_hex(context["w3"].eth.send_raw_transaction(raw))
            if broadcast_hash.lower() != tx_hash.lower():
                raise FarmingGasSponsorError("Celo returned an unexpected Farming gas transaction hash.", status_code=503)
        except FarmingGasSponsorError:
            raise
        except Exception:
            try:
                context["w3"].eth.get_transaction(tx_hash)
            except Exception as exc:
                raise FarmingGasSponsorError("Gas support is awaiting Celo RPC confirmation. Retry with the same request ID.",
                                             status_code=503, detail={"sponsor_tx_hash": tx_hash}) from exc
        _update(db, request_id, {"status": "sent", "updated_at": datetime.now(timezone.utc).isoformat()})
        return {"success": True, "sponsored": True, "payer": "app", "sponsor_tx_hash": tx_hash,
                "amount_wei": str(estimated_fee), "amount_celo": f"{estimated_fee / WEI_PER_CELO:.8f}",
                "post_topup_wait_seconds": POST_TOPUP_WAIT_SECONDS, **estimate}
    except FarmingGasSponsorError:
        raise
    except Exception as exc:
        logger.warning("Farming gas sponsorship failed (%s)", type(exc).__name__)
        raise FarmingGasSponsorError("Farming gas support is temporarily unavailable. No farm transaction was submitted.",
                                     status_code=503) from exc
