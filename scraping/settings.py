import os
import csv
from dotenv import load_dotenv
from header_templates import (
    REAL_BROWSER_HEADERS_GROUP_1,
    REAL_BROWSER_HEADERS_GROUP_2,
)

# ---------------------------------------------------------------------------
# Load DB credentials from .secrets/admin_creds.env
# ---------------------------------------------------------------------------
_secrets_path = os.path.join(
    os.path.dirname(__file__), '..', '.secrets', 'admin_creds.env'
)
load_dotenv(dotenv_path=os.path.abspath(_secrets_path))

POSTGRES_HOST     = os.getenv('POSTGRES_HOST', 'localhost')
POSTGRES_DATABASE = os.getenv('POSTGRES_ECOM_DATABASE_NAME', 'ecom_intel')
POSTGRES_USERNAME = os.getenv('POSTGRES_ADMIN_USERNAME')
POSTGRES_PASSWORD = os.getenv('POSTGRES_ADMIN_PASSWORD')
POSTGRES_PORT     = int(os.getenv('POSTGRES_PORT', 5432))

# ---------------------------------------------------------------------------
# Target categories — loaded from docs/chosen_categories.csv
# First line ("Amazon US") is a header and is skipped.
# ---------------------------------------------------------------------------
_categories_path = os.path.join(
    os.path.dirname(__file__), '..', 'docs', 'chosen_categories.csv'
)

TARGET_CATEGORIES: list[str] = []
with open(os.path.abspath(_categories_path), newline='', encoding='utf-8') as f:
    reader = csv.reader(f)
    for i, row in enumerate(reader):
        if i == 0:          # skip "Amazon US" header
            continue
        if row and row[0].strip():
            TARGET_CATEGORIES.append(row[0].strip())

# ---------------------------------------------------------------------------
# Proxy (Oxylabs) — disabled by default; enable in spider custom_settings
# ---------------------------------------------------------------------------
PROXY_USER  = os.getenv('PROXY_USER', '')
PROXY_PASSWORD = os.getenv('PROXY_PASSWORD', '')
PROXY_URL   = os.getenv('PROXY_URL', 'us-pr.oxylabs.io')
PROXY_PORTS = [str(p) for p in range(20001, 20009)]   # sticky sessions

# ---------------------------------------------------------------------------
# Scrapy core
# ---------------------------------------------------------------------------
BOT_NAME = "ecom-intelligence"
SPIDER_MODULES = ["spiders"]
NEWSPIDER_MODULE = "spiders"

ROBOTSTXT_OBEY = False          # Amazon blocks scraping in robots.txt
COOKIES_ENABLED = True
REFERER_ENABLED = True

TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"

# ---------------------------------------------------------------------------
# Downloader middlewares (global defaults — spiders may override)
# ---------------------------------------------------------------------------
DOWNLOADER_MIDDLEWARES = {
    # Disable Scrapy's default UA middleware; use random-UA library instead
    'scrapy.downloadermiddlewares.useragent.UserAgentMiddleware': None,
    'scrapy_user_agents.middlewares.RandomUserAgentMiddleware': 400,

    # Disable default headers; HeaderRotationMiddleware takes full control
    'scrapy.downloadermiddlewares.defaultheaders.DefaultHeadersMiddleware': None,
    'middlewares.HeaderRotationMiddleware': 500,

    # Proxy middlewares — commented out; enable per-spider when needed
    # 'middlewares.RandomizedProxyMiddleware': 100,
    # 'scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware': 110,

    'scrapy.downloadermiddlewares.cookies.CookiesMiddleware': 700,
}

SPIDER_MIDDLEWARES = {
    'scrapy.spidermiddlewares.referer.RefererMiddleware': 800,
}

# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------
RETRY_ENABLED = True
RETRY_TIMES = 3
RETRY_DELAY = 2
RETRY_HTTP_CODES = [500, 502, 503, 504, 408, 429]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_scraping_dir = os.path.dirname(os.path.abspath(__file__))
LOG_DIR      = os.path.join(_scraping_dir, 'logs')       # scraping/logs/
HTML_DEBUG_DIR = os.path.join(_scraping_dir, 'html_debug')  # scraping/html_debug/
LOG_ROTATING_MAX_BYTES = 5 * 1024 * 1024
LOG_ROTATING_BACKUP_COUNT = 5
LOG_FORMAT = '%(asctime)s [%(levelname)s] [%(name)s:%(lineno)d]: %(message)s'

# ---------------------------------------------------------------------------
# Header template groups (used by HeaderRotationMiddleware)
# ---------------------------------------------------------------------------
REAL_BROWSER_HEADERS_GROUP_1 = REAL_BROWSER_HEADERS_GROUP_1
REAL_BROWSER_HEADERS_GROUP_2 = REAL_BROWSER_HEADERS_GROUP_2
