# Scraping — Run Reference

All commands run from the `scraping/` directory.  
**MUST READ before building new spiders:** `../docs/scraping_pitfalls.md`

---

## Setup

```bash
cd scraping/

# Option A — activate venv then use bare scrapy/psql commands (shown below)
../.venv_scrape/Scripts/activate          # Windows
# source ../.venv_scrape/bin/activate     # Unix

# Option B — call the venv directly without activating (safer in scripts)
# Replace 'scrapy' with: D:/Documents/projects/GitHub/ecom-intelligence/.venv_scrape/Scripts/scrapy

# Credentials loaded automatically from ../.secrets/admin_creds.env
# Target categories loaded automatically from ../docs/chosen_categories.csv
```

### Playwright setup (one-time)

Required for `use_playwright=true` (the default). Chromium must be installed into the venv's Playwright:

```bash
D:/Documents/projects/GitHub/ecom-intelligence/.venv_scrape/Scripts/python -m playwright install chromium
```

Chromium installs to: `C:\Users\yashl\AppData\Local\ms-playwright\chromium-*`  
Already done as of 2026-06-09 — only needed again if the venv is recreated.

---

## Spider 1 — AmzCategoryHierarchy

Traverses the Amazon bestseller left-nav sidebar and writes the full category
tree to the DB. Run once per market before anything else.

**Writes to:**
- `transformed.amz_category` — one row per node
- `transformed.amz_category_hierarchy` — closure table (all ancestor-descendant pairs)

### Run (all 10 categories)

```bash
scrapy crawl AmzCategoryHierarchy -a marketplace_id=amazon_us \
  -s LOG_FILE=logs/amz_category_hierarchy.log
```

### Run (subset of categories)

Use `-a target_categories=` with pipe-delimited names. Do not use commas — category
names contain commas and would be split incorrectly.

```bash
# Single category
scrapy crawl AmzCategoryHierarchy -a marketplace_id=amazon_us \
  -a "target_categories=Home & Kitchen" \
  -s LOG_FILE=logs/amz_category_hierarchy_hk.log

# Multiple categories
scrapy crawl AmzCategoryHierarchy -a marketplace_id=amazon_us \
  -a "target_categories=Pet Supplies|Office Products" \
  -s LOG_FILE=logs/amz_category_hierarchy_subset.log

# Category names with commas — pipe delimiter handles them correctly
scrapy crawl AmzCategoryHierarchy -a marketplace_id=amazon_us \
  -a "target_categories=Arts, Crafts & Sewing|Clothing, Shoes & Jewelry" \
  -s LOG_FILE=logs/amz_category_hierarchy_subset.log
```

Omit `target_categories` entirely to run all categories in `chosen_categories.csv`.

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `marketplace_id` | `amazon_us` | Target marketplace. Must exist in `transformed.marketplaces`. |
| `target_categories` | *(all from chosen_categories.csv)* | **Pipe-delimited** (`\|`) category names to restrict the run. E.g. `"Home & Kitchen"` or `"Pet Supplies\|Office Products"`. Must match root category names exactly. Do NOT use commas — category names contain commas. |
| `use_playwright` | `true` | `true` — zip 19901 bootstrap sets US geo before nav traversal (required for correct US category tree from non-US IPs). `false` — plain HTTP, no geo fix, may produce wrong hierarchy from non-US IP. |

### Post-run (first time only)

```bash
# Seed the scrape controller from ALL nodes discovered by this spider
# (root + intermediate + leaf). Re-running is safe (ON CONFLICT DO NOTHING).
psql -d ecom_intel -U ecom_intel_admin -f db/seed_controller.sql
```

### Verify

```sql
-- Node counts by depth
SELECT depth, is_leaf, COUNT(*) FROM transformed.amz_category
WHERE marketplace_id = 'amazon_us' GROUP BY depth, is_leaf ORDER BY depth;

-- Check for orphaned nodes (should be 0 after a clean run)
SELECT COUNT(*) FROM transformed.amz_category_hierarchy h
WHERE h.marketplace_id = 'amazon_us' AND h.depth_from_ancestor = 0
  AND NOT EXISTS (
      SELECT 1 FROM transformed.amz_category c
      WHERE c.marketplace_id = h.marketplace_id AND c.node_id = h.ancestor_node_id
  );
```

---

## Spider 2 — AmzRankings

Scrapes bestseller and new-release ranking pages for nodes seeded in
`transformed.amz_category_scrape_controller`. Writes raw output to staging;
run the merge script afterwards to promote to transformed.

