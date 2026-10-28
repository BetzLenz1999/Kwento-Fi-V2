"""Celo G$ Community Post Support.

The browser submits transactions from the member's own wallet. This module
prepares exact-amount approve/support calldata, verifies the resulting
CommunitySupport contract event, then delegates atomic ledger/feed creation to
a service-role SQL RPC. It never signs transactions or holds G$.
"""
from __future__ import annotations

import logging
import os
import re
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)
CELO_CHAIN_ID = 42220
TOKEN_DECIMALS = 18
MINIMUM_GD = Decimal("20")
try:
    MAXIMUM_GD = Decimal(os.getenv("COMMUNITY_POST_SUPPORT_MAX_GD", "1000000"))
    if not MAXIMUM_GD.is_finite() or MAXIMUM_GD < MINIMUM_GD:
        raise InvalidOperation
except (InvalidOperation, ValueError):
    logger.warning("Invalid COMMUNITY_POST_SUPPORT_MAX_GD; using the safe default of 1000000 G$")
    MAXIMUM_GD = Decimal("1000000")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
GD_TOKEN_ADDRESS = os.getenv(
    "GOODDOLLAR_TOKEN_ADDRESS",
    os.getenv("CELO_GD_TOKEN_CONTRACT", "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A"),
)
CELO_RPC_URL = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CELO_EXPLORER_TX = "https://celoscan.io/tx/"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def contract_address() -> str:
    return (os.getenv("COMMUNITY_SUPPORT_CONTRACT_ADDRESS") or "").strip()


def is_available() -> bool:
    """The UI stays closed until an operator enables the feature and configures its deployed contract."""
    address = contract_address()
    return (
        _truthy(os.getenv("COMMUNITY_POST_SUPPORT_ENABLED", ""))
        and bool(_ADDRESS_RE.fullmatch(address))
        and address.lower() != ZERO_ADDRESS
        and bool(_ADDRESS_RE.fullmatch(GD_TOKEN_ADDRESS))
        and GD_TOKEN_ADDRESS.lower() != ZERO_ADDRESS
    )


def public_config() -> dict:
    if not is_available():
        return {"enabled": False}
    return {
        "enabled": True,
        "contract_address": contract_address(),
        "token_address": GD_TOKEN_ADDRESS,
        "chain_id": CELO_CHAIN_ID,
        "minimum_gd": str(MINIMUM_GD),
        "decimals": TOKEN_DECIMALS,
        "explorer_tx": CELO_EXPLORER_TX,
    }


def _get_admin_db():
    from supabase_client import get_supabase_admin_client
    db = get_supabase_admin_client()
    if not db:
        raise RuntimeError("Community support database is unavailable")
    return db


def _rows(result) -> list[dict]:
    data = getattr(result, "data", None)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _valid_wallet(value: str | None) -> bool:
    return bool(value and _ADDRESS_RE.fullmatch(str(value).strip())
                and str(value).strip().lower() != ZERO_ADDRESS)


def _parse_amount(raw) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < MINIMUM_GD or amount > MAXIMUM_GD:
        return None
    if amount.as_tuple().exponent < -TOKEN_DECIMALS:
        return None
    return amount


def _amount_wei(amount: Decimal) -> int:
    return int(amount * (Decimal(10) ** TOKEN_DECIMALS))


def _selector(signature: str) -> str:
    from web3 import Web3
    digest = Web3.keccak(text=signature)
    return "0x" + bytes(digest[:4]).hex()


def _word(value: int) -> str:
    if value < 0 or value >= 2**256:
        raise ValueError("ABI uint256 value is out of range")
    return value.to_bytes(32, "big").hex()


def _address_word(address: str) -> str:
    if not _valid_wallet(address):
        raise ValueError("Invalid wallet address")
    return address.lower()[2:].rjust(64, "0")


def _encode_call(signature: str, words: list[str]) -> str:
    return _selector(signature) + "".join(words)


def _fetch_post(post_id: int) -> dict | None:
    db = _get_admin_db()
    result = (
        db.table("community_feed_posts")
        .select("id,wallet_address,username,post_type,is_deleted,is_hidden")
        .eq("id", int(post_id))
        .limit(1)
        .execute()
    )
    rows = _rows(result)
    if not rows:
        return None
    post = rows[0]
    if post.get("is_deleted") or post.get("is_hidden") or post.get("post_type") != "user":
        return None
    if not _valid_wallet(post.get("wallet_address")):
        return None
    author_name = str(post.get("username") or "").strip().lstrip("@")
    if not _USERNAME_RE.fullmatch(author_name):
        return None
    return post


