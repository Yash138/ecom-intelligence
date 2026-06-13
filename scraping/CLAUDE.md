# Scraping — Context for Claude

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-06-09 | All | Initial creation — spider architecture, controller pattern, key pitfalls P8–P18, settings |
| 2026-06-13 | Spider Architecture, Key Pitfalls, Docs | AmzProducts added; P19–P22 documented (contamination bugs, networkidle); new spider process updated |
| 2026-06-13 | AmzProducts Design, Key Pitfalls | Corrected related_asins selector (FBT widget); added brand/seller/is_fba fallbacks for Amazon-sold products; P23 added (bootstrap wait_for_selector) |
| 2026-06-13 | AmzProducts Design, Key Pitfalls | Fixed last_month_sales (split-text badge, P24); fixed variant_asins to include color/size labels via variationValues parsing |
| 2026-06-13 | Key Pitfalls, Spider Architecture | P25: window.scrollTo does not trigger Amazon IntersectionObserver lazy-load; fixed with iterative scrollIntoView IIFE in _playwright_meta() |

## Spider Architecture

Three spiders, run in order:

1. **`AmzCategoryHierarchy`** — one-time. Scrapes Amazon's browse node tree → `transformed.amz_category` + `transformed.amz_category_hierarchy`. 8,782 nodes written. Complete.
2. **`AmzRankings`** — scrapes bestseller/new-releases pages for all nodes. Reads pending nodes from `transformed.amz_category_scrape_controller`. Writes products to `staging.amz_ranking_snapshot`.
3. **`AmzProducts`** — **built 2026-06-13.** Scrapes product detail pages for ASINs from `transformed.amz_product_scrape_queue`. Writes to `staging.amz_product_snapshot`.

## AmzProducts — Design Decisions

### Source of ASINs
Queue table `transformed.amz_product_scrape_queue`. Seeded via `db/seed_product_queue.sql`.

**No `is_leaf` filter** on queue seeding — `AmzRankings` scrapes all node levels (root + intermediate + leaf), so filtering to `is_leaf=TRUE` would silently exclude valid ASINs. Seed query uses `DISTINCT ON (marketplace_id, asin)` over `transformed.amz_ranking` with no category join.

### Queue Table Design
`transformed.amz_product_scrape_queue` (DDL: `db/ddl.sql` §8a):
- **Delete-on-success** pattern: row deleted after confirmed scrape + DB write.
- **No scrape_status column**: job is either pending (in queue) or done (deleted).
- Variant ASINs discovered during scraping are inserted (`ON CONFLICT DO NOTHING`).
- `added_at` timestamp for ordering and debugging.

### Product Snapshot Table
`staging.amz_product_snapshot` (DDL: `db/ddl.sql` §8b):
- One row per `(marketplace_id, asin)`. UPSERT semantics.
- `first_captured_at` — set on INSERT, **never updated** (SCD2 foundation).
- `last_captured_at` — updated on every re-scrape.
- No transformation in staging: all fields stored as raw text/values. Type conversion happens in the transformed layer.

### Zip Code / Buybox Setup
Zip code `19901` must be set before product pages load. Without it Amazon geo-blocks and the buybox (price, seller_name, seller_id, is_fba) does not render.

Implementation: **bootstrap request** to `amazon.com` before any product pages:
1. `start()` yields one request to `amazon.com` with `PageMethod` steps for the location popover.
2. Cookies with zip preference persist to all subsequent product pages in the same Playwright context.
3. Product queue is loaded from DB and yielded in the bootstrap callback `_load_product_queue()` — guarantees zip is set before any product page is requested.

Popover flow:
```python
PageMethod('click', '#glow-ingress-block'),
PageMethod('fill', '#GLUXZipUpdateInput', '19901'),
PageMethod('click', 'span#GLUXZipUpdate input.a-button-input'),
```

### Fields and Selectors (confirmed 2026-06-13)

**Static** (always present in plain HTTP response):

