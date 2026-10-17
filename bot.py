"""Mobi Price Finder — controlled text price-list imports into PostgreSQL."""
import html
import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)
from database import (
    add_supplier, get_supplier, get_supplier_offers, init_db, list_suppliers,
    save_price_list, update_supplier_settings,
)
from product_parser import parse_product

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def allowed(update):
    configured = os.getenv("OWNER_TELEGRAM_ID", "").strip()
    return configured.isdigit() and update.effective_user and str(update.effective_user.id) == configured


async def authorize(update):
    if allowed(update):
        return True
    await update.effective_message.reply_text(
        "🔒 Доступ закрыт. Для настройки отправь /myid и добавь свой ID в Railway "
        "как OWNER_TELEGRAM_ID."
    )
    return False


def parse_price_list(text):
    lines = [x.strip(" *\\\t\r") for x in html.unescape(text).splitlines()]
    lines = [x for x in lines if x]
    output, pending = [], None
    number = r"\d+(?:[.,]\d{1,2})?"
    for line in lines:
        if re.fullmatch(number, line):
            if pending:
                output.append((pending, line))
                pending = None
            continue
        match = re.fullmatch(rf"(.+?)\s+({number})\s*(?:\$|USD)?", line, re.I)
        if match:
            output.append((match.group(1).strip(), match.group(2)))
            pending = None
        else:
            pending = line
    return output


def keyboard_suppliers():
    buttons = [[InlineKeyboardButton(s['name'], callback_data=f"pick:{s['id']}")]
               for s in list_suppliers()[:75]]
    buttons.append([InlineKeyboardButton("➕ Новый поставщик", callback_data="supplier:new")])
    return InlineKeyboardMarkup(buttons)


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
        "Пришли текстовый прайс — проверим его перед сохранением."
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
    supported = [(p, v) for p, v in parsed if p.get('model') and p.get('storage_gb') and not p.get('warnings')]
    previews = [f"• {p['original']} — {v}" for p, v in supported[:3]]
    await message.reply_text(
        f"📋 Прайс: {len(parsed)} строк\nПоставщик: {s['name']}\n"
        f"Валюта: {s['currency']} | НДС: {s['tax_status']}\n"
        f"Кандидатов на запись: {len(supported)} (дубликаты свернём)\n"
        f"Нуждаются в проверке: {len(parsed) - len(supported)}\n\n"
        + "\n".join(previews)
        + "\n\nСохранить?",
        reply_markup=confirmation_keyboard(),
    )


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not allowed(update):
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer()
    action = query.data or ''
    if action == 'supplier:new':
        context.user_data['await_name'] = True
        await query.message.reply_text("Введи название нового поставщика одним сообщением.")
    elif action.startswith('pick:'):
        sid = int(action.split(':')[1])
        s = get_supplier(sid)
        if not s:
            await query.message.reply_text("Поставщик не найден")
            return
        context.user_data['supplier_id'] = sid
        await query.message.reply_text(f"✅ Выбран поставщик: {s['name']}")
        await show_pending(query.message, context)
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
            "Другие позиции поставщика не удалены."
            + ("\nПримеры пропусков:\n" + '\n'.join(
                f"• {name[:70]} — {reason}" for name, reason in summary['rejected'])
               if summary['rejected'] else "")
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorize(update):
        return
    text = (update.effective_message.text or '').strip()
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
    rows = parse_price_list(text)
    if not rows:
        await update.effective_message.reply_text('Не удалось найти пары «товар — цена».')
        return
    context.user_data['pending'] = rows
    await show_pending(update.effective_message, context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Telegram error: %s", context.error, exc_info=context.error)


def main():
    token = os.environ['TELEGRAM_BOT_TOKEN']
    init_db()
    logger.info('PostgreSQL: подключение успешно, таблицы готовы')
    app = ApplicationBuilder().token(token).build()
    for command, handler in [('start', start), ('myid', myid), ('suppliers', suppliers),
                             ('addsupplier', addsupplier), ('settings', settings), ('prices', prices)]:
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    logger.info('Mobi Price Finder запущен')
    app.run_polling()


if __name__ == '__main__':
    main()
