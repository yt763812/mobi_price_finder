"""Распознавание характеристик товаров для Mobi Price Finder (начальная версия)."""
import re


def normalize(text):
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


# Более специфичные оттенки идут раньше общих цветов.
COLORS = [
    ("deep_blue", ["deep blue", "глубокий синий", "темно-синий", "темный синий"]),
    ("sky_blue", ["sky blue", "light blue", "голубой", "ледяной голубой"]),
    ("cosmic_orange", ["cosmic orange", "космический оранжевый"]),
    ("natural_titanium", ["natural titanium", "природный титан"]),
    ("black", ["black", "blk", "черный"]),
    ("white", ["white", "белый", "белая"]),
    ("blue", ["blue", "синий", "синяя"]),
    ("green", ["green", "зеленый", "зеленая"]),
    ("orange", ["orange", "оранжевый"]),
    ("silver", ["silver", "серебристый"]),
    ("burgundy", ["burgundy", "бургунди"]),
    ("purple", ["purple", "фиолетовый", "сиреневый"]),
]

REGION_FLAGS = {
    "🇺🇸": "USA", "🇯🇵": "Japan", "🇭🇰": "Hong Kong",
    "🇨🇳": "China", "🇦🇪": "UAE", "🇪🇺": "EU",
    "🇨🇦": "Canada", "🇦🇺": "Australia", "🇬🇧": "UK",
    "🇸🇬": "Singapore", "🇮🇳": "India", "🇰🇷": "South Korea",
}
REGION_TEXT = [
    ("Hong Kong", ["hong kong", "гонконг", "гонконгская", "hk"]),
    ("USA", ["usa", "сша", "us"]),
    ("Japan", ["japan", "япония", "jp"]),
    ("China", ["china", "китай", "cn"]),
    ("UAE", ["uae", "оаэ"]),
    ("EU", ["eu", "европейская версия"]),
    ("Canada", ["canada", "ca"]),
    ("Australia", ["australia", "au"]),
]


def _contains(text, term):
    return bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text))


def detect_color(text):
    t = normalize(text)
    for canonical, variants in COLORS:
        if any(_contains(t, v) for v in variants):
            return canonical
    return None


def detect_storage(text):
    t = normalize(text)
    # Формат 8GB/256GB: первый показатель обычно RAM, второй — накопитель.
    pair = re.search(r"(?<!\d)(?:4|6|8|12|16|18|24|32|64)\s*(?:gb|гб|g)?\s*[/+]\s*(64|128|256|512|1024|2048)\s*(?:gb|гб|g)?(?!\d)", t)
    if pair:
        return int(pair.group(1))
    found = re.findall(r"(?<!\w)(\d{1,4})\s*(tb|тб|gb|гб|g)(?!\w)", t)
    if found:
        amount, unit = found[-1]
        size = int(amount) * (1024 if unit in ("tb", "тб") else 1)
        return size
    pair = re.search(r"(?<!\d)(?:4|6|8|12|16|18|24|32|64)\s*(?:gb|гб|g)?\s*[/+]\s*(64|128|256|512|1024|2048)\s*(?:gb|гб|g)?(?!\d)", t)
    if pair:
        return int(pair.group(1))
    # В некоторых прайсах единица измерения отсутствует.
    match = re.search(r"(?<!\d)(128|256|512|1024)(?!\d)", t)
    return int(match.group(1)) if match else None


def detect_iphone(text):
    t = normalize(text)
    m = re.search(
        r"(?:iphone|айфон|айфона)\s*(\d{1,2})"
        r"(?:\s*(pro\s*max|про\s*макс|promax|про|pro|plus|плюс))?",
        t,
    )
    if not m:
        return None
    suffix = (m.group(2) or "").replace(" ", "")
    labels = {"promax": "Pro Max", "промакс": "Pro Max",
              "pro": "Pro", "про": "Pro", "plus": "Plus", "плюс": "Plus"}
    return f"iPhone {m.group(1)}" + (f" {labels[suffix]}" if suffix in labels else "")


def detect_region(text):
    flags = [name for flag, name in REGION_FLAGS.items() if flag in text]
    if flags:
        return flags[0] if len(set(flags)) == 1 else None
    t = normalize(text)
    for name, variants in REGION_TEXT:
        if any(_contains(t, v) for v in variants):
            return name
    return None