| Field | Selector / Method |
|---|---|
| `title` | `#productTitle::text` — strip whitespace |
| `rating` | `span.a-icon-alt::text` → regex `(\d+\.?\d*)` |
| `review_count` | `#acrCustomerReviewText::text` → strip `()`, remove commas |
| `brand` | `#bylineInfo::text` → regex `Visit the (.+?) Store` or strip `Brand: ` prefix → fallback "Brand Name" row in `th.prodDetSectionEntry` table |
| `main_image_url` | `#landingImage::attr(data-a-dynamic-image)` → JSON, key with largest area |
| `bsr_entries` | `th.prodDetSectionEntry` → filter "Best Sellers Rank" → `xpath('../td')` → `li.xpath('string()')` → regex `#([\d,]+)\s+in\s+(.+?)(?:\s*\(See\b\|$)` |
| `last_month_sales` | `contains(., 'bought in past month/week')` innermost + `string()` → regex `([\d,K+]+)\s+bought in past (?:month\|week)`; badge text is split across child elements so `contains(text(), ...)` never matches |
| `has_variants` | `[id*="inline-twister"]` — presence check |
| `is_small_business` | `[id*="sbe_badge"]` — presence check |
| `about_this_item` | `#feature-bullets ul li span.a-list-item::text` — join with `\n` |
| `variant_asins` | script tag containing `dimensionToAsinMap` + `variationValues` + `dimensions`; keys use `_` delimiter (e.g. `"12_5"` → color[12]+size[5]); returns `[{"asin":..., "color_name":..., "size_name":...}]` |
| `related_asins` | `[data-cel-widget*="p13n-desktop-sims-fbt"] a[href*="/dp/"]` → regex `/dp/([A-Z0-9]{10})/` → deduplicate, exclude current ASIN |
| `rating_breakdown` | `a[aria-label*="percent of reviews have"]` → parse all 5 stars |
| `weight` | `th.prodDetSectionEntry` where th text == "Item Weight" → `xpath('../td')` |
| `dimensions` | `th.prodDetSectionEntry` where "Item Dimensions" in th text → `xpath('../td')` |
| `launch_date` | `th.prodDetSectionEntry` where th text == "Date First Available" → `xpath('../td')` |

**JS-rendered** (Playwright + zip 19901 required):

| Field | Selector |
|---|---|
| `price` | `span.apex-basisprice-value span.a-offscreen` (FBA) → fallback `#tp_price_block_total_price_ww span.a-offscreen` (FBM) |
| `seller_name` | `#sellerProfileTriggerId::text` → fallback `#merchant-info` (Amazon-sold): first `<a>` text after "Sold by" |
| `seller_id` | `#sellerProfileTriggerId::attr(href)` → parse `seller=` param (NULL for Amazon-sold — no `seller=` in their URL) |
| `is_fba` | `#sellerProfileTriggerId::attr(href)` → check `isAmazonFulfilled=1` → fallback `#merchant-info` text: "Fulfilled by Amazon" |

**Amazon-sold products** (e.g. Owala sold by "Amazon Resale") do not have `#bylineInfo` or `#sellerProfileTriggerId`. For these:
- `brand`: falls back to "Brand Name" row in `th.prodDetSectionEntry` table
- `seller_name` / `is_fba`: fall back to `#merchant-info` text parsing

**Key selector fix:** `.prodDetSectionEntry` is a class on `<th>` elements, not `<tr>` elements. Always select `th.prodDetSectionEntry` and navigate to sibling `<td>` with `xpath('../td')`.

### Monitoring
Null rates tracked per run for: `title`, `brand`, `rating`, `review_count`, `price`, `seller_name`, `is_fba`, `bsr_entries`, `last_month_sales`, `is_small_business`, `has_variants`.

**Product table key fields (minimum — finalise before DDL):**

| Field | Notes |
|---|---|
| `asin` | Primary key component |
| `marketplace_id` | Multi-market support |
| `browse_node_id` | Raw from product page — may differ from ranking node |
| `node_status` | `'resolved'` \| `'unresolved'` |
| `title` | Rarely changes — low refresh priority |
| `description` | Rarely changes |
| `bullet_points` | Rarely changes |
| `seller_name` | Occasionally changes |
| `rating` | Changes frequently |
| `review_count` | Changes frequently |
| `last_month_sales` | Changes frequently |
| `scraped_at` | Timestamp of this fetch |

