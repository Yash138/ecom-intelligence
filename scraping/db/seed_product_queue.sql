-- ============================================================
-- Seed amz_product_scrape_queue from transformed.amz_ranking
-- ============================================================
-- Run manually after each AmzRankings merge cycle:
--   psql -d ecom_intel -U ecom_intel_admin -f scraping/db/seed_product_queue.sql
--
-- Strategy:
--   - Sources ALL distinct ASINs from transformed.amz_ranking regardless of node depth.
--     No is_leaf filter: AmzRankings scrapes all node levels (root + intermediate + leaf),
--     so filtering to is_leaf=TRUE would silently exclude valid ASINs.
--   - For ASINs that appear in multiple nodes/dates, picks the most recent product_url
--     (DISTINCT ON + ORDER BY scrape_date DESC).
--   - ON CONFLICT DO NOTHING: never overwrites existing queue rows.
--     Ensures variant ASINs added by the spider are not overwritten.
-- ============================================================

INSERT INTO transformed.amz_product_scrape_queue (
    marketplace_id,
    asin,
    product_url
)
SELECT DISTINCT ON (r.marketplace_id, r.asin)
    r.marketplace_id,
    r.asin,
    r.product_url
FROM transformed.amz_ranking r
WHERE r.product_url IS NOT NULL
  AND r.asin IS NOT NULL
ORDER BY r.marketplace_id, r.asin, r.scrape_date DESC
ON CONFLICT (marketplace_id, asin) DO NOTHING;

-- Show queue state after seeding
SELECT
    marketplace_id,
    COUNT(*)                                             AS total_in_queue,
    COUNT(*) FILTER (WHERE added_at > NOW() - INTERVAL '5 minutes') AS newly_added
FROM transformed.amz_product_scrape_queue
GROUP BY marketplace_id;