Scrapes ranking pages at **every node level** — root, intermediate, and leaf.
Amazon exposes a distinct page for each node; all levels are scraped.
`AmzProducts` reads the resulting ASINs from the queue seeded by `seed_product_queue.sql`
(no is_leaf filter — all ranked ASINs are scraped regardless of node depth).

**Product coverage:**
- `use_playwright=true` (default) — **~100 products/node** (50/page × 2 pages). Playwright scrolls the page to trigger Amazon's lazy-load for items 31-50 before Scrapy reads the HTML.
- `use_playwright=false` — **~60 products/node** (30 static/page × 2 pages). Plain HTTP, faster, no browser overhead. Use for quick re-scrapes or controller seeding verification.

**Writes to:**
- `staging.amz_ranking_snapshot` — raw append (duplicates possible on resume)
- `monitoring.scrape_run_field_stats` — null rates per field per run

**Post-run promote to:**
- `transformed.amz_ranking` — deduplicated permanent store

### Production runs — use `run_rankings.ps1` (Playwright mode)

**Do not run all categories in a single scrapy command when Playwright is on.**
Chromium's V8 heap accumulates JS state across thousands of pages and OOMs after several
hours. `run_rankings.ps1` runs one category at a time — the browser restarts between each,
keeping memory bounded.

**Subcategory / node-level filtering is not supported in `run_rankings.ps1`.**
For subcategory targeting, use the direct scrapy commands below.

#### Sequential — all categories

```powershell
# All 9 categories, bestseller (default)
.\run_rankings.ps1

# All 9 categories, new releases
.\run_rankings.ps1 -ListType new_release
```

#### Sequential — resume after crash

```powershell
# Skip categories already completed; pick up from "Home & Kitchen" onwards
.\run_rankings.ps1 -StartFrom "Home & Kitchen"
.\run_rankings.ps1 -ListType new_release -StartFrom "Pet Supplies"
```

#### Single category

```powershell
.\run_rankings.ps1 -Category "Pet Supplies"
.\run_rankings.ps1 -Category "Pet Supplies" -ListType new_release
.\run_rankings.ps1 -Category "Arts, Crafts & Sewing"
```

#### Multiple specific categories (pipe-delimited, runs sequentially)

```powershell
.\run_rankings.ps1 -Categories "Pet Supplies|Office Products"
.\run_rankings.ps1 -Categories "Pet Supplies|Office Products" -ListType new_release

# Category names that contain commas work fine with pipe delimiter
.\run_rankings.ps1 -Categories "Arts, Crafts & Sewing|Home & Kitchen|Pet Supplies"
.\run_rankings.ps1 -Categories "Arts, Crafts & Sewing|Clothing, Shoes & Jewelry" -ListType new_release
```

Each category writes its own log: `logs/amz_rankings_<list_type>_<category>.log`.
After all categories finish, run the merge script once:

```bash
psql -d ecom_intel -U ecom_intel_admin -f db/merge_rankings.sql
```

---

### Direct scrapy commands

Use these for subcategory/node-level filtering, plain-HTTP fast mode, or any scenario
the script doesn't cover. The `categories` parameter accepts **pipe-delimited (`|`)** node
names — works for top-level categories, intermediate nodes, and leaf nodes alike.
Do NOT use commas as delimiter: names like `"Arts, Crafts & Sewing"` contain commas and
would be split silently.

#### Sequential — all pending nodes

```bash
# All categories, all pending nodes — Playwright on (production quality)
scrapy crawl AmzRankings -a list_type=bestseller \
  -s LOG_FILE=logs/amz_rankings_bestseller.log

# Fast mode — plain HTTP, ~60 products/node, no OOM risk, safe to run all at once
scrapy crawl AmzRankings -a list_type=bestseller -a use_playwright=false \
  -s LOG_FILE=logs/amz_rankings_bestseller_fast.log
```

#### Single category

```bash
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Office Products" \
  -s LOG_FILE=logs/amz_rankings_office.log

# Category name with a comma — quote the whole -a value
scrapy crawl AmzRankings -a list_type=bestseller \
  -a "categories=Arts, Crafts & Sewing" \
  -s LOG_FILE=logs/amz_rankings_arts.log
```

#### Multiple categories

```bash
scrapy crawl AmzRankings -a list_type=bestseller \
  -a "categories=Office Products|Pet Supplies" \
  -s LOG_FILE=logs/amz_rankings_multi.log

# With comma-containing names
scrapy crawl AmzRankings -a list_type=bestseller \
  -a "categories=Arts, Crafts & Sewing|Clothing, Shoes & Jewelry" \
  -s LOG_FILE=logs/amz_rankings_multi.log
```

