"Arts, Crafts & Sewing"
"Clothing, Shoes & Jewelry"
Handmade Products
Health & Household
Home & Kitchen
Kitchen & Dining
Office Products
"Patio, Lawn & Garden"
Pet Supplies



```sql
-- Verify orphans are resolved/gone from amz_category_hierarchy table:
SELECT COUNT(*)
FROM transformed.amz_category_hierarchy h
WHERE h.marketplace_id = 'amazon_us'
  AND h.depth_from_ancestor = 0
  AND NOT EXISTS (
      SELECT 1 FROM transformed.amz_category c
      WHERE c.marketplace_id = h.marketplace_id
        AND c.node_id = h.ancestor_node_id
  );
-- Should return 0

-- To check which category orphans belong to: 
SELECT
    root.node_name                                  AS root_category,
    COUNT(DISTINCT h.ancestor_node_id)              AS orphaned_nodes
FROM transformed.amz_category_hierarchy h
-- find the root category above the orphan's known descendant
JOIN transformed.amz_category_hierarchy up
    ON  up.descendant_node_id = h.descendant_node_id
    AND up.marketplace_id     = h.marketplace_id
JOIN transformed.amz_category root
    ON  root.node_id        = up.ancestor_node_id
    AND root.marketplace_id = h.marketplace_id
    AND root.depth          = 0
WHERE h.marketplace_id       = 'amazon_us'
  AND h.depth_from_ancestor  = 0
  AND NOT EXISTS (
      SELECT 1 FROM transformed.amz_category c
      WHERE c.marketplace_id = h.marketplace_id
        AND c.node_id        = h.ancestor_node_id
  )
GROUP BY root.node_name
ORDER BY orphaned_nodes DESC;


-- Do the orphans have ANY descendants in amz_category at all?
SELECT COUNT(DISTINCT orphan.ancestor_node_id) AS orphans_with_known_descendants
FROM transformed.amz_category_hierarchy orphan
JOIN transformed.amz_category_hierarchy lineage
    ON  lineage.ancestor_node_id  = orphan.ancestor_node_id
    AND lineage.marketplace_id    = orphan.marketplace_id
    AND lineage.depth_from_ancestor > 0
JOIN transformed.amz_category child
    ON  child.node_id        = lineage.descendant_node_id
    AND child.marketplace_id = lineage.marketplace_id
WHERE orphan.marketplace_id      = 'amazon_us'
  AND orphan.depth_from_ancestor = 0
  AND NOT EXISTS (
      SELECT 1 FROM transformed.amz_category c
      WHERE c.marketplace_id = orphan.marketplace_id
        AND c.node_id        = orphan.ancestor_node_id
  );

```