"""
AmzCategoryHierarchy spider
===========================
Traverses Amazon.com bestseller left-nav sidebar for the 10 target categories
and writes the full category tree directly to:

    transformed.amz_category          — one row per node
    transformed.amz_category_hierarchy — closure table (all ancestor-descendant pairs)

Run once per market, or re-run to refresh (upsert is idempotent).

Usage:
    cd scraping/
    scrapy crawl AmzCategoryHierarchy -a marketplace_id=amazon_us
"""

import scrapy
from scrapy import signals
from datetime import datetime as dt

from helpers.postgres_handler import PostgresDBHandler


class AmzCategoryHierarchySpider(scrapy.Spider):
    name = "AmzCategoryHierarchy"
    allowed_domains = ["www.amazon.com"]

    BASE_URL = "https://www.amazon.com/gp/bestsellers/"

    custom_settings = {
        "ITEM_PIPELINES": {},           # spider writes directly to DB — no pipeline needed
        "DOWNLOAD_DELAY": 3,
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

    def __init__(self, marketplace_id="amazon_us", **kwargs):
        super().__init__(**kwargs)
        self.marketplace_id = marketplace_id
        self.visited_urls: set[str] = set()
        self.visited_node_ids: set[str] = set()   # dedup by node_id — catches cross-URL cycles
        self.target_slugs: set[str] = set()        # populated from parse_root; only follow these slugs
        self.nodes_written = 0

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = cls(*args, **kwargs)
        spider.settings = crawler.settings
        spider.target_categories = [c.lower() for c in crawler.settings.get('TARGET_CATEGORIES', [])]
        spider.db = PostgresDBHandler(
            host=crawler.settings.get('POSTGRES_HOST'),
            database=crawler.settings.get('POSTGRES_DATABASE'),
            user=crawler.settings.get('POSTGRES_USERNAME'),
            password=crawler.settings.get('POSTGRES_PASSWORD'),
            port=crawler.settings.get('POSTGRES_PORT'),
        )
        crawler.signals.connect(spider.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(spider.spider_closed, signal=signals.spider_closed)
        return spider

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def spider_opened(self, spider):
        self.db.connect()
        self.log(f"DB connected. Target categories ({len(self.target_categories)}): {self.target_categories}", 20)

    def spider_closed(self, spider, reason):
        self.log(f"Spider closed [{reason}]. Nodes written: {self.nodes_written}", 20)
        self.db.close()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    async def start(self):
        yield scrapy.Request(
            url=self.BASE_URL,
            callback=self.parse_root,
        )

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
            # then attributes to all their subcategories incorrectly.
            # The category name is unique across our 10 target categories and fits VARCHAR(30).
            node_id = name
            self.visited_node_ids.add(node_id)

            self.log(f"Root category found: '{name}' → node_id={node_id}, slug={url_slug}", 20)

            self._write_node(
                node_id=node_id,
                node_name=name,
                url_slug=url_slug,
                parent_node_id=None,
                depth=0,
                ancestor_chain=[],
            )
            matched += 1

            yield scrapy.Request(
                url=url,
                callback=self.parse,
                meta={
                    'depth_level': 1,
                    'parent_node_id': node_id,
                    'parent_name': name,
                    'ancestor_chain': [node_id],   # ordered root → immediate parent
                },
            )

        if matched == 0:
            self.log("WARNING: No target categories found on root page. Check XPath or TARGET_CATEGORIES.", 40)
        else:
            self.log(f"Found {matched}/{len(self.target_categories)} target categories on root page.", 20)

    def parse(self, response):
        """
        Parse a category/subcategory page.
        Finds child nodes in the sidebar nav (treeitem links not yet visited).
        Each new link is a child of the current page's category.
        """
        parent_node_id = response.meta['parent_node_id']
        depth_level    = response.meta['depth_level']
        ancestor_chain = response.meta['ancestor_chain']

        children_found = 0

        for item in response.css('[class*="zg-browse-item"]'):
            name = (item.css('a::text').get() or '').strip()
            href = item.css('a::attr(href)').get() or ''
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

            # Only follow links within the 10 target category slugs —
            # Amazon's nav renders all root categories on every page, which would
            # pull in non-target categories (e.g. videogames, automotive) and
            # create runaway depth if not filtered here.
            if self.target_slugs and url_slug not in self.target_slugs:
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

            yield scrapy.Request(
                url=url,
                callback=self.parse,
                meta={
                    'depth_level': depth_level + 1,
                    'parent_node_id': node_id,
                    'parent_name': name,
                    'ancestor_chain': ancestor_chain + [node_id],
                },
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
            /Best-Sellers-Arts-Crafts-Sewing/zgbs/arts-crafts/           → 'arts-crafts'
            /Best-Sellers-Pet-Supplies-Dog-Supplies/zgbs/pet-supplies/2975312011/ → '2975312011'

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
            /Best-Sellers-Arts-Crafts-Sewing/zgbs/arts-crafts/           → 'arts-crafts'
            /Best-Sellers-Pet-Supplies-Dog-Supplies/zgbs/pet-supplies/2975312011/ → 'pet-supplies'
        """
        path = url.split('?')[0].rstrip('/')
        parts = path.split('/')
        try:
            idx = parts.index('zgbs')
            return parts[idx + 1] if idx + 1 < len(parts) else ''
        except ValueError:
            return ''
