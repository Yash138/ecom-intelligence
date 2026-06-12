# Scraping Pitfalls — Lessons Learned

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-06-07 | All | Initial creation — 6 hard bugs hit during AmzCategoryHierarchy development |
| 2026-06-13 | TOC, P21 | Added P21 — shared URL slug root node_id collision; discovered during AmzRankings contamination investigation |

## Table of Contents

- [P1 — Scrapy 2.13+ start() method](#p1--scrapy-213-start-method)
- [P2 — Amazon URL structure changed to /zgbs/](#p2--amazon-url-structure-changed-to-zgbs)
- [P3 — Amazon selector changed from role=treeitem](#p3--amazon-selector-changed-from-roletreeitem)
- [P4 — Cross-category nav drift](#p4--cross-category-nav-drift)
- [P5 — psycopg2 aborted transaction cascade](#p5--psycopg2-aborted-transaction-cascade)
- [P6 — ON CONFLICT cardinality violation in batch upserts](#p6--on-conflict-cardinality-violation-in-batch-upserts)
- [P7 — pkill does not work on Windows](#p7--pkill-does-not-work-on-windows)
- [P21 — Shared URL slug causes root node_id collision](#p21--shared-url-slug-causes-root-node-id-collision)

---

## P1 — Scrapy 2.13+ start() method

**Symptom:** Spider starts and immediately closes with 0 pages crawled. No HTTP request made. No error.

**Cause:** Scrapy 2.13 introduced `StartSpiderMiddleware` which calls `spider.start()` (async generator). The old `start_requests()` method is ignored silently.

**Fix:**
```python
# WRONG — silently ignored in Scrapy 2.13+
def start_requests(self):
    yield scrapy.Request(url=self.BASE_URL, callback=self.parse_root)

# CORRECT
async def start(self):
    yield scrapy.Request(url=self.BASE_URL, callback=self.parse_root)
```

**Rule: All new spiders must use `async def start(self)` not `start_requests()`.**

---

## P2 — Amazon URL structure changed to /zgbs/

**Symptom:** Category nodes have `node_id=None`, links not followed, 0 nodes written.

**Cause:** Amazon changed bestseller URLs from `/gp/bestsellers/<slug>/<id>` to `/Best-Sellers-<Title>/zgbs/<slug>/<id>`. Any URL helper built on the old pattern breaks silently.

**New pattern:**
```
Root:    /Best-Sellers-Arts-Crafts-Sewing/zgbs/arts-crafts/
Child:   /Best-Sellers-Pet-Supplies-Dogs/zgbs/pet-supplies/2975312011/
```

**Fix:** Parse `zgbs` as the anchor segment, not `bestsellers`. Always verify actual page URL structure before writing URL helpers — do not assume from old code.

---

## P3 — Amazon selector changed from role=treeitem

**Symptom:** `parse_root` finds 0 matching categories. `WARNING: No target categories found on root page.`

**Cause:** Amazon redesigned the bestseller nav. `role="treeitem"` no longer exists on any element. Category links are now plain `<li>` elements with a hashed CSS class containing `zg-browse-item`.

**Fix:**
```python
# WRONG
response.xpath("//*[@role='treeitem']")

# CORRECT — matches class containing 'zg-browse-item' regardless of hash suffix
response.css('[class*="zg-browse-item"]')
```

**Rule: Never rely on ARIA roles or stable class names on Amazon. Always fetch and inspect the live page before writing selectors. Use substring class selectors (`*=`) for resilience.**

---

## P4 — Cross-category nav drift

**Symptom:** Spider runs for 18+ hours, reaches depth 50–100+, scrapes thousands of nodes from categories not in the target list (e.g. `videogames`, `automotive`).

**Cause:** Amazon renders ALL root category links in the left nav on every page — including categories we never targeted. Following every `zg-browse-item` link without filtering pulls in the entire Amazon catalog.

**Fix:** In `parse_root`, collect the URL slugs of matched categories into `self.target_slugs`. In `parse`, skip any link whose slug is not in `self.target_slugs`:
```python
url_slug = self._slug(url)
if self.target_slugs and url_slug not in self.target_slugs:
    continue
```

**Rule: Any spider that follows nav links must explicitly constrain traversal to the target scope. Never follow all visible links on a page without a scope check.**

Also dedup by `node_id` (not just URL) — same node can be reachable via different URL paths:
```python
if node_id in self.visited_node_ids:
    continue
self.visited_node_ids.add(node_id)
```

---

## P5 — psycopg2 aborted transaction cascade

**Symptom:** After one DB error, all subsequent DB operations fail with `InFailedSqlTransaction: current transaction is aborted, commands ignored until end of transaction block`.

**Cause:** psycopg2 does not auto-rollback on error. When any operation fails, the connection stays in an aborted transaction state. Every subsequent call on the same connection fails until an explicit rollback.

**Fix:** Wrap every DB operation in try/except with rollback:
```python
try:
    with self.connection.cursor() as cur:
        execute_values(cur, query, values)
    self.connection.commit()
except Exception:
    self.connection.rollback()
    raise
```

**Rule: Every method in `PostgresDBHandler` that executes SQL must have a `rollback()` in the except block. This is already implemented — do not remove it.**

---

## P6 — ON CONFLICT cardinality violation in batch upserts

**Symptom:** `psycopg2.errors.CardinalityViolation: ON CONFLICT DO UPDATE command cannot affect row a second time`

**Cause:** `execute_values` with `ON CONFLICT DO UPDATE` fails if the batch itself contains two rows with the same conflict key. Amazon's nav can cause this in the closure table: a node that appears in its own ancestor chain generates both a self-reference `(node_id, node_id, 0)` and a duplicate ancestor pair `(node_id, node_id, distance)`.

**Fix:** Deduplicate the batch by conflict key before inserting:
```python
seen_pairs = set()
closure_records = []
for ancestor_id, distance in ...:
    pair = (ancestor_id, node_id)
    if pair in seen_pairs:
        continue
    seen_pairs.add(pair)
    closure_records.append(...)
```

**Rule: Any batch upsert must be deduplicated by the conflict key before calling `execute_values`. Never assume input data is already deduplicated.**

---

## P7 — pkill does not work on Windows

**Symptom:** `pkill -f "scrapy crawl"` exits 0 but spider keeps running. DB continues being written.

**Cause:** `pkill` is a Unix command. On Windows via Git Bash, it silently does nothing.

**Fix:** Kill by PID using wmic:
```bash
wmic process where "commandline like '%AmzCategoryHierarchy%' and name like '%python%'" delete
```

Or use Task Manager → Details tab → kill `python.exe` / `scrapy.exe` processes.

**Rule: Never use `pkill` in runbooks or scripts targeting Windows. Document Windows-specific kill commands explicitly.**

---

## P21 — Shared URL slug causes root node_id collision

**Symptom:** After running `AmzCategoryHierarchy`, running `AmzRankings` for a single category (e.g. Tools & Home Improvement) also scrapes nodes from other categories (e.g. Kitchen & Dining, Home & Kitchen). `amz_category_scrape_controller` assigns subcategories from multiple roots to a single category name. All three categories show today's `last_scraped_at` even though only one was requested.

**Cause:** `_node_id(url)` returns the URL slug for root-level URLs (no numeric ID in the path). Amazon assigns the same slug `/hi/` to Home & Kitchen, Kitchen & Dining, and Tools & Home Improvement. All three roots call `_write_node(node_id='hi', node_name=..., ...)` — each overwrites the previous row in `amz_category`. The spider completes with only one root row surviving. `seed_controller.sql` then joins via the closure table and assigns all subcategories under the shared slug to that single root's category name, corrupting the controller for all three categories.

**Fix in `AmzCategoryHierarchy.parse_root()`:** Use the category display name as node_id instead of `_node_id(url)`:
```python
# WRONG — returns URL slug 'hi' for all three shared-slug categories
node_id = self._node_id(url)

# CORRECT — category name is unique across all 10 targets and fits VARCHAR(30)
node_id = name
```

**Companion fix in `AmzRankings._build_url()`:** The URL construction check must use `isdigit()` not `node_id == url_slug`. String root node_ids (category names like "Home & Kitchen") do not appear in the URL — only numeric subcategory IDs do:
```python
# WRONG — fails when node_id is 'Home & Kitchen' (not equal to url_slug 'hi')
if node_id == url_slug:
    return f'{base}/{url_slug}'
return f'{base}/{url_slug}/{node_id}'

# CORRECT
if node_id.isdigit():
    return f'{base}/{url_slug}/{node_id}'
return f'{base}/{url_slug}'
```

**Rule: Never use `_node_id(url)` (slug-based ID) for root nodes. Root node_ids must be unique across all target categories regardless of shared URL slug. Always run `validate_hierarchy.sql` CHECK 3 after `AmzCategoryHierarchy` to detect this collision before seeding the controller.**
