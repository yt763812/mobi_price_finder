"""Restricted Telegram interface for manual sync and one-of-two review."""
import asyncio
import logging
from telegram import InlineKeyboardButton,InlineKeyboardMarkup
from telegram.ext import CommandHandler,CallbackQueryHandler
from . import store

log = logging.getLogger(__name__)


def can_launch(update):
    return bool(update.effective_user and update.effective_user.id in store.launch_ids())


def can_approve(update):
    return bool(update.effective_user and update.effective_user.id in store.approval_ids())


def panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🔄 Запустить сверку',callback_data='onl:run')],
        [InlineKeyboardButton('📊 Статус',callback_data='onl:status'),
         InlineKeyboardButton('📦 Кандидаты',callback_data='onl:list:0')]
    ])


async def command(update,context):
    if not can_launch(update):
        await update.effective_message.reply_text('🔒 Раздел доступен только администраторам Onlíner.')
        return
    await update.effective_message.reply_text('🛍 Onlíner — управление ассортиментом\nПубликация через API отключена.',reply_markup=panel_keyboard())


async def show_candidates(message,offset=0):
    rows = await asyncio.to_thread(store.list_candidates,offset,5)
    if not rows:
        await message.reply_text('Нет кандидатов, ожидающих решения.')
        return
    for item in rows:
        price = f'{item["price_byn"]} BYN' if item['price_byn'] is not None else 'не указана'
        text = (f'📦 #{item["id"]} · {item["category"]}\n{item["model"]}\n'
                f'Цвет: {item["color"] or "—"}\nЦена MobiStore: {price}\n'
                f'Статус: {item["status"]}\nПричина: {item["reason"][:250]}\n'
                f'productId-кандидаты: {item["candidate_product_ids"][:150] or "—"}')
        keys = [[InlineKeyboardButton('🔎 Подробнее',callback_data=f'onl:item:{item["id"]}')]]
        await message.reply_text(text,reply_markup=InlineKeyboardMarkup(keys))
    if len(rows) == 5:
        await message.reply_text('Следующие позиции:',reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('➡️ Далее',callback_data=f'onl:list:{offset+5}')]]))


async def callback(update,context):
    query = update.callback_query
    if not can_launch(update):
        await query.answer('Нет доступа',show_alert=True)
        return
    action = (query.data or '').split(':')
    if len(action)<2:
        await query.answer('Некорректная команда')
        return
    if action[1]=='run':
        job_id,created = await asyncio.to_thread(store.enqueue,update.effective_user.id)
        await query.answer('Задание создано' if created else 'Задание уже выполняется')
        await query.message.reply_text((f'🔄 Сверка #{job_id} поставлена в очередь. Бот продолжает работать.' if created else
                                        f'⏳ Сверка #{job_id} уже запущена или ожидает выполнения.'))
    elif action[1]=='status':
        job=await asyncio.to_thread(store.current_job)
        await query.answer()
        if job:
            text = f'Сверка #{job["id"]}\nСостояние: {job["state"]}\nЭтап: {job["stage"]}'
            if job['state']=='completed':
                text += f'\nРезультат: {job["summary"]}'
            elif job['state']=='failed':
                text += f'\nОшибка: {str(job["error"])[:400]}'
        else:text='Сверок пока не было.'
        await query.message.reply_text(text[:3900])
    elif action[1]=='list':
        await query.answer()
        offset=int(action[2]) if len(action)>2 and action[2].isdigit() else 0
        await show_candidates(query.message,min(offset,10000))
    elif action[1]=='item' and len(action)>=3 and action[2].isdigit():
        item=await asyncio.to_thread(store.get_candidate,int(action[2]))
        await query.answer()
        if not item:
            await query.message.reply_text('Позиция не найдена.')
            return
        decision_info=(f'Решение: {item["status"]}, администратор {item["decision_by"]}'
                       if item['decision_by'] else 'Решение ещё не принято.')
        keys=[]
        if item['status'] in ('pending','needs_review') and can_approve(update):
            keys=[[InlineKeyboardButton('✅ Одобрить',callback_data=f'onl:decide:{item["id"]}:approved'),
                   InlineKeyboardButton('❌ Отклонить',callback_data=f'onl:decide:{item["id"]}:rejected')]]
        await query.message.reply_text(
            f'#{item["id"]} {item["model"]} ({item["color"]})\n'
            f'Причина: {item["reason"]}\n{decision_info}\nПубликации не будет.',
            reply_markup=InlineKeyboardMarkup(keys) if keys else None)
    elif action[1]=='decide' and len(action)==4 and action[2].isdigit():
        if not can_approve(update):
            await query.answer('Только два согласующих администратора могут принять решение',show_alert=True)
            return
        candidate_id=int(action[2]); target=action[3]
        if target not in ('approved','rejected'):
            await query.answer('Некорректное решение',show_alert=True)
            return
        result=await asyncio.to_thread(store.decide,candidate_id,update.effective_user.id,target)
        await query.answer('Решение сохранено' if result=='ok' else 'Решение уже принято или позиция не найдена',show_alert=result!='ok')
        item=await asyncio.to_thread(store.get_candidate,candidate_id)
        if item:
            try:await query.edit_message_reply_markup(reply_markup=None)
            except Exception:pass
            await query.message.reply_text(f'#{candidate_id}: {item["status"]}. Решение администратора {item["decision_by"]}. Никакой публикации не выполнялось.')


def register(app):
    app.add_handler(CommandHandler('onliner',command))
    app.add_handler(CallbackQueryHandler(callback,pattern=r'^onl:'))
