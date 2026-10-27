"""Community Feed — HTTP routes.

Blueprint ``community_feed`` at ``/community``. A chronological feed of user
posts (caption + images/video) with Messenger-style reactions.

The whole feature is HIDDEN by default: the page redirects away and every API
call returns ``{enabled: false}`` until an admin flips ``community_feed_feature``
ON from the dashboard. The gate is enforced server-side, not just by hiding the
UI.

Privacy: the browser never receives a wallet address. ``service._public_post``
reduces ownership to a boolean ``is_mine``.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, redirect, render_template, request, session

from . import rewards
from . import reward_settings
from . import service as svc
from . import storage
from . import vault_donations
from . import notifications as community_notifications
from . import withdrawals as withdrawal_vault

logger = logging.getLogger(__name__)

community_feed_bp = Blueprint("community_feed", __name__, url_prefix="/community")

# In-process throttle (per wallet), best-effort per gunicorn worker. Enough to
# blunt an abusive burst without a shared store — same approach as the chatroom.
_THROTTLE_LOCK = threading.Lock()
_HITS: dict[str, deque] = defaultdict(deque)


def _throttle(key: str, window_seconds: int, allow: int = 1) -> int:
    """Returns 0 when allowed, else the seconds the caller must wait.

    ``allow`` is how many hits are permitted inside the window. It defaults to
    1 (a genuine rate limit) and is raised only for the upload presign, where a
    single post legitimately needs one request per file plus a poster frame.
    """
    window = max(1, int(window_seconds))
    allow = max(1, int(allow))
    key = (key or "anon").lower()
    now = time.time()
    with _THROTTLE_LOCK:
        hits = _HITS[key]
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= allow:
            return max(1, int(window - (now - hits[0])))
        hits.append(now)
    return 0


def _session_wallet() -> str | None:
    wallet = session.get("wallet") or session.get("wallet_address")
    if not wallet or not (session.get("verified") or session.get("ubi_verified")):
        return None
    return wallet


def _community_messages_enabled() -> bool:
    return (os.getenv("COMMUNITY_DIRECT_MESSAGES_ENABLED", "1") or "1").strip().lower() not in {
        "0", "false", "off", "no"
    }


def _admin_wallet() -> str | None:
    wallet = session.get("wallet") or session.get("wallet_address")
    if not wallet:
        return None
    try:
        from supabase_client import is_admin

        return wallet if is_admin(wallet) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: admin check failed: %s", exc)
        return None


def _disabled_response():
    return jsonify({
        "success": False,
        "enabled": False,
        "error": "The community feed is not available yet.",
    }), 403


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _withdrawal_origin_ok() -> bool:
    """Reject cross-origin cookie-authenticated mutation requests."""
    origin = request.headers.get("Origin", "")
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in {"https", "http"} and bool(parsed.netloc) \
            and hmac.compare_digest(parsed.netloc.casefold(), request.host.casefold())
    except Exception:  # noqa: BLE001
        return False


def _withdrawal_admin_db():
    """Money-moving withdrawal state requires the service-role Supabase client."""
    from supabase_client import get_supabase_admin_client

    db = get_supabase_admin_client()
    if not db:
        raise RuntimeError("Service-role database client is unavailable")
    return db


def _withdrawal_rows(result) -> list[dict]:
    data = getattr(result, "data", None)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _withdrawal_rpc(db, name: str, args: dict):
    result = db.rpc(name, args).execute()
    data = getattr(result, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        raise RuntimeError(f"Database RPC {name} returned no state")
    return data


def _open_withdrawal(db, wallet: str) -> dict | None:
    result = (db.table("community_reward_withdrawals")
              .select("*")
              .ilike("wallet_address", wallet.lower())
              .in_("status", ["pending", "claimed"])
              .order("id", desc=True).limit(1).execute())
    rows = _withdrawal_rows(result)
    return rows[0] if rows else None


def _claimed_withdrawal(db, wallet: str, withdrawal_id: str | None = None) -> dict | None:
    """Read only a claimed reservation belonging to the authenticated wallet."""
    query = (db.table("community_reward_withdrawals").select("*")
             .ilike("wallet_address", wallet.lower()).eq("status", "claimed"))
    if withdrawal_id is not None:
        query = query.eq("withdrawal_id", withdrawal_id)
    rows = _withdrawal_rows(query.order("id", desc=True).execute())
    for row in rows:
        if row.get("authorization_signature"):
            return row
    return None


def _private_withdrawal_json(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Vary"] = "Cookie"
    return response, status


def _fresh_withdrawal_identity(wallet: str) -> tuple[bool, bool]:
    """Return (verified, chain_available), bypassing the identity read cache."""
    try:
        from blockchain import is_identity_verified

        result = is_identity_verified(wallet, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community withdrawal identity check unavailable: %s", exc)
        return False, False
    if result.get("error"):
        return False, False
    return bool(result.get("verified")), True


def _authorization_payload(row: dict) -> dict:
    return {
        "recipient": row["wallet_address"],
        "amount": str(row["amount_base_units"]),
        "withdrawalId": row["withdrawal_id"],
        "nonce": str(row["authorization_nonce"]),
        "deadline": int(row["authorization_deadline"]),
        "projectId": row["project_id"],
    }


def _sign_reserved_withdrawal(db, runtime, row: dict) -> dict:
    authorization = _authorization_payload(row)
    signature = str(row.get("authorization_signature") or "")
    if not signature:
        signature = withdrawal_vault.sign_authorization(
            runtime.issuer_private_key,
            domain={"verifyingContract": runtime.vault_address},
            message=authorization,
        )
        saved = _withdrawal_rpc(db, "community_reward_withdrawal_authorize", {
            "p_withdrawal_id": row["withdrawal_id"],
            "p_signature": signature,
        })
        row = saved
    return {
        "success": True,
        "status": "authorized",
        "withdrawal_id": row["withdrawal_id"],
        "authorization": authorization,
        "signature": signature,
        "amount_gd": str(row["amount_gd"]),
        "vault_address": runtime.vault_address,
        "token_address": runtime.token_address,
        "chain_id": withdrawal_vault.CELO_CHAIN_ID,
        "token_decimals": runtime.decimals,
        "tx_hash": row.get("tx_hash"),
    }


# ── Page ─────────────────────────────────────────────────────────────────────

@community_feed_bp.route("/")
def community_feed_home():
    """Feed page. Guests may view; they get the shared connect card."""
    wallet = _session_wallet()

    if not svc.is_enabled():
        # Enabled-check first: a hidden feature must not even render a shell.
        return redirect("/wallet" if wallet else "/")

    # NOTE: deliberately NOT face-verification gated — like the chatroom, the
    # feed is for all signed-in users and guests may read it. Moderation is the
    # report/ban path. Re-add a human_verification_redirect only if the product
    # decision changes.
    from routes import _claim_boot_context

    return render_template(
        "community.html",
        **_claim_boot_context(
            wallet or "",
            session.get("login_method", ""),
            claim_only=True,
            fv_return_to="/community",
        ),
        is_guest=not bool(wallet),
        messages_enabled=_community_messages_enabled(),
        vault_donations_enabled=vault_donations.donations_enabled(),
        display_name=svc.display_name(wallet) if wallet else None,
        username=svc.get_username(wallet) if wallet else None,
        avatar_url=svc.get_avatar_url(wallet) if wallet else None,
        username_required=svc.REQUIRE_USERNAME,
        max_caption=svc.MAX_CAPTION,
        max_media=svc.MAX_MEDIA_PER_POST,
        max_image_mb=svc.MAX_IMAGE_MB,
        max_video_mb=svc.MAX_VIDEO_MB,
        max_avatar_mb=svc.MAX_AVATAR_MB,
        storage_ready=storage.storage_available(),
        # The "Today" chip is stamped SERVER-side on purpose. The feed is a
        # daily surface and the Daily Learn card already resets on the UTC
        # clock, so the date the page shows must come from that same clock — a
        # client-rendered date would disagree with the reset for anyone whose
        # device is not on UTC.
        today_label=datetime.now(timezone.utc).strftime("%d %b").lstrip("0"),
    )


@community_feed_bp.route("/p/<int:post_id>")
def community_feed_post(post_id: int):
    """Canonical public URL for one post, including social-share metadata.

    The post is loaded server-side so crawlers and messaging apps receive a
    real per-post title/description/image instead of a feed URL with a hash
    fragment (fragments are never sent to the server). The service serializer
    deliberately removes the owner's wallet address before the object reaches
    the template or browser.
    """
    wallet = _session_wallet()
    if not svc.is_enabled():
        return redirect("/wallet" if wallet else "/")

    result = svc.get_post(post_id, viewer=wallet)
    post = result.get("post") if result.get("success") else None
    if not post:
        return Response("Post not found", status=404, mimetype="text/plain")

    from routes import _claim_boot_context

    media = post.get("media") or []
    first_media = media[0] if media else {}
    share_image = first_media.get("url") if first_media.get("type") == "image" else first_media.get("poster")
    if not share_image:
        share_image = request.url_root.rstrip("/") + "/static/images/community-post-preview.svg"
    username = post.get("username") or "Community member"
    body = " ".join((post.get("body") or "").split())
    description = body[:180] + ("…" if len(body) > 180 else "")
    if not description:
        description = "A post from the GoodMarket Community."
    canonical_url = request.url_root.rstrip("/") + "/community/p/" + str(post_id)
    return render_template(
        "community.html",
        **_claim_boot_context(
            wallet or "",
            session.get("login_method", ""),
            claim_only=True,
            fv_return_to=canonical_url,
        ),
        is_guest=not bool(wallet),
        messages_enabled=_community_messages_enabled(),
        vault_donations_enabled=vault_donations.donations_enabled(),
        display_name=svc.display_name(wallet) if wallet else None,
        username=svc.get_username(wallet) if wallet else None,
        avatar_url=svc.get_avatar_url(wallet) if wallet else None,
        username_required=svc.REQUIRE_USERNAME,
        max_caption=svc.MAX_CAPTION,
        max_media=svc.MAX_MEDIA_PER_POST,
        max_image_mb=svc.MAX_IMAGE_MB,
        max_video_mb=svc.MAX_VIDEO_MB,
        max_avatar_mb=svc.MAX_AVATAR_MB,
        storage_ready=storage.storage_available(),
        focus_post=post,
        focus_post_id=post_id,
        canonical_url=canonical_url,
        share_title=("Community Vault donation · Sytem" if post.get("post_type") == "system_vault_donation"
                     else f"@{username} on GoodMarket Community"),
        share_description=description,
        share_image=share_image,
    )


# ── Feed ─────────────────────────────────────────────────────────────────────

@community_feed_bp.route("/api/state", methods=["GET"])
def api_state():
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    username = svc.get_username(wallet) if wallet else None
    return jsonify({
        "success": True,
        "enabled": True,
        "wallet": wallet,
        "username": username,
        "display_name": svc.display_name(wallet) if wallet else None,
        "avatar_url": svc.get_avatar_url(wallet) if wallet else None,
        "needs_username": bool(wallet) and svc.REQUIRE_USERNAME and not username,
        "is_admin": bool(_admin_wallet()),
        "is_banned": svc.is_banned(wallet) if wallet else False,
        "storage_ready": storage.storage_available(),
        "reactions": svc.REACTIONS,
        "max_caption": svc.MAX_CAPTION,
        "max_media": svc.MAX_MEDIA_PER_POST,
    })


def _private_notification_json(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Vary"] = "Cookie"
    return response, status


@community_feed_bp.route("/api/notifications", methods=["GET"])
def api_notifications():
    """Read the authenticated member's private Community activity inbox."""
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return _private_notification_json({
            "success": False, "code": "auth_required", "error": "Not authenticated",
        }, 401)
    raw_limit = request.args.get("limit", "20")
    raw_cursor = request.args.get("cursor")
    if not re.fullmatch(r"\d{1,2}", raw_limit or ""):
        return _private_notification_json({
            "success": False, "code": "invalid_limit", "error": "Invalid notification limit.",
        }, 400)
    limit = int(raw_limit)
    if not 1 <= limit <= 50:
        return _private_notification_json({
            "success": False, "code": "invalid_limit", "error": "Invalid notification limit.",
        }, 400)
    cursor = None
    if raw_cursor is not None:
        if not re.fullmatch(r"\d{1,19}", raw_cursor or ""):
            return _private_notification_json({
                "success": False, "code": "invalid_cursor", "error": "Invalid notification cursor.",
            }, 400)
        cursor = int(raw_cursor)
        if not 1 <= cursor <= 9_223_372_036_854_775_807:
            return _private_notification_json({
                "success": False, "code": "invalid_cursor", "error": "Invalid notification cursor.",
            }, 400)
    wait = _throttle(f"community-notifications:{wallet}", 10, allow=30)
    if wait:
        return _private_notification_json({
            "success": False, "code": "rate_limited", "error": "Please wait before refreshing notifications.",
            "retry_after": wait,
        }, 429)
    result = community_notifications.list_for_wallet(
        wallet, limit=limit, before_id=cursor)
    status = 503 if result.get("code") == "db" else (400 if result.get("code") == "cursor" else 200)
    return _private_notification_json(result, status)


