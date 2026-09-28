"""``python -m v2.cloud migrate [--url URL]`` — runs the v2 Alembic chain to head (S1 task 3).

``purge`` (Q5 retention sweep) is added in Task 11.
"""
from __future__ import annotations

import argparse

from v2.cloud.cli import migrate
from v2.cloud.config import V2Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m v2.cloud")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate_p = sub.add_parser("migrate", help="Run the v2 Alembic chain to head")
    migrate_p.add_argument("--url", default=None, help="Database URL (default: V2Settings().database_url)")

    args = parser.parse_args()

    if args.command == "migrate":
        url = args.url or V2Settings().database_url
        if not url:
            parser.error("--url or V2_DATABASE_URL/DATABASE_URL is required")
        migrate(url)


if __name__ == "__main__":
    main()
