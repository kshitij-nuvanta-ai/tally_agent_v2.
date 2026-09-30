"""``python -m backend.sync purge [--workspace ID] [--now] [--url URL]`` (Q5, Task 11) — deletes every sync row
of workspaces soft-deleted past the grace period (or immediately with ``--now``), and logs row counts only.

There is no ``migrate`` sub-command: the sync tables are created by ``alembic upgrade head`` (revision ``006``).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from backend.config import Settings
from backend.sync.cli import purge_cli


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.sync")
    sub = parser.add_subparsers(dest="command", required=True)

    purge_p = sub.add_parser("purge", help="Delete sync rows for soft-deleted workspaces past the grace period")
    purge_p.add_argument("--url", default=None, help="Database URL (default: DATABASE_URL from the settings)")
    purge_p.add_argument("--workspace", default=None, help="Limit the purge to one workspace id")
    purge_p.add_argument("--now", action="store_true", help="Ignore the grace period")

    args = parser.parse_args()

    if args.command == "purge":
        url = args.url or Settings().DATABASE_URL
        if not url:
            parser.error("--url or DATABASE_URL is required")
        workspace_id = uuid.UUID(args.workspace) if args.workspace else None
        counts = asyncio.run(purge_cli(url, workspace_id=workspace_id, now=args.now))
        # Row counts only (Q5 / decision 14 discipline) — never a workspace id, GUID or name.
        print(json.dumps({"command": "purge", "now": args.now, "counts": counts}))


if __name__ == "__main__":
    main()
