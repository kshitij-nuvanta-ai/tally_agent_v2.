# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TallyPrime AI Agent — an AI-powered chatbot that connects to a live TallyPrime instance (default: `localhost:9000`; also works over LAN), lets users ask natural language questions about accounting data, and returns answers with charts and tables.

**Stack**: Python (FastAPI) backend + React (Vite) frontend + Claude API (Anthropic SDK with tool-calling) + PostgreSQL (auth + persistence)
**Tally Version**: TallyPrime 7.0+ (native JSON support, XML preferred for stability)

**Operating mode:** SaaS + DB mode (Postgres + JWT auth + per-workspace conversations) is the default and only maintained mode. Legacy mode (in-memory sessions, no auth) is frozen — bug fixes only, no new features.

**Where to look:**
- **What's next** → [`docs/roadmap.md`](docs/roadmap.md) (canonical roadmap, sets A/B/C, active work)
- **Closed phases** → § Implementation Phases below
- **Tally API learnings & write safety** → [`LESSONS.md`](LESSONS.md)
- **Parked items** → [`docs/open-items-parked.md`](docs/open-items-parked.md)
- **Original spec (archived)** → [`TALLYPRIME_AGENT_PLAN.md`](TALLYPRIME_AGENT_PLAN.md)

## Build & Run Commands

**Local run notes:** commands assume the project venv is active; if `uvicorn`/`python` aren't found, prefix with `uv run` (e.g. `PYTHONPATH=. uv run uvicorn backend.main:app ...`). The backend `--port` MUST match the frontend's `VITE_API_URL` (set in `frontend/.env`) — otherwise every API call 500s. Default is 8000.

### Backend (Python)
```bash
# Install dependencies
uv sync --extra dev --extra langfuse --extra db

# Run the FastAPI server (DB mode — default for all new work; requires Postgres + JWT_SECRET in .env)
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# Run the FastAPI server (legacy mode — FROZEN; no new features. Omit DATABASE_URL to use in-memory sessions)
# Kept for reference only — do NOT use for new development.

# Run Alembic migrations
PYTHONPATH=. python -m alembic upgrade head

# Run all unit tests (fast, no external deps)
pytest tests/unit/ -v

# Run a single test file
pytest tests/unit/test_request_builder.py -v

# Run a single test
pytest tests/unit/test_request_builder.py::test_trial_balance_xml_has_correct_dates -v

# Integration tests (needs mock Tally server)
pytest tests/integration/ -v

# E2E scenario tests (includes legacy + DB smoke tests)
ANTHROPIC_API_KEY=test-key pytest tests/e2e/ -v

# DB integration tests (requires Postgres)
# TEST_DATABASE_URL is a SEPARATE test DB (create/drop schema) — it is NEVER read from
# .env (which only carries the dev DATABASE_URL) and never falls back to it, so the dev
# `tallyagent` DB is never clobbered. DB tests skip when TEST_DATABASE_URL is unset.
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/tallyagent_test ANTHROPIC_API_KEY=test-key pytest tests/integration/test_auth_flow.py tests/integration/test_workspace_flow.py tests/integration/test_conversation_flow.py tests/e2e/test_db_smoke.py -v

# --- This machine's local Postgres (verified 2026-06-23) ---
# Server: Homebrew postgresql@16 (binaries under /opt/homebrew/opt/postgresql@16/bin).
#   Start: brew services start postgresql@16   (durable; ask before running)
# Superuser role is the macOS user `nuvanta-mac-3` (NO password); there is NO `postgres`
# role, so the placeholder user:pass above does not apply here. Create the test DB once:
#   createdb -h localhost tallyagent_test
# Full DB suite (283 tests — integration + DB e2e + dedup), all green 2026-09-30 (22 s that run; earlier runs took ~14 min):
TEST_DATABASE_URL=postgresql+asyncpg://nuvanta-mac-3@localhost/tallyagent_test ANTHROPIC_API_KEY=test-key PYTHONPATH=. uv run pytest tests/integration/ tests/e2e/test_db_smoke.py tests/e2e/test_db_data_entry.py tests/e2e/test_db_data_entry_group_b.py tests/unit/test_dedup.py -q 2>&1 | tee logs/db-suite-run.log

# All backend tests (unit + integration + E2E mock)
ANTHROPIC_API_KEY=test-key pytest tests/ -v --ignore=tests/e2e_live/

# All tests with coverage
pytest --cov=backend --cov-report=html

# Live E2E tests (needs real Tally + Claude API key, -s for stdout logging)
# ⚠️ EXPENSIVE: Uses real Claude API calls. NEVER run twice — always tee to log on first run.
RUN_LIVE_TESTS=1 PYTHONPATH=. pytest tests/e2e_live/ -v -s --host <TALLY_IP> --port 9000 2>&1 | tee docs/e2e-live-results.log

# E2E live tests in mock mode (no real Tally needed, still needs ANTHROPIC_API_KEY)
# ⚠️ EXPENSIVE: Uses real Claude API calls. NEVER run twice — always tee to log on first run.
PYTHONPATH=. pytest tests/e2e_live/ -v -s --tally-mode mock 2>&1 | tee docs/e2e-live-mock-results.log

# Verify Tally connectivity
python scripts/test_tally_connection.py

# Live test: agent pipeline against real Tally (needs ANTHROPIC_API_KEY)
PYTHONPATH=. uv run python scripts/test_agent_live.py --host <TALLY_IP> --port 9000

# Seed test data into Tally — ALWAYS pass --skip-preflight (see § Workflow Preferences)
python scripts/seed_tally_data.py --host <TALLY_IP> --port 9000 --skip-preflight

# Eval framework & Playwright tests — PREREQUISITE: backend AND frontend must be running:
#   Terminal 1: uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000 2>&1 | tee docs/be_run1.log
#   Terminal 2: cd frontend && npm run dev   (Vite dev server on port 5173)
# ⚠️ EXPENSIVE: Uses real Claude API calls. NEVER run twice — always tee to log on first run.
# Results auto-saved to tests/eval/results/ (transcripts, screenshots, scores, report)
PYTHONPATH=. python tests/eval/collect.py --scenario all --frontend-url http://localhost:5173 2>&1 | tee docs/eval-collect.log
ANTHROPIC_API_KEY=$(grep ANTHROPIC_API_KEY .env | cut -d= -f2) PYTHONPATH=. python tests/eval/judge.py 2>&1 | tee docs/eval-judge.log
PYTHONPATH=. python tests/eval/report.py               # → tests/eval/results/report.html

# Run single eval scenario
# ⚠️ EXPENSIVE: Uses real Claude API calls. NEVER run twice — always tee to log on first run.
PYTHONPATH=. python tests/eval/collect.py --scenario manual_test_regression --frontend-url http://localhost:5173 2>&1 | tee docs/eval-collect-manual.log

# Eval via pytest (all-in-one)
# ⚠️ EXPENSIVE: Uses real Claude API calls. NEVER run twice — always tee to log on first run.
RUN_EVAL_TESTS=1 PYTHONPATH=. pytest tests/eval/test_eval.py -v -s 2>&1 | tee docs/eval-pytest.log

# Generate golden fixtures from live Tally
PYTHONPATH=. python tests/eval/generate_golden.py --host <TALLY_IP> --port 9000
```

