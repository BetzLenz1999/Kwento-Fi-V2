from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from flask import Blueprint, jsonify, redirect, render_template, request, session

from . import service as svc
import web_push_service

messages_bp = Blueprint("community_messages", __name__)
_SEND_RATE_SECONDS = max(1, int(os.getenv("COMMUNITY_DM_SEND_RATE_SECONDS", "2")))
_SEND_HITS: dict[str, deque] = defaultdict(deque)
_SEND_LOCK = threading.Lock()


def _enabled() -> bool:
    return (os.getenv("COMMUNITY_DIRECT_MESSAGES_ENABLED", "1") or "1").strip().lower() not in {"0", "false", "off", "no"}


def _wallet() -> str | None:
    wallet = session.get("wallet") or session.get("wallet_address")
    if not wallet or not (session.get("verified") or session.get("ubi_verified")):
        return None
    return wallet


def _auth_error():
    return jsonify({"success": False, "code": "auth_required", "error": "Please log in to use Messages."}), 401


def _result(result: dict):
    status = 200 if result.get("success") else 400
    if result.get("code") == "forbidden":
        status = 403
    if result.get("code") == "not_found":
        status = 404
    if result.get("code") == "db_unavailable":
        status = 503
    if result.get("code") == "db_error":
        status = 503
    if result.get("code") == "storage_unavailable":
        status = 503
    return jsonify(result), status


def _send_allowed(wallet: str) -> bool:
    now = time.time()
    key = (wallet or "").lower()
    with _SEND_LOCK:
        hits = _SEND_HITS[key]
        while hits and hits[0] <= now - _SEND_RATE_SECONDS:
            hits.popleft()
        if hits:
            return False
        hits.append(now)
    return True


@messages_bp.route("/messages")
def messages_page():
    if not _enabled():
        return redirect("/community")
    wallet = _wallet()
    if not wallet:
        return redirect("/")
    return render_template("messages.html", wallet=wallet,
                           viewer_username=svc.profile_service.get_username(wallet),
                           initial_username=(request.args.get("with") or "").strip().lstrip("@"),
                           initial_conversation_id=(request.args.get("conversation") or "").strip())


