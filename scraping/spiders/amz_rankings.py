"""
AmzRankings spider
==================
Scrapes Amazon bestseller and new-release ranking pages for all leaf nodes
seeded in transformed.amz_category_scrape_controller.

Single spider, parameterized by list_type:
  - 'bestseller'   → https://www.amazon.com/gp/bestsellers/<url_slug>/<node_id>
  - 'new_release'  → https://www.amazon.com/gp/new-releases/<url_slug>/<node_id>

Output: staging.amz_ranking_snapshot (raw append, duplicates possible on resume).
Post-run: execute db/merge_rankings.sql to promote staging → transformed.amz_ranking.

Usage:
    cd scraping/
    # Full run — Playwright enabled (50 products/page, ~100/node)
    scrapy crawl AmzRankings -a list_type=bestseller -a marketplace_id=amazon_us

    # Fast mode — plain HTTP (30 products/page, ~60/node)
    scrapy crawl AmzRankings -a list_type=bestseller -a use_playwright=false

    # Single category test
    scrapy crawl AmzRankings -a list_type=bestseller -a categories="Carriers & Travel Products" -a include_descendants=false -s LOG_FILE=logs/test.log

MUST READ before modifying: docs/scraping_pitfalls.md
  P1 — use async def start(), not start_requests()
  P3 — never rely on stable Amazon class names; use *=-substring selectors
  P4 — validate selectors against live page before running at scale
  P5 — psycopg2 rollback on every error (already in PostgresDBHandler)
  P8 — Amazon lazy-loads items 31-50 per page via ACP widget; plain HTTP returns 30 max.
       use_playwright=true triggers scroll which loads the remaining 20 before parse().
"""

import os
import uuid
import re
import scrapy
from scrapy import signals
from datetime import datetime as dt

try:
    from scrapy_playwright.page import PageMethod
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

from helpers.postgres_handler import PostgresDBHandler