### Frontend (React)
```bash
cd frontend
npm install
VITE_DB_MODE=true npm run dev        # Vite dev server (DB mode — DEFAULT for all new work)
npm run dev                          # Legacy mode (frozen) — no auth, no sidebar
npm run build                        # Production build
npm test                             # Vitest unit tests
npm run test:watch                   # Vitest in watch mode
npm run test:playwright              # Playwright visual tests (responsive + eval-visual + db-mode × 3 viewports)
                                     # PREREQUISITE: backend must be running (uvicorn on port 8000)

# IMPORTANT: To regenerate Playwright screenshots, only delete the specific spec's screenshot
# directory (e.g. __screenshots__/*/db-mode.spec.ts/), not the entire __screenshots__/.
# See § Workflow Preferences → Playwright discipline.
```

## Architecture

### Three-Layer Backend

1. **Tally Bridge** (`backend/tally_bridge/`) — HTTP client that communicates with TallyPrime's XML/JSON API (default endpoint `localhost:9000`; also works across the LAN). Core modules:
   - `client.py` — Async HTTP client (httpx) posting to `http://<TALLY_HOST>:<TALLY_PORT>`
   - `request_builder.py` — Builds XML request payloads for each Tally operation
   - `response_parser.py` — Parses XML/JSON responses into normalized Python dicts
   - `queries/` — Domain-specific queries: `masters.py` (ledgers, groups), `reports.py` (TB, P&L, BS), `vouchers.py` (day book, sales/purchase register)
   - `models.py` — Pydantic models for Tally data structures
   - `exceptions.py` — `TallyConnectionError`, `TallyResponseError`

2. **Multi-Agent System** (`backend/agents/`) — Orchestrator routes queries to specialized agents:
   - `orchestrator.py` — Classifies query type (simple_lookup, comparison, trend, top_n, aggregation, greeting) and routes to appropriate agent(s)
   - `query_agent.py` — Fetches raw data from Tally via Claude tool-calling loop
   - `analysis_agent.py` — Performs comparisons, trends, rankings on fetched data
   - `chart_agent.py` — Picks chart type and formats data for Recharts rendering
   - `tools.py` — Claude-compatible tool schemas + tool handler mapping to tally_bridge functions
   - `prompts.py` — System prompts for each agent
   - `context.py` — Session/conversation context management

3. **FastAPI Routes** (`backend/api/`) — REST endpoints:
   - `POST /api/chat` — Main chat endpoint (NL query → agent pipeline → response with optional table/chart)
   - `GET /api/health` — Tally connectivity check
   - `GET /api/companies` — List loaded Tally companies (legacy mode)
   - `GET /api/reports/{name}` — Direct report access
   - DB mode adds: `POST /api/auth/{register,login,refresh,logout}`, `GET /api/auth/me`, workspace CRUD (`/api/workspaces`), conversation CRUD (`/api/workspaces/:id/conversations`), `GET /api/usage`

