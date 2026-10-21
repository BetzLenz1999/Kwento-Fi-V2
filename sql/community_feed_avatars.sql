-- GoodMarket Community Feed — member profile pictures (avatars).
--
-- One row per wallet: the member's uploaded profile picture. Supabase stores
-- METADATA ONLY; the bytes live in Cloudflare R2 (see storage/r2.py) and are
-- referenced by r2_key, exactly like community_feed_media.
--
-- WHY ITS OWN TABLE, NOT a column on user_data: user_data is the app-wide
-- identity row and is read on nearly every request. An avatar is a COMMUNITY
-- feature (a profile picture shown on feed cards and the public profile), so it
-- belongs to this module's tables — the same isolation rule the rest of the feed
-- follows. It also keeps the avatar write off the user_data update path, so a
-- picture change can never race a username/verification update.
--
-- ONE row per wallet (wallet_address is the PRIMARY KEY): a new upload REPLACES
-- the old one, so a member cannot accumulate avatars and the read is a single
-- row. The service purges the replaced R2 object on the same path.
--
-- PUBLIC: an avatar is meant to be seen on every card, so — unlike the owner
-- wallet column on community_feed_posts — the public URL is served to everyone.
-- The WALLET is still never sent to the browser (the response carries only the
-- public URL), and RLS keeps the anon key out.
--
-- Run this in the Supabase SQL editor BEFORE enabling avatar uploads.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS community_feed_avatars (
    wallet_address TEXT PRIMARY KEY,
    -- Snapshot at upload time so a diagnostic query needs no join. Never sent
    -- to the browser; the public shape exposes only public_url.
    username       TEXT,
    r2_key         TEXT NOT NULL,
    public_url     TEXT NOT NULL,
    mime           TEXT,
    bytes          BIGINT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Batched "fetch the avatar for each of these authors" lookup for a feed page.
CREATE INDEX IF NOT EXISTS idx_community_feed_avatars_wallet
    ON community_feed_avatars(wallet_address);

-- ── Row Level Security ───────────────────────────────────────────────────────
-- Enabled with NO policies: the anon/authenticated roles can read/write
-- NOTHING, and only the service role (which bypasses RLS) can touch the table.
-- Same access model as the rest of the community feed — the browser is never
-- given a Supabase credential for this feature.
ALTER TABLE community_feed_avatars ENABLE ROW LEVEL SECURITY;