class AmzRankingsSpider(scrapy.Spider):
    name = "AmzRankings"
    allowed_domains = ["www.amazon.com"]

    # Base settings — apply in all modes.
    # When use_playwright=true, from_crawler() overrides DOWNLOAD_DELAY,
    # CONCURRENT_REQUESTS, CONCURRENT_REQUESTS_PER_DOMAIN, and adds
    # DOWNLOAD_HANDLERS + PLAYWRIGHT_* settings.
    custom_settings = {
        "ITEM_PIPELINES": {},           # spider writes directly to DB — no pipeline needed
        "DOWNLOAD_DELAY": 8,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "CONCURRENT_REQUESTS": 16,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 16,
        "DEPTH_LIMIT": 0,               # pagination handled manually in parse()
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

    def __init__(self, list_type='bestseller', marketplace_id='amazon_us',
                 min_days_since_last_scrape=1,
                 categories=None, include_descendants='true',
                 use_playwright='true',
                 **kwargs):
        super().__init__(**kwargs)
        if list_type not in ('bestseller', 'new_release'):
            raise ValueError(
                f"list_type must be 'bestseller' or 'new_release', got '{list_type}'"
            )
        self.list_type = list_type
        self.marketplace_id = marketplace_id
        self.min_days_since_last_scrape = int(min_days_since_last_scrape)

        # categories: comma-separated node names to filter on.
        #   None / omitted → scrape all categories.
        #   e.g. "Office Products"
        #   e.g. "Office Products|Pet Supplies"   (pipe-delimited — commas appear in names)
        #   e.g. "Arts, Crafts & Sewing|Toys & Games"
        #   e.g. "Staplers"   (an intermediate or leaf node)
        self.categories = (
            [c.strip() for c in categories.split('|') if c.strip()]
            if categories else None
        )

        # include_descendants: whether to expand named nodes to their full subtree.
        #   True (default) — scrape named node + all descendants (every depth).
        #   False          — scrape only the named nodes themselves, no expansion.
        self.include_descendants = str(include_descendants).lower() not in ('false', '0', 'no')

        # use_playwright: render pages in Chromium to capture lazy-loaded items 31-50.
        #   True (default) — ~50 products per page, ~100 per node.
        #   False          — plain HTTP, ~30 products per page, ~60 per node. Faster.
        self.use_playwright = str(use_playwright).lower() not in ('false', '0', 'no')
        if self.use_playwright and not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "use_playwright=true but scrapy-playwright is not installed.\n"
                "Run: pip install scrapy-playwright && playwright install chromium"
            )

        self.run_id = uuid.uuid4()
        self.run_date = dt.now().date()
        self.products_written = 0
        self.nodes_completed = 0
        self._loaded_node_ids: list[str] = []   # populated in start(); used by spider_closed

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = cls(*args, **kwargs)
        spider._set_crawler(crawler)   # sets spider.crawler + spider.settings (Scrapy 2.16 API)
        spider.db = PostgresDBHandler(
            host=crawler.settings.get('POSTGRES_HOST'),
            database=crawler.settings.get('POSTGRES_DATABASE'),
            user=crawler.settings.get('POSTGRES_USERNAME'),
            password=crawler.settings.get('POSTGRES_PASSWORD'),
            port=crawler.settings.get('POSTGRES_PORT'),
        )

        if spider.use_playwright:
            # Playwright runs a real Chromium process per request.
            # Must reduce concurrency (RAM) and increase delay (detection).
            # These override the custom_settings values above.
            # NOTE: proxy is NOT configured here — see docs/possible_enhancements.md
            # for what changes when Oxylabs proxies are eventually enabled.
            #
            # headless=False tells Playwright to use Chrome for Testing (the full browser
            # binary) rather than Chrome Headless Shell. Chrome Headless Shell is blocked
            # by Windows Defender/SmartScreen in some environments even when the binary
            # exists on disk. Chrome for Testing does not have this problem.
            # --headless=new passed as a Chrome arg keeps the browser invisible despite
            # headless=False — so no Chrome windows appear on screen during scraping.
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
                'PLAYWRIGHT_MAX_PAGES_PER_CONTEXT': 4,
                # Block resources that are useless for scraping — images alone account
                # for 400+ requests per node and are the primary cause of Chromium OOM.
                # Scripts and XHR are kept; ACP lazy-load requires them.
                'PLAYWRIGHT_ABORT_REQUEST': (
                    lambda req: req.resource_type in ('image', 'media', 'font', 'stylesheet')
                ),
                'DOWNLOAD_DELAY':                   8,
                'RANDOMIZE_DOWNLOAD_DELAY':         True,
                'CONCURRENT_REQUESTS':              4,
                'CONCURRENT_REQUESTS_PER_DOMAIN':   4,
            }, priority='spider')

        crawler.signals.connect(spider.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(spider.spider_closed, signal=signals.spider_closed)
        return spider

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def spider_opened(self, spider):
        self.db.connect()
        self.log(
            f"DB connected. run_id={self.run_id} list_type={self.list_type} "
            f"marketplace_id={self.marketplace_id} "
            f"categories={self.categories or 'ALL'} "
            f"include_descendants={self.include_descendants} "
            f"min_days_since_last_scrape={self.min_days_since_last_scrape} "
            f"use_playwright={self.use_playwright}",
            20,
        )

    def spider_closed(self, spider, reason):
        # Any node still in_progress at spider close did NOT complete successfully.
        # _mark_node_complete() is called from parse() only when the last page is
        # processed without error — so nodes that are still in_progress here either
        # had a request failure or a Playwright launch error.
        # Reset them to 'pending' so the next run retries them.
        # Scoped to _loaded_node_ids so concurrent spider runs are not affected.
        try:
            if self._loaded_node_ids:
                self.db.execute(
                    """
                    UPDATE transformed.amz_category_scrape_controller
                    SET scrape_status   = 'pending',
                        last_scraped_at = NULL
                    WHERE marketplace_id        = %s
                      AND list_type             = %s
                      AND scrape_status         = 'in_progress'
                      AND subcategory_node_id   = ANY(%s)
                    """,
                    (self.marketplace_id, self.list_type, self._loaded_node_ids),
                )
                self.log(
                    "Failed/incomplete nodes reset to 'pending' for retry on next run.",
                    30,
                )
            else:
                pass
        except Exception as e:
            self.log(f"WARNING: could not reset in_progress nodes: {e}", 40)

        self._write_monitoring_stats()

        self.log(
            f"Spider closed [{reason}]. "
            f"nodes_completed={self.nodes_completed} "
            f"products_written={self.products_written}",
            20,
        )
        self.db.close()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    async def start(self):
        """
        Load eligible leaf nodes from the scrape controller and yield page-1 requests.

        Eligibility:
          - scrape_status = 'pending'
          - OR in_progress (crashed run — pick up and retry)
          - OR complete but last_scraped_at older than min_days_since_last_scrape

        Category filtering (when self.categories is set):
          1. Resolve category names → node_ids via amz_category (name match).
          2. include_descendants=True  → expand via closure table to ALL
             descendants at every depth (root + intermediate + leaf).
          3. include_descendants=False → scrape only the named nodes themselves,
             regardless of whether they are leaves or intermediate nodes.
        """
        # ------------------------------------------------------------------
        # Step 1: resolve category names → target subcategory_node_ids
        # ------------------------------------------------------------------
        node_id_filter_clause = ""
        filter_params: dict = {
            'marketplace_id': self.marketplace_id,
            'list_type':      self.list_type,
        }

        if self.categories:
            # Look up all nodes whose name matches any of the given names
            matched = self.db.read(
                query="""
                    SELECT node_id, node_name, is_leaf
                    FROM transformed.amz_category
                    WHERE marketplace_id = %s
                      AND node_name      = ANY(%s)
                """,
                params=(self.marketplace_id, self.categories),
            )
            if not matched:
                self.log(
                    f"ERROR: No nodes found for categories={self.categories}. "
                    "Check spelling — names must match amz_category.node_name exactly.",
                    40,
                )
                return

            matched_ids = [r['node_id'] for r in matched]
            self.log(
                f"Category filter resolved: {[r['node_name'] for r in matched]} "
                f"→ node_ids={matched_ids}",
                20,
            )

            if self.include_descendants:
                # Expand each matched node to ALL descendants (root + intermediate + leaf)
                # via the closure table. AmzRankings scrapes every level.
                desc = self.db.read(
                    query="""
                        SELECT DISTINCT h.descendant_node_id AS node_id
                        FROM transformed.amz_category_hierarchy h
                        WHERE h.marketplace_id   = %s
                          AND h.ancestor_node_id = ANY(%s)
                    """,
                    params=(self.marketplace_id, matched_ids),
                )
                target_ids = [r['node_id'] for r in desc]
                self.log(
                    f"include_descendants=True → expanded to {len(target_ids)} nodes "
                    f"(all depths: root + intermediate + leaf).",
                    20,
                )
            else:
                # Only scrape the named nodes themselves — no subtree expansion.
                target_ids = [r['node_id'] for r in matched]
                self.log(
                    f"include_descendants=False → {len(target_ids)} nodes to scrape "
                    f"(named nodes only, no subtree expansion).",
                    20,
                )

            if not target_ids:
                self.log("No eligible leaf nodes after category resolution. Exiting.", 30)
                return

            node_id_filter_clause = "AND ctrl.subcategory_node_id = ANY(%(target_ids)s)"
            filter_params['target_ids'] = target_ids

        # ------------------------------------------------------------------
        # Step 2: load eligible rows from the controller
        # ------------------------------------------------------------------
        rows = self.db.read(
            query=f"""
                SELECT
                    ctrl.category,
                    ctrl.subcategory,
                    ctrl.subcategory_node_id,
                    cat.url_slug
                FROM transformed.amz_category_scrape_controller ctrl
                JOIN transformed.amz_category cat
                  ON  cat.marketplace_id = ctrl.marketplace_id
                 AND  cat.node_id        = ctrl.subcategory_node_id
                WHERE ctrl.marketplace_id = %(marketplace_id)s
                  AND ctrl.list_type      = %(list_type)s
                  {node_id_filter_clause}
                  AND (
                      ctrl.scrape_status = 'pending'
                      OR ctrl.scrape_status = 'in_progress'
                      OR (
                          ctrl.scrape_status = 'complete'
                          AND ctrl.last_scraped_at < NOW() - INTERVAL '{self.min_days_since_last_scrape} days'
                      )
                  )
                ORDER BY ctrl.category, ctrl.subcategory
            """,
            params=filter_params,
        )

        if not rows:
            self.log(
                f"No pending nodes found for list_type={self.list_type} "
                f"categories={self.categories or 'ALL'}. "
                "All nodes may already be scraped — use min_days_since_last_scrape=0 to force.",
                30,
            )
            return

        self.log(
            f"Loaded {len(rows)} pending nodes to scrape "
            f"(list_type={self.list_type}, categories={self.categories or 'ALL'} "
            f"use_playwright={self.use_playwright}).",
            20,
        )

        # Batch-mark all as in_progress before yielding requests
        # (prevents duplicate pickup if spider is triggered concurrently)
        self._loaded_node_ids = [r['subcategory_node_id'] for r in rows]
        self.db.execute(
            """
            UPDATE transformed.amz_category_scrape_controller
            SET scrape_status = 'in_progress'
            WHERE marketplace_id        = %s
              AND list_type             = %s
              AND subcategory_node_id   = ANY(%s)
            """,
            (self.marketplace_id, self.list_type, self._loaded_node_ids),
        )

        for row in rows:
            url = self._build_url(row['url_slug'], row['subcategory_node_id'])
            self.log(
                f"Queuing: {row['category']} / {row['subcategory']} "
                f"node_id={row['subcategory_node_id']} url={url}",
                20,
            )
            meta = {
                'category':            row['category'],
                'subcategory':         row['subcategory'],
                'subcategory_node_id': row['subcategory_node_id'],
                'url_slug':            row['url_slug'],
                'page':                1,
            }
            if self.use_playwright:
                meta.update(self._playwright_meta())
            yield scrapy.Request(
                url=url,
                callback=self.parse,
                meta=meta,
                errback=self.handle_error,
            )

    def _playwright_meta(self) -> dict:
        """
        Playwright meta keys to merge into any Request that needs full page rendering.

        Flow once the browser loads the page:
          1. wait_for_selector — confirms the initial 30 cards are in the DOM.
          2. evaluate (scroll) — triggers the ACP widget's scroll event listener,
             which fires the lazy-load XHR for items 31-50.
          3. wait_for_timeout — 3 seconds for the XHR to respond and the DOM to update
             with the remaining cards before Scrapy reads the HTML.

        The resulting response.text contains all 50 product cards — no separate
        XHR request needed. _extract_products() selectors work unchanged.
        """
        return {
            'playwright': True,
            'playwright_page_methods': [
                PageMethod('wait_for_selector', 'div[data-asin]'),
                PageMethod('evaluate', 'window.scrollTo(0, document.body.scrollHeight)'),
                PageMethod('wait_for_timeout', 1500),   # ms — wait for lazy-load XHR
            ],
        }

    def parse(self, response):
        """
        Parse one page of a ranking list.

        With use_playwright=true: response contains all 50 products (30 static + 20
        lazy-loaded after scroll). With use_playwright=false: response contains only
        the 30 products rendered server-side.

        After extraction, follows li.a-last > a to the next page if present.
        Page 2 requests carry the same playwright meta so the scroll is repeated.

        IMPORTANT (P3): Amazon changes class names frequently.
        Validate selectors against a live page before running at scale.
        Dump HTML to html_debug/ if products_found == 0.

        Current expected structure (as of 2026-06):
          Product card:  div[data-asin]
          Fallback card: li[class*="zg-item-immersion"]
          ASIN:          data-asin attribute on the card element
          Rank badge:    span[class*="zg-bdg-text"]
          Title:         div[class*="p13n-sc-css-line-clamp"] or span[class*="p13n-sc-truncate"]
          Rating:        span.a-icon-alt (text: "4.5 out of 5 stars")
          Review count:  span[aria-hidden="true"].a-size-small
          Price:         span[class*="p13n-sc-price"] or span.a-price span.a-offscreen
          Product URL:   a.a-link-normal::attr(href) — first anchor in card
          Next page:     li.a-last > a::attr(href)
        """
        category            = response.meta['category']
        subcategory         = response.meta['subcategory']
        subcategory_node_id = response.meta['subcategory_node_id']
        url_slug            = response.meta['url_slug']
        page                = response.meta['page']

        self.log(
            f"Parsing [{self.list_type}] {category} / {subcategory} "
            f"node={subcategory_node_id} page={page} url={response.url}",
            20,
        )

        products = self._extract_products(response, category, subcategory, subcategory_node_id)

        if not products:
            self.log(
                f"WARNING: 0 products found on page {page} for node={subcategory_node_id}. "
                "Selector may have changed. Dumping HTML to html_debug/.",
                40,
            )
            self._dump_html(response, subcategory_node_id, page)
        else:
            self.log(
                f"  Found {len(products)} products on page {page} "
                f"for node={subcategory_node_id}",
                20,
            )
            self._write_products(products)

        next_href = response.css('li.a-last > a::attr(href)').get()

        if products and next_href:
            meta = {
                'category':            category,
                'subcategory':         subcategory,
                'subcategory_node_id': subcategory_node_id,
                'url_slug':            url_slug,
                'page':                page + 1,
            }
            if self.use_playwright:
                meta.update(self._playwright_meta())
            yield scrapy.Request(
                url=response.urljoin(next_href),
                callback=self.parse,
                meta=meta,
                errback=self.handle_error,
            )
        else:
            self._mark_node_complete(category, subcategory)

    def handle_error(self, failure):
        """Log request errors (main page). Node stays in_progress for retry on next run."""
        meta = failure.request.meta
        self.log(
            f"Request failed for node={meta.get('subcategory_node_id')} "
            f"page={meta.get('page')} url={failure.request.url}: {failure.value}",
            40,
        )

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def _extract_products(self, response, category, subcategory, subcategory_node_id):
        """
        Extract all product records from a ranking page.

        Tries two card selector patterns (grid layout vs list layout fallback).
        Returns list of dicts ready for bulk insert into staging.amz_ranking_snapshot.
        """
        # 2026 page structure: div[data-asin] is the product card wrapper.
        # zg-grid-general-faceout is now INSIDE div[data-asin], not the card itself.
        # Fallback covers older list-layout pages (li[class*="zg-item-immersion"]).
        cards = response.css('div[data-asin]')
        if not cards:
            cards = response.css('li[class*="zg-item-immersion"]')
        if not cards:
            return []

        records = []
        for card in cards:
            asin = (
                card.attrib.get('data-asin')
                or card.css('[data-asin]').attrib.get('data-asin')
                or ''
            ).strip()
            if not asin:
                continue

            rank_position = self._parse_rank(card)
            title         = self._parse_title(card)
            rating        = self._parse_rating(card)
            review_count  = self._parse_review_count(card)
            price         = self._parse_price(card)
            product_url   = self._parse_product_url(response, card)

            records.append({
                'run_id':               str(self.run_id),
                'marketplace_id':       self.marketplace_id,
                'list_type':            self.list_type,
                'category':             category,
                'subcategory':          subcategory,
                'subcategory_node_id':  subcategory_node_id,
                'depth':                None,           # not tracked at this layer
                'rank_position':        rank_position,
                'asin':                 asin,
                'title':                title,
                'rating':               rating,
                'review_count':         review_count,
                'price':                price,
                'product_url':          product_url,
                'scraped_at':           dt.now(),
            })

        return records

    # ------------------------------------------------------------------
    # Field parsers — each returns None on failure (stored as NULL)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_rank(card) -> int | None:
        """Extract rank badge text (e.g. '#1' → 1)."""
        text = (card.css('span[class*="zg-bdg-text"]::text').get() or '').strip()
        m = re.search(r'\d+', text)
        return int(m.group()) if m else None

    @staticmethod
    def _parse_title(card) -> str | None:
        """Extract product title — tries multiple selector patterns."""
        for sel in [
            'div[class*="p13n-sc-css-line-clamp"]::text',   # 2026 grid layout
            'span[class*="p13n-sc-truncate"]::text',
            'span[class*="zg_title"]::text',
            'a.a-link-normal span::text',
            'div[class*="a-section"] span::text',
        ]:
            text = (card.css(sel).get() or '').strip()
            if text:
                return text
        return None

    @staticmethod
    def _parse_rating(card) -> float | None:
        """Extract star rating from 'X out of 5 stars' text."""
        text = (card.css('span.a-icon-alt::text').get() or '').strip()
        m = re.match(r'(\d+\.?\d*)', text)
        return float(m.group(1)) if m else None

    @staticmethod
    def _parse_review_count(card) -> int | None:
        """Extract review count — the number next to the star rating."""
        for sel in [
            'span[aria-hidden="true"].a-size-small::text',      # 2026: <span aria-hidden="true" class="a-size-small">45,438</span>
            'span[class*="a-size-small"] a::text',
            'a[class*="a-link-normal"] span[class*="a-size-small"]::text',
            'span[aria-label*="stars"] + span a::text',
        ]:
            text = (card.css(sel).get() or '').strip()
            cleaned = re.sub(r'[,\s]', '', text)
            m = re.search(r'\d+', cleaned)
            if m:
                return int(m.group())
        return None

    @staticmethod
    def _parse_price(card) -> float | None:
        """
        Extract price. Amazon uses multiple price display patterns.
        'a-offscreen' span holds the machine-readable value (e.g. '$29.99').
        """
        for sel in [
            'span[class*="p13n-sc-price"]::text',
            'span.a-price span.a-offscreen::text',
            'span[class*="a-price"] span[class*="a-offscreen"]::text',
        ]:
            text = (card.css(sel).get() or '').strip()
            cleaned = re.sub(r'[^\d.]', '', text)
            try:
                return round(float(cleaned), 2) if cleaned else None
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_product_url(response, card) -> str | None:
        """
        Extract raw product URL from the first anchor in the card.
        Stored as-is (ref params intact) per design doc decision.
        """
        href = card.css('a.a-link-normal::attr(href)').get() or ''
        if not href:
            return None
        return response.urljoin(href)

    # ------------------------------------------------------------------
    # DB writes
    # ------------------------------------------------------------------

    def _write_products(self, products: list[dict]):
        """Batch-insert product records into staging.amz_ranking_snapshot."""
        # Dedup by (run_id, marketplace_id, list_type, subcategory_node_id, asin)
        # within this batch to avoid PK violations on resume (P6 lesson)
        seen = set()
        deduped = []
        for p in products:
            key = (p['run_id'], p['marketplace_id'], p['list_type'],
                   p['subcategory_node_id'], p['asin'])
            if key not in seen:
                seen.add(key)
                deduped.append(p)

        self.db.bulk_upsert(
            table='staging.amz_ranking_snapshot',
            data=deduped,
            conflict_columns=['run_id', 'marketplace_id', 'list_type',
                              'subcategory_node_id', 'asin'],
            update_columns=['rank_position', 'title', 'rating', 'review_count',
                            'price', 'product_url', 'scraped_at'],
        )
        self.products_written += len(deduped)

    def _mark_node_complete(self, category: str, subcategory: str):
        """Mark a leaf node as complete in the scrape controller."""
        self.db.execute(
            """
            UPDATE transformed.amz_category_scrape_controller
            SET scrape_status  = 'complete',
                last_scraped_at = NOW()
            WHERE marketplace_id = %s
              AND list_type      = %s
              AND category       = %s
              AND subcategory    = %s
            """,
            (self.marketplace_id, self.list_type, category, subcategory),
        )
        self.nodes_completed += 1

    def _write_monitoring_stats(self):
        """
        Compute field-level null rates for this run and write to monitoring.scrape_run_field_stats.
        Queries staging.amz_ranking_snapshot for this run_id.
        """
        fields = ['title', 'rating', 'review_count', 'price', 'product_url',
                  'rank_position', 'subcategory_node_id']

        stats = []
        for field in fields:
            try:
                rows = self.db.read(
                    query=f"""
                        SELECT
                            COUNT(*)                                  AS total,
                            COUNT(*) FILTER (WHERE {field} IS NULL)  AS null_count
                        FROM staging.amz_ranking_snapshot
                        WHERE run_id = %s
                    """,
                    params=(str(self.run_id),),
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

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _build_url(self, url_slug: str, node_id: str) -> str:
        """
        Construct the ranking page URL for a given node.

        Bestseller:   https://www.amazon.com/gp/bestsellers/<url_slug>/<node_id>
        New release:  https://www.amazon.com/gp/new-releases/<url_slug>/<node_id>

        Root nodes (node_id == url_slug, e.g. 'arts-crafts') use slug-only URL.
        Amazon redirects these to canonical /zgbs/ or /new-releases/ paths — that's fine.
        """
        if self.list_type == 'bestseller':
            base = 'https://www.amazon.com/gp/bestsellers'
        else:
            base = 'https://www.amazon.com/gp/new-releases'

        if node_id == url_slug:
            return f'{base}/{url_slug}'
        return f'{base}/{url_slug}/{node_id}'

    # ------------------------------------------------------------------
    # Debug helpers
    # ------------------------------------------------------------------

    def _dump_html(self, response, node_id: str, page: int):
        """Save response HTML to html_debug/ for selector debugging."""
        from scrapy.utils.project import get_project_settings
        settings = get_project_settings()
        debug_dir = settings.get('HTML_DEBUG_DIR', 'html_debug')
        os.makedirs(debug_dir, exist_ok=True)
        filename = f'amz_rankings_{self.list_type}_{node_id}_p{page}.html'
        path = os.path.join(debug_dir, filename)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(response.text)
        self.log(f"HTML dumped → {path}", 30)
