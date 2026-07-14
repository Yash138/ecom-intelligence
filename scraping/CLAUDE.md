# Scraping — Context for Claude

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-06-09 | All | Initial creation — spider architecture, controller pattern, key pitfalls P8–P18, settings |
| 2026-06-13 | Spider Architecture, Key Pitfalls, Docs | AmzProducts added; P19–P22 documented (contamination bugs, networkidle); new spider process updated |
| 2026-06-13 | AmzProducts Design, Key Pitfalls | Corrected related_asins selector (FBT widget); added brand/seller/is_fba fallbacks for Amazon-sold products; P23 added (bootstrap wait_for_selector) |
| 2026-06-13 | AmzProducts Design, Key Pitfalls | Fixed last_month_sales (split-text badge, P24); fixed variant_asins to include color/size labels via variationValues parsing |
| 2026-06-13 | Key Pitfalls, Spider Architecture | P25: window.scrollTo does not trigger Amazon IntersectionObserver lazy-load; fixed with iterative scrollIntoView IIFE in _playwright_meta() |
| 2026-06-14 | Spider Architecture, Key Pitfalls | AmzCategoryHierarchy: added Playwright + zip 19901 bootstrap (same pattern as AmzProducts); plain HTTP from non-US IP serves geo-specific nav tree (P26) |
| 2026-06-14 | Key Pitfalls, AmzProducts Design | P26: Amazon A/B / bot-detection serves reduced page layouts; sparse-page retry added to AmzProducts parse_product() |
| 2026-06-14 | Key Pitfalls, Spider Architecture | P27: AmzCategoryHierarchy sibling-as-child bug — iterating all zg-browse-item treats siblings as children → depth explosions; fixed with children-only XPath in parse() |
| 2026-06-15 | Spider Architecture, Running Spiders | AmzCategoryHierarchy: added `-a target_categories` pipe-delimited arg; full re-run started (Home & Kitchen done: 1,172 nodes clean); validate_hierarchy.sql corrected to 9 categories (removed Kitchen & Dining which was never in chosen_categories.csv) |
| 2026-06-15 | Key Pitfalls | P28: Amazon DAG nodes appear in multiple root category navs; separate Scrapy runs claim them under different slugs → multi-slug ancestry. Fix: root_url_slug meta guard + single-run all-categories strategy |
| 2026-06-21 | Key Pitfalls, Spider Architecture | P29: Partial restart contamination — restarting with category subset resets visited_node_ids, allowing already-complete categories' DAG nodes to be re-claimed. Fix: spider_opened pre-populates visited_node_ids from DB for non-target categories. AmzCategoryHierarchy complete: 7,083 nodes, 9 cats. |
| 2026-07-02 | Key Pitfalls, Spider Architecture, Settings | P30: playwright-stealth v2.0.3 integrated via `playwright_page_init_callback` (2-arg arity). P31: Amazon buybox seller/FBA layout updated — 3 patterns now handled. DOWNLOAD_DELAY raised to 12s. Smoke test clean: 4 real products, all fields populated. |
| 2026-07-03 | Key Pitfalls, AmzProducts Design | P32: book/media `#bylineInfo` structure differs — first text node is "by" not the brand; fix: fallback to `#bylineInfo .author a::text`. P33: compact seller regex `[^.]+?` fails for "Amazon.com" — changed to `.+?` with updated terminator `(?:\s+and\s+ships from|\.\s*$)`. |
| 2026-07-04 | Key Pitfalls, AmzProducts Design, Settings | P34: fingerprint rotation. The default `use_scrapy_headers` fed `RandomUserAgentMiddleware`'s pool (incl. Chrome-59/mobile/bot UAs) into Playwright's nav request while the engine was Chromium-148 — a self-inflicted mismatch; raw UA was also `HeadlessChrome`. Fix: `PLAYWRIGHT_PROCESS_REQUEST_HEADERS=None`, disable UA/header middlewares in PW mode, per-context coherent Chrome/148 UA + sec-ch-ua, 3 round-robin contexts (distinct session identities), zip-bootstrap per context, `Asia/Kolkata` tz, randomized dwell. Verified via httpbin; smoke test 6/6 written, 0 blocks. |

