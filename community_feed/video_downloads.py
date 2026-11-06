"""Community permanent-video download payment preparation and verification.

The service never signs transactions or holds G$. The user's wallet submits
listing/purchase transactions; this module verifies their Celo receipts before
marking a listing active or returning the public media URL. Public R2 URLs are
still directly accessible, so this is a voluntary-payment flow, not a secure
media paywall.
"""
from __future__ import annotations

import logging
import os
import re
from decimal import Decimal

logger = logging.getLogger(__name__)
CELO_CHAIN_ID = 42220
TOKEN_DECIMALS = 18
MINIMUM_GD = 1_000
MAXIMUM_GD = 10_000
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
PURCHASE_REF_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
EXPLORER_TX = "https://celoscan.io/tx/"
DEFAULT_GD_TOKEN_ADDRESS = "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A"
CELO_RPC_URL = os.getenv("CELO_RPC_URL", "https://forno.celo.org")


def _resolve_token_address() -> str:
    """Resolve the G$ token, treating a blank setting as "use the default".

    ``os.getenv("X", default)`` only falls back when ``X`` is *unset*. A Vercel
    variable created but left empty (or set to ``""``) is present, so it used to
    shadow the official address and silently disable every paid flow.
    """
    from env_utils import get_env_str

    return (get_env_str("GOODDOLLAR_TOKEN_ADDRESS")
            or get_env_str("CELO_GD_TOKEN_CONTRACT")
            or DEFAULT_GD_TOKEN_ADDRESS)


GD_TOKEN_ADDRESS = _resolve_token_address()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def contract_address() -> str:
    return (os.getenv("COMMUNITY_VIDEO_DOWNLOADS_CONTRACT_ADDRESS") or "").strip()


def unavailable_reason() -> str:
    """Which setting keeps paid downloads off, or "" when they are available.

    The codes are safe to publish: they name the missing setting, never its
    value, so a deployment can surface "enabled flag missing" instead of the
    old catch-all that read as a contract problem.
    """
    if not _truthy(os.getenv("COMMUNITY_VIDEO_DOWNLOADS_ENABLED", "")):
        return "disabled"
    contract = contract_address()
    if not ADDRESS_RE.fullmatch(contract) or contract.lower() == ZERO_ADDRESS:
        return "contract"
    if not ADDRESS_RE.fullmatch(GD_TOKEN_ADDRESS) or GD_TOKEN_ADDRESS.lower() == ZERO_ADDRESS:
        return "token"
    return ""


def is_available() -> bool:
    return unavailable_reason() == ""


def public_config() -> dict:
    reason = unavailable_reason()
    if reason:
        return {"enabled": False, "reason": reason}
    return {
        "enabled": True,
        "contract_address": contract_address(),
        "token_address": GD_TOKEN_ADDRESS,
        "chain_id": CELO_CHAIN_ID,
        "decimals": TOKEN_DECIMALS,
        "min_gd": MINIMUM_GD,
        "max_gd": MAXIMUM_GD,
        "explorer_tx": EXPLORER_TX,
        "source_is_public": True,
    }


def _amount_base_units(price) -> int | None:
    if isinstance(price, bool) or price is None:
        return None
    text = str(price).strip()
    if not re.fullmatch(r"\d+", text):
        return None
    amount = int(text)
    if not MINIMUM_GD <= amount <= MAXIMUM_GD:
        return None
    return amount * (10 ** TOKEN_DECIMALS)


def _get_db():
    from supabase_client import get_supabase_admin_client
    db = get_supabase_admin_client()
    if not db:
        raise RuntimeError("Community download database is unavailable")
    return db


