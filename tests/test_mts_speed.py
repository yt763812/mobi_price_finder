import threading
import time
import unittest
from scrapers.mts_crawl import crawl_mts

BASE = 'https://shop.mts.by/phones/'

def card(i):
    return (f'<article class="card-product" data-ga-item-id="{i}" '
            f'data-ga-item-name="Phone {i}"><p class="card-product__title">Phone {i}</p>'
            f'<a class="card-product__link" href="/phones/item-{i}/"></a></article>')

def detail(i):
    return (f'<script type="application/ld+json">{{"@type":"Product",'
            f'"name":"Phone {i}","offers":{{"@type":"Offer",'
            f'"price":"200","priceCurrency":"BYN"}}}}</script>')

class Fetcher:
    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()
    def get(self, url):
        if url == BASE:
            return ''.join(card(i) for i in range(1, 7))
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            time.sleep(.02)
            return detail(int(url.rstrip('/').rsplit('-', 1)[-1]))
        finally:
            with self.lock:
                self.active -= 1

class TestSpeed(unittest.TestCase):
    def test_parallel_details_and_preserve_products(self):
        f = Fetcher()
        r = crawl_mts(f, BASE, 'smartphones', workers=3)
        self.assertEqual(len(r.products), 6)
        self.assertEqual(r.issues, [])
        self.assertEqual(f.maximum, 3)
    def test_worker_limit(self):
        with self.assertRaises(ValueError):
            crawl_mts(Fetcher(), BASE, 'smartphones', workers=4)

if __name__ == '__main__':
    unittest.main()
