"""GoodMarket Community Feed — business logic.

A chronological, Facebook/Instagram-style feed of user posts: a caption plus
optional images/video. Media bytes live in Cloudflare R2 (``storage/r2.py``);
Supabase holds metadata only.

Design notes (mirroring the rest of this codebase):
- Reads go through the service-role client first so RLS cannot silently hide
  rows (the lotto/gcash lesson).
- The feature is HIDDEN by default: ``is_enabled()`` reads the
  ``community_feed_feature`` row from ``maintenance_settings`` and fails CLOSED,
  so a database hiccup can never leak the feed before an admin turns it on.
- The wallet address is the ownership key only and is NEVER returned to the
  browser — ``_public_post()`` drops it and exposes ``is_mine`` instead.
- Every public function returns a plain dict and never raises; the routes
  translate the dict into a JSON response.
- ``supabase_client`` is imported lazily inside the helpers so this module
  imports (and is testable) with no dependencies installed.
"""

from __future__ import annotations

import logging
import os
import re
import base64
import hashlib
import json
import time
from functools import lru_cache
from datetime import datetime, timezone

from . import notifications as community_notifications
from . import rewards

from env_utils import get_env_int

logger = logging.getLogger(__name__)

FEATURE_NAME = "community_feed_feature"

MAX_CAPTION = get_env_int("COMMUNITY_MAX_CAPTION", 2200)
# The UI counts characters; the server also bounds UTF-8 bytes, because an emoji
# costs 4 bytes and a bytes-blind check would let a ~8KB post through the
# 2200-character gate.
MAX_CAPTION_BYTES = get_env_int("COMMUNITY_MAX_CAPTION_BYTES", MAX_CAPTION * 4)
MAX_COMMENT = get_env_int("COMMUNITY_MAX_COMMENT", 500)
MAX_MEDIA_PER_POST = get_env_int("COMMUNITY_MAX_MEDIA_PER_POST", 4)
POST_RATE_LIMIT_SECONDS = get_env_int("COMMUNITY_POST_RATE_LIMIT_SECONDS", 60)
COMMENT_RATE_LIMIT_SECONDS = get_env_int("COMMUNITY_COMMENT_RATE_LIMIT_SECONDS", 5)
UPLOAD_RATE_LIMIT_SECONDS = get_env_int("COMMUNITY_UPLOAD_RATE_LIMIT_SECONDS", 10)
# How many presigns one wallet may request inside the upload window. A single
# post carries up to MAX_MEDIA_PER_POST files and every video also uploads a
# poster frame, so allowing only ONE presign per window made any post with a
# second image fail ("Please wait before uploading again") after file 1.
UPLOAD_BURST_ALLOWANCE = max(1, MAX_MEDIA_PER_POST) * 2
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 30
REACTION_USERS_DEFAULT_PAGE_SIZE = 25
REACTION_USERS_MAX_PAGE_SIZE = 50
REACTION_CURSOR_VERSION = 1

# Follow-to-follow. A cap on how many accounts one wallet may follow, so a
# scripted account cannot mint an unbounded edge table. The follower side is
# unbounded on purpose (popularity is not a bug).
FOLLOW_LIMIT = get_env_int("COMMUNITY_FOLLOW_LIMIT", 5000)

# "Suggested for you" — the right-column panel. Ranked by DISTINCT ACTIVE DAYS
# in a recent window, not by raw volume: "always active" means present most
# days, and a single viral post must not win the panel. Read per call so a
# tuning change needs no redeploy; a malformed value falls back to the default
# (never 0, which would switch the panel off).
SUGGESTIONS_ENABLED = get_env_int("COMMUNITY_SUGGESTIONS_ENABLED", 1)
SUGGESTIONS_LIMIT = max(1, get_env_int("COMMUNITY_SUGGESTIONS_LIMIT", 12))
SUGGESTIONS_WINDOW_DAYS = max(1, get_env_int("COMMUNITY_SUGGESTIONS_WINDOW_DAYS", 30))
SUGGESTIONS_ACTIVE_FLOOR_DAYS = max(
    1, get_env_int("COMMUNITY_SUGGESTIONS_ACTIVE_FLOOR_DAYS", 14))
# The ranking moves slowly, so a short cache is enough and keeps the panel off
# the per-page-load cost path. Keyed by (viewer, limit) — see the helper.
SUGGESTIONS_CACHE_TTL_SECONDS = max(
    0, get_env_int("COMMUNITY_SUGGESTIONS_CACHE_TTL_SECONDS", 120))

# A public post must carry a real username. Without this gate a user who never
# picked one publishes a shortened wallet address, which looks like spam and
# partly undoes the "the browser never sees an address" rule. Enforced in the
# service, not just the UI, so a direct API call cannot bypass it.
REQUIRE_USERNAME = os.getenv("COMMUNITY_REQUIRE_USERNAME", "1").strip() != "0"

# Shown for a legacy row written before the username requirement. Deliberately
# NOT a wallet address: an old post must not leak one either.
ANON_LABEL = "Community member"

# Video is accepted but bounded; images get the tighter cap.
MAX_IMAGE_MB = get_env_int("R2_MAX_IMAGE_MB", 10)
MAX_VIDEO_MB = get_env_int("R2_MAX_VIDEO_MB", 100)
MAX_IMAGE_BYTES = MAX_IMAGE_MB * 1024 * 1024
MAX_VIDEO_BYTES = MAX_VIDEO_MB * 1024 * 1024

# The six reactions (Messenger model — one per user per post, switchable).
REACTIONS = {
    "like": "👍",
    "love": "❤️",
    "haha": "😂",
    "wow": "😮",
    "sad": "😢",
    "angry": "😡",
}
REACTION_KEYS = tuple(REACTIONS)

# How many reaction rows one feed page's breakdown scan may read. The breakdown
# is decoration (which emoji to show); ``reaction_count`` on the post is the
# authoritative total, so a bounded scan cannot under-report the headline
# number. Without a bound, one viral post could pull thousands of rows into a
# single page render.
REACTION_SCAN_LIMIT = get_env_int("COMMUNITY_REACTION_SCAN_LIMIT", 2000)

# Declared content types we will hand a presigned PUT to, mapped to the file
# extension used in the object key. Anything else is refused before signing.
IMAGE_MIME_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
VIDEO_MIME_TYPES = {
    "video/mp4": "mp4",
    "video/webm": "webm",
}
ALLOWED_MIME_TYPES = {**IMAGE_MIME_TYPES, **VIDEO_MIME_TYPES}

# Profile pictures are IMAGES ONLY (no video, no SVG — it can carry script) and
# get their own, tighter size cap: an avatar is rendered small on every card, so
# a 10MB upload would only cost storage and mobile data for no visible gain.
AVATAR_MIME_TYPES = dict(IMAGE_MIME_TYPES)
MAX_AVATAR_MB = get_env_int("COMMUNITY_MAX_AVATAR_MB", 5)
MAX_AVATAR_BYTES = MAX_AVATAR_MB * 1024 * 1024
AVATAR_RATE_LIMIT_SECONDS = get_env_int("COMMUNITY_AVATAR_RATE_LIMIT_SECONDS", 10)

# Control characters (including zero-width/line-separator tricks) are stripped
# so a caption can never smuggle invisible content into the rendered feed. The
# newline is deliberately NOT in this class — line breaks are legitimate
# content, collapsed per-line by sanitize_caption instead.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029]")
_HSPACE = re.compile(r"[ \t\f\v]+")
_BLANK_RUN = re.compile(r"\n{3,}")


# ── Infrastructure helpers ───────────────────────────────────────────────────

def _get_supabase():
    """Service-role client first (RLS-safe reads), falling back to anon."""
    try:
        from supabase_client import get_supabase_admin_client, get_supabase_client

        return get_supabase_admin_client() or get_supabase_client()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: supabase client unavailable: %s", exc)
        return None


def _rows(result) -> list[dict]:
    """Rows from a PostgREST response, tolerating a None/odd payload."""
    if not result:
        return []
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    return list(data or [])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def short_wallet(wallet: str | None) -> str:
    """Public fallback label when a user has no username yet."""
    wallet = (wallet or "").strip()
    if len(wallet) >= 10:
        return f"{wallet[:6]}…{wallet[-4:]}"
    return wallet or "anonymous"


def get_username(wallet: str | None) -> str | None:
    """The user's GoodMarket username, matched case-insensitively.

    Sessions store checksummed wallets while the bot stores lowercase, so an
    exact-match lookup silently misses (the documented case-mismatch bug class).
    """
    if not wallet:
        return None
    supabase = _get_supabase()
    if not supabase:
        return None
    try:
        result = (
            supabase.table("user_data")
            .select("username")
            .ilike("wallet_address", wallet)
            .limit(1)
            .execute()
        )
        for row in _rows(result):
            username = (row.get("username") or "").strip()
            if username:
                return username
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: username lookup failed: %s", exc)
    return None


def display_name(wallet: str | None) -> str:
    """Username when set, otherwise a shortened wallet address."""
    return get_username(wallet) or short_wallet(wallet)