def _rows(result) -> list[dict]:
    data = getattr(result, "data", None)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _fetch_post(post_id: int) -> tuple[dict | None, list[dict]]:
    db = _get_db()
    rows = _rows(db.table("community_feed_posts").select(
        "id,wallet_address,username,post_type,is_deleted,is_hidden,is_permanent,"
        "visibility,download_price_gd,download_listing_status"
    ).eq("id", int(post_id)).limit(1).execute())
    if not rows:
        return None, []
    post = rows[0]
    media_rows = _rows(db.table("community_feed_media").select(
        "id,public_url,media_type,mime,poster_url"
    ).eq("post_id", int(post_id)).order("position").execute())
    return post, media_rows


def _eligible(post: dict | None, media: list[dict]) -> tuple[bool, str]:
    if (not post or post.get("post_type") != "user"
            or post.get("visibility") != "public"
            or post.get("is_deleted") or post.get("is_hidden")):
        return False, "This post is not available for download."
    if not post.get("is_permanent"):
        return False, "Paid downloads are only available for permanent posts."
    if len(media) != 1 or media[0].get("media_type") != "video":
        return False, "Paid downloads require a post with exactly one video and no photos."
    if _amount_base_units(post.get("download_price_gd")) is None:
        return False, "This post does not have a valid download price."
    return True, ""


def _web3():
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL, request_kwargs={"timeout": 15}))
    if int(w3.eth.chain_id) != CELO_CHAIN_ID:
        raise RuntimeError("Configured RPC is not Celo Mainnet")
    address = contract_address()
    if not ADDRESS_RE.fullmatch(address) or address.lower() == ZERO_ADDRESS:
        raise RuntimeError("Download contract is not configured")
    contract_address_checksum = Web3.to_checksum_address(address)
    if not w3.eth.get_code(contract_address_checksum):
        raise RuntimeError("Download contract is not deployed")
    token_abi = [{"type": "function", "name": "gDollar", "stateMutability": "view",
                  "inputs": [], "outputs": [{"name": "", "type": "address"}]}]
    deployed_token = w3.eth.contract(address=contract_address_checksum, abi=token_abi).functions.gDollar().call()
    if str(deployed_token).lower() != GD_TOKEN_ADDRESS.lower():
        raise RuntimeError("Configured G$ token does not match the deployed download contract")
    return Web3, w3


def _selector(signature: str) -> str:
    from web3 import Web3
    return bytes(Web3.keccak(text=signature)[:4]).hex()


def _word(value: int) -> str:
    if value < 0 or value >= 2**256:
        raise ValueError("ABI integer is out of range")
    return value.to_bytes(32, "big").hex()


def _address_word(value: str) -> str:
    if not ADDRESS_RE.fullmatch(str(value or "")):
        raise ValueError("Invalid wallet address")
    return str(value)[2:].lower().rjust(64, "0")


def _encode(signature: str, words: list[str]) -> str:
    return "0x" + _selector(signature) + "".join(words)


def _lookup_media(post_id: int) -> tuple[dict | None, dict | None]:
    post, media = _fetch_post(post_id)
    ok, _error = _eligible(post, media)
    if not ok:
        return None, None
    return post, media[0]


