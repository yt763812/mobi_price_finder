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
    found = re.findall(r"(?<!\w)(\d{1,4})\s*(tb|тб|gb|гб|g)(?!\w)", t)
    if found:
        amount, unit = found[-1]
        size = int(amount) * (1024 if unit in ("tb", "тб") else 1)
        return size
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


def parse_product(text):
    model = detect_iphone(text)
    region = detect_region(text)
    sim = detect_sim(text)
    warnings = []
    if model and region == "USA" and sim in ("dual_sim", "sim_esim"):
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
        "brand": "Apple" if model else None,
        "model": model,
        "storage_gb": detect_storage(text),
        "color": detect_color(text),
        "region": region,
        "sim": sim,
        "condition": detect_condition(text),
        "warnings": warnings,
    }
