-- Seed amz_category_scrape_controller from leaf nodes discovered by AmzCategoryHierarchy.
-- Run AFTER the AmzCategoryHierarchy spider completes.
--
-- Seeds one row per (marketplace, list_type, root_category, leaf_node) combination.
-- list_type covers both 'bestseller' and 'new_release' for each leaf node.

INSERT INTO transformed.amz_category_scrape_controller (
    marketplace_id,
    list_type,
    category,
    subcategory,
    subcategory_node_id,
    max_depth_configured,
    depth_scraped_upto,
    scrape_status
)
SELECT
    leaf.marketplace_id,
    lt.list_type,
    root_cat.node_name      AS category,
    leaf.node_name          AS subcategory,
    leaf.node_id            AS subcategory_node_id,
    10                      AS max_depth_configured,
    0                       AS depth_scraped_upto,
    'pending'               AS scrape_status
FROM transformed.amz_category leaf
-- Find the root ancestor (depth = 0) for each leaf via the closure table
JOIN transformed.amz_category_hierarchy h
    ON  h.descendant_node_id = leaf.node_id
    AND h.ancestor_node_id  != leaf.node_id     -- exclude self
JOIN transformed.amz_category root_cat
    ON  root_cat.node_id       = h.ancestor_node_id
    AND root_cat.marketplace_id = leaf.marketplace_id
    AND root_cat.depth          = 0             -- root category only
-- Cross join to seed both list types
CROSS JOIN (
    VALUES ('bestseller'), ('new_release')
) AS lt(list_type)
WHERE leaf.is_leaf = TRUE
  AND leaf.marketplace_id = 'amazon_us'
ON CONFLICT (marketplace_id, list_type, category, subcategory)
DO NOTHING;

-- Verify
SELECT
    list_type,
    COUNT(*) AS leaf_nodes,
    COUNT(DISTINCT category) AS root_categories
FROM transformed.amz_category_scrape_controller
WHERE marketplace_id = 'amazon_us'
GROUP BY list_type
ORDER BY list_type;
