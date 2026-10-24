"""Background retry worker for completed, uncredited Community Daily Learn.

The database RPC claims bounded work with SKIP LOCKED and a retry cooldown.
Ledger idempotency remains the final duplicate-credit guard. In serverless
runtimes this in-process worker is intentionally not started; the existing
page-load repair remains active and deployments may invoke the same reconciler
from their supported external scheduler.

Environment:
    COMMUNITY_DAILY_LEARN_RECONCILER_ENABLED      default on
    COMMUNITY_DAILY_LEARN_RECONCILER_INTERVAL_SEC default 900
    COMMUNITY_DAILY_LEARN_RECONCILER_BATCH_SIZE   default 100 (max 500)
"""
from __future__ import annotations

import logging
import os
import threading
import time

from env_utils import get_env_int

logger = logging.getLogger(__name__)
_ENABLED = os.getenv("COMMUNITY_DAILY_LEARN_RECONCILER_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
_INTERVAL_SEC = max(60, get_env_int("COMMUNITY_DAILY_LEARN_RECONCILER_INTERVAL_SEC", 900))
_BATCH_SIZE = max(1, min(get_env_int("COMMUNITY_DAILY_LEARN_RECONCILER_BATCH_SIZE", 100), 500))
_scheduler_stop = threading.Event()
_scheduler_thread = None
_scheduler_lock = threading.Lock()


def reconcile_once(app=None) -> dict:
    """Execute a single bounded retry pass; exposed for testing/operations."""
    from . import rewards

    if app is None:
        return rewards.reconcile_uncredited_daily_learn_attempts(batch_size=_BATCH_SIZE)
    with app.app_context():
        return rewards.reconcile_uncredited_daily_learn_attempts(batch_size=_BATCH_SIZE)


def _loop(app=None):
    # Do not delay app startup with a database scan.
    _scheduler_stop.wait(min(30, _INTERVAL_SEC))
    while not _scheduler_stop.is_set():
        try:
            reconcile_once(app)
        except Exception as exc:  # noqa: BLE001 - next pass is the retry
            logger.warning("Community Daily Learn reconciler tick failed: %s", exc)
        _scheduler_stop.wait(_INTERVAL_SEC)


def is_reconciler_enabled() -> bool:
    return _ENABLED


def init_daily_learn_reconciler(app=None) -> bool:
    """Start the worker once per process. Multiple processes remain DB-safe."""
    global _scheduler_thread
    if not _ENABLED:
        logger.info("Community Daily Learn reconciler disabled")
        return False
    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return True
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(
            target=_loop, args=(app,), daemon=True,
            name="community-daily-learn-reconciler",
        )
        _scheduler_thread.start()
        logger.info(
            "Community Daily Learn reconciler started (every %ss, batch=%s)",
            _INTERVAL_SEC, _BATCH_SIZE,
        )
        return True


def stop_daily_learn_reconciler():
    global _scheduler_thread
    with _scheduler_lock:
        _scheduler_stop.set()
        _scheduler_thread = None
