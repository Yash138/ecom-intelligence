# Possible Enhancements

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-06-09 | All | Initial creation — deferred items, concerns, and when to revisit |

## Table of Contents

- [Detection Risk — Playwright Stealth](#detection-risk--playwright-stealth)
- [Proxy Compatibility with Playwright](#proxy-compatibility-with-playwright)
- [Memory Usage at Scale](#memory-usage-at-scale)
- [Other Deferred Items](#other-deferred-items)

---

## Detection Risk — Playwright Stealth

**Status: Next step after Playwright is confirmed working.**

Headless Chromium exposes signals that Amazon's bot detection can read:
- `navigator.webdriver = true` — the most reliable bot signal; real browsers set this to `undefined`
- Consistent viewport/resolution fingerprint across all requests
- Missing browser plugins, fonts, and other browser entropy that real users have
- CDP (Chrome DevTools Protocol) artifacts in timing patterns

**Fix:** `playwright-stealth` patches these signals on the Playwright page object before any navigation.

```bash
pip install playwright-stealth
```

Usage in spider:

```python
from playwright_stealth import stealth_async

# In the page coroutine or PageMethod:
PageMethod('add_init_script', stealth_script)
# or via a custom async coroutine that calls stealth_async(page)
```

**When to implement:** After first Playwright test run. If Amazon blocks or serves CAPTCHAs during the run, add stealth immediately. If not, defer — it adds a dependency and some overhead.

---

## Proxy Compatibility with Playwright

**Status: Deferred — proxies are currently disabled. Revisit when proxies are enabled.**

### Why it is a concern

With plain Scrapy, proxies work through `HttpProxyMiddleware` in the middleware chain. Scrapy is a Python HTTP client — every request passes through middleware before being sent. Configuring a proxy via middleware (or `PROXY` setting) routes all Scrapy requests through it automatically.

With `scrapy-playwright`, Playwright launches a real **Chromium browser process**. That browser makes its own TCP connections directly, bypassing Scrapy's middleware chain entirely. So `HttpProxyMiddleware`, `RandomizedProxyMiddleware`, and any Scrapy-level proxy config have **zero effect** on Playwright requests.

To proxy Playwright requests, the proxy must be configured at the browser launch level:

```python
PLAYWRIGHT_LAUNCH_OPTIONS = {
    "headless": True,
    "proxy": {
        "server": "http://us-pr.oxylabs.io:20001",
        "username": "your_proxy_user",
        "password": "your_proxy_password",
    }
}
```

### The sticky-session complication

The current project uses Oxylabs sticky sessions via ports 20001–20008 (see `settings.py`). The `RandomizedProxyMiddleware` rotates across these ports per request. With Playwright, a single proxy config is set at browser launch time and applies to the entire browser session — port rotation logic from the middleware does not carry over.

### What needs to change when proxies are enabled

1. Add proxy config to `PLAYWRIGHT_LAUNCH_OPTIONS` in the spider's `from_crawler` when `use_playwright=True`.
2. Decide on port assignment strategy — either fix one port for the Playwright browser or implement per-context proxy rotation (Playwright supports per-context proxy config, which is more complex but allows rotation).
3. Verify that Oxylabs sticky-session behaviour (keeping the same exit IP for a session) still applies when configuring via browser launch options vs. HTTP headers.
4. The Scrapy middleware proxy config still applies to any non-Playwright requests (e.g. if `use_playwright=false` mode is used or future spiders run without Playwright).

---

## Memory Usage at Scale

**Status: Note — acceptable on dev machine, revisit before cloud deployment.**

Headless Chromium uses approximately 150–300 MB of RAM per active page. With `CONCURRENT_REQUESTS=4` (the setting applied when `use_playwright=True`), peak RAM is **~600 MB–1.2 GB** during a scrape run.

On a development machine with 16+ GB RAM this is fine. On a cloud VM (Hetzner CX21 = 4 GB RAM), this is the full RAM budget.

**When to revisit:** Before any cloud deployment. Options at that point:
- Use Hetzner CX31 (8 GB) or larger.
- Reduce `PLAYWRIGHT_MAX_PAGES_PER_CONTEXT` to 2 and `CONCURRENT_REQUESTS` to 2 (halves RAM, halves throughput).
- Consider splitting the scrape run across multiple VM instances if throughput matters.

---

## Other Deferred Items

| Item | Why deferred | When to revisit |
|---|---|---|
| `playwright-stealth` | Not needed until bot blocking is confirmed | After first Playwright test run |
| Per-context proxy rotation | Proxy is disabled; complex to implement | When proxies are enabled |
| Playwright on `AmzProducts` | Not built yet | When AmzProducts spider is started |
| Distributed scraping (multiple VMs) | Volume doesn't justify it yet | Phase 1 full category runs (840K ASINs) |
| Scrapy-cloud / Scrapyd deployment | Local-first strategy; cloud deferred | When local machine is a bottleneck |
