import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from core import Store
from olx import APIError, OLX


def keys(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=value) for text, value in row] for row in rows])


MENU = keys([("Добавить тему", "add"), ("Мои темы", "list:0")])
PROMPTS = {"query": "Что ищем на OLX?", "city": "Введи город как в объявлении или - для любого города",
           "words": "Введи обязательные слова через запятую или - без фильтра",
           "cap": "Введи максимум и валюту, например 450 USD или 5000000 UZS\nДля любой цены отправь -"}


def values(field, value):
    value = value.strip()
    if not 0 < len(value) <= 150:
        raise ValueError("Введи от 1 до 150 символов")
    if field == "cap":
        if value == "-":
            return {"cap": None, "currency": ""}
        try:
            number, currency = value.upper().split()
            amount = float(number.replace(",", "."))
            if currency not in ("USD", "UZS") or not math.isfinite(amount) or amount < 0:
                raise ValueError
        except ValueError:
            raise ValueError(PROMPTS["cap"]) from None
        return {"cap": amount, "currency": currency}
    return {field: "" if value == "-" and field != "query" else value}


def interface(store, owner):
    r = Router()
    r.message.filter(F.from_user.id == owner, F.chat.type == ChatType.PRIVATE)
    r.callback_query.filter(F.from_user.id == owner, F.message.chat.type == ChatType.PRIVATE)
    editing = []
    db = store.db

    async def card(m, key):
        t = store.topic(key)
        if not t:
            return await m.answer("Тема удалена", reply_markup=MENU)
        price = "Любая" if t["cap"] is None else f"{t['cap']:g} {t['currency']}"
        status = "В работе" if t["active"] else "На паузе"
        rows = [[(name, f"{field}:{key}") for name, field in pair] for pair in [
            [("Запрос", "query"), ("Цена", "cap")], [("Город", "city"), ("Слова", "words")],
            [("Пауза" if t["active"] else "Возобновить", "toggle"), ("Проверить", "check")],
            [("Удалить", "delete"), ("Мои темы", "list")]]]
        rows[-1][-1] = ("Мои темы", "list:0")
        await m.answer(f"{t['query']} · {status}\nЦена до: {price}\nГород: {t['city'] or 'Любой'}\n"
                       f"Слова: {t['words'] or 'Любые'}", reply_markup=keys(*rows))

    @r.message(Command("start", "menu", "cancel"))
    async def menu(m: Message):
        editing.clear()
        await m.answer("Что будем искать?", reply_markup=MENU)

    @r.callback_query()
    async def button(c: CallbackQuery):
        await c.answer()
        editing.clear()
        action, _, arg = (c.data or "").partition(":")
        key = int(arg) if arg.isdecimal() else 0
        m = c.message
        if action == "list":
            topics = db.execute("SELECT * FROM topics ORDER BY id LIMIT 9 OFFSET ?", (key,)).fetchall()
            rows = [[(("🟢 " if t["active"] else "⏸ ") + t["query"][:40], f"show:{t['id']}")] for t in topics[:8]]
            nav = []
            if key:
                nav.append(("Назад", f"list:{max(0, key-8)}"))
            if len(topics) > 8:
                nav.append(("Дальше", f"list:{key+8}"))
            return await m.answer("Мои темы" if topics else "Тем пока нет",
                                  reply_markup=keys(*rows, *([nav] if nav else []), [("Добавить тему", "add")]))
        if action == "add" or (action in PROMPTS and store.topic(key)):
            editing.append(("query" if action == "add" else action, key))
            return await m.answer(PROMPTS[editing[0][0]], reply_markup=keys([("Отмена", "list:0")]))
        t = store.topic(key)
        if t:
            with db:
                if action == "toggle":
                    db.execute("UPDATE topics SET active=1-active, since=?, revision=revision+1, due=0 WHERE id=?",
                               (time.time(), key))
                    db.execute("DELETE FROM pending WHERE topic=?", (key,))
                elif action == "check":
                    if not t["active"]:
                        return await m.answer("Сначала возобнови мониторинг темы")
                    db.execute("UPDATE topics SET due=? WHERE id=?", (min(time.time(), t["due"]), key))
                    db.commit()
                    return await m.answer("Тема в очереди на проверку")
                elif action == "delete":
                    return await m.answer("Удалить эту тему?", reply_markup=keys(
                        [("Удалить", f"confirm:{key}"), ("Оставить", f"show:{key}")]))
                elif action == "confirm":
                    db.execute("DELETE FROM topics WHERE id=?", (key,))
        await card(m, key)

    @r.message(F.text)
    async def entry(m: Message):
        if not editing:
            return await menu(m)
        field, key = editing[0]
        try:
            fields = values(field, m.text)
        except ValueError as exc:
            return await m.answer(str(exc))
        with db:
            if key:
                db.execute("UPDATE topics SET " + ", ".join(f"{k}=?" for k in fields)
                           + ", revision=revision+1, due=0, since=? WHERE id=?", (*fields.values(), time.time(), key))
                db.execute("DELETE FROM pending WHERE topic=?", (key,))
                if field == "query":
                    db.execute("DELETE FROM seen WHERE topic=?", (key,))
            else:
                key = db.execute("INSERT INTO topics(query,since) VALUES(?,?)",
                                 (fields["query"], time.time())).lastrowid
        editing.clear()
        await card(m, key)
    return r


