# Database Table Structure — ecommerce (PostgreSQL)

> **Database:** `ecommerce`  
> **Schemas:** `staging` · `transformed` · `curated`  
> **Last updated:** 2026-04-16

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-04-16 | All | Initial documentation of all tables across staging, transformed, and curated schemas |

## Table of Contents

- [Schema Overview](#schema-overview)
- [staging schema](#staging-schema)
- [transformed schema](#transformed-schema)
  - [trf_amz__product_master](#transformedtrf_amz__product_master)
  - [trf_amz__product_details](#transformedtrf_amz__product_details)
  - [trf_amz__product_category_master](#transformedtrf_amz__product_category_master)
  - [trf_amz__seller](#transformedtrf_amz__seller)
  - [trf_amz__product_rankings](#transformedtrf_amz__product_rankings)
  - [amz__category](#transformedamz__category)
  - [amz__category_hierarchy](#transformedamz__category_hierarchy)
  - [amz__category_hierarchy_flattened](#transformedamz__category_hierarchy_flattened)
  - [amz__category_refresh_controller](#transformedamz__category_refresh_controller)
- [curated schema](#curated-schema)
  - [amz__product_consolidated (partitioned)](#curatedamz__product_consolidated-partitioned)
  - [amz__lc_opportunity_signals](#curatedamz__lc_opportunity_signals)
  - [amz__best_sellers](#curatedamz__best_sellers)
  - [amz__product_details (legacy)](#curatedamz__product_details-legacy)
  - [amz__product_category (reference)](#curatedamz__product_category-reference)
- [Views](#views)
  - [curated views](#curated-views)
  - [transformed views](#transformed-views)
- [Stored Procedures](#stored-procedures)

---

This document describes every table across all three schemas, including column definitions, key constraints, how each table behaves (SCD type / insert pattern), and what kind of data it holds.

---

## Schema Overview

| Schema | Purpose | Pattern |
|---|---|---|
| `staging` | Raw landing zone for scraped data; consumed by transformation procedures | Truncate-and-load or upsert-on-PK |
| `transformed` | Cleansed, normalised, historical store; source of truth for business entities | SCD Type-1 or SCD Type-2 |
| `curated` | Analyst-facing, denormalised views and consolidated snapshots; rebuilt periodically | Delete-and-rebuild (effectively SCD Type-1 snapshot) |

---

## staging schema

Staging tables are **temporary landing zones**. Data is written here by Scrapy spiders and immediately consumed by stored procedures that move it into `transformed`. These tables are kept lean — minimal typing, raw strings for numeric fields, no history.

| Table | One-liner |
|---|---|
| `staging.stg_amz__product` | Raw product detail records scraped from individual Amazon product pages; primary key on `asin`, upserted on each scrape run. |
| `staging.stg_amz__product_rankings` | Raw ranking rows (bestsellers, movers & shakers, hot new releases, most wished for) scraped from Amazon category listing pages; unique on `(list_type, category, sub_category, asin)`. |
| `staging.stg_amz__product_url_feeder` | Feed table populated by `sp_amz__refresh_product_url_feeder`; holds `(asin, product_url)` pairs that need scraping by `AmzProducts` or `AmzProductsLC`; truncated and rebuilt before each scrape cycle. |
| `staging.stg_amz__product_error_urls` | Tracks ASINs whose product page returned an error during scraping; used to retry non-404 failures and permanently exclude 404s from future scraping. |
| `staging.stg_amz__best_sellers` | Raw best-seller snapshot rows (asin + rank per category/sub-category); appended on each load with a `load_timestamp`. |
| `staging.stg_amz__product_reviews` | Raw customer review records scraped from product review pages; unique on `review_id`. |
| `staging.asin_temp_20250523` | Temporary one-off table created on 2025-05-23; holds a list of ASINs for a one-time data migration or fix. |

---

## transformed schema

Transformed tables are the **normalised, cleaned, versioned** layer. They form the single source of truth for all product, seller, category, and ranking data. SCD Type-2 tables keep full history; all other tables are upsert/SCD-1.

---

### transformed.trf_amz__product_master

**Behaviour:** SCD Type-2  
**Description:** Master product record table. Tracks changes over time to non-transactional product attributes: name, brand, launch date, and product URL. Each time any of these fields changes, the existing row is marked `is_latest = FALSE` and a new row is inserted with the new values and the scrape date. One row per `(asin, scrape_date)`.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `asin` | varchar(20) | NOT NULL | Amazon Standard Identification Number — unique product identifier |
| `product_name` | text | NOT NULL | Full product title as it appears on the Amazon listing |
| `brand_name` | varchar(200) | NULL | Brand name extracted from the product page byline section |
| `launch_date` | date | NULL | "Date First Available" from the product technical details section |
| `product_url` | text | NOT NULL | Canonical product page URL on Amazon India |
| `is_latest` | boolean | NULL | `TRUE` for the currently active record; `FALSE` for historical versions |
| `scrape_date` | timestamp | NOT NULL | Timestamp when the spider captured this version of the product |

**Primary Key:** `(asin, scrape_date)`  
**Indexes:** `asin` (btree)

---

### transformed.trf_amz__product_details

**Behaviour:** SCD Type-2  
**Description:** Transactional product metrics table. Tracks changes over time to frequently-changing product KPIs: price, rating, reviews count, stock status, and fulfilment method. Each time any tracked metric changes on a new scrape day, the existing `is_latest = TRUE` row is marked `FALSE` and a new row is inserted. One row per `(asin, scrape_date)`.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `asin` | varchar(20) | NOT NULL | Amazon product identifier — joins to `trf_amz__product_master` |
| `last_month_sale` | integer | NULL | Estimated units sold in the last 30 days (sourced from "X bought in past month" badge) |
| `rating` | numeric(3,2) | NULL | Average customer star rating (e.g. `4.30`) |
| `reviews_count` | integer | NULL | Total number of customer ratings/reviews on the listing |
| `sell_mrp` | numeric(16,2) | NULL | Maximum Retail Price / list price (M.R.P.) in INR |
| `sell_price` | numeric(16,2) | NULL | Actual selling / discounted price in INR |
| `is_fba` | boolean | NULL | `TRUE` if the item ships from and is fulfilled by Amazon |
| `is_variant_available` | boolean | NULL | `TRUE` if the product listing has one or more variant options (size, colour, etc.) |
| `is_oos` | boolean | NULL | `TRUE` if the product is currently out of stock or temporarily unavailable |
| `is_latest` | boolean | NULL | `TRUE` for the currently active metric snapshot; `FALSE` for historical |
| `scrape_date` | timestamp | NOT NULL | Timestamp when this metric snapshot was captured |

**Primary Key:** `(asin, scrape_date)`  
**Indexes:** `asin` (btree)

---

### transformed.trf_amz__product_category_master

**Behaviour:** Append-only / SCD Type-1 (upsert do-nothing on conflict)  
**Description:** Maps each ASIN to its category and lowest-level sub-category. An ASIN can appear multiple times if it belongs to multiple categories. New combinations are inserted; existing ones are left unchanged. Populated by `sp_amz__scd2_update_product_data()` during the staging-to-transformed step.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `asin` | varchar(20) | NOT NULL | Amazon product identifier |
| `category` | varchar(50) | NOT NULL | Top-level category code (e.g. `kitchen`, `electronics`, `sports`) |
| `lowest_category` | varchar(50) | NOT NULL | Leaf-node sub-category identifier (Amazon numeric node ID or slug) |
| `inserted_on` | timestamp | NULL | Row creation timestamp (defaults to `CURRENT_TIMESTAMP`) |

**Primary Key:** `(asin, category, lowest_category)`  
**Indexes:** `asin` (btree), `category` (btree)

---

### transformed.trf_amz__seller

**Behaviour:** SCD Type-1 (upsert — update `seller_name` and `brand_store_url` if new data available)  
**Description:** Seller and brand mapping table. Stores the relationship between a seller ID, seller display name, brand they sell, and the brand's Amazon storefront URL. One row per `(seller_id, brand_name)` combination. Updated via `sp_amz__scd2_update_product_data()`.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `seller_id` | varchar(100) | NOT NULL | Amazon seller account identifier (from `/seller/` URL path) |
| `seller_name` | varchar(255) | NULL | Display name of the seller as shown on the product listing |
| `brand_name` | varchar(200) | NOT NULL | Brand name associated with this seller's product |
| `brand_store_url` | text | NULL | URL to the brand's official storefront on Amazon India |
| `inserted_on` | timestamp | NULL | Row creation timestamp (defaults to `CURRENT_TIMESTAMP`) |

**Primary Key:** `(seller_id, brand_name)`  
**Indexes:** `brand_name` (btree)

---

### transformed.trf_amz__product_rankings

**Behaviour:** Append-only (partitioned parent table)  
**Description:** Partitioned table storing all historical product ranking events. Each row records that an ASIN appeared at a particular rank in a particular category/sub-category listing on a given scrape date. Partitioned by `list_type` into five child tables. Populated by `sp_amz__process_product_rankings()`.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | bigint | NOT NULL | Surrogate key (auto-incremented sequence) |
| `list_type` | varchar(50) | NOT NULL | Ranking list type: `bestsellers`, `movers_and_shakers`, `hot_new_releases`, `most_wished_for`, or catch-all default |
| `category` | varchar(100) | NOT NULL | Top-level Amazon category slug (e.g. `kitchen`, `electronics`) |
| `sub_category` | varchar(100) | NULL | Sub-category slug within the category page |
| `asin` | varchar(50) | NULL | Product ASIN observed at this rank |
| `rank` | integer | NULL | Ordinal position on the list page (1 = highest ranked) |
| `sales_rank` | varchar(100) | NULL | Raw sales rank string from the page (used for `movers_and_shakers`; contains percentage change text) |
| `product_url` | text | NULL | Product page URL observed on the category listing |
| `scrape_date` | timestamp | NULL | Timestamp when this ranking entry was captured (defaults to `CURRENT_TIMESTAMP`) |

**Primary Key:** `(id, list_type)`  
**Partitions:** `trf_amz__product_rankings_bestsellers`, `trf_amz__product_rankings_movers_and_shakers`, `trf_amz__product_rankings_hot_new_releases`, `trf_amz__product_rankings_most_wished_for`, `trf_amz__product_rankings_default`  
**Indexes:** `asin` (btree), `category` (btree) — on both parent and each partition

---

### transformed.amz__category

**Behaviour:** Reference / static lookup  
**Description:** Maps Amazon category codes (slugs used in URLs) to human-readable display names and Amazon's internal numeric category IDs. Used by views and curated procedures to resolve human-friendly labels.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `category_code` | varchar(30) | NOT NULL | URL slug used internally (e.g. `hpc`, `pet-supplies`) |
| `category_name` | varchar(50) | NOT NULL | Human-readable display name (e.g. `Health, Personal Care & Baby`, `Pet Supplies`) |
| `category_id` | varchar(20) | NULL | Amazon's numeric node ID for this top-level category |

**Primary Key:** `category_code`

---

### transformed.amz__category_hierarchy

**Behaviour:** Reference / rebuilt periodically by `CategoryRefresh` spider  
**Description:** Stores the full category tree for a given top-level Amazon category. Each row represents a leaf-node path from the root down to a data node, with up to 8 levels of hierarchy (lvl1–lvl8). Each `data_node` is the final leaf category ID. Used to resolve `lowest_category` slugs to structured hierarchy paths.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `category` | text | NOT NULL | Root category code (e.g. `kitchen`) |
| `lvl1` | text | NULL | First level sub-category Amazon node ID |
| `lvl2` | text | NULL | Second level sub-category Amazon node ID |
| `lvl3` | text | NULL | Third level sub-category Amazon node ID |
| `lvl4` | text | NULL | Fourth level sub-category Amazon node ID |
| `lvl5` | text | NULL | Fifth level sub-category Amazon node ID |
| `lvl6` | text | NULL | Sixth level sub-category Amazon node ID |
| `lvl7` | text | NULL | Seventh level sub-category Amazon node ID |
| `lvl8` | text | NULL | Eighth level sub-category Amazon node ID |
| `data_node` | text | NOT NULL | The leaf-level category node ID (most specific category in this path) |

**Primary Key / Unique:** `(category, data_node)`

---

### transformed.amz__category_hierarchy_flattened

**Behaviour:** Reference / rebuilt alongside `amz__category_hierarchy`  
**Description:** Flat lookup table mapping every Amazon category node ID to its display name. Used by views (`vw_amz__category_hierarchy_flattened_names`, `vw_amz__category_hierarchy_names`) to resolve numeric IDs to readable names. One row per unique node ID.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `category_id` | text | NOT NULL | Amazon numeric node ID or slug |
| `category_name` | text | NULL | Human-readable name of this category node |

**Primary Key:** `category_id`

---

### transformed.amz__category_refresh_controller

**Behaviour:** SCD Type-1 (update in place)  
**Description:** Controls and tracks the scraping progress for each `(category, lowest_category)` combination used by the `AmzProductsLC` spider. Records how many pages have been crawled so far (`refreshed_pages_upto`) vs the total available (`total_pages`), and the configured refresh frequency. The view `vw_amz__pending_category_refresh` queries this table to determine which categories are due for re-scraping.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `category` | varchar(50) | NOT NULL | Top-level category code (e.g. `kitchen`) |
| `lowest_category` | varchar(50) | NOT NULL | Leaf-node sub-category identifier |
| `refreshed_pages_upto` | integer | NULL | Number of pages already scraped in the most recent run |
| `total_pages` | integer | NULL | Total number of search result pages available for this category/LC combination |
| `products_per_page` | integer | NULL | Number of products found on each page (typically 16–24) |
| `last_refresh_timestamp` | timestamp | NOT NULL | Timestamp of the most recent completed scrape |
| `frequency_type` | varchar(20) | NOT NULL | Unit of the refresh interval: `hour`, `day`, `week`, or `month` |
| `frequency_interval` | integer | NOT NULL | Number of `frequency_type` units between refreshes (e.g. `1` + `week` = weekly) |

**Primary Key:** `(category, lowest_category)`

---

### transformed.trf_amz__product_rankings_old _(legacy)_

**Behaviour:** Append-only (legacy, non-partitioned)  
**Description:** Older, non-partitioned version of the product rankings table, superseded by the partitioned `trf_amz__product_rankings`. Retained for historical data continuity and queries via `curated.vw_amz__category_scrape_freq`. Same schema as the current rankings table.

---

### transformed.amz__category_hierarchy_bkp / trf_amz__product_category_master_bkp / amz__category_hierarchy_flattened_bkp _(backup tables)_

Point-in-time backup snapshots taken before structural changes. Not actively written to by any pipeline component.

---

## curated schema

Curated tables are **analyst-facing, denormalised snapshots**. They are rebuilt periodically (delete + reinsert) by stored procedures and consumed by BI tools and analytics views. They trade normalisation for query simplicity.

---

### curated.amz__product_consolidated _(partitioned)_

**Behaviour:** Delete-and-rebuild per category (effectively SCD Type-1 snapshot)  
**Description:** The primary analyst table. A wide, denormalised product snapshot that joins product master, product details, category assignment, and seller info into one row per product-scrape-date. Partitioned by `category` (list partitioning) with one partition per Amazon category (e.g. `amz__product_consolidated_electronics`, `amz__product_consolidated_kitchen`). Rebuilt by calling `curated.sp_amz__product_consolidate(category)` which deletes all rows for the category then re-inserts from the transformed layer. The `weekly_schedule_scrape_date` column normalises each scrape date to the start of its scrape week per a category-specific day-offset schedule.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | bigint | NOT NULL | Surrogate key (auto-incremented sequence) |
| `category` | text | NOT NULL | Top-level category code — also the partition key |
| `lowest_category` | text | NULL | Leaf-node sub-category identifier |
| `asin` | text | NULL | Amazon Standard Identification Number |
| `brand_name` | text | NULL | Brand name |
| `seller_name` | text | NULL | Seller display name (falls back to `seller_id` if name is NULL) |
| `launch_date` | date | NULL | Product "Date First Available" |
| `last_month_sale` | integer | NULL | Estimated units sold in the past 30 days |
| `rating` | numeric(3,2) | NULL | Average star rating |
| `reviews_count` | integer | NULL | Total number of customer reviews |
| `sell_mrp` | numeric(16,2) | NULL | List price (M.R.P.) in INR |
| `sell_price` | numeric(16,2) | NULL | Actual selling price in INR |
| `is_fba` | boolean | NULL | TRUE if fulfilled by Amazon |
| `is_variant_available` | boolean | NULL | TRUE if product has variants |
| `is_oos` | boolean | NULL | TRUE if product is out of stock |
| `pd_is_latest` | boolean | NULL | Mirrors `is_latest` from `trf_amz__product_details`; TRUE for the most recent scrape snapshot |
| `pd_scrape_date` | timestamp | NULL | Timestamp from the source product-detail scrape |
| `product_name` | text | NULL | Full product title |
| `product_url` | text | NULL | Amazon product page URL |
| `brand_store_url` | text | NULL | Brand storefront URL on Amazon India |
| `inserted_at` | timestamp | NULL | Row insertion timestamp into this table (defaults to `now()`) |
| `weekly_schedule_scrape_date` | date | NULL | Normalised week-start date for the scrape, offset per category's scraping day in the week (used for week-over-week trending analysis) |

**Primary Key:** `(id, category)`  
**Partitions:** One per category (31 total, e.g. `amz__product_consolidated_electronics`)  
**Indexes:** `asin WHERE pd_is_latest` (partial btree), `brand_name` (btree), `seller_name` (btree), `lowest_category` (btree), `pd_scrape_date` (BRIN)

---

### curated.amz__lc_opportunity_signals

**Behaviour:** Delete-and-rebuild per category (or full TRUNCATE when called without a category filter)  
**Description:** Pre-computed, materialised scoring signals for the **Category Opportunity Scoring Engine**. One row per `(category, lowest_category)` — rebuilt by calling `curated.sp_amz__compute_lc_opportunity_signals(category)`. Contains all 8 raw signal groups (market size, competition density, entry barrier, margin proxy, market fragmentation, demand-supply gap, growth trajectory, and ranking list presence) needed to produce a ranked opportunity score. Analysts normalise and weight these signals in a query or notebook to generate the final ranked output. Sourced from `curated.amz__product_consolidated` (Signals 1–7) and `transformed.trf_amz__product_rankings` (Signal 8). Signals 1–6 use only `pd_is_latest = TRUE` rows (current state); Signal 7 uses all historical rows; Signal 8 looks at trailing 30 days of ranking appearances.

| Column | Type | Description |
|---|---|---|
| `category` | text PK | Top-level category code — also the partition filter used at refresh time |
| `lowest_category` | text PK | Leaf sub-category identifier (Amazon numeric node ID) |
| `signal_computed_at` | timestamp | When this row was last computed (set to `NOW()` on each rebuild) |
| `data_as_of_date` | date | Latest `pd_scrape_date` in the source data — indicates freshness of the underlying product data |
| `product_count` | integer | Distinct ASINs currently active in this LC (`pd_is_latest = TRUE`) |
| `total_monthly_units` | bigint | `SUM(last_month_sale)` across all current products — proxy for total unit demand |
| `estimated_monthly_gmv_inr` | numeric(18,2) | Estimated monthly revenue: `total_monthly_units × avg_sell_price` in INR |
| `avg_sell_price` | numeric(10,2) | Average selling price in INR across all current products |
| `median_sell_price` | numeric(10,2) | Median selling price — less sensitive to extreme outliers than the average |
| `pct_low_review_products` | numeric(5,2) | % of current products with fewer than 200 reviews — India threshold for low competition |
| `avg_reviews_count` | numeric(10,2) | Average reviews count across all current products |
| `median_reviews_count` | numeric(10,2) | Median reviews count |
| `p75_reviews_count` | numeric(10,2) | 75th percentile reviews count — indicates how skewed the top-end is |
| `top10_avg_reviews` | numeric(10,2) | Average reviews count of the top-10 products by `last_month_sale` — how entrenched are category leaders |
| `top10_avg_rating` | numeric(4,2) | Average star rating of the top-10 by sales |
| `top10_oldest_launch_date` | date | Oldest launch date among the top-10 — older = more entrenched |
| `top10_newest_launch_date` | date | Newest launch date among the top-10 |
| `top10_avg_months_old` | numeric(6,1) | Average age in months of top-10 products (from `launch_date` to `CURRENT_DATE`) |
| `avg_discount_pct` | numeric(5,2) | Average `(MRP − price) / MRP × 100` — margin pressure indicator |
| `avg_mrp` | numeric(10,2) | Average list price (MRP) in INR. MRP values > ₹10,000,000 are treated as data errors and excluded |
| `price_stddev` | numeric(10,2) | Standard deviation of sell_price — high variance = room for premium positioning |
| `pct_products_above_500_inr` | numeric(5,2) | % of products priced ≥ ₹500 — price floor viability signal |
| `pct_products_above_1000_inr` | numeric(5,2) | % of products priced ≥ ₹1,000 |
| `pct_high_discount` | numeric(5,2) | % of products with discount > 40% of MRP — margin squeeze warning |
| `unique_brands` | integer | Count of distinct brand names among current products |
| `top3_brand_volume_share_pct` | numeric(5,2) | Share of total `last_month_sale` held by the top-3 brands — concentration signal |
| `top1_brand_volume_share_pct` | numeric(5,2) | Share of total `last_month_sale` held by the single top brand — monopoly signal |
| `pct_unbranded_products` | numeric(5,2) | % of products with NULL brand name — indicates generic / private label opportunity |
| `oos_rate_pct` | numeric(5,2) | % of current products that are out of stock — demand-supply gap signal |
| `avg_rating` | numeric(4,2) | Average star rating across all current products |
| `avg_rating_weakness_pct` | numeric(5,2) | Average of `(1 − rating/5) × 100` — higher = weaker customer satisfaction across LC |
| `pct_below_4star` | numeric(5,2) | % of products rated below 4.0 stars — quality gap opportunity |
| `pct_fba` | numeric(5,2) | % of products fulfilled by Amazon — high FBA = competitive / standardised market |
| `weeks_of_data` | integer | Distinct weeks of historical data available for this LC in `amz__product_consolidated` |
| `review_growth_4w_pct` | numeric(8,2) | % change in avg `reviews_count` between the most recent 4-week window and the prior 4-week window. NULL if data doesn't cover both windows |
| `sales_growth_4w_pct` | numeric(8,2) | % change in avg `last_month_sale` between the most recent 4-week window and the prior 4-week window. NULL if data doesn't cover both windows |
| `new_products_last_8w` | integer | Count of ASINs with `launch_date` in the last 8 weeks — new entrant signal |
| `new_products_pct` | numeric(5,2) | `new_products_last_8w` as a % of all ASINs ever seen in this LC |
| `bestsellers_days_present` | integer | Days in the trailing 30 days where ≥1 product from this LC appeared on the `bestsellers` list. Max = 30 |
| `hnr_days_present` | integer | Same for `hot_new_releases` list |
| `mas_days_present` | integer | Same for `movers_and_shakers` list |
| `mwf_days_present` | integer | Same for `most_wished_for` list |

**Primary Key:** `(category, lowest_category)`  
**Minimum row requirement:** Only LCs with `product_count >= 3` are inserted (enforced in the procedure).

---

### curated.amz__product_details _(legacy)_

**Behaviour:** SCD Type-2 (legacy)  
**Description:** Older product details table that predates the transformed-layer SCD2 approach. Tracks product detail snapshots with an `is_latest` flag similar to `trf_amz__product_details` but with slightly different structure. Queried by `vw_best_selling_lowest_categories`. Being superseded by `curated.amz__product_consolidated`.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | integer | NOT NULL | Surrogate key |
| `asin` | varchar(20) | NULL | Amazon product identifier |
| `product_name` | text | NULL | Product title |
| `seller_name` | varchar(255) | NULL | Seller display name |
| `last_month_sale` | integer | NULL | Estimated monthly sales units |
| `rating` | numeric(3,2) | NULL | Average star rating |
| `reviews_count` | integer | NULL | Total review count |
| `sell_mrp` | numeric(10,2) | NULL | List price in INR |
| `sell_price` | numeric(10,2) | NULL | Selling price in INR |
| `lowest_category` | varchar(100) | NULL | Leaf sub-category name or slug |
| `launch_date` | timestamp | NULL | Product first available date |
| `product_url` | text | NULL | Product page URL |
| `reviews_url` | text | NULL | Direct URL to the reviews section |
| `seller_store_url` | text | NULL | Seller storefront URL |
| `lowest_category_bs_url` | text | NULL | Best-sellers URL for the lowest category |
| `load_timestamp` | timestamp | NULL | When this row was loaded |
| `is_latest` | boolean | NULL | TRUE for the most recent snapshot |
| `lowest_category_products_url` | text | NULL | Search/listing URL for products in this lowest category |
| `category` | varchar(50) | NULL | Top-level category |
| `sub_category` | varchar(50) | NULL | Mid-level sub-category |

---

### curated.amz__best_sellers

**Behaviour:** Append-only (new snapshot each load)  
**Description:** Historical best-seller rank snapshots. Every time the best-sellers spider runs, it appends rows for all ranked ASINs under each category/sub-category with a `load_timestamp`. An `is_latest` flag marks the most recently loaded set. Used to track rank movement over time.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | integer | NOT NULL | Surrogate key |
| `asin` | varchar(20) | NULL | Amazon product identifier |
| `category` | varchar(50) | NULL | Top-level category |
| `sub_category` | varchar(100) | NULL | Sub-category within the category page |
| `product_name` | text | NULL | Product title at time of scrape |
| `rank` | integer | NULL | Position on the best-sellers list |
| `product_url` | text | NULL | Product page URL |
| `load_timestamp` | timestamp | NULL | When this snapshot was loaded |
| `is_latest` | boolean | NULL | TRUE for the most recently loaded snapshot |

---

### curated.amz__product_category _(reference)_

**Behaviour:** Reference / static  
**Description:** Top-level category reference table mapping category codes to their Amazon browse node URLs and active status. Used in views to enrich category data with hyperlinks.

| Column | Type | Nullable | Description |
|---|---|---|---|
| `category` | varchar(50) | NULL | Category code slug |
| `url` | varchar(100) | NULL | Amazon URL for this category's landing page |
| `is_active` | boolean | NULL | Whether this category is actively scraped |
| `last_refreshed_timestamp` | timestamp | NULL | When this category's URL was last validated |

---

### curated.amz__product_subcategory_old _(legacy reference)_

**Behaviour:** Legacy reference  
**Description:** Old sub-category reference table mapping category + sub-category pairs to their Amazon URLs. Superseded by `transformed.amz__category_hierarchy`. Retained for use in `vw_best_selling_lowest_categories`.

---

## Views

### curated views

| View | Description |
|---|---|
| `curated.vw_amz__product_demand_analysis` | Per-ASIN, per-lowest-category, per-week demand metrics: sales volume, ratings, review density (weekly & monthly rate of review accumulation), review momentum (30-day rolling), and a recent sales index relative to the ASIN's peak. The primary demand-analysis view. |
| `curated.vw_amz__lc_demand_analysis` | Aggregated version of `vw_amz__product_demand_analysis` at the `(category, lowest_category, weekly_schedule_scrape_date)` level; adds category-level max values and a review momentum index. Used for market-level opportunity scoring. |
| `curated.vw_amz__brandwise_volume` | Aggregates total `last_month_sale` units by `(category, lowest_category, seller_name, brand_name, weekly_schedule_scrape_date)`. Used for brand share analysis. |
| `curated.vw_amz__product_count_brandwise` | Counts distinct active ASINs by `(category, lowest_category, seller_name, brand_name)` where `pd_is_latest = TRUE`. Used for brand portfolio width analysis. |
| `curated.vw_amz_kpi__product_daily_performance` | Joins `trf_amz__product_master`, `trf_amz__product_details` (latest only), and `trf_amz__product_category_master`; imputes missing `sell_price` using category average discount or modal price bucket; resolves human-readable category and sub-category names. The primary product-level KPI view for dashboards. |
| `curated.vw_best_selling_lowest_categories` | Identifies the most consistently best-selling lowest-level categories — those that appear in the best-sellers list across all scrape days. Ranks sub-categories by product count and total sales within each category/sub-category grouping. |
| `curated.vw_amz__category_scrape_freq` | (Legacy) Uses `trf_amz__product_rankings_old` to analyse scrape coverage frequency per category/sub-category/lowest-category combination. |
| `curated.vw_amz__list_type_lc_scarcity` | Measures how often each lowest-category appears across scrape days for each list type — used to identify consistently vs rarely appearing sub-categories (scarcity signal). |

### transformed views

| View | Description |
|---|---|
| `transformed.vw_amz__pending_category_refresh` | Queries `amz__category_refresh_controller` to return all `(category, lowest_category)` combinations that are either overdue for a full refresh (next_refresh_timestamp in the past) or have partially completed scrapes (`refreshed_pages_upto < total_pages`). Used by the `AmzProductsLC` spider as its work queue. |
| `transformed.vw_amz__category_scrape_freq` | Analyses scrape coverage and lowest-category presence percentage across all scraped days using `trf_amz__product_rankings`. Used to populate/validate `amz__category_refresh_controller`. |
| `transformed.vw_amz__category_hierarchy_flattened_names` | Resolves all node IDs in `amz__category_hierarchy` to their human-readable names using `amz__category_hierarchy_flattened`. Returns `(category, category_name, sub_category, sub_category_name)` pairs. |
| `transformed.vw_amz__category_hierarchy_names` | Fully name-resolved version of `amz__category_hierarchy` — replaces all numeric node IDs with text names across all 8 hierarchy levels. |

---

## Stored Procedures

| Procedure | Description |
|---|---|
| `staging.sp_amz__refresh_product_url_feeder()` | Truncates and repopulates `stg_amz__product_url_feeder` with all ASINs that need scraping: ASINs in rankings but not yet in product master, non-404 error URLs for retry, and products missing seller info. All-category version. |
| `staging.sp_amz__refresh_product_url_feeder(category)` | Same as above but scoped to a single category; also includes ASINs from that category's product master that are missing a seller mapping. |
| `transformed.sp_amz__scd2_update_product_data()` | Core transformation procedure. Reads `stg_amz__product` and applies: (1) upserts new seller records, (2) inserts new `(asin, category, lowest_category)` combinations, (3) SCD2 update on `trf_amz__product_master`, (4) SCD2 update on `trf_amz__product_details`. |
| `transformed.sp_amz__process_product_rankings(list_type)` | Moves ranking rows for a given `list_type` from `stg_amz__product_rankings` into `trf_amz__product_rankings`, then deletes them from staging. |
| `transformed.sp_amz__category_refresh_controller(category, lowest_category, refreshed_pages_upto, total_pages)` | Updates the `last_refresh_timestamp` and page-progress counters for a `(category, lowest_category)` row in the refresh controller table. |
| `curated.sp_amz__product_consolidate(category)` | Rebuilds the consolidated snapshot for one category: deletes all existing rows for that category from `amz__product_consolidated`, then re-inserts by joining transformed master, details, category, and seller tables. Computes `weekly_schedule_scrape_date` with a category-specific day offset. Fixes two bugs in the original: (1) `DISTINCT ON (brand_name)` in a CTE prevents seller fan-out where one brand has many resellers; (2) `DISTINCT ON (asin, week)` in a CTE collapses SCD2 detail history to one row per ASIN per ISO-week. |
| `curated.sp_amz__product_consolidate_all()` | Iterates over all distinct categories in `trf_amz__product_category_master` and calls `sp_amz__product_consolidate(category)` for each; continues on individual category failures. |
| `curated.sp_amz__compute_lc_opportunity_signals(category DEFAULT NULL)` | Computes and materialises all 8 scoring signals into `curated.amz__lc_opportunity_signals`. When called with a category argument, deletes and rebuilds rows for that category only. When called with no argument (NULL), truncates the full table and rebuilds all categories. Must be run after `sp_amz__product_consolidate` to ensure source data is current. Script: `stored_procs/02_sp_amz__compute_lc_opportunity_signals.sql`. |