#### Single subcategory (intermediate or leaf node)

The `categories` parameter matches any node name — not just top-level categories.
Pass an intermediate or leaf node name and the spider resolves it via the DB.

```bash
# Scrape "Staplers" + its full subtree (include_descendants=true is the default)
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Staplers" \
  -s LOG_FILE=logs/amz_rankings_staplers.log

# Scrape only the "Staplers" page itself — no subtree
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Staplers" -a include_descendants=false \
  -s LOG_FILE=logs/amz_rankings_staplers.log

# Intermediate node — scrapes "Dogs" + all leaf nodes beneath it
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Dogs" \
  -s LOG_FILE=logs/amz_rankings_dogs.log
```

#### Multiple subcategories

```bash
# Two leaf nodes — scrape both pages
scrapy crawl AmzRankings -a list_type=bestseller \
  -a "categories=Staplers|Pens" \
  -s LOG_FILE=logs/amz_rankings_multi_sub.log

# Two intermediate nodes — scrape both subtrees
scrapy crawl AmzRankings -a list_type=bestseller \
  -a "categories=Dogs|Cats" \
  -s LOG_FILE=logs/amz_rankings_multi_sub.log

# Mix of levels — include_descendants expands each named node independently
scrapy crawl AmzRankings -a list_type=bestseller \
  -a "categories=Staplers|Dogs|Office Products" \
  -s LOG_FILE=logs/amz_rankings_mixed.log
```

### `use_playwright` flag

| `use_playwright` | Products/node | Speed | Use when |
|---|---|---|---|
| `true` (default) | ~100 | Slower — real browser per page | Production runs, full data needed |
| `false` | ~60 | Faster — plain HTTP | Quick re-scrapes, debugging, controller checks |

```bash
# Full coverage (default — Playwright on)
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Office Products"

# Fast mode — plain HTTP
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Office Products" -a use_playwright=false
```

### `include_descendants` flag

| `include_descendants` | Behaviour |
|---|---|
| `true` (default) | Scrapes the named node + every node in its subtree (root, intermediate, and leaf) |
| `false` | Scrapes only the named node itself — no subtree expansion |

```bash
# Entire Office Products tree — root + every intermediate + every leaf
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Office Products"

# Only the "Office Products" root page, nothing below
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Office Products" -a include_descendants=false
```

### Force re-scrape already-scraped nodes

```bash
# min_days_since_last_scrape=0 re-queues complete nodes regardless of age
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Office Products" -a min_days_since_last_scrape=0 \
  -s LOG_FILE=logs/amz_rankings_office_rescrape.log
```

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `list_type` | `bestseller` | `bestseller` or `new_release` |
| `marketplace_id` | `amazon_us` | Target marketplace |
| `categories` | *(all)* | **Pipe-delimited** (`\|`) node names to filter on. E.g. `"Pet Supplies"` or `"Pet Supplies\|Office Products"`. Must match `amz_category.node_name` exactly. Omit to scrape all pending nodes. Do NOT use commas — category names contain commas and the value would be split incorrectly. |
| `include_descendants` | `true` | `true` — scrape named node + full subtree (all depths). `false` — scrape only the named node itself, no expansion. Works for any node type. |
| `min_days_since_last_scrape` | `1` | Nodes with `last_scraped_at` older than this many days are re-queued. Set to `0` to force re-scrape of everything in scope. |
| `use_playwright` | `true` | `true` — Chromium renders each page + scrolls to trigger lazy-load; ~100 products/node. `false` — plain HTTP, ~60 products/node, no browser overhead. |

### Post-run — promote staging → transformed

```bash
psql -d ecom_intel -U ecom_intel_admin -f db/merge_rankings.sql
```

The merge script is idempotent — running it multiple times is safe.
It does not truncate staging automatically; do that manually once confident:

```sql
TRUNCATE staging.amz_ranking_snapshot;
```

### Verify

```sql
-- Products written in the latest run
SELECT list_type, category, COUNT(*) as products
FROM staging.amz_ranking_snapshot
WHERE run_id = (SELECT MAX(run_id::text)::uuid FROM staging.amz_ranking_snapshot)
GROUP BY list_type, category ORDER BY category;

-- Null rates from monitoring (latest run)
SELECT field_name, total_records, null_count, null_rate
FROM monitoring.scrape_run_field_stats
WHERE run_id = (SELECT run_id FROM monitoring.scrape_run_field_stats ORDER BY run_date DESC LIMIT 1)
ORDER BY null_rate DESC;

-- Controller status after run
SELECT list_type, scrape_status, COUNT(*) FROM transformed.amz_category_scrape_controller
WHERE marketplace_id = 'amazon_us' GROUP BY list_type, scrape_status ORDER BY list_type, scrape_status;
```

