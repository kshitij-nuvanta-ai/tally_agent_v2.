# v2 merge — implementation plan

Spec: [`specs/2026-09-30-v2-merge-design.md`](../specs/2026-09-30-v2-merge-design.md).
Status: [`2026-09-30-v2-merge-tracker.md`](2026-09-30-v2-merge-tracker.md).
Branch: `feat/merge-v2-into-backend`, cut from `feat/bi-s1-cloud` @ `99ced44`.

## Rules for every task

- Move files with `git mv` so history follows them.
- Each task ends with the suites it touches green, then one commit. Never `git add -A`; stage named paths.
- No behaviour change to chat, upload, write-to-Tally or the frontend.
- No paid runs: `ANTHROPIC_API_KEY=test-key`, mock Tally, mock Claude. `e2e_live` and eval are not run.
- DB tests use `TEST_DATABASE_URL=postgresql+asyncpg://nuvanta-mac-3@localhost/tallyagent_test` only.

## Tasks

| # | Task | Done when |
|---|---|---|
| T0 | Baselines on this machine: app no-DB, v2 no-DB, app DB suite, v2 DB suite. | Four counts recorded in the tracker. |
| T1 | **Packages move.** `v2/contract` → `contract/`, `v2/agent` → `agent/`, `v2/probes` → `probes/`, `v2/cloud` → `backend/` per spec §4, `v2/tests` → `tests/{sync,agent,contract,probes}/`. Rewrite `v2.*` imports. The sync app still builds through a temporary `create_app` so its tests run unchanged. | All former v2 tests pass from the root project; `v2/` holds only `pyproject.toml`, `uv.lock`, `README.md`. |
| T2 | **One project.** `db` extra → core dependencies; delete `v2/pyproject.toml`, `v2/uv.lock`; one `uv.lock`; move `v2/README.md` content into the root `README.md`. | `uv sync --extra dev --extra langfuse` installs everything; `v2/` is gone. |
| T3 | **One settings class** (M2). Sync fields into `backend/config.py` with `V2_*` aliases; sync code and tests use it; delete the sync config module. | Sync tests green; a test proves both `DEVICE_TOKEN_SECRET` and `V2_DEVICE_TOKEN_SECRET` are read. |
| T4 | **One Base + one migration chain** (M4, M5, M6). Sync models on `backend.db.models.Base`; delete `current.py`; write `006_sync_tables`; delete the v2 Alembic dir; sync test harness uses the single chain and the real `users`/`workspaces`. | Spec §5 three-row table covered by tests; metadata-vs-schema test green; both DB suites green in one run. |
| T5 | **One engine, one app** (M1, M3). Sync routes take sessions from `get_db`; routers, error handlers and limiters installed in `backend/main.py`; engine created only in the lifespan; temporary `create_app` removed; `purge` CLI at `python -m backend.sync`. | One server on 8000 serves every route; no import-time engine. |
| T6 | **One auth** (M7, M8). One token-decode function with two thin adapters (HTTPException / `ApiError`); `verify_password` from `backend/utils/auth.py`; delete `passwords.py`, `web_jwt.py`; shared login limiter. | Shared-limiter test green; sync auth tests green. |
| T7 | **One tally bridge** (M10, M11). `backend/tally_bridge` → `tally_bridge/`; fold in `agent/tally/*` per spec §6; one `sanitize_xml` / `detect_error`; probes use `tally_bridge.import_builder`. | No `# Copied from:` header left; bridge, agent, contract and probe tests green. |
| T8 | **Layer test** (M12). Replace `test_isolation.py` and `test_copied_headers.py` with `tests/test_layers.py`. | Test green; fails when a forbidden import is added (checked once by hand). |
| T9 | **Docs.** Part 1 spec "Changed" line; roadmap; `CLAUDE.md` build commands and layout; `.env.example`; parked items (contract model duplicates, review M-items). | Docs match the code. |
| T10 | **Verification** per spec §7, including the real-app pass on `tallyagent_fork` and the adopt path on a copy of `tallyagent_v2_review`. | Results in the tracker. |
| T11 | **Code review** → `docs/code-review-v2-merge-2026-09-30.md`; fix findings. | Review stored; suites re-run after fixes. |

## Order and why

T1 and T2 first: they are mechanical, and once every test runs from one project each later task can be checked against
the whole suite. T3–T6 then remove the duplicated plumbing one piece at a time. T7 is last among the code tasks
because it is the only one that touches code the chat path runs through.
