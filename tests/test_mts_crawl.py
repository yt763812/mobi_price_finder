import unittest
from scrapers.mts_crawl import crawl_mts, page_number

BASE='https://shop.mts.by/phones/'

def card(sku):
    return ('<article class="card-product" data-ga-item-id="'+sku+'" '
            'data-ga-item-name="Phone '+sku+'"><p class="card-product__title">Phone '+sku+'</p>'
            '<a class="card-product__link" href="/phones/item-'+sku+'/">Details</a></article>')

def detail(sku):
    return ('<script type="application/ld+json">{"@type":"Product","name":"Phone '+sku+'",'
            '"offers":{"@type":"Offer","price":"200", "priceCurrency":"BYN"}}</script>')

class Fetcher:
    def __init__(self, files): self.files=files; self.calls=[]
    def get(self, url):
        self.calls.append(url)
        return self.files[url]

class TestCrawl(unittest.TestCase):
    def test_pagination_and_prices(self):
        pages={BASE: card('1')+'<a href="?page=2">Next</a>',
               BASE+'?page=2':card('2'),
               'https://shop.mts.by/phones/item-1/':detail('1'),
               'https://shop.mts.by/phones/item-2/':detail('2')}
        result=crawl_mts(Fetcher(pages), BASE, 'smartphones')
        self.assertEqual((result.pages, len(result.products), len(result.issues)), (2,2,0))
        self.assertFalse(result.incomplete)
    def test_page_limit_sets_incomplete(self):
        pages={BASE:card('1')+'<a href="?page=2">Next</a>',
               'https://shop.mts.by/phones/item-1/':detail('1')}
        result=crawl_mts(Fetcher(pages),BASE,'smartphones',max_pages=1)
        self.assertTrue(result.incomplete)
    def test_reject_external_pagination(self):
        self.assertIsNone(page_number('https://evil.test/phones/?page=2',BASE))
        self.assertIsNone(page_number('https://shop.mts.by/other/?page=2',BASE))

if __name__=='__main__': unittest.main()