def prepare_listing(author_wallet: str, post_id: int) -> dict:
    if not is_available():
        return {"success": False, "code": "disabled", "error": "Paid video downloads are not configured."}
    try:
        post, media = _lookup_media(post_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("video download listing lookup failed: %s", exc)
        return {"success": False, "code": "database", "error": "Could not load this post."}
    if not post:
        return {"success": False, "code": "ineligible", "error": "This post is not eligible for paid downloads."}
    if str(post.get("wallet_address") or "").lower() != str(author_wallet or "").lower():
        return {"success": False, "code": "owner", "error": "Only the author can register this listing."}
    price = _amount_base_units(post.get("download_price_gd"))
    if not price:
        return {"success": False, "code": "price", "error": "This post has no valid download price."}
    try:
        Web3, w3 = _web3()
        contract = Web3.to_checksum_address(contract_address())
        owner = Web3.to_checksum_address(author_wallet)
        # Confirm an existing on-chain listing before preparing a registration.
        abi = [{"type": "function", "name": "listings", "stateMutability": "view",
                "inputs": [{"name": "creator", "type": "address"}, {"name": "postId", "type": "uint256"}],
                "outputs": [{"name": "priceBaseUnits", "type": "uint256"},
                           {"name": "active", "type": "bool"}]}]
        existing = w3.eth.contract(address=contract, abi=abi).functions.listings(owner, int(post_id)).call()
        if existing[1]:
            if int(existing[0]) != price:
                return {"success": False, "code": "listing_conflict", "error": "This post ID already has a different on-chain listing."}
            _get_db().table("community_feed_posts").update({
                "download_listing_status": "active"
            }).eq("id", int(post_id)).eq("wallet_address", author_wallet.lower()).execute()
            return {"success": True, "already_registered": True, "post_id": int(post_id), "listing_status": "active"}
        data = _encode("registerListing(uint256,uint256)", [_word(int(post_id)), _word(price)])
        return {"success": True, "post_id": int(post_id), "price_gd": int(post["download_price_gd"]),
                "price_base_units": str(price), "contract_address": contract,
                "chain_id": CELO_CHAIN_ID, "data": data, "listing_status": "pending"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("video download listing preparation failed: %s", exc)
        return {"success": False, "code": "contract", "error": "The download contract is not ready on Celo."}


def _get_receipt(tx_hash: str):
    if not TX_RE.fullmatch(tx_hash or ""):
        return None, None, "Invalid transaction hash."
    try:
        Web3, w3 = _web3()
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        tx = w3.eth.get_transaction(tx_hash)
        return Web3, (w3, receipt, tx), ""
    except Exception as exc:  # noqa: BLE001
        logger.info("video download transaction is pending: %s", exc)
        return None, None, "Transaction is still confirming on Celo."


def _input_hex(tx) -> str:
    value = tx.get("input") or tx.get("data") or b""
    return ("0x" + bytes(value).hex()) if isinstance(value, (bytes, bytearray)) else str(value).lower()


def _find_event(receipt, signature: str, topic_checks: list[str], data_words: list[int]) -> bool:
    from web3 import Web3
    topic0 = "0x" + bytes(Web3.keccak(text=signature)).hex()
    for log in receipt.get("logs", []):
        if str(log.get("address") or "").lower() != contract_address().lower():
            continue
        topics = [("0x" + bytes(t).hex()) if isinstance(t, (bytes, bytearray)) else str(t).lower()
                  for t in (log.get("topics") or [])]
        if not topics or topics[0].lower() != topic0.lower():
            continue
        if len(topics) < len(topic_checks) + 1:
            continue
        if any(topics[i + 1].lower() != topic_checks[i].lower() for i in range(len(topic_checks))):
            continue
        data = log.get("data") or b""
        raw = bytes(data).hex() if isinstance(data, (bytes, bytearray)) else str(data).removeprefix("0x")
        values = [int(raw[i * 64:(i + 1) * 64], 16) for i in range(len(raw) // 64)]
        if values[:len(data_words)] == data_words:
            return True
    return False


def _topic_word(value: int) -> str:
    return "0x" + _word(value)


def confirm_listing(author_wallet: str, post_id: int, tx_hash: str) -> dict:
    if not is_available():
        return {"success": False, "code": "disabled", "error": "Paid video downloads are not configured."}
    try:
        post, media = _lookup_media(post_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("video download listing verify lookup failed: %s", exc)
        return {"success": False, "code": "database", "error": "Could not load this post."}
    if not post or str(post.get("wallet_address") or "").lower() != str(author_wallet or "").lower():
        return {"success": False, "code": "owner", "error": "Only the author can confirm this listing."}
    amount = _amount_base_units(post.get("download_price_gd"))
    Web3, data, pending_error = _get_receipt(tx_hash)
    if not data:
        return {"success": False, "status": "confirming", "code": "pending", "error": pending_error}
    w3, receipt, tx = data
    if int(receipt.get("status") or 0) != 1:
        return {"success": False, "code": "reverted", "error": "The listing transaction failed."}
    if str(tx.get("from") or "").lower() != str(author_wallet).lower() or str(tx.get("to") or "").lower() != contract_address().lower():
        return {"success": False, "code": "tx_mismatch", "error": "Transaction sender or contract does not match."}
    expected_data = _encode("registerListing(uint256,uint256)", [_word(int(post_id)), _word(amount)])
    if _input_hex(tx) != expected_data.lower():
        return {"success": False, "code": "tx_mismatch", "error": "This transaction does not register this post and price."}
    creator_topic = "0x" + _address_word(author_wallet)
    if not _find_event(receipt, "ListingRegistered(uint256,address,uint256)",
                       [_topic_word(int(post_id)), creator_topic], [amount]):
        return {"success": False, "code": "event_missing", "error": "The listing event could not be verified."}
    try:
        _get_db().table("community_feed_posts").update({
            "download_listing_status": "active", "download_listing_tx": tx_hash.lower()
        }).eq("id", int(post_id)).eq("wallet_address", author_wallet.lower()).execute()
    except Exception as exc:  # noqa: BLE001
        logger.exception("video download listing status update failed: %s", exc)
        return {"success": False, "code": "database", "error": "The listing is on-chain but its status could not be saved. Retry verification."}
    return {"success": True, "status": "active", "post_id": int(post_id), "tx_hash": tx_hash.lower()}


def prepare_purchase(buyer_wallet: str, post_id: int, purchase_ref: str) -> dict:
    if not is_available():
        return {"success": False, "code": "disabled", "error": "Paid video downloads are not configured."}
    if not PURCHASE_REF_RE.fullmatch(purchase_ref or "") or int(purchase_ref, 16) == 0:
        return {"success": False, "code": "purchase_ref", "error": "Invalid download purchase reference."}
    try:
        post, media = _lookup_media(post_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("video download purchase lookup failed: %s", exc)
        return {"success": False, "code": "database", "error": "Could not load this post."}
    if not post or post.get("download_listing_status") != "active":
        return {"success": False, "code": "listing", "error": "This post is not currently available for paid download."}
    if str(post.get("wallet_address") or "").lower() == str(buyer_wallet or "").lower():
        return {"success": False, "code": "self", "error": "You cannot buy your own download."}
    try:
        Web3, w3 = _web3()
        contract = Web3.to_checksum_address(contract_address())
        abi = [{"type": "function", "name": "listings", "stateMutability": "view",
                "inputs": [{"name": "creator", "type": "address"}, {"name": "postId", "type": "uint256"}],
                "outputs": [{"name": "priceBaseUnits", "type": "uint256"},
                           {"name": "active", "type": "bool"}]}]
        expected_creator = str(post["wallet_address"]).lower()
        listing = w3.eth.contract(address=contract, abi=abi).functions.listings(Web3.to_checksum_address(expected_creator), int(post_id)).call()
        expected_amount = _amount_base_units(post.get("download_price_gd"))
        if not listing[1] or int(listing[0]) != expected_amount:
            return {"success": False, "code": "listing_mismatch", "error": "The on-chain listing does not match the current post record."}
        data = _encode("purchaseDownload(uint256,address,bytes32)", [
            _word(int(post_id)), _address_word(expected_creator), purchase_ref[2:].lower()
        ])
        return {"success": True, "post_id": int(post_id),
                "price_gd": int(post["download_price_gd"]),
                "price_base_units": str(expected_amount),
                "author_username": str(post.get("username") or "member").lstrip("@"),
                "contract_address": contract, "token_address": GD_TOKEN_ADDRESS,
                "chain_id": CELO_CHAIN_ID, "purchase_ref": purchase_ref.lower(),
                "data": data, "source_is_public": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("video download purchase preparation failed: %s", exc)
        return {"success": False, "code": "contract", "error": "The download contract is not ready on Celo."}


def confirm_purchase(buyer_wallet: str, post_id: int, purchase_ref: str, tx_hash: str) -> dict:
    if not is_available():
        return {"success": False, "code": "disabled", "error": "Paid video downloads are not configured."}
    try:
        post, media = _fetch_post(post_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("video download purchase confirmation lookup failed: %s", exc)
        return {"success": False, "code": "database", "error": "Could not load this post."}
    eligible, error = _eligible(post, media)
    if not eligible or not post or post.get("download_listing_status") != "active":
        return {"success": False, "code": "listing", "error": error or "This listing is not active."}
    if not PURCHASE_REF_RE.fullmatch(purchase_ref or "") or int(purchase_ref, 16) == 0:
        return {"success": False, "code": "purchase_ref", "error": "Invalid purchase reference."}
    if str(post.get("wallet_address") or "").lower() == str(buyer_wallet or "").lower():
        return {"success": False, "code": "self", "error": "You cannot buy your own download."}
    Web3, data, pending_error = _get_receipt(tx_hash)
    if not data:
        return {"success": False, "status": "confirming", "code": "pending", "error": pending_error}
    w3, receipt, tx = data
    if int(receipt.get("status") or 0) != 1:
        return {"success": False, "code": "reverted", "error": "The payment transaction failed."}
    if str(tx.get("from") or "").lower() != str(buyer_wallet).lower() or str(tx.get("to") or "").lower() != contract_address().lower():
        return {"success": False, "code": "tx_mismatch", "error": "Transaction sender or contract does not match."}
    expected_data = _encode("purchaseDownload(uint256,address,bytes32)", [
        _word(int(post_id)), _address_word(str(post["wallet_address"])), purchase_ref[2:].lower()
    ])
    if _input_hex(tx) != expected_data.lower():
        return {"success": False, "code": "tx_mismatch", "error": "This transaction does not match the requested post and purchase reference."}
    buyer_topic = "0x" + _address_word(buyer_wallet)
    creator_topic = "0x" + _address_word(str(post["wallet_address"]))
    price = _amount_base_units(post.get("download_price_gd"))
    if not _find_event(receipt,
                       "DownloadPurchased(uint256,address,address,uint256,bytes32)",
                       [_topic_word(int(post_id)), buyer_topic, creator_topic],
                       [price, int(purchase_ref, 16)]):
        return {"success": False, "code": "event_missing", "error": "The exact payment event could not be verified."}
    db = _get_db()
    try:
        db.table("community_feed_download_purchases").insert({
            "post_id": int(post_id), "buyer_wallet": buyer_wallet.lower(),
            "author_wallet": str(post["wallet_address"]).lower(),
            "amount_base_units": str(price), "purchase_ref": purchase_ref.lower(),
            "tx_hash": tx_hash.lower(), "status": "confirmed",
        }).execute()
    except Exception as exc:  # noqa: BLE001
        # Unique-index conflicts mean a replay/previous confirmation. Do not
        # grant a new entitlement through a second tx hash or API replay.
        logger.info("video download purchase insert rejected/replayed: %s", exc)
        existing = _rows(db.table("community_feed_download_purchases").select(
            "post_id,buyer_wallet,tx_hash,status"
        ).eq("purchase_ref", purchase_ref.lower()).limit(1).execute())
        if not existing or int(existing[0].get("post_id") or 0) != int(post_id) \
                or str(existing[0].get("buyer_wallet") or "").lower() != str(buyer_wallet).lower() \
                or str(existing[0].get("tx_hash") or "").lower() != tx_hash.lower():
            return {"success": False, "code": "duplicate", "error": "This purchase reference has already been used."}
    return {"success": True, "status": "confirmed", "post_id": int(post_id),
            "price_gd": int(post["download_price_gd"]), "tx_hash": tx_hash.lower(),
            "tx_url": EXPLORER_TX + tx_hash.lower(), "download_url": media[0].get("public_url"),
            "source_is_public": True}
