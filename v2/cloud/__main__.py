"""``python -m v2.cloud migrate [--url URL]`` — runs the v2 Alembic chain to head (S1 task 3).

``python -m v2.cloud purge [--workspace ID] [--now] [--url URL]`` (Q5, Task 11) — deletes every v2 row of
workspaces soft-deleted past the grace period (or immediately with ``--now``), and logs row counts only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from v2.cloud.cli import migrate, purge_cli
from v2.cloud.config import V2Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m v2.cloud")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate_p = sub.add_parser("migrate", help="Run the v2 Alembic chain to head")
    migrate_p.add_argument("--url", default=None, help="Database URL (default: V2Settings().database_url)")

    purge_p = sub.add_parser("purge", help="Delete v2 rows for soft-deleted workspaces past the grace period")
    purge_p.add_argument("--url", default=None, help="Database URL (default: V2Settings().database_url)")
    purge_p.add_argument("--workspace", default=None, help="Limit the purge to one workspace id")
    purge_p.add_argument("--now", action="store_true", help="Ignore the grace period")

    args = parser.parse_args()

    if args.command == "migrate":
        url = args.url or V2Settings().database_url
        if not url:
            parser.error("--url or V2_DATABASE_URL/DATABASE_URL is required")
        migrate(url)
    elif args.command == "purge":
        url = args.url or V2Settings().database_url
        if not url:
            parser.error("--url or V2_DATABASE_URL/DATABASE_URL is required")
        workspace_id = uuid.UUID(args.workspace) if args.workspace else None
        counts = asyncio.run(purge_cli(url, workspace_id=workspace_id, now=args.now))
        # Row counts only (Q5 / decision 14 discipline) — never a workspace id, GUID or name.
        print(json.dumps({"command": "purge", "now": args.now, "counts": counts}))


if __name__ == "__main__":
    main()
