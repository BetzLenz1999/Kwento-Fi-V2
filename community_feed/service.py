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
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

FEATURE_NAME = "community_feed_feature"

MAX_CAPTION = int(os.getenv("COMMUNITY_MAX_CAPTION", "2200"))
# The UI counts characters; the server also bounds UTF-8 bytes, because an emoji
# costs 4 bytes and a bytes-blind check would let a ~8KB post through the
# 2200-character gate.
MAX_CAPTION_BYTES = int(os.getenv("COMMUNITY_MAX_CAPTION_BYTES", str(MAX_CAPTION * 4)))
MAX_COMMENT = int(os.getenv("COMMUNITY_MAX_COMMENT", "500"))
MAX_MEDIA_PER_POST = int(os.getenv("COMMUNITY_MAX_MEDIA_PER_POST", "4"))
POST_RATE_LIMIT_SECONDS = int(os.getenv("COMMUNITY_POST_RATE_LIMIT_SECONDS", "60"))
COMMENT_RATE_LIMIT_SECONDS = int(os.getenv("COMMUNITY_COMMENT_RATE_LIMIT_SECONDS", "5"))
UPLOAD_RATE_LIMIT_SECONDS = int(os.getenv("COMMUNITY_UPLOAD_RATE_LIMIT_SECONDS", "10"))
# How many presigns one wallet may request inside the upload window. A single
# post carries up to MAX_MEDIA_PER_POST files and every video also uploads a
# poster frame, so allowing only ONE presign per window made any post with a
# second image fail ("Please wait before uploading again") after file 1.
UPLOAD_BURST_ALLOWANCE = max(1, MAX_MEDIA_PER_POST) * 2
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 30

# A public post must carry a real username. Without this gate a user who never
# picked one publishes a shortened wallet address, which looks like spam and
# partly undoes the "the browser never sees an address" rule. Enforced in the
# service, not just the UI, so a direct API call cannot bypass it.
REQUIRE_USERNAME = os.getenv("COMMUNITY_REQUIRE_USERNAME", "1").strip() != "0"

# Shown for a legacy row written before the username requirement. Deliberately
# NOT a wallet address: an old post must not leak one either.
ANON_LABEL = "Community member"

# Video is accepted but bounded; images get the tighter cap.
MAX_IMAGE_MB = int(os.getenv("R2_MAX_IMAGE_MB", "10"))
MAX_VIDEO_MB = int(os.getenv("R2_MAX_VIDEO_MB", "100"))
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
                 my_reaction: str | None = None) -> dict:
    """Shape a post for the browser.

    The wallet address is dropped and ownership is reduced to ``is_mine`` — a
    boolean — so a feed response can never leak an address.
    """
    owner = (row.get("wallet_address") or "").lower()
    return {
        "id": row.get("id"),
        "username": row.get("username") or ANON_LABEL,
        "body": row.get("body") or "",
        "media": media,
        "reaction_count": int(row.get("reaction_count") or 0),
        "comment_count": int(row.get("comment_count") or 0),
        "my_reaction": my_reaction,
        "is_mine": bool(viewer) and owner == viewer.lower(),
        "is_edited": bool(row.get("edited_at")),
        "created_at": row.get("created_at"),
        "edited_at": row.get("edited_at"),
    }


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


def _public_comment(row: dict, viewer: str | None) -> dict:
    owner = (row.get("wallet_address") or "").lower()
    return {
        "id": row.get("id"),
        "username": row.get("username") or ANON_LABEL,
        "body": row.get("body") or "",
        "is_mine": bool(viewer) and owner == viewer.lower(),
        "created_at": row.get("created_at"),
    }


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


