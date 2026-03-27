from __future__ import annotations

import asyncio

from app.bootstrap_admin import ensure_default_admin_user
from app.database import LocalDatabase, init_database_sync


async def _bootstrap() -> None:
    database = LocalDatabase()
    await database.initialize()
    await ensure_default_admin_user(database)


def main() -> None:
    path = init_database_sync()
    print(f"database initialized: {path}")
    asyncio.run(_bootstrap())
    print("default admin ready: username=admin, password=admin")
    print("(change password after first login in production; also used for /admin)")


if __name__ == "__main__":
    main()
