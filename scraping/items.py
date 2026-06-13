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
    # identity
    marketplace_id = scrapy.Field()
    asin = scrapy.Field()
    # static fields
    title = scrapy.Field()
    brand = scrapy.Field()
    main_image_url = scrapy.Field()
    launch_date = scrapy.Field()         # raw text, e.g. "January 1, 2023"
    about_this_item = scrapy.Field()     # newline-delimited bullets
    # volatile fields
    rating = scrapy.Field()
    review_count = scrapy.Field()
    rating_breakdown = scrapy.Field()    # dict: {"5": 63, "4": 12, ...}
    bsr_entries = scrapy.Field()         # list: [{"rank": 360, "category": "..."}]
    last_month_sales = scrapy.Field()    # raw text, e.g. "100+" or "1K+"
    # JS-rendered fields (Playwright + zip 19901)
    price = scrapy.Field()
    seller_name = scrapy.Field()
    seller_id = scrapy.Field()
    is_fba = scrapy.Field()
    # variant and related products
    has_variants = scrapy.Field()
    variant_asins = scrapy.Field()       # list of ASINs
    related_asins = scrapy.Field()       # list of ASINs
    # product attributes (sparse)
    weight = scrapy.Field()
    dimensions = scrapy.Field()
    # metadata
    is_small_business = scrapy.Field()
    html_file_path = scrapy.Field()
