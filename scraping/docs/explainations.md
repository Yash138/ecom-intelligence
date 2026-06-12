# How amz_category_hierarchy table works


## How `amz_category` was populated

The `AmzCategoryHierarchy` spider traversed Amazon's bestseller left-nav sidebar. For every category link it found, it inserted one row into `amz_category`:

```
Pet Supplies           → node_id='pet-supplies',  depth=0, parent=NULL,         is_leaf=False
  Dogs                 → node_id='2975312011',     depth=1, parent='pet-supplies', is_leaf=False
    Apparel            → node_id='2975313011',     depth=2, parent='2975312011',  is_leaf=False
      Dog Shirts       → node_id='some_id',        depth=3, parent='2975313011',  is_leaf=True
```

Your data: 10 root nodes (depth=0), then 8,772 more nodes across depths 1–51. `is_leaf=True` means the spider found no further children under that node. These leaf nodes are what the rankings spider will scrape.

---

## What `ancestor_node_id` and `descendant_node_id` mean

A closure table stores **every ancestor-descendant pair** in the tree — not just direct parent-child. For any node X, there is one row per ancestor it has, including itself.

Using the `Hardware` leaf node example from your DB (`node_id='511228'`, under Tools & Home Improvement):

```
ancestor_node_id  │ descendant_node_id │ depth_from_ancestor
──────────────────┼────────────────────┼────────────────────
511228            │ 511228             │ 0    ← self-reference: "Hardware is 0 hops from itself"
hi                │ 511228             │ 1    ← "Tools & Home Improvement is 1 hop above Hardware"
```

Read each row as: **"ancestor_node_id is `depth_from_ancestor` hops above descendant_node_id."**

A deeper example — if you had `Pet Supplies → Dogs → Apparel → Dog Shirts`, the closure table rows for `Dog Shirts` would be:

```
ancestor_node_id  │ descendant_node_id │ depth_from_ancestor
──────────────────┼────────────────────┼────────────────────
dog-shirts-id     │ dog-shirts-id      │ 0   ← self
apparel-id        │ dog-shirts-id      │ 1   ← direct parent
2975312011        │ dog-shirts-id      │ 2   ← Dogs (grandparent)
pet-supplies      │ dog-shirts-id      │ 3   ← root (great-grandparent)
```

---

## The 9,063 depth=0 rows

`depth_from_ancestor=0` = self-reference rows. Every node has exactly one: `ancestor = descendant = itself`. So 9,063 self-rows for 8,782 nodes means **281 extra rows**. That's a data integrity issue — there are 281 nodes recorded in the hierarchy table that are missing from `amz_category`. Let me verify:

Confirmed. 281 real orphan nodes. All depth=0 rows are self-references (no data corruption there). Let me flag that issue too.

---

## How to read the table overall

**Rule: read every row as: "It takes `depth_from_ancestor` hops to get from `ancestor_node_id` down to `descendant_node_id`."**

From your actual data for `pet-supplies`:

```
ancestor_node_id │ descendant_node_id │ depth_from_ancestor
─────────────────┼────────────────────┼────────────────────
pet-supplies     │ pet-supplies       │ 0   ← self
pet-supplies     │ 2975312011 (Dogs)  │ 1   ← direct child
pet-supplies     │ 2975313011 (Apparel)│ 2  ← grandchild
pet-supplies     │ some-leaf-id       │ 3+  ← great-grandchild etc.
```

The table has **189,230 total rows** because every node gets one row per ancestor (including itself). A node at depth 30 in the tree generates 31 rows (itself + 30 ancestors).

**Useful queries:**

```sql
-- Get full path from root to a specific node
SELECT a.node_name, h.depth_from_ancestor
FROM transformed.amz_category_hierarchy h
JOIN transformed.amz_category a ON a.node_id = h.ancestor_node_id
WHERE h.descendant_node_id = '2975313011'  -- Dogs > Apparel
  AND h.marketplace_id = 'amazon_us'
ORDER BY h.depth_from_ancestor DESC;
-- Returns: Pet Supplies (2 hops up) → Dogs (1 hop up) → Apparel (self)

-- Get ALL leaf nodes under Pet Supplies (for the rankings spider)
SELECT c.node_id, c.node_name
FROM transformed.amz_category c
JOIN transformed.amz_category_hierarchy h ON h.descendant_node_id = c.node_id
WHERE h.ancestor_node_id = 'pet-supplies'
  AND c.is_leaf = TRUE
  AND c.marketplace_id = 'amazon_us';
```

---

## The 281 orphan issue

There are 281 nodes that exist as ancestors in the hierarchy table but have no row in `amz_category`. They were part of traversal paths (in the `ancestor_chain` meta) but their own category row is missing.

Since they appear as ancestors, they're intermediate nodes (not leaves) — the rankings spider seeds from `is_leaf = TRUE` nodes in `amz_category`, so it won't be affected. But the tree has gaps.

This was caused by a spider bug from the earlier problematic runs (before the rollback fix and the slug filter fix). The final clean run wrote all nodes it visited, but these 281 nodes were written as ancestors in closure rows by the clean run's descendants — yet their own rows were never committed because they'd been partially processed in a prior aborted run and the node_id dedup in the clean run skipped re-visiting them.

Fix: re-run `AmzCategoryHierarchy` to fill the gaps. It's idempotent — upserts on both tables. The 281 missing `amz_category` rows will be inserted, and the closure table will be updated. This should be done before seeding the controller.