def validate_deployed_contract(*, expected_token: str | None = None,
                               expected_vault: str | None = None) -> bool:
    """Verify the configured address is deployed on Celo with expected immutables."""
    from web3 import Web3
    address = contract_address()
    if not _ADDRESS_RE.fullmatch(address) or address.lower() == ZERO_ADDRESS:
        raise RuntimeError("Community Support contract address is not configured")
    w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL, request_kwargs={"timeout": 12}))
    if int(w3.eth.chain_id) != CELO_CHAIN_ID:
        raise RuntimeError("Configured RPC is not Celo Mainnet")
    code = w3.eth.get_code(Web3.to_checksum_address(address))
    if not code:
        raise RuntimeError("Community Support contract is not deployed at the configured address")
    abi = [
        {"type": "function", "name": "goodDollarToken", "stateMutability": "view",
         "inputs": [], "outputs": [{"type": "address"}]},
        {"type": "function", "name": "communityVault", "stateMutability": "view",
         "inputs": [], "outputs": [{"type": "address"}]},
    ]
    contract = w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
    token = str(contract.functions.goodDollarToken().call()).lower()
    vault = str(contract.functions.communityVault().call()).lower()
    if expected_token and token != str(expected_token).lower():
        raise RuntimeError("Community Support contract uses a different G$ token")
    if expected_vault and vault != str(expected_vault).lower():
        raise RuntimeError("Community Support contract points to a different Community Vault")
    token_address = expected_token or GD_TOKEN_ADDRESS
    token_abi = [{"type": "function", "name": "decimals", "stateMutability": "view",
                 "inputs": [], "outputs": [{"type": "uint8"}]}]
    token_contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=token_abi)
    if int(token_contract.functions.decimals().call()) != TOKEN_DECIMALS:
        raise RuntimeError("Configured G$ token does not use the expected 18 decimals")
    return True


