# Category Opportunity Scoring Engine — Approach

> **Context:** Amazon India (amazon.in) data, not US  
> **Goal:** Rank leaf-level categories by attractiveness for new product entry  
> **Date:** 2026-04-16

---

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-04-16 | All | Initial document — scoring engine design for Amazon India data |

## Table of Contents

- [1. What We're Solving](#1-what-were-solving)
- [2. India vs US — Key Differences That Affect Scoring](#2-india-vs-us--key-differences-that-affect-scoring)
- [3. Data We Have (and What Each Contributes)](#3-data-we-have-and-what-each-contributes)
- [4. Scoring Signals — What to Compute and Why](#4-scoring-signals--what-to-compute-and-why)
  - [Signal 1: Market Size (weight: 25%)](#signal-1-market-size-weight-25)
  - [Signal 2: Competition Density (weight: 20%)](#signal-2-competition-density-weight-20-higher--more-opportunity)
  - [Signal 3: Growth Trajectory (weight: 20%)](#signal-3-growth-trajectory-weight-20)
  - [Signal 4: Entry Barrier (weight: 15%)](#signal-4-entry-barrier-weight-15-lower-barrier--higher-score)
  - [Signal 5: Margin Proxy (weight: 10%)](#signal-5-margin-proxy-weight-10)
  - [Signal 6: Market Fragmentation (weight: 5%)](#signal-6-market-fragmentation-weight-5)
  - [Signal 7: Demand-Supply Gap (weight: 5%)](#signal-7-demand-supply-gap-weight-5)
- [5. Composite Opportunity Score](#5-composite-opportunity-score)
- [6. Normalisation Method](#6-normalisation-method)
- [7. Filters Before Scoring (Data Quality Gates)](#7-filters-before-scoring-data-quality-gates)
- [8. What the Output Looks Like](#8-what-the-output-looks-like)
- [9. Implementation Plan](#9-implementation-plan)
- [10. What's Missing From Current Data (Gaps to Plug Later)](#10-whats-missing-from-current-data-gaps-to-plug-later)
- [11. Scoring Weight Calibration](#11-scoring-weight-calibration)
- [12. Decision Framework — When Is a Score "Actionable"?](#12-decision-framework--when-is-a-score-actionable)

---

## 1. What We're Solving

The existing strategy doc (`plans/epip_first_step_strategy`) was written for Amazon US. The goal and the framework are identical — build a scoring engine that converts raw Amazon data into a ranked shortlist of categories worth entering — but the **data, market dynamics, and signal interpretation must be adapted for India**.

The output is a ranked table of lowest-level Amazon India categories, each scored 0–100 on "entry opportunity." You sit with this list, filter for categories you have affinity for or sourcing access to, and pick 3–5 to investigate deeply before any product or supplier decision.

---

## 2. India vs US — Key Differences That Affect Scoring

| Dimension | Amazon US | Amazon India | Impact on Scoring |
|---|---|---|---|
| Market maturity | Highly mature, saturated | Still growing, large underserved segments | Lower review counts = less entrenched incumbents; this is a signal of opportunity not weakness |
| Consumer behaviour | Price-sensitive on commodities, willing to pay premium in niches | Highly value-driven; discounts are expected in most categories | High MRP-to-sell-price gaps are normal; margin proxy must account for this |
| FBA penetration | Dominant | Growing but significant self-ship | FBA rate is still a proxy for competition seriousness |
| Brand landscape | Global brands dominant in most categories | Mix of global, domestic (e.g. Prestige, Asian Paints), and emerging D2C brands | Brand concentration score has different baseline |
| Review volume | Tens of thousands on top products | Hundreds to low thousands on top products | Normalise thresholds accordingly — <200 reviews is low competition in India |
| Sales velocity signals | Highly reliable (Jungle Scout, etc. cross-validate) | `last_month_sale` from the "X bought in past month" badge is Amazon India's estimate — directionally correct, not exact | Treat as a relative signal, not absolute truth |
| Category depth | Deep hierarchy, many 4–6 level leaf nodes | Similar depth; our data captures up to 8 levels | Same approach applies |

---

## 3. Data We Have (and What Each Contributes)

All data lives in the `ecommerce` PostgreSQL database. The scoring engine primarily reads from the **curated** and **transformed** layers — both of which are already cleaned, SCD2-versioned, and aggregated.

| Table / View | Schema | What it contributes to scoring |
|---|---|---|
| `amz__product_consolidated` | curated | Primary source: all product attributes + metrics in one wide table, per scrape week |
| `vw_amz__product_demand_analysis` | curated | Pre-computed demand velocity, review momentum, rating weakness per ASIN per LC per week |
| `vw_amz__lc_demand_analysis` | curated | Same signals aggregated to the (category, lowest_category, week) level — the primary input for LC-level scoring |
| `vw_amz__brandwise_volume` | curated | Brand market share (volume) per LC |
| `vw_amz__product_count_brandwise` | curated | Portfolio width per brand per LC |
| `trf_amz__product_rankings` | transformed | Historical ranking appearances — signals growth/scarcity |
| `vw_amz__list_type_lc_scarcity` | curated | How frequently each LC appears across list types — a demand signal |
| `trf_amz__product_category_master` | transformed | ASIN to (category, lowest_category) mapping |
| `amz__category_refresh_controller` | transformed | Which LCs are active and how many pages/products each has |
| `vw_amz__pending_category_refresh` | transformed | Currently pending — not directly used for scoring but useful for data freshness checks |

---

## 4. Scoring Signals — What to Compute and Why

The engine computes **7 signals** per lowest-category. Each is normalised to 0–100 before being combined.

---

### Signal 1: Market Size (weight: 25%)

**What:** Total addressable volume in the LC — how big is the pie you'd be competing for.

**How to compute:**
```sql
-- Per (category, lowest_category), most recent complete week
SELECT
    category,
    lowest_category,
    SUM(last_month_sale)                        AS total_monthly_units,
    AVG(sell_price)                             AS avg_sell_price,
    SUM(last_month_sale) * AVG(sell_price)      AS estimated_monthly_gmv_inr,
    COUNT(DISTINCT asin)                        AS product_count
FROM curated.amz__product_consolidated
WHERE pd_is_latest = TRUE
GROUP BY category, lowest_category
```

**Normalise:** `score = (estimated_monthly_gmv_inr / max_gmv_across_all_lcs) * 100`

**India note:** Use GMV (units × price) rather than units alone — a 50-unit/month ₹5,000 product is more attractive than a 500-unit/month ₹50 product.

---

### Signal 2: Competition Density (weight: 20%, higher = more opportunity)

**What:** How crowded is the top of the market. In India, a product with <200 reviews is considered low competition. We want LCs where many top products still have thin review bases.

**How to compute:**
```sql
-- Share of products with < 200 reviews among current listings in the LC
SELECT
    category,
    lowest_category,
    COUNT(DISTINCT CASE WHEN reviews_count < 200 THEN asin END) * 100.0
        / NULLIF(COUNT(DISTINCT asin), 0)   AS pct_low_review_products,
    AVG(reviews_count)                      AS avg_reviews_count,
    PERCENTILE_CONT(0.5) WITHIN GROUP
        (ORDER BY reviews_count)            AS median_reviews_count
FROM curated.amz__product_consolidated
WHERE pd_is_latest = TRUE
GROUP BY category, lowest_category
```

**Normalise:** `score = pct_low_review_products` (already 0–100). A higher score means more products are beatable.

**India note:** The threshold of 200 reviews (vs ~500 for US) reflects that India's market is less review-saturated. Adjust this threshold as the dataset grows.

---

### Signal 3: Growth Trajectory (weight: 20%)

**What:** Is this category rising, flat, or declining? We want LCs with accelerating review velocity and consistent list-type presence.

**Three sub-signals combined:**

**3a. Review momentum (from `vw_amz__lc_demand_analysis`):**
```sql
-- 30-day review growth at LC level, most recent 8 weeks
SELECT category, lowest_category,
       AVG(review_momentum_30_days_range)  AS avg_review_momentum
FROM curated.vw_amz__lc_demand_analysis
WHERE weekly_schedule_scrape_date >= CURRENT_DATE - INTERVAL '8 weeks'
GROUP BY category, lowest_category
```

**3b. List-type presence (`vw_amz__list_type_lc_scarcity`):**
Does this LC appear on `hot_new_releases` or `movers_and_shakers`? Presence on these lists = active demand growth signal.
```sql
SELECT category, lowest_category,
       MAX(CASE WHEN list_type = 'hot_new_releases' THEN lc_scrape_count ELSE 0 END)    AS hnr_presence,
       MAX(CASE WHEN list_type = 'movers_and_shakers' THEN lc_scrape_count ELSE 0 END)  AS mas_presence
FROM curated.vw_amz__list_type_lc_scarcity
GROUP BY category, lowest_category
```

**3c. Recent sales index trend:** From `vw_amz__lc_demand_analysis`, compare `recent_sales_index` today vs 4 weeks ago — positive delta = growing.

**Combined growth score:** `0.5 × review_momentum_normalised + 0.3 × list_presence_normalised + 0.2 × sales_index_delta_normalised`

---

### Signal 4: Entry Barrier (weight: 15%, lower barrier = higher score)

**What:** How entrenched are the top incumbents. In India, even category leaders are often beatable if their products are old or poorly reviewed.

**How to compute:**
```sql
-- Top 10 products by last_month_sale in each LC
WITH ranked AS (
    SELECT category, lowest_category, asin, last_month_sale, reviews_count,
           launch_date, rating,
           RANK() OVER (PARTITION BY category, lowest_category ORDER BY last_month_sale DESC NULLS LAST) AS rk
    FROM curated.amz__product_consolidated
    WHERE pd_is_latest = TRUE
)
SELECT category, lowest_category,
       AVG(reviews_count)  AS top10_avg_reviews,   -- high = hard to compete
       AVG(rating)         AS top10_avg_rating,     -- near 4.5 = hard to win on quality
       MIN(launch_date)    AS oldest_top_product    -- very old = brand deeply entrenched
FROM ranked
WHERE rk <= 10
GROUP BY category, lowest_category
```

**Normalise (inverse — lower barrier = higher score):**
- `reviews_barrier = 100 - (top10_avg_reviews / max_across_all_lcs * 100)`
- `age_barrier = 100 - (years_since_oldest_top_product / max_years * 100)`
- `entry_barrier_score = 0.6 × reviews_barrier + 0.4 × age_barrier`

---

### Signal 5: Margin Proxy (weight: 10%)

**What:** Can you make money here? In India, categories with consistent pricing (low discount variance) and reasonable price floors allow better margin planning.

**How to compute:**
```sql
SELECT category, lowest_category,
       AVG((sell_mrp - sell_price) / NULLIF(sell_mrp, 0) * 100)  AS avg_discount_pct,
       STDDEV(sell_price)                                          AS price_stddev,
       AVG(sell_price)                                             AS avg_sell_price,
       PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY sell_price)   AS p25_price
FROM curated.amz__product_consolidated
WHERE pd_is_latest = TRUE AND sell_mrp > 0 AND sell_price > 0
GROUP BY category, lowest_category
```

**Score logic:**
- Avoid extreme discounting: penalise LCs where `avg_discount_pct > 40%` (margin gets compressed)
- Avoid sub-₹200 average price (absolute margin is too thin for FBA economics)
- Reward price diversity (`price_stddev` indicates room for a premium positioning play)

**Margin score:** `= f(avg_sell_price, avg_discount_pct, price_stddev)` — normalised composite.

---

### Signal 6: Market Fragmentation (weight: 5%)

**What:** Is the category dominated by one or two brands, or spread across many? Fragmented markets are easier to enter.

**How to compute:**
```sql
-- Brand concentration: share of top 3 brands by volume vs total
WITH brand_vol AS (
    SELECT category, lowest_category, brand_name,
           SUM(last_month_sale) AS brand_volume,
           RANK() OVER (PARTITION BY category, lowest_category ORDER BY SUM(last_month_sale) DESC) AS brand_rank
    FROM curated.amz__product_consolidated
    WHERE pd_is_latest = TRUE AND brand_name IS NOT NULL
    GROUP BY category, lowest_category, brand_name
),
lc_totals AS (
    SELECT category, lowest_category,
           SUM(brand_volume) AS total_volume,
           COUNT(DISTINCT brand_name) AS unique_brands
    FROM brand_vol GROUP BY category, lowest_category
)
SELECT bv.category, bv.lowest_category,
       SUM(CASE WHEN brand_rank <= 3 THEN brand_volume ELSE 0 END) * 100.0
           / NULLIF(lt.total_volume, 0)  AS top3_brand_share_pct,
       lt.unique_brands
FROM brand_vol bv JOIN lc_totals lt USING (category, lowest_category)
GROUP BY bv.category, bv.lowest_category, lt.total_volume, lt.unique_brands
```

**Score (fragmentation = higher opportunity):**
`fragmentation_score = 100 - top3_brand_share_pct`
Bonus for `unique_brands > 20` (wide market, no single player dominates).

---

### Signal 7: Demand-Supply Gap (weight: 5%)

**What:** Is there unmet demand in the category — poor ratings, high OOS rates, or review velocity accelerating but product quality not keeping up?

**How to compute:**
```sql
SELECT category, lowest_category,
       AVG(CASE WHEN is_oos THEN 1 ELSE 0 END) * 100   AS oos_rate_pct,
       AVG(rating)                                       AS avg_rating,
       1 - AVG(rating) / 5.0                            AS avg_rating_weakness
FROM curated.amz__product_consolidated
WHERE pd_is_latest = TRUE
GROUP BY category, lowest_category
```

**Score:** `= 0.5 × (oos_rate_pct normalised) + 0.5 × (avg_rating_weakness × 100)`  
High OOS + low average ratings = strong gap signal.

---

## 5. Composite Opportunity Score

Each of the 7 signals is normalised to 0–100, then combined with the weights below:

```
Opportunity Score = 
    0.25 × market_size_score
  + 0.20 × competition_density_score
  + 0.20 × growth_trajectory_score
  + 0.15 × entry_barrier_score
  + 0.10 × margin_proxy_score
  + 0.05 × fragmentation_score
  + 0.05 × demand_supply_gap_score
```

**Output table:**
```
category | lowest_category | lc_name | opportunity_score | market_size_score | competition_density_score | growth_score | entry_barrier_score | margin_score | fragmentation_score | gap_score | avg_sell_price | total_monthly_units | unique_brands | avg_reviews | top10_avg_reviews
```

---

## 6. Normalisation Method

All signals use **min-max normalisation** within the current LC dataset (not historical):

```python
score = (value - min_value) / (max_value - min_value) * 100
```

For inverse signals (barrier, discount rate):
```python
score = 100 - ((value - min_value) / (max_value - min_value) * 100)
```

**Important:** Normalise *after* filtering out LCs with fewer than N products (suggested: N = 5) and fewer than M months of data (suggested: M = 2 weeks). Thin data should not influence the scoring distribution.

---

## 7. Filters Before Scoring (Data Quality Gates)

Not all LCs in the refresh controller are worth scoring. Apply these filters first:

| Filter | Rationale |
|---|---|
| `product_count >= 5` | LCs with <5 tracked products have unreliable aggregates |
| `total_monthly_units > 0` | Exclude LCs with no sales signal at all |
| `avg_sell_price >= 200` (INR) | Below ₹200 average price, FBA economics are too thin for a new entrant |
| `pd_scrape_date >= CURRENT_DATE - 30` | Only include LCs with data scraped in the last 30 days |
| Exclude categories in the exclude list | `boost`, `amazon-renewed`, `mobile-apps`, `books`, `digital-text`, `gift-cards`, `software` (non-physical or non-enterable categories) |

---

## 8. What the Output Looks Like

The engine produces **one row per (category, lowest_category)** — i.e., one row per leaf-level niche. A typical output might look like:

| Category | LC Name | Score | Market Size | Competition | Growth | Entry Barrier | Notes |
|---|---|---|---|---|---|---|---|
| kitchen | Pressure Cookers | 78 | 85 | 70 | 82 | 74 | High volume, moderate competition, growing |
| sports | Resistance Bands | 74 | 62 | 88 | 91 | 68 | Fragmented, fast-growing, low reviews incumbent |
| electronics | USB-C Hubs | 44 | 90 | 25 | 70 | 35 | Big market but very entrenched |

You then personally filter this list by:
1. Categories you have sourcing access to (domestic manufacturer, import feasibility)
2. Categories you have any personal knowledge of
3. ₹ price range that fits your target working capital

---

## 9. Implementation Plan

### Phase 1 — Signal SQL Views (1–2 days)

Create SQL views directly on top of the existing curated/transformed layer. No new tables, no new pipelines.

```
sql/
  scoring/
    v_score__market_size.sql
    v_score__competition_density.sql
    v_score__growth_trajectory.sql
    v_score__entry_barrier.sql
    v_score__margin_proxy.sql
    v_score__fragmentation.sql
    v_score__demand_supply_gap.sql
    v_score__composite.sql          ← final scored output view
```

Each view is independently queryable — useful for debugging individual signals.

### Phase 2 — Jupyter Notebook for Exploration (1 day)

A single notebook `notebooks/category_opportunity_scoring.ipynb`:
- Reads `v_score__composite` into a pandas DataFrame
- Renders a sortable table with colour-coded signal columns
- Scatter plots: market_size_score vs competition_density_score (quadrant analysis)
- Bar charts: top 20 LCs by composite score
- Filter widgets by category and minimum price

No web app. No deployment. Run locally.

### Phase 3 — Manual Review and Shortlisting (you, ~2 hours)

Sit with the output. Highlight 10–15 LCs that score >60. For each:
- Can I source this product domestically or via import?
- Is there a brand positioning angle (quality gap, underserved segment)?
- Is the price point right for your capital?

Output of Phase 3: A shortlist of 3–5 LCs to investigate further.

### Phase 4 — Deep Dive on Shortlisted LCs (per LC)

For each shortlisted LC:
- Pull the full ASIN list with reviews, pricing history, launch dates
- Identify the top 5 products by sales and analyse their review text (manual or NLP)
- Identify the bottom quartile by rating — what are customers complaining about?
- Estimate landed cost (supplier quotes) and FBA fees to validate margin

---

## 10. What's Missing From Current Data (Gaps to Plug Later)

These signals would improve the engine but are not available yet. They are **not blockers** for Phase 1.

| Missing Signal | Why It Matters | How to Get It |
|---|---|---|
| Review text / sentiment | "Products are poorly rated but WHY?" — without text, you can't know if the gap is fixable | Scrape via `stg_amz__product_reviews` (already has table, reviews spider not yet active) |
| Keyword search volume | Demand validation beyond what's already selling | Amazon Ads keyword planner, or third-party tools (Helium 10, DataFeedWatch) |
| Historical price trends | Is the category in a price war? Are prices rising? | Already captured via SCD2 in `trf_amz__product_details` — compute `sell_price` delta over time |
| FBA fee estimates | True margin calculation requires knowing fulfilment costs, not just sell price | Compute from product dimensions/weight (need to scrape product specs) or use Amazon's FBA calculator with average dimensions |
| New entrant success rate | How often do new products (< 6 months old) break into the top 50? | Derivable from existing data: filter `trf_amz__product_master` by recent `launch_date` and join to rankings |
| Seasonality | Is Q4 just a spike? | Already capturable from `weekly_schedule_scrape_date` time series — need 6+ months of data to see seasonal patterns |

---

## 11. Scoring Weight Calibration

The weights in Section 5 are a starting point, not gospel. After Phase 2, examine whether the top-scored LCs match your intuition. If they don't:

- **Score feels too dominated by large categories:** Reduce market_size weight; increase competition and growth weights
- **High-scoring LCs are all electronics/tech (hard to enter physically):** Add a "manufacturability" manual flag column to de-prioritise complex categories
- **Growth signal is noisy:** Require a minimum 4-week data window before including a LC in the growth calculation

The calibration step is intentional — the first run will surface blind spots in the weighting. Adjust once, not continuously.

---

## 12. Decision Framework — When Is a Score "Actionable"?

A LC is worth deep-diving when:

1. Composite score ≥ 60
2. `avg_sell_price` ≥ ₹500 (enough margin headroom for FBA + marketing)
3. `top10_avg_reviews` < 500 (incumbents are beatable)
4. `growth_score` > 50 (market is not declining)
5. At least 10 tracked products in the LC (enough data to trust the aggregates)

Any LC that passes all 5 criteria goes into your shortlist for manual review.
