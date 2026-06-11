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

### Run (single category only — for targeted re-runs / gap fills)

Temporarily edit `../docs/chosen_categories.csv` to contain only the target
category, run the spider, then restore the file.

```bash
# Example: fix orphaned nodes in Tools & Home Improvement only
# 1. Edit chosen_categories.csv → one line: Tools & Home Improvement
scrapy crawl AmzCategoryHierarchy -a marketplace_id=amazon_us \
  -s LOG_FILE=logs/amz_category_hierarchy_fix.log
# 2. Restore chosen_categories.csv
```

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `marketplace_id` | `amazon_us` | Target marketplace. Must exist in `transformed.marketplaces`. |

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
`AmzProducts` (future) uses the same controller but filters to leaf nodes only at query time.

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

```powershell
# Full bestseller run — all 10 categories sequentially (default)
.\run_rankings.ps1

# New releases
.\run_rankings.ps1 -ListType new_release

# Resume after a crash — skip categories already completed
.\run_rankings.ps1 -StartFrom "Home & Kitchen"

# Resume new releases from a specific category
.\run_rankings.ps1 -ListType new_release -StartFrom "Pet Supplies"

# Single category only — runs and stops
.\run_rankings.ps1 -Category "Pet Supplies"
.\run_rankings.ps1 -Category "Pet Supplies" -ListType new_release
```

Each category writes its own log: `logs/amz_rankings_<list_type>_<category>.log`.
After all categories finish, run the merge script once:

```bash
psql -d ecom_intel -U ecom_intel_admin -f db/merge_rankings.sql
```

---

### Single-category / debug runs (direct scrapy)

```bash
# Single category test — Playwright on
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Office Products" \
  -s LOG_FILE=logs/amz_rankings_office_bestseller.log

# Fast mode — plain HTTP, ~60 products/node, no Playwright overhead
# Safe to run all categories at once (no browser, no OOM risk)
scrapy crawl AmzRankings -a list_type=bestseller -a use_playwright=false \
  -s LOG_FILE=logs/amz_rankings_bestseller_fast.log
```

### Filter by category

The `categories` parameter accepts one or more node names, **pipe-delimited (`|`)**.
Commas cannot be used as the delimiter because category names themselves contain commas
(e.g. `"Arts, Crafts & Sewing"`). Using a comma would silently split the name and match nothing.

```bash
# Single category — all nodes in its subtree
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Office Products" \
  -s LOG_FILE=logs/amz_rankings_office_bestseller.log

# Category name that contains a comma — works fine with pipe delimiter
scrapy crawl AmzRankings -a list_type=bestseller \
  -a "categories=Arts, Crafts & Sewing" \
  -s LOG_FILE=logs/amz_rankings_arts.log

# Multiple categories — pipe-delimited
scrapy crawl AmzRankings -a list_type=bestseller \
  -a "categories=Office Products|Pet Supplies" \
  -s LOG_FILE=logs/amz_rankings_multi.log

# Multiple categories where names contain commas
scrapy crawl AmzRankings -a list_type=bestseller \
  -a "categories=Arts, Crafts & Sewing|Clothing, Shoes & Jewelry" \
  -s LOG_FILE=logs/amz_rankings_multi.log

# Intermediate or leaf node by name — scrapes that subtree
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Staplers" \
  -s LOG_FILE=logs/amz_rankings_staplers.log
```

### `use_playwright` flag

| `use_playwright` | Products/node | Speed | Use when |
|---|---|---|---|
| `true` (default) | ~100 | Slower — real browser per page | Production runs, full data needed |
| `false` | ~60 | Faster — plain HTTP | Quick re-scrapes, debugging, controller checks |

```bash
# Full coverage (default — Playwright on)
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Carriers & Travel Products" -a include_descendants=false

# Fast mode — skip lazy-loaded items, plain HTTP
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Carriers & Travel Products" -a include_descendants=false \
  -a use_playwright=false
```

### `include_descendants` flag

| `include_descendants` | Behaviour |
|---|---|
| `true` (default) | Scrapes the named node + every node in its subtree (root, intermediate, and leaf) |
| `false` | Scrapes only the named node itself — no subtree expansion |

```bash
# All nodes under Office Products — root page + every intermediate + every leaf
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Office Products"

# Only the "Office Products" root page, nothing below it
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Office Products" -a include_descendants=false

# All nodes under the "Dogs" subcategory (intermediate node)
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Dogs"

# Only the "Dogs" page itself
scrapy crawl AmzRankings -a list_type=bestseller \
  -a categories="Dogs" -a include_descendants=false
```

### Force re-scrape already-scraped nodes

```bash
# min_days_since_last_scrape=0 marks complete nodes as eligible again
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

## Run order (Phase 0)

```
1. AmzCategoryHierarchy          (once per market)
2. seed_controller.sql           (once, after hierarchy spider)
3. AmzRankings list_type=bestseller
4. AmzRankings list_type=new_release
5. merge_rankings.sql            (after each rankings run)
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
