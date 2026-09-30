# TallyPrime AI Agent

AI-powered chatbot that connects to a live TallyPrime instance, lets users ask natural-language questions about accounting data, and replies with answers, charts, and tables.

**Stack:** Python (FastAPI) backend · React (Vite + Tailwind + Recharts) frontend · Claude API (tool-calling) · PostgreSQL (optional, for auth + persistence + agent sync)

**State on 2026-09-30:** the sync service that used to live under `v2/` (its own app on port 8100) is merged into this one project: one app on port 8000, one settings class, one Alembic chain, one `uv` project. Design: [`docs/specs/2026-09-30-v2-merge-design.md`](docs/specs/2026-09-30-v2-merge-design.md); status: [`docs/plans/2026-09-30-v2-merge-tracker.md`](docs/plans/2026-09-30-v2-merge-tracker.md).

**Target architecture (direction set 2026-09-30): installer → agent → tally bridge.** The bridge will run on the Tally PC inside the desktop agent, and the cloud app will talk only to its own Postgres. Not done yet: chat still reads Tally live through `tally_bridge` (the synced tables are not read by chat), legacy (non-DB) mode still exists and is to be removed in a separate later step, and the desktop agent and installer are not built (`agent/` is a placeholder).

---

## Prerequisites

- **Python 3.12+** with [`uv`](https://docs.astral.sh/uv/) (recommended) or pip
- **Node.js 20+** and npm
- **TallyPrime 7.0+** running with the HTTP/XML server enabled (default `localhost:9000`) — see [Installing TallyPrime on macOS](#installing-tallyprime-on-macos) if you don't already have it
- **Anthropic API key** — get one at <https://console.anthropic.com/>
- **PostgreSQL 14+** *(only if you want auth + persistent conversations; optional)*

> No Tally instance handy? You can run the app in **mock mode** for tests and demos — see [Testing](#testing).

---

## Installing TallyPrime on macOS

TallyPrime is a Windows-only application, but it runs reliably on macOS via [Wine](https://www.winehq.org/). Use the **Edit Log** edition — it exposes the HTTP/XML server this project depends on.

### 1. Install Wine

`brew install --cask --no-quarantine wine-stable` no longer works: Homebrew disabled all Wine casks on 2026-09-01 (Gatekeeper) and removed `--no-quarantine`. What works (verified 2026-09-22, Wine 11.0, Apple Silicon + Rosetta):

```bash
curl -fsSL -o wine.tar.xz https://github.com/Gcenx/macOS_Wine_builds/releases/download/11.0_1/wine-stable-11.0_1-osx64.tar.xz
echo "b50dc50ec7f41d58b115a6b685d4d1315ba3c797bd3aa0f49213f2703cb82388  wine.tar.xz" | shasum -a 256 -c -
tar -xJf wine.tar.xz && mv "Wine Stable.app" /Applications/ && xattr -cr "/Applications/Wine Stable.app"
export PATH="/Applications/Wine Stable.app/Contents/Resources/wine/bin:$PATH"
```

### 2. Download TallyPrime 7.0 Edit Log

Get the **TallyPrime Edit Log** installer (version 7.0 or later) from <https://tallysolutions.com/download/>. Save the `.exe` to `~/Downloads`.

### 3. Install under Wine

```bash
cd ~/Downloads
wine TallyPrime_EditLog_Setup.exe   # filename will vary; use the one you downloaded
```

Click through the installer with default options. It installs to `~/.wine/drive_c/Program Files/TallyPrimeEditLog/`.

### 4. Run TallyPrime

```bash
cd ~/.wine/drive_c/Program\ Files/TallyPrimeEditLog && wine tally.exe &
```

### 5. Enable the HTTP/XML server

Inside TallyPrime: **F1 (Help) → Settings → Connectivity → Client/Server configuration** → set *TallyPrime is acting as* to **Both**, *Port* to **9000**. Save and restart Tally.

Verify from the host:

```bash
curl http://localhost:9000   # → <RESPONSE>TallyPrime Server is Running</RESPONSE>
python scripts/test_tally_connection.py
```

---

## Quick start (legacy mode — no auth, in-memory sessions)

This is the fastest way to see the app working end-to-end.

### 1. Clone and configure

```bash
git clone <repo-url>
cd tally_agent
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY (and TALLY_HOST/PORT if Tally isn't on localhost:9000)
```

### 2. Install backend dependencies

```bash
uv sync --extra dev --extra langfuse
```

### 3. Install frontend dependencies

```bash
cd frontend
npm install
cd ..
```

### 4. Run it

Open two terminals:

```bash
# Terminal 1 — backend
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# Terminal 2 — frontend
cd frontend
npm run dev
```

Visit <http://localhost:5173>.

### 5. (Optional) Verify Tally connectivity

```bash
python scripts/test_tally_connection.py
```

---

## Running with auth + persistence (DB mode)

DB mode adds user accounts, workspaces (one per Tally company), and persistent conversation history. **All new feature work targets DB mode** — legacy mode is frozen.

### 1. Create the database

```bash
createdb tallyagent
createdb tallyagent_test   # for DB integration tests
```

### 2. Configure `.env`

Uncomment and fill in the DB-mode block in `.env`:

```env
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/tallyagent
JWT_SECRET=<generate-a-random-32+-char-string>
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/tallyagent_test
```

Generate a JWT secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 3. Install dependencies and run migrations

The database packages are core dependencies (the old `--extra db` is still accepted and is empty).

```bash
uv sync --extra dev --extra langfuse
PYTHONPATH=. python -m alembic upgrade head
PYTHONPATH=. DATABASE_URL=$TEST_DATABASE_URL python -m alembic upgrade head   # also migrate the test DB
```

### 4. Run with DB mode enabled

```bash
# Terminal 1 — backend (auto-detects DB mode from DATABASE_URL)
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# Terminal 2 — frontend (must opt in to DB mode)
cd frontend
VITE_DB_MODE=true npm run dev
```

Register a user at <http://localhost:5173/register>, then connect a Tally company workspace from the sidebar.

### 5. (Optional) Enable the agent sync routes

The same app (port 8000) also serves the 19 routes the desktop agent and the web sync status use: `/api/agent/*` (device login, refresh, logout, workspaces), `/api/sync/*` (bind, heartbeat, state, relink, runs, coverage, batches, reconcile, snapshots, parity), `/api/devices`, and `/api/workspaces/{id}/sync-status` and `/sync/commands`. There is no separate service: port 8100 and `/api/v2/health` are gone.

They are mounted only in DB mode and only when `DEVICE_TOKEN_SECRET` is set:

```env
DEVICE_TOKEN_SECRET=<another-random-32+-char-string, different from JWT_SECRET>
```

| `DEVICE_TOKEN_SECRET` | Result |
|---|---|
| unset | The app starts, the sync and agent routes are not mounted, one warning is logged. |
| set, under 32 characters or equal to `JWT_SECRET` | Startup fails. |
| set, 32+ characters, different from `JWT_SECRET` | The routes are served. |

Web login, agent login and the relink password check share one per-email login limiter (`LOGIN_RATE_MAX` failures per `LOGIN_RATE_WINDOW_S`, default 5 per 900 s).

Delete the synced rows of soft-deleted workspaces past the grace period (`PURGE_GRACE_DAYS`); `--workspace` limits it to one workspace, `--now` ignores the grace period:

```bash
PYTHONPATH=. uv run python -m backend.sync purge [--url URL] [--workspace ID] [--now]
```

The desktop agent that calls these routes is not built yet, and chat does not read the synced tables yet.

---

## Database migrations (DB mode)

Schema is managed by [Alembic](https://alembic.sqlalchemy.org/). Migration files live in `backend/db/migrations/versions/`. ORM models live in `backend/db/models.py` (9 app tables) and `backend/db/sync_models/` (21 sync tables), all on one declarative `Base`. There is one Alembic chain (version table `alembic_version`); after `upgrade head` the database has 30 application tables plus `alembic_version`.

Revision `006` (`006_sync_tables.py`) adds the 21 sync tables:

| Database before | What `upgrade` does |
|---|---|
| No sync tables (chain at `005`) | Creates the 21 tables. |
| Already migrated by the old separate sync chain (`alembic_version_v2` at `v2_001`) | Creates nothing, keeps the tables and rows, drops `alembic_version_v2`. |
| `alembic_version_v2` at any other revision | Fails with a message and changes nothing. |

### Apply migrations

```bash
# Upgrade the dev DB to the latest schema
PYTHONPATH=. python -m alembic upgrade head

# Upgrade the test DB too (same migrations, different URL)
PYTHONPATH=. DATABASE_URL=$TEST_DATABASE_URL python -m alembic upgrade head
```

Run `alembic upgrade head` after pulling new commits whenever `backend/db/migrations/versions/` has new files. The backend will refuse to start in DB mode if migrations are out of date.

### Create a new migration

After editing `backend/db/models.py` or `backend/db/sync_models/`:

```bash
# Autogenerate from model diff
PYTHONPATH=. python -m alembic revision --autogenerate -m "add foo column to bar"

# Review the generated file in backend/db/migrations/versions/ — autogenerate
# misses things like enum changes and server defaults; edit by hand if needed.

# Apply locally
PYTHONPATH=. python -m alembic upgrade head
```

Conventions:
- File prefix is sequential (`003_`, `004_`, …) — match the existing pattern.
- Always include a working `downgrade()` so rollbacks are possible.
- Don't edit a migration after it has been merged to `master` — write a new one.

### Rollback

```bash
PYTHONPATH=. python -m alembic downgrade -1        # one step back
PYTHONPATH=. python -m alembic downgrade <rev>     # to a specific revision
PYTHONPATH=. python -m alembic history             # see all revisions
PYTHONPATH=. python -m alembic current             # see what's applied
```

### Seed / reference data

The schema does not require any seed data — a fresh DB after `alembic upgrade head` is fully usable. Users register through the UI and create workspaces from there.

To populate **Tally** (not Postgres) with the Bharat Traders sample data used by tests and demos:

```bash
python scripts/seed_tally_data.py --host <TALLY_HOST> --port 9000
```

### Reset a local DB

```bash
dropdb tallyagent && createdb tallyagent
PYTHONPATH=. python -m alembic upgrade head
```

---

## Repository layout

```
backend/
  agents/          # Multi-agent system: orchestrator, query, analysis, chart agents
  api/             # FastAPI routes: chat, health, auth, workspaces, conversations,
                   #   and the agent/sync routes (agent_auth, devices, sync, workspace_sync, sync_dependencies)
  sync/            # Sync server logic: binding, runs, state, ingest/, parity/, clock, errors, wiring,
                   #   purge CLI (`python -m backend.sync purge`)
  db/              # SQLAlchemy models (models.py, sync_models/) + Alembic migrations (DB mode)
  utils/           # auth, device_tokens, rate_limit, date/currency helpers
  config.py        # pydantic-settings (loads .env) — the one settings class
  main.py          # FastAPI app entry point (the one app, port 8000)
tally_bridge/      # Tally HTTP client, XML request builders, response parsers, queries, writer, mock handler
                   #   (was backend/tally_bridge/, plus the read code written for the agent)
contract/          # Wire contract between agent and cloud: request models, parsing, transcoding
agent/             # Placeholder for the desktop agent (not built yet)
probes/            # Live-Tally probe harness (`python -m probes ...`)
frontend/
  src/             # React app (chat UI, sidebar, auth pages)
  tests/           # Vitest unit tests + Playwright visual tests
tests/
  unit/            # Backend unit tests (pure logic, no I/O)
  integration/     # Mock-Tally integration tests (some require Postgres)
  e2e/             # End-to-end with mock Claude (legacy + DB smoke)
  e2e_live/        # End-to-end against real Tally + real Claude (gated)
  eval/            # LLM-as-a-judge eval framework
  sync/            # Sync server tests: unit/ (no DB) and db/ (need TEST_DATABASE_URL)
  tally_bridge/    # Tests for the bridge code that came from the agent side
  contract/        # Wire-contract tests
  probes/          # Probe-harness tests (fake Tally, no live calls)
  test_layers.py   # Import rules between the top-level packages (see below)
  fixtures/        # Sample Tally XML/JSON responses
scripts/           # Utilities: connection test, seed data, live agent test
docs/              # Design specs, plans, code reviews
CLAUDE.md          # Detailed engineering guide for AI assistants (also useful for humans)
TALLYPRIME_AGENT_PLAN.md   # Full project spec and phased build order
LESSONS.md         # Hard-won Tally API learnings
```

### Layer rule

`tests/test_layers.py` enforces which top-level package may import which:

| Package | Must not import |
|---|---|
| `tally_bridge` | `backend`, `agent`, `contract`, `probes` (it stands alone) |
| `contract` | `backend`, `agent`, `probes` (it may import `tally_bridge`) |
| `agent` | `backend`, `probes` |
| `backend` | `agent`, `probes` |
| `probes` | `backend` |

None of them may import `tests` or `scripts`. One recorded exception: `tally_bridge/mock_handler.py` loads `tests/fixtures/generate_fixtures.py` from its file path at run time.

---

## Testing

> ⚠️ Live and eval tests hit the real Claude API and cost money. Always pipe to a log file with `tee` and never re-run unnecessarily.

### Backend — fast, no external deps

```bash
uv run pytest tests/unit/ -v                                                          # unit tests only
ANTHROPIC_API_KEY=test-key PYTHONPATH=. uv run pytest tests/ --ignore=tests/e2e_live  # all tests (mock Tally, mock Claude)
uv run pytest --cov=backend --cov-report=html                                         # with coverage
```

Without `TEST_DATABASE_URL` the DB tests skip. On 2026-09-30: 3186 passed / 594 skipped.

### Backend — DB tests (requires Postgres)

Set `TEST_DATABASE_URL` and the same command runs the whole suite, DB tests included, in one session (on 2026-09-30: 3770 passed / 10 skipped):

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/tallyagent_test \
ANTHROPIC_API_KEY=test-key PYTHONPATH=. \
uv run pytest tests/ --ignore=tests/e2e_live
```

`TEST_DATABASE_URL` must be a separate database from the dev one. The migration tests also create and drop throwaway databases named `<test database>_mig_<random>` on the same server.

### Backend — live tests (real Tally + real Claude)

```bash
RUN_LIVE_TESTS=1 PYTHONPATH=. pytest tests/e2e_live/ -v -s \
  --host <TALLY_HOST> --port 9000 2>&1 | tee docs/e2e-live-results.log
```

Or in mock mode (no Tally needed, still uses Claude API):

```bash
PYTHONPATH=. pytest tests/e2e_live/ -v -s --tally-mode mock \
  2>&1 | tee docs/e2e-live-mock-results.log
```

### Frontend

```bash
cd frontend
npm test                     # Vitest unit tests
npm run test:playwright      # Visual tests (backend must be running on :8000)
```

To regenerate Playwright baselines for a single spec only:

```bash
rm -rf tests/playwright/__screenshots__/*/<spec-name>.spec.ts/
npm run test:playwright -- --update-snapshots
```

---

## Live-Tally probes

The probe harness (`probes/`) measures how a real TallyPrime behaves. Specs: `docs/specs/2026-09-22-bi-s0-probes-design.md`; results: `docs/bi-s0-probe-results-2026-09-23.md` and later files. Run from the repo root:

```bash
uv run python -m probes list                  # every probe and its last outcome (no Tally needed)
uv run python -m probes run <id>              # one probe; also: run --first | run --all
uv run python -m probes report
uv run python -m probes anchors               # read-only: is company A intact?
```

Automated runs (S0-D9, S0 spec §5.8):

- `uv run python -m probes reset-a` — stops the operator's TallyPrime, copies the pristine `seed_data/100003` into `s0probe`, starts Tally with `/DATA:… /LOAD:100003` and renames the company to "Bharat Traders Probe Copy". Also undoes anything a probe couldn't revert.
- `uv run python -m probes run --all --auto` (or `run <id> --auto`) — the operator performs every pause: edits via XML import (read back), open/close/backup/restore via Tally restarts.
- Watch for **`CLICK NEEDED`**: after every restart that loads a company, click "T: Continue In Educational Mode" on Tally's licence box (the operator waits up to 15 minutes).
- The operator stops only a TallyPrime it started on `s0probe` (`--stop-any-tally` overrides) and never edits `tally.ini` (it backs it up once as `tally.ini.before-s0`). Log: `probes/results/logs/s0-auto-<date>.log`.

Probe company A's data folder under Wine: `C:\users\Public\TallyPrimeEditLog\s0probe` (= `~/.wine/drive_c/users/Public/TallyPrimeEditLog/s0probe`). Wine maps `/` to `Z:`, so a folder on the Mac is reachable inside Tally as `Z:\<path>` (used for the seed backup in `seed_data/`).

---

## Useful scripts

```bash
# Verify Tally is reachable
python scripts/test_tally_connection.py

# Seed Bharat Traders test data into a Tally instance
python scripts/seed_tally_data.py --host <TALLY_HOST> --port 9000

# Run the full agent pipeline against a real Tally (needs ANTHROPIC_API_KEY)
PYTHONPATH=. uv run python scripts/test_agent_live.py --host <TALLY_HOST> --port 9000
```

---

## Contributing

1. Read [`CLAUDE.md`](CLAUDE.md) — it's the canonical engineering guide (build commands, conventions, testing rules, DB-mode notes).
2. Read [`LESSONS.md`](LESSONS.md) for non-obvious Tally API gotchas before touching `tally_bridge/` or write paths.
3. New work goes through DB mode. Legacy mode is frozen.
4. Specs and plans live in `docs/specs/` and `docs/plans/`. Code review notes in `docs/code-review-*.md`.
5. Test counts and current architecture status are tracked in `CLAUDE.md`.

---

## Configuration reference

All settings load from `.env` via `pydantic-settings`. See [`.env.example`](.env.example) for the full list. Quick reference:

| Variable | Default | Notes |
|---|---|---|
| `TALLY_HOST` / `TALLY_PORT` | `localhost` / `9000` | Tally HTTP endpoint |
| `ANTHROPIC_API_KEY` | — | Required |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Query + analysis agents |
| `CLAUDE_CLASSIFIER_MODEL` | `claude-haiku-4-5-20251001` | Orchestrator routing |
| `DATABASE_URL` | *(unset)* | Set to enable DB mode |
| `JWT_SECRET` | — | Required when `DATABASE_URL` is set; min 32 chars |
| `VITE_DB_MODE` | *(unset)* | Frontend: set to `true` for auth pages + sidebar |
| `TEST_DATABASE_URL` | *(unset)* | Separate database for the DB tests; they skip without it |

### Agent sync settings

Read by the same settings class (`backend/config.py`). Each old `V2_`-prefixed name (`V2_DEVICE_TOKEN_SECRET`, …, plus `V2_DATABASE_URL` for `DATABASE_URL` and `V2_WEB_JWT_SECRET` for `JWT_SECRET`) is still accepted; the new name wins when both are set. `V2_PORT` is no longer used.

| Variable | Default | Notes |
|---|---|---|
| `DEVICE_TOKEN_SECRET` | `""` | Secret. Signs the agent's device tokens. Unset: sync routes not mounted. Set: min 32 chars and different from `JWT_SECRET`, or startup fails. |
| `DEVICE_ACCESS_MINUTES` | `15` | Device access-token lifetime. |
| `DEVICE_REFRESH_DAYS` | `90` | Device refresh-token lifetime. |
| `TAKEOVER_LOGIN_MAX_AGE_MINUTES` | `10` | Max age of the web login accepted for a device takeover. |
| `INGEST_MAX_GZIP_BYTES` | `5242880` | Ingest body limit (compressed). |
| `INGEST_MAX_DECOMPRESSED_BYTES` | `52428800` | Ingest body limit (decompressed). |
| `INGEST_MAX_OBJECTS` | `500` | Objects per ingest request. |
| `PARITY_TOLERANCE_PAISE` | `100` | Parity comparison tolerance. |
| `QUARANTINE_ERROR_THRESHOLD` | `50` | Errors before a run is quarantined. |
| `STORAGE_ALERT_BYTES` | `5368709120` | Storage ops-signal threshold (5 GiB). |
| `LOGIN_RATE_MAX` / `LOGIN_RATE_WINDOW_S` | `5` / `900` | Failed logins per email; one budget for web login, agent login and relink. |
| `DEVICE_RATE_MAX` / `DEVICE_RATE_WINDOW_S` | `600` / `60` | Device endpoint rate limit. |
| `MAINTENANCE_SLICE_SECONDS` / `MAINTENANCE_SLICE_ROWS` | `2.0` / `5000` | Lazy-maintenance slice budget. |
| `STORAGE_ESTIMATE_INTERVAL_SECONDS` | `3600` | Storage estimate refresh cadence. |
| `PURGE_GRACE_DAYS` | `30` | Grace period before `purge` deletes a soft-deleted workspace's data. |
