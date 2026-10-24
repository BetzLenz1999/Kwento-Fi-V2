"""Community badge definitions, evaluation, public listing, and moderation.

All reads and writes use the server-side Supabase service-role client. Callers
should invoke the event hooks only after the corresponding server-side write is
committed; evaluation is deliberately best-effort and never raises into the
original post/reaction action. In particular, ``on_daily_learn_completed`` is
for the trusted completion path after the server has validated a complete
2/2 cycle, never for card opens or client-submitted completion claims.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFINITIONS_TABLE = "community_badge_definitions"
AWARDS_TABLE = "community_user_badges"
POSTS_TABLE = "community_feed_posts"
REACTIONS_TABLE = "community_feed_reactions"
BANS_TABLE = "community_feed_bans"
DAILY_COMPLETIONS_TABLE = "community_daily_learn_completions"
POSITIVE_REACTIONS = frozenset({"like", "love", "haha", "wow"})

# Thresholds are server policy; no caller-supplied counts or thresholds are
# accepted. Change these constants (or replace them with a future admin config)
# rather than letting browser input change badge eligibility.
HELPFUL_REACTION_THRESHOLD = 10
HELPFUL_POST_THRESHOLD = 3
PROMPT_RESPONSE_THRESHOLD = 3
CONTRIBUTOR_DAYS_THRESHOLD = 7
CONTRIBUTOR_WINDOW_DAYS = 30
BUILDER_PROMPT_THRESHOLD = 5
BUILDER_REACTION_THRESHOLD = 5
DAILY_LEARN_DAYS_THRESHOLD = 7

# Public metadata is intentionally separate from the private award/revocation
# columns stored in Supabase. Keep aligned with sql/community_badges.sql.
BADGE_CATALOGUE: dict[str, dict[str, Any]] = {
    "first_post": {
        "name": "First Post",
        "description": "Published your first visible Community post.",
        "icon_key": "message-circle",
        "color_token": "community-orange",
        "display_order": 10,
    },
    "first_reaction": {
        "name": "First Reaction",
        "description": "Reacted to another member’s visible post.",
        "icon_key": "heart",
        "color_token": "community-orange",
        "display_order": 20,
    },
    "community_voice": {
        "name": "Community Voice",
        "description": "Answered official Community prompts on three different UTC dates.",
        "icon_key": "mic-2",
        "color_token": "community-gold",
        "display_order": 30,
    },
    "helpful_member": {
        "name": "Helpful Member",
        "description": "Received at least 10 positive reactions across three or more posts.",
        "icon_key": "spark-heart",
        "color_token": "community-gold",
        "display_order": 40,
    },
    "consistent_contributor": {
        "name": "Consistent Contributor",
        "description": "Posted on seven distinct UTC dates within 30 days.",
        "icon_key": "calendar-check",
        "color_token": "community-orange",
        "display_order": 50,
    },
    "community_builder": {
        "name": "Community Builder",
        "description": "Shared five official prompt responses and received five positive reactions, without an active moderation penalty.",
        "icon_key": "users-round",
        "color_token": "community-gold",
        "display_order": 60,
    },
    "daily_learner": {
        "name": "Daily Learner",
        "description": "Completed Daily Learn on seven distinct UTC days.",
        "icon_key": "book-open-check",
        "color_token": "community-orange",
        "display_order": 70,
    },
}


def _get_supabase():
    """Return only the service-role client; never fall back to an anon client."""
    try:
        from supabase_client import get_supabase_admin_client
        return get_supabase_admin_client()
    except Exception as exc:  # noqa: BLE001 - badge problems must not affect feed
        logger.warning("community_badges: service-role client unavailable: %s", exc)
        return None


def _rows(result) -> list[dict]:
    if not result:
        return []
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    return list(data or [])


def _wallet(value: str | None) -> str:
    return str(value or "").strip().lower()


def _utc_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _utc_day(value) -> date | None:
    parsed = _utc_datetime(value)
    return parsed.date() if parsed else None


def _fetch(client, table: str, *, columns: str = "*", filters=()) -> list[dict]:
    query = client.table(table).select(columns)
    for method, field, value in filters:
        query = getattr(query, method)(field, value)
    return _rows(query.execute())


def _visible_posts_for_wallet(client, wallet: str) -> list[dict]:
    """Load only public, nondeleted, nonhidden posts owned by ``wallet``."""
    return _fetch(
        client, POSTS_TABLE,
        filters=(
            ("eq", "wallet_address", wallet),
            ("eq", "visibility", "public"),
            ("eq", "is_deleted", False),
            ("eq", "is_hidden", False),
        ),
    )


def _badge_activity_start(client) -> datetime | None:
    """Use the fresh catalogue seed as the prospective-only rollout fence."""
    definitions = _fetch(client, DEFINITIONS_TABLE, columns="key,created_at")
    dates = [parsed for row in definitions
             if (parsed := _utc_datetime(row.get("created_at"))) is not None]
    return min(dates) if dates else None


def _visible_posts_by_ids(client, post_ids) -> list[dict]:
    ids = list(dict.fromkeys(post_ids))
    if not ids:
        return []
    return _fetch(
        client, POSTS_TABLE,
        filters=(("in_", "id", ids), ("eq", "visibility", "public"),
                 ("eq", "is_deleted", False), ("eq", "is_hidden", False)),
    )


def _positive_received_counts(client, wallet: str, posts: list[dict],
                              activity_start: datetime) -> tuple[int, set[str]]:
    post_by_id = {str(row.get("id")): row for row in posts if row.get("id") is not None}
    if not post_by_id:
        return 0, set()
    reactions = _fetch(
        client, REACTIONS_TABLE,
        filters=(("in_", "post_id", [row.get("id") for row in posts]),
                 ("in_", "reaction", sorted(POSITIVE_REACTIONS))),
    )
    qualifying = [
        reaction for reaction in reactions
        if str(reaction.get("reaction") or "").lower() in POSITIVE_REACTIONS
        and _wallet(reaction.get("wallet_address")) != wallet
        and str(reaction.get("post_id")) in post_by_id
        and (created := _utc_datetime(reaction.get("created_at"))) is not None
        and created >= activity_start
    ]
    return len(qualifying), {str(row.get("post_id")) for row in qualifying}


def _prompt_responses(posts: list[dict]) -> list[dict]:
    # ``prompt_id`` is the official link on a Community prompt response. Daily
    # Learn quiz completions are recorded separately and are never prompt rows.
    return [row for row in posts if row.get("prompt_id") not in (None, "", 0, "0")]


def _distinct_utc_days(rows: list[dict], field: str = "created_at") -> set[date]:
    return {day for row in rows if (day := _utc_day(row.get(field))) is not None}


def _active_award_exists(client, wallet: str, badge_key: str) -> bool:
    rows = _fetch(
        client, AWARDS_TABLE, columns="id,revoked_at",
        filters=(("eq", "wallet_address", wallet), ("eq", "badge_key", badge_key)),
    )
    # Lifetime uniqueness includes revoked records: a revoke never makes an
    # award eligible to be re-issued.
    return bool(rows)


def award_badge(wallet_address: str, badge_key: str, *, reason: str | None = None,
                event_key: str | None = None, metadata: dict | None = None,
                client=None) -> dict:
    """Award once for a lifetime; return only a minimal internal status.

    The DB unique constraints are the race-safe authority. ``event_key`` is
    server-created and never included in public payloads.
    """
    wallet = _wallet(wallet_address)
    if not wallet or badge_key not in BADGE_CATALOGUE:
        return {"success": False, "awarded": False}
    db = client or _get_supabase()
    if not db:
        return {"success": False, "awarded": False}
    key = event_key or f"community-badge:{badge_key}:{wallet}"
    try:
        if _active_award_exists(db, wallet, badge_key):
            return {"success": True, "awarded": False, "already_awarded": True}
        db.table(AWARDS_TABLE).insert({
            "wallet_address": wallet,
            "badge_key": badge_key,
            "award_reason": reason or f"Qualified for {badge_key}",
            "source_event_key": key,
            "metadata": metadata or {},
        }).execute()
        return {"success": True, "awarded": True}
    except Exception as exc:  # concurrent/duplicate awards are harmless
        logger.info("community_badges: award write was not applied (%s): %s", badge_key, exc)
        return {"success": False, "awarded": False}


def evaluate_badges(event_type: str, wallet_address: str, *, post_id=None,
                    day_utc: str | date | None = None, client=None) -> dict:
    """Evaluate applicable badge rules against authoritative server-side rows.

    ``event_type`` is an internal hook label (e.g. ``post_created``,
    ``reaction_added``, ``post_hidden``, or ``community_daily_learn_completed``).
    The function is safe to call repeatedly and swallows DB failures so the
    originating action remains successful. It intentionally performs no global
    history scan; evaluation is scoped to the event owner.
    """
    wallet = _wallet(wallet_address)
    if not wallet:
        return {"success": False, "awarded": []}
    db = client or _get_supabase()
    if not db:
        return {"success": False, "awarded": []}
    awarded: list[str] = []
    try:
        activity_start = _badge_activity_start(db)
        if activity_start is None:
            # A missing/unseeded catalogue is not permission to sweep historical
            # activity; fail closed until the migration has been applied.
            return {"success": False, "awarded": []}

        # Record completion only when the trusted caller confirms a full cycle.
        # No score, answers, wallet, or client event payload is public.
        if event_type == "community_daily_learn_completed":
            if day_utc is not None:
                try:
                    completed_day = date.fromisoformat(
                        day_utc.isoformat() if isinstance(day_utc, date) else str(day_utc)
                    )
                except (TypeError, ValueError):
                    return {"success": False, "awarded": []}
                day_text = completed_day.isoformat()
                try:
                    db.table(DAILY_COMPLETIONS_TABLE).insert({
                        "wallet_address": wallet,
                        "day_utc": day_text,
                        "source_event_key": f"daily-learn-completed:{wallet}:{day_text}",
                    }).execute()
                except Exception as exc:
                    # The unique wallet/day constraint means a repeated successful
                    # callback is already represented; continue with the count.
                    logger.debug("community_badges: completion event duplicate/unavailable: %s", exc)

        # Data reads are intentionally performed only on demand for the rules
        # that rely on them. A failed read aborts this evaluation safely.
        if event_type in {"post_created", "post_hidden", "post_deleted", "moderation_action",
                          "reaction_added", "reaction_removed", "community_daily_learn_completed"}:
            posts = [row for row in _visible_posts_for_wallet(db, wallet)
                     if (created := _utc_datetime(row.get("created_at"))) is not None
                     and created >= activity_start]
            prompt_posts = _prompt_responses(posts)
            candidates: list[tuple[str, bool, str]] = []

            if event_type in {"post_created", "post_hidden", "post_deleted", "moderation_action"}:
                candidates.append(("first_post", bool(posts), "first visible post"))
                dates = _distinct_utc_days(posts)
                now = datetime.now(timezone.utc)
                cutoff = now - timedelta(days=CONTRIBUTOR_WINDOW_DAYS)
                recent_days = {
                    day for row in posts
                    if (dt := _utc_datetime(row.get("created_at"))) is not None
                    and cutoff <= dt <= now
                    for day in [dt.date()]
                }
                candidates.append(("consistent_contributor",
                                   len(recent_days) >= CONTRIBUTOR_DAYS_THRESHOLD,
                                   "seven distinct visible-post UTC dates in 30 days"))
                candidates.append(("community_voice",
                                   len(_distinct_utc_days(prompt_posts)) >= PROMPT_RESPONSE_THRESHOLD,
                                   "official prompt responses on three UTC dates"))

            if event_type in {"post_created", "post_hidden", "post_deleted", "moderation_action",
                              "reaction_added", "reaction_removed"}:
                positive_count, reacted_post_ids = _positive_received_counts(
                    db, wallet, posts, activity_start
                )
                candidates.append(("helpful_member",
                                   positive_count >= HELPFUL_REACTION_THRESHOLD
                                   and len(reacted_post_ids) >= HELPFUL_POST_THRESHOLD,
                                   "positive reactions across at least three visible posts"))
                candidates.append(("community_voice",
                                   len(_distinct_utc_days(prompt_posts)) >= PROMPT_RESPONSE_THRESHOLD,
                                   "official prompt responses on three UTC dates"))
                if event_type in {"post_created", "post_hidden", "post_deleted", "moderation_action"}:
                    candidates.append(("first_post", bool(posts), "first visible post"))
                    recent_days = {
                        dt.date() for row in posts
                        if (dt := _utc_datetime(row.get("created_at"))) is not None
                        and datetime.now(timezone.utc) - timedelta(days=CONTRIBUTOR_WINDOW_DAYS)
                        <= dt <= datetime.now(timezone.utc)
                    }
                    candidates.append(("consistent_contributor",
                                       len(recent_days) >= CONTRIBUTOR_DAYS_THRESHOLD,
                                       "seven distinct visible-post UTC dates in 30 days"))

                no_active_penalty = not _fetch(
                    db, BANS_TABLE, columns="wallet_address",
                    filters=(("eq", "wallet_address", wallet),),
                )
                candidates.append(("community_builder",
                                   len(prompt_posts) >= BUILDER_PROMPT_THRESHOLD
                                   and positive_count >= BUILDER_REACTION_THRESHOLD
                                   and no_active_penalty,
                                   "five prompt responses, five positive reactions, no active penalty"))

            for key, qualified, award_reason in candidates:
                if qualified and not _active_award_exists(db, wallet, key):
                    result = award_badge(wallet, key, reason=award_reason, client=db)
                    if result.get("awarded"):
                        awarded.append(key)

        if event_type == "reaction_added":
            # First Reaction accepts any successful reaction type, but must be
            # on another member's currently visible public post.
            own_reactions = _fetch(
                db, REACTIONS_TABLE, columns="post_id,wallet_address,created_at",
                filters=(("eq", "wallet_address", wallet),),
            )
            own_reactions = [row for row in own_reactions
                             if (created := _utc_datetime(row.get("created_at"))) is not None
                             and created >= activity_start]
            active_posts = _visible_posts_by_ids(db, [r.get("post_id") for r in own_reactions])
            if any(_wallet(row.get("wallet_address")) != wallet for row in active_posts):
                if not _active_award_exists(db, wallet, "first_reaction"):
                    result = award_badge(wallet, "first_reaction",
                                         reason="first reaction on another member’s visible post", client=db)
                    if result.get("awarded"):
                        awarded.append("first_reaction")

        if event_type == "community_daily_learn_completed":
            completions = _fetch(
                db, DAILY_COMPLETIONS_TABLE, columns="day_utc",
                filters=(("eq", "wallet_address", wallet),),
            )
            completed_days = {str(row.get("day_utc")) for row in completions if row.get("day_utc")}
            if len(completed_days) >= DAILY_LEARN_DAYS_THRESHOLD and not _active_award_exists(
                db, wallet, "daily_learner"
            ):
                result = award_badge(
                    wallet, "daily_learner",
                    reason="completed Daily Learn on seven distinct UTC days", client=db,
                )
                if result.get("awarded"):
                    awarded.append("daily_learner")

        return {"success": True, "awarded": awarded}
    except Exception as exc:  # noqa: BLE001 - best effort by contract
        logger.warning("community_badges: evaluation failed for event %s: %s", event_type, exc)
        return {"success": False, "awarded": awarded}


def on_post_created(wallet_address: str, post_id=None, *, client=None) -> dict:
    return evaluate_badges("post_created", wallet_address, post_id=post_id, client=client)


def on_reaction_added(wallet_address: str, post_id=None, *, client=None) -> dict:
    return evaluate_badges("reaction_added", wallet_address, post_id=post_id, client=client)


def on_reaction_removed(wallet_address: str, post_id=None, *, client=None) -> dict:
    return evaluate_badges("reaction_removed", wallet_address, post_id=post_id, client=client)


def on_post_visibility_changed(wallet_address: str, post_id=None, *, client=None) -> dict:
    """Re-evaluate after a post becomes hidden/deleted or moderation changes."""
    return evaluate_badges("moderation_action", wallet_address, post_id=post_id, client=client)


def on_daily_learn_completed(wallet_address: str, day_utc: str | date, *, client=None) -> dict:
    """Record a server-confirmed full 2/2 completion and evaluate Daily Learner."""
    return evaluate_badges("community_daily_learn_completed", wallet_address,
                           day_utc=day_utc, client=client)


def list_public_badges(wallet_address: str, *, client=None) -> dict:
    """Return public metadata and award dates only; wallet/reason/keys stay private."""
    wallet = _wallet(wallet_address)
    if not wallet:
        return {"success": False, "badges": []}
    db = client or _get_supabase()
    if not db:
        return {"success": False, "badges": []}
    try:
        awards = _fetch(
            db, AWARDS_TABLE, columns="badge_key,awarded_at,revoked_at",
            filters=(("eq", "wallet_address", wallet),),
        )
        active = {row.get("badge_key"): row for row in awards if not row.get("revoked_at")}
        if not active:
            return {"success": True, "badges": []}
        definitions = _fetch(
            db, DEFINITIONS_TABLE, columns="key,name,description,icon_key,display_order,is_enabled",
            filters=(("eq", "is_enabled", True),),
        )
        by_key = {row.get("key"): row for row in definitions}
        public = []
        for key, award in active.items():
            definition = by_key.get(key)
            if not definition:
                continue
            public.append({
                "key": key,
                "name": definition.get("name"),
                "description": definition.get("description"),
                "icon_key": definition.get("icon_key"),
                "awarded_at": award.get("awarded_at"),
            })
        public.sort(key=lambda item: (by_key[item["key"]].get("display_order") or 0, item["key"]))
        return {"success": True, "badges": public}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_badges: public badge list unavailable: %s", exc)
        return {"success": False, "badges": []}


def list_my_badges(wallet_address: str, *, client=None) -> dict:
    """Owner listing shares the same deliberately public, wallet-free payload."""
    return list_public_badges(wallet_address, client=client)


def evaluate_wallet(wallet: str, event_type: str | None = None, *, client=None) -> dict:
    """Parent-facing event hook. Returns exactly ``success`` and ``new_badges``."""
    if event_type:
        results = [evaluate_badges(event_type, wallet, client=client)]
    else:
        # A caller without an event label asks for a scoped reconciliation of
        # this wallet's current eligible activity, not a global backfill.
        results = [
            evaluate_badges("post_created", wallet, client=client),
            evaluate_badges("reaction_added", wallet, client=client),
            evaluate_badges("community_daily_learn_completed", wallet, client=client),
        ]
    fresh = list(dict.fromkeys(key for result in results
                               for key in (result.get("awarded") or [])))
    return {"success": all(bool(result.get("success")) for result in results),
            "new_badges": fresh}


def public_badges_for_wallet(wallet: str, *, client=None) -> list[dict]:
    """Public metadata list for a wallet; the wallet itself is never included."""
    result = list_public_badges(wallet, client=client)
    return list(result.get("badges") or []) if result.get("success") else []


def public_badges_for_wallets(wallets, *, client=None) -> dict[str, list[dict]]:
    """Fetch badge metadata in two bounded queries; expose at most 3 per owner."""
    normalized = list(dict.fromkeys(_wallet(wallet) for wallet in (wallets or []) if _wallet(wallet)))
    output = {wallet: [] for wallet in normalized}
    if not normalized:
        return output
    db = client or _get_supabase()
    if not db:
        return output
    try:
        awards = _fetch(
            db, AWARDS_TABLE, columns="wallet_address,badge_key,awarded_at,revoked_at",
            filters=(("in_", "wallet_address", normalized),),
        )
        active = [row for row in awards if not row.get("revoked_at")]
        keys = list(dict.fromkeys(row.get("badge_key") for row in active if row.get("badge_key")))
        if not keys:
            return output
        definitions = _fetch(
            db, DEFINITIONS_TABLE,
            columns="key,name,description,icon_key,display_order,is_enabled",
            filters=(("in_", "key", keys), ("eq", "is_enabled", True)),
        )
        by_key = {row.get("key"): row for row in definitions}
        for row in active:
            wallet = _wallet(row.get("wallet_address"))
            definition = by_key.get(row.get("badge_key"))
            if wallet not in output or not definition:
                continue
            output[wallet].append({
                "key": row.get("badge_key"),
                "name": definition.get("name"),
                "description": definition.get("description"),
                "icon_key": definition.get("icon_key"),
                "awarded_at": row.get("awarded_at"),
                "_display_order": definition.get("display_order") or 0,
            })
        for wallet, items in output.items():
            items.sort(key=lambda item: (item.pop("_display_order", 0), item["key"]))
            output[wallet] = items[:3]
        return output
    except Exception as exc:  # noqa: BLE001 - badges never break feed rendering
        logger.warning("community_badges: batched public badge read unavailable: %s", exc)
        return output


def revoke_badge(wallet_address: str, badge_key: str, actor: str | None = None,
                 reason: str | None = None, *, client=None,
                 actor_wallet: str | None = None) -> dict:
    """Privately record moderator revocation; never return the reason or wallet."""
    wallet, actor = _wallet(wallet_address), _wallet(actor or actor_wallet)
    private_reason = str(reason or "").strip()
    if not wallet or not actor or badge_key not in BADGE_CATALOGUE or not private_reason:
        return {"success": False, "revoked": False}
    db = client or _get_supabase()
    if not db:
        return {"success": False, "revoked": False}
    try:
        rows = _fetch(
            db, AWARDS_TABLE, columns="id,revoked_at",
            filters=(("eq", "wallet_address", wallet), ("eq", "badge_key", badge_key)),
        )
        active = next((row for row in rows if not row.get("revoked_at")), None)
        if not active:
            return {"success": True, "revoked": False}
        db.table(AWARDS_TABLE).update({
            "revoked_at": datetime.now(timezone.utc).isoformat(),
            "revoked_by": actor,
            "revoke_reason": private_reason,
        }).eq("id", active.get("id")).is_("revoked_at", "null").execute()
        return {"success": True, "revoked": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_badges: revoke failed for %s: %s", badge_key, exc)
        return {"success": False, "revoked": False}
