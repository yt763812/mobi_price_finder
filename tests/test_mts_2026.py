import unittest
from decimal import Decimal
from scrapers.mts_modern import discover_cards, detail_offer
from scrapers.mts_store_catalog_v4 import crawl_mts_catalog

BASE = 'https://shop.mts.by/phones/'
NAME = 'Смартфон Samsung Galaxy A57 (A576) 8/256GB серый'
LINK = '/phones/samsung/samsung-galaxy-a57-8-256gb-a576-gray/'
CATALOG = f'''<html><main><div class="goods-container"><div class="tile">
 <a href="{LINK}">{NAME}</a>
 <span>от 56.48 BYN/мес.</span><span>от 1219 BYN</span>
 <span>1899 BYN -35%</span><button>Купить</button>
 </div></div></main></html>'''
DETAIL = f'''<html><main><h1>{NAME}</h1><div>Код товара: 00-00010065</div>
 <section><div><b>Единый платеж</b></div><div><span>1219 BYN</span></div></section>
 <section>Настроить оплату частями 56.48 BYN/мес.</section></main></html>'''

class FakeFetcher:
    def get(self, url):
        if url == BASE: return CATALOG
        if url.endswith('gray/'): return DETAIL
        raise AssertionError(url)

class ModernTests(unittest.TestCase):
    def test_catalog_finds_product_not_nav(self):
        result = discover_cards(CATALOG,BASE)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].name,NAME)
    def test_single_payment_only(self):
        sku, amount, issue = detail_offer(DETAIL,BASE+LINK.lstrip('/'),NAME)
        self.assertIsNone(issue)
        self.assertEqual(sku,'00-00010065')
        self.assertEqual(amount,Decimal('1219'))
    def test_no_monthly_price_instead(self):
        bad = DETAIL.replace('<div><span>1219 BYN</span></div>', '<div><span>56.48 BYN/мес.</span></div>')
        self.assertIsNotNone(detail_offer(bad, BASE,NAME)[2])
    def test_crawl_modern(self):
        result = crawl_mts_catalog(FakeFetcher(), BASE, 'smartphones',max_pages=1)
        self.assertEqual(len(result.products),1)
        self.assertFalse(result.issues)
        self.assertEqual(result.products[0].price,Decimal('1219'))

if __name__ == '__main__': unittest.main()