---

## Spider 3 — AmzProducts

Reads ASINs from `transformed.amz_product_scrape_queue`, scrapes the product detail
page for each, and writes a snapshot to `staging.amz_product_snapshot`.

The queue is seeded from `transformed.amz_ranking` with no `is_leaf` filter — all
ranked ASINs are included regardless of node depth, because AmzRankings scrapes
root, intermediate, and leaf nodes and all may produce valid products.

- Requires Playwright — sets zip 19901 via the Amazon location popover before
  loading any product pages (required for buybox fields: price, seller, is_fba).
- Deletes each queue row on successful scrape.
- Discovers variant ASINs (color/size siblings) and inserts them into the queue
  automatically — they will be scraped in the same run if the queue is seeded
  before the spider finishes, or in the next run.

**Reads from:**
- `transformed.amz_product_scrape_queue` — ASINs to scrape (seeded by `seed_product_queue.sql`)

**Writes to:**
- `staging.amz_product_snapshot` — one row per ASIN, UPSERT on re-scrape
- `monitoring.scrape_run_field_stats` — null rates per field for this run

### Pre-requisite — seed the product queue

Run after both rankings merge cycles (bestseller + new_release) are done:

```bash
psql -d ecom_intel -U ecom_intel_admin -f db/seed_product_queue.sql
```

### Run (standard)

```bash
scrapy crawl AmzProducts -a marketplace_id=amazon_us \
  -s LOG_FILE=logs/amz_products.log
```

### Run with HTML archiving (debugging / selector validation)

Saves rendered HTML to `scraping/html_archive/<marketplace_id>_<asin>.html` (1.5–2.5 MB each).
Off by default — only enable when diagnosing field extraction issues.

```bash
scrapy crawl AmzProducts -a marketplace_id=amazon_us -a save_html=true \
  -s LOG_FILE=logs/amz_products.log
```

Custom archive directory:

```bash
scrapy crawl AmzProducts -a marketplace_id=amazon_us -a save_html=true \
  -a html_archive_dir=D:/tmp/amz_html \
  -s LOG_FILE=logs/amz_products.log
```

### Run a limited batch (testing / spot checks)

```bash
# Scrape only the first 5 ASINs from the queue
scrapy crawl AmzProducts -a marketplace_id=amazon_us -a limit=5 \
  -s LOG_FILE=logs/amz_products_test.log
```

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `marketplace_id` | `amazon_us` | Target marketplace. Must match queue rows. |
| `use_playwright` | `true` | Must stay `true` for production — Playwright is required for the zip bootstrap and buybox fields. `false` skips the zip set and will return NULL price/seller/is_fba. |
| `limit` | *(all)* | Stop after this many ASINs. Useful for smoke tests. |
| `save_html` | `false` | Save rendered page HTML to disk for each scraped ASIN. |
| `html_archive_dir` | `scraping/html_archive/` | Directory for saved HTML files. Created if absent. Ignored when `save_html=false`. |

### Post-run validation

```bash
psql -d ecom_intel -U ecom_intel_admin -f db/validate_product_run.sql
```

All checks must return 0 rows. The summary at the bottom shows pass/fail counts.
Key things it checks:

| Tier | Check |
|---|---|
| 2 | Null rate per field vs. threshold (e.g. title < 5%, rating < 15%, last_month_sales < 65%) |
| 3a | Price ≤ 0 or > $5,000 |
| 3b | Rating outside 1.0–5.0 |
| 3c | BSR rank > 5,000,000 (indicates regex parse failure) |
| 3d | Variant ASIN not matching `^[A-Z0-9]{10}$` |
| 3e | Title NULL or contains "robot"/"captcha" (bot-block detection) |
| 3f | Variant entries missing dimension labels (variationValues parse failure) |
| 4 | Queue not fully drained (remaining rows = failed scrapes) |

### Verify