## Spider Architecture

Three spiders, run in order:

1. **`AmzCategoryHierarchy`** — one-time per market (re-runnable). **Complete as of 2026-06-21**: 9 categories, 7,083 nodes, all 6 validate checks clean. Re-run only if categories expand.
2. **`AmzRankings`** — scrapes bestseller/new-releases pages for all nodes. Reads pending nodes from `transformed.amz_category_scrape_controller`. **Bestseller complete as of 2026-06-21**: 516,220 rows merged to `transformed.amz_ranking`, 423,936 ASINs queued. New-release run deferred.
3. **`AmzProducts`** — **built 2026-06-13, stealth-patched 2026-07-02.** Scrapes product detail pages for ASINs from `transformed.amz_product_scrape_queue`. **Queue has 423K ASINs. Full run not yet started.** Only run within 11 AM–11 PM IST (India IP unmasked).

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
| `brand` | `#bylineInfo::text` → regex `Visit the (.+?) Store` or strip `Brand: ` prefix → `#bylineInfo .author a::text` (books/media where bylineInfo first text node is "by") → fallback "Brand Name" row in `th.prodDetSectionEntry` table |
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

**Known design gap — `_write_monitoring_stats` will fail silently on large runs:** `_scraped_asins` is a flat Python list appended to on every product write, then passed as `ANY(%s)` at spider close. For 50K+ products this becomes a massive array parameter — slow, and psycopg2 may fail. The exception is caught so the spider won't crash, but stats won't be written. **Future fix:** store `self.run_start = dt.now()` in `__init__` and filter the monitoring query by `WHERE last_captured_at >= %s` instead of `asin = ANY(%s)`. Not blocking for the current run.

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

The pitfalls below (P8–P30) are from `AmzRankings`, `AmzProducts`, and `AmzCategoryHierarchy` development. Apply to any future spider using Playwright or the controller pattern.

**P8 — Amazon ACP lazy-loading**
Never assume all items are in the static HTTP response. Amazon lazy-loads items 31–50 per page via ACP widget. Always dump raw HTML and count `div[data-asin]`. If count < expected, lazy loading is the cause. Fix: scrapy-playwright + iterative scrollIntoView (see P25 for the correct scroll approach).

**P26 — Non-US IP serves geo-specific Amazon nav; category tree differs from US users**
Amazon's bestseller left-nav varies by the requesting IP's geolocation. Running `AmzCategoryHierarchy` from an Indian IP (even targeting amazon.com) produces a category tree that does not match what US users see — wrong ancestors, missing subcategories, phantom depth. Confirmed: hierarchy data showed depth=48 for a node that should be 3 levels deep, and cross-category ancestor assignments (e.g. Kitchen & Dining → Wine Accessories).

Fix: add Playwright + zip 19901 bootstrap (same pattern as AmzProducts). Setting the US delivery zip via the homepage location popover causes Amazon to serve US-local nav on all subsequent category page requests in the same browser context.

Rule: any spider that reads Amazon's category nav must set zip 19901 before fetching category pages. `AmzCategoryHierarchy` now does this by default (`use_playwright=true`).

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

**P26 — Amazon A/B testing / bot-detection cohort serves reduced page layouts**
Amazon assigns each browser session to a test cohort via session cookies. Some cohorts receive a reduced layout where BSR, rating breakdown, variant selectors, product details table, and small business badge are entirely absent — even with the correct zip set and no login required. This is independent of the delivery zip and is not fixable by cookie manipulation.

Observed: two Chrome instances, same product, same zip (19901), no login — one shows full data, one shows all mid-page sections hidden.