def get_feed(viewer: str | None = None, cursor: int | None = None,
             limit: int | None = None, author: str | None = None) -> dict:
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

    try:
        query = (
            supabase.table("community_feed_posts")
            .select("*")
            .eq("is_deleted", False)
            .eq("is_hidden", False)
        )
        if author:
            query = query.ilike("wallet_address", author)
        if cursor:
            query = query.lt("id", int(cursor))
        # Fetch one extra row to know whether another page exists.
        result = query.order("id", desc=True).limit(size + 1).execute()
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: feed query failed: %s", exc)
        return {"success": False, "error": "Could not load the feed", "posts": []}

    has_more = len(rows) > size
    rows = rows[:size]
    post_ids = [r.get("id") for r in rows if r.get("id") is not None]
    media = _media_for_posts(supabase, post_ids)
    reactions = _reactions_for_posts(supabase, post_ids, viewer)

    posts = [
        _public_post(row, media.get(row.get("id"), []), viewer,
                     reactions.get(row.get("id")))
        for row in rows
    ]
    return {
        "success": True,
        "posts": posts,
        "next_cursor": posts[-1]["id"] if posts and has_more else None,
        "has_more": has_more,
    }


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
    media = _media_for_posts(supabase, [row.get("id")]).get(row.get("id"), [])
    reactions = _reactions_for_posts(supabase, [row.get("id")], viewer)
    return {"success": True,
            "post": _public_post(row, media, viewer, reactions.get(row.get("id")))}


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


