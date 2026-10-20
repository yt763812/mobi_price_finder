"""Mobi Price Finder — controlled text price-list imports into PostgreSQL."""
import html
import logging
import os
import re
import hashlib
from io import BytesIO
from datetime import datetime
import time
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)
from database import (
    add_supplier, get_supplier, get_supplier_offers, get_offers, search_offers, init_db, list_suppliers,
    save_price_list, save_excel_rows, update_supplier_settings, rename_supplier, delete_supplier,
    get_excel_profiles, save_excel_profiles, export_supplier_offers,
)
from product_parser import parse_product, section_brand, contextual_name
from excel_importer import (inspect_xlsx, extract_xlsx, MAX_FILE_BYTES,
                            sheet_columns, inspect_mapping, sheet_fingerprint)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _user_id(update):
    user = update.effective_user
    return str(user.id) if user else ""


def is_owner(update):
    owner_id = os.getenv("OWNER_TELEGRAM_ID", "").strip()
    return bool(owner_id and owner_id.isdigit() and _user_id(update) == owner_id)


def is_manager(update):
    # One or several IDs, separated by comma, semicolon or whitespace.
    raw_ids = os.getenv("MANAGER_TELEGRAM_IDS", "")
    manager_ids = set(re.findall(r"\d+", raw_ids))
    return bool(_user_id(update) and _user_id(update) in manager_ids)


def allowed(update):
    return is_owner(update) or is_manager(update)


async def authorize(update):
    if allowed(update):
        return True
    await update.effective_message.reply_text(
        "🔒 Доступ закрыт. Отправь /myid и передай Telegram ID владельцу бота "
        "для добавления в MANAGER_TELEGRAM_IDS."
    )
    return False


def _clean_price_name(value):
    # Remove decorative symbols, not letters or meaningful model attributes.
    value = re.sub(r"^[\s•▪▫◾🔹🔸✅☑️📱🎧⌚💻📦💎🔥⭐️✨➖➡️—–-\uFE0F]+", "", value)
    return value.strip(" \t|:—–-•")


def _supplier_default_brand(supplier_id):
    """Optional Apple-only supplier IDs, e.g. APPLE_ONLY_SUPPLIER_IDS=3,7."""
    ids = set(re.findall(r"\d+", os.getenv('APPLE_ONLY_SUPPLIER_IDS', '')))
    return 'Apple' if str(supplier_id) in ids else None


def multipart_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Закончить и проверить", callback_data="parts:finish")],
        [InlineKeyboardButton("❌ Отменить сбор", callback_data="parts:cancel")],
    ])


def multipart_start_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Загрузить прайс частями", callback_data="parts:start")],
    ])


def parse_price_list(text, initial_brand=None, return_brand=False):
    """Parse supplier text using an explicit terminal price, preserving headings.

    A bare final number is accepted only when the prefix looks like a product
    and the price is not confused with an isolated model number or quantity.
    """
    output = []
    pending = None
    active_brand = initial_brand
    number = r"(\d+(?:[.,]\d{1,2})?)"
    currency = r"(?:\$|USD|EUR|RUB|₽|€)?"
    trailing_note = r"(?:\s*\((?:e\s*sim|sim\s*\+\s*esim|dual\s*sim)\))?"
    # Miscellaneous availability notes follow the price in real supplier lists.
    last_note = r"(?:\s*(?:🔥|бронь|завтра|в наличии|резерв|\((?:актив(?:,\s*запак)?|бронь|резерв)\)))*"
    priced = re.compile(rf"^(.+?)\s*[-—–:]\s*[$€₽]?\s*{number}\s*{currency}{trailing_note}{last_note}\s*$", re.I)
    standalone = re.compile(rf"^[$€₽]?\s*{number}\s*{currency}{trailing_note}{last_note}\s*$", re.I)
    bare = re.compile(rf"^(.+?)\s+[$€₽]?\s*{number}\s*{currency}{trailing_note}{last_note}\s*$", re.I)
    section_labels = re.compile(r"^(?:iphone|iphones|apple|samsung|redmi|xiaomi|macbook|ipad|apple watch|dyson)\s*[:：]?\s*[\W_]*$", re.I)
    separators = re.compile(r"^[\W_]+$", re.UNICODE)

    def is_product_name(name, price, explicit=False):
        if not name or not any(ch.isalpha() for ch in name):
            return False
        if re.search(r"(?i)^(?:от\s+\d+\s*шт|доставка|оплата|заказ до|курс|скидка|отдаю с)\b", name):
            return False
        # A trailing generation number is not necessarily a price: "AirPods 4".
        if not explicit and float(price.replace(',', '.')) < 10:
            return False
        if not explicit and len(name.split()) < 2 and not re.search(r'[\u0400-\u04ff]', name):
            return False
        return True

    def clean_line(line):
        # Markdown emphasis in copied supplier messages is not product data.
        return html.unescape(line).replace('**', '').replace('`', '').replace('\u00a0', ' ').strip().strip('\\*_ ')

    for raw in text.splitlines():
        line = clean_line(raw)
        if not line:
            continue
        heading = section_brand(line)
        if heading:
            active_brand, pending = heading, None
            continue
        if section_labels.fullmatch(line):
            key = re.sub(r'[^a-z]', '', line.lower())
            if key in ('iphone', 'iphones', 'apple'):
                active_brand = 'Apple'
            elif key in ('redmi', 'xiaomi', 'samsung'):
                active_brand = key.capitalize()
            pending = None
            continue
        # A product-specific subheading is not a SKU.
        if re.match(r'^\d{1,2}\s+pro\s*/\s*pro\s*max\b', line, re.I):
            continue
        if separators.fullmatch(line) or line.startswith('@'):
            pending = None
            continue
        # Avoid incorrectly choosing between prices for separate regions.
        # Example: "🇭🇰/🇪🇺 1250/1270" needs a separate future workflow.
        if re.search(r"\b\d{2,6}\s*/\s*\d{2,6}\s*(?:🔥|бронь)?\s*$", line):
            pending = None
            continue
        match = priced.fullmatch(line)
        explicit = bool(match)
        if not match:
            match = bare.fullmatch(line)
        if match:
            name = _clean_price_name(match.group(1))
            price = match.group(2)
            if is_product_name(name, price, explicit):
                # Preserve eSIM when the supplier puts the note after the price.
                if re.search(r'\(e\s*sim\)', line, re.I) and not re.search(r'\besim\b', name, re.I):
                    name += ' eSIM'
                if re.search(r'\(актив(?:,\s*запак)?\)', line, re.I):
                    name += ' Активирован'
                # Unlabelled iPhone generations are frequent after non-iPhone
                # categories. Only infer Apple from strongly iPhone-shaped rows;
                # explicit Samsung/Redmi/Xiaomi context takes precedence.
                brand = active_brand
                if brand not in ('Samsung', 'Redmi', 'Xiaomi') and re.match(
                    r'^\d{2}(?:e|\s+(?:pro(?:\s+max)?|plus))?\s+'
                    r'(?:64|128|256|512|1\s*tb|2\s*tb)\b', name, re.I
                ):
                    brand = 'Apple'
                output.append((contextual_name(name, brand), price))
            pending = None
            continue
        match = standalone.fullmatch(line)
        if match and pending:
            name = _clean_price_name(pending)
            if is_product_name(name, match.group(1), True):
                output.append((contextual_name(name, active_brand), match.group(1)))
            pending = None
            continue
        # Do not take bare prices from paragraphs such as "+1%".
        pending = line if len(line) < 250 and not re.search(r'\bпо запросу\b', line, re.I) else None
    return (output, active_brand) if return_brand else output


