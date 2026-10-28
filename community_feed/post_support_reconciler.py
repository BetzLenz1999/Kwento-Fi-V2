"""Bounded recovery worker for Community Post Support receipts and reserves.

This worker never signs or sends transactions. It verifies each stored tx against
Celo, calls the idempotent SQL finalizer for confirmed support, and releases an
expired reservation only after its deadline plus a conservative grace window.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from env_utils import get_env_int

from . import post_support

logger = logging.getLogger(__name__)
_ENABLED = os.getenv("COMMUNITY_POST_SUPPORT_RECONCILER_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
_INTERVAL_SEC = max(60, get_env_int("COMMUNITY_POST_SUPPORT_RECONCILER_INTERVAL_SEC", 900))
_BATCH_SIZE = max(1, min(get_env_int("COMMUNITY_POST_SUPPORT_RECONCILER_BATCH_SIZE", 100), 500))
_MIN_EXPIRY_GRACE = timedelta(minutes=16)
_DROPPED_TX_GRACE = timedelta(hours=1)
_stop = threading.Event()
_thread = None
_lock = threading.Lock()


def _release(db, row: dict, reason: str) -> bool:
    try:
        result = post_support._rpc(db, "community_post_support_release_reservation", {
            "p_support_id": str(row["id"]), "p_reason": reason,
        })
        return result.get("success") is True
    except Exception as exc:  # noqa: BLE001
        logger.warning("post support reservation release failed for %s: %s", row.get("id"), exc)
        return False


def reconcile_once(app=None) -> dict:
    """Run one bounded pass; safe to invoke from an external scheduler too."""
    if app is not None:
        with app.app_context():
            return reconcile_once()
    db = post_support._admin_db()
    if db is None:
        return {"success": False, "error": "service-role database unavailable", "scanned": 0}
    cutoff = datetime.now(timezone.utc) - _MIN_EXPIRY_GRACE
    try:
        result = (db.table("community_post_supports").select("*")
                  .in_("status", ["prepared", "submitted"])
                  .lte("expires_at", cutoff.isoformat())
                  .order("expires_at").limit(_BATCH_SIZE).execute())
        rows = getattr(result, "data", None) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("post support reconciliation scan failed: %s", exc)
        return {"success": False, "error": "support scan unavailable", "scanned": 0}

    counts = {"success": True, "scanned": len(rows), "finalized": 0,
              "released": 0, "pending": 0, "failed": 0}
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            status = row.get("status")
            if status == "prepared" or not row.get("tx_hash"):
                if _release(db, row, "expired unsubmitted support authorization"):
                    counts["released"] += 1
                else:
                    counts["failed"] += 1
                continue

            verification = post_support._verify_receipt(str(row["tx_hash"]), row)
            if verification.get("status") == "confirmed":
                final = post_support._rpc(db, "community_post_support_finalize", {
                    "p_support_id": str(row["id"]),
                    "p_tx_hash": str(row["tx_hash"]).lower(),
                    "p_confirmed_block": int(verification["block_number"]),
                })
                if final.get("success") is True:
                    counts["finalized"] += 1
                else:
                    counts["failed"] += 1
                continue
            if verification.get("status") == "reverted":
                if _release(db, row, "verified transaction reverted"):
                    counts["released"] += 1
                else:
                    counts["failed"] += 1
                continue

            expiry = row.get("expires_at")
            try:
                expiry_dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                if expiry_dt.tzinfo is None:
                    expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                counts["failed"] += 1
                continue
            # Expired typed data cannot succeed on-chain after its deadline. Give
            # pending txs an extra hour for receipt providers to catch up first.
            if verification.get("status") == "pending" and now - expiry_dt >= _DROPPED_TX_GRACE:
                if _release(db, row, "expired transaction authorization; no receipt found"):
                    counts["released"] += 1
                else:
                    counts["failed"] += 1
            else:
                counts["pending"] += 1
        except Exception as exc:  # noqa: BLE001 - continue with next row
            logger.warning("post support reconciliation failed for %s: %s", row.get("id"), exc)
            counts["failed"] += 1
    return counts


def _loop(app=None):
    _stop.wait(min(30, _INTERVAL_SEC))
    while not _stop.is_set():
        try:
            reconcile_once(app)
        except Exception as exc:  # noqa: BLE001 - retry on next tick
            logger.warning("post support reconciler tick failed: %s", exc)
        _stop.wait(_INTERVAL_SEC)


def init_post_support_reconciler(app=None) -> bool:
    """Start one worker per process; the SQL finalizer remains cross-worker safe."""
    global _thread
    if not _ENABLED:
        logger.info("Community Post Support reconciler disabled")
        return False
    with _lock:
        if _thread and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(target=_loop, args=(app,), daemon=True,
                                   name="community-post-support-reconciler")
        _thread.start()
        logger.info("Community Post Support reconciler started (every %ss, batch=%s)",
                    _INTERVAL_SEC, _BATCH_SIZE)
        return True


def stop_post_support_reconciler():
    global _thread
    with _lock:
        _stop.set()
        _thread = None
