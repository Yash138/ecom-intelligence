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

    # Save rendered HTML for debugging
    scrapy crawl AmzProducts -a limit=5 -a save_html=true

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
import scrapy
from scrapy import signals
from datetime import datetime as dt

try:
    from scrapy_playwright.page import PageMethod
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

from helpers.postgres_handler import PostgresDBHandler


class AmzProductsSpider(scrapy.Spider):
    name = "AmzProducts"
    allowed_domains = ["www.amazon.com"]

    ZIP_CODE = '19901'       # US delivery zip — must be set before product pages load
    AMAZON_HOME = 'https://www.amazon.com'

    custom_settings = {
        "ITEM_PIPELINES": {},           # spider writes directly to DB
        "DOWNLOAD_DELAY": 6,
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
                 limit=None, save_html='false', **kwargs):
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

        self.run_id = uuid.uuid4()
        self.run_date = dt.now().date()
        self.products_written = 0
        self.variants_queued = 0
        self._scraped_asins: list[str] = []

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
                'PLAYWRIGHT_MAX_PAGES_PER_CONTEXT': 4,
                # Block resources that add no scraping value. Images alone cause
                # significant memory growth over long runs (P12).
                'PLAYWRIGHT_ABORT_REQUEST': (
                    lambda req: req.resource_type in ('image', 'media', 'font', 'stylesheet')
                ),
                'DOWNLOAD_DELAY':                  6,
                'RANDOMIZE_DOWNLOAD_DELAY':        True,
                'CONCURRENT_REQUESTS':             4,
                'CONCURRENT_REQUESTS_PER_DOMAIN':  4,
            }, priority='spider')

        crawler.signals.connect(spider.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(spider.spider_closed, signal=signals.spider_closed)
        return spider

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def spider_opened(self, spider):
        self.db.connect()
        _scraping_dir = os.path.dirname(os.path.abspath(__file__))
        self._html_archive_dir = os.path.join(_scraping_dir, '..', 'html_archive')
        if self.save_html:
            os.makedirs(self._html_archive_dir, exist_ok=True)
        self.log(
            f"DB connected. run_id={self.run_id} "
            f"marketplace_id={self.marketplace_id} "
            f"use_playwright={self.use_playwright} "
            f"limit={self.limit} "
            f"save_html={self.save_html}",
            20,
        )

    def spider_closed(self, spider, reason):
        self._write_monitoring_stats()
        self.log(
            f"Spider closed [{reason}]. "
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
        If use_playwright=True: yield a bootstrap request to amazon.com that
        sets the delivery zip to 19901 via the location popover. The zip cookie
        persists for all subsequent product page requests in the same Playwright
        context, causing Amazon to serve US-local buybox pricing and seller info.

        The actual product queue is loaded and yielded from _load_product_queue()
        (the bootstrap callback) to guarantee zip is set before any product page
        is requested.

        If use_playwright=False: load queue directly and yield product requests.
        JS-rendered fields (price, seller, is_fba) will be NULL in this mode.
        """
        if self.use_playwright:
            yield scrapy.Request(
                url=self.AMAZON_HOME,
                callback=self._load_product_queue,
                meta={
                    'playwright': True,
                    'playwright_page_methods': [
                        PageMethod('wait_for_load_state', 'load'),
                        PageMethod('wait_for_timeout', 1500),
                        PageMethod('click', '#glow-ingress-block'),
                        PageMethod('wait_for_timeout', 1500),
                        PageMethod('fill', '#GLUXZipUpdateInput', self.ZIP_CODE),
                        PageMethod('wait_for_timeout', 500),
                        PageMethod('click', 'span#GLUXZipUpdate input.a-button-input'),
                        PageMethod('wait_for_timeout', 2500),
                    ],
                },
                errback=self.handle_error,
            )
        else:
            for req in self._build_product_requests():
                yield req

    def _load_product_queue(self, response):
        """
        Called after bootstrap sets the zip code.
        Loads the product queue from DB and yields one Request per ASIN.
        """
        self.log(f"Zip set to {self.ZIP_CODE}. Loading product queue...", 20)
        for req in self._build_product_requests():
            yield req

    def _build_product_requests(self) -> list:
        """Load pending ASINs from queue, return list of scrapy.Request objects."""
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
                "Queue is empty — nothing to scrape. "
                "Run seed_product_queue.sql first.",
                30,
            )
            return []

        self.log(
            f"Loaded {len(rows)} ASINs from queue "
            f"(marketplace={self.marketplace_id}, limit={self.limit}).",
            20,
        )

        requests = []
        for row in rows:
            meta: dict = {
                'asin':        row['asin'],
                'product_url': row['product_url'],
            }
            if self.use_playwright:
                meta['playwright'] = True
                meta['playwright_page_methods'] = [
                    # wait_for_load_state('load') covers initial HTML + blocking scripts.
                    # Fixed 3s wait for the buybox AJAX (price/seller) to settle.
                    # networkidle is NOT used — Amazon fires continuous analytics XHR
                    # that prevent it from ever firing (P22).
                    PageMethod('wait_for_load_state', 'load'),
                    PageMethod('wait_for_timeout', 3000),
                ]
            requests.append(
                scrapy.Request(
                    url=row['product_url'],
                    callback=self.parse_product,
                    meta=meta,
                    errback=self.handle_error,
                )
            )
        return requests

    def parse_product(self, response):
        """
        Parse one Amazon product detail page.
        Extracts all 20 fields, UPSERTs to staging.amz_product_snapshot,
        deletes from queue on success, queues discovered variant ASINs.
        """
        asin = response.meta['asin']

        self.log(f"Parsing product asin={asin} url={response.url}", 20)

        data = self._extract_product(response, asin)

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
            'bsr_entries':      self._parse_bsr(response),
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
        #bylineInfo is itself the <a> element (not a container).
        Two patterns:
          - "Visit the {Brand} Store"
          - "Brand: {Brand}"
        """
        text = (response.css('#bylineInfo::text').get() or '').strip()
        m = re.search(r'Visit the (.+?) Store', text)
        if m:
            return m.group(1).strip()
        if text.startswith('Brand: '):
            return text[7:].strip()
        if text:
            return text
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
    def _parse_last_month_sales(response) -> str | None:
        """
        Extracts "100+" or "1K+" from the "X bought in past month" banner.
        Amazon uses varying element types; xpath text search is most resilient.
        """
        for el in response.xpath('//*[contains(text(), "bought in past month")]'):
            text = (el.xpath('text()').get() or '').strip()
            m = re.search(r'([\d,K+]+)\s+bought in past month', text)
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
        Parse dimensionToAsinMap from inline <script> tags.
        Returns a sorted list of unique ASINs (excluding current product if present).
        Stores as simple ASIN list — type/value details require a deeper parse
        of variationValues which has a different key structure.
        """
        for script in response.css('script::text').getall():
            if 'dimensionToAsinMap' not in script:
                continue
            m = re.search(r'"dimensionToAsinMap"\s*:\s*(\{[^}]*\})', script)
            if m:
                try:
                    asin_map = json.loads(m.group(1))
                    asins = sorted(set(asin_map.values()))
                    return asins if asins else None
                except Exception:
                    pass
            break
        return None

    @staticmethod
    def _parse_related_asins(response) -> list | None:
        """Parse exportAlternativeAsinsInfo data attribute. Returns list of ASINs or None."""
        raw = response.css('#exportAlternativeAsinsInfo::attr(data-asinsinfo)').get()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            asins = list(data.keys())
            return asins if asins else None
        except Exception:
            return None

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
        return (response.css('#sellerProfileTriggerId::text').get() or '').strip() or None

    @staticmethod
    def _parse_seller_id(response) -> str | None:
        href = response.css('#sellerProfileTriggerId::attr(href)').get() or ''
        m = re.search(r'seller=([A-Z0-9]+)', href)
        return m.group(1) if m else None

    @staticmethod
    def _parse_is_fba(response) -> bool | None:
        """
        Returns True if Amazon fulfills, False if seller fulfills, None if seller
        info is missing (no buybox rendered — may mean out of stock or blocked page).
        """
        href = response.css('#sellerProfileTriggerId::attr(href)').get()
        if href is None:
            return None
        return bool(re.search(r'isAmazonFulfilled=1', href))

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
        for variant_asin in variant_asins:
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