def auto_draft_keyboard(draft):
    revision = draft['revision']
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Сохранить общий прайс", callback_data=f"draft:save:{revision}")],
        [InlineKeyboardButton("❌ Отменить сбор", callback_data=f"draft:cancel:{revision}")],
    ])


def append_to_auto_draft(context, supplier_id, incoming):
    """Reparse complete joined text so headings and broken line boundaries survive."""
    draft = context.user_data.get('auto_draft')
    if draft and draft['supplier_id'] != supplier_id:
        raise ValueError('Сначала сохрани или отмени черновик другого поставщика.')
    if draft is None:
        draft = {'supplier_id': supplier_id, 'messages': [], 'revision': 0, 'rows': []}
    if len(draft['messages']) >= 100 or sum(map(len, draft['messages'])) + len(incoming) > 800_000:
        raise ValueError('Достигнут лимит черновика. Сохрани его или отмени.')
    if incoming in draft['messages']:
        return draft, False
    messages = draft['messages'] + [incoming]
    joined = '\n'.join(messages)
    rows = parse_price_list(joined, _supplier_default_brand(supplier_id))
    if len(rows) > 25_000:
        raise ValueError('Слишком много товарных строк в черновике.')
    draft.update(messages=messages, rows=rows, revision=draft['revision'] + 1)
    context.user_data['auto_draft'] = draft
    context.user_data.pop('pending', None)
    context.user_data.pop('undecided_text', None)
    return draft, True


async def auto_draft_status(message, draft):
    await message.reply_text(
        f"📥 Черновик прайса: {len(draft['messages'])} сообщ.\n"
        f"Распознано строк с ценой: {len(draft['rows'])}.\n"
        "Можешь отправить следующие сообщения. Когда всё пришло, нажми «Сохранить общий прайс». "
        "До нажатия данные в базе не изменятся.",
        reply_markup=auto_draft_keyboard(draft),
    )


def mode_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Авто", callback_data="mode:auto"),
         InlineKeyboardButton("🔎 Поиск", callback_data="mode:search")],
        [InlineKeyboardButton("📥 Загрузка прайса", callback_data="mode:import")],
        [InlineKeyboardButton("👥 Поставщики", callback_data="nav:suppliers"),
         InlineKeyboardButton("⚙️ Настройки", callback_data="nav:settings")],
    ])


def decision_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Искать", callback_data="decide:search"),
         InlineKeyboardButton("📥 Это прайс", callback_data="decide:import")],
    ])


async def mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize(update):
        return
    current = context.user_data.get('mode', 'auto')
    labels = {'auto': 'Авто', 'search': 'Поиск', 'import': 'Загрузка прайса'}
    await update.effective_message.reply_text(
        f"Режим: {labels.get(current, 'Авто')}. Выбери нужный:",
        reply_markup=mode_keyboard(),
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mode(update, context)


def _price_import_time(value, short=False):
    if value is None:
        return 'нет данных'
    try:
        tz_name = os.getenv('PRICE_TIMEZONE', 'Europe/Moscow')
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = timezone.utc
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(tz).strftime('%d.%m' if short else '%d.%m.%Y в %H:%M')
    except (AttributeError, ValueError, TypeError):
        return 'нет данных'


def _supplier_title(supplier):
    return (f"{supplier['name']}\n"
            f"🕒 Последний прайс: {_price_import_time(supplier.get('last_price_import_at'))}")


def keyboard_suppliers():
    buttons = [[InlineKeyboardButton(
                    f"{s['name']} · {_price_import_time(s.get('last_price_import_at'), short=True)}",
                    callback_data=f"pick:{s['id']}")]
               for s in list_suppliers()[:75]]
    buttons.append([InlineKeyboardButton("➕ Новый поставщик", callback_data="supplier:new")])
    return InlineKeyboardMarkup(buttons)


def supplier_manage_keyboard(supplier, can_delete=True):
    sid = supplier['id']
    buttons = [[InlineKeyboardButton("✏️ Переименовать", callback_data=f"supplier:rename:{sid}")]]
    if can_delete:
        buttons.append([InlineKeyboardButton("🗑 Удалить", callback_data=f"supplier:delete:{sid}")])
    buttons += [
        [InlineKeyboardButton("📥 Выгрузить прайс (Excel)", callback_data=f"supplier:export:{sid}")],
        [InlineKeyboardButton("⚙️ Валюта и НДС", callback_data=f"supplier:settings:{sid}")],
        [InlineKeyboardButton("👥 Все поставщики", callback_data="nav:suppliers")],
    ]
    return InlineKeyboardMarkup(buttons)


def supplier_delete_keyboard(sid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить с товарами", callback_data=f"supplier:confirm_delete:{sid}")],
        [InlineKeyboardButton("↩️ Отмена", callback_data=f"supplier:cancel_delete:{sid}")],
    ])


def settings_keyboard(supplier):
    sid = supplier['id']
    buttons = [
        [InlineKeyboardButton(f"Валюта: {supplier['currency']}", callback_data=f"settings:currency:{sid}")],
        [InlineKeyboardButton(f"НДС: {supplier['tax_status']}", callback_data=f"settings:vat:{sid}")],
    ]
    return InlineKeyboardMarkup(buttons)


def confirmation_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Сохранить прайс", callback_data="import:save")],
        [InlineKeyboardButton("❌ Отменить", callback_data="import:cancel")],
    ])


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize(update):
        return
    await update.effective_message.reply_text(
        "👋 Mobi Price Finder\n\n/suppliers — выбрать/добавить поставщика\n"
        "/settings — валюта и НДС выбранного поставщика\n"
        "/prices — сохранённые цены выбранного поставщика\n"
        "/find iPhone 17 Pro 256GB Black — сравнить поставщиков\n"
        "/menu — меню и режим работы\n"
        "Просто напиши название товара — бот выполнит поиск.\n"
        "Прайсы с ценами бот предложит сохранить.\n"
        "Для длинного текстового прайса выбери 📚 Прайс частями.",
        reply_markup=mode_keyboard(),
    )


async def suppliers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize(update):
        return
    await update.effective_message.reply_text("Выбери поставщика (рядом дата последнего прайса):", reply_markup=keyboard_suppliers())


async def addsupplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize(update):
        return
    context.user_data['await_name'] = True
    await update.effective_message.reply_text("Введи название нового поставщика одним сообщением.")


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize(update):
        return
    sid = context.user_data.get('supplier_id')
    s = get_supplier(sid) if sid else None
    if not s:
        await update.effective_message.reply_text("Сначала выбери поставщика через /suppliers")
        return
    await update.effective_message.reply_text(
        f"⚙️ {s['name']}\nВалюта: {s['currency']}\nНДС: {s['tax_status']}",
        reply_markup=settings_keyboard(s),
    )


