# Fast Execution Strategy — Product Discovery via Direct Scraping

> **Status:** Superseded and incorporated into `docs/new_infra/infra_design.md` §0 (Phase 0).  
> This file is the original ideation conversation. Refer to infra_design.md §0 for the canonical version.

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-05-11 | All | Initial ideation — fast product discovery strategy using direct scraping |

## Table of Contents

- [Original Discussion](#original-discussion)

---

## Original Discussion

```
just for the sake of initiating/accelerating the things if I want to find some products where there is a high probability of finding a winning products then what should be the strategy?
intention is to choose a category then somehow create a list of products - chosen using some rules applied on all the products right now available on amazon.com picked from bestseller or from new releases section.

my idea: 
- may be look for a product which is in bestseller of a category but still its rating is 4 or below
- if a product is having the number of reviews lesser than the median then also this could be a good indicator of a potential product


do these sounds good, and what else can be used as indicator to find the potential products
```


Your thinking is solid! Let me build on it with a more complete framework.

## Your Two Ideas — Validated ✅

**1. Bestseller with rating ≤ 4.0**
Good signal, but refine it: the sweet spot is actually **3.5–4.2**. Below 3.5 means the product concept itself may be broken. Above 4.2 = hard to differentiate. 3.5–4.2 means *demand is proven but execution is weak* — that's your opportunity.

**2. Fewer reviews than category median**
Yes, strong signal. This is essentially a **"low social proof despite traction"** indicator — the product is selling well enough to be a bestseller, but hasn't accumulated review momentum yet, meaning you can still enter and compete.

---

## Additional Indicators to Stack

The real power is **signal stacking** — a product that hits 3–4 of these simultaneously is a strong candidate.

**Demand-side signals**
- **BSR in top 1% but review count < 200** — selling well before going viral, early mover window still open
- **High search volume keyword with low sponsored competition** — use tools like Helium 10's Cerebro; organic ranking is still winnable
- **"Frequently bought together" gap** — the product exists but no one has made a bundle/kit version yet

**Competition-side signals**
- **Top 3 listings have poor images or thin bullet points** — the bar is low; better creative alone can win
- **No brand dominates** — if page 1 is all generic/unbranded sellers, there's no entrenched player to fight
- **Price clustering is inconsistent** — e.g., products ranging from $8 to $45 for the same item = market hasn't found equilibrium, room to position

**Review content mining (highest alpha)**
- **Recurring 3-star review complaints** — these are your product brief; solve the #1 complaint and you win
- **5-star reviews mentioning what's *missing*** — e.g., "love it but wish it came with X" → make the version with X
- **Q&A section with unanswered questions** — signals unmet expectations the current sellers ignore

**Trend signals**
- **New Releases bestseller + < 6 months old + < 50 reviews** — early traction before saturation
- **Google Trends upward slope** for the category keyword (not yet peaked)
- **TikTok/Pinterest virality before Amazon catches up** — the product is trending socially but Amazon listings are still weak

---
---


## A Practical Scoring Rubric

When you scan a product, score it:

| Signal | Points |
|---|---|
| BSR Top 1%, reviews < 200 | +2 |
| Rating 3.5–4.2 | +2 |
| Top competitor has weak listing (images/copy) | +1 |
| Recurring fixable complaint in reviews | +2 |
| No dominant brand on page 1 | +1 |
| Google Trends upward | +1 |
| Price inconsistency across page 1 | +1 |

**Score ≥ 6 → serious candidate. Score ≥ 8 → move fast.**

---

## The Process in Order

1. Pick a **parent category** with healthy margin potential (typically $20–$80 price range, lightweight/small)
2. Scrape/browse **Bestsellers + New Releases** top 100 in that category
3. Filter by your two rules first (quick pass)
4. For survivors, stack the additional signals above
5. Deep-dive review mining on the top 3–5 candidates
6. Validate with keyword tools before committing

Would you like help building an automated version of this — a tool that pulls Amazon BSR data and scores products against these rules?

---

```
think it this way:

* let's say I build a simple scraper which can scrap these three things:
   * bestseller page (given the category/subcategory)
   * new release page (given the category/subcategory)
   * products page (given the url of the page) - the info it has: all the products from same category listed with their price, rating, review counts, last month sale number, 


so the products list we got from bestseller and new release we can go to their products listing page fetch sales details as well.

if this data we have which is showing just the current situation of the market, then what are the steps you would take to filter out the procuts
```

Great framing. With that data snapshot, here's exactly how I'd filter:

## The Filtering Pipeline

### Stage 1 — Hard Disqualifiers (eliminate immediately)
These are dead ends regardless of other signals:

- **Rating < 3.5** → product concept is broken, not just execution
- **Price < $15** → margins too thin after Amazon fees + shipping
- **Dominated by a major brand** (P&G, Scotts, Clorox, Raid etc.) → you can't out-resource them
- **Review count > 2,000 AND rating > 4.5** → market is locked, the winner has already won

---

### Stage 2 — Opportunity Scoring (score each surviving product)

Run every remaining product through this:

```
SIGNAL 1 — Weak leader despite proven demand
Condition: BSR rank ≤ 100 AND rating between 3.5–4.2
Score: +3  ← highest weight, this is the core thesis

SIGNAL 2 — Early but already gaining traction  
Condition: From New Releases AND review count < 150 AND rating ≥ 4.0
Score: +3  ← new release with immediate pull = strong product-market fit

SIGNAL 3 — Low review count relative to sales velocity
Condition: "X+ bought in past month" is high BUT review count is low
Ratio: if (monthly_sales / review_count) > 10 → underreviewed, still enterable
Score: +2

SIGNAL 4 — Price room exists
Condition: product price is in the bottom 30% of its category page's price range
Meaning: customers are already paying more for similar things → you can charge more
Score: +2

SIGNAL 5 — Small seller, not a brand
Condition: "Small Business" badge OR no recognizable brand name
Score: +1  ← means no moat, you can compete

SIGNAL 6 — High sales but few listing variations
Condition: product has no variants (size/color/bundle) visible
Score: +1  ← you can enter with a bundle or size variant and capture adjacent demand
```

**Threshold: Score ≥ 6 → shortlist it**

---

### Stage 3 — Comparative Context Check

Once you have a shortlist, do a **same-category cross-check**:

- Pull the **median review count** across all products on that category page
- Any shortlisted product with reviews **below that median** gets **+1 bonus**
- Pull the **median rating** — products rated below median but still in bestseller get **+1 bonus**

This is where your original two ideas plug in — not as primary filters but as **tiebreakers** on already-qualified candidates.

---

### Stage 4 — Manual Gut Check (only ~5 products reach here)

Before declaring a winner, ask three questions manually:

1. **Is the complaint fixable?** (you'd need to read reviews, outside your scraper scope for now — flag for manual review)
2. **Can it be sourced?** Is this a generic/commodity product that a manufacturer can replicate, or is it patented/branded?
3. **Does the listing look beatable?** Bad images, thin description, no A+ content = yes

---

## What Your Scraper Output Should Look Like

Each product row should carry:

```
name | bsr_rank | source (bestseller/new_release) | price | rating | 
review_count | monthly_sales | small_business (bool) | 
computed: sales_to_review_ratio | opportunity_score
```

Then just **sort by opportunity_score descending** and the top 10 are your research candidates.

---

## One Important Nuance

From your **Image 3** (the Pest Control category search page) — notice products like *1,000 Encarsia Formosa* (rating 3.8, only 3 reviews) and *Dalotia coriaria* (3.6 rating, 12 reviews) with $60 price point. These would score high on your system — small sellers, niche biological pest control, low review counts, reasonable prices. That's exactly the kind of product your pipeline should surface.

Want me to build this scraper + scoring engine as actual working code?