-- validate_hierarchy.sql
-- Run after AmzCategoryHierarchy completes, BEFORE seed_controller.sql.
-- All checks must return 0 rows (or the expected counts noted in comments).
-- If any check fails, do NOT proceed to seeding — truncate and re-run the spider.
--
-- Usage:
--   psql -d ecom_intel -U ecom_intel_admin -f db/validate_hierarchy.sql

\echo ''
\echo '=============================='
\echo 'Hierarchy Validation Checks'
\echo '=============================='

-- ------------------------------------------------------------------
-- CHECK 1: Exactly 9 root nodes (depth=0), one per target category.
-- Expected: count = 9
-- Failure: the spider missed some target categories (no match on root page)
--          or TARGET_CATEGORIES config is wrong.
-- Note: chosen_categories.csv has 9 categories — Kitchen & Dining is NOT included.
-- ------------------------------------------------------------------
\echo ''
\echo 'CHECK 1 — Root node count (expect 9):'
SELECT COUNT(*) AS root_count
FROM transformed.amz_category
WHERE marketplace_id = 'amazon_us' AND depth = 0;

-- ------------------------------------------------------------------
-- CHECK 2: All 9 expected categories are present at depth=0.
-- Expected: 0 rows returned (no missing categories).
-- Failure: the spider did not find one or more target categories on the
--          Amazon root page. The missing category has no hierarchy data.
-- ------------------------------------------------------------------
\echo ''
\echo 'CHECK 2 — Missing root categories (expect 0 rows):'
SELECT expected.category
FROM (VALUES
    ('Arts, Crafts & Sewing'),
    ('Clothing, Shoes & Jewelry'),
    ('Handmade Products'),
    ('Health & Household'),
    ('Home & Kitchen'),
    ('Office Products'),
    ('Patio, Lawn & Garden'),
    ('Pet Supplies'),
    ('Tools & Home Improvement')
) AS expected(category)
WHERE NOT EXISTS (
    SELECT 1
    FROM transformed.amz_category c
    WHERE c.marketplace_id = 'amazon_us'
      AND c.depth = 0
      AND c.node_name = expected.category
);

-- ------------------------------------------------------------------
-- CHECK 3: No shared root node_ids.
-- Expected: 0 rows returned.
-- Failure: multiple root categories share the same node_id — they are
--          overwriting each other in the DB. Root categories that share
--          a URL slug (e.g. /hi/) must use distinct node_ids. After the
--          parse_root() fix, node_id = category name, which is unique.
-- ------------------------------------------------------------------
\echo ''
\echo 'CHECK 3 — Shared root node_ids (expect 0 rows):'
SELECT node_id, COUNT(*) AS occurrences, array_agg(node_name) AS categories
FROM transformed.amz_category
WHERE marketplace_id = 'amazon_us' AND depth = 0
GROUP BY node_id
HAVING COUNT(*) > 1;

-- ------------------------------------------------------------------
-- CHECK 4: No subcategory node appears under more than one root category.
-- Expected: 0 rows returned.
-- Failure: closure table contamination — a node is listed as a descendant
--          of multiple root categories. This causes seed_controller.sql to
--          assign it to the wrong category (or multiple categories), and
--          AmzRankings to scrape it during the wrong category's run.
-- ------------------------------------------------------------------
\echo ''
\echo 'CHECK 4 — Subcategories under multiple roots (expect 0 rows):'
SELECT
    h.descendant_node_id,
    c.node_name                                         AS node_name,
    COUNT(DISTINCT root.node_id)                        AS root_count,
    array_agg(root.node_name ORDER BY root.node_name)  AS appears_under
FROM transformed.amz_category_hierarchy h
JOIN transformed.amz_category c
    ON  c.node_id        = h.descendant_node_id
    AND c.marketplace_id = h.marketplace_id
JOIN transformed.amz_category root
    ON  root.node_id        = h.ancestor_node_id
    AND root.marketplace_id = h.marketplace_id
    AND root.depth          = 0
WHERE h.marketplace_id = 'amazon_us'
GROUP BY h.descendant_node_id, c.node_name
HAVING COUNT(DISTINCT root.node_id) > 1
ORDER BY root_count DESC, node_name
LIMIT 20;

-- ------------------------------------------------------------------
-- CHECK 5: Every node in amz_category has at least one ancestor path
--          back to a depth=0 root in the closure table.
-- Expected: 0 rows returned.
-- Failure: orphaned nodes — present in amz_category but not reachable
--          from any root via the hierarchy. seed_controller.sql would
--          skip these (the JOIN to amz_category_hierarchy finds no root),
--          so they would never be scraped.
-- ------------------------------------------------------------------
\echo ''
\echo 'CHECK 5 — Nodes with no path to a root (expect 0 rows):'
SELECT c.node_id, c.node_name, c.depth, c.url_slug
FROM transformed.amz_category c
WHERE c.marketplace_id = 'amazon_us'
  AND NOT EXISTS (
      SELECT 1
      FROM transformed.amz_category_hierarchy h
      JOIN transformed.amz_category root
          ON  root.node_id        = h.ancestor_node_id
          AND root.marketplace_id = h.marketplace_id
          AND root.depth          = 0
      WHERE h.descendant_node_id = c.node_id
        AND h.marketplace_id     = c.marketplace_id
  )
ORDER BY c.depth, c.node_name
LIMIT 20;

-- ------------------------------------------------------------------
-- CHECK 6: Every node has its own self-reference row (depth_from_ancestor=0).
-- Expected: 0 rows returned.
-- Failure: _write_node() failed to insert the self-reference closure record
--          for some nodes. seed_controller.sql JOIN would miss these nodes.
-- ------------------------------------------------------------------
\echo ''
\echo 'CHECK 6 — Nodes missing self-reference in hierarchy (expect 0 rows):'
SELECT c.node_id, c.node_name, c.depth
FROM transformed.amz_category c
WHERE c.marketplace_id = 'amazon_us'
  AND NOT EXISTS (
      SELECT 1
      FROM transformed.amz_category_hierarchy h
      WHERE h.marketplace_id       = c.marketplace_id
        AND h.ancestor_node_id     = c.node_id
        AND h.descendant_node_id   = c.node_id
        AND h.depth_from_ancestor  = 0
  )
ORDER BY c.depth, c.node_name
LIMIT 20;

-- ------------------------------------------------------------------
-- SUMMARY: Node counts per root category (informational).
-- Use this to spot categories with suspiciously low node counts —
-- may indicate the traversal was cut short or cross-contamination
-- claimed nodes before the correct root could.
-- ------------------------------------------------------------------
\echo ''
\echo 'SUMMARY — Nodes per root category (informational):'
SELECT
    root.node_name                                          AS category,
    root.url_slug,
    COUNT(DISTINCT h.descendant_node_id)                   AS total_nodes,
    COUNT(DISTINCT CASE WHEN c.is_leaf  THEN c.node_id END) AS leaf_nodes,
    COUNT(DISTINCT CASE WHEN NOT c.is_leaf THEN c.node_id END) AS intermediate_nodes
FROM transformed.amz_category root
JOIN transformed.amz_category_hierarchy h
    ON  h.ancestor_node_id = root.node_id
    AND h.marketplace_id   = root.marketplace_id
JOIN transformed.amz_category c
    ON  c.node_id        = h.descendant_node_id
    AND c.marketplace_id = h.marketplace_id
WHERE root.marketplace_id = 'amazon_us'
  AND root.depth          = 0
GROUP BY root.node_name, root.url_slug
ORDER BY root.node_name;
