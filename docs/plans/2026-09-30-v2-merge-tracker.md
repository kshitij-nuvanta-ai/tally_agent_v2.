# v2 merge tracker — one DB, one backend, one tally bridge

Direction (senior dev, 2026-09-30): architecture becomes **installer → agent → tally bridge**; the cloud app talks only to
our Postgres DB; `v2/` merges back into the main backend; non-DB ("legacy") mode is cleaned up separately afterwards
(unit tests stay); the installer is to be defined in detail.

Status values: **Not started** · **In progress** · **Done**. Update this file in the same turn as the work, with proof
(commit hash or test output).

## Resume here

**Step 1 in progress on `feat/merge-v2-into-backend`.** Done: T0 baselines, T1 + T2 (commit `13d6bbd`), T3 + T4 (commit
`7d5d64e`), T5 + T6 (commit `12c36ba`). In progress: T7 (one tally bridge) + T8 (layer test). Next: T9 (docs), T10 (verification), T11 (code review). Commits are local;
pushing needs GitHub login (0.3).

## Status

| # | Task | Status | Proof |
|---|---|---|---|
| 0.1 | Explore v1, v2, docs and git state | Done | findings summarised below |
| 0.2 | Plan approved | Done | user: "we can start the merging v2 into our old code", 2026-09-30 (step 1 only) |
| 0.3 | GitHub login on this machine | Not started | `gh auth login` failed 2026-09-30; needed to push |
| 0.4 | Fork has all 4 branches (compare with old repo) | Not started | local clone has `master`, `dev`, `feat/bi-s0-probe-harness`, `feat/bi-s1-cloud` |
| **1** | **Merge v2 into backend** (`feat/merge-v2-into-backend`) | **In progress** | started 2026-09-30 |
| 1.0 | Merge spec + plan in `docs/`, old spec marked changed, roadmap updated | In progress | spec + plan committed in `13d6bbd`; old spec "Changed" line and roadmap still to do (plan T9) |
| 1.1 | Move files to target layout (`v2/` removed) | Done | commit `13d6bbd`; 3106 passed no-DB, 1719 + 1948 with DB (= baselines). `v2/` holds only `README.md` until the docs task |
| 1.2 | One app: routers in `backend/main.py`, port 8100 gone | Done | commit `12c36ba`; `tests/sync/unit/test_wiring.py`, `tests/sync/db/test_main_app.py` (19 sync routes + all existing routes on one app, no collisions) |
| 1.3 | One settings class | Done | commit `7d5d64e`; `tests/unit/test_config_sync.py` (52 tests: both env spellings, new name wins) |
| 1.4 | One auth (copies deleted, shared login limiter) | Done | commit `12c36ba`; `tests/sync/db/test_shared_login_limiter.py` (7 tests, both directions) |
| 1.5 | One DB engine + migration `006_sync_tables` | Done | one Base + migration `006` + one DB harness done in `7d5d64e` (`tests/sync/db/test_migration.py`, 21 tests; adopt path also run on a throwaway copy of the dev DB: 32 → 31 tables, only `alembic_version_v2` removed, all row counts equal). One engine: `12c36ba` |
| 1.6 | One tally bridge (top-level `tally_bridge/`) | In progress | plan T7 + T8 under way |
| 1.7 | One project, one test tree, new isolation test | In progress | one project + one test tree (`13d6bbd`); whole suite runs in one session with the DB (`7d5d64e`); new layer test is T8 |
| 1.8 | Parked items recorded in `docs/open-items-parked.md` | Not started | — |
| 1.9 | Verification: migrations (fresh + already-v2 DB), all suites, real-app pass | Not started | — |
| 1.10 | Code review stored in `docs/`, fixes applied | Not started | — |
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
| `v2/tests/` | `tests/sync/`, `tests/agent/`, `tests/contract/`, `tests/probes/` |
| `v2/pyproject.toml`, `v2/uv.lock`, `v2/README.md` | deleted; one root project |

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
| 2026-09-30 | Corrected: the plan is **not** approved yet. 0.2 and 1 set back to Not started; work paused until approval. App no-DB baseline measured on this machine: 1595 passed / 134 skipped. |