def detect_sim(text):
    t = normalize(text)
    # Сначала наиболее специфичные типы.
    if re.search(r"(?<!\w)(dual\s*esim|2\s*esim|две\s*esim)(?!\w)", t):
        return "dual_esim"
    if re.search(r"(?<!\w)(esim\s*\+\s*(?:sim|nano.?sim)|(?:sim|nano.?sim)\s*\+\s*esim)(?!\w)", t):
        return "sim_esim"
    if re.search(r"(?<!\w)(dual\s*sim|2\s*sim|две\s*sim)(?!\w)", t):
        return "dual_sim"
    if _contains(t, "esim"):
        return "esim"  # eSIM упомянута; число SIM не подтверждено.
    return None


def detect_condition(text):
    t = normalize(text)
    markers = []
    if re.search(r"\b(актив|активирован|activated)\b", t):
        markers.append("activated")
    if re.search(r"\b(запечатан|запечатанный|sealed)\b", t):
        markers.append("sealed")
    if re.search(r"\b(восстановлен|refurbished|refurb)\b", t):
        markers.append("refurbished")
    if re.search(r"\b(б/у|бу|used)\b", t):
        markers.append("used")
    return markers




# Универсальное определение моделей. Более конкретные шаблоны идут первыми.
DEVICE_PATTERNS = [
    ("Samsung", r"(?:samsung\s*)?(?:galaxy\s*)?(s(?:2[0-9]|3[0-9])\s*(?:ultra|plus|\+|fe)?)", "Galaxy"),
    ("Samsung", r"(?:samsung\s*)?(?:galaxy\s*)?(a[0-9]{2}\s*(?:5g)?)", "Galaxy"),
    ("Samsung", r"(?:samsung\s*)?(?:galaxy\s*)?(z\s*(?:fold|flip)\s*[0-9]+)", "Galaxy"),
    ("Google", r"(?:google\s*)?(pixel\s*[0-9]+(?:\s*(?:pro\s*xl|pro|xl|a))?)", ""),
    ("Xiaomi", r"(?:xiaomi\s*)?((?:redmi\s*)?note\s*[0-9]+\s*(?:pro\s*\+?|pro|plus|5g)?)", ""),
    ("Xiaomi", r"(?:xiaomi\s*)([0-9]{1,2}\s*(?:ultra|pro|t\s*pro|t)?)", ""),
    ("POCO", r"(?:poco\s*)([xfm][0-9]+\s*(?:pro|ultra)?)", ""),
    ("HONOR", r"(?:honor\s*)(magic\s*[0-9]+\s*(?:pro|lite|ultimate)?)", ""),
    ("HONOR", r"(?:honor\s*)([0-9]{2,3}\s*(?:pro|lite)?)", ""),
]

# Дополнительные семейства: только при явно указанном бренде.
# Не распознаём аксессуары по упоминанию совместимого устройства.
EXTRA_FAMILIES = [
    ("Huawei", r"huawei\s+(mate\s*(?:xt|x[0-9]+|[0-9]+)(?:\s*(?:pro\s*max|pro|rs|x|s))?|pura\s*[0-9]+(?:\s*(?:ultra|pro|plus))?|nova\s*[0-9]+(?:\s*(?:pro|ultra|se))?)"),
    ("Sony", r"sony\s+(xperia\s*(?:1|5|10)\s*(?:viii|vii|vi|v|iv|iii|ii|i|[0-9]+))"),
    ("Xiaomi", r"xiaomi\s+(mix\s*(?:flip|fold)\s*[0-9]*|redmi\s*[0-9]+[a-z]?(?:\s*(?:4g|5g|pro|plus))?|pad\s*[0-9]+(?:\s*pro)?)"),
    ("Samsung", r"samsung\s+(?:galaxy\s+)?(a[0-9]{2}[a-z]?(?:\s*(?:4g|5g))?|tab\s+[a-z][0-9]+(?:\s*(?:ultra|plus|fe))?)"),
    ("HONOR", r"honor\s+(magic\s*(?:v[0-9]+|[0-9]+)(?:\s*(?:pro|lite|ultimate))?|[0-9]{2,3}[a-z]?(?:\s*(?:pro|lite|5g))?)"),
    ("Oukitel", r"oukitel\s+(?:wp|rt|g|c|k)[0-9]+[a-z]?(?:\s*(?:pro|s|ultra))?"),
    ("Ulefone", r"ulefone\s+(?:armor|note|power)\s*[0-9]+[a-z]?(?:\s*(?:pro|ultra))?"),
    ("8849", r"8849\s+(?:hike|shark|tank)\s*(?:mini\s*)?[0-9]+[a-z]?(?:\s*(?:pro|ultra))?"),
    ("JBL", r"jbl\s+(?:boombox|charge|flip|xtreme|partybox|go|clip)\s*[0-9]+(?:\s*(?:essential|plus))?"),
    ("Canon", r"canon\s+(?:eos\s+)?(?:r[0-9]+|c[0-9]+|[0-9]+d)\b"),
    ("Nikon", r"nikon\s+(?:coolpix\s+)?(?:z\s*[0-9]+|p[0-9]+|d[0-9]+)\b"),
]

