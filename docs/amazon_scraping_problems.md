# Amazon Scraping — Problems & Blockers

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-07-04 | All | Initial creation — consolidated from scraping_pitfalls.md, scraping/CLAUDE.md P8–P33, and observed production run behaviour |
| 2026-07-04 | 1a, 1b, 1c | P34 fingerprint rotation implemented — coherent per-context Chrome/148 UA + sec-ch-ua (killed the middleware UA/`HeadlessChrome` mismatch), 3 round-robin session identities, Asia/Kolkata tz. Updates the "Anti-Bot Detection" status. |

## Table of Contents

- [1. Anti-Bot Detection & Blocking](#1-anti-bot-detection--blocking)
- [2. Geo-Specific Content (Non-US IP)](#2-geo-specific-content-non-us-ip)
- [3. Dynamic / JS-Rendered Content](#3-dynamic--js-rendered-content)
- [4. Page Structure & Selector Fragility](#4-page-structure--selector-fragility)
- [5. Category Hierarchy Correctness](#5-category-hierarchy-correctness)
- [6. Data Extraction Bugs](#6-data-extraction-bugs)
- [7. Throughput & Operational Constraints](#7-throughput--operational-constraints)
- [8. Scrapy & Infrastructure Issues](#8-scrapy--infrastructure-issues)

---

## 1. Anti-Bot Detection & Blocking

### 1a — Playwright fingerprint exposed as a bot
**What happens:** Amazon detects the Playwright browser context via `navigator.webdriver=true` and ~30 other JS signals (missing plugins, incorrect WebGL renderer, abnormal timing). Returns CAPTCHA or blank pages.

**Observed:** After 22+ hours of unpatched Playwright scraping (AmzRankings run), AmzProducts saw 57% null title rate — every page was a CAPTCHA.

**CAPTCHA signals to detect:**
- URL redirect to `/errors/validateCaptcha`
- `csm-captcha-instrumentation.min.js` in page body
- `api.auth-challenge.amazon.com` call in page body
- Text "To discuss automated access to Amazon data" on page
- Null `#productTitle` after full Playwright render

**Fix applied:** `playwright-stealth==2.0.3` via `playwright_page_init_callback` (must fire BEFORE `page.goto()`). Arity is `(page, request)` — NOT 3 args; wrong arity silently fails.

**Pitfall ref:** P30

---

### 1b — Fingerprint inconsistency from middleware (ROOT CAUSE — fixed P34)
**What happened:** playwright-stealth was applied, yet Amazon still blocked after ~1h45m / 160 products (2026-07-03 run). Investigation (2026-07-04) found the block was largely self-inflicted, not pure session-duration:

1. scrapy-playwright's default `PLAYWRIGHT_PROCESS_REQUEST_HEADERS = use_scrapy_headers` injected the **Scrapy request's UA** into Playwright's navigation request. `RandomUserAgentMiddleware` rotates through a pool including **`Chrome/59`, mobile, and bot UAs** — so the HTTP `User-Agent` contradicted the real Chromium-148 engine (`navigator.userAgent`, sec-ch-ua, TLS all said 148). A rotating-but-inconsistent UA is a bigger tell than a static consistent one.
2. The raw engine UA is **`HeadlessChrome/148`** — literal "Headless" = instant block (only masked before because the random middleware UA overwrote it).
3. Headless sec-ch-ua advertises only "Chromium" (no "Google Chrome" brand).

**Fix (P34):**
- `PLAYWRIGHT_PROCESS_REQUEST_HEADERS = None`; disable UA/header middlewares in Playwright mode.
- Per-context coherent `Chrome/148` Windows UA + `sec-ch-ua` override (adds "Google Chrome" brand). Verified consistent via httpbin.org/headers; zero "HeadlessChrome" in scraped HTML.
- 3 browser contexts, each its own zip-bootstrap → distinct cookie/session identities; product requests round-robin across them (each identity ~1/3 the request rate).
- `timezone_id = Asia/Kolkata` (matches unmasked India IP); randomized dwell.

Smoke test (2026-07-04): 6/6 products written, 0 blocks. **Full-run durability being measured** — see whether the session-duration block still appears now that the fingerprint is coherent and load is split across 3 identities.

**Still open (if blocking persists):** per-identity cookie accumulation over hours (contexts persist for the whole run — no time-based recycling yet), and the single fixed India IP.

---

### 1c — IP geo-location fingerprint
**What happens:** Amazon detects the requesting IP's country. From an Indian IP, amazon.com returns Indian-localised content: different category trees, Indian pricing, geo-specific product selection.

**Fix applied:** Set US delivery zip 19901 via the location popover on the homepage before any scraping. This forces Amazon to serve US-local buybox pricing and US category navigation.

**Constraint:** Only scrape within 11 AM–11 PM IST — IP is unmasked during this window and Amazon behaviour is predictable.

**Pitfall ref:** P26

---

## 2. Geo-Specific Content (Non-US IP)

### 2a — Category hierarchy differs by IP geolocation
**What happens:** Running `AmzCategoryHierarchy` from an Indian IP produces a category tree that does not match what US users see — wrong ancestors, missing subcategories, phantom depth (e.g. depth=48 for a node that should be at depth 3).

**Root cause:** Amazon's left-nav renders differently by IP. The zip 19901 bootstrap (homepage location popover) fixes this by setting a US delivery address, causing Amazon to serve US-local nav on all subsequent requests in the same Playwright context.

**Fix applied:** Playwright + zip 19901 bootstrap in `AmzCategoryHierarchy` (same pattern as `AmzProducts`). `use_playwright=true` is now the default.

**Pitfall ref:** P26

---

### 2b — Plain HTTP from non-US IP returns geo-localised product pages
**What happens:** Without Playwright and zip 19901, product detail pages (price, seller, buybox) are served with Indian pricing and sometimes Indian seller information.

**Fix applied:** Bootstrap request on `AmzProducts` startup: click `#glow-ingress-block`, fill `#GLUXZipUpdateInput` with `19901`, confirm. Zip cookie persists to all subsequent product pages in the same Playwright context.

---

## 3. Dynamic / JS-Rendered Content

### 3a — ACP lazy-loading: items 31–50 not in static HTTP response
**What happens:** Amazon bestseller pages only include 30 items in the initial HTML. Items 31–50 are loaded via Amazon Component Platform (ACP) — an internal JS framework that fires a POST to fetch the next batch. The POST requires a server-side session token registered by the browser's JS initialisation; it cannot be replicated by Scrapy alone.

**Fix applied:** scrapy-playwright. Playwright runs a real Chromium browser, executes the JS, and the page renders all 50 items.

**Pitfall ref:** P8

---

### 3b — `window.scrollTo()` does not trigger Amazon's lazy-load
**What happens:** Amazon uses `IntersectionObserver` (not a scroll event) to detect when the user has scrolled to the bottom and fire the ACP batch request. `window.scrollTo(0, document.body.scrollHeight)` fires a scroll event but does NOT trigger the observer — item count stays at 30.

**Fix applied:** Iterative `scrollIntoView` on the last visible card:
```javascript
cards[cards.length - 1].scrollIntoView({behavior: 'instant', block: 'end'});
await new Promise(r => setTimeout(r, 2000));
```
Repeat up to 5 times. Typical path: 30 → 38 → 46 → 50 in 3 passes.

**Pitfall ref:** P25

---

### 3c — `networkidle` never fires on Amazon product pages
**What happens:** Amazon continuously fires analytics and tracking XHR requests (Amplitude, CloudFront, Ads telemetry). `wait_for_load_state('networkidle')` waits for 500ms of network silence — which never happens. Page load hangs indefinitely.

**Fix applied:** `wait_for_load_state('load')` + fixed `wait_for_timeout(3000)` for the buybox AJAX to settle. Never use `networkidle` for any Amazon page.

**Pitfall ref:** P22

---

### 3d — `wait_for_selector` times out on Amazon's bot-detection cohort
**What happens:** Amazon's A/B testing assigns sessions to cohorts. Some cohorts receive a reduced page layout where the zip popover (`#GLUXZipUpdateInput`) or key product sections simply don't render. `wait_for_selector` times out at 15s.

**Observed:** Bootstrap failure on 1 of 3 smoke test runs (intermittent — fresh retry succeeded).

**Fix applied for products:** Sparse-page detection (`bsr_entries=None AND rating_breakdown=None`) triggers one retry with `dont_filter=True`. A new session cookie may hit a different cohort and serve the full layout.

**Fix for bootstrap timeout:** Currently: retry the full spider run. Longer-term: detect and handle CAPTCHA on the homepage before proceeding.

**Pitfall ref:** P26 (sparse page)

---

## 4. Page Structure & Selector Fragility

### 4a — Amazon URL structure changed (/zgbs/)
**What happens:** Category bestseller URLs changed from `/gp/bestsellers/<slug>/<id>` to `/Best-Sellers-<Title>/zgbs/<slug>/<id>`. URL helpers built on the old pattern extract `node_id=None` and follow no links.

**Fix applied:** Parse `zgbs` as the anchor segment.

**Pitfall ref:** P2

---

### 4b — ARIA roles and class names change without warning
**What happens:** Amazon redesigned the bestseller nav. `role="treeitem"` no longer exists. Category links became plain `<li>` elements with hashed CSS classes containing `zg-browse-item`.

**Fix applied:** Use substring selectors (`[class*="zg-browse-item"]`). Never use exact class names or ARIA roles on Amazon.

**Pitfall ref:** P3

---

### 4c — Buybox layout changed: 3 distinct patterns now exist
**What happens:** Amazon's buybox seller/FBA information appears in 3 different HTML layouts depending on seller type and page variant:
1. Third-party FBA: `#sellerProfileTriggerId` link
2. Tabular: `<span>Sold by:</span><span>Name</span>` sibling pair
3. Compact: `"Ships from and sold by Name."` in `span.a-color-secondary` text

The old `#merchant-info` free-text pattern stopped working for most products.

**Fix applied:** All 3 patterns handled in `_parse_seller_name()` and `_parse_is_fba()`.

**Pitfall ref:** P31

---

### 4d — Compact seller regex fails for seller names containing dots
**What happens:** The compact text pattern `"Ships from and sold by Amazon.com."` — the regex `[^.]+?` (no dots) can't match `Amazon.com`. Seller is extracted as `None` for all Amazon-sold products.

**Fix applied:** Changed to `.+?` with terminator `(?:\s+and\s+ships from|\.\s*$)`.

**Pitfall ref:** P33

---

### 4e — Book/media brand extraction returns "by" instead of author name
**What happens:** For books, maps, and media products, `#bylineInfo`'s first direct text node is the word "by" (not the brand). The author's name is in a child `<a>` element. `#bylineInfo::text` returns "by".

**Fix applied:** Added `#bylineInfo .author a::text` fallback before the generic text return. Added guard `text.lower() not in ('by', '')` to prevent returning the bare word "by".

**Pitfall ref:** P32

---

### 4f — `last_month_sales` badge text is split across child elements
**What happens:** Amazon splits the count and label: `<span><span>20K+</span> bought in past month</span>`. `contains(text(), "bought in past month")` finds zero matches — the count is in a child element and the parent's direct text is just " bought in past month".

**Fix applied:** `contains(., ...)` (all descendant text) + innermost-element constraint + `string()` to reconstruct full text.

**Pitfall ref:** P24

---

## 5. Category Hierarchy Correctness

### 5a — Sibling nodes treated as children during traversal
**What happens:** Amazon's bestseller nav shows three sets of links on every category page: ancestors, the currently-selected node, and siblings (other children of the current node's parent). Iterating all `zg-browse-item` items picks up siblings and attributes them as children of the current node — producing ancestor chains of depth 48 for nodes that are 3 levels deep.

**Fix applied:** XPath targeting only the children wrapper `<li>` (identified by containing a `<ul>`):
```
//span[@aria-current="page"]/ancestor::li[1]/following-sibling::li[1][.//ul]//a
```

**Pitfall ref:** P27

---

### 5b — Amazon DAG nodes appear in multiple root category navs
**What happens:** Some browse nodes (e.g. "Garage Storage") legitimately appear in both Home & Kitchen and Tools & Home Improvement navs. Running two separate Scrapy processes resets `visited_node_ids` between runs — the same node gets claimed under two different root slugs, producing conflicting ancestry chains in the DB.

**Fix applied:** Always run all categories in a single Scrapy process so `visited_node_ids` is shared. Added `root_url_slug` meta guard to block following cross-root links.

**Pitfall ref:** P28

---

### 5c — Partial restart re-claims already-owned nodes
**What happens:** Restarting `AmzCategoryHierarchy` with a subset of categories resets `visited_node_ids` to empty. DAG nodes already owned by completed categories can be re-claimed under the restarted category's slug.

**Fix applied:** `spider_opened` pre-populates `visited_node_ids` from DB for all non-target categories before any scraping starts.

**Pitfall ref:** P29

---

### 5d — Shared URL slug causes root node_id collision
**What happens:** Home & Kitchen, Kitchen & Dining, and Tools & Home Improvement all resolve to the same URL slug `/hi/`. `_node_id(url)` returns `hi` for all three — each root overwrites the previous row in `amz_category`. One root survives; the controller assigns all nodes to the wrong category.

**Fix applied:** Use the category display name (unique across all targets) as the root `node_id` instead of the URL slug.

**Pitfall ref:** P21

---

### 5e — Cross-category contamination via eligibility query
**What happens:** If hierarchy nodes have wrong root ancestry (from P21/P27), the eligibility query for `AmzRankings` expands to node_ids belonging to a different category. Without a category constraint, a run for "Arts & Crafts" scrapes nodes from "Home & Kitchen".

**Fix applied:** Resolve root categories by querying the controller's `category` column on matched IDs (not the expanded closure table). Add `AND ctrl.category = ANY(root_categories)` to the eligibility query.

**Pitfall ref:** P20

---

## 6. Data Extraction Bugs

### 6a — BSR section absent on some product pages
**What happens:** Some products (particularly books, maps, media) do not have a "Best Sellers Rank" section in the product details table. BSR rank values are unavailable.

**Behaviour:** `_parse_bsr()` returns `None`; spider falls back to `_parse_breadcrumb_categories()` which extracts the breadcrumb path as BSR entries with `rank=None` and `from_breadcrumb=True`.

**Status:** Expected behaviour. The breadcrumb fallback provides category context even without a rank number.

---

### 6b — Seller name / FBA null for out-of-stock products
**What happens:** Products with no active seller (out of stock, discontinued) have an empty buybox — no `#sellerProfileTriggerId`, no "Sold by" text. `seller_name=None`, `is_fba=None`.

**Status:** Correct behaviour — no seller info is available to extract. Observed null rate: ~1–3% of scraped products.

---

### 6c — Variant ASIN discovery: most are already in queue
**What happens:** `variant_asins` extracted from `dimensionToAsinMap` in product page scripts. Spider attempts to insert all discovered variants into the queue (`ON CONFLICT DO NOTHING`). In the production run, 7,615 variants were discovered but only 147 were actually new — the rest were already in the queue from `AmzRankings`.

**Status:** Working as designed. `variants_queued` in spider logs counts attempted inserts, not successful new insertions.

---

## 7. Throughput & Operational Constraints

### 7a — Very low Playwright throughput
**Current rate:** ~1 product per 15–17 seconds (DOWNLOAD_DELAY=12s + 3s buybox wait, 4 concurrent pages but effectively sequential when all are in the wait phase).

**Scale problem:** 425,572 ASINs at 4/min = ~1,775 hours = ~74 days. Not viable for a single machine with no proxies.

**Options not yet implemented:**
- Proxy rotation (Oxylabs middleware already in `middlewares.py` — needs credentials)
- `use_playwright=false` for the 15 static fields first, then a smaller Playwright pass for the 4 JS fields
- Multiple parallel machines

---

### 7b — Session-based blocking limits continuous run to ~1.5–2h
**Current state:** Spider gets blocked after ~1h45m of continuous Playwright scraping (160 products). Must be restarted to get a fresh browser context.

**Per-day capacity estimate:** 3–4 sessions of 1.5h each within the 11 AM–11 PM window = ~500–650 products/day. At that rate: 425K ASINs ≈ 650–850 days.

**This is the primary blocker for Phase 0 completion.**

---

### 7c — Chromium OOM on very long runs
**What happens:** Chromium's V8 heap grows unbounded over many hours of Playwright scraping. Memory usage increases progressively until OOM crash.

**Mitigation applied:** Resource blocking (images, media, fonts, stylesheets) significantly reduces memory growth. `PLAYWRIGHT_MAX_PAGES_PER_CONTEXT=4`.

**Remaining risk:** Still present on multi-hour runs. Regular restarts avoid it.

**Pitfall ref:** P10

---

### 7d — India IP constraint: 11 AM–11 PM IST only
**What happens:** IP is unmasked — running outside the window increases CAPTCHA risk significantly.

**Status:** Operational constraint. No fix — accept the window.

---

## 8. Scrapy & Infrastructure Issues

### 8a — Scrapy 2.13+ silently ignores `start_requests()`
**What happens:** `start_requests()` is ignored. Spider starts and immediately closes with 0 pages crawled, no error.

**Fix:** Always use `async def start(self)` not `def start_requests(self)`.

**Pitfall ref:** P1

---

### 8b — `from_crawler` must call `_set_crawler()`
**What happens:** Scrapy 2.16 sets `spider.crawler` via `spider._set_crawler(crawler)`. If overriding `from_crawler` without calling it, `spider.crawler` doesn't exist and the dupe filter crashes with `AttributeError` on the first duplicate URL — only at scale, silent on small test runs.

**Fix:** Always call `spider._set_crawler(crawler)` in `from_crawler` overrides.

**Pitfall ref:** P9

---

### 8c — Windows Defender blocks Chrome Headless Shell
**What happens:** `headless=True` selects Chrome Headless Shell on Windows. Windows Defender/SmartScreen silently blocks it. Error: `Executable doesn't exist` or `spawn UNKNOWN`.

**Fix:** `headless=False, args=['--headless=new', '--disable-gpu']` — uses Chrome for Testing instead.

**Pitfall ref:** P13

---

### 8d — psycopg2 does not auto-rollback on DB error
**What happens:** After one DB error, all subsequent operations fail with `InFailedSqlTransaction` until an explicit rollback. If not handled, the spider silently fails to write any subsequent products.

**Fix:** Every DB method wraps in try/except with `connection.rollback()` in the except block.

**Pitfall ref:** P5

---

### 8e — `ON CONFLICT DO UPDATE` fails on within-batch duplicates
**What happens:** `execute_values` with `ON CONFLICT DO UPDATE` fails if the same conflict key appears twice in one batch. Amazon's DAG hierarchy can produce this in closure table writes.

**Fix:** Deduplicate by conflict key before calling `execute_values`.

**Pitfall ref:** P6

---

### 8f — `pkill` does not work on Windows
**What happens:** `pkill -f "scrapy crawl"` exits 0 but the spider keeps running.

**Fix:** Kill by PID via `wmic` or Task Manager.

**Pitfall ref:** P7

---

### 8g — `wait_for_selector` for zip bootstrap is intermittently slow
**What happens:** The zip popover (`#GLUXZipUpdateInput`) sometimes takes >15s to appear after clicking `#glow-ingress-block`. Bootstrap times out; spider exits with `products_written=0`.

**Observed:** 1 of 3 smoke test runs failed at bootstrap; retry succeeded. Cause is intermittent — likely Amazon serving a slow or different homepage variant to the Playwright context.

**Current fix:** Retry the run. Longer-term: increase timeout to 30s, or add a homepage CAPTCHA check before the bootstrap attempt.

---

*Technical detail for all P-numbered items: `docs/scraping_pitfalls.md` (P1–P7, P21) and `scraping/CLAUDE.md` Key Pitfalls section (P8–P33).*
