import asyncio
import subprocess
import sys

from app.main import main


if __name__ == "__main__":
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
    )
    asyncio.run(main())
