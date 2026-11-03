import asyncio
import sys

import aiohttp

from olx import APIError, OLX


async def run():
    headers = {"Accept": "application/json", "User-Agent": "OLX-monitor/1.0"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=headers) as session:
        api = OLX(session)
        try:
            async for rows in api.offers(" ".join(sys.argv[1:]) or "телефон", 1):
                print(f"Выдача OLX доступна, прочитано объявлений: {len(rows)}")
                if rows:
                    number = await api.phone(rows[0]["id"])
                    print("Телефон доступен" if number else "Ответ получен без номера")
                else:
                    print("Проверка телефонов пропущена, выдача пуста")
        except APIError as exc:
            print(f"Ошибка OLX: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
