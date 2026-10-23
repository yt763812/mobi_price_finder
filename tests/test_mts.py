import unittest
from scrapers.mts import inspect_mts

URL = 'https://shop.mts.by/phones/'

def card(prices, ga='3199', name='Apple iPhone 15 Pro Max 256GB by ELNU Грейд B', sku='1913531'):
    return f'''<article class="card-product" data-ga-item-id="{sku}" data-ga-item-name="{name}" data-ga-item-price="{ga}">
    <p class="card-product__title">{name}</p><div class="card-product__prices">{prices}</div>
    <button class="card-product__button">Доставим под заказ</button>
    <a class="card-product__link" href="/phones/iphone-test/"></a></article>'''

class TestMTS(unittest.TestCase):
    def test_from_price_skipped(self):
        html = card('<p class="card-product__price card-product__price--red">от <span class="num">147.<small>78</small></span> руб./мес.</p><p class="card-product__price">от <span class="num">3099</span> руб.</p>')
        products, issues, count = inspect_mts(html, URL, 'smartphones')
        self.assertEqual((count, len(products)), (1, 0))
        self.assertIn('FROM price 3099', issues[0][1])
    def test_exact_verified_price(self):
        html = card('<p class="card-product__price"> <span class="num">3199</span> руб.</p>')
        products, issues, count = inspect_mts(html, URL, 'smartphones')
        self.assertEqual((count, len(products), len(issues)), (1, 1, 0))
        self.assertEqual(str(products[0].price), '3199')
        self.assertEqual(products[0].sku, '1913531')
    def test_conflicting_price_skipped(self):
        html = card('<p class="card-product__price"> <span class="num">3099</span> руб.</p>')
        products, issues, _ = inspect_mts(html, URL, 'smartphones')
        self.assertEqual(products, [])
        self.assertIn('conflicting prices', issues[0][1])

if __name__ == '__main__':
    unittest.main()