**Deferred — product listing spider:** considered but deferred until after AmzProducts is complete and a stable ASIN watchlist exists. Intent: scrape listing pages (multiple ASINs per page) to track fast-changing fields (rating, review count, sales velocity) without hitting individual detail pages. Low bot risk vs detail pages. Revisit once AmzProducts is running and the watchlist is confirmed.

## Controller Pattern

`AmzRankings` (and future spiders) use a DB controller table to manage scrape state:

- Table: `transformed.amz_category_scrape_controller`
- Statuses: `pending` → `in_progress` → `complete`
- Eligible nodes for a run: `pending` OR `in_progress` OR `complete` AND `last_scraped_at < now() - interval N days`
- **Always include `in_progress` in the eligibility query.** Nodes crash-stuck in `in_progress` would otherwise be skipped forever.
- Before test runs, reset node: `UPDATE ... SET scrape_status = 'pending', last_scraped_at = NULL WHERE node_id = ...`

## Amazon 2026 Page Structure (Bestseller)

The page structure changed from the India spider. Current selectors (confirmed on 2026-06-09):

| Field | Selector |
|---|---|
| Product card | `div[data-asin]` (primary) — the outer wrapper |
| Fallback card | `li[class*="zg-item-immersion"]` — old structure, kept as fallback |
| Title | `div[class*="p13n-sc-css-line-clamp"]::text` (new), then `span[class*="p13n-sc-truncate"]::text` |
| Review count | `span[aria-hidden="true"].a-size-small::text` (new), then `span[class*="a-size-small"] a::text` |
| Next page | `li.a-last > a::attr(href)` |

Note: `div[class*="zg-grid-general-faceout"]` exists in the HTML but is INSIDE `div[data-asin]`. Do not use it as the card selector — descendants will have `data-asin=''`.

## JS Rendering — Generic Amazon Problem

**This applies to any Amazon page, not just rankings.**

Amazon serves only a subset of products in the initial HTTP response. The rest are lazy-loaded via an internal component framework called **ACP (Amazon Component Platform)**. On bestseller pages, items 31–50 per page are lazy-loaded.

How it works:
1. Initial HTML contains 30 items + ACP widget config (`data-acp-path`, `data-acp-params`, `data-acp-stamp` attributes).
2. Browser JS initialises the ACP widget and registers session state server-side.
3. User scrolls → JS fires a POST to `{acp-path}/nextPage?page-type=zeitgeist&stamp=...`.
4. ACP server validates session state and returns items 31–50 as an HTML fragment.

**Scrapy cannot do step 2.** Firing the POST directly returns `<ol><videoResponsePlaceholder></ol>` (200 OK, empty body). The ACP server requires prior JS initialisation. This is not fixable by adding headers or cookies.

**The fix is `scrapy-playwright`.** Playwright runs a real Chromium browser, executes the JS, scrolls to trigger the lazy load, and returns the fully rendered DOM. See `docs/top_100_rankings_approach.md` for the full implementation plan.

Current ceiling without Playwright: **30 static items per page × 2 pages = 60 products per node**.

## ACP Widget Identifiers

When inspecting a new Amazon page for ACP attributes:
- `data-acp-path` — the endpoint prefix, e.g. `/acp/p13n-zg-list-grid-desktop/.../`
- `data-acp-params` — JSON blob with `tok`, `ts`, `rid`, `d1`, `d2`; used as `X-Amz-Acp-Params` header
- `data-acp-stamp` — timestamp; update `ts` in params to current epoch ms before firing
- `X-Amzn-Flow-Closure-Id` — generated by ACP JS at runtime; NOT in HTML; cannot be replicated without JS

## Key Pitfalls

