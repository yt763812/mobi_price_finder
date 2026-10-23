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
    ("glacier", ["glacier", "глейшер", "ледниковый"]),
    ("starlight", ["starlight", "сияющая звезда"]),
    ("midnight", ["midnight", "полночь"]),
    ("graphite", ["graphite", "графит"]),
    ("gold", ["gold", "золотой", "золотистый"]),
    ("pink", ["pink", "розовый"]),
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
    ("Singapore", ["singapore", "sg"]),
    ("India", ["india", "in"]),
    ("South Korea", ["south korea", "kr"]),
    ("UK", ["united kingdom", "uk"]),
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
        if size in (32, 64, 128, 256, 512, 1024, 2048, 4096, 8192):
            return size
    pair = re.search(r"(?<!\d)(?:4|6|8|12|16|18|24|32|64)\s*(?:gb|гб|g)?\s*[/+]\s*(64|128|256|512|1024|2048)\s*(?:gb|гб|g)?(?!\d)", t)
    if pair:
        return int(pair.group(1))
    # В некоторых прайсах единица измерения отсутствует.
    match = re.search(r"(?<!\d)(128|256|512|1024)(?!\d)", t)
    return int(match.group(1)) if match else None


def detect_iphone(text):
    t = normalize(text)
    # iPhone Air is a distinct model family, not a generic two-word search.
    if re.search(r"(?<!\w)(?:iphone|айфон)\s+air(?!\w)", t):
        return "iPhone Air"
    m = re.search(
        r"(?:iphone|айфон|айфона)\s*(\d{1,2})"
        r"(?:\s*(pro\s*max|про\s*макс|promax|про|pro|plus|плюс|air))?",
        t,
    )
    if not m:
        if re.search(r"(?<!\w)(?:iphone|айфон)\s*air(?!\w)", t):
            return "iPhone Air"
        return None
    suffix = (m.group(2) or "").replace(" ", "")
    labels = {"promax": "Pro Max", "промакс": "Pro Max",
              "pro": "Pro", "про": "Pro", "plus": "Plus", "плюс": "Plus", "air": "Air"}
    return f"iPhone {m.group(1)}" + (f" {labels[suffix]}" if suffix in labels else "")


def detect_region(text):
    flags = [name for flag, name in REGION_FLAGS.items() if flag in text]
    flags += [country_from_flag(m.group(0)) for m in re.finditer(r"[\U0001F1E6-\U0001F1FF]{2}", text) if country_from_flag(m.group(0))]
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


# Product-role identification: compatibility mentions must not turn a case into a phone.
# Only explicit accessory nouns are used; unknown products remain searchable.
ACCESSORY_PATTERNS = [
    r"(?<!\w)(?:case|cases|cover|covers|чехол|чехлы|чехла|накладка|бампер|bumper)(?!\w)",
    r"(?<!\w)(?:screen\s*protector|glass\s*protector|tempered\s*glass|защитн\w*\s*стекл\w*|стекло|glass|пленка|плёнка)(?!\w)",
    r"(?<!\w)(?:charger|charging\s*adapter|power\s*adapter|зарядное\s*устройство|зарядка|адаптер\s*питания)(?!\w)",
    r"(?<!\w)(?:phone\s*holder|держатель\s*для\s*телефона|кабель|cable)(?!\w)",
    r"(?<!\w)(?:keyboard|клавиатура|smart\s*folio|folio\s*cover)(?!\w)",
]


# Compatibility references must not turn a component into a complete device.
COMPONENT_PATTERNS = [
    r"(?<!\w)(?:левое\s*ухо|правое\s*ухо|левый\s*наушник|правый\s*наушник|left\s*earbud|right\s*earbud|left\s*earpiece|right\s*earpiece)(?!\w)",
    r"(?<!\w)(?:зарядный\s*кейс|зарядный\s*футляр|charging\s*case|replacement\s*case)(?!\w)",
]
EXTRA_ACCESSORY_PATTERNS = [
    r"(?<!\w)(?:magsafe\s*battery|battery\s*pack|внешний\s*аккумулятор|пауэрбанк|power\s*bank)(?!\w)",
    r"(?<!\w)(?:защитное\s*стекло|glass\s*film|ремешок\s*для|strap\s*for|strap\s*для)(?!\w)",
]


def product_role(text):
    t = normalize(text)
    if any(re.search(p, t) for p in COMPONENT_PATTERNS):
        return 'component'
    if any(re.search(p, t) for p in ACCESSORY_PATTERNS + EXTRA_ACCESSORY_PATTERNS):
        return 'accessory'
    return 'device'


def is_accessory(text):
    return product_role(text) != 'device'


def detect_audio_watch_model(text):
    t = normalize(text)
    m = re.search(r'(?<!\w)air\s*pods\s*(pro|max|[1-9])(?:\s*([1-9]))?(?!\w)', t)
    if m:
        family = m.group(1)
        if family.isdigit():
            return 'AirPods', int(family)
        return 'AirPods ' + family.title(), int(m.group(2)) if m.group(2) else None
    m = re.search(r'(?<!\w)(?:apple\s*)?watch\s*(ultra|se|series|s)(?:\s*([0-9]{1,2}))?(?!\w)', t)
    if m:
        family = m.group(1)
        if family in ('series', 's'):
            return 'Apple Watch Series', int(m.group(2)) if m.group(2) else None
        return 'Apple Watch ' + family.upper().replace('ULTRA','Ultra'), int(m.group(2)) if m.group(2) else None
    return None, None


