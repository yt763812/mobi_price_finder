"""Category seeds are configurable: unknown sites are deliberately not guessed."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Shop:
    slug: str
    domain: str
    categories: dict[str, str]

SHOPS = {
    'a1': Shop('a1', 'shop.a1.by', {
        'smartphones': 'https://shop.a1.by/catalog/phones/smartphones',
        'tablets': 'https://shop.a1.by/catalog/tablets-and-books/tablets',
        'watches': 'https://shop.a1.by/catalog/smart-watch',
        'headphones': 'https://shop.a1.by/catalog/accessories/headphones',
    }),
    'mts': Shop('mts', 'shop.mts.by', {
        'smartphones': 'https://shop.mts.by/phones/',
        'tablets': 'https://shop.mts.by/tabletpc/tablet/',
        'laptops': 'https://shop.mts.by/tabletpc/laptops/',
        'watches': 'https://shop.mts.by/gadgets/smart-watch/',
        'headphones': 'https://shop.mts.by/gadgets/headphones/',
    }),
    'dns': Shop('dns', 'dns-shop.by', {}),
    'nsv': Shop('nsv', 'nsv.by', {}),
}
