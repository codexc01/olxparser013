import asyncio
import random
import time
from datetime import timedelta
from urllib.parse import quote

from aiogram import BaseMiddleware, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from sqlalchemy import distinct, func, select, update
from sqlalchemy.dialects.postgresql import insert

from .db import Control, Listing, Match, Notice, Run, Search, User, now
from .monitor import details
from .source import key, url

MENU = ['🔎 Добавить поиск', '📋 Мои поиски', '🆕 Последние объявления',
        '📊 Статистика', '⚙️ Настройки', '❓ Помощь']


def reply(labels, width=2):
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=v) for v in labels[i:i+width]]
                               for i in range(0, len(labels), width)], resize_keyboard=True)


def inline(items):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=v)] for t,v in items])


class Wizard(StatesGroup):
    input = State()


LABEL = dict(url='Вставьте ссылку на поисковую выдачу OLX.uz', name='Название поиска (до 80 символов)',
             query='Что искать на OLX?', category='Категория', region='Область как в OLX', city='Город как в OLX',
             min='Минимальная цена', max='Максимальная цена', currency='Валюта цены',
             condition='Состояние товара', attribute='Дополнительный атрибут OLX в формате ключ=значение, например mobile_phone_manufacturer=Apple', words='Обязательные слова или фраза', mode='Режим совпадения',
             exclude='Исключающие слова через запятую', missing='Показывать объявления без цены?',
             interval='Интервал проверки в минутах', confirm='Сохранить поиск?')
CHOICES = dict(category=['Электроника', 'Транспорт', 'Недвижимость'], interval=['3','5'],
               mode=['все','любое','фраза'], missing=['да','нет'], currency=['UZS','USD'],
               condition=['Новый','Б/у'], confirm=['Сохранить'])
CATEGORIES = {'Электроника':'elektronika','Транспорт':'transport','Недвижимость':'nedvizhimost'}
FILTERS = ['region','city','min','max','currency','condition','attribute','words','mode','exclude','missing','interval','confirm']


def address(d):
    if d.get('url'):
        return url(d['url'])
    parts = [CATEGORIES.get(d.get('category')), 'q-' + quote(d['query'].strip().replace(' ', '-'), safe='') if d.get('query') else None]
    if not any(parts):
        raise ValueError('Укажите категорию или запрос')
    return 'https://www.olx.uz/' + '/'.join(p for p in parts if p) + '/'


async def prompt(m, state):
    data = await state.get_data()
    k = data['steps'][data['pos']]
    buttons = list(CHOICES.get(k, []))
    if k not in ('url','name','confirm'):
        buttons.append('Пропустить')
    header = LABEL[k]
    if k == 'confirm':
        d = data['values']
        header = f"{d['name']}\n{address(d)}\n" + '\n'.join(f'{LABEL[x]}: {d[x]}' for x in FILTERS if x in d) + '\nСохранить поиск?'
    await m.answer(header, reply_markup=reply(buttons + ['Назад','Отмена']))


class Access(BaseMiddleware):
    def __init__(self):
        self.last = {}

    async def __call__(self, handler, event, data):
        m = event.message if isinstance(event, CallbackQuery) else event
        if not m or m.chat.type != 'private':
            return
        uid = event.from_user.id
        t = time.monotonic()
        if t - self.last.get(uid, 0) < 0.45:
            if isinstance(event, CallbackQuery):
                await event.answer('Попробуйте через секунду')
            return
        self.last[uid] = t
        if len(self.last) > 10000:
            self.last = {k:v for k,v in self.last.items() if t-v < 60}
        async with data['sessions'].begin() as db:
            await db.execute(insert(User).values(id=uid).on_conflict_do_nothing())
            data['db'] = db
            data['user'] = await db.get(User, uid)
            return await handler(event, data)