def detect_extra_model(text):
    t = normalize(text)
    if re.search(r"\b(?:cover|case|чехол|protector|adapter|battery grip|mount adapter|for xperia|для xperia)\b", t):
        return None, None
    for brand, pattern in EXTRA_FAMILIES:
        m = re.search(r"(?<!\w)" + pattern + r"(?!\w)", t, re.I)
        if m:
            return brand, re.sub(r"\s+", " ", m.group(0)).upper()
    return None, None

def detect_other_model(text):
    t = normalize(text)
    for brand, pattern, prefix in DEVICE_PATTERNS:
        match = re.search(r"(?<!\w)" + pattern + r"(?!\w)", t, re.I)
        if match:
            # Не распознаём сокращения вроде S26 без названия бренда:
            # иначе можно ошибочно принять артикул за телефон.
            if brand == "Samsung" and not re.search(r"\b(samsung|galaxy|самсунг)\b", t):
                continue
            if brand == "Google" and not re.search(r"\b(google|pixel|гугл)\b", t):
                continue
            if brand == "Xiaomi" and not re.search(r"\b(xiaomi|redmi|сяоми|ксиаоми)\b", t):
                continue
            name = re.sub(r"\s+", " ", match.group(1).upper()).strip()
            return brand, (prefix + " " + name).strip()
    return detect_extra_model(text)

def detect_ram(text):
    t = normalize(text)
    pair = re.search(r"(?<!\d)(4|6|8|12|16|18|24|32|64)\s*(?:gb|гб|g)?\s*[/+]\s*(64|128|256|512|1024|2048)\s*(?:gb|гб|g)?(?!\d)", t)
    if pair:
        return int(pair.group(1))
    match = re.search(r"(?<!\w)(?:ram|озу|оперативная память)\s*[:\-]?\s*(4|6|8|12|16|18|24|32|64)\s*(?:gb|гб|g)?(?!\w)", t)
    return int(match.group(1)) if match else None


def has_unmodeled_bundle(text):
    """Fail closed: do not merge different bundles/accessories as one model."""
    t = normalize(text)
    return bool(re.search(
        r"(?<!\w)(?:body\s*only|body|lens\s*kit|kit\s*lens|"
        r"double\s*kit|twin\s*kit|kit|bundle|комплект|набор|"
        r"с\s*объективом|без\s*объектива)(?!\w)", t
    ))


def parse_product(text):
    model = detect_iphone(text)
    other_brand, other_model = detect_other_model(text) if not model else (None, None)
    if not model:
        model = other_model
    # A bundle changes the commercial SKU. Until it is modeled explicitly,
    # store it by full original name rather than a lossy base-model signature.
    if model and has_unmodeled_bundle(text):
        model = None
    region = detect_region(text)
    sim = detect_sim(text)
    warnings = []
    if model and model.startswith("iPhone ") and region == "USA" and sim in ("dual_sim", "sim_esim"):
        number = int(re.search(r"iPhone (\d+)", model).group(1))
        if number >= 14:
            warnings.append("Возможное противоречие: USA и физическая SIM — проверить версию iPhone")
    flags_present = [flag for flag in REGION_FLAGS if flag in text]
    if len(flags_present) > 1 and len({REGION_FLAGS[f] for f in flags_present}) > 1:
        warnings.append("Указано несколько разных флагов региона — уточнить")
    if region and flags_present and any(REGION_FLAGS[f] != region for f in flags_present):
        warnings.append("Флаг и текстовый регион расходятся — уточнить")
    return {
        "original": text,
        "brand": "Apple" if detect_iphone(text) else other_brand,
        "model": model,
        "storage_gb": detect_storage(text),
        "ram_gb": detect_ram(text),
        "color": detect_color(text),
        "region": region,
        "sim": sim,
        "condition": detect_condition(text),
        "warnings": warnings,
    }