def detect_condition(text):
    t = normalize(text)
    markers = []
    if re.search(r"\b(актив|активирован|activated|activ)\b", t):
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
    ("Redmi", r"redmi\s+[0-9]+(?:\s*(?:pro\s*max|pro|plus|ultra))?"),
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

# Строгие шаблоны семейств: бренд/семейство должны явно присутствовать.
# Год выпуска, размер дисплея и чип не считаются моделью или памятью.
CATALOG_FAMILIES = [
    ("Apple", r"\b(?:apple\s+)?ipad\s+(?:pro|air|mini)\s*(?:11|13|6|7)?\b", "iPad"),
    ("Apple", r"\b(?:apple\s+)?ipad\s+(?:10|11)(?:\b|\.)", "iPad"),
    ("Apple", r"\b(?:apple\s+)?macbook\s+(?:air|pro|neo)\b", "MacBook"),
    ("Apple", r"\b(?:apple\s+)?mac\s*mini\b", "Mac mini"),
    ("Samsung", r"\bsamsung\s+(?:galaxy\s+)?(?:watch\s*(?:ultra|classic|[0-9]+)|buds\s*(?:[0-9]+(?:\s*pro)?|pro\s*[0-9]+|fe))\b", "Samsung"),
    ("Huawei", r"\bhuawei\s+(?:watch\s+(?:gt\s*\d+[a-z]?|fit\s*\d+[a-z]?|ultimate)|band\s*\d+|freebuds\s+\w+\s*\d*|freeclip\s*\d+)\b", "Huawei"),
    ("Huawei", r"\bhuawei\s+(?:matepad\s+(?:se\s*)?\d+(?:\.\d+)?|pura\s*\d+[a-z]?|nova\s*\d+[a-z]?(?:\s*(?:pro|ultra))?)\b", "Huawei"),
    ("Google", r"\bgoogle\s+pixel\s+(?:watch|buds)\s*(?:pro|a|\d+)?\b", "Google"),
    ("Xiaomi", r"\b(?:xiaomi\s+)?(?:poco\s+(?:pad|[cfmx]\s*\d+[a-z]?)(?:\s*(?:pro|ultra))?|redmi\s+(?:pad\s*\w+|a\s*\d+[a-z]?))\b", "Xiaomi"),
    ("HONOR", r"\bhonor\s+(?:honor\s+)?(?:x\s*\d+[a-z]?(?:\s*pro)?|magic\s*\w+|\d{2,3}[a-z]?)\b", "HONOR"),
    ("Nothing", r"\bnothing\s+(?:phone\s*\(?\d+[a-z]?\)?|ear\s*\(?[a-z0-9]+\)?)\b", "Nothing"),
    ("OnePlus", r"\boneplus\s+(?:nord\s+\w+|buds\s+\w+(?:\s*\d+)?|\d+[a-z]?)\b", "OnePlus"),
    ("Realme", r"\brealme\s+(?:gt\s+\w+|\d+\s*(?:pro|plus)?)\b", "Realme"),
]


def detect_catalog_model(text):
    t = normalize(text)
    # Never promote an accessory mentioning a compatible model to a device.
    if product_role(text) != 'device':
        return None, None
    for brand, pattern, _ in CATALOG_FAMILIES:
        m = re.search(pattern, t, re.I)
        if m:
            model = re.sub(r'\s+', ' ', m.group(0)).strip()
            if brand == 'Apple' and model.startswith('apple '):
                model = model[6:]
            if brand in ('Samsung', 'Huawei', 'Google', 'HONOR', 'Nothing', 'OnePlus', 'Realme'):
                model = re.sub(r'^(?:' + re.escape(brand) + r')\s+', '', model, flags=re.I)
            return brand, model
    return None, None


def detect_ram(text):
    t = normalize(text)
    # Memory pairs: 8/256, 8Gb 256Gb, 12GB+512GB.
    # Require storage capacity as second element to avoid CPU identifiers.
    size = r'(?:64|128|256|512|1024|2048)'
    ram = r'(?:4|6|8|12|16|18|24|32|48|64|96)'
    pair = re.search(r'(?<![\w.])(' + ram + r')\s*(?:gb|гб|g)?\s*[/+]\s*(' + size + r')\s*(?:gb|гб|g|tb|тб)?(?!\w)', t)
    if pair:
        return int(pair.group(1))
    # Explicit dual size, e.g. 8Gb 256Gb; avoid a lone 8GB in accessory names.
    pair = re.search(r'(?<![\w.])(' + ram + r')\s*(?:gb|гб)\s+(' + size + r')\s*(?:gb|гб|tb|тб)(?!\w)', t)
    if pair:
        return int(pair.group(1))
    match = re.search(r'(?<!\w)(?:ram|озу|оперативная память)\s*[:\-]?\s*(' + ram + r')\s*(?:gb|гб|g)?(?!\w)', t)
    return int(match.group(1)) if match else None