@community_feed_bp.route("/api/notifications/<int:notification_id>/read", methods=["POST"])
def api_notification_mark_read(notification_id: int):
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return _private_notification_json({
            "success": False, "code": "auth_required", "error": "Not authenticated",
        }, 401)
    wait = _throttle(f"community-notifications:{wallet}", 10, allow=30)
    if wait:
        return _private_notification_json({
            "success": False, "code": "rate_limited", "error": "Please wait before updating notifications.",
            "retry_after": wait,
        }, 429)
    result = community_notifications.mark_one_read(wallet, notification_id)
    status = 503 if result.get("code") == "db" else (400 if result.get("code") == "id" else 200)
    return _private_notification_json(result, status)


@community_feed_bp.route("/api/notifications/read-all", methods=["POST"])
def api_notifications_mark_all_read():
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return _private_notification_json({
            "success": False, "code": "auth_required", "error": "Not authenticated",
        }, 401)
    wait = _throttle(f"community-notifications:{wallet}", 10, allow=30)
    if wait:
        return _private_notification_json({
            "success": False, "code": "rate_limited", "error": "Please wait before updating notifications.",
            "retry_after": wait,
        }, 429)
    result = community_notifications.mark_all_read(wallet)
    status = 503 if result.get("code") == "db" else 200
    return _private_notification_json(result, status)


@community_feed_bp.route("/api/rewards", methods=["GET"])
def api_rewards():
    """The viewer's G$ earned balance. Reading it is gated like the feed."""
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    return jsonify(svc.rewards_summary(wallet))


@community_feed_bp.route("/api/rewards/withdrawals/history", methods=["GET"])
def api_withdrawal_history():
    """Read a member's successful WithdrawalPaid events from the verified vault."""
    def history_json(payload, status=200):
        response = jsonify(payload)
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        return response, status

    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return history_json({"success": False, "error": "Not authenticated"}, 401)
    retry = _throttle("withdraw-history:" + wallet, 30, allow=6)
    if retry:
        return history_json({"success": False, "error": "Too many history requests. Please wait and retry."}, 429)

    def integer_arg(name, default=None):
        raw = request.args.get(name)
        if raw is None:
            return default
        if not re.fullmatch(r"\d+", raw):
            raise ValueError(name)
        return int(raw)

    try:
        limit = integer_arg("limit", 20)
        before_block = integer_arg("before_block")
        before_log_index = integer_arg("before_log_index")
        if not 1 <= limit <= 50:
            raise ValueError("limit")
        if (before_block is None) != (before_log_index is None):
            raise ValueError("cursor")
    except ValueError:
        return history_json({"success": False, "error": "Invalid limit or pagination cursor."}, 400)

    try:
        from web3 import Web3
        runtime = withdrawal_vault.build_runtime(require_enabled=False, require_signer_key=False)
        deployment_block = withdrawal_vault.deployment_block_for_runtime(runtime)
        wallet = Web3.to_checksum_address(wallet)
        vault_address = Web3.to_checksum_address(runtime.vault_address)
        latest = int(runtime.web3.eth.block_number)
        if deployment_block > latest:
            return history_json({"success": False, "error": "Withdrawal history start block is newer than the Celo chain tip."}, 503)
        end_block = min(latest, before_block if before_block is not None else latest)
        max_chunk = int(os.environ.get("GETLOGS_MAX_CHUNK", "4000"))
        chunk = max(500, min(max_chunk, 4000))
        topic0 = Web3.to_hex(Web3.keccak(text="WithdrawalPaid(address,uint256,bytes32,uint256,uint256)"))
        recipient_topic = "0x" + wallet.lower().removeprefix("0x").rjust(64, "0")
        collected = []
        scanned_blocks = 0
        # The shared block-span setting can be lowered for RPC providers, but a
        # page must not turn into thousands of serial eth_getLogs requests.
        # When the budget is reached the block cursor resumes the scan later.
        max_scan_blocks = min(100000, chunk * 50)
        cursor_tuple = (before_block, before_log_index) if before_block is not None else None
        while (end_block >= deployment_block and len(collected) <= limit
               and scanned_blocks < max_scan_blocks):
            start_block = max(deployment_block, end_block - chunk + 1,
                              end_block - (max_scan_blocks - scanned_blocks) + 1)
            logs = runtime.web3.eth.get_logs({
                "address": vault_address,
                "fromBlock": start_block,
                "toBlock": end_block,
                "topics": [topic0, recipient_topic],
            })
            logs = sorted(logs, key=lambda item: (int(item["blockNumber"]), int(item["logIndex"])), reverse=True)
            for log in logs:
                bn, li = int(log["blockNumber"]), int(log["logIndex"])
                if cursor_tuple and (bn, li) >= cursor_tuple:
                    continue
                try:
                    if Web3.to_checksum_address(log.get("address") or "") != vault_address:
                        continue
                except Exception:
                    continue
                raw_topics = log.get("topics") or []
                raw_data = log.get("data", b"")
                try:
                    topics = [Web3.to_bytes(hexstr=item) if isinstance(item, str) else bytes(item)
                              for item in raw_topics]
                    data = Web3.to_bytes(hexstr=raw_data) if isinstance(raw_data, str) else bytes(raw_data)
                except Exception:
                    continue
                if len(topics) != 3 or any(len(item) != 32 for item in topics) or len(data) != 96:
                    continue
                if Web3.to_hex(topics[0]).lower() != topic0.lower():
                    continue
                if Web3.to_checksum_address("0x" + topics[1][-20:].hex()) != wallet:
                    continue
                # Decode ABI words only after validating event signature/topic count/data size.
                withdrawal_id = Web3.to_hex(topics[2]).lower()
                amount = int.from_bytes(data[0:32], "big")
                tx_hash = Web3.to_hex(log["transactionHash"]).lower()
                confirmations = max(0, latest - bn + 1)
                try:
                    block = runtime.web3.eth.get_block(bn)
                    timestamp = datetime.fromtimestamp(int(block["timestamp"]), timezone.utc).isoformat().replace("+00:00", "Z")
                except Exception:
                    timestamp = None
                collected.append({
                    "tx_hash": tx_hash, "withdrawal_id": withdrawal_id,
                    "amount_gd": str(withdrawal_vault.base_units_to_gd(amount, runtime.decimals)),
                    "block_number": bn, "log_index": li, "timestamp_utc": timestamp,
                    "confirmations": confirmations,
                    "status": "confirmed" if confirmations >= runtime.confirmations else "confirming",
                })
                if len(collected) > limit:
                    break
            scanned_blocks += end_block - start_block + 1
            if len(collected) > limit:
                break
            end_block = start_block - 1
        reached_start = end_block < deployment_block
        collected.sort(key=lambda item: (item["block_number"], item["log_index"]), reverse=True)
        has_more = len(collected) > limit or (not reached_start and scanned_blocks >= max_scan_blocks)
        page_items = collected[:limit]
        next_cursor = None
        if len(collected) > limit:
            last = page_items[-1]
            next_cursor = {"block_number": last["block_number"], "log_index": last["log_index"]}
        elif has_more:
            # All logs in scanned blocks were returned; continue strictly below
            # the lowest fully scanned block on the next page.
            next_cursor = {"block_number": end_block + 1, "log_index": 0}
        return history_json({"success": True, "items": page_items, "has_more": has_more,
                             "next_cursor": next_cursor})
    except withdrawal_vault.WithdrawalConfigError as exc:
        logger.warning("community withdrawal history runtime unavailable: %s", withdrawal_vault.operator_detail(exc))
        return history_json({"success": False, "error": withdrawal_vault.MEMBER_CONFIG_ERROR}, 503)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community withdrawal history RPC failed: %s", exc)
        return history_json({"success": False, "error": "Withdrawal history could not be loaded. Please retry."}, 503)