@messages_bp.route("/api/messages/push/status", methods=["GET", "POST"])
def push_status():
    if not _enabled():
        return jsonify({"success": False, "code": "disabled", "enabled": False}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    configured = web_push_service.configured()
    if not configured:
        return jsonify({"success": True, "enabled": False, "public_key": "",
                        "storage_ready": False, "subscribed": False})
    if not web_push_service.storage_ready():
        return jsonify({"success": False, "code": "storage_unavailable",
                        "enabled": True, "storage_ready": False,
                        "error": "Message notifications cannot be enabled right now. Please try again later."}), 503
    body = {}
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
    endpoint = str(body.get("endpoint") or "").strip()
    subscribed = False
    if endpoint and configured:
        subscribed = web_push_service.has_subscription(wallet, endpoint)
        if subscribed is None:
            return jsonify({"success": False, "code": "status_unavailable",
                            "error": "Could not verify this device’s notification setup."}), 503
    return jsonify({"success": True, "enabled": True, "storage_ready": True,
                    "public_key": web_push_service.public_key(),
                    "subscribed": subscribed})


@messages_bp.route("/api/messages/push/subscribe", methods=["POST"])
def push_subscribe():
    if not _enabled():
        return jsonify({"success": False, "code": "disabled"}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    if not web_push_service.configured():
        return jsonify({"success": False, "code": "not_configured", "error": "Notifications are not configured yet."}), 503
    if not web_push_service.storage_ready():
        return jsonify({"success": False, "code": "storage_unavailable",
                        "error": "Message notifications cannot be enabled right now. Please try again later."}), 503
    data = request.get_json(silent=True) or {}
    return _result(web_push_service.save_subscription(wallet, data.get("subscription"),
                                                       request.headers.get("User-Agent")))


@messages_bp.route("/api/messages/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    if not _enabled():
        return jsonify({"success": False, "code": "disabled"}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    data = request.get_json(silent=True) or {}
    return _result(web_push_service.remove_subscription(wallet, data.get("endpoint")))


@messages_bp.route("/api/messages/summary", methods=["GET"])
def summary():
    """Badge numbers only. Cheap enough to run on every Community page load."""
    if not _enabled():
        return jsonify({"success": False, "code": "disabled",
                        "unread_total": 0, "unread_conversation_count": 0}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    return _result(svc.unread_summary(wallet))


@messages_bp.route("/api/messages/conversations", methods=["GET"])
def conversations():
    if not _enabled():
        return jsonify({"success": False, "code": "disabled", "conversations": []}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    return _result(svc.list_conversations(wallet))


@messages_bp.route("/api/messages/start", methods=["POST"])
def start():
    if not _enabled():
        return jsonify({"success": False, "code": "disabled"}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    data = request.get_json(silent=True) or {}
    return _result(svc.start_conversation(wallet, data.get("username") or ""))


@messages_bp.route("/api/messages/conversations/<conversation_id>", methods=["GET"])
def thread(conversation_id: str):
    if not _enabled():
        return jsonify({"success": False, "code": "disabled"}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    return _result(svc.get_thread(wallet, conversation_id, request.args.get("limit", type=int)))


@messages_bp.route("/api/messages/conversations/<conversation_id>/messages", methods=["POST"])
def post(conversation_id: str):
    if not _enabled():
        return jsonify({"success": False, "code": "disabled"}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    if not _send_allowed(wallet):
        return jsonify({"success": False, "code": "rate_limited",
                        "error": "Please wait a moment before sending another message."}), 429
    data = request.get_json(silent=True) or {}
    return _result(svc.send_message(wallet, conversation_id, data.get("body"),
                                    data.get("client_message_id"), data.get("attachment")))


@messages_bp.route("/api/messages/profile/<username>/onchain-transfer", methods=["POST"])
def save_onchain_transfer(username: str):
    """Verify a Celo transfer again, then save a structured DM message."""
    if not _enabled():
        return jsonify({"success": False, "code": "disabled"}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    if not _send_allowed(wallet):
        return jsonify({"success": False, "code": "rate_limited",
                        "error": "Please wait a moment before saving another transfer message."}), 429
    data = request.get_json(silent=True) or {}
    from community_feed import profile_tips
    verified = profile_tips.record(wallet, username, data.get("token"),
                                   data.get("amount"), data.get("tx_hash"))
    if not verified.get("success"):
        return _result(verified)
    return _result(svc.send_transfer_message(wallet, username, verified.get("tip") or {}))


@messages_bp.route("/api/messages/conversations/<conversation_id>/attachments/presign", methods=["POST"])
def presign_attachment(conversation_id: str):
    if not _enabled():
        return jsonify({"success": False, "code": "disabled"}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    data = request.get_json(silent=True) or {}
    return _result(svc.presign_attachment(wallet, conversation_id,
                                          data.get("name"), data.get("mime"), data.get("bytes")))


@messages_bp.route("/api/messages/attachments/<attachment_id>/download", methods=["GET"])
def download_attachment(attachment_id: str):
    if not _enabled():
        return jsonify({"success": False, "code": "disabled"}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    result = svc.attachment_download(wallet, attachment_id)
    if not result.get("success"):
        return _result(result)
    return redirect(result["url"], code=302)


@messages_bp.route("/api/messages/attachments/<upload_id>/cancel", methods=["POST"])
def cancel_attachment(upload_id: str):
    if not _enabled():
        return jsonify({"success": False, "code": "disabled"}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    return _result(svc.cancel_attachment(wallet, upload_id))


@messages_bp.route("/api/messages/conversations/<conversation_id>/read", methods=["POST"])
def read(conversation_id: str):
    if not _enabled():
        return jsonify({"success": False, "code": "disabled"}), 404
    wallet = _wallet()
    if not wallet:
        return _auth_error()
    return _result(svc.mark_read(wallet, conversation_id))


def init_messages(app):
    app.register_blueprint(messages_bp)
    return True
