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

import logging
import os
import threading
import time
from collections import defaultdict, deque

from flask import Blueprint, jsonify, redirect, render_template, request, session

from . import rewards
from . import service as svc
from . import storage

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
    return render_template(
        "community.html",
        wallet=wallet,
        is_guest=not bool(wallet),
        display_name=svc.display_name(wallet) if wallet else None,
        username=svc.get_username(wallet) if wallet else None,
        avatar_url=svc.get_avatar_url(wallet) if wallet else None,
        username_required=svc.REQUIRE_USERNAME,
        login_method=session.get("login_method", ""),
        walletconnect_project_id=os.getenv("WALLETCONNECT_PROJECT_ID", ""),
        max_caption=svc.MAX_CAPTION,
        max_media=svc.MAX_MEDIA_PER_POST,
        max_image_mb=svc.MAX_IMAGE_MB,
        max_video_mb=svc.MAX_VIDEO_MB,
        max_avatar_mb=svc.MAX_AVATAR_MB,
        storage_ready=storage.storage_available(),
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


@community_feed_bp.route("/api/rewards", methods=["GET"])
def api_rewards():
    """The viewer's G$ earned balance. Reading it is gated like the feed."""
    if not svc.is_enabled():
        return _disabled_response()
    wallet = _session_wallet()
    return jsonify(svc.rewards_summary(wallet))


@community_feed_bp.route("/api/admin/reward-rates", methods=["GET"])
def api_admin_reward_rates():
    """The rates in force, plus the bounds the editor must respect.

    Admin-only. The bounds travel with the payload so the form's min/max can
    never drift from the server clamp — the server clamp is still the control.
    """
    if not _admin_wallet():
        return jsonify({"success": False, "error": "Admin access required"}), 403
    rates = rewards.effective_rates(use_cache=False)
    low, high = rewards.RATE_BOUNDS["comment_out"]
    return jsonify({
        "success": True,
        "rates": {name: float(value) for name, value in rates.items()},
        # `names` fixes the display order (largest to smallest) so the editor
        # cannot reorder the anti-farm story by accident.
        "names": list(rewards.RATE_NAMES),
        "min": float(low),
        "max": float(high),
        "defaults": {name: float(rewards._env_rate(name))
                     for name in rewards.RATE_NAMES},
    })


@community_feed_bp.route("/api/admin/reward-rates", methods=["POST"])
def api_admin_set_reward_rates():
    """Change the rates. Admin-only, audited, clamped server-side.

    This is the ONLY admin write path in the rewards system. It writes config,
    never a balance: the ledger stays append-only and the balance stays a SUM,
    so an admin still cannot change what a member has already earned.
    """
    admin = _admin_wallet()
    if not admin:
        return jsonify({"success": False, "error": "Admin access required"}), 403
    data = request.get_json(silent=True) or {}
    result = rewards.set_rates(
        data.get("rates") or {},
        actor=admin,
        reason=data.get("reason") or "",
    )
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
    return jsonify(svc.get_feed(viewer=viewer, cursor=cursor, limit=limit))


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
        max_avatar_mb=svc.MAX_AVATAR_MB,
        storage_ready=storage.storage_available(),
    ), (200 if profile else 404)


@community_feed_bp.route("/api/profile/<username>", methods=["GET"])
def api_profile(username: str):
    if not svc.is_enabled():
        return _disabled_response()
    result = svc.get_public_profile(username, viewer=_session_wallet())
    return jsonify(result), (200 if result.get("success") else 404)


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

    result = svc.create_post(wallet, body.get("caption"), verified)
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
    result = svc.clear_reaction(wallet, post_id)
    status = 200 if result.get("success") else (
        404 if result.get("code") == "not_found" else 400)
    return jsonify(result), status


# ── Comments ─────────────────────────────────────────────────────────────────

@community_feed_bp.route("/api/posts/<int:post_id>/comments", methods=["GET"])
def api_get_comments(post_id: int):
    if not svc.is_enabled():
        return _disabled_response()
    return jsonify(svc.get_comments(post_id, viewer=_session_wallet()))


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

    result = svc.add_comment(wallet, post_id, _json_body().get("body"))
    if result.get("success"):
        return jsonify(result)
    status = {"not_found": 404, "banned": 403}.get(result.get("code"), 400)
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
