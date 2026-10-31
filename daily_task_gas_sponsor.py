"""Bounded Celo gas sponsorship for payment-gated Daily Task submissions."""
from __future__ import annotations

from datetime import datetime, timezone
import os

from web3 import Web3

from daily_task_contract_service import (
    DailyTaskContractError,
    SUBMISSION_FEE_WEI,
    get_contract_context,
)

WEI_PER_CELO = 10**18
APPROVAL_GAS_FALLBACK = 100_000
PAYMENT_GAS_FALLBACK = 250_000
GAS_PRICE_BUFFER_BPS = 13_000  # 1.30x current Celo gas price.
SPONSOR_TX_GAS = 21_000


class DailyTaskGasSponsorError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400, detail: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or {}


def _env_decimal(name: str, default: str) -> float:
    try:
        value = float(os.getenv(name, default))
        if value < 0 or value != value or value == float("inf"):
            return float(default)
        return value
    except Exception:
        return float(default)


def _get_supabase():
    from supabase_client import get_supabase_admin_client, get_supabase_client
    return get_supabase_admin_client() or get_supabase_client()


def _execute_gas_reservation(supabase, wallet: str, intent_id: str, amount_wei: int, daily_cap_wei: int) -> bool:
    day = datetime.now(timezone.utc).date().isoformat()
    try:
        response = supabase.rpc("reserve_daily_task_gas_sponsorship", {
            "p_wallet": wallet.lower(),
            "p_day_utc": day,
            "p_intent_id": intent_id,
            "p_amount_wei": str(amount_wei),
            "p_daily_cap_wei": str(daily_cap_wei),
        }).execute()
        data = getattr(response, "data", None)
        if isinstance(data, list):
            data = data[0] if data else False
        if isinstance(data, dict):
            data = data.get("reserve_daily_task_gas_sponsorship", data.get("reserved", False))
        return data is True or str(data).lower() == "true"
    except Exception as exc:
        raise DailyTaskGasSponsorError("Gas support could not be reserved safely. No CELO was sent.", status_code=503) from exc


def _update_sponsorship(supabase, intent_id: str, values: dict) -> None:
    try:
        supabase.table("daily_task_gas_sponsorships").update(values).eq("intent_id", intent_id).execute()
    except Exception:
        # A sent transaction remains verifiable on-chain even if a DB status
        # update temporarily fails. Do not re-send funds in the same intent.
        pass


def _release_unbroadcast_reservation(supabase, wallet: str, intent_id: str) -> None:
    """Release a slot only if no signed/broadcast transaction was persisted."""
    try:
        supabase.rpc("release_daily_task_gas_sponsorship", {
            "p_wallet": wallet.lower(),
            "p_intent_id": intent_id,
        }).execute()
    except Exception:
        # Leave the reservation intact if we cannot prove it was deleted.
        pass


def _sponsorship_response(prior_row: dict, estimated: dict) -> dict:
    amount = int(prior_row.get("amount_wei") or 0)
    return {
        "success": True,
        "sponsored": True,
        "payer": "app",
        "sponsor_tx_hash": prior_row["sponsor_tx_hash"],
        "amount_wei": str(amount),
        "amount_celo": f"{amount / WEI_PER_CELO:.8f}",
        **estimated,
    }