4. **Database Layer** (`backend/db/`) — PostgreSQL persistence (Set A1, optional via `DATABASE_URL`):
   - `engine.py` — Async SQLAlchemy engine + session factory
   - `models.py` — ORM models: User, Workspace, Conversation, Message, UploadedFile, VoucherEntry, VoucherEntryRevision (edit-history version trail), LedgerMapping, UsageLog
   - `migrations/` — Alembic migrations

5. **Agent Registry** (`backend/agents/registry.py`) — Pluggable agent types keyed by `workspace.agent_type`. Currently registers "tally" → `Orchestrator`. `BaseAgent` interface in `backend/agents/base.py`.

### Agent Pipeline Flow

```
User query → Orchestrator (classify) → Query Agent (fetch from Tally via tools)
  → Analysis Agent (if comparison/trend/top-N) → Chart Agent (if visual needed)
  → Final NL response with optional data table and chart spec
```

### Frontend (React + Vite + Tailwind + Recharts)

Chat-based UI at `frontend/src/`. Always run with `VITE_DB_MODE=true` — DB mode is the only supported path:
- **DB mode** (`VITE_DB_MODE=true`): `BrowserRouter` + `AuthProvider` → `LoginPage`/`RegisterPage` (public) or `ChatApp` (protected: `Header` + `Sidebar` + `ChatWindow`).
- **Legacy mode** (no `VITE_DB_MODE`): frozen. `SessionProvider` → `Header` + `ChatWindow`. No auth, no sidebar. Kept for reference; no new work.

Key components: `ChatWindow`, `MessageBubble` (renders text/table/chart inline), `ChartRenderer` (bar/line/pie via Recharts), `DataTable` (sortable with CSV export), `QuickActions` (preset query buttons), `Sidebar` (conversation list grouped by workspace), `ConnectCompanyModal`, `UserMenu`.

## Key Technical Conventions

- **Dates**: Tally expects DD-MM-YYYY format. Indian Financial Year = April 1 to March 31. Quarters: Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar.
- **Currency**: Use Indian comma formatting (₹12,34,567.00). Negative = outflow/debit, Positive = inflow/credit (Tally convention).
- **XML requests**: Preferred over JSON for Tally communication — more stable and documented across versions. Use `xml.etree.ElementTree` for parsing.
- **Claude models**: `claude-sonnet-4-6` for query/analysis agents, `claude-haiku-4-5-20251001` for orchestrator classification, `claude-opus-4-6` for eval judge.
- **Session management**: **DB mode** (Postgres-backed, persistent conversations, per-workspace config) is the default and only maintained path. Legacy in-memory mode is frozen.
- **Tally Bridge supports reads + writes**: query path (`backend/tally_bridge/queries/`) is read-only and broadly used. Write path (`backend/tally_bridge/import_builder.py`) lands vouchers via Set B1a (Payment) and Group B (Sales/Purchase/DN/CN). All writes must follow [`LESSONS.md` § 15 Tally Write Safety](LESSONS.md).
- **Configuration**: All settings via `.env` file loaded by `pydantic_settings.BaseSettings` in `backend/config.py`.

## Environment Variables

```
TALLY_HOST, TALLY_PORT (default: localhost:9000)
ANTHROPIC_API_KEY
CLAUDE_MODEL (default: claude-sonnet-4-6)
CLAUDE_CLASSIFIER_MODEL (default: claude-haiku-4-5-20251001)
EVAL_JUDGE_MODEL (default: claude-opus-4-6)
APP_HOST, APP_PORT (default: 0.0.0.0:8000)
VITE_API_URL (default: http://localhost:8000)

# DB mode (Set A1) — REQUIRED for new development. Legacy fallback exists but is frozen.
DATABASE_URL                         # e.g. postgresql+asyncpg://user:pass@localhost/tallyagent
JWT_SECRET                           # minimum 32 chars
JWT_ACCESS_TOKEN_EXPIRY_MINUTES      # default: 30
JWT_REFRESH_TOKEN_EXPIRY_DAYS        # default: 7
VITE_DB_MODE=true                    # frontend: always set to true for DB mode (auth + sidebar)
TEST_DATABASE_URL                    # for DB integration tests (separate DB recommended)
TALLY_WRITE_ENABLED=true             # required for write paths (Set B1a, Group B)
```

## Testing

_Approximate counts as of 2026-06-29 (write-flow edit + edit-history closeout); ~1,625 backend + ~381 frontend, 0 failures. (Live-Tally e2e_live and eval are gated/expensive — see commands above.)_

