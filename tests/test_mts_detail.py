import unittest
from decimal import Decimal
from scrapers.mts_detail import detail_price

URL='https://shop.mts.by/phones/example/'
NAME='Apple iPhone 15 Pro Max 256GB by ELNU Грейд B (натуральный титан)'

def html(name=NAME, offer='"price": "3199.00", "priceCurrency": "BYN"'):
    return '<script type="application/ld+json">'+ '{"@context":"https://schema.org","@type":"Product","name":'+__import__('json').dumps(name,ensure_ascii=False)+',"offers":{"@type":"Offer",'+offer+'}}</script>'

class TestMTSDetail(unittest.TestCase):
    def test_exact_product_offer(self):
        p, error=detail_price(html(), URL, NAME, '1913531', 'smartphones')
        self.assertIsNone(error)
        self.assertEqual(p.price, Decimal('3199.00'))
        self.assertEqual(p.sku, '1913531')
    def test_wrong_product_name(self):
        p, error=detail_price(html('Different phone'), URL, NAME, '1913531', 'smartphones')
        self.assertIsNone(p)
    def test_reject_installment_without_offer_price(self):
        p, error=detail_price(html(offer='"lowPrice":"3099", "priceCurrency":"BYN"'), URL, NAME, '1913531', 'smartphones')
        self.assertIsNone(p)
    def test_reject_bad_currency(self):
        p, error=detail_price(html(offer='"price":"3199", "priceCurrency":"USD"'), URL, NAME, '1913531', 'smartphones')
        self.assertIsNone(p)

if __name__ == '__main__': unittest.main()