def _comment_post_owner(supabase, post_id) -> str | None:
    """The owner wallet of a post, for the comment-reward reversal.

    Needed because a comment credits the owner too, so the reversal must find
    the owner's row as well as the commenter's. Never raises.
    """
    if post_id in (None, ""):
        return None
    try:
        result = (
            supabase.table("community_feed_posts")
            .select("wallet_address,post_type")
            .eq("id", int(post_id))
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: post owner lookup failed: %s", exc)
        return None
    rows = _rows(result)
    if not rows or rows[0].get("post_type") == "system_vault_donation":
        return None
    return rows[0].get("wallet_address")


def rewards_summary(wallet: str | None) -> dict:
    """The rewards card payload for the routes (thin delegate, see rewards.py).

    Kept here so the routes talk to one module, and so the rewards feature can
    be stubbed in a test without reaching into ``community_feed.rewards``.
    """
    try:
        return rewards.summary(wallet)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: rewards summary failed: %s", exc)
        return {"success": True, "enabled": False, "is_guest": not wallet,
                "balance_gd": 0.0, "min_payout_gd": 0.0, "progress": 0.0,
                "status": "guest" if not wallet else "pending",
                "can_withdraw": False, "budgets": None,
                "configuration_available": False}


def _viewer_reward_outcome(value) -> dict:
    """Expose only the viewer's status/reason; reward hooks are best-effort."""
    if not isinstance(value, dict):
        return {"status": "not_credited", "reason": "reward_unavailable"}
    output = {key: value[key] for key in ("status", "reason") if key in value}
    output.setdefault("status", "not_credited")
    return output


def _persisted_reward_time(supabase, table: str, filters: dict, supplied=None):
    """Recover a persisted event timestamp; never substitute the current time.

    Minimal-return PostgREST deployments may omit the insert representation.
    An unreadable source time stays unknown and fails closed if a cutoff is set.
    """
    if supplied:
        return supplied
    try:
        query = supabase.table(table).select("created_at")
        for key, value in filters.items():
            query = query.eq(key, value)
        rows = _rows(query.limit(1).execute())
        return rows[0].get("created_at") if rows else None
    except Exception as exc:  # noqa: BLE001 - never fail the saved interaction
        logger.warning("community_feed: reward source timestamp unavailable: %s", exc)
        return None


def is_enabled() -> bool:
    """True only when the admin has turned the feed ON.

    Fails CLOSED: a missing row, a DB error or an unreadable value all mean
    "not enabled", so the feed stays hidden until explicitly switched on.

    ``COMMUNITY_FEED_ENABLED=0`` is a hard kill switch that wins over the admin
    flag — a deploy can take the feature down without a DB write.
    """
    if os.getenv("COMMUNITY_FEED_ENABLED", "1").strip() == "0":
        return False
    supabase = _get_supabase()
    if not supabase:
        return False
    try:
        result = (
            supabase.table("maintenance_settings")
            .select("is_maintenance")
            .eq("feature_name", FEATURE_NAME)
            .limit(1)
            .execute()
        )
        rows = _rows(result)
        if not rows:
            return False
        return not bool(rows[0].get("is_maintenance", True))
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: feature flag read failed: %s", exc)
        return False


# ── Moderation state ─────────────────────────────────────────────────────────

def is_banned(wallet: str | None) -> bool:
    if not wallet:
        return False
    supabase = _get_supabase()
    if not supabase:
        # Fail OPEN on an unreadable ban list: a moderation outage must not lock
        # every user out of the feed. Abuse is still bounded by rate limits and
        # by the report → admin path.
        return False
    try:
        result = (
            supabase.table("community_feed_bans")
            .select("wallet_address")
            .ilike("wallet_address", wallet)
            .limit(1)
            .execute()
        )
        return bool(_rows(result))
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: ban lookup failed: %s", exc)
        return False


# ── Sanitising ───────────────────────────────────────────────────────────────

def sanitize_caption(raw: str | None) -> str:
    """Normalize a caption while preserving intentional line breaks.

    - ``\\r\\n`` / ``\\r`` are normalized to ``\\n``.
    - Control characters are stripped (invisible-content guard) EXCEPT the
      newline, so multi-line captions keep their structure.
    - Runs of spaces/tabs collapse, per-line trailing space is trimmed, and 3+
      consecutive blank lines collapse to 2.
    Length is validated by the caller so the two rules stay independently
    testable.
    """
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    lines = [_HSPACE.sub(" ", line).rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip("\n").strip()


def validate_caption(raw: str | None) -> tuple[bool, str, str]:
    """Returns ``(ok, error, clean)`` for a caption.

    A caption is required when the post carries no media and optional when it
    does — the same rule the chatroom uses for an attachment-only message.
    """
    clean = sanitize_caption(raw)
    if len(clean) > MAX_CAPTION:
        return False, f"Caption is too long (max {MAX_CAPTION} characters)", clean
    if len(clean.encode("utf-8")) > MAX_CAPTION_BYTES:
        return False, "Caption is too long", clean
    return True, "", clean


def validate_comment(raw: str | None) -> tuple[bool, str, str]:
    clean = sanitize_caption(raw)
    if not clean:
        return False, "Comment cannot be empty", clean
    if len(clean) > MAX_COMMENT:
        return False, f"Comment is too long (max {MAX_COMMENT} characters)", clean
    return True, "", clean


# ── Upload planning ──────────────────────────────────────────────────────────

def plan_upload(content_type: str | None, declared_bytes: int | None = None
                ) -> tuple[bool, str, dict]:
    """Validate a requested upload and describe it.

    Returns ``(ok, error, plan)`` where plan carries ``media_type``, ``ext`` and
    ``max_bytes``. The content type is the client's declaration; the real bytes
    are re-checked after upload (``verify_uploaded_object``).
    """
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_MIME_TYPES:
        return False, (
            "Unsupported file type. Please upload a JPEG, PNG, GIF, WEBP, "
            "MP4 or WEBM file."
        ), {}
    media_type = "image" if mime in IMAGE_MIME_TYPES else "video"
    max_bytes = MAX_IMAGE_BYTES if media_type == "image" else MAX_VIDEO_BYTES
    if declared_bytes is not None:
        try:
            declared = int(declared_bytes)
        except (TypeError, ValueError):
            declared = 0
        if declared > max_bytes:
            limit_mb = MAX_IMAGE_MB if media_type == "image" else MAX_VIDEO_MB
            return False, f"File is too large (max {limit_mb}MB)", {}
    return True, "", {
        "media_type": media_type,
        "mime": mime,
        "ext": ALLOWED_MIME_TYPES[mime],
        "max_bytes": max_bytes,
    }


def plan_avatar_upload(content_type: str | None, declared_bytes: int | None = None
                       ) -> tuple[bool, str, dict]:
    """Validate a requested PROFILE PICTURE upload and describe it.

    Same shape as ``plan_upload`` but restricted to images and bounded by the
    tighter avatar cap. A profile picture is never a video, so accepting one
    here would only let a user publish a clip that every card then fails to
    render.
    """
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime not in AVATAR_MIME_TYPES:
        return False, (
            "Unsupported image type. Please upload a JPEG, PNG, GIF or WEBP "
            "image."
        ), {}
    if declared_bytes is not None:
        try:
            declared = int(declared_bytes)
        except (TypeError, ValueError):
            declared = 0
        if declared > MAX_AVATAR_BYTES:
            return False, f"Image is too large (max {MAX_AVATAR_MB}MB)", {}
    return True, "", {
        "media_type": "image",
        "mime": mime,
        "ext": AVATAR_MIME_TYPES[mime],
        "max_bytes": MAX_AVATAR_BYTES,
    }


def verify_uploaded_object(plan: dict, head: dict | None,
                           magic: bytes | None = None) -> tuple[bool, str, dict]:
    """Confirm an object the browser uploaded is what we asked for.

    The presigned PUT is not trust: a client can declare ``image/jpeg`` and
    upload anything. This checks the object exists, its real size, and (when we
    could read the first bytes) that the magic bytes agree with the declared
    media type.
    """
    if not head or not head.get("exists"):
        return False, "The upload could not be found. Please try again.", {}

    size = head.get("bytes")
    max_bytes = int(plan.get("max_bytes") or 0)
    if size is None:
        return False, "The upload could not be verified. Please try again.", {}
    if max_bytes and size > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        return False, f"File is too large (max {limit_mb}MB)", {}
    if size <= 0:
        return False, "The uploaded file is empty.", {}

    if magic:
        try:
            from storage import r2

            sniffed = r2.sniff_media_type(magic)
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_feed: magic sniff failed: %s", exc)
            sniffed = None
        # We read bytes, so we can judge them. A sniff that disagrees with the
        # declaration is a mismatch; so is bytes we can READ but cannot
        # identify — an HTML document uploaded as "image/jpeg" is exactly the
        # stored-XSS case this check exists for. Only an unreadable/empty head
        # (some R2 setups refuse ranged GETs) fails open.
        if sniffed != plan.get("media_type"):
            return False, (
                "That file does not look like the type it claims to be."
            ), {}

    return True, "", {
        "bytes": int(size),
        "mime": head.get("content_type") or plan.get("mime"),
        "media_type": plan.get("media_type"),
    }


# ── Public shaping ───────────────────────────────────────────────────────────

def _public_post(row: dict, media: list[dict], viewer: str | None,
                 my_reaction: str | None = None,
                 is_following: bool = False,
                 avatar_url: str | None = None,
                 reactions: dict[str, int] | None = None,
                 earned_badges: list[dict] | None = None,
                 donation: dict | None = None,
                 support: dict | None = None) -> dict:
    """Shape a post for the browser.

    The wallet address is dropped and ownership is reduced to ``is_mine`` — a
    boolean — so a feed response can never leak an address. ``is_following``
    likewise reduces the follow graph to a per-author boolean the viewer owns.
    ``avatar_url`` is a same-origin profile path, never a public R2 URL.
    ``reactions`` is a ``{key: count}`` breakdown (no
    wallet, just tallies) so a card can show WHICH reactions were used.
    """
    if row.get("post_type") == "system_vault_donation":
        donation = donation or {}
        try:
            base_units = int(donation.get("amount_base_units") or 0)
            decimals = max(0, min(36, int(donation.get("token_decimals") or 18)))
            padded = str(base_units).rjust(decimals + 1, "0")
            whole = padded[:-decimals] if decimals else padded
            fraction = padded[-decimals:].rstrip("0") if decimals else ""
            amount = whole + ("." + fraction if fraction else "")
        except (TypeError, ValueError):
            amount = "0"
        tx_hash = str(donation.get("tx_hash") or "").lower()
        tx_url = ("https://celoscan.io/tx/" + tx_hash
                  if re.fullmatch(r"0x[0-9a-f]{64}", tx_hash) else "")
        donor_name = (donation.get("donor_name") or "").strip()
        is_garden_sponsor = donation.get("consent_version") == "garden-sponsor-v1"
        body = (f"{donor_name} contributed {amount} G$ to the KwentoFi Garden sponsor pool."
                if is_garden_sponsor else
                f"{donor_name} donated {amount} G$ to the Community Withdrawal Vault.")
        return {
            "id": row.get("id"),
            "post_type": "system_vault_donation",
            "username": "KwentoFi",
            "body": body,
            "avatar_url": None,
            "media": [],
            "reaction_count": int(row.get("reaction_count") or 0),
            "reaction_breakdown": _clean_breakdown(reactions),
            "comment_count": int(row.get("comment_count") or 0),
            "prompt_id": None,
            "badges": [],
            "my_reaction": my_reaction,
            "is_mine": False,
            "is_following": False,
            "is_edited": False,
            "is_pinned": bool(row.get("is_pinned")),
            "is_interactive": True,
            "created_at": row.get("created_at"),
            "edited_at": None,
            "donor_name": donor_name,
            "amount_gd": amount,
            "linkedin_url": donation.get("linkedin_url") or "",
            "tx_hash": tx_hash,
            "tx_url": tx_url,
            "donation_type": "garden_sponsor" if is_garden_sponsor else "community_vault",
        }
    if row.get("post_type") == "system_support":
        support = support or {}
        tx_hash = str(support.get("tx_hash") or "").lower()
        tx_url = ("https://celoscan.io/tx/" + tx_hash
                  if re.fullmatch(r"0x[0-9a-f]{64}", tx_hash) else "")
        return {
            "id": row.get("id"),
            "post_type": "system_support",
            "username": "KwentoFi",
            "body": row.get("body") or "A community member supported a post with G$.",
            "avatar_url": None,
            "media": [],
            "reaction_count": int(row.get("reaction_count") or 0),
            "reaction_breakdown": _clean_breakdown(reactions),
            "comment_count": int(row.get("comment_count") or 0),
            "prompt_id": None,
            "badges": [],
            "my_reaction": my_reaction,
            "is_mine": False,
            "is_following": False,
            "is_edited": False,
            "is_pinned": bool(row.get("is_pinned")),
            "is_interactive": True,
            "created_at": row.get("created_at"),
            "edited_at": None,
            "donor_username": normalize_username(support.get("sender_username")),
            "author_username": normalize_username(support.get("recipient_username")),
            "amount_gd": str(support.get("amount") or ""),
            "tx_hash": tx_hash,
            "tx_url": tx_url,
        }
    owner = (row.get("wallet_address") or "").lower()
    # Strip a stored `@` (a legacy/imported row): the card draws its own prefix.
    username = (row.get("username") or "").strip().lstrip("@")
    return {
        "id": row.get("id"),
        "post_type": row.get("post_type") or "user",
        "username": username or ANON_LABEL,
        "body": row.get("body") or "",
        "avatar_url": public_avatar_path(username) if avatar_url else None,
        "media": media,
        "reaction_count": int(row.get("reaction_count") or 0),
        "reaction_breakdown": _clean_breakdown(reactions),
        "comment_count": int(row.get("comment_count") or 0),
        "prompt_id": row.get("prompt_id"),
        "badges": list(earned_badges or [])[:3],
        "my_reaction": my_reaction,
        "is_mine": bool(viewer) and owner == viewer.lower(),
        "is_following": bool(is_following),
        "is_edited": bool(row.get("edited_at")),
        "created_at": row.get("created_at"),
        "edited_at": row.get("edited_at"),
        "is_pinned": bool(row.get("is_pinned")),
    }


def _clean_breakdown(raw: dict | None) -> dict[str, int]:
    """Normalise a reaction tally: known keys only, positive counts only.

    Drops unknown keys so a stale row (a reaction removed from ``REACTIONS``)
    can never render a chip the client has no emoji for, and drops zero counts
    so the card does not have to filter.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key in REACTION_KEYS:
        try:
            count = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if count > 0:
            out[key] = count
    return out


def _public_media(row: dict) -> dict:
    return {
        "type": row.get("media_type") or "image",
        "url": row.get("public_url"),
        "poster": row.get("poster_url"),
        "w": row.get("width"),
        "h": row.get("height"),
        "duration": float(row["duration_sec"]) if row.get("duration_sec") else None,
        "mime": row.get("mime"),
    }


def _public_comment(row: dict, viewer: str | None,
                    avatar_url: str | None = None,
                    post_owner: str | None = None,
                    profile_username: str | None = None) -> dict:
    owner = (row.get("wallet_address") or "").lower()
    # Strip a stored `@` for the same reason as `_public_post`: the UI draws
    # the prefix itself, so a legacy row must not render `@@betz`.
    username = (row.get("username") or "").strip().lstrip("@")
    deleted = bool(row.get("is_deleted"))
    # A stored comment username is a historical display snapshot. Only a live,
    # current profile name may create a clickable profile identity; deleted or
    # anonymous comments are never linkable.
    current_profile = None if deleted else normalize_username(profile_username)
    mention = (row.get("reply_to_username_snapshot") or "").strip().lstrip("@")
    return {
        "id": row.get("id"),
        "parent_comment_id": row.get("parent_comment_id"),
        "reply_to_username": mention or None,
        "username": "Deleted comment" if deleted else (username or ANON_LABEL),
        "profile_username": current_profile,
        "body": "This comment was deleted." if deleted else (row.get("body") or ""),
        # Keep historical public avatar behavior when user_data is unavailable,
        # but only profile_username creates a clickable profile identity.
        "avatar_url": (public_avatar_path(current_profile or username)
                       if avatar_url and not deleted and (current_profile or username)
                       else None),
        "is_mine": not deleted and bool(viewer) and owner == viewer.lower(),
        "is_post_author": bool(post_owner) and owner == post_owner.lower() and not deleted,
        "is_deleted": deleted,
        "created_at": row.get("created_at"),
    }


def _avatars_for_wallets(supabase, wallets: list[str]) -> dict[str, bool]:
    """Avatar-presence flag per wallet, in ONE batched query.

    Returns ``{lowercased_wallet: True}``. The wallet and stored R2 URL/key are
    server-side lookup details only; public serializers turn presence into a
    same-origin profile path. A wallet with no avatar simply has no entry.
    """
    wanted = sorted({(w or "").lower() for w in wallets if w})
    if not wanted:
        return {}
    try:
        result = (
            supabase.table("community_feed_avatars")
            .select("wallet_address,r2_key")
            .in_("wallet_address", wanted)
            .execute()
        )
        out: dict[str, bool] = {}
        for row in _rows(result):
            wallet = (row.get("wallet_address") or "").lower()
            key = (row.get("r2_key") or "").strip()
            if wallet and key:
                out[wallet] = True
        return out
    except Exception as exc:  # noqa: BLE001
        # A missing avatar table (migration not yet run) must not break the
        # feed: avatars are decoration, posts are the product.
        logger.warning("community_feed: avatar batch failed: %s", exc)
        return {}


def _current_usernames_for_wallets(supabase, wallets: list[str]) -> dict[str, str]:
    """Batch current profile names for reply mentions; snapshots are fallback."""
    wanted = sorted({str(wallet or "").lower() for wallet in wallets
                     if re.fullmatch(r"0x[a-f0-9]{40}", str(wallet or ""), re.I)})
    names: dict[str, str] = {}
    # Keep PostgREST OR filters bounded; wallet values are strictly validated
    # EVM addresses before being interpolated into the filter expression.
    for start in range(0, len(wanted), 100):
        batch = wanted[start:start + 100]
        clause = ",".join(f"wallet_address.ilike.{address}" for address in batch)
        try:
            result = (
                supabase.table("user_data")
                .select("wallet_address,username")
                .or_(clause)
                .limit(len(batch))
                .execute()
            )
            for row in _rows(result):
                wallet = str(row.get("wallet_address") or "").lower()
                # This helper also serves reply notifications/snapshots, where
                # legacy imported names may not satisfy today's handle regex.
                # Callers that create profile links validate at serialization.
                username = (row.get("username") or "").strip().lstrip("@")
                if wallet in batch and username:
                    names[wallet] = username
        except Exception as exc:  # noqa: BLE001
            logger.info("community_feed: batch mention-name lookup skipped: %s", exc)
    return names


def _cursor_secret() -> bytes:
    """Return the application's existing signing secret for private cursors."""
    try:
        from flask import current_app

        secret = current_app.secret_key
        if secret:
            return str(secret).encode("utf-8")
    except Exception:  # noqa: BLE001 - service tests may run without Flask context
        pass
    # These are the same secret sources and fallback used by main.py. Do not
    # add a cursor-specific setting: cursors must remain valid across workers.
    return str(os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY") or
               "your-secret-key-here").encode("utf-8")


@lru_cache(maxsize=8)
def _reaction_cursor_fernet(secret: bytes):
    """Derive a Fernet key from the existing app secret; never invent a new env."""
    from cryptography.fernet import Fernet

    key_bytes = hashlib.pbkdf2_hmac(
        "sha256", secret, b"goodmarket-community-reactions-v1", iterations=200_000)
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def _encode_reaction_cursor(*, post_id: int, reaction: str | None,
                            created_at: str, wallet: str) -> str:
    """Encrypt the private reaction sort key into an opaque cursor token.

    The wallet is a server-only tie-breaker. It is encrypted rather than merely
    base64 encoded or signed, so it cannot be recovered from a browser cursor.
    """
    payload = json.dumps({
        "version": REACTION_CURSOR_VERSION,
        "post_id": int(post_id),
        "reaction": reaction,
        "created_at": str(created_at),
        "wallet": str(wallet).lower(),
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    token = _reaction_cursor_fernet(_cursor_secret()).encrypt(payload)
    return token.decode("ascii")


def _decode_reaction_cursor(cursor: str, *, post_id: int,
                            reaction: str | None) -> dict | None:
    """Decode and validate a reaction cursor without exposing its contents."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        return None
    try:
        from cryptography.fernet import InvalidToken
    except ImportError:  # pragma: no cover - production dependency is declared
        return None
    try:
        payload = _reaction_cursor_fernet(_cursor_secret()).decrypt(
            cursor.encode("ascii"))
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict) or int(value.get("post_id")) != int(post_id):
            return None
        if value.get("version") != REACTION_CURSOR_VERSION:
            return None
        if value.get("reaction") != reaction:
            return None
        wallet = str(value.get("wallet") or "").lower()
        if not re.fullmatch(r"0x[a-f0-9]{40}", wallet):
            return None
        created_at = str(value.get("created_at") or "")
        if _parse_dt(created_at) is None:
            return None
        return {"created_at": created_at, "wallet": wallet}
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeError, InvalidToken):
        return None


def get_avatar_url(wallet: str | None) -> str | None:
    """One member's same-origin avatar path, or None. Never returns an R2 URL."""
    if not wallet:
        return None
    supabase = _get_supabase()
    if not supabase:
        return None
    has_avatar = _avatars_for_wallets(supabase, [wallet]).get((wallet or "").lower())
    return public_avatar_path(get_username(wallet)) if has_avatar else None


def get_avatar_descriptor(wallet: str | None) -> dict | None:
    """Private storage metadata for one member's current avatar.

    The descriptor stays inside the server route; callers must never serialize
    its R2 key or wallet lookup into a browser response.
    """
    if not wallet:
        return None
    supabase = _get_supabase()
    if not supabase:
        return None
    try:
        result = (
            supabase.table("community_feed_avatars")
            .select("r2_key,mime,bytes")
            .eq("wallet_address", wallet.lower())
            .limit(1)
            .execute()
        )
        rows = _rows(result)
        if not rows:
            return None
        row = rows[0]
        key = (row.get("r2_key") or "").strip()
        if not key:
            return None
        return {
            "r2_key": key,
            "mime": (row.get("mime") or "").strip().lower(),
            "bytes": row.get("bytes"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: avatar descriptor lookup failed: %s", exc)
        return None


# ── Feed reads ───────────────────────────────────────────────────────────────

def _media_for_posts(supabase, post_ids: list[int]) -> dict[int, list[dict]]:
    """One query for every post's media (never one query per post)."""
    out: dict[int, list[dict]] = {pid: [] for pid in post_ids}
    if not post_ids:
        return out
    try:
        result = (
            supabase.table("community_feed_media")
            .select("*")
            .in_("post_id", post_ids)
            .order("position")
            .order("id")
            .execute()
        )
        for row in _rows(result):
            pid = row.get("post_id")
            if pid in out:
                out[pid].append(_public_media(row))
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: media batch failed: %s", exc)
    return out


def _reactions_for_posts(supabase, post_ids: list[int],
                         viewer: str | None) -> dict[int, str]:
    """The viewer's own reaction per post, in ONE batched query."""
    if not post_ids or not viewer:
        return {}
    try:
        result = (
            supabase.table("community_feed_reactions")
            .select("post_id,reaction")
            .in_("post_id", post_ids)
            .ilike("wallet_address", viewer)
            .execute()
        )
        return {
            row["post_id"]: row["reaction"]
            for row in _rows(result)
            if row.get("post_id") is not None
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: reaction batch failed: %s", exc)
        return {}


def _reaction_breakdowns_for_posts(supabase, post_ids: list[int],
                                   ) -> dict[int, dict[str, int]]:
    """Which reactions each post received, in ONE batched query.

    Returns ``{post_id: {reaction_key: count}}`` — tallies only, never a wallet
    (the same no-address rule as ``_reactions_for_posts``). This is what lets a
    card show "👍 ❤️ 😂 12" the way a social feed does instead of a single
    anonymous thumbs-up.

    ``reaction_count`` on the post row stays the AUTHORITATIVE total; this
    breakdown is a best-effort tally of the reaction rows, so a pathological
    post with thousands of reactions may report fewer distinct chips than its
    total. The UI therefore never derives the headline number from the
    breakdown — it only uses it to pick the emoji row.
    """
    if not post_ids:
        return {}
    try:
        result = (
            supabase.table("community_feed_reactions")
            .select("post_id,reaction")
            .in_("post_id", post_ids)
            .limit(REACTION_SCAN_LIMIT)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: reaction breakdown failed: %s", exc)
        return {}
    out: dict[int, dict[str, int]] = {}
    for row in _rows(result):
        post_id = row.get("post_id")
        key = (row.get("reaction") or "").strip().lower()
        if post_id is None or key not in REACTION_KEYS:
            continue
        bucket = out.setdefault(post_id, {})
        bucket[key] = bucket.get(key, 0) + 1
    return out


def _follow_states(supabase, wallets: list[str],
                   viewer: str | None) -> set[str]:
    """Which of these author wallets the viewer follows, in ONE batched query.

    Returns the set of followed wallets (lowercase). One query per feed page,
    never one per card — the N+1 that a per-card `is_following` would cause.
    """
    if not wallets or not viewer:
        return set()
    wanted = sorted({(w or "").lower() for w in wallets if w})
    if not wanted:
        return set()
    try:
        result = (
            supabase.table("community_feed_follows")
            .select("followee_wallet")
            .in_("followee_wallet", wanted)
            .ilike("follower_wallet", viewer)
            .execute()
        )
        return {
            (row.get("followee_wallet") or "").lower()
            for row in _rows(result)
            if row.get("followee_wallet")
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: follow batch failed: %s", exc)
        return set()


def followers_count(wallet: str | None) -> int:
    """How many wallets follow this one. 0 when the wallet is empty."""
    if not wallet:
        return 0
    supabase = _get_supabase()
    if not supabase:
        return 0
    return _count(supabase, "community_feed_follows",
                  [("ilike", "followee_wallet", wallet)])


def following_count(wallet: str | None) -> int:
    """How many wallets this one follows."""
    if not wallet:
        return 0
    supabase = _get_supabase()
    if not supabase:
        return 0
    return _count(supabase, "community_feed_follows",
                  [("ilike", "follower_wallet", wallet)])


def _is_following(supabase, follower: str, followee: str) -> bool:
    """Whether ``follower`` already follows ``followee`` (both wallets)."""
    try:
        result = (
            supabase.table("community_feed_follows")
            .select("follower_wallet")
            .ilike("follower_wallet", follower)
            .ilike("followee_wallet", followee)
            .limit(1)
            .execute()
        )
        return bool(_rows(result))
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: follow lookup failed: %s", exc)
        return False


def follow_user(wallet: str | None, username: str | None) -> dict:
    """Follow the member behind ``username``.

    The client names a USERNAME only; the wallet is resolved server-side and is
    never echoed back. Idempotent: following an already-followed member is a
    SUCCESS, so a double-tap converges instead of surfacing an error. Refuses a
    self-follow, a banned follower and a following-count over FOLLOW_LIMIT.
    """
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    if is_banned(wallet):
        return {"success": False, "error": "You cannot follow in the community.",
                "code": "banned"}

    target = wallet_for_username(username)
    if not target:
        return {"success": False, "error": "Member not found",
                "code": "not_found"}
    follower = wallet.lower()
    followee = target.lower()
    if follower == followee:
        return {"success": False, "error": "You cannot follow yourself.",
                "code": "self"}

    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    if _is_following(supabase, follower, followee):
        # Idempotent success — the state the caller asked for already holds.
        return {"success": True, "following": True,
                "follower_count": followers_count(followee)}

    if following_count(follower) >= FOLLOW_LIMIT:
        return {"success": False, "error": "You are following too many members.",
                "code": "limit"}

    try:
        supabase.table("community_feed_follows").insert({
            "follower_wallet": follower,
            "followee_wallet": followee,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        # A unique violation means a concurrent insert won the race; the caller
        # wanted to follow, and the member IS now followed — treat it as success
        # rather than reporting a failure for a state that already holds.
        logger.warning("community_feed: follow insert failed: %s", exc)

    # The panel excludes members the viewer already follows, so a stale cache
    # would keep showing the member they just followed.
    invalidate_suggestions_cache(follower)
    return {"success": True, "following": True,
            "follower_count": followers_count(followee)}


def unfollow_user(wallet: str | None, username: str | None) -> dict:
    """Stop following the member behind ``username``. Idempotent."""
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}

    target = wallet_for_username(username)
    if not target:
        return {"success": False, "error": "Member not found",
                "code": "not_found"}

    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    try:
        supabase.table("community_feed_follows").delete().ilike(
            "follower_wallet", wallet).ilike(
            "followee_wallet", target).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: unfollow delete failed: %s", exc)
        return {"success": False, "error": "Could not update your follows",
                "code": "db"}

    # Unfollowing frees a slot in the panel, so drop the viewer's cached copy.
    invalidate_suggestions_cache(wallet)
    return {"success": True, "following": False,
            "follower_count": followers_count(target)}


def get_follow_counts(username: str | None) -> dict:
    """Public follower/following counts for a member's profile."""
    target = wallet_for_username(username)
    if not target:
        return {"success": False, "error": "Member not found",
                "code": "not_found"}
    return {
        "success": True,
        "followers": followers_count(target),
        "following": following_count(target),
    }


def _donation_rows_for_posts(supabase, post_ids: list) -> dict[int, dict]:
    if not post_ids:
        return {}
    try:
        rows = _rows(supabase.table("community_vault_donations")
                     .select("feed_post_id,donor_name,linkedin_url,amount_base_units,token_decimals,tx_hash,consent_version")
                     .in_("feed_post_id", post_ids)
                     .eq("status", "confirmed").execute())
        result = {}
        for row in rows:
            try:
                result[int(row.get("feed_post_id"))] = row
            except (TypeError, ValueError):
                continue
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: pinned donation metadata load failed: %s", exc)
        return {}


def _support_rows_for_posts(supabase, post_ids: list) -> dict[int, dict]:
    """Load confirmed post-support metadata in one batch without exposing wallets."""
    if not post_ids:
        return {}
    try:
        rows = _rows(supabase.table("community_chat_tips")
                     .select("feed_post_id,amount,tx_hash,sender_username,recipient_username")
                     .in_("feed_post_id", post_ids)
                     .eq("context", "post_support").execute())
        result = {}
        for row in rows:
            try:
                result[int(row.get("feed_post_id"))] = row
            except (TypeError, ValueError):
                continue
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: post support metadata load failed: %s", exc)
        return {}


def get_pinned_donation_feed(viewer: str | None = None,
                             cursor: int | None = None,
                             limit: int | None = None) -> dict:
    """Return one keyset page of verified, pinned donor acknowledgments."""
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "posts": []}
    try:
        size = int(limit or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    size = max(1, min(MAX_PAGE_SIZE, size))
    try:
        query = (supabase.table("community_feed_posts").select("*")
                 .in_("post_type", ["system_vault_donation", "system_support"])
                 .eq("is_pinned", True).eq("is_hidden", False)
                 .eq("is_deleted", False))
        if cursor:
            query = query.lt("id", int(cursor))
        rows = _rows(query.order("id", desc=True).limit(size + 1).execute())
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: pinned donor query failed: %s", exc)
        return {"success": False, "error": "Could not load pinned donor acknowledgments", "posts": []}
    has_more = len(rows) > size
    rows = rows[:size]
    post_ids = [row.get("id") for row in rows if row.get("id") is not None]
    donations = _donation_rows_for_posts(supabase, post_ids)
    supports = _support_rows_for_posts(supabase, post_ids)
    viewer_reactions = _reactions_for_posts(supabase, post_ids, viewer)
    reaction_breakdowns = _reaction_breakdowns_for_posts(supabase, post_ids)
    posts = []
    for row in rows:
        post_id = row.get("id")
        if post_id is None:
            continue
        post_id = int(post_id)
        donation = donations.get(post_id)
        support = supports.get(post_id)
        if row.get("post_type") == "system_vault_donation":
            if not donation or not donation.get("tx_hash"):
                continue
        elif row.get("post_type") == "system_support":
            if not support or not support.get("tx_hash"):
                continue
        else:
            continue
        posts.append(_public_post(
            row, [], viewer,
            my_reaction=viewer_reactions.get(row.get("id")),
            reactions=reaction_breakdowns.get(row.get("id")),
            donation=donation,
            support=support,
        ))
    return {
        "success": True,
        "posts": posts,
        "next_cursor": rows[-1].get("id") if rows and has_more else None,
        "has_more": has_more,
    }


def _missing_feed_filter_columns(exc: Exception) -> set[str]:
    """Return feed filter columns explicitly reported missing by PostgREST.

    Older deployments may still have the base ``community_feed_posts`` table
    without the optional pinning/type migration. Do not hide arbitrary query
    failures: only recognize an undefined-column/schema-cache error naming one
    of the filters that this function can safely omit for legacy user posts.
    """
    message = str(exc).lower()
    if not any(marker in message for marker in (
            "42703", "pgrst204", "does not exist", "could not find",
            "schema cache", "undefined column")):
        return set()
    return {column for column in ("post_type", "is_pinned")
            if column in message}


def get_feed(viewer: str | None = None, cursor: int | None = None,
             limit: int | None = None, author: str | None = None,
             prompt_id: str | None = None) -> dict:
    """One keyset page of visible posts, newest first.

    Keyset (``id < cursor``) rather than OFFSET: offset paging over a live feed
    duplicates and skips rows as new posts land.
    """
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "posts": []}

    try:
        size = int(limit or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    size = max(1, min(MAX_PAGE_SIZE, size))

    use_post_type_filter = True
    use_pin_filter = not author
    while True:
        try:
            query = supabase.table("community_feed_posts").select("*")
            if use_post_type_filter:
                query = query.in_(
                    "post_type", ["user", "system_support", "system_vault_donation"])
            query = query.eq("is_deleted", False).eq("is_hidden", False)
            # Keep pinned posts out of the main stream when the pin migration
            # is present. NULL means an ordinary legacy post, not a pinned one.
            if use_pin_filter:
                query = query.or_("is_pinned.eq.false,is_pinned.is.null")
            if author:
                query = query.ilike("wallet_address", author)
            if prompt_id:
                from .daily_prompts import filter_prompt_posts
                query = filter_prompt_posts(query, prompt_id)
            if cursor:
                query = query.lt("id", int(cursor))
            # Fetch one extra row to know whether another page exists.
            result = query.order("id", desc=True).limit(size + 1).execute()
            rows = _rows(result)
            break
        except Exception as exc:  # noqa: BLE001
            missing = _missing_feed_filter_columns(exc)
            dropped = set()
            if use_post_type_filter and "post_type" in missing:
                use_post_type_filter = False
                dropped.add("post_type")
            if use_pin_filter and "is_pinned" in missing:
                use_pin_filter = False
                dropped.add("is_pinned")
            if not dropped:
                logger.error("community_feed: feed query failed: %s", exc)
                return {"success": False, "error": "Could not load the feed", "posts": []}
            logger.warning(
                "community_feed: retrying feed without unavailable legacy columns: %s",
                ", ".join(sorted(dropped)))

    has_more = len(rows) > size
    rows = rows[:size]
    post_ids = [r.get("id") for r in rows if r.get("id") is not None]
    media = _media_for_posts(supabase, post_ids)
    reactions = _reactions_for_posts(supabase, post_ids, viewer)
    # ONE batched query for "which of these authors does the viewer follow",
    # never one per card.
    followed = _follow_states(
        supabase, [r.get("wallet_address") for r in rows], viewer)
    # ONE batched query for the authors' profile pictures, likewise per page.
    avatars = _avatars_for_wallets(
        supabase, [r.get("wallet_address") for r in rows])
    # ONE batched query for which reactions each post received (tallies only).
    breakdowns = _reaction_breakdowns_for_posts(supabase, post_ids)
    # ONE batched query for badge metadata; migrations being absent or a badge
    # read failing only yields empty chips and never interrupts the feed.
    from . import badges as community_badges
    badges_by_wallet = community_badges.public_badges_for_wallets(
        [r.get("wallet_address") for r in rows], client=supabase)
    supports = _support_rows_for_posts(supabase, post_ids)
    donations = _donation_rows_for_posts(supabase, post_ids)

    posts = [
        _public_post(
            row, media.get(row.get("id"), []), viewer,
            reactions.get(row.get("id")),
            is_following=(row.get("wallet_address") or "").lower() in followed,
            avatar_url=avatars.get((row.get("wallet_address") or "").lower()),
            reactions=breakdowns.get(row.get("id")),
            earned_badges=badges_by_wallet.get((row.get("wallet_address") or "").lower(), []),
            support=supports.get(row.get("id")),
            donation=donations.get(row.get("id")),
        )
        for row in rows
    ]
    return {
        "success": True,
        "posts": posts,
        "next_cursor": posts[-1]["id"] if posts and has_more else None,
        "has_more": has_more,
    }


def get_discover_feed(viewer: str | None, before_id: int | None = None,
                      limit: int | None = None) -> dict:
    """Return older, public member posts this signed-in viewer has not seen.

    The ordinary feed remains chronological and unchanged. Candidate windows
    use a descending ID keyset and query the viewer's impression rows in batches;
    this avoids random ordering, OFFSET drift, and an unbounded seen-ID payload.
    """
    if not viewer:
        return {"success": False, "error": "Sign in to discover unseen posts.",
                "code": "auth", "posts": []}
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "posts": []}
    try:
        size = max(1, min(MAX_PAGE_SIZE, int(limit or DEFAULT_PAGE_SIZE)))
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE

    rows = []
    scan_cursor = int(before_id) if before_id else None
    next_cursor = None
    has_more = False
    wallet = str(viewer).strip().lower()
    try:
        # Bound work even for a highly active account that has viewed nearly all
        # old posts. Each round examines at most 10x the requested page size.
        for _ in range(10):
            query = (supabase.table("community_feed_posts").select("*")
                     .eq("post_type", "user").eq("visibility", "public")
                     .eq("is_deleted", False).eq("is_hidden", False))
            if scan_cursor:
                query = query.lt("id", scan_cursor)
            candidates = _rows(query.order("id", desc=True)
                               .limit(min(100, size * 3 + 1)).execute())
            if not candidates:
                break
            candidate_ids = [int(row["id"]) for row in candidates if row.get("id") is not None]
            seen_rows = _rows(supabase.table("community_feed_post_views")
                              .select("post_id").ilike("wallet_address", wallet)
                              .in_("post_id", candidate_ids).execute())
            seen = {int(row["post_id"]) for row in seen_rows
                    if row.get("post_id") is not None}
            unseen = [row for row in candidates if int(row["id"]) not in seen]
            rows.extend(unseen)
            if len(rows) > size:
                has_more = True
                rows = rows[:size]
                break
            scan_cursor = int(candidates[-1]["id"])
            if len(candidates) < min(100, size * 3 + 1):
                break
        page_rows = rows[:size]
        post_ids = [row.get("id") for row in page_rows if row.get("id") is not None]
        media = _media_for_posts(supabase, post_ids)
        reactions = _reactions_for_posts(supabase, post_ids, viewer)
        followed = _follow_states(supabase,
                                  [row.get("wallet_address") for row in page_rows], viewer)
        avatars = _avatars_for_wallets(supabase,
                                       [row.get("wallet_address") for row in page_rows])
        breakdowns = _reaction_breakdowns_for_posts(supabase, post_ids)
        from . import badges as community_badges
        badges_by_wallet = community_badges.public_badges_for_wallets(
            [row.get("wallet_address") for row in page_rows], client=supabase)
        supports = _support_rows_for_posts(supabase, post_ids)
        posts = [_public_post(
            row, media.get(row.get("id"), []), viewer, reactions.get(row.get("id")),
            is_following=(row.get("wallet_address") or "").lower() in followed,
            avatar_url=avatars.get((row.get("wallet_address") or "").lower()),
            reactions=breakdowns.get(row.get("id")),
            earned_badges=badges_by_wallet.get((row.get("wallet_address") or "").lower(), []),
            support=supports.get(row.get("id")),
        ) for row in page_rows]
        return {"success": True, "posts": posts,
                "next_cursor": posts[-1]["id"] if posts and has_more else None,
                "has_more": has_more}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: discover query failed: %s", exc)
        return {"success": False, "error": "Could not load Discover posts.", "posts": []}


def record_post_views(viewer: str | None, post_ids: list[int]) -> dict:
    """Idempotently record only currently visible public user posts."""
    if not viewer:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    if not post_ids or len(post_ids) > 50:
        return {"success": False, "error": "Choose between 1 and 50 posts.",
                "code": "limit"}
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}
    try:
        ids = list(dict.fromkeys(int(value) for value in post_ids))
        if any(value < 1 for value in ids):
            raise ValueError("post ids must be positive")
        rows = _rows(supabase.table("community_feed_posts").select("id")
                     .in_("id", ids).eq("post_type", "user")
                     .eq("visibility", "public").eq("is_deleted", False)
                     .eq("is_hidden", False).execute())
        visible_ids = {int(row["id"]) for row in rows if row.get("id") is not None}
        if visible_ids:
            payload = [{"wallet_address": str(viewer).strip().lower(),
                        "post_id": post_id} for post_id in sorted(visible_ids)]
            supabase.table("community_feed_post_views").upsert(
                payload, on_conflict="wallet_address,post_id",
                ignore_duplicates=True).execute()
        return {"success": True, "recorded": len(visible_ids)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: view impression write failed: %s", exc)
        return {"success": False, "error": "Could not record post views.",
                "code": "db"}


def get_post(post_id: int, viewer: str | None = None) -> dict:
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable"}
    try:
        result = (
            supabase.table("community_feed_posts")
            .select("*")
            .eq("id", int(post_id))
            .limit(1)
            .execute()
        )
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: post lookup failed: %s", exc)
        return {"success": False, "error": "Could not load the post"}
    if not rows or rows[0].get("is_deleted") or rows[0].get("is_hidden"):
        return {"success": False, "error": "Post not found", "code": "not_found"}
    row = rows[0]
    if row.get("post_type") == "system_vault_donation":
        donations = _donation_rows_for_posts(supabase, [row.get("id")])
        donation = donations.get(int(row["id"]))
        if not donation or not donation.get("tx_hash"):
            return {"success": False, "error": "Post not found", "code": "not_found"}
        reactions = _reactions_for_posts(supabase, [row.get("id")], viewer)
        breakdown = _reaction_breakdowns_for_posts(supabase, [row.get("id")])
        return {"success": True,
                "post": _public_post(
                    row, [], viewer, reactions.get(row.get("id")),
                    reactions=breakdown.get(row.get("id")), donation=donation)}
    if row.get("post_type") == "system_support":
        support = _support_rows_for_posts(supabase, [row.get("id")]).get(int(row["id"]))
        if not support or not support.get("tx_hash"):
            return {"success": False, "error": "Post not found", "code": "not_found"}
        reactions = _reactions_for_posts(supabase, [row.get("id")], viewer)
        breakdown = _reaction_breakdowns_for_posts(supabase, [row.get("id")])
        return {"success": True,
                "post": _public_post(
                    row, [], viewer, reactions.get(row.get("id")),
                    reactions=breakdown.get(row.get("id")), support=support)}
    media = _media_for_posts(supabase, [row.get("id")]).get(row.get("id"), [])
    reactions = _reactions_for_posts(supabase, [row.get("id")], viewer)
    avatar = _avatars_for_wallets(
        supabase, [row.get("wallet_address")]).get(
            (row.get("wallet_address") or "").lower())
    breakdown = _reaction_breakdowns_for_posts(
        supabase, [row.get("id")]).get(row.get("id"))
    from . import badges as community_badges
    earned = community_badges.public_badges_for_wallet(
        row.get("wallet_address"), client=supabase)[:3]
    return {"success": True,
            "post": _public_post(row, media, viewer, reactions.get(row.get("id")),
                                 avatar_url=avatar, reactions=breakdown,
                                 earned_badges=earned)}


# ── Public profile / per-user views ──────────────────────────────────────────
# A profile is addressed by USERNAME, never by wallet. The username is resolved
# to its owner wallet server-side (the wallet is the ownership key for the
# queries below) and that wallet is NEVER placed in a response — the same rule
# as `_public_post`. These views are therefore wallet-leak-proof by
# construction: there is no code path that copies a wallet into the output.

# Same rule as the app-wide `POST /api/user/username` gate and the chatroom.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")


def normalize_username(username: str | None) -> str | None:
    """Strip a leading ``@`` and reject anything that is not a username."""
    name = (username or "").strip().lstrip("@")
    if not _USERNAME_RE.fullmatch(name):
        return None
    return name


def public_avatar_path(username: str | None) -> str | None:
    """Build the public same-origin image route for a valid community handle."""
    name = normalize_username(username)
    return f"/community/api/profile/{name}/avatar" if name else None


# ── "Suggested for you" ──────────────────────────────────────────────────────
# The right-column panel on /community. Ranks members by how many DISTINCT DAYS
# they were active in a recent window (see sql/community_suggested_members.sql),
# then resolves usernames in ONE batched query and DROPS the wallet: the panel
# is a set of names, never a list of addresses — the same rule as the feed,
# search and profile.
#
# The ranking moves slowly, so a short per-process cache keeps the panel off the
# per-page-load cost path. The cache key includes the VIEWER because
# `is_following` is viewer-specific; a follow toggle invalidates it (see
# `invalidate_suggestions_cache`). A plain dict is enough here — the GIL makes a
# get/set of a whole entry atomic, and a torn entry is impossible.
_SUGGESTIONS_CACHE: dict[tuple[str, int], tuple[float, dict]] = {}


def _suggestions_cache_get(viewer_key: str, size: int):
    if SUGGESTIONS_CACHE_TTL_SECONDS <= 0:
        return None
    entry = _SUGGESTIONS_CACHE.get((viewer_key, size))
    if not entry:
        return None
    expires_at, payload = entry
    if time.monotonic() >= expires_at:
        _SUGGESTIONS_CACHE.pop((viewer_key, size), None)
        return None
    return payload


def _suggestions_cache_set(viewer_key: str, size: int, payload: dict) -> None:
    if SUGGESTIONS_CACHE_TTL_SECONDS <= 0:
        return
    _SUGGESTIONS_CACHE[(viewer_key, size)] = (
        time.monotonic() + SUGGESTIONS_CACHE_TTL_SECONDS, payload)


def invalidate_suggestions_cache(viewer: str | None = None) -> None:
    """Drop the cached panel for one viewer, or for everyone when ``None``.

    Called after a follow/unfollow so a member who just followed somebody does
    not keep seeing them under "Suggested for you" until the TTL lapses.
    """
    if viewer is None:
        _SUGGESTIONS_CACHE.clear()
        return
    key = str(viewer or "").lower()
    for cache_key in [k for k in _SUGGESTIONS_CACHE if k[0] == key]:
        _SUGGESTIONS_CACHE.pop(cache_key, None)


def _banned_wallets(supabase, wallets: list[str]) -> set[str]:
    """Batched ban lookup — the same rule as `is_banned`, in ONE query.

    A per-candidate `is_banned()` call would be an N+1 on every panel load. The
    filter is `ilike` (like `is_banned`) so a legacy non-lowercase ban row still
    matches. Fails OPEN on a read error, exactly like `is_banned`: a moderation
    outage must not empty the whole panel.
    """
    wanted = sorted({str(w or "").lower() for w in wallets if w})
    if not wanted:
        return set()
    banned: set[str] = set()
    # Keep the PostgREST OR filter bounded; wallets are validated EVM addresses
    # by the time they reach here, but the batch size is the real guard.
    for start in range(0, len(wanted), 100):
        batch = wanted[start:start + 100]
        clause = ",".join(f"wallet_address.ilike.{address}" for address in batch)
        try:
            result = (
                supabase.table("community_feed_bans")
                .select("wallet_address")
                .or_(clause)
                .limit(len(batch))
                .execute()
            )
            for row in _rows(result):
                banned.add(str(row.get("wallet_address") or "").lower())
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_feed: batched ban lookup failed: %s", exc)
    return banned


def _fill_with_newest(supabase, ranked: list[tuple[str, int]],
                      size: int) -> list[tuple[str, int]]:
    """Append newest username-bearing members until ``size`` is reached.

    The cold-start fallback: a young community may not have enough members who
    clear the activity floor, and an almost-empty panel is worse than a
    chronological one. This is clearly a fallback, not the ranking.
    """
    seen = {wallet for wallet, _ in ranked}
    if len(ranked) >= size:
        return ranked
    try:
        rows = _rows(supabase.rpc("community_newest_members", {
            # Over-ask: some of these get filtered (banned / already followed /
            # no username) before the panel is built.
            "result_limit": max(size - len(ranked), 1) * 2,
        }).execute())
    except Exception as exc:  # noqa: BLE001
        logger.info("community_feed: newest-members fallback skipped: %s", exc)
        return ranked
    for row in rows:
        wallet = str(row.get("wallet_address") or "").lower()
        if not wallet or wallet in seen:
            continue
        seen.add(wallet)
        ranked.append((wallet, 0))
        if len(ranked) >= size:
            break
    return ranked


def suggested_members(viewer: str | None = None, limit: int | None = None) -> dict:
    """Active members to suggest to ``viewer``, most consistently active first.

    Returns ``{success, members, enabled, source}``. ``members`` carries
    usernames only — the wallet is the lookup key and is dropped here, exactly
    like the feed, search and profile responses. Never raises.
    """
    if not SUGGESTIONS_ENABLED:
        return {"success": True, "members": [], "enabled": False,
                "source": "disabled"}

    try:
        size = int(limit or SUGGESTIONS_LIMIT)
    except (TypeError, ValueError):
        size = SUGGESTIONS_LIMIT
    size = max(1, min(SUGGESTIONS_LIMIT, size))

    viewer_key = str(viewer or "").lower()
    cached = _suggestions_cache_get(viewer_key, size)
    if cached is not None:
        return cached

    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable",
                "members": [], "source": "error"}

    try:
        rows = _rows(supabase.rpc("community_suggested_members", {
            "window_days": SUGGESTIONS_WINDOW_DAYS,
            "active_floor_days": SUGGESTIONS_ACTIVE_FLOOR_DAYS,
            "result_limit": size,
        }).execute())
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: suggested-members ranking failed: %s", exc)
        return {"success": False, "error": "Could not load suggestions",
                "members": [], "source": "error"}

    ranked: list[tuple[str, int]] = []
    for row in rows:
        wallet = str(row.get("wallet_address") or "").lower()
        if not wallet:
            continue
        try:
            active_days = int(row.get("active_days") or 0)
        except (TypeError, ValueError):
            active_days = 0
        ranked.append((wallet, active_days))

    active_found = len(ranked)
    ranked = _fill_with_newest(supabase, ranked, size)
    if not active_found:
        source = "newest"
    elif len(ranked) > active_found:
        source = "mixed"
    else:
        source = "activity"

    # The viewer is never suggested to themselves (the server would refuse the
    # follow anyway), and neither is a member they already follow.
    candidates = [wallet for wallet, _ in ranked
                  if wallet and wallet != viewer_key]
    if not candidates:
        return {"success": True, "members": [], "enabled": True, "source": source}

    usernames = _current_usernames_for_wallets(supabase, candidates)
    # A member without a username cannot be shown: the row renders `@name`, and
    # the only fallback would be a wallet-derived label, which the repo forbids
    # (ANON_LABEL is a fixed string, never short_wallet()).
    candidates = [wallet for wallet in candidates if usernames.get(wallet)]
    if not candidates:
        return {"success": True, "members": [], "enabled": True, "source": source}

    banned = _banned_wallets(supabase, candidates)
    candidates = [wallet for wallet in candidates if wallet not in banned]

    # `_follow_states` is the same batched helper the feed uses for its cards,
    # so this stays ONE query rather than one per row.
    followed = _follow_states(supabase, candidates, viewer)
    candidates = [wallet for wallet in candidates if wallet not in followed]

    avatars = _avatars_for_wallets(supabase, candidates)
    days_by_wallet = dict(ranked)

    members = []
    seen = set()
    for wallet in candidates:
        username = usernames.get(wallet) or ""
        key = username.lower()
        if not username or key in seen:
            continue
        seen.add(key)
        members.append({
            "username": username,
            "avatar_url": (public_avatar_path(username)
                           if avatars.get(wallet) else None),
            "active_days": days_by_wallet.get(wallet, 0),
            "window_days": SUGGESTIONS_WINDOW_DAYS,
            "is_following": False,
        })
        if len(members) >= size:
            break

    payload = {"success": True, "members": members, "enabled": True,
               "source": source}
    _suggestions_cache_set(viewer_key, size, payload)
    return payload


# Public member search. A user may type a partial name, so the term is
# normalised to a safe subset and the WILDCARD is added server-side: a raw
# `%`/`_` from the client would otherwise widen the match (PostgREST `ilike`
# treats both as wildcards), turning a search box into a table scan.
SEARCH_MIN_CHARS = 2
SEARCH_LIMIT = 12
_SEARCH_TERM_RE = re.compile(r"[^A-Za-z0-9_]")


def normalize_search_term(term: str | None) -> str | None:
    """Strip to the characters a username can contain; None when too short."""
    cleaned = _SEARCH_TERM_RE.sub("", (term or "").strip().lstrip("@"))
    if len(cleaned) < SEARCH_MIN_CHARS:
        return None
    return cleaned[:24]


def _escape_like_literal(value: str) -> str:
    """Escape SQL LIKE metacharacters so a username remains a literal value."""
    return (value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_pattern(term: str) -> str:
    """Wrap a search term for PostgREST ``ilike``, escaping LIKE metacharacters.

    ``_`` IS a legal username character, so it cannot be stripped like ``%`` —
    but it is also a single-character wildcard, so a raw term would match names
    the user never asked for. Escaping it (SQL LIKE's default escape is ``\\``)
    keeps the search literal.
    """
    escaped = _escape_like_literal(term)
    return f"%{escaped}%"


def _search_or_clause(term: str) -> str:
    """PostgREST ``or`` clause matching ``term`` with OR without a stored ``@``.

    Kept for callers that need an explicit disjunction. The feed's own search
    does NOT need it: ``_like_pattern`` wraps the term in ``%...%``, and ``%``
    matches any run of characters — including a stored leading ``@`` — so the
    single pattern already finds ``@betz`` from ``bet``.
    """
    return (f"username.ilike.{_like_pattern(term)},"
            f"username.ilike.{_like_pattern('@' + term)}")


def username_variants(name: str) -> list[str]:
    """The stored spellings a username may have: plain, and ``@``-prefixed.

    The username form validates to ``[A-Za-z0-9_]{3,24}``, so ``@`` should never
    be stored — but a row written or imported before that gate can carry one.
    Resolution and search therefore try both spellings instead of assuming one,
    so a member is found whether or not the stored name keeps the prefix.
    """
    cleaned = (name or "").strip().lstrip("@")
    if not cleaned:
        return []
    return [cleaned, "@" + cleaned]


def search_users(term: str | None, limit: int | None = None) -> dict:
    """Public member search by username.

    Returns usernames only — the wallet is the ownership key and is dropped
    here, exactly like the feed and profile responses, so the search box can
    never become an address oracle. LIKE metacharacters in the caller's term are
    escaped before the server adds its own wildcards.
    """
    cleaned = normalize_search_term(term)
    if not cleaned:
        return {"success": True, "users": [], "term": (term or "").strip()}

    try:
        size = int(limit or SEARCH_LIMIT)
    except (TypeError, ValueError):
        size = SEARCH_LIMIT
    size = max(1, min(SEARCH_LIMIT, size))

    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable",
                "users": [], "term": cleaned}

    try:
        result = (
            supabase.table("user_data")
            .select("username,wallet_address")
            .ilike("username", _like_pattern(cleaned))
            .order("username")
            .limit(size)
            .execute()
        )
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: user search failed: %s", exc)
        return {"success": False, "error": "Could not search members",
                "users": [], "term": cleaned}

    # ONE batched avatar lookup for the matched members. The wallet is used as
    # the lookup KEY only and is dropped from the result, like every other feed
    # response — the search box must never become an address oracle.
    avatars = _avatars_for_wallets(
        supabase, [row.get("wallet_address") for row in rows])

    users = []
    seen = set()
    for row in rows:
        # A stored name may keep a leading `@` (a legacy/imported row). Strip it
        # for display: the UI renders its own `@` prefix, so keeping the stored
        # one would show `@@betz`.
        username = (row.get("username") or "").strip().lstrip("@")
        key = username.lower()
        # A row may carry no username (legacy) or the same name twice; the
        # public view is a set of names, never a list of accounts.
        if not username or key in seen:
            continue
        seen.add(key)
        users.append({
            "username": username,
            "avatar_url": (public_avatar_path(username)
                           if avatars.get((row.get("wallet_address") or "").lower())
                           else None),
        })
    return {"success": True, "users": users, "term": cleaned}


def wallet_for_username(username: str | None) -> str | None:
    """Resolve a public username to its owner wallet (server-side only).

    Tries both stored spellings (plain and ``@``-prefixed) so a legacy row is
    still reachable by the name the member actually types. LIKE metacharacters
    are escaped and the returned row is verified to match the requested handle.
    """
    name = normalize_username(username)
    if not name:
        return None
    supabase = _get_supabase()
    if not supabase:
        return None
    for variant in username_variants(name):
        try:
            result = (
                supabase.table("user_data")
                .select("username,wallet_address")
                .ilike("username", _escape_like_literal(variant))
                .limit(1)
                .execute()
            )
            rows = _rows(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_feed: profile wallet lookup failed: %s", exc)
            return None
        if rows:
            matched = normalize_username(rows[0].get("username"))
            wallet = (rows[0].get("wallet_address") or "").strip()
            if matched and matched.casefold() == name.casefold() and wallet:
                return wallet
    return None


def _count(supabase, table: str, filters: list[tuple[str, str, object]],
           select: str = "*") -> int:
    """Bounded row count. Falls back to len(rows) when the client gives no count.

    ``filters`` are ``(op, column, value)`` triples; ``op`` is ``"eq"`` or
    ``"ilike"``. The wallet is matched with ``ilike`` because sessions carry
    checksummed addresses while rows are stored lowercase (the documented
    case-mismatch bug class).

    ``select`` is the projected column list. The count itself comes from
    PostgREST's ``count="exact"`` and is independent of the projection, so a
    caller may name a narrow column to keep the payload small — but it MUST
    name one the table actually has. The default ``"*"`` is safe for any table:
    this helper must never hard-code a column, because
    ``community_feed_follows`` is keyed by its wallet pair and has no ``id``,
    and asking PostgREST for a column that does not exist fails the WHOLE
    request (``42703``) — which the ``except`` below turns into a silent 0, so
    the follower counts read as zero while the follow itself succeeds.
    """
    try:
        query = supabase.table(table).select(select, count="exact")
        for op, column, value in filters:
            query = getattr(query, op)(column, value)
        result = query.limit(1000).execute()
        count = getattr(result, "count", None)
        if count is None:
            count = len(_rows(result))
        return int(count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: count on %s failed: %s", table, exc)
        return 0


def get_public_profile(username: str | None,
                       viewer: str | None = None) -> dict:
    """Username-scoped public profile: identity + feed activity counts.

    Contains NO wallet, balance or private activity. ``post_count`` /
    ``album_count`` / ``media_count`` come from the feed's own tables; the
    public message count is owned by the chatroom and is hydrated by the page
    from the chatroom's own endpoint, so neither module reads the other's data.

    ``viewer`` (a wallet, or None for a guest) only decides the viewer's OWN
    ``is_following`` / ``is_self`` booleans — it never changes what is exposed
    about the member.
    """
    name = normalize_username(username)
    if not name:
        return {"success": False, "error": "Member not found", "code": "not_found"}

    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    wallet = wallet_for_username(name)
    if not wallet:
        return {"success": False, "error": "Member not found", "code": "not_found"}

    joined_at = None
    try:
        result = (
            supabase.table("user_data")
            .select("username,created_at")
            .ilike("wallet_address", wallet)
            .limit(1)
            .execute()
        )
        rows = _rows(result)
        if rows:
            joined_at = rows[0].get("created_at")
            # Strip a stored `@` so the page shows `@betz`, never `@@betz`.
            stored = (rows[0].get("username") or "").strip().lstrip("@")
            name = stored or name
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: profile identity read failed: %s", exc)

    post_count = _count(
        supabase, "community_feed_posts",
        [("ilike", "wallet_address", wallet),
         ("eq", "is_deleted", False),
         ("eq", "is_hidden", False)],
        select="id")

    album_count = 0
    media_count = 0
    try:
        posts = (
            supabase.table("community_feed_posts")
            .select("id")
            .ilike("wallet_address", wallet)
            .eq("is_deleted", False)
            .eq("is_hidden", False)
            .order("id", desc=True)
            .limit(200)
            .execute()
        )
        ids = [r.get("id") for r in _rows(posts) if r.get("id") is not None]
        media = _media_for_posts(supabase, ids)
        media_count = sum(len(items) for items in media.values())
        album_count = sum(1 for items in media.values() if items)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: profile album count failed: %s", exc)

    followers = followers_count(wallet)
    following = following_count(wallet)
    is_self = bool(viewer) and viewer.lower() == wallet.lower()
    is_following = bool(viewer) and not is_self and _is_following(
        supabase, viewer.lower(), wallet.lower())
    avatar = _avatars_for_wallets(supabase, [wallet]).get(wallet.lower())

    return {
        "success": True,
        "profile": {
            "username": name,
            "avatar_url": public_avatar_path(name) if avatar else None,
            "joined_at": joined_at,
            "post_count": post_count,
            "album_count": album_count,
            "media_count": media_count,
            "followers": followers,
            "following": following,
            "is_following": is_following,
            "is_self": is_self,
        },
    }


def get_user_posts(username: str | None, viewer: str | None = None,
                   cursor: int | None = None, limit: int | None = None) -> dict:
    """One keyset page of a member's visible posts, newest first."""
    name = normalize_username(username)
    if not name:
        return {"success": False, "error": "Member not found",
                "code": "not_found", "posts": []}
    wallet = wallet_for_username(name)
    if not wallet:
        return {"success": False, "error": "Member not found",
                "code": "not_found", "posts": []}
    return get_feed(viewer=viewer, cursor=cursor, limit=limit, author=wallet)


def get_user_album(username: str | None, viewer: str | None = None,
                   cursor: int | None = None, limit: int | None = None) -> dict:
    """A member's uploaded media, grouped into ALBUMS.

    An album is a POST that carries media — its caption is the album title and
    its images/videos are the album's items (``community_feed_media`` has no
    per-file caption by design, see the proposal §4.2). This needs no new table
    and no new upload flow: everything here is already-posted, already-moderated
    media. Pages by the post's id (keyset), like the main feed.
    """
    name = normalize_username(username)
    if not name:
        return {"success": False, "error": "Member not found",
                "code": "not_found", "albums": []}
    wallet = wallet_for_username(name)
    if not wallet:
        return {"success": False, "error": "Member not found",
                "code": "not_found", "albums": []}

    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable",
                "code": "db", "albums": []}

    try:
        size = int(limit or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    size = max(1, min(MAX_PAGE_SIZE, size))

    try:
        query = (
            supabase.table("community_feed_posts")
            .select("id,wallet_address,username,body,created_at,edited_at")
            .ilike("wallet_address", wallet)
            .eq("is_deleted", False)
            .eq("is_hidden", False)
        )
        if cursor:
            query = query.lt("id", int(cursor))
        # Fetch extra rows because caption-only posts are dropped below.
        result = query.order("id", desc=True).limit(size * 3 + 1).execute()
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: album query failed: %s", exc)
        return {"success": False, "error": "Could not load the album",
                "code": "db", "albums": []}

    media = _media_for_posts(supabase, [r.get("id") for r in rows
                                        if r.get("id") is not None])
    albums = []
    for row in rows:
        items = media.get(row.get("id")) or []
        if not items:
            continue
        albums.append({
            "post_id": row.get("id"),
            "username": row.get("username") or ANON_LABEL,
            "caption": row.get("body") or "",
            "created_at": row.get("created_at"),
            "item_count": len(items),
            "has_video": any(i.get("type") == "video" for i in items),
            "cover": next((i for i in items if i.get("type") == "image"),
                          items[0]),
            "items": items,
        })

    # The scan is bounded at `size * 3 + 1` posts, so a short read means we saw
    # every remaining post (no more pages). A page that filled up needs the
    # cursor to continue after the LAST POST SCANNED — not after the last album
    # kept, or a run of caption-only posts would be re-fetched forever.
    scanned_all = len(rows) <= size * 3
    if len(albums) > size:
        albums = albums[:size]
        next_cursor = albums[-1]["post_id"]
        has_more = True
    elif scanned_all:
        next_cursor = None
        has_more = False
    else:
        next_cursor = rows[-1].get("id") if rows else None
        has_more = next_cursor is not None

    return {
        "success": True,
        "albums": albums,
        "next_cursor": next_cursor,
        "has_more": bool(has_more),
    }


# ── Writes ───────────────────────────────────────────────────────────────────

def _record_media(supabase, post_id: int, media_items: list[dict]) -> list[dict]:
    """Insert media rows for a post. Returns the inserted rows."""
    if not media_items:
        return []
    payload = []
    for index, item in enumerate(media_items):
        payload.append({
            "post_id": post_id,
            "r2_key": item["key"],
            "public_url": item["public_url"],
            "media_type": item["media_type"],
            "mime": item.get("mime"),
            "bytes": item.get("bytes"),
            "width": item.get("width"),
            "height": item.get("height"),
            "duration_sec": item.get("duration"),
            "poster_url": item.get("poster_url"),
            "position": index,
        })
    try:
        result = supabase.table("community_feed_media").insert(payload).execute()
        return _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: media insert failed: %s", exc)
        return []


def create_post(wallet: str, caption: str | None, media_items: list[dict],
                prompt_id: str | None = None) -> dict:
    """Create a post from already-verified media descriptors.

    ``media_items`` must have been through ``verify_uploaded_object`` — this
    function does not talk to R2 and never trusts a client-supplied URL.
    """
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    if is_banned(wallet):
        return {"success": False, "error": "You cannot post in the community.",
                "code": "banned"}

    # Username is required: without it the post would render as a wallet
    # address. Checked BEFORE the media validation so the user is not asked to
    # re-upload a large file only to be refused at the end.
    username = get_username(wallet)
    if REQUIRE_USERNAME and not username:
        return {"success": False,
                "error": "Please choose a username before posting.",
                "code": "username_required"}

    media_items = media_items or []
    if len(media_items) > MAX_MEDIA_PER_POST:
        return {"success": False,
                "error": f"You can attach up to {MAX_MEDIA_PER_POST} files.",
                "code": "too_many_media"}

    ok, error, clean = validate_caption(caption)
    if not ok:
        return {"success": False, "error": error, "code": "caption"}
    if not clean and not media_items:
        return {"success": False,
                "error": "Please write something or attach a photo/video.",
                "code": "empty"}

    if prompt_id:
        from .daily_prompts import validate_prompt_response
        prompt_check = validate_prompt_response(prompt_id, wallet)
        if not prompt_check.get("success"):
            return {"success": False,
                    "error": prompt_check.get("error") or "This prompt is not accepting responses.",
                    "code": "prompt_ineligible"}

    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    payload = {
        "wallet_address": (wallet or "").lower(),
        "username": username,
        "body": clean or None,
    }
    if prompt_id:
        payload["prompt_id"] = prompt_id
    try:
        result = supabase.table("community_feed_posts").insert(payload).execute()
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: post insert failed: %s", exc)
        if prompt_id and ("unique" in str(exc).lower() or "prompt_wallet_unique" in str(exc).lower()):
            return {"success": False,
                    "error": "You have already answered this prompt.",
                    "code": "prompt_already_answered"}
        return {"success": False, "error": "Could not publish your post",
                "code": "db"}
    if not rows:
        return {"success": False, "error": "Could not publish your post",
                "code": "db"}

    post_row = rows[0]
    inserted = _record_media(supabase, post_row["id"], media_items)
    try:
        from . import badges as community_badges
        community_badges.on_post_created(wallet, post_row.get("id"), client=supabase)
    except Exception as exc:  # noqa: BLE001 - badges never fail a post
        logger.info("community_feed: post badge evaluation skipped: %s", exc)
    avatar = _avatars_for_wallets(supabase, [wallet]).get((wallet or "").lower())
    try:
        earned = community_badges.public_badges_for_wallet(wallet, client=supabase)[:3]
    except Exception:  # noqa: BLE001
        earned = []
    post = _public_post(post_row, [_public_media(r) for r in inserted], wallet,
                        avatar_url=avatar, earned_badges=earned)
    return {"success": True, "post": post}


# ── Profile pictures (avatars) ───────────────────────────────────────────────
# One picture per wallet, stored in community_feed_avatars. A new upload
# REPLACES the old row and hard-deletes the replaced R2 object on the same path,
# so a member never accumulates pictures (and never leaves orphans in the
# bucket). The picture is PUBLIC — that is the point of a profile picture — but
# public feed/profile payloads never include the WALLET, R2 URL or R2 key. They
# receive only a same-origin username route; the owner-only signed upload flow
# continues to pass its upload reference to the browser.

def set_avatar(wallet: str, avatar: dict) -> dict:
    """Store a member's profile picture from an already-verified descriptor.

    ``avatar`` must have come through ``storage.verify_avatar`` — this function
    does not talk to R2 to validate bytes and never trusts a client URL. The
    previous picture's object is purged after the row is replaced.
    """
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    if is_banned(wallet):
        return {"success": False, "error": "You cannot post in the community.",
                "code": "banned"}

    key = (avatar or {}).get("key")
    public_url = (avatar or {}).get("public_url")
    if not key or not public_url:
        return {"success": False, "error": "Invalid upload reference.",
                "code": "avatar"}

    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    lower_wallet = (wallet or "").lower()
    previous = _avatar_key(supabase, lower_wallet)

    username = get_username(wallet)
    payload = {
        "wallet_address": lower_wallet,
        "username": username,
        "r2_key": key,
        "public_url": public_url,
        "mime": (avatar or {}).get("mime"),
        "bytes": (avatar or {}).get("bytes"),
        "updated_at": _now().isoformat(),
    }
    try:
        result = supabase.table("community_feed_avatars").upsert(
            payload, on_conflict="wallet_address").execute()
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: avatar upsert failed: %s", exc)
        return {"success": False, "error": "Could not save your profile picture",
                "code": "db"}
    if not rows:
        return {"success": False, "error": "Could not save your profile picture",
                "code": "db"}

    # Purge the REPLACED object (not the new one). Best-effort: the row is
    # already the source of truth, so a storage hiccup must not fail the save.
    if previous and previous != key:
        _purge_objects([previous])

    return {"success": True, "avatar_url": public_avatar_path(username)}


def clear_avatar(wallet: str) -> dict:
    """Remove a member's profile picture and purge its object."""
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    lower_wallet = (wallet or "").lower()
    previous = _avatar_key(supabase, lower_wallet)
    try:
        supabase.table("community_feed_avatars").delete().eq(
            "wallet_address", lower_wallet).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: avatar delete failed: %s", exc)
        return {"success": False, "error": "Could not remove your profile picture",
                "code": "db"}

    if previous:
        _purge_objects([previous])
    return {"success": True, "avatar_url": None}


def _avatar_key(supabase, lower_wallet: str) -> str | None:
    """The stored R2 key for a wallet's current picture, or None."""
    if not lower_wallet:
        return None
    try:
        result = (
            supabase.table("community_feed_avatars")
            .select("r2_key")
            .eq("wallet_address", lower_wallet)
            .limit(1)
            .execute()
        )
        rows = _rows(result)
        if not rows:
            return None
        return (rows[0].get("r2_key") or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: avatar key lookup failed: %s", exc)
        return None


def update_caption(wallet: str, post_id: int, caption: str | None) -> dict:
    """Edit a post's caption. Media is never editable (see the proposal)."""
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    ok, error, clean = validate_caption(caption)
    if not ok:
        return {"success": False, "error": error, "code": "caption"}

    try:
        current = (
            supabase.table("community_feed_posts")
            .select("wallet_address,is_deleted")
            .eq("id", int(post_id))
            .limit(1)
            .execute()
        )
        rows = _rows(current)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: caption edit lookup failed: %s", exc)
        return {"success": False, "error": "Could not update the post", "code": "db"}
    if not rows:
        return {"success": False, "error": "Post not found", "code": "not_found"}
    if (rows[0].get("wallet_address") or "").lower() != wallet.lower():
        return {"success": False, "error": "You can only edit your own post.",
                "code": "forbidden"}
    if rows[0].get("is_deleted"):
        return {"success": False, "error": "Post not found", "code": "not_found"}

    # A caption edit on a media-less post must not blank it out.
    if not clean:
        has_media = False
        try:
            media = (
                supabase.table("community_feed_media")
                .select("id")
                .eq("post_id", int(post_id))
                .limit(1)
                .execute()
            )
            has_media = bool(_rows(media))
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_feed: media check failed: %s", exc)
            has_media = True  # fail open: do not block a legitimate edit
        if not has_media:
            return {"success": False,
                    "error": "Please write something or attach a photo/video.",
                    "code": "empty"}

    try:
        supabase.table("community_feed_posts").update({
            "body": clean or None,
            "edited_at": _now().isoformat(),
        }).eq("id", int(post_id)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: caption update failed: %s", exc)
        return {"success": False, "error": "Could not update the post", "code": "db"}
    return {"success": True, "body": clean}


def _pin_target(supabase, wallet: str, post_id) -> tuple[dict | None, dict | None]:
    """Load a post for pinning, enforcing ownership. Returns (row, error)."""
    try:
        result = (
            supabase.table("community_feed_posts")
            .select("id,wallet_address,post_type,is_deleted,is_hidden,is_pinned")
            .eq("id", int(post_id))
            .limit(1)
            .execute()
        )
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: pin lookup failed: %s", exc)
        return None, {"success": False, "error": "Could not update the pin", "code": "db"}
    if not rows or rows[0].get("is_deleted"):
        return None, {"success": False, "error": "Post not found", "code": "not_found"}
    row = rows[0]
    if row.get("post_type") != "user":
        return None, {"success": False, "error": "Only your own posts can be pinned.",
                      "code": "forbidden"}
    if (row.get("wallet_address") or "").lower() != wallet.lower():
        return None, {"success": False, "error": "You can only pin your own post.",
                      "code": "forbidden"}
    return row, None


def pin_post(wallet: str, post_id) -> dict:
    """Pin one of the member's own posts to the top of the Community feed.

    Free and off-chain. A member may hold ONE pinned post at a time: pinning a
    second post releases the first, so the top of the feed can never be filled
    by a single author.
    """
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    row, error = _pin_target(supabase, wallet, post_id)
    if error:
        return error
    if row.get("is_hidden"):
        return {"success": False, "error": "A hidden post cannot be pinned.",
                "code": "hidden"}

    now = _now().isoformat()
    try:
        # Release any other pin this member holds first, so the "one pin per
        # member" rule holds even if an older row was pinned before this rule.
        (
            supabase.table("community_feed_posts")
            .update({"is_pinned": False, "pinned_at": None})
            .ilike("wallet_address", wallet)
            .eq("post_type", "user")
            .eq("is_pinned", True)
            .execute()
        )
        (
            supabase.table("community_feed_posts")
            .update({"is_pinned": True, "pinned_at": now})
            .eq("id", int(post_id))
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: pin failed: %s", exc)
        return {"success": False, "error": "Could not pin the post", "code": "db"}
    return {"success": True, "is_pinned": True}


def unpin_post(wallet: str, post_id) -> dict:
    """Release the member's pin. Free and off-chain."""
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    _row, error = _pin_target(supabase, wallet, post_id)
    if error:
        return error
    try:
        (
            supabase.table("community_feed_posts")
            .update({"is_pinned": False, "pinned_at": None})
            .eq("id", int(post_id))
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: unpin failed: %s", exc)
        return {"success": False, "error": "Could not unpin the post", "code": "db"}
    return {"success": True, "is_pinned": False}


def get_pinned_member_posts(viewer: str | None = None,
                            limit: int | None = None) -> dict:
    """The member pins shown above the chronological feed.

    Donor acknowledgments have their own pinned area, so this query is limited
    to ``user`` posts to keep the two surfaces from overlapping.
    """
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "posts": []}
    try:
        size = int(limit or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    size = max(1, min(MAX_PAGE_SIZE, size))
    try:
        query = (
            supabase.table("community_feed_posts").select("*")
            .eq("post_type", "user")
            .eq("is_pinned", True)
            .eq("is_hidden", False)
            .eq("is_deleted", False)
        )
        rows = _rows(query.order("id", desc=True).limit(size).execute())
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: pinned member query failed: %s", exc)
        return {"success": False, "error": "Could not load pinned posts", "posts": []}

    post_ids = [row.get("id") for row in rows if row.get("id") is not None]
    media = _media_for_posts(supabase, post_ids)
    reactions = _reactions_for_posts(supabase, post_ids, viewer)
    followed = _follow_states(
        supabase, [row.get("wallet_address") for row in rows], viewer)
    avatars = _avatars_for_wallets(
        supabase, [row.get("wallet_address") for row in rows])
    breakdowns = _reaction_breakdowns_for_posts(supabase, post_ids)
    from . import badges as community_badges
    badges_by_wallet = community_badges.public_badges_for_wallets(
        [row.get("wallet_address") for row in rows], client=supabase)

    posts = [
        _public_post(
            row, media.get(row.get("id"), []), viewer,
            my_reaction=reactions.get(row.get("id")),
            reactions=breakdowns.get(row.get("id")),
            is_following=followed.get((row.get("wallet_address") or "").lower()),
            avatar_url=avatars.get((row.get("wallet_address") or "").lower()),
            earned_badges=badges_by_wallet.get((row.get("wallet_address") or "").lower()),
        )
        for row in rows
    ]
    return {"success": True, "posts": posts}


def delete_post(wallet: str, post_id: int) -> dict:
    """Soft-delete a post and hard-delete its R2 objects.

    Soft delete in the database keeps the audit trail; the media is really
    removed from R2 because a reported video must actually stop being served.
    """
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    try:
        result = (
            supabase.table("community_feed_posts")
            .select("wallet_address,is_deleted")
            .eq("id", int(post_id))
            .limit(1)
            .execute()
        )
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: delete lookup failed: %s", exc)
        return {"success": False, "error": "Could not delete the post", "code": "db"}
    if not rows or rows[0].get("is_deleted"):
        return {"success": False, "error": "Post not found", "code": "not_found"}
    if (rows[0].get("wallet_address") or "").lower() != wallet.lower():
        return {"success": False, "error": "You can only delete your own post.",
                "code": "forbidden"}

    keys = _media_keys(supabase, int(post_id))
    _mark_deleted(supabase, int(post_id), wallet, hidden=False)
    removed = _purge_objects(keys)
    return {"success": True, "deleted_objects": removed}


def _media_keys(supabase, post_id: int) -> list[str]:
    try:
        result = (
            supabase.table("community_feed_media")
            .select("r2_key")
            .eq("post_id", int(post_id))
            .execute()
        )
        return [r.get("r2_key") for r in _rows(result) if r.get("r2_key")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: media key lookup failed: %s", exc)
        return []


def _purge_objects(keys: list[str]) -> int:
    """Best-effort R2 delete. A storage failure must never fail the DB delete
    (the row is already gone from every listing)."""
    if not keys:
        return 0
    try:
        from storage import r2

        return r2.delete_objects(keys)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: R2 delete failed: %s", exc)
        return 0


def _mark_deleted(supabase, post_id: int, by_wallet: str, hidden: bool) -> bool:
    payload = {
        "is_deleted": True,
        "is_pinned": False,
        "pinned_at": None,
        "deleted_by": (by_wallet or "").lower(),
        "deleted_at": _now().isoformat(),
    }
    if hidden:
        payload["is_hidden"] = True
    try:
        supabase.table("community_feed_posts").update(payload).eq(
            "id", int(post_id)).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: delete update failed: %s", exc)
        return False


# ── Reactions ────────────────────────────────────────────────────────────────

def set_reaction(wallet: str, post_id: int, reaction: str) -> dict:
    """Set, switch or clear the viewer's reaction on a post.

    One reaction per wallet per post (composite PK). The count is adjusted on
    the same path and is NEVER taken from the client.
    """
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    reaction = (reaction or "").strip().lower()
    if reaction not in REACTION_KEYS:
        return {"success": False, "error": "Unknown reaction", "code": "reaction"}
    if is_banned(wallet):
        return {"success": False, "error": "You cannot react in the community.",
                "code": "banned"}

    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    post = _load_visible_post(supabase, post_id)
    if not post:
        return {"success": False, "error": "Post not found", "code": "not_found"}
    post_owner = (None if post.get("post_type") == "system_vault_donation"
                  else post.get("wallet_address"))

    previous = _current_reaction(supabase, post_id, wallet)
    if previous == reaction:
        # Tapping your own active chip removes it.
        return clear_reaction(wallet, post_id, _supabase=supabase, _post=post)

    reward_outcome = {"status": "not_credited", "reason": "reward_unavailable"}
    reaction_created_at = None
    try:
        if previous:
            # Switch: update in place, count does not move. A switch is NOT a
            # new reaction, so it must never credit — and because the ledger
            # credit is keyed on (post, reactor), the first credit stands and
            # nothing more is written.
            supabase.table("community_feed_reactions").update(
                {"reaction": reaction}
            ).eq("post_id", int(post_id)).ilike(
                "wallet_address", wallet).execute()
            count = int(post.get("reaction_count") or 0)
        else:
            inserted = supabase.table("community_feed_reactions").insert({
                "post_id": int(post_id),
                "wallet_address": wallet.lower(),
                "reaction": reaction,
            }).execute()
            inserted_rows = _rows(inserted)
            reaction_created_at = inserted_rows[0].get("created_at") if inserted_rows else None
            count = int(post.get("reaction_count") or 0) + 1
            _write_reaction_count(supabase, post_id, count)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: reaction write failed: %s", exc)
        return {"success": False, "error": "Could not save your reaction",
                "code": "db"}

    # A brand-new reaction earns. Self-activity is filtered inside the hook, so
    # reacting to your own post credits nobody. Deliberately OUTSIDE the write
    # try/except and after the reaction is persisted: a rewards problem must
    # never turn a successful reaction into a reported failure. Wrapped here as
    # well as inside the hook — a bug in the hook itself (or an import error)
    # must not be able to fail the user's reaction.
    if not previous:
        community_notifications.notify_reaction(
            supabase,
            actor_wallet=wallet,
            actor_username=get_username(wallet) or "Community member",
            post_owner_wallet=post_owner,
            post_id=post_id,
            reaction=reaction,
        )
        try:
            reward_outcome = rewards.on_reaction_added(
                wallet, post_id, post_owner,
                event_created_at=_persisted_reward_time(
                    supabase, "community_feed_reactions",
                    {"post_id": int(post_id), "wallet_address": wallet.lower()},
                    reaction_created_at),
                _supabase=supabase)
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_feed: reward credit failed: %s", exc)

    # Badge evaluation is server-side and only follows a persisted reaction.
    # Re-evaluate the recipient too so a newly earned Helpful Member badge can
    # appear without waiting for another profile visit.
    try:
        from . import badges as community_badges
        community_badges.on_reaction_added(wallet, post_id, client=supabase)
        owner_wallet = (post_owner or "").lower()
        if owner_wallet and owner_wallet != wallet.lower():
            community_badges.on_reaction_added(owner_wallet, post_id, client=supabase)
    except Exception as exc:  # noqa: BLE001 - badges never fail a reaction
        logger.info("community_feed: reaction badge evaluation skipped: %s", exc)

    return {"success": True, "reaction_count": count, "my_reaction": reaction,
            "reward": _viewer_reward_outcome(reward_outcome),
            "reaction_breakdown": _post_breakdown(supabase, post_id)}


def clear_reaction(wallet: str, post_id: int, *, _supabase=None,
                   _post=None) -> dict:
    """Remove the viewer's reaction, decrementing the count (floored at 0)."""
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    supabase = _supabase or _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    post = _post or _load_visible_post(supabase, post_id)
    if not post:
        return {"success": False, "error": "Post not found", "code": "not_found"}
    post_owner = (None if post.get("post_type") == "system_vault_donation"
                  else post.get("wallet_address"))

    previous = _current_reaction(supabase, post_id, wallet)
    if not previous:
        return {"success": True,
                "reaction_count": int(post.get("reaction_count") or 0),
                "my_reaction": None,
                "reward": {"status": "already_credited"}}

    try:
        supabase.table("community_feed_reactions").delete().eq(
            "post_id", int(post_id)).ilike("wallet_address", wallet).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: reaction delete failed: %s", exc)
        return {"success": False, "error": "Could not remove your reaction",
                "code": "db"}

    count = max(0, int(post.get("reaction_count") or 0) - 1)
    _write_reaction_count(supabase, post_id, count)
    # Removing a reaction reverses both sides of the credit it produced. The
    # ledger's unique `reverses_ledger_id` makes a replayed undo a no-op.
    try:
        reward_outcome = rewards.on_reaction_removed(
            wallet, post_id, post_owner, _supabase=supabase)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: reward reversal failed: %s", exc)
        reward_outcome = {"status": "not_credited", "reason": "reward_unavailable"}
    return {"success": True, "reaction_count": count, "my_reaction": None,
            "reward": _viewer_reward_outcome(reward_outcome),
            "reaction_breakdown": _post_breakdown(supabase, post_id)}


def _post_breakdown(supabase, post_id: int) -> dict[str, int]:
    """The cleaned ``{key: count}`` breakdown for ONE post (one query)."""
    return _clean_breakdown(
        _reaction_breakdowns_for_posts(supabase, [post_id]).get(post_id))


def _write_reaction_count(supabase, post_id: int, count: int) -> None:
    try:
        supabase.table("community_feed_posts").update(
            {"reaction_count": max(0, int(count))}
        ).eq("id", int(post_id)).execute()
    except Exception as exc:  # noqa: BLE001
        # The reaction row itself is authoritative; the count is repairable via
        # recount_reaction_count(), so a failed counter write is not fatal.
        logger.warning("community_feed: reaction count write failed: %s", exc)


def _current_reaction(supabase, post_id: int, wallet: str) -> str | None:
    try:
        result = (
            supabase.table("community_feed_reactions")
            .select("reaction")
            .eq("post_id", int(post_id))
            .ilike("wallet_address", wallet)
            .limit(1)
            .execute()
        )
        rows = _rows(result)
        return rows[0].get("reaction") if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: reaction lookup failed: %s", exc)
        return None


def recount_reaction_count(post_id: int) -> int:
    """Recompute a post's reaction count from the reaction rows.

    Repair path for a failed counter write / an admin "Recalculate" action.
    """
    supabase = _get_supabase()
    if not supabase:
        return 0
    try:
        result = (
            supabase.table("community_feed_reactions")
            .select("wallet_address")
            .eq("post_id", int(post_id))
            .execute()
        )
        count = len(_rows(result))
        _write_reaction_count(supabase, post_id, count)
        return count
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: recount failed: %s", exc)
        return 0


def _load_visible_post_status(supabase, post_id: int) -> tuple[dict | None, str | None]:
    try:
        result = (
            supabase.table("community_feed_posts")
            .select("id,wallet_address,reaction_count,is_deleted,is_hidden,post_type")
            .eq("id", int(post_id))
            .limit(1)
            .execute()
        )
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: post load failed: %s", exc)
        return None, "db"
    if not rows:
        return None, "not_found"
    row = rows[0]
    if row.get("is_deleted") or row.get("is_hidden"):
        return None, "not_found"
    return row, None


def _load_visible_post(supabase, post_id: int) -> dict | None:
    row, _error = _load_visible_post_status(supabase, post_id)
    return row


def get_reaction_users(post_id: int, *, cursor: str | None = None,
                       limit: int | None = None,
                       reaction: str | None = None) -> dict:
    """Return public reactor identities for one visible post.

    The database query is keyset-paginated by ``created_at`` plus the private
    wallet tie-breaker. The tie-breaker is carried only inside an encrypted,
    authenticated cursor and is never serialized into a response.
    """
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db",
                "reactors": []}
    try:
        size = REACTION_USERS_DEFAULT_PAGE_SIZE if limit is None else int(limit)
    except (TypeError, ValueError):
        return {"success": False, "error": "Invalid reaction page size.",
                "code": "limit", "reactors": []}
    if not 1 <= size <= REACTION_USERS_MAX_PAGE_SIZE:
        return {"success": False, "error": "Invalid reaction page size.",
                "code": "limit", "reactors": []}

    selected_reaction = None if reaction in (None, "") else str(reaction).strip().lower()
    if selected_reaction is not None and selected_reaction not in REACTION_KEYS:
        return {"success": False, "error": "Unknown reaction filter.",
                "code": "filter", "reactors": []}

    post, post_error = _load_visible_post_status(supabase, post_id)
    if not post:
        code = post_error or "not_found"
        return {"success": False,
                "error": "Database unavailable" if code == "db" else "Post not found",
                "code": code,
                "reactors": []}

    decoded = None
    if cursor is not None:
        decoded = _decode_reaction_cursor(
            cursor, post_id=int(post_id), reaction=selected_reaction)
        if not decoded:
            return {"success": False, "error": "Invalid reaction cursor.",
                    "code": "cursor", "reactors": []}

    try:
        query = (supabase.table("community_feed_reactions")
                 .select("wallet_address,reaction,created_at")
                 .eq("post_id", int(post_id)))
        if selected_reaction:
            query = query.eq("reaction", selected_reaction)
        if decoded:
            # PostgREST OR keeps the cursor predicate in SQL; no OFFSET or
            # unbounded in-process scan is used for a live reaction list.
            created_at = decoded["created_at"]
            wallet = decoded["wallet"]
            query = query.or_(
                f"created_at.lt.{created_at},"
                f"and(created_at.eq.{created_at},wallet_address.lt.{wallet})")
        rows = _rows(query.order("created_at", desc=True)
                     .order("wallet_address", desc=True)
                     .limit(size + 1).execute())
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: reaction users query failed: %s", exc)
        return {"success": False, "error": "Could not load reactions", "code": "db",
                "reactors": []}

    has_more = len(rows) > size
    page_rows = rows[:size]
    wallets = [(row.get("wallet_address") or "").lower() for row in page_rows
               if row.get("wallet_address")]
    # Exactly one batched profile query and one batched avatar query for the
    # page. The private wallet values never cross this serializer boundary.
    names = _current_usernames_for_wallets(supabase, wallets)
    avatars = _avatars_for_wallets(supabase, wallets)
    reactors = []
    for row in page_rows:
        wallet = (row.get("wallet_address") or "").lower()
        profile_username = normalize_username(names.get(wallet))
        reactors.append({
            "username": profile_username or ANON_LABEL,
            "profile_username": profile_username,
            "avatar_url": (public_avatar_path(profile_username)
                           if profile_username and avatars.get(wallet) else None),
            "reaction": (row.get("reaction") or "").strip().lower(),
        })
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        try:
            next_cursor = _encode_reaction_cursor(
                post_id=int(post_id), reaction=selected_reaction,
                created_at=str(last.get("created_at") or ""),
                wallet=str(last.get("wallet_address") or "").lower())
        except Exception as exc:  # noqa: BLE001
            logger.error("community_feed: reaction cursor encryption unavailable: %s", exc)
            return {"success": False, "error": "Reaction pagination is unavailable.",
                    "code": "cursor_unavailable", "reactors": []}
    return {"success": True, "reactors": reactors,
            "next_cursor": next_cursor, "has_more": has_more}


# ── Comments ─────────────────────────────────────────────────────────────────

def add_comment(wallet: str, post_id: int, body: str,
                parent_comment_id=None) -> dict:
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    if is_banned(wallet):
        return {"success": False, "error": "You cannot comment in the community.",
                "code": "banned"}

    # Same rule as posting: a comment without a username renders as a wallet
    # address, so it is refused rather than masked.
    username = get_username(wallet)
    if REQUIRE_USERNAME and not username:
        return {"success": False,
                "error": "Please choose a username before commenting.",
                "code": "username_required"}

    ok, error, clean = validate_comment(body)
    if not ok:
        return {"success": False, "error": error, "code": "comment"}

    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}
    post = _load_visible_post(supabase, post_id)
    if not post:
        return {"success": False, "error": "Post not found", "code": "not_found"}
    post_owner = (None if post.get("post_type") == "system_vault_donation"
                  else post.get("wallet_address"))

    parent = None
    if parent_comment_id not in (None, ""):
        if isinstance(parent_comment_id, bool):
            return {"success": False, "error": "Invalid reply target.",
                    "code": "parent"}
        if isinstance(parent_comment_id, float) and not parent_comment_id.is_integer():
            return {"success": False, "error": "Invalid reply target.",
                    "code": "parent"}
        try:
            parent_id = int(parent_comment_id)
        except (TypeError, ValueError):
            return {"success": False, "error": "Invalid reply target.",
                    "code": "parent"}
        if parent_id <= 0 or parent_id > 9_223_372_036_854_775_807:
            return {"success": False, "error": "Invalid reply target.",
                    "code": "parent"}
        try:
            parent_result = (
                supabase.table("community_feed_comments")
                .select("id,post_id,wallet_address,username,is_deleted")
                .eq("id", parent_id)
                .eq("post_id", int(post_id))
                .limit(1)
                .execute()
            )
            parent_rows = _rows(parent_result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_feed: reply parent lookup failed: %s", exc)
            return {"success": False, "error": "Could not verify the reply target.",
                    "code": "db"}
        if not parent_rows or parent_rows[0].get("is_deleted"):
            return {"success": False, "error": "Reply target not found.",
                    "code": "parent_not_found"}
        parent = parent_rows[0]

    try:
        result = supabase.table("community_feed_comments").insert({
            "post_id": int(post_id),
            "wallet_address": wallet.lower(),
            "username": username,
            "body": clean,
            "parent_comment_id": int(parent.get("id")) if parent else None,
            # Never accept a mention recipient from the browser. The trigger
            # independently recomputes both fields from this same parent row.
            "reply_to_wallet": parent.get("wallet_address") if parent else None,
            "reply_to_username_snapshot": parent.get("username") if parent else None,
        }).execute()
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: comment insert failed: %s", exc)
        return {"success": False, "error": "Could not post your comment",
                "code": "db"}
    if not rows:
        return {"success": False, "error": "Could not post your comment",
                "code": "db"}

    if parent is None:
        _bump_comment_count(supabase, post_id, +1)
    # Persist private in-app notifications only after the comment itself is
    # saved. Notification-storage failure is logged by the helper and cannot
    # turn a successful comment into a reported failure.
    community_notifications.notify_comment(
        supabase,
        actor_wallet=wallet,
        actor_username=username,
        post_owner_wallet=post_owner,
        post_id=post_id,
        comment_id=rows[0].get("id"),
        parent=parent,
    )
    # A new comment earns for BOTH sides — the commenter (COMMENT_IN_RATE) and
    # the post owner (COMMENT_OUT_RATE, the larger rate); self-activity credits
    # nobody. Best-effort, and after the comment is persisted, so a rewards
    # problem never reports the comment as failed.
    reward_outcome = {"status": "not_credited", "reason": "reward_unavailable"}
    try:
        reward_outcome = rewards.on_comment_added(
            wallet, rows[0].get("id"), post_id, post_owner,
            event_created_at=_persisted_reward_time(
                supabase, "community_feed_comments", {"id": rows[0].get("id")},
                rows[0].get("created_at")),
            _supabase=supabase)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: reward credit failed: %s", exc)
    if parent is not None:
        _notify_comment_reply(
            actor_wallet=wallet, actor_username=username,
            parent=parent, post=post, post_id=post_id)
    avatar = _avatars_for_wallets(supabase, [wallet]).get((wallet or "").lower())
    mention = (get_username(parent.get("wallet_address")) or parent.get("username")
               if parent else None)
    public_comment = _public_comment(
        rows[0], wallet, avatar_url=avatar,
        post_owner=post_owner,
        profile_username=normalize_username(username))
    public_comment["reply_to_username"] = (
        (mention or "").strip().lstrip("@") or None)
    return {"success": True,
            "comment": public_comment,
            "reward": _viewer_reward_outcome(reward_outcome)}


def _notify_comment_reply(*, actor_wallet: str, actor_username: str,
                          parent: dict, post: dict, post_id: int) -> None:
    """Best-effort Telegram alerts through the user's linked notification channel."""
    actor = (actor_wallet or "").lower()
    parent_wallet = (parent.get("wallet_address") or "").lower()
    post_wallet = (post.get("wallet_address") or "").lower()
    if post.get("post_type") == "system_vault_donation":
        post_wallet = ""
    recipients: dict[str, str] = {}
    if parent_wallet and parent_wallet != actor:
        recipients[parent_wallet] = "parent"
    if post_wallet and post_wallet != actor:
        recipients.setdefault(post_wallet, "post")
    if not recipients:
        return
    try:
        from html import escape
        from telegram_notify import notify_user_by_wallet_async
        actor_label = escape((actor_username or "Community member").strip().lstrip("@")[:80])
        post_label = escape((post.get("username") or "Community member").strip().lstrip("@")[:80])
        post_url = f"{os.getenv('PRODUCTION_DOMAIN', 'https://goodmarketph.live').rstrip('/')}/community/p/{int(post_id)}"
        safe_post_url = escape(post_url, quote=True)
        for recipient, kind in recipients.items():
            if kind == "parent":
                text = (f"<b>@{actor_label}</b> replied to your comment on "
                        f"<b>@{post_label}'s</b> post. "
                        f'<a href="{safe_post_url}">View thread</a>')
            else:
                text = (f"<b>@{actor_label}</b> replied in your Community post. "
                        f'<a href="{safe_post_url}">View thread</a>')
            try:
                notify_user_by_wallet_async(recipient, text)
            except Exception as exc:  # noqa: BLE001
                logger.info("community_feed: reply notification failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.info("community_feed: reply notification skipped: %s", exc)


def _bump_comment_count(supabase, post_id: int, delta: int) -> None:
    try:
        current = (
            supabase.table("community_feed_posts")
            .select("comment_count")
            .eq("id", int(post_id))
            .limit(1)
            .execute()
        )
        rows = _rows(current)
        count = int(rows[0].get("comment_count") or 0) if rows else 0
        supabase.table("community_feed_posts").update(
            {"comment_count": max(0, count + delta)}
        ).eq("id", int(post_id)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: comment count write failed: %s", exc)


def get_comments(post_id: int, viewer: str | None = None, limit: int = 1000) -> dict:
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "comments": []}
    post = _load_visible_post(supabase, post_id)
    if not post:
        return {"success": False, "error": "Post not found", "code": "not_found",
                "comments": []}
    try:
        result = (
            supabase.table("community_feed_comments")
            .select("*")
            .eq("post_id", int(post_id))
            .order("id")
            .limit(max(1, min(1000, int(limit or 1000))))
            .execute()
        )
        all_rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: comment list failed: %s", exc)
        return {"success": False, "error": "Could not load comments", "comments": []}
    by_id = {str(row.get("id")): row for row in all_rows
             if row.get("id") is not None}
    included = {key for key, row in by_id.items() if not row.get("is_deleted")}
    # Preserve a deleted ancestor as a content-free placeholder while any live
    # descendant still points to it, so soft deletion never breaks the tree.
    for row in list(by_id.values()):
        if row.get("is_deleted"):
            continue
        parent_id = row.get("parent_comment_id")
        visited = set()
        while parent_id not in (None, "") and str(parent_id) not in visited:
            key = str(parent_id)
            visited.add(key)
            parent = by_id.get(key)
            if not parent:
                break
            included.add(key)
            parent_id = parent.get("parent_comment_id")
    rows = [row for row in all_rows if str(row.get("id")) in included]
    avatars = _avatars_for_wallets(
        supabase, [r.get("wallet_address") for r in rows if not r.get("is_deleted")])
    current_names = _current_usernames_for_wallets(
        supabase,
        [r.get("wallet_address") for r in rows if not r.get("is_deleted")]
        + [r.get("reply_to_wallet") for r in rows],
    )
    return {"success": True,
            "comments": [
                _public_comment(r, viewer,
                                avatar_url=avatars.get(
                                    (r.get("wallet_address") or "").lower()),
                                post_owner=post.get("wallet_address"),
                                profile_username=current_names.get(
                                    (r.get("wallet_address") or "").lower()))
                | {"reply_to_username": (
                    (current_names.get((r.get("reply_to_wallet") or "").lower())
                     or r.get("reply_to_username_snapshot") or "").strip().lstrip("@") or None
                )}
                for r in rows
            ]}


def delete_comment(wallet: str, comment_id: int) -> dict:
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}
    try:
        result = (
            supabase.table("community_feed_comments")
            .select("post_id,wallet_address,is_deleted,parent_comment_id")
            .eq("id", int(comment_id))
            .limit(1)
            .execute()
        )
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: comment delete lookup failed: %s", exc)
        return {"success": False, "error": "Could not delete the comment", "code": "db"}
    if not rows or rows[0].get("is_deleted"):
        return {"success": False, "error": "Comment not found", "code": "not_found"}
    if (rows[0].get("wallet_address") or "").lower() != wallet.lower():
        return {"success": False, "error": "You can only delete your own comment.",
                "code": "forbidden"}
    try:
        supabase.table("community_feed_comments").update(
            {"is_deleted": True}).eq("id", int(comment_id)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: comment delete failed: %s", exc)
        return {"success": False, "error": "Could not delete the comment", "code": "db"}
    if rows[0].get("parent_comment_id") in (None, ""):
        _bump_comment_count(supabase, rows[0].get("post_id"), -1)
    # Deleting a comment reverses both sides of the credit it produced, so the
    # owner's rate must be resolved here to find the owner's row.
    try:
        reward_outcome = rewards.on_comment_removed(
            wallet, comment_id,
            _comment_post_owner(supabase, rows[0].get("post_id")),
            _supabase=supabase)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: reward reversal failed: %s", exc)
        reward_outcome = {"status": "not_credited", "reason": "reward_unavailable"}
    return {"success": True,
            "reward": _viewer_reward_outcome(reward_outcome)}


# ── Reports ──────────────────────────────────────────────────────────────────

def report_post(wallet: str, post_id: int, reason: str | None = None) -> dict:
    """Report a post. A duplicate report is an idempotent success."""
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}
    if not _load_visible_post(supabase, post_id):
        return {"success": False, "error": "Post not found", "code": "not_found"}

    clean_reason = sanitize_caption(reason)[:500] or None
    try:
        supabase.table("community_feed_reports").insert({
            "post_id": int(post_id),
            "reporter": wallet.lower(),
            "reason": clean_reason,
        }).execute()
    except Exception as exc:  # noqa: BLE001
        # A UNIQUE violation means this user already reported the post — that is
        # the desired end state, not an error.
        logger.info("community_feed: report insert rejected (likely duplicate): %s", exc)

    _bump_report_count(supabase, post_id, +1)
    return {"success": True}


def _bump_report_count(supabase, post_id: int, delta: int) -> None:
    try:
        current = (
            supabase.table("community_feed_posts")
            .select("report_count")
            .eq("id", int(post_id))
            .limit(1)
            .execute()
        )
        rows = _rows(current)
        count = int(rows[0].get("report_count") or 0) if rows else 0
        supabase.table("community_feed_posts").update(
            {"report_count": max(0, count + delta)}
        ).eq("id", int(post_id)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: report count write failed: %s", exc)


# ── Admin ────────────────────────────────────────────────────────────────────

def admin_list_reports(status: str = "open", limit: int = 100) -> dict:
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "reports": []}
    try:
        query = supabase.table("community_feed_reports").select("*")
        if status and status != "all":
            query = query.eq("status", status)
        result = query.order("id", desc=True).limit(
            max(1, min(500, int(limit or 100)))).execute()
        return {"success": True, "reports": _rows(result)}
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: report list failed: %s", exc)
        return {"success": False, "error": "Could not load reports", "reports": []}


def admin_hide_post(post_id: int, admin_wallet: str, hidden: bool = True) -> dict:
    """Hide/unhide a post. Hiding keeps the row (and its media) for review."""
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}
    owner = None
    try:
        rows = _rows(supabase.table("community_feed_posts").select(
            "wallet_address,post_type").eq("id", int(post_id)).limit(1).execute())
        owner = (rows[0].get("wallet_address")
                 if rows and rows[0].get("post_type") != "system_vault_donation" else None)
    except Exception:
        pass
    try:
        patch = {"is_hidden": bool(hidden)}
        if hidden:
            patch.update({"is_pinned": False, "pinned_at": None})
        supabase.table("community_feed_posts").update(
            patch).eq("id", int(post_id)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: hide failed: %s", exc)
        return {"success": False, "error": "Could not update the post", "code": "db"}
    if hidden:
        _resolve_reports(supabase, post_id)
    if owner:
        try:
            from . import badges as community_badges
            community_badges.on_post_visibility_changed(owner, post_id, client=supabase)
        except Exception as exc:  # noqa: BLE001
            logger.info("community_feed: moderation badge evaluation skipped: %s", exc)
    return {"success": True, "is_hidden": bool(hidden)}


def admin_delete_post(post_id: int, admin_wallet: str) -> dict:
    """Hard-stop a post: hide it, mark it deleted, and purge the R2 objects."""
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}
    owner = None
    try:
        rows = _rows(supabase.table("community_feed_posts").select(
            "wallet_address,post_type").eq("id", int(post_id)).limit(1).execute())
        owner = (rows[0].get("wallet_address")
                 if rows and rows[0].get("post_type") != "system_vault_donation" else None)
    except Exception:
        pass
    keys = _media_keys(supabase, int(post_id))
    if not _mark_deleted(supabase, int(post_id), admin_wallet, hidden=True):
        return {"success": False, "error": "Could not delete the post", "code": "db"}
    _resolve_reports(supabase, post_id)
    if owner:
        try:
            from . import badges as community_badges
            community_badges.on_post_visibility_changed(owner, post_id, client=supabase)
        except Exception as exc:  # noqa: BLE001
            logger.info("community_feed: moderation badge evaluation skipped: %s", exc)
    return {"success": True, "deleted_objects": _purge_objects(keys)}


def _resolve_reports(supabase, post_id: int) -> None:
    try:
        supabase.table("community_feed_reports").update(
            {"status": "resolved"}).eq("post_id", int(post_id)).eq(
            "status", "open").execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: report resolve failed: %s", exc)


def admin_resolve_report(report_id: int, admin_wallet: str,
                         status: str = "resolved") -> dict:
    status = status if status in ("open", "resolved", "dismissed") else "resolved"
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}
    try:
        supabase.table("community_feed_reports").update(
            {"status": status}).eq("id", int(report_id)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: report update failed: %s", exc)
        return {"success": False, "error": "Could not update the report", "code": "db"}
    return {"success": True, "status": status}


def admin_set_ban(wallet: str, admin_wallet: str, banned: bool,
                  reason: str | None = None) -> dict:
    if not wallet:
        return {"success": False, "error": "A wallet is required", "code": "wallet"}
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}
    target = wallet.lower()
    try:
        if banned:
            supabase.table("community_feed_bans").upsert({
                "wallet_address": target,
                "reason": sanitize_caption(reason)[:500] or None,
                "banned_by": (admin_wallet or "").lower(),
            }).execute()
        else:
            supabase.table("community_feed_bans").delete().ilike(
                "wallet_address", target).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: ban write failed: %s", exc)
        return {"success": False, "error": "Could not update the ban", "code": "db"}
    return {"success": True, "banned": bool(banned)}


def admin_stats() -> dict:
    """Small dashboard summary. Never raises."""
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable"}
    out = {"success": True, "posts": 0, "open_reports": 0, "bans": 0}
    for key, table, filters in (
        ("posts", "community_feed_posts", {"is_deleted": False}),
        ("open_reports", "community_feed_reports", {"status": "open"}),
        ("bans", "community_feed_bans", {}),
    ):
        try:
            query = supabase.table(table).select("id")
            for column, value in filters.items():
                query = query.eq(column, value)
            result = query.limit(1000).execute()
            out[key] = len(_rows(result))
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_feed: stat %s failed: %s", key, exc)
    return out
