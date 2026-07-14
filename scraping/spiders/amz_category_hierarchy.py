"""
AmzCategoryHierarchy spider
===========================
Traverses Amazon.com bestseller left-nav sidebar for the 10 target categories
and writes the full category tree directly to:

    transformed.amz_category          — one row per node
    transformed.amz_category_hierarchy — closure table (all ancestor-descendant pairs)

Run once per market, or re-run to refresh (upsert is idempotent).

Playwright is enabled by default (use_playwright=true). It sets a US delivery zip
(19901) via the Amazon homepage location popover before any category pages are
fetched. Without this, Amazon may serve geo-specific nav from a non-US IP, producing
a category tree that differs from what US users see.

Usage:
    cd scraping/

    # Standard run — Playwright, US zip set automatically
    scrapy crawl AmzCategoryHierarchy -a marketplace_id=amazon_us

    # Single-category test run — use -a target_categories (pipe-delimited, avoids JSON quoting issues)
    scrapy crawl AmzCategoryHierarchy -a marketplace_id=amazon_us -a "target_categories=Home & Kitchen"

    # Multi-category subset
    scrapy crawl AmzCategoryHierarchy -a marketplace_id=amazon_us -a "target_categories=Home & Kitchen|Pet Supplies"

    # Plain HTTP (no zip — only use from a US IP)
    scrapy crawl AmzCategoryHierarchy -a marketplace_id=amazon_us -a use_playwright=false

MUST READ before modifying: docs/scraping_pitfalls.md
  P1  — use async def start(), not start_requests()
  P5  — psycopg2 rollback on every error (in PostgresDBHandler)
  P9  — from_crawler must call _set_crawler()
  P21 — root node_id must be category name, not URL slug
"""

import json
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

from helpers.postgres_handler import PostgresDBHandler


async def apply_stealth(page, request):
    await _stealth.apply_stealth_async(page)


