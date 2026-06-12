-- Seed transformed.marketplaces
-- Run once after DDL is applied.

INSERT INTO transformed.marketplaces (marketplace_id, platform, country_code, currency, domain, is_active)
VALUES
    ('amazon_us', 'amazon', 'US', 'USD', 'amazon.com',    TRUE),
    ('amazon_in', 'amazon', 'IN', 'INR', 'amazon.in',     FALSE),  -- future
    ('amazon_uk', 'amazon', 'GB', 'GBP', 'amazon.co.uk',  FALSE),  -- future
    ('amazon_ca', 'amazon', 'CA', 'CAD', 'amazon.ca',     FALSE)   -- future
ON CONFLICT (marketplace_id) DO NOTHING;