@community_feed_bp.route("/api/rewards/withdrawals/reconciliation", methods=["GET"])
def api_withdrawal_reconciliation_status():
    """Expose only whether this wallet has a signed claimed row to reconcile."""
    if not svc.is_enabled():
        response = _disabled_response()
        response[0].headers["Cache-Control"] = "private, no-store, max-age=0"
        return response
    wallet = _session_wallet()
    if not wallet:
        return _private_withdrawal_json({"success": False, "error": "Not authenticated"}, 401)
    try:
        row = _claimed_withdrawal(_withdrawal_admin_db(), wallet)
        if not row or not row.get("authorization_signature"):
            return _private_withdrawal_json({"success": True, "needs_reconciliation": False})
        return _private_withdrawal_json({
            "success": True,
            "needs_reconciliation": True,
            "withdrawal": {
                "withdrawal_id": row.get("withdrawal_id"),
                "amount_gd": str(row.get("amount_gd") or "0"),
                "tx_hash": row.get("tx_hash"),
            },
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("community withdrawal reconciliation status lookup failed: %s", exc)
        return _private_withdrawal_json({
            "success": False,
            "error": "Withdrawal reconciliation status could not be loaded. Please retry.",
        }, 503)


@community_feed_bp.route("/api/rewards/withdrawals/reconciliation", methods=["POST"])
def api_withdrawal_reconciliation_scan():
    """Scan one bounded page for the authenticated wallet's existing payout."""
    if not _withdrawal_origin_ok():
        return _private_withdrawal_json({"success": False, "error": "Cross-origin withdrawal request rejected"}, 403)
    if not svc.is_enabled():
        response = _disabled_response()
        response[0].headers["Cache-Control"] = "private, no-store, max-age=0"
        return response
    wallet = _session_wallet()
    if not wallet:
        return _private_withdrawal_json({"success": False, "error": "Not authenticated"}, 401)
    retry = _throttle("withdraw-reconcile:" + wallet, 30, allow=6)
    if retry:
        return _private_withdrawal_json({"success": False, "error": "Too many reconciliation requests. Please wait and retry."}, 429)

    body = _json_body()
    withdrawal_id = str(body.get("withdrawal_id") or "").strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{64}", withdrawal_id):
        return _private_withdrawal_json({"success": False, "error": "A valid withdrawal ID is required."}, 400)
    before_block = body.get("before_block")
    if before_block is not None:
        if isinstance(before_block, bool) or not re.fullmatch(r"\d+", str(before_block)):
            return _private_withdrawal_json({"success": False, "error": "The scan cursor is invalid."}, 400)
        before_block = int(before_block)

    try:
        db = _withdrawal_admin_db()
        row = _claimed_withdrawal(db, wallet, withdrawal_id)
        if not row or not row.get("authorization_signature"):
            return _private_withdrawal_json({
                "success": False,
                "status": "manual_review",
                "error": "No existing signed Community withdrawal was found. No balance change was made; please contact support.",
            }, 409)
        stored_hash = str(row.get("tx_hash") or "").strip().lower()
        if stored_hash:
            if not re.fullmatch(r"0x[0-9a-f]{64}", stored_hash):
                return _private_withdrawal_json({
                    "success": False,
                    "status": "manual_review",
                    "error": "The existing withdrawal needs support review. No balance change was made.",
                }, 409)
            return _private_withdrawal_json({
                "success": True, "status": "transaction_found",
                "withdrawal_id": withdrawal_id, "tx_hash": stored_hash,
            })

        runtime = withdrawal_vault.build_runtime(require_enabled=False, require_signer_key=False)
        deployment_block = withdrawal_vault.deployment_block_for_runtime(runtime)
        result = withdrawal_vault.scan_withdrawal_transaction_page(
            runtime, row, deployment_block=deployment_block, before_block=before_block,
        )
        if result.get("status") == "invalid_cursor":
            return _private_withdrawal_json({
                "success": False,
                "status": "invalid_cursor",
                "error": "The scan cursor is outside the configured vault history range.",
            }, 400)
        if result.get("status") == "rpc_error":
            logger.warning("community withdrawal reconciliation RPC unavailable")
            return _private_withdrawal_json({
                "success": False,
                "status": "retryable",
                "error": "Withdrawal reconciliation could not complete. No balance change was made; please retry.",
            }, 503)
        if result.get("status") == "manual_review":
            return _private_withdrawal_json({
                "success": False,
                "status": "manual_review",
                "error": "The existing withdrawal could not be matched exactly. No balance change was made; please contact support.",
            }, 409)
        return _private_withdrawal_json({"success": True, "withdrawal_id": withdrawal_id, **result})
    except withdrawal_vault.WithdrawalConfigError as exc:
        logger.warning("community withdrawal reconciliation configuration unavailable: %s",
                       withdrawal_vault.operator_detail(exc))
        return _private_withdrawal_json({
            "success": False, "status": "retryable",
            "error": withdrawal_vault.MEMBER_CONFIG_ERROR,
        }, 503)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community withdrawal reconciliation failed: %s", exc)
        return _private_withdrawal_json({
            "success": False, "status": "retryable",
            "error": "Withdrawal reconciliation could not complete. No balance change was made; please retry.",
        }, 503)


@community_feed_bp.route("/api/rewards/withdrawal-eligibility", methods=["GET"])
def api_withdrawal_eligibility():
    """Read-only, fail-closed readiness check for the direct user-paid vault."""
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    try:
        db = _withdrawal_admin_db()
        open_row = _open_withdrawal(db, wallet)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community withdrawal reservation lookup failed: %s", exc)
        return jsonify({"success": True, "threshold_met": False,
                        "can_request": False,
                        "message": "Withdrawal readiness could not be verified. Please retry shortly."})
    if open_row and (open_row.get("authorization_signature") or open_row.get("tx_hash")):
        try:
            resume_runtime = withdrawal_vault.build_runtime(require_enabled=False, require_signer_key=False)
            is_paused = bool(resume_runtime.vault.functions.paused().call())
            if is_paused and not open_row.get("tx_hash"):
                return jsonify({"success": True, "threshold_met": True,
                                "can_request": False, "status": "paused",
                                "message": "Community withdrawals are temporarily paused."})
            return jsonify({"success": True, "threshold_met": True,
                            "can_request": True,
                            "status": "transaction_submitted" if open_row.get("tx_hash") else "resume",
                            "withdrawal_id": open_row.get("withdrawal_id"),
                            "tx_hash": open_row.get("tx_hash"),
                            "amount_gd": str(open_row.get("amount_gd") or "0"),
                            "message": "An existing Community withdrawal is reserved. Continuing will safely resume or confirm that same request."})
        except Exception as exc:  # noqa: BLE001
            logger.warning("community withdrawal resume check failed: %s", exc)
            return jsonify({"success": True, "threshold_met": True,
                            "can_request": False,
                            "message": "The existing withdrawal needs a fresh vault check before it can be resumed."})
    summary = rewards.summary(wallet)
    if not summary.get("balance_available"):
        return jsonify({"success": True, "threshold_met": False,
                        "can_request": False,
                        "message": "Your Community balance could not be fully verified. Please retry shortly."})
    if not summary.get("eligible"):
        return jsonify({"success": True, "threshold_met": False,
                        "can_request": False,
                        "message": "A currently verified GoodDollar identity is required to withdraw."})
    balance = Decimal(str(summary.get("balance_gd") or "0"))
    minimum = Decimal(str(summary.get("min_payout_gd") or "500"))
    remaining = max(Decimal("0"), minimum - balance)
    if remaining > 0:
        return jsonify({
            "success": True, "threshold_met": False, "can_request": False,
            "remaining_gd": str(remaining),
            "message": f"You need to reach {minimum:g} G$ to withdraw. You still need {remaining:g} G$.",
        })
    try:
        runtime = withdrawal_vault.build_runtime()
        if open_row:
            if not open_row.get("tx_hash") and runtime.vault.functions.paused().call():
                return jsonify({"success": True, "threshold_met": True,
                                "can_request": False, "status": "paused",
                                "message": "Community withdrawals are temporarily paused."})
            return jsonify({"success": True, "threshold_met": True,
                            "can_request": True,
                            "status": "transaction_submitted" if open_row.get("tx_hash") else "resume",
                            "withdrawal_id": open_row.get("withdrawal_id"),
                            "tx_hash": open_row.get("tx_hash"),
                            "amount_gd": str(open_row.get("amount_gd") or balance),
                            "message": "An existing Community withdrawal is reserved. Continuing will safely resume or confirm that same request."})
        if runtime.vault.functions.paused().call():
            return jsonify({"success": True, "threshold_met": True,
                            "can_request": False,
                            "message": "Community withdrawals are temporarily paused."})
    except withdrawal_vault.WithdrawalConfigError as exc:
        logger.warning("community withdrawal readiness check failed: %s",
                       withdrawal_vault.operator_detail(exc))
        return jsonify({"success": True, "threshold_met": True,
                        "can_request": False,
                        "message": withdrawal_vault.MEMBER_CONFIG_ERROR})
    except Exception as exc:  # noqa: BLE001
        logger.warning("community withdrawal readiness check failed: %s", exc)
        return jsonify({"success": True, "threshold_met": True,
                        "can_request": False,
                        "message": "Withdrawal readiness could not be verified. Please retry shortly."})
    return jsonify({
        "success": True, "threshold_met": True, "can_request": True,
        "amount_gd": str(balance),
        "message": "Your full available Community balance is ready for a direct wallet withdrawal. You will pay Celo gas.",
    })


@community_feed_bp.route("/api/rewards/withdrawals/authorize", methods=["POST"])
def api_withdrawal_authorize():
    """Reserve the full server-derived balance and issue the issuer EIP-712 signature."""
    if not _withdrawal_origin_ok():
        return jsonify({"success": False, "error": "Cross-origin withdrawal request rejected"}), 403
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    retry = _throttle("withdraw-auth:" + wallet, 60, allow=3)
    if retry:
        return jsonify({"success": False, "error": "Too many withdrawal requests. Please wait and retry."}), 429
    try:
        from web3 import Web3

        member = Web3.to_checksum_address(wallet)
        db = _withdrawal_admin_db()
        existing = _open_withdrawal(db, member)
        if existing:
            resume_runtime = withdrawal_vault.build_runtime(require_enabled=False, require_signer_key=False)
            if existing.get("tx_hash"):
                return jsonify({"success": False, "status": "transaction_submitted",
                                "withdrawal_id": existing["withdrawal_id"], "tx_hash": existing["tx_hash"],
                                "error": "A transaction is already linked to this withdrawal. Confirm it before retrying."}), 409

            chain_now = int(resume_runtime.web3.eth.get_block("latest")["timestamp"])
            row_nonce = int(existing.get("authorization_nonce") or 0)
            current_nonce = int(resume_runtime.vault.functions.nonces(member).call())
            row_deadline = int(existing.get("authorization_deadline") or 0)
            has_signature = bool(existing.get("authorization_signature"))
            if has_signature and current_nonce != row_nonce:
                recovered_hash = None
                if current_nonce > row_nonce:
                    recovered_hash = withdrawal_vault.find_withdrawal_transaction(resume_runtime, existing)
                if recovered_hash:
                    _withdrawal_rpc(db, "community_reward_withdrawal_set_tx", {
                        "p_withdrawal_id": existing["withdrawal_id"], "p_tx_hash": recovered_hash,
                    })
                    return jsonify({"success": False, "status": "transaction_submitted",
                                    "withdrawal_id": existing["withdrawal_id"], "tx_hash": recovered_hash,
                                    "error": "Recovered the submitted withdrawal transaction. Confirming its receipt."}), 409
                return jsonify({"success": False, "status": "reconciliation_required",
                                "withdrawal_id": existing.get("withdrawal_id"),
                                "error": "The on-chain nonce changed. The existing signed request needs reconciliation; do not submit it again."}), 409

            if row_deadline <= chain_now:
                _withdrawal_rpc(db, "community_reward_withdrawal_fail", {
                    "p_withdrawal_id": existing["withdrawal_id"],
                    "p_reason": "Unsubmitted authorization expired on Celo",
                    "p_tx_hash": None,
                })
                existing = None
            elif has_signature:
                if resume_runtime.vault.functions.paused().call():
                    return jsonify({"success": False, "status": "paused",
                                    "error": "Community withdrawals are temporarily paused."}), 409
                return jsonify(_sign_reserved_withdrawal(db, resume_runtime, existing))
            elif current_nonce != row_nonce:
                _withdrawal_rpc(db, "community_reward_withdrawal_fail", {
                    "p_withdrawal_id": existing["withdrawal_id"],
                    "p_reason": "Unissued reservation nonce was superseded on Celo",
                    "p_tx_hash": None,
                })
                existing = None

        runtime = withdrawal_vault.build_runtime()
        verified, chain_available = _fresh_withdrawal_identity(member)
        if not chain_available:
            return jsonify({"success": False, "error": "GoodDollar identity verification is temporarily unavailable."}), 503
        if not verified:
            return jsonify({"success": False, "error": "A currently verified GoodDollar identity is required to withdraw."}), 403
        balance, complete = rewards._balance_read(member, db)
        if not complete:
            return jsonify({"success": False, "error": "The full Community ledger could not be read. No authorization was issued."}), 503
        if balance <= 0:
            return jsonify({"success": False, "error": "There is no available Community G$ balance to withdraw."}), 409
        amount_base = withdrawal_vault.amount_to_base_units(balance, runtime.decimals)
        min_gd = withdrawal_vault.base_units_to_gd(runtime.min_amount, runtime.decimals)
        required_minimum = max(rewards.MIN_PAYOUT_GD, min_gd)
        if balance < required_minimum or amount_base < runtime.min_amount:
            return jsonify({"success": False, "error": f"At least {required_minimum:g} G$ is required to withdraw."}), 409
        if amount_base > runtime.max_amount:
            return jsonify({"success": False, "error": "Your full balance exceeds this vault's per-withdrawal limit. Partial withdrawals are not enabled."}), 409
        db_row = _open_withdrawal(db, member)
        latest = runtime.web3.eth.get_block("latest")
        chain_now = int(latest["timestamp"])
        if db_row:
            if db_row.get("tx_hash"):
                return jsonify({"success": False, "status": "transaction_submitted",
                                "withdrawal_id": db_row["withdrawal_id"], "tx_hash": db_row["tx_hash"],
                                "error": "A transaction is already linked to this withdrawal. Confirm it before retrying."}), 409
            row_nonce = int(db_row.get("authorization_nonce") or 0)
            current_nonce = int(runtime.vault.functions.nonces(member).call())
            row_deadline = int(db_row.get("authorization_deadline") or 0)
            has_signature = bool(db_row.get("authorization_signature"))
            if has_signature and current_nonce != row_nonce:
                recovered_hash = None
                if current_nonce > row_nonce:
                    recovered_hash = withdrawal_vault.find_withdrawal_transaction(runtime, db_row)
                if recovered_hash:
                    _withdrawal_rpc(db, "community_reward_withdrawal_set_tx", {
                        "p_withdrawal_id": db_row["withdrawal_id"], "p_tx_hash": recovered_hash,
                    })
                    return jsonify({"success": False, "status": "transaction_submitted",
                                    "withdrawal_id": db_row["withdrawal_id"], "tx_hash": recovered_hash,
                                    "error": "Recovered the submitted withdrawal transaction. Confirming its receipt."}), 409
                return jsonify({"success": False, "status": "reconciliation_required",
                                "withdrawal_id": db_row.get("withdrawal_id"),
                                "error": "The on-chain withdrawal nonce changed. Reconcile the existing request before retrying."}), 409
            if row_deadline <= chain_now:
                _withdrawal_rpc(db, "community_reward_withdrawal_fail", {
                    "p_withdrawal_id": db_row["withdrawal_id"],
                    "p_reason": "Unsubmitted authorization expired on Celo",
                    "p_tx_hash": None,
                })
                db_row = None
            elif not has_signature and current_nonce != row_nonce:
                _withdrawal_rpc(db, "community_reward_withdrawal_fail", {
                    "p_withdrawal_id": db_row["withdrawal_id"],
                    "p_reason": "Unissued reservation nonce was superseded on Celo",
                    "p_tx_hash": None,
                })
                db_row = None
            elif not has_signature and (
                int(db_row.get("amount_base_units") or 0) != amount_base
                or Decimal(str(db_row.get("amount_gd") or "0")) != balance
            ):
                _withdrawal_rpc(db, "community_reward_withdrawal_fail", {
                    "p_withdrawal_id": db_row["withdrawal_id"],
                    "p_reason": "Community ledger balance changed before authorization",
                    "p_tx_hash": None,
                })
                db_row = None
            if db_row:
                if runtime.vault.functions.paused().call():
                    return jsonify({"success": False, "error": "Community withdrawals are temporarily paused."}), 409
                return jsonify(_sign_reserved_withdrawal(db, runtime, db_row))

        if runtime.vault.functions.paused().call():
            return jsonify({"success": False, "error": "Community withdrawals are temporarily paused."}), 409
        daily_remaining = int(runtime.vault.functions.currentUtcDayRemaining().call())
        if amount_base > daily_remaining:
            return jsonify({"success": False, "error": "The vault's remaining daily limit is below your full Community balance. Please retry after the UTC daily limit resets."}), 409
        nonce = int(runtime.vault.functions.nonces(member).call())
        deadline = min(chain_now + 10 * 60,
                       chain_now + withdrawal_vault.MAX_AUTHORIZATION_LIFETIME_SECONDS)
        withdrawal_id = "0x" + secrets.token_hex(32)
        prepared = _withdrawal_rpc(db, "community_reward_withdrawal_prepare", {
            "p_wallet_address": member.lower(),
            "p_amount_gd": str(balance),
            "p_amount_base_units": str(amount_base),
            "p_withdrawal_id": withdrawal_id,
            "p_idempotency_key": "community_withdrawal:" + withdrawal_id.lower(),
            "p_authorization_nonce": str(nonce),
            "p_authorization_deadline": deadline,
            "p_project_id": withdrawal_vault.COMMUNITY_PROJECT_ID,
            "p_chain_id": withdrawal_vault.CELO_CHAIN_ID,
            "p_vault_address": runtime.vault_address,
            "p_token_address": runtime.token_address,
            "p_onchain_daily_remaining": str(daily_remaining),
        })
        row = prepared.get("withdrawal") if isinstance(prepared.get("withdrawal"), dict) else prepared
        if not row.get("withdrawal_id"):
            raise RuntimeError("Withdrawal reservation did not return its persisted row")
        if row.get("status") not in {"pending", "claimed"}:
            raise RuntimeError("Withdrawal reservation is not open")
        if not prepared.get("created", True):
            if row.get("tx_hash"):
                return jsonify({"success": False, "status": "transaction_submitted",
                                "withdrawal_id": row["withdrawal_id"], "tx_hash": row["tx_hash"],
                                "error": "A transaction is already linked to this withdrawal."}), 409
            if int(runtime.vault.functions.nonces(member).call()) != int(row.get("authorization_nonce") or 0):
                return jsonify({"success": False, "status": "reconciliation_required",
                                "withdrawal_id": row["withdrawal_id"],
                                "error": "The on-chain withdrawal nonce changed. Reconcile the existing request before retrying."}), 409
            if int(row.get("authorization_deadline") or 0) <= chain_now:
                return jsonify({"success": False, "status": "expired",
                                "withdrawal_id": row["withdrawal_id"],
                                "error": "The existing authorization expired. Retry after the request is safely released."}), 409
        return jsonify(_sign_reserved_withdrawal(db, runtime, row))
    except withdrawal_vault.WithdrawalConfigError as exc:
        logger.warning("community withdrawal authorization unavailable: %s",
                       withdrawal_vault.operator_detail(exc))
        return jsonify({"success": False, "error": withdrawal_vault.MEMBER_CONFIG_ERROR}), 503
    except withdrawal_vault.WithdrawalPolicyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001
        logger.error("community withdrawal authorization failed: %s", exc)
        return jsonify({"success": False, "error": "Could not prepare a Community withdrawal. No payout was submitted."}), 503


@community_feed_bp.route("/api/rewards/withdrawals/confirm", methods=["POST"])
def api_withdrawal_confirm():
    """Verify the member's exact vault transaction before finalizing its ledger debit."""
    if not _withdrawal_origin_ok():
        return jsonify({"success": False, "error": "Cross-origin withdrawal request rejected"}), 403
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    body = _json_body()
    withdrawal_id = str(body.get("withdrawal_id") or "").strip().lower()
    tx_hash = str(body.get("tx_hash") or "").strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{64}", withdrawal_id) or not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash):
        return jsonify({"success": False, "error": "A valid withdrawal ID and transaction hash are required."}), 400
    retry = _throttle("withdraw-confirm:" + wallet, 60, allow=10)
    if retry:
        return jsonify({"success": False, "error": "Too many confirmation checks. Please wait and retry."}), 429
    try:
        from web3 import Web3

        runtime = withdrawal_vault.build_runtime(require_enabled=False, require_signer_key=False)
        db = _withdrawal_admin_db()
        result = (db.table("community_reward_withdrawals").select("*")
                  .eq("withdrawal_id", withdrawal_id)
                  .ilike("wallet_address", wallet.lower()).limit(1).execute())
        rows = _withdrawal_rows(result)
        if not rows:
            return jsonify({"success": False, "error": "Community withdrawal not found."}), 404
        row = rows[0]
        if row.get("status") == "completed":
            if str(row.get("tx_hash") or "").lower() != tx_hash:
                return jsonify({"success": False, "error": "Completed withdrawal transaction hash does not match."}), 409
            return jsonify({"success": True, "status": "completed", "tx_hash": row.get("tx_hash")})
        if row.get("status") != "claimed" or not row.get("authorization_signature"):
            return jsonify({"success": False, "error": "Community withdrawal is not authorized."}), 409
        if row.get("tx_hash") and str(row["tx_hash"]).lower() != tx_hash:
            return jsonify({"success": False, "error": "A different transaction is already linked to this withdrawal."}), 409
        try:
            tx = runtime.web3.eth.get_transaction(tx_hash)
        except Exception:
            return jsonify({"success": True, "status": "pending", "tx_hash": tx_hash,
                            "message": "Transaction is not visible on Celo yet. Keep this page open or retry confirmation."}), 202
        withdrawal_vault.decode_withdrawal_transaction(runtime, tx, row)
        if not row.get("tx_hash"):
            _withdrawal_rpc(db, "community_reward_withdrawal_set_tx", {
                "p_withdrawal_id": withdrawal_id, "p_tx_hash": tx_hash,
            })
            row["tx_hash"] = tx_hash
        try:
            receipt = runtime.web3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return jsonify({"success": True, "status": "pending", "tx_hash": tx_hash,
                            "message": "Transaction is awaiting a Celo receipt."}), 202
        status = int(receipt.get("status", 0))
        if status != 1:
            _withdrawal_rpc(db, "community_reward_withdrawal_fail", {
                "p_withdrawal_id": withdrawal_id,
                "p_reason": "Member transaction reverted on Celo",
                "p_tx_hash": tx_hash,
            })
            return jsonify({"success": False, "status": "failed", "tx_hash": tx_hash,
                            "error": "The withdrawal transaction reverted. Your Community ledger balance was not debited."}), 409
        if not withdrawal_vault.verify_withdrawal_receipt(runtime, receipt, row):
            return jsonify({"success": False, "status": "reconciliation_required",
                            "tx_hash": tx_hash,
                            "error": "The successful receipt did not contain the exact expected vault payout event. The ledger was not changed; support review is required."}), 409
        confirmations = int(runtime.web3.eth.block_number) - int(receipt["blockNumber"]) + 1
        if confirmations < runtime.confirmations:
            return jsonify({"success": True, "status": "confirming", "tx_hash": tx_hash,
                            "confirmations": confirmations,
                            "required_confirmations": runtime.confirmations}), 202
        completed = _withdrawal_rpc(db, "community_reward_withdrawal_finalize", {
            "p_withdrawal_id": withdrawal_id, "p_tx_hash": tx_hash,
        })
        return jsonify({"success": True, "status": "completed", "tx_hash": tx_hash,
                        "amount_gd": str(completed.get("amount_gd") or row.get("amount_gd"))})
    except withdrawal_vault.WithdrawalConfigError as exc:
        logger.warning("community withdrawal receipt verification unavailable: %s",
                       withdrawal_vault.operator_detail(exc))
        return jsonify({"success": False, "error": withdrawal_vault.MEMBER_CONFIG_ERROR}), 503
    except withdrawal_vault.WithdrawalPolicyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.error("community withdrawal confirmation failed: %s", exc)
        return jsonify({"success": False, "error": "Could not verify the transaction. The withdrawal remains open for safe reconciliation."}), 503


@community_feed_bp.route("/api/admin/withdrawal-vault/config", methods=["GET"])
def api_admin_withdrawal_vault_config():
    """Public vault addresses and pooled balance for dashboard-authorized funders."""
    if not _admin_wallet():
        return jsonify({"success": False, "error": "Admin access required"}), 403
    try:
        runtime = withdrawal_vault.build_runtime(require_enabled=False, require_signer_key=False)
        balance_base = int(runtime.token.functions.balanceOf(runtime.vault_address).call())
        return jsonify({
            **withdrawal_vault.public_config(runtime),
            "withdrawal_enabled": withdrawal_vault.feature_enabled(),
            "vault_balance_base_units": str(balance_base),
            "vault_balance_gd": str(withdrawal_vault.base_units_to_gd(balance_base, runtime.decimals)),
            "paused": bool(runtime.vault.functions.paused().call()),
        })
    except withdrawal_vault.WithdrawalConfigError as exc:
        logger.warning("community vault funding config read failed: %s",
                       withdrawal_vault.operator_detail(exc))
        return jsonify({"success": False, "enabled": False,
                        "error": "Community vault funding is unavailable until runtime configuration is complete.",
                        "detail": withdrawal_vault.operator_detail(exc),
                        "code": getattr(exc, "code", "unclassified")}), 503
    except Exception as exc:  # noqa: BLE001
        logger.warning("community vault funding config read failed: %s", exc)
        return jsonify({"success": False, "error": "Could not verify the Community vault on Celo."}), 503


@community_feed_bp.route("/api/admin/withdrawal-vault/fund/confirm", methods=["POST"])
def api_admin_withdrawal_vault_fund_confirm():
    """Verify and idempotently record a G$ transfer signed by the admin wallet."""
    if not _withdrawal_origin_ok():
        return jsonify({"success": False, "error": "Cross-origin funding confirmation rejected"}), 403
    admin_wallet = _admin_wallet()
    if not admin_wallet:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    tx_hash = str(_json_body().get("tx_hash") or "").strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash):
        return jsonify({"success": False, "error": "A valid transaction hash is required."}), 400
    try:
        runtime = withdrawal_vault.build_runtime(require_enabled=False, require_signer_key=False)
        try:
            tx = runtime.web3.eth.get_transaction(tx_hash)
        except Exception:
            return jsonify({"success": True, "status": "pending", "tx_hash": tx_hash}), 202
        try:
            receipt = runtime.web3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return jsonify({"success": True, "status": "pending", "tx_hash": tx_hash}), 202
        amount_base = withdrawal_vault.verify_funding_receipt(runtime, receipt, tx, admin_wallet)
        confirmations = int(runtime.web3.eth.block_number) - int(receipt["blockNumber"]) + 1
        if confirmations < runtime.confirmations:
            return jsonify({"success": True, "status": "confirming", "tx_hash": tx_hash,
                            "confirmations": confirmations,
                            "required_confirmations": runtime.confirmations}), 202
        db = _withdrawal_admin_db()
        recorded = _withdrawal_rpc(db, "community_reward_vault_funding_record", {
            "p_funder_wallet": admin_wallet.lower(),
            "p_amount_gd": str(withdrawal_vault.base_units_to_gd(amount_base, runtime.decimals)),
            "p_amount_base_units": str(amount_base),
            "p_tx_hash": tx_hash,
            "p_block_number": int(receipt["blockNumber"]),
            "p_token_address": runtime.token_address,
            "p_vault_address": runtime.vault_address,
        })
        return jsonify({"success": True, "status": "confirmed", "tx_hash": tx_hash,
                        "amount_gd": str(recorded.get("amount_gd") or withdrawal_vault.base_units_to_gd(amount_base, runtime.decimals))})
    except withdrawal_vault.WithdrawalConfigError as exc:
        logger.warning("community vault funding verification unavailable: %s",
                       withdrawal_vault.operator_detail(exc))
        return jsonify({"success": False,
                        "error": "Vault configuration is unavailable for funding verification.",
                        "detail": withdrawal_vault.operator_detail(exc),
                        "code": getattr(exc, "code", "unclassified")}), 503
    except withdrawal_vault.WithdrawalPolicyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.error("community vault funding confirmation failed: %s", exc)
        return jsonify({"success": False, "error": "Could not verify or record this funding transaction. The on-chain transfer, if successful, remains in the vault."}), 503


@community_feed_bp.route("/api/vault-donations/config", methods=["GET"])
def api_vault_donations_config():
    """Expose only runtime-verified network, token and recipient values."""
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return _private_withdrawal_json({"success": False, "error": "Not authenticated"}, 401)
    try:
        return _private_withdrawal_json(vault_donations.public_config(wallet))
    except withdrawal_vault.WithdrawalConfigError as exc:
        return _private_withdrawal_json({
            "success": False,
            "error": "Community Vault donations are unavailable until the shared withdrawal vault is configured and enabled.",
            "code": exc.code,
        }, 503)
    except withdrawal_vault.WithdrawalPolicyError as exc:
        return _private_withdrawal_json({"success": False, "error": str(exc), "code": "vault_unavailable"}, 503)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community vault donation config read failed: %s", exc)
        return _private_withdrawal_json({"success": False, "error": "Could not verify the Community Vault on Celo."}, 503)


@community_feed_bp.route("/api/vault-donations/intent", methods=["POST"])
def api_vault_donation_intent():
    if not svc.is_enabled():
        return _disabled_response()
    if not _withdrawal_origin_ok():
        return _private_withdrawal_json({"success": False, "error": "Cross-origin donation request rejected"}, 403)
    wallet = _session_wallet()
    if not wallet:
        return _private_withdrawal_json({"success": False, "error": "Not authenticated"}, 401)
    wait = _throttle(f"vault-donation-intent:{wallet}", 10)
    if wait:
        return _private_withdrawal_json({"success": False, "error": "Please wait before preparing another donation.", "retry_after": wait}, 429)
    result = vault_donations.create_intent(wallet, _json_body())
    status = 200 if result.get("success") else (503 if result.get("code") in {"db", "profile", "donations_disabled", "vault_unavailable"} else 400)
    return _private_withdrawal_json(result, status)


@community_feed_bp.route("/api/vault-donations/confirm", methods=["POST"])
def api_vault_donation_confirm():
    if not svc.is_enabled():
        return _disabled_response()
    if not _withdrawal_origin_ok():
        return _private_withdrawal_json({"success": False, "error": "Cross-origin donation confirmation rejected"}, 403)
    wallet = _session_wallet()
    if not wallet:
        return _private_withdrawal_json({"success": False, "error": "Not authenticated"}, 401)
    body = _json_body()
    result = vault_donations.confirm_transaction(wallet, body.get("donation_id"), body.get("tx_hash"))
    if result.get("success") and result.get("status") in {"pending", "confirming"}:
        return _private_withdrawal_json(result, 202)
    if result.get("status") == "processing" or result.get("retryable"):
        return _private_withdrawal_json(result, 503)
    status = 200 if result.get("success") else (503 if result.get("code") in {"unavailable", "vault_unavailable"} else 400)
    return _private_withdrawal_json(result, status)


@community_feed_bp.route("/api/vault-donations/cancel", methods=["POST"])
def api_vault_donation_cancel():
    if not svc.is_enabled():
        return _disabled_response()
    if not _withdrawal_origin_ok():
        return _private_withdrawal_json({"success": False, "error": "Cross-origin donation cancellation rejected"}, 403)
    wallet = _session_wallet()
    if not wallet:
        return _private_withdrawal_json({"success": False, "error": "Not authenticated"}, 401)
    result = vault_donations.cancel_intent(wallet, _json_body().get("donation_id"))
    return _private_withdrawal_json(result, 200 if result.get("success") else 400)


@community_feed_bp.route("/api/daily-learn", methods=["GET"])
def api_daily_learn():
    """Return today's server-published two-question snapshot.

    This endpoint is intentionally independent from the full Learn & Earn page
    maintenance flag. The snapshot service owns the Community kill switch and
    strips answer keys before the payload reaches the browser.
    """
    if not svc.is_enabled():
        return _disabled_response()
    from .daily_learn import get_daily_snapshot, public_snapshot
    from .daily_learn_attempts import get_attempt

    result = public_snapshot(get_daily_snapshot())
    if not result.get("success"):
        return jsonify(result), 503
    wallet = _session_wallet()
    today = datetime.now(timezone.utc).date().isoformat()
    result["day_utc"] = today
    result["snapshot_status"] = result.get("status", "published")
    result["total_questions"] = 2
    result["completed_questions"] = 0
    result["score"] = 0
    if wallet:
        attempt = get_attempt(wallet, today)
        if not attempt.get("success"):
            return jsonify({"success": False, "status": "unavailable",
                            "message": "Daily Learn is temporarily unavailable."}), 503
        result.update({
            "status": attempt.get("status", "available"),
            "completed_questions": int(attempt.get("completed_questions") or 0),
            "score": int(attempt.get("score") or 0),
        })
        if result["status"] == "completed":
            # A completed GET is also a safe repair path when the answer was
            # saved but the best-effort credit hook failed or the process died.
            result["reward"] = rewards.daily_learn_completion_status(wallet, today)
            result["questions"] = []
        else:
            position = min(result["completed_questions"], 1)
            result["questions"] = (result.get("questions") or [])[position:position + 1]
    else:
        result["status"] = "available"
        result["questions"] = (result.get("questions") or [])[:1]
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    result["reset_at"] = datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    return jsonify(result)


@community_feed_bp.route("/api/cron/daily-learn-reconcile", methods=["GET"])
def api_daily_learn_reconcile_cron():
    """Vercel Cron entry point; never run without the configured shared secret."""
    secret = os.getenv("CRON_SECRET", "")
    authorization = request.headers.get("Authorization", "")
    if not secret or not hmac.compare_digest(authorization, f"Bearer {secret}"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    if not rewards.is_enabled():
        return jsonify({"success": True, "skipped": "rewards_disabled"})
    try:
        result = rewards.reconcile_uncredited_daily_learn_attempts(batch_size=10)
        if result.get("error"):
            return jsonify({"success": False, "result": result}), 503
        return jsonify({"success": True, "result": result})
    except Exception as exc:  # noqa: BLE001 - the next scheduled run retries
        logger.exception("Community Daily Learn cron reconciliation failed: %s", exc)
        return jsonify({"success": False, "error": "Reconciliation failed"}), 503


@community_feed_bp.route("/api/daily-learn/answer", methods=["POST"])
def api_daily_learn_answer():
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Connect a verified wallet to answer."}), 401
    wait = _throttle(f"daily-learn:{wallet}", 60, allow=10)
    if wait:
        return jsonify({"success": False, "error": "Please wait before submitting another answer.",
                        "code": "rate_limited", "retry_after": wait}), 429
    if not rewards.is_eligible(wallet):
        return jsonify({"success": False, "error": "A verified wallet is required to submit Daily Learn answers.",
                        "code": "verified_wallet_required"}), 403
    body = _json_body()
    from .daily_learn_attempts import submit_answer

    result = submit_answer(
        wallet, body.get("day_utc"), body.get("question_id"), body.get("answer"),
        skipped=body.get("skipped") is True)
    if result.get("success") and result.get("status") == "completed":
        # Both hooks are idempotent/best-effort. Replaying the final answer is a
        # safe repair path if a prior process died after saving completion.
        result["reward"] = rewards.daily_learn_completion_status(
            wallet, result.get("day_utc"))
        try:
            from . import badges
            badges.on_daily_learn_completed(wallet, result.get("day_utc"))
        except Exception as exc:  # noqa: BLE001
            logger.info("Daily Learn badge evaluation skipped: %s", exc)
    if result.get("success"):
        return jsonify(result)
    status = 503 if result.get("status") == "unavailable" else (
        401 if result.get("code") == "invalid_wallet" else 400)
    return jsonify(result), status


@community_feed_bp.route("/api/daily-prompt", methods=["GET"])
def api_daily_prompt():
    """Return today's social prompt; this is separate from Daily Learn."""
    if not svc.is_enabled():
        return _disabled_response()
    from .daily_prompts import get_active_prompt

    result = get_active_prompt(_session_wallet())
    return jsonify(result), (200 if result.get("success") else 503)


@community_feed_bp.route("/api/admin/daily-prompts", methods=["GET"])
def api_admin_list_daily_prompts():
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    from .daily_prompts import list_prompts

    result = list_prompts(
        prompt_date=request.args.get("prompt_date"),
        status=request.args.get("status"),
        limit=request.args.get("limit", 30),
    )
    return jsonify(result), (200 if result.get("success") else 503)


@community_feed_bp.route("/api/admin/daily-prompts", methods=["POST"])
def api_admin_create_daily_prompt():
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    from .daily_prompts import create_prompt

    result = create_prompt(_json_body(), admin)
    if result.get("success"):
        prompt = result.get("prompt") or {}
        _log_admin_action(admin, "community_daily_prompt_create", {
            "prompt_id": prompt.get("id"), "prompt_date": prompt.get("prompt_date"),
            "status": prompt.get("status"),
        })
    return jsonify(result), (200 if result.get("success") else 400)


@community_feed_bp.route("/api/admin/daily-prompts/<prompt_id>", methods=["PATCH"])
def api_admin_update_daily_prompt(prompt_id: str):
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    from .daily_prompts import update_prompt

    result = update_prompt(prompt_id, _json_body(), admin)
    if result.get("success"):
        prompt = result.get("prompt") or {}
        _log_admin_action(admin, "community_daily_prompt_update", {
            "prompt_id": prompt_id, "prompt_date": prompt.get("prompt_date"),
            "status": prompt.get("status"),
        })
    return jsonify(result), (200 if result.get("success") else 400)


@community_feed_bp.route("/api/admin/daily-prompts/<prompt_id>/archive", methods=["POST"])
def api_admin_archive_daily_prompt(prompt_id: str):
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    from .daily_prompts import archive_prompt

    result = archive_prompt(prompt_id, admin)
    if result.get("success"):
        _log_admin_action(admin, "community_daily_prompt_archive", {"prompt_id": prompt_id})
    return jsonify(result), (200 if result.get("success") else 400)


@community_feed_bp.route("/api/admin/reward-rates", methods=["GET"])
def api_admin_reward_rates():
    """Return one authoritative flat Community reward configuration."""
    if not _admin_wallet():
        return jsonify({"success": False, "error": "Admin access required"}), 403
    result = reward_settings.get_settings()
    return jsonify(result), (200 if result.get("success") else 503)


@community_feed_bp.route("/api/admin/reward-rates", methods=["POST"])
def api_admin_set_reward_rates():
    """Atomically update rates/caps through the SQL settings RPC."""
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    data = request.get_json(silent=True) or {}
    patch = {}
    if isinstance(data.get("rates"), dict):
        patch["rates"] = data["rates"]
    for key in ("daily_cap", "post_cap", "daily_learn_rate",
                "daily_learn_rate_max", "daily_learn_cap"):
        if key in data:
            patch[key] = data[key]
    # Flat rate keys remain accepted for older admin forms.
    for key in rewards.RATE_NAMES:
        if key in data:
            patch[key] = data[key]
    expected_version = data.get("config_version", data.get("expected_version"))
    result = reward_settings.update_settings(
        patch, actor=admin, reason=data.get("reason") or "",
        expected_version=expected_version)
    if not result.get("success"):
        return jsonify(result), 400
    return jsonify(result)


@community_feed_bp.route("/api/feed", methods=["GET"])
def api_feed():
    if not svc.is_enabled():
        return _disabled_response()
    viewer = _session_wallet()
    cursor = request.args.get("cursor", type=int)
    limit = request.args.get("limit", type=int)
    result = svc.get_feed(
        viewer=viewer, cursor=cursor, limit=limit,
        prompt_id=request.args.get("prompt_id") or None)
    if result.get("success") and cursor is None:
        pinned = svc.get_pinned_donation_feed(viewer=viewer, limit=10)
        result["pinned_posts"] = pinned.get("posts", []) if pinned.get("success") else []
        result["pinned_next_cursor"] = pinned.get("next_cursor") if pinned.get("success") else None
    return jsonify(result)


@community_feed_bp.route("/api/feed/pinned-donations", methods=["GET"])
def api_pinned_donations():
    if not svc.is_enabled():
        return _disabled_response()
    result = svc.get_pinned_donation_feed(
        viewer=_session_wallet(),
        cursor=request.args.get("cursor", type=int),
        limit=request.args.get("limit", type=int),
    )
    return jsonify(result), (200 if result.get("success") else 503)


@community_feed_bp.route("/api/users/search", methods=["GET"])
def api_search_users():
    """Public member search by username (read-only, guests allowed).

    Same contract as the feed: reading is public, and the response carries
    usernames only — never a wallet. A guest may search and then open a public
    profile; reacting/commenting still raises the connect card.
    """
    if not svc.is_enabled():
        return _disabled_response()
    result = svc.search_users(
        request.args.get("q"),
        limit=request.args.get("limit", type=int),
    )
    return jsonify(result), (200 if result.get("success") else 400)


# ── Public profile ───────────────────────────────────────────────────────────
# A member's profile is addressed by USERNAME, never by wallet. The page and
# every API below strip the wallet in the service layer; the template/bundle
# only ever sees a username.

@community_feed_bp.route("/u/<username>")
def community_feed_profile(username: str):
    """Public profile for a member: their posts + their media albums.

    Guests may view, exactly like the feed itself (the shared connect card is
    rendered for them). A username with no member behind it renders the
    not-found state with a 404, so a link never lands on a broken page.
    """
    if not svc.is_enabled():
        wallet = _session_wallet()
        return redirect("/wallet" if wallet else "/")

    wallet = _session_wallet()
    viewer = svc.get_username(wallet) if wallet else None
    result = svc.get_public_profile(username, viewer=wallet)
    profile = result.get("profile") if result.get("success") else None
    return render_template(
        "community_profile.html",
        profile=profile,
        wallet=wallet,
        username=svc.normalize_username(username),
        viewer_username=viewer,
        is_guest=not bool(wallet),
        display_name=svc.display_name(wallet) if wallet else None,
        login_method=session.get("login_method", ""),
        walletconnect_project_id=os.getenv("WALLETCONNECT_PROJECT_ID", ""),
        privy_app_id=os.getenv("PRIVY_APP_ID", ""),
        privy_client_id=os.getenv("PRIVY_CLIENT_ID", ""),
        max_avatar_mb=svc.MAX_AVATAR_MB,
        storage_ready=storage.storage_available(),
    ), (200 if profile else 404)


@community_feed_bp.route("/api/profile/<username>", methods=["GET"])
def api_profile(username: str):
    if not svc.is_enabled():
        return _disabled_response()
    result = svc.get_public_profile(username, viewer=_session_wallet())
    return jsonify(result), (200 if result.get("success") else 404)


@community_feed_bp.route("/api/profile/<username>/badges", methods=["GET"])
def api_profile_badges(username: str):
    if not svc.is_enabled():
        return _disabled_response()
    normalized = svc.normalize_username(username)
    wallet = svc.wallet_for_username(normalized)
    if not wallet:
        return jsonify({"success": False, "badges": []}), 404
    from . import badges
    return jsonify({"success": True, "badges": badges.public_badges_for_wallet(wallet)})


@community_feed_bp.route("/api/me/badges", methods=["GET"])
def api_my_badges():
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    from . import badges
    return jsonify({"success": True, "badges": badges.public_badges_for_wallet(wallet)})


@community_feed_bp.route("/api/profile/<username>/avatar", methods=["GET"])
def api_profile_avatar(username: str):
    """Serve one member's intentionally public avatar through the app server.

    The browser addresses the image by username; wallet resolution, the avatar
    row and its R2 key stay server-side. The validator includes R2's current
    object ETag, so even an overwrite of the same key invalidates cached bytes.
    """
    if not svc.is_enabled():
        return _disabled_response()
    normalized = svc.normalize_username(username)
    wallet = svc.wallet_for_username(normalized) if normalized else None
    if not wallet:
        return Response(status=404, headers={"Cache-Control": "no-store"})
    avatar = svc.get_avatar_descriptor(wallet)
    if not avatar:
        return Response(status=404, headers={"Cache-Control": "no-store"})

    etag = storage.avatar_etag(avatar)
    headers = {"X-Content-Type-Options": "nosniff"}
    if etag:
        headers.update({
            "Cache-Control": (
                "public, max-age=30, s-maxage=60, stale-while-revalidate=30"),
            "ETag": f'"{etag}"',
        })
        if request.if_none_match.contains(etag):
            return Response(status=304, headers=headers)
    else:
        # Without an upstream version, serve this response but never let a
        # browser/proxy keep bytes that cannot be validated on the next request.
        headers["Cache-Control"] = "no-store"

    image = storage.read_avatar(avatar)
    if not image:
        return Response(status=404, headers={"Cache-Control": "no-store"})
    return Response(image["body"], mimetype=image["mime"], headers=headers)


@community_feed_bp.route("/api/users/<username>/follow", methods=["POST"])
def api_follow(username: str):
    """Follow a member. A write, so a guest gets the auth refusal (the browser
    also raises the connect card). Idempotent: re-following is a success."""
    if not svc.is_enabled():
        return _disabled_response()
    result = svc.follow_user(_session_wallet(), username)
    return jsonify(result), (200 if result.get("success") else 400)


@community_feed_bp.route("/api/users/<username>/follow", methods=["DELETE"])
def api_unfollow(username: str):
    """Stop following a member. Idempotent, like the follow."""
    if not svc.is_enabled():
        return _disabled_response()
    result = svc.unfollow_user(_session_wallet(), username)
    return jsonify(result), (200 if result.get("success") else 400)


@community_feed_bp.route("/api/profile/<username>/tip/prepare", methods=["POST"])
def api_profile_tip_prepare(username: str):
    """Prepare a non-custodial Celo transfer to this username."""
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    wait = _throttle(f"profile-tip:{wallet}", 10)
    if wait:
        return jsonify({"success": False, "error": "Please wait before preparing another transfer.",
                        "code": "rate_limited", "retry_after": wait}), 429
    from . import profile_tips
    body = _json_body()
    result = profile_tips.prepare(wallet, username, body.get("token"), body.get("amount"))
    return jsonify(result), (200 if result.get("success") else 400)

@community_feed_bp.route("/api/profile/<username>/tip/confirm", methods=["POST"])
def api_profile_tip_confirm(username: str):
    """Verify and record a signed Community Profile transfer."""
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    wait = _throttle(f"profile-tip-confirm:{wallet}", 10)
    if wait:
        return jsonify({"success": False, "error": "Please wait before confirming another transfer.",
                        "code": "rate_limited", "retry_after": wait}), 429
    from . import profile_tips
    body = _json_body()
    result = profile_tips.record(wallet, username, body.get("token"), body.get("amount"), body.get("tx_hash"))
    return jsonify(result), (200 if result.get("success") else 400)

@community_feed_bp.route("/api/profile/<username>/posts", methods=["GET"])
def api_profile_posts(username: str):
    if not svc.is_enabled():
        return _disabled_response()
    result = svc.get_user_posts(
        username, viewer=_session_wallet(),
        cursor=request.args.get("cursor", type=int),
        limit=request.args.get("limit", type=int))
    return jsonify(result), (200 if result.get("success") else 404)


@community_feed_bp.route("/api/profile/<username>/album", methods=["GET"])
def api_profile_album(username: str):
    if not svc.is_enabled():
        return _disabled_response()
    result = svc.get_user_album(
        username, viewer=_session_wallet(),
        cursor=request.args.get("cursor", type=int),
        limit=request.args.get("limit", type=int))
    return jsonify(result), (200 if result.get("success") else 404)


@community_feed_bp.route("/api/posts/<int:post_id>", methods=["GET"])
def api_get_post(post_id: int):
    if not svc.is_enabled():
        return _disabled_response()
    result = svc.get_post(post_id, viewer=_session_wallet())
    return jsonify(result), (200 if result.get("success") else 404)


@community_feed_bp.route("/api/posts", methods=["POST"])
def api_create_post():
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    wait = _throttle(f"post:{wallet}", svc.POST_RATE_LIMIT_SECONDS)
    if wait:
        return jsonify({
            "success": False,
            "code": "rate_limited",
            "error": "Please wait before posting again.",
            "retry_after": wait,
        }), 429

    body = _json_body()
    raw_items = body.get("media") or []
    if not isinstance(raw_items, list):
        return jsonify({"success": False, "error": "Invalid media list."}), 400
    if len(raw_items) > svc.MAX_MEDIA_PER_POST:
        return jsonify({
            "success": False,
            "code": "too_many_media",
            "error": f"You can attach up to {svc.MAX_MEDIA_PER_POST} files.",
        }), 400

    # Every object is re-verified server-side: the browser uploaded it directly,
    # so a client-supplied URL is never trusted.
    verified: list[dict] = []
    for item in raw_items:
        ok, error, clean = storage.verify_upload(wallet, item)
        if not ok:
            return jsonify({"success": False, "code": "media", "error": error}), 400
        verified.append(clean)

    prompt_id = body.get("prompt_id")
    if prompt_id is not None and (not isinstance(prompt_id, str) or len(prompt_id) > 64):
        return jsonify({"success": False, "error": "Invalid prompt reference."}), 400
    result = svc.create_post(wallet, body.get("caption"), verified, prompt_id=prompt_id)
    return jsonify(result), (200 if result.get("success") else 400)


@community_feed_bp.route("/api/posts/<int:post_id>", methods=["PATCH"])
def api_update_post(post_id: int):
    """Edit the caption only. Media is never editable (see the proposal)."""
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    result = svc.update_caption(wallet, post_id, _json_body().get("caption"))
    if result.get("success"):
        return jsonify(result)
    code = result.get("code")
    status = {"not_found": 404, "forbidden": 403}.get(code, 400)
    return jsonify(result), status


@community_feed_bp.route("/api/posts/<int:post_id>", methods=["DELETE"])
def api_delete_post(post_id: int):
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    result = svc.delete_post(wallet, post_id)
    if result.get("success"):
        return jsonify(result)
    status = {"not_found": 404, "forbidden": 403}.get(result.get("code"), 400)
    return jsonify(result), status


# ── Reactions ────────────────────────────────────────────────────────────────

@community_feed_bp.route("/api/posts/<int:post_id>/reaction", methods=["POST"])
def api_set_reaction(post_id: int):
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    wait = _throttle(f"reaction:{wallet}", 30, allow=30)
    if wait:
        return jsonify({"success": False, "error": "Please wait before reacting again.",
                        "code": "rate_limited", "retry_after": wait}), 429
    result = svc.set_reaction(wallet, post_id, _json_body().get("reaction"))
    if result.get("success"):
        return jsonify(result)
    status = {"not_found": 404, "banned": 403}.get(result.get("code"), 400)
    return jsonify(result), status


@community_feed_bp.route("/api/posts/<int:post_id>/reaction", methods=["DELETE"])
def api_clear_reaction(post_id: int):
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    wait = _throttle(f"reaction:{wallet}", 30, allow=30)
    if wait:
        return jsonify({"success": False, "error": "Please wait before reacting again.",
                        "code": "rate_limited", "retry_after": wait}), 429
    result = svc.clear_reaction(wallet, post_id)
    status = 200 if result.get("success") else (
        404 if result.get("code") == "not_found" else 400)
    return jsonify(result), status


# ── Comments ─────────────────────────────────────────────────────────────────

@community_feed_bp.route("/api/posts/<int:post_id>/comments", methods=["GET"])
def api_get_comments(post_id: int):
    if not svc.is_enabled():
        return _disabled_response()
    result = svc.get_comments(post_id, viewer=_session_wallet())
    return jsonify(result), (404 if result.get("code") == "not_found" else 200)


@community_feed_bp.route("/api/posts/<int:post_id>/comments", methods=["POST"])
def api_add_comment(post_id: int):
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    wait = _throttle(f"comment:{wallet}", svc.COMMENT_RATE_LIMIT_SECONDS)
    if wait:
        return jsonify({
            "success": False,
            "code": "rate_limited",
            "error": "Please wait before commenting again.",
            "retry_after": wait,
        }), 429

    data = _json_body()
    result = svc.add_comment(
        wallet, post_id, data.get("body"),
        parent_comment_id=data.get("parent_comment_id"),
    )
    if result.get("success"):
        return jsonify(result)
    status = {"not_found": 404, "parent_not_found": 404,
              "banned": 403}.get(result.get("code"), 400)
    return jsonify(result), status


@community_feed_bp.route("/api/comments/<int:comment_id>", methods=["DELETE"])
def api_delete_comment(comment_id: int):
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    result = svc.delete_comment(wallet, comment_id)
    status = 200 if result.get("success") else (
        404 if result.get("code") == "not_found" else
        403 if result.get("code") == "forbidden" else 400)
    return jsonify(result), status


# ── Reports ──────────────────────────────────────────────────────────────────

@community_feed_bp.route("/api/report", methods=["POST"])
def api_report():
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    body = _json_body()
    post_id = body.get("post_id")
    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "A post is required."}), 400
    result = svc.report_post(wallet, post_id, body.get("reason"))
    status = 200 if result.get("success") else (
        404 if result.get("code") == "not_found" else 400)
    return jsonify(result), status