class AmzCategoryHierarchySpider(scrapy.Spider):
    name = "AmzCategoryHierarchy"
    allowed_domains = ["www.amazon.com"]

    BASE_URL    = "https://www.amazon.com/gp/bestsellers/"
    AMAZON_HOME = "https://www.amazon.com"
    ZIP_CODE    = "19901"   # US delivery zip — makes Amazon serve US-local category nav

    custom_settings = {
        "ITEM_PIPELINES": {},           # spider writes directly to DB — no pipeline needed
        "DOWNLOAD_DELAY": 8,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "CONCURRENT_REQUESTS": 4,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
        "DEPTH_LIMIT": 0,               # no Scrapy depth limit — we track depth via meta
        "RETRY_TIMES": 2,
        "RETRY_HTTP_CODES": [429, 503, 403],
        "DOWNLOADER_MIDDLEWARES": {
            'scrapy.downloadermiddlewares.useragent.UserAgentMiddleware': None,
            'scrapy_user_agents.middlewares.RandomUserAgentMiddleware': 400,
            'scrapy.downloadermiddlewares.defaultheaders.DefaultHeadersMiddleware': None,
            'middlewares.HeaderRotationMiddleware': 500,
            'scrapy.downloadermiddlewares.cookies.CookiesMiddleware': 700,
        },
    }

    def __init__(self, marketplace_id="amazon_us", use_playwright="true",
                 target_categories=None, **kwargs):
        super().__init__(**kwargs)
        self.marketplace_id = marketplace_id
        self.use_playwright = str(use_playwright).lower() not in ('false', '0', 'no')
        if self.use_playwright and not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "use_playwright=true but scrapy-playwright is not installed.\n"
                "Run: pip install scrapy-playwright && playwright install chromium"
            )
        # target_categories spider arg: pipe-delimited string, e.g. "Home & Kitchen|Pet Supplies"
        # Takes priority over the TARGET_CATEGORIES setting when set.
        self._target_categories_arg = target_categories
        self.visited_urls: set[str] = set()
        self.visited_node_ids: set[str] = set()   # dedup by node_id — catches cross-URL cycles
        self.target_slugs: set[str] = set()        # populated from parse_root; only follow these slugs
        self.nodes_written = 0

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = cls(*args, **kwargs)
        spider._set_crawler(crawler)   # sets spider.crawler + spider.settings (Scrapy 2.16 API — see P9)

        if spider._target_categories_arg:
            # Spider arg (-a target_categories="Cat1|Cat2") — pipe-delimited, no JSON quoting issues
            raw_cats = [c.strip() for c in spider._target_categories_arg.split('|') if c.strip()]
        else:
            # Settings fallback (-s TARGET_CATEGORIES=...) — JSON array string or list
            raw_cats = crawler.settings.get('TARGET_CATEGORIES', [])
            if isinstance(raw_cats, str) and raw_cats:
                raw_cats = json.loads(raw_cats)

        spider.target_categories = [c.lower() for c in raw_cats]
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
                'PLAYWRIGHT_ABORT_REQUEST': (
                    lambda req: req.resource_type in ('image', 'media', 'font', 'stylesheet')
                ),
            }, priority='spider')

        crawler.signals.connect(spider.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(spider.spider_closed, signal=signals.spider_closed)
        return spider

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def spider_opened(self, spider):
        self.db.connect()
        # Pre-populate visited_node_ids from categories NOT in this run.
        # Protects already-complete categories from DAG node re-claiming during partial restarts.
        # On a full fresh run (truncated DB) this is a no-op; on a full re-run of all categories
        # the NOT IN clause excludes nothing, so again no rows are loaded.
        if self.target_categories:
            placeholders = ','.join(['%s'] * len(self.target_categories))
            rows = self.db.read(
                query=f"""
                    SELECT DISTINCT c.node_id
                    FROM transformed.amz_category c
                    JOIN transformed.amz_category_hierarchy h
                        ON h.descendant_node_id = c.node_id
                        AND h.marketplace_id = c.marketplace_id
                    JOIN transformed.amz_category root
                        ON root.node_id = h.ancestor_node_id
                        AND root.marketplace_id = h.marketplace_id
                        AND root.depth = 0
                    WHERE c.marketplace_id = %s
                      AND LOWER(root.node_name) NOT IN ({placeholders})
                """,
                params=[self.marketplace_id] + list(self.target_categories),
            )
            self.visited_node_ids.update(row['node_id'] for row in rows)
        self.log(
            f"DB connected. use_playwright={self.use_playwright} "
            f"Pre-loaded {len(self.visited_node_ids)} node_ids from non-target categories. "
            f"Target categories ({len(self.target_categories)}): {self.target_categories}",
            20,
        )

    def spider_closed(self, spider, reason):
        self.log(f"Spider closed [{reason}]. Nodes written: {self.nodes_written}", 20)
        self.db.close()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    async def start(self):
        """
        If use_playwright=True: yield a bootstrap request to amazon.com that sets
        the delivery zip to 19901 via the location popover. The zip cookie persists
        for all subsequent category page requests in the same Playwright browser
        context, causing Amazon to serve the US-local category nav tree.

        The actual crawl starts from _after_bootstrap() to guarantee zip is set
        before any bestseller page is requested.

        If use_playwright=False: go straight to the bestseller root page. Only use
        this from a US IP.
        """
        if self.use_playwright:
            yield scrapy.Request(
                url=self.AMAZON_HOME,
                callback=self._after_bootstrap,
                meta={
                    'playwright': True,
                    'playwright_page_init_callback': apply_stealth,
                    'playwright_page_methods': [
                        PageMethod('wait_for_load_state', 'load'),
                        PageMethod('wait_for_timeout', 1500),
                        PageMethod('click', '#glow-ingress-block'),
                        PageMethod('wait_for_selector', '#GLUXZipUpdateInput',
                                   state='visible', timeout=15000),
                        PageMethod('fill', '#GLUXZipUpdateInput', self.ZIP_CODE),
                        PageMethod('wait_for_timeout', 500),
                        PageMethod('click', 'span#GLUXZipUpdate input.a-button-input'),
                        PageMethod('wait_for_timeout', 2500),
                    ],
                },
            )
        else:
            yield scrapy.Request(
                url=self.BASE_URL,
                callback=self.parse_root,
            )

    def _after_bootstrap(self, response):
        """Called after zip is set. Yields the bestseller root page request."""
        self.log(f"Zip set to {self.ZIP_CODE}. Starting category crawl...", 20)
        yield scrapy.Request(
            url=self.BASE_URL,
            callback=self.parse_root,
            meta=self._playwright_meta(),
        )

    def _playwright_meta(self) -> dict:
        """
        Playwright meta for category nav pages. domcontentloaded is sufficient —
        the left-nav sidebar is server-rendered HTML, no lazy-loading needed.
        """
        return {
            'playwright': True,
            'playwright_page_init_callback': apply_stealth,
            'playwright_page_methods': [
                PageMethod('wait_for_load_state', 'domcontentloaded'),
            ],
        }

    def parse_root(self, response):
        """
        Parse the root /gp/bestsellers/ page.
        Finds top-level category links matching our 10 target categories.
        Each match becomes a root node (depth=0, parent=None).
        """
        matched = 0
        for item in response.css('[class*="zg-browse-item"]'):
            name = (item.css('a::text').get() or '').strip()
            href = item.css('a::attr(href)').get() or ''
            if not name or not href:
                continue

            if name.lower() not in self.target_categories:
                continue

            url = self._clean_url(response.urljoin(href))
            if url in self.visited_urls:
                continue
            self.visited_urls.add(url)

            url_slug = self._slug(url)
            self.target_slugs.add(url_slug)         # record slugs we're allowed to crawl

            # Use the category name as node_id for root nodes.
            # _node_id() returns the URL slug (e.g. 'hi') for root categories that have
            # no numeric ID in the URL. Multiple root categories can share the same slug
            # (Home & Kitchen, Kitchen & Dining, and Tools & Home Improvement all use /hi/).
            # Using the slug as node_id means all three overwrite the same row in
            # amz_category, leaving a single contaminated root that seed_controller.sql
            # then attributes to all their subcategories incorrectly (P21).
            node_id = name
            self.visited_node_ids.add(node_id)

            self.log(f"Root category found: '{name}' node_id={node_id} slug={url_slug}", 20)

            self._write_node(
                node_id=node_id,
                node_name=name,
                url_slug=url_slug,
                parent_node_id=None,
                depth=0,
                ancestor_chain=[],
            )
            matched += 1

            meta = {
                'depth_level': 1,
                'parent_node_id': node_id,
                'parent_name': name,
                'ancestor_chain': [node_id],   # ordered root → immediate parent
                'root_url_slug': url_slug,      # slug of this root category — enforced on all descendants
            }
            if self.use_playwright:
                meta.update(self._playwright_meta())

            yield scrapy.Request(
                url=url,
                callback=self.parse,
                meta=meta,
            )

        if matched == 0:
            self.log("WARNING: No target categories found on root page. Check XPath or TARGET_CATEGORIES.", 40)
        else:
            self.log(f"Found {matched}/{len(self.target_categories)} target categories on root page.", 20)

    def parse(self, response):
        """
        Parse a category/subcategory page.
        Finds ONLY the direct children of the currently-selected nav node and follows them.

        Amazon's nav sidebar renders three types of zg-browse-item links on every page:
          - Ancestors (breadcrumb path, shown with ‹ prefix and zg-browse-up class)
          - The current selected node (span[aria-current="page"], no <a> link)
          - Children (nested in a wrapper <li> immediately after the selected <li>)
          - Siblings (plain <li> items at the same depth as the selected node — leaf pages)

        The old approach (iterating all zg-browse-item) incorrectly treated siblings as
        children, causing ancestor chains to grow to depth 48 for 3-level-deep nodes (P27).

        Fix: XPath targets only the <li> that immediately follows the selected node AND
        contains a nested <ul> (children wrapper). At leaf nodes, following siblings are
        plain <li> items with no nested <ul>, so the predicate returns empty — correct
        leaf detection with no code change needed.
        """
        parent_node_id = response.meta['parent_node_id']
        depth_level    = response.meta['depth_level']
        ancestor_chain = response.meta['ancestor_chain']
        root_url_slug  = response.meta.get('root_url_slug', '')

        children_found = 0

        # Anchors in the children wrapper immediately following the selected nav item.
        # The [.//ul] predicate excludes leaf pages where following siblings are plain
        # link items (siblings of the current node, not its children).
        child_anchors = response.xpath(
            '//span[@aria-current="page"]/ancestor::li[1]/following-sibling::li[1][.//ul]//a'
        )

        for a in child_anchors:
            name = (a.xpath('text()').get() or '').strip()
            href = a.attrib.get('href', '') or ''
            if not name or not href:
                continue

            url = self._clean_url(response.urljoin(href))
            if url in self.visited_urls:
                continue

            node_id = self._node_id(url)
            if not node_id:
                continue

            # Dedup by node_id — catches same node reachable via different URL paths
            if node_id in self.visited_node_ids:
                continue

            url_slug = self._slug(url)

            # Skip children whose URL slug differs from the root category's slug.
            # Amazon sometimes shows cross-category node links in the bestseller nav
            # (e.g. H&K nav showing a T&HI node). Following these would write wrong
            # ancestry chains. root_url_slug is set in parse_root() and propagated
            # through all child request metas.
            if root_url_slug and url_slug != root_url_slug:
                self.log(
                    f"  Skipping '{name}' — slug '{url_slug}' ≠ root slug '{root_url_slug}'",
                    20,
                )
                continue

            self.visited_urls.add(url)
            self.visited_node_ids.add(node_id)

            self.log(
                f"  {'  ' * depth_level}Child: '{name}' "
                f"node_id={node_id} depth={depth_level} parent={parent_node_id}",
                20,
            )

            self._write_node(
                node_id=node_id,
                node_name=name,
                url_slug=url_slug,
                parent_node_id=parent_node_id,
                depth=depth_level,
                ancestor_chain=ancestor_chain,
            )
            children_found += 1

            meta = {
                'depth_level': depth_level + 1,
                'parent_node_id': node_id,
                'parent_name': name,
                'ancestor_chain': ancestor_chain + [node_id],
                'root_url_slug': root_url_slug,
            }
            if self.use_playwright:
                meta.update(self._playwright_meta())

            yield scrapy.Request(
                url=url,
                callback=self.parse,
                meta=meta,
            )

        # If no new children found, the current node is a leaf
        if children_found == 0 and parent_node_id:
            self.log(f"  Leaf node: {parent_node_id}", 20)
            self.db.execute(f"""
                UPDATE transformed.amz_category
                SET is_leaf = TRUE, last_verified_at = NOW()
                WHERE marketplace_id = '{self.marketplace_id}'
                  AND node_id = '{parent_node_id}'
            """)

    # ------------------------------------------------------------------
    # DB writes
    # ------------------------------------------------------------------

    def _write_node(self, node_id, node_name, url_slug, parent_node_id, depth, ancestor_chain):
        """Upsert the node into amz_category and populate all closure table entries."""

        # 1. Upsert the node itself
        self.db.bulk_upsert(
            table='transformed.amz_category',
            data=[{
                'marketplace_id':   self.marketplace_id,
                'node_id':          node_id,
                'node_name':        node_name,
                'url_slug':         url_slug,
                'parent_node_id':   parent_node_id,
                'depth':            depth,
                'is_leaf':          False,       # updated to TRUE when no children found
                'is_active':        True,
                'first_seen_at':    dt.now(),
                'last_verified_at': dt.now(),
            }],
            conflict_columns=['marketplace_id', 'node_id'],
            update_columns=[
                'node_name', 'url_slug', 'parent_node_id',
                'depth', 'is_active', 'last_verified_at',
            ],
        )

        # 2. Build all closure table entries for this node
        #    ancestor_chain is ordered [root_id, ..., immediate_parent_id]
        # Build closure records; deduplicate by (ancestor_node_id, descendant_node_id)
        # to guard against cycles in Amazon's nav (selected node can appear in its own page).
        seen_pairs: set[tuple] = set()
        closure_records = []
        for ancestor_id, distance in [(node_id, 0)] + [
            (aid, d) for d, aid in enumerate(reversed(ancestor_chain), start=1)
        ]:
            pair = (ancestor_id, node_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            closure_records.append({
                'marketplace_id':       self.marketplace_id,
                'ancestor_node_id':     ancestor_id,
                'descendant_node_id':   node_id,
                'depth_from_ancestor':  distance,
            })

        self.db.bulk_upsert(
            table='transformed.amz_category_hierarchy',
            data=closure_records,
            conflict_columns=['marketplace_id', 'ancestor_node_id', 'descendant_node_id'],
            update_columns=['depth_from_ancestor'],
        )

        self.nodes_written += 1

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_url(url: str) -> str:
        """Strip /ref=... tracking params and trailing slashes."""
        return url.split('/ref')[0].rstrip('/')

    @staticmethod
    def _node_id(url: str) -> str | None:
        """
        Extract the node identifier from a bestseller URL.

        URL pattern (post-2024): /Best-Sellers-<Title>/zgbs/<slug>[/<numeric_id>][/ref=...]
        Examples:
            /Best-Sellers-Arts-Crafts-Sewing/zgbs/arts-crafts/           -> 'arts-crafts'
            /Best-Sellers-Pet-Supplies-Dog-Supplies/zgbs/pet-supplies/2975312011/ -> '2975312011'

        Returns None if URL doesn't contain a recognisable zgbs path.
        """
        path = url.split('?')[0].rstrip('/')
        parts = path.split('/')
        try:
            idx = parts.index('zgbs')
        except ValueError:
            return None
        # Segments after 'zgbs': [slug] or [slug, numeric_id]
        after = parts[idx + 1:]
        if not after:
            return None
        # If last segment is numeric, that's the node ID
        if len(after) >= 2 and after[-1].isdigit():
            return after[-1]
        # Otherwise the slug IS the root node identifier
        return after[0] if after[0] else None

    @staticmethod
    def _slug(url: str) -> str:
        """
        Extract the category slug — always the first segment after 'zgbs'.

        Examples:
            /Best-Sellers-Arts-Crafts-Sewing/zgbs/arts-crafts/           -> 'arts-crafts'
            /Best-Sellers-Pet-Supplies-Dog-Supplies/zgbs/pet-supplies/2975312011/ -> 'pet-supplies'
        """
        path = url.split('?')[0].rstrip('/')
        parts = path.split('/')
        try:
            idx = parts.index('zgbs')
            return parts[idx + 1] if idx + 1 < len(parts) else ''
        except ValueError:
            return ''
