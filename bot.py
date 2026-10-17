"""Mobi Price Finder: supplier registration and preview of text price lists."""
import html
import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from database import add_supplier, init_db, list_suppliers
from product_parser import parse_product

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def parse_price_list(text):
    text = html.unescape(text)
    lines = [line.strip(" *\\\t\r") for line in text.splitlines()]
    lines = [line for line in lines if line]
    products = []
    pending_name = None
    number = r"\d+(?:[.,]\d{1,2})?"
    for line in lines:
        if re.fullmatch(number, line):
            if pending_name:
                products.append((pending_name, line))
                pending_name = None
            continue
        match = re.fullmatch(
            rf"(.+?)\s+({number})\s*(?:\$|USD)?", line, re.IGNORECASE,
        )
        if match:
            name, price = match.groups()
            products.append((name.strip(), price))
            pending_name = None
        else:
            pending_name = line
    return products


def supplier_keyboard(include_new=True):
    suppliers = list_suppliers()
    buttons = [
        [InlineKeyboardButton(row["name"], callback_data=f"supplier:{row['id']}")]
        for row in suppliers[:40]
    ]
    if include_new:
        buttons.append([InlineKeyboardButton("➕ Новый поставщик", callback_data="supplier:new")])
    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👋 Привет! Я Mobi Price Finder.\n\n"
        "Пришли текстовый прайс или используй /suppliers для управления поставщиками.\n"
        "Сохранение цен пока выключено — сначала проверяем привязку к поставщику."
    )


async def suppliers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_suppliers()
    names = "\n".join(f"• {r['name']}" for r in rows[:25]) or "Поставщиков пока нет."
    await update.effective_message.reply_text(
        "🏷 Поставщики:\n" + names + "\n\nВыбери действие:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить", callback_data="supplier:new")],
            [InlineKeyboardButton("📋 Выбрать поставщика", callback_data="supplier:choose")],
        ]),
    )


async def add_supplier_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_supplier_name"] = True
    await update.effective_message.reply_text("Напиши название нового поставщика одним сообщением.")


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data or ""
    if action == "supplier:new":
        context.user_data["awaiting_supplier_name"] = True
        await query.message.reply_text("Напиши название нового поставщика одним сообщением.")
    elif action == "supplier:choose":
        await query.message.reply_text("Выбери поставщика:", reply_markup=supplier_keyboard())
    elif action.startswith("supplier:"):
        try:
            supplier_id = int(action.split(":", 1)[1])
        except ValueError:
            return
        suppliers = list_suppliers()
        matched = next((s for s in suppliers if s["id"] == supplier_id), None)
        if not matched:
            await query.message.reply_text("Поставщик не найден. Открой /suppliers ещё раз.")
            return
        context.user_data["selected_supplier_id"] = supplier_id
        context.user_data["selected_supplier_name"] = matched["name"]
        await query.message.reply_text(
            f"✅ Выбран поставщик: {matched['name']}\n"
            "Теперь можешь отправить текстовый прайс."
        )
        if context.user_data.get("pending_prices"):
            await query.message.reply_text(
                f"📋 Ожидающий прайс: {len(context.user_data['pending_prices'])} позиций.\n"
                "Поставщик назначен. Цены ещё НЕ сохранены."
            )


async def send_preview(message, products, supplier_name=None):
    result = [
        "✅ Прайс распознан!",
        f"Позиций: {len(products)}",
        f"Поставщик: {supplier_name or 'не определён'}",
        "",
        "Первые 5 товаров:",
    ]
    for name, price in products[:5]:
        product = parse_product(name)
        result.append(f"\n📱 {name}")
        result.append(f"💰 {price} (валюта пока неизвестна)")
        result.append(f"Модель: {product['model'] or 'не определена'}")
        result.append(f"Память: {product['storage_gb'] or 'не указана'}")
        result.append(f"Цвет: {product['color'] or 'не указан'}")
        result.append(f"Регион: {product['region'] or 'не указан'}")
        result.append(f"SIM: {product['sim'] or 'не указана'}")
    if len(products) > 5:
        result.append(f"\nИ ещё {len(products) - 5} позиций.")
    result.append("\n⚠️ Тестовый режим: предложения пока не сохраняются в БД.")
    await message.reply_text("\n".join(result)[:4000])


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return
    text = message.text.strip()
    if context.user_data.pop("awaiting_supplier_name", False):
        if len(text) > 120 or not text or "\n" in text:
            context.user_data["awaiting_supplier_name"] = True
            await message.reply_text("Название должно быть одной строкой до 120 символов. Напиши снова.")
            return
        supplier_id = add_supplier(text)
        context.user_data["selected_supplier_id"] = supplier_id
        context.user_data["selected_supplier_name"] = text
        await message.reply_text(f"✅ Поставщик «{text}» добавлен и выбран.")
        pending = context.user_data.get("pending_prices")
        if pending:
            await message.reply_text(
                f"📋 Ожидающий прайс: {len(pending)} позиций.\n"
                "Привязка выполнена. Сохранение цен добавим следующим шагом."
            )
        return

    products = parse_price_list(text)
    if not products:
        await message.reply_text("Не удалось распознать товары и цены.")
        return

    context.user_data["pending_prices"] = products
    supplier_name = context.user_data.get("selected_supplier_name")
    await send_preview(message, products, supplier_name)
    if not supplier_name:
        await message.reply_text(
            "К какому поставщику относится этот прайс?",
            reply_markup=supplier_keyboard(),
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Ошибка: %s", context.error, exc_info=context.error)


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    logger.info("Проверяем подключение PostgreSQL...")
    init_db()
    logger.info("PostgreSQL: подключение успешно, таблицы готовы")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("suppliers", suppliers_command))
    app.add_handler(CommandHandler("addsupplier", add_supplier_command))
    app.add_handler(CallbackQueryHandler(callbacks, pattern=r"^supplier:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    logger.info("Mobi Price Finder запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
