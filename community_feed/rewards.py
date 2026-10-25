"""Community Feed — G$ rewards for reactions and comments.

Design doc: ``docs/COMMUNITY_REWARDS_LEDGER_PROPOSAL.md``.

The rules (all of them are enforced here, not in the UI):

- Commenting on someone else's post pays the commenter ``comment_in`` (1.5 G$
  by default) and that post's owner ``comment_out`` (2.5 G$).
- Reacting to someone else's post pays the reactor ``reaction_in`` (0.1 G$) and
  that post's owner ``reaction_out`` (0.5 G$).
- The rates are unequal on purpose, and the gap is the design: a comment costs
  thought and leaves content behind, a tap costs nothing and leaves no trace.
  Paying them the same would make the cheapest action the most profitable one.
- **Self-activity earns nothing.** Without this a user farms by reacting to
  their own posts in a loop — the cheapest exploit there is.
- **Switching a reaction earns nothing** (the credit is per post+reactor, and
  ``community_feed_reactions`` already allows one reaction per wallet per post).
- **Un-react then re-react earns nothing.** The un-react writes a reversal and
  the re-react finds its idempotency key already consumed. Net zero. This is
  the anti-farm property a naive "add on every write" design lacks.
- Daily cap per wallet and a cap per post, because a reward per action with no
  ceiling is an invitation.

Non-negotiables from the design doc:

- **There is no stored balance.** The balance is the sum of an append-only
  ledger; the database blocks UPDATE/DELETE on it (see the SQL). Nothing in
  this module — or in any admin surface — can write a balance directly.
- **A source event credits exactly once, forever** (unique ``idempotency_key``),
  and a credit is reversed at most once (unique ``reverses_ledger_id``).
- **v1 has no payout path.** Earnings accrue and are displayed; nothing is
  withdrawable. The 500 G$ threshold is a display/goal figure in v1.
- A reward failure must NEVER break the user's reaction or comment. Every
  public hook here swallows its own errors and logs them.

Prospective-only by construction: credits are written by the live hooks, so a
pre-existing reaction is never re-walked and never earns. The
``COMMUNITY_REWARD_START_AT`` timestamp is the additional guard used when a
one-time backfill is deliberately run (see the proposal §7).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from env_utils import get_env_decimal, get_env_int

logger = logging.getLogger(__name__)

FEATURE_NAME = "community_rewards_feature"

# Per-event rates, each an env var so the economics can be tuned without a
# deploy. They are deliberately NOT equal: a comment costs the user thought and
# leaves content behind, a tap costs nothing and leaves no trace, so paying them
# the same would make the cheapest action the most profitable one.
#
#   comment_in     I commented on someone's post        (I did the work)
#   comment_out    someone commented on my post          (I earned attention)
#   reaction_out   someone reacted to my post            (the creator signal)
#   reaction_in    I reacted to someone's post           (the farm vector)
#
# reaction_in is the smallest of the four by design: it is the only one that
# pays a user for tapping a stranger's post, and the only one with no content
# left behind to audit. It is priced as a nudge, not an income.
def _rate(name: str, default: str) -> Decimal:
    value = get_env_decimal(name, default)
    return value if value > 0 else Decimal("0")


#: The four rate names. Order is the anti-farm ordering (largest to smallest)
#: and is used verbatim as the JSON keys, the admin form field names, and the
#: card payload keys, so there is exactly one vocabulary.
RATE_NAMES = ("comment_out", "comment_in", "reaction_out", "reaction_in")

_ENV_RATE_DEFAULTS = {
    "comment_out": "2.5",
    "comment_in": "1.5",
    "reaction_out": "0.5",
    "reaction_in": "0.1",
}

# Hard server-side bounds. The admin UI carries the same numbers, but the bound
# that MATTERS is this one: an admin form is a convenience, not a control, and
# anything reaching the endpoint directly must be clamped here too.
#
# The floor is 0 on purpose — setting a rate to 0 is the supported way to switch
# an action off without a deploy (e.g. kill the tap reward during an incident).
# The ceiling is well under the shared daily cap: a rate above DAILY_CAP would
# make every credit unsatisfiable, which is a foot-gun, not a feature.
#
# NOTE: Daily Learn is deliberately NOT in RATE_NAMES, so it is not bounded by
# RATE_MAX. It has its own ceiling (DAILY_LEARN_RATE_MAX) AND its own daily
# allowance (daily_learn_daily_cap), because a single Daily Learn cycle can be
# worth more than the whole shared cap. Sharing one budget is what silently
# voided the Daily Learn credit: a 2 x 25 G$ cycle exactly consumed the 50 G$
# cap, so any reaction or comment in the same window blocked the reward.
RATE_MAX = get_env_decimal("COMMUNITY_REWARD_RATE_MAX", "10")
RATE_BOUNDS = {name: (Decimal("0"), RATE_MAX) for name in RATE_NAMES}

#: Where the admin overrides live. Stored as a JSON blob in the existing
#: ``maintenance_settings`` table, the same place ``community_stories_config``
#: keeps its numeric rewards — so this adds no new table for the config itself.
RATES_FEATURE = "community_reward_rates"

# 60s, matching maintenance_service's TTL. The cache is PER PROCESS: a write
# invalidates the worker that handled it, and the other gunicorn workers pick
# the change up within this window. That is the honest guarantee — do not claim
# "instant" across workers without a shared store.
_RATES_CACHE_TTL = get_env_int("COMMUNITY_REWARD_RATES_CACHE_TTL", 60)
_rates_cache: tuple = (None, 0.0)

# Caps. A daily cap is the single most effective control: it bounds the worst
# case per account per day no matter how many reactions an attacker fabricates.
DAILY_CAP = get_env_decimal("COMMUNITY_REWARD_DAILY_CAP", "50")
POST_CAP = get_env_decimal("COMMUNITY_REWARD_POST_CAP", "100")

# The payout minimum. v1 does not pay out (no payout path ships), so this is
# the goal the card shows. 500 G$ is the gas-justified figure from the
# proposal §6; it ships as an env var so it can be raised without a deploy.
MIN_PAYOUT_GD = get_env_decimal("COMMUNITY_REWARD_MIN_PAYOUT_GD", "500")

# Community Daily Learn uses its own Admin Quiz Settings rate (G$ per correct
# answer), separate from the full Learn & Earn quiz's max-reward rate.
#
# The ceiling is operator-configurable (COMMUNITY_DAILY_LEARN_RATE_MAX). It used
# to be a hardcoded Decimal("50"), which meant the bound could not be raised
# without a code change AND - worse - a database rate above the code ceiling was
# silently rejected at credit time: the member completed a 2/2 cycle and received
# nothing. One source of truth is what prevents that mismatch.
#
# This is the DEFAULT only. Read the live value through daily_learn_rate_max().
DAILY_LEARN_RATE_MAX_DEFAULT = Decimal("50")

#: The database CHECK constraints are the last line of defence and are NOT
#: env-driven on purpose: a bug in the application must not be able to persist a
#: rate beyond this bound. Keep ``COMMUNITY_DAILY_LEARN_RATE_MAX`` at or below
#: the deployed constraint (see sql/community_daily_learn_rate_ceiling.sql).
DAILY_LEARN_RATE_DB_BOUND = Decimal("500")
DAILY_LEARN_EVENT = "daily_learn"
DAILY_LEARN_CLAIM_RPC = "community_daily_learn_claim_uncredited_questions"

# Daily Learn gets its OWN 24h allowance instead of sharing DAILY_CAP.
#
# The owner's requirement is that a completed cycle ALWAYS credits. One cycle is
# two questions, so the worst case is `2 x rate ceiling` G$, which can be larger
# than the shared cap — sharing one budget could therefore never satisfy the
# requirement. The two budgets are also summed over DISJOINT event types, so a
# reaction can neither block Daily Learn nor be blocked by it.
DEFAULT_DAILY_LEARN_CAP = Decimal("100")

#: The event types the SHARED daily cap counts. Daily Learn is excluded on
#: purpose: it is budgeted by ``daily_learn_daily_cap()`` instead, so a large
#: Daily Learn credit can never swallow the reaction/comment budget.
SHARED_CAP_EVENT_TYPES = ("reaction_in", "reaction_out", "comment_in", "comment_out")


def daily_learn_rate_max() -> Decimal:
    """The per-correct-answer ceiling, in G$.

    Read per call (not at import) so an operator can raise it without a
    redeploy, matching the repo convention for switches.

    A malformed, negative or non-finite value falls back to the default rather
    than to 0: a bad env var must not silently switch Daily Learn earning off
    (``rate = 0`` is the supported way to do that deliberately).

    The value is capped at ``DAILY_LEARN_RATE_DB_BOUND``. Exceeding the database
    CHECK would let the admin form accept a rate that the insert then rejects
    with a raw constraint violation, so clamp here and say so.
    """
    configured = get_env_decimal(
        "COMMUNITY_DAILY_LEARN_RATE_MAX", str(DAILY_LEARN_RATE_MAX_DEFAULT))
    if not configured.is_finite() or configured < 0:
        logger.warning(
            "community_rewards: COMMUNITY_DAILY_LEARN_RATE_MAX=%s is not a "
            "usable ceiling; using %s", configured, DAILY_LEARN_RATE_MAX_DEFAULT)
        return DAILY_LEARN_RATE_MAX_DEFAULT
    if configured > DAILY_LEARN_RATE_DB_BOUND:
        logger.warning(
            "community_rewards: COMMUNITY_DAILY_LEARN_RATE_MAX=%s exceeds the "
            "database bound (%s); using the bound",
            configured, DAILY_LEARN_RATE_DB_BOUND)
        return DAILY_LEARN_RATE_DB_BOUND
    return configured


def daily_learn_daily_cap() -> Decimal:
    """Daily Learn's own 24h allowance, in G$.

    Read per call (not at import) so an operator can change it without a
    redeploy, matching the repo convention for switches.

    The floor is one full cycle (``2 x daily_learn_rate_max()``): a smaller
    allowance would make a perfect 2/2 cycle partially or wholly unsatisfiable,
    which is the exact failure this budget exists to prevent. A value below the
    floor is raised to it and logged. ``rate = 0`` remains the supported way to
    switch Daily Learn earning off.
    """
    floor = daily_learn_rate_max() * 2
    configured = get_env_decimal(
        "COMMUNITY_REWARD_DAILY_LEARN_CAP", str(DEFAULT_DAILY_LEARN_CAP))
    if not configured.is_finite() or configured < floor:
        logger.warning(
            "community_rewards: COMMUNITY_REWARD_DAILY_LEARN_CAP=%s is below one "
            "full Daily Learn cycle (%s); using the floor so a 2/2 completion "
            "always credits", configured, floor)
        return floor
    return configured


def daily_learn_window_start(now: datetime | None = None) -> datetime:
    """Start of the current UTC day — the window Daily Learn is budgeted over.

    Deliberately NOT the trailing-24h window the social cap uses. The Daily Learn
    cycle itself is per UTC day (the ledger key is ``daily_learn:<day>:<wallet>``
    and resets at 00:00 UTC), so a rolling window would let the two clocks drift
    apart and block a user who completes the quiz at roughly the same time each
    day. Aligning the budget to the cycle's own boundary is what makes "a
    completed cycle always credits" true.
    """
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)


def daily_learn_budget() -> tuple[Decimal, datetime, tuple]:
    """The Daily Learn cap, window start and the event types it covers.

    One helper so the pre-check and the insertion-boundary check in ``_credit``
    can never disagree about which budget applies.
    """
    return (daily_learn_daily_cap(), daily_learn_window_start(),
            (DAILY_LEARN_EVENT,))

# Whether the card shows the VIEWER'S OWN balance and goal. Default ON for
# signed-in members; the client still withholds personal figures from guests.
# The balance is not spendable or on-chain, so the card clearly says that no
# withdrawal path is available yet. Read per call, not at import, so an operator
# can explicitly hide it without a redeploy — the repo convention for switches.
def show_balance() -> bool:
    return _env_flag("COMMUNITY_REWARD_SHOW_BALANCE", "1")

# Verification gate. Only a face-verified wallet may EARN. This is an earning
# gate, not an access gate: an unverified wallet may still post, react and
# comment — it just accrues nothing, and the card says why.
#
# The check is the ON-CHAIN GoodDollar identity read, NOT the session's
# "verified" key. That key is a LOGIN flag: the login path sets it for every
# wallet ("UBI check temporarily disabled — allow all wallets in"), so gating on
# it would let every unverified wallet earn, which is the opposite of the rule.
# ``blockchain.is_identity_verified`` is the same authority the referral payouts
# and the Telegram bot already gate on.
REQUIRE_VERIFIED = os.getenv("COMMUNITY_REWARD_REQUIRE_VERIFIED", "1").strip() != "0"

LEDGER = "community_reward_ledger"


# ── Configuration ────────────────────────────────────────────────────────────

def _env_flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip() != "0"


def rewards_enabled_env() -> bool:
    """Hard kill switch, independent of the database flag."""
    return _env_flag("COMMUNITY_REWARDS_ENABLED", "1")


def start_at() -> datetime | None:
    """The earliest event time that may earn, or None when ungated.

    Only used to fence a deliberate backfill. The primary reason old reactions
    never earn is that nothing re-walks them: credits come from the live hooks.
    """
    raw = (os.getenv("COMMUNITY_REWARD_START_AT") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        # An unparseable fence must not silently become "everything earns".
        logger.error("community_rewards: unparseable COMMUNITY_REWARD_START_AT=%r; "
                     "treating every event as not rewardable", raw)
        return datetime.max.replace(tzinfo=timezone.utc)


def is_rewardable(created_at) -> bool:
    """True when an event at ``created_at`` may earn."""
    fence = start_at()
    if fence is None:
        return True
    if created_at is None:
        # An event with no timestamp cannot be proven to be after the fence.
        return False
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc) >= fence


def is_enabled(*, _supabase=None) -> bool:
    """True only when the admin has turned rewards ON.

    Fails CLOSED, exactly like the feed itself: a missing row, a DB error or an
    unreadable value all mean "off". The env var is a hard kill switch on top.
    """
    if not rewards_enabled_env():
        return False
    supabase = _supabase or _get_supabase()
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: feature flag read failed: %s", exc)
        return False
    rows = _rows(result)
    if not rows:
        return False
    value = rows[0].get("is_maintenance")
    return value is False or value == 0 or value == "false" or value == "f"


# ── Infrastructure helpers ───────────────────────────────────────────────────

def _get_supabase():
    """Service-role client first (RLS-safe reads), falling back to anon."""
    try:
        from supabase_client import get_supabase_admin_client, get_supabase_client

        return get_supabase_admin_client() or get_supabase_client()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_rewards: supabase client unavailable: %s", exc)
        return None


def _rows(result) -> list[dict]:
    if not result:
        return []
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    return list(data or [])


def _as_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _same_wallet(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and str(a).strip().lower() == str(b).strip().lower()


# ── Rates: env defaults, admin overrides, one resolution path ────────────────
#
# The rate an event is credited at is resolved in ONE place (``_resolve_rates``)
# so the ledger, the caps and the card can never disagree. Precedence is:
#
#   1. an admin override in ``maintenance_settings`` (``community_reward_rates``)
#   2. the env var (``COMMUNITY_REWARD_COMMENT_OUT``, ...)
#   3. the shipped default (2.5 / 1.5 / 0.5 / 0.1)
#
# An override changes only FUTURE credits. The ledger stores the concrete
# ``delta_gd`` per row and is append-only, so past earnings are untouched — that
# is what makes an admin-editable rate safe rather than a way to rewrite history.

def _env_rate(name: str) -> Decimal:
    return _rate("COMMUNITY_REWARD_" + name.upper(), _ENV_RATE_DEFAULTS[name])


def _clamp(name: str, value: Decimal) -> Decimal:
    """Clamp to the server-side bounds. Out-of-range is clamped, not rejected.

    Clamping is the safer default for a value that is already live: rejecting a
    partial update would leave the rates in an unknown state, and an admin who
    typed 999 means "the maximum", not "fail". The raw value is logged so the
    intent is recoverable from the audit trail.
    """
    low, high = RATE_BOUNDS[name]
    if value < low:
        logger.warning("community_rewards: rate %s=%s below floor %s; clamped",
                       name, value, low)
        return low
    if value > high:
        logger.warning("community_rewards: rate %s=%s above ceiling %s; clamped",
                       name, value, high)
        return high
    return value


def _parse_rate(name: str, raw) -> Decimal | None:
    """Parse one rate, or None when it is unusable. Never raises."""
    if raw is None:
        return None
    try:
        value = Decimal(str(raw).strip())
    except Exception:  # noqa: BLE001
        logger.error("community_rewards: unparseable rate %s=%r; ignored", name, raw)
        return None
    if not value.is_finite():
        logger.error("community_rewards: non-finite rate %s=%r; ignored", name, raw)
        return None
    return _clamp(name, value)


def _read_overrides(supabase) -> dict:
    """The admin rate overrides, or {} when there are none / unreadable."""
    try:
        result = (
            supabase.table("maintenance_settings")
            .select("custom_message")
            .eq("feature_name", RATES_FEATURE)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: rate override read failed: %s", exc)
        return {}
    rows = _rows(result)
    if not rows:
        return {}
    raw = rows[0].get("custom_message")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        logger.error("community_rewards: rate override blob is not JSON; ignored")
        return {}
    if not isinstance(parsed, dict):
        logger.error("community_rewards: rate override blob is not an object; ignored")
        return {}
    overrides = {}
    for name in RATE_NAMES:
        value = _parse_rate(name, parsed.get(name))
        if value is not None:
            overrides[name] = value
    return overrides


def _resolve_rates(supabase) -> dict:
    """Resolve all four rates from ONE override read. Override -> env -> default.

    One read for all four, not one per rate: the overrides live in a single JSON
    blob, so four reads would be four identical queries. A corrupt or unreadable
    override falls back to the env value rather than to zero — a broken config
    row must not silently switch the economy off.
    """
    overrides = _read_overrides(supabase) if supabase is not None else {}
    resolved = {}
    for name in RATE_NAMES:
        override = overrides.get(name)
        resolved[name] = (override if override is not None
                          else _clamp(name, _env_rate(name)))
    return resolved


def effective_rates(supabase=None, *, use_cache: bool = True) -> dict:
    """All four rates in force, as Decimals. Cached for ``_RATES_CACHE_TTL``.

    The cache is what keeps this off the hot write path: it is read on every
    reaction and comment, and an uncached read is a query. It is keyed on
    nothing because there is one rate config for the whole app.

    ``use_cache=False`` forces a read. The admin GET path uses it so an operator
    always sees the stored value, not one this worker cached up to a TTL ago.
    """
    global _rates_cache
    if use_cache:
        cached, expires_at = _rates_cache
        if cached is not None and time.time() < expires_at:
            return dict(cached)
    supabase = supabase or _get_supabase()
    rates = _resolve_rates(supabase)
    if use_cache:
        _rates_cache = (dict(rates), time.time() + _RATES_CACHE_TTL)
    return rates


def invalidate_rates_cache() -> None:
    """Drop this process's cached rates. Call after any write."""
    global _rates_cache
    _rates_cache = (None, 0.0)


