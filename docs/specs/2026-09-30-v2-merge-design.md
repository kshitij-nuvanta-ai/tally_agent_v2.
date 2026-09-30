# v2 merge — one DB, one backend, one tally bridge

> **Status:** tracked in [`plans/2026-09-30-v2-merge-tracker.md`](../plans/2026-09-30-v2-merge-tracker.md), not here.
> **Supersedes:** Part 1 spec §5 "Code isolation (v2)" and §10 ("merging v2 into the current code is a later step with
> its own spec") — this is that spec. S1 spec decisions D5 (read-only `users`/`workspaces`), D27 (port 8100, `V2_`
> prefix) and plan ruling A2/A12 are changed as written below.

## 1. Why

Direction from the senior dev, 2026-09-30 (tracker change log, `plans/2026-09-22-bi-part1-tracker.md`):

- Architecture becomes **installer → agent → tally bridge**. The bridge runs on the Tally PC; the cloud app talks only
  to our Postgres DB.
- Merge `v2/` back into the main code: same DB, same backend, same tally bridge.
- Clean up non-DB ("legacy") mode separately afterwards; unit tests stay.
- The installer is to be defined in detail.

`v2/` was built isolated on purpose: its own FastAPI app (port 8100), settings class, DB engine, Alembic chain and
copies of auth and Tally-client code. Its 21 tables already sit in the same database as the app's 9, with foreign keys
to `users` and `workspaces`. So this merge moves code and bookkeeping. It moves no data.

## 2. Scope

**In (this spec, branch `feat/merge-v2-into-backend`):** the structural merge in §4.

**Out (separate branches, separate specs):**

| Later step | What |
|---|---|
| Legacy removal | Delete non-DB mode (backend, frontend, tests). Auth on `/api/reports`, one ownership helper. |
| DB reads | Chat tools and reports answer from the `tally_*` tables (Part 2). |
| Agent + installer | Build the agent; the backend stops importing `tally_bridge`; write path through the agent. |

**No behaviour change** to chat, upload, write-to-Tally or the frontend in this step.

## 3. Decisions

