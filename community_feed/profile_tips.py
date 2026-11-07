"""Stateless, non-custodial Community Profile transfers on Celo.

The browser names a recipient by username. This module resolves the username
server-side, prepares an unsigned transaction, and verifies the signed hash.
The blockchain receipt is the source of truth; no transfer ledger is written.
"""
from __future__ import annotations
import logging, os, re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from env_utils import get_env_decimal
from . import service as svc
logger = logging.getLogger(__name__)
CELO_CHAIN_ID = int(os.getenv("CELO_MAINNET_CHAIN_ID", "42220"))
CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CELO_RPC_FALLBACKS = [u.strip() for u in os.getenv(
    "CELO_RPC_FALLBACKS",
    "https://forno.celo.org,https://rpc.ankr.com/celo,https://celo.drpc.org",
).split(",") if u.strip()]
MAX_AMOUNT = get_env_decimal("COMMUNITY_PROFILE_TIP_MAX_AMOUNT", "1000000")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TRANSFER_SELECTOR = "a9059cbb"
TOKENS = {
    "GD": {"label":"G$", "decimals":18, "native":False, "address":GOODDOLLAR_CONTRACT_ADDRESS},
    "CUSD": {"label":"cUSD", "decimals":18, "native":False, "address":os.getenv("CUSD_CONTRACT", "0x765DE816845861e75A25fCA122bb6898B8B1282a")},
    "USDT": {"label":"USDT", "decimals":6, "native":False, "address":os.getenv("USDT_CONTRACT", "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e")},
    "CELO": {"label":"CELO", "decimals":18, "native":True, "address":""},
}
_ALIASES = {"G$":"GD", "G":"GD", "GD":"GD", "CUSD":"CUSD", "C$":"CUSD", "USDT":"USDT", "CELO":"CELO"}
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")

def normalize_token(raw):
    return _ALIASES.get(str(raw or "").strip().upper())

def parse_amount(raw):
    try:
        amount = Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0 or amount > MAX_AMOUNT:
        return None
    return amount

def _wei(amount, decimals):
    return int((amount * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_HALF_UP))

def _encode_transfer(recipient, amount):
    address = str(recipient).lower().removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{40}", address):
        raise ValueError("Invalid recipient address")
    return "0x" + TRANSFER_SELECTOR + address.rjust(64, "0") + format(amount, "x").rjust(64, "0")

def _username_wallet(username):
    name = (username or "").strip().lstrip("@")
    if not _NAME_RE.fullmatch(name):
        return None, name
    return svc.wallet_for_username(name), name

def prepare(sender_wallet, username, token, amount):
    token_key = normalize_token(token)
    parsed = parse_amount(amount)
    if not token_key or parsed is None:
        return {"success":False, "error":"Choose a supported token and enter a valid amount.", "code":"input"}
    recipient, name = _username_wallet(username)
    if not recipient:
        return {"success":False, "error":f"@{name} was not found in GoodMarket.", "code":"recipient"}
    if recipient.lower() == (sender_wallet or "").lower():
        return {"success":False, "error":"You cannot send money to yourself.", "code":"self"}
    meta = TOKENS[token_key]
    raw = _wei(parsed, meta["decimals"])
    if meta["native"]:
        to, data, value = recipient, "0x", hex(raw)
    else:
        to, data, value = meta["address"], _encode_transfer(recipient, raw), "0x0"
    return {"success":True, "token":token_key, "token_label":meta["label"], "network":"celo", "chain_id":CELO_CHAIN_ID,
            "decimals":meta["decimals"], "amount":format(parsed.normalize(), "f"), "recipient_username":name,
            "to":to, "data":data, "value":value, "explorer":"https://celoscan.io/tx/",
            "minipay_fee_currencies":{"cusd":"0x765DE816845861e75A25fCA122bb6898B8B1282a",
                                      "usdt_adapter":"0x0E2A3e05bc9A16F5292A6170456A710cb89C6f72",
                                      "usdc_adapter":"0x2F25deB3848C207fc8E0c34035B3Ba7fC157602B"}}