# ── Eligibility: only a face-verified wallet may earn ────────────────────────

# The identity read is an RPC call, and this is on the hot path (every reaction
# and comment). Cached per wallet for a short TTL: a verified verdict is stable,
# and a 5-minute window is short enough that someone who verifies mid-session
# starts earning without a reload. Per process, like the rates cache.
_ELIGIBILITY_TTL = get_env_int("COMMUNITY_REWARD_ELIGIBILITY_TTL", 300)
_eligibility_cache: dict = {}


def _identity_verified(wallet: str) -> bool | None:
    """On-chain face-verification, or None when the chain could not be read.

    ``None`` is a distinct answer from ``False``: "we could not ask" must not be
    treated as "the chain said no", because that would silently switch off
    earning for every verified user during an RPC outage. The caller decides.
    """
    try:
        from blockchain import is_identity_verified

        result = is_identity_verified(wallet) or {}
        if result.get("verified") is True:
            return True
        if result.get("error"):
            return None
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: identity check failed for %s: %s",
                       str(wallet)[:10], exc)
        return None


def is_eligible(wallet: str) -> bool:
    """May this wallet earn? Fails CLOSED: unverifiable means not eligible.

    Only face-verified wallets earn. An unverified wallet can still use the
    feed — this gates the credit, not the action.
    """
    if not wallet:
        return False
    if not REQUIRE_VERIFIED:
        return True

    key = str(wallet).lower()
    now = time.time()
    cached = _eligibility_cache.get(key)
    if cached and now < cached[1]:
        return cached[0]

    verdict = _identity_verified(wallet)
    eligible = verdict is True
    # Cache the answer — including a False — so a farm cannot turn every single
    # reaction into an RPC call. A None (transport error) is NOT cached: the
    # next attempt should try the chain again rather than inherit an outage.
    if verdict is not None:
        _eligibility_cache[key] = (eligible, now + _ELIGIBILITY_TTL)
    return eligible