- **Unit tests** (`tests/unit/`): Pure logic, no I/O. Test request XML construction, response parsing, date utils, currency formatting, mock handler, auth utils, pricing, DB models. ~956 tests.
- **Integration tests** (`tests/integration/`): Use mock Tally HTTP server (`tests/mocks/mock_tally_server.py`) built with aiohttp. Tests full request→parse→return cycle + mock format parity. Also includes DB integration tests (auth flow, workspace CRUD, conversation persistence) — these require `TEST_DATABASE_URL`. ~132 + 15 DB tests.
- **E2E tests** (`tests/e2e/`): Full NL query → agent → Tally → response pipeline. Uses mock Claude API (`tests/mocks/mock_claude_api.py`) to avoid API costs. Includes legacy smoke tests and DB smoke tests (require `TEST_DATABASE_URL`). ~39 + 14 DB tests.
- **E2E live tests** (`tests/e2e_live/`): End-to-end against real Tally + real Claude API. Gated by `RUN_LIVE_TESTS=1` env var OR `--tally-mode mock`. In mock mode, uses built-in mock handler (no real Tally needed, still needs Claude API key). 19 tests.
- **Eval tests** (`tests/eval/`): Two-phase eval framework (collect → judge → report). Playwright drives multi-turn conversations against real frontend, LLM-as-a-judge scores responses across 5 dimensions (factual, quality, coherence, error handling, chart quality). 8 scenarios, 49 turns. Gated by `RUN_EVAL_TESTS=1`. Run standalone: `collect.py` → `judge.py` → `report.py`. Mock mode: `--tally-mode mock` auto-selects `*_mock.yaml` scenario variants when available.
- **Frontend unit tests** (`frontend/src/__tests__/`): Vitest + React Testing Library. Tests all components + utils. ~381 tests. NOTE: the default sandbox `TMPDIR` is not writable for vitest/Playwright — run with `TMPDIR="$PWD/.tmp_vitest"` (see § Workflow Preferences).
- **Frontend Playwright tests** (`frontend/tests/playwright/`): Visual tests — responsive (5 page states × 3 viewports) + eval-visual (8 fixtures × 3 viewports) + db-mode (20 specs × 3 viewports, 11 viewport-specific skips) ≈ 49 pass + 11 skip. Discipline rules (state matrix, visual checklist, screenshot regeneration scope, main-agent visual review) live in § Workflow Preferences → Playwright discipline.
- **Fixtures** in `tests/fixtures/` — Sample Tally XML/JSON responses for each report type.
- **Test plan thoroughness** is mandatory for every design spec — see § Workflow Preferences → Spec & test plan thoroughness.
- **Test company**: `Bharat Traders Private Limited` (Electronics & Office Supplies trader, Maharashtra, FY Apr 2025–Mar 2026). Seeded via `scripts/seed_tally_data.py`; backup committed at `seed_data/TDBK1800_100003.001`. Setup procedure in [`docs/seed-data-setup.md`](docs/seed-data-setup.md).

## Workflow Preferences

### Git branch discipline

- **Allowed for Claude:** work on a feature branch → merge into the `dev` branch.
- **NOT allowed:** never merge into `master` directly. `master` only changes via PR (reviewed and merged through GitHub).

### Feature development lifecycle

Standard flow for every feature, in order:

1. **Spec + plan** before implementation (`docs/specs/*`, `docs/plans/*`).
2. **Implement** (subagent-driven, per § Process).
3. **Thorough test plan** after implementation — must cover E2E + frontend + backend, with test coverage stated. Don't stop at smoke tests: poke for full FE/BE E2E coverage.
4. **Test cases** — including Playwright specs + screenshots (per § Playwright discipline).
5. **Code review** → fix review findings → feature-complete merge (into `dev`, per § Git branch discipline).
6. **Manual testing finds bugs** → that means the test cases were insufficient: first improve/extend the test cases to catch the bug, then debug and fix.
7. **Tracker update — as each part completes, not at the end** (see § Always update the tracker).
8. **Docs update** (roadmap, CLAUDE.md, LESSONS.md as applicable) before session end.

Additional rules:
- **Backend logs → `logs/`**: keep backend server logs in the `logs/` folder for each test run (e.g. `uvicorn ... 2>&1 | tee logs/be_<run>.log`).
- **E2E defaults to no-live-Tally, no-Claude-API** (mock Tally + mock Claude). Real-API runs are a separate, deliberate step.
- **Claude-API eval runs only once everything is complete**: smoke → manual → eval, in that order. Eval flow: run eval scenario (`collect.py`) → run judge (`judge.py`) → generate report (`report.py`) or check the results JSON.
- **Eval covers the query flow and (since 2026-06-09) a write flow** — `scenarios/write_flow_mock.yaml` (upload → voucher review → Write to Tally), with `voucher_correctness` rubric + upload/action turn types in `collect.py`. Run in mock-Tally mode (real Vision, no live writes). See `docs/eval-write-flow-2026-06-09.md`.

### Process

