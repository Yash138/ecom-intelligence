-- validate_product_run.sql
-- Run after each AmzProducts spider batch to verify run health and data quality.
--
-- Usage:
--   psql -d ecom_intel -U ecom_intel_admin -f scraping/db/validate_product_run.sql
--
-- Default: checks the most recent run (by run_date DESC).
-- To target a specific run_id, set the variable before running:
--   psql -d ecom_intel -U ecom_intel_admin \
--        -v run_id="'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'" \
--        -f scraping/db/validate_product_run.sql
--
-- A healthy run returns 0 rows in every check section.
-- Rows = problems that need investigation.
--
-- Tier 1 (run health) is checked from logs — not in this script.
-- Tier 2 (field null rates) — compares latest run against thresholds.
-- Tier 3 (data sanity)     — checks snapshot table for bad values.
-- Tier 4 (queue drain)     — verifies queue is empty post-run.

-- -------------------------------------------------------------------------
-- Setup: identify the run to validate
-- -------------------------------------------------------------------------

-- Use :run_id if passed on the command line, otherwise pick the latest run.
-- psql substitutes :run_id as a literal; if not set the COALESCE picks latest.
DO $$
BEGIN
    -- no-op; just a safe way to start the script without \set portability issues
END $$;

\echo ''
\echo '======================================================================'
\echo 'AmzProducts Run Validation'
\echo '======================================================================'

\echo ''
\echo '--- Run summary ---'
SELECT
    run_id,
    run_date,
    marketplace_id,
    SUM(total_records)                          AS total_products,
    SUM(null_count)                             AS total_null_values,
    COUNT(DISTINCT field_name)                  AS fields_tracked,
    COUNT(*) FILTER (WHERE null_rate = 0)       AS fields_fully_populated
FROM monitoring.scrape_run_field_stats
WHERE run_id = (
    SELECT run_id
    FROM monitoring.scrape_run_field_stats
    ORDER BY run_date DESC, run_id DESC
    LIMIT 1
)
GROUP BY run_id, run_date, marketplace_id;


-- -------------------------------------------------------------------------
-- Tier 2: Field null rate violations
-- Rows returned = fields whose null rate exceeds the acceptable threshold.
-- -------------------------------------------------------------------------

\echo ''
\echo '--- Tier 2: Null rate violations (rows = problems) ---'
\echo '    Threshold: max acceptable null % for each field'

WITH latest_run AS (
    SELECT run_id
    FROM monitoring.scrape_run_field_stats
    ORDER BY run_date DESC, run_id DESC
    LIMIT 1
),
thresholds (field_name, max_null_rate) AS (
    VALUES
    -- Critical: these break core scoring signals
    ('title',            0.05),
    ('rating',           0.15),
    ('review_count',     0.15),
    ('price',            0.35),
    ('brand',            0.25),
    ('seller_name',      0.30),
    ('is_fba',           0.30),
    ('bsr_entries',      0.35),
    -- Informational: expected nulls for many product types
    ('last_month_sales', 0.65),
    -- Boolean: never null — always derived from page structure
    ('has_variants',     0.00),
    ('is_small_business',0.00)
)
SELECT
    s.field_name,
    s.null_count,
    s.total_records,
    round(s.null_rate * 100, 1)        AS actual_null_pct,
    round(t.max_null_rate * 100, 1)    AS threshold_pct,
    round((s.null_rate - t.max_null_rate) * 100, 1) AS excess_pct
FROM monitoring.scrape_run_field_stats s
JOIN latest_run lr    ON s.run_id = lr.run_id
JOIN thresholds t     ON s.field_name = t.field_name
WHERE s.null_rate > t.max_null_rate
ORDER BY (s.null_rate - t.max_null_rate) DESC;


-- -------------------------------------------------------------------------
-- Tier 2 supplement: full null rate report for the latest run (informational)
-- -------------------------------------------------------------------------

\echo ''
\echo '--- Tier 2 (info): Full null rates for latest run ---'

