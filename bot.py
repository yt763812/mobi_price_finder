
import os

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я Mobi Price Finder.\n\n"
        "Скоро я научусь анализировать прайсы "
        "поставщиков и искать лучшие закупочные цены."
    )


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))

    print("Mobi Price Finder запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
