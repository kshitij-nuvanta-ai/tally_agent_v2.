# v2 merge tracker — one DB, one backend, one tally bridge

Direction (senior dev, 2026-09-30): architecture becomes **installer → agent → tally bridge**; the cloud app talks only to
our Postgres DB; `v2/` merges back into the main backend; non-DB ("legacy") mode is cleaned up separately afterwards
(unit tests stay); the installer is to be defined in detail.

Status values: **Not started** · **In progress** · **Done**. Update this file in the same turn as the work, with proof
(commit hash or test output).

## Resume here

**State on 2026-10-01.** Step 1 (the merge) is built, reviewed, and the review fixes are verified by full suite
runs. Branch `feat/merge-v2-into-backend`, six local commits, **nothing pushed**.

| Commit | What |
|---|---|
| `13d6bbd` | T1 + T2: `v2/` moved into the main project, one uv project |
| `7d5d64e` | T3 + T4: one settings class, one `Base`, migration `006`, one DB test harness |
| `12c36ba` | T5 + T6: one app, one engine, one auth, shared login limiter |
| `23c6401` | T7 + T8: one `tally_bridge/`, `tests/test_layers.py` |
| `e01ff43` | T9 + T10: docs, real-app pass recorded |
| the commit after `e01ff43` (`fix(merge): code review fix round …`) | T11: review fix round + review write-up |

**Committed 2026-10-01 with the user's go-ahead:** the fix round for review
findings I1, I2, I3, M6 and the M4 log text, plus the review write-up (`docs/code-review-v2-merge-2026-09-30.md`),
spec §7a, the parked review minors and this tracker. Verification on 2026-10-01, Mac kept awake with `caffeinate -i`:
- full suite without a DB: **3209 passed / 606 skipped / 1 failed** (`logs/fixround-nodb-full.log`). The failure,
  `tests/integration/test_vouchers.py::test_ledger_vouchers_with_ampersand_in_name`, was an `httpx.ReadError` from
  the mock Tally server; the file passed 5/5 re-runs and `tests/integration` 166/166. No fix-round change touches
  `tally_bridge/`, the mock server or those tests — recorded as a flaky run.
- full suite with `TEST_DATABASE_URL`, first run: 3805 passed / 1 failed (`logs/fixround-db-full.log`):
  `tests/sync/db/test_ingest_api.py::test_mid_batch_failure_leaves_zero_rows` still expected the exception to
  propagate, which M6 changed on purpose. The test now expects the 500 `internal_error` answer; its rollback checks
  (zero rows, zero `sync_batches`, `sync_workspaces` untouched, retry succeeds) are unchanged and pass. No other test
  expects an exception out of a sync route.
- full suite with `TEST_DATABASE_URL` after that test update: **3806 passed / 10 skipped / 0 failed**
  (`logs/fixround-db-full-2.log`). `tallyagent_test` left as found (`alembic_version` only).
- The overnight stalls did not recur with the Mac awake (runs took 4 m 45 s and 10 m 21 s): they were the machine
  sleeping (`sleep 1` in `pmset -g`).
- Earlier checks still stand: the stricter adopt check on throwaway copies of `tallyagent` and `tallyagent_v2_review`
  (both adopted, 32 → 31 tables, row counts unchanged); `tallyagent_fork` untouched (still `006`, 31 tables).

**Next steps, in order:**
1. User reviews `tallyagent_fork` in TablePlus / DBeaver (connection details under "Database for review").
2. User runs `gh auth login` (0.3); compare the fork's branches with the old repo (0.4); push
   `feat/merge-v2-into-backend`. Where it merges (`dev` / `master`) is decided after that.
3. Proposed next (not approved): a small `tally_bridge` cleanup PR after the push — company-list duplicates, a guard
   test against new duplicate names, rename of the float parsers (see `docs/open-items-parked.md`).
4. Steps 2–4 of this tracker and the open questions stay parked until the user picks them up.

## Status

