"""
AmzProducts spider
==================
Scrapes Amazon product detail pages for ASINs queued in
transformed.amz_product_scrape_queue.

Source: transformed.amz_product_scrape_queue (seeded via seed_product_queue.sql)
Output: staging.amz_product_snapshot (UPSERT — one row per ASIN, updated on re-scrape)

Requires Playwright (use_playwright=true, default) to render JS-dependent fields:
  price, seller_name, seller_id, is_fba.
  Zip code 19901 must be set before product pages load so Amazon serves US-local
  buybox pricing. A bootstrap request to amazon.com sets the zip via the location
  popover; cookies persist to all subsequent product page requests in the same
  Playwright browser context.

Queue management:
  - Row is DELETED from queue on successful scrape + DB write.
  - Row remains in queue on any error (request failure or DB failure).
  - Variant ASINs discovered during scraping are INSERTED to queue (ON CONFLICT DO NOTHING).

Usage:
    cd scraping/

    # Standard run — Playwright, all pending ASINs
    scrapy crawl AmzProducts -a marketplace_id=amazon_us

    # Test run — 2 products only
    scrapy crawl AmzProducts -a limit=2 -s LOG_FILE=logs/test_products.log

    # Save rendered HTML to default path (html_archive/ in repo root)
    scrapy crawl AmzProducts -a limit=5 -a save_html=true

    # Save rendered HTML to a custom path
    scrapy crawl AmzProducts -a save_html=true -a html_archive_dir=D:/html_dumps/owala

    # Plain HTTP (static fields only; price/seller/is_fba will be NULL)
    scrapy crawl AmzProducts -a use_playwright=false -a limit=10

MUST READ before modifying: docs/scraping_pitfalls.md
  P1  — use async def start(), not start_requests()
  P5  — psycopg2 rollback on every error (in PostgresDBHandler)
  P9  — from_crawler must call _set_crawler()
  P12 — block images/fonts/stylesheets in Playwright to prevent OOM
  P13 — Windows: headless=False + --headless=new arg
"""

import os
import re
import uuid
import json
import random
import scrapy
from scrapy import signals
from datetime import datetime as dt

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

from helpers.postgres_handler import PostgresDBHandler
from helpers.delay_handler import DelayHandler


# ---------------------------------------------------------------------------
# Fingerprint constants (P34)
# ---------------------------------------------------------------------------
# The installed Playwright Chromium reports UA "HeadlessChrome/148.0.0.0" — the
# "Headless" token is an instant bot tell. Overriding the context user_agent
# removes it AND makes navigator.userAgent match the HTTP header. The engine is
# Chromium 148, so we claim Chrome/148 to stay consistent with sec-ch-ua.
#
# Real headless Chromium advertises only "Chromium" in sec-ch-ua (no "Google
# Chrome" brand). Overriding sec-ch-ua via extra_http_headers restores the
# "Google Chrome" brand so client hints match a genuine Chrome install.
# Verified against httpbin.org/headers — see P34 in scraping/CLAUDE.md.
_CHROME_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36'
)
_SEC_CH_UA = '"Chromium";v="148", "Google Chrome";v="148", "Not.A/Brand";v="99"'
_SEC_CH_HEADERS = {
    'sec-ch-ua': _SEC_CH_UA,
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
}
# Common desktop viewports — one per context for a little extra entropy.
# Kept coherent with a Windows desktop; do NOT vary OS/engine (would create
# navigator.platform / sec-ch-ua-platform mismatches on this Chromium binary).
_VIEWPORTS = [
    {'width': 1920, 'height': 1080},
    {'width': 1536, 'height': 864},
    {'width': 1440, 'height': 900},
    {'width': 1366, 'height': 768},
]
_N_CONTEXTS = 3   # distinct browser contexts = distinct cookie/session identities


