import asyncio
import logging
import random
import time
from datetime import datetime, timedelta

import httpx
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from .db import Control, Listing, Match, Notice, Run, Search, User, dialect_insert, now
from .source import SourceError, fetch, matches


async def snapshot(db, searches, ads, max_age):
    detected = now()
    for ad in sorted(ads, key=lambda a: a.get('published') or detected.isoformat()):
        matching = [s for s in searches if matches(ad, s.filters)]
        if not matching:
            continue
        stmt = dialect_insert(db, Listing).values(id=ad['id'], data=ad, first_seen=detected, last_seen=detected)
        await db.execute(stmt.on_conflict_do_update(index_elements=['id'], set_={'data': ad, 'last_seen': detected}))
        for s in matching:
            fresh = bool(s.initialized)
            stmt = dialect_insert(db, Match).values(search_id=s.id, listing_id=ad['id'], new=fresh)
            added = await db.scalar(stmt.on_conflict_do_nothing().returning(Match.search_id))
            if not added or not fresh or not (await db.get(User, s.user_id)).enabled:
                continue
            if max_age and (not ad['published'] or datetime.fromisoformat(ad['published']) < detected - timedelta(hours=max_age)):
                continue
            await db.execute(dialect_insert(db, Notice).values(user_id=s.user_id, listing_id=ad['id']).on_conflict_do_nothing())
    for s in searches:
        s.initialized = s.initialized or detected
        s.success = detected
        s.failures = 0
        s.due = detected + timedelta(seconds=s.interval + random.uniform(0, 5))


async def cycle(sessions, client, config, bot):
    async with sessions() as db:
        ctl = await db.get(Control, 1)
        if ctl.paused or (ctl.cooldown and ctl.cooldown > now()):
            return
        due = list(await db.scalars(select(Search).where(Search.active, Search.due <= now()).order_by(Search.due)))
    groups = {}
    for s in due:
        groups.setdefault(s.key, []).append(s)
    results = []
    for group in list(groups.values())[:4]:
        start = time.monotonic()
        error = None
        ads = []
        stats = {'requests': 0}
        try:
            ads = await fetch(client, group[0].url, stats=stats)
        except (SourceError, httpx.HTTPError) as exc:
            error = exc if isinstance(exc, SourceError) else SourceError(type(exc).__name__)
        results.append((group, ads, error, int((time.monotonic()-start)*1000), stats['requests']))
        if error and error.delay:
            break
        await asyncio.sleep(1)
    alerts = []
    async with sessions.begin() as db:
        for group, ads, error, elapsed, requests in results:
            ids = [s.id for s in group]
            searches = list(await db.scalars(select(Search).where(Search.id.in_(ids), Search.active,
                                   Search.key == group[0].key, Search.due <= now()).with_for_update()))
            db.add(Run(key=group[0].key, elapsed_ms=elapsed, requests=requests, found=len(ads), error=str(error) if error else None))
            if not error:
                await snapshot(db, searches, ads, config.max_age_hours)
                continue
            logging.warning('source_error key=%s error=%s', group[0].key, error)
            if error.delay:
                (await db.get(Control, 1)).cooldown = now() + timedelta(seconds=error.delay)
            if any(s.failures == 0 for s in searches):
                alerts.append(str(error))
            for s in searches:
                s.failures += 1
                delay = max(error.delay, min(3600, 60 * 2 ** min(s.failures, 6)))
                s.due = now() + timedelta(seconds=delay + random.uniform(1, 10))
    for msg in set(alerts):
        for uid in config.admin_ids:
            try:
                await bot.send_message(uid, 'Ошибка мониторинга OLX: ' + msg)
            except Exception as exc:
                logging.warning('admin_alert_%s', type(exc).__name__)


def details(ad, first):
    price = f"{ad['price']} {ad['currency']}" if ad['price'] is not None else 'не указана'
    date = 'Опубликовано: ' + ad['published'] if ad.get('published') else 'Обнаружено: ' + first.isoformat(timespec='minutes')
    return f"{ad['title'][:220]}\n💰 Цена: {price}\n📍 {ad.get('city') or 'Место не указано'}\n🕐 {date}"


async def deliver(sessions, bot):
    async with sessions.begin() as db:
        ctl = await db.get(Control, 1)
        if ctl.telegram_due and ctl.telegram_due > now():
            return
        n = await db.scalar(select(Notice).where(Notice.status == 'pending', Notice.due <= now())
                            .order_by(Notice.due).limit(1).with_for_update(skip_locked=True))
        if not n:
            return
        names = list(await db.scalars(select(Search.name).join(Match).where(Search.user_id == n.user_id,
                    Search.active, Match.new, Match.listing_id == n.listing_id).order_by(Search.id)))
        user = await db.get(User, n.user_id)
        if not user.enabled or not names:
            n.status = 'skipped'
            return
        item = await db.get(Listing, n.listing_id)
        ad = item.data
        caption = '🔔 Новое объявление\n' + details(ad, item.first_seen) + '\n🔎 ' + ', '.join(names)
        caption = caption[:500]
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='🔗 Открыть на OLX', url=ad['url'])],
            [InlineKeyboardButton(text='📋 Мои поиски', callback_data='list:0')]])
        n.attempts += 1
        try:
            message = None
            if ad.get('photo'):
                try:
                    message = await bot.send_photo(user.id, ad['photo'], caption=caption, reply_markup=buttons)
                except TelegramBadRequest:
                    pass
            if message is None:
                message = await bot.send_message(user.id, caption, reply_markup=buttons)
            n.sent, n.message_id, n.status = now(), message.message_id, 'sent'
        except TelegramForbiddenError:
            user.enabled, n.status = False, 'blocked'
        except TelegramRetryAfter as exc:
            ctl.telegram_due = n.due = now() + timedelta(seconds=exc.retry_after + 1)
        except Exception as exc:
            logging.warning('delivery_%s', type(exc).__name__)
            n.due = now() + timedelta(seconds=min(3600, 2 ** min(n.attempts, 12)))


async def repeat(job, seconds, *args):
    while True:
        try:
            await job(*args)
        except Exception as exc:
            logging.error('%s_%s', job.__name__, type(exc).__name__)
        await asyncio.sleep(seconds)
