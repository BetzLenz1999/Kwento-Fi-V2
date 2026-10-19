"""Community Chatroom — on-chain message layer.

Messages live in the ``GoodMarketChat`` contract as EVENTS (see
``contracts/GoodMarketChat.sol``). This module is the READ side plus the small
amount of encoding the backend needs to hand the browser an unsigned
transaction. It never signs and never holds a key.

What moved on-chain (Phase 1 + 2)
---------------------------------
- text messages      -> ``Posted`` events, ``kind = KIND_TEXT``
- image messages     -> ``Posted`` events, ``kind = KIND_IMAGE`` (body is the
                        ImgBB URL; the bytes still live on ImgBB)
- tip announcements  -> ``Posted`` events, ``kind = KIND_TIP``
- bans               -> the contract's ``banned`` mapping
- replies            -> the ``ref`` field of the event (parent message id)

What deliberately STAYS in the database
---------------------------------------
- the feature flag (``maintenance_settings``)
- report records (``community_chat_reports``) — admin-side workflow, and a
  report needs a message id, not a wallet
- usernames (``user_data``) — a username can change, and the event stores only
  the author address

Design rules (mirroring the rest of this codebase):
- Reads go over PUBLIC RPCs with failover, never through a wallet provider, so
  rendering a room never prompts anyone to sign.
- Every function returns a plain dict and never raises; a chain read failure
  degrades to an empty page rather than a 500.
- Message ids are the contract's ``id`` (0-based, monotonic). ``after_id`` is a
  cursor over that same space, so polling stays O(new messages).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
CHAT_CONTRACT = (os.getenv("CHAT_CONTRACT") or "").strip()
CHAT_CHAIN_ID = int(os.getenv("CHAT_CHAIN_ID", "42220"))

# Where the scan starts. Without this the scan would walk from genesis, which is
# hundreds of thousands of blocks and would time out. Set it at deploy time from
# the receipt block (the deploy script prints it).
CHAT_DEPLOYMENT_BLOCK = int(os.getenv("CHAT_DEPLOYMENT_BLOCK", "0"))

# Chunk size for eth_getLogs. forno rejects ranges above ~5000 blocks, so the
# scan must chunk below that; the adaptive retry halves further when a provider
# still refuses.
GETLOGS_MAX_CHUNK = int(os.getenv("CHATROOM_GETLOGS_MAX_CHUNK", "4000"))
GETLOGS_MIN_CHUNK = int(os.getenv("CHATROOM_GETLOGS_MIN_CHUNK", "500"))

# Hard cap on blocks scanned in one request when no deployment block is set, so
# a misconfiguration cannot turn a page load into a chain walk.
MAX_LOOKBACK_BLOCKS = int(os.getenv("CHATROOM_LOOKBACK_BLOCKS", "120000"))

# Result cache. A warm poll is a cheap no-op; the TTL is short so a just-posted
# message appears promptly.
CACHE_TTL_SECONDS = float(os.getenv("CHATROOM_CACHE_TTL", "3"))

# Event + ABI constants. Computed from the signature at import when eth_utils is
# available, and pinned as the verified literals otherwise -- so a signature
# change can never silently leave a stale topic behind (that mistake is exactly
# how an earlier revision of this file shipped a wrong selector).
#
#   post(uint8,uint256,bytes)                       -> 0x1c9d6ab3
#   Posted(uint256,address,uint8,uint256,uint64,bytes) -> 0x7af93838...
POST_SIGNATURE = "post(uint8,uint256,bytes)"
POSTED_SIGNATURE = "Posted(uint256,address,uint8,uint256,uint64,bytes)"
POST_SELECTOR_FALLBACK = "1c9d6ab3"
POSTED_TOPIC_FALLBACK = "0x7af93838f39374fd6a45698488a62da2b916ef9138f556365ed1bf61e5a3af35"
BANNED_SELECTOR = "9c8d83bb"          # banned(address)
IS_MODERATOR_SELECTOR = "fa6f3936"    # isModerator(address)
MESSAGE_COUNT_SELECTOR = "3dbcc8d1"   # messageCount()


def _selector(signature: str, fallback: str) -> str:
    try:
        from eth_utils import keccak

        return keccak(text=signature)[:4].hex()
    except Exception:  # noqa: BLE001
        return fallback


def _topic(signature: str, fallback: str) -> str:
    try:
        from eth_utils import keccak

        return "0x" + keccak(text=signature).hex()
    except Exception:  # noqa: BLE001
        return fallback


POST_SELECTOR = _selector(POST_SIGNATURE, POST_SELECTOR_FALLBACK)
POSTED_TOPIC = _topic(POSTED_SIGNATURE, POSTED_TOPIC_FALLBACK)

KIND_TEXT = 0
KIND_IMAGE = 1
KIND_TIP = 2
_KIND_NAMES = {KIND_TEXT: "text", KIND_IMAGE: "image", KIND_TIP: "tip"}

# Mirrors the contract's MAX_LEN. The backend validates in BYTES (the contract
# counts bytes) while the DB path counted characters, so the two limits are kept
# as separate constants rather than one shared number.
MAX_BODY_BYTES = int(os.getenv("CHATROOM_MAX_MESSAGE_BYTES", "500"))

# Explorer base for the chain the room lives on.
_EXPLORERS = {
    42220: "https://celoscan.io/tx/",
    50: "https://xdcscan.com/tx/",
}


def explorer_url(tx_hash: str | None) -> str | None:
    """Block-explorer link for a post, so the UI can prove it is on-chain."""
    if not tx_hash or not str(tx_hash).startswith("0x"):
        return None
    base = _EXPLORERS.get(CHAT_CHAIN_ID)
    return f"{base}{tx_hash}" if base else None



# Default public Celo RPCs (same list the rest of the app falls back to).
_DEFAULT_RPC_URLS = [
    "https://forno.celo.org",
    "https://rpc.ankr.com/celo",
    "https://celo.drpc.org",
]

_cache: dict[str, tuple[float, object]] = {}


def rpc_urls() -> list[str]:
    """Primary then fallbacks, de-duplicated, primary first."""
    primary = (os.getenv("CELO_RPC_URL") or "").strip()
    raw = (os.getenv("CELO_RPC_FALLBACKS") or "").strip()
    urls: list[str] = []
    for u in [primary] + [x.strip() for x in raw.split(",") if x.strip()] + _DEFAULT_RPC_URLS:
        if u and u not in urls:
            urls.append(u)
    return urls


def is_configured() -> bool:
    """Whether the on-chain room can be read at all."""
    return bool(CHAT_CONTRACT) and CHAT_CONTRACT.startswith("0x") and len(CHAT_CONTRACT) == 42


def _cache_get(key: str):
    hit = _cache.get(key)
    if not hit:
        return None
    stamp, value = hit
    if time.time() - stamp > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value):
    _cache[key] = (time.time(), value)


def invalidate_cache():
    """Drop the memoised reads. Called after a confirmed post so the author's
    own next poll sees their message instead of a stale page."""
    _cache.clear()


# ── JSON-RPC ─────────────────────────────────────────────────────────────────

def _rpc(method: str, params: list, timeout: float = 10.0):
    """One JSON-RPC call with failover across the configured RPCs.

    Returns ``(result, error)``. Never raises.
    """
    import json
    import urllib.request

    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_error = "no_rpc_available"
    for url in rpc_urls():
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode())
            if "result" in body:
                return body["result"], None
            # A structured JSON-RPC error means the node is alive; the call
            # itself failed. Surface it rather than pretending the node is down.
            err = body.get("error") or {}
            last_error = str(err.get("message") or err)[:200]
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)[:200]
            continue
    return None, last_error


def _hex_to_int(value) -> int:
    """Decode a 32-byte word to int.

    Accepts the hex string a JSON-RPC node returns AND the ``HexBytes`` a
    web3-based caller passes. ``int(str(HexBytes))`` raises (it renders as
    ``b'...'``), which would silently collapse every id to 0.
    """
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(bytes(value), "big") if value else 0
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return 0


def _to_hex_str(value) -> str:
    """Normalize a log field to a ``0x``-prefixed hex string.

    Raw JSON-RPC (what this module uses in production) returns hex strings, but
    web3-based callers and tests hand back ``HexBytes``. Accept both so the
    decoder is correct regardless of who fetched the log.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    text = str(value)
    return text if text.startswith("0x") else "0x" + text