# ── Profile picture (avatar) ─────────────────────────────────────────────────

@community_feed_bp.route("/api/avatar/presign", methods=["POST"])
def api_presign_avatar():
    """Hand the browser a signed PUT for a profile picture (images only).

    Same browser -> R2 flow as a post's media, with its own (tighter) image cap
    and its own throttle key, so a burst of avatar uploads cannot exhaust the
    post-media allowance.
    """
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    if svc.is_banned(wallet):
        return jsonify({"success": False, "code": "banned",
                        "error": "You cannot post in the community."}), 403
    if not svc.normalize_username(svc.get_username(wallet)):
        return jsonify({
            "success": False,
            "code": "username_required",
            "error": "Choose a community username before uploading a profile picture.",
        }), 400

    wait = _throttle(f"avatar:{wallet}", svc.AVATAR_RATE_LIMIT_SECONDS)
    if wait:
        return jsonify({
            "success": False,
            "code": "rate_limited",
            "error": "Please wait before uploading a new picture.",
            "retry_after": wait,
        }), 429

    body = _json_body()
    result = storage.sign_avatar_upload(
        wallet, body.get("content_type"), body.get("bytes"))
    if not result.get("success"):
        status = 503 if result.get("code") == "storage_unconfigured" else 400
        return jsonify(result), status
    return jsonify(result)


