import unittest
from scrapers.core import parse_products

class ParserTest(unittest.TestCase):
    def test_jsonld_product(self):
        html = '''<script type="application/ld+json">{"@type":"Product","sku":"123",
        "name":"Phone 256 GB","url":"/product/123", "offers":{"@type":"Offer",
        "price":"1299.00","priceCurrency":"BYN","availability":"https://schema.org/InStock"}}</script>'''
        result = parse_products(html, 'https://shop.mts.by/phones/', 'mts', 'smartphones')
        self.assertEqual(len(result), 1)
        self.assertEqual(str(result[0].price), '1299.00')
        self.assertEqual(result[0].availability, 'InStock')
    def test_no_installment_without_full_price(self):
        html = '<html>Платеж в рассрочку 29 BYN/мес</html>'
        self.assertEqual(parse_products(html, 'https://shop.mts.by/phones/', 'mts','smartphones'), [])
    def test_foreign_product_link_rejected(self):
        html = '''<script type="application/ld+json">{"@type":"Product","name":"X","url":"https://evil.test/",
        "offers":{"price":10,"priceCurrency":"BYN"}}</script>'''
        self.assertEqual(parse_products(html, 'https://shop.mts.by/phones/', 'mts','smartphones'), [])
if __name__ == '__main__':
    unittest.main()