| # | Task | Status | Proof |
|---|---|---|---|
| 0.1 | Explore v1, v2, docs and git state | Done | findings summarised below |
| 0.2 | Plan approved | Done | user: "we can start the merging v2 into our old code", 2026-09-30 (step 1 only) |
| 0.3 | GitHub login on this machine | Not started | `gh auth login` failed 2026-09-30; needed to push |
| 0.4 | Fork has all 4 branches (compare with old repo) | Not started | local clone has `master`, `dev`, `feat/bi-s0-probe-harness`, `feat/bi-s1-cloud` |
| **1** | **Merge v2 into backend** (`feat/merge-v2-into-backend`) | **In progress** | started 2026-09-30 |
| 1.0 | Merge spec + plan in `docs/`, old spec marked changed, roadmap updated | Done | spec + plan `13d6bbd`; "Changed 2026-09-30" blocks in both BI specs, roadmap Set C, README, `CLAUDE.md`, BI tracker (docs commit) |
| 1.1 | Move files to target layout (`v2/` removed) | Done | commit `13d6bbd`; 3106 passed no-DB, 1719 + 1948 with DB (= baselines). `v2/` holds only `README.md` until the docs task |
| 1.2 | One app: routers in `backend/main.py`, port 8100 gone | Done | commit `12c36ba`; `tests/sync/unit/test_wiring.py`, `tests/sync/db/test_main_app.py` (19 sync routes + all existing routes on one app, no collisions) |
| 1.3 | One settings class | Done | commit `7d5d64e`; `tests/unit/test_config_sync.py` (52 tests: both env spellings, new name wins) |
| 1.4 | One auth (copies deleted, shared login limiter) | Done | commit `12c36ba`; `tests/sync/db/test_shared_login_limiter.py` (7 tests, both directions) |
| 1.5 | One DB engine + migration `006_sync_tables` | Done | one Base + migration `006` + one DB harness done in `7d5d64e` (`tests/sync/db/test_migration.py`, 21 tests; adopt path also run on a throwaway copy of the dev DB: 32 → 31 tables, only `alembic_version_v2` removed, all row counts equal). One engine: `12c36ba` |
| 1.6 | One tally bridge (top-level `tally_bridge/`) | Done | commit `23c6401`; `request_builder.py` byte-identical to before; 1,129 recorded request-builder calls unchanged; no `Copied from` header left |
| 1.7 | One project, one test tree, new isolation test | Done | one project + one test tree (`13d6bbd`); whole suite in one session with the DB (`7d5d64e`); `tests/test_layers.py` (`23c6401`, 11 tests; hand-checked that a forbidden import fails it) |
| 1.8 | Parked items recorded in `docs/open-items-parked.md` | Done | section "From the v2 merge (2026-09-30)", six items |
| 1.9 | Verification: migrations (fresh + already-v2 DB), all suites, real-app pass | Done (awaiting the user's review of the DB) | Migrations: `tallyagent_fork` adopted 005 + v2_001 → `006` (32 → 31 tables); fresh throwaway DB → `006`; schemas identical (`pg_dump -s`). Suites at `23c6401`: 3186 passed no-DB, 3770 passed with DB. Real-app pass on port 8000 against `tallyagent_fork`: web flows, agent login → bind → full first sync (14 batches, 65 masters, 240 vouchers) → parity `ok` (0 mismatches), sync-status, web command delivered on heartbeat, token separation, shared login limiter, restart round-trip — all as expected; logs `logs/be_merge_verify*.log`. Frontend vitest 381 passed. Not run: chat success path (no Claude key), Playwright, `e2e_live`, eval, live Tally |
| 1.10 | Code review stored in `docs/`, fixes applied | Done | committed 2026-10-01 (`fix(merge): code review fix round …`); review: 0 Critical, 3 Important, 10 Minor (`docs/code-review-v2-merge-2026-09-30.md`). Fixes for I1, I2, I3, M6 (+ M4 log text). Full suites 2026-10-01: 3209 passed / 606 skipped no-DB (1 flaky mock-server read error, passed on re-run); 3806 passed / 10 skipped with DB (`logs/fixround-*.log`) |
| **2** | **Remove legacy mode** (`chore/remove-legacy-mode`) | **Not started** | — |
| 2.1 | Backend legacy paths deleted, `DATABASE_URL` required | Not started | — |
| 2.2 | Frontend legacy app deleted | Not started | — |
| 2.3 | Legacy-forced tests converted or deleted; bridge unit tests kept | Not started | — |
| 2.4 | Auth on `/api/reports`, one ownership helper, dead config removed | Not started | — |
| 2.5 | README / `CLAUDE.md` / roadmap updated | Not started | — |
| **3** | **Chat and reports read from synced tables** | **Not started** (needs spec) | — |
| **4** | **Agent + installer; bridge leaves backend; write path** | **Not started** (needs spec + senior dev answers) | — |

## Assumptions in force (raise with the senior dev if wrong)

1. **Decided by the user 2026-09-30 (option a):** the 21 v2 tables stay as they are, in the existing database, and
   join the main migration chain. `sync_workspaces` is not folded into `workspaces`. After the migration work is
   done, the user reviews the result in a database viewer before it is accepted.
2. The backend keeps its direct Tally access until the agent ships (step 4); the agent is only a read library today,
   so nothing can fill the synced tables yet.
3. "Write to Tally" is unchanged in steps 1–2; its route through the agent is decided in the agent/installer spec.
4. The workspace demo/mock setting is unchanged; only the legacy header toggle goes, in step 2.

## Target layout after step 1

| From | To |
|---|---|
| `v2/cloud/models/*` | `backend/db/sync_models/`, on the same `Base` as `backend/db/models.py` |
| `v2/cloud/models/current.py` | deleted; use `User`, `Workspace` from `backend/db/models.py` |
| `v2/cloud/api/{agent_auth,devices,sync,web_sync}.py` | `backend/api/` (`web_sync.py` → `workspace_sync.py`) |
| `v2/cloud/{sync,ingest,parity}/`, `clock.py`, `errors.py` | `backend/sync/` |
| `v2/cloud/auth/{device_tokens,rate_limit}.py` | `backend/utils/` |
| `v2/cloud/auth/{passwords,web_jwt}.py` | deleted; use `backend/utils/auth.py` and `get_current_user` |
| `v2/cloud/config.py` | merged into `backend/config.py` |
| `v2/cloud/db.py` | deleted; use `backend/db/engine.py` |
| `v2/cloud/main.py`, `cli.py`, `__main__.py` | routers in `backend/main.py`; `purge` CLI in `backend/sync/cli.py` |
| `v2/cloud/alembic/` | `backend/db/migrations/versions/006_sync_tables.py` |
| `v2/contract/` | top-level `contract/` |
| `v2/agent/` | top-level `agent/` |
| `v2/agent/tally/` + `backend/tally_bridge/` | one top-level `tally_bridge/` |
| `v2/probes/` | top-level `probes/` |
| `v2/tests/` | `tests/sync/`, `tests/tally_bridge/`, `tests/contract/`, `tests/probes/` |
| `v2/pyproject.toml`, `v2/uv.lock`, `v2/README.md` | deleted; one root project |

## Database for review

| Field | Value |
|---|---|
| Host / port | `localhost` / `5432` |
| User / password | `nuvanta-mac-3` / (empty) |
| Migrated database | `tallyagent_fork` — revision `006`, 31 tables, two sample synced workspaces |
| Old database, untouched, to compare | `tallyagent` — revision `005` + `alembic_version_v2`, 32 tables |

App login for the sample data: `merge-verify-20260930-194055@example.com` / `Merge-Verify-12345!`. The servers are
stopped; start with `PYTHONPATH=. uv run uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000` and
`cd frontend && npm run dev`.

## Code review (2026-09-30, range `99ced44..23c6401`)

No Critical findings; no regression found for chat, upload, write-to-Tally, auth, workspaces or the sync API.

| ID | Finding | Status |
|---|---|---|
| I1 | Migration `006` adopt path only checks table names, so a hand-altered sync table would be accepted | Fixed (uncommitted): compares column names, types, nullability; refuses on a difference. Checked on copies of both old-chain DBs |
| I2 | Sync tables present but no `alembic_version_v2` → raw `DuplicateTableError` | Fixed (uncommitted): refuses with a clear message |
| I3 | `alembic upgrade head` ignores `.env` and falls back to `alembic.ini`'s hard-coded `tallyagent`, so it can migrate a different database than the app uses (pre-existing; bit this session once) | Fixed (uncommitted): `backend/db/migration_url.py` — the app's own database URL; fails if none is set; `alembic.ini` has no URL |
| M6 | An unexpected non-DB error on a sync route answers in neither the old shape nor the sync contract | Fixed (uncommitted): `{"error": "internal_error", "detail": ""}` |
| M1 | Only `V2_DATABASE_URL` set now puts the main app in DB mode | Accepted; noted in merge spec §7a |
| M2 | New name in `.env` beats old name in the real environment | Accepted ("new name wins"); noted in merge spec §7a |
| M3 | `v2/README.md` still tracked | Fixed in `e01ff43` |
| M4 | Stale `v2` names in log text / logger names / `V2_ROOT` | Log text fix in flight; logger names `v2.ops.*` and token `typ` `v2_device` kept on purpose; rest to park |
| M5 | "Agent ships no write code" guarantee is gone | Accepted — matches the user's direction that writes go through the agent (O1) |
| M7 | No startup check that the DB is at `006` when sync routes are mounted | Parked in `open-items-parked.md` |
| M8 | Probe `health_check` now uses the app's request and 90 s timeout (no caller found) | Parked in `open-items-parked.md` |
| M9 | Login-limiter state on the module-level app can carry between e2e tests | Parked in `open-items-parked.md` |
| M10 | `pool_pre_ping` and `hide_parameters` now apply to every route | Accepted (spec M3) |

## Installer and agent (step 4) — what is decided, recommended and open

Source for "decided": `docs/specs/2026-09-21-bi-part1-sync-design.md` (decision 2, §5 "Windows agent", §7, R17, R18).

### Already decided in the spec

| Topic | Decision |
|---|---|
| Build | Python, PyInstaller `--onedir`; built on a Windows CI job (PyInstaller cannot cross-compile) |
| Signing | Signed installer, Microsoft Artifact Signing, consistent publisher name |
| Minimum OS | Windows 10 |
| How it runs | Windows service + tray app, talking over localhost / named pipe |
| Uninstall | Revokes the device token on the server; "Remove device" in Settings does the same |
| Local data | SQLite: company, cursors, outbox of unsent batches (deleted once the server acks) |
| Network | Uses the Windows system proxy |
| Auto-update | Silent: version check, signed download, sha256 check, self-test, rollback on failure, staged rollout |

### Recommendations — NOT yet confirmed by the senior dev

Status of every row: **Proposed 2026-09-30, awaiting confirmation.**

| # | Open point | Recommendation | Why |
|---|---|---|---|
| I1 | Installer packaging tool | **Inno Setup** | Free, one script file, commonly paired with PyInstaller folder builds; registers the service, runs fully silent (needed for auto-update), signs the output. MSI only if customers deploy through corporate IT tools. |
| I2 | Update hosting and version | **Backend announces the version on the heartbeat reply; the installer file sits in cloud storage** | The agent already sends its version on every heartbeat and commands already ride on the reply, so this gives per-device staged rollout with no separate update server. Agent version comes from one version file written into the build. |
| I3 | Install folder | **Program in `C:\Program Files\<Company>\<Agent>\`; data in `C:\ProgramData\<Company>\<Agent>\`** | Ordinary users must not be able to replace the files of a service that runs with high privileges. Data folder restricted to the service and administrators. |
| I4 | Admin rights (Q31) | **Admin once, service only, for the first version** | The elevated service is what lets updates install silently. The agent core already has to run as a plain foreground program (Mac development), so a per-user mode is cheap to add if a customer is blocked. |
| I5 | Windows test machine (Q29) | **Cloud Windows 10 VM first (Azure has Windows 10 images); a physical PC later** | Install / uninstall / update / rollback tests need a machine that resets to a clean snapshot. A physical PC is still needed for sleep/resume and for the speed measurements. Correctness checks need a licensed TallyPrime, not Educational mode. |
| I6 | Where the company is picked (Q11) | **Inside the agent, as already built** | Only the agent can see which companies are open in Tally, and the cloud side is built and tested for this. The website shows the linked company and handles re-linking. |

### Still open — needs the senior dev

| # | Question | Note |
|---|---|---|
| O1 | How "Write to Tally" reaches Tally once the backend has no bridge | **Direction given by the user 2026-09-30:** write to our DB first, then update Tally; if the two conflict, show the user both and let them choose which to keep. Consequences to design in the agent spec: the agent needs write code (reverses the spec rule "the agent ships with no write code"); a "pending / written / conflict" state per voucher; what counts as a conflict; what "keep ours" and "keep Tally's" each do. **Details still open — see O1a–O1c.** |
| O1a | Which table holds the voucher before Tally accepts it | Proposal: `voucher_entries` with a pending status, not the synced `tally_vouchers` table — that table mirrors Tally and the correctness checks compare it with Tally, so a row Tally does not have yet would show as a mismatch. |
| O1b | What counts as a conflict | Candidates: Tally rejects the write; the invoice already exists in Tally; a ledger or stock item was renamed or deleted in Tally; the voucher was edited in Tally after we wrote it; the agent is offline. |
| O1c | What each choice does | Proposal: "keep ours" = the agent alters the voucher in Tally to match our DB; "keep Tally's" = our pending version is discarded and the next sync brings Tally's version in. |
| O2 | Token storage between service and tray | The spec stores the token with Windows DPAPI, which is tied to one Windows user; the service and the tray run as different users. Proposal: the service owns the token, the tray asks the service. |
| O3 | Workspace creation at company pick | As built, the agent can only attach to a workspace that already exists (S1 decision D5). If the pick is in the agent, should the agent create the workspace then? |
| O4 | What chat shows for a workspace that has never synced | — |
| O5 | Does the "connect to Tally host and port" screen go away, and in which step | Follows from "sync mode is default". |
| O6 | CI | The repo has no `.github/`; a Windows build job is needed before the first installer. |
| O7 | Who writes the agent/installer spec | No spec exists for this stage yet. |

## Baselines before the merge (measured on this machine 2026-09-30, logs in `logs/baseline-*.log`)

| Suite | Result |
|---|---|
| App, no DB | 1595 passed / 134 skipped |
| App, with `TEST_DATABASE_URL` | 1719 passed / 10 skipped |
| v2, no DB | 1511 passed / 437 skipped |
| v2, with DB | 1948 passed |
| **Target after the merge, no DB** | **3106 passed** |
| **Target after the merge, with DB** | **3667 passed** |

## Change log

| Date | Change |
|---|---|
| 2026-09-30 | Tracker created; branch `feat/merge-v2-into-backend` cut from `feat/bi-s1-cloud` @ `99ced44`; spec and plan drafted. |
| 2026-09-30 | User decided option (a) for the database: keep `sync_workspaces` as its own table; one migration chain. User will review the migrated database in a viewer afterwards. Assumptions 2–4 still unconfirmed. |
| 2026-09-30 | Added the installer section: what the BI sync spec already decides, six recommendations (I1–I6, proposed, not confirmed by the senior dev) and the questions still open (O1–O7). |
| 2026-09-30 | User gave the write-path direction (O1): DB first, then Tally, user chooses on conflict. Sub-questions O1a–O1c added. |
| 2026-09-30 | **User approved starting step 1 (the merge).** Steps 2–4 and the open questions stay parked. Backend keeps its direct Tally access during the merge (no behaviour change). |
| 2026-09-30 | User: when done, push to the feature branch `feat/merge-v2-into-backend` on the fork; where it merges (`dev` / `master`) is decided later. Needs GitHub login on this machine (0.3). |
| 2026-09-30 | Baselines measured (T0 done): app 1595 / 1719 with DB, v2 1511 / 1948 with DB. T1 + T2 (move packages, one project) started. |
| 2026-09-30 | **T1 + T2 done, commit `13d6bbd`:** `v2/` moved into the main project, one uv project and lockfile; all suites equal the baselines. Temporary modules left under `backend/sync/` (`config.py`, `db.py`, `app.py`, `alembic/`, `passwords.py`, `web_jwt.py`) and `backend/db/sync_models/{base,current}.py`. T3 + T4 started. |
| 2026-09-30 | **T3 + T4 done, commit `7d5d64e`:** one settings class (`V2_*` names accepted as aliases), sync models on the one `Base`, migration `006_sync_tables` (create / adopt / refuse), old Alembic chain removed, one DB test harness. Suites: 3167 passed no-DB, 3740 passed with DB in one session (+73 new tests). Finding: the 9 app tables' models differ from their own migrations in 27 catalog details (pre-existing, pinned in the migration test, not changed). Decision taken: if `DEVICE_TOKEN_SECRET` is unset the app starts and agent sync routes are simply not mounted; set-but-invalid fails startup. T5 + T6 started. |
| 2026-09-30 | **T5 + T6 done, commit `12c36ba`:** one app (port 8100 and `/api/v2/health` removed), one engine, one token decode, one shared login limiter. Suites: 3183 passed no-DB, 3767 passed with DB. Notes: web login now follows `LOGIN_RATE_MAX` / `LOGIN_RATE_WINDOW_S` if an operator sets them (defaults = the old 5 / 900 s); an unexpected non-DB error on a sync route now returns the app's JSON 500 instead of plain text. T7 + T8 started. |
| 2026-09-30 | **T7 + T8 done, commit `23c6401`:** one top-level `tally_bridge/` (agent copy folded in), `tests/test_layers.py` replaces the isolation and copied-header tests. Suites: 3186 passed no-DB, 3770 passed with DB. Findings: one real layer violation fixed (`tally_bridge` imported `backend.utils.date_utils`; FY helpers moved to `tally_bridge/dates.py`); two `parse_company_list` and two `build_company_list` kept because they differ (parked). `tallyagent_fork` migrated to `006` for the user's review. T9 (docs), T10 (real-app pass) and T11 (code review) started. |
| 2026-09-30 | **T9 (docs) and T10 (real-app pass) done.** Real-app pass found no merge regression. Pre-existing limitation noticed: `POST /api/chat` with no `ANTHROPIC_API_KEY` returns a generic 500 (server stays up). `tallyagent_fork` now holds two sample synced workspaces (users `merge-verify-20260930-194055@example.com` and `…-194012@example.com`) for the user's review. Code review (T11) still running. |
| 2026-10-01 | Fix worker cut off by an API overload; main agent reviewed its diff, ran the new tests (24 passed) and part of the suite (395 passed), checked the stricter adopt path on copies of both old-chain DBs, wrote the review doc, spec §7a and parked items. Full-suite runs stalled while the Mac slept — still owed. Nothing new committed. |
| 2026-10-01 | **Fix round verified.** Full suites with the Mac awake: 3209 passed no-DB (one flaky mock-server read error, passed on re-run); with DB the first run caught one stale test (`test_mid_batch_failure_leaves_zero_rows` expected the pre-M6 exception), updated to expect the 500 answer with its rollback checks unchanged; re-run 3806 passed / 0 failed. The overnight stalls were the Mac sleeping. Committed on the user's go-ahead (`fix(merge): code review fix round …`). |
| 2026-09-30 | **Code review (T11) done:** 0 Critical, 3 Important (all on migrations), 10 Minor — table under "Code review". Fix round for I1, I2, I3, M6 started; still running and uncommitted at end of day. "Resume here" rewritten for tomorrow; "Database for review" section added. |
| 2026-09-30 | Corrected: the plan is **not** approved yet. 0.2 and 1 set back to Not started; work paused until approval. App no-DB baseline measured on this machine: 1595 passed / 134 skipped. |
