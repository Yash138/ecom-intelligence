-- ============================================================
-- merge_rankings.sql
-- Promotes staging.amz_ranking_snapshot → transformed.amz_ranking
-- Run after each AmzRankings spider run completes.
--
-- Dedup key: (marketplace_id, list_type, subcategory_node_id, asin, scrape_date)
-- Same ASIN can legitimately rank in multiple subcategories on the same day —
-- subcategory_node_id is required in the key to capture those distinct appearances.
-- Resume duplicates (same ASIN + subcategory + day, different run_id) → UPDATE
-- with latest values from the most recent scrape.
--
-- Usage:
--   psql -d ecom_intel -U ecom_intel_admin -f db/merge_rankings.sql
--
-- Or filter to a specific run_id:
--   psql -d ecom_intel -U ecom_intel_admin \
--        -v run_id="'<uuid>'" \
--        -f db/merge_rankings.sql
-- ============================================================

INSERT INTO transformed.amz_ranking (
    marketplace_id,
    list_type,
    category,
    subcategory,
    subcategory_node_id,
    scrape_date,
    run_id,
    depth,
    rank_position,
    asin,
    title,
    rating,
    review_count,
    price,
    product_url
)
SELECT DISTINCT ON (marketplace_id, list_type, subcategory_node_id, asin, scraped_at::DATE)
    marketplace_id,
    list_type,
    category,
    subcategory,
    subcategory_node_id,
    scraped_at::DATE  AS scrape_date,
    run_id::UUID,
    depth,
    rank_position,
    asin,
    title,
    rating,
    review_count,
    price,
    product_url
FROM staging.amz_ranking_snapshot
ORDER BY marketplace_id, list_type, subcategory_node_id, asin, scraped_at::DATE, scraped_at DESC
ON CONFLICT (marketplace_id, list_type, subcategory_node_id, asin, scrape_date)
DO UPDATE SET
    run_id        = EXCLUDED.run_id,
    rank_position = EXCLUDED.rank_position,
    title         = EXCLUDED.title,
    rating        = EXCLUDED.rating,
    review_count  = EXCLUDED.review_count,
    price         = EXCLUDED.price,
    product_url   = EXCLUDED.product_url,
    category      = EXCLUDED.category,
    subcategory   = EXCLUDED.subcategory,
    depth         = EXCLUDED.depth;

-- Optional: truncate staging after successful merge (uncomment when confident)
-- TRUNCATE staging.amz_ranking_snapshot;