@community_feed_bp.route("/api/avatar", methods=["POST"])
def api_set_avatar():
    """Save a profile picture from an already-uploaded, verified object.

    The object is re-verified server-side (the browser uploaded it directly), so
    a client-supplied URL is never trusted.
    """
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    if not svc.normalize_username(svc.get_username(wallet)):
        return jsonify({
            "success": False,
            "code": "username_required",
            "error": "Choose a community username before uploading a profile picture.",
        }), 400

    ok, error, clean = storage.verify_avatar(wallet, _json_body())
    if not ok:
        return jsonify({"success": False, "code": "avatar", "error": error}), 400
    result = svc.set_avatar(wallet, clean)
    return jsonify(result), (200 if result.get("success") else 400)


@community_feed_bp.route("/api/avatar", methods=["DELETE"])
def api_clear_avatar():
    """Remove the signed-in member's profile picture."""
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    result = svc.clear_avatar(wallet)
    return jsonify(result), (200 if result.get("success") else 400)


# ── Upload ───────────────────────────────────────────────────────────────────

@community_feed_bp.route("/api/upload/presign", methods=["POST"])
def api_presign_upload():
    """Hand the browser a signed PUT so it uploads straight to R2.

    No bytes pass through Flask: with gthread + 4 workers a proxied 100MB video
    would hold one of ~16 slots for the whole upload.
    """
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    if not wallet:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    if svc.is_banned(wallet):
        return jsonify({"success": False, "code": "banned",
                        "error": "You cannot post in the community."}), 403

    wait = _throttle(f"upload:{wallet}", svc.UPLOAD_RATE_LIMIT_SECONDS,
                     allow=svc.UPLOAD_BURST_ALLOWANCE)
    if wait:
        return jsonify({
            "success": False,
            "code": "rate_limited",
            "error": "Please wait before uploading again.",
            "retry_after": wait,
        }), 429

    body = _json_body()
    result = storage.sign_upload(
        wallet, body.get("content_type"), body.get("bytes"))
    if not result.get("success"):
        status = 400
        if result.get("code") == "storage_unconfigured":
            status = 503
        return jsonify(result), status
    return jsonify(result)


