"""Non-custodial G$ support for Community Feed posts.

All wallet transfers are supporter-signed on Celo. This module signs only a
short-lived EIP-712 authorization binding the post, both wallets/usernames,
amount, nonce, chain and deployed contract. SQL settlement happens only after
receipt/event verification and must be idempotent.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from . import reward_settings

logger = logging.getLogger(__name__)
CHAIN_ID = 42220
TOKEN_DECIMALS = 18
AUTH_LIFETIME_SECONDS = 15 * 60
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _admin_db():
    try:
        from supabase_client import get_supabase_admin_client
        return get_supabase_admin_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("post support admin DB unavailable: %s", exc)
        return None


def _payload(result: Any) -> dict | None:
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return None
    return data if isinstance(data, dict) else None


def _rpc(db, name: str, args: dict) -> dict:
    result = db.rpc(name, args).execute()
    return _payload(result) or {"success": False, "error": "Database returned an invalid response."}


def _settings() -> dict:
    result = reward_settings.get_settings()
    if result.get("success") is not True:
        raise RuntimeError("Community reward settings are unavailable")
    return result


def _config_address(name: str) -> str | None:
    value = (os.getenv(name) or "").strip()
    return value if ADDRESS_RE.fullmatch(value) else None


def _verify_deployment(token: str, contract: str, signer_key: str) -> bool:
    """Fail closed unless configured code/immutables match the signed intent."""
    try:
        from eth_account import Account
        from web3 import Web3
        expected_verifier = _config_address("COMMUNITY_POST_SUPPORT_VERIFIER_ADDRESS")
        if not expected_verifier or Account.from_key(signer_key).address.lower() != expected_verifier.lower():
            return False
        abi = [
            {"type": "function", "name": "verifier", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
            {"type": "function", "name": "goodDollarToken", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
        ]
        for url in _rpc_urls():
            try:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
                if int(w3.eth.chain_id) != CHAIN_ID:
                    continue
                deployed = w3.eth.contract(address=Web3.to_checksum_address(contract), abi=abi)
                actual_verifier = deployed.functions.verifier().call()
                actual_token = deployed.functions.goodDollarToken().call()
                return actual_verifier.lower() == expected_verifier.lower() and actual_token.lower() == token.lower()
            except Exception as exc:  # try configured Celo RPC fallback
                logger.warning("post support deployment check failed on %s: %s", url, exc)
        return False
    except Exception as exc:  # missing crypto/RPC dependencies or invalid key
        logger.warning("post support deployment configuration invalid: %s", exc)
        return False


def _feature_is_enabled(settings: dict) -> bool:
    return (os.getenv("COMMUNITY_POST_SUPPORT_ENABLED", "").strip().upper() == "YES"
            and settings.get("support_bonus_enabled") is True)


def _amount_to_base_units(value: Any) -> tuple[Decimal, int] | None:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    # SQL stores user-facing G$ amounts at four decimal places. Reject rather
    # than silently round any value that the ledger/support intent cannot store.
    if amount * Decimal(10_000) != (amount * Decimal(10_000)).to_integral_value():
        return None
    scaled = amount * (Decimal(10) ** TOKEN_DECIMALS)
    integral = scaled.to_integral_value()
    if scaled != integral:
        return None
    return amount, int(integral)


def _lookup_username_for_wallet(db, wallet: str) -> str | None:
    try:
        result = (db.table("user_data").select("username,wallet_address")
                  .ilike("wallet_address", wallet).limit(5).execute())
        rows = getattr(result, "data", None) or []
        matches = []
        for row in rows:
            row_wallet = str(row.get("wallet_address") or "").strip()
            username = str(row.get("username") or "").strip().lstrip("@")
            if (row_wallet.lower() == wallet.lower() and USERNAME_RE.fullmatch(username)):
                matches.append(username)
        if not matches:
            return None
        # Multiple distinct usernames mapped to one wallet are ambiguous; fail closed.
        if len({name.casefold() for name in matches}) != 1:
            return None
        return matches[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("post support username lookup failed: %s", exc)
        return None


def _eligible_post(db, post_id: int) -> dict | None:
    try:
        result = (db.table("community_feed_posts")
                  .select("id,wallet_address,username,post_type,visibility,is_hidden,is_deleted")
                  .eq("id", int(post_id)).limit(1).execute())
        rows = getattr(result, "data", None) or []
        if not rows:
            return None
        row = rows[0]
        if row.get("is_hidden") or row.get("is_deleted"):
            return None
        if row.get("post_type") != "user" or row.get("visibility") != "public":
            return None
        wallet = str(row.get("wallet_address") or "").strip()
        if not ADDRESS_RE.fullmatch(wallet):
            return None
        return row
    except Exception as exc:  # noqa: BLE001
        logger.warning("post support post lookup failed: %s", exc)
        return None


def _sign_authorization(*, support_contract: str, request_data: dict, private_key: str) -> str:
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from web3 import Web3
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("eth-account and web3 are required for G$ support") from exc

    full_message = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "SupportAuthorization": [
                {"name": "postId", "type": "uint256"},
                {"name": "supporter", "type": "address"},
                {"name": "creator", "type": "address"},
                {"name": "supporterUsernameHash", "type": "bytes32"},
                {"name": "creatorUsernameHash", "type": "bytes32"},
                {"name": "amount", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        },
        "primaryType": "SupportAuthorization",
        "domain": {
            "name": "CommunityPostSupport",
            "version": "1",
            "chainId": CHAIN_ID,
            "verifyingContract": support_contract,
        },
        "message": {
            "postId": int(request_data["postId"]),
            "supporter": request_data["supporter"],
            "creator": request_data["creator"],
            "supporterUsernameHash": Web3.keccak(text=request_data["supporterUsername"]),
            "creatorUsernameHash": Web3.keccak(text=request_data["creatorUsername"]),
            "amount": int(request_data["amount"]),
            "nonce": int(request_data["nonce"]),
            "deadline": int(request_data["deadline"]),
        },
    }
    return "0x" + Account.sign_message(
        encode_typed_data(full_message=full_message), private_key=private_key
    ).signature.hex()


def _build_transactions(*, token: str, contract: str, request_data: dict, signature: str) -> tuple[str, str]:
    """Return exact-amount approve calldata and supportPost(tuple,signature) calldata."""
    try:
        from eth_abi import encode
        from web3 import Web3
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("eth-abi and web3 are required to encode support transactions") from exc
    request_tuple = (
        int(request_data["postId"]),
        request_data["creator"],
        request_data["supporterUsername"],
        request_data["creatorUsername"],
        int(request_data["amount"]),
        int(request_data["nonce"]),
        int(request_data["deadline"]),
    )
    tuple_type = "(uint256,address,string,string,uint256,uint256,uint256)"
    selector = Web3.keccak(
        text="supportPost((uint256,address,string,string,uint256,uint256,uint256),bytes)"
    )[:4]
    encoded = encode([tuple_type, "bytes"], [request_tuple, bytes.fromhex(signature.removeprefix("0x"))])
    support_data = Web3.to_hex(selector + encoded)
    approve_data = (
        "0x095ea7b3"
        + contract.lower().removeprefix("0x").rjust(64, "0")
        + format(int(request_data["amount"]), "x").rjust(64, "0")
    )
    return approve_data, support_data


def prepare(supporter_wallet: str, post_id: int, amount_gd: Any) -> dict:
    """Validate a post and reserve reward budget before returning signed data."""
    if not ADDRESS_RE.fullmatch(str(supporter_wallet or "")):
        return {"success": False, "code": "wallet", "error": "Connect a valid wallet first."}
    db = _admin_db()
    if db is None:
        return {"success": False, "code": "database", "error": "Support is temporarily unavailable."}
    try:
        settings = _settings()
    except Exception as exc:  # fail closed until migration/settings are available
        logger.warning("post support settings unavailable: %s", exc)
        return {"success": False, "code": "configuration", "error": "Support is not configured yet."}
    if not _feature_is_enabled(settings):
        return {"success": False, "code": "disabled", "error": "G$ support is not available right now."}

    token = _config_address("COMMUNITY_POST_SUPPORT_TOKEN_ADDRESS")
    contract = _config_address("COMMUNITY_POST_SUPPORT_CONTRACT_ADDRESS")
    signer_key = (os.getenv("COMMUNITY_POST_SUPPORT_SIGNER_KEY") or "").strip()
    if not token or not contract or not re.fullmatch(r"0x[0-9a-fA-F]{64}", signer_key) or not _verify_deployment(token, contract, signer_key):
        return {"success": False, "code": "configuration", "error": "Support is not configured yet."}

    parsed = _amount_to_base_units(amount_gd)
    if not parsed:
        return {"success": False, "code": "amount", "error": "Enter a valid G$ amount."}
    amount, amount_raw = parsed
    minimum = Decimal(str(settings.get("support_min_amount_gd", "25")))
    maximum = Decimal(str(settings.get("support_max_amount_gd", "10000")))
    if amount < minimum or amount > maximum:
        return {"success": False, "code": "amount", "error": f"Support amount must be between {minimum} and {maximum} G$."}

    post = _eligible_post(db, int(post_id))
    if not post:
        return {"success": False, "code": "post", "error": "This post cannot receive support."}
    creator = str(post.get("wallet_address") or "").strip()
    if creator.lower() == supporter_wallet.lower():
        return {"success": False, "code": "self", "error": "You cannot support your own post."}
    supporter_name = _lookup_username_for_wallet(db, supporter_wallet)
    creator_name = str(post.get("username") or "").strip().lstrip("@")
    if not supporter_name or not USERNAME_RE.fullmatch(creator_name):
        return {"success": False, "code": "username", "error": "Both accounts need a valid Community username to use support."}

    bonus = Decimal(str(settings.get("support_bonus_rate_gd", "5")))
    if bonus <= 0:
        return {"success": False, "code": "bonus", "error": "Support reward is not configured."}
    support_id = str(uuid.uuid4())
    nonce = secrets.randbits(256)
    deadline = int(datetime.now(timezone.utc).timestamp()) + AUTH_LIFETIME_SECONDS
    expires_at = datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat()
    request_data = {
        "postId": int(post_id),
        "creator": creator,
        "supporterUsername": supporter_name,
        "creatorUsername": creator_name,
        "amount": amount_raw,
        "nonce": nonce,
        "deadline": deadline,
    }
    try:
        signature = _sign_authorization(
            support_contract=contract, request_data={**request_data, "supporter": supporter_wallet},
            private_key=signer_key,
        )
        approve_data, support_data = _build_transactions(
            token=token, contract=contract, request_data=request_data, signature=signature
        )
    except Exception as exc:
        logger.exception("post support EIP-712 signing failed")
        return {"success": False, "code": "signing", "error": "Could not prepare a secure support authorization."}

    args = {
        "p_support_id": support_id,
        "p_post_id": int(post_id),
        "p_supporter_wallet": supporter_wallet,
        "p_creator_wallet": creator,
        "p_supporter_username": supporter_name,
        "p_creator_username": creator_name,
        "p_amount_base_units": str(amount_raw),
        "p_amount_gd": format(amount, "f"),
        "p_token_address": token,
        "p_support_contract": contract,
        "p_nonce": str(nonce),
        "p_expires_at": expires_at,
        "p_bonus_gd": format(bonus, "f"),
        "p_budget_day_utc": datetime.now(timezone.utc).date().isoformat(),
    }
    try:
        created = _rpc(db, "community_post_support_create_intent", args)
    except Exception as exc:
        logger.warning("post support intent RPC failed: %s", exc)
        return {"success": False, "code": "reservation", "error": "Could not reserve the support reward. Please try again."}
    if created.get("success") is not True:
        return {"success": False, "code": created.get("code", "reservation"), "error": created.get("error", "Support reward budget is unavailable.")}

    return {
        "success": True,
        "support_id": support_id,
        "chain_id": CHAIN_ID,
        "token_address": token,
        "support_contract_address": contract,
        "creator": creator,
        "supporter_username": supporter_name,
        "creator_username": creator_name,
        "amount_gd": format(amount, "f"),
        "amount_base_units": str(amount_raw),
        "bonus_gd": str(created.get("bonus_gd", bonus)),
        "minimum_amount_gd": str(minimum),
        "expires_at": expires_at,
        "support_request": {
            "postId": int(post_id),
            "creator": creator,
            "supporterUsername": supporter_name,
            "creatorUsername": creator_name,
            "amount": str(amount_raw),
            "nonce": str(nonce),
            "deadline": deadline,
        },
        "signature": signature,
        "approval_transaction": {
            "to": token, "data": approve_data, "value": "0x0", "chain_id": CHAIN_ID,
        },
        "transaction": {
            "to": contract, "data": support_data, "value": "0x0", "chain_id": CHAIN_ID,
        },
        "explorer_tx_base": "https://celoscan.io/tx/",
    }


def _rpc_urls() -> list[str]:
    urls = [os.getenv("COMMUNITY_POST_SUPPORT_RPC_URL", "").strip(),
            os.getenv("CELO_RPC_URL", "").strip(), "https://forno.celo.org"]
    seen = set()
    return [url for url in urls if url and not (url in seen or seen.add(url))]


def _verify_receipt(tx_hash: str, row: dict) -> dict:
    try:
        from web3 import Web3
    except ImportError:
        return {"ok": False, "status": "rpc_unavailable", "error": "Celo verification is unavailable."}

    try:
        expected_post_id = int(row["post_id"])
        expected_amount = int(row["amount_base_units"])
        expected_supporter = str(row["supporter_wallet"]).lower()
        expected_creator = str(row["creator_wallet"]).lower()
        expected_contract = Web3.to_checksum_address(row["support_contract"])
        expected_token = Web3.to_checksum_address(row["token_address"])
    except Exception:
        return {"ok": False, "status": "mismatch", "error": "Stored support intent is invalid."}

    event_topic = Web3.to_hex(Web3.keccak(text="PostSupported(uint256,address,address,uint256,string,string)")).lower()
    transfer_topic = Web3.to_hex(Web3.keccak(text="Transfer(address,address,uint256)")).lower()
    pending_seen = False
    for url in _rpc_urls():
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 12}))
            if int(w3.eth.chain_id) != CHAIN_ID:
                continue
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt is None:
                pending_seen = True
                continue
            if int(receipt.get("status", 0)) != 1:
                return {"ok": False, "status": "reverted", "error": "The support transaction reverted."}
            tx = w3.eth.get_transaction(tx_hash)
            if (tx.get("from") or "").lower() != expected_supporter or (tx.get("to") or "").lower() != expected_contract.lower():
                return {"ok": False, "status": "mismatch", "error": "The transaction sender or contract does not match."}

            support_events = []
            transfers = []
            for log in receipt.get("logs") or []:
                topics = [Web3.to_hex(topic).lower() for topic in (log.get("topics") or [])]
                log_address = (log.get("address") or "").lower()
                if log_address == expected_contract.lower() and topics and topics[0] == event_topic:
                    if len(topics) != 4:
                        continue
                    post_id = int(topics[1], 16)
                    supporter = "0x" + topics[2][-40:]
                    creator = "0x" + topics[3][-40:]
                    amount, supporter_name, creator_name = w3.codec.decode(
                        ["uint256", "string", "string"], bytes.fromhex(Web3.to_hex(log.get("data") or "0x")[2:])
                    )
                    support_events.append((post_id, supporter.lower(), creator.lower(), int(amount), supporter_name, creator_name))
                if log_address == expected_token.lower() and topics and topics[0] == transfer_topic and len(topics) == 3:
                    sender = "0x" + topics[1][-40:]
                    receiver = "0x" + topics[2][-40:]
                    transferred = int(Web3.to_hex(log.get("data") or "0x0"), 16)
                    transfers.append((sender.lower(), receiver.lower(), transferred))
            if len(support_events) != 1:
                return {"ok": False, "status": "mismatch", "error": "Expected exactly one valid support event."}
            ev = support_events[0]
            if ev[:4] != (expected_post_id, expected_supporter, expected_creator, expected_amount):
                return {"ok": False, "status": "mismatch", "error": "Support event does not match the prepared intent."}
            if ev[4] != row.get("supporter_username") or ev[5] != row.get("creator_username"):
                return {"ok": False, "status": "mismatch", "error": "Username snapshots do not match the verified intent."}
            exact_transfers = [t for t in transfers if t == (expected_supporter, expected_creator, expected_amount)]
            if len(exact_transfers) != 1:
                return {"ok": False, "status": "mismatch", "error": "Expected one exact G$ transfer to the post author."}
            return {"ok": True, "status": "confirmed", "block_number": int(receipt.get("blockNumber", 0))}
        except Exception as exc:  # pending and RPC failures are retryable
            if "not found" in str(exc).lower() or "not mined" in str(exc).lower():
                pending_seen = True
                continue
            logger.warning("post support receipt verification failed on %s: %s", url, exc)
    return {"ok": False, "status": "pending" if pending_seen else "rpc_unavailable",
            "retryable": True, "error": "Waiting for Celo transaction confirmation." if pending_seen else "Celo confirmation is temporarily unavailable."}


def confirm(supporter_wallet: str, support_id: str, tx_hash: str) -> dict:
    """Verify receipt then atomically settle DB ledger/system post."""
    if not ADDRESS_RE.fullmatch(str(supporter_wallet or "")):
        return {"success": False, "code": "wallet", "error": "Connect a valid wallet first."}
    try:
        support_id = str(uuid.UUID(str(support_id)))
    except (ValueError, TypeError, AttributeError):
        return {"success": False, "code": "intent", "error": "Invalid support request."}
    if not TX_HASH_RE.fullmatch(str(tx_hash or "")):
        return {"success": False, "code": "tx_hash", "error": "A valid Celo transaction hash is required."}
    db = _admin_db()
    if db is None:
        return {"success": False, "code": "database", "error": "Support confirmation is temporarily unavailable."}
    try:
        result = (db.table("community_post_supports").select("*")
                  .eq("id", support_id).limit(1).execute())
        rows = getattr(result, "data", None) or []
        if not rows:
            return {"success": False, "code": "intent", "error": "Support request was not found."}
        row = rows[0]
    except Exception as exc:
        logger.warning("post support intent lookup failed: %s", exc)
        return {"success": False, "code": "database", "error": "Support confirmation is temporarily unavailable."}
    if str(row.get("supporter_wallet") or "").lower() != supporter_wallet.lower():
        return {"success": False, "code": "wallet_mismatch", "error": "This support request belongs to another wallet."}
    if row.get("status") == "confirmed":
        return {"success": True, "status": "confirmed", "already_confirmed": True,
                "bonus_gd": str(row.get("bonus_credited_gd") or 0), "system_post_id": row.get("system_post_id")}
    if row.get("status") not in {"prepared", "submitted", "settlement_pending"}:
        return {"success": False, "code": "intent_state", "error": "This support request can no longer be confirmed."}

    try:
        attached = _rpc(db, "community_post_support_attach_tx", {
            "p_support_id": support_id, "p_tx_hash": tx_hash.lower()
        })
    except Exception as exc:
        logger.warning("post support tx attach failed: %s", exc)
        return {"success": False, "code": "database", "error": "Could not record the transaction yet. Retry confirmation."}
    if attached.get("success") is not True:
        return {"success": False, "code": attached.get("code", "tx_conflict"), "error": attached.get("error", "Transaction could not be attached to this support request.")}

    verification = _verify_receipt(tx_hash, row)
    if verification.get("status") != "confirmed":
        return {"success": False, "status": verification.get("status"),
                "retryable": verification.get("retryable", False), "error": verification.get("error")}
    try:
        final = _rpc(db, "community_post_support_finalize", {
            "p_support_id": support_id,
            "p_tx_hash": tx_hash.lower(),
            "p_confirmed_block": verification["block_number"],
        })
    except Exception as exc:
        logger.error("post support finalization pending for %s: %s", support_id, exc)
        return {"success": False, "status": "settlement_pending", "retryable": True,
                "error": "Support is confirmed on Celo; reward and feed post are being finalized. Do not send again."}
    if final.get("success") is not True:
        return {"success": False, "status": "settlement_pending", "retryable": True,
                "error": final.get("error", "Support is confirmed on Celo; finalization will be retried. Do not send again.")}
    return {"success": True, "status": "confirmed", "already_confirmed": bool(final.get("already_confirmed")),
            "bonus_gd": str(final.get("bonus_gd", row.get("bonus_reserved_gd", "5"))),
            "system_post_id": final.get("system_post_id"), "tx_hash": tx_hash.lower()}
