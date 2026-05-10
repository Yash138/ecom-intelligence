# E-commerce Product Intelligence Platform — First Step Strategy

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-04-22 | All | Initial strategy doc — rationale for building category scoring engine first |

## Table of Contents

- [Context](#context)
- [Brutally Honest Assessment](#brutally-honest-assessment)
- [The Very First Thing to Build: Category Opportunity Scoring Engine](#the-very-first-thing-to-build-category-opportunity-scoring-engine)
- [Phase After Niche Selection](#phase-after-niche-selection)
- [Repo Decision: New Repo](#repo-decision-new-repo)
- [Verification / How You Know It's Working](#verification--how-you-know-its-working)

---

## Context
User has (or is planning to have) a data warehouse with all Amazon US data: product details, historic data, reviews, sales estimates, best sellers, movers & shakers, etc. They are the first user of their own system, with no niche selected, no product ideas — only the intent to enter ecomm in a category with the highest upside. The question is: what is the very first thing to build on top of this data?

---

## Brutally Honest Assessment

You have a warehouse full of data and zero conviction about where to play. The warehouse doesn't help you until it answers one specific question: **"Which category should I enter and why?"** Everything else is premature.

The trap most builders fall into here: they keep adding more data pipelines, more tables, more scraping — because the engineering feels productive. But the actual bottleneck is not data volume. It's the absence of a decision framework that converts raw Amazon data into a niche selection verdict.

---

## The Very First Thing to Build: Category Opportunity Scoring Engine

**Not** a product finder. **Not** a trend dashboard. **Not** a full web app.

A single analytical model that ranks Amazon US categories by entry attractiveness — and produces a ranked shortlist you can personally act on.

### What it computes (from warehouse data you already have):

| Signal | Source Table | What it Tells You |
|--------|-------------|-------------------|
| Revenue addressable market | Sales estimates × BSR distribution per category | How big is the pie |
| Competition density | Review count distribution (products with <50 reviews as % of top 100) | Is there room for a new entrant |
| Margin proxy | Price distribution vs FBA fee estimates | Can you make money |
| Growth trajectory | BSR trend over time (30/60/90 day delta) | Is the category rising or declining |
| Barrier to entry | Avg review count of top 10 products | How hard is it to dislodge incumbents |
| Brand concentration | How many unique brands in top 100 | Oligopoly vs fragmented market |

### Output: A ranked table of ~200-500 Amazon leaf categories scored 0-100 on "opportunity."

You personally sit down with this output, filter by categories you have any affinity/knowledge for, and pick your top 3-5 to go deeper on.

### What to build it with (keep it dead simple):

1. **SQL views / dbt models** on top of your warehouse — define the scoring logic in SQL
2. **A single Jupyter notebook or Metabase dashboard** to explore the output
3. **No API, no web app, no user auth** — you are the only user, run it locally

This is the minimum that answers the most important question. Build nothing else until you have your niche.

---

## Phase After Niche Selection

Once you have a category (e.g., "kitchen gadgets sub-niche: precision measuring tools"), the next layer becomes:
- Product-level deep dive (specific ASINs, review sentiment mining, gap analysis)
- Competitor tracking (BSR movements, pricing changes, review velocity)
- Supplier/sourcing cost estimation (to validate margin)
- Keyword demand analysis

But none of that is useful without a niche first.

---

## Repo Decision: New Repo

**Verdict: Create a new repo.**

### Why not the current repo:
- Current repo = Amazon **India** scraping pipelines (Airflow DAGs for `amazon_category_daily`, `amz_products_lc_weekly`)
- New system = Amazon **US** intelligence platform — fundamentally different market, different data sources, different purpose
- The `airflow_init` branch context is tied to a scraping-first India approach that you are reconsidering
- Mixing the two creates confusion about scope, makes git history noisy, and muddies the purpose of each codebase
- If you ever bring in collaborators or investors, a clean repo with a clear identity is important

### What to carry over:
- Airflow DAG patterns (copy relevant pipeline patterns, don't link repos)
- The data provider research (the markdown report lives here but can be referenced from new repo docs)
- Any reusable Python utilities for API calls

### New repo structure (multi-platform ready):

```
ecom-intelligence/
│
├── warehouse/                        # All data modeling (dbt)
│   ├── sources/                      # Raw table declarations, one folder per platform
│   │   ├── amazon/                   # sources.yml pointing at raw Amazon tables
│   │   └── _template/                # Copy this when adding Walmart, eBay, Etsy, etc.
│   │
│   ├── staging/                      # Platform-specific cleaning & normalization
│   │   └── amazon/                   # stg_amazon_products, stg_amazon_reviews, stg_amazon_bsr, etc.
│   │                                 # (add walmart/, etsy/ here later)
│   │
│   ├── intermediate/                 # UNIFIED cross-platform models — the critical layer
│   │   ├── int_products.sql          # Normalized product schema (works for any platform)
│   │   ├── int_categories.sql        # Unified category taxonomy
│   │   ├── int_reviews.sql           # Unified review schema
│   │   └── int_sales_estimates.sql   # Unified sales/demand signals
│   │
│   └── marts/                        # Business-facing output models
│       ├── opportunity/              # Category scoring, opportunity ranking
│       ├── product_research/         # Product-level deep dives
│       └── competitor_tracking/      # BSR movements, pricing, review velocity
│
├── notebooks/                        # Exploratory analysis (local, no server needed)
│   ├── category_scoring.ipynb        # First thing you build and run
│   └── product_deep_dive.ipynb       # After niche is selected
│
├── pipelines/                        # Ingestion DAGs/flows, namespaced by platform
│   ├── amazon/
│   │   ├── products/
│   │   ├── reviews/
│   │   ├── bestsellers/
│   │   └── sales_estimates/
│   └── _template/                    # Skeleton DAG to copy for new platforms
│
├── api/                              # FastAPI — build this last, after the mart layer is stable
│   ├── routers/
│   └── schemas/
│
└── docs/
    ├── data_dictionary/              # What each field means, source, freshness
    └── decisions/                    # ADRs: why Amazon-first, why this warehouse, etc.
```

### The key architectural principle:
Platform-specific code lives **only** in `staging/`. The `intermediate/` layer normalizes everything into a common schema. All business logic in `marts/` and the `api/` only ever talks to `intermediate/` — never directly to staging.

Adding Walmart later = add `staging/walmart/` + map to existing `int_*` models. Zero changes to marts or API.

### Name suggestion: `ecom-intelligence`

---

## Verification / How You Know It's Working

You sit down with the category scoring output and it surprises you — it shows you 2-3 categories you hadn't thought of, with quantitative reasons why they score high. You can click into any category and see the underlying data. That's the definition of "working."

If it just confirms what you already guessed, the scoring weights are probably wrong.
