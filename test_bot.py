import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from core import Store, matches, parse
from main import interface, values
from olx import APIError, OLX


def offer():
    return parse(dict(id=1, title="iPhone Pro", description="128 GB", created_time="2026-01-01T00:00:00Z",
                      last_refresh_time="2027-01-01T00:00:00Z", url="https://www.olx.uz/d/abc",
                      params=[dict(key="price", value=dict(value=450, currency="USD"))],
                      location=dict(city=dict(name="Ташкент"))))


class Storage(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.addCleanup(self.s.db.close)
        self.s.db.execute("INSERT INTO topics(query,since) VALUES('iphone',?)", (time.time(),))
        self.t = self.s.topic(1)

    def test_baseline_changes_and_duplicates(self):
        a = offer()
        self.s.remember(self.t, [a, a])
        count = lambda: self.s.db.execute("SELECT count(*) FROM pending").fetchone()[0]
        self.assertEqual(count(), 0)
        self.s.remember(self.t, [dict(a, amount=420)] * 2)
        self.assertEqual(count(), 1)
        self.s.remember(self.t, [dict(a, id=2, created=time.time()+1)] * 2)
        self.assertEqual(count(), 2)

    def test_filters(self):
        t = dict(self.t, city=" ташкент ", words="pro,128", cap=450, currency="USD")
        self.assertTrue(matches(t, offer()))
        for change in [dict(city="Навои"), dict(words="pro,max"), dict(cap=449), dict(currency="UZS")]:
            self.assertFalse(matches(dict(t, **change), offer()))
        for v in ("inf USD", "-1 USD", "450", "0 EUR"):
            with self.assertRaises(ValueError):
                values("cap", v)


class Network(unittest.IsolatedAsyncioTestCase):
    async def test_http_and_schema(self):
        for status, data in [(403, {}), (429, {}), (200, []), (200, {"data": {}})]:
            session = MagicMock()
            response = session.get.return_value.__aenter__.return_value
            response.status = status
            response.headers = {"Retry-After": "1000"}
            response.json = AsyncMock(return_value=data)
            api = OLX(session)
            with self.assertRaises(APIError):
                await anext(api.offers("x", 1))
            if status != 200:
                self.assertGreater(api.next, time.monotonic()+990)

    async def test_timeout(self):
        for exc in (TimeoutError(), aiohttp.ClientConnectionError()):
            session = MagicMock()
            session.get.side_effect = exc
            with self.assertRaises(APIError):
                await OLX(session).get("url")


class Telegram(unittest.IsolatedAsyncioTestCase):
    async def test_controls(self):
        s = Store(":memory:")
        self.addCleanup(s.db.close)
        dp = Dispatcher()
        dp.include_router(interface(s, 7))
        bot = Bot("123456:fake")
        self.addAsyncCleanup(bot.session.close)
        bot.session.make_request = AsyncMock(return_value=True)

        async def feed(text, callback=False, user=7):
            person = {"id": user, "is_bot": False, "first_name": "A"}
            message = {"message_id": 1, "date": 1, "chat": {"id": user, "type": "private"},
                       "from": person, "text": text}
            payload = {"callback_query": {"id": "1", "from": person, "chat_instance": "a", "message": message,
                                          "data": text}} if callback else {"message": message}
            await dp.feed_update(bot, Update.model_validate({"update_id": 1, **payload}))

        await feed("/start", user=9)
        bot.session.make_request.assert_not_called()
        await feed("/start")
        self.assertTrue(bot.session.make_request.called)
        await feed("add", True)
        await feed("велосипед")
        await feed("cap:1", True)
        await feed("450 USD")
        self.assertEqual(s.topic(1)["cap"], 450)
        await feed("list:0", True)
        await feed("toggle:1", True)
        self.assertFalse(s.topic(1)["active"])
        await feed("toggle:1", True)
        await feed("check:1", True)
        await feed("delete:1", True)
        self.assertTrue(s.topic(1))
        await feed("confirm:1", True)
        self.assertIsNone(s.topic(1))