Fix in `AmzProducts`: after extracting fields, if `bsr_entries=None AND rating_breakdown=None` (strongest signal — both are always rendered on a full-layout page), re-issue the request once with `sparse_retry=1` in meta and `dont_filter=True`. The fresh request gets a new session cookie and may hit a different cohort. DB write and queue delete are deferred until after the retry resolves. One retry max — if still sparse, write what we have and let the null rate monitor flag it.

Signal rationale: `bsr_entries` and `rating_breakdown` are always present on a normal product page regardless of login, zip, price availability, or product type. Both being NULL simultaneously means the entire mid-page section block failed to render, which is a layout issue not a data issue. Using both together avoids false positives from products that legitimately have no BSR (e.g. new products) or no reviews.

**P28 — Amazon DAG nodes appear in multiple root category navs; separate runs produce multi-slug ancestry**
Some Amazon browse nodes (e.g. "Garage Storage" node 165112011, "Mounted Closet Systems" node 165119011) legitimately appear in multiple root category navs — e.g. Home & Kitchen (slug `home-garden`) AND Tools & Home Improvement (slug `hi`). Amazon serves each from a different URL slug depending on which root's nav context you're browsing.

If you run `AmzCategoryHierarchy` as two separate Scrapy processes (one per category), `visited_node_ids` is per-process and resets between runs. The first run claims these DAG nodes via one slug and writes ancestors for that root. The second run visits the same nodes via a different slug and writes a second set of ancestors for the other root. Both ancestor chains survive in the DB (they have different `ancestor_node_id` values, so no PK conflict). Check 4 of `validate_hierarchy.sql` catches this: "Subcategories under multiple roots."

Two fixes applied:

1. **`root_url_slug` meta guard (code):** `parse_root()` passes `root_url_slug` through all request meta. `parse()` skips any child link whose URL slug differs from the root's slug. This prevents following cross-root links that Amazon serves as contextual "see also" entries in a category sidebar.

2. **Single-run all-categories strategy (operational):** pass all target categories via one `-a target_categories=` command. All categories run in one Scrapy process, so `visited_node_ids` is shared. The first category to claim any DAG node wins globally — no second run can re-claim it under a different slug.

Rule: never run `AmzCategoryHierarchy` as separate per-category processes if any categories might share DAG nodes (which is common on Amazon). Always run all categories together.

**P29 — Partial restart contamination: restarting with a category subset re-claims DAG nodes owned by already-complete categories**
When restarting `AmzCategoryHierarchy` with only a subset of categories (e.g. `-a target_categories=clothing`), the spider process starts fresh with `visited_node_ids = set()`. Any DAG node that was already written under a completed category (e.g. Patio owns "Outdoor Storage" node 13400641) is not in `visited_node_ids` — so when the restarted run traverses T&HI's nav and encounters the same node, it writes a second ancestry chain under T&HI. CHECK 4 of `validate_hierarchy.sql` catches this.

This is distinct from P28 (which is about cross-root link-following). P29 occurs even when the `root_url_slug` guard is in place — it fires when the `visited_node_ids` dedup set is empty and a node from another category appears naturally in the current category's nav.

Fix: `spider_opened` pre-populates `visited_node_ids` from the DB for all categories NOT in `target_categories`:
```python
if self.target_categories:
    rows = self.db.read(
        query=f"""
            SELECT DISTINCT c.node_id
            FROM transformed.amz_category c
            JOIN transformed.amz_category_hierarchy h ON ...
            JOIN transformed.amz_category root ON ... AND root.depth = 0
            WHERE c.marketplace_id = %s
              AND LOWER(root.node_name) NOT IN ({placeholders})
        """,
        params=[self.marketplace_id] + list(self.target_categories),
    )
    self.visited_node_ids.update(row['node_id'] for row in rows)
```
On a full fresh run (DB empty), this is a no-op (0 rows). On a partial restart, it loads all node_ids belonging to already-complete categories before any scraping starts.