async def monitor(store, api, interval, pages):
    db = store.db
    cleanup = 0
    while True:
        now = time.time()
        if now >= cleanup:
            with db:
                db.execute("DELETE FROM seen WHERE touched<?", (now - 30 * 86400,))
                # Expired IDs must never make old offers look new again
                db.execute("UPDATE topics SET since=max(since,?)", (now - 30 * 86400,))
            cleanup = now + 86400
        t = db.execute("SELECT * FROM topics WHERE active AND due<=? ORDER BY due,id LIMIT 1", (now,)).fetchone()
        if not t:
            await asyncio.sleep(1)
            continue
        try:
            async for offers in api.offers(t["query"], pages):
                store.remember(t, offers)
        except APIError as exc:
            logging.warning("Тема %s: %s", t["id"], exc)
        with db:
            db.execute("UPDATE topics SET due=? WHERE id=? AND revision=?", (time.time()+interval, t["id"], t["revision"]))


async def deliver(store, api, bot, owner, phones):
    db = store.db
    while True:
        row = db.execute("SELECT * FROM pending ORDER BY id LIMIT 1").fetchone()
        if not row:
            await asyncio.sleep(1)
            continue
        a = json.loads(row["body"])
        phone = ""
        if phones and api.next <= time.monotonic() + api.gap:
            try:
                phone = await api.phone(a["id"])
            except APIError as exc:
                logging.info("Телефон недоступен: %s", exc)
        if not db.execute("SELECT 1 FROM pending WHERE id=? AND body=?", (row["id"], row["body"])).fetchone():
            continue
        previous = a["previous"]
        title = "🔔 Новое объявление!" if previous is None else "📉 Цена снизилась"
        price = a["label"] + (f" (было {previous:g} {a['currency']})" if previous is not None else "")
        date = datetime.fromtimestamp(a["created"], ZoneInfo("Asia/Tashkent")).strftime("%d.%m.%Y %H:%M")
        lines = [title, "", "📦 " + a["title"], "💰 Цена: " + price, "📍 Город: " + a["city"]]
        if phone:
            lines.append("📞 Телефон: " + phone)
        lines += ["🕒 Опубликовано: " + date, "🔎 Тема: " + a["query"]]
        try:
            await bot.send_message(owner, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Открыть объявление", url=a["url"])]]))
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            continue
        except TelegramAPIError as exc:
            logging.warning("Ошибка доставки: %s", type(exc).__name__)
            await asyncio.sleep(30)
            continue
        with db:
            db.execute("DELETE FROM pending WHERE id=?", (row["id"],))
        await asyncio.sleep(1)


async def main():
    load_dotenv()
    owner = int(os.environ["OWNER_ID"])
    store = Store(os.getenv("DB_PATH", "olx.sqlite3"))
    dp = Dispatcher()
    dp.include_router(interface(store, owner))
    async with Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(link_preview_is_disabled=True)) as bot:
        headers = {"Accept": "application/json", "User-Agent": "OLX-monitor/1.0"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=headers) as session:
            api = OLX(session, max(2, int(os.getenv("REQUEST_GAP", "3"))))
            async with asyncio.TaskGroup() as group:
                workers = [group.create_task(monitor(store, api, max(15, int(os.getenv("CHECK_INTERVAL", "120"))),
                                                     max(1, min(10, int(os.getenv("MAX_PAGES", "2")))))),
                           group.create_task(deliver(store, api, bot, owner, os.getenv("FETCH_PHONE", "1") == "1"))]
                try:
                    await dp.start_polling(bot, handle_as_tasks=False, close_bot_session=False)
                finally:
                    for worker in workers:
                        worker.cancel()
    store.db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    if os.getenv("DIAGNOSE_ONLY") == "1":
        from diagnose import run
        raise SystemExit(asyncio.run(run()))
    asyncio.run(main())