async def prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize(update):
        return
    sid = context.user_data.get('supplier_id')
    supplier = get_supplier(sid) if sid else None
    if supplier is None:
        await update.effective_message.reply_text(
            'Сначала выбери поставщика через /suppliers.'
        )
        return

    offers = get_supplier_offers(supplier['id'], limit=20)
    if not offers:
        await update.effective_message.reply_text(
            f"У поставщика «{supplier['name']}» пока нет сохранённых предложений."
        )
        return

    lines = [f"📦 {supplier['name']} — последние {len(offers)} предложений:"]
    for i, offer in enumerate(offers, 1):
        price = (f"{offer['price']} {offer['currency'] or 'валюта неизвестна'}"
                 if offer['price'] is not None else 'По запросу')
        fields = []
        if offer['region']:
            fields.append(f"регион: {offer['region']}")
        if offer['sim']:
            fields.append(f"SIM: {offer['sim']}")
        if offer['tax_status'] == 'with_vat':
            fields.append('с НДС')
        elif offer['tax_status'] == 'without_vat':
            fields.append('без НДС')
        freshness = 'актуально' if offer['is_fresh'] else 'срок актуальности истёк'
        date = offer['updated_at'].strftime('%d.%m.%Y %H:%M UTC')
        details = ' | '.join(fields)
        lines.append(
            f"\n{i}. {offer['original_name']}\n"
            f"💰 {price}\n"
            + (f"{details}\n" if details else '')
            + f"🕒 {date} · {freshness}"
        )

    # Telegram limits messages to 4096 characters.
    text = ''
    for line in lines:
        if len(text) + len(line) + 1 > 3500:
            if text:
                await update.effective_message.reply_text(text)
            text = ''
        text += ('\n' if text else '') + line
    if text:
        await update.effective_message.reply_text(text)


async def show_pending(message, context):
    pending = context.user_data.get('pending')
    if not pending:
        return
    s = get_supplier(context.user_data.get('supplier_id')) if context.user_data.get('supplier_id') else None
    if not s:
        await message.reply_text("Выбери поставщика для прайса:", reply_markup=keyboard_suppliers())
        return
    if s['currency'] not in ('USD', 'BYN', 'RUB', 'EUR'):
        await message.reply_text("⚠️ Сначала выбери валюту в /settings.")
        return
    if s['tax_status'] == 'unknown':
        await message.reply_text(
            f"⚠️ У поставщика {s['name']} не указан НДС. Открой /settings и установи его "
            "перед сохранением."
        )
        return
    parsed = [(parse_product(name), price) for name, price in pending]
    supported = parsed
    previews = [f"• {p['original']} — {v}" for p, v in supported[:3]]
    await message.reply_text(
        f"📋 Прайс: {len(parsed)} строк\nПоставщик: {s['name']}\n"
        f"Валюта: {s['currency']} | НДС: {s['tax_status']}\n"
        f"Строк для импорта: {len(supported)} (точные дубли названий свернём)\n"
        f"Нераспознанные модели сохраняются по исходному названию\n\n"
        + "\n".join(previews)
        + "\n\nСохранить?",
        reply_markup=confirmation_keyboard(),
    )



EXCEL_FIELDS = ('name', 'price', 'brand', 'color', 'region', 'category')
EXCEL_FIELD_NAMES = {'name': 'Название товара', 'price': 'Цена', 'brand': 'Бренд',
                     'color': 'Цвет', 'region': 'Регион', 'category': 'Категория'}


def excel_menu(stage):
    buttons = []
    for i, item in enumerate(stage['sheets']):
        mark = '✅' if item.get('enabled') else '⏭' if item.get('skipped') else '⚠️'
        buttons.append([InlineKeyboardButton(f'{mark} {item["sheet"][:40]}', callback_data=f'xm:sheet:{i}')])
    buttons.append([InlineKeyboardButton('✅ Предпросмотр и сохранение', callback_data='xm:review')])
    buttons.append([InlineKeyboardButton('❌ Отмена', callback_data='excel:cancel')])
    return InlineKeyboardMarkup(buttons)


def excel_sheet_menu(stage, index):
    item = stage['sheets'][index]
    mapping = item.get('mapping') or {}
    def letter(field):
        idx = mapping.get(field)
        return '-' if idx is None else __import__('openpyxl').utils.get_column_letter(idx+1)
    buttons = [[InlineKeyboardButton(f'Строка заголовков: {item.get("header_row", 0)}', callback_data=f'xm:head:{index}')]]
    for field in EXCEL_FIELDS:
        buttons.append([InlineKeyboardButton(f'{EXCEL_FIELD_NAMES[field]}: {letter(field)}', callback_data=f'xm:field:{index}:{field}')])
    buttons.extend([[InlineKeyboardButton('✅ Использовать лист', callback_data=f'xm:use:{index}')],
                    [InlineKeyboardButton('⏭ Пропустить лист', callback_data=f'xm:skip:{index}')],
                    [InlineKeyboardButton('↩️ К списку листов', callback_data='xm:list')]])
    return InlineKeyboardMarkup(buttons)


def staged_excel_ready(stage):
    return all(item.get('enabled') or item.get('skipped') for item in stage['sheets']) and any(
        item.get('enabled') for item in stage['sheets'])


async def excel_review(message, context):
    stage = context.user_data.get('excel_pending')
    if not stage or stage['expires'] < time.time():
        await message.reply_text('Настройка истекла. Пришли Excel ещё раз.')
        return
    lines = ['📊 Excel — проверка перед импортом', 'Поставщик: '+stage['supplier_name']]
    for item in stage['sheets']:
        if item.get('enabled'):
            lines.append(f'✅ {item["sheet"]}: {item["valid"]} строк')
            lines.extend('• '+v for v in item.get('samples', [])[:2])
            for parsed in item.get('parsed_samples', [])[:2]:
                display = lambda v: str(v) if v is not None else 'Не указан'
                lines.append('   ↳ Модель: '+display(parsed.get('model'))
                             +' | Память: '+(str(parsed['storage'])+' GB' if parsed.get('storage') is not None else 'Не указан'))
                lines.append('   ↳ Цвет: '+display(parsed.get('color'))
                             +' | SIM: '+display(parsed.get('sim'))
                             +' | Регион: '+display(parsed.get('region')))
                if parsed.get('warnings'):
                    lines.append('   ⚠️ '+'; '.join(parsed['warnings'])[:130])
            if item.get('uncertain'):
                lines.append(f'⚠️ Неоднозначные характеристики: {item["uncertain"]} строк(и). Исходные названия сохраняются.')
        elif item.get('skipped'):
            lines.append('⏭ '+item['sheet']+' — пропущен')
        else:
            lines.append('⚠️ '+item['sheet']+' — требует настройки')
    lines.append('Никакие товары ещё не сохранены. При подтверждении прежний прайс выбранного поставщика будет заменён.')
    keyboard = [[InlineKeyboardButton('🛠 Настроить листы', callback_data='xm:list')]]
    if staged_excel_ready(stage):
        keyboard.insert(0, [InlineKeyboardButton('✅ Сохранить прайс', callback_data='excel:save')])
    keyboard.append([InlineKeyboardButton('❌ Отменить', callback_data='excel:cancel')])
    await message.reply_text('\n'.join(lines)[:4000], reply_markup=InlineKeyboardMarkup(keyboard))