def invalidate_eligibility_cache() -> None:
    """Drop the cached verification verdicts (used by tests and admin paths)."""
    _eligibility_cache.clear()


def set_rates(values: dict, *, actor: str, reason: str,
              supabase=None) -> dict:
    """Persist admin rate overrides. Returns ``{"success": bool, ...}``.

    Refuses without an ``actor`` and a ``reason``: an economy change that cannot
    be attributed is an economy change nobody can audit, and this is the only
    admin write path in the rewards system. Unknown rate names are refused
    outright rather than ignored, so a typo cannot look like success.

    Only the names present in ``values`` are written; the rest keep their
    current value.
    """
    if not actor or not actor.strip():
        return {"success": False, "error": "An actor is required"}
    if not reason or not reason.strip():
        return {"success": False, "error": "A reason is required"}
    if not isinstance(values, dict) or not values:
        return {"success": False, "error": "No rates supplied"}

    unknown = sorted(set(values) - set(RATE_NAMES))
    if unknown:
        return {"success": False,
                "error": "Unknown rate(s): " + ", ".join(unknown)}

    supabase = supabase or _get_supabase()
    if not supabase:
        return {"success": False, "error": "Database unavailable"}

    # Merge onto the current EFFECTIVE values, not onto the overrides: an admin
    # editing one rate in the UI must not silently drop the other three.
    merged = {name: str(rate) for name, rate in effective_rates(supabase).items()}
    clamped = {}
    for name, raw in values.items():
        parsed = _parse_rate(name, raw)
        if parsed is None:
            return {"success": False, "error": f"Invalid value for {name}"}
        merged[name] = str(parsed)
        clamped[name] = str(parsed)

    # The actor and reason ride inside the blob so the row is self-describing
    # even when read straight from the table, with no join.
    blob = json.dumps(merged, sort_keys=True)
    audit = {"feature_name": RATES_FEATURE, "custom_message": blob,
             "is_maintenance": False}
    try:
        existing = (
            supabase.table("maintenance_settings")
            .select("id")
            .eq("feature_name", RATES_FEATURE)
            .limit(1)
            .execute()
        )
        if _rows(existing):
            supabase.table("maintenance_settings").update(audit).eq(
                "feature_name", RATES_FEATURE).execute()
        else:
            supabase.table("maintenance_settings").insert(audit).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("community_rewards: rate write failed: %s", exc)
        return {"success": False, "error": "Could not save the rates"}

    # Invalidate AFTER the write, and build the response from what was actually
    # written rather than re-reading. A re-read here would repopulate the cache
    # itself and make the invalidation above redundant — which is exactly the
    # bug this ordering avoids: the next reader must hit the database.
    invalidate_rates_cache()
    logger.info("community_rewards: rates updated by %s (%s): %s",
                actor[:10], reason.strip()[:120], clamped)
    return {"success": True, "rates": {k: float(v) for k, v in merged.items()},
            "changed": clamped, "actor": actor, "reason": reason.strip()}


