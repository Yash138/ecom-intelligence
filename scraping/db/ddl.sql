-- ============================================================
-- ecom_intel DB — full DDL
-- Run in this order. Connect as ecom_intel_admin.
-- ============================================================

-- 1. Schemas
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS transformed;
CREATE SCHEMA IF NOT EXISTS monitoring;
CREATE SCHEMA IF NOT EXISTS curated;

-- 2. Marketplace lookup
--    Seed: see seed_marketplaces.sql
CREATE TABLE IF NOT EXISTS transformed.marketplaces (
    marketplace_id  VARCHAR(20) PRIMARY KEY,    -- 'amazon_us', 'amazon_in', 'amazon_uk'
    platform        VARCHAR(20)  NOT NULL,       -- 'amazon'
    country_code    CHAR(2)      NOT NULL,
    currency        CHAR(3)      NOT NULL,
    domain          VARCHAR(50)  NOT NULL,       -- 'amazon.com', 'amazon.in'
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE
);

-- 3. Category nodes — one row per Amazon browse node
CREATE TABLE IF NOT EXISTS transformed.amz_category (
    marketplace_id   VARCHAR(20)  NOT NULL REFERENCES transformed.marketplaces(marketplace_id),
    node_id          VARCHAR(30)  NOT NULL,
    node_name        VARCHAR(255) NOT NULL,
    url_slug         VARCHAR(100) NOT NULL,      -- segment in URL: amazon.com/bestsellers/<slug>
    parent_node_id   VARCHAR(30),                -- NULL for 10 root categories
    depth            SMALLINT     NOT NULL,      -- 0 = root category
    is_leaf          BOOLEAN      NOT NULL DEFAULT FALSE,
    is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
    first_seen_at    TIMESTAMP    NOT NULL DEFAULT NOW(),
    last_verified_at TIMESTAMP,
    PRIMARY KEY (marketplace_id, node_id)
);

-- 4. Category hierarchy — closure table
--    One row per ancestor-descendant pair (including self: depth_from_ancestor = 0).
--    Replaces the India lvl1..lvl8 fixed-column approach.
--
--    Query: all leaf nodes under "Home & Kitchen" (node_id = 'home-garden')
--        SELECT c.*
--        FROM transformed.amz_category c
--        JOIN transformed.amz_category_hierarchy h
--            ON h.descendant_node_id = c.node_id
--        WHERE h.ancestor_node_id = 'home-garden'
--          AND c.is_leaf = TRUE
--          AND c.marketplace_id = 'amazon_us';
CREATE TABLE IF NOT EXISTS transformed.amz_category_hierarchy (
    marketplace_id        VARCHAR(20) NOT NULL REFERENCES transformed.marketplaces(marketplace_id),
    ancestor_node_id      VARCHAR(30) NOT NULL,
    descendant_node_id    VARCHAR(30) NOT NULL,
    depth_from_ancestor   SMALLINT    NOT NULL,  -- 0 = self, 1 = direct child, etc.
    PRIMARY KEY (marketplace_id, ancestor_node_id, descendant_node_id)
);

-- 5. Scrape controller — one row per (marketplace, list_type, category, subcategory)
--    Seeded from leaf nodes (see seed_controller.sql).
--    Spider reads this to know what to scrape and where to resume.
CREATE TABLE IF NOT EXISTS transformed.amz_category_scrape_controller (
    marketplace_id        VARCHAR(20) NOT NULL REFERENCES transformed.marketplaces(marketplace_id),
    list_type             VARCHAR(20) NOT NULL,              -- 'bestseller', 'new_release'
    category              VARCHAR(100) NOT NULL,
    subcategory           VARCHAR(100) NOT NULL,
    subcategory_node_id   VARCHAR(30)  NOT NULL,
    max_depth_configured  SMALLINT     NOT NULL DEFAULT 10,
    depth_scraped_upto    SMALLINT     NOT NULL DEFAULT 0,
    last_scraped_at       TIMESTAMP,
    scrape_status         VARCHAR(20)  NOT NULL DEFAULT 'pending',
    -- scrape_status values: 'pending' | 'in_progress' | 'complete'
    PRIMARY KEY (marketplace_id, list_type, category, subcategory)
);

-- 6. Ranking snapshot — raw append output of AmzRankings spider
--    Short-term buffer. Duplicates possible from resume scenarios.
--    Promoted to transformed.amz_ranking via MERGE after each run.
CREATE TABLE IF NOT EXISTS staging.amz_ranking_snapshot (
    id                  UUID         NOT NULL DEFAULT gen_random_uuid(),
    run_id              UUID         NOT NULL,
    marketplace_id      VARCHAR(20)  NOT NULL REFERENCES transformed.marketplaces(marketplace_id),
    list_type           VARCHAR(20)  NOT NULL,
    category            VARCHAR(100) NOT NULL,
    subcategory         VARCHAR(100),
    subcategory_node_id VARCHAR(30),
    depth               SMALLINT,
    rank_position       SMALLINT,
    asin                VARCHAR(20)  NOT NULL,
    title               TEXT,
    rating              NUMERIC(3,2),
    review_count        INTEGER,
    price               NUMERIC(10,2),
    product_url         TEXT,                   -- raw href from page, ref params intact
    scraped_at          TIMESTAMP    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id)
);

-- Unique constraint required for ON CONFLICT in bulk_upsert during spider writes.
-- Dedup key within a single run: same ASIN cannot rank twice in the same node+run.
ALTER TABLE staging.amz_ranking_snapshot
    ADD CONSTRAINT amz_ranking_snapshot_run_asin_uq
    UNIQUE (run_id, marketplace_id, list_type, subcategory_node_id, asin);

