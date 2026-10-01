# Code review — v2 merge (2026-09-30)

- **Range:** `99ced44..23c6401` on `feat/merge-v2-into-backend` (T1–T8). Docs commit `e01ff43` not reviewed.
- **Reviewer:** independent subagent, read-only, given the code and the spec's claims to check (not the author's
  conclusions).
- **Spec:** [`specs/2026-09-30-v2-merge-design.md`](specs/2026-09-30-v2-merge-design.md).
- **Verdict:** 0 Critical, 3 Important, 10 Minor. No regression found for existing callers of chat, upload,
  write-to-Tally, auth, workspaces or the sync API.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| I1 | Important | Migration `006` adopt path checked only that the 21 table names exist, so a hand-altered sync table (dropped column, changed type) was stamped `006` | **Fixed:** adopt compares every column's name, type and nullability against this revision's own frozen definitions and refuses on a difference, changing nothing. Indexes and constraints are not compared (stated in the docstring). New test adopts a database built by the ORIGINAL `v2_001` file from git |
| I2 | Important | Sync tables present but no `alembic_version_v2` fell into the fresh path and failed with a raw `DuplicateTableError` | **Fixed:** refused with a clear message that lists the tables and says what to do |
| I3 | Important | `alembic upgrade head` ignored `.env` and fell back to the URL hard-coded in `alembic.ini` (`localhost/tallyagent`), so it could migrate a different database than the app serves (pre-existing; it happened once in this session) | **Fixed:** `backend/db/migration_url.py` — `config.attributes["url"]`, else the app's own `Settings().DATABASE_URL` (environment, then `.env`, `V2_DATABASE_URL` alias), else fail. `alembic.ini` carries no URL |
| M6 | Minor | An unexpected non-DB error on a sync route answered with the main app's generic body, neither the old plain 500 nor the sync contract | **Fixed:** `SyncRoute` answers `{"error": "internal_error", "detail": ""}` and logs class, route and raising location only; `ApiError`, `HTTPException` and validation errors unchanged |
| M4 | Minor | Stale `v2` names at run time | **Partly fixed:** log text `"v2 db error"` → `"sync db error"`. Kept on purpose: logger names `v2.ops.*` / `v2.ingest.*` (log filters and tests key on them) and the token `typ` `"v2_device"` (it is in issued tokens). `V2_ROOT` in probes and a stale package docstring: parked |
| M3 | Minor | `v2/README.md` still tracked | **Fixed** in `e01ff43` |
| M1 | Minor | `V2_DATABASE_URL` alone now puts the main app in DB mode | **Accepted** (the old name is an alias of `DATABASE_URL` by design); noted in the spec |
| M2 | Minor | For aliased settings, the new name in `.env` beats the old name in the process environment | **Accepted** ("new name wins", across sources); noted in the spec |
| M5 | Minor | The "agent ships no write code" guarantee is gone; `agent` may import all of `tally_bridge` | **Accepted:** matches the user's direction that writes go through the agent (merge tracker O1) |
| M10 | Minor | `pool_pre_ping` and `hide_parameters` now apply to every route | **Accepted:** spec M3 |
| M7 | Minor | No startup check that the database is at `006` when the sync routes are mounted | **Parked** (`open-items-parked.md`) |
| M8 | Minor | Probe `health_check` now uses the app's request and 90 s timeout (no caller found) | **Parked** |
| M9 | Minor | Login-limiter state on the module-level app can carry between e2e tests sharing an email | **Parked** |

## Checked and found sound (reviewer)

Tally request bytes (`post_xml`, `esc`, `wrap_import`, moved helpers); app code under `backend/agents`,
`backend/api/chat.py`, `backend/services` changed imports only; token decode keeps both families' responses; web and
device tokens rejected on each other's routes; startup rules for `DEVICE_TOKEN_SECRET`; shared login limiter keys and
limits; 19 sync routes with no collision; ownership checks unchanged; `SyncRoute` scoped to the sync routers; migration
fresh / adopt / refuse / rollback / downgrade; no weakened assertions in moved tests.

## Verification after the fix round

Full suites on 2026-10-01: **3209 passed / 606 skipped** without a DB (one flaky mock-server read error in
`tests/integration/test_vouchers.py`, passed on re-run) and **3806 passed / 10 skipped** with `TEST_DATABASE_URL`
(logs `logs/fixround-*.log`). The DB run first caught one test that still expected the pre-M6 behaviour
(`test_mid_batch_failure_leaves_zero_rows` expected the exception to escape the route); it now expects the 500
`internal_error` answer, and its rollback checks are unchanged and pass. Details in the merge tracker
(`plans/2026-09-30-v2-merge-tracker.md`, row 1.10). The
stricter adopt check was also run against throwaway copies of both old-chain databases on this machine
(`tallyagent`, `tallyagent_v2_review`): both adopted, 32 → 31 tables, row counts unchanged.

## Not reviewed / not run

`frontend/`, docs, `uv.lock`; most probe modules; the internals of `backend/sync/{ingest,parity}` (import and
setting-name changes only); multi-worker behaviour; live Tally, `tests/e2e_live`, eval, Playwright.
