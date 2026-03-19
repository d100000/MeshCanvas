from __future__ import annotations

from app.database import init_database_sync


def main() -> None:
    path = init_database_sync()
    print(f"database initialized: {path}")


if __name__ == "__main__":
    main()
