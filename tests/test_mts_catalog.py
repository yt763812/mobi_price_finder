import unittest
from scrapers.mts_catalog import crawl_mts_catalog

HTML = '''<article class="card-product" data-ga-item-id="1913531" data-ga-item-name="Apple iPhone" data-ga-item-price="3199">
<p class="card-product__title">Apple iPhone 15 Pro Max Grade B</p>
<p class="card-product__price card-product__price--red">от <span class="num">147.<small>78</small></span> руб./мес.</p>
<p class="card-product__price">от <span class="num">3099</span> руб.</p>
<a class="card-product__link" href="/phones/iphone-15/"></a></article>'''
class Fetch:
    def __init__(self): self.calls=[]
    def get(self, url): self.calls.append(url); return HTML
class TestCatalogue(unittest.TestCase):
    def test_only_catalogue_request_and_listed_price(self):
        f=Fetch()
        result=crawl_mts_catalog(f,'https://shop.mts.by/phones/','smartphones',max_pages=2)
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(result.products[0].price,3099)
        self.assertTrue(result.products[0].approximate)
        self.assertEqual(result.products[0].analytics_price,'3199')
        self.assertFalse(result.incomplete)
    def test_avoids_installment(self):
        f=Fetch(); r=crawl_mts_catalog(f,'https://shop.mts.by/phones/','smartphones')
        self.assertEqual(str(r.products[0].price),'3099')
if __name__=='__main__': unittest.main()
