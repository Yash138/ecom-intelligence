-- Seed amz_category_scrape_controller from ALL nodes discovered by AmzCategoryHierarchy.
-- Run AFTER the AmzCategoryHierarchy spider completes.
--
-- Seeds one row per (marketplace, list_type, root_category, node) combination.
-- ALL nodes are seeded — root, intermediate, and leaf — because:
--   - AmzRankings scrapes ranking pages at every level (each node has its own page)
--   - AmzProducts filters to leaf nodes only at query time via JOIN to amz_category
--
-- list_type covers both 'bestseller' and 'new_release' for every node.
--
-- Root-ancestor resolution: JOIN to amz_category with depth=0.
--   For root nodes (depth=0): self-reference row in hierarchy satisfies depth=0 → category = own name
--   For all other nodes:      finds the unique depth=0 ancestor in their closure rows

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
    node.marketplace_id,
    lt.list_type,
    root_cat.node_name   AS category,
    node.node_name       AS subcategory,
    node.node_id         AS subcategory_node_id,
    10                   AS max_depth_configured,
    0                    AS depth_scraped_upto,
    'pending'            AS scrape_status
FROM transformed.amz_category node
-- Resolve root ancestor via closure table.
-- Filter root_cat to depth=0 to always get a true root category.
-- For root nodes themselves (depth=0): self-reference row (ancestor=self, depth_from_ancestor=0)
--   satisfies root_cat.depth=0 → category = own name. No exclusion needed.
-- For all other nodes: finds the unique depth=0 ancestor in their closure rows.
JOIN transformed.amz_category_hierarchy h
    ON  h.descendant_node_id = node.node_id
    AND h.marketplace_id     = node.marketplace_id
JOIN transformed.amz_category root_cat
    ON  root_cat.node_id        = h.ancestor_node_id
    AND root_cat.marketplace_id = node.marketplace_id
    AND root_cat.depth          = 0
-- Cross join to seed both list types
CROSS JOIN (
    VALUES ('bestseller'), ('new_release')
) AS lt(list_type)
WHERE node.marketplace_id = 'amazon_us'
ON CONFLICT (marketplace_id, list_type, category, subcategory)
DO NOTHING;

-- Verify: total nodes seeded per list_type, broken down by depth
SELECT
    list_type,
    COUNT(*)                    AS total_nodes,
    COUNT(DISTINCT category)    AS root_categories,
    SUM(CASE WHEN cat.is_leaf THEN 1 ELSE 0 END) AS leaf_nodes,
    SUM(CASE WHEN NOT cat.is_leaf THEN 1 ELSE 0 END) AS intermediate_nodes
FROM transformed.amz_category_scrape_controller ctrl
JOIN transformed.amz_category cat
    ON  cat.node_id        = ctrl.subcategory_node_id
    AND cat.marketplace_id = ctrl.marketplace_id
WHERE ctrl.marketplace_id = 'amazon_us'
GROUP BY list_type
ORDER BY list_type;