def _resume_sponsorship(context, supabase, intent_hex: str, prior_row: dict, estimated: dict) -> dict:
    """Return an existing top-up or rebroadcast its identical signed payload."""
    tx_hash = str(prior_row.get("sponsor_tx_hash") or "")
    status = str(prior_row.get("status") or "")
    raw_hex = str(prior_row.get("signed_tx_hex") or "")
    if status == "sent" and tx_hash:
        return _sponsorship_response(prior_row, estimated)
    if status != "reserved" or not tx_hash or not raw_hex:
        raise DailyTaskGasSponsorError(
            "A gas-support transaction is still being reconciled. Retry shortly; do not create another payment.",
            status_code=409,
        )

    tx_bytes = Web3.to_bytes(hexstr=raw_hex)
    expected_hash = Web3.to_hex(Web3.keccak(tx_bytes)).lower()
    if expected_hash != tx_hash.lower():
        raise DailyTaskGasSponsorError("Stored gas-support transaction failed its integrity check. Contact support.", status_code=503)
    try:
        try:
            context.w3.eth.get_transaction(tx_hash)
        except Exception:
            # Rebroadcast the exact same signed transaction: if an earlier
            # request reached the node, it has the same nonce/hash and cannot
            # transfer a second top-up.
            context.w3.eth.send_raw_transaction(tx_bytes)
    except Exception as exc:
        try:
            context.w3.eth.get_transaction(tx_hash)
        except Exception as lookup_exc:
            raise DailyTaskGasSponsorError(
                "Gas support is awaiting Celo RPC confirmation. Retry this step; the app will reuse the same transaction.",
                status_code=503,
                detail={"sponsored": False, "sponsor_tx_hash": tx_hash},
            ) from lookup_exc
    _update_sponsorship(supabase, intent_hex, {
        "status": "sent",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    prior_row["status"] = "sent"
    return _sponsorship_response(prior_row, estimated)


def sponsor_for_payment(wallet: str, intent_id: str) -> dict:
    """Fund the estimated task tx gas only for an eligible signed-in wallet."""
    try:
        context = get_contract_context()
        wallet = Web3.to_checksum_address(wallet)
        intent_hex = Web3.to_hex(Web3.to_bytes(hexstr=intent_id))
        if len(Web3.to_bytes(hexstr=intent_hex)) != 32:
            raise DailyTaskGasSponsorError("Invalid payment intent.")
        supabase = _get_supabase()
        if not supabase:
            raise DailyTaskGasSponsorError("Gas support is temporarily unavailable.", status_code=503)
        result = supabase.table("daily_task_payment_intents").select("*").eq("intent_id", intent_hex).limit(1).execute()
        if not result.data:
            raise DailyTaskGasSponsorError("Payment intent not found.", status_code=404)
        intent = result.data[0]
        if str(intent.get("wallet_address", "")).lower() != wallet.lower():
            raise DailyTaskGasSponsorError("Payment intent belongs to another wallet.", status_code=403)
        if intent.get("status") != "issued":
            raise DailyTaskGasSponsorError("Payment intent is no longer available.", status_code=409)
        if int(intent.get("voucher_deadline") or 0) <= int(datetime.now(timezone.utc).timestamp()):
            raise DailyTaskGasSponsorError("Payment authorization expired. Start the task again.", status_code=410)
        if int(intent.get("reward_amount_wei") or 0) <= 0:
            raise DailyTaskGasSponsorError("Payment authorization is invalid.", status_code=400)
        token_balance = int(context.token.functions.balanceOf(wallet).call())
        if token_balance < SUBMISSION_FEE_WEI:
            raise DailyTaskGasSponsorError("You need at least 20 G$ in your wallet to pay the non-refundable submission fee.", status_code=402)

        balance_wei = int(context.w3.eth.get_balance(wallet))
        threshold = int(_env_decimal("DAILY_TASK_GAS_SPONSOR_THRESHOLD_CELO", "0.3") * WEI_PER_CELO)
        gas_price = int(context.w3.eth.gas_price)
        if gas_price <= 0:
            raise DailyTaskGasSponsorError("Unable to estimate Celo network fees. Please try again.", status_code=503)

        allowance = int(context.token.functions.allowance(wallet, context.contract_address).call())
        approval_gas = 0
        if allowance < SUBMISSION_FEE_WEI:
            try:
                approval_gas = int(context.token.functions.approve(context.contract_address, SUBMISSION_FEE_WEI).estimate_gas({"from": wallet}))
            except Exception:
                approval_gas = APPROVAL_GAS_FALLBACK
        if allowance >= SUBMISSION_FEE_WEI:
            try:
                pay = context.contract.functions.payTaskSubmission(
                    Web3.to_bytes(hexstr=intent_hex),
                    Web3.to_bytes(hexstr=intent["platform_hash"]),
                    Web3.to_bytes(hexstr=intent["post_commitment"]),
                    int(intent["reward_amount_wei"]),
                    int(intent["voucher_deadline"]),
                    intent["owner_signature"],
                )
                payment_gas = int(pay.estimate_gas({"from": wallet}))
            except Exception:
                payment_gas = PAYMENT_GAS_FALLBACK
        else:
            # The contract call cannot estimate until the wallet approves; use
            # the conservative configured fallback for the exact same call.
            payment_gas = PAYMENT_GAS_FALLBACK
        total_gas = approval_gas + payment_gas
        required_wei = (total_gas * gas_price * GAS_PRICE_BUFFER_BPS + 9_999) // 10_000
        if required_wei <= 0:
            raise DailyTaskGasSponsorError("Could not estimate the transaction fee.", status_code=503)
        estimated = {
            "estimated_gas": total_gas,
            "estimated_fee_wei": str(required_wei),
            "estimated_fee_celo": f"{required_wei / WEI_PER_CELO:.8f}",
            "user_balance_celo": f"{balance_wei / WEI_PER_CELO:.8f}",
        }

        # Check for an earlier attempt before the threshold branch: the user
        # may have closed their tab after the app sent its top-up. Reusing the
        # original transaction is safe even if that credit now moves the wallet
        # above the 0.3 CELO threshold.
        try:
            prior = supabase.table("daily_task_gas_sponsorships").select("status,amount_wei,sponsor_tx_hash,signed_tx_hex").eq("intent_id", intent_hex).limit(1).execute()
            prior_rows = getattr(prior, "data", None) or []
            if prior_rows:
                return _resume_sponsorship(context, supabase, intent_hex, prior_rows[0], estimated)
        except DailyTaskGasSponsorError:
            raise
        except Exception:
            # The atomic reservation below remains the final duplicate guard.
            pass

        if balance_wei >= threshold:
            if balance_wei < required_wei:
                raise DailyTaskGasSponsorError(
                    "Your wallet has at least 0.3 CELO, so you pay the network fee, but the current estimate is higher than your balance. Add CELO and try again.",
                    status_code=402,
                    detail={**estimated, "sponsored": False, "payer": "user"},
                )
            return {"success": True, "sponsored": False, "payer": "user", **estimated}

        max_tx = int(_env_decimal("DAILY_TASK_GAS_MAX_PER_TX_CELO", "0.02") * WEI_PER_CELO)
        if required_wei > max_tx:
            raise DailyTaskGasSponsorError(
                "The current estimated fee is above the app’s per-task gas support limit. Add CELO and try again later.",
                status_code=503,
                detail={**estimated, "sponsored": False, "payer": "user"},
            )
        daily_cap = int(_env_decimal("DAILY_TASK_GAS_DAILY_CAP_CELO", "2") * WEI_PER_CELO)
        if daily_cap <= 0:
            raise DailyTaskGasSponsorError("App-funded gas support is paused. Add CELO to continue.", status_code=503)
        if required_wei <= 0 or required_wei > max_tx:
            raise DailyTaskGasSponsorError("The gas support amount is outside configured safety limits.", status_code=503)

        sponsor_balance = int(context.w3.eth.get_balance(context.account.address))
        sponsor_tx_gas_price = int(context.w3.eth.gas_price * 1.2)
        sponsor_tx_fee = SPONSOR_TX_GAS * sponsor_tx_gas_price
        if sponsor_balance < required_wei + sponsor_tx_fee:
            raise DailyTaskGasSponsorError("App gas-support wallet is temporarily underfunded. Please add CELO or try later.", status_code=503)
        nonce = context.w3.eth.get_transaction_count(context.account.address, "pending")
        transaction = {
            "chainId": 42220,
            "nonce": nonce,
            "to": wallet,
            "value": required_wei,
            "gas": SPONSOR_TX_GAS,
            "gasPrice": sponsor_tx_gas_price,
        }
        signed = context.account.sign_transaction(transaction)
        raw_tx = bytes(signed.raw_transaction)
        tx_hash_hex = Web3.to_hex(Web3.keccak(raw_tx))
        if not _execute_gas_reservation(supabase, wallet, intent_hex, required_wei, daily_cap):
            raise DailyTaskGasSponsorError(
                "App-funded gas support has reached its per-wallet or daily limit. Add CELO or try again tomorrow.",
                status_code=429,
                detail={**estimated, "sponsored": False, "payer": "user"},
            )
        try:
            supabase.table("daily_task_gas_sponsorships").update({
                "sponsor_tx_hash": tx_hash_hex,
                "signed_tx_hex": Web3.to_hex(raw_tx),
                "amount_wei": str(required_wei),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("intent_id", intent_hex).execute()
        except Exception as exc:
            _release_unbroadcast_reservation(supabase, wallet, intent_hex)
            raise DailyTaskGasSponsorError("Gas support was reserved but not broadcast. Retry later; no CELO has been sent.", status_code=503) from exc
        try:
            returned_hash = Web3.to_hex(context.w3.eth.send_raw_transaction(raw_tx))
            if returned_hash.lower() != tx_hash_hex.lower():
                raise DailyTaskGasSponsorError("Celo returned an unexpected gas-support transaction hash.", status_code=503)
        except DailyTaskGasSponsorError:
            raise
        except Exception:
            # Leave the signed transaction stored as `reserved`. A retry will
            # query or rebroadcast exactly this payload, never create a new one.
            try:
                context.w3.eth.get_transaction(tx_hash_hex)
            except Exception as exc:
                raise DailyTaskGasSponsorError(
                    "Gas support is awaiting Celo RPC confirmation. Retry this step; the app will reuse the same transaction.",
                    status_code=503,
                    detail={"sponsored": False, "sponsor_tx_hash": tx_hash_hex},
                ) from exc
        _update_sponsorship(supabase, intent_hex, {
            "status": "sent",
            "sponsor_tx_hash": tx_hash_hex,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return {
            "success": True,
            "sponsored": True,
            "payer": "app",
            "sponsor_tx_hash": tx_hash_hex,
            "amount_wei": str(required_wei),
            "amount_celo": f"{required_wei / WEI_PER_CELO:.8f}",
            **estimated,
        }
    except DailyTaskGasSponsorError:
        raise
    except DailyTaskContractError as exc:
        raise DailyTaskGasSponsorError(str(exc), status_code=503) from exc
    except Exception as exc:
        raise DailyTaskGasSponsorError("Unable to prepare Daily Task gas support. No Daily Task payment was submitted.", status_code=503) from exc