# ── Admin ────────────────────────────────────────────────────────────────────

@community_feed_bp.route("/api/admin/overview", methods=["GET"])
def api_admin_overview():
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    status = request.args.get("status", "open")
    stats = svc.admin_stats()
    reports = svc.admin_list_reports(status=status)
    return jsonify({
        "success": True,
        "stats": stats,
        "reports": reports.get("reports", []),
        "storage_ready": storage.storage_available(),
    })


@community_feed_bp.route("/api/admin/posts/<int:post_id>/hide", methods=["POST"])
def api_admin_hide(post_id: int):
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    hidden = bool(_json_body().get("hidden", True))
    result = svc.admin_hide_post(post_id, admin, hidden=hidden)
    return jsonify(result), (200 if result.get("success") else 400)


@community_feed_bp.route("/api/admin/posts/<int:post_id>", methods=["DELETE"])
def api_admin_delete(post_id: int):
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    result = svc.admin_delete_post(post_id, admin)
    _log_admin_action(admin, "community_feed_delete_post", {"post_id": post_id})
    return jsonify(result), (200 if result.get("success") else 400)


@community_feed_bp.route("/api/admin/reports/<int:report_id>", methods=["POST"])
def api_admin_resolve_report(report_id: int):
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    result = svc.admin_resolve_report(
        report_id, admin, _json_body().get("status", "resolved"))
    return jsonify(result), (200 if result.get("success") else 400)


