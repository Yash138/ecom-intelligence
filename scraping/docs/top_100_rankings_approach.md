# Top-100 Rankings — Full Scrape Approach

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-06-09 | All | Initial creation — root cause analysis, XHR approach post-mortem, Playwright implementation plan |

## Table of Contents

- [Current State](#current-state)
- [Root Cause — Why Only 60](#root-cause--why-only-60)
- [What We Already Tried](#what-we-already-tried)
- [The Fix — Playwright Integration](#the-fix--playwright-integration)
- [Implementation Plan](#implementation-plan)
- [Pipeline Changes Required](#pipeline-changes-required)
- [Open Questions Before Implementation](#open-questions-before-implementation)

---

## Current State

`AmzRankings` spider scrapes **60 products per node** (30 per page × 2 pages) instead of the expected 100.

Amazon's bestseller page shows 50 products per page. Only the first 30 are in the server-side HTML. The remaining 20 are lazy-loaded when the user scrolls to the bottom of the grid. Following `li.a-last > a` for pagination is correct and gives us 2 pages, but each page is still capped at 30.

---

## Root Cause — Why Only 60

Amazon's ranking grid uses an internal component called **ACP (Amazon Component Platform)**, specifically `p13n-zg-list-grid-desktop`. This is how it works in a real browser:

1. Browser receives the initial HTML — contains 30 products and the ACP widget config (`data-acp-path`, `data-acp-params` embedded as HTML attributes).
2. Browser executes JavaScript — the ACP widget initialises, registers a session state with Amazon's servers ("page loaded, grid initialised").
3. User scrolls to the bottom of the 30 visible products — JS fires a POST to `/acp/p13n-zg-list-grid-desktop/.../nextPage?page-type=zeitgeist&stamp=...`.
4. Amazon's ACP server validates the request against the registered session state and returns items 31–50 as an HTML fragment (same structure as the main page).

**Scrapy skips step 2 entirely.** It has no JS engine. When we fire the POST directly, Amazon receives it but finds no initialised widget state for our session — it returns an empty `<ol><videoResponsePlaceholder></ol>` (165 bytes). The `tok` token embedded in the HTML validates (no 403), but the server won't page a widget that was never initialised.

This is not a headers problem or a missing cookie problem. It is a fundamental JS-initialisation requirement.

---

## What We Already Tried

### Approach 1 — Construct `?pg=2` manually
Manually appended `?pg=2` to the page 1 URL. Worked for pagination (got page 2) but did not solve the 30-item-per-page cap.

### Approach 2 — Follow `li.a-last > a`
Switched to Amazon's native next-page link. Confirmed correct — uses the proper `/zgbs/` path with `ref=` params. Still 30 items per page because the cap is within-page, not across pages. **This change is kept** — it is the correct pagination approach.

### Approach 3 — Direct ACP XHR POST
Extracted `data-acp-path` and `data-acp-params` from the initial HTML and fired a POST to the `nextPage` endpoint with the required headers (`X-Amz-Acp-Params`, `X-Requested-With`, `Referer`).

Result: **200 OK, empty list**. Amazon accepted the request but returned no products. The ACP server requires the widget to have been JS-initialised in the same session before it will serve the lazy batch. Without JS execution in step 2, the session state is never registered. This code has been reverted from the spider.

---

## The Fix — Playwright Integration

Use `scrapy-playwright` to render the initial page with a real Chromium browser. Playwright executes JS, which:
- Initialises the ACP widget (step 2 above)
- Scrolls to the bottom of the grid (triggering the lazy load)
- Waits until all 50 product cards are present in the DOM

The response Scrapy sees is the fully rendered HTML with all 50 items. No separate XHR call needed — the DOM already has everything. The same `_extract_products` selectors work unchanged.

### Why not Selenium / Splash?
- **Selenium** — heavier, no async integration with Scrapy's async engine.
- **Splash** — unmaintained (last release 2021), no longer recommended.
- **scrapy-playwright** — first-class async integration, maintained, runs Chromium headlessly. Correct choice.

---

## Implementation Plan

### Step 1 — Install scrapy-playwright

```bash
pip install scrapy-playwright
playwright install chromium
```

Add to `requirements.txt` (or `pyproject.toml`).

### Step 2 — Enable Playwright in settings

In `settings.py`, add:

```python
DOWNLOAD_HANDLERS = {
    "http":  "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"  # already set
PLAYWRIGHT_BROWSER_TYPE = "chromium"
PLAYWRIGHT_LAUNCH_OPTIONS = {"headless": True}
```

### Step 3 — Create a Playwright page script

Write a reusable page script (`scraping/playwright_scripts/scroll_to_load.py` or inline) that:
1. Waits for the initial 30 product cards to render (`div[data-asin]`).
2. Scrolls to the bottom of the grid container.
3. Waits for the product count to reach 50 (or for a timeout, whichever comes first).

```python
async def scroll_and_wait(page):
    # Wait for initial grid
    await page.wait_for_selector('div[data-asin]')
    # Scroll to bottom of grid to trigger ACP lazy load
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    # Wait for 50 items or 5 second timeout
    try:
        await page.wait_for_function(
            "document.querySelectorAll('div[data-asin]').length >= 50",
            timeout=5000,
        )
    except Exception:
        pass  # Accept however many loaded within timeout
```

### Step 4 — Update `start()` in AmzRankings

Yield Playwright requests instead of plain Scrapy requests:

```python
yield scrapy.Request(
    url=url,
    callback=self.parse,
    meta={
        'playwright': True,
        'playwright_page_coroutines': [scroll_and_wait],
        ...existing meta...
    },
)
```

Page 2 requests (from `li.a-last > a`) also need `'playwright': True` and the same scroll script — page 2 also has only 30 items statically.

### Step 5 — Adjust download delay and concurrency

Playwright runs a real browser instance per request. This is significantly slower than plain HTTP. Recommended settings for production runs:

```python
DOWNLOAD_DELAY = 15              # up from 8
CONCURRENT_REQUESTS = 4          # down from 16; each is a full browser tab
PLAYWRIGHT_MAX_PAGES_PER_CONTEXT = 4
```

---

## Pipeline Changes Required

| Component | Change | File |
|---|---|---|
| `settings.py` | Add `DOWNLOAD_HANDLERS`, `PLAYWRIGHT_*` settings | `scraping/settings.py` |
| `requirements.txt` | Add `scrapy-playwright` | `requirements.txt` |
| `AmzRankings.__init__` | Add `use_playwright` bool param (default `True`) to allow toggling off for quick runs | `spiders/amz_rankings.py` |
| `AmzRankings.start()` | Pass `playwright=True` and page coroutine in meta | `spiders/amz_rankings.py` |
| `AmzRankings.parse_xhr()` | Remove entirely (XHR approach abandoned) | `spiders/amz_rankings.py` |
| `AmzRankings.handle_xhr_error()` | Remove entirely | `spiders/amz_rankings.py` |
| `AmzRankings.parse()` | Remove ACP XHR block; keep only `li.a-last > a` pagination | `spiders/amz_rankings.py` |
| Playwright scroll script | New file or inline coroutine | `spiders/amz_rankings.py` or `playwright_scripts/` |
| `README.md` | Add Playwright install steps; update product count from 60 to ~100 | `scraping/README.md` |
| `scraping_pitfalls.md` | Add P8 — JS lazy loading requires Playwright; plain Scrapy caps at 30/page | `docs/scraping_pitfalls.md` |

---

## Open Questions Before Implementation

1. **Proxy compatibility** — Playwright requires proxy config via `PLAYWRIGHT_LAUNCH_OPTIONS`. Oxylabs proxy integration needs to be verified when proxies are enabled. Plain Scrapy's proxy middleware does not apply automatically to Playwright requests.

2. **Detection risk** — Headless Chromium is detectable. May need `playwright-stealth` (`pip install playwright-stealth`) to mask headless signals (navigator.webdriver, etc.). Evaluate after first test run.

3. **Toggle flag** — Add `-a use_playwright=false` to allow running the spider in fast/cheap mode (60 products, no browser) for controller seeding runs or re-scrapes where full coverage is not needed.

4. **Memory** — Chromium uses ~150–300 MB per page. With `CONCURRENT_REQUESTS=4`, peak RAM usage will be ~1.2 GB. Acceptable on a development machine; worth noting for any future cloud deployment.
