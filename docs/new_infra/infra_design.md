# Infrastructure Design Document — Amazon E-Commerce Data Platform

**Version**: 1.2  
**Date**: 2026-04-22  
**Status**: Draft  
**Audience**: Engineers, DevOps, Data Engineers

---

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-04-22 | All | v1.2 — initial design finalized |
| 2026-05-11 | §5 Repo Structure, §6 CI/CD, §9 Data Flow, §12 Decisions Log | Renamed `ecom-pipelines` → `ecom-intelligence` throughout |
| 2026-05-11 | §0 Phase 0 (new section) | Added fast execution strategy as mandatory first step before any infra build |
| 2026-05-11 | §1, §2.1, §3.7, §5, §9, §12 | Full revamp — added Phase 0 to vendor onboarding table, rewrote repo structure with Phase 0 directories, updated data flow diagram and decisions log |
| 2026-06-03 | §0.4, §12 | DB changed from `ecommerce` to `ecom_intel`; multi-market schema decision added (Option A — marketplace as column); `marketplaces` lookup table defined |
| 2026-06-04 | §0.1–0.5, §5, §9, §12 | Phase 0 full design update: category hierarchy spider added as prerequisite; all table DDL finalized; schema layout defined (staging/transformed/monitoring/curated); HTML archiving + validation pipeline added; spider build order documented; data flow diagram updated; India spider analysis recorded |
| 2026-06-04 | §5, §12 | Orchestration repo renamed `ecom-orchestration` → `orchestration`; scope changed to generic/multi-project with per-project folders + shared utilities; DAGs-last rule added |
| 2026-06-04 | §0.4, §12 | Ranking data split into two layers: staging (raw append) + transformed (deduplicated via MERGE); dedup key defined as (marketplace_id, list_type, subcategory_node_id, asin, scrape_date); controller reset strategy (time-based, configurable N days) documented |
| 2026-06-08 | §0.2, §12 | AmzRankings scrape scope corrected: all nodes scraped (root + intermediate + leaf), not leaf-only; controller seeded with all nodes; AmzProducts filters to leaf at query time via JOIN |

## Table of Contents