**MUST READ before building any spider:** `docs/scraping_pitfalls.md` — 8 bugs from `AmzCategoryHierarchy` development.

The pitfalls below (P8–P25) are from `AmzRankings` and `AmzProducts` development. Apply to any future spider using Playwright or the controller pattern.

**P8 — Amazon ACP lazy-loading**
Never assume all items are in the static HTTP response. Amazon lazy-loads items 31–50 per page via ACP widget. Always dump raw HTML and count `div[data-asin]`. If count < expected, lazy loading is the cause. Fix: scrapy-playwright + iterative scrollIntoView (see P25 for the correct scroll approach).

**P9 — `from_crawler` override must call `_set_crawler()`**
Scrapy 2.16 sets `spider.crawler` (no underscore) via `spider._set_crawler(crawler)`. If you override `from_crawler` without calling `super()`, you must call `spider._set_crawler(crawler)` explicitly. Setting `spider._crawler = crawler` manually is wrong — `spider.crawler` won't exist and the dupe filter will crash with `AttributeError: 'XSpider' object has no attribute 'crawler'` the first time a duplicate URL is encountered. This bug is silent on small runs (no dupes) and only surfaces at scale.

**P10 — Playwright OOM on long runs: never scrape all categories in one go**
Chromium's V8 heap grows unbounded when one browser process handles thousands of pages over many hours — even with resource blocking. Symptoms: starts fast, slows progressively as GC cycles lengthen, then OOM crash. Fix: use `run_rankings.ps1` which runs one category at a time (fresh browser per category). Never pass all categories to a single scrapy process when using Playwright.

**P11 — `--js-flags=--max-old-space-size` does nothing for Chrome**
This is a Node.js flag. Chrome's V8 is embedded differently and ignores it. Do not add it to `PLAYWRIGHT_LAUNCH_OPTIONS['args']` — it gives false confidence that memory is capped when it isn't.

**P12 — Block images/fonts/stylesheets in Playwright**
Amazon bestseller pages fire 400+ image requests per node. Without blocking, Chromium OOMs within hours and each page takes much longer. Always set `PLAYWRIGHT_ABORT_REQUEST` to block `image`, `media`, `font`, `stylesheet`. Scripts and XHR must remain unblocked — ACP lazy-load requires them.

**P13 — Windows Defender blocks Chrome Headless Shell**
On Windows, `headless=True` selects Chrome Headless Shell, which Windows Defender/SmartScreen silently blocks even when the binary exists. Symptom: `Executable doesn't exist` or `spawn UNKNOWN`. Fix: `headless=False, args=['--headless=new', '--disable-gpu']` — uses Chrome for Testing (visible in process list but no window appears).

**P14 — `spider_closed` must reset `in_progress` → `pending`, not mark complete**
When a spider crashes or is killed, nodes still in `in_progress` must be reset to `pending` so the next run retries them. `_mark_node_complete()` is the only path that sets `complete` — it must only be called after both pages are successfully parsed. The `spider_closed` handler resets everything still in `in_progress` at close time.

**P15 — Nullable columns in composite PRIMARY KEY**
PostgreSQL allows nullable columns in a PK definition but the columns implicitly get NOT NULL. If a column like `subcategory_node_id` is intended to be nullable, it must not be in the PK. Use a surrogate UUID (`id UUID DEFAULT gen_random_uuid()`) as the PK for append/staging tables. Natural compound keys work for dimension tables with guaranteed non-null values.

**P16 — `ON CONFLICT DO UPDATE` fails when staging has within-batch duplicates**
If staging already contains two rows with the same merge key (e.g. two resume runs on the same day), the `INSERT ... ON CONFLICT DO UPDATE` fails: `command cannot affect row a second time`. Fix: wrap the SELECT in `DISTINCT ON (conflict_key_columns) ... ORDER BY ..., scraped_at DESC` to pre-deduplicate within staging before the INSERT sees any row twice.