def setup(dp: Dispatcher):
    access = Access()
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)

    @dp.message(CommandStart())
    async def start(m: Message, state: FSMContext):
        await state.clear()
        await m.answer('👋 Добро пожаловать!\n\nЭтот бот автоматически отслеживает новые объявления OLX.uz\n\nСоздайте поиск, укажите интересующие параметры и получайте уведомления прямо в Telegram\n\n🔔 Проверка каждые 3 или 5 минут', reply_markup=reply(MENU))

    @dp.message(F.text == MENU[0])
    async def add(m, state: FSMContext):
        await state.clear()
        await m.answer('Выберите способ добавления поиска', reply_markup=inline([('🔗 По ссылке OLX','add:url'),('⚙️ Настроить вручную','add:manual')]))

    @dp.callback_query(F.data.startswith('add:'))
    async def begin(c, state: FSMContext, user):
        await c.answer()
        await state.set_state(Wizard.input)
        steps = (['url','name'] if c.data == 'add:url' else ['name','query','category']) + FILTERS
        values = {'interval':user.interval//60}
        if user.region:
            values['region'] = user.region
        if user.currency:
            values['currency'] = user.currency
        await state.set_data({'steps':steps,'pos':0,'values':values})
        await prompt(c.message, state)

    @dp.message(Wizard.input)
    async def step(m, state: FSMContext, user, db):
        d = await state.get_data()
        steps, pos, values = d['steps'], d['pos'], d['values']
        field = steps[pos]
        value = (m.text or '').strip()
        if value == 'Отмена' or value == 'Назад' and pos == 0:
            await state.clear()
            return await m.answer('Настройка закрыта', reply_markup=reply(MENU))
        if value == 'Назад':
            await state.update_data(pos=pos-1)
            return await prompt(m, state)
        try:
            if field == 'confirm':
                if value != 'Сохранить':
                    raise ValueError('Нажмите «Сохранить»')
                source = address(values)
                filters = {k:values[k] for k in FILTERS if k in values and k != 'interval'}
                s = None
                if d.get('edit'):
                    s = await db.scalar(select(Search).where(Search.id==d['edit'], Search.user_id==user.id).with_for_update())
                    if not s:
                        raise ValueError('Поиск удалён')
                if s is None:
                    s = Search(user_id=user.id)
                    db.add(s)
                if s.key != key(source) or s.filters != filters:
                    s.initialized = None
                s.name, s.url, s.key, s.filters = values['name'], source, key(source), filters
                s.interval = int(values.get('interval',user.interval//60))*60
                s.due = now() + timedelta(seconds=random.uniform(1,8))
                await db.flush()
                await state.clear()
                return await m.answer('✅ Поиск сохранён', reply_markup=reply(MENU))
            if value == 'Пропустить' and field not in ('name','url'):
                if field not in ('region','currency','interval'):
                    values.pop(field, None)
            else:
                if not value or len(value) > (2000 if field == 'url' else 80 if field=='name' else 150):
                    raise ValueError('Проверьте длину значения')
                if field in CHOICES and value not in CHOICES[field]:
                    raise ValueError('Выберите значение кнопкой')
                if field == 'url':
                    value = url(value)
                if field == 'attribute' and ('=' not in value or not all(x.strip() for x in value.split('=',1))):
                    raise ValueError('Укажите ключ и значение через знак равенства')
                if field in ('min','max'):
                    if not value.isdecimal() or len(value)>15:
                        raise ValueError('Введите неотрицательное целое число')
                    value = int(value)
                    if field=='max' and value < values.get('min',0):
                        raise ValueError('Максимальная цена меньше минимальной')
                values[field] = value
            if steps[pos+1]=='confirm':
                address(values)
                if ('min' in values or 'max' in values) and not values.get('currency'):
                    raise ValueError('Для сравнения цен вернитесь и выберите валюту')
        except ValueError as exc:
            return await m.answer(str(exc))
        await state.update_data(values=values,pos=pos+1)
        await prompt(m,state)

    async def show_list(m, db, uid, offset):
        rows = list(await db.scalars(select(Search).where(Search.user_id==uid).order_by(Search.id).offset(offset).limit(11)))
        buttons = [(('🟢 ' if s.active else '⏸ ') + s.name, f'view:{s.id}') for s in rows[:10]]
        if offset:
            buttons.append(('Назад', f'list:{max(0,offset-10)}'))
        if len(rows)>10:
            buttons.append(('Далее', f'list:{offset+10}'))
        await m.answer('Ваши поиски' if rows else 'Пока нет поисков', reply_markup=inline(buttons) if buttons else reply(MENU))

    @dp.callback_query(F.data.startswith('list:'))
    async def page(c, db, user):
        await c.answer()
        await show_list(c.message, db, user.id, int(c.data.split(':')[1]))

    @dp.callback_query(F.data.regexp(r'^(view|active|check|edit|remove|delete):\d+$'))
    async def manage(c, db, user, state: FSMContext):
        action, sid = c.data.split(':')
        s = await db.scalar(select(Search).where(Search.id==int(sid), Search.user_id==user.id).with_for_update())
        if not s:
            return await c.answer('Поиск недоступен')
        await c.answer()
        if action == 'delete':
            await db.delete(s)
            return await c.message.answer('Поиск удалён')
        if action == 'remove':
            return await c.message.answer('Удалить поиск и его историю?', reply_markup=inline([('Удалить',f'delete:{sid}'),('Отмена',f'view:{sid}')]))
        if action == 'edit':
            await state.set_state(Wizard.input)
            await state.set_data(dict(edit=s.id, pos=0, steps=['url','name']+FILTERS,
                                      values=dict(s.filters, url=s.url, name=s.name, interval=s.interval//60)))
            return await prompt(c.message,state)
        if action == 'active':
            s.active = not s.active
            if s.active:
                s.due = now()
        if action == 'check':
            if not s.active or s.manual and now()-s.manual < timedelta(seconds=60):
                return await c.message.answer('Проверка недоступна, проверьте статус и подождите минуту')
            s.due = s.manual = now()
        count = await db.scalar(select(func.count()).select_from(Match).where(Match.search_id==s.id,Match.new))
        checked = s.success.isoformat(timespec='minutes') if s.success else 'ещё не выполнена'
        text = f'{s.name}\n{s.url}\nИнтервал: {s.interval//60} мин\nСтатус: {"активен" if s.active else "пауза"}\nПоследняя проверка: {checked}\nНовых: {count}'
        for k,v in s.filters.items():
            text += f'\n{LABEL.get(k,k)}: {v}'
        keyboard = inline([('⏸ Приостановить' if s.active else '▶️ Запустить',f'active:{sid}'),
                      ('🔄 Проверить сейчас',f'check:{sid}'),('✏️ Изменить',f'edit:{sid}'),('🗑 Удалить',f'remove:{sid}')])
        keyboard.inline_keyboard.append([InlineKeyboardButton(text='Открыть поиск на OLX',url=s.url)])
        await c.message.answer(text,reply_markup=keyboard)

    async def settings(m,user):
        await m.answer(f'Язык: русский\nРегион: {user.region or "любой"}\nВалюта: {user.currency or "любая"}',reply_markup=inline([
            ('🔔 '+('Включены' if user.enabled else 'Выключены'),'pref:enabled'),(f'⏱ {user.interval//60} мин','pref:interval'),
            ('💰 Сменить валюту','pref:currency'),('📍 Регион по умолчанию','pref:region'),('Удалить мои данные','pref:erase')]))

    @dp.callback_query(F.data.startswith('pref:'))
    async def prefs(c,user,db,state: FSMContext):
        await c.answer()
        action = c.data.split(':')[1]
        if action=='enabled':
            user.enabled = not user.enabled
        elif action=='interval':
            user.interval = 480-user.interval
        elif action=='currency':
            user.currency = {'':'UZS','UZS':'USD','USD':''}[user.currency]
        elif action=='region':
            await state.set_state('region_input')
            return await c.message.answer('Введите область или «Пропустить» для любого региона', reply_markup=reply(['Пропустить','Отмена']))
        elif action=='erase':
            return await c.message.answer('Удалить все ваши данные?',reply_markup=inline([('Да, удалить','pref:confirm')]))
        elif action=='confirm':
            await db.delete(user)
            await state.clear()
            return await c.message.answer('Ваши данные удалены')
        await settings(c.message,user)

    @dp.message(StateFilter('region_input'))
    async def region(m,user,state: FSMContext):
        if not m.text or len(m.text)>100:
            return await m.answer('Введите до 100 символов')
        if m.text!='Отмена':
            user.region = '' if m.text=='Пропустить' else m.text
        await state.clear()
        await m.answer('Настройки сохранены',reply_markup=reply(MENU))

    @dp.message(Command('admin'))
    async def admin(m,user,db,config):
        if user.id not in config.admin_ids:
            return await m.answer('Нет доступа')
        ctl = await db.get(Control,1)
        async def count(model, *conditions):
            return await db.scalar(select(func.count()).select_from(model).where(*conditions))
        active = await db.scalar(select(func.count(distinct(Search.user_id))).where(Search.active))
        requests = await db.scalar(select(func.sum(Run.requests)))
        avg = await db.scalar(select(func.avg(Run.elapsed_ms)))
        latest = await db.scalar(select(Run).order_by(Run.id.desc()).limit(1))
        errors = list(await db.scalars(select(Run.error).where(Run.error.is_not(None)).order_by(Run.id.desc()).limit(3)))
        await m.answer(f'Пользователей: {await count(User)}, активных: {active}\nАктивных поисков: {await count(Search,Search.active)}\n'
            f'Запросов к OLX: {requests or 0}\nПроверок выдачи: {await count(Run)}, ошибок: {await count(Run,Run.error.is_not(None))}\n'
            f'Среднее время: {int(avg or 0)} мс\nОтправлено: {await count(Notice,Notice.status=="sent")}\n'
            f'В очереди: {await count(Notice,Notice.status=="pending")}\nПланировщик: {"пауза" if ctl.paused else "включён"}\n'
            f'Последняя проверка OLX: {latest.created if latest else "нет"}\nОшибки: {", ".join(errors) or "нет"}',
            reply_markup=inline([('Приостановить','admin:pause'),('Возобновить','admin:resume'),('Перезапустить проверки','admin:restart')]))

    @dp.callback_query(F.data.startswith('admin:'))
    async def control(c,user,db,config):
        if user.id not in config.admin_ids:
            return await c.answer('Нет доступа')
        ctl = await db.get(Control,1)
        ctl.paused = c.data=='admin:pause'
        if c.data=='admin:restart':
            await db.execute(update(Search).where(Search.active).values(due=now()))
        await c.answer('Настройка применена')

    @dp.message()
    async def menu(m,user,db):
        if m.text==MENU[1]:
            return await show_list(m,db,user.id,0)
        if m.text==MENU[4]:
            return await settings(m,user)
        if m.text==MENU[2]:
            ids = select(Match.listing_id).join(Search).where(Search.user_id==user.id)
            rows = list(await db.scalars(select(Listing).where(Listing.id.in_(ids)).order_by(Listing.first_seen.desc()).limit(5)))
            if not rows:
                return await m.answer('Объявлений пока нет')
            for item in rows:
                await m.answer(details(item.data,item.first_seen)+'\n'+item.data['url'])
        elif m.text==MENU[3]:
            counts=[]
            for days in (1,7):
                counts.append(await db.scalar(select(func.count(distinct(Match.listing_id))).join(Search).where(
                    Search.user_id==user.id,Match.new, Match.created>=now()-timedelta(days=days))))
            active = await db.scalar(select(func.count()).select_from(Search).where(Search.user_id==user.id,Search.active))
            sent = await db.scalar(select(func.count()).select_from(Notice).where(Notice.user_id==user.id,Notice.status=='sent'))
            checked = await db.scalar(select(func.max(Search.success)).where(Search.user_id==user.id))
            errors = await db.scalar(select(func.count()).select_from(Run).where(Run.error.is_not(None),
                     Run.key.in_(select(Search.key).where(Search.user_id==user.id))))
            await m.answer(f'Активных поисков: {active}\nНовых за сутки: {counts[0]}, за неделю: {counts[1]}\n'
                           f'Отправлено: {sent}\nПоследняя проверка: {checked or "ещё не было"}\nОшибок: {errors}')
        else:
            await m.answer('Откройте «Добавить поиск» и вставьте ссылку с фильтрами OLX или настройте запрос вручную\n'
                           'Первый снимок доступен в последних объявлениях. Уведомления начнутся со следующей проверки\n'
                           'Цены сравниваются только в выбранной валюте', reply_markup=reply(MENU))