async def excel_manual_callback(query, context, action):
    stage = context.user_data.get('excel_pending')
    if not stage or stage['expires'] < time.time():
        await query.message.reply_text('Настройка Excel истекла. Загрузи файл ещё раз.')
        return
    parts = action.split(':')
    op = parts[1]
    if op == 'review':
        await excel_review(query.message, context)
        return
    if op == 'list':
        await query.message.reply_text('Выбери лист для настройки. ✅ — включён, ⏭ — пропущен, ⚠️ — требует настройки.', reply_markup=excel_menu(stage))
        return
    try:
        idx = int(parts[2]); item = stage['sheets'][idx]
    except (IndexError, ValueError):
        return
    if op == 'sheet':
        await query.message.reply_text(f'Лист «{item["sheet"]}». Выбери заголовки и колонки, затем нажми «Использовать лист».', reply_markup=excel_sheet_menu(stage, idx))
    elif op == 'head':
        choices = [[InlineKeyboardButton('Без заголовков (0)', callback_data=f'xm:sethead:{idx}:0')]]
        choices += [[InlineKeyboardButton(f'Строка {n}', callback_data=f'xm:sethead:{idx}:{n}')] for n in range(1, 21)]
        await query.message.reply_text('Выбери номер строки с заголовками. 0 — данные начинаются с первой строки.', reply_markup=InlineKeyboardMarkup(choices))
    elif op == 'sethead':
        item['header_row'] = int(parts[3]); item['enabled'] = False; item['skipped'] = False
        await query.message.reply_text('Строка установлена.', reply_markup=excel_sheet_menu(stage, idx))
    elif op == 'field':
        field = parts[3]
        if field not in EXCEL_FIELDS:
            return
        columns = sheet_columns(stage['data'], item['sheet'])
        choices = []
        if field not in ('name', 'price'):
            choices.append([InlineKeyboardButton('Не использовать', callback_data=f'xm:setcol:{idx}:{field}:x')])
        for col in columns:
            descr = ' / '.join(col['examples'])[:37] or '(пусто в начале)'
            choices.append([InlineKeyboardButton(f'{col["letter"]} — {descr}', callback_data=f'xm:setcol:{idx}:{field}:{col["index"]}')])
        await query.message.reply_text('Выбери столбец: '+EXCEL_FIELD_NAMES[field], reply_markup=InlineKeyboardMarkup(choices))
    elif op == 'setcol':
        field, chosen = parts[3], parts[4]
        if field not in EXCEL_FIELDS:
            return
        item.setdefault('mapping', {k: None for k in EXCEL_FIELDS})[field] = None if chosen == 'x' else int(chosen)
        item['enabled'] = False; item['skipped'] = False
        await query.message.reply_text('Столбец выбран.', reply_markup=excel_sheet_menu(stage, idx))
    elif op == 'skip':
        item['enabled'] = False; item['skipped'] = True
        await query.message.reply_text('Лист пропущен.', reply_markup=excel_menu(stage))
    elif op == 'use':
        mapping = item.get('mapping') or {}
        if mapping.get('name') is None or mapping.get('price') is None or mapping['name'] == mapping['price']:
            await query.message.reply_text('❌ Укажи разные столбцы товара и цены.')
            return
        info = inspect_mapping(stage['data'], item['sheet'], mapping, item.get('header_row', 0))
        if not info['valid'] or info['truncated']:
            await query.message.reply_text('❌ Нет строк с распознанными названиями и положительными ценами либо лист слишком велик. Исправь столбцы.')
            return
        item.update(info); item['enabled'] = True; item['skipped'] = False
        await query.message.reply_text(f'✅ {item["sheet"]}: найдено {info["valid"]} предложений.', reply_markup=excel_menu(stage))


def _audit_cell(value):
    """Prevent an imported supplier name from becoming an Excel formula."""
    if value is None:
        return 'Не указан'
    if isinstance(value, str):
        value = value.strip()
        return "'" + value if value.startswith(('=', '+', '-', '@')) else (value or 'Не указан')
    return value


