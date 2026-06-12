import scrapy


class AmzCategoryItem(scrapy.Item):
    marketplace_id = scrapy.Field()
    node_id = scrapy.Field()
    node_name = scrapy.Field()
    url_slug = scrapy.Field()
    parent_node_id = scrapy.Field()
    depth = scrapy.Field()
    is_leaf = scrapy.Field()


class AmzRankingItem(scrapy.Item):
    run_id = scrapy.Field()
    marketplace_id = scrapy.Field()
    list_type = scrapy.Field()
    category = scrapy.Field()
    subcategory = scrapy.Field()
    subcategory_node_id = scrapy.Field()
    depth = scrapy.Field()
    rank_position = scrapy.Field()
    asin = scrapy.Field()
    title = scrapy.Field()
    rating = scrapy.Field()
    review_count = scrapy.Field()
    price = scrapy.Field()
    product_url = scrapy.Field()
    scraped_at = scrapy.Field()


class AmzProductItem(scrapy.Item):
    # Schema TBD — populated when AmzProducts spider is built
    asin = scrapy.Field()
    marketplace_id = scrapy.Field()
