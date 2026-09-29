# v2 — BI Part 1 (syncing)

Built **beside** the current code. Specs: `docs/specs/2026-09-21-bi-part1-sync-design.md` (§5 "Code isolation (v2)")
and `docs/specs/2026-09-22-bi-s0-probes-design.md`. Status: `docs/plans/2026-09-22-bi-part1-tracker.md`.

## Isolation rules
- Nothing outside `v2/` (and `docs/`) is changed for Part 1.
- v2 never imports `backend`, `scripts` or `tests`. What it needs is **copied** into v2 and fixed there.
  Each copied file starts with `# Copied from: <path> @ <commit>`.
- `v2/agent/` never imports `v2.probes` (the agent ships with no write code).
- `v2/tests/test_isolation.py` enforces the import rules.

## Commands (run from the repo root)
- Tests: `uv run --project v2 pytest v2/tests -q`
- Probes: `uv run --project v2 python -m v2.probes list | run <id> | run --first | run --all | report`

## Live Tally on this Mac (tier B)
The repo README's `brew install --cask --no-quarantine wine-stable` no longer works: Homebrew disabled all Wine casks on
2026-09-01 (Gatekeeper) and removed `--no-quarantine`. What works (verified 2026-09-22, Wine 11.0, Apple Silicon + Rosetta):

    curl -fsSL -o wine.tar.xz https://github.com/Gcenx/macOS_Wine_builds/releases/download/11.0_1/wine-stable-11.0_1-osx64.tar.xz
    echo "b50dc50ec7f41d58b115a6b685d4d1315ba3c797bd3aa0f49213f2703cb82388  wine.tar.xz" | shasum -a 256 -c -
    tar -xJf wine.tar.xz && mv "Wine Stable.app" /Applications/ && xattr -cr "/Applications/Wine Stable.app"
    export PATH="/Applications/Wine Stable.app/Contents/Resources/wine/bin:$PATH"
    cd ~/.wine/drive_c/Program\ Files/TallyPrimeEditLog && wine tally.exe &
    curl http://localhost:9000        # → <RESPONSE>TallyPrime Server is Running</RESPONSE>

Probe company A's data folder: `C:\users\Public\TallyPrimeEditLog\s0probe` (= `~/.wine/drive_c/users/Public/TallyPrimeEditLog/s0probe`).
The seed backup is at `Z:\Users\nuvanta-mac-3\work\Tally prime\seed_data` inside Tally (Wine maps `/` to `Z:`).

## Automated runs (S0-D9, S0 spec §5.8)
- `uv run --project v2 python -m v2.probes reset-a` — stops the operator's TallyPrime, copies the pristine
  `seed_data/100003` into `s0probe`, starts Tally with `/DATA:… /LOAD:100003` and renames the company to
  "Bharat Traders Probe Copy". Also undoes anything a probe couldn't revert.
- `uv run --project v2 python -m v2.probes run --all --auto` (or `run <id> --auto`) — the operator performs every pause:
  edits via XML import (read back), open/close/backup/restore via Tally restarts.
- Watch for **`CLICK NEEDED`**: after every restart that loads a company, click "T: Continue In Educational Mode" on
  Tally's licence box (the operator waits up to 15 minutes).
- The operator stops only a TallyPrime it started on `s0probe` (`--stop-any-tally` overrides) and never edits
  `tally.ini` (it backs it up once as `tally.ini.before-s0`). Log: `v2/probes/results/logs/s0-auto-<date>.log`.

## Cloud (S1)
The cloud app (`v2/cloud/`) is a separate FastAPI service that ingests the desktop agent's sync and serves the web
`sync-status` / device endpoints. It creates its own `v2_*` tables next to the current app's tables in Postgres and
never touches the current app's tables.

    # create / upgrade the v2 tables (idempotent; needs V2_DATABASE_URL or DATABASE_URL)
    PYTHONPATH=. uv run --project v2 python -m v2.cloud migrate

    # delete every v2 row of workspaces soft-deleted past the grace period (V2_PURGE_GRACE_DAYS);
    # --workspace limits it to one workspace, --now ignores the grace period; logs row counts only
    PYTHONPATH=. uv run --project v2 python -m v2.cloud purge [--workspace ID] [--now]

    # serve (the port is V2_PORT, default 8100; `main.py` exposes a module-level `app = create_app()`)
    V2_DEVICE_TOKEN_SECRET=... PYTHONPATH=. uv run --project v2 uvicorn v2.cloud.main:app --port 8100

Settings are read from the environment (or `.env`) with the `V2_` prefix (`v2/cloud/config.py`). Startup fails fast
(`validate_for_serving`) if `database_url` is empty, either secret is under 32 characters, or the two secrets are equal.

| Setting | Default | Notes |
|---|---|---|
| `V2_DATABASE_URL` | `""` | **Required.** Falls back to `DATABASE_URL`. Secret (contains credentials). |
| `V2_WEB_JWT_SECRET` | `""` | **Required, secret**, min 32 chars. Falls back to `JWT_SECRET` (the current app's web session secret). |
| `V2_DEVICE_TOKEN_SECRET` | `""` | **Required, secret**, min 32 chars, must differ from the web JWT secret. |
| `V2_PORT` | `8100` | |
| `V2_DEVICE_ACCESS_MINUTES` | `15` | Device access-token lifetime. |
| `V2_DEVICE_REFRESH_DAYS` | `90` | Device refresh-token lifetime. |
| `V2_TAKEOVER_LOGIN_MAX_AGE_MINUTES` | `10` | Max age of the web login accepted for a device takeover. |
| `V2_INGEST_MAX_GZIP_BYTES` | `5242880` | Ingest body limit (compressed). |
| `V2_INGEST_MAX_DECOMPRESSED_BYTES` | `52428800` | Ingest body limit (decompressed). |
| `V2_INGEST_MAX_OBJECTS` | `500` | Objects per ingest request. |
| `V2_PARITY_TOLERANCE_PAISE` | `100` | Parity comparison tolerance. |
| `V2_QUARANTINE_ERROR_THRESHOLD` | `50` | Errors before a run is quarantined. |
| `V2_STORAGE_ALERT_BYTES` | `5368709120` | Storage ops-signal threshold (5 GiB). |
| `V2_LOGIN_RATE_MAX` / `V2_LOGIN_RATE_WINDOW_S` | `5` / `900` | Login rate limit. |
| `V2_DEVICE_RATE_MAX` / `V2_DEVICE_RATE_WINDOW_S` | `600` / `60` | Device endpoint rate limit. |
| `V2_MAINTENANCE_SLICE_SECONDS` / `V2_MAINTENANCE_SLICE_ROWS` | `2.0` / `5000` | Lazy-maintenance slice budget. |
| `V2_STORAGE_ESTIMATE_INTERVAL_SECONDS` | `3600` | Storage estimate refresh cadence. |
| `V2_PURGE_GRACE_DAYS` | `30` | Grace period before `purge` deletes a soft-deleted workspace's data. |
