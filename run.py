"""Entry point for the existing Bot-Hosting.net configuration."""
import asyncio
import json
import logging
import os

from dotenv import load_dotenv

from main import main


def configure_owner():
    if os.getenv("OWNER_ID"):
        return
    admins = json.loads(os.getenv("ADMIN_IDS") or "[]")
    if isinstance(admins, list) and len(admins) == 1 and type(admins[0]) is int and admins[0] > 0:
        os.environ["OWNER_ID"] = str(admins[0])
        return
    raise SystemExit("Укажи OWNER_ID в настройках хостинга и перезапусти сервер")


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    if os.getenv("DIAGNOSE_ONLY") == "1":
        from diagnose import run
        raise SystemExit(asyncio.run(run()))
    configure_owner()
    asyncio.run(main())