def _like_pattern(term: str) -> str:
    """Wrap a search term for PostgREST ``ilike``, escaping LIKE metacharacters.

    ``_`` IS a legal username character, so it cannot be stripped like ``%`` —
    but it is also a single-character wildcard, so a raw term would match names
    the user never asked for. Escaping it (SQL LIKE's default escape is ``\\``)
    keeps the search literal.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
            .select("username")
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

    users = []
    seen = set()
    for row in rows:
        username = (row.get("username") or "").strip()
        key = username.lower()
        # A row may carry no username (legacy) or the same name twice; the
        # public view is a set of names, never a list of accounts.
        if not username or key in seen:
            continue
        seen.add(key)
        users.append({"username": username})
    return {"success": True, "users": users, "term": cleaned}


def wallet_for_username(username: str | None) -> str | None:
    """Resolve a public username to its owner wallet (server-side only)."""
    name = normalize_username(username)
    if not name:
        return None
    supabase = _get_supabase()
    if not supabase:
        return None
    try:
        result = (
            supabase.table("user_data")
            .select("wallet_address")
            .ilike("username", name)
            .limit(1)
            .execute()
        )
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: profile wallet lookup failed: %s", exc)
        return None
    if not rows:
        return None
    return (rows[0].get("wallet_address") or "").strip() or None


def _count(supabase, table: str, filters: list[tuple[str, str, object]]) -> int:
    """Bounded row count. Falls back to len(rows) when the client gives no count.

    ``filters`` are ``(op, column, value)`` triples; ``op`` is ``"eq"`` or
    ``"ilike"``. The wallet is matched with ``ilike`` because sessions carry
    checksummed addresses while rows are stored lowercase (the documented
    case-mismatch bug class).
    """
    try:
        query = supabase.table(table).select("id", count="exact")
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


def get_public_profile(username: str | None) -> dict:
    """Username-scoped public profile: identity + feed activity counts.

    Contains NO wallet, balance or private activity. ``post_count`` /
    ``album_count`` / ``media_count`` come from the feed's own tables; the
    public message count is owned by the chatroom and is hydrated by the page
    from the chatroom's own endpoint, so neither module reads the other's data.
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
            name = (rows[0].get("username") or name).strip() or name
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: profile identity read failed: %s", exc)

    post_count = _count(
        supabase, "community_feed_posts",
        [("ilike", "wallet_address", wallet),
         ("eq", "is_deleted", False),
         ("eq", "is_hidden", False)])

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

    return {
        "success": True,
        "profile": {
            "username": name,
            "joined_at": joined_at,
            "post_count": post_count,
            "album_count": album_count,
            "media_count": media_count,
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


def create_post(wallet: str, caption: str | None, media_items: list[dict]) -> dict:
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

    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}

    payload = {
        "wallet_address": (wallet or "").lower(),
        "username": username,
        "body": clean or None,
    }
    try:
        result = supabase.table("community_feed_posts").insert(payload).execute()
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: post insert failed: %s", exc)
        return {"success": False, "error": "Could not publish your post",
                "code": "db"}
    if not rows:
        return {"success": False, "error": "Could not publish your post",
                "code": "db"}

    post_row = rows[0]
    inserted = _record_media(supabase, post_row["id"], media_items)
    post = _public_post(post_row, [_public_media(r) for r in inserted], wallet)
    return {"success": True, "post": post}


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

    previous = _current_reaction(supabase, post_id, wallet)
    if previous == reaction:
        # Tapping your own active chip removes it.
        return clear_reaction(wallet, post_id, _supabase=supabase, _post=post)

    try:
        if previous:
            # Switch: update in place, count does not move.
            supabase.table("community_feed_reactions").update(
                {"reaction": reaction}
            ).eq("post_id", int(post_id)).ilike(
                "wallet_address", wallet).execute()
            count = int(post.get("reaction_count") or 0)
        else:
            supabase.table("community_feed_reactions").insert({
                "post_id": int(post_id),
                "wallet_address": wallet.lower(),
                "reaction": reaction,
            }).execute()
            count = int(post.get("reaction_count") or 0) + 1
            _write_reaction_count(supabase, post_id, count)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: reaction write failed: %s", exc)
        return {"success": False, "error": "Could not save your reaction",
                "code": "db"}

    return {"success": True, "reaction_count": count, "my_reaction": reaction}


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

    previous = _current_reaction(supabase, post_id, wallet)
    if not previous:
        return {"success": True,
                "reaction_count": int(post.get("reaction_count") or 0),
                "my_reaction": None}

    try:
        supabase.table("community_feed_reactions").delete().eq(
            "post_id", int(post_id)).ilike("wallet_address", wallet).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: reaction delete failed: %s", exc)
        return {"success": False, "error": "Could not remove your reaction",
                "code": "db"}

    count = max(0, int(post.get("reaction_count") or 0) - 1)
    _write_reaction_count(supabase, post_id, count)
    return {"success": True, "reaction_count": count, "my_reaction": None}


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


def _load_visible_post(supabase, post_id: int) -> dict | None:
    try:
        result = (
            supabase.table("community_feed_posts")
            .select("id,wallet_address,reaction_count,is_deleted,is_hidden")
            .eq("id", int(post_id))
            .limit(1)
            .execute()
        )
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_feed: post load failed: %s", exc)
        return None
    if not rows:
        return None
    row = rows[0]
    if row.get("is_deleted") or row.get("is_hidden"):
        return None
    return row


# ── Comments ─────────────────────────────────────────────────────────────────

def add_comment(wallet: str, post_id: int, body: str) -> dict:
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

    try:
        result = supabase.table("community_feed_comments").insert({
            "post_id": int(post_id),
            "wallet_address": wallet.lower(),
            "username": username,
            "body": clean,
        }).execute()
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: comment insert failed: %s", exc)
        return {"success": False, "error": "Could not post your comment",
                "code": "db"}
    if not rows:
        return {"success": False, "error": "Could not post your comment",
                "code": "db"}

    _bump_comment_count(supabase, post_id, +1)
    return {"success": True, "comment": _public_comment(rows[0], wallet)}


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


def get_comments(post_id: int, viewer: str | None = None, limit: int = 50) -> dict:
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "comments": []}
    try:
        result = (
            supabase.table("community_feed_comments")
            .select("*")
            .eq("post_id", int(post_id))
            .eq("is_deleted", False)
            .order("id")
            .limit(max(1, min(200, int(limit or 50))))
            .execute()
        )
        rows = _rows(result)
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: comment list failed: %s", exc)
        return {"success": False, "error": "Could not load comments", "comments": []}
    return {"success": True,
            "comments": [_public_comment(r, viewer) for r in rows]}


def delete_comment(wallet: str, comment_id: int) -> dict:
    if not wallet:
        return {"success": False, "error": "Not authenticated", "code": "auth"}
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}
    try:
        result = (
            supabase.table("community_feed_comments")
            .select("post_id,wallet_address,is_deleted")
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
    _bump_comment_count(supabase, rows[0].get("post_id"), -1)
    return {"success": True}


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
    try:
        supabase.table("community_feed_posts").update(
            {"is_hidden": bool(hidden)}).eq("id", int(post_id)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_feed: hide failed: %s", exc)
        return {"success": False, "error": "Could not update the post", "code": "db"}
    if hidden:
        _resolve_reports(supabase, post_id)
    return {"success": True, "is_hidden": bool(hidden)}


def admin_delete_post(post_id: int, admin_wallet: str) -> dict:
    """Hard-stop a post: hide it, mark it deleted, and purge the R2 objects."""
    supabase = _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable", "code": "db"}
    keys = _media_keys(supabase, int(post_id))
    if not _mark_deleted(supabase, int(post_id), admin_wallet, hidden=True):
        return {"success": False, "error": "Could not delete the post", "code": "db"}
    _resolve_reports(supabase, post_id)
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