| ID | Decision | Replaces |
|---|---|---|
| M1 | One FastAPI app. The four sync routers mount in `backend/main.py`; port 8100 and `GET /api/v2/health` go. | D27 |
| M2 | One settings class (`backend/config.py`). New names are unprefixed (`DEVICE_TOKEN_SECRET`, …); the `V2_*` names stay accepted as aliases. | D27, A12 |
| M3 | One DB engine (`backend/db/engine.py`), with `pool_pre_ping` and `hide_parameters` adopted from v2. | — |
| M4 | One declarative `Base`. Sync models live in `backend/db/sync_models/` and reference the real `User` / `Workspace` tables; the read-only reflections are deleted. | D5 (read-only reflection) |
| M5 | One Alembic chain. Revision `006_sync_tables` creates the 21 tables, or adopts them when `alembic_version_v2` is already at `v2_001`, then drops `alembic_version_v2`. | Part 1 §5 "separate chain" |
| M6 | The 21 tables keep their names, columns and constraints. `sync_workspaces` stays a table; folding it into `workspaces.config` is dropped. | Part 1 §5 "folding … is part of the later merge" |
| M7 | One auth. Web routes use `get_current_user`; agent login and relink use `verify_password` from `backend/utils/auth.py`. The copied `passwords.py` and `web_jwt.py` are deleted. Device tokens keep their own secret, which must differ from `JWT_SECRET` (D6 stands). | copied helpers |
| M8 | Agent login and web login share one per-email limiter (5 failures / 15 min). | separate stores |
| M9 | Two error shapes stay: sync and agent routes keep `{"error", "detail"}` (the agent's wire contract); existing routes keep `{"detail"}`. | — |
| M10 | One tally bridge: top-level `tally_bridge/`, holding the current bridge plus what only the v2 copy had. The backend keeps importing it until the agent ships. | decision 2 "copy, don't import" |
| M11 | Existing float-returning report parsers keep their behaviour; the Decimal parsers from v2 are added beside them for the sync path. | — |
| M12 | Layer rule (tested): `agent` imports neither `backend` nor `probes`; `backend` imports neither `agent` nor `probes`; `contract` imports none of them; all may import `tally_bridge` and `contract`. | `test_isolation.py` rules |
| M13 | One uv project and lockfile. The `db` extra becomes core dependencies. | two projects |

## 4. Target layout

| From | To |
|---|---|
| `v2/cloud/models/*` | `backend/db/sync_models/` |
| `v2/cloud/models/current.py` | deleted |
| `v2/cloud/api/{agent_auth,devices,sync,web_sync}.py` | `backend/api/{agent_auth,devices,sync,workspace_sync}.py` |
| `v2/cloud/api/dependencies.py` | `backend/api/sync_dependencies.py` |
| `v2/cloud/{sync,ingest,parity}/`, `clock.py`, `errors.py` | `backend/sync/` |
| `v2/cloud/auth/{device_tokens,rate_limit}.py` | `backend/utils/` |
| `v2/cloud/auth/{passwords,web_jwt}.py` | deleted |
| `v2/cloud/config.py`, `db.py`, `main.py` | merged into `backend/config.py`, `backend/db/engine.py`, `backend/main.py` |
| `v2/cloud/cli.py`, `__main__.py` | `backend/sync/cli.py` (`purge` only; `migrate` is plain `alembic upgrade head`) |
| `v2/cloud/alembic/` | `backend/db/migrations/versions/006_sync_tables.py` |
| `v2/contract/` | `contract/` |
| `v2/agent/` | `agent/` |
| `v2/agent/tally/` + `backend/tally_bridge/` | `tally_bridge/` |
| `v2/probes/` | `probes/` |
| `v2/tests/` | `tests/{sync,agent,contract,probes}/` |
| `v2/pyproject.toml`, `v2/uv.lock`, `v2/README.md` | deleted; README content moves to the root `README.md` |

## 5. Migration `006_sync_tables`

| Database state before | What `upgrade()` does | State after |
|---|---|---|
| Fresh (chain at `005`, no sync tables) | Creates the 21 tables (body of `v2_001`) | 30 app tables, `alembic_version = 006` |
| Already has the v2 chain (`alembic_version_v2 = v2_001`) | Creates nothing; drops `alembic_version_v2` | same tables and rows, `alembic_version = 006` |
| `alembic_version_v2` present at any other revision | Fails with a clear message | unchanged |

`downgrade()` drops the 21 tables. Known databases in the second state: dev `tallyagent`, scratch
`tallyagent_v2_review`, `tallyagent_fork`.

## 6. Tally bridge consolidation

| Item | Source | Result in `tally_bridge/` |
|---|---|---|
| Client | both | One `TallyClient`: mock mode kept (backend), per-request timeout and `TallyTimeoutError` added (v2). `post_xml` still returns `str`; `post` returns `TallyResponse`. |
| Envelopes | both | v1 `build_*` builders kept; v2 `wrap_collection` / `wrap_report` added; company name always escaped; date variables typed. |
| XML helpers | three copies | One `sanitize_xml`, `detect_error`, `get_text`; `read_objects` and the fixed `parse_company_list` from v2. |
| Amounts | v1 float, v2 Decimal | Both kept: `parse_amount` (float, chat path) and `amounts.py` (Decimal, sync path). |
| Report parsers | v1 float, v2 Decimal subset | v1 parsers unchanged; v2 Decimal parsers in `tally_bridge/sync_reports.py`. |
| Writes | v1 only | `import_builder.py`, `writer.py` unchanged. Probes import `_esc` / `_wrap_import` from it. |
| Mock | v1 only | `mock_handler.py` unchanged. |

Any v1 envelope whose bytes change (escaping, typed dates) is covered by the existing request-builder tests, which are
updated in the same commit with the reason.

## 7. Test plan

| Suite | Expectation |
|---|---|
| `tests/unit`, `tests/integration`, `tests/e2e` (no DB) | Same pass count as the pre-merge baseline (1595) apart from import-path edits. |
| DB suite (`TEST_DATABASE_URL`) | 283 stay green. |
| `tests/sync` (was `v2/tests/cloud`) unit + DB | Same counts as `v2/tests/cloud` before the merge. |
| `tests/agent`, `tests/contract`, `tests/probes` | Same counts as before. |
| New: `tests/test_layers.py` | Enforces M12. |
| New: migration tests | The three rows of §5; model metadata equals the migrated schema. |
| New: shared login limiter | Failures on web login count against agent login and the reverse. |
| Real-app pass | One server on 8000: register → agent login → bind → heartbeat → batch → sync-status; chat in mock mode. |

Not run in this step: `e2e_live`, eval, Playwright, live Tally tiers B and C.

## 8. Risks

| Risk | Mitigation |
|---|---|
| A database with the v2 chain at an unexpected revision | §5 row 3: fail loudly, never guess. |
| Typed date variables change what existing reports return | Request-builder tests plus mock-parity tests; v2 probes already proved the typed form on live Tally (C33). |
| The shared test database: both suites used `tallyagent_test` and must not run together (A2) | One suite now, one harness; A2 no longer applies. |
| Import of `backend.main` builds an engine at import time (v2 did) | Engine is built only in the lifespan. |
