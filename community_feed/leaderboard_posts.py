"""Publication helpers for the weekly EXP leaderboard feed posts.

The snapshot remains the source of truth. This module only renders a public,
whitelisted summary and links already-published week posts; private score
breakdowns and wallet addresses are never included.
"""
from __future__ import annotations

import logging
import os
import re
import ipaddress
from datetime import date
from urllib.parse import urlsplit

from env_utils import get_env_int

logger = logging.getLogger(__name__)
POST_TABLE = "leaderboard_weekly_posts"
SNAPSHOT_TABLE = "leaderboard_weekly_snapshots"
MAX_ARCHIVE_ROWS = 500
_LABEL_RE = re.compile(r"^0x[a-fA-F0-9]{4}…[a-fA-F0-9]{4}$")


def _max_top_n() -> int:
    """Admin-configurable Top N ceiling (no fixed 10/20 cap).

    Falls back to the environment/default when imported outside the package
    (for example, by a standalone test loader).
    """
    try:
        from . import weekly_exp
        return weekly_exp.max_top_n()
    except Exception:  # noqa: BLE001 - standalone import context
        return max(1, min(10_000, get_env_int("WEEKLY_EXP_MAX_TOP_N", 10_000)))


def _rows(result) -> list[dict]:
    data = getattr(result, "data", None)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _origin() -> str:
    candidate = (os.getenv("PRODUCTION_DOMAIN") or "https://kwentofi.live").strip().rstrip("/")
    parts = urlsplit(candidate)
    hostname = (parts.hostname or "").lower()
    loopback = hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if ((parts.scheme != "https" and not (parts.scheme == "http" and loopback)) or not parts.netloc
            or parts.path not in {"", "/"}
            or parts.username or parts.password or parts.query or parts.fragment):
        return "https://kwentofi.live"
    return candidate


def _week_start(value: str | date) -> date:
    day = date.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(day, date) or day.isoweekday() != 1:
        raise ValueError("week_start must be a Monday UTC date")
    return day


def history(db, *, before: str | date | None = None, limit: int = MAX_ARCHIVE_ROWS) -> list[dict]:
    """Return public week/post references; no participant data is queried here."""
    try:
        size = max(1, min(MAX_ARCHIVE_ROWS, int(limit)))
    except (TypeError, ValueError):
        size = MAX_ARCHIVE_ROWS
    query = db.table(POST_TABLE).select("week_start,post_id,created_at")
    if before is not None:
        query = query.lt("week_start", _week_start(before).isoformat())
    post_rows = _rows(query.order("week_start", desc=True).limit(size).execute())
    starts = [str(row.get("week_start")) for row in post_rows if row.get("week_start")]
    snapshots = {}
    if starts:
        result = (db.table(SNAPSHOT_TABLE)
                  .select("week_start,week_end_exclusive,data_status,generated_at")
                  .in_("week_start", starts).execute())
        snapshots = {str(row.get("week_start")): row for row in _rows(result)}
    origin = _origin()
    output = []
    for row in post_rows:
        start = str(row.get("week_start") or "")
        snapshot = snapshots.get(start, {})
        try:
            parsed = _week_start(start)
            post_id = int(row.get("post_id"))
        except (TypeError, ValueError):
            continue
        if post_id <= 0:
            continue
        output.append({
            "week_start": parsed.isoformat(),
            "week_end_exclusive": str(snapshot.get("week_end_exclusive") or ""),
            "status": str(snapshot.get("data_status") or "unavailable"),
            "post_id": post_id,
            "url": f"{origin}/community/p/{post_id}",
            "generated_at": snapshot.get("generated_at"),
        })
    return output


def _public_standings(rows: list[dict]) -> list[dict]:
    ceiling = _max_top_n()
    safe = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            rank = int(row.get("rank"))
            points = int(row.get("total_exp"))
        except (TypeError, ValueError):
            continue
        if not 1 <= rank <= ceiling or not 0 <= points <= 100_000_000:
            continue
        label = str(row.get("display_name") or "")[:48]
        if not _LABEL_RE.fullmatch(label):
            label = "Community member"
        safe.append({"rank": rank, "display_name": label, "total_exp": points})
    return safe


def _is_our_post_url(value: object) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
        expected = urlsplit(_origin())
        return (parsed.scheme == "https" and parsed.netloc.lower() == expected.netloc.lower()
                and not parsed.username and not parsed.password
                and re.fullmatch(r"/community/p/[1-9][0-9]*", parsed.path) is not None
                and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def build_body(*, week_start: str | date, standings: list[dict],
               previous_posts: list[dict], status: str = "provisional") -> str:
    """Render a plain-text caption; client rendering linkifies only HTTPS URLs."""
    start = _week_start(week_start)
    end = date.fromordinal(start.toordinal() + 6)
    state = "Final weekly results" if status == "finalized" else "Provisional weekly standings"
    lines = [f"{state} · {start.isoformat()} to {end.isoformat()} (UTC)"]
    safe_standings = _public_standings(standings)
    if safe_standings:
        lines.append("")
        lines.extend(
            f"{item['rank']}. {item['display_name']} — {item['total_exp']} EXP"
            for item in safe_standings
        )
    else:
        lines.extend(["", "No qualifying participants were recorded for this week."])
    links = [item for item in previous_posts[:2]
             if isinstance(item, dict) and _is_our_post_url(item.get("url"))]
    if links:
        lines.extend(["", "Previous weekly results:"])
        lines.extend(f"Week of {item['week_start']}: {item['url']}" for item in links)
    lines.extend(["", f"View all weekly results: {_origin()}/community/weekly-leaderboard/archive"])
    lines.extend(["", "Scores are calculated for this UTC week only. Prior weeks are retained as history."])
    if status != "finalized":
        lines.append("This week's standings are provisional and may change after verified activity reconciliation.")
    return "\n".join(lines)[:5000]


def publish_weekly_post(db, *, week_start: str | date,
                        standings: list[dict], status: str = "provisional") -> dict:
    """Create once or update the current week's post through a service-role RPC."""
    start = _week_start(week_start)
    prior = history(db, before=start, limit=2)
    body = build_body(week_start=start, standings=standings,
                      previous_posts=prior, status=status)
    result = db.rpc("weekly_leaderboard_publish_system_post", {
        "p_week_start": start.isoformat(),
        "p_body": body,
    }).execute()
    data = getattr(result, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or data.get("success") is not True:
        raise RuntimeError("weekly leaderboard system post publication failed")
    return {"post_id": int(data["post_id"]), "status": data.get("status", status),
            "created": bool(data.get("created")), "updated": bool(data.get("updated"))}


def archive(db, *, limit: int = MAX_ARCHIVE_ROWS) -> list[dict]:
    """All available week-post links for the public archive page."""
    return history(db, limit=limit)


def weeks_for_posts(db, post_ids: list) -> dict[int, str]:
    """Map feed post ids to their weekly period start (UTC Monday).

    The feed attaches this to ``system_weekly_leaderboard`` posts so the private
    claim panel can address the exact period the post announces, without ever
    exposing a wallet or an entitlement to another viewer.
    """
    if not post_ids:
        return {}
    try:
        rows = _rows(db.table(POST_TABLE)
                     .select("post_id,week_start")
                     .in_("post_id", post_ids).execute())
    except Exception as exc:  # noqa: BLE001 - a missing week only hides the panel
        logger.warning("community_feed: weekly post week lookup failed: %s", exc)
        return {}
    result: dict[int, str] = {}
    for row in rows:
        try:
            result[int(row.get("post_id"))] = str(row.get("week_start") or "")
        except (TypeError, ValueError):
            continue
    return result