**P17 — Comma delimiter conflicts with category names that contain commas**
Using `,` to delimit multiple categories in the `categories` spider parameter silently splits names like `"Arts, Crafts & Sewing"` into `["Arts", "Crafts & Sewing"]` — neither matches anything in the DB, the spider finds 0 nodes and exits cleanly with no error. Use `|` as the delimiter instead (`"Arts, Crafts & Sewing|Pet Supplies"`). Any parameter that accepts a list of values whose members may themselves contain the delimiter has this problem.

**P18 — `wait_for_selector` times out on empty Amazon category pages**
Some category nodes (e.g. "Scrapbooking Pens & Markers") have no bestsellers listed. Amazon serves a page with the message "Sorry, there are no Best Sellers available in this category." — no `div[data-asin]` is ever inserted into the DOM. `wait_for_selector('div[data-asin]')` waits the full 30s default timeout and then raises `TimeoutError`, failing the request. The node stays `in_progress` and is retried on every subsequent run forever.
Fix: replace `wait_for_selector` with `wait_for_load_state('domcontentloaded')` — always completes immediately regardless of page content. Then in `parse()`, check for the empty-page message text and call `_mark_node_complete()` so the node is not retried.

**P19 — `_mark_node_complete()` matched by (category, subcategory) name, not node_id**
Original implementation filtered by `category = %s AND subcategory = %s` (display names). Subcategory names repeat across root categories — e.g. "Storage" appears under both Home & Kitchen and Tools & Home Improvement. Any UPDATE using name columns matches every row with that subcategory name across all categories, silently marking nodes complete in the wrong category's run.
Fix: filter by `subcategory_node_id = %s`. node_id is unique per node. Both call sites in `parse()` (normal completion and empty-page completion) were updated to pass `subcategory_node_id`.

**P20 — Closure table cross-contamination causes eligibility query to queue wrong categories**
`AmzRankings.start()` expands a named category to all descendants via the closure table (`amz_category_hierarchy`). If the hierarchy spider attributed nodes from another category to the wrong root (e.g. because of shared URL slug traversal — see P21), the expansion returns node_ids that belong to a different top-level category. Without a category constraint, the eligibility query queues and scrapes those foreign nodes during the wrong category's run.
Fix: after expanding `matched_ids` to `target_ids` via the closure table, resolve the correct root categories by querying `amz_category_scrape_controller` directly on `matched_ids` (not the expanded set). This uses the controller's own `category` column — which was set correctly at seed time from the category name — and adds `AND ctrl.category = ANY(root_categories)` to the eligibility query. The closure table is used only for ID expansion, not for category attribution.

**P21 — Shared URL slug causes root category node_id collision in AmzCategoryHierarchy**
`_node_id(url)` returns the URL slug for root nodes (no numeric ID in the URL). Multiple root categories share the same slug: Home & Kitchen, Kitchen & Dining, and Tools & Home Improvement all resolve to `/hi/`. All three call `_write_node(node_id='hi', ...)` — each overwrites the previous row in `amz_category`. After the spider completes, only one root survives in the DB. `seed_controller.sql` then finds that root's subtree and assigns all nodes under it to a single category name, corrupting the controller for all three categories.
Fix: in `parse_root()`, use `node_id = name` (the display category name) instead of `_node_id(url)`. Category names are unique across all 10 target categories and fit within VARCHAR(30). Companion fix: `_build_url()` now checks `node_id.isdigit()` to determine whether to append the node_id to the URL — string (root) node_ids do not appear in the URL, only numeric subcategory IDs do.
Always run `validate_hierarchy.sql` after `AmzCategoryHierarchy` to detect this class of collision before seeding the controller.

## New Spider Development Process

Follow this sequence every time. Do not skip steps — most selector bugs and wasted development time come from skipping steps 1–4.

**Step 1 — Define what to scrape**
Before writing a line of code, ask: what fields are needed? Agree on the exact attribute list (field name, type, nullable or not, example value). Do not start development without this — selectors found for the wrong fields waste hours.

**Step 2 — Set expectations**
Agree on: target page URL pattern, expected record count per page/node, any known Amazon restrictions (login walls, bot detection, lazy-loading). Document assumptions before they become surprises mid-build.

