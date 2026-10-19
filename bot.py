"""Mobi Price Finder — controlled text price-list imports into PostgreSQL."""
import html
import logging
import os
import re
import hashlib
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)
from database import (
    add_supplier, get_supplier, get_supplier_offers, get_offers, search_offers, init_db, list_suppliers,
    save_price_list, save_excel_rows, update_supplier_settings, rename_supplier, delete_supplier,
    get_excel_profiles, save_excel_profiles,
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
    value = re.sub(r"^[\s•▪▫🔹🔸✅☑️📱📦💎🔥⭐️✨➖➡️—–-]+", "", value)
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
    """Read Telegram supplier messages with headings, flags and price annotations.

    Keep a canonical name (e.g. 'iPhone 17 Pro') so search can distinguish
    Apple devices from similarly named Xiaomi/Redmi products.
    """
    output, pending = [], None
    active_brand = initial_brand
    number = r"(\d+(?:[.,]\d{1,2})?)"
    currency = r"(?:\$|USD|EUR|RUB|₽|€)"
    # The trailing eSIM note is product information, not part of the price.
    trailing = r"(?:\s*\((?:e\s*sim|sim\s*\+\s*esim|dual\s*sim)\))?"
    priced = re.compile(rf"^(.+?)\s*[-—–:]\s*[$€₽]?\s*{number}\s*{currency}{trailing}\s*$", re.I)
    standalone = re.compile(rf"^[$€₽]?\s*{number}\s*{currency}{trailing}\s*$", re.I)
    for raw in html.unescape(text).splitlines():
        line = raw.strip().strip('*_`\\ ').strip()
        if not line:
            continue
        heading = section_brand(line)
        if heading:
            active_brand = heading
            pending = None
            continue
        # A model-only heading like '18 Pro/Pro Max 📱📱:' remains in Apple section.
        if re.fullmatch(r'[0-9]{1,2}\s+Pro\s*/\s*Pro\s*Max\s*📱*\s*:', line, re.I):
            continue
        # Remove Markdown fragments copied from formatted Telegram messages.
        line = line.replace('**', '').replace('\u00a0', ' ').strip()
        match = priced.fullmatch(line)
        if match:
            name = _clean_price_name(match.group(1))
            # Parenthesized eSIM info is crucial to search and display.
            suffix = re.search(r'\(e\s*sim\)', line, re.I)
            if suffix and not re.search(r'\besim\b', name, re.I):
                name += ' eSIM'
            if name:
                output.append((contextual_name(name, active_brand), match.group(2)))
            pending = None
            continue
        match = standalone.fullmatch(line)
        if match and pending:
            output.append((contextual_name(_clean_price_name(pending), active_brand), match.group(1)))
            pending = None
            continue
        # Avoid treating category labels as product names.
        if re.fullmatch(r'[—–\-_=📱⌚💻\s]+', line):
            continue
        pending = line
    return (output, active_brand) if return_brand else output


def mode_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Авто", callback_data="mode:auto"),
         InlineKeyboardButton("🔎 Поиск", callback_data="mode:search")],
        [InlineKeyboardButton("📥 Загрузка прайса", callback_data="mode:import"),
         InlineKeyboardButton("📚 Прайс частями", callback_data="parts:start")],
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


def keyboard_suppliers():
    buttons = [[InlineKeyboardButton(s['name'], callback_data=f"pick:{s['id']}")]
               for s in list_suppliers()[:75]]
    buttons.append([InlineKeyboardButton("➕ Новый поставщик", callback_data="supplier:new")])
    return InlineKeyboardMarkup(buttons)


def supplier_manage_keyboard(supplier, can_delete=True):
    sid = supplier['id']
    buttons = [[InlineKeyboardButton("✏️ Переименовать", callback_data=f"supplier:rename:{sid}")]]
    if can_delete:
        buttons.append([InlineKeyboardButton("🗑 Удалить", callback_data=f"supplier:delete:{sid}")])
    buttons += [
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
    await update.effective_message.reply_text("Выбери поставщика:", reply_markup=keyboard_suppliers())


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


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not allowed(update):
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer()
    action = query.data or ''
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
                context.user_data['pending'] = rows
                await show_pending(query.message, context)
    elif action == 'supplier:new':
        context.user_data.pop('await_rename_id', None)
        context.user_data['await_name'] = True
        await query.message.reply_text("Введи название нового поставщика одним сообщением.")
    elif action.startswith('pick:'):
        sid = int(action.split(':')[1])
        if context.user_data.get('parts') and context.user_data['parts']['supplier_id'] != sid:
            await query.message.reply_text('Сначала заверши или отмени сбор прайса для текущего поставщика.', reply_markup=multipart_keyboard())
            return
        s = get_supplier(sid)
        if not s:
            await query.message.reply_text("Поставщик не найден")
            return
        context.user_data['supplier_id'] = sid
        await query.message.reply_text(f"✅ Выбран поставщик: {s['name']}", reply_markup=supplier_manage_keyboard(s, is_owner(update)))
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
        if operation == 'rename':
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
    if current_mode == 'import':
        if not rows:
            await update.effective_message.reply_text(
                'Не найдено пар «товар — цена». Проверь формат прайса или выбери «Авто».'
            )
            return
        context.user_data['pending'] = rows
        await show_pending(update.effective_message, context)
        return

    # В авторежиме одиночное название без цены — поиск по всем поставщикам.
    # Даже один товар с ценой может оказаться как прайсом, так и запросом;
    # в этом случае не принимаем решение без пользователя.
    if len(rows) >= 2:
        context.user_data['pending'] = rows
        await show_pending(update.effective_message, context)
    elif len(rows) == 1 or ('\n' in text and not rows):
        context.user_data['undecided_text'] = text
        await update.effective_message.reply_text(
            'Это поиск товара или загрузка прайса?', reply_markup=decision_keyboard()
        )
    else:
        await run_search(update.effective_message, text)




async def handle_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize(update):
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
    # Do not rank different currencies/VAT modes as if they are comparable.
    groups = {}
    for offer in matches:
        groups.setdefault((offer['currency'], offer['tax_status']), []).append(offer)
    blocks = [f'🔎 {query_text}\nНайдено предложений: {len(matches)}']
    index = 0
    for (currency, tax), offers in sorted(groups.items()):
        offers.sort(key=lambda o: (o['price'], o['supplier_name'].casefold()))
        blocks.append(f'\n📊 {currency}, ' + {'with_vat':'с НДС', 'without_vat':'без НДС',
                      'unknown':'НДС не указан'}.get(tax,'НДС не указан'))
        for offer in offers:
            index += 1
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