- [0. Phase 0 — Fast Execution Strategy (Execute First)](#0-phase-0--fast-execution-strategy-execute-first)
  - [0.1 Goal](#01-goal)
  - [0.2 What to Scrape](#02-what-to-scrape)
  - [0.3 Scraper Setup](#03-scraper-setup)
  - [0.4 Storage](#04-storage)
  - [0.5 Scoring Rubric (SQL)](#05-scoring-rubric-sql)
  - [0.6 Output and Next Action](#06-output-and-next-action)
  - [0.7 Transition Trigger to Phase 1](#07-transition-trigger-to-phase-1)
- [1. Overview and Goals](#1-overview-and-goals)
  - [Current Scale Context](#current-scale-context)
  - [Non-goals (explicit)](#non-goals-explicit)
- [2. Data Sources and Truth Classes](#2-data-sources-and-truth-classes)
  - [2.1 Vendor Onboarding Phases](#21-vendor-onboarding-phases)
  - [2.2 Amazon Browse Node Hierarchy — First Pipeline](#22-amazon-browse-node-hierarchy--first-pipeline)
- [3. Data Layers (Iceberg Lakehouse)](#3-data-layers-iceberg-lakehouse)
  - [3.1 Landing (forensic archive)](#31-landing-forensic-archive)
  - [3.2 Raw Iceberg](#32-raw-iceberg)
  - [3.3 Clean](#33-clean)
  - [3.4 Transformed](#34-transformed)
  - [3.5 Curated](#35-curated)
  - [3.6 Multi-Platform Architecture](#36-multi-platform-architecture)
  - [3.7 Category Opportunity Scoring Engine — First Deliverable](#37-category-opportunity-scoring-engine--first-deliverable)
- [4. Infrastructure Components](#4-infrastructure-components)
  - [4.0 Cost Comparison — Why Not AWS](#40-cost-comparison--why-not-aws)
  - [4.1 Object Storage](#41-object-storage)
  - [4.2 Iceberg Catalog](#42-iceberg-catalog)
  - [4.3 Query Engine](#43-query-engine)
  - [4.4 Orchestration](#44-orchestration)
  - [4.5 Execution Pattern](#45-execution-pattern)
  - [4.6 Secrets Management](#46-secrets-management)
  - [4.7 VM Layout (Hetzner Cloud — cost-optimized)](#47-vm-layout-hetzner-cloud--cost-optimized)
  - [4.8 Spark Cluster (Standalone on Docker — Hetzner)](#48-spark-cluster-standalone-on-docker--hetzner)
- [5. Repository Structure](#5-repository-structure)
- [6. CI/CD Design](#6-cicd-design)
- [7. Environments](#7-environments)
- [8. Run Metadata — Required for Every Batch](#8-run-metadata--required-for-every-batch)
- [9. Data Flow Diagram](#9-data-flow-diagram)
- [10. IAM and Access Control](#10-iam-and-access-control)
- [11. Observability](#11-observability)
- [12. Decisions Log](#12-decisions-log)
- [13. What Not To Do](#13-what-not-to-do)

---

## 0. Phase 0 — Fast Execution Strategy (Execute First)

**Execute this entire phase before subscribing to Keepa, provisioning any cloud infra, or building any pipeline from this document.**

The rest of this document describes a monitoring and trend platform — it answers "which categories are growing over time." Phase 0 answers "which specific products can I launch in 90 days" using only a current snapshot. These are sequential: Phase 0 produces the product shortlist; the full platform is built to monitor and validate that shortlist and support ongoing decisions after launch.

### 0.1 Goal

Get a ranked shortlist of 10–15 product candidates across the 10 categories in `docs/chosen_categories.csv` within one weekend, using zero paid infrastructure. Source: Amazon.com bestseller and new releases pages scraped directly. Storage: local Postgres (`ecom_intel`). Analysis: SQL scoring query.

### 0.2 What to Scrape

**Spider build order — must follow this sequence:**

```
1. AmzCategoryHierarchy  →  transformed.amz_category + transformed.amz_category_hierarchy
                                        ↓
2. Seeder SQL            →  transformed.amz_category_scrape_controller  (ALL nodes — root + intermediate + leaf)
                                        ↓
3. AmzRankings           →  staging.amz_ranking_snapshot  (bestseller + new_release)
                                        ↓
4. AmzProducts           →  staging.amz_product_snapshot  (product detail pages)
```

**Step 1 — Category hierarchy (prerequisite, run once per market):**

`AmzCategoryHierarchy` spider navigates the bestseller left-nav sidebar (same `role="treeitem"` structure as India `AmzCategoryUrls`) and writes directly to DB — no intermediate JSON file. Scoped to the 10 categories in `docs/chosen_categories.csv`. Populates two tables (see §0.4).

**Step 3 — Rankings pages (10 categories, all depths):**

`AmzRankings` spider accepts `list_type` param (`bestseller` or `new_release`). Reads pending entries from `amz_category_scrape_controller`. Scrapes ranking pages at **every node level** — root, intermediate, and leaf — because Amazon exposes a distinct bestseller/new-release page for each node in the tree.

| list_type | URL pattern | Products per node |
|---|---|---|
| `bestseller` | `amazon.com/gp/bestsellers/<url_slug>/<node_id>` | up to 100 |
| `new_release` | `amazon.com/gp/new-releases/<url_slug>/<node_id>` | up to 100 |

**Controller is shared between AmzRankings and AmzProducts.** Both spiders read from `amz_category_scrape_controller`, but apply different filters at query time:
- `AmzRankings` — reads all nodes (no `is_leaf` filter); scrapes every level
- `AmzProducts` — filters to `is_leaf = TRUE` via JOIN to `amz_category`; only leaf-level product detail pages are needed

**Fields captured from ranking pages (available without product detail fetch):**

| Field | Source | Notes |
|---|---|---|
| `asin` | `data-asin` attribute | Primary key |
| `rank_position` | Rank badge | 1–100 |
| `title` | Title element | For manual review |
| `rating` | Star widget | Scoring signal |
| `review_count` | Ratings count | Scoring signal |
| `price` | Price element | Scoring signal; NULL if absent |
| `product_url` | Raw `href` from anchor | Stored as-is — ref params intact for bot evasion |
| `subcategory_node_id` | URL param `rh=n%3A{id}` | Links to `amz_category` |

**Step 4 — Product detail pages:**

`AmzProducts` spider uses the raw `product_url` from `amz_ranking_snapshot` directly — never constructs URLs. Fields captured: `bsr_rank`, `monthly_sales`, `brand`, `has_variants`, `is_small_business`, `is_fba`, `launch_date`, `seller_id`, `sell_mrp`. Schema TBD — deferred to after ranking spider is validated.

### 0.3 Scraper Setup

**India spider mapping — what we reuse vs replace:**

| India spider | What it did | US equivalent | Change |
|---|---|---|---|
| `AmzCategoryUrls` | Traverses bestseller nav, saves URLs to `.txt` file | `AmzCategoryHierarchy` | Writes to DB instead; captures node IDs and parent-child relationships |
| `CategoryRefresh` | Builds nested JSON hierarchy, saves to file; `parse_category_mapping.py` loads to DB via `lvl1`…`lvl8` columns | Replaced by `AmzCategoryHierarchy` | Direct DB write; closure table instead of fixed-depth lvl columns |
| `AmzCategory` | Scrapes ranking pages, reads URLs from `.txt` file | `AmzRankings` | Reads from DB controller; single spider with `list_type` param |
| `AmzProducts` | Scrapes product detail pages from DB queue | `AmzProducts` | Minimal changes; update selectors for .com |

**Anti-bot stack (carry over from India — proven working):**

1. **Random user agents** — `scrapy_user_agents.RandomUserAgentMiddleware`. Default UA middleware disabled.
2. **Header rotation** — `HeaderRotationMiddleware`: two groups of real browser header templates, alternates by weekday, random pick within group. Does not override User-Agent or Cookie headers.
3. **Adaptive delay** — `DelayHandler`: increases delay exponentially on empty/blocked responses, decreases on clean runs. Operates on live Scrapy downloader slot — no restart needed.
4. **Oxylabs proxy** — `RandomizedProxyMiddleware`: built, commented out by default. Enable if Amazon.com blocks direct requests. Credentials in `.secrets/`.

**Settings for `AmzRankings` (listing pages — rate-limit sensitive):**
```python
DOWNLOAD_DELAY = 8
RANDOMIZE_DOWNLOAD_DELAY = True
CONCURRENT_REQUESTS = 16
CONCURRENT_REQUESTS_PER_DOMAIN = 16
CONCURRENT_REQUESTS_PER_IP = 4
DEPTH_LIMIT = 10          # configurable — max subcategory depth to traverse
RETRY_TIMES = 1
RETRY_DELAY = 15
```

**Settings for `AmzProducts` (product pages — can be more aggressive):**
```python
DOWNLOAD_DELAY = 2
RANDOMIZE_DOWNLOAD_DELAY = True
CONCURRENT_REQUESTS = 32
CONCURRENT_REQUESTS_PER_DOMAIN = 32
CONCURRENT_REQUESTS_PER_IP = 4
RETRY_TIMES = 3
RETRY_HTTP_CODES = [429, 503, 403]
```

### 0.4 Storage

**Database:** `ecom_intel` on localhost Postgres. Separate from India `ecommerce` DB — do not mix. Admin credentials in `.secrets/admin_creds.env`.

**Schema layout:**

| Schema | Purpose |
|---|---|
| `staging` | Raw scrape output from spiders — as close to source as possible |
| `transformed` | Reference tables, cleaned data, scrape state/controllers |
| `monitoring` | Scrape run stats, null rate tracking, validation alerts |
| `curated` | (Phase 1+) Business logic, scoring, reporting-ready |

**All tables** include `marketplace_id` as a column and part of the primary key (multi-market design, Option A).

---

**Table DDL — create in this order:**

```sql
-- 1. Schemas
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS transformed;
CREATE SCHEMA IF NOT EXISTS monitoring;
CREATE SCHEMA IF NOT EXISTS curated;

-- 2. Market lookup (seed with 'amazon_us' before running any spider)
CREATE TABLE transformed.marketplaces (
    marketplace_id   VARCHAR(20) PRIMARY KEY,  -- 'amazon_us', 'amazon_in', 'amazon_uk'
    platform         VARCHAR(20),              -- 'amazon'
    country_code     CHAR(2),
    currency         CHAR(3),
    domain           VARCHAR(50),              -- 'amazon.com', 'amazon.in'
    is_active        BOOLEAN DEFAULT TRUE
);

-- 3. Category nodes — one row per Amazon browse node
CREATE TABLE transformed.amz_category (
    marketplace_id   VARCHAR(20) REFERENCES transformed.marketplaces(marketplace_id),
    node_id          VARCHAR(30),
    node_name        VARCHAR(255),
    url_slug         VARCHAR(100),   -- used in URL: amazon.com/bestsellers/<url_slug>
    parent_node_id   VARCHAR(30),    -- NULL for the 10 root categories
    depth            SMALLINT,       -- 0 = one of our 10 root categories
    is_leaf          BOOLEAN,
    is_active        BOOLEAN DEFAULT TRUE,
    first_seen_at    TIMESTAMP DEFAULT NOW(),
    last_verified_at TIMESTAMP,
    PRIMARY KEY (marketplace_id, node_id)
);

-- 4. Category hierarchy — closure table (all ancestor→descendant pairs incl. self)
--    Replaces the India lvl1…lvl8 fixed-column approach.
--    Query all leaf nodes under a root: WHERE ancestor_node_id = X AND depth_from_ancestor > 0
CREATE TABLE transformed.amz_category_hierarchy (
    marketplace_id        VARCHAR(20) REFERENCES transformed.marketplaces(marketplace_id),
    ancestor_node_id      VARCHAR(30),
    descendant_node_id    VARCHAR(30),
    depth_from_ancestor   SMALLINT,    -- 0 = self, 1 = direct child, etc.
    PRIMARY KEY (marketplace_id, ancestor_node_id, descendant_node_id)
);

-- 5. Scrape controller — one row per (marketplace, list_type, category, subcategory)
--    Seeded from amz_category leaf nodes. Spider reads this to know what/where to scrape.
CREATE TABLE transformed.amz_category_scrape_controller (
    marketplace_id        VARCHAR(20) REFERENCES transformed.marketplaces(marketplace_id),
    list_type             VARCHAR(20),            -- 'bestseller', 'new_release'
    category              VARCHAR(100),
    subcategory           VARCHAR(100),
    subcategory_node_id   VARCHAR(30),
    max_depth_configured  SMALLINT,
    depth_scraped_upto    SMALLINT DEFAULT 0,
    last_scraped_at       TIMESTAMP,
    scrape_status         VARCHAR(20) DEFAULT 'pending',  -- 'pending','in_progress','complete'
    PRIMARY KEY (marketplace_id, list_type, category, subcategory)
);

-- 6. Ranking snapshot — raw append output of AmzRankings spider
--    Short-term buffer. Duplicates possible from resume scenarios.
--    Promoted to transformed.amz_ranking via MERGE after each run.
CREATE TABLE staging.amz_ranking_snapshot (
    run_id              UUID,
    marketplace_id      VARCHAR(20) REFERENCES transformed.marketplaces(marketplace_id),
    list_type           VARCHAR(20),
    category            VARCHAR(100),
    subcategory         VARCHAR(100),
    subcategory_node_id VARCHAR(30),
    depth               SMALLINT,
    rank_position       SMALLINT,
    asin                VARCHAR(20),
    title               TEXT,
    rating              NUMERIC(3,2),
    review_count        INTEGER,
    price               NUMERIC(10,2),
    product_url         TEXT,           -- raw href from page, ref params intact
    scraped_at          TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (run_id, marketplace_id, list_type, subcategory_node_id, asin)
);
CREATE INDEX ON staging.amz_ranking_snapshot (marketplace_id, list_type, scraped_at);

-- 7. Ranking — deduplicated, permanent store (promoted from staging via MERGE)
--    Merge key: (marketplace_id, list_type, subcategory_node_id, asin, scrape_date)
--    Rationale: same ASIN can rank in multiple subcategories on the same day —
--    subcategory_node_id is required in the key to preserve those distinct appearances.
--    scrape_date preserves the time series across daily/weekly runs.
CREATE TABLE transformed.amz_ranking (
    marketplace_id      VARCHAR(20) REFERENCES transformed.marketplaces(marketplace_id),
    list_type           VARCHAR(20),
    category            VARCHAR(100),
    subcategory         VARCHAR(100),
    subcategory_node_id VARCHAR(30),
    scrape_date         DATE,
    run_id              UUID,           -- run that last wrote this row
    depth               SMALLINT,
    rank_position       SMALLINT,
    asin                VARCHAR(20),
    title               TEXT,
    rating              NUMERIC(3,2),
    review_count        INTEGER,
    price               NUMERIC(10,2),
    product_url         TEXT,
    PRIMARY KEY (marketplace_id, list_type, subcategory_node_id, asin, scrape_date)
);
CREATE INDEX ON transformed.amz_ranking (marketplace_id, list_type, scrape_date);
CREATE INDEX ON transformed.amz_ranking (asin, marketplace_id, scrape_date);

-- MERGE logic (runs as post-spider step):
-- WHEN MATCHED (same ASIN + subcategory + day, duplicate from resume) → UPDATE with latest values
-- WHEN NOT MATCHED (new day or new ASIN/subcategory combo) → INSERT

-- 8. Product snapshot — output of AmzProducts spider (schema TBD, deferred)
--    Will be designed after ranking spider is validated.

-- 8. Monitoring — null rate tracking per run per field
CREATE TABLE monitoring.scrape_run_field_stats (
    run_id          UUID,
    spider_name     VARCHAR(50),
    run_date        DATE,
    marketplace_id  VARCHAR(20),
    field_name      VARCHAR(100),
    total_records   INTEGER,
    null_count      INTEGER,
    null_rate       NUMERIC(5,4),
    PRIMARY KEY (run_id, field_name)
);
```

**HTML archiving (validation + backfill support):**

Every product page HTML is saved to disk as gzip, alongside the scrape. Stored in `html_archive/<spider_name>/<date>/<asin>.html.gz`. Pointer stored in the relevant snapshot table as `html_file_path VARCHAR(500)`. Retention: 7 days (cleanup script deletes files older than 7 days). Storage estimate: ~40 KB/page compressed; 35k pages/day ≈ 1.4 GB/day.

Purpose: if the validation pipeline flags an abnormal null rate spike (indicating Amazon changed a selector), the archived HTML lets you re-parse with the corrected XPath without waiting for the next scrape run.

### 0.5 Scoring Rubric (SQL)

```sql
-- Stage 1: hard disqualifiers
WITH filtered AS (
    SELECT *
    FROM amazon_us_product_snapshot
    WHERE scraped_at::DATE = CURRENT_DATE
      AND rating >= 3.5
      AND price >= 15.0
      AND review_count <= 2000 OR rating <= 4.5   -- not a locked winner
),

-- Stage 2: opportunity score
scored AS (
    SELECT *,
        -- Signal 1: proven demand, weak execution
        CASE WHEN bsr_rank <= 100 AND rating BETWEEN 3.5 AND 4.2 THEN 3 ELSE 0 END
        -- Signal 2: new release gaining traction early
        + CASE WHEN source = 'new_release' AND review_count < 150 AND rating >= 4.0 THEN 3 ELSE 0 END
        -- Signal 3: selling fast relative to review count (still enterable)
        + CASE WHEN monthly_sales IS NOT NULL AND review_count > 0
               AND (monthly_sales::float / review_count) > 10 THEN 2 ELSE 0 END
        -- Signal 4: price room (in bottom 30% of category price range)
        + CASE WHEN price < PERCENTILE_CONT(0.3) WITHIN GROUP (ORDER BY price)
                              OVER (PARTITION BY category) THEN 2 ELSE 0 END
        -- Signal 5: small seller, no brand moat
        + CASE WHEN is_small_business THEN 1 ELSE 0 END
        -- Signal 6: no variants = you can enter with a bundle/variant
        + CASE WHEN has_variants = FALSE THEN 1 ELSE 0 END
        AS opportunity_score,
        -- Stage 3 tiebreakers (median context)
        review_count < PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY review_count)
                        OVER (PARTITION BY category) AS below_median_reviews,
        rating < AVG(rating) OVER (PARTITION BY category) AS below_avg_rating
    FROM filtered
)

SELECT
    asin, title, category, source, bsr_rank, price, rating, review_count,
    monthly_sales, brand, has_variants, is_small_business,
    opportunity_score,
    -- apply tiebreaker bonuses
    opportunity_score
        + CASE WHEN below_median_reviews THEN 1 ELSE 0 END
        + CASE WHEN below_avg_rating AND rating >= 3.5 THEN 1 ELSE 0 END
    AS final_score
FROM scored
WHERE opportunity_score >= 4     -- pre-filter noise
ORDER BY final_score DESC
LIMIT 30;
```

**Threshold: final_score ≥ 6 → shortlist. ≥ 8 → prioritise.**

### 0.6 Output and Next Action

The query produces a ranked list of ~10–30 product candidates. From here:

1. **Manual gut check (top 10–15):** Is the complaint fixable? Can it be sourced? Does the current listing look beatable (weak images, thin copy, no A+ content)?
2. **Shortlist to 5 finalists.**
3. **Pull Keepa history on finalists only** — subscribe to Keepa entry plan (€49/mo), fetch 12-month BSR + price + review history for those specific ASINs. This validates whether the opportunity is stable or already declining. ~15 ASINs costs negligible tokens.
4. **Select 1–2 products** to take to the consultant (Rishi) for supplier discovery.

### 0.7 Transition Trigger to Phase 1

Move to Phase 1 (Keepa browse node pipeline + category scoring engine) **only when:**

- A product is selected and you need to understand the full competitive landscape of its category at depth, OR
- You want ongoing price/BSR monitoring across the 14 categories after launch, OR
- The snapshot approach misses too many opportunities because it lacks trend data

Until one of those triggers is true, Phase 0 output is sufficient for the current business decision.

---

## 1. Overview and Goals

> **Phase 0 precedes everything in this document.** Before building any pipeline described here, complete §0 (direct scraping → product shortlist). This platform is only built once a product is selected and ongoing monitoring is needed.

This platform ingests Amazon US marketplace data from multiple vendor APIs, stores it in a layered Iceberg lakehouse on object storage, and serves it via SQL (Trino) for product discovery, pricing intelligence, and trend analysis.

**Core principle**: Object storage + Iceberg is the system of record. Nothing important lives only in a transient database.

### Current scale context
- Existing Postgres DB: **~7.5 GB** (2.3M ASINs, 1 year history, India market only)
- Keepa alone tracks **1.5B ASINs** for Amazon US. At ~50 KB per ASIN per year (compressed Parquet, all history types), US full coverage = **~75 TB** in transformed layer alone. With landing + raw + clean copies: **~350–400 TB** over 2–3 years. Adding Canada, UK, France, India: **~700 TB – 1 PB range**.
- Realistic phased scale:
  - Phase 1 (niche selection, top 200 US categories, ~5M ASINs): **5–10 TB**
  - Phase 2 (US full coverage, ~100–200M active ASINs): **50–100 TB**
  - Phase 3 (US + 4 markets): **300–700 TB**
- Architecture is designed for Phase 3 scale; infrastructure is sized for Phase 1 to start.
- Cost efficiency is a first-class constraint at this stage.

### Non-goals (explicit)
- Postgres is **not** a historical data store. It is only used for Airflow/Prefect metadata DB.
- No on-the-fly scraping infrastructure owned in-house; vendor APIs handle anti-bot concerns.
- No data warehousing in Snowflake/BigQuery — Trino over Iceberg is the query layer.

---

## 2. Data Sources and Truth Classes

Every record ingested must be tagged with its **truth class** — this governs how downstream consumers trust and reconcile data.

| Truth Class | Sources | Nature | Use |
|---|---|---|---|
| **A — First-party actuals** | Amazon SP-API, Amazon Ads API | Seller-authorized ground truth | Your own inventory, pricing, listings, ad performance |
| **B — Page-truth snapshots** | Rainforest API, SerpApi | Structured capture of amazon.com at a point in time | Competitor prices, offer pages, search results |
| **C — Historical signals** | Keepa | Time-series: price/BSR/offer/review history | Trend analysis, rank tracking, Buy Box dynamics |
| **D — Modeled estimates** | Jungle Scout API, SellerApp | Estimated sales volumes, keyword demand | Directional signals only; always stored with `is_estimate=true` |

### Vendor API Summary

| Vendor | Primary Data | Auth Model | Rate Limits | Notes |
|---|---|---|---|---|
| Amazon SP-API | Inventory, your offer pricing, listings | OAuth2, gated to Professional Selling Account | Per-operation limits | Only for your seller account data |
| Amazon Ads API | Campaign performance, ad spend | OAuth2, access tokens expire 60 min | Async report API for bulk | Use v3 async reporting for batch pulls |
| Keepa | Price history, BSR, Buy Box history, review counts | Token-based subscription | Tokens/min by plan tier | Token budget per run must be controlled |
| Rainforest API | Product pages, offers, reviews, search | API key | Credits/month plan | Use `customer_zipcode` for US localization; `include_html=true` for audit trail |
| SerpApi | Product and search page extraction | API key | Searches/month plan | `amazon_domain=amazon.com`; `raw_html_file` in metadata for traceability |
| Jungle Scout API | Sales estimates, keyword volume, share of voice | API key | Plan-based | Mark all outputs `is_estimate=true`; not ground truth |

**Important**: PA-API (Amazon Product Advertising API) is deprecated as of April 30, 2026. Do not design any new pipeline against it.

### 2.1 Vendor Onboarding Phases

Vendors are onboarded in three deliberate phases — never all simultaneously. Each phase is gated by the business decision made in the previous phase.

| Phase | Trigger | Vendors active | Purpose |
|---|---|---|---|
| **Phase 0 — Product shortlist** | Immediate — no paid APIs | Direct Scrapy scraping | Scrape bestseller + new release pages for 14 categories. Score products via SQL. No API cost. See §0 for full details. |
| **Phase 1 — Niche validation** | After product shortlist from Phase 0 | Keepa only | Pull 12-month BSR/price/review history on Phase 0 finalists (~5 ASINs). Then build full category scoring if needed. ~€49/mo. |
| **Phase 2 — Niche deep dive** | After category is selected | + Rainforest API | Real-time product/offer/review snapshots for the chosen category. ASIN universe already built from Keepa. ~$50–100/mo added. |
| **Phase 3 — Launch prep** | After product is selected | + SerpApi | Keyword/search positioning, share of voice, organic vs sponsored ranking. ~$50/mo added. |

SP-API and Ads API are added only when you have an active seller account (Truth Class A data — your own listings, inventory, ad performance).

### 2.2 Amazon Browse Node Hierarchy — First Pipeline

The Amazon US category tree (~25,000 browse nodes) is the **prerequisite input** to every Keepa category query and to the Category Opportunity Scoring Engine. This is the first pipeline built — nothing else can run without it.

```
Fetch US browse node tree (Amazon PA-API or Rainforest category endpoint)
  → raw.amazon_browse_nodes   (node_id, name, parent_id, level)
  → clean.category_hierarchy  (full parent→leaf path, depth, leaf flag)
```

This table is the backbone of all category-level analysis. Every downstream model joins to it.

---

## 3. Data Layers (Iceberg Lakehouse)

```
object-storage/
  landing/          ← exact vendor payloads, immutable
  raw/              ← Iceberg tables, close to source schema
  clean/            ← standardized schema, quarantine applied
  transformed/      ← business entities, deduped, SCD logic
  curated/          ← analytics-ready marts, stable for BI/apps
```

### Layer Definitions

#### 3.1 Landing (forensic archive)
- **Format**: original JSON or CSV exactly as received from vendor
- **Immutability**: write-once; never modified after ingestion
- **Partitioning**: `vendor/YYYY/MM/DD/batch_id/`
- **Purpose**: replay source when schema drifts or parsing bugs are found
- **Retention**: indefinite (cheap object storage)

#### 3.2 Raw Iceberg
- **Format**: Parquet-backed Iceberg tables
- **Schema**: close to vendor source; minimal normalization
- **One table per source entity** (e.g., `raw.keepa_products`, `raw.rainforest_offers`, `raw.spapi_inventory`)
- **Required metadata columns on every table**:

  | Column | Type | Description |
  |---|---|---|
  | `ingested_at` | TIMESTAMP | When this record hit the pipeline |
  | `vendor` | VARCHAR | Source vendor identifier |
  | `truth_class` | CHAR(1) | A / B / C / D |
  | `batch_id` | VARCHAR | Run identifier for lineage |
  | `source_file` | VARCHAR | S3 path of the landing file |
  | `record_hash` | VARCHAR | SHA256 of the raw payload |
  | `is_estimate` | BOOLEAN | True for modeled/estimated fields |
  | `capture_timestamp` | TIMESTAMP | When vendor captured the page (if available) |

#### 3.3 Clean
- **Format**: Parquet-backed Iceberg
- **What happens here**:
  - Schema standardized across vendors (unified column names)
  - Type casting and null enforcement
  - Bad rows quarantined to `clean.quarantine` with rejection reason
  - US marketplace filter applied (`amazon_domain = amazon.com`)
- **Key tables**: `clean.products`, `clean.offers`, `clean.prices`, `clean.reviews`, `clean.bsr_history`

#### 3.4 Transformed
- **Format**: Parquet-backed Iceberg
- **What happens here**:
  - Join vendor sources by ASIN + marketplace
  - Slowly Changing Dimension (SCD Type 2) for product attributes
  - Deduplication by `(asin, vendor, capture_timestamp)`
  - Cross-source reconciliation: truth class A overrides B; B overrides C/D
- **Key tables**: `transformed.product_master`, `transformed.price_history`, `transformed.offer_snapshots`, `transformed.bsr_trends`

#### 3.5 Curated
- **Format**: Parquet-backed Iceberg
- **Purpose**: stable semantic layer for dashboards, product scoring, alerts
- **Do not break these table contracts** without a migration plan — apps depend on them
- **Key marts**: `curated.product_opportunity_scores`, `curated.competitor_price_index`, `curated.keyword_demand_signals`

### Table Design Rules
- Partition conservatively: over-partitioning creates small-file problems
- Compact Iceberg tables periodically (weekly for high-write tables)
- Define business keys explicitly even if not physically enforced
- All tables must have audit columns (`created_at`, `updated_at`, `batch_id`)

### 3.6 Multi-Platform Architecture

The data layer is designed to support multiple e-commerce platforms (Walmart, eBay, Etsy, Amazon CA/UK/FR/IN) without rewriting business logic above the clean layer.

**Rule: Platform-specific logic lives only in `raw.*` and `clean.*`. Everything above uses a unified entity schema.**

```
raw.keepa_products          ← Amazon-specific
raw.walmart_products        ← Walmart-specific (future)
         │
         ▼  (PySpark transform — maps to common schema)
         │
clean.amazon_products       ← cleaned, still platform-tagged
clean.walmart_products      ← future
         │
         ▼  (PySpark transform — unified join by platform + product_id)
         │
transformed.product_master  ← (platform, platform_product_id, title, brand, ...)
transformed.price_history   ← unified price timeline across all platforms
transformed.offer_snapshots ← unified seller/offer data
transformed.bsr_trends      ← unified sales rank signals
         │
         ▼  (dbt models — platform-agnostic business logic)
         │
curated.product_opportunity_scores
curated.competitor_price_index
curated.keyword_demand_signals
```

Adding a new platform = new ingestion pipeline + PySpark mapping job into `transformed.*`. Zero changes to dbt models or the query layer.

All `transformed.*` tables are keyed by `(platform, platform_product_id)` — never by ASIN alone.

---

## 3.7 Category Opportunity Scoring Engine — Phase 1 Deliverable

> **This is a Phase 1 output, not Phase 0.** Phase 0 (§0) produces a product shortlist from a current snapshot without Keepa. This engine is built only if Phase 0 doesn't surface enough signal or ongoing category monitoring is needed post-launch.

**Purpose**: Answer "which Amazon US category should I enter?" with historical trend depth. Built on top of Keepa data, not scraped snapshots.

Built entirely from Phase 1 data (Keepa only). Six scoring signals:

| Signal | Source table | What it measures |
|---|---|---|
| Revenue addressable market | `transformed.bsr_trends` + sales estimates | How big is the pie |
| Competition density | % of top-100 products with <50 reviews | Room for a new entrant |
| Margin proxy | `transformed.price_history` vs FBA fee estimates | Can you make money |
| Growth trajectory | BSR delta over 30/60/90 days | Rising or declining category |
| Barrier to entry | Avg review count of top 10 products per category | Cost to displace incumbents |
| Brand concentration | Unique brands in top 100 per category | Fragmented vs oligopoly |

**Output**: `curated.product_opportunity_scores` — ranked table of ~200–500 Amazon leaf categories scored 0–100. Explored via Jupyter notebook against Trino.

**Success criterion**: Output surfaces 2–3 surprising categories with quantitative justification — not just confirms prior intuition. If it only confirms what you already thought, the scoring weights are wrong.

---

## 4. Infrastructure Components

### 4.0 Cost Comparison — Why Not AWS

AWS was the default recommendation in early drafts. At this data scale (~7.5 GB today, ~100 GB in 12 months) it is not cost-justified primarily because of **egress fees**.

| Provider combo | Storage/mo (100 GB) | Compute (3 VMs) | Egress (~1 TB/mo) | **Est. total/mo** |
|---|---|---|---|---|
| **Cloudflare R2 + Hetzner** | $1.50 | ~$30 | **$0** | **~$32** |
| Backblaze B2 + Hetzner | $0.60 | ~$30 | ~$0 (free tier) | **~$31** |
| DigitalOcean Spaces + Droplets | $5 | ~$100 | $10 | ~$115 |
| AWS S3 + EC2 | $2.30 | ~$120 | **$90** | **~$212** |

AWS egress ($0.09/GB) becomes the dominant cost line the moment pipelines start reading data for transforms and Trino queries. At 1 TB/month of internal reads the bill is $90 in egress alone — before any compute.

**Decision: Cloudflare R2 for object storage + Hetzner Cloud for compute.**

Rationale:
- R2 has **zero egress fees** — Trino reading Iceberg files from R2 costs nothing
- R2 has official Trino + Iceberg documentation and config examples
- Hetzner is 3–5x cheaper per vCPU/RAM than AWS EC2, DigitalOcean, or Vultr
- Both are production-grade with strong uptime track records
- Fully S3-compatible API — no application code changes needed vs AWS S3

**Fallback**: If R2 + Hetzner cross-region connectivity causes latency issues in testing, **Backblaze B2 + Hetzner** is the next option (B2 has a documented Iceberg integration and similar egress pricing).

---

### 4.1 Object Storage
**Choice: Cloudflare R2**

```
Bucket layout (one bucket per environment):
  ecom-data-platform-dev/
    landing/
    raw/
    clean/
    transformed/
    curated/
    _metadata/

  ecom-data-platform-prod/
    (same structure)
```

R2 is accessed via S3-compatible API. Trino configuration uses the R2 endpoint URL:

```properties
# trino/catalog/iceberg.properties
connector.name=iceberg
iceberg.catalog.type=rest          # or hive/glue substitute — see 4.2
fs.native-s3.enabled=true
s3.endpoint=https://<account-id>.r2.cloudflarestorage.com
s3.region=auto
s3.path-style-access=true
s3.aws-access-key=<R2_ACCESS_KEY_ID>
s3.aws-secret-key=<R2_SECRET_ACCESS_KEY>
```

Access keys are R2 API tokens (scoped per bucket, not account-wide).

**Important**: Verify R2 connectivity against your specific Trino version in dev before promoting to prod. R2 is in Trino's tested list, but test with your exact image.

### 4.2 Iceberg Catalog
**Choice: Project Nessie** (self-hosted on `vm-orchestrator`)

Nessie runs as a single lightweight JVM process (~256 MB RAM). It gives:
- A neutral catalog that is not cloud-vendor-locked
- Branch/tag semantics useful for testing transforms without breaking prod tables
- REST catalog API that Trino supports natively

If Nessie operational overhead ever becomes a problem, **migrate to a REST catalog backed by a simple SQLite/Postgres store**. Avoid Glue — it ties you to AWS.

```properties
# trino/catalog/iceberg.properties (Nessie variant)
iceberg.catalog.type=rest
iceberg.rest-catalog.uri=http://vm-orchestrator:19120/api/v1
iceberg.rest-catalog.warehouse=s3://ecom-data-platform-prod
```

### 4.3 Query Engine
**Choice: Trino** (single-node to start) — **read-only query layer**

Trino is the query and analytics engine. It does **not** write data — all writes go through PySpark jobs or dbt-spark. Trino reads Iceberg tables for ad-hoc SQL, dashboards, and notebooks.

- Separate catalogs per layer: `catalog_raw`, `catalog_clean`, `catalog_curated`
- Write access restricted: Spark and dbt write; Trino is read-only
- Hosted on `vm-trino` (dedicated VM — Trino's JVM heap needs memory headroom)
- Both Trino and Spark point to the **same Nessie catalog instance** on `vm-orchestrator`. This is how Trino immediately sees tables that Spark writes — they share the same metadata registry.

```properties
# trino/catalog/iceberg.properties
iceberg.catalog.type=rest
iceberg.rest-catalog.uri=http://vm-orchestrator:19120/api/v1
iceberg.rest-catalog.warehouse=s3://ecom-data-platform-prod

# spark/conf/spark-defaults.conf
spark.sql.catalog.nessie=org.apache.iceberg.spark.SparkCatalog
spark.sql.catalog.nessie.catalog-impl=org.apache.iceberg.nessie.NessieCatalog
spark.sql.catalog.nessie.uri=http://vm-orchestrator:19120/api/v1
spark.sql.catalog.nessie.warehouse=s3://ecom-data-platform-prod
```

### 4.4 Orchestration
**Choice: Apache Airflow** (team is already using it in this repo)

Airflow makes sense here because:
- Multiple DAGs and schedules already exist
- Team has operational familiarity
- Mature retry, backfill, and admin UI

If operational overhead grows, **Prefect** is the cleanest migration path.

**Airflow setup**:
- `LocalExecutor` to start (simpler, no Redis/Celery needed at this scale)
- Upgrade to `CeleryExecutor` + Redis broker when parallel task count demands it
- PostgreSQL as Airflow metadata DB (this is the **only** Postgres in the stack)
- Hosted on `vm-orchestrator`
- Workers on `vm-worker-1` — scale to `vm-worker-2` etc. as needed

### 4.5 Execution Pattern
All pipeline jobs run as **Docker containers** triggered by Airflow via `DockerOperator`. Two distinct image types:

**Image 1: `ecom-intelligence` (Python ingestion)**
```
Airflow DockerOperator → ecom-intelligence:vX.Y.Z
  → reads Vault secrets (vendor API keys)
  → calls vendor API (Keepa, Rainforest, SerpApi, SP-API)
  → writes raw JSON/CSV to R2 landing layer (S3 API)
  → parses + writes raw Iceberg via Nessie catalog
  → emits structured logs → exits
```

**Image 2: `ecom-spark` (PySpark transforms)**
```
Airflow DockerOperator → ecom-spark:vX.Y.Z
  → connects to Spark standalone master (vm-spark-master:7077)
  → submits PySpark job (spark-submit --master spark://vm-spark-master:7077)
  → Spark reads from R2 via S3A connector (Nessie catalog)
  → transforms: raw → clean → transformed
  → writes Iceberg tables back to R2 via Nessie
  → job completes → container exits
```

**Image 3: `ecom-dbt` (dbt-spark transforms)**
```
Airflow DockerOperator → ecom-dbt:vX.Y.Z
  → runs: dbt run --select curated.*
  → dbt sends SQL to Spark via Thrift server or Spark Connect
  → Spark executes: transformed → curated
  → writes curated Iceberg tables via Nessie
  → exits
```

A typical daily DAG chain:
```
[ingestion] → [spark_raw_to_transformed] → [dbt_transformed_to_curated] → [alert_on_quarantine]
```

### 4.6 Secrets Management
**Choice: HashiCorp Vault** (self-hosted on `vm-orchestrator`, dev mode → production mode)

At this scale, AWS Secrets Manager is unnecessary cost and lock-in. Vault runs alongside the Airflow process on the same VM.

Airflow supports Vault as a secrets backend natively:
```python
# airflow.cfg
[secrets]
backend = airflow.providers.hashicorp.secrets.vault.VaultBackend
backend_kwargs = {"connections_path": "airflow/connections", "variables_path": "airflow/variables", "url": "http://127.0.0.1:8200"}
```

**Split**:
| Secret type | Store |
|---|---|
| Vendor API keys, OAuth tokens | Vault (`/secret/ecom/vendors/`) |
| Airflow connections | Vault Airflow backend |
| Non-sensitive config (batch sizes, thresholds) | Airflow Variables or repo config YAML |
| GitHub Actions deploy credentials | GitHub Secrets (short-lived SSH key or token) |

No secrets in `.env` files on servers, DAG code, or any repo.

### 4.7 VM Layout (Hetzner Cloud — cost-optimized)

| VM | Hetzner type | vCPU | RAM | Cost/mo | Purpose |
|---|---|---|---|---|---|
| `vm-orchestrator` | CX22 | 2 | 4 GB | ~€4.35 | Airflow webserver + scheduler + Vault + Nessie |
| `vm-worker-1` | CX32 | 4 | 8 GB | ~€8.50 | Airflow task workers + Docker runtime for ingestion containers |
| `vm-trino` | CX42 | 8 | 16 GB | ~€17.90 | Trino single-node (read-only query layer) |
| `vm-spark-master` | CX22 | 2 | 4 GB | ~€4.35 | Spark standalone master + driver |
| `vm-spark-worker-1` | CX52 | 16 | 32 GB | ~€38.00 | Spark executor (heavy PySpark transforms) |
| **Total** | | | | **~€73/mo** | ~$79/mo |

All VMs in the same Hetzner datacenter region (Falkenstein EU or Ashburn US — pick based on latency to vendor APIs). Use Hetzner's private network for VM-to-VM traffic (free, no egress between VMs).

Scale path:
- Add `vm-spark-worker-2`, `vm-spark-worker-3` as data volume grows (Phase 2+)
- Upgrade `vm-spark-worker-1` to CX62 (32 vCPU, 64 GB) before adding new workers
- Upgrade `vm-trino` CPU/RAM before splitting into coordinator + worker nodes
- R2 scales automatically — no storage VM needed
- Consider IOMETE on Kubernetes (replacing standalone Spark) at Phase 3 scale (300+ TB)

### 4.8 Spark Cluster (Standalone on Docker — Hetzner)

**Choice: Apache Spark standalone mode**, running as Docker containers on Hetzner VMs.

Spark is the **write engine** for all data transforms: `raw → clean → transformed`. dbt (via Spark Thrift Server) handles `transformed → curated`.

```
vm-spark-master:  Spark master process (Docker container)
vm-spark-worker-1: Spark worker process (Docker container, 16 vCPU / 32 GB)
```

Spark connects to R2 via the **S3A connector** (Hadoop's S3-compatible filesystem). Both Spark and Trino share the same Nessie catalog on `vm-orchestrator`.

**Key Spark config for R2 + Iceberg + Nessie:**
```properties
# spark-defaults.conf
spark.hadoop.fs.s3a.endpoint=https://<account-id>.r2.cloudflarestorage.com
spark.hadoop.fs.s3a.path.style.access=true
spark.hadoop.fs.s3a.access.key=<R2_ACCESS_KEY>
spark.hadoop.fs.s3a.secret.key=<R2_SECRET_KEY>
spark.hadoop.fs.s3a.impl=org.apache.hadoop.fs.s3a.S3AFileSystem

spark.sql.catalog.nessie=org.apache.iceberg.spark.SparkCatalog
spark.sql.catalog.nessie.catalog-impl=org.apache.iceberg.nessie.NessieCatalog
spark.sql.catalog.nessie.uri=http://vm-orchestrator:19120/api/v1
spark.sql.catalog.nessie.warehouse=s3://ecom-data-platform-prod
spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions
```

**⚠️ Known Risk — R2 + S3A Multipart Uploads:**
Cloudflare R2 has known compatibility issues with Hadoop's S3A connector around multipart uploads and checksum validation depending on the Hadoop/Spark version used. Symptoms: large file writes fail silently or throw `EntityTooSmall` or checksum mismatch errors.

**Action required**: Before building any PySpark pipeline, verify R2 + S3A compatibility in dev with a real multi-GB write test. Workarounds if issues arise:
- Set `fs.s3a.multipart.size=128M` and `fs.s3a.multipart.threshold=256M`
- Disable checksum: `fs.s3a.checksum.type=NONE`
- Pin to a Hadoop version known to work with R2 (test with 3.3.4+)
- Fallback option: Backblaze B2 (same zero-egress economics, better S3A compatibility track record)

**Parallelism tuning** (Spark gives explicit control):
```python
# Control partition count for shuffles
spark.conf.set("spark.sql.shuffle.partitions", "200")

# Explicit repartition before heavy joins
df = df.repartition(200, "asin")

# Coalesce before writing to avoid small files
df.coalesce(50).write.format("iceberg").mode("append").save(...)
```

Monitor via Spark Web UI (`vm-spark-master:8080`) — shows active jobs, stages, task distribution, executor utilization, and shuffle metrics.

**Future migration path**: When standalone Spark operational overhead grows or Phase 3 scale demands autoscaling, migrate to IOMETE on Kubernetes. PySpark job code is unchanged — only the cluster management layer changes.

---

## 5. Repository Structure

### Active repo: `ecom-intelligence` (this repo)

All Phase 0 work lives here. Phase 1+ code is added to the same repo as each phase begins.

```
ecom-intelligence/
  CLAUDE.md
  docs/                             ← project documentation (exists)
  .secrets                          ← gitignored — API keys, proxy credentials
  .gitignore

  ── Phase 0 (build this first) ──────────────────────────────────────────

  scraping/                         ← Scrapy project root (run all scrapy commands from here)
    scrapy.cfg                      ← points to settings module
    spiders/
      __init__.py
      amz_category_hierarchy.py     ← FIRST: traverses bestseller nav, writes category tree to DB
      amz_rankings.py               ← SECOND: scrapes bestseller/new_release pages (list_type param)
      amz_products.py               ← THIRD: scrapes product detail pages using raw URLs from ranking table
    helpers/
      __init__.py
      postgres_handler.py           ← DB connection + bulk_upsert (ported from India)
      delay_handler.py              ← adaptive delay logic (ported from India)
      constants.py                  ← LOG_DIR, file size constants
    middlewares.py                  ← HeaderRotationMiddleware + proxy middlewares (ported from India)
    header_templates.py             ← real browser header groups for HeaderRotationMiddleware
    settings.py                     ← DB creds, target categories, middleware stack, Scrapy config
    pipelines.py                    ← writes to ecom_intel Postgres + HTML archive
    items.py                        ← AmzCategoryItem, AmzRankingItem, AmzProductItem
    db/
      ddl.sql                       ← all CREATE TABLE statements (§0.4 order)
      seed_marketplaces.sql         ← INSERT into transformed.marketplaces for amazon_us
      seed_controller.sql           ← seeds amz_category_scrape_controller from leaf nodes

  analysis/                         ← scoring and exploration
    sql/
      01_create_snapshot_table.sql  ← creates amazon_us_product_snapshot table
      02_opportunity_scoring.sql    ← the full 4-stage scoring rubric
    notebooks/
      product_shortlist.ipynb       ← explore scored output, shortlist manually

  ── Phase 1+ (build after product is selected) ──────────────────────────

  pipelines/
    ingestion/                      ← vendor API → R2 landing → raw Iceberg
      keepa/
        browse_nodes.py             ← FIRST Phase 1 script (US category tree)
        products.py                 ← batch ASIN fetch
      rainforest/                   ← Phase 2+
      serpapi/                      ← Phase 3+
      spapi/                        ← when seller account exists
    spark/                          ← PySpark: raw → clean → transformed
      raw_to_clean/
        amazon_products.py
        amazon_bsr.py
        amazon_reviews.py
      clean_to_transformed/
        product_master.py           ← unified cross-platform join
        price_history.py
        bsr_trends.py

  warehouse/                        ← dbt-spark: transformed → curated
    models/
      staging/
        amazon/
      intermediate/                 ← cross-platform unified models (int_*)
      marts/
        opportunity/                ← category_opportunity_scores
        product_research/
        competitor_tracking/
    dbt_project.yml
    profiles.yml                    ← points to Spark Thrift Server

  libraries/                        ← shared Phase 1+ utilities
    vendors/                        ← API clients (Keepa, Rainforest, etc.)
    io/                             ← S3/R2 helpers, Iceberg writer utilities
    validation/                     ← schema validation, quarantine logic
    common/                         ← logging, metrics, config loader

  notebooks/                        ← Phase 1+ analysis
    category_scoring.ipynb
    product_deep_dive.ipynb

  tests/
  pyproject.toml
  Dockerfile.scraping               ← Phase 0: Scrapy image (if containerized later)
  Dockerfile.ingestion              ← Phase 1+: lightweight Python image
  Dockerfile.spark                  ← Phase 1+: PySpark + Iceberg/Nessie jars
  Dockerfile.dbt                    ← Phase 1+: dbt-spark image
  .github/
    workflows/
      build-and-push.yml
      run-tests.yml
```

### Future repo: `orchestration` (Phase 1+ only — not yet created)

Generic orchestration repo — not scoped to ecom-intelligence. Any project needing scheduled pipelines lands here. Projects are isolated by top-level folder. A `shared/` folder holds operators, hooks, and utilities reusable across projects.

**DAGs are written last.** For any phase, DAGs are only added after the phase scripts in `ecom-intelligence` are tested end-to-end and considered deployment-ready. A DAG that calls untested scripts is just scheduled failures.

```
orchestration/
  ecom-intelligence/                ← ecom project DAGs
    dags/
      phase1/
        keepa_browse_nodes.py       ← FIRST DAG (Phase 1)
        keepa_daily.py
        rainforest_snapshots.py     ← Phase 2+
        serpapi_search.py           ← Phase 3+
        spapi_inventory.py          ← when seller account exists
      processing/
        spark_raw_to_clean.py
        spark_clean_to_transformed.py
        dbt_transformed_to_curated.py
      maintenance/
        iceberg_compaction.py
    config/
      dag_defaults.yaml             ← pinned image tags, retry policy
    tests/

  <future-project>/                 ← other projects land here, same structure
    dags/
    config/
    tests/

  shared/                           ← generic, reusable across all projects
    operators/                      ← custom Airflow operators
    hooks/                          ← custom Airflow hooks
    utils/                          ← shared Python utilities for DAGs

  requirements.txt
  .github/
    workflows/
      deploy-dags.yml
```

**Why two repos (Phase 1+)**:
- `orchestration` is lightweight — no heavy dependencies, deploys via rsync to Airflow
- `ecom-intelligence` builds Docker images with heavy dependencies (Spark, dbt, vendor SDKs)
- DAG failures and transform failures have different on-call surfaces
- Orchestration and data logic release independently
- Other projects can use `orchestration` without pulling in ecom-specific code

**Dependency**: DAGs reference `ecom-intelligence` pipeline images by tag (e.g., `ecom-spark:v1.4.2`), pinned in `ecom-intelligence/config/dag_defaults.yaml`.

---

## 6. CI/CD Design

### Pipeline Repo (`ecom-intelligence`)

```yaml
# Triggers: push to main, PR
on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    - ruff lint + format check
    - mypy type check
    - pytest unit tests
    - pytest integration tests (against local MinIO + local Iceberg catalog)

  build-and-push:
    needs: test
    if: github.ref == 'refs/heads/main'
    - docker build
    - push to ECR or GitHub Container Registry
    - tag: git sha + semver tag if present

  deploy-prod:
    needs: build-and-push
    if: startsWith(github.ref, 'refs/tags/v')
    - update prod worker VM image reference
    - notify Slack
```

### Orchestration Repo (`ecom-orchestration`)

```yaml
# Triggers: push to main
jobs:
  validate-dags:
    - python -c "from airflow.models import DagBag; ..."  ← import-time DAG validation
    - pytest tests/

  deploy-dags:
    needs: validate-dags
    if: github.ref == 'refs/heads/main'
    - rsync dags/ to Airflow DAGs folder on vm-orchestrator (via SSH or S3 sync)
    - Airflow picks up new DAGs automatically via scheduler
```

### Deployment model
| Branch/Tag | Environment |
|---|---|
| `main` | dev / staging |
| `v*` semver tag | prod |

---

## 7. Environments

Start with two environments only:

| Environment | Purpose |
|---|---|
| `dev` | Development, testing new pipelines, schema experiments |
| `prod` | Live ingestion; feeds dashboards and product scoring |

Add `staging` only when you need a full dress rehearsal before prod deploys (e.g., before a major schema migration).

### Environment isolation

Each environment has:
- Separate S3 bucket or prefix (`s3://ecom-data-platform-dev/`, `s3://ecom-data-platform-prod/`)
- Separate Nessie catalog branches (`dev` vs `prod` — Nessie supports branching natively)
- Separate Vault paths (`/ecom/dev/...` vs `/ecom/prod/...`)
- Separate Airflow instance or separate Airflow environment variable set
- Separate IAM roles per environment

---

## 8. Run Metadata — Required for Every Batch

Every pipeline run must emit a metadata record to a control table (`_metadata.pipeline_runs`):

| Column | Description |
|---|---|
| `run_id` | UUID for this execution |
| `vendor` | Source vendor |
| `pipeline` | Pipeline name |
| `layer_from` | Source layer (e.g., `landing`) |
| `layer_to` | Target layer (e.g., `raw`) |
| `extraction_window_start` | Start of the data window fetched |
| `extraction_window_end` | End of the data window fetched |
| `schema_version` | Vendor schema version observed |
| `file_count` | Number of landing files in this run |
| `row_count` | Records written |
| `record_hash_sample` | Sample hash for spot-check |
| `quality_status` | `PASS` / `WARN` / `QUARANTINE` |
| `promoted_at` | Timestamp when layer promotion completed |
| `truth_class` | A / B / C / D |

This goes into a lightweight Iceberg table, not ad-hoc logs.

---

## 9. Data Flow Diagram

### Phase 0 (current — execute first)

```
── Step 0: Category hierarchy (run once per market, prerequisite) ──────────────────

Amazon.com /bestsellers/<category> nav sidebar  (10 categories)
         │
         ▼
  AmzCategoryHierarchy spider
  Traverses role="treeitem" links, captures node_id + node_name + parent + url_slug
         │
         ├──► transformed.amz_category          (one row per node)
         └──► transformed.amz_category_hierarchy (closure table — all ancestor-descendant pairs)
                       │
                       ▼
              seed_controller.sql  (SELECT leaf nodes → INSERT INTO amz_category_scrape_controller)

── Step 1: Rankings (on-demand, repeatable) ────────────────────────────────────────

transformed.amz_category_scrape_controller  (pending leaf nodes)
         │
         ▼
  AmzRankings spider  (list_type = 'bestseller' | 'new_release')
  Anti-bot: random UA + HeaderRotationMiddleware + DelayHandler → Oxylabs proxy fallback
  Depth: configurable via DEPTH_LIMIT setting
         │
         ├──► staging.amz_ranking_snapshot
         │    Fields: run_id, asin, rank_position, title, rating, review_count,
         │            price, product_url (raw href), subcategory_node_id, depth
         │
         ├──► html_archive/<date>/<asin>.html.gz  (7-day retention)
         │
         └──► monitoring.scrape_run_field_stats   (null rates per field per run)
                       │
                       ▼
              Validation check: null rate vs threshold / rolling 4-run avg
              Alert if spike detected (selector likely changed)

── Step 2: Product details (after ranking validated) ───────────────────────────────

staging.amz_ranking_snapshot  (ASINs + raw product_url)
         │
         ▼
  AmzProducts spider  (uses raw product_url — never constructs URLs)
         │
         ├──► staging.amz_product_snapshot  (schema TBD)
         │    Fields: bsr_rank, monthly_sales, brand, has_variants,
         │            is_small_business, is_fba, launch_date, sell_mrp
         │
         └──► html_archive/<date>/<asin>.html.gz  (7-day retention)

── Step 3: Scoring ─────────────────────────────────────────────────────────────────

SQL scoring query (analysis/sql/02_opportunity_scoring.sql)
4 stages: hard disqualifiers → signal scoring → median tiebreakers → ranked output
         │
         ▼
  product_shortlist.ipynb → 10–15 ranked product candidates
         │
         ▼
  Manual gut check (listing quality, sourcing feasibility, complaint fixability)
         │
         ▼
  Keepa history pull on 5 finalists only (~15 ASINs, negligible token cost)
         │
         ▼
  Product selection → triggers Phase 1
```

### Phase 1+ (future — triggered by product selection or need for ongoing monitoring)

```
Vendor APIs (phased onboarding)
  ├── Keepa              (Phase 1 — Truth C)
  ├── Rainforest API     (Phase 2 — Truth B)
  ├── SerpApi            (Phase 3 — Truth B)
  ├── Amazon SP-API      (when seller account exists — Truth A)
  └── Amazon Ads API     (when seller account exists — Truth A)
         │
         ▼
  [Airflow] → DockerOperator → ecom-intelligence image (Python ingestion)
         │
         ▼
  Landing Layer on R2 (raw JSON/CSV, immutable, partitioned by vendor/date/batch_id)
         │
         ▼  Python ingestion job parses + writes
         │
  Raw Iceberg (vendor schema, metadata columns: ingested_at, truth_class, batch_id, record_hash)
         │
         ▼
  [Airflow] → DockerOperator → ecom-spark image (PySpark)
         │         spark-submit → vm-spark-master → vm-spark-worker-1
         │
  Clean Iceberg (standardized schema, bad rows → quarantine, US marketplace filter)
         │
         ▼
  Transformed Iceberg (unified platform schema, joined by ASIN, SCD Type 2, cross-source reconciled)
    platform_product_master / price_history / offer_snapshots / bsr_trends
         │
         ▼
  [Airflow] → DockerOperator → ecom-dbt image (dbt-spark)
         │         dbt run → Spark Thrift Server → vm-spark-worker-1
         │
  Curated Iceberg (dbt models: opportunity scoring, competitor tracking, keyword demand)
    curated.product_opportunity_scores  ← first mart built
    curated.competitor_price_index
    curated.keyword_demand_signals
         │
         ▼
  Trino (vm-trino, read-only) ──► Notebooks / Dashboards / Ad-hoc SQL

All layers share Nessie catalog on vm-orchestrator:19120
All data lives on Cloudflare R2 (zero egress)
```

---

## 10. IAM and Access Control

Create separate service identities for each role:

| Identity | Permissions |
|---|---|
| `svc-ingestion` | R2 write to `landing/`; read Vault secrets (vendor API keys) |
| `svc-spark` | R2 read `landing/`; read/write `raw/`, `clean/`, `transformed/`; Nessie catalog write |
| `svc-dbt` | R2 read `transformed/`; read/write `curated/`; Nessie catalog write |
| `svc-trino` | R2 read all layers; Nessie catalog read only |
| `svc-analyst` | Trino read-only on `catalog_curated` |
| `svc-cicd` | GitHub Container Registry push; SSH deploy to VMs (no data access) |
| `svc-airflow` | Docker socket on worker VM; read Vault for connection strings |

---

## 11. Observability

| Signal | Tool | What to watch |
|---|---|---|
| Logs | Loki (self-hosted on vm-orchestrator) | Pipeline errors, vendor API failures, quarantine events, Spark job failures |
| Metrics | Prometheus + Grafana | Row counts per run, quarantine rate, Trino query latency, R2 write throughput, Spark executor utilization |
| Alerts | Slack webhook | Pipeline failure, quarantine rate >5%, Keepa token exhaustion, Spark job OOM |
| Airflow | Airflow UI (vm-orchestrator:8080) | DAG run status, task retries, SLA misses |
| Spark | Spark Web UI (vm-spark-master:8080) | Active jobs, stage breakdown, executor utilization, shuffle metrics, task skew |
| Trino | Trino UI (vm-trino:8080) | Query history, slow queries, memory usage, active workers |

Airflow Slack alerts configured via `notifications.py` (carries over from existing repo).

---

## 12. Decisions Log

All major decisions are closed. Recorded here for future reference.

| Decision | Choice | Rationale |
|---|---|---|
| First step before any infra | **Phase 0: direct Scrapy scraping** | Get a product shortlist in days using zero paid infrastructure. The full platform is only needed for ongoing monitoring post-product-selection. Direct scraping of bestseller + new releases pages gives enough signal (BSR, rating, review count, monthly sales) to score and shortlist products. |
| Anti-bot strategy for Phase 0 | **Random user-agents + custom headers, Oxylabs proxy as fallback** | India spiders already have this setup working. Oxylabs credentials available if Amazon.com blocks direct requests. |
| Object storage | **Cloudflare R2** | Zero egress fees; S3-compatible API; official Trino + Spark S3A support. Fallback: Backblaze B2 if R2+S3A multipart issues cannot be resolved. |
| Compute | **Hetzner Cloud** | 3–5x cheaper per vCPU/RAM vs AWS/DO. All VMs in same region = free private network traffic. |
| Iceberg catalog | **Project Nessie** (self-hosted) | Lightweight JVM, not cloud-locked, supports branching for dev/prod isolation. Shared by both Spark and Trino. |
| Secrets | **HashiCorp Vault** (self-hosted) | Free, no lock-in, native Airflow secrets backend support. |
| Orchestrator | **Apache Airflow** | Existing familiarity and DAG patterns from India project. Migrate to Prefect if ops overhead grows. |
| Transform engine | **PySpark** (raw → transformed) | Data scale (50–700 TB) requires distributed processing. Explicit parallelism control suits a data engineering team. PySpark code is portable if cluster backend changes. |
| Business/analytical layer | **dbt-spark** (transformed → curated) | SQL-first, testable, lineage graph. dbt handles aggregations and scoring models. Spark executes via Thrift Server. |
| Query layer | **Trino** (read-only) | Ad-hoc SQL, dashboards, notebooks. Trino never writes — all writes go through Spark/dbt. |
| Spark cluster | **Standalone on Docker/Hetzner** | Cheapest, no Kubernetes overhead. Migrate to IOMETE on Kubernetes at Phase 3 scale (300+ TB) — PySpark code unchanged. |
| Container runtime | **Docker on VM** | Consistent with existing pattern. DockerOperator triggers ingestion, Spark, and dbt images. |
| Container registry | **GitHub Container Registry** | Free tier, stays within existing GitHub setup. |
| Repo count | **2 repos** (`orchestration` + `ecom-intelligence`) | DAGs and data logic release independently. Three Docker images (ingestion, spark, dbt) built from `ecom-intelligence`. |
| `orchestration` repo scope | **Generic — multi-project** | Not scoped to ecom-intelligence. Projects are isolated by top-level folder (`ecom-intelligence/`, `<future-project>/`). A `shared/` folder holds operators/hooks/utils reusable across all projects. Named `orchestration` (not `ecom-orchestration`) to stay tool-agnostic and project-agnostic. |
| DAG development timing | **DAGs written last per phase** | DAGs are wiring, not logic. Added to `orchestration` only after the corresponding phase scripts in `ecom-intelligence` are tested end-to-end and deployment-ready. |
| Ranking data layers | **staging + transformed** (two tables) | `staging.amz_ranking_snapshot` = raw spider append, short-term buffer, duplicates possible from resume. `transformed.amz_ranking` = permanent deduplicated store, promoted via MERGE post-spider. Queries and scoring always run against transformed, never staging. |
| Ranking dedup key | **(marketplace_id, list_type, subcategory_node_id, asin, scrape_date)** | Same ASIN can legitimately rank in multiple subcategories on the same day — subcategory_node_id must be in the key to preserve those distinct appearances. scrape_date preserves the time series across runs. Resume duplicates (same ASIN + subcategory + day) are resolved via MERGE UPDATE. |
| Controller reset strategy | **Time-based with configurable N** (`min_days_since_last_scrape`) | Resets `scrape_status = pending` only for nodes where `last_scraped_at < NOW() - INTERVAL 'N days'`. Prevents redundant re-scraping if spider is triggered twice in a day. N passed as spider argument at runtime; default = 1. |
| Spark cluster evolution | Standalone → IOMETE/K8s | Trigger: Phase 3 scale or operational pain. Code unchanged — only cluster management layer swaps. |
| Local DB name | **`ecom_intel`** | Generic name — not US-specific, supports all future markets. Separate from the India `ecommerce` DB which is a frozen read-only archive. |
| Multi-market DB schema | **Option A — `marketplace_id` as column** | Single schema set shared across all markets. Schema-per-market (Option B) rejected — cross-market queries would require UNION ALL everywhere. Every table has `marketplace_id` as part of its primary key. |
| Marketplace reference table | **`transformed.marketplaces`** | Lookup for all active markets (marketplace_id, platform, country_code, currency, domain). Every staging and transformed table FK-references this. |
| Category hierarchy storage | **Closure table** (`amz_category_hierarchy`) | Replaces India's fixed `lvl1`…`lvl8` column approach. Any ancestor-descendant query is a simple join; no hard-coded depth limit; works regardless of how deep Amazon's tree goes. |
| Category spider output | **Direct DB write** | India approach saved to JSON file → separate `parse_category_mapping.py` script to load. We write directly to `amz_category` + `amz_category_hierarchy` from the spider pipeline. Eliminates the intermediate file step. |
| Ranking spider design | **Single `AmzRankings` spider with `list_type` param** | India had `AmzCategory` reading from a `.txt` URL file. We read from `amz_category_scrape_controller` and pass `list_type` as a parameter. One spider handles both bestseller and new_release. |
| AmzRankings scrape scope | **All nodes — root, intermediate, and leaf** | Amazon exposes a distinct ranking page at every level of the category tree. Scraping only leaf nodes misses the aggregate views (e.g. "Dogs" bestsellers across all dog subcategories). AmzProducts differs — it only needs leaf-level product detail pages. |
| Controller node scope | **All nodes seeded; spiders filter at query time** | `seed_controller.sql` seeds root + intermediate + leaf nodes. AmzRankings reads all. AmzProducts adds `AND cat.is_leaf = TRUE` via JOIN to `amz_category`. Keeps the controller as a single shared source of truth. |
| `product_url` storage | **Raw href, ref params intact** | Constructed URLs (without Amazon's ref/navigation params) are easier for bot detection systems to identify. Store exactly what's on the page. Used by `AmzProducts` spider directly. |
| `page_num` in ranking table | **Dropped** | Redundant — `rank_position` (1–100) already encodes position across both pagination pages. |
| HTML archiving | **Gzipped HTML on local disk, 7-day retention** | Amazon's CSS/XPath selectors can change without warning. Archiving lets you re-parse with corrected selectors without re-scraping. ~40 KB/page compressed; ~1.4 GB/day at 35k ASINs. Pointer stored as `html_file_path` in snapshot tables. |
| Validation pipeline | **Null rate comparison per field per run** | Stored in `monitoring.scrape_run_field_stats`. Compare against fixed thresholds (Mode A, start here) or rolling 4-run average (Mode B, add once history exists). Spike in null rate = selector likely changed. |
| Scrape controller | **`transformed.amz_category_scrape_controller`** | Same pattern as India's `amz__category_refresh_controller`. Tracks `depth_scraped_upto` and `scrape_status` per leaf node per list_type. Enables resume on failure and incremental scraping. |

**Open item**: Verify R2 + Spark S3A multipart upload compatibility in dev before building any PySpark pipeline. See Section 4.8 for known risk and workarounds.

---

## 13. What Not To Do

**Data storage**
- Do not store any historical vendor data in Postgres — it does not scale for time-series trend queries at TB scale
- Do not skip the immutable landing layer — raw replays are essential when vendor schemas change or parsing bugs are found
- Do not use Postgres for anything except Airflow metadata DB

**Infrastructure**
- Do not use AWS S3 — egress fees ($0.09/GB) dominate the bill the moment Spark and Trino start reading data for transforms
- Do not use AWS EC2 — Hetzner is 3–5x cheaper per vCPU/RAM for identical workloads
- Do not run Trino and Airflow on the same VM — Trino's JVM heap competes with everything else
- Do not run Spark master and Spark workers on the same VM — resource contention causes unpredictable job failures

**Spark + R2**
- Do not assume R2 + Spark S3A multipart uploads work out of the box — test with a multi-GB write in dev first (see Section 4.8)
- Do not write from Trino — Trino is read-only; all writes go through PySpark jobs or dbt
- Do not let Spark write many small files — coalesce before writing; compact Iceberg tables weekly

**Vendor data**
- Do not use PA-API (Amazon Product Advertising API) — deprecated April 30, 2026
- Do not treat Jungle Scout / SellerApp sales estimates as ground truth — tag `is_estimate=true`; reconcile against SP-API actuals
- Do not onboard all vendors simultaneously — follow the Phase 1/2/3 sequencing; Keepa alone covers Phase 1 entirely

**Secrets and credentials**
- Do not put vendor API credentials in DAG code, `.env` files, or GitHub secrets as a long-term runtime store — all secrets go in Vault
- Do not use account-wide R2 API tokens — scope tokens per bucket per service identity

**Architecture**
- Do not write platform-specific logic above the `clean` layer — the multi-platform unified schema breaks the moment Amazon-specific fields appear in `transformed.*` or `curated.*`
- Do not build the API layer before the curated mart layer is stable — apps depending on unstable schemas will require constant rework
