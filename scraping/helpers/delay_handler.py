from datetime import datetime as dt
import math
from urllib.parse import urlparse


class DelayHandler:
    def __init__(self, initial_delay, max_none_counter, time_window, log, crawler):
        self.initial_delay = initial_delay
        self.delay = initial_delay
        self.max_none_counter = max_none_counter
        self.time_window = time_window
        self.none_response_timestamps = []
        self.none_counter = 0
        self.log = log
        self.crawler = crawler

    def _update_none_response_timestamps(self):
        current_time = dt.now()
        self.none_response_timestamps = [
            ts for ts in self.none_response_timestamps
            if (current_time - ts).total_seconds() <= self.time_window
        ]
        return current_time, len(self.none_response_timestamps)

    def _adjust_delay(self, new_delay, request=None):
        self.delay = new_delay
        slot_key = None
        if request and 'download_slot' in request.meta:
            slot_key = request.meta['download_slot']
        if not slot_key and request:
            slot_key = urlparse(request.url).netloc
        slots = self.crawler.engine.downloader.slots
        if slot_key in slots:
            slots[slot_key].delay = self.delay
            self.log(f"Delay adjusted to {self.delay}s on slot '{slot_key}'", level=30)
        else:
            self.log(f"Slot '{slot_key}' not found. Available: {list(slots.keys())}", level=40)

    def handle_none_response(self, failed_urls, item, response):
        current_time, none_count = self._update_none_response_timestamps()
        if item:
            failed_urls.append({
                'asin': item.get('asin'),
                'product_url': item.get('product_url'),
                'status_code': response.status,
                'error': 'Page not loading properly',
            })
        self.none_response_timestamps.append(current_time)
        self.none_counter = none_count + 1
        self.log(f"None responses in last {self.time_window}s: {self.none_counter}", 30)
        if self.none_counter >= self.max_none_counter:
            new_delay = round(math.sqrt(self.delay) + 1, 4) ** 2
        else:
            new_delay = self.delay + 1
        self._adjust_delay(new_delay, response.request)
        return True

    def handle_successful_response(self, response):
        current_time, none_count = self._update_none_response_timestamps()
        if not self.none_response_timestamps:
            new_delay = max(self.initial_delay, round(math.sqrt(self.delay) - 1, 4) ** 2)
            self._adjust_delay(new_delay, response.request)
        else:
            self.none_counter = none_count
        return False