**Step 3 — Download the raw HTML**
Save the actual target page to disk before writing any selectors:
```bash
# Using scrapy fetch (respects middleware settings)
scrapy fetch "https://www.amazon.com/..." > html_debug/target_page.html

# Or via curl (no middleware)
curl -A "Mozilla/5.0..." "https://www.amazon.com/..." -o html_debug/target_page.html
```
Never write selectors by reading the live page in a browser — what you see is rendered JS; what Scrapy receives is the raw HTTP response and they differ significantly on Amazon.

**Step 4 — Analyse the HTML and write selectors offline**
Open the saved HTML file, identify the exact elements and attributes for each field. Write down the CSS selector or XPath for each. Check for Amazon's hashed class names — always use substring selectors (`[class*="..."]`), never exact class names (P3).

**Step 5 — Validate selectors in Scrapy shell against a live page**
```bash
# From scraping/
D:/Documents/projects/GitHub/ecom-intelligence/.venv_scrape/Scripts/scrapy shell "https://www.amazon.com/..."

# Inside shell — test each selector before trusting it
response.css('div[data-asin]')              # card count
response.css('div[data-asin]').get()        # first card HTML
response.css('[class*="zg-bdg-text"]::text').getall()   # rank badges
```
A selector that works on the saved HTML but returns nothing in the shell means Amazon serves different markup to Scrapy — add headers, check JS rendering requirements, or switch to Playwright.

**Step 5 — Validate against RENDERED HTML for JS-dependent pages**
For pages that require Playwright, scrapy shell (plain HTTP) will NOT render JS-dependent fields. Use the render scripts in `html_debug/` to save the post-Playwright DOM, then validate ALL selectors (static + JS) against the saved file before building.

**Step 6 — Build the spider**
Only after steps 1–5 are done. Selectors are confirmed; no guessing during development.

**P22 — `networkidle` never fires on Amazon product pages**
Amazon fires continuous analytics/tracking XHR requests after page load — Amplitude, CloudFront, Ads telemetry, etc. `wait_until='networkidle'` waits for the network to be idle for 500ms, which never happens on Amazon. Playwright's `page.goto()` and `wait_for_load_state('networkidle')` both time out.

Fix: use `wait_for_load_state('load')` (page and blocking scripts loaded) followed by a fixed `wait_for_timeout(3000)` to let the buybox AJAX settle. Do not use `networkidle` for any Amazon page.

**P23 — Bootstrap zip-set: use `wait_for_selector` not a fixed timeout after clicking the popover**
The zip code popover (`#GLUXZipUpdateInput`) takes a variable amount of time to open after clicking `#glow-ingress-block`. Using a fixed `wait_for_timeout(1500)` before `fill()` is fragile: if the page is slow, `fill()` hits its own 30s timeout and the error message says "Page.fill: Timeout 30000ms exceeded" — obscuring the real cause (popover never opened).

Fix: replace `wait_for_timeout(1500)` with `wait_for_selector('#GLUXZipUpdateInput', state='visible', timeout=15000)`. This waits explicitly for the element and fails fast with a clear error if the popover doesn't open.

**P24 — `contains(text(), ...)` fails when badge text is split across child elements**
Amazon's "X bought in past month" badge splits the count and label: `<span><span>20K+</span> bought in past month</span>`. `contains(text(), "bought in past month")` checks direct text nodes only — it finds 0 matches because "20K+" is in a child `<span>` and the parent's direct text node is just " bought in past month" (no count).

`el.xpath('text()').get()` also only returns the FIRST direct text node (often whitespace), not the full badge text.

Fix: use `contains(., ...)` (checks all descendant text) with the innermost-element constraint `not(descendant::*[contains(., ...)])` to avoid matching the entire page body. Then use `el.xpath('string()').get()` to concatenate all descendant text.

Same pattern applies to any badge/label where number and text are in sibling or nested elements. Also: Amazon shows "past week" instead of "past month" for high-velocity products — handle both periods.