class AmzProductsSpider(scrapy.Spider):
    name = "AmzProducts"
    allowed_domains = ["www.amazon.com"]

    ZIP_CODE = '19901'       # US delivery zip — must be set before product pages load
    AMAZON_HOME = 'https://www.amazon.com'

    custom_settings = {
        "ITEM_PIPELINES": {},           # spider writes directly to DB
        "DOWNLOAD_DELAY": 12,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "CONCURRENT_REQUESTS": 16,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 16,
        "RETRY_TIMES": 1,
        "RETRY_DELAY": 15,
        "RETRY_HTTP_CODES": [429, 503, 403],
        "DOWNLOADER_MIDDLEWARES": {
            'scrapy.downloadermiddlewares.useragent.UserAgentMiddleware': None,
            'scrapy_user_agents.middlewares.RandomUserAgentMiddleware': 400,
            'scrapy.downloadermiddlewares.defaultheaders.DefaultHeadersMiddleware': None,
            'middlewares.HeaderRotationMiddleware': 500,
            'scrapy.downloadermiddlewares.cookies.CookiesMiddleware': 700,
        },
    }

    def __init__(self, marketplace_id='amazon_us', use_playwright='true',
                 limit=None, save_html='false', html_archive_dir=None, **kwargs):
        super().__init__(**kwargs)
        self.marketplace_id = marketplace_id

        self.use_playwright = str(use_playwright).lower() not in ('false', '0', 'no')
        if self.use_playwright and not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "use_playwright=true but scrapy-playwright is not installed.\n"
                "Run: pip install scrapy-playwright && playwright install chromium"
            )

        self.limit = int(limit) if limit else None
        self.save_html = str(save_html).lower() not in ('false', '0', 'no')
        self._html_archive_dir_param = html_archive_dir  # None → default path resolved in spider_opened

        self.run_id = uuid.uuid4()
        self.run_date = dt.now().date()
        self.products_written = 0
        self.variants_queued = 0
        self._scraped_asins: list[str] = []
        self.consecutive_blocked = 0
        self.total_blocked = 0

        # Fingerprint rotation (P34): N browser contexts, each a distinct
        # cookie/session identity. Built here, bootstrapped (zip set) in start().
        self.n_contexts = _N_CONTEXTS if self.use_playwright else 1
        self._contexts = self._build_contexts()
        self._contexts_done = 0            # bootstraps completed (ready or failed)
        self._ready_context_names: list[str] = []
        self._queue_rows: list[dict] = []  # loaded once in spider_opened

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = cls(*args, **kwargs)
        spider._set_crawler(crawler)   # Scrapy 2.16: sets spider.crawler + spider.settings
        spider.db = PostgresDBHandler(
            host=crawler.settings.get('POSTGRES_HOST'),
            database=crawler.settings.get('POSTGRES_DATABASE'),
            user=crawler.settings.get('POSTGRES_USERNAME'),
            password=crawler.settings.get('POSTGRES_PASSWORD'),
            port=crawler.settings.get('POSTGRES_PORT'),
        )

        if spider.use_playwright:
            crawler.settings.setdict({
                'DOWNLOAD_HANDLERS': {
                    'http':  'scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler',
                    'https': 'scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler',
                },
                'PLAYWRIGHT_BROWSER_TYPE': 'chromium',
                'PLAYWRIGHT_LAUNCH_OPTIONS': {
                    'headless': False,
                    'args': ['--headless=new', '--disable-gpu'],
                },
                'PLAYWRIGHT_MAX_PAGES_PER_CONTEXT': 2,
                # Block resources that add no scraping value. Images alone cause
                # significant memory growth over long runs (P12).
                'PLAYWRIGHT_ABORT_REQUEST': (
                    lambda req: req.resource_type in ('image', 'media', 'font', 'stylesheet')
                ),
                # P34: hand full header control to the browser. The default
                # `use_scrapy_headers` injects the Scrapy request's UA/headers
                # into Playwright's navigation request — which meant
                # RandomUserAgentMiddleware was sending e.g. "Chrome/59 on Win7"
                # (and mobile/bot UAs) on a Chromium-148 engine: a glaring
                # inconsistency. None => browser sends its own coherent headers,
                # using the per-context user_agent + sec-ch-ua we set.
                'PLAYWRIGHT_PROCESS_REQUEST_HEADERS': None,
                # Disable UA randomization + header rotation in Playwright mode —
                # identity is now owned by the browser context, not middleware.
                'DOWNLOADER_MIDDLEWARES': {
                    'scrapy.downloadermiddlewares.useragent.UserAgentMiddleware': None,
                    'scrapy_user_agents.middlewares.RandomUserAgentMiddleware': None,
                    'scrapy.downloadermiddlewares.defaultheaders.DefaultHeadersMiddleware': None,
                    'middlewares.HeaderRotationMiddleware': None,
                    'scrapy.downloadermiddlewares.cookies.CookiesMiddleware': 700,
                },
                'DOWNLOAD_DELAY':                  12,
                'RANDOMIZE_DOWNLOAD_DELAY':        True,
                'CONCURRENT_REQUESTS':             4,
                'CONCURRENT_REQUESTS_PER_DOMAIN':  4,
            }, priority='spider')

        crawler.signals.connect(spider.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(spider.spider_closed, signal=signals.spider_closed)
        return spider

    # ------------------------------------------------------------------
    # Context / fingerprint setup (P34)
    # ------------------------------------------------------------------

    def _build_contexts(self) -> list:
        """
        Build N browser-context definitions. Each is a distinct cookie/session
        identity to Amazon. All share the same engine-consistent Chrome/148
        Windows UA + sec-ch-ua (varying those would create client-hint/JS
        mismatches on this single Chromium binary); identity separation comes
        from the isolated context (own cookies + session-id) and a distinct
        viewport per context.
        """
        contexts = []
        for i in range(self.n_contexts):
            contexts.append({
                'name': f'ctx{i}',
                'kwargs': {
                    'user_agent': _CHROME_UA,
                    'extra_http_headers': dict(_SEC_CH_HEADERS),
                    'viewport': dict(_VIEWPORTS[i % len(_VIEWPORTS)]),
                    'locale': 'en-US',
                    # Timezone matches the (unmasked) India IP, NOT the US delivery
                    # zip. A US timezone on an Indian IP is a classic proxy/bot
                    # mismatch signal. Persona: Indian machine, US delivery address.
                    'timezone_id': 'Asia/Kolkata',
                },
            })
        return contexts

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def spider_opened(self, spider):
        self.db.connect()
        if self._html_archive_dir_param:
            self._html_archive_dir = os.path.abspath(self._html_archive_dir_param)
        else:
            _spiders_dir = os.path.dirname(os.path.abspath(__file__))
            self._html_archive_dir = os.path.normpath(
                os.path.join(_spiders_dir, '..', 'html_archive')
            )
        if self.save_html:
            os.makedirs(self._html_archive_dir, exist_ok=True)

        # DelayHandler tracks bad responses and adjusts download slot delay live.
        # initial_delay matches the Playwright DOWNLOAD_DELAY set in from_crawler.
        # max_none_counter=3: switch from linear (+1s) to exponential growth after
        # 3 bad responses within the 5-minute window.
        self.delay_handler = DelayHandler(
            initial_delay=12,
            max_none_counter=3,
            time_window=300,
            log=self.log,
            crawler=self.crawler,
        )

        # Load the product queue once — sharded/round-robined across contexts
        # later in _emit_product_requests().
        self._queue_rows = self._load_queue_rows()

        self.log(
            f"DB connected. run_id={self.run_id} "
            f"marketplace_id={self.marketplace_id} "
            f"use_playwright={self.use_playwright} "
            f"limit={self.limit} "
            f"n_contexts={self.n_contexts} "
            f"queue_rows={len(self._queue_rows)} "
            f"save_html={self.save_html} "
            f"html_archive_dir={self._html_archive_dir}",
            20,
        )

    def _load_queue_rows(self) -> list:
        """Read pending ASINs from the queue (ordered, optional limit)."""
        query = """
            SELECT marketplace_id, asin, product_url
            FROM transformed.amz_product_scrape_queue
            WHERE marketplace_id = %s
            ORDER BY added_at ASC
        """
        params: list = [self.marketplace_id]
        if self.limit:
            query += " LIMIT %s"
            params.append(self.limit)
        rows = self.db.read(query=query, params=tuple(params))
        if not rows:
            self.log(
                "Queue is empty — nothing to scrape. Run seed_product_queue.sql first.",
                30,
            )
        return rows or []

    def spider_closed(self, spider, reason):
        self._write_monitoring_stats()
        self.log(
            f"Spider closed [{reason}]. "
            f"total_blocked={self.total_blocked} "
            f"products_written={self.products_written} "
            f"variants_queued={self.variants_queued}",
            20,
        )
        self.db.close()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    async def start(self):
        """
        use_playwright=True: yield ONE bootstrap request per browser context.
        Each bootstrap sets the delivery zip to 19901 via the location popover;
        the zip cookie persists to all product pages in that context. Product
        requests are emitted (round-robin across ready contexts) only after all
        bootstraps resolve — see _context_ready() / _emit_product_requests().

        Each context is a distinct cookie/session identity to Amazon (P34), so
        product load is spread across N identities rather than one.

        use_playwright=False: yield product requests directly (static fields only;
        price/seller/is_fba will be NULL).
        """
        if not self.use_playwright:
            for row in self._queue_rows:
                yield self._product_request(row['asin'], row['product_url'])
            return

        for ctx in self._contexts:
            yield scrapy.Request(
                url=self.AMAZON_HOME,
                callback=self._context_ready,
                errback=self._bootstrap_error,
                dont_filter=True,   # N identical URLs — must bypass the dupe filter
                meta={
                    'playwright': True,
                    'playwright_context': ctx['name'],
                    'playwright_context_kwargs': ctx['kwargs'],
                    'playwright_page_init_callback': apply_stealth,
                    'ctx_name': ctx['name'],
                    'playwright_page_methods': [
                        PageMethod('wait_for_load_state', 'load'),
                        # Let the homepage settle before clicking — the glow
                        # ingress block is sometimes not yet interactive at 1.5s,
                        # which is the main cause of the popover-never-opens
                        # bootstrap failures (context lost).
                        PageMethod('wait_for_timeout', 2500),
                        PageMethod('click', '#glow-ingress-block'),
                        # wait_for_selector is more robust than a fixed timeout:
                        # if the popover never opens (slow load, bot block), we get
                        # a clear error immediately rather than a cryptic fill()
                        # timeout 30s later (P23).
                        PageMethod('wait_for_selector', '#GLUXZipUpdateInput',
                                   state='visible', timeout=20000),
                        PageMethod('fill', '#GLUXZipUpdateInput', self.ZIP_CODE),
                        PageMethod('wait_for_timeout', 500),
                        PageMethod('click', 'span#GLUXZipUpdate input.a-button-input'),
                        PageMethod('wait_for_timeout', 2500),
                    ],
                },
            )

    def _context_ready(self, response):
        """Bootstrap callback — one per context. Zip is now set for this context."""
        name = response.meta['ctx_name']
        # Use _is_captcha (NOT _is_blocked) — the homepage has no #productTitle,
        # which would false-positive the null-title branch of _is_blocked.
        if self._is_captcha(response):
            self.log(f"Bootstrap BLOCKED for context {name} — excluding it.", 40)
        else:
            self._ready_context_names.append(name)
            self.log(f"Context {name} ready (zip {self.ZIP_CODE} set).", 20)
        self._contexts_done += 1
        if self._contexts_done >= self.n_contexts:
            yield from self._emit_product_requests()

    def _bootstrap_error(self, failure):
        """Bootstrap errback — count it done so remaining contexts can proceed."""
        name = failure.request.meta.get('ctx_name', '?')
        self.log(f"Bootstrap failed for context {name}: {failure.value}", 40)
        self._contexts_done += 1
        if self._contexts_done >= self.n_contexts:
            yield from self._emit_product_requests()

    def _emit_product_requests(self):
        """Emit all queued product requests, round-robin across ready contexts."""
        ready = self._ready_context_names
        if not ready:
            self.log(
                "No contexts became ready (all bootstraps blocked/failed). "
                "Nothing to scrape — try again later in the IST window.",
                40,
            )
            return
        self.log(
            f"{len(ready)}/{self.n_contexts} context(s) ready: {ready}. "
            f"Emitting {len(self._queue_rows)} product requests round-robin.",
            20,
        )
        for idx, row in enumerate(self._queue_rows):
            ctx = ready[idx % len(ready)]
            yield self._product_request(row['asin'], row['product_url'], ctx=ctx)

    def _product_request(self, asin, product_url, ctx=None, sparse_retry=0):
        """Build one product-page Request (Playwright bound to `ctx` if given)."""
        meta: dict = {'asin': asin, 'product_url': product_url}
        if sparse_retry:
            meta['sparse_retry'] = sparse_retry
        if self.use_playwright:
            meta['playwright'] = True
            meta['playwright_page_init_callback'] = apply_stealth
            # wait_for_load_state('load') covers initial HTML + blocking scripts.
            # Randomized dwell (2.5–4.5s) lets the buybox AJAX (price/seller)
            # settle and mimics human variance rather than a fixed cadence.
            # networkidle is NOT used — Amazon fires continuous analytics XHR
            # that prevent it from ever firing (P22).
            meta['playwright_page_methods'] = [
                PageMethod('wait_for_load_state', 'load'),
                PageMethod('wait_for_timeout', random.randint(2500, 4500)),
            ]
            if ctx:
                meta['playwright_context'] = ctx
        return scrapy.Request(
            url=product_url,
            callback=self.parse_product,
            meta=meta,
            errback=self.handle_error,
            dont_filter=bool(sparse_retry),
        )

    @staticmethod
    def _is_captcha(response) -> bool:
        """
        Hard CAPTCHA / bot-block signals — valid on ANY Amazon page (incl. the
        homepage bootstrap, which has no product title).

          1. URL redirect to /errors/validateCaptcha
          2. CAPTCHA JS instrumentation script in body
          3. Auth-challenge API call in body (newer CAPTCHA flow)
          4. Bot-block landing page text
        """
        url = response.url
        text = response.text
        if '/errors/validateCaptcha' in url:
            return True
        if 'csm-captcha-instrumentation.min.js' in text:
            return True
        if 'api.auth-challenge.amazon.com' in text:
            return True
        if 'To discuss automated access to Amazon data' in text:
            return True
        return False

    @classmethod
    def _is_blocked(cls, response) -> bool:
        """
        Product-page block check: the hard CAPTCHA signals PLUS a null-title
        fallback (a rendered product page with no #productTitle is almost
        certainly a block, not a real listing). Do NOT use this for the homepage
        bootstrap — the homepage has no product title and would false-positive.
        """
        if cls._is_captcha(response):
            return True
        title_sel = response.css('#productTitle::text').get()
        if not title_sel or not title_sel.strip():
            return True
        return False

    def _handle_blocked(self, asin: str, response) -> None:
        """
        Called when _is_blocked() returns True.
        Increments backoff via DelayHandler, tracks consecutive count,
        and closes the spider after 10 consecutive blocks.
        ASIN is NOT deleted from queue — stays for retry on next run.
        """
        self.total_blocked += 1
        self.consecutive_blocked += 1
        self.delay_handler.handle_none_response([], None, response)
        self.log(
            f"  BLOCKED asin={asin} — consecutive={self.consecutive_blocked}/10 "
            f"total_blocked={self.total_blocked} current_delay={self.delay_handler.delay}s",
            30,
        )
        if self.consecutive_blocked >= 10:
            self.log(
                "10 consecutive blocked responses from Amazon — "
                "exponential backoff exhausted. Closing spider. "
                "Queue is intact; resume on next run within the 11AM–11PM IST window.",
                40,
            )
            self.crawler.engine.close_spider(self, 'blocked_by_amazon')

    def parse_product(self, response):
        """
        Parse one Amazon product detail page.
        Extracts all 20 fields, UPSERTs to staging.amz_product_snapshot,
        deletes from queue on success, queues discovered variant ASINs.
        """
        asin = response.meta['asin']
        sparse_retry = response.meta.get('sparse_retry', 0)

        self.log(f"Parsing product asin={asin} url={response.url}", 20)

        # Block detection — must run before any extraction attempt.
        # CAPTCHA / bot-block pages return garbage HTML; extraction would
        # produce null fields and write corrupt rows to staging.
        if self._is_blocked(response):
            self._handle_blocked(asin, response)
            return

        data = self._extract_product(response, asin)

        # Sparse-page detection: Amazon's A/B testing / bot-detection cohort
        # assignment can serve a reduced page layout where BSR, rating breakdown,
        # variants, and product details are entirely absent. The strongest signal
        # is bsr_entries=None AND rating_breakdown=None — both are mid-page
        # sections that Amazon renders regardless of login or price availability.
        # Retry once with a fresh request; the new session may hit a different
        # cohort and serve the full layout. Do NOT write to DB or delete from
        # queue until after the retry resolves.
        if (
            data.get('bsr_entries') is None
            and data.get('rating_breakdown') is None
            and sparse_retry == 0
        ):
            self.log(
                f"  asin={asin} sparse page (bsr_entries=None, rating_breakdown=None) "
                f"— retrying once with fresh request.",
                30,
            )
            # Reuse the SAME context so the zip 19901 cookie is still in effect.
            yield self._product_request(
                asin,
                response.meta['product_url'],
                ctx=response.meta.get('playwright_context'),
                sparse_retry=1,
            )
            return

        if sparse_retry > 0:
            if data.get('bsr_entries') is None and data.get('rating_breakdown') is None:
                self.log(
                    f"  asin={asin} still sparse after retry — writing what we have.",
                    30,
                )
            else:
                self.log(f"  asin={asin} retry recovered full layout.", 20)

        # Optionally save rendered HTML for debugging/replay
        html_file_path = None
        if self.save_html:
            html_file_path = os.path.abspath(
                os.path.join(self._html_archive_dir, f'{self.marketplace_id}_{asin}.html')
            )
            try:
                with open(html_file_path, 'w', encoding='utf-8') as f:
                    f.write(response.text)
            except Exception as e:
                self.log(f"WARNING: could not save HTML for asin={asin}: {e}", 40)
                html_file_path = None
        data['html_file_path'] = html_file_path

        # Write to DB — UPSERT. On failure, leave in queue for retry.
        try:
            self._write_product(data)
        except Exception as e:
            self.log(f"ERROR: DB write failed for asin={asin}: {e}", 40)
            return

        # Delete from queue after confirmed successful write
        try:
            self.db.execute(
                """
                DELETE FROM transformed.amz_product_scrape_queue
                WHERE marketplace_id = %s AND asin = %s
                """,
                (self.marketplace_id, asin),
            )
        except Exception as e:
            self.log(f"WARNING: could not delete asin={asin} from queue: {e}", 40)

        self.products_written += 1
        self._scraped_asins.append(asin)
        # Successful write — reset consecutive block counter and decay delay.
        self.consecutive_blocked = 0
        self.delay_handler.handle_successful_response(response)

        self.log(
            f"  asin={asin} written. "
            f"price={data.get('price')} "
            f"seller={data.get('seller_name')!r} "
            f"is_fba={data.get('is_fba')} "
            f"is_sb={data.get('is_small_business')} "
            f"bsr={len(data.get('bsr_entries') or [])} entries "
            f"variants={len(data.get('variant_asins') or [])}",
            20,
        )

        # Queue variant ASINs for future scraping
        if data.get('variant_asins'):
            self._queue_variant_asins(data['variant_asins'], asin)

    def handle_error(self, failure):
        """Log request errors. ASIN stays in queue for retry on next run."""
        asin = failure.request.meta.get('asin', 'bootstrap')
        self.log(
            f"Request failed for asin={asin} url={failure.request.url}: {failure.value}",
            40,
        )

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def _extract_product(self, response, asin: str) -> dict:
        """Orchestrate all field extractors. Returns dict ready for _write_product()."""
        return {
            'marketplace_id':   self.marketplace_id,
            'asin':             asin,
            'title':            self._parse_title(response),
            'brand':            self._parse_brand(response),
            'main_image_url':   self._parse_main_image(response),
            'launch_date':      self._parse_detail(response, 'Date First Available'),
            'about_this_item':  self._parse_about(response),
            'rating':           self._parse_rating(response),
            'review_count':     self._parse_review_count(response),
            'rating_breakdown': self._parse_rating_breakdown(response),
            'bsr_entries':      self._parse_bsr(response) or self._parse_breadcrumb_categories(response),
            'last_month_sales': self._parse_last_month_sales(response),
            'price':            self._parse_price(response),
            'seller_name':      self._parse_seller_name(response),
            'seller_id':        self._parse_seller_id(response),
            'is_fba':           self._parse_is_fba(response),
            'has_variants':     self._parse_has_variants(response),
            'variant_asins':    self._parse_variant_asins(response),
            'related_asins':    self._parse_related_asins(response),
            'weight':           self._parse_detail(response, 'Item Weight'),
            'dimensions':       self._parse_dimensions(response),
            'is_small_business': self._parse_is_small_business(response),
        }

    # ---- Static field parsers ----

    @staticmethod
    def _parse_title(response) -> str | None:
        return (response.css('#productTitle::text').get() or '').strip() or None

    @staticmethod
    def _parse_brand(response) -> str | None:
        """
        Two patterns for brand byline:
          - #bylineInfo is itself the <a> element: "Visit the {Brand} Store" or "Brand: X"
          - #bylineInfo absent (Amazon-sold or some listings): fall back to
            "Brand Name" row in the product detail table.
        """
        text = (response.css('#bylineInfo::text').get() or '').strip()
        m = re.search(r'Visit the (.+?) Store', text)
        if m:
            return m.group(1).strip()
        if text.startswith('Brand: '):
            return text[7:].strip()
        # Books/media: bylineInfo first text node is "by"; author name is in .author a
        author = (response.css('#bylineInfo .author a::text').get() or '').strip()
        if author:
            return author
        if text and text.lower() not in ('by', ''):
            return text
        # Fallback: "Brand Name" row in product detail table
        for th_el in response.css('th.prodDetSectionEntry'):
            if (th_el.css('::text').get() or '').strip() == 'Brand Name':
                td = (th_el.xpath('../td').css('::text').get() or '').strip()
                return td or None
        return None

    @staticmethod
    def _parse_main_image(response) -> str | None:
        """Parse data-a-dynamic-image JSON dict; return URL with largest pixel area."""
        raw = response.css('#landingImage::attr(data-a-dynamic-image)').get()
        if not raw:
            return None
        try:
            imgs = json.loads(raw)
            return max(imgs, key=lambda k: imgs[k][0] * imgs[k][1])
        except Exception:
            return None

    @staticmethod
    def _parse_about(response) -> str | None:
        bullets = [
            b.strip()
            for b in response.css('#feature-bullets ul li span.a-list-item::text').getall()
            if b.strip()
        ]
        return '\n'.join(bullets) if bullets else None

    @staticmethod
    def _parse_rating(response) -> float | None:
        text = (response.css('span.a-icon-alt::text').get() or '').strip()
        m = re.match(r'(\d+\.?\d*)', text)
        return float(m.group(1)) if m else None

    @staticmethod
    def _parse_review_count(response) -> int | None:
        text = (response.css('#acrCustomerReviewText::text').get() or '').strip()
        cleaned = re.sub(r'[(),\s]', '', text)
        m = re.search(r'\d+', cleaned)
        return int(m.group()) if m else None

    @staticmethod
    def _parse_rating_breakdown(response) -> dict | None:
        """Returns {"5": 63, "4": 12, "3": 7, "2": 6, "1": 12} or None."""
        breakdown = {}
        for el in response.css('a[aria-label*="percent of reviews have"]'):
            label = el.attrib.get('aria-label', '')
            m = re.match(r'(\d+) percent.*?(\d) star', label)
            if m:
                breakdown[m.group(2)] = int(m.group(1))
        return breakdown if breakdown else None

    @staticmethod
    def _parse_bsr(response) -> list | None:
        """
        Parse Best Sellers Rank entries from the product details table.
        Returns [{"rank": 360, "category": "Patio, Lawn & Garden"}, ...] or None.

        The BSR th has class 'prodDetSectionEntry'. We select all <th> with this
        class, find the one containing 'Best Sellers Rank', then navigate to the
        sibling <td> via xpath('../td') to read the <li> items.
        """
        entries = []
        for th_el in response.css('th.prodDetSectionEntry'):
            th_text = (th_el.css('::text').get() or '').strip()
            if 'Best Sellers Rank' not in th_text:
                continue
            td_el = th_el.xpath('../td')
            for li in td_el.css('li'):
                full_text = (li.xpath('string()').get() or '').strip()
                m = re.search(r'#([\d,]+)\s+in\s+(.+?)(?:\s*\(See\b|$)', full_text)
                if m:
                    rank = int(m.group(1).replace(',', ''))
                    cat = m.group(2).strip()
                    entries.append({'rank': rank, 'category': cat})
            break
        return entries if entries else None

    @staticmethod
    def _parse_breadcrumb_categories(response) -> list | None:
        """
        Fallback category source when BSR section is absent (sparse page / A/B layout).

        Extracts the breadcrumb navigation path: the <li> elements inside
        #wayfinding-breadcrumbs_feature_div, skipping divider items
        (li.a-breadcrumb-divider). All breadcrumb items — including the leaf node —
        have <a> tags confirmed via browser inspect.

        Returns entries in the same shape as _parse_bsr() but with rank=None and
        from_breadcrumb=True so consumers can distinguish them:
            [
                {"rank": None, "category": "Patio, Lawn & Garden", "from_breadcrumb": True},
                {"rank": None, "category": "Grill Thermometers",   "from_breadcrumb": True},
            ]
        Returns None if the breadcrumb element is not found.
        """
        crumbs = [
            t.strip()
            for t in response.css(
                '#wayfinding-breadcrumbs_feature_div '
                'ul li:not(.a-breadcrumb-divider) a::text'
            ).getall()
            if t.strip()
        ]
        if not crumbs:
            return None
        return [{'rank': None, 'category': c, 'from_breadcrumb': True} for c in crumbs]

    @staticmethod
    def _parse_last_month_sales(response) -> str | None:
        """
        Extracts "100+", "1K+", "20K+" from the "X bought in past month/week" badge.

        Amazon splits the count and label across child elements, so
        contains(text(), ...) never matches — the count lives in a child <span>
        and " bought in past month" is a sibling text node of the parent element.
        Must use contains(., ...) (matches all descendant text) with string() to
        reconstruct the full text.

        Amazon shows "past week" on some high-velocity products instead of
        "past month". Both are captured; the raw count is returned either way.
        """
        for period in ('past month', 'past week'):
            phrase = f'bought in {period}'
            for el in response.xpath(
                f"//*[contains(., '{phrase}') and "
                f"not(descendant::*[contains(., '{phrase}')])]"
            ):
                full_text = (el.xpath('string()').get() or '').strip()
                m = re.search(r'([\d,K+]+)\s+bought in past (?:month|week)', full_text)
                if m:
                    return m.group(1)
        return None

    @staticmethod
    def _parse_has_variants(response) -> bool:
        return bool(response.css('[id*="inline-twister"]'))

    @staticmethod
    def _parse_is_small_business(response) -> bool:
        return bool(response.css('[id*="sbe_badge"]'))

    @staticmethod
    def _parse_variant_asins(response) -> list | None:
        """
        Parse variant ASINs with their dimension labels from inline <script> tags.

        Amazon encodes variant data in three co-located keys:
          - dimensionToAsinMap: {"12_5": "B0XX", ...}
            Keys are underscore-delimited dimension indices (one per dimension).
          - dimensions: ["color_name", "size_name"]
            Maps each index position to a dimension name.
          - variationValues: {"color_name": ["Blue", "Red"], "size_name": ["S", "M"]}
            Maps each dimension to its ordered list of values.

        Key "12_5" with dimensions ["color_name", "size_name"] decodes as:
          color_name[12] + size_name[5] → the ASIN's color and size.

        Returns a list of dicts, one per unique ASIN:
          [{"asin": "B0XX", "color_name": "Blue", "size_name": "Large"}, ...]

        variationValues uses nested arrays so a brace-counting approach is needed
        to find the object boundaries (simple regex with [^}]+ would stop too early).
        """
        for script in response.css('script::text').getall():
            if 'dimensionToAsinMap' not in script:
                continue

            m_asin = re.search(r'"dimensionToAsinMap"\s*:\s*(\{[^}]+\})', script)
            if not m_asin:
                break
            try:
                asin_map = json.loads(m_asin.group(1))
            except Exception:
                break

            # dimensions: simple flat array
            dimensions: list = []
            m_dims = re.search(r'"dimensions"\s*:\s*(\[[^\]]+\])', script)
            if m_dims:
                try:
                    dimensions = json.loads(m_dims.group(1))
                except Exception:
                    pass

            # variationValues: nested object — count braces to find end
            variation_values: dict = {}
            idx_vv = script.find('"variationValues"')
            if idx_vv >= 0:
                try:
                    brace_start = script.index('{', idx_vv)
                    depth = 0
                    end = brace_start
                    for pos, ch in enumerate(script[brace_start:], brace_start):
                        if ch == '{':
                            depth += 1
                        elif ch == '}':
                            depth -= 1
                            if depth == 0:
                                end = pos
                                break
                    variation_values = json.loads(script[brace_start:end + 1])
                except Exception:
                    pass

            seen: set = set()
            variants: list = []
            for key, asin in asin_map.items():
                if asin in seen:
                    continue
                seen.add(asin)
                variant: dict = {'asin': asin}
                if dimensions and variation_values:
                    for i, idx_str in enumerate(key.split('_')):
                        if i >= len(dimensions):
                            break
                        dim_name = dimensions[i]
                        vals = variation_values.get(dim_name, [])
                        try:
                            variant[dim_name] = vals[int(idx_str)]
                        except (IndexError, ValueError):
                            pass
                variants.append(variant)

            return variants if variants else None
        return None

    @staticmethod
    def _parse_related_asins(response) -> list | None:
        """
        Extract Frequently Bought Together (FBT) ASINs.
        FBT items are in the widget [data-cel-widget*="p13n-desktop-sims-fbt"].
        ASINs are embedded in product link hrefs (/dp/<ASIN>).
        The current product's ASIN is excluded.
        """
        current_asin = response.meta.get('asin', '')
        fbt_widget = response.css('[data-cel-widget*="p13n-desktop-sims-fbt"]')
        if not fbt_widget:
            return None
        seen: set = set()
        asins: list = []
        for a in fbt_widget.css('a[href*="/dp/"]'):
            m = re.search(r'/dp/([A-Z0-9]{10})', a.attrib.get('href', ''))
            if m and m.group(1) not in seen and m.group(1) != current_asin:
                seen.add(m.group(1))
                asins.append(m.group(1))
        return asins if asins else None

    @staticmethod
    def _parse_detail(response, label: str) -> str | None:
        """
        Extract a product detail field by its <th> label from the details table.
        The <th> elements have class 'prodDetSectionEntry'.
        """
        for th_el in response.css('th.prodDetSectionEntry'):
            th_text = (th_el.css('::text').get() or '').strip()
            if th_text == label:
                td_text = (th_el.xpath('../td').css('::text').get() or '').strip()
                return td_text or None
        return None

    @staticmethod
    def _parse_dimensions(response) -> str | None:
        """
        Item Dimensions th label varies: 'Item Dimensions LxWxH', 'Item Dimensions'.
        Uses substring match instead of exact equality.
        """
        for th_el in response.css('th.prodDetSectionEntry'):
            th_text = (th_el.css('::text').get() or '').strip()
            if 'Item Dimensions' in th_text:
                td_text = (th_el.xpath('../td').css('::text').get() or '').strip()
                return td_text or None
        return None

    # ---- JS-rendered field parsers (require Playwright + zip 19901) ----

    @staticmethod
    def _parse_price(response) -> float | None:
        """
        Two price block patterns on Amazon US:
          - FBA/Prime:  span.apex-basisprice-value span.a-offscreen  (e.g. Catchmaster)
          - FBM/other: #tp_price_block_total_price_ww span.a-offscreen (e.g. BubbleBlooms)
        Both return the machine-readable price text (e.g. '$12.99').
        """
        for sel in [
            'span.apex-basisprice-value span.a-offscreen',
            '#tp_price_block_total_price_ww span.a-offscreen',
        ]:
            text = (response.css(f'{sel}::text').get() or '').strip()
            if text:
                cleaned = re.sub(r'[^\d.]', '', text)
                try:
                    return round(float(cleaned), 2) if cleaned else None
                except ValueError:
                    continue
        return None

    @staticmethod
    def _parse_seller_name(response) -> str | None:
        """
        Seller display patterns (Amazon layout has changed over time):
          1. Third-party sellers: #sellerProfileTriggerId link text
          2. New tabular buybox: <span>Sold by:</span><span>Name</span> pair
          3. Compact combined: "Ships from and sold by Name." or "Sold by Name and ships from..."
          4. Legacy: #merchant-info free-text
        """
        name = (response.css('#sellerProfileTriggerId::text').get() or '').strip()
        if name:
            return name
        # New tabular buybox: <span>Sold by:</span><span>Seller Name</span>
        sold_by = response.xpath(
            '//span[normalize-space(text())="Sold by:"]/following-sibling::span[1]/text()'
        ).get()
        if sold_by:
            sold_by = sold_by.strip()
            if sold_by:
                return sold_by
        # Compact combined text: "Ships from and sold by Name." or "Sold by Name and ships from..."
        # Note: seller names can contain dots (e.g. "Amazon.com") so use .+? not [^.]+?
        for t in response.css('span.a-color-secondary::text, span.a-size-small.a-color-secondary::text').getall():
            t = t.strip()
            m = re.search(r'[Ss]old by (.+?)(?:\s+and\s+ships from|\.\s*$)', t)
            if m:
                return m.group(1).strip()
        # Legacy fallback: #merchant-info free-text (older page layouts)
        merchant_texts = response.css('#merchant-info ::text').getall()
        in_sold_by = False
        for t in merchant_texts:
            t = t.strip()
            if not t:
                continue
            if 'Sold by' in t:
                in_sold_by = True
                continue
            if in_sold_by and t not in ('and', '.', ','):
                return t
        return None

    @staticmethod
    def _parse_seller_id(response) -> str | None:
        """seller= query param from the seller profile link. NULL for Amazon-sold products."""
        href = response.css('#sellerProfileTriggerId::attr(href)').get() or ''
        m = re.search(r'seller=([A-Z0-9]+)', href)
        return m.group(1) if m else None

    @staticmethod
    def _parse_is_fba(response) -> bool | None:
        """
        Returns True if Amazon fulfills, False if seller fulfills, None if no
        buybox info rendered (out of stock or blocked page).

        Detection patterns (Amazon layout has changed over time):
          1. #sellerProfileTriggerId href: isAmazonFulfilled=1 (third-party FBA)
          2. New tabular buybox: "Ships from:" label + adjacent span — "Amazon" means FBA
          3. Compact combined: "Ships from and sold by Amazon" / "ships from Amazon Fulfillment"
          4. Legacy: #merchant-info text "Fulfilled by Amazon"
        """
        href = response.css('#sellerProfileTriggerId::attr(href)').get()
        if href is not None:
            return bool(re.search(r'isAmazonFulfilled=1', href))
        # New tabular buybox: <span>Ships from:</span><span>Amazon.com</span>
        ships_from = response.xpath(
            '//span[normalize-space(text())="Ships from:"]/following-sibling::span[1]/text()'
        ).get()
        if ships_from:
            return 'amazon' in ships_from.lower()
        # Compact combined text
        for t in response.css('span.a-color-secondary::text, span.a-size-small.a-color-secondary::text').getall():
            t_lower = t.lower()
            if 'ships from' in t_lower or 'fulfilled by' in t_lower:
                return 'amazon' in t_lower
        # Legacy fallback: #merchant-info free-text (older page layouts)
        merchant_text = ' '.join(response.css('#merchant-info ::text').getall())
        if merchant_text.strip():
            return 'Fulfilled by Amazon' in merchant_text or 'Ships from and sold by Amazon' in merchant_text
        return None

    # ------------------------------------------------------------------
    # DB writes
    # ------------------------------------------------------------------

    def _write_product(self, data: dict):
        """
        UPSERT one product snapshot row.
        first_captured_at is NOT in the DO UPDATE SET clause — preserved on conflict.
        JSONB fields are serialized to JSON strings with an explicit ::jsonb cast.
        """
        jsonb_fields = ('rating_breakdown', 'bsr_entries', 'variant_asins', 'related_asins')

        params = dict(data)
        for field in jsonb_fields:
            val = params.get(field)
            params[field] = json.dumps(val) if val is not None else None

        self.db.execute(
            """
            INSERT INTO staging.amz_product_snapshot (
                marketplace_id, asin,
                first_captured_at, last_captured_at,
                title, brand, main_image_url, launch_date, about_this_item,
                rating, review_count, rating_breakdown, bsr_entries, last_month_sales,
                price, seller_name, seller_id, is_fba,
                has_variants, variant_asins, related_asins,
                weight, dimensions,
                is_small_business, html_file_path
            ) VALUES (
                %(marketplace_id)s, %(asin)s,
                NOW(), NOW(),
                %(title)s, %(brand)s, %(main_image_url)s, %(launch_date)s, %(about_this_item)s,
                %(rating)s, %(review_count)s,
                %(rating_breakdown)s::jsonb, %(bsr_entries)s::jsonb, %(last_month_sales)s,
                %(price)s, %(seller_name)s, %(seller_id)s, %(is_fba)s,
                %(has_variants)s, %(variant_asins)s::jsonb, %(related_asins)s::jsonb,
                %(weight)s, %(dimensions)s,
                %(is_small_business)s, %(html_file_path)s
            )
            ON CONFLICT (marketplace_id, asin) DO UPDATE SET
                last_captured_at  = NOW(),
                title             = EXCLUDED.title,
                brand             = EXCLUDED.brand,
                main_image_url    = EXCLUDED.main_image_url,
                launch_date       = EXCLUDED.launch_date,
                about_this_item   = EXCLUDED.about_this_item,
                rating            = EXCLUDED.rating,
                review_count      = EXCLUDED.review_count,
                rating_breakdown  = EXCLUDED.rating_breakdown,
                bsr_entries       = EXCLUDED.bsr_entries,
                last_month_sales  = EXCLUDED.last_month_sales,
                price             = EXCLUDED.price,
                seller_name       = EXCLUDED.seller_name,
                seller_id         = EXCLUDED.seller_id,
                is_fba            = EXCLUDED.is_fba,
                has_variants      = EXCLUDED.has_variants,
                variant_asins     = EXCLUDED.variant_asins,
                related_asins     = EXCLUDED.related_asins,
                weight            = EXCLUDED.weight,
                dimensions        = EXCLUDED.dimensions,
                is_small_business = EXCLUDED.is_small_business,
                html_file_path    = EXCLUDED.html_file_path
            """,
            params,
        )

    def _queue_variant_asins(self, variant_asins: list, source_asin: str):
        """Insert newly discovered variant ASINs into the scrape queue."""
        for variant in variant_asins:
            # variant_asins is now a list of dicts {"asin": ..., "color_name": ..., ...}
            variant_asin = variant['asin'] if isinstance(variant, dict) else variant
            if variant_asin == source_asin:
                continue
            product_url = f'https://www.amazon.com/dp/{variant_asin}'
            try:
                self.db.execute(
                    """
                    INSERT INTO transformed.amz_product_scrape_queue
                        (marketplace_id, asin, product_url)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (marketplace_id, asin) DO NOTHING
                    """,
                    (self.marketplace_id, variant_asin, product_url),
                )
                self.variants_queued += 1
            except Exception as e:
                self.log(
                    f"WARNING: could not queue variant {variant_asin} "
                    f"(from {source_asin}): {e}",
                    40,
                )

    def _write_monitoring_stats(self):
        """Compute per-field null rates for this run and write to monitoring table."""
        if not self._scraped_asins:
            return

        fields = [
            'title', 'brand', 'rating', 'review_count', 'price',
            'seller_name', 'is_fba', 'bsr_entries', 'last_month_sales',
            'is_small_business', 'has_variants',
        ]
        stats = []
        for field in fields:
            try:
                rows = self.db.read(
                    query=f"""
                        SELECT
                            COUNT(*)                                 AS total,
                            COUNT(*) FILTER (WHERE {field} IS NULL) AS null_count
                        FROM staging.amz_product_snapshot
                        WHERE asin = ANY(%s)
                          AND marketplace_id = %s
                    """,
                    params=(self._scraped_asins, self.marketplace_id),
                )
                if rows and rows[0]['total'] > 0:
                    total      = rows[0]['total']
                    null_count = rows[0]['null_count']
                    stats.append({
                        'run_id':        str(self.run_id),
                        'spider_name':   self.name,
                        'run_date':      self.run_date,
                        'marketplace_id': self.marketplace_id,
                        'field_name':    field,
                        'total_records': total,
                        'null_count':    null_count,
                        'null_rate':     round(null_count / total, 4),
                    })
            except Exception as e:
                self.log(f"WARNING: monitoring stats failed for field={field}: {e}", 40)

        if stats:
            try:
                self.db.bulk_upsert(
                    table='monitoring.scrape_run_field_stats',
                    data=stats,
                    conflict_columns=['run_id', 'field_name'],
                    update_columns=['total_records', 'null_count', 'null_rate'],
                )
                self.log(f"Monitoring stats written for {len(stats)} fields.", 20)
            except Exception as e:
                self.log(f"WARNING: could not write monitoring stats: {e}", 40)
