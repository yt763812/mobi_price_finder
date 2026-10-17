
import re

COLORS = {
    "black": ["black", "blk", "черный", "чёрный"],
    "white": ["white", "белый", "белая"],
    "blue": ["blue", "синий", "синяя"],
    "green": ["green", "зеленый", "зелёный"],
    "orange": ["orange", "оранжевый"],
    "silver": ["silver", "серебристый"],
    "burgundy": ["burgundy", "бургунди"],
    "purple": ["purple", "фиолетовый"],
}

def normalize(text):
    text = text.lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def detect_color(text):
    text = normalize(text)

    for color, variants in COLORS.items():
        for variant in variants:
            if re.search(
                rf"(?<!\w){re.escape(variant)}(?!\w)",
                text
            ):
                return color

    return None


def detect_storage(text):
    text = normalize(text)

    match = re.search(
        r"(?<!\d)(128|256|512|1024)\s*(gb|гб|g|tb|тб)?(?!\w)",
        text
    )

    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)

    if unit in ("tb", "тб"):
        return value * 1024

    return value


def detect_iphone(text):
    text = normalize(text)

    match = re.search(
        r"(?:iphone|айфон|айфона)\s*(\d{1,2})"
        r"(?:\s*(pro max|про макс|promax|pro|про|plus|плюс))?",
        text
    )

    if not match:
        return None

    number = match.group(1)
    variant = match.group(2)

    variants = {
        "pro max": "Pro Max",
        "про макс": "Pro Max",
        "promax": "Pro Max",
        "pro": "Pro",
        "про": "Pro",
        "plus": "Plus",
        "плюс": "Plus",
    }

    suffix = variants.get(variant, "")
    return f"iPhone {number} {suffix}".strip()


def parse_product(text):
    model = detect_iphone(text)
    storage = detect_storage(text)
    color = detect_color(text)

    return {
        "original": text,
        "brand": "Apple" if model else None,
        "model": model,
        "storage_gb": storage,
        "color": color,
    }