**P27 — AmzCategoryHierarchy: iterating all `zg-browse-item` treats siblings as children**
Amazon's bestseller nav sidebar shows three sets of links on every category page: (1) ancestor breadcrumbs (`zg-browse-up` class, ‹ prefix), (2) the currently-selected node (`span[aria-current="page"]`, no `<a>` link), and (3) the selected node's children (a wrapper `<li>` immediately after the selected `<li>`, containing a `<ul>`). On leaf pages (no children), the nav shows the sibling nodes — other children of the current node's parent — as plain `<li>` items after the selected `<li>`.

The original implementation iterated ALL `[class*="zg-browse-item"]` items. On any non-root page, the unvisited siblings of the current node appear as `<a>` links in the sidebar (same URL slug, not yet visited), so the spider picked them up, added the current node to their `ancestor_chain`, and followed them. Their children were then attributed to the wrong parent. This caused ancestor chains to grow to depth 48 for nodes that were actually 3 levels deep, and produced cross-category ancestry (e.g. Kitchen & Dining → Wine Accessories → Coffee, Tea & Espresso).

Fix: in `parse()`, replace the `zg-browse-item` iteration with:
```python
child_anchors = response.xpath(
    '//span[@aria-current="page"]/ancestor::li[1]/following-sibling::li[1][.//ul]//a'
)
```
- `ancestor::li[1]` — the `<li>` that contains the selected span
- `following-sibling::li[1]` — the NEXT sibling `<li>` (if any)
- `[.//ul]` — only if that sibling contains a nested `<ul>` (children wrapper). At leaf pages, the following siblings are plain `<li>` items with `<a>` tags and no nested `<ul>`, so this predicate correctly returns empty — leaf detection for free with no extra logic
- `//a` — all anchor tags in the children group

Confirmed against captured nav HTML (3 depths): depth-0 and depth-1 pages return only direct children; depth-2 leaf page returns empty.

**P30 — Unpatched Playwright is fingerprinted as a bot by Amazon**
After 22+ hours of unstealth Playwright scraping (AmzRankings bestseller run), Amazon detected the bot fingerprint and began serving CAPTCHA pages to AmzProducts. Signal: `csm-captcha-instrumentation.min.js` appearing in every response log, 57% null title rate in staging.

Root cause: Playwright exposes `navigator.webdriver=true` plus ~30 other JS signals (missing plugins, wrong WebGL renderer, etc.) that Amazon's fingerprint detection checks.

Fix: `playwright-stealth==2.0.3` patches these signals via `page.add_init_script()`. The script MUST be added BEFORE `page.goto()` — init scripts only apply to future navigations from the point they are registered.

**The only correct integration hook is `playwright_page_init_callback`** (scrapy-playwright ≥0.0.46 feature). This is a coroutine called with `(page, request)` BEFORE `page.goto()` runs. Standard `playwright_page_methods` are called AFTER `page.goto()` and cannot patch init scripts retroactively.

**Arity:** scrapy-playwright 0.0.46 calls the callback with 2 args `(page, request)` — NOT 3. Using `(page, request, spider)` raises `TypeError` (caught as warning, stealth fails silently, CAPTCHA returns).

