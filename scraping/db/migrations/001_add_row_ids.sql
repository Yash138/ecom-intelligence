-- Migration 001: add surrogate UUID primary key to staging and id column to transformed ranking
-- Run once against ecom_intel. Safe to re-run (IF NOT EXISTS / IF EXISTS guards).
--
-- staging.amz_ranking_snapshot:
--   - Adds id UUID as new PRIMARY KEY (old composite PK dropped).
--   - Old composite (run_id, marketplace_id, list_type, subcategory_node_id, asin) had a latent
--     NULL issue (subcategory_node_id is nullable) and prevented the row-level dedup queries.
--
-- transformed.amz_ranking:
--   - Adds id UUID as a plain column. Natural PK unchanged.

-- ── staging.amz_ranking_snapshot ───────────────────────────────────────────

ALTER TABLE staging.amz_ranking_snapshot
    ADD COLUMN IF NOT EXISTS id UUID NOT NULL DEFAULT gen_random_uuid();

ALTER TABLE staging.amz_ranking_snapshot
    DROP CONSTRAINT IF EXISTS amz_ranking_snapshot_pkey;

ALTER TABLE staging.amz_ranking_snapshot
    ADD PRIMARY KEY (id);

-- ── transformed.amz_ranking ────────────────────────────────────────────────

ALTER TABLE transformed.amz_ranking
    ADD COLUMN IF NOT EXISTS id UUID NOT NULL DEFAULT gen_random_uuid();