**P25 — `window.scrollTo(bottom)` does not trigger Amazon's IntersectionObserver lazy-load**
Amazon uses IntersectionObserver (not a scroll event) to lazy-load items 31–50 per page. `window.scrollTo(0, document.body.scrollHeight)` fires a scroll event but does NOT trigger the observer — the count stays at 30. Confirmed via live Playwright test.

Fix: repeatedly scroll the last visible card into the viewport using `scrollIntoView`, then wait 2s for the XHR batch to arrive. Repeat until count reaches 50 or stops growing. Typical path: 30 → 38 → 46 → 50 (3 iterations, ~6s total). Implemented as an async IIFE in `_playwright_meta()`:
```javascript
(async () => {
    const sel = 'div[data-asin]';
    for (let i = 0; i < 5; i++) {
        const cards = document.querySelectorAll(sel);
        if (cards.length >= 50) break;
        const prev = cards.length;
        if (cards.length > 0)
            cards[cards.length - 1].scrollIntoView({behavior: 'instant', block: 'end'});
        await new Promise(r => setTimeout(r, 2000));
        if (document.querySelectorAll(sel).length === prev) break;
    }
})()
```
Rule: for any lazy-load on Amazon, test with a Playwright debug script first (`html_debug/test_scroll.py`) — do not assume the scroll mechanism without verification.

---

## Running Spiders

Always use the project venv — never the system Python:

```bash
# From scraping/
D:/Documents/projects/GitHub/ecom-intelligence/.venv_scrape/Scripts/scrapy crawl AmzRankings -a list_type=bestseller -a categories="Carriers & Travel Products" -a include_descendants=false -s LOG_FILE=logs/test.log
```

Install packages into the venv:
```bash
D:/Documents/projects/GitHub/ecom-intelligence/.venv_scrape/Scripts/pip install <package>
```

Playwright Chromium is installed at: `C:\Users\yashl\AppData\Local\ms-playwright\chromium-1223`

## Settings and Concurrency

- Scrapy 2.16 removed `CONCURRENT_REQUESTS_PER_IP` from `DownloaderAwarePriorityQueue`. Do not set it in `custom_settings`.
- Current `AmzRankings` concurrency (plain HTTP): `CONCURRENT_REQUESTS=16`, `DOWNLOAD_DELAY=8`. With Playwright: `CONCURRENT_REQUESTS=4`, `DOWNLOAD_DELAY=8`, `PLAYWRIGHT_MAX_PAGES_PER_CONTEXT=4`. Resource blocking (images/fonts/stylesheets) keeps memory manageable. **Do not run all categories in one go** — Chromium V8 heap grows unbounded over hours and OOMs. Use `run_rankings.ps1` which runs one category at a time and restarts the browser between each.
- Current `AmzProducts` concurrency: `CONCURRENT_REQUESTS=4`, `DOWNLOAD_DELAY=6`, `PLAYWRIGHT_MAX_PAGES_PER_CONTEXT=4`. Same resource blocking as AmzRankings.
- `TWISTED_REACTOR` is already set to `asyncioreactor` in `settings.py` — required for `scrapy-playwright`.

## Docs

- `docs/scraping_pitfalls.md` — 8 bugs (P1–P7, P21); must read before new spider
- `docs/top_100_rankings_approach.md` — plan to reach 100 products/node via Playwright
- `docs/sanity_checks.md` — SQL queries for verifying DB state
- `docs/explainations.md` — closure table explanation for `amz_category_hierarchy`
- `db/validate_hierarchy.sql` — 6 integrity checks; run after AmzCategoryHierarchy, before seed_controller.sql; all checks must return 0 rows
- `db/seed_product_queue.sql` — manual seeder; run after AmzRankings merge cycle before AmzProducts
- `html_debug/validate_product_selectors.py` — selector validation script for AmzProducts; reads rendered HTML files
- `html_debug/render_product.py` — renders catchmaster product page with Playwright + zip 19901
- `html_debug/render_bubbleblooms.py` — renders BubbleBlooms product page (SB badge validation)