# ── Ledger reads ─────────────────────────────────────────────────────────────

def _balance_read(wallet: str, supabase) -> tuple[Decimal, bool]:
    """Return the ledger sum and whether every page was read successfully."""
    if not wallet:
        return Decimal("0"), True
    if not supabase:
        return Decimal("0"), False
    total = Decimal("0")
    offset = 0
    page = 1000
    while True:
        try:
            result = (
                supabase.table(LEDGER)
                .select("delta_gd")
                .ilike("wallet_address", wallet)
                .order("id")
                .range(offset, offset + page - 1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_rewards: balance read failed: %s", exc)
            return total, False
        rows = _rows(result)
        for row in rows:
            total += _as_decimal(row.get("delta_gd"))
        if len(rows) < page:
            return total, True
        offset += page


def balance(wallet: str, *, _supabase=None) -> Decimal:
    """The wallet's earned balance, summed from the append-only ledger.

    Summed here rather than read from the ``community_reward_balances`` view so
    the value is authoritative and testable without the view existing. The view
    computes exactly this and exists for SQL/admin reporting. This compatibility
    helper retains its Decimal return even when a read is incomplete; the card
    uses the accompanying availability flag from ``summary`` to fail safely.
    """
    if not wallet:
        return Decimal("0")
    supabase = _supabase or _get_supabase()
    return _balance_read(wallet, supabase)[0]


def _credits_since(supabase, wallet: str, since: datetime,
                   event_types: tuple | None = None,
                   fail_closed_to: Decimal | None = None) -> Decimal:
    """Positive credits for one wallet since ``since`` (for a daily cap).

    ``event_types`` narrows the sum to a budget's own event types. Daily Learn
    and the reaction/comment budget are summed over DISJOINT sets, so one can
    never consume the other's allowance. ``None`` means "all event types", which
    only the fail-closed path uses.

    ``fail_closed_to`` is the value returned when the ledger cannot be read, so
    an unreadable cap must not become "no cap". It defaults to ``DAILY_CAP`` for
    callers using the shared budget; a caller measuring against a different
    budget must pass that budget, otherwise the returned number has no relation
    to the ceiling it is compared against.
    """
    try:
        query = (
            supabase.table(LEDGER)
            .select("delta_gd,created_at,event_type")
            .ilike("wallet_address", wallet)
            .gte("created_at", since.isoformat())
        )
        if event_types:
            query = query.in_("event_type", list(event_types))
        result = query.execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: daily cap read failed: %s", exc)
        return DAILY_CAP if fail_closed_to is None else fail_closed_to
    total = Decimal("0")
    for row in _rows(result):
        delta = _as_decimal(row.get("delta_gd"))
        if delta > 0:
            total += delta
    return total


def _credits_for_post(supabase, post_id) -> Decimal:
    """Positive credits tied to one post (for the per-post cap)."""
    if post_id in (None, ""):
        return Decimal("0")
    try:
        result = (
            supabase.table(LEDGER)
            .select("delta_gd")
            .eq("post_id", int(post_id))
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: post cap read failed: %s", exc)
        return POST_CAP
    total = Decimal("0")
    for row in _rows(result):
        delta = _as_decimal(row.get("delta_gd"))
        if delta > 0:
            total += delta
    return total


def _find_by_key(supabase, key: str) -> dict | None:
    try:
        result = (
            supabase.table(LEDGER)
            .select("id,wallet_address,delta_gd,event_type")
            .eq("idempotency_key", key)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: ledger lookup failed: %s", exc)
        return None
    rows = _rows(result)
    return rows[0] if rows else None


# ── Ledger writes ────────────────────────────────────────────────────────────

def _insert(supabase, row: dict) -> bool:
    """Append one ledger row. False when it was already there (benign)."""
    # The timestamp is written explicitly rather than left to the DB default so
    # the daily-cap window is computed from the same clock the row carries.
    row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    try:
        supabase.table(LEDGER).insert(row).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        # A unique-violation on idempotency_key means the event was already
        # credited (or a concurrent request won the race). That is the
        # invariant working, not an error.
        #
        # Any OTHER failure here — most commonly a ledger CHECK constraint that
        # was never widened for this event type — silently voids a real reward.
        # WARNING (not DEBUG) so it is visible in normal production logs: a
        # missing migration must not look like "the user earned nothing".
        logger.warning("community_rewards: ledger insert skipped (%s, %s): %s",
                       row.get("event_type"), row.get("idempotency_key"), exc)
        return False


def _credit(supabase, wallet: str, *, key: str, event_type: str, amount: Decimal,
            post_id=None, comment_id=None, event_created_at=None,
            source_question_id=None) -> bool:
    """Credit ``amount`` G$, honouring the fences and the caps. Never raises.

    ``amount`` is explicit rather than read from a module global so the caller
    decides the rate: one comment pays the commenter and the post owner at two
    different rates, from two separate ledger rows.

    Returns True when a row was written.
    """
    if not wallet:
        return False
    if amount is None or amount <= 0:
        return False
    if not is_rewardable(event_created_at):
        return False

    # Only a face-verified wallet earns. This sits at the single credit choke
    # point, so it covers BOTH sides of an interaction at once: an unverified
    # actor earns nothing, AND an unverified post owner earns nothing when a
    # verified user reacts to their post. Gating at the route would have covered
    # only the actor and quietly paid unverified owners.
    if not is_eligible(wallet):
        logger.info("community_rewards: %s is not verified; no credit (%s)",
                    wallet[:10], event_type)
        return False

    # Check first, then insert. The unique index on idempotency_key is the
    # authoritative guard (it also wins a concurrent race), but checking here
    # makes the "already credited" case a clean no-op rather than a caught
    # exception, which is what keeps un-react/re-react at net zero.
    if _find_by_key(supabase, key):
        return False

    now = datetime.now(timezone.utc)
    # Which daily budget applies. Daily Learn is budgeted by its OWN allowance,
    # over its own UTC-day window, so a completed cycle always credits and can
    # never consume (or be blocked by) the shared reaction/comment budget.
    if event_type == DAILY_LEARN_EVENT:
        daily_cap, window_start, cap_event_types = daily_learn_budget()
    else:
        daily_cap, window_start = DAILY_CAP, now - timedelta(hours=24)
        cap_event_types = SHARED_CAP_EVENT_TYPES
    if _credits_since(supabase, wallet, window_start, cap_event_types,
                      fail_closed_to=daily_cap) + amount > daily_cap:
        logger.info("community_rewards: daily cap reached for %s (%s)",
                    wallet[:10], event_type)
        return False
    if post_id not in (None, "") and \
            _credits_for_post(supabase, post_id) + amount > POST_CAP:
        logger.info("community_rewards: post cap reached for post %s", post_id)
        return False

    row = {
        "wallet_address": wallet.lower(),
        "delta_gd": str(amount),
        "event_type": event_type,
        "idempotency_key": key,
        "post_id": int(post_id) if post_id not in (None, "") else None,
        "comment_id": int(comment_id) if comment_id not in (None, "") else None,
    }
    if source_question_id is not None:
        row["source_question_id"] = source_question_id
    return _insert(supabase, row)


def _daily_learn_attempt(db, wallet: str, day_utc: str) -> dict | None:
    """Read the persisted completion; never trust a client-supplied score."""
    try:
        rows = _rows(db.table("community_daily_learn_attempts")
                     .select("status,score,reward_rate_gd,question_1_id,correct_1,"
                             "question_2_id,correct_2")
                     .ilike("wallet_address", wallet)
                     .eq("day_utc", day_utc)
                     .limit(1).execute())
        if rows and rows[0].get("status") == "completed":
            return rows[0]
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: Daily Learn attempt read failed: %s", exc)
        return None


def _configured_daily_learn_rate(db) -> Decimal | None:
    """Read the Admin Quiz Settings rate; a DB failure must not mint a fallback."""
    try:
        rows = _rows(db.table("quiz_settings")
                     .select("community_daily_learn_reward_per_correct")
                     .order("id").limit(1).execute())
        rate = _as_decimal(
            rows[0].get("community_daily_learn_reward_per_correct") if rows else 1)
        if not rate.is_finite() or rate < 0 or rate > daily_learn_rate_max():
            logger.error("community_rewards: invalid Community Daily Learn rate: %s", rate)
            return None
        return rate
    except Exception as exc:  # noqa: BLE001 - configuration read fails closed
        logger.warning("community_rewards: Daily Learn rate read failed: %s", exc)
        return None


def _daily_learn_ledger_row_matches(row: dict, wallet: str, amount: Decimal,
                                   question_id: str) -> bool:
    """Validate the full identity and amount before calling a question paid."""
    try:
        delta = _as_decimal(row.get("delta_gd"))
    except Exception:  # noqa: BLE001 - malformed ledger data is not success
        return False
    return (
        _same_wallet(row.get("wallet_address"), wallet)
        and row.get("event_type") == DAILY_LEARN_EVENT
        and delta == amount
        and str(row.get("source_question_id") or "") == question_id
    )


def _daily_learn_ledger_row(db, key: str) -> dict | None:
    rows = _rows(db.table(LEDGER).select(
        "wallet_address,delta_gd,event_type,idempotency_key,source_question_id"
    ).eq("idempotency_key", key).limit(1).execute())
    return rows[0] if rows else None


def daily_learn_completion_status(wallet: str, day_utc: str, *, _supabase=None,
                                  _attempt: dict | None = None) -> dict:
    """Credit each correct question from a persisted completion exactly once.

    Question-level idempotency keys are shared by completion, page-load repair,
    and the background reconciler. A legacy cycle-level row is recognized as
    paid to avoid retroactively paying existing completions a second time.
    """
    try:
        identity = (wallet or "").strip().lower()
        parsed_day = datetime.fromisoformat(str(day_utc)).date().isoformat()
        if not identity or parsed_day != str(day_utc):
            return {"status": "not_credited", "reason": "invalid_completion"}
        db = _supabase or _get_supabase()
        if db is None:
            return {"status": "not_credited", "reason": "temporarily_unavailable"}
        if not is_enabled(_supabase=db):
            return {"status": "not_credited", "reason": "rewards_disabled"}

        attempt = _attempt or _daily_learn_attempt(db, identity, parsed_day)
        if not attempt:
            return {"status": "not_credited", "reason": "completion_not_found"}
        try:
            score = int(attempt.get("score") or 0)
        except (TypeError, ValueError):
            return {"status": "not_credited", "reason": "temporarily_unavailable"}
        if score <= 0:
            return {"status": "not_credited", "reason": "no_correct_answers"}
        if score > 2:
            logger.error("community_rewards: invalid Daily Learn score %s for %s", score, identity[:10])
            return {"status": "not_credited", "reason": "temporarily_unavailable"}

        correct_questions = []
        for suffix in ("1", "2"):
            if attempt.get(f"correct_{suffix}") is True:
                question_id = str(attempt.get(f"question_{suffix}_id") or "").strip()
                if not question_id:
                    return {"status": "not_credited", "reason": "temporarily_unavailable"}
                correct_questions.append(question_id)
        if len(correct_questions) != score:
            logger.error("community_rewards: Daily Learn score/correctness mismatch for %s",
                         identity[:10])
            return {"status": "not_credited", "reason": "temporarily_unavailable"}
        if not correct_questions:
            return {"status": "not_credited", "reason": "no_correct_answers"}

        # Preserve older one-row-per-cycle credits. Historical attempt rows may
        # have a NULL rate snapshot, so validate the immutable key, wallet, type,
        # and positive amount rather than comparing with today's admin rate.
        legacy_key = f"daily_learn:{parsed_day}:{identity}"
        try:
            legacy = _daily_learn_ledger_row(db, legacy_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_rewards: legacy Daily Learn lookup failed: %s", exc)
            return {"status": "not_credited", "reason": "temporarily_unavailable"}
        if legacy:
            if (_same_wallet(legacy.get("wallet_address"), identity)
                    and legacy.get("event_type") == DAILY_LEARN_EVENT
                    and _as_decimal(legacy.get("delta_gd")) > 0
                    and not legacy.get("source_question_id")):
                return {"status": "already_credited",
                        "credited_questions": len(correct_questions),
                        "credited_gd": float(_as_decimal(legacy.get("delta_gd")))}
            logger.error("community_rewards: invalid legacy Daily Learn ledger row for %s",
                         identity[:10])
            return {"status": "not_credited", "reason": "ledger_integrity_error"}

        raw_rate = attempt.get("reward_rate_gd")
        rate = (_as_decimal(raw_rate) if raw_rate is not None
                else _configured_daily_learn_rate(db))
        if (rate is None or not rate.is_finite()
                or rate < 0 or rate > daily_learn_rate_max()):
            # Name the reason: a rate above the ceiling is an operator
            # misconfiguration, not an outage. Without this line the member sees
            # a completed 2/2 cycle credit nothing and the log says only
            # "temporarily_unavailable", which is the silent-failure shape this
            # change exists to remove.
            logger.error(
                "community_rewards: Daily Learn rate %s for %s is outside 0..%s; "
                "credit skipped. Raise COMMUNITY_DAILY_LEARN_RATE_MAX (database "
                "bound %s) or lower the Admin Quiz Settings rate.",
                raw_rate if raw_rate is not None else "(unreadable)",
                identity[:10], daily_learn_rate_max(), DAILY_LEARN_RATE_DB_BOUND)
            return {"status": "not_credited", "reason": "rate_out_of_range"}
        if rate == 0:
            return {"status": "not_credited", "reason": "reward_disabled"}

        question_keys = {
            question_id: f"daily_learn:{parsed_day}:{identity}:{question_id}"
            for question_id in correct_questions
        }
        existing_rows = {}
        try:
            for question_id, key in question_keys.items():
                row = _daily_learn_ledger_row(db, key)
                if row:
                    if not _daily_learn_ledger_row_matches(row, identity, rate, question_id):
                        logger.error("community_rewards: invalid Daily Learn row for %s (%s)",
                                     identity[:10], question_id)
                        return {"status": "not_credited", "reason": "ledger_integrity_error"}
                    existing_rows[question_id] = row
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_rewards: question credit lookup failed: %s", exc)
            return {"status": "not_credited", "reason": "temporarily_unavailable"}

        missing_questions = [q for q in correct_questions if q not in existing_rows]
        if not missing_questions:
            return {"status": "already_credited",
                    "credited_questions": len(correct_questions),
                    "credited_gd": float(rate * len(correct_questions))}
        if not is_eligible(identity):
            return {"status": "not_credited", "reason": "verified_wallet_required"}
        if not is_rewardable(datetime.now(timezone.utc)):
            return {"status": "not_credited", "reason": "reward_unavailable"}

        # Check the Daily Learn allowance separately so the user can distinguish a
        # cap from an unreadable ledger. _credit repeats the check at the
        # insertion boundary. The window is the UTC day (not a rolling 24h), so
        # the budget resets exactly when the quiz does; only `daily_learn` rows
        # count, so reactions and comments can neither block the cycle nor be
        # blocked by it.
        daily_cap, window_start, cap_event_types = daily_learn_budget()
        try:
            cap_rows = _rows(db.table(LEDGER).select(
                "delta_gd,created_at,event_type").ilike(
                    "wallet_address", identity).eq(
                        "event_type", DAILY_LEARN_EVENT).gte(
                    "created_at", window_start.isoformat()
                ).execute())
        except Exception as exc:  # noqa: BLE001
            logger.warning("community_rewards: Daily Learn cap read failed: %s", exc)
            return {"status": "not_credited", "reason": "temporarily_unavailable"}
        recent_credits = sum(
            (_as_decimal(row.get("delta_gd")) for row in cap_rows
             if _as_decimal(row.get("delta_gd")) > 0), Decimal("0"))
        missing_amount = rate * len(missing_questions)
        if recent_credits + missing_amount > daily_cap:
            return {"status": "not_credited", "reason": "daily_cap_reached"}

        newly_credited = 0
        for question_id in missing_questions:
            key = question_keys[question_id]
            inserted = _credit(
                db, identity, key=key, event_type=DAILY_LEARN_EVENT,
                amount=rate, event_created_at=datetime.now(timezone.utc),
                source_question_id=question_id)
            if inserted:
                newly_credited += 1
                continue
            # An idempotent race is success only after validating the winning row.
            try:
                existing = _daily_learn_ledger_row(db, key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("community_rewards: Daily Learn confirmation failed: %s", exc)
                return {"status": "pending", "reason": "temporarily_unavailable",
                        "credited_questions": len(existing_rows) + newly_credited}
            if existing and _daily_learn_ledger_row_matches(
                    existing, identity, rate, question_id):
                existing_rows[question_id] = existing
                continue
            if existing:
                return {"status": "not_credited", "reason": "ledger_integrity_error"}
            reason = ("verified_wallet_required" if not is_eligible(identity)
                      else "temporarily_unavailable")
            return {"status": "pending", "reason": reason,
                    "credited_questions": len(existing_rows) + newly_credited}

        return {"status": "credited" if newly_credited else "already_credited",
                "credited_questions": len(correct_questions),
                "credited_gd": float(rate * len(correct_questions))}
    except Exception as exc:  # noqa: BLE001 - rewards must never break learning
        logger.warning("community_rewards: Daily Learn credit skipped: %s", exc)
        return {"status": "not_credited", "reason": "temporarily_unavailable"}


def on_daily_learn_completed(wallet: str, day_utc: str, *, _supabase=None) -> bool:
    """Backward-compatible boolean hook for a persisted 2/2 completion."""
    return daily_learn_completion_status(wallet, day_utc, _supabase=_supabase).get(
        "status") == "credited"


def reconcile_uncredited_daily_learn_attempts(*, _supabase=None,
                                              batch_size: int = 100) -> dict:
    """Retry a bounded batch of saved completions that have no ledger credit."""
    db = _supabase or _get_supabase()
    if db is None:
        return {"claimed": 0, "credited": 0, "already_credited": 0,
                "not_credited": 0, "error": "database_unavailable"}
    if not is_enabled(_supabase=db):
        return {"claimed": 0, "credited": 0, "already_credited": 0,
                "not_credited": 0, "skipped": "rewards_disabled"}
    try:
        result = db.rpc(DAILY_LEARN_CLAIM_RPC, {
            "p_limit": max(1, min(int(batch_size), 500)),
        }).execute()
        attempts = _rows(result)
    except Exception as exc:  # noqa: BLE001 - reconciler retries next interval
        logger.warning("community_rewards: Daily Learn reconciliation claim failed: %s", exc)
        return {"claimed": 0, "credited": 0, "already_credited": 0,
                "not_credited": 0, "error": "claim_failed"}

    summary = {"claimed": len(attempts), "credited": 0,
               "already_credited": 0, "not_credited": 0}
    for attempt in attempts:
        wallet = str(attempt.get("attempt_wallet_address") or "").strip().lower()
        day_utc = str(attempt.get("attempt_day_utc") or "")
        internal_attempt = {
            "status": "completed",
            "score": attempt.get("attempt_score"),
            "reward_rate_gd": attempt.get("attempt_reward_rate_gd"),
            "question_1_id": attempt.get("attempt_question_1_id"),
            "correct_1": attempt.get("attempt_correct_1"),
            "question_2_id": attempt.get("attempt_question_2_id"),
            "correct_2": attempt.get("attempt_correct_2"),
        }
        outcome = daily_learn_completion_status(
            wallet, day_utc, _supabase=db, _attempt=internal_attempt)
        status = outcome.get("status")
        if status in summary:
            summary[status] += 1
        else:
            summary["not_credited"] += 1
    if summary["claimed"]:
        logger.info(
            "community_rewards: Daily Learn reconciliation claimed=%s credited=%s "
            "already=%s pending=%s",
            summary["claimed"], summary["credited"], summary["already_credited"],
            summary["not_credited"],
        )
    return summary


def _reverse(supabase, original: dict, *, actor_wallet: str) -> bool:
    """Write the compensating negative row for a credit, at most once.

    The reversal is a NEW row (the ledger rejects edits); ``reverses_ledger_id``
    carries a unique index, so replaying the same undo is a no-op.
    """
    original_id = original.get("id")
    if original_id in (None, ""):
        return False
    delta = _as_decimal(original.get("delta_gd"))
    if delta == 0:
        return False
    # A credit is reversed at most once. The unique partial index on
    # reverses_ledger_id is the authoritative guard; this lookup makes the
    # replayed undo a clean no-op (an un-react is often dispatched twice by a
    # double-tap, and the second one must not subtract twice).
    try:
        existing = (
            supabase.table(LEDGER)
            .select("id")
            .eq("reverses_ledger_id", int(original_id))
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: reversal lookup failed: %s", exc)
        return False
    if _rows(existing):
        return False
    return _insert(supabase, {
        "wallet_address": str(original.get("wallet_address") or actor_wallet).lower(),
        "delta_gd": str(-delta),
        "event_type": original.get("event_type") or "admin_adjust",
        "idempotency_key": f"undo:{original_id}",
        "reverses_ledger_id": int(original_id),
        "reason": "reversed: source event removed",
        "actor": actor_wallet.lower(),
    })


# ── Public event hooks (called by community_feed/service.py) ─────────────────
# Each one is best-effort: a rewards problem must never break the user's
# reaction or comment, so every hook swallows its own errors.

def on_reaction_added(wallet: str, post_id, post_owner: str | None, *,
                      event_created_at=None, _supabase=None) -> None:
    """Credit the reactor and the post owner. Self-activity earns nothing."""
    try:
        if not wallet or not is_enabled(_supabase=_supabase):
            return
        supabase = _supabase or _get_supabase()
        if not supabase:
            return
        # Reacting to your own post earns nothing on either side.
        if _same_wallet(wallet, post_owner):
            return
        # Resolve the rates ONCE for this event, so both sides are priced from
        # the same read: an admin changing a rate mid-request must not produce a
        # ledger row from one rate and a card from another.
        rates = effective_rates(supabase)
        # The reactor earns the smallest rate: tapping is the cheapest action
        # and the one with nothing left behind to audit.
        _credit(supabase, wallet, amount=rates["reaction_in"],
                key=f"reaction_in:{post_id}:{wallet.lower()}",
                event_type="reaction_in", post_id=post_id,
                event_created_at=event_created_at)
        if post_owner:
            # Keyed by the REACTOR so each distinct reactor credits the owner
            # once, and so the reversal can find exactly this row.
            _credit(supabase, post_owner, amount=rates["reaction_out"],
                    key=f"reaction_out:{post_id}:{wallet.lower()}",
                    event_type="reaction_out", post_id=post_id,
                    event_created_at=event_created_at)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: reaction credit failed: %s", exc)


def on_reaction_removed(wallet: str, post_id, post_owner: str | None, *,
                        _supabase=None) -> None:
    """Reverse both sides of a reaction. Replayed undo is a no-op."""
    try:
        if not wallet or not is_enabled(_supabase=_supabase):
            return
        supabase = _supabase or _get_supabase()
        if not supabase:
            return
        if _same_wallet(wallet, post_owner):
            return
        for owner, key in ((wallet, f"reaction_in:{post_id}:{wallet.lower()}"),
                           (post_owner, f"reaction_out:{post_id}:{wallet.lower()}")):
            if not owner:
                continue
            original = _find_by_key(supabase, key)
            if original:
                _reverse(supabase, original, actor_wallet=wallet)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: reaction reversal failed: %s", exc)


def on_comment_added(wallet: str, comment_id, post_id, post_owner: str | None, *,
                     event_created_at=None, _supabase=None) -> None:
    """Credit the commenter AND the post owner, at their own rates.

    Both sides earn, and they earn *different* amounts: the commenter did the
    work (``comment_in``) and the owner earned the attention (``comment_out``,
    the largest rate in the system). They are two
    separate ledger rows keyed on the same comment, so each reverses on its own.

    Self-activity earns nothing: commenting on your own post credits nobody.
    """
    try:
        if not wallet or not is_enabled(_supabase=_supabase):
            return
        supabase = _supabase or _get_supabase()
        if not supabase:
            return
        if _same_wallet(wallet, post_owner):
            return
        rates = effective_rates(supabase)
        _credit(supabase, wallet, amount=rates["comment_in"],
                key=f"comment_in:{comment_id}",
                event_type="comment_in", post_id=post_id, comment_id=comment_id,
                event_created_at=event_created_at)
        if post_owner:
            _credit(supabase, post_owner, amount=rates["comment_out"],
                    key=f"comment_out:{comment_id}",
                    event_type="comment_out", post_id=post_id,
                    comment_id=comment_id, event_created_at=event_created_at)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: comment credit failed: %s", exc)


def on_comment_removed(wallet: str, comment_id, post_owner: str | None = None, *,
                       _supabase=None) -> None:
    """Reverse both sides of a comment. A replayed undo is a no-op.

    Each side reverses its own row, so a missing owner credit (self-activity,
    or an owner whose own credit was refused by a cap) does not block the
    commenter's reversal.
    """
    try:
        if not wallet or not is_enabled(_supabase=_supabase):
            return
        supabase = _supabase or _get_supabase()
        if not supabase:
            return
        for owner, key in ((wallet, f"comment_in:{comment_id}"),
                           (post_owner, f"comment_out:{comment_id}")):
            if not owner:
                continue
            original = _find_by_key(supabase, key)
            if original:
                _reverse(supabase, original, actor_wallet=wallet)
    except Exception as exc:  # noqa: BLE001
        logger.warning("community_rewards: comment reversal failed: %s", exc)


# ── Display ──────────────────────────────────────────────────────────────────

def _rate_payload(_supabase=None) -> dict:
    """Return the reward rates in force as plain floats for the card.

    Read from the same resolver the ledger uses, so the page can never quote a
    rate the ledger does not actually pay — including an admin override.
    """
    payload = {name: float(rate) for name, rate in effective_rates().items()}
    db = _supabase or _get_supabase()
    daily_rate = _configured_daily_learn_rate(db) if db else None
    payload["daily_learn_per_correct"] = float(daily_rate or Decimal("0"))
    # The live ceiling, so an admin surface can render its bound from ONE source
    # instead of pinning the number in the markup.
    payload["daily_learn_rate_max"] = float(daily_learn_rate_max())
    return payload


def summary(wallet: str | None, *, _supabase=None) -> dict:
    """The card payload for one viewer. Never raises, never leaks a wallet."""
    enabled = is_enabled(_supabase=_supabase)
    if not wallet:
        return {
            "success": True,
            "enabled": enabled,
            "is_guest": True,
            "balance_gd": 0.0,
            "min_payout_gd": float(MIN_PAYOUT_GD),
            "progress": 0.0,
            "status": "guest",
            "can_withdraw": False,
            "eligible": False,
            "balance_available": True,
            "rates": _rate_payload(_supabase),
            "daily_cap_gd": float(DAILY_CAP),
            "daily_learn_cap_gd": float(daily_learn_daily_cap()),
            "daily_learn_rate_max": float(daily_learn_rate_max()),
            "show_balance": show_balance(),
        }
    supabase = _supabase or _get_supabase()
    current, balance_available = _balance_read(wallet, supabase)
    minimum = MIN_PAYOUT_GD
    progress = 0.0
    if minimum > 0:
        progress = float(min(current, minimum) / minimum * 100)
    # Read the rates ONCE and reuse them, so `rates` and the headline figure
    # cannot come from two different reads of a value an admin can change.
    rates = _rate_payload(supabase)
    # An unverified viewer sees a card that says WHY it is zero. Without this the
    # card would show 0 and stay silent, which reads as "rewards are broken"
    # rather than "you have not verified yet".
    eligible = is_eligible(wallet)
    if eligible:
        status = "ready" if current >= minimum else "pending"
    else:
        status = "unverified"
    return {
        "success": True,
        "enabled": enabled,
        "is_guest": False,
        "balance_gd": float(current),
        "min_payout_gd": float(minimum),
        "progress": round(progress, 1),
        "status": status,
        "eligible": eligible,
        "balance_available": balance_available,
        # A ledger summary alone cannot establish payout readiness; the
        # dedicated withdrawal endpoint also verifies current identity, vault
        # immutables, pause state, and on-chain limits before issuing a signature.
        "can_withdraw": False,
        # The full breakdown, so the card can state the rates instead of a
        # single misleading average. Rates are per event type, not per action.
        "rates": rates,
        "reward_per_action": float(max(rates.values())) if rates else 0.0,
        "daily_cap_gd": float(DAILY_CAP),
        # Daily Learn is budgeted separately, so the card can explain both caps
        # instead of letting a silent skip look like "the quiz paid nothing".
        "daily_learn_cap_gd": float(daily_learn_daily_cap()),
        # The live per-answer ceiling, so no surface has to pin the number.
        "daily_learn_rate_max": float(daily_learn_rate_max()),
        # Whether the card may show the balance block. Defaults on for members;
        # the client still withholds this personal figure from guests.
        "show_balance": show_balance(),
    }