def _hex(value):
    raw = value.hex() if hasattr(value, "hex") else str(value)
    return raw if raw.startswith("0x") else "0x" + raw

def _celo_rpc_urls():
    """Return the configured primary followed by unique Celo fallbacks."""
    urls = [CELO_RPC]
    for url in CELO_RPC_FALLBACKS:
        if url not in urls:
            urls.append(url)
    return urls

def verify(tx_hash, token, sender, recipient, amount):
    meta = TOKENS.get(token); parsed = parse_amount(amount)
    if not meta or parsed is None: return {"ok":False, "error":"Invalid transfer details."}
    expected = _wei(parsed, meta["decimals"])
    from web3 import Web3
    saw_pending = False
    last_error = None
    sender, recipient = sender.lower(), recipient.lower()
    for rpc_url in _celo_rpc_urls():
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout":12}))
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if not receipt:
                saw_pending = True
                continue
            if receipt.get("status") != 1:
                return {"ok":False, "status":"reverted", "error":"The Celo transaction was reverted. No successful transfer was detected."}
            if meta["native"]:
                tx = w3.eth.get_transaction(tx_hash)
                if (tx.get("from") or "").lower() != sender or (tx.get("to") or "").lower() != recipient:
                    return {"ok":False, "status":"mismatch", "error":"The transaction sender or recipient does not match this profile."}
                if int(tx.get("value") or 0) != expected:
                    return {"ok":False, "status":"mismatch", "error":"The confirmed amount does not match."}
                return {"ok":True, "status":"confirmed"}
            received = 0
            for log in receipt.get("logs") or []:
                if (log.get("address") or "").lower() != meta["address"].lower(): continue
                topics = log.get("topics") or []
                if len(topics) < 3 or _hex(topics[0]).lower() != TRANSFER_TOPIC: continue
                frm, to = "0x" + _hex(topics[1])[-40:], "0x" + _hex(topics[2])[-40:]
                if frm.lower() == sender and to.lower() == recipient:
                    received += int(_hex(log.get("data") or "0x0"), 16)
            if received != expected:
                return {"ok":False, "status":"mismatch", "error":"No exact matching token transfer was confirmed."}
            return {"ok":True, "status":"confirmed"}
        except Exception as exc:
            last_error = exc
            logger.warning("profile tip verification failed on %s: %s", rpc_url, exc)
    if saw_pending:
        return {"ok":False, "status":"pending", "retryable":True,
                "error":"Transaction submitted. Waiting for Celo confirmation."}
    logger.warning("profile tip verification unavailable after %d RPCs: %s", len(_celo_rpc_urls()), last_error)
    return {"ok":False, "status":"rpc_unavailable", "retryable":True,
            "error":"Celo confirmation is temporarily unavailable. We will keep checking."}

def record(sender_wallet, username, token, amount, tx_hash):
    """Verify a transfer and return its receipt details without writing state."""
    prepared = prepare(sender_wallet, username, token, amount)
    if not prepared.get("success"): return prepared
    if not tx_hash or not str(tx_hash).startswith("0x"):
        return {"success":False, "error":"A transaction hash is required.", "code":"tx_hash"}
    recipient, name = _username_wallet(username)
    checked = verify(tx_hash, prepared["token"], sender_wallet, recipient, prepared["amount"])
    if not checked.get("ok"):
        status = checked.get("status") or "verify_failed"
        return {"success":False, "status":status, "retryable":bool(checked.get("retryable")),
                "error":checked.get("error"), "code":status}
    return {"success":True, "verified":True, "tip":{"token":prepared["token"], "token_label":prepared["token_label"],
            "amount":prepared["amount"], "recipient_username":name, "tx_hash":tx_hash,
            "explorer_url":"https://celoscan.io/tx/"+tx_hash}}
