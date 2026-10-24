"""Community Daily Learn question snapshots.

The full Learn & Earn page and Community Daily Learn intentionally have separate
availability controls.  A snapshot is generated once per UTC day from the
existing ``quiz_questions`` bank and then becomes the sole source for that day's
Community attempt.  Correct answers stay server-side in the snapshot payload.

This module is deliberately small and dependency-light so it can be called by a
route, a scheduler, or a prewarm job without importing the full Learn & Earn
page/session machinery.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import date, datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

SNAPSHOT_TABLE = "community_daily_learn_snapshots"
QUESTION_TABLE = "quiz_questions"
COMMUNITY_FEATURE = "community_daily_learn_feature"
SNAPSHOT_STATUS = "published"


def _db(client=None):
    if client is not None:
        return client
    from supabase_client import get_supabase_admin_client, get_supabase_client

    return get_supabase_admin_client() or get_supabase_client()


def _community_enabled() -> tuple[bool, str]:
    """Read only the Community Daily Learn kill switch.

    The full ``learn_earn`` maintenance flag is intentionally not consulted.
    The migration seeds this feature as enabled; administrators can use this
    separate flag as the emergency kill switch for the two-question cycle.
    """
    try:
        from maintenance_service import maintenance_service

        status = maintenance_service.get_maintenance_status(COMMUNITY_FEATURE)
        if status.get("is_maintenance"):
            return False, status.get("message") or "Daily Learn is temporarily unavailable."
        return True, ""
    except Exception as exc:  # fail closed for the feature's own switch
        logger.warning("Daily Learn feature flag could not be read: %s", exc)
        return False, "Daily Learn is temporarily unavailable."


def _utc_day(day: date | str | None = None) -> str:
    if day is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(day, date):
        return day.isoformat()
    parsed = date.fromisoformat(str(day))
    return parsed.isoformat()


def _valid_question(row: dict[str, Any]) -> bool:
    return bool(
        row.get("question_id")
        and str(row.get("question") or "").strip()
        and all(str(row.get(key) or "").strip() for key in ("answer_a", "answer_b", "answer_c", "answer_d"))
        and str(row.get("correct") or "").upper() in {"A", "B", "C", "D"}
        and row.get("is_active", True) is not False
    )


def _public_question(row: dict[str, Any], number: int) -> dict[str, Any]:
    return {
        "question_number": number,
        "question_id": row["question_id"],
        "question": str(row["question"]).strip(),
        "options": [str(row[key]).strip() for key in ("answer_a", "answer_b", "answer_c", "answer_d")],
        "category": row.get("category") or "general",
        "difficulty": row.get("difficulty") or "medium",
    }


def _server_question(row: dict[str, Any], number: int) -> dict[str, Any]:
    payload = _public_question(row, number)
    payload["correct_answer"] = ord(str(row["correct"]).upper()) - ord("A")
    payload["explanation"] = str(
        row.get("explanation") or row.get("explanation_text") or ""
    ).strip()
    return payload


def _select_pair(rows: list[dict[str, Any]], day_utc: str, salt: str | None = None) -> list[dict[str, Any]]:
    """Select a stable, distinct pair without client-side randomness."""
    valid = [row for row in rows if _valid_question(row)]
    if len(valid) < 2:
        raise ValueError("at least two active valid Learn & Earn questions are required")
    rotation_salt = salt if salt is not None else os.getenv("COMMUNITY_DAILY_LEARN_ROTATION_SALT", "goodmarket-daily-learn-v1")
    ranked = sorted(
        valid,
        key=lambda row: hashlib.sha256(f"{rotation_salt}:{day_utc}:{row['question_id']}".encode()).hexdigest(),
    )
    first = ranked[0]
    first_category = str(first.get("category") or "general").strip().lower()
    second = next(
        (row for row in ranked[1:]
         if str(row.get("category") or "general").strip().lower() != first_category),
        None,
    )
    return [first, second] if second is not None else ranked[:2]


def _read_snapshot(client, day_utc: str) -> dict[str, Any] | None:
    result = client.table(SNAPSHOT_TABLE).select("*").eq("day_utc", day_utc).limit(1).execute()
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def get_daily_snapshot(day: date | str | None = None, *, client=None) -> dict[str, Any]:
    """Return or atomically publish the day's snapshot.

    Existing snapshots are returned without consulting full Learn & Earn
    maintenance.  A missing snapshot is generated from the source bank, then
    upserted on the unique UTC day key so concurrent workers converge on one
    published pair.
    """
    day_utc = _utc_day(day)
    enabled, message = _community_enabled()
    if not enabled:
        return {"success": False, "status": "unavailable", "message": message}

    db = _db(client)
    if db is None:
        return {"success": False, "status": "unavailable", "message": "Daily Learn is temporarily unavailable."}

    try:
        existing = _read_snapshot(db, day_utc)
        if existing:
            return {"success": True, "status": existing.get("status", SNAPSHOT_STATUS), "snapshot": existing}

        result = db.table(QUESTION_TABLE).select("*").execute()
        pair = _select_pair(getattr(result, "data", None) or [], day_utc)
        row = {
            "day_utc": day_utc,
            "status": SNAPSHOT_STATUS,
            "question_1": _server_question(pair[0], 1),
            "question_2": _server_question(pair[1], 2),
            "source": QUESTION_TABLE,
        }
        # The table is intentionally immutable, so do not use upsert: an
        # upsert would attempt an UPDATE when another worker won the same day.
        # A duplicate insert is harmless; the read below converges both workers
        # on the already-published row.
        try:
            db.table(SNAPSHOT_TABLE).insert(row).execute()
        except Exception:
            logger.info("Daily Learn snapshot already published for %s", day_utc)
        published = _read_snapshot(db, day_utc)
        if not published:
            # Never serve a local, unpublished fallback: answer submissions and
            # completion rewards must be bound to the immutable stored row.
            return {"success": False, "status": "unavailable",
                    "message": "Daily Learn is temporarily unavailable."}
        return {"success": True, "status": SNAPSHOT_STATUS, "snapshot": published}
    except Exception as exc:  # the feed must remain usable if Daily Learn is down
        logger.exception("Daily Learn snapshot unavailable for %s: %s", day_utc, exc)
        return {"success": False, "status": "unavailable", "message": "Daily Learn is temporarily unavailable."}


def public_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Strip server answer keys before a snapshot reaches the browser."""
    if not result.get("success"):
        return result
    snapshot = result.get("snapshot") or {}
    questions = []
    for key in ("question_1", "question_2"):
        question = snapshot.get(key)
        if isinstance(question, dict):
            questions.append({
                k: v for k, v in question.items()
                if k not in {"correct_answer", "explanation"}
            })
    return {"success": True, "status": result.get("status", SNAPSHOT_STATUS), "day_utc": snapshot.get("day_utc"), "questions": questions}


def score_snapshot(snapshot: dict[str, Any], answers: list[int]) -> int:
    """Score exactly two answers against the immutable server-side payload."""
    if len(answers) != 2:
        raise ValueError("exactly two answers are required")
    questions = [snapshot.get("question_1"), snapshot.get("question_2")]
    return sum(int(answer) == int(question["correct_answer"]) for answer, question in zip(answers, questions))