```sql
-- Snapshot count and field null rates for the latest run
SELECT field_name, null_count, total_records,
       round(null_rate * 100, 1) AS null_pct
FROM monitoring.scrape_run_field_stats
WHERE run_id = (
    SELECT run_id FROM monitoring.scrape_run_field_stats
    ORDER BY run_date DESC, run_id DESC LIMIT 1
)
ORDER BY null_rate DESC;

-- Snapshot row count (total products scraped, all time)
SELECT marketplace_id, COUNT(*) AS total_products,
       MAX(last_captured_at) AS latest_scrape
FROM staging.amz_product_snapshot
GROUP BY marketplace_id;

-- Queue remaining (should be 0 after a clean run)
SELECT COUNT(*) AS remaining FROM transformed.amz_product_scrape_queue;

-- Sample: check a specific product
SELECT asin, title, brand, price, rating, review_count,
       is_fba, seller_name, last_month_sales,
       jsonb_array_length(bsr_entries) AS bsr_categories,
       jsonb_array_length(variant_asins) AS variant_count
FROM staging.amz_product_snapshot
WHERE asin = 'B0BZYCJK89';
```

---

## Run order (Phase 0)

**Current path (bestseller only):** new_release rankings are deferred — will be added in a future run
once the bestseller product scrape is complete and validated.

```
1. AmzCategoryHierarchy              (once per market)
2. validate_hierarchy.sql            (MUST pass — all 6 checks must return 0 rows)
3. seed_controller.sql               (once, after hierarchy spider)
4. AmzRankings  list_type=bestseller
5. merge_rankings.sql
6. seed_product_queue.sql            (after bestseller merge)
7. AmzProducts                       (reads queue, writes amz_product_snapshot)
8. validate_product_run.sql          (MUST pass — all checks return 0 rows)
```

**Full sequence (bestseller path) — copy-paste commands (run from `scraping/` in PowerShell):**

```powershell
# 1. Category hierarchy
scrapy crawl AmzCategoryHierarchy -a marketplace_id=amazon_us `
  -s LOG_FILE=logs/amz_category_hierarchy.log

# 2. Validate hierarchy (all 6 checks must return 0 rows)
psql -d ecom_intel -U ecom_intel_admin -f db/validate_hierarchy.sql

# 3. Seed the scrape controller
psql -d ecom_intel -U ecom_intel_admin -f db/seed_controller.sql

# 4+5. Bestseller rankings + merge
.\run_rankings.ps1
psql -d ecom_intel -U ecom_intel_admin -f db/merge_rankings.sql

# 6. Seed product queue
psql -d ecom_intel -U ecom_intel_admin -f db/seed_product_queue.sql

# 7. Scrape product pages
scrapy crawl AmzProducts -a marketplace_id=amazon_us `
  -s LOG_FILE=logs/amz_products.log

# 8. Validate product run
psql -d ecom_intel -U ecom_intel_admin -f db/validate_product_run.sql
```

**Future — when new_release rankings are needed:**

```powershell
# Run after the bestseller product scrape is complete and validated.
# Truncate staging before the new_release run to avoid reprocessing bestseller rows in merge.
.\run_rankings.ps1 -ListType new_release
psql -d ecom_intel -U ecom_intel_admin -f db/merge_rankings.sql
# Re-seed queue and re-run AmzProducts to pick up new ASINs from new_release rankings
psql -d ecom_intel -U ecom_intel_admin -f db/seed_product_queue.sql
scrapy crawl AmzProducts -a marketplace_id=amazon_us -s LOG_FILE=logs/amz_products_nr.log
```

### validate_hierarchy.sql — checks and what they catch

| Check | What it catches |
|---|---|
| 1 — Root node count = 9 | Spider missed target categories on root page |
| 2 — All 9 categories present | Same as above, but names the missing ones |
| 3 — No shared root node_ids | Multiple root categories overwriting each other (shared URL slug bug) |
| 4 — No node under multiple roots | Closure table cross-contamination — node attributed to wrong category |
| 5 — Every node reachable from a root | Orphaned nodes that seed_controller would skip entirely |
| 6 — Every node has self-reference row | Incomplete closure records — seed_controller JOIN would miss nodes |

All checks must return 0 rows. The summary at the end shows node counts per category — any category with suspiciously low counts (e.g. 0 leaf nodes) indicates traversal contamination even if individual checks pass.

```bash
psql -d ecom_intel -U ecom_intel_admin -f db/validate_hierarchy.sql
```

---

## Log files

Logs go to `scraping/logs/` (gitignored). Use timestamped filenames to avoid
Scrapy appending to a previous run's log:

```bash
scrapy crawl AmzRankings -a list_type=bestseller \
  -s LOG_FILE=logs/amz_rankings_bestseller_$(date +%Y%m%d_%H%M%S).log
```

On Windows (PowerShell):

```powershell
scrapy crawl AmzRankings -a list_type=bestseller `
  -s LOG_FILE=logs/amz_rankings_bestseller_$(Get-Date -Format yyyyMMdd_HHmmss).log
```