def prepare_support(sender_wallet: str, post_id, raw_amount) -> dict:
    if not is_available():
        return {"success": False, "error": "Post support is not available yet.", "code": "disabled"}
    if not _valid_wallet(sender_wallet):
        return {"success": False, "error": "Connect your GoodMarket wallet first.", "code": "wallet"}
    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return {"success": False, "error": "That post could not be found.", "code": "post"}
    if post_id <= 0:
        return {"success": False, "error": "That post could not be found.", "code": "post"}
    amount = _parse_amount(raw_amount)
    if amount is None:
        return {"success": False, "error": "Support must be at least 20 G$ and use no more than 18 decimal places.", "code": "amount"}
    try:
        validate_deployed_contract(expected_token=GD_TOKEN_ADDRESS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community post support: contract runtime validation failed: %s", exc)
        return {"success": False, "error": "The Community Support contract is not ready on Celo. No transaction was prepared.", "code": "contract"}
    try:
        post = _fetch_post(post_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("community post support: post lookup failed: %s", exc)
        return {"success": False, "error": "Could not load that post. Please try again.", "code": "database"}
    if not post:
        return {"success": False, "error": "That post is unavailable for support.", "code": "post"}
    author_wallet = str(post["wallet_address"]).lower()
    if author_wallet == sender_wallet.lower():
        return {"success": False, "error": "You cannot support your own post.", "code": "self"}
    amount_wei = _amount_wei(amount)
    contract = contract_address()
    try:
        approval_data = _encode_call(
            "approve(address,uint256)", [_address_word(contract), _word(amount_wei)]
        )
        support_data = _encode_call(
            "supportPost(uint256,address,uint256)",
            [_word(post_id), _address_word(author_wallet), _word(amount_wei)],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("community post support: calldata encoding failed: %s", exc)
        return {"success": False, "error": "Could not prepare the wallet transaction.", "code": "config"}
    return {
        "success": True,
        "post_id": post_id,
        "amount": format(amount.normalize(), "f"),
        "amount_base_units": str(amount_wei),
        "contract_address": contract,
        "token_address": GD_TOKEN_ADDRESS,
        "chain_id": CELO_CHAIN_ID,
        "approval_data": approval_data,
        "support_data": support_data,
        "author_username": post["username"],
        "explorer_tx": CELO_EXPLORER_TX,
    }


def _hex(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        value = "0x" + bytes(value).hex()
    text = str(value or "")
    return text if text.startswith("0x") else "0x" + text


def _topic_address(value) -> str:
    return "0x" + _hex(value)[-40:]


def _verify_support_tx(tx_hash: str, sender_wallet: str, post: dict, post_id: int) -> dict:
    if not _TX_RE.fullmatch(tx_hash or ""):
        return {"ok": False, "error": "Invalid Celo transaction hash."}
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL, request_kwargs={"timeout": 15}))
        if int(w3.eth.chain_id) != CELO_CHAIN_ID:
            return {"ok": False, "error": "The configured verification RPC is not Celo Mainnet."}
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if not receipt:
            return {"pending": True, "error": "Transaction is still confirming on Celo."}
    except Exception as exc:  # noqa: BLE001
        logger.info("community post support: receipt not available for %s: %s", tx_hash, exc)
        return {"pending": True, "error": "Transaction is still confirming on Celo. Keep this page open or retry verification."}
    status = int(receipt.get("status") or 0)
    if status != 1:
        return {"ok": False, "error": "The wallet transaction failed on Celo."}
    try:
        tx = w3.eth.get_transaction(tx_hash)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community post support: transaction lookup failed: %s", exc)
        return {"pending": True, "error": "Could not read the transaction yet. Please retry verification."}
    contract = contract_address().lower()
    sender = sender_wallet.lower()
    author = str(post.get("wallet_address") or "").lower()
    tx_to = (tx.get("to") or "").lower()
    tx_from = (tx.get("from") or "").lower()
    if tx_to != contract or tx_from != sender:
        return {"ok": False, "error": "This transaction was not sent from your wallet to the Community Support contract."}
    tx_input = _hex(tx.get("input") or tx.get("data") or "0x").lower()
    try:
        expected_selector = _selector("supportPost(uint256,address,uint256)").lower()
        if not tx_input.startswith(expected_selector) or len(tx_input) != 2 + 8 + 64 * 3:
            return {"ok": False, "error": "This is not a Community post-support transaction."}
        encoded = tx_input[10:]
        tx_post_id = int(encoded[0:64], 16)
        tx_author = "0x" + encoded[88:128]
        tx_amount = int(encoded[128:192], 16)
        if tx_post_id != post_id or tx_author.lower() != author:
            return {"ok": False, "error": "The transaction does not match this post and its author."}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community post support: call-data decode failed: %s", exc)
        return {"ok": False, "error": "Could not validate the support transaction."}

    try:
        topic0 = "0x" + bytes(Web3.keccak(text="PostSupported(uint256,address,address,uint256)")).hex()
        matches = []
        for log in receipt.get("logs") or []:
            if str(log.get("address") or "").lower() != contract:
                continue
            topics = log.get("topics") or []
            if len(topics) != 4 or _hex(topics[0]).lower() != topic0.lower():
                continue
            log_post_id = int(_hex(topics[1]), 16)
            log_sender = _topic_address(topics[2]).lower()
            log_author = _topic_address(topics[3]).lower()
            log_amount = int(_hex(log.get("data")), 16)
            if (log_post_id == post_id and log_sender == sender
                    and log_author == author and log_amount == tx_amount):
                matches.append(log_amount)
        if len(matches) != 1:
            return {"ok": False, "error": "No unique matching post-support event was found in this transaction."}
        transfer_topic = "0x" + bytes(Web3.keccak(text="Transfer(address,address,uint256)")).hex()
        transfers = []
        for log in receipt.get("logs") or []:
            if str(log.get("address") or "").lower() != GD_TOKEN_ADDRESS.lower():
                continue
            topics = log.get("topics") or []
            if len(topics) != 3 or _hex(topics[0]).lower() != transfer_topic.lower():
                continue
            if (_topic_address(topics[1]).lower() == sender
                    and _topic_address(topics[2]).lower() == author
                    and int(_hex(log.get("data")), 16) == matches[0]):
                transfers.append(matches[0])
        if len(transfers) != 1:
            return {"ok": False, "error": "No matching G$ transfer to the post author was found."}
        if matches[0] < int(MINIMUM_GD * (10**TOKEN_DECIMALS)):
            return {"ok": False, "error": "The verified support amount is below the 20 G$ minimum."}
        amount = Decimal(matches[0]) / (Decimal(10) ** TOKEN_DECIMALS)
        return {"ok": True, "amount": amount, "amount_text": format(amount.normalize(), "f")}
    except Exception as exc:  # noqa: BLE001
        logger.exception("community post support: event verification failed: %s", exc)
        return {"ok": False, "error": "Could not verify the Community Support event."}


def confirm_support(sender_wallet: str, post_id, tx_hash: str) -> dict:
    if not is_available():
        return {"success": False, "error": "Post support is not available yet.", "code": "disabled"}
    if not _valid_wallet(sender_wallet):
        return {"success": False, "error": "Connect your GoodMarket wallet first.", "code": "wallet"}
    try:
        post_id = int(post_id)
        if post_id <= 0:
            raise ValueError("invalid post")
        post = _fetch_post(post_id)
    except (TypeError, ValueError):
        return {"success": False, "error": "That post could not be found.", "code": "post"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("community post support: post lookup failed: %s", exc)
        return {"success": False, "error": "Could not load that post. Please retry confirmation.", "code": "database"}
    if not post:
        return {"success": False, "error": "That post is no longer available for support.", "code": "post"}
    author_wallet = str(post["wallet_address"]).lower()
    if author_wallet == sender_wallet.lower():
        return {"success": False, "error": "You cannot support your own post.", "code": "self"}

    if not _TX_RE.fullmatch(tx_hash or ""):
        return {"success": False, "error": "Enter a valid Celo transaction hash.", "code": "tx_hash"}
    try:
        db = _get_admin_db()
        prior = _rows(db.table("community_chat_tips").select(
            "id,context,sender_wallet,post_id,feed_post_id,tx_hash"
        ).eq("tx_hash", tx_hash.lower()).limit(1).execute())
        if prior:
            row = prior[0]
            if (row.get("context") == "post_support"
                    and str(row.get("sender_wallet", "")).lower() == sender_wallet.lower()
                    and int(row.get("post_id") or 0) == post_id
                    and row.get("feed_post_id")):
                return {
                    "success": True, "duplicate": True, "status": "confirmed",
                    "feed_post_id": row["feed_post_id"], "tx_hash": tx_hash.lower(),
                    "tx_url": CELO_EXPLORER_TX + tx_hash.lower(),
                }
            return {"success": False, "error": "This transaction hash has already been used for another activity.", "code": "replay"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("community post support: duplicate check failed: %s", exc)
        return {"success": False, "error": "Could not check support status. Please retry without sending another transaction.", "code": "database"}

    verified = _verify_support_tx(tx_hash, sender_wallet, post, post_id)
    if verified.get("pending"):
        return {"success": False, "status": "confirming", "error": verified["error"], "code": "pending"}
    if not verified.get("ok"):
        return {"success": False, "error": verified.get("error") or "Support transaction could not be verified.", "code": "verification"}

    try:
        from community_feed import service as svc
        sender_username = svc.get_username(sender_wallet) or "Member"
        sender_username = str(sender_username).strip().lstrip("@")
        if not _USERNAME_RE.fullmatch(sender_username):
            sender_username = "Member"
        result = db.rpc("community_post_support_finalize", {
            "p_sender_wallet": sender_wallet.lower(),
            "p_post_id": post_id,
            "p_author_wallet": author_wallet,
            "p_sender_username": sender_username,
            "p_author_username": str(post["username"]).strip().lstrip("@"),
            "p_amount_gd": str(verified["amount"]),
            "p_tx_hash": tx_hash.lower(),
        }).execute()
        data = getattr(result, "data", None)
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict) or not data.get("success"):
            raise RuntimeError("Database finalizer returned no confirmed support record")
        return {
            "success": True,
            "duplicate": bool(data.get("duplicate")),
            "status": "confirmed",
            "amount": verified["amount_text"],
            "feed_post_id": data.get("feed_post_id"),
            "tx_hash": tx_hash.lower(),
            "tx_url": CELO_EXPLORER_TX + tx_hash.lower(),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("community post support: atomic finalizer failed for %s: %s", tx_hash, exc)
        return {
            "success": False,
            "status": "submitted",
            "error": "Your on-chain support was sent, but the thank-you post could not be saved yet. Keep the transaction hash and retry confirmation; do not send again.",
            "code": "recording",
            "tx_hash": tx_hash.lower(),
        }