CREATE INDEX IF NOT EXISTS idx_ranking_snapshot_lookup
    ON staging.amz_ranking_snapshot (marketplace_id, list_type, scraped_at);

-- 7. Ranking — deduplicated permanent store
--    Promoted from staging via MERGE after each spider run.
--    Dedup key: (marketplace_id, list_type, subcategory_node_id, asin, scrape_date)
--    Same ASIN can legitimately rank in multiple subcategories on the same day —
--    subcategory_node_id is required in the key to preserve those distinct appearances.
CREATE TABLE IF NOT EXISTS transformed.amz_ranking (
    id                  UUID         NOT NULL DEFAULT gen_random_uuid(),
    marketplace_id      VARCHAR(20)  NOT NULL REFERENCES transformed.marketplaces(marketplace_id),
    list_type           VARCHAR(20)  NOT NULL,
    category            VARCHAR(100) NOT NULL,
    subcategory         VARCHAR(100),
    subcategory_node_id VARCHAR(30),
    scrape_date         DATE         NOT NULL,
    run_id              UUID         NOT NULL,   -- run that last wrote this row
    depth               SMALLINT,
    rank_position       SMALLINT,
    asin                VARCHAR(20)  NOT NULL,
    title               TEXT,
    rating              NUMERIC(3,2),
    review_count        INTEGER,
    price               NUMERIC(10,2),
    product_url         TEXT,
    PRIMARY KEY (marketplace_id, list_type, subcategory_node_id, asin, scrape_date)
);

CREATE INDEX IF NOT EXISTS idx_ranking_by_date
    ON transformed.amz_ranking (marketplace_id, list_type, scrape_date);

CREATE INDEX IF NOT EXISTS idx_ranking_by_asin
    ON transformed.amz_ranking (asin, marketplace_id, scrape_date);

-- 8a. Product scrape queue — to-do list for AmzProducts spider
--     Seeded from transformed.amz_ranking via seed_product_queue.sql.
--     Delete-on-success: row is removed after a successful scrape + DB write.
--     No scrape_status column — job is either pending (in queue) or done (deleted).
--     Variant ASINs discovered during scraping are also inserted here (ON CONFLICT DO NOTHING).
CREATE TABLE IF NOT EXISTS transformed.amz_product_scrape_queue (
    marketplace_id   VARCHAR(20)  NOT NULL REFERENCES transformed.marketplaces(marketplace_id),
    asin             VARCHAR(20)  NOT NULL,
    product_url      TEXT         NOT NULL,
    added_at         TIMESTAMP    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (marketplace_id, asin)
);

-- 8b. Product snapshot — output of AmzProducts spider
--     One row per (marketplace_id, asin). UPSERT semantics on re-scrape.
--     first_captured_at: set on INSERT only, never updated (SCD2 foundation).
--     last_captured_at: updated on every re-scrape.
--     JS-rendered fields (price, seller_name, seller_id, is_fba) require
--     Playwright with zip code 19901 set; NULL when scraped without Playwright.
CREATE TABLE IF NOT EXISTS staging.amz_product_snapshot (
    -- identity
    marketplace_id      VARCHAR(20)  NOT NULL REFERENCES transformed.marketplaces(marketplace_id),
    asin                VARCHAR(20)  NOT NULL,
    -- SCD2 timestamps
    first_captured_at   TIMESTAMP    NOT NULL DEFAULT NOW(),
    last_captured_at    TIMESTAMP    NOT NULL DEFAULT NOW(),
    -- static fields (rarely change)
    title               TEXT,
    brand               VARCHAR(255),
    main_image_url      TEXT,
    launch_date         VARCHAR(50),        -- raw text, e.g. "January 1, 2023"
    about_this_item     TEXT,               -- newline-delimited bullet points
    -- volatile fields (change frequently)
    rating              NUMERIC(3,2),
    review_count        INTEGER,
    rating_breakdown    JSONB,              -- {"5":63,"4":12,"3":7,"2":6,"1":12}
    bsr_entries         JSONB,              -- [{"rank":360,"category":"Patio, Lawn & Garden"},...]
    last_month_sales    VARCHAR(30),        -- raw text, e.g. "100+" or "1K+"
    -- JS-rendered fields (Playwright + zip 19901 required)
    price               NUMERIC(10,2),
    seller_name         VARCHAR(255),
    seller_id           VARCHAR(50),
    is_fba              BOOLEAN,
    -- variant and related products
    has_variants        BOOLEAN,
    variant_asins       JSONB,              -- ["B0XX","B0YY"]
    related_asins       JSONB,              -- ["B0AA","B0BB"]
    -- product attributes (sparse — not present on all products)
    weight              VARCHAR(100),
    dimensions          VARCHAR(200),
    -- metadata
    is_small_business   BOOLEAN,
    html_file_path      VARCHAR(500),       -- path to archived rendered HTML; NULL if not saved
    PRIMARY KEY (marketplace_id, asin)
);

CREATE INDEX IF NOT EXISTS idx_product_snapshot_by_marketplace
    ON staging.amz_product_snapshot (marketplace_id, last_captured_at);

CREATE INDEX IF NOT EXISTS idx_product_snapshot_by_asin
    ON staging.amz_product_snapshot (asin);

-- 9. Monitoring — null rate tracking per run per field
CREATE TABLE IF NOT EXISTS monitoring.scrape_run_field_stats (
    run_id          UUID         NOT NULL,
    spider_name     VARCHAR(50)  NOT NULL,
    run_date        DATE         NOT NULL,
    marketplace_id  VARCHAR(20)  NOT NULL,
    field_name      VARCHAR(100) NOT NULL,
    total_records   INTEGER      NOT NULL,
    null_count      INTEGER      NOT NULL,
    null_rate       NUMERIC(5,4) NOT NULL,
    PRIMARY KEY (run_id, field_name)
);
