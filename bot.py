
import os
import re
import html
from product_parser import parse_product


from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


def parse_price_list(text):
    text = html.unescape(text)
    lines = [line.strip(" *\\\t\r") for line in text.splitlines()]
    lines = [line for line in lines if line]

    products = []
    pending_name = None

    number = r"\d+(?:[.,]\d{1,2})?"

    for line in lines:
        # Цена отдельной строкой
        if re.fullmatch(number, line):
            if pending_name:
                products.append((pending_name, line))
                pending_name = None
            continue

        # Название и цена в одной строке
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
    await update.message.reply_text(
        "👋 Привет! Я Mobi Price Finder.\n\n"
        "Пришли мне текстовый прайс, "
        "и я попробую распознать товары и цены."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products = parse_price_list(update.message.text)

    if not products:
        await update.message.reply_text(
            "Не удалось распознать товары и цены."
        )
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

    if product["model"]:
        result.append(f"✅ Модель: {product['model']}")
    else:
        result.append("⚠️ Модель не определена")

    if product["storage_gb"]:
        result.append(
            f"💾 Память: {product['storage_gb']} GB"
        )

    if product["color"]:
        result.append(f"🎨 Цвет: {product['color']}")

    result.append("")


    if len(products) > 10:
        result.append(f"\nИ ещё {len(products) - 10} позиций.")

    result.append(
        "\n⚠️ Тестовый режим: товары пока не сохраняются."
    )

    await update.message.reply_text("\n".join(result))


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    print("Mobi Price Finder запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
