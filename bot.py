import os
import re
import html
import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from product_parser import parse_product

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def parse_price_list(text: str):
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
            rf"(.+?)\s+({number})\s*(?:\$|USD)?",
            line,
            re.IGNORECASE,
        )
        if match:
            name, price = match.groups()
            products.append((name.strip(), price))
            pending_name = None
        else:
            pending_name = line

    return products


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👋 Привет! Я Mobi Price Finder.\n\n"
        "Отправь мне текстовый прайс, и я попробую распознать товары и цены."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    products = parse_price_list(message.text)
    if not products:
        await message.reply_text("Не удалось распознать товары и цены.")
        return

    result = [
        "✅ Прайс обработан!",
        f"Распознано позиций: {len(products)}",
        "",
        "Первые 10 товаров:",
    ]

    for name, price in products[:10]:
        product = parse_product(name)
        result.append(f"📱 {name}")
        result.append(f"💰 Цена: {price}")
        result.append(
            f"✅ Модель: {product['model']}"
            if product['model'] else "⚠️ Модель не определена"
        )
        if product['storage_gb'] is not None:
            result.append(f"💾 Память: {product['storage_gb']} GB")
        if product['color']:
            result.append(f"🎨 Цвет: {product['color']}")
        result.append("")

    if len(products) > 10:
        result.append(f"И ещё {len(products) - 10} позиций.")

    result.append("⚠️ Тестовый режим: товары пока не сохраняются.")
    await message.reply_text("\n".join(result)[:4000])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Ошибка обработки сообщения: %s", context.error, exc_info=context.error)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("В Railway отсутствует TELEGRAM_BOT_TOKEN")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    logger.info("Mobi Price Finder запускается")
    app.run_polling()


if __name__ == "__main__":
    main()