WITH latest_run AS (
    SELECT run_id
    FROM monitoring.scrape_run_field_stats
    ORDER BY run_date DESC, run_id DESC
    LIMIT 1
),
thresholds (field_name, max_null_rate) AS (
    VALUES
    ('title',            0.05),
    ('rating',           0.15),
    ('review_count',     0.15),
    ('price',            0.35),
    ('brand',            0.25),
    ('seller_name',      0.30),
    ('is_fba',           0.30),
    ('bsr_entries',      0.35),
    ('last_month_sales', 0.65),
    ('has_variants',     0.00),
    ('is_small_business',0.00)
)
SELECT
    s.field_name,
    s.null_count,
    s.total_records,
    round(s.null_rate * 100, 1)      AS null_pct,
    round(t.max_null_rate * 100, 1)  AS threshold_pct,
    CASE WHEN s.null_rate > t.max_null_rate THEN 'FAIL' ELSE 'ok' END AS status
FROM monitoring.scrape_run_field_stats s
JOIN latest_run lr ON s.run_id = lr.run_id
LEFT JOIN thresholds t ON s.field_name = t.field_name
ORDER BY s.null_rate DESC;


-- -------------------------------------------------------------------------
-- Tier 3a: Price out of plausible range
-- -------------------------------------------------------------------------

\echo ''
\echo '--- Tier 3a: Price out of range (rows = bad values) ---'

SELECT asin, price
FROM staging.amz_product_snapshot
WHERE price IS NOT NULL
  AND (price <= 0 OR price > 5000)
ORDER BY asin;


-- -------------------------------------------------------------------------
-- Tier 3b: Rating out of range (must be 1.0–5.0 on Amazon)
-- -------------------------------------------------------------------------

\echo ''
\echo '--- Tier 3b: Rating out of range (rows = bad values) ---'

SELECT asin, rating
FROM staging.amz_product_snapshot
WHERE rating IS NOT NULL
  AND (rating < 1.0 OR rating > 5.0)
ORDER BY asin;


-- -------------------------------------------------------------------------
-- Tier 3c: BSR rank suspiciously high — indicates regex parse failure
-- Legitimate BSR ranks cap at ~5M on Amazon US
-- -------------------------------------------------------------------------

\echo ''
\echo '--- Tier 3c: BSR rank > 5,000,000 (rows = likely parser error) ---'

SELECT
    asin,
    entry->>'category'                      AS category,
    (entry->>'rank')::int                   AS rank
FROM staging.amz_product_snapshot,
     jsonb_array_elements(bsr_entries) AS entry
WHERE bsr_entries IS NOT NULL
  AND (entry->>'rank')::int > 5000000
ORDER BY rank DESC;


-- -------------------------------------------------------------------------
-- Tier 3d: Invalid ASIN format in variant_asins JSONB
-- All ASINs must match ^[A-Z0-9]{10}$
-- -------------------------------------------------------------------------

\echo ''
\echo '--- Tier 3d: Invalid ASIN format in variant_asins (rows = parse error) ---'

SELECT DISTINCT
    s.asin                  AS source_asin,
    v->>'asin'              AS bad_variant_asin
FROM staging.amz_product_snapshot s,
     jsonb_array_elements(s.variant_asins) v
WHERE s.variant_asins IS NOT NULL
  AND v->>'asin' !~ '^[A-Z0-9]{10}$'
ORDER BY s.asin;


-- -------------------------------------------------------------------------
-- Tier 3e: CAPTCHA / bot-block detection
-- A scrape hit by Amazon's bot detection will have a NULL title or
-- a title matching known CAPTCHA page text
-- -------------------------------------------------------------------------

\echo ''
\echo '--- Tier 3e: Possible CAPTCHA / blocked pages (rows = investigate HTML) ---'

SELECT asin, title
FROM staging.amz_product_snapshot
WHERE title IS NULL
   OR lower(title) LIKE '%robot%'
   OR lower(title) LIKE '%captcha%'
   OR lower(title) LIKE '%not a robot%'
   OR lower(title) LIKE '%enter the characters%'
   OR lower(title) LIKE '%sorry%something went wrong%'
ORDER BY asin;


-- -------------------------------------------------------------------------
-- Tier 3f: Variant dimension keys missing labels
-- If dimensionToAsinMap was parsed but variationValues extraction failed,
-- variant entries will have only {"asin": "..."} with no dimension fields.
-- More than 20% of variants missing labels signals a variationValues parse failure.
-- -------------------------------------------------------------------------

