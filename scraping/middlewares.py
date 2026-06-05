import random
from datetime import datetime


class HeaderRotationMiddleware:
    """
    Picks a random header template per request.
    Alternates between group 1 (even weekdays) and group 2 (odd weekdays).
    Never overrides User-Agent (handled by RandomUserAgentMiddleware) or Cookie.
    """

    def __init__(self, group1, group2):
        self.group1 = group1
        self.group2 = group2

    @classmethod
    def from_crawler(cls, crawler):
        group1 = crawler.settings.get('REAL_BROWSER_HEADERS_GROUP_1')
        group2 = crawler.settings.get('REAL_BROWSER_HEADERS_GROUP_2')
        return cls(group1, group2)

    def process_request(self, request, spider):
        day = datetime.now().weekday()  # Monday=0, Sunday=6
        pool = self.group1 if day % 2 == 0 else self.group2
        headers = random.choice(pool).copy()
        for name, value in headers.items():
            if name.lower() not in ('user-agent', 'cookie'):
                request.headers.setdefault(name, value)


class ProxyMiddleware:
    """Single rotating proxy — picks a random port per request."""

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.settings)

    def __init__(self, settings):
        self.username = settings.get('PROXY_USER')
        self.password = settings.get('PROXY_PASSWORD')
        self.url = settings.get('PROXY_URL')
        self.ports = settings.get('PROXY_PORTS')

    def process_request(self, request, spider):
        port = random.choice(self.ports)
        request.meta['proxy'] = f'https://{self.username}:{self.password}@{self.url}:{port}'


class RandomizedProxyMiddleware:
    """
    Sticky-session proxy pool — shuffles sessions and cycles through them.
    Prevents the same proxy being reused too frequently.
    """

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.settings)

    def __init__(self, settings):
        self.username = settings.get('PROXY_USER')
        self.password = settings.get('PROXY_PASSWORD')
        self.url = settings.get('PROXY_URL')
        self.ports = settings.get('PROXY_PORTS')
        self.sessions = [
            f'https://{self.username}:{self.password}@{self.url}:{p}'
            for p in self.ports
        ]
        self._reset_pool()

    def _reset_pool(self):
        self.pool = self.sessions.copy()
        random.shuffle(self.pool)

    def process_request(self, request, spider):
        if not self.pool:
            self._reset_pool()
        request.meta['proxy'] = self.pool.pop()