def has_unmodeled_bundle(text):
    """Fail closed: do not merge different bundles/accessories as one model."""
    t = normalize(text)
    return bool(re.search(
        r"(?<!\w)(?:body\s*only|body|lens\s*kit|kit\s*lens|"
        r"double\s*kit|twin\s*kit|kit|bundle|комплект|набор|"
        r"с\s*объективом|без\s*объектива)(?!\w)", t
    ))


def detect_sku(text):
    """Extract only explicitly bracketed product codes; no speculative SKU guesses."""
    matches = re.findall(r"\[([A-Za-z0-9][A-Za-z0-9 /._-]{3,35})\]", text)
    return matches[0].strip() if matches else None


# Brand/category headings are whole lines without prices; never guess from arbitrary prose.
SECTION_BRANDS = {'apple': 'Apple', 'iphone': 'Apple', 'samsung': 'Samsung',
                  'xiaomi': 'Xiaomi', 'redmi': 'Redmi', 'poco': 'POCO'}


def section_brand(text):
    cleaned = re.sub(r"[\s:—–\-]+$", "", str(text).strip()).casefold()
    return SECTION_BRANDS.get(cleaned)


def contextual_name(name, brand=None):
    """Canonicalize *only* recognizable abbreviated devices; leave unknown lines intact."""
    name = str(name).strip()
    if not name:
        return name
    t = normalize(name)
    # Any explicit brand takes precedence over a section/default supplier brand.
    if re.search(r"(?<!\w)(?:iphone|айфон|apple|samsung|galaxy|redmi|xiaomi|poco|pixel|google|honor|huawei)(?!\w)", t):
        # 'Apple 17 Pro' is itself an abbreviation, not an explicit iPhone.
        if re.match(r"^apple\s+\d{1,2}(?:\s|$)", t):
            return 'iPhone ' + re.sub(r'^apple\s+', '', name, flags=re.I)
        return name
    if brand == 'Apple':
        if re.match(r"^\d{1,2}\s*(?:pro\s*max|promax|pro|plus|mini|air)?(?:\s|$)", t):
            return 'iPhone ' + name
    elif brand == 'Samsung':
        if re.match(r"^(?:s(?:2[0-9]|3[0-9])|a\d{2}|z\s*(?:flip|fold)\s*\d+)(?:\b|\s)", t):
            return 'Samsung ' + name
    elif brand in ('Redmi', 'Xiaomi'):
        if re.match(r"^\d{1,2}(?:\s+(?:pro|ultra|plus|pro\s*max))?(?:\s|$)", t):
            return brand + ' ' + name
    return name


def country_from_flag(flag):
    """Decode ordinary Unicode regional-indicator flags without third-party data."""
    chars = list(flag)
    if len(chars) != 2 or not all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in chars):
        return None
    country = ''.join(chr(ord(c) - 0x1F1E6 + ord('A')) for c in chars)
    common = {'US': 'USA', 'JP': 'Japan', 'HK': 'Hong Kong', 'AE': 'UAE',
              'CN': 'China', 'GB': 'UK', 'AU': 'Australia', 'SG': 'Singapore',
              'IN': 'India', 'KR': 'South Korea', 'CA': 'Canada', 'EU': 'EU'}
    return common.get(country, country)


def parse_product(text):
    model = detect_iphone(text)
    audio_family, audio_generation = detect_audio_watch_model(text)
    if not model and audio_family:
        model = audio_family + (f" {audio_generation}" if audio_generation is not None else "")
    other_brand, other_model = detect_other_model(text) if not model else (None, None)
    if not model:
        model = other_model
    if not model:
        catalog_brand, catalog_model = detect_catalog_model(text)
        if catalog_model:
            other_brand, model = catalog_brand, catalog_model
    # A bundle changes the commercial SKU. Until it is modeled explicitly,
    # store it by full original name rather than a lossy base-model signature.
    if model and has_unmodeled_bundle(text):
        model = None
    region = detect_region(text)
    sim = detect_sim(text)
    warnings = []
    # Multiple explicit region codes may indicate conflicting supplier data.
    normalized = normalize(text)
    region_mentions = {name for name, aliases in REGION_TEXT
                       if any(_contains(normalized, alias) for alias in aliases)}
    if len(region_mentions) > 1:
        region = None
        warnings.append("Несколько регионов в одной строке — проверь вручную")
    if re.search(r"\b(?:\d+\s*(?:gb|гб|tb|тб))\s*[/+]\s*(?:\d+\s*(?:gb|гб|tb|тб))", normalized):
        # Different capacities may mean RAM/SSD; existing RAM detector handles
        # supported pairs. Never pretend the split is certain otherwise.
        if detect_ram(text) is None:
            warnings.append("Несколько объёмов памяти: уточни RAM и накопитель")
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
        "sku": detect_sku(text),
        "is_accessory": is_accessory(text),
        "product_role": product_role(text),
        "family": audio_family,
        "generation": audio_generation,
        "warnings": warnings,
    }