def _hex_to_bytes(value) -> bytes:
    text = _to_hex_str(value)[2:]
    if len(text) % 2:
        text = "0" + text
    try:
        return bytes.fromhex(text)
    except ValueError:
        return b""


def _topic_to_address(topic) -> str:
    """Last 20 bytes of a 32-byte topic, lowercase."""
    raw = _to_hex_str(topic)[2:]
    return "0x" + raw[-40:].lower() if len(raw) >= 40 else ""


# ── Block range ──────────────────────────────────────────────────────────────

def _latest_block() -> int:
    cached = _cache_get("latest_block")
    if cached is not None:
        return cached
    result, _ = _rpc("eth_blockNumber", [])
    block = _hex_to_int(result)
    if block:
        _cache_set("latest_block", block)
    return block


def _from_block(latest: int) -> int:
    """Where to start scanning. Prefer the recorded deployment block; otherwise
    bound the lookback so a missing env var cannot walk the whole chain."""
    if CHAT_DEPLOYMENT_BLOCK > 0:
        return CHAT_DEPLOYMENT_BLOCK
    return max(0, latest - MAX_LOOKBACK_BLOCKS)


def _fetch_logs(from_block: int, to_block: int) -> list:
    """All ``Posted`` logs in [from, to], chunked below the provider limit with
    an adaptive halve-and-retry so a tightened limit cannot silently zero the
    room.

    The retry halves all the way down to a SINGLE block before giving up. A
    fixed floor above the provider's real limit is the exact defect the
    paluwagan scan shipped: the first chunk was already too large and the
    "adaptive" retry could never shrink below its floor, so every scan raised,
    every caller swallowed it, and the room looked empty. Halving is bounded
    (log2 of the chunk size, ~12 rounds) and only costs anything on a provider
    that is actually restrictive.
    """
    logs: list = []
    start = from_block
    chunk = max(1, GETLOGS_MAX_CHUNK)
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        params = [{
            "address": CHAT_CONTRACT,
            "fromBlock": hex(start),
            "toBlock": hex(end),
            "topics": [POSTED_TOPIC],
        }]
        result, error = _rpc("eth_getLogs", params)
        if result is None:
            # Provider refused the range — halve and retry the SAME window.
            if chunk > 1:
                chunk = max(1, chunk // 2)
                continue
            logger.warning("chatroom: eth_getLogs failed at %s-%s: %s", start, end, error)
            break
        logs.extend(result)
        start = end + 1
    return logs


# ── Decoding ─────────────────────────────────────────────────────────────────

def decode_posted_log(log: dict) -> dict | None:
    """Turn one raw ``Posted`` log into the app's message shape.

    The event is ``Posted(uint256 id, address author, uint8 kind, uint256 ref,
    uint64 ts, bytes body)``. ``id`` and ``author`` are indexed (topics 1/2);
    ``kind``, ``ref``, ``ts`` and ``body`` are ABI-encoded in ``data``.

    Decoding is done by slicing 32-byte words rather than by ABI-decoding, so
    this module imports and tests without web3/eth_abi (the same ABI-free
    approach the tip verifier uses).
    """
    try:
        topics = log.get("topics") or []
        if len(topics) < 3:
            return None
        if _to_hex_str(topics[0]).lower() != POSTED_TOPIC.lower():
            return None

        msg_id = _hex_to_int(topics[1])
        author = _topic_to_address(topics[2])

        data = _hex_to_bytes(log.get("data"))
        if len(data) < 128:
            return None
        # data word 0 = kind, 1 = ref, 2 = ts, 3 = offset to body, then body
        kind = int.from_bytes(data[0:32], "big")
        ref = int.from_bytes(data[32:64], "big")
        ts = int.from_bytes(data[64:96], "big")
        body_offset = int.from_bytes(data[96:128], "big")

        body = b""
        if 0 < body_offset and body_offset + 32 <= len(data):
            length = int.from_bytes(data[body_offset:body_offset + 32], "big")
            start = body_offset + 32
            end = start + length
            if end <= len(data):
                body = data[start:end]

        created_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
        return {
            "id": msg_id,
            "wallet_address": author,
            "author": author,
            "kind": kind,
            "message_type": _KIND_NAMES.get(kind, "text"),
            "ref": ref or None,
            "reply_to_id": ref or None,
            "body": body.decode("utf-8", errors="replace"),
            "created_at": created_at,
            "block_number": _hex_to_int(log.get("blockNumber")),
            "tx_hash": log.get("transactionHash"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("chatroom: could not decode a Posted log: %s", exc)
        return None


def fetch_messages(after_id: int | None = None, limit: int = 50,
                   max_scan_blocks: int | None = None) -> dict:
    """On-chain messages in chronological order.

    ``after_id`` returns only ids greater than it (the polling cursor). Without
    it the newest ``limit`` are returned. ``success`` is False when the chain
    could not be read at all, so the caller can tell "empty room" from "RPC is
    down" instead of silently showing nothing.
    """
    if not is_configured():
        return {"success": False, "error": "not_configured", "messages": [], "latest_id": after_id or 0}

    latest = _latest_block()
    if not latest:
        return {"success": False, "error": "rpc_unavailable", "messages": [], "latest_id": after_id or 0}

    from_block = _from_block(latest)
    logs = _fetch_logs(from_block, latest)
    decoded = [m for m in (decode_posted_log(l) for l in logs) if m]

    # De-duplicate by id (a re-org or an overlapping chunk can repeat a log),
    # then order. The contract guarantees monotonic ids.
    by_id: dict[int, dict] = {}
    for m in decoded:
        by_id[m["id"]] = m
    ordered = [by_id[k] for k in sorted(by_id)]

    if after_id is not None:
        ordered = [m for m in ordered if m["id"] > int(after_id)]
        window = ordered[:limit]
    else:
        window = ordered[-limit:]

    latest_id = ordered[-1]["id"] if ordered else (after_id or 0)
    return {
        "success": True,
        "messages": window,
        "latest_id": latest_id,
        "has_more": len(ordered) > len(window),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# ── Contract views (bans / moderators) ───────────────────────────────────────

def _call_address_bool(selector: str, address: str) -> bool | None:
    """Read a ``function(address) view returns (bool)`` slot.

    Returns None when the chain could not be read, so callers can fail OPEN
    (a moderation read outage must not lock every user out of the room).
    """
    if not is_configured() or not address:
        return None
    clean = address.lower().replace("0x", "")
    if len(clean) != 40:
        return None
    data = "0x" + selector + clean.rjust(64, "0")
    result, error = _rpc("eth_call", [{"to": CHAT_CONTRACT, "data": data}, "latest"])
    if result is None:
        logger.warning("chatroom: eth_call %s failed: %s", selector, error)
        return None
    return _hex_to_int(result) != 0


def is_banned_onchain(wallet: str | None) -> bool | None:
    """Contract ban list. None = could not read."""
    if not wallet:
        return False
    cached = _cache_get(f"banned:{wallet.lower()}")
    if cached is not None:
        return cached
    value = _call_address_bool(BANNED_SELECTOR, wallet)
    if value is not None:
        _cache_set(f"banned:{wallet.lower()}", value)
    return value


def is_moderator(wallet: str | None) -> bool | None:
    """Contract moderator set. None = could not read."""
    if not wallet:
        return False
    cached = _cache_get(f"mod:{wallet.lower()}")
    if cached is not None:
        return cached
    value = _call_address_bool(IS_MODERATOR_SELECTOR, wallet)
    if value is not None:
        _cache_set(f"mod:{wallet.lower()}", value)
    return value


def message_count() -> int | None:
    result, _ = _rpc("eth_call", [{"to": CHAT_CONTRACT, "data": "0x" + MESSAGE_COUNT_SELECTOR}, "latest"])
    if result is None:
        return None
    return _hex_to_int(result)



# ── Network fee (CIP-64 fee currencies) ────────────────────────
#
# Posting is an on-chain transaction, so the POSTER pays the network fee. The
# fee is always PRICED in CELO — that is the chain's unit of account — but
# Celo's CIP-64 lets the sender settle it in an allowlisted ERC-20 instead.
#
# CELO stays the PRIMARY displayed value everywhere: the notice shows the CELO
# cost (and its USD equivalent) no matter which token actually settles it,
# because that is the one number comparable across wallets and networks. The
# chosen token is a settlement detail, never the headline. Users otherwise ask
# "why does posting need a signature?" — the notice answers that up front.
#
# USDT/USDC have 6 decimals, so Celo requires their ADAPTER address as
# `feeCurrency` (the adapter normalises to 18 decimals). cUSD is 18 decimals and
# is allowlisted directly. Native CELO means "omit the feeCurrency field".
#
# Wallet support matters: a wallet that does not implement CIP-64 silently
# drops the field and pays in CELO instead, so a stablecoin fee is best-effort
# and the flow must always still work with native CELO.

# Gas accounting for the fee notice. THREE different numbers, do not conflate:
#
#   * POST_GAS_LIMIT  — the tx-level LIMIT (insurance). Unused gas is refunded,
#     so it is NEVER the cost. Mirrors the frontend POST_GAS_FALLBACK.
#   * POST_GAS_USED   — what a post actually consumes. The contract's measured
#     worst case is 71,728 gas (cold: a new wallet pays a cold SSTORE for
#     lastPostAt); a warm post is ~49k. We quote the COLD worst case because a
#     new user's first post genuinely is cold, so the notice never understates.
#   * FEE_CURRENCY_INTRINSIC_GAS — Celo's CIP-64 surcharge, charged on top of
#     the 21,000 standard intrinsic whenever feeCurrency is set. Verified live:
#     eth_estimateGas returns 71,865 for a fee-currency transfer vs 21,000 for
#     the same native transfer (71,865 - 21,000 = 50,865 ~= 50,000).
#
# Quoting the limit instead of the used gas overstates the cost ~2x
# (150,000 vs ~72,000), which is what users compared against their wallet.
POST_GAS_LIMIT = int(os.getenv("CHATROOM_POST_GAS_LIMIT", "150000"))
POST_GAS_USED = int(os.getenv("CHATROOM_POST_GAS_USED", "72000"))
FEE_CURRENCY_INTRINSIC_GAS = int(os.getenv("CHATROOM_FEE_CURRENCY_INTRINSIC_GAS", "50000"))

# Back-compat alias for the value the estimate bills by default.
DEFAULT_POST_GAS_UNITS = POST_GAS_USED

FEE_CURRENCY_CELO = "CELO"

FEE_CURRENCIES = {
    "CELO": {
        "key": "CELO",
        "label": "CELO",
        "native": True,
        "decimals": 18,
        # Native CELO: no feeCurrency field on the transaction.
        "address": "",
        "coin_id": "celo",
        "note": "Celo's native token — works with every wallet.",
    },
    "CUSD": {
        "key": "CUSD",
        "label": "cUSD",
        "native": False,
        "decimals": 18,
        "address": os.getenv("CELO_CUSD_TOKEN", "0x765DE816845861e75A25fCA122bb6898B8B1282a"),
        "coin_id": "celo-dollar",
        "note": "18 decimals, so the token address is the fee currency.",
    },
    "USDT": {
        "key": "USDT",
        "label": "USDT",
        "native": False,
        "decimals": 6,
        "address": os.getenv("CELO_USDT_ADAPTER", "0x0E2A3e05bc9A16F5292A6170456A710cb89C6f72"),
        "coin_id": "tether",
        "note": "6 decimals — Celo requires the adapter address.",
    },
    "USDC": {
        "key": "USDC",
        "label": "USDC",
        "native": False,
        "decimals": 6,
        "address": os.getenv("CELO_USDC_ADAPTER", "0x2F25deB3848C207fc8E0c34035B3Ba7fC157602B"),
        "coin_id": "usd-coin",
        "note": "6 decimals — Celo requires the adapter address.",
    },
}

# Fee currencies are only meaningful on Celo. XDC has no CIP-64 equivalent, so
# an XDC-configured room advertises native XDC alone.
FEE_CURRENCY_SUPPORTED = CHAT_CHAIN_ID == 42220

_CELO_USD_CACHE: dict[str, float] = {"price": 0.0, "expires_at": 0.0}
_CELO_USD_TTL = 300.0


def resolve_fee_currency(key: str | None) -> dict | None:
    """Look up a fee currency by key (case-insensitive). None when unknown."""
    if not key:
        return FEE_CURRENCIES[FEE_CURRENCY_CELO]
    return FEE_CURRENCIES.get(str(key).strip().upper())


def fee_currency_list() -> list:
    """The fee currencies the UI may offer, CELO first (the default)."""
    if not FEE_CURRENCY_SUPPORTED:
        return [dict(FEE_CURRENCIES[FEE_CURRENCY_CELO], supported=False)]
    return [dict(FEE_CURRENCIES[k], supported=True)
            for k in (FEE_CURRENCY_CELO, "CUSD", "USDT", "USDC")]


def gas_price_wei() -> int | None:
    """Current gas price from the public RPC. None when unreadable."""
    cached = _cache_get("gas_price")
    if cached is not None:
        return cached
    result, _ = _rpc("eth_gasPrice", [])
    price = _hex_to_int(result)
    if price:
        _cache_set("gas_price", price)
        return price
    return None


def _celo_usd_price() -> float:
    """CELO/USD for the informational estimate.

    Reuses the app's shared market-price helper so there is one price source
    (already cached upstream). Any failure returns the last known value, or
    0.0, which the UI renders as "no USD estimate" rather than a wrong number.
    """
    now = time.time()
    if _CELO_USD_CACHE["price"] and _CELO_USD_CACHE["expires_at"] > now:
        return _CELO_USD_CACHE["price"]
    try:
        from blockchain import _get_market_usd_prices  # local import: heavy module

        price = float((_get_market_usd_prices() or {}).get("celo") or 0)
        if price > 0:
            _CELO_USD_CACHE["price"] = price
            _CELO_USD_CACHE["expires_at"] = now + _CELO_USD_TTL
            return price
    except Exception as exc:  # noqa: BLE001
        logger.debug("chatroom: CELO USD price unavailable: %s", exc)
    return _CELO_USD_CACHE["price"]


def _format_small(value: float) -> str:
    """Trim a small CELO amount to something readable in a one-line notice."""
    if value <= 0:
        return "0"
    if value < 0.000001:
        return f"{value:.10f}".rstrip("0")
    if value < 1:
        return f"{value:.8f}".rstrip("0")
    return f"{value:.6f}".rstrip("0")


def estimate_post_fee(fee_currency: str | None = None, gas_units: int | None = None) -> dict:
    """Cost of ONE post, priced in CELO, plus the token that will settle it.

    Billed on gas USED, not the tx limit — unused gas is refunded, so quoting
    the limit overstates the cost ~2x and users notice. A fee currency adds
    Celo's CIP-64 intrinsic surcharge on top.

    Never raises: an unreadable gas price still returns ``success: True`` with
    ``estimated: False`` so the notice can say the fee varies instead of showing
    a wrong figure or an error.
    """
    currency = resolve_fee_currency(fee_currency) or FEE_CURRENCIES[FEE_CURRENCY_CELO]
    units = int(gas_units or POST_GAS_USED)
    if units <= 0:
        units = POST_GAS_USED
    # CIP-64 costs extra intrinsic gas; the user pays it, so the quote includes
    # it. Native CELO has no surcharge.
    if not currency["native"]:
        units += FEE_CURRENCY_INTRINSIC_GAS

    price = gas_price_wei()
    estimated = price is not None
    cost_wei = units * (price or 0)
    cost_celo = cost_wei / 1e18
    usd_price = _celo_usd_price()
    usd = cost_celo * usd_price if usd_price > 0 else 0.0

    return {
        "success": True,
        "estimated": estimated,
        # Billed units (gas used + any CIP-64 surcharge). The LIMIT is reported
        # separately so the two are never confused again.
        "gas_units": units,
        "gas_used": units,
        "gas_limit": POST_GAS_LIMIT,
        "gas_price_wei": str(price or 0),
        # The headline number: always CELO, whatever settles the fee.
        "celo_cost_wei": str(cost_wei),
        "celo_cost": cost_celo,
        "celo_cost_formatted": _format_small(cost_celo),
        "celo_usd_price": usd_price,
        "usd_cost": usd,
        "usd_cost_formatted": f"≈ ${usd:.6f}" if usd > 0 else "",
        # Which token the sender chose to settle it in.
        "fee_currency": currency["key"],
        "fee_currency_label": currency["label"],
        "fee_currency_address": currency["address"],
        "fee_currency_native": currency["native"],
        "fee_currency_supported": FEE_CURRENCY_SUPPORTED,
    }

# ── Unsigned transaction for the browser ─────────────────────────────────────

def encode_post_calldata(kind: int, ref: int, body: str) -> str:
    """ABI-encode ``post(uint8,uint256,bytes)`` for the browser to sign.

    The backend builds the calldata so the encoding has ONE implementation; the
    wallet still signs it, so the user remains the only party that can post.
    """
    payload = body.encode("utf-8")
    length = len(payload)
    padded = payload + b"\x00" * ((32 - length % 32) % 32)
    # head: selector + kind + ref + offset-to-bytes ; tail: length + padded body
    head = (
        "0x" + POST_SELECTOR
        + int(kind).to_bytes(32, "big").hex()
        + int(ref).to_bytes(32, "big").hex()
        + (96).to_bytes(32, "big").hex()
    )
    tail = length.to_bytes(32, "big").hex() + padded.hex()
    return head + tail


def prepare_post(wallet: str, kind: int, ref: int, body: str) -> dict:
    """Everything the browser needs to sign a post. No key, no signing here."""
    if not is_configured():
        return {"success": False, "error": "The chatroom contract is not configured.",
                "code": "not_configured"}
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    if kind not in _KIND_NAMES:
        return {"success": False, "error": "Unsupported message kind.", "code": "bad_kind"}

    banned = is_banned_onchain(wallet)
    if banned:
        return {"success": False, "error": "You cannot post in the chatroom.", "code": "banned"}

    return {
        "success": True,
        "to": CHAT_CONTRACT,
        "chain_id": CHAT_CHAIN_ID,
        "data": encode_post_calldata(kind, ref, body),
        "value": "0x0",
        "kind": kind,
        # The browser sizes gas from these; they mirror the contract's measured
        # worst case (see the contract header) rather than the tip flow's
        # ERC-20 numbers.
        "gas_fallback": 150000,
        "gas_ceiling": 300000,
    }
