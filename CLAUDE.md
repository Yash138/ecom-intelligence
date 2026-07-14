# Ecom Intelligence — Project Context

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-05-11 | All | Initial creation — project context, new infra decisions, data providers, repo structure |
| 2026-05-11 | Project, Repo Structure | Owner corrected to personal project; repo renamed `ecom-pipelines` → `ecom-intelligence` |
| 2026-05-11 | Current State, Development Approach, Keepa API Plan, Consultant, Finalized Categories | Added Keepa limits, consultant Rishi, 14 categories, 16-weekend timeline |
| 2026-05-11 | Maintenance Rules | Added doc convention rule — all docs require Update History + TOC |
| 2026-05-11 | First Thing to Build, Repo Structure | Revised to reflect Phase 0 (direct scraping) as the immediate first step before any Keepa/infra work |
| 2026-06-03 | Project Scope, New Infra Key Decisions, Credentials | Scope expanded to multi-market; DB created (`ecom_intel`); multi-market schema decision (Option A — marketplace as column) |
| 2026-06-04 | New Infra Key Decisions | Orchestration repo renamed `ecom-orchestration` → `orchestration`; scope made generic/multi-project |
| 2026-06-07 | Maintenance Rules, Repo Structure, Feedback Log | Added scraping pitfalls reference; AmzCategoryHierarchy completed (8,782 nodes); current build status updated |
| 2026-06-08 | Current State, First Thing to Build | AmzRankings spider built; scrape scope corrected to all nodes (not leaf-only); controller seeded with all nodes; AmzProducts filters leaf at query time |
| 2026-06-09 | Current State, Repo Structure, Feedback Log | Playwright integration complete; AmzRankings now scrapes 100 products/node; `use_playwright` toggle added; venv noted |
| 2026-06-13 | Current State, Maintenance Rules, Repo Structure, Feedback Log | Cross-category contamination (P19/P20/P21) found and fixed; all tables truncated; AmzCategoryHierarchy re-running; `validate_hierarchy.sql` added; bug count updated to 8; three additional code fixes applied (`_set_crawler`, docstring, empty root_categories log) |
| 2026-06-13 | Current State, First Thing to Build, Repo Structure, Feedback Log | AmzProducts spider built; DDL §8a+8b added; `seed_product_queue.sql` created; `items.py` updated; selectors validated against rendered HTML; P22 documented |
| 2026-06-13 | Maintenance Rules, Repo Structure, Feedback Log | Post-build selector fixes committed (related_asins FBT approach, brand/seller/is_fba fallbacks for Amazon-sold products); bootstrap zip-set hardened with wait_for_selector (P23); docs updated |
| 2026-06-13 | Feedback Log | Fixed last_month_sales (P24 — split-text badge requires contains(.,...)+string()); fixed variant_asins to include color/size labels (dimensionToAsinMap + variationValues); validated on Owala |
| 2026-06-14 | Repo Structure, Feedback Log | `validate_product_run.sql` added; `html_archive_dir` param added to AmzProducts; Tier 3f window-function-in-HAVING fixed |
| 2026-06-14 | Maintenance Rules, Feedback Log | P27: AmzCategoryHierarchy sibling-as-child bug fixed; parse() now uses children-only XPath; pitfall range updated to P8–P27 |
| 2026-06-14 | Maintenance Rules, Feedback Log | AmzCategoryHierarchy: Playwright + zip 19901 bootstrap added (P26 — geo-specific nav from non-US IP); AmzRankings scroll fix (P25 — IntersectionObserver, iterative scrollIntoView) |
| 2026-06-15 | Current State, Repo Structure, Feedback Log | AmzCategoryHierarchy full re-run started; H&K done (1,172 nodes clean); remaining 8 cats in progress; `target_categories` spider arg added; `validate_hierarchy.sql` corrected to 9 categories (removed Kitchen & Dining) |
| 2026-06-21 | Current State, Repo Structure, Feedback Log | AmzCategoryHierarchy complete — 9 categories, 7,083 nodes, all 6 validate checks clean; controller seeded (7,298 rows); AmzRankings bestseller run started; P28+P29 documented; `run_rankings.ps1` fixed (full venv path + Kitchen & Dining removed) |
| 2026-07-02 | Current State, Feedback Log, Maintenance Rules | playwright-stealth integrated (P30); callback arity fix; seller/FBA selectors updated for new Amazon buybox layout (P31); DOWNLOAD_DELAY 12s; smoke test clean — 4 real products, all fields populated |
| 2026-07-03 | Feedback Log, Repo Structure | CAPTCHA detection + exponential backoff wired into AmzProducts; shuts down after 10 consecutive blocks; DelayHandler log noise fixed |
| 2026-07-03 | Maintenance Rules, Feedback Log | P32: brand 'by' bug fixed for books/media (`#bylineInfo .author a::text` fallback added). P33: seller name regex `[^.]+?` failed for "Amazon.com"; fixed to `.+?` with `\s+and\s+ships from` terminator. Production run started (425,572 ASINs). |
| 2026-07-04 | Repo Structure | `docs/amazon_scraping_problems.md` created — consolidated all 33 pitfalls + current operational blockers (session blocking, throughput) |

