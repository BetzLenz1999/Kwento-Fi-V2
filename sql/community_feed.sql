-- GoodMarket Community Feed — Facebook/Instagram-style posts with media.
--
-- A chronological feed of user posts (caption + images/video), with
-- Messenger-style reactions. Supabase stores METADATA ONLY: the bytes live in
-- Cloudflare R2 (see storage/r2.py) and are referenced by r2_key.
--
-- HIDDEN BY DEFAULT: the feature-visibility row at the bottom is seeded with
-- is_maintenance = TRUE, so /community and every /community/api/* call report
-- "not available" until an admin flips it ON (Feature Visibility → Community
-- Feed). The gate is enforced server-side, not by hiding the UI.
--
-- Run this in the Supabase SQL editor BEFORE enabling the feature.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Posts ────────────────────────────────────────────────────────────────────
-- wallet_address is the OWNER (kept private — never sent to the browser).
-- username is a snapshot taken at post time so rendering never needs a join.
-- body is the CAPTION: optional when the post carries media, required when it
-- does not (enforced in the service, not here, so the two rules stay testable).
CREATE TABLE IF NOT EXISTS community_feed_posts (
    id             BIGSERIAL PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    username       TEXT,
    body           TEXT,
    visibility     TEXT NOT NULL DEFAULT 'public',
    -- Denormalised so the feed is one query instead of an N+1 aggregate.
    -- Adjusted on the same path as the reaction write; see recount helper.
    reaction_count INTEGER NOT NULL DEFAULT 0,
    comment_count  INTEGER NOT NULL DEFAULT 0,
    report_count   INTEGER NOT NULL DEFAULT 0,
    is_hidden      BOOLEAN NOT NULL DEFAULT FALSE,
    is_deleted     BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_by     TEXT,
    deleted_at     TIMESTAMPTZ,
    edited_at      TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Feed pagination: keyset, newest-first, over visible posts only.
CREATE INDEX IF NOT EXISTS idx_community_feed_posts_feed
    ON community_feed_posts(id DESC) WHERE NOT is_deleted AND NOT is_hidden;
-- "My posts" / author-scoped lookups.
CREATE INDEX IF NOT EXISTS idx_community_feed_posts_wallet
    ON community_feed_posts(wallet_address, id DESC);

-- ── Media ────────────────────────────────────────────────────────────────────
-- One row per image/video. r2_key is the source of truth; public_url is stored
-- so the feed needs no signing at render time (the bucket is public-read).
-- There is deliberately NO caption column here: a post has ONE caption, and
-- per-file captions are how carousels become confusing (see the proposal).
CREATE TABLE IF NOT EXISTS community_feed_media (
    id           BIGSERIAL PRIMARY KEY,
    post_id      BIGINT NOT NULL REFERENCES community_feed_posts(id) ON DELETE CASCADE,
    r2_key       TEXT NOT NULL,
    public_url   TEXT NOT NULL,
    media_type   TEXT NOT NULL CHECK (media_type IN ('image', 'video')),
    mime         TEXT,
    bytes        BIGINT,
    width        INTEGER,
    height       INTEGER,
    -- Client-declared. There is no ffprobe in this environment, so this is a UX
    -- guard, NOT a server-verified value; the byte cap is what bounds cost.
    duration_sec NUMERIC(6, 2),
    poster_url   TEXT,
    position     SMALLINT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_community_feed_media_post
    ON community_feed_media(post_id, position);

-- ── Reactions ────────────────────────────────────────────────────────────────
-- Messenger model: ONE reaction per wallet per post, switchable. The composite
-- primary key makes a second reaction physically impossible, so switching is a
-- single UPDATE of `reaction` and a double-tap can never inflate the count.
--
-- This is deliberately NOT Facebook's model (one row per type, six counters):
-- six denormalised counters have to be kept consistent on every write and race
-- when a user taps two reactions at once, and "12 👍 3 ❤️ 1 😮" wraps on a
-- 360px card. See docs/COMMUNITY_FEED_R2_PROPOSAL.md §4.1.
CREATE TABLE IF NOT EXISTS community_feed_reactions (
    post_id        BIGINT NOT NULL REFERENCES community_feed_posts(id) ON DELETE CASCADE,
    wallet_address TEXT NOT NULL,
    reaction       TEXT NOT NULL
        CHECK (reaction IN ('like', 'love', 'haha', 'wow', 'sad', 'angry')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, wallet_address)
);

-- Batched "which of these posts did I react to" lookup for a feed page.
CREATE INDEX IF NOT EXISTS idx_community_feed_reactions_post
    ON community_feed_reactions(post_id, reaction);
CREATE INDEX IF NOT EXISTS idx_community_feed_reactions_wallet
    ON community_feed_reactions(wallet_address, post_id);

-- ── Comments ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS community_feed_comments (
    id             BIGSERIAL PRIMARY KEY,
    post_id        BIGINT NOT NULL REFERENCES community_feed_posts(id) ON DELETE CASCADE,
    wallet_address TEXT NOT NULL,
    username       TEXT,
    body           TEXT NOT NULL,
    is_deleted     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_community_feed_comments_post
    ON community_feed_comments(post_id, id) WHERE NOT is_deleted;

-- ── Reports ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS community_feed_reports (
    id         BIGSERIAL PRIMARY KEY,
    post_id    BIGINT REFERENCES community_feed_posts(id) ON DELETE CASCADE,
    comment_id BIGINT REFERENCES community_feed_comments(id) ON DELETE CASCADE,
    reporter   TEXT NOT NULL,
    reason     TEXT,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_community_feed_reports_open
    ON community_feed_reports(status, id DESC);
-- One report per user per post: a duplicate is an idempotent success.
CREATE UNIQUE INDEX IF NOT EXISTS idx_community_feed_reports_unique
    ON community_feed_reports(post_id, reporter) WHERE comment_id IS NULL;

-- ── Bans ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS community_feed_bans (
    wallet_address TEXT PRIMARY KEY,
    reason         TEXT,
    banned_by      TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Row Level Security ───────────────────────────────────────────────────────
-- The service reads/writes through the SERVICE-ROLE client (the lotto/gcash
-- lesson: anon + RLS silently hides rows). RLS here is defence in depth: if the
-- anon key is ever used directly, it must not be able to read the owner wallet
-- columns or write rows. The browser is never given a Supabase credential for
-- this feature, and `_public_post()` strips the wallet regardless.
ALTER TABLE community_feed_posts     ENABLE ROW LEVEL SECURITY;
ALTER TABLE community_feed_media     ENABLE ROW LEVEL SECURITY;
ALTER TABLE community_feed_reactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE community_feed_comments  ENABLE ROW LEVEL SECURITY;
ALTER TABLE community_feed_reports   ENABLE ROW LEVEL SECURITY;
ALTER TABLE community_feed_bans      ENABLE ROW LEVEL SECURITY;

-- No policies are created on purpose: with RLS enabled and no policy, the anon
-- and authenticated roles can read/write NOTHING. Only the service role (which
-- bypasses RLS) can touch these tables, which is exactly the access model.

-- ── Feature flag: HIDDEN by default ──────────────────────────────────────────
-- maintenance_settings semantics: is_maintenance = TRUE  →  HIDDEN.
-- The NOT EXISTS guard keeps this idempotent even though the table may not
-- carry a UNIQUE constraint on feature_name.
INSERT INTO maintenance_settings (feature_name, is_maintenance, maintenance_message)
SELECT 'community_feed_feature', TRUE, ''
WHERE NOT EXISTS (
    SELECT 1 FROM maintenance_settings WHERE feature_name = 'community_feed_feature'
);
