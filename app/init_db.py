"""Database initialization and reset CLI.

Usage:
    python -m app.init_db           # Initialize database (create if not exists)
    python -m app.init_db --reset   # Wipe database and config, allowing fresh setup
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.core.config import clear_config, get_config_path, get_database_path
from app.database import init_database_sync

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Database initialization and reset tool")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe database and config file, allowing fresh setup on next startup",
    )
    args = parser.parse_args()

    if args.reset:
        _do_reset()
    else:
        path = init_database_sync()
        logger.info("database initialized: %s", path)


def _do_reset() -> None:
    db_path = get_database_path()
    config_path = get_config_path()

    print(f"\n  Database : {db_path}")
    print(f"  Config   : {config_path}\n")
    confirm = input("  This will DELETE the database and config file. Type 'yes' to confirm: ").strip()
    if confirm.lower() != "yes":
        print("  Cancelled.")
        sys.exit(0)

    # Remove database file and WAL/SHM files
    removed = []
    for suffix in ("", "-wal", "-shm"):
        p = db_path.parent / (db_path.name + suffix)
        if p.exists():
            p.unlink()
            removed.append(str(p))

    # Remove config file
    if config_path.exists():
        config_path.unlink()
        removed.append(str(config_path))

    # Clear caches
    clear_config()

    if removed:
        logger.info("reset complete, removed: %s", ", ".join(removed))
        print(f"\n  Reset complete. Removed {len(removed)} file(s).")
    else:
        print("\n  Nothing to remove (already clean).")

    print("  Restart the server to begin fresh setup at /setup\n")


if __name__ == "__main__":
    main()