def _build_supplier_export(supplier, offers):
    """Build an XLSX audit of DB state, including parsed view and raw fields."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    all_sheet = workbook.active
    all_sheet.title = 'Все товары'
    check_sheet = workbook.create_sheet('Проверка')
    headers = [
        'ID записи', 'Исходное название (БД)', 'Модель в поиске', 'Бренд',
        'Память, GB', 'RAM, GB', 'Цвет', 'Регион', 'SIM', 'Состояние',
        'Цена', 'Валюта', 'НДС', 'Обновлён', 'Актуален',
        'Модель сохранена в БД', 'Цвет сохранён в БД', 'Регион сохранён в БД',
        'SIM сохранён в БД', 'Предупреждения БД',
    ]
    check_headers = ['ID записи', 'Исходное название', 'Причина проверки', 'Цена', 'Валюта']
    all_sheet.append(headers)
    check_sheet.append(check_headers)
    vat_labels = {'with_vat': 'С НДС', 'without_vat': 'Без НДС', 'unknown': 'Не указан'}
    for offer in offers:
        parsed = parse_product(offer['original_name'])
        def effective(key):
            value = parsed.get(key)
            return value if value is not None and value != [] and value != '' else offer.get(key)
        conditions = parsed.get('condition') or offer.get('condition_tags') or []
        if isinstance(conditions, str):
            conditions = [conditions]
        condition_labels = {'activated': 'Активирован', 'sealed': 'Запечатан',
                            'refurbished': 'Восстановлен', 'used': 'Б/у'}
        status = ', '.join(condition_labels.get(str(c), str(c)) for c in conditions) or 'Не указано'
        date = offer.get('updated_at')
        if date and date.tzinfo:
            date = date.astimezone(ZoneInfo(os.getenv('PRICE_TIMEZONE', 'Europe/Moscow'))).replace(tzinfo=None)
        price = float(offer['price']) if offer.get('price') is not None else None
        row = [
            offer['id'], _audit_cell(offer['original_name']), _audit_cell(effective('model')),
            _audit_cell(effective('brand')), effective('storage_gb'), effective('ram_gb'),
            _audit_cell(effective('color')), _audit_cell(effective('region')),
            _audit_cell(effective('sim')), status, price, offer.get('currency'),
            vat_labels.get(offer.get('tax_status'), 'Не указан'), date,
            'Да' if offer.get('is_fresh') else 'Нет',
            _audit_cell(offer.get('model')), _audit_cell(offer.get('color')),
            _audit_cell(offer.get('region')), _audit_cell(offer.get('sim')),
            '; '.join(str(x) for x in (offer.get('warnings') or [])),
        ]
        all_sheet.append(row)
        reasons = []
        if not effective('model'):
            reasons.append('Модель не распознана')
        if price is None or price <= 0:
            reasons.append('Некорректная или отсутствующая цена')
        if price is not None and price < 50:
            reasons.append('Цена ниже 50 — проверь, если это не аксессуар')
        if not offer.get('is_fresh'):
            reasons.append('Предложение устарело')
        if parsed.get('model') and offer.get('model') and parsed['model'] != offer['model']:
            reasons.append('Модель из текста отличается от сохранённой')
        reasons += [str(w) for w in (offer.get('warnings') or [])]
        if reasons:
            check_sheet.append([offer['id'], _audit_cell(offer['original_name']),
                                '; '.join(reasons), price, offer.get('currency')])

    for sheet, widths, price_col in [
        (all_sheet, [13, 70, 29, 17, 15, 14, 22, 21, 18, 20, 16, 13, 16, 22, 12, 29, 22, 21, 18, 50], 11),
        (check_sheet, [13, 75, 65, 16, 14], 4),
    ]:
        sheet.freeze_panes = 'C2' if sheet is all_sheet else 'B2'
        sheet.auto_filter.ref = sheet.dimensions
        sheet.row_dimensions[1].height = 28
        for cell in sheet[1]:
            cell.fill = PatternFill('solid', fgColor='17365D')
            cell.font = Font(color='FFFFFF', bold=True)
            cell.alignment = Alignment(vertical='center', wrap_text=True)
        for col, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(col)].width = width
        for row in sheet.iter_rows(min_row=2):
            if row[0].row % 2 == 0:
                for cell in row:
                    cell.fill = PatternFill('solid', fgColor='F1F6FA')
            row[price_col - 1].number_format = '#,##0.00'
        if sheet is all_sheet:
            for cell in sheet['N'][1:]:
                cell.number_format = 'DD.MM.YYYY HH:MM'
    data = BytesIO()
    workbook.save(data)
    data.seek(0)
    return data, check_sheet.max_row - 1


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not allowed(update):
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer()
    action = query.data or ''
    if action.startswith('draft:'):
        op, revision_raw = action.split(':')[1:]
        draft = context.user_data.get('auto_draft')
        if not draft or not revision_raw.isdigit() or int(revision_raw) != draft['revision']:
            await query.message.reply_text('Кнопка устарела: используй кнопки в последнем сообщении о черновике.')
            return
        if op == 'cancel':
            context.user_data.pop('auto_draft', None)
            await query.message.reply_text('❌ Черновик отменён. Сохранённый прайс не изменён.')
        elif op == 'save':
            sid = context.user_data.get('supplier_id')
            if sid != draft['supplier_id'] or not get_supplier(sid):
                await query.message.reply_text('Поставщик изменён. Выбери прежнего поставщика.')
                return
            if not draft['rows']:
                await query.message.reply_text('Нет распознанных товаров. Прайс не заменён.')
                return
            try:
                parsed = [(parse_product(name), price) for name, price in draft['rows']]
                summary = save_price_list(sid, parsed)
            except Exception:
                logger.exception('Failed to save auto draft')
                await query.message.reply_text('❌ Ошибка сохранения. Старый прайс должен остаться без изменений. Проверь Railway Logs.')
                return
            context.user_data.pop('auto_draft', None)
            await query.message.reply_text(
                f"✅ Общий прайс сохранён!\nСообщений: {len(draft['messages'])}\n"
                f"Конфигураций: {summary['saved']}\nДубли: {summary['duplicates']}\n"
                f"Пропущено: {summary['skipped']}\nСтарый прайс поставщика заменён."
            )
        return
    if action == 'parts:start':
        sid = context.user_data.get('supplier_id')
        supplier = get_supplier(sid) if sid else None
        if not supplier:
            await query.message.reply_text('Сначала выбери поставщика через /suppliers.')
            return
        if context.user_data.get('parts'):
            await query.message.reply_text('Сбор прайса уже идёт. Отправляй следующие части или заверши его.', reply_markup=multipart_keyboard())
            return
        context.user_data['parts'] = {'supplier_id': sid, 'rows': [], 'messages': 0, 'started': time.time(), 'brand': _supplier_default_brand(sid)}
        context.user_data.pop('pending', None)
        context.user_data.pop('undecided_text', None)
        await query.message.reply_text(
            f'📚 Сбор текстового прайса для «{supplier["name"]}».\n'
            'Отправляй части отдельными сообщениями, включая эмодзи. '
            'Ничего не сохраняется до подтверждения.\n'
            'После последней части нажми «Закончить и проверить».',
            reply_markup=multipart_keyboard())
    elif action == 'parts:cancel':
        context.user_data.pop('parts', None)
        await query.message.reply_text('❌ Сбор отменён. Текущий прайс поставщика не изменился.')
    elif action == 'parts:finish':
        batch = context.user_data.get('parts')
        if not batch or not batch['rows']:
            await query.message.reply_text('Нет собранных товаров. Пришли части прайса или отменяй сбор.')
            return
        if context.user_data.get('supplier_id') != batch['supplier_id']:
            await query.message.reply_text('Выбран другой поставщик. Верни прежнего или отмени сбор.')
            return
        context.user_data['pending'] = list(batch['rows'])
        context.user_data.pop('parts', None)
        await show_pending(query.message, context)
    elif action.startswith('mode:'):
        mode_name = action.split(':', 1)[1]
        if mode_name not in ('auto', 'search', 'import'):
            return
        context.user_data['mode'] = mode_name
        labels = {'auto': 'Авто', 'search': 'Поиск', 'import': 'Загрузка прайса'}
        await query.message.reply_text(f"✅ Режим: {labels[mode_name]}")
    elif action == 'nav:suppliers':
        await query.message.reply_text('Выбери поставщика:', reply_markup=keyboard_suppliers())
    elif action == 'nav:settings':
        sid = context.user_data.get('supplier_id')
        supplier = get_supplier(sid) if sid else None
        if supplier:
            await query.message.reply_text(
                f"⚙️ {supplier['name']}", reply_markup=settings_keyboard(supplier)
            )
        else:
            await query.message.reply_text('Сначала выбери поставщика:', reply_markup=keyboard_suppliers())
    elif action.startswith('decide:'):
        raw = context.user_data.pop('undecided_text', None)
        if not raw:
            await query.message.reply_text('Сообщение для обработки уже отсутствует. Пришли его ещё раз.')
            return
        decision = action.split(':', 1)[1]
        if decision == 'search':
            await run_search(query.message, raw)
        elif decision == 'import':
            rows = parse_price_list(raw, _supplier_default_brand(context.user_data.get('supplier_id')))
            if not rows:
                await query.message.reply_text('В сообщении нет распознанных пар «товар — цена».')
            else:
                sid = context.user_data.get('supplier_id')
                if not sid or not get_supplier(sid):
                    await query.message.reply_text('Сначала выбери поставщика через /suppliers.')
                    return
                try:
                    draft, _ = append_to_auto_draft(context, sid, raw)
                except ValueError as exc:
                    await query.message.reply_text(str(exc))
                    return
                await auto_draft_status(query.message, draft)
    elif action == 'supplier:new':
        context.user_data.pop('await_rename_id', None)
        context.user_data['await_name'] = True
        await query.message.reply_text("Введи название нового поставщика одним сообщением.")
    elif action.startswith('pick:'):
        sid = int(action.split(':')[1])
        if context.user_data.get('auto_draft') and context.user_data['auto_draft']['supplier_id'] != sid:
            await query.message.reply_text('Сначала сохрани или отмени черновик текущего поставщика.', reply_markup=auto_draft_keyboard(context.user_data['auto_draft']))
            return
        if context.user_data.get('parts') and context.user_data['parts']['supplier_id'] != sid:
            await query.message.reply_text('Сначала заверши или отмени сбор прайса для текущего поставщика.', reply_markup=multipart_keyboard())
            return
        s = get_supplier(sid)
        if not s:
            await query.message.reply_text("Поставщик не найден")
            return
        context.user_data['supplier_id'] = sid
        await query.message.reply_text(f"✅ Выбран поставщик:\n{_supplier_title(s)}", reply_markup=supplier_manage_keyboard(s, is_owner(update)))
        await show_pending(query.message, context)
    elif action.startswith('supplier:'):
        parts = action.split(':')
        if len(parts) != 3 or not parts[2].isdigit():
            return
        operation, sid = parts[1], int(parts[2])
        if operation in ('delete', 'confirm_delete') and not is_owner(update):
            await query.message.reply_text('🔒 Удалять поставщиков может только владелец бота.')
            return
        s = get_supplier(sid)
        if not s:
            await query.message.reply_text('Поставщик уже удалён.', reply_markup=keyboard_suppliers())
            return
        if operation == 'export':
            try:
                offers = export_supplier_offers(sid)
                if not offers:
                    await query.message.reply_text('У этого поставщика пока нет сохранённых товаров.')
                    return
                file_buffer, review_count = _build_supplier_export(s, offers)
                file_name = f'pricefinder_supplier_{sid}_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx'
                await query.message.reply_document(
                    document=file_buffer, filename=file_name,
                    caption=(f'📥 Выгрузка из PostgreSQL: {s["name"]}\n'
                             f'Товаров: {len(offers)}\n'
                             f'Позиций для проверки: {review_count}\n'
                             'Показаны все сохранённые товары, включая устаревшие.'),
                )
            except Exception:
                logger.exception('Failed to export supplier %s', sid)
                await query.message.reply_text('Не удалось выгрузить прайс. Проверь логи Railway.')
        elif operation == 'rename':
            context.user_data.pop('await_name', None)
            context.user_data['await_rename_id'] = sid
            await query.message.reply_text(
                f"✏️ Новое название для «{s['name']}»? Отправь одним сообщением. "
                "Чтобы отменить, нажми кнопку.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton('❌ Отмена', callback_data=f'supplier:cancel_rename:{sid}')
                ]])
            )
        elif operation == 'cancel_rename':
            if context.user_data.get('await_rename_id') == sid:
                context.user_data.pop('await_rename_id', None)
            await query.message.reply_text('Изменение названия отменено.', reply_markup=supplier_manage_keyboard(s, is_owner(update)))
        elif operation == 'delete':
            await query.message.reply_text(
                f"⚠️ Удалить «{s['name']}» и ВСЕ его сохранённые товары? "
                "Это действие нельзя отменить.", reply_markup=supplier_delete_keyboard(sid)
            )
        elif operation == 'cancel_delete':
            await query.message.reply_text('Удаление отменено.', reply_markup=supplier_manage_keyboard(s, is_owner(update)))
        elif operation == 'confirm_delete':
            deleted = delete_supplier(sid)
            if deleted:
                if context.user_data.get('supplier_id') == sid:
                    context.user_data.pop('supplier_id', None)
                    context.user_data.pop('pending', None)
                    context.user_data.pop('excel_pending', None)
                if context.user_data.get('await_rename_id') == sid:
                    context.user_data.pop('await_rename_id', None)
                await query.message.reply_text(
                    f"✅ Поставщик «{s['name']}» и его товары удалены.",
                    reply_markup=keyboard_suppliers()
                )
            else:
                await query.message.reply_text('Поставщик уже удалён.', reply_markup=keyboard_suppliers())
        elif operation == 'settings':
            context.user_data['supplier_id'] = sid
            await query.message.reply_text(f"⚙️ {s['name']}", reply_markup=settings_keyboard(s))
    elif action.startswith('settings:currency:'):
        sid = int(action.rsplit(':', 1)[1])
        buttons = [[InlineKeyboardButton(c, callback_data=f"setcur:{sid}:{c}")]
                   for c in ('USD', 'BYN', 'RUB', 'EUR')]
        await query.message.reply_text("Выбери валюту:", reply_markup=InlineKeyboardMarkup(buttons))
    elif action.startswith('settings:vat:'):
        sid = int(action.rsplit(':', 1)[1])
        options = [('without_vat', 'Без НДС'), ('with_vat', 'С НДС')]
        buttons = [[InlineKeyboardButton(label, callback_data=f"setvat:{sid}:{value}")]
                   for value, label in options]
        await query.message.reply_text("Выбери статус НДС:", reply_markup=InlineKeyboardMarkup(buttons))
    elif action.startswith('setcur:') or action.startswith('setvat:'):
        kind, sid, value = action.split(':', 2)
        sid = int(sid)
        s = update_supplier_settings(sid, **({'currency': value} if kind == 'setcur' else {'tax_status': value}))
        await query.message.reply_text(f"✅ Настройки {s['name']}: {s['currency']}, {s['tax_status']}")
        await show_pending(query.message, context)
    elif action.startswith('xm:'):
        await excel_manual_callback(query, context, action)
    elif action == 'excel:cancel':
        context.user_data.pop('excel_pending', None)
        await query.message.reply_text('Excel-импорт отменён. Данные не сохранены.')
    elif action == 'excel:save':
        staged = context.user_data.get('excel_pending')
        if not staged or time.time() > staged['expires']:
            await query.message.reply_text('Подтверждение истекло. Отправь Excel ещё раз.')
            return
        sid = context.user_data.get('supplier_id')
        if sid != staged['supplier_id'] or not staged_excel_ready(staged):
            await query.message.reply_text('Поставщик изменился. Отправь Excel ещё раз.')
            return
        await query.message.reply_text('⏳ Сохраняю Excel в PostgreSQL. Подожди...')
        try:
            # Re-parse the exact staged file; no Telegram re-download required.
            extracted, skipped = [], 0
            selected_items = [it for it in staged['sheets'] if it.get('enabled')]
            for selected in selected_items:
                batch, missing = extract_xlsx(staged['data'], selected['sheet'], selected['mapping'], selected['header_row'], default_brand=_supplier_default_brand(sid))
                extracted.extend(batch)
                skipped += missing
            parsed = [(parse_product(name), price) for name, price in extracted]
            report = save_excel_rows(sid, parsed, staged['currency'], staged['tax_status'])
            context.user_data.pop('excel_pending', None)
            try:
                profiles = [{**it, 'fingerprint': sheet_fingerprint(staged['data'], it['sheet'], it['header_row'], it['mapping'])} for it in selected_items]
                save_excel_profiles(sid, profiles)
            except Exception:
                logger.exception('Excel profiles save failed')
        except Exception:
            logger.exception('Excel save failed')
            await query.message.reply_text('❌ Импорт не выполнен. Проверь Railway Logs. Повтори отправку файла.')
            return
        await query.message.reply_text(
            f"✅ Excel сохранён!\nКонфигураций: {report['saved']}\n"
            f"Нераспознанных строк (сохранены по исходному названию): {report['unrecognized_rows']}\n"
            f"Дубли внутри файла: {report['duplicates']}\n"
            f"Строк без цены: {skipped}\n"
            'Предыдущий прайс этого поставщика полностью заменён.\n'
            f"Валюта: {staged['currency']}, НДС: {staged['tax_status']}."
        )
    elif action == 'import:cancel':
        context.user_data.pop('pending', None)
        await query.message.reply_text("Прайс отменён. В базу ничего не записано.")
    elif action == 'import:save':
        pending = context.user_data.get('pending')
        sid = context.user_data.get('supplier_id')
        if not pending or not sid:
            await query.message.reply_text('Нет ожидающего прайса или поставщика.')
            return
        rows = [(parse_product(name), price) for name, price in pending]
        summary = save_price_list(sid, rows)
        context.user_data.pop('pending', None)
        await query.message.reply_text(
            f"✅ Сохранено конфигураций: {summary['saved']}\n"
            f"Дублей внутри прайса: {summary['duplicates']}\n"
            f"Пропущено для проверки: {summary['skipped']}\n"
            "Предыдущий прайс этого поставщика полностью заменён."
            + ("\nПримеры пропусков:\n" + '\n'.join(
                f"• {name[:70]} — {reason}" for name, reason in summary['rejected'])
               if summary['rejected'] else "")
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize(update):
        return
    text = (update.effective_message.text or '').strip()
    rename_sid = context.user_data.get('await_rename_id')
    if rename_sid is not None:
        if not text or len(text) > 120 or '\n' in text:
            await update.effective_message.reply_text('Название должно быть одной строкой (до 120 символов).')
            return
        try:
            renamed = rename_supplier(rename_sid, text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        if renamed is None:
            context.user_data.pop('await_rename_id', None)
            await update.effective_message.reply_text('Поставщик больше не существует.')
            return
        context.user_data.pop('await_rename_id', None)
        await update.effective_message.reply_text(
            f"✅ Название изменено на «{renamed['name']}».",
            reply_markup=supplier_manage_keyboard(renamed)
        )
        return
    if context.user_data.pop('await_name', False):
        if not text or len(text) > 120 or '\n' in text:
            context.user_data['await_name'] = True
            await update.effective_message.reply_text('Название должно быть одной строкой (до 120 символов).')
            return
        sid = add_supplier(text)
        context.user_data['supplier_id'] = sid
        await update.effective_message.reply_text(f'✅ Поставщик «{text}» добавлен/выбран.')
        await show_pending(update.effective_message, context)
        return
    existing_draft = context.user_data.get('auto_draft')
    if existing_draft is not None:
        sid = context.user_data.get('supplier_id')
        if sid != existing_draft['supplier_id']:
            await update.effective_message.reply_text('Черновик относится к другому поставщику.', reply_markup=auto_draft_keyboard(existing_draft))
            return
        try:
            draft, added = append_to_auto_draft(context, sid, text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc), reply_markup=auto_draft_keyboard(existing_draft))
            return
        if added:
            await auto_draft_status(update.effective_message, draft)
        else:
            await update.effective_message.reply_text('Такое сообщение уже есть в черновике.', reply_markup=auto_draft_keyboard(draft))
        return
    batch = context.user_data.get('parts')
    if batch is not None:
        if batch['supplier_id'] != context.user_data.get('supplier_id'):
            await update.effective_message.reply_text('Сбор относится к другому поставщику. Отмени сбор и начни заново.', reply_markup=multipart_keyboard())
            return
        if batch['messages'] >= 100 or len(batch['rows']) >= 20000:
            await update.effective_message.reply_text('Достигнут лимит частей/строк. Заверши текущий сбор.', reply_markup=multipart_keyboard())
            return
        rows, next_brand = parse_price_list(text, batch.get('brand'), return_brand=True)
        if not rows and next_brand == batch.get('brand'):
            await update.effective_message.reply_text('В этой части не удалось распознать пары «товар — цена». Часть не добавлена. Проверь формат и отправь снова.', reply_markup=multipart_keyboard())
            return
        batch['rows'].extend(rows)
        batch['brand'] = next_brand
        batch['messages'] += 1
        await update.effective_message.reply_text(
            f'✅ Часть {batch["messages"]} принята: {len(rows)} строк. '
            f'Всего накоплено: {len(batch["rows"])} строк.\n'
            'Отправляй следующую часть или заверши сбор.', reply_markup=multipart_keyboard())
        return
    current_mode = context.user_data.get('mode', 'auto')
    if current_mode == 'search':
        await run_search(update.effective_message, text)
        return

    rows = parse_price_list(text, _supplier_default_brand(context.user_data.get('supplier_id')))
    if current_mode == 'import' or (len(rows) >= 2 and context.user_data.get('supplier_id')):
        sid = context.user_data.get('supplier_id')
        if not sid or not get_supplier(sid):
            await update.effective_message.reply_text('Сначала выбери поставщика через /suppliers.')
            return
        try:
            draft, _ = append_to_auto_draft(context, sid, text)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        await auto_draft_status(update.effective_message, draft)
    elif len(rows) == 1 or ('\n' in text and not rows):
        context.user_data['undecided_text'] = text
        await update.effective_message.reply_text('Это поиск товара или загрузка прайса?', reply_markup=decision_keyboard())
    else:
        await run_search(update.effective_message, text)



async def handle_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize(update):
        return
    if context.user_data.get('auto_draft'):
        await update.effective_message.reply_text('Сначала сохрани или отмени текстовый черновик.', reply_markup=auto_draft_keyboard(context.user_data['auto_draft']))
        return
    if context.user_data.get('parts'):
        await update.effective_message.reply_text('Сначала заверши или отмени сбор текстового прайса. Excel не будет импортирован во время сбора.', reply_markup=multipart_keyboard())
        return
    doc = update.effective_message.document
    if not doc or not doc.file_name.lower().endswith('.xlsx'):
        await update.effective_message.reply_text('Поддерживаются файлы .xlsx.')
        return
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await update.effective_message.reply_text('Файл больше 12 МБ.')
        return
    sid = context.user_data.get('supplier_id')
    supplier = get_supplier(sid) if sid else None
    if not supplier:
        await update.effective_message.reply_text('Сначала выбери поставщика через /suppliers.')
        return
    if supplier['currency'] not in ('USD', 'BYN', 'RUB', 'EUR') or supplier['tax_status'] not in ('with_vat', 'without_vat'):
        await update.effective_message.reply_text('Сначала укажи валюту и НДС в /settings.')
        return
    try:
        tg_file = await doc.get_file()
        payload = bytes(await tg_file.download_as_bytearray())
        detected = inspect_xlsx(payload)
        profiles = get_excel_profiles(sid)
        sheets = []
        for result in detected:
            name = result['sheet']
            if result['status'] == 'preview' and result.get('valid', 0) > 0 and not result.get('ambiguous') and not result.get('truncated'):
                item = dict(result, enabled=True, skipped=False)
            else:
                item = {'sheet': name, 'mapping': {k: None for k in EXCEL_FIELDS},
                        'header_row': 0, 'enabled': False, 'skipped': False}
                old = profiles.get(name)
                if old:
                    oldmap, oldhead = old['mapping'], old['header_row']
                    fingerprint = sheet_fingerprint(payload, name, oldhead, oldmap)
                    if fingerprint == old['column_fingerprint']:
                        check = inspect_mapping(payload, name, oldmap, oldhead)
                        if check['valid'] and not check['truncated']:
                            item.update(check)
                            item.update(mapping=oldmap, header_row=oldhead, enabled=True)
            # Reuse supplier's validated column choice if headers are unchanged.
            old = profiles.get(name)
            if old:
                oldmap, oldhead = old['mapping'], old['header_row']
                if sheet_fingerprint(payload, name, oldhead, oldmap) == old['column_fingerprint']:
                    check = inspect_mapping(payload, name, oldmap, oldhead)
                    if check['valid'] and not check['truncated']:
                        item.update(check, mapping=oldmap, header_row=oldhead, enabled=True, skipped=False)
            sheets.append(item)
        if not sheets:
            await update.effective_message.reply_text('В файле нет доступных листов.')
            return
    except Exception as exc:
        logger.exception('Excel inspection failed')
        await update.effective_message.reply_text('Ошибка чтения Excel: '+str(exc)[:180])
        return
    context.user_data['excel_pending'] = {
        'data': payload, 'sheets': sheets, 'supplier_id': sid,
        'supplier_name': supplier['name'], 'currency': supplier['currency'],
        'tax_status': supplier['tax_status'], 'expires': time.time()+1800,
    }
    await excel_review(update.effective_message, context)



async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize(update):
        return
    query_text = " ".join(context.args or []).strip()
    if not query_text:
        await update.effective_message.reply_text(
            'Просто напиши название товара, например: айфон 17 про 256 чёрный'
        )
        return
    await run_search(update.effective_message, query_text)


def _search_normalize(text):
    text = text.casefold().replace('ё', 'е')
    text = re.sub(r'айфона?|айфон', 'iphone', text)
    text = re.sub(r'(\d+)\s*(?:гб|gb)\b', r'\1gb', text)
    return re.findall(r'[a-zа-я0-9]+', text)


def _offer_matches(query, requested, offer):
    original = offer['original_name']
    found = parse_product(original)
    if requested['is_accessory'] != found['is_accessory']:
        return False

    query_model = requested.get('model')
    # A stored, explicitly recognized model is valid even if the original text
    # is an abbreviation. This supports older rows that retain structured data.
    found_model = found.get('model') or offer.get('model')
    if query_model:
        if not found_model and query_model.startswith('iPhone '):
            # A bare "17 Pro" could be iPhone *or* Redmi. Only infer Apple
            # for a supplier explicitly designated as Apple-only; never infer
            # it from a matching number alone.
            if _supplier_default_brand(offer.get('supplier_id')) == 'Apple':
                inferred = contextual_name(original, 'Apple')
                found = parse_product(inferred)
                found_model = found.get('model')
        if not found_model or query_model.casefold() != found_model.casefold():
            return False
    else:
        if not all(t in _search_normalize(original) for t in _search_normalize(query)):
            return False

    for field in ('storage_gb', 'ram_gb', 'color', 'region', 'sim'):
        required = requested.get(field)
        if required is not None and (found.get(field) or offer.get(field)) != required:
            return False
    return True


def _display(value, labels=None):
    if value is None or value == '' or value == []:
        return 'Не указан'
    return labels.get(value, value) if labels else str(value)


SIM_LABELS = {'esim': 'eSIM', 'sim_esim': 'SIM + eSIM',
              'dual_sim': '2 SIM', 'dual_esim': '2 eSIM'}
COLOR_LABELS = {'black': 'Black', 'white': 'White', 'blue': 'Blue',
                'deep_blue': 'Deep Blue', 'sky_blue': 'Sky Blue',
                'silver': 'Silver', 'green': 'Green', 'orange': 'Orange',
                'purple': 'Purple', 'cosmic_orange': 'Cosmic Orange',
                'natural_titanium': 'Natural Titanium', 'burgundy': 'Burgundy',
                'glacier': 'Glacier', 'starlight': 'Starlight',
                'midnight': 'Midnight', 'graphite': 'Graphite',
                'gold': 'Gold', 'pink': 'Pink'}


def format_offer(offer, index):
    parsed = parse_product(offer['original_name'])
    def get(key):
        return parsed.get(key) or offer.get(key)
    memory = get('storage_gb')
    memory_label = f'{memory} GB' if memory is not None else 'Не указан'
    condition = parsed.get('condition') or offer.get('condition_tags') or []
    condition_labels = {'activated': 'Активирован', 'sealed': 'Запечатан',
                        'refurbished': 'Восстановлен', 'used': 'Б/у'}
    condition_label = ', '.join(condition_labels.get(c, c) for c in condition) if condition else 'Не указано'
    vat = {'with_vat': 'с НДС', 'without_vat': 'без НДС',
           'unknown': 'НДС не указан'}.get(offer.get('tax_status'), 'НДС не указан')
    return (f"{index}. 🏪 {offer['supplier_name']}\n"
            f"{offer['original_name']}\n"
            f"💰 {offer['price']} {offer['currency']} ({vat})\n"
            f"Память: {memory_label} | Цвет: {_display(get('color'), COLOR_LABELS)}\n"
            f"SIM: {_display(get('sim'), SIM_LABELS)} | Регион: {_display(get('region'))}\n"
            f"Состояние: {condition_label}")


def _interpret_search_query(query_text):
    """Short iPhone-style queries default to iPhone, never to Redmi/Xiaomi.

    An explicit brand always wins. Other bare model families (e.g. S25) keep
    their existing behavior. Matching still requires a recognized iPhone model.
    """
    compact = re.sub(r"\s+", " ", query_text.strip())
    if re.match(r"^\d{1,2}\s+(?:pro\s*max|promax|pro|plus|mini|air)(?=\s|$)", compact, re.I):
        return "iPhone " + compact
    return compact


async def run_search(message, query_text):
    query_text = query_text.strip()
    if not query_text:
        await message.reply_text('Напиши название товара для поиска.')
        return
    requested = parse_product(_interpret_search_query(query_text))
    matches = [o for o in search_offers()
               if o['is_fresh'] and _offer_matches(query_text, requested, o)]
    if not matches:
        await message.reply_text(f'🔎 {query_text}\nПо актуальным прайсам совпадений нет.')
        return
    # Currency/VAT groups cannot be compared on the same price scale.
    # Apply the per-supplier cap across all groups, then cap the full output.
    groups = {}
    for offer in matches:
        groups.setdefault((offer['currency'], offer['tax_status']), []).append(offer)

    selected = []
    supplier_counts = {}
    for group_key, offers in sorted(groups.items()):
        offers.sort(key=lambda o: (o['price'], o['supplier_name'].casefold()))
        for offer in offers:
            supplier_key = offer['supplier_id']
            if supplier_counts.get(supplier_key, 0) >= 3:
                continue
            selected.append((group_key, offer))
            supplier_counts[supplier_key] = supplier_counts.get(supplier_key, 0) + 1
            if len(selected) >= 20:
                break
        if len(selected) >= 20:
            break

    blocks = [f'🔎 {query_text}\nНайдено предложений: {len(matches)}\nПоказано: {len(selected)} (не более 3 от поставщика)']
    previous_group = None
    for index, (group_key, offer) in enumerate(selected, 1):
        if group_key != previous_group:
            currency, tax = group_key
            blocks.append(f'\n📊 {currency}, ' + {'with_vat':'с НДС', 'without_vat':'без НДС',
                          'unknown':'НДС не указан'}.get(tax,'НДС не указан'))
            previous_group = group_key
        blocks.append(format_offer(offer, index))
    chunk = ''
    for block in blocks:
        if len(block) > 3700:
            block = block[:3690] + '…'
        if chunk and len(chunk) + len(block) + 2 > 3800:
            await message.reply_text(chunk)
            chunk = ''
        chunk += ('\n\n' if chunk else '') + block
    if chunk:
        await message.reply_text(chunk)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Telegram error: %s", context.error, exc_info=context.error)


def main():
    token = os.environ['TELEGRAM_BOT_TOKEN']
    init_db()
    logger.info('PostgreSQL: подключение успешно, таблицы готовы')
    app = ApplicationBuilder().token(token).build()
    for command, handler in [('start', start), ('myid', myid), ('suppliers', suppliers),
                             ('addsupplier', addsupplier), ('settings', settings), ('prices', prices),
                             ('find', find), ('menu', menu), ('mode', mode)]:
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_excel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    logger.info('Mobi Price Finder запущен')
    app.run_polling()


if __name__ == '__main__':
    main()