@community_feed_bp.route("/api/admin/bans", methods=["POST"])
def api_admin_ban():
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    body = _json_body()
    result = svc.admin_set_ban(
        body.get("wallet"), admin, bool(body.get("banned", True)),
        body.get("reason"))
    _log_admin_action(admin, "community_feed_ban",
                      {"wallet": body.get("wallet"), "banned": bool(body.get("banned", True))})
    return jsonify(result), (200 if result.get("success") else 400)


@community_feed_bp.route("/api/admin/posts/<int:post_id>/recount", methods=["POST"])
def api_admin_recount(post_id: int):
    """Repair a drifted reaction count from the authoritative reaction rows."""
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    return jsonify({"success": True,
                    "reaction_count": svc.recount_reaction_count(post_id)})


@community_feed_bp.route("/api/admin/badges/revoke", methods=["POST"])
def api_admin_revoke_badge():
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    body = _json_body()
    username = svc.normalize_username(body.get("username"))
    badge_key = body.get("badge_key")
    reason = body.get("reason")
    if (not username or not isinstance(badge_key, str) or not isinstance(reason, str)
            or len(reason.strip()) < 8):
        return jsonify({"success": False, "error":
                        "A username, badge, and moderation reason (at least 8 characters) are required."}), 400
    target_wallet = svc.wallet_for_username(username)
    if not target_wallet:
        return jsonify({"success": False, "error": "Member not found."}), 404
    from . import badges

    result = badges.revoke_badge(
        target_wallet, badge_key, actor=admin, reason=reason.strip())
    if result.get("success") and result.get("revoked"):
        _log_admin_action(admin, "community_badge_revoke", {
            "username": username, "badge_key": badge_key,
            "reason": reason.strip(),
        })
    return jsonify({"success": bool(result.get("success")),
                    "revoked": bool(result.get("revoked"))}), (
                        200 if result.get("success") else 400)


def _log_admin_action(admin_wallet: str, action: str, details: dict) -> None:
    """Best-effort audit log — never let logging break the admin action."""
    try:
        from supabase_client import log_admin_action

        log_admin_action(admin_wallet, action, details)
    except Exception as exc:  # noqa: BLE001
        logger.info("community_feed: admin log skipped (%s): %s", action, exc)


def init_community_feed(app):
    """Register the community feed blueprint on the Flask app."""
    app.register_blueprint(community_feed_bp)
    return True