Implementation (in each spider's import block + module-level):
```python
try:
    from scrapy_playwright.page import PageMethod
    from playwright_stealth import Stealth
    _PLAYWRIGHT_AVAILABLE = True
    _stealth = Stealth()
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    _stealth = None

async def apply_stealth(page, request):
    await _stealth.apply_stealth_async(page)
```

In every Playwright request meta dict:
```python
meta = {
    'playwright': True,
    'playwright_page_init_callback': apply_stealth,  # fires BEFORE goto
    'playwright_page_methods': [...],
}
```

All 3 spiders updated. Also increased DOWNLOAD_DELAY to 12s in Playwright mode (was 6–8s). Full smoke test: 4 real products across categories, no CAPTCHA, all fields extracted.

**P31 — Amazon buybox seller/FBA layout changed (~2025): `#merchant-info` and `#sellerProfileTriggerId` no longer sufficient**
Amazon rolled out a new tabular buybox layout and a compact single-line format. The old `#merchant-info` free-text fallback stopped working. Three patterns now exist:
1. `#sellerProfileTriggerId` link (third-party sellers) — unchanged
2. Tabular: `<span>Sold by:</span><span>Name</span>` sibling pair — new default
3. Compact: `"Ships from and sold by Name."` or `"Sold by Name and ships from Amazon Fulfillment."` in `span.a-color-secondary` — Amazon-sold products

For is_fba:
1. `#sellerProfileTriggerId` href `isAmazonFulfilled=1` — unchanged
2. Tabular: `<span>Ships from:</span><span>Name</span>` — "amazon" in name → True
3. Compact: "ships from" or "fulfilled by" in `span.a-color-secondary` text → "amazon" in text → True

Fix applied in `_parse_seller_name()` and `_parse_is_fba()` in `amz_products.py`.

See also P33 — the compact regex had a second bug where `[^.]+?` excluded dots, silently failing for "Amazon.com".

**P32 — Book/media `#bylineInfo` structure: first text node is "by" not the brand**
For books, maps, and media products, `#bylineInfo` has this structure:
```html
<div id="bylineInfo">by
  <span class="author">
    <a href="...">Rand McNally</a>
    <span class="contribution">(Author)</span>
  </span>
</div>
```
`response.css('#bylineInfo::text').get()` returns only the first direct text node: `"by"`. The existing fallbacks (`Visit the X Store`, `Brand: X`) don't match it, so `brand = 'by'` was written to the DB.

Fix: added `#bylineInfo .author a::text` lookup before the generic text fallback:
```python
author = (response.css('#bylineInfo .author a::text').get() or '').strip()
if author:
    return author
if text and text.lower() not in ('by', ''):
    return text
```
The `text.lower() not in ('by', '')` guard prevents returning the bare word "by" as a brand.

**P33 — Compact seller name regex `[^.]+?` fails for seller names containing dots**
The compact "Ships from and sold by Name." pattern uses `span.a-color-secondary` text. The regex `r'[Ss]old by ([^.]+?)(?:\s+and\s+ships|\.\s*$)'` uses `[^.]+?` (no dots allowed) for the capture group. Amazon.com's seller name appears as "Amazon.com" — the `.` in `.com` breaks the match.

Symptom: `seller_name=None` for all Amazon-sold products, even when `"Ships from and sold by Amazon.com."` is clearly visible in the page text.

Fix: changed to `r'[Ss]old by (.+?)(?:\s+and\s+ships from|\.\s*$)'`:
- `(.+?)` allows any character (including `.`)
- Terminator updated from `\s+and\s+ships` to `\s+and\s+ships from` (more specific, avoids false matches)

Verified against all 38 saved HTMLs: Amazon.com, third-party sellers, and "Sold by Name and ships from Amazon Fulfillment" all match correctly.

**P34 — Playwright fingerprint rotation: the middleware UA/headers were actively HARMING us**
Root-cause of the ~1h45m session block (P30 stealth was applied but blocking still happened after 160 products).

Three compounding fingerprint bugs, all now fixed in `AmzProducts`:

1. **`use_scrapy_headers` fed the random middleware UA into Playwright's navigation request.** scrapy-playwright's default `PLAYWRIGHT_PROCESS_REQUEST_HEADERS = use_scrapy_headers` overrides the browser's navigation-request headers with the *Scrapy request's* headers — including the UA set by `RandomUserAgentMiddleware`. That middleware (`scrapy_user_agents`) rotates through a huge pool incl. ancient/mobile/bot UAs (observed: `Chrome/59`, Nintendo DSi, LYF phones). So the HTTP `User-Agent` said e.g. "Chrome 59 / Win7" while the real engine was **Chromium 148**, `navigator.userAgent` said Chromium 148, sec-ch-ua said 148, TLS said 148 — a glaring, self-inflicted mismatch. This is a bigger bot tell than an un-rotated but consistent UA.
2. **The raw engine UA is `HeadlessChrome/148.0.0.0`** — the literal "Headless" token is an instant block. (Only masked before because the middleware happened to overwrite it with the random UA.)
3. **Headless Chromium's sec-ch-ua advertises only "Chromium"** (no "Google Chrome" brand) — a subtle client-hint tell even after fixing the UA.

Fixes:
- `PLAYWRIGHT_PROCESS_REQUEST_HEADERS = None` — ignore Scrapy-injected headers; the browser sends its own coherent header set.
- Disable `RandomUserAgentMiddleware` + `HeaderRotationMiddleware` in Playwright mode (identity now owned by the browser context, not middleware). They remain active for `use_playwright=false` (plain-HTTP static-field scraping).
- Per-context `user_agent` = `Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/148.0.0.0 Safari/537.36` (matches the real engine version → consistent sec-ch-ua; removes "Headless").
- Per-context `extra_http_headers` sets `sec-ch-ua` to include `"Google Chrome";v="148"` so client hints match a genuine Chrome.
- Verified against `httpbin.org/headers`: UA, sec-ch-ua, sec-ch-ua-platform, and JS `navigator.userAgent` all coherent; zero "HeadlessChrome" tokens in scraped HTML.

**Identity rotation (fingerprint diversity):** `_N_CONTEXTS = 3` browser contexts, each bootstrapped with its own zip-19901 flow → each is a **distinct cookie/session identity** to Amazon (Amazon keys identity on the session-id cookie, not UA). Product requests are emitted **round-robin** across ready contexts, so each identity sees ~1/N of the total request rate (with DOWNLOAD_DELAY serializing dispatch, each identity hits Amazon ~every 36s instead of ~12s — far more human). Bootstrap uses `_is_captcha()` (hard CAPTCHA signals only), NOT `_is_blocked()` (which has a null-`#productTitle` branch that false-positives on the title-less homepage). A context whose zip bootstrap fails is excluded; the run continues on the survivors.

**Why UA string itself is NOT rotated across contexts:** on a single Chromium binary, varying the UA brand/OS/version would contradict the fixed engine-generated sec-ch-ua / `navigator.userAgentData` / `navigator.platform` (Win32) / TLS. Consistency beats diversity here — identity separation comes from cookies (separate contexts) + viewport, not UA strings. Real UA-string rotation would require full per-context client-hint spoofing (future work, and better solved by IP rotation).

**Other measures added:** `timezone_id = 'Asia/Kolkata'` (matches the unmasked India IP — a US timezone on an India IP is a proxy/bot mismatch; the US *delivery* zip is a separate concept); randomized per-page dwell (`wait_for_timeout` 2.5–4.5s) instead of a fixed 3s; distinct viewport per context.

**Still NOT addressed (real remaining levers):** IP rotation (single fixed India IP — Oxylabs middleware stubbed in `middlewares.py`); time-based context recycling (contexts persist for the whole run — cookies accumulate; could close+rebootstrap after N products). See `docs/amazon_scraping_problems.md` §1b, §7.

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
- Current `AmzRankings` concurrency (plain HTTP): `CONCURRENT_REQUESTS=16`, `DOWNLOAD_DELAY=8`. With Playwright: `CONCURRENT_REQUESTS=4`, `DOWNLOAD_DELAY=12`, `PLAYWRIGHT_MAX_PAGES_PER_CONTEXT=4`. Resource blocking (images/fonts/stylesheets) keeps memory manageable. **Do not run all categories in one go** — Chromium V8 heap grows unbounded over hours and OOMs. Use `run_rankings.ps1` which runs one category at a time and restarts the browser between each.
- Current `AmzProducts` concurrency: `CONCURRENT_REQUESTS=4`, `DOWNLOAD_DELAY=12` (Playwright), `PLAYWRIGHT_MAX_PAGES_PER_CONTEXT=4`. Same resource blocking as AmzRankings.
- Current `AmzRankings` Playwright delay: `DOWNLOAD_DELAY=12` (raised from 8 — P30). Plain HTTP mode unchanged at 8s.
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