## Table of Contents

- [Project](#project)
- [Maintenance Rules](#maintenance-rules)
- [Current State](#current-state)
- [Development Approach](#development-approach)
- [Keepa API Plan](#keepa-api-plan)
- [Finalized Categories (Amazon US)](#finalized-categories-amazon-us)
- [Consultant](#consultant)
- [New Infra — Key Decisions](#new-infra--key-decisions)
- [Data Providers & Truth Classes](#data-providers--truth-classes)
- [First Thing to Build](#first-thing-to-build)
- [Credentials](#credentials)
- [Repo Structure](#repo-structure)
- [Feedback / Error Log](#feedback--error-log)

---

## Project
- **Owner:** Yash (personal project)
- **Goal:** System for product discovery, evaluation, and differentiation — supports launching products in the ecommerce market.
- **Scope:** Multi-market from the start (Amazon US initial target; Amazon IN, UK, CA and other markets planned). All DB and pipeline designs must treat `marketplace_id` as a first-class dimension.

## Maintenance Rules
- Update this file whenever new decisions, credentials, or structural info emerge. Do not wait to be asked.
- Keep it dense — pointers and decisions only. Link to docs; don't reproduce them.
- Log all dev errors and user corrections in the Feedback Log section.
- **Doc convention (all documents):** Every doc must have `## Document Update History` (table: Date | Sections Changed | Summary) immediately after the title block, then `## Table of Contents` (anchor links, 2 levels) immediately below. Update the history table on every edit.
- **MUST READ before building any spider:** `docs/scraping_pitfalls.md` (P1–P7, P21 from AmzCategoryHierarchy) + `scraping/CLAUDE.md` Key Pitfalls (P8–P34 from AmzRankings/AmzProducts/AmzCategoryHierarchy). These mistakes MUST be avoided at any cost.

## Current State
- **Old infra (exists, India):** Scrapy-based scraping of amazon.in → PostgreSQL (`ecommerce` DB). ~7.5 GB, 2.3M ASINs, 1 year history. Airflow + DockerOperator on local machine. Not being migrated — separate concern.
- **New infra (Phase 0 — ready for AmzProducts full run as of 2026-07-02):**
  - `AmzCategoryHierarchy` **complete**: 9 categories, 7,083 nodes, all 6 validate checks clean.
  - `seed_controller.sql` run: 7,298 rows inserted.
  - `AmzRankings bestseller` **complete**: all 9 categories scraped, 516,220 rows in `transformed.amz_ranking`.
  - `merge_rankings.sql` **run**: staging promoted to transformed.
  - `seed_product_queue.sql` **run**: 423,936 ASINs in `transformed.amz_product_scrape_queue`.
- **Next step (only remaining):**
  6. `cd scraping && .venv_scrape/Scripts/scrapy crawl AmzProducts -a marketplace_id=amazon_us -s LOG_FILE=logs/products_run1.log`
  Then: `psql -d ecom_intel -U ecom_intel_admin -f scraping/db/validate_product_run.sql`
- **Time constraint:** India IP unmasked — only run AmzProducts within **11 AM–11 PM IST**.
- **`AmzProducts` spider is built + stealth-patched** (2026-06-13, patched 2026-07-02). Reads from `transformed.amz_product_scrape_queue`, writes to `staging.amz_product_snapshot`. Requires Playwright + zip 19901 for buybox fields. playwright-stealth applied via `playwright_page_init_callback`. Design in `scraping/CLAUDE.md` § AmzProducts Design Decisions.
- **`staging.amz_product_snapshot` truncated** (2026-07-02) — CAPTCHA garbage from first failed run removed. Queue (`transformed.amz_product_scrape_queue`) intact with 423K ASINs.
- **Active branch:** `dev_scraping`
- **AmzRankings Playwright:** complete — scrapes 100 products/node (50/page × 2 pages). `use_playwright=false` for fast 60-product runs. Chromium installed at `C:\Users\yashl\AppData\Local\ms-playwright\chromium-1223`.

## Development Approach
- **No paid infra on Day 1.** Start with Keepa subscription only. Build locally. Cloud infra (Hetzner + R2 + Trino) provisioned only when data volume or performance demands it.
- **Phase 1 local stack:** Keepa API → local Postgres or DuckDB → pandas/Jupyter for scoring. No Spark, no Trino needed yet.
- **Timeline:** 16 weekends (May–end of August 2026). AI-driven development. Keepa fetch rate is the primary bottleneck.
- **Phase 1 target (3–5 weekends):** Category Opportunity Scores for the 14 finalized categories. Must be ready before Week 7 of the consultant's roadmap (product selection deadline).

## Keepa API Plan
- **Entry plan: 20 tokens/min (~€49/month)**
- 1 basic product fetch = 1 token (price, BSR, review history); +2 tokens for Buy Box history
- Batch: up to 100 ASINs per request at same token cost — always batch
- 20 tokens/min → **~9,600 ASINs/day** (full history, 3 tokens/ASIN)
- For Phase 1 scoring: fetch top 200 ASINs per leaf node. ~4,200 leaf nodes × 200 ASINs = 840K ASINs. Batched at 100/request → ~8,400 requests → **completable in hours on entry plan**
- Upgrade path: €129/mo (60 tok/min), €459/mo (250 tok/min) if deeper catalog needed
- Tokens expire after 60 min if unused — run continuous ingestion, don't let tokens pile up

## Finalized Categories (Amazon US)
14 categories selected. Full list: `docs/chosen_categories.md`

## Consultant
- **Rishi** — hired to guide the Amazon launch process
- YouTube: https://www.youtube.com/@Indiamaan
- Delivered roadmap: `docs/new_infra/Ecom Roadmap.xlsx` (4 phases, 20 weeks to brand live)
- He is not aware of the ecom-intelligence data system being built in parallel
- Roadmap phases: Phase 1 (Wk 0–7): Research & Product Finalisation → Phase 2 (Wk 8–11): Supplier Setup → Phase 3 (Wk 12–13): Amazon Setup → Phase 4 (Wk 14–20): Branding & Launch

## New Infra — Key Decisions
Full design: `docs/new_infra/infra_design.md` (v1.2, 2026-04-22) — source of truth. `docs/new_infra/design_doc_chatgpt.md` is superseded — ignore.

| Concern | Choice |
|---|---|
| Object storage | Cloudflare R2 (zero egress) |
| Compute | Hetzner Cloud |
| Table format | Apache Iceberg |
| Iceberg catalog | Project Nessie (self-hosted) |
| Query layer | Trino (read-only) |
| Orchestration | Apache Airflow (LocalExecutor to start) |
| Transforms | PySpark (raw→transformed), dbt-spark (transformed→curated) |
| Secrets | HashiCorp Vault (self-hosted) |
| Repos | 2 repos: `orchestration` (DAGs, generic/multi-project) + `ecom-intelligence` (scripts, Spark, dbt) |
| Local DB (Phase 0) | `ecom_intel` on localhost Postgres — separate from India `ecommerce` DB |
| Multi-market schema | Option A: `marketplace_id` column on every table; single schema set, not schema-per-market |

**Do not use:** AWS S3/EC2 (egress cost), Postgres for historical data, PA-API (deprecated 2026-04-30). Do not write Phase 0 data into the old `ecommerce` India DB.

## Data Providers & Truth Classes
Full research: `docs/new_infra/Amazon-Ecom-data-providers-deep-research-report.md`

| Phase | Vendor | Truth Class | Purpose |
|---|---|---|---|
| 1 (Day 1) | Keepa | C | Price/BSR/offer history, browse node hierarchy |
| 2 (after niche selected) | Rainforest API | B | Product/offer/review snapshots |
| 3 (after product selected) | SerpApi | B | Keyword/search/organic ranking |
| When seller account exists | Amazon SP-API + Ads API | A | Own inventory, listings, ad performance |

Jungle Scout / SellerApp = Truth Class D (estimates, store with `is_estimate=true`).

## First Thing to Build
**Phase 0 (immediate — no paid APIs needed):**
1. Adapt India Scrapy spiders for Amazon.com → scrape bestseller + new releases for 14 categories → local Postgres
2. Run SQL scoring rubric → ranked product shortlist
3. Manual gut check → Keepa history pull on 5 finalists only → product selection
- Full approach: `docs/new_infra/infra_design.md` §0
- Original strategy ideation: `docs/fast_execution_new_strategy.md`

**Phase 1 (after product selected — triggers full infra build):**
- Browse node pipeline via Keepa → category opportunity scoring
- Scoring signals: `docs/new_infra/category_opportunity_scoring_approach.md` (India data — adapt <50 review threshold for US)
- Strategy rationale: `docs/old_infra/epip_first_step_strategy.md`

## Credentials
- **PostgreSQL (India DB — frozen):** host=localhost, db=ecommerce, user=llm_readonly, password=gaC5.adu1, port=5432 — read-only archive, do not write
- **PostgreSQL (ecom_intel — active):** host=localhost, db=ecom_intel, port=5432, admin user=ecom_intel_admin — full creds in `.secrets/admin_creds.env` (gitignored)
- **Cloud infra credentials:** not yet provisioned

## Repo Structure
- `CLAUDE.md` — this file
- `docs/chosen_categories.md` — 14 finalized Amazon US categories
- `docs/fast_execution_new_strategy.md` — original ideation for Phase 0 scraping strategy (superseded by infra_design.md §0)
- `docs/new_infra/infra_design.md` — **final** new infra design; **§0 is Phase 0 (execute first)** — direct scraping for product shortlist
- `docs/new_infra/Ecom Roadmap.xlsx` — consultant's 20-week launch roadmap
- `docs/design_doc_chatgpt.md` — superseded first draft, ignore (moved out of new_infra/)
- `docs/new_infra/category_opportunity_scoring_approach.md` — scoring engine design (signals, SQL, weights)
- `docs/new_infra/Amazon-Ecom-data-providers-deep-research-report.md` — vendor API research
- `docs/old_infra/system_flow.md` — old India pipeline architecture (Scrapy → Postgres)
- `docs/old_infra/table_structure.md` — old India DB schema
- `docs/old_infra/data_dictionary.md` — old India data dictionary
- `docs/old_infra/epip_first_step_strategy.md` — strategy rationale for building category scoring first
- `docs/scraping_pitfalls.md` — **MUST READ** — P1–P7 + P21 from AmzCategoryHierarchy; P8–P34 in `scraping/CLAUDE.md`
- `docs/amazon_scraping_problems.md` — consolidated view of all problems (bot detection, throughput, data bugs, infra) — good starting point for planning solutions
- `scraping/` — Scrapy project; flat layout (no package wrapper)
- `scraping/spiders/amz_products.py` — AmzProducts spider (built 2026-06-13)
- `scraping/db/seed_product_queue.sql` — manual seeder for product queue; run after each rankings merge cycle
- `scraping/db/ddl.sql` — full schema including §8a (queue) + §8b (product snapshot)
- `scraping/db/validate_product_run.sql` — post-run health check; run after each AmzProducts batch; 0 rows on all checks = healthy

## Feedback / Error Log
<!-- Format: YYYY-MM-DD | context | what went wrong or was corrected -->
- 2026-05-11 | CLAUDE.md setup | Do not mix Sprouts work email with this personal project
- 2026-06-07 | AmzCategoryHierarchy | 7 bugs fixed — full details in `docs/scraping_pitfalls.md`
- 2026-06-09 | venv | Always use `.venv_scrape` in repo root — never system Python or global pip
- 2026-06-09 | AmzRankings | Playwright integration done; `use_playwright` toggle added; docs: `scraping/docs/top_100_rankings_approach.md`, `scraping/docs/possible_enhancements.md`
- 2026-06-13 | AmzRankings contamination | Three cross-category contamination bugs found and fixed: P19 (`_mark_node_complete` matched by subcategory name not node_id), P20 (closure table contamination allowed wrong categories into eligibility query), P21 (shared URL slug `/hi/` caused three root categories to overwrite same `amz_category` row). All `ecom_intel` tables truncated. Full bug details: `scraping/CLAUDE.md` P19–P21; P21 also in `docs/scraping_pitfalls.md`.
- 2026-06-13 | validate_hierarchy.sql | New pre-seed validation script added (`scraping/db/validate_hierarchy.sql`). 6 checks. Must run after `AmzCategoryHierarchy`, before `seed_controller.sql`. All checks must return 0 rows before proceeding.
- 2026-06-13 | run_rankings.ps1 | Added `-Categories` param (pipe-delimited subset of categories). Fixed `-Category` silent failure caused by PowerShell case-insensitive variable collision (`$Categories` param overwrote `$categories` array). Renamed internal array to `$allCategories`.
- 2026-06-13 | Code fixes | (1) `AmzCategoryHierarchy.from_crawler()` now calls `_set_crawler(crawler)` instead of `spider.settings = crawler.settings` — P9 consistency. (2) `_playwright_meta()` docstring corrected to describe `wait_for_load_state` flow. (3) `AmzRankings.start()` now logs a clear error when `root_categories` is empty ("Has seed_controller.sql been run?") instead of the misleading "all nodes already scraped" message.
- 2026-06-13 | AmzProducts built | Spider built; 20 fields; Playwright + zip 19901 for buybox; delete-on-success queue; SCD2 timestamps; monitoring stats. Fixed selectors: `#bylineInfo` is `<a>` not container; `th.prodDetSectionEntry` is on `<th>` not `<tr>`; `networkidle` times out (P22). Validated on catchmaster + BubbleBlooms rendered HTML.
- 2026-06-13 | AmzProducts post-build fixes | (1) `related_asins`: replaced `#exportAlternativeAsinsInfo` (doesn't exist) with FBT widget `[data-cel-widget*="p13n-desktop-sims-fbt"] a[href*="/dp/"]`. (2) `brand`/`seller_name`/`is_fba`: added `#merchant-info` fallbacks for Amazon-sold products (no `#bylineInfo` / `#sellerProfileTriggerId`). (3) Bootstrap zip-set: replaced `wait_for_timeout(1500)` with `wait_for_selector('#GLUXZipUpdateInput', state='visible', timeout=15000)` — P23. Validated on Owala (Amazon-sold FBA): brand=Owala, seller=Amazon Resale, is_fba=True, bsr rank 1 in 3 categories, dimensions=✓.
- 2026-06-13 | AmzProducts selector fixes #2 | (1) `last_month_sales`: changed `contains(text(), ...)` → `contains(., ...)` + `string()` for split-text badge; also handles "past week" badge (P24). (2) `variant_asins`: now decodes full dimension labels from `variationValues` + `dimensions`; returns `[{"asin":..., "color_name":..., "size_name":...}]` instead of plain ASIN list. `_queue_variant_asins` updated for new dict format. Validated on Owala: last_month_sales=20K+, variant_asins=65 entries with color+size.
- 2026-06-13 | AmzRankings Playwright scroll fix | P25: `window.scrollTo(bottom)` does not trigger Amazon's IntersectionObserver; lazy-load requires `scrollIntoView` on the last visible card, repeated until count reaches 50. Fixed in `_playwright_meta()` — replaced single scrollTo+1.5s with async IIFE (up to 5 iterations × 2s). Verified via `html_debug/test_scroll.py`: 30→38→46→50 in 3 passes. See P25 in `scraping/CLAUDE.md`.
- 2026-06-14 | AmzCategoryHierarchy | P26: non-US IP serves geo-specific Amazon nav — hierarchy built from Indian IP had wrong ancestors, depth=48 for a 3-level node, cross-category contamination. Fix: added Playwright + zip 19901 bootstrap (same pattern as AmzProducts). `use_playwright=true` is now the default. See P26 in `scraping/CLAUDE.md`.
- 2026-06-14 | validate_product_run.sql | Tier 3f initial draft used window function in HAVING (illegal in Postgres) — rewrote as CTE with GROUP BY. Script verified against live DB: all checks 0 rows, Tier 4 shows 69 Owala variant ASINs in queue (expected from test run, not a bug).
- 2026-06-14 | AmzCategoryHierarchy sibling-as-child bug | P27: spider iterated all `zg-browse-item` elements on each page, which includes sibling nodes (other children of the current node's parent) shown as `<a>` links. Spider treated them as children of the current node, producing wrong ancestor_chains and depth explosions (depth=48 for 3-level nodes). Fix: replaced iteration with `//span[@aria-current="page"]/ancestor::li[1]/following-sibling::li[1][.//ul]//a` — targets ONLY the children wrapper `<li>` (identified by containing a `<ul>`). Leaf pages have no such wrapper, so they return empty (correct leaf detection). Nav HTML dumped for 3 depths (nav_root.html, nav_depth1.html, nav_depth2.html) and verified. See P27 in `scraping/CLAUDE.md`.
- 2026-06-21 | AmzCategoryHierarchy | P28: Amazon DAG nodes appear under multiple root category navs. Running as separate Scrapy processes resets `visited_node_ids` per process — second run re-claims nodes already owned by first run under a different root slug → CHECK 4 fails. Fix: (1) `root_url_slug` meta guard in `parse()` blocks following cross-root links; (2) always run all categories in a single Scrapy process (`-a target_categories=` with all categories). See `scraping/CLAUDE.md` P28.
- 2026-06-21 | AmzCategoryHierarchy | P29: Partial restart contamination — restarting with a category subset resets `visited_node_ids` to empty, so DAG nodes owned by already-complete categories can be re-claimed under the restarted category's slug. Fix: `spider_opened` pre-populates `visited_node_ids` from DB for all non-target categories. On a full fresh run this is a no-op. See `scraping/CLAUDE.md` P29.
- 2026-06-21 | run_rankings.ps1 | Fixed bare `scrapy` call → full venv path (`D:/Documents/projects/GitHub/ecom-intelligence/.venv_scrape/Scripts/scrapy.exe`). Removed `Kitchen & Dining` from `$allCategories` (never in chosen_categories; not in hierarchy DB).
- 2026-06-21 | AmzCategoryHierarchy final counts | 9 categories complete: Arts 473, Clothing 1682, Handmade 10, H&H 985, H&K 1099, Office 509, Patio 682, Pet 571, T&HI 1072 = 7,083 total. All 6 validate checks pass.
- 2026-07-02 | playwright-stealth (P30, P31) | Amazon CAPTCHA blocked AmzProducts after 22+ hours of unstealth scraping (57% null title rate). Fix: integrated `playwright-stealth==2.0.3` (`Stealth().apply_stealth_async(page)`) via `playwright_page_init_callback` in all 3 spiders. Callback arity is `(page, request)` — NOT 3 args; wrong arity silently fails and CAPTCHA returns. DOWNLOAD_DELAY raised to 12s (Playwright mode). Additionally fixed seller/FBA selectors (P31): Amazon's new tabular buybox and compact "Ships from and sold by" layouts replaced `#merchant-info` — 3 patterns now handled in `_parse_seller_name` and `_parse_is_fba`. Full smoke test: 4 real products across categories, no CAPTCHA, all fields populated. See `scraping/CLAUDE.md` P30–P31.
- 2026-07-03 | CAPTCHA backoff | Wired `DelayHandler` into `AmzProducts`. `_is_blocked()` checks 5 signals (CAPTCHA URL redirect, instrumentation JS, auth-challenge API, bot-block text, null title). On block: exponential backoff via `DelayHandler`, ASIN stays in queue. After 10 consecutive blocks → graceful shutdown with `blocked_by_amazon` reason. `consecutive_blocked` resets to 0 on each successful write. `DelayHandler` log noise fixed (only logs when delay actually changes).
- 2026-07-03 | P32 + P33 | Brand extraction `'by'` bug: books/media `#bylineInfo` first text node is "by" not the brand — added `#bylineInfo .author a::text` fallback; guard added `text.lower() not in ('by', '')`. Seller regex `[^.]+?` silently failed for "Amazon.com" — changed to `.+?` with terminator `(?:\s+and\s+ships from|\.\s*$)`. Verified on 38 saved HTMLs; re-ingest of 14 previously scraped ASINs done; production run started (425,572 ASINs). See `scraping/CLAUDE.md` P32–P33.