- **Always use skills** for all tasks — debugging, feature development, TDD, planning, code review, etc. Never skip skill invocation even for seemingly simple tasks. If there's even a 1% chance a skill applies, invoke it.
- **Prefer superpowers skills over feature-dev**: When both `superpowers:*` and `feature-dev:*` skills could apply, always use the superpowers variant first (e.g., `superpowers:brainstorming` over `feature-dev:feature-dev`, `superpowers:systematic-debugging` over ad-hoc debugging, `superpowers:test-driven-development` over writing tests directly).
- **Code review after every implementation**: After completing any implementation task (feature, bugfix, refactor), always run a code review using `superpowers:requesting-code-review` or the `code-review` agent. Never skip this step — even on smooth implementations. (Caught violation during Phase 9: all 17 tasks shipped without review.) Store review results in `docs/code-review-*.md`. Also report clearly which test suites were NOT run (e.g., `e2e_live` needs real Tally).
- **Main agent = orchestrator only**: The main conversation agent should NEVER write implementation code directly. Always spawn subagents (via the Agent tool) for code changes, writing tests, and debugging. The main agent's role is to plan, dispatch subagents, review their output, and integrate results. This preserves context window for planning and coordination.
- **Persist artifacts in docs/**: Store code review results, implementation plans, and status tracking in `docs/` so they survive across sessions. Examples: `docs/specs/*`, `docs/plans/*`, `docs/code-review-*.md`.

### What targets which doc

- **What's next** → [`docs/roadmap.md`](docs/roadmap.md). Single source of truth for active work. Update it when feature sets close, when new sets are added, when priority changes, or when a probe/gating step is added or resolved. Don't track active roadmap in `CLAUDE.md` (status only) or in individual spec/plan docs (those are per-feature).
- **Closed phases** → § Implementation Phases in this file. Promote completed roadmap items here with a one-line summary.
- **Tally API gotchas** → [`LESSONS.md`](LESSONS.md). Especially § 15 (write safety rules). Cross-reference, don't re-state.
- **Parked items** → [`docs/open-items-parked.md`](docs/open-items-parked.md).
- **Item-level status** → the feature's **tracker** in `docs/plans/*-tracker.md` (e.g.
  [`docs/plans/2026-09-22-bi-part1-tracker.md`](docs/plans/2026-09-22-bi-part1-tracker.md) for the BI sync work).

### Always update the tracker

**Whenever any part is completed, update the tracker in the same turn — never as a later cleanup pass.** A tracker
that lags behind the work is worse than no tracker: the next session reads it, believes it, and redoes or skips work.
This has already gone wrong once (2026-09-23: probes had run live, but the tracker still said "nothing has been run
live yet" and "`v2/` is not committed").

What "update the tracker" means every time:
1. **Flip the status** of the item you finished (⬜ → 🟡 when you start it, ✅ when it's done), and of any parent
   stage/section row whose state changed as a result.
2. **Add the proof** the tracker's own rules demand — a test name, a log path, a fixture path, a doc section, or a
   commit SHA. No proof → not ✅.
3. **Add a dated change-log row** at the bottom of the tracker describing what happened, including findings that
   contradicted expectations.
4. **Rewrite the "Resume here" block** so it describes the state you are actually leaving behind and the next
   concrete step — not the state you found.
5. **Fan out the consequences:** a result that changes the design → update the spec too (with a dated "Changed" line
   in its header) and say so in the tracker; a stage opening or closing → also update `docs/roadmap.md`.

The same rule applies to a tracker's own local "Rules for updating this file" block — follow it, don't just read it.

### Default to DB mode

DB mode is now the default. Legacy mode (non-DB / no-user-auth / no-sidebar) is frozen. Apply this everywhere:
- New features: DB mode only. No `if not settings.db_mode` branches.
- New tests: DB mode only (require `TEST_DATABASE_URL` or DB fixtures).
- Bug fixes: fix in DB mode. If legacy-only and not blocking a demo, ignore.
- Existing legacy unit/integration tests covering core Tally bridge logic: keep — they're mode-independent.
- Existing legacy E2E/smoke tests: delete-when-broken; don't invest in keeping them green.
- Manual testing: always start backend with `DATABASE_URL` + `JWT_SECRET` (+ `TALLY_WRITE_ENABLED=true` for write paths), frontend with `VITE_DB_MODE=true`.

### Expensive test discipline

- **Never run `e2e_live`, `eval/collect.py`, `eval/judge.py`, `eval/test_eval.py`, or `scripts/test_agent_live.py` twice for logging.** They make real Claude API calls. Always pipe to a log file on the *first* run via `2>&1 | tee docs/<logfile>.log`. The commands above already include this pattern — preserve it.

### Spec & test plan thoroughness

Every design spec must include:

1. **State matrix** — every `component × state × variant` combination. Cover all user action flows (approve / discard / edit→approve / edit→discard / edit→cancel / reclassify). Include currency, GST, and error variants.
2. **Test fixture matrix by category**:
   - Backend fixtures (e.g. Vision JSON): one per doc type × currency × GST variant × edge case.
   - Sample upload files: one per supported format (jpg, png, pdf, csv, xlsx) plus rejection cases (unsupported format, oversize, no extension).
   - Mock API responses: every external service response the tests need.
   - Frontend mock data: TypeScript prop objects for component tests.
3. **DB persistence scenarios** — if the feature writes to DB: create/update/delete flows, linked records, status transitions, query-back verification.
4. **Regression coverage** — fixtures must cover existing flows being extended, not just new ones.

High-level "test these layers" tables alone are insufficient. Enumerate the surface; don't summarize it.

### Test reality, not an ideal

Two bug classes have repeatedly slipped through green test suites and were only caught by manual testing against the live app + seed company (e.g. 2026-06-24: voucher-edit Save didn't update the card; party ledgers under custom sub-groups flagged as "new"). Both share a root cause: the tests validated a simpler world than production. Rules to prevent recurrence:

1. **Mock the network, not your own components.** Every user-facing flow (edit / approve / discard / reclassify) needs ≥1 test that renders the **top-level container** (e.g. `ChatWindow`), drives real clicks, and asserts the **rendered DOM** changed — stubbing only the true external boundary (the HTTP API call). Never stub the internal component handoff where the bug usually lives (parent state merge, prop wiring). Isolated tests with a mocked `onSave`/`onEdit` prove the callback fires, not that the UI updates.
2. **Fixtures mirror the seed company, not an ideal.** Test masters/ledgers/groups must include the messy real shapes the live company has: **custom sub-groups** (e.g. `National Creditors`/`Local Creditors` under Sundry Creditors), **non-bill-wise parties**, duplicate names under different parents, custom GST-ledger parents. A fixture that puts every party directly under "Sundry Creditors" encodes the same wrong assumption as buggy code and will *confirm* the bug. Prefer golden fixtures generated from live Tally (`tests/eval/generate_golden.py`) so test data can't drift simpler than reality.
3. **Adversarial fixture review.** Before trusting a green test, ask "what real-world variant is this fixture NOT modeling?" and add one fixture per variant.
4. **A flagged gap is a TODO, not a footnote.** If a coverage gap is identified, write the test or get explicit sign-off to skip — do not silently downgrade it to "minor".
5. **Test the blast radius, not just the diff lines.** When a change touches a flow (e.g. party resolution), exercise the whole flow's real-world variants — not only the literally changed lines (the defect is often in adjacent pre-existing code).
6. **One real-app pass before claiming "covered."** For any touched user flow, run the actual app against real data once (per § Default to DB mode) before reporting coverage. "Layers exist and pass in isolation" ≠ "the connected real-data path works".
7. **The refresh round-trip test (persisted flows).** A whole class of bugs (2026-06-24: edited voucher values lost on reload; voucher-action result messages lost on reload) survived green suites because no test exercised the **page-refresh / rehydration lifecycle**. For ANY flow that persists state, add an explicit *act → reload → re-assert* test, on BOTH layers:
   - **Backend (DB e2e):** after the action, **re-fetch the conversation from the DB** (the same query rehydration uses — `Message` rows ordered by `created_at`) and assert the **entire user-visible state** is intact: the card's edited field VALUES, its `status`, AND any result/assistant messages — not just the one attribute the fix touched. A test that asserts only `status` survives will miss edits and messages (it did). Mirror the existing `..._persists_*_on_reload` tests but assert the whole object, and assert message COUNT (exactly one per action — not zero, not two).
   - **Frontend:** don't rely on mocked-API unit tests for persistence — they never touch the real round-trip. Where feasible, drive a real reload (Playwright/eval harness "reload and re-assert" step) so FE rehydration + BE persistence are verified together across the seam.
   - **Trigger:** any change that adds, removes, or alters a persistence path REQUIRES a refresh round-trip test before it's "done" — ESPECIALLY a fix that *removes* a write (e.g. making an action local-only silently drops persistence; that exact change caused the edit-lost-on-reload bug). When you change persistence semantics, immediately ask "does this survive a refresh?" and write the test that proves it.

### Playwright discipline

- **State matrix before specs.** Build a `component × state × viewport` matrix before writing Playwright specs. Each state needs a mock data variant and a separate screenshot. For example: VoucherReviewCard = {draft / pending / written / discarded / error}; Sidebar = {expanded / collapsed / active-highlight / empty-workspace}; Header = {live-badge / demo-badge / long-name-truncation}.
- **Assertions before screenshots.** Add `expect(locator).toHaveText(...)` content assertions *before* `toHaveScreenshot()` calls — screenshots catch regressions but assertions catch functional bugs.
- **Visual checklist comment block.** Every `toHaveScreenshot()` MUST be preceded by a `// VISUAL CHECKLIST:` comment block stating: layout elements, specific text, colors / highlights, spacing / alignment, and what should NOT be visible. This guides the manual review.
- **Main agent must inspect screenshots — every viewport.** After any Playwright run, the main agent (not a subagent — subagents can't see images) must use the Read tool on PNGs in `frontend/tests/playwright/__screenshots__/{mobile,tablet,desktop}/` before reporting pass/fail. Check for: blank space, content cutoff, sidebar/header overlap, missing text/tables/charts, unreadable elements. "N passed" only means pixel-diff matched the baseline — it does NOT mean the UI is correct. On first `--update-snapshots` there is no baseline, so everything passes by definition.
- **Screenshot regeneration scope.** Only delete `frontend/tests/playwright/__screenshots__/*/<spec-name>.spec.ts/` for the specific spec being regenerated. Deleting the entire `__screenshots__/` directory wipes responsive + eval-visual baselines that require a different frontend mode to regenerate.

### Seeder / Tally write specifics

- **Always pass `--skip-preflight` to `seed_tally_data.py`.** A fresh Tally company's F2 date starts at FY-start (e.g. 2025-04-01), so the preflight check halts the seed. The user typically sets F2 manually in the UI right before/during the seed. Vouchers later than F2 drop silently; the verifier (`scripts/verify_tally_bridge_live.py`) catches it via the `day_book >= 50 vouchers` check.
- **Read Tally config before any write.** See [`LESSONS.md` § 15](LESSONS.md) for the full operational checklist (probe voucher-type config, readback after every ALTER, REFERENCEDATE silent-overwrite detection, voucher-type config changes via UI only, etc.).

## Implementation Phases

Closed phases in build order, with one-line learnings. For what's next, see [`docs/roadmap.md`](docs/roadmap.md).

| Phase | Status | Notes & key learnings |
|---|---|---|
| 1 — Tally Bridge | ✅ | `client.py`, `request_builder.py`, `response_parser.py`, models, exceptions. XML preferred over JSON across Tally versions. |
| 2 — Agent Orchestrator & Tools | ✅ | Claude tool-calling loop, query classification. |
| 2b — Analysis & Chart Agents | ✅ | Comparison, trend, top-N, aggregation. |
| 3 — FastAPI Backend | ✅ | `POST /api/chat`, health, companies, reports. |
| 4 — React Frontend | ✅ | Chat UI with inline Recharts + DataTable. |
| 4b — Automated tests | ✅ | Vitest + Playwright (responsive + eval-visual). |
| 5 — Eval Framework | ✅ | Two-phase (collect → judge → report). 8 scenarios, 49 turns. |
| 6 — Eval Fixes (6 sub-phases) | ✅ | Flatten multi-dataset, date resolution, auto-enable charts, tool call limit 10→25, Langfuse observability. |
| 7 — Date Validation | ✅ | `validate_tally_date()` regex + strptime + ISO autofix. `$$InDateRange` removed (not valid TDL). |
| 8 — Eval Accuracy + UX | ✅ | Prompt rules 10–13, chart metadata stripping, inline markdown tables. |
| 8b — Analysis Agent Fixes | ✅ | `_separate_tool_results()`, MAX_TOOL_CALLS 8→15, linear Change % line. |
| 9 — Mock Tally + Demo Mode | ✅ | Built-in `mock_handler.py`, frontend toggle, `e2e_live --tally-mode mock`. |
| 10 — Enriched Mock Data | ✅ | Fixture generator (34 ledgers, 50 vouchers, 15 stock items), 71 format parity tests. |
| 11 — Tally Bridge Gap Closure | ✅ | 6 new tools (12→18): `list_all_ledgers`, `list_stock_items`, `list_account_groups`, `get_payment_register`, `get_receipt_register`, `get_cash_flow`. |
| 12 — Code Execution Tool | ✅ | QueryAgent = data-fetch only; AnalysisAgent = sole computation authority. anthropic 0.84.0, pydantic 2.12.5. |
| 13 — Chart Rendering Fixes | ✅ | `_format_pie_data` dynamic numeric scan; strip `*` in `_to_numeric`; `_has_table_intent()` honors user intent. |
| 14 — Eval Data Accuracy | ✅ | Session context for AnalysisAgent, partial-period P&L rejection, ground truth keys. |
| 15 — Architecture Bug Fixes | ✅ | Unconditional AnalysisAgent, context window 4→8 messages, QueryAgent max_tokens 1024→2048. |
| 16 — Chart Stabilization | ✅ | Haiku chart advisor (`get_chart_advice()`) + markdown table parser + ChartAgent Rules A–D. AnalysisAgent streaming (max_tokens 32768), FE timeout 480s. |
| Set A1 — Auth + Persistence | ✅ merged | DB mode (Postgres + JWT + workspaces + conversations). DB mode is now default. |
| Set B1a — Expense Receipt Entry | ✅ merged | File upload → Vision extract → Tally Payment voucher. First write path live. |
| Group A — UI enhancements (F4 + F5 + F6) | ✅ 2026-04-12 | Deferred conversation creation, workspace landing, voucher button states, responsive drawer. |
| Tally Seed Stage 0 — write exploration | ✅ 2026-05-04 | All 9 write ops + delete path verified against live Tally. Canonical reference: `docs/tally-write-exploration-v4.md`. |
| Tally Seed Stage 1 — build + live verify | ✅ 2026-05-07 | Builders + writer + seeder + Tier-3 verifier (13/13). 50 vouchers, 16 bills receivable, 8 bills payable. REFERENCE/REFERENCEDATE/BILLALLOCATIONS baked into seeder. |
| Tally Seed Stage 2 — backup distribution | ✅ 2026-05-08 | Receipts at gross + Agst Ref on RCT/PMT. Backup committed at `seed_data/TDBK1800_100003.001`. Restore + reseed guide: `docs/seed-data-setup.md`. Expected residuals: ₹9,70,537 receivable / ₹18,34,142 payable. |
| FX → INR (Slice A / F2) | ✅ merged 2026-06-08 | Stop model converting; extract original currency+amount, code-computed rate (doc→default→fallback), chat-only override, INR-only into Tally, no-rate writes blocked. Spec/review: `docs/specs/2026-06-08-fx-inr-conversion-design.md`, `docs/code-review-fx-inr-2026-06-08.md`. |
| Group B — write-agent (F1+F3+B1b/B1c) | ✅ merged 2026-06-09 | Upload→Vision classifies 5 types (payment/purchase/sales/DN/CN)→route→party+GST+bill alloc→write; company dropdown (test-connection); DB audit. Task 0 probes E1–E8 + TDS + bank done (`docs/group-b-task0-probe-results-2026-06-08.md`). Review: `docs/code-review-group-b-2026-06-09.md`. |
| GST on invoices + DN/CN direction | ✅ merged 2026-06-09 | GST line items → Input/Output GST ledgers (resolved from Duties & Taxes). DN/CN posting direction fixed (was inverted) — **all live-verified** (real Vision/PDF → live Tally → read-back, `logs/manual_test_*_live.log`). Write-flow eval added (`docs/eval-write-flow-2026-06-09.md`). |
| Invoice entry Phase 1 — Invoice No. + dedup | ✅ merged 2026-06-10 | Supplier invoice no. → Tally `REFERENCE` + shown on card; duplicate **hard block** (file-hash + party/invoice-no vs DB & Tally, server-side re-derived). Spec `docs/specs/2026-06-10-invoice-entry-phase1-design.md`; review `docs/code-review-invoice-dedup-2026-06-10.md`. |
| Invoice entry Phase 2 — Inventory line items | ✅ merged 2026-06-11 | Goods invoices post line items into the Tally **stock grid** (qty/rate) via the stock-based builder; Vision unit extraction, stock-item fuzzy resolver, goods-vs-services routing, match/create-in-card UI. **Live-verified 5/5.** Post-merge live fixes (2026-06-11/12): distinct `invoice_number` extraction, only-create-missing masters + `altered=1`=already-exists, FE reverts false "Written". New Tally gotchas in [`LESSONS.md`](LESSONS.md) §15 rules 10–14. |
| Upload/voucher persistence fix | ✅ merged 2026-06-12 | DB mode rehydrates conversations from `Message` rows only; `/chat/upload` persisted none (cards vanished on reload) and `/chat/voucher-action` never persisted written/discarded status (gated on a `conversation_id` the entry dict never carried). Now persists upload Messages + titles the conversation, and threads `conversation_id` via the request to persist status. New lesson [`LESSONS.md`](LESSONS.md) §17; review `docs/code-review-upload-voucher-persistence-2026-06-12.md`. |
| Sidebar conversation actions (rename + delete) | ✅ merged 2026-06-12 | ChatGPT-style per-conversation kebab (⋯) menu: inline Rename (PATCH) + Delete behind a confirm (soft-delete); deleting the open chat → workspace landing; menu flips up near the list bottom; a11y roles; theme-matched confirm dialog. Frontend-only (backend `DELETE`/`PATCH` pre-existed). Spec `docs/specs/2026-06-12-sidebar-conversation-actions-design.md`; review `docs/code-review-sidebar-conversation-actions-2026-06-12.md`. |
| Classifier company-anchoring | ✅ 2026-06-19→29 | Vision prompt anchors purchase-vs-sales to the workspace's own company; hierarchy-aware ledger matching (custom creditor/debtor sub-groups); existence-safe `create_ledger`; billwise party ledgers. Spec `docs/specs/2026-06-19-classifier-company-anchoring-design.md`; review `docs/code-review-write-flow-fixes-2026-06-19.md`. |
| Edit-flow correctness + edit-history + file_id linkage | ✅ 2026-06-29 | Edit Save = local merge (no premature write); draft edits + result messages persist across refresh; inventory edit recomputes total+GST; **`voucher_entry_revisions`** version trail (migration 004) + **`file_id`** join across upload→card→history→audit (migration 005). Full detail: `docs/specs/2026-06-29-edit-history-file-id-design.md`. |

**Up next:** hardening from the write-flow code analysis (`docs/code-analysis-write-flow-2026-06-09.md`) — SSRF host/port allowlist, entry-dict typing + sign-convention single-source, cheap correctness warnings (DN/CN no-ref, currency-defaulted-to-INR, GST-ledger-missing block), eval mock GST ledgers. Then future slices (supplier-payment-against-bill, TDS journals, bank reconciliation) — each needs its own spec. See [`docs/roadmap.md`](docs/roadmap.md).
