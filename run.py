import asyncio
import os
import subprocess
import sys

from app.main import main
from app.db import Base, Control
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def prepare_sqlite(database_url):
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine)
    async with sessions.begin() as session:
        if await session.get(Control, 1) is None:
            session.add(Control(id=1))
    await engine.dispose()


if __name__ == "__main__":
    database_url = os.getenv('DATABASE_URL', 'sqlite+aiosqlite:///./olx-monitor.db')
    if database_url.startswith('sqlite+'):
        asyncio.run(prepare_sqlite(database_url))
    else:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
        )
    asyncio.run(main())
