import asyncio
import json
import logging

import httpx
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import Settings
from .monitor import cycle, deliver, repeat
from .ui import setup


class LogFormat(logging.Formatter):
    def format(self, record):
        return json.dumps({'level': record.levelname, 'event': record.getMessage()}, ensure_ascii=False)


async def main():
    handler = logging.StreamHandler()
    handler.setFormatter(LogFormat())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger('httpx').setLevel(logging.WARNING)
    config = Settings()
    engine = create_async_engine(config.database_url, pool_pre_ping=True, hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    dp = Dispatcher(events_isolation=SimpleEventIsolation())
    setup(dp)
    async with Bot(config.bot_token.get_secret_value()) as bot, httpx.AsyncClient(
            timeout=25, follow_redirects=False, trust_env=False,
            headers={'User-Agent':'OLX-monitor/1.0','Accept-Language':'ru'}) as client:
        tasks = [asyncio.create_task(repeat(cycle,3,sessions,client,config,bot)),
                 asyncio.create_task(repeat(deliver,1.2,sessions,bot))]
        try:
            await dp.start_polling(bot, sessions=sessions, config=config, close_bot_session=False)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
