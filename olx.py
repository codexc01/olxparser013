import asyncio
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp

from core import parse

BASE = "https://www.olx.uz/api/v1/offers/"


class APIError(Exception):
    pass


class OLX:
    def __init__(self, session, gap=3):
        self.session = session
        self.lock = asyncio.Lock()
        self.next = 0
        self.phone_next = 0
        self.gap = gap

    async def get(self, url, params=None, optional=False):
        if optional and (self.lock.locked() or self.next > time.monotonic() + self.gap):
            raise APIError("API занят")
        async with self.lock:
            await asyncio.sleep(max(0, self.next - time.monotonic()))
            self.next = time.monotonic() + self.gap
            try:
                async with self.session.get(url, params=params, allow_redirects=False) as r:
                    if r.status in (403, 429):
                        retry = r.headers.get("Retry-After", "")
                        delay = 900 if r.status == 403 else 120
                        try:
                            delay = max(delay, int(retry))
                        except ValueError:
                            try:
                                delay = max(delay, (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
                            except (TypeError, ValueError, OverflowError):
                                pass
                        if optional and r.status == 403:
                            self.phone_next = time.monotonic() + delay
                        else:
                            self.next = time.monotonic() + delay
                    if r.status != 200:
                        raise APIError(f"HTTP {r.status}")
                    data = await r.json()
                    if not isinstance(data, dict) or "data" not in data:
                        raise APIError("Изменился формат ответа OLX")
                    return data["data"]
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                raise APIError(type(exc).__name__) from exc

    async def offers(self, query, pages):
        for page in range(pages):
            rows = await self.get(BASE, {"query": query, "limit": 40, "offset": 40 * page,
                                         "sort_by": "created_at:desc"})
            if not isinstance(rows, list):
                raise APIError("Вместо списка объявлений пришёл другой ответ")
            offers = []
            for raw in rows:
                try:
                    offers.append(parse(raw))
                except (TypeError, AttributeError, KeyError, ValueError, OverflowError):
                    logging.warning("Не удалось прочитать поля объявления")
            if rows and not offers:
                raise APIError("Не удалось прочитать выдачу OLX")
            yield offers
            if len(rows) < 40:
                break

    async def phone(self, offer):
        if self.phone_next > time.monotonic():
            raise APIError("Телефоны временно недоступны")
        data = await self.get(f"{BASE}{int(offer)}/phones/", optional=True)
        nums = data.get("phones") if isinstance(data, dict) else None
        return next((n[:50] for n in nums if isinstance(n, str) and n.strip()), "") if isinstance(nums, list) else ""