\echo ''
\echo '--- Tier 3f: variant_asins missing dimension labels (rows = parse issue) ---'

WITH pairs AS (
    SELECT
        s.asin,
        jsonb_array_length(s.variant_asins) AS total_variants,
        v                                   AS variant_entry
    FROM staging.amz_product_snapshot s,
         jsonb_array_elements(s.variant_asins) v
    WHERE s.variant_asins IS NOT NULL
      AND jsonb_array_length(s.variant_asins) > 0
),
per_product AS (
    SELECT
        asin,
        total_variants,
        COUNT(*) FILTER (
            WHERE jsonb_typeof(variant_entry) = 'object'
              AND variant_entry = jsonb_build_object('asin', variant_entry->>'asin')
        ) AS variants_no_labels
    FROM pairs
    GROUP BY asin, total_variants
)
SELECT
    asin,
    total_variants,
    variants_no_labels,
    round(variants_no_labels::numeric / total_variants * 100, 1) AS pct_no_labels
FROM per_product
WHERE variants_no_labels::numeric / total_variants > 0.20
ORDER BY asin;


-- -------------------------------------------------------------------------
-- Tier 4: Queue drain check
-- After a full run, queue should be empty. Remaining rows = failed scrapes.
-- -------------------------------------------------------------------------

\echo ''
\echo '--- Tier 4: Queue drain (rows = ASINs that failed to scrape) ---'

SELECT marketplace_id, asin, added_at
FROM transformed.amz_product_scrape_queue
ORDER BY marketplace_id, added_at;


-- -------------------------------------------------------------------------
-- Summary
-- -------------------------------------------------------------------------

\echo ''
\echo '--- Summary: row counts across all checks ---'

SELECT 'tier2_null_violations'   AS check_name,
       COUNT(*)                  AS row_count
FROM (
    WITH latest_run AS (
        SELECT run_id FROM monitoring.scrape_run_field_stats
        ORDER BY run_date DESC, run_id DESC LIMIT 1
    ),
    thresholds (field_name, max_null_rate) AS (
        VALUES ('title',0.05),('rating',0.15),('review_count',0.15),
               ('price',0.35),('brand',0.25),('seller_name',0.30),
               ('is_fba',0.30),('bsr_entries',0.35),
               ('last_month_sales',0.65),('has_variants',0.00),('is_small_business',0.00)
    )
    SELECT 1
    FROM monitoring.scrape_run_field_stats s
    JOIN latest_run lr ON s.run_id = lr.run_id
    JOIN thresholds t  ON s.field_name = t.field_name
    WHERE s.null_rate > t.max_null_rate
) x

UNION ALL SELECT 'tier3a_price_out_of_range', COUNT(*) FROM staging.amz_product_snapshot
WHERE price IS NOT NULL AND (price <= 0 OR price > 5000)

UNION ALL SELECT 'tier3b_rating_out_of_range', COUNT(*) FROM staging.amz_product_snapshot
WHERE rating IS NOT NULL AND (rating < 1.0 OR rating > 5.0)

UNION ALL SELECT 'tier3c_bsr_rank_gt_5m', COUNT(*)
FROM staging.amz_product_snapshot, jsonb_array_elements(bsr_entries) entry
WHERE bsr_entries IS NOT NULL AND (entry->>'rank')::int > 5000000

UNION ALL SELECT 'tier3d_invalid_variant_asin', COUNT(*)
FROM (
    SELECT DISTINCT s.asin, v->>'asin'
    FROM staging.amz_product_snapshot s, jsonb_array_elements(s.variant_asins) v
    WHERE s.variant_asins IS NOT NULL AND v->>'asin' !~ '^[A-Z0-9]{10}$'
) x

UNION ALL SELECT 'tier3e_captcha_or_null_title', COUNT(*) FROM staging.amz_product_snapshot
WHERE title IS NULL OR lower(title) LIKE ANY(ARRAY['%robot%','%captcha%','%not a robot%'])

UNION ALL SELECT 'tier4_queue_not_drained', COUNT(*) FROM transformed.amz_product_scrape_queue

ORDER BY check_name;

\echo ''
\echo '======================================================================'
\echo 'Done. Zero row_count in all summary rows = healthy run.'
\echo '======================================================================'
\echo ''
