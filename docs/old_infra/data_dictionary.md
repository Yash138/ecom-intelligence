# Data Dictionary — transformed & curated schemas

> **Database:** `ecommerce`  
> **Scope:** `transformed` schema and `curated` schema (staging tables excluded)  
> **Last updated:** 2026-04-16

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-04-16 | All | Initial column-level documentation for transformed and curated schemas |

## Table of Contents

- [transformed schema](#transformed-schema)
  - [trf_amz__product_master](#transformedtrf_amz__product_master)
  - [trf_amz__product_details](#transformedtrf_amz__product_details)
  - [trf_amz__product_category_master](#transformedtrf_amz__product_category_master)
  - [trf_amz__seller](#transformedtrf_amz__seller)
  - [trf_amz__product_rankings / partitions](#transformedtrf_amz__product_rankings--partitions)
  - [amz__category](#transformedamz__category)
  - [amz__category_hierarchy](#transformedamz__category_hierarchy)
  - [amz__category_hierarchy_flattened](#transformedamz__category_hierarchy_flattened)
  - [amz__category_refresh_controller](#transformedamz__category_refresh_controller)
- [curated schema](#curated-schema)
  - [amz__product_consolidated (and partitions)](#curatedamz__product_consolidated-and-category-partitions)
  - [amz__lc_opportunity_signals](#curatedamz__lc_opportunity_signals)
  - [amz__best_sellers](#curatedamz__best_sellers)
  - [amz__product_details (legacy)](#curatedamz__product_details-legacy)
  - [amz__product_category (reference)](#curatedamz__product_category-reference)
  - [amz__product_subcategory_old (legacy)](#curatedamz__product_subcategory_old-legacy-reference)
- [curated views — column reference](#curated-views--column-reference)
  - [vw_amz__product_demand_analysis](#curatedvw_amz__product_demand_analysis)
  - [vw_amz__lc_demand_analysis](#curatedvw_amz__lc_demand_analysis)
  - [vw_amz_kpi__product_daily_performance](#curatedvw_amz_kpi__product_daily_performance)
  - [vw_amz__brandwise_volume](#curatedvw_amz__brandwise_volume)
  - [vw_amz__product_count_brandwise](#curatedvw_amz__product_count_brandwise)
  - [vw_best_selling_lowest_categories](#curatedvw_best_selling_lowest_categories)
  - [vw_amz__pending_category_refresh (transformed)](#transformedvw_amz__pending_category_refresh)

---

This document provides column-level descriptions for every table and view in the `transformed` and `curated` schemas. For each column, the entry includes the data type, nullability, and a description that explains what the value means in the context of how it is populated by the scraping pipeline.

---

## transformed schema

---

### transformed.trf_amz__product_master

Core product identity table. One row per `(asin, scrape_date)` — a new row is inserted each time a tracked field changes. The most current version of a product is identified by `is_latest = TRUE`.

| Column | Type | Nullable | PK | Description |
|---|---|---|---|---|
| `asin` | varchar(20) | NOT NULL | ✓ | Amazon Standard Identification Number. Unique 10-character alphanumeric product identifier assigned by Amazon. This is the primary business key used to track a product across the entire pipeline. |
| `product_name` | text | NOT NULL | | Full product title as scraped from the `#productTitle` element on the Amazon product page. Includes model numbers, sizes, colours, and other descriptors in the title. |
| `brand_name` | varchar(200) | NULL | | Brand name extracted from the `#bylineInfo_feature_div` byline section on the product page. NULL when the brand is not listed (e.g. generic or private-label products without a byline). |
| `launch_date` | date | NULL | | The "Date First Available" value from the product's technical details table. Represents when the product listing was first created on Amazon India. NULL when the field is not present on the listing. |
| `product_url` | text | NOT NULL | | Canonical URL of the Amazon India product listing page (`/dp/<ASIN>/...`). Stripped of tracking query parameters. |
| `is_latest` | boolean | NULL | | SCD2 currency flag. `TRUE` for the currently active record (the most recent known version). `FALSE` for superseded historical versions. Filter on `is_latest = TRUE` to get the current state of each product. |
| `scrape_date` | timestamp | NOT NULL | ✓ | Timestamp when the Scrapy spider captured this product page. Serves as the effective date in the SCD2 pattern — the combination `(asin, scrape_date)` is the primary key. |

---

### transformed.trf_amz__product_details

Product KPI/metrics table. One row per `(asin, scrape_date)` — a new row is inserted whenever any tracked metric changes from the current value. The most current metrics are identified by `is_latest = TRUE`.

| Column | Type | Nullable | PK | Description |
|---|---|---|---|---|
| `asin` | varchar(20) | NOT NULL | ✓ | Amazon product identifier. Foreign key to `trf_amz__product_master.asin`. |
| `last_month_sale` | integer | NULL | | Estimated units sold in the last 30 days. Sourced from the "X bought in past month" social-proof badge on the product page (e.g. "1K+ bought in past month" → `1000`). NULL or `0` if the badge is not present (low-volume products). This is Amazon's displayed estimate, not an exact figure. |
| `rating` | numeric(3,2) | NULL | | Average customer star rating rounded to 2 decimal places (range: 1.00–5.00). Scraped from the `#averageCustomerReviews` widget. NULL when no reviews exist for the product. |
| `reviews_count` | integer | NULL | | Total number of global customer ratings. Sourced from the ratings count link next to the star rating widget. NULL if no ratings exist. Note: this is the count of ratings (including those without text), not just written reviews. |
| `sell_mrp` | numeric(16,2) | NULL | | Maximum Retail Price in Indian Rupees (INR), shown as "M.R.P.: ₹X,XXX" on the listing. This is the price before any discount. NULL when the product does not list an MRP (common for some categories). |
| `sell_price` | numeric(16,2) | NULL | | Actual selling price in INR. Extracted from various price display elements (`#corePriceDisplay`, `.apexPriceToPay`, etc.) depending on listing layout. NULL when the current price cannot be determined (e.g. "Price not listed" or only available via "See price at checkout"). |
| `is_fba` | boolean | NULL | | Fulfilled By Amazon flag. `TRUE` when the "Ships from" attribute in the product details is "Amazon.com" (or equivalent Amazon fulfilment centre). `FALSE` for merchant-fulfilled or third-party shipped. NULL when the fulfilment info is not available on the page. |
| `is_variant_available` | boolean | NULL | | `TRUE` if the product listing exposes multiple variants (e.g. size, colour, material options). Derived from the `data-totalvariationcount` attribute on the variant selection widget — `TRUE` when count > 0. |
| `is_oos` | boolean | NULL | | Out-of-stock flag. `TRUE` when the page shows "Currently unavailable." or "Temporarily out of stock." text, or when the "Add to Cart" button is absent. `FALSE` for in-stock products. |
| `is_latest` | boolean | NULL | | SCD2 currency flag. `TRUE` for the currently active metrics snapshot. Filter on `is_latest = TRUE` to get each product's current KPIs. |
| `scrape_date` | timestamp | NOT NULL | ✓ | Timestamp when the spider captured these metrics. Part of the composite primary key with `asin`. |

---

### transformed.trf_amz__product_category_master

ASIN-to-category mapping table. Append-only with upsert-do-nothing on conflict. An ASIN may appear multiple times if it belongs to multiple top-level categories (rare but possible).

| Column | Type | Nullable | PK | Description |
|---|---|---|---|---|
| `asin` | varchar(20) | NOT NULL | ✓ | Amazon product identifier. |
| `category` | varchar(50) | NOT NULL | ✓ | Top-level category code (URL slug) as scraped from the product breadcrumb or Best Sellers Rank section. Examples: `kitchen`, `electronics`, `sports`, `hpc`, `pet-supplies`. Mapped via `transformed.amz__category` where an Amazon numeric category ID is encountered in place of a slug. |
| `lowest_category` | varchar(50) | NOT NULL | ✓ | The most specific (leaf-node) category the product belongs to, as observed on its listing. This is Amazon's numeric node ID extracted from the breadcrumb or BSR section URLs (e.g. `1380441031`). Used to join with `amz__category_hierarchy` for hierarchy resolution. |
| `inserted_on` | timestamp | NULL | | Row creation timestamp. Populated by `CURRENT_TIMESTAMP` default when the row is first inserted. Not updated on subsequent scrapes due to the `ON CONFLICT DO NOTHING` behaviour. |

---

### transformed.trf_amz__seller

Seller and brand relationship table. One row per unique `(seller_id, brand_name)` combination. Upserted — seller name and store URL are updated if newer data contains them.

| Column | Type | Nullable | PK | Description |
|---|---|---|---|---|
| `seller_id` | varchar(100) | NOT NULL | ✓ | Amazon seller account identifier. Extracted from the seller's page URL (e.g. `/s?i=merchant&me=<seller_id>` or `/seller/<seller_id>`). Used to uniquely identify a seller account independently of their display name. |
| `seller_name` | varchar(255) | NULL | | Display name of the selling entity as shown in the "Sold by" section of the product listing. NULL when the seller link exists but the display name cannot be extracted. Updated on upsert if a new non-NULL value is available. |
| `brand_name` | varchar(200) | NOT NULL | ✓ | The brand name associated with this seller's product. Extracted from the byline on the product page. Part of the composite key because a seller account may sell products under multiple brand names. |
| `brand_store_url` | text | NULL | | URL to the brand's official Amazon India storefront (`/stores/<brand-name>/...`). Extracted from the byline link on the product page. NULL when no storefront URL is present. Updated on upsert with the latest non-NULL value. |
| `inserted_on` | timestamp | NULL | | Row creation timestamp (defaults to `CURRENT_TIMESTAMP`). Not updated on subsequent upserts. |

---

### transformed.trf_amz__product_rankings / partitions

Historical product ranking events. One row per observed appearance of an ASIN on a category ranking list page on a given scrape. Partitioned by `list_type` for query efficiency.

| Column | Type | Nullable | PK | Description |
|---|---|---|---|---|
| `id` | bigint | NOT NULL | ✓ | Auto-incremented surrogate key. Part of the composite PK with `list_type` to support partitioning. |
| `list_type` | varchar(50) | NOT NULL | ✓ | The Amazon ranking list type this record was scraped from. Values: `bestsellers` (top-selling products), `movers_and_shakers` (products with biggest recent rank improvement), `hot_new_releases` (top new/recent products), `most_wished_for` (most wish-listed products), `default` (catch-all for any other list). Also the partition key. |
| `category` | varchar(100) | NOT NULL | | Top-level Amazon category slug where this product was ranked (e.g. `kitchen`, `electronics`). |
| `sub_category` | varchar(100) | NULL | | Sub-category page slug where the product was ranked (the last path segment of the category URL). NULL if the product was ranked at the top-level category page rather than a sub-category. |
| `asin` | varchar(50) | NULL | | ASIN of the product at this rank position. NULL in rare cases where the page renders a rank slot without an identifiable product. |
| `rank` | integer | NULL | | Ordinal position of this product on the list page at the time of scraping (1 = highest/best ranked). Extracted from the rank badge displayed on each product card. |
| `sales_rank` | varchar(100) | NULL | | Raw sales rank text from the listing. Primarily used for `movers_and_shakers` list type where this field contains the rank change string (e.g. "+2,500 in Best Sellers"). NULL for other list types. |
| `product_url` | text | NULL | | URL of the product's detail page as observed on the category listing. May include tracking parameters; these are preserved as scraped. |
| `scrape_date` | timestamp | NULL | | Timestamp when this ranking entry was captured. Defaults to `CURRENT_TIMESTAMP` at insertion time. |

---

### transformed.amz__category

Top-level category reference / lookup table. Static data maintained manually or via a one-time load.

| Column | Type | Nullable | PK | Description |
|---|---|---|---|---|
| `category_code` | varchar(30) | NOT NULL | ✓ | Internal URL slug for this category as used throughout the pipeline (e.g. `hpc`, `pet-supplies`, `home-improvement`). This is the authoritative identifier used in `trf_amz__product_category_master.category` and all DAG configurations. |
| `category_name` | varchar(50) | NOT NULL | | Full human-readable display name for this category (e.g. `Health, Personal Care & Baby`, `Pet Supplies`, `Home Improvement`). Used in views and dashboards to replace internal codes with readable labels. |
| `category_id` | varchar(20) | NULL | | Amazon's numeric browse-node ID for this top-level category (e.g. `976419031` for Kitchen). Some product pages report this numeric ID in their breadcrumbs instead of the slug; this field allows resolution from numeric ID back to the pipeline's category code. |

---

### transformed.amz__category_hierarchy

Full category tree structure for each top-level category. Each row represents one root-to-leaf path through the Amazon category tree. Populated by the `CategoryRefresh` spider.

| Column | Type | Nullable | PK | Description |
|---|---|---|---|---|
| `category` | text | NOT NULL | ✓ | Root-level category identifier (e.g. `kitchen`). Corresponds to `amz__category.category_code`. |
| `lvl1` | text | NULL | | Amazon node ID at hierarchy depth 1 under the root category. |
| `lvl2` | text | NULL | | Amazon node ID at hierarchy depth 2. NULL if the path is shorter than 2 levels. |
| `lvl3` | text | NULL | | Amazon node ID at hierarchy depth 3. NULL if the path is shorter than 3 levels. |
| `lvl4` | text | NULL | | Amazon node ID at hierarchy depth 4. NULL if the path is shorter than 4 levels. |
| `lvl5` | text | NULL | | Amazon node ID at hierarchy depth 5. NULL if the path is shorter than 5 levels. |
| `lvl6` | text | NULL | | Amazon node ID at hierarchy depth 6. NULL if the path is shorter than 6 levels. |
| `lvl7` | text | NULL | | Amazon node ID at hierarchy depth 7. NULL if the path is shorter than 7 levels. |
| `lvl8` | text | NULL | | Amazon node ID at hierarchy depth 8. NULL if the path is shorter than 8 levels. |
| `data_node` | text | NOT NULL | ✓ | The leaf-node (most specific) category ID in this path. This is the `lowest_category` value that appears in `trf_amz__product_category_master` and `amz__category_refresh_controller`. The combination `(category, data_node)` is unique — one row per root-to-leaf path. |

---

### transformed.amz__category_hierarchy_flattened

Flat ID-to-name lookup for every category node across all levels of the hierarchy.

| Column | Type | Nullable | PK | Description |
|---|---|---|---|---|
| `category_id` | text | NOT NULL | ✓ | Amazon numeric node ID or category slug. Can appear as any level node (lvl1 through lvl8, data_node) in `amz__category_hierarchy`. Used as the join key when resolving node IDs to names in views. |
| `category_name` | text | NULL | | Human-readable display name for this node as it appears in Amazon's category tree (e.g. "Cookware", "Frying Pans & Skillets", "Laptops"). NULL if the node was recorded without a name. |

---

### transformed.amz__category_refresh_controller

Scraping progress and schedule tracker for the `AmzProductsLC` spider. One row per `(category, lowest_category)` — the finest granularity at which product listing pages are scraped.

| Column | Type | Nullable | PK | Description |
|---|---|---|---|---|
| `category` | varchar(50) | NOT NULL | ✓ | Top-level category code (e.g. `kitchen`). Matches `trf_amz__product_category_master.category`. |
| `lowest_category` | varchar(50) | NOT NULL | ✓ | Leaf-node sub-category identifier (Amazon numeric node ID). Matches `trf_amz__product_category_master.lowest_category`. The URL format is `https://www.amazon.in/s?i={category}&rh=n%3A{lowest_category}`. |
| `refreshed_pages_upto` | integer | NULL | | Number of search result pages that have been successfully scraped in the most recent run. Compared against `total_pages` to detect partially-completed scrape jobs. Reset to 0 when a full refresh is triggered. Updated in real-time by the spider during scraping via `sp_amz__category_refresh_controller`. |
| `total_pages` | integer | NULL | | Total number of search result pages available for this `(category, lowest_category)` combination, as detected from the pagination strip on the first page. Updated each time the spider runs. |
| `products_per_page` | integer | NULL | | Number of products found on each page of results (typically 16 or 24 depending on layout). Updated after each page is scraped. |
| `last_refresh_timestamp` | timestamp | NOT NULL | | Timestamp of the most recently completed or in-progress scrape cycle for this category/LC combination. Used with `frequency_type` and `frequency_interval` to calculate `next_refresh_timestamp` in `vw_amz__pending_category_refresh`. |
| `frequency_type` | varchar(20) | NOT NULL | | Time unit for the refresh schedule. One of: `hour`, `day`, `week`, `month`. Combined with `frequency_interval` to determine how often this category/LC should be re-scraped. |
| `frequency_interval` | integer | NOT NULL | | Number of `frequency_type` units between refreshes. For example, `frequency_interval = 1` + `frequency_type = 'week'` = refresh every 7 days. The `vw_amz__pending_category_refresh` view uses this to compute whether a category is overdue. |

---

---

## curated schema

---

### curated.amz__product_consolidated (and category partitions)

Wide denormalised product snapshot table. Rebuilt per category by `sp_amz__product_consolidate`. Partitioned by `category` — analysts typically query a specific partition or filter by category.

| Column | Type | Nullable | PK | Description |
|---|---|---|---|---|
| `id` | bigint | NOT NULL | ✓ | Auto-incremented surrogate key. Part of composite PK with `category` due to list partitioning. |
| `category` | text | NOT NULL | ✓ | Top-level category code (e.g. `electronics`, `kitchen`). Also the list partition key. Defaults to `'NA'` if the ASIN has no category assignment in `trf_amz__product_category_master`. |
| `lowest_category` | text | NULL | | Leaf sub-category identifier (Amazon node ID). The most specific category this product belongs to within the top-level category. |
| `asin` | text | NULL | | Amazon product identifier. Use this to join back to the transformed layer for additional history. |
| `brand_name` | text | NULL | | Brand name from `trf_amz__product_master`. NULL for generic or unbranded products. |
| `seller_name` | text | NULL | | Seller display name. Resolved by joining `trf_amz__seller` on brand name (case-insensitive, excluding "generic"). Falls back to `seller_id` when the seller name is NULL in the seller table. NULL when no seller record exists for the brand. |
| `launch_date` | date | NULL | | Product "Date First Available". Sourced from `trf_amz__product_master.launch_date`. NULL when not available on the product listing. |
| `last_month_sale` | integer | NULL | | Estimated units sold in the past 30 days. Sourced from `trf_amz__product_details`. `0` or NULL for products without the sales badge. |
| `rating` | numeric(3,2) | NULL | | Average star rating (1.00–5.00). Sourced from `trf_amz__product_details`. NULL for products with no ratings. |
| `reviews_count` | integer | NULL | | Total customer ratings count. Sourced from `trf_amz__product_details`. NULL for unrated products. |
| `sell_mrp` | numeric(16,2) | NULL | | List price (M.R.P.) in INR. NULL when not listed. |
| `sell_price` | numeric(16,2) | NULL | | Actual selling price in INR. NULL when price cannot be determined from the listing. |
| `is_fba` | boolean | NULL | | `TRUE` if fulfilled by Amazon. |
| `is_variant_available` | boolean | NULL | | `TRUE` if product has multiple variants. |
| `is_oos` | boolean | NULL | | `TRUE` if product is currently out of stock. |
| `pd_is_latest` | boolean | NULL | | Mirrors `trf_amz__product_details.is_latest`. `TRUE` for the most recent metrics row. Since the procedure only inserts from `is_latest = TRUE` rows, this should always be `TRUE` in this table unless the procedure is modified. Useful as a quick filter to avoid accidental inclusion of stale snapshots. |
| `pd_scrape_date` | timestamp | NULL | | The `scrape_date` from `trf_amz__product_details` — when the product's current metrics were captured. Use this for time-series analysis and for understanding how fresh the data is. |
| `product_name` | text | NULL | | Full product title from `trf_amz__product_master`. |
| `product_url` | text | NULL | | Canonical product page URL from `trf_amz__product_master`. |
| `brand_store_url` | text | NULL | | Brand storefront URL from `trf_amz__seller`. NULL when no seller record or no storefront is registered. |
| `inserted_at` | timestamp | NULL | | Timestamp when this row was inserted into the consolidated table (defaults to `now()`). Represents the time the consolidation procedure ran, not when the product was scraped. |
| `weekly_schedule_scrape_date` | date | NULL | | Normalised scrape date aligned to a category-specific day within the ISO week containing `pd_scrape_date`. Used for consistent week-over-week trending — different categories are scraped on different days of the week, so this field offsets each scrape date to a comparable weekly anchor. Offset values: `sports` → Sunday (0d), `kitchen`/`hpc` → Monday (+1d), `industrial`/`automotive` → Tuesday (+2d), `grocery`/`garden`/`pet-supplies` → Wednesday (+3d), `home-improvement`/`beauty`/`baby`/`shoes`/`office`/`jewelry`/`luggage` → Thursday (+4d), `electronics` → Friday (+5d), all others → Sunday (+0d). |

---

### curated.amz__lc_opportunity_signals

Pre-computed scoring signals for the Category Opportunity Scoring Engine. One row per `(category, lowest_category)`. Rebuilt by `curated.sp_amz__compute_lc_opportunity_signals()`. Signals 1–6 reflect the current state of each LC (sourced from `amz__product_consolidated WHERE pd_is_latest = TRUE`). Signal 7 uses full historical weekly rows. Signal 8 uses trailing-30-day ranking data from `transformed.trf_amz__product_rankings`. LCs with fewer than 3 products are excluded.

**How to produce the opportunity ranking:** Normalise each signal to 0–100 and apply the weights below. See `stored_procs/03_opportunity_score.sql` for the ready-to-run query.

| Signal group | Columns | Weight | Direction |
|---|---|---|---|
| Market Size | `estimated_monthly_gmv_inr`, `total_monthly_units`, `avg_sell_price` | 25% | Higher = more opportunity |
| Competition Density | `pct_low_review_products`, `avg_reviews_count`, `median_reviews_count` | 20% | Higher `pct_low_review` = more opportunity |
| Entry Barrier | `top10_avg_reviews`, `top10_avg_months_old` | 15% | Lower barrier = higher score (inverted) |
| Growth Trajectory | `new_products_pct`, `sales_growth_4w_pct`, `review_growth_4w_pct` | 15% | Higher growth = more opportunity |
| Demand-Supply Gap | `oos_rate_pct`, `pct_below_4star` | 10% | Higher gap = more opportunity |
| Margin Proxy | `pct_products_above_500_inr`, `avg_discount_pct`, `price_stddev` | 10% | Avoid sub-₹200 price floor and >40% discount |
| Market Fragmentation | `top3_brand_volume_share_pct`, `pct_unbranded_products` | 5% | Lower concentration = more opportunity |
| Ranking Presence | `hnr_days_present`, `mas_days_present` | context signal only | More days = active demand growth |

| Column | Type | Nullable | Description |
|---|---|---|---|
| `category` | text | NOT NULL | Top-level category code. Part of the primary key. Matches `amz__product_consolidated.category`. |
| `lowest_category` | text | NOT NULL | Leaf sub-category identifier (Amazon numeric node ID). Part of the primary key. |
| `signal_computed_at` | timestamp | NOT NULL | Timestamp when this row was last computed — use to check signal freshness relative to `data_as_of_date`. |
| `data_as_of_date` | date | NULL | The latest `pd_scrape_date` in the source product data for this LC. If this date is > 4 weeks old, growth signals will be NULL and the signals reflect stale data. |
| `product_count` | integer | NULL | Number of distinct ASINs currently active in this LC (`pd_is_latest = TRUE`). Minimum 3 to be included. A proxy for how established the LC is. |
| `total_monthly_units` | bigint | NULL | Sum of `last_month_sale` across all current products — the total estimated units sold per month in this LC. Use as a relative volume signal, not an absolute count (Amazon's badge is directional). |
| `estimated_monthly_gmv_inr` | numeric(18,2) | NULL | `total_monthly_units × avg_sell_price` — proxy for total revenue opportunity in INR per month. The primary market-size signal. Use log-normalised for scoring due to extreme right skew. |
| `avg_sell_price` | numeric(10,2) | NULL | Average selling price in INR. Filter out LCs below ₹200 before scoring — absolute margin is too thin below this floor for FBA economics. |
| `median_sell_price` | numeric(10,2) | NULL | Median selling price. More robust than average when a few very high-priced products pull up the mean. |
| `pct_low_review_products` | numeric(5,2) | NULL | Percentage of current products with `reviews_count < 200`. India-specific threshold — in the US, the low-competition threshold is ~500 reviews. Higher = more products beatable without a large review base. This is the primary competition density signal. |
| `avg_reviews_count` | numeric(10,2) | NULL | Average reviews count across all current products. High average = established, competitive market. |
| `median_reviews_count` | numeric(10,2) | NULL | Median reviews count. Prefer this over average when a few viral products skew the distribution. |
| `p75_reviews_count` | numeric(10,2) | NULL | 75th percentile of reviews count. Measures how competitive the upper tier is — if the 75th percentile is still below 200, the LC is broadly underserved. |
| `top10_avg_reviews` | numeric(10,2) | NULL | Average reviews count of the top-10 products by `last_month_sale`. Measures how entrenched the category leaders are. Top-10 with <500 reviews each = leaders are still beatable. |
| `top10_avg_rating` | numeric(4,2) | NULL | Average star rating of the top-10 products by sales. If top-10 have ratings below 4.0, there is a quality gap even at the top — strong opportunity for a better product. |
| `top10_oldest_launch_date` | date | NULL | Launch date of the oldest product among the top-10. Very old = incumbent has had years to build reviews and loyalty. |
| `top10_newest_launch_date` | date | NULL | Launch date of the newest product among the top-10. Recent top-10 entrants = the category is still accepting new sellers at the top. |
| `top10_avg_months_old` | numeric(6,1) | NULL | Average age in months of the top-10 products from launch to `CURRENT_DATE`. High = entrenched incumbents. Low = recently launched products dominating = good entry window. |
| `avg_discount_pct` | numeric(5,2) | NULL | Average `(MRP − sell_price) / MRP × 100`. Reflects the typical discount depth in this LC. Discounts >40% compress margins significantly. Values > 100 indicate data errors (sell_price > MRP) and are excluded. |
| `avg_mrp` | numeric(10,2) | NULL | Average list price (MRP) in INR. MRP values exceeding ₹10,000,000 are treated as data entry errors and nulled out before aggregation. |
| `price_stddev` | numeric(10,2) | NULL | Standard deviation of sell_price across current products. High variance = the LC accommodates both budget and premium products — room for a differentiated positioning play. |
| `pct_products_above_500_inr` | numeric(5,2) | NULL | Percentage of products priced ≥ ₹500. Use as a margin viability filter — below this threshold, FBA fulfilment costs erode margin. |
| `pct_products_above_1000_inr` | numeric(5,2) | NULL | Percentage of products priced ≥ ₹1,000. Higher = more room for premium positioning. |
| `pct_high_discount` | numeric(5,2) | NULL | Percentage of products with discount > 40% of MRP. If >50% of the LC is deeply discounted, margin is structurally squeezed — flag as a risk. |
| `unique_brands` | integer | NULL | Count of distinct non-NULL brand names. Low count = oligopoly. High count = fragmented, easier to enter without brand recognition. |
| `top3_brand_volume_share_pct` | numeric(5,2) | NULL | Share of total `last_month_sale` held by the top-3 brands. >70% = concentrated market; <30% = fragmented and easier to enter. |
| `top1_brand_volume_share_pct` | numeric(5,2) | NULL | Share of total `last_month_sale` held by the single leading brand. >50% = near-monopoly; proceed with caution unless you can directly target the leader's gaps. |
| `pct_unbranded_products` | numeric(5,2) | NULL | Percentage of products with NULL brand name. High = large generic / unbranded segment — signal that brand-building can create differentiation. |
| `oos_rate_pct` | numeric(5,2) | NULL | Percentage of current products marked `is_oos = TRUE`. Persistent OOS = supply cannot keep up with demand — a strong entry opportunity signal. |
| `avg_rating` | numeric(4,2) | NULL | Average star rating across all current products. LC-level average below 3.8 = systemic quality gap — a new product with a better offer can capture share quickly. |
| `avg_rating_weakness_pct` | numeric(5,2) | NULL | Average of `(1 − rating/5) × 100` per product. Ranges 0–100; higher = weaker customer satisfaction. Complement to `avg_rating`. |
| `pct_below_4star` | numeric(5,2) | NULL | Percentage of products rated below 4.0 stars. >40% = most of the LC has a quality gap — strong opportunity for a well-made product. |
| `pct_fba` | numeric(5,2) | NULL | Percentage of current products fulfilled by Amazon (FBA). High FBA rate (>60%) = serious, professional sellers dominate; lower FBA = delivery experience is inconsistent, a new FBA listing has a logistics advantage. |
| `weeks_of_data` | integer | NULL | Number of distinct `weekly_schedule_scrape_date` values in `amz__product_consolidated` for this LC. <4 weeks = insufficient history for trend analysis; growth signals may be NULL. |
| `review_growth_4w_pct` | numeric(8,2) | NULL | Percentage change in average `reviews_count` between the most recent 4-week window and the prior 4-week window. Positive = reviews (and likely sales) are accelerating. NULL when the data window doesn't cover both periods — check `data_as_of_date`. |
| `sales_growth_4w_pct` | numeric(8,2) | NULL | Percentage change in average `last_month_sale` between the most recent 4-week window and the prior 4-week window. Positive = demand is growing. NULL for the same reason as `review_growth_4w_pct`. |
| `new_products_last_8w` | integer | NULL | Count of ASINs with `launch_date` within the last 8 weeks. High new-entrant count = the category is attracting new sellers, indicating a perceived opportunity or trending demand. |
| `new_products_pct` | numeric(5,2) | NULL | `new_products_last_8w` as a percentage of all ASINs ever tracked in this LC. >10% = rapid expansion of the seller base. |
| `bestsellers_days_present` | integer | NULL | Number of days in the trailing 30 days where at least one product from this LC appeared on Amazon's `bestsellers` list. Max = 30 (daily scrape). 0 = no bestseller coverage → either niche or not being scraped at this level. |
| `hnr_days_present` | integer | NULL | Same for `hot_new_releases`. Consistent presence here = Amazon itself signals active new-product demand in this LC — a strong growth indicator. |
| `mas_days_present` | integer | NULL | Same for `movers_and_shakers`. Presence = sudden rank improvement events — indicates a rapidly growing or disrupted sub-market. |
| `mwf_days_present` | integer | NULL | Same for `most_wished_for`. Presence = consumer intent without conversion — potential demand not yet captured by existing supply. |

---

### curated.amz__best_sellers

Historical best-seller rank snapshots. Append-only per load cycle.

| Column | Type | Nullable | PK | Description |
|---|---|---|---|---|
| `id` | integer | NOT NULL | ✓ | Surrogate key (auto-incremented). |
| `asin` | varchar(20) | NULL | | Amazon product identifier. |
| `category` | varchar(50) | NULL | | Top-level Amazon category code. |
| `sub_category` | varchar(100) | NULL | | Sub-category name or slug within the category page. |
| `product_name` | text | NULL | | Product title at the time of the best-seller scrape. |
| `rank` | integer | NULL | | Position on the best-sellers list (1 = top seller). Extracted from the rank badge on the category listing page by the `AmzCategory` spider (list_type = `bestsellers`). |
| `product_url` | text | NULL | | Product page URL from the category listing. |
| `load_timestamp` | timestamp | NULL | | Timestamp when this snapshot row was inserted. Represents the time the `AmzCategory` spider run completed for this category. |
| `is_latest` | boolean | NULL | | `TRUE` for rows belonging to the most recently loaded snapshot for each `(category, sub_category)` combination. Used to filter for current best-sellers without aggregating across all history. |

---

### curated.amz__product_details _(legacy)_

Legacy product detail snapshot. Predates the transformed-layer SCD2 model. Kept for backward compatibility with `vw_best_selling_lowest_categories` and related legacy queries.

| Column | Type | Nullable | Description |
|---|---|---|
| `id` | integer | NOT NULL | Surrogate key. |
| `asin` | varchar(20) | NULL | Amazon product identifier. |
| `product_name` | text | NULL | Product title. |
| `seller_name` | varchar(255) | NULL | Seller display name. |
| `last_month_sale` | integer | NULL | Estimated monthly sales units. |
| `rating` | numeric(3,2) | NULL | Average star rating. |
| `reviews_count` | integer | NULL | Total review count. |
| `sell_mrp` | numeric(10,2) | NULL | List price in INR. |
| `sell_price` | numeric(10,2) | NULL | Selling price in INR. |
| `lowest_category` | varchar(100) | NULL | Leaf sub-category name or slug at time of scrape. |
| `launch_date` | timestamp | NULL | Product first available date (stored as timestamp in this legacy table, not date). |
| `product_url` | text | NULL | Product page URL. |
| `reviews_url` | text | NULL | Direct URL to the customer reviews section for this product. |
| `seller_store_url` | text | NULL | Seller storefront URL. |
| `lowest_category_bs_url` | text | NULL | Best-sellers URL for the product's lowest category (used by `AmzLCPageCount` spider). |
| `load_timestamp` | timestamp | NULL | When this row was loaded. |
| `is_latest` | boolean | NULL | `TRUE` for the most recent snapshot for this ASIN. |
| `lowest_category_products_url` | text | NULL | Search results URL listing all products in this lowest category, used as the start URL by `AmzLCPageCount`. |
| `category` | varchar(50) | NULL | Top-level category code. |
| `sub_category` | varchar(50) | NULL | Mid-level sub-category name. |

---

### curated.amz__product_category _(reference)_

Top-level category reference table with URLs.

| Column | Type | Nullable | Description |
|---|---|---|
| `category` | varchar(50) | NULL | Category code (e.g. `kitchen`, `electronics`). Matches codes used throughout the pipeline. |
| `url` | varchar(100) | NULL | Amazon India URL for this category's main browse or best-sellers page. |
| `is_active` | boolean | NULL | Whether this category is actively scraped in the pipeline. |
| `last_refreshed_timestamp` | timestamp | NULL | Timestamp when the category URL was last validated or updated. |

---

### curated.amz__product_subcategory_old _(legacy reference)_

| Column | Type | Nullable | Description |
|---|---|---|
| `category` | varchar(50) | NULL | Parent category code. |
| `sub_category` | varchar(50) | NULL | Sub-category name or slug. |
| `url` | text | NULL | Amazon URL for this sub-category's browse or best-sellers page. |
| `is_active` | boolean | NULL | Whether this sub-category is actively used. |
| `last_refreshed_timestamp` | timestamp | NULL | Last update timestamp. |

---

## curated views — column reference

---

### curated.vw_amz__product_demand_analysis

Sourced from `curated.amz__product_consolidated`. Computes demand velocity and review momentum metrics per ASIN per lowest category per scrape week. The primary per-product analytics view.

| Column | Description |
|---|---|
| `category` | Top-level category code. |
| `lowest_category` | Leaf sub-category identifier. |
| `asin` | Amazon product identifier. |
| `launch_date` | Product's first available date. |
| `last_month_sale` | Estimated units sold in the past 30 days at this snapshot. |
| `rating` | Average star rating at this snapshot. |
| `reviews_count` | Total review count at this snapshot. |
| `sell_mrp` | List price in INR at this snapshot. |
| `sell_price` | Selling price in INR at this snapshot. |
| `pd_scrape_date` | Timestamp of the product detail scrape for this snapshot. |
| `tracking_asin_since` | Earliest `pd_scrape_date` ever recorded for this ASIN in this category/LC. Represents the date the pipeline first started monitoring this product. |
| `weekly_schedule_scrape_date` | Normalised week-anchor date (see `amz__product_consolidated` description). |
| `max_rating` | Highest rating this ASIN has ever achieved across all historical snapshots in this category/LC. |
| `min_reviews_count` | Lowest reviews count ever recorded — approximates the review count when tracking started. |
| `max_reviews_count` | Highest reviews count ever recorded — used as denominator for normalised review momentum. |
| `max_last_month_sale` | Highest monthly sales ever recorded for this ASIN — used as denominator for the sales index. |
| `weeks_since_tracking` | Number of complete weeks between `tracking_asin_since` and the current `pd_scrape_date`. Used for velocity calculations. |
| `months_since_tracking` | Number of calendar months between `tracking_asin_since` and the current `pd_scrape_date`. |
| `review_density_weekly` | Average number of new reviews gained per week since tracking began: `(reviews_count - min_reviews_count) / weeks_since_tracking`. `0` on the first observed snapshot. |
| `review_density_monthly` | Average number of new reviews gained per month since tracking began: `(reviews_count - min_reviews_count) / months_since_tracking`. `0` on the first observed snapshot. |
| `review_momentum_30_days_range` | Normalised review acceleration over the trailing 30-day window: `(reviews_count - reviews_count_30_days_ago) / max_reviews_count`. Measures how rapidly reviews are accumulating recently vs the product's peak review count. Range: 0–1. |
| `rating_weekness` | Inverse rating signal: `1 - (rating / 5)`. Higher values indicate weaker customer satisfaction. Used to flag quality gaps in a sub-category. |
| `recent_sales_index` | Current `last_month_sale` normalised by the product's peak sales: `last_month_sale / max_last_month_sale`. 1.0 = currently at peak. Used to identify products trending down after a peak. |

---

### curated.vw_amz__lc_demand_analysis

Aggregation of `vw_amz__product_demand_analysis` to the lowest-category level per week. Used for market-level opportunity scoring.

| Column | Description |
|---|---|
| `category` | Top-level category code. |
| `lowest_category` | Leaf sub-category identifier. |
| `weekly_schedule_scrape_date` | Normalised week anchor date. |
| `product_count` | Number of distinct ASINs tracked in this LC during this week. |
| `total_volume` | Sum of `last_month_sale` across all products in this LC this week. Proxy for total market demand. |
| `avg_rating` | Average star rating across all products in this LC this week. |
| `total_reviews` | Sum of `reviews_count` across all products. |
| `avg_sell_price` | Average selling price across all products in this LC this week. |
| `review_density_weekly` | Sum of per-product weekly review density. Higher value = faster review accumulation across the LC. |
| `review_density_monthly` | Sum of per-product monthly review density. |
| `rating_weakness` | `1 - (avg_rating / 5)` — market-level rating weakness. |
| `avg_rating_weekness` | Average of per-product `rating_weekness` values. |
| `max_total_volume` | The highest `total_volume` ever seen for this LC (within the current category/week partition). Used for indexing. |
| `max_avg_rating` | The highest `avg_rating` ever seen for this LC. |
| `max_total_reviews` | The highest `total_reviews` ever seen for this LC. |
| `review_momentum_30_days_range` | Normalised 30-day review growth at the LC level: `(total_reviews - total_reviews_30_days_ago) / max_total_reviews`. |
| `recent_sales_index` | `total_volume / max_total_volume` — how current demand compares to the LC's peak demand. |

---

### curated.vw_amz_kpi__product_daily_performance

Flattened, analyst-ready view of the current state of each product. Joins master + details + category + name resolution. Used as the primary BI/dashboard data source.

| Column | Description |
|---|---|
| `asin` | Amazon product identifier. |
| `product_name` | Current product title. |
| `brand_name` | Current brand name. |
| `launch_date` | Date first available. |
| `product_url` | Product page URL. |
| `last_month_sale` | Current estimated monthly sales. |
| `rating` | Current average star rating. |
| `reviews_count` | Current total review count. |
| `sell_mrp` | Current list price in INR. |
| `sell_price` | Current selling price in INR. Imputed when NULL: uses category-average discount applied to MRP if MRP is present, otherwise falls back to the modal price bucket for the category/LC. |
| `discount` | Discount percentage: `(sell_mrp - sell_price) / sell_mrp * 100`. NULL when either price is missing. |
| `is_fba` | TRUE if fulfilled by Amazon. |
| `is_oos` | TRUE if currently out of stock. |
| `is_variant_available` | TRUE if variants are available. |
| `category` | Human-readable category name (resolved via `amz__category`; falls back to raw code). |
| `lowest_category` | Human-readable lowest sub-category name (resolved via `vw_amz__category_hierarchy_flattened_names`; falls back to raw node ID). |

---

### curated.vw_amz__brandwise_volume

| Column | Description |
|---|---|
| `category` | Category code. |
| `lowest_category` | Leaf sub-category identifier. |
| `seller_name` | Seller display name. |
| `brand_name` | Brand name. |
| `weekly_schedule_scrape_date` | Normalised week anchor date. |
| `total_volume` | Sum of `last_month_sale` across all ASINs for this brand/seller/LC/week combination. Represents estimated total monthly units by this brand in this sub-category for the given week. |

---

### curated.vw_amz__product_count_brandwise

| Column | Description |
|---|---|
| `category` | Category code. |
| `lowest_category` | Leaf sub-category identifier. |
| `seller_name` | Seller display name. |
| `brand_name` | Brand name. |
| `product_count` | Count of distinct ASINs this brand currently has listed (`pd_is_latest = TRUE`) in this sub-category. Represents portfolio breadth. |

---

### curated.vw_best_selling_lowest_categories

Identifies the most consistently best-selling lowest-level categories and their top-performing products.

| Column | Description |
|---|---|
| `category` | Top-level category code. |
| `sub_category` | Sub-category slug. |
| `lowest_category` | Lowest (leaf) category name. |
| `avg_rating` | Average star rating of products currently in the best-sellers list for this LC. |
| `category_count` | Count of best-seller products in this LC. Indicates the LC's breadth of representation on the best-sellers list. |
| `total_sales_qty` | Sum of `last_month_sale` for products in this LC on the current best-sellers snapshot. Proxy for market size. |
| `lowest_category_products_url` | Amazon search URL listing all products in this lowest category. Used as a spider entry point by `AmzLCPageCount`. |
| `category_url` | Amazon URL for the parent category. |
| `sub_category_url` | Amazon URL for the sub-category. |
| `d_rnk` | Dense rank of this LC within its `(category, sub_category)` group, ordered by `category_count` descending. Rank 1 = most represented LC on the best-sellers list. |

---

### transformed.vw_amz__pending_category_refresh

Work queue view for the `AmzProductsLC` spider. Returns categories that are overdue for scraping or have incomplete scrape jobs.

| Column | Description |
|---|---|
| `category` | Top-level category code. |
| `lowest_category` | Leaf sub-category identifier. |
| `refreshed_pages_upto` | Pages scraped so far in the most recent run. `0` = not yet started. |
| `total_pages` | Total pages available. |
| `products_per_page` | Products per page. |
| `last_refresh_timestamp` | When the most recent scrape was run. |
| `frequency_type` | Refresh frequency unit (`hour`, `day`, `week`, `month`). |
| `frequency_interval` | Interval count. |
| `next_refresh_timestamp` | Computed next due timestamp: `last_refresh_timestamp + (frequency_interval * frequency_type)`. |
| `scraping_mandatory` | `TRUE` if `next_refresh_timestamp` is in the past — i.e., a full fresh scrape is required. `FALSE` if a partial resume is needed (`refreshed_pages_upto < total_pages`). The spider uses this to decide whether to start from page 1 or resume from `refreshed_pages_upto`. |
