# S1 — Cloud (sync tables, device auth, ingest, parity) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Tick each box in this file as soon as that step is verified**, not at the end. After each task, the controller
> updates the tracker (`docs/plans/2026-09-22-bi-part1-tracker.md` §4 S1 rows) in the same turn (CLAUDE.md "Always
> update the tracker"). Implementers never edit the tracker, roadmap or spec themselves; they report, the controller
> writes.

**Goal:** Build the cloud half of BI Part 1 sync as a separate v2 FastAPI app (`v2/cloud/`, port 8100) plus a shared
wire package (`v2/contract/`): device auth with one active device per workspace, binding, heartbeat/state/commands,
runs + cursors + FY coverage, the full ingest pipeline with rung 0 and quarantine, reconcile + snapshots, the parity
engine (rungs 1–2, C47 forex, classifier, ladder, bisect), lazy maintenance + `purge`, all proven offline on the S0
captures.

**Architecture:** `v2/contract` owns the wire format: Tally's own field text, unchanged (D1), plus strict parsers and
the Tally-XML → wire transcoder. `v2/cloud` is a FastAPI app with its own settings (`V2_*`), its own async SQLAlchemy
engine and its own Alembic chain (`alembic_version_v2`, migration `v2_001`). It reads the current app's `users` /
`workspaces` tables and never writes them. The business logic sits in pure functions: parse, derive, rung 0/1/2,
forex, classifier, ladder, coverage. They are tested without a DB, and thin async services wire them to Postgres.
Every test is offline (tier A). DB tests need `TEST_DATABASE_URL` and skip without it.

**Tech Stack:** Python ≥ 3.12, FastAPI, pydantic v2 + pydantic-settings, SQLAlchemy 2 async + asyncpg, Alembic,
PyJWT, passlib[bcrypt] (bcrypt < 5), httpx (`ASGITransport` for API tests), pytest + pytest-asyncio
(`asyncio_mode = "auto"`), uv (`--project v2`).

**Spec:** [`docs/specs/2026-09-25-bi-s1-cloud-design.md`](../specs/2026-09-25-bi-s1-cloud-design.md). Read all of
it before any task. The sections each task argues from are named in its header. Parent:
[`2026-09-21-bi-part1-sync-design.md`](../specs/2026-09-21-bi-part1-sync-design.md) (§4, §5 "Code isolation (v2)",
§6, §13). Tally facts: [`LESSONS.md`](../../LESSONS.md) §15 rules 17–30. S0 exit gate:
[`docs/bi-s0-exit-gate-2026-09-25.md`](../bi-s0-exit-gate-2026-09-25.md) (exceptions (d), (f), (g)).
**Tracker:** [`2026-09-22-bi-part1-tracker.md`](2026-09-22-bi-part1-tracker.md) §4 (rows S1.0–S1.26).

## Global Constraints

- **Isolation:** the S1 `git diff --name-only` touches only `v2/` and `docs/`. Nothing under `v2/` imports
  `backend`, `scripts` or `tests`. `v2.cloud` never imports `v2.agent` or `v2.probes`. `v2.agent` never imports
  `v2.cloud`. `v2.contract` imports neither `v2.cloud` nor `v2.agent` nor `v2.probes`. Anything copied from current
  code starts with `# Copied from: <path> @ <short sha>`.
- **Current tables:** only `CREATE TABLE` / `CREATE INDEX` of **new** tables. `users` / `workspaces` are read only
  (login, ownership, `is_deleted`). FKs to them are allowed. v2 never inserts into `workspaces` (D5).
- **Own Alembic chain:** `v2/cloud/alembic/`, version table `alembic_version_v2`, first revision `v2_001`. The
  current `alembic_version` table is never read or written.
- **Money:** `Decimal` end to end. Columns: money `Numeric(18,2)`, forex face `Numeric(18,4)`, rate
  `Numeric(18,6)`, quantity `Numeric(18,4)`. Never `Float`. A parse failure is an error, **never zero**. `""` →
  `None`. A missing key stays absent (Part 1 §13).
- **Sign:** debit negative, credit positive (Tally's export convention), everywhere.
- **Forex (D3):** a forex expression's money column holds the **stated INR base after `=`**. Face, currency and rate
  go in their own nullable columns. **No face × rate computation** anywhere in `v2/contract` or `v2/cloud` (the
  only multiplication is §10.5's `self_check`).
- **Posting rule:** `ledger_entries` = `ALLLEDGERENTRIES.LIST` only (LESSONS rule 18). A wire voucher with a
  `ledgerentries_list` key is rejected `duplicate_posting_list`.
- **Reserved-name clean-up (D31):** a leading U+0004 and the spaces after it are stripped from every text field
  before resolution.
- **Tolerance (Q19):** `V2_PARITY_TOLERANCE_PAISE=100` (₹1.00), inclusive (`|diff| <= 1.00` matches). Forex face is
  compared to `0.01`.
- **Retention:** parity runs 90 d, matching lines 7 d, mismatching lines 90 d (Q21). `raw` kept for the newest 2
  FYs only (Q22). Batch log 90 d.
- **Limits:** gzip body ≤ 5 MB (`V2_INGEST_MAX_GZIP_BYTES`), decompressed ≤ 50 MB, ≤ 500 objects
  (`V2_INGEST_MAX_OBJECTS`). Access token 15 min. Refresh token 90 days, sliding, rotated on every refresh. Take-over
  needs a login ≤ 10 min old.
- **No business data in logs:** request bodies are never logged. The ops signal `v2.ops.integrity` carries counts,
  causes and diff buckets only: no names, no GUIDs, no exact amounts.
- **App:** port `8100`, settings prefix `V2_`, same Postgres as the current app. No scheduler: everything is
  request-driven, and maintenance runs in bounded heartbeat slices (≤ 2 s / 5,000 rows).
- **Tests:** offline only. DB tests use `TEST_DATABASE_URL` (a separate test DB, never read from `.env`, never the
  dev DB). On this Mac: `TEST_DATABASE_URL=postgresql+asyncpg://nuvanta-mac-3@localhost/tallyagent_test`. Real
  captures come first; FakeBooks only where a capture doesn't exist, and every such test says so in its docstring.

## Review Focus

These are the inputs the spec implies but no build-order item tests directly. Each one has a test pinned in the task
named.

1. **A capture set whose fields are split across files.** The real captures don't carry every required key in one
   file. Company A's voucher files (`p06`, `p16_A_vouchers_fy`, `p18_A_vouchers_fy`) have **no `ALTERID`**, and
   `p06` has no `ISCANCELLED`/`ISOPTIONAL`/`ISPOSTDATED`. Many master lists (`p25_A_groups`, `p18_B_ledger_list`,
   `p22_B_currencies`, `p25_*_voucher_types`, `p22_B_usd_ledger`) have **no GUID/AlterID**. A reasonable person
   expects the real-data tests to *join* captures by GUID/name and never invent a value silently. Pinned: Task 12
   `test_assembler_never_fills_a_value_no_capture_holds` and
   `test_assembler_marks_synthetic_identities_by_gap_id`.
2. **The shared test DB.** The current app's DB suite runs `Base.metadata.drop_all` on the same `tallyagent_test`.
   v2 tables that keep FKs to `users` would make that `DROP` fail. The expectation: running the v2 DB tests leaves
   the test DB exactly as it found it. Pinned: Task 3 `test_session_teardown_leaves_no_v2_objects`, and Task 15
   runs the current DB suite after the v2 one.
3. **A gzip body that is small on the wire but huge inflated** (zip bomb), and a body with no `Content-Encoding`.
   Expected: 413 without allocating 50 MB+, and a plain-JSON body accepted. Pinned: Task 8c
   `test_zip_bomb_is_413_before_full_inflate` and `test_uncompressed_json_body_is_accepted`.
4. **A time-zone edge on the FY boundary.** "Current FY" is decided by the IST date. A heartbeat at 2026-03-31
   20:00 UTC is already 1 April in IST. Expected: the new FY's coverage and raw-window rules follow IST, not UTC.
   Pinned: Task 5 `test_current_fy_uses_ist_date_at_utc_evening_of_31_march` and Task 11
   `test_raw_window_follows_ist_fy`.
5. **The same batch arriving twice at once** (an agent retry racing the first request). Expected: one stored set of
   rows. The loser gets the replayed response, or a clean 409 `batch_id_reused` if the body differs, never a 500 or a
   double insert. Pinned: Task 8c `test_concurrent_identical_batches_store_once`.

---

## Spec ambiguities resolved in this plan (for the controller to confirm or reverse)

| # | Where | Ambiguity | Resolution taken here |
|---|---|---|---|
| A1 | §13.2, §6.2 | Spec says tests may import FakeBooks from `v2.probes.setup`. FakeBooks actually lives in `v2/tests/probes/fake_books.py`, and company B is loaded by `v2.probes.setup.company_b.load_company_b`. | Tests import `v2.tests.probes.fake_books.FakeBooks` + `v2.probes.setup.company_b.load_company_b` (the pattern in `v2/tests/probes/test_company_b.py::_empty_b`). Only tests do this. |
| A2 | §16 "DB integration", CLAUDE.md | `tallyagent_test` is shared with the current app's DB suite, whose teardown is `Base.metadata.drop_all`. v2 FKs into `users` would break that drop. | The v2 session fixture creates stand-in `users`/`workspaces` only if absent (and remembers it did). At session end it runs `alembic downgrade base` on the v2 chain, drops the `alembic_version_v2` table, drops stand-ins it created, and deletes the test rows it inserted (email domain `@v2test.invalid`). Never run the two suites concurrently. |
| A3 | §5.2 | Spec says to copy the forex grammar from `v2/probes/reads.py`. Commit `5abb6a1` has since built the stated-base grammar in `v2/agent/tally/amounts.py`, and it is closer to D3. It differs in one rule: with no `= base`, the agent derives face × rate, but D3 says no computation. | `v2/contract/parse.py` copies `_FOREX`/`parse_forex` from `v2/agent/tally/amounts.py` (header names it), changed so that no stated base → `Amount(inr=None, stated=False)`, never a computed base. |
| A4 | §13.1 vs real files | Company A voucher captures lack `ALTERID` (all three) and `p06` lacks the flags. Several master lists lack GUID/AlterID (the G4 gap is wider than §5.3 says: B **groups** and B **ledger list** too). | Task 12's fixture assembler joins captures by GUID/name: `p06` lines + `p04_A_voucher_full` AlterID + `p16_A_vouchers_fy` flags; `p04_A_*_full` identities + `p25_A`/`p16_A` fields. Where no capture holds an identity, the assembler reads the Task 0 `s1_*` G4 capture. If that is missing, it assigns a deterministic synthetic identity (`<company guid>-fx-<kind>-<sha8(name)>`, alterid `1`), and the test id carries `synthetic:G4`. Values (amounts, parents, flags) are **never** synthesised. |
| A5 | §10.2/D9 on company B | B's books start 01-04-2022, so the B 31-03-2023 parity test needs a ledger-level TB as-on books_from. No such capture exists, and G1–G5 don't include it. | B's books-start anchor comes from `s1_B_tb_ledgerwise_asof_2022-04-01.xml` if Task 0 captured it (recommended **G6**). Otherwise it comes from `company_b_data.generate()` ledger openings (the values setup-b wrote and `_verify` read back), with `anchor_source="dataset"` in the test name. Listed as a gap in the Task 15 report. |
| A6 | §7.5 table | "First match wins", but `company_bound_elsewhere` is listed last. Taken literally, an unbound workspace would bind to a GUID already bound to the user's other workspace. | Order: 404/410 → `company_bound_elsewhere` → the rest as listed. |
| A7 | §7.9 | The shape of `quarantine: [...]` entries is not given. | `{"kind", "guid", "code", "voucher_date"?}`. The object is **omitted** from `objects`. A code not in the deterministic set → 422 `batch_rejected` with per-entry code `quarantine_code_not_allowed`. |
| A8 | §8.2 | "Fatal code" is undefined. `V2_QUARANTINE_ERROR_THRESHOLD` has no default. | `FATAL_RUN_CODES = {"company_mismatch", "unrecoverable"}`. Threshold default `50`. |
| A9 | §7.1, D24 | Per-device rate limit numbers are not given. | Login: copied `_check_rate_limit` semantics (5 per 15 min per email). Device: 600 requests / 60 s per device, in-process sliding window. `Retry-After` = seconds until the oldest hit leaves the window. |
| A10 | §15.4 | The "24 cells" are not written out. | Task 7 writes the full table (`COVERAGE_MATRIX`) and the test enumerates it. `pending` + replay is unreachable, so it is treated as a first ack → `running`. |
| A11 | §7.10 `add_fy` | Whether the new FY row is `complete` at once. | As the spec says: `complete` with `months_total = 0`, `months_done = []`. |
| A12 | §9.4 | The web JWT secret name. | `V2_WEB_JWT_SECRET`, falling back to `JWT_SECRET` (the same value the current app signs with). |
| A13 | §16 "API integration" | "A fresh app instance" needs an app factory and an injectable clock. | `v2.cloud.main.create_app(settings, clock)`; `v2.cloud.clock.SystemClock` / `FixedClock`. |
| A14 | §16 | The spec says "the existing v2 suite (917)". The actual count on 2026-09-25 is **973 passed** (`uv run --project v2 pytest v2/tests -q`, 239 s). | Pre-flight re-counts it. The baseline is whatever pre-flight measures. |
| A15 | §11 | A few refusals the plan needs have no code in §11. | New codes, all 409 unless noted: `run_kind_not_allowed` (a `first_sync` outside `awaiting_first_connection`/`first_sync`), `relink_not_prompted` (relink without a matching `relink_prompt`), `quarantine_code_not_allowed` (A7, a per-object code inside a 422 `batch_rejected`). The controller adds them to spec §11 when it next edits the spec. |
| A16 | §4.9 Q5 | The 30-day purge grace has no timestamp: the current app's soft-delete sets only `is_deleted`. | The grace is measured from `workspaces.updated_at` of the deleted row (read-only). The test docstring records this. |
| A17 | §14 scenario 3 | It says "24 months acked", which is true only when the first sync starts in March. | The test computes `months_total` from `fy_rows_for_bind` for the fixed clock (18 on 2026-09-25) and asserts that number. |
| A18 | §10.1 item 6 | "Σ all parsed rows of the group TB (primary rows + synthetic rows)" double-counts. The TB is `EXPLODEFLAG`, so sub-group rows repeat their parent, and `Opening Stock` is **nested** inside `Current Assets`: B's −19,54,753.74 already includes the −24,450.00. Measured while writing this plan: primary + both synthetic rows = −24,450.00 on B. | Imbalance = Σ first-occurrence primary-group rows + the top-level `Unadjusted Forex Gain/Loss` row (0 if absent). Measured: B 31-03-2023 = **0.00** (primary 183.87 + forex −183.87); A FY-end (`p16_A_tb_fy_end.xml`) = **33,05,800.00**. Both are pinned in Task 9. |

---

## File structure

```
v2/
  pyproject.toml                      MODIFY: add fastapi, uvicorn, sqlalchemy[asyncio], asyncpg, alembic, pydantic,
                                      pydantic-settings, pyjwt, passlib[bcrypt], bcrypt<5; testpaths unchanged
  contract/
    __init__.py
    parse.py                          §5.2 parsers + WireParseError + Amount
    models.py                         wire kinds, REQUIRED_KEYS, request/response pydantic models, error code sets
    transcode.py                      Tally XML -> wire objects (masters, vouchers, counters, report cells)
    tally_rules.py                    PRIMARY_NATURE, RESERVED_VOUCHER_TYPES, SYNTHETIC_TB_ROWS, OPENING_STOCK_ROW
  cloud/
    __init__.py
    __main__.py                       python -m v2.cloud migrate | purge
    cli.py
    main.py                           create_app(settings, clock)
    config.py                         V2Settings
    clock.py                          SystemClock, FixedClock, ist_date(), fy_start_of()
    db.py                             make_engine, session dependency
    errors.py                         ApiError + handler ({"error","detail",...extra})
    alembic.ini
    alembic/env.py, alembic/script.py.mako, alembic/versions/v2_001_sync_tables.py
    models/__init__.py, base.py, current.py (read-only users/workspaces Tables), bookkeeping.py, masters.py,
          vouchers.py, snapshots.py, parity.py
    auth/__init__.py, passwords.py (copied verify_password), web_jwt.py, device_tokens.py, rate_limit.py
    api/__init__.py, dependencies.py, agent_auth.py, devices.py, sync.py, web_sync.py
    sync/__init__.py, binding.py, state.py, runs.py, coverage.py, commands.py, maintenance.py, purge.py
    ingest/__init__.py, parsed.py, validate.py, resolve.py, derive.py, store.py, pipeline.py, reconcile.py,
          snapshots.py
    parity/__init__.py, model.py, anchors.py, rung0.py, rung1.py, forex.py, rung2.py, classify.py, ladder.py,
          bisect.py, engine.py, opsignal.py
  tests/
    test_isolation.py                 MODIFY: the new dependency rules
    contract/__init__.py, test_parse.py, test_transcode.py, test_roundtrip.py
    cloud/__init__.py, conftest.py (DB harness, factories, api client), realdata.py (fixture assembler),
          fakeb.py (FakeBooks company B generator, cached)
    cloud/unit/  test_config_clock.py, test_tokens.py, test_rate_limit.py, test_derive.py, test_validate.py,
                 test_rung0.py, test_resolve.py, test_coverage.py, test_anchors.py, test_rung1.py, test_forex.py,
                 test_rung2.py, test_classify.py, test_ladder.py, test_bisect.py, test_opsignal.py,
                 test_snapshot_rows.py, test_realdata_parity_a.py, test_realdata_parity_b.py,
                 test_realdata_midbackfill.py, test_assembler.py
    cloud/db/    test_migration.py, test_auth_api.py, test_binding_api.py, test_heartbeat_api.py, test_runs_api.py,
                 test_ingest_api.py, test_ingest_scenarios.py, test_reconcile_snapshots_api.py,
                 test_parity_api.py, test_maintenance_purge.py, test_state_matrix_api.py,
                 test_end_to_end_fakeb.py, test_restart_roundtrip.py
    fixtures/sync/s1_*                Task 0 captures (controller)
```

## Commands used throughout (run from the repo root)

```bash
# whole v2 suite (baseline 973 passed, ~4 min)
uv run --project v2 pytest v2/tests -q
# contract / cloud unit (no DB)
uv run --project v2 pytest v2/tests/contract v2/tests/cloud/unit -q
# cloud DB + API (Homebrew postgresql@16 must be running; ask before `brew services start postgresql@16`)
TEST_DATABASE_URL=postgresql+asyncpg://nuvanta-mac-3@localhost/tallyagent_test \
  uv run --project v2 pytest v2/tests/cloud/db -q 2>&1 | tee logs/v2-s1-db-<task>.log
# isolation
uv run --project v2 pytest v2/tests/test_isolation.py -q
```

Commit trailer on **every** commit in this plan (use a HEREDOC):

```
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

Never `git add -A`. Add the named files only. The untracked root files (screenshots, `scripts/cleanup_*.py`,
`backups/`, `.pgtmp/`, `logs/`, …) must never be committed on this branch (S0 exit-gate note).

**Review checkpoint (every task):** after the commit, the controller dispatches a fresh reviewer subagent
(superpowers:requesting-code-review, task-scoped: the diff `git diff <task-start-sha>..HEAD`, the spec sections in
the task header, and this task's text). The reviewer checks spec conformance, the Global Constraints, and that every
test named in the task exists and asserts what its name says. Critical/Important findings are fixed in the same task
before the next task starts. Minors are listed for Task 13. The controller then updates the tracker row(s) named in
the task.

---

## Task P: Pre-flight scan

**Spec:** §1, §6.2, §13, §16 "Regression / isolation". **Tracker:** none (the log only).

**Files:** none are changed. Output goes to `logs/v2-s1-preflight.log` (untracked).

- [ ] **Step 1: Branch and clean-tree check**

```bash
cd "/Users/nuvanta-mac-3/work/Tally prime"
git rev-parse --abbrev-ref HEAD            # expect feat/bi-s0-probe-harness (or an S1 branch cut from it)
git status --short | grep -v '^??'         # expect nothing (only untracked noise)
git rev-parse --short HEAD | tee logs/v2-s1-preflight.log
```

If the controller wants a dedicated branch, cut `feat/bi-s1-cloud` from the current HEAD now (git branch
discipline: feature branch → `dev`, never `master`).

- [ ] **Step 2: Baseline v2 suite**

```bash
uv run --project v2 pytest v2/tests -q 2>&1 | tail -3 | tee -a logs/v2-s1-preflight.log
```
Expected: `973 passed` (or the current count). Record the number: it is the regression baseline for Task 15.

- [ ] **Step 3: Postgres and test DB reachable**

```bash
/opt/homebrew/opt/postgresql@16/bin/pg_isready -h localhost | tee -a logs/v2-s1-preflight.log
/opt/homebrew/opt/postgresql@16/bin/psql -h localhost -d tallyagent_test -c '\dt' | tee -a logs/v2-s1-preflight.log
```
If Postgres isn't running, **ask the user** before `brew services start postgresql@16` (durable change). If
`tallyagent_test` is missing: `createdb -h localhost tallyagent_test`. Record whether `users`/`workspaces` already
exist there (they decide whether the v2 harness creates stand-ins, A2).

- [ ] **Step 4: Fixture inventory (Review Focus 1)**

```bash
cd v2/tests/fixtures/sync
for f in p06_A_vouchers_nested.xml p16_A_vouchers_fy.xml p04_A_voucher_full.xml p21_B_fy2022_month_09.xml \
         p22_B_forex_sales.xml p25_A_groups.xml p04_A_group_full.xml p18_B_ledger_list.xml p16_B_ledgers.xml \
         p22_B_currencies.xml p25_B_voucher_types.xml p22_B_usd_ledger.xml; do
  echo "$f alt=$(grep -c '<ALTERID' $f) guid=$(grep -c '<GUID' $f) canc=$(grep -c '<ISCANCELLED' $f) parent=$(grep -c '<PARENT' $f)"
done | tee -a ../../../../logs/v2-s1-preflight.log
ls s1_* 2>/dev/null | tee -a ../../../../logs/v2-s1-preflight.log   # Task 0 output, may be empty
```
Expected (measured while writing this plan): A voucher files `alt=0`; `p04_A_voucher_full` `alt=50 guid=50`;
B month/forex files carry everything; `p25_A_groups`, `p18_B_ledger_list`, `p22_B_currencies`,
`p25_B_voucher_types`, `p22_B_usd_ledger` `guid=0`; `p16_B_ledgers` `guid=27`. Any difference changes Task 12's
assembler joins. Report it to the controller before Task 1.

- [ ] **Step 5: Isolation baseline**

```bash
uv run --project v2 pytest v2/tests/test_isolation.py -q
git diff --name-only master...HEAD | grep -v '^v2/\|^docs/' || echo "only v2/ and docs/"
```

No commit (nothing changes). **Review checkpoint:** the controller reads the log and confirms the baseline count, DB
state and fixture inventory before dispatching Task 1.

---

## Task 0: Live fixture capture G1–G5 (done by the controller)

**Spec:** §13.3, §18 item 0, §17.2 item 5. **Tracker:** S1 fixture-gap note.

**This task is done by the controller, not by an implementer subagent.** It is read-only live Tally. Files land
under `v2/tests/fixtures/sync/s1_*`, each with its `.xml.json` sidecar (same Capture format as S0).

Names this plan's later tasks expect (the controller renames to these, or tells Task 12 the real names):

| Gap | Expected file(s) | Consumed by |
|---|---|---|
| G1 | `s1_A_tb_ledgerwise_asof_2025-04-01.xml` (ledger-level TB, `ISLEDGERWISE=Yes`, from 01-04-2025 to 01-04-2025) | Task 12 A parity anchor (D9, E = books_from) |
| G2 | `s1_B_tb_ledgerwise_current.xml`, `s1_B_tb_ledgerwise_asof_2023-03-31.xml` | Task 10 forex (TB row plain vs expression), Task 12 B parity |
| G3 | `s1_B_tb_asof_2026-03-31.xml` (group TB, EXPLODEFLAG, current FY end) | Task 12 B current-period parity |
| G4 | `s1_A_voucher_types_ids.xml`, `s1_A_currencies_ids.xml`, `s1_A_stock_groups_ids.xml`, `s1_A_units_ids.xml`, `s1_B_voucher_types_ids.xml`, `s1_B_currencies_ids.xml`, `s1_B_groups_ids.xml`, `s1_B_ledgers_ids.xml` (GUID + AlterID + Name + Parent) | Task 1 transcoder tests, Task 12 assembler |
| G5 | `s1_B_ledger_balances_touched.xml` (mirrored `ledger_balance` re-read filtered to the ledgers of one month) | Task 8c balance ingest, Task 12 |
| G6 (recommended, not in the spec) | `s1_B_tb_ledgerwise_asof_2022-04-01.xml` | Task 12 B books-start anchor (A5) |

**Fallback rule for every later task:** each test that consumes an `s1_*` file does
`path = SYNC / "s1_…"; if not path.exists(): pytest.skip("G<n> not captured — FakeBooks twin runs instead")`. A
FakeBooks twin of the same test runs unconditionally. So a missing capture never turns the suite red, and never
turns it green on invented data. Task 15 lists every gap still open.

- [ ] Controller: captures committed as `data(bi/v2): S1 task 0 — read-only captures G1–G5 (+G6)` (trailer), or
  gaps recorded in the tracker as "FakeBooks substitute".

---
## Task 1: `v2/contract` — parsers, wire models, transcoder

**Spec:** §5 (all), D1, D2, D3, D31, §13.1, §16 "Contract unit tests". **Tracker:** prerequisite for S1.9–S1.11
(note "contract built").

**Files:**
- Modify: `v2/pyproject.toml` (dependencies; see Step 1), `v2/uv.lock` (regenerated)
- Create: `v2/contract/__init__.py`, `v2/contract/parse.py`, `v2/contract/tally_rules.py`,
  `v2/contract/models.py`, `v2/contract/transcode.py`
- Test: `v2/tests/contract/__init__.py`, `v2/tests/contract/test_parse.py`, `v2/tests/contract/test_transcode.py`,
  `v2/tests/contract/test_roundtrip.py`

**Interfaces:**
- Produces (`v2.contract.parse`):
  - `class WireParseError(ValueError)` with `.code: str` (one of `unparseable_amount`, `invalid_date`,
    `invalid_logical`, `invalid_counter`) and `.text: str | None`.
  - `@dataclass(frozen=True) class Amount: inr: Decimal | None; fx_currency: str | None; fx_amount: Decimal | None;
    fx_rate: Decimal | None; stated: bool; text: str`.
  - `amount(text: str | None) -> Amount | None` (`None` for missing or blank)
  - `tally_date(text: str) -> date`
  - `logical(text: str) -> bool`
  - `quantity(text: str | None) -> tuple[Decimal | None, str]`
  - `rate(text: str | None) -> tuple[Decimal | None, str | None, str]`
  - `credit_period(text: str | None) -> tuple[int | None, str]`
  - `name(text: str | None) -> str`
  - `counter(text: str) -> int`
- Produces (`v2.contract.tally_rules`): `PRIMARY_NATURE: dict[str, str]`, `RESERVED_VOUCHER_TYPES: frozenset[str]`
  (24 names), `OPENING_STOCK_ROW = "Opening Stock"`, `UNADJUSTED_FOREX_ROW = "Unadjusted Forex Gain/Loss"`,
  `SYNTHETIC_TB_ROWS = frozenset({OPENING_STOCK_ROW, UNADJUSTED_FOREX_ROW})`, `PL_ACCOUNT_LEDGER = "Profit & Loss A/c"`.
- Produces (`v2.contract.models`): `MASTER_KINDS`, `BALANCE_KINDS`, `ALL_KINDS`,
  `REQUIRED_KEYS: dict[str, tuple[str, ...]]`, `LINE_REQUIRED`, `INVENTORY_REQUIRED`,
  `DETERMINISTIC_CODES: frozenset[str]`, `RETRYABLE_CODES: frozenset[str]`, and the pydantic models `WireObject`,
  `QuarantineEntry`, `Chunk`, `BatchRequest`, `BatchResponse`, `SnapshotRequest`, `HeartbeatRequest`,
  `RunCreate`, `RunPatch`, `CoveragePatch`, `ReconcileRequest`, `ParityRequest`, `BindRequest`, `LoginRequest`,
  `RelinkRequest`, `WebCommand` (fields exactly as the §7 JSON examples).
- Produces (`v2.contract.transcode`): `class TallyErrorEnvelope(ValueError)`,
  `masters_from_xml(raw_xml: str, kind: str) -> list[dict]` (each `{"kind", "data"}`),
  `vouchers_from_xml(raw_xml: str) -> list[dict]`, `counters_from_xml(raw_xml: str) -> dict[str, str]`
  (verbatim `guid`, `name`, `altvchid`, `altmstid`, `booksfrom`), `report_cells(raw_xml: str, report_type: str) ->
  list[dict[str, str]]`, `MASTER_TAGS: dict[str, str]`.

- [ ] **Step 1: Dependencies**

Edit `v2/pyproject.toml` `dependencies`:

```toml
dependencies = [
    "httpx>=0.27",
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.30",
    "alembic>=1.13",
    "pyjwt>=2.8",
    "passlib[bcrypt]>=1.7",
    "bcrypt>=4.0,<5.0",
]
```

Run: `uv lock --project v2 && uv sync --project v2`. Expected: a lock update, no errors. (The bcrypt pin matches the
current app: passlib breaks on bcrypt 5.)

- [ ] **Step 2: Write the failing parser tests** (`v2/tests/contract/test_parse.py`)

Every value comes from a named capture (the comment names it).

```python
from datetime import date
from decimal import Decimal

import pytest

from v2.contract import parse
from v2.contract.parse import WireParseError


@pytest.mark.parametrize("text, inr", [
    ("-62800.00", Decimal("-62800.00")),      # p16_A_ledgers.xml
    ("1261.42", Decimal("1261.42")),          # p22_B_forex_sales.xml, Output CGST
    ("0.00", Decimal("0.00")),
    ("-1,048,846.53", Decimal("-1048846.53")),
])
def test_amount_plain_numbers(text, inr):
    a = parse.amount(text)
    assert a.inr == inr and a.stated and a.fx_amount is None and a.text == text


def test_amount_forex_voucher_line_takes_the_stated_base():            # p22_B_forex_sales.xml [S0-B:101]
    a = parse.amount("-$448.44 @ ? 82.99/$ = -? 37216.04")
    assert (a.inr, a.fx_currency, a.fx_amount, a.fx_rate, a.stated) == (
        Decimal("-37216.04"), "$", Decimal("-448.44"), Decimal("82.99"), True)


def test_amount_forex_ledger_balance_takes_the_stated_base():          # p22_B_usd_ledger.xml
    a = parse.amount("-$1609.71 @ ? 82.58/$ = -? 132929.85")
    assert a.inr == Decimal("-132929.85") and a.fx_amount == Decimal("-1609.71") and a.fx_rate == Decimal("82.58")


def test_amount_forex_credit_side_positive():
    a = parse.amount("$448.44 @ ? 82.99/$ = ? 37216.04")
    assert a.inr == Decimal("37216.04") and a.fx_amount == Decimal("448.44")


def test_amount_expression_without_base_is_unstated_never_computed():   # D3
    a = parse.amount("-$448.44 @ ? 82.99/$")
    assert a.inr is None and a.stated is False and a.fx_amount == Decimal("-448.44")


@pytest.mark.parametrize("text", ["", "   "])
def test_amount_blank_is_none_never_zero(text):
    assert parse.amount(text) is None
    assert parse.amount(None) is None


@pytest.mark.parametrize("text", ["abc", "12.3.4", "$ @ ? /$", "-$1 @ ? 2/€ = -? 2"])
def test_amount_junk_raises(text):
    with pytest.raises(WireParseError) as err:
        parse.amount(text)
    assert err.value.code == "unparseable_amount"


@pytest.mark.parametrize("text, expected", [
    ("20220901", date(2022, 9, 1)),       # p22_B_forex_sales.xml DATE
    ("1-Oct-25", date(2025, 10, 1)),      # report date form
    ("15-Dec-25", date(2025, 12, 15)),
    ("01-10-2025", date(2025, 10, 1)),
    ("01-04-2022", date(2022, 4, 1)),     # snapshot from_date
])
def test_tally_date_forms(text, expected):
    assert parse.tally_date(text) == expected


@pytest.mark.parametrize("text", ["", "2022-13-01", "31-02-2023", "yesterday"])
def test_tally_date_invalid_raises(text):
    with pytest.raises(WireParseError) as err:
        parse.tally_date(text)
    assert err.value.code == "invalid_date"


def test_logical_yes_no_only():
    assert parse.logical("Yes") is True and parse.logical("No") is False
    for bad in ("", "yes ", "Y", "True"):
        with pytest.raises(WireParseError):
            parse.logical(bad)


@pytest.mark.parametrize("text, number", [
    (" 17 Nos", Decimal("17")),               # p22_B_forex_sales.xml
    ("10 Box 0 Nos", Decimal("10")),          # p15_B_compound_unit_voucher.xml (C40)
    ("-2.0000 NOS", Decimal("-2.0000")),
    ("", None),
])
def test_quantity_leading_number(text, number):
    assert parse.quantity(text) == (number, text)


def test_rate_plain_and_currency():
    assert parse.rate("824.46/Nos") == (Decimal("824.46"), "Nos", "824.46/Nos")
    assert parse.rate("$12.50/Nos") == (None, "Nos", "$12.50/Nos")
    assert parse.rate("") == (None, None, "")


def test_credit_period_days_only():                                     # p23_B_bills_credit_period.xml
    assert parse.credit_period("30 Days") == (30, "30 Days")
    assert parse.credit_period("45 Days") == (45, "45 Days")
    assert parse.credit_period("15-Oct-25") == (None, "15-Oct-25")
    assert parse.credit_period(None) == (None, "")


def test_name_strips_reserved_prefix_and_keeps_unicode():               # p25_A_groups.xml, p23_B_bills_receivable_due.xml
    assert parse.name("\u0004 Primary") == "Primary"
    assert parse.name("\u0004Primary") == "Primary"
    assert parse.name("शर्मा ट्रेडर्स") == "शर्मा ट्रेडर्स"
    assert parse.name("Duties & Taxes") == "Duties & Taxes"


def test_counter_leading_space():                                       # p01_A_counters_baseline.xml
    assert parse.counter(" 50") == 50 and parse.counter(" 965") == 965
    with pytest.raises(WireParseError):
        parse.counter("x")
```

- [ ] **Step 3: Run, verify they fail**

Run: `uv run --project v2 pytest v2/tests/contract/test_parse.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'v2.contract'`.

- [ ] **Step 4: Implement `v2/contract/parse.py`** (and an empty `v2/contract/__init__.py`)

Get the source sha first: `git log -1 --format=%h -- v2/agent/tally/amounts.py`.

```python
"""Wire-value parsers (S1 spec §5.2). Strict: a value that is present but unparseable raises WireParseError; blank is
None; nothing is ever turned into zero (Part 1 §13).

Forex grammar: # Copied from: v2/agent/tally/amounts.py @ <sha> — changed per S1 D3: an expression without a stated
"= base" part gives inr=None, stated=False (the agent's copy derives face x rate; S1 never computes a base)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


class WireParseError(ValueError):
    def __init__(self, code: str, text: str | None):
        super().__init__(f"{code}: {text!r}")
        self.code, self.text = code, text


@dataclass(frozen=True)
class Amount:
    inr: Decimal | None
    fx_currency: str | None
    fx_amount: Decimal | None
    fx_rate: Decimal | None
    stated: bool
    text: str


_PLAIN = re.compile(r"^-?\d+(\.\d+)?$")
_NUM = r"\d[\d,]*(?:\.\d+)?"
_CURRENCY = r"[^\d\s@=/,.+?₹-]+"
_BASE_SYMBOL = r"\?|₹|Rs\.?"
_FOREX = re.compile(
    rf"^\s*(?P<sign>-?)\s*(?P<cur>{_CURRENCY})\s*(?P<face>{_NUM})"
    rf"\s*@\s*(?P<rsym>{_BASE_SYMBOL})?\s*(?P<rate>{_NUM})\s*/\s*(?P<per>{_CURRENCY})"
    rf"(?:\s*=\s*(?P<bsign>-?)\s*(?P<bsym>{_BASE_SYMBOL})?\s*(?P<base>{_NUM}))?\s*$")


def _dec(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def amount(text: str | None) -> Amount | None:
    if text is None or not text.strip():
        return None
    if "@" not in text:
        cleaned = text.strip().replace(",", "")
        if not _PLAIN.match(cleaned):
            raise WireParseError("unparseable_amount", text)
        return Amount(Decimal(cleaned), None, None, None, True, text)
    m = _FOREX.match(text)
    if m is None or m["per"] != m["cur"]:
        raise WireParseError("unparseable_amount", text)
    negative = m["sign"] == "-"
    face = _dec(m["face"]) * (-1 if negative else 1)
    fx_rate = _dec(m["rate"])
    if m["base"] is None:
        return Amount(None, m["cur"], face, fx_rate, False, text)
    stated = _dec(m["base"])
    if ((m["bsign"] == "-") != negative and face != 0 and stated != 0) or (m["bsym"] or "") != (m["rsym"] or ""):
        raise WireParseError("unparseable_amount", text)
    return Amount(stated * (-1 if m["bsign"] == "-" else 1), m["cur"], face, fx_rate, True, text)


_DATE_FORMS = ("%Y%m%d", "%d-%b-%y", "%d-%m-%Y")


def tally_date(text: str) -> date:
    for form in _DATE_FORMS:
        try:
            return datetime.strptime((text or "").strip(), form).date()
        except ValueError:
            continue
    raise WireParseError("invalid_date", text)


def logical(text: str) -> bool:
    if text == "Yes":
        return True
    if text == "No":
        return False
    raise WireParseError("invalid_logical", text)


_LEADING = re.compile(r"^\s*(-?\d[\d,]*(?:\.\d+)?)")


def quantity(text: str | None) -> tuple[Decimal | None, str]:
    text = text or ""
    m = _LEADING.match(text)
    return (_dec(m.group(1)) if m else None), text


def rate(text: str | None) -> tuple[Decimal | None, str | None, str]:
    text = text or ""
    if not text.strip():
        return None, None, text
    number, _, unit = text.partition("/")
    unit = unit.strip() or None
    try:
        return Decimal(number.strip().replace(",", "")), unit, text
    except InvalidOperation:
        return None, unit, text                       # a currency-symbol rate: text kept, never parsed to a number


_DAYS = re.compile(r"^\s*(\d+)\s*Days?\s*$", re.IGNORECASE)


def credit_period(text: str | None) -> tuple[int | None, str]:
    text = text or ""
    m = _DAYS.match(text)
    return (int(m.group(1)) if m else None), text


def name(text: str | None) -> str:
    text = text or ""
    if text.startswith("\u0004"):
        text = text[1:].lstrip(" ")
    return text.strip()


def counter(text: str) -> int:
    try:
        return int((text or "").strip())
    except ValueError:
        raise WireParseError("invalid_counter", text) from None
```

- [ ] **Step 5: Run, verify the parser tests pass**

Run: `uv run --project v2 pytest v2/tests/contract/test_parse.py -q` → all PASS.

- [ ] **Step 6: Write the failing transcoder tests** (`v2/tests/contract/test_transcode.py`)

```python
from decimal import Decimal
from pathlib import Path

import pytest

from v2.contract import transcode
from v2.contract.parse import amount

SYNC = Path(__file__).resolve().parents[1] / "fixtures" / "sync"


def _read(name: str) -> str:
    return (SYNC / name).read_text(encoding="utf-8")


def test_forex_sales_usd_voucher_is_verbatim():                        # spec §5.4, p22_B_forex_sales.xml
    vouchers = {v["data"]["guid"]: v["data"] for v in transcode.vouchers_from_xml(_read("p22_B_forex_sales.xml"))}
    usd = vouchers["138b7373-753c-4dbe-aa63-b802035f0ba9-000003c1"]
    assert usd["masterid"] == " 961" and usd["alterid"] == " 965" and usd["date"] == "20220901"
    assert [(e["ledgername"], e["amount"]) for e in usd["ledger_entries"]] == [
        ("Gulf Office Supplies LLC (USD)", "-$448.44 @ ? 82.99/$ = -? 37216.04"),
        ("Export Sales", "$448.44 @ ? 82.99/$ = ? 37216.04")]


def test_inr_sale_example_matches_spec():                              # spec §5.4 first example
    v = next(v["data"] for v in transcode.vouchers_from_xml(_read("p22_B_forex_sales.xml"))
             if v["data"]["guid"].endswith("-00000067"))
    assert v["ledger_entries"][0]["bill_allocations"] == [
        {"name": "Inv/103", "billtype": "New Ref", "billcreditperiod": "30 Days", "amount": "-16538.66"}]
    assert v["inventory_entries"][0]["actualqty"] == " 17 Nos" and v["inventory_entries"][0]["rate"] == "824.46/Nos"
    assert sum(amount(e["amount"]).inr for e in v["ledger_entries"]) == Decimal("0.00")


@pytest.mark.parametrize("fixture", ["p06_A_vouchers_nested.xml", "p22_B_forex_sales.xml",
                                     "p21_B_fy2022_month_09.xml"])
def test_only_allledgerentries_become_ledger_entries(fixture):         # LESSONS rule 18, the 2x trap
    for v in transcode.vouchers_from_xml(_read(fixture)):
        assert "ledgerentries_list" not in v["data"]
        total = sum((amount(e["amount"]).inr for e in v["data"]["ledger_entries"]), Decimal("0"))
        assert total == Decimal("0.00"), v["data"]["guid"]


def test_p06_has_50_vouchers_all_balanced():
    assert len(transcode.vouchers_from_xml(_read("p06_A_vouchers_nested.xml"))) == 50


def test_cancelled_voucher_has_no_entries_and_empty_party():           # p03_B_flagged_month_2023_02.xml [S0-B:201]
    v = next(v["data"] for v in transcode.vouchers_from_xml(_read("p03_B_flagged_month_2023_02.xml"))
             if "[S0-B:201]" in v["data"].get("narration", ""))
    assert v["iscancelled"] == "Yes" and v.get("partyledgername", "") == "" and v["ledger_entries"] == []


def test_hindi_narration_round_trips():                                # p15_B_hindi_narration.xml
    texts = [v["data"].get("narration", "") for v in transcode.vouchers_from_xml(_read("p15_B_hindi_narration.xml"))]
    assert any(any("ऀ" <= ch <= "ॿ" for ch in t) for t in texts)


def test_compound_unit_quantity_verbatim():                            # p15_B_compound_unit_voucher.xml
    qtys = [i["actualqty"] for v in transcode.vouchers_from_xml(_read("p15_B_compound_unit_voucher.xml"))
            for i in v["data"].get("inventory_entries", []) if "actualqty" in i]
    assert "10 Box 0 Nos" in [q.strip() for q in qtys]


def test_group_master_keeps_reserved_prefix_verbatim():                # p25_A_groups.xml (D31 applies server-side)
    groups = {g["data"]["name"]: g["data"] for g in transcode.masters_from_xml(_read("p25_A_groups.xml"), "group")}
    assert groups["Current Assets"]["parent"].startswith("\u0004")
    assert groups["North Zone Debtors"]["parent"] == "Sundry Debtors"


def test_ledger_master_name_from_attribute_and_forex_balance():        # p22_B_usd_ledger.xml
    ledgers = transcode.masters_from_xml(_read("p22_B_usd_ledger.xml"), "ledger")
    usd = next(l["data"] for l in ledgers if l["data"]["name"] == "Gulf Office Supplies LLC (USD)")
    assert usd["currencyname"] == "$" and usd["closingbalance"] == "-$1609.71 @ ? 82.58/$ = -? 132929.85"


def test_ledger_full_carries_guid_and_alterid():                       # p04_A_ledger_full.xml
    ledgers = transcode.masters_from_xml(_read("p04_A_ledger_full.xml"), "ledger")
    assert len(ledgers) == 35 and all(l["data"]["guid"] and l["data"]["alterid"] for l in ledgers)


def test_counters_verbatim():                                          # p01_A_counters_baseline.xml
    c = transcode.counters_from_xml(_read("p01_A_counters_baseline.xml"))
    assert (c["altvchid"], c["altmstid"], c["booksfrom"], c["guid"]) == (
        " 50", " 265", "20250401", "710de34a-3661-4a7b-8148-c2206c3b3e17")


def test_tb_cells_pair_name_with_amounts_and_keep_synthetic_rows():    # p18_B_tb_asof_2023-03-31.xml
    cells = transcode.report_cells(_read("p18_B_tb_asof_2023-03-31.xml"), "trial_balance")
    names = [c["dspdispname"] for c in cells]
    assert names[:2] == ["Capital Account", "Capital Account"]
    assert {"Opening Stock", "Unadjusted Forex Gain/Loss"} <= set(names)
    forex = next(c for c in cells if c["dspdispname"] == "Unadjusted Forex Gain/Loss")
    assert forex == {"dspdispname": "Unadjusted Forex Gain/Loss", "dspcldramta": "-183.87", "dspclcramta": ""}


@pytest.mark.parametrize("fixture, report_type, keys", [
    ("p12_A_stock_summary_today.xml", "stock_summary", {"dspdispname", "dspclqty", "dspclrate", "dspclamta"}),
    ("p23_B_bills_receivable_due.xml", "bills_receivable",
     {"billdate", "billref", "billparty", "billcl", "billdue", "billoverdue"}),
    ("p12_A_bs_today.xml", "balance_sheet", {"dspdispname", "bssubamt", "bsmainamt"}),
    ("p12_A_pl_fy2025.xml", "profit_and_loss", {"dspdispname", "plsubamt", "bsmainamt"}),
])
def test_report_cell_keys_per_type(fixture, report_type, keys):        # spec §7.12
    cells = transcode.report_cells(_read(fixture), report_type)
    assert cells and all(set(c) <= keys for c in cells)


@pytest.mark.parametrize("fixture", ["p10_A_no_company_report.xml", "p10_A_no_company_collection.xml"])
def test_error_envelope_raises(fixture):
    with pytest.raises(transcode.TallyErrorEnvelope):
        transcode.report_cells(_read(fixture), "trial_balance")
    with pytest.raises(transcode.TallyErrorEnvelope):
        transcode.vouchers_from_xml(_read(fixture))
```

Also add one parametrized smoke test over **every** §13.1 capture (the list in the spec table, expanded from its
globs). Each capture must transcode without raising through the right function (vouchers / masters kind / report
type / counters). Name it `test_every_s13_capture_transcodes`. The parameter list is a literal tuple of
`(fixture, fn, arg)`, written out in the test file, one line per file. If a capture's real shape makes a test above
fail (for example, the P&L cell keys differ), fix the **transcoder**, then correct the key set to the capture's real
keys and say so in the commit message. Never edit a fixture.

- [ ] **Step 7: Run, verify they fail**

Run: `uv run --project v2 pytest v2/tests/contract/test_transcode.py -q` → FAIL (module missing).

- [ ] **Step 8: Implement `v2/contract/tally_rules.py`, `models.py`, `transcode.py`**

`tally_rules.py`: copy `PRIMARY_NATURE` from `v2/probes/reads.py` (header `# Copied from: v2/probes/reads.py @
<sha>`), plus:

```python
RESERVED_VOUCHER_TYPES = frozenset({
    "Attendance", "Contra", "Credit Note", "Debit Note", "Delivery Note", "Job Work In Order", "Job Work Out Order",
    "Journal", "Material In", "Material Out", "Memorandum", "Payment", "Payroll", "Physical Stock", "Purchase",
    "Purchase Order", "Receipt", "Receipt Note", "Rejections In", "Rejections Out", "Reversing Journal", "Sales",
    "Sales Order", "Stock Journal"})   # probe 25 observation `base_types` (v2/probes/results/results.json), 24 values
OPENING_STOCK_ROW = "Opening Stock"
UNADJUSTED_FOREX_ROW = "Unadjusted Forex Gain/Loss"
SYNTHETIC_TB_ROWS = frozenset({OPENING_STOCK_ROW, UNADJUSTED_FOREX_ROW})
PL_ACCOUNT_LEDGER = "Profit & Loss A/c"
PRIMARY_PARENT = "Primary"
```

`models.py` (the key constants; the pydantic bodies copy the §7 JSON field names one for one):

```python
MASTER_KINDS = ("currency", "group", "voucher_type", "unit", "stock_group", "ledger", "stock_item")   # §12 step 6 order
BALANCE_KINDS = ("ledger_balance", "stock_balance")
ALL_KINDS = MASTER_KINDS + BALANCE_KINDS + ("voucher",)
REQUIRED_KEYS = {
    "currency": ("guid", "alterid", "name", "expandedsymbol"),
    "group": ("guid", "alterid", "name", "parent"),
    "voucher_type": ("guid", "alterid", "name", "parent"),
    "ledger": ("guid", "alterid", "name", "parent"),
    "stock_group": ("guid", "alterid", "name", "parent"),
    "unit": ("guid", "alterid", "name"),
    "stock_item": ("guid", "alterid", "name", "parent", "baseunits"),
    "ledger_balance": ("guid", "name", "closingbalance", "captured_at"),
    "stock_balance": ("guid", "name", "closingvalue", "captured_at"),
    "voucher": ("guid", "masterid", "alterid", "date", "vouchertypename", "iscancelled", "isoptional",
                "ispostdated", "ledger_entries"),
}
LINE_REQUIRED = ("ledgername", "amount", "isdeemedpositive")
INVENTORY_REQUIRED = ("stockitemname", "amount")
DETERMINISTIC_CODES = frozenset({"unbalanced_voucher", "unparseable_amount", "forex_base_missing", "invalid_date",
                                 "invalid_logical", "missing_field", "duplicate_posting_list", "unknown_kind"})
RETRYABLE_CODES = frozenset({"missing_master", "ambiguous_master"})
```

`transcode.py` rules:
- Run `v2.agent`-free XML sanitising **copied** from `v2/agent/tally/xml_utils.py` (`sanitize_xml`, `detect_error`,
  header `# Copied from:`): contract must not import `v2.agent`.
- `detect_error(raw)` non-None → `TallyErrorEnvelope`.
- Vouchers: every `VOUCHER` element with children. Leaf children → `tag.lower(): text` (entities decoded by
  ElementTree; text **not** stripped, so `" 961"` keeps its space). Lists: `ALLLEDGERENTRIES.LIST` →
  `ledger_entries` (leaf fields + `bill_allocations` from `BILLALLOCATIONS.LIST`, mapping `NAME→name`,
  `BILLTYPE→billtype`, `AMOUNT→amount`, `BILLCREDITPERIOD→billcreditperiod`, `BILLDATE→billdate`, only the keys that
  are present). `ALLINVENTORYENTRIES.LIST` (else `INVENTORYENTRIES.LIST`, never both) → `inventory_entries` (leaf
  fields + `batch_allocations` + `accounting_allocations`, raw only). `LEDGERENTRIES.LIST` is **not emitted**. An
  empty list placeholder (no children) is skipped. `ledger_entries` is always present (`[]` if none).
- Masters: element tag per `MASTER_TAGS = {"currency": "CURRENCY", "group": "GROUP", "voucher_type": "VOUCHERTYPE",
  "ledger": "LEDGER", "stock_group": "STOCKGROUP", "unit": "UNIT", "stock_item": "STOCKITEM"}`. `name` = child
  `NAME` text, else the `NAME` attribute. Leaf children lowercased. `LANGUAGENAME.LIST` and other lists are ignored.
- Report cells: walk children in document order. For TB-like types, each `DSPACCNAME` starts a row, and the next
  `DSPACCINFO`'s leaf tags fill it (`dspdispname`, `dspcldramta`, `dspclcramta`). Stock summary: `DSPDISPNAME` +
  `DSPSTKINFO` leaves. Bills: each `BILLFIXED` + following siblings `BILLCL`/`BILLDUE`/`BILLOVERDUE`. BS/P&L:
  `BSNAME`/`PLNAME` + `BSAMT`/`PLAMT` leaves. **Read the capture first** and adapt the element names to the capture's
  real ones; the test's key sets are the contract.
- Counters: `GUID`, `NAME` (or `BASICCOMPANYNAME`), `ALTVCHID`, `ALTMSTID`, `BOOKSFROM` leaf text, verbatim.

- [ ] **Step 9: Round-trip test** (`v2/tests/contract/test_roundtrip.py`)

```python
import json
from pathlib import Path

import pytest

from v2.contract import transcode
from v2.contract.models import WireObject

SYNC = Path(__file__).resolve().parents[1] / "fixtures" / "sync"


@pytest.mark.parametrize("fixture", ["p06_A_vouchers_nested.xml", "p22_B_forex_sales.xml",
                                     "p15_B_hindi_narration.xml", "p03_B_flagged_month_2023_02.xml"])
def test_voucher_wire_json_model_is_lossless(fixture):
    for obj in transcode.vouchers_from_xml((SYNC / fixture).read_text(encoding="utf-8")):
        again = WireObject.model_validate(json.loads(json.dumps(obj, ensure_ascii=False))).model_dump()
        assert again == obj
```

- [ ] **Step 10: Run all contract tests**

Run: `uv run --project v2 pytest v2/tests/contract -q` → all PASS.

- [ ] **Step 11: Commit**

```bash
git add v2/pyproject.toml v2/uv.lock v2/contract v2/tests/contract
git commit -F - <<'MSG'
feat(bi/v2): S1 task 1 — v2/contract parsers, wire models and Tally-XML transcoder

Verbatim-text wire (D1), strict Decimal parsers incl. the stated-base forex expression (D3, no computed base),
ALLLEDGERENTRIES-only posting rule, report cells per type; tested on the S0 captures.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** the reviewer checks: no `v2.agent`/`v2.probes` import in `v2/contract`; no `*` of face ×
rate in `parse.py`; every §5.2 row has a test with its fixture named; no fixture was edited. Tracker: note "contract
✅" on S1.9–S1.11 proof column (partial).

---

## Task 2: `v2/cloud` skeleton — app, config, clock, DB, errors, copied auth helpers, isolation

**Spec:** §6.1, §6.2, §9.4, D2, D27, Global Constraints. **Tracker:** S1.0 (partial: skeleton + isolation).

**Files:**
- Create: `v2/cloud/__init__.py`, `v2/cloud/main.py`, `v2/cloud/config.py`, `v2/cloud/clock.py`, `v2/cloud/db.py`,
  `v2/cloud/errors.py`, `v2/cloud/auth/__init__.py`, `v2/cloud/auth/passwords.py`, `v2/cloud/auth/web_jwt.py`
- Modify: `v2/tests/test_isolation.py`
- Test: `v2/tests/cloud/__init__.py`, `v2/tests/cloud/unit/__init__.py`, `v2/tests/cloud/unit/test_config_clock.py`

**Interfaces:**
- Produces:
  - `V2Settings` (pydantic-settings, `env_prefix="V2_"`), with fields `database_url` (alias `V2_DATABASE_URL` |
    `DATABASE_URL`), `web_jwt_secret` (alias `V2_WEB_JWT_SECRET` | `JWT_SECRET`), `device_token_secret`,
    `device_access_minutes=15`, `device_refresh_days=90`, `takeover_login_max_age_minutes=10`,
    `ingest_max_gzip_bytes=5_242_880`, `ingest_max_decompressed_bytes=52_428_800`, `ingest_max_objects=500`,
    `parity_tolerance_paise=100`, `quarantine_error_threshold=50`, `storage_alert_bytes=5_368_709_120`,
    `login_rate_max=5`, `login_rate_window_s=900`, `device_rate_max=600`, `device_rate_window_s=60`,
    `maintenance_slice_seconds=2.0`, `maintenance_slice_rows=5000`, `purge_grace_days=30`, `port=8100`. Plus
    `validate_for_serving()`, which raises `ValueError` if a secret is < 32 chars or `database_url` is empty.
  - `clock.Clock` protocol `now() -> datetime` (UTC, aware); `SystemClock`; `FixedClock(dt)` with `.advance(**kw)`.
    `ist_date(dt) -> date` (UTC+05:30). `fy_start_of(d: date) -> date` (1 April on or before `d`).
    `fy_end_of(d) -> date`. `current_fy_start(clock) -> date` (by IST date).
  - `errors.ApiError(status: int, code: str, detail: str = "", **extra)`, and `install_error_handler(app)` → JSON
    `{"error": code, "detail": detail, **extra}`, with `Retry-After` when `extra` has `retry_after`.
  - `db.make_engine(url) -> AsyncEngine`, `db.session_dep(request) -> AsyncIterator[AsyncSession]` (reads
    `request.app.state.sessionmaker`).
  - `main.create_app(settings: V2Settings | None = None, clock: Clock | None = None) -> FastAPI`. It sets
    `app.state.settings`, `.clock`, `.engine`, `.sessionmaker`, mounts the routers later tasks add, and exposes
    `GET /api/v2/health` → `{"ok": true}`. `main.py` also has a module-level `app = create_app()` for uvicorn.
    `create_app` never calls `validate_for_serving()` at import: it calls it in the lifespan startup, and only
    when `database_url` is set.
  - `auth.passwords.verify_password(plain, hashed) -> bool` (copied).
  - `auth.web_jwt.decode_web_access(token, secret) -> str` (the user id). It raises `ApiError(401, "token_invalid")`
    unless `payload["type"] == "access"`, and `ApiError(401, "token_expired")` on expiry.

- [ ] **Step 1: Write the failing tests** (`v2/tests/cloud/unit/test_config_clock.py`)

```python
from datetime import date, datetime, timezone

import jwt
import pytest

from v2.cloud.auth.web_jwt import decode_web_access
from v2.cloud.clock import FixedClock, current_fy_start, fy_start_of, ist_date
from v2.cloud.config import V2Settings
from v2.cloud.errors import ApiError


def test_ist_date_crosses_midnight_before_utc():
    assert ist_date(datetime(2026, 3, 31, 20, 0, tzinfo=timezone.utc)) == date(2026, 4, 1)


def test_fy_start_of():
    assert fy_start_of(date(2026, 3, 31)) == date(2025, 4, 1)
    assert fy_start_of(date(2026, 4, 1)) == date(2026, 4, 1)


def test_current_fy_uses_ist():
    assert current_fy_start(FixedClock(datetime(2026, 3, 31, 20, 0, tzinfo=timezone.utc))) == date(2026, 4, 1)


def test_settings_defaults_and_web_secret_alias(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "w" * 32)
    monkeypatch.delenv("V2_WEB_JWT_SECRET", raising=False)
    s = V2Settings(_env_file=None)
    assert (s.port, s.ingest_max_objects, s.parity_tolerance_paise, s.web_jwt_secret) == (8100, 500, 100, "w" * 32)


def test_settings_refuse_short_device_secret():
    with pytest.raises(ValueError):
        V2Settings(_env_file=None, database_url="postgresql+asyncpg://x/y", web_jwt_secret="w" * 32,
                   device_token_secret="short").validate_for_serving()


def test_web_jwt_accepts_access_rejects_refresh_and_device_typ():
    secret = "w" * 32
    ok = jwt.encode({"sub": "u1", "type": "access", "exp": 4102444800}, secret, algorithm="HS256")
    assert decode_web_access(ok, secret) == "u1"
    for bad in ({"sub": "u1", "type": "refresh", "exp": 4102444800},
                {"sub": "d1", "typ": "v2_device", "exp": 4102444800}):
        with pytest.raises(ApiError) as err:
            decode_web_access(jwt.encode(bad, secret, algorithm="HS256"), secret)
        assert err.value.code == "token_invalid"
```

And the isolation additions (in `v2/tests/test_isolation.py`, a new function; keep the existing ones):

```python
def _layer_violations(root: Path) -> list[str]:
    rules = {"cloud": ("v2.agent", "v2.probes"), "agent": ("v2.cloud",), "contract": ("v2.cloud", "v2.agent", "v2.probes")}
    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if ".venv" in rel.parts or rel.parts[0] not in rules:
            continue
        mods = imported_modules(path.read_text(encoding="utf-8"), package=_package_for(rel))
        for banned in rules[rel.parts[0]]:
            if any(m == banned or m.startswith(banned + ".") for m in mods):
                found.append(f"{rel}: {rel.parts[0]} imports {banned}")
    return found


def test_layer_rules_hold():
    assert _layer_violations(V2_ROOT) == []


def test_layer_scanner_flags_each_rule(tmp_path):
    for d in ("cloud", "agent", "contract"):
        (tmp_path / d).mkdir()
    (tmp_path / "cloud" / "a.py").write_text("from v2.agent.tally import client\n")
    (tmp_path / "cloud" / "b.py").write_text("import v2.probes.reads\n")
    (tmp_path / "agent" / "c.py").write_text("from v2.cloud import main\n")
    (tmp_path / "contract" / "d.py").write_text("from ..cloud import x\n")
    (tmp_path / "cloud" / "ok.py").write_text("from v2.contract import parse\n")
    assert _layer_violations(tmp_path) == [
        "agent/c.py: agent imports v2.cloud", "cloud/a.py: cloud imports v2.agent",
        "cloud/b.py: cloud imports v2.probes", "contract/d.py: contract imports v2.cloud"]
```

- [ ] **Step 2: Run, verify they fail**

Run: `uv run --project v2 pytest v2/tests/cloud/unit/test_config_clock.py v2/tests/test_isolation.py -q`
Expected: the new tests FAIL (import errors). The existing isolation tests still pass.

- [ ] **Step 3: Implement** the files listed. `passwords.py` starts with
  `# Copied from: backend/utils/auth.py @ <sha>` and holds only `_pwd_context` + `verify_password`.
  `web_jwt.py` is a v2 re-implementation of `backend/api/dependencies.py::get_current_user`'s token check (header
  `# Copied from: backend/api/dependencies.py @ <sha> (token check only)`). `config.py` uses labelled sections with
  inline comments, the current `backend/config.py` convention.

```python
# v2/cloud/clock.py
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Protocol

IST = timezone(timedelta(hours=5, minutes=30))


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    def __init__(self, dt: datetime):
        self._dt = dt

    def now(self) -> datetime:
        return self._dt

    def advance(self, **delta) -> None:
        self._dt += timedelta(**delta)


def ist_date(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def fy_start_of(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def fy_end_of(d: date) -> date:
    start = fy_start_of(d)
    return date(start.year + 1, 3, 31)


def current_fy_start(clock: Clock) -> date:
    return fy_start_of(ist_date(clock.now()))
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run --project v2 pytest v2/tests/cloud/unit/test_config_clock.py v2/tests/test_isolation.py -q` → PASS.

- [ ] **Step 5: App smoke test** (append to `test_config_clock.py`)

```python
import httpx

from v2.cloud.main import create_app


async def test_health_route_without_db():
    app = create_app(V2Settings(_env_file=None, database_url="", web_jwt_secret="w" * 32,
                                device_token_secret="d" * 32))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/v2/health")
    assert r.status_code == 200 and r.json() == {"ok": True}
```

`create_app` creates the engine lazily (`database_url == ""` → no engine), so the smoke test needs no DB. Run it →
PASS.

- [ ] **Step 6: Commit**

```bash
git add v2/cloud v2/tests/cloud/__init__.py v2/tests/cloud/unit v2/tests/test_isolation.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 2 — v2/cloud app skeleton, V2 settings, IST clock, errors, copied auth helpers

Isolation test extended: cloud never imports agent/probes, agent never imports cloud, contract imports neither.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** copied-file headers present; `V2_` prefix; web JWT and device secret are separate settings
(D6). Tracker: S1.0 🟡 (skeleton + isolation; migration chain pending Task 3).

---
## Task 3: Migration `v2_001` — every §4 table, own Alembic chain, DB test harness

**Spec:** §4.1–§4.7, §6.2 (read-only current tables), §13.4, §16 "DB integration" (migration up/down with the
current tables present), D18, D22. **Tracker:** S1.0 (Alembic chain), S1.1, S1.2, S1.3, S1.4.

**Files:**
- Create: `v2/cloud/models/__init__.py`, `base.py`, `current.py`, `bookkeeping.py`, `masters.py`, `vouchers.py`,
  `snapshots.py`, `parity.py`; `v2/cloud/alembic.ini`, `v2/cloud/alembic/env.py`,
  `v2/cloud/alembic/script.py.mako`, `v2/cloud/alembic/versions/v2_001_sync_tables.py`; `v2/cloud/cli.py`,
  `v2/cloud/__main__.py` (the `migrate` subcommand only; `purge` comes in Task 11)
- Test: `v2/tests/cloud/conftest.py`, `v2/tests/cloud/db/__init__.py`, `v2/tests/cloud/db/test_migration.py`

**Interfaces:**
- Produces:
  - `models.base.Base` (DeclarativeBase, its own `MetaData`). `models.current.users_table` /
    `workspaces_table`: `Table` objects in `Base.metadata` with `info={"v2_readonly": True}` and only the columns v2
    reads (`users`: id, email, password_hash, name, is_active; `workspaces`: id, user_id, name, is_deleted). **No
    mapped class** for either.
  - ORM classes: `SyncWorkspace`, `AgentDevice`, `SyncRun`, `SyncBatch`, `SyncQuarantine`, `SyncCommand`,
    `SyncFyCoverage`, `TallyCurrency`, `TallyGroup`, `TallyVoucherType`, `TallyLedger`, `TallyStockGroup`,
    `TallyUnit`, `TallyStockItem`, `TallyVoucher`, `TallyVoucherLedgerLine`, `TallyVoucherInventoryLine`,
    `TallyBillAllocation`, `TallyReportSnapshot`, `ParityRun`, `ParityLine`. Table names are exactly the §4 names.
    Bare FKs, no `relationship()`.
  - `V2_TABLES: tuple[str, ...]`, all v2 table names in FK-safe **drop** order (children first). `purge` and the
    harness use it.
  - `v2.cloud.cli.migrate(url: str, revision: str = "head") -> None` / `downgrade(url, revision="base")`. The
    Alembic `env.py` configures `version_table="alembic_version_v2"` and an `include_object` that excludes every
    table whose `info.get("v2_readonly")` is set **or** that is not in `V2_TABLES`.
  - Test harness (`v2/tests/cloud/conftest.py`): `requires_db` marker; the session fixture `v2_schema`; the
    function fixtures `engine`, `session`; factories `make_user(session, email=None, password="Passw0rd!Passw0rd",
    is_active=True) -> uuid`, `make_workspace(session, user_id, name="W", is_deleted=False) -> uuid`; `app_client`
    (httpx `AsyncClient` over `create_app(settings, FixedClock)`); `settings` (a `V2Settings` with 32-char test
    secrets and `TEST_DATABASE_URL`); `clock` (`FixedClock(2026-09-25T06:30Z)`).

- [ ] **Step 1: Write the harness** (`v2/tests/cloud/conftest.py`)

```python
"""S1 DB harness. TEST_DATABASE_URL only (never .env, never the dev DB). The DB is shared with the current app's
suite (plan ambiguity A2): stand-in users/workspaces are created only if absent, and teardown leaves the DB as found."""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from v2.cloud.auth.passwords import _pwd_context
from v2.cloud.cli import downgrade, migrate
from v2.cloud.clock import FixedClock
from v2.cloud.config import V2Settings
from v2.cloud.main import create_app
from v2.cloud.models import V2_TABLES

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
requires_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set — skipping v2 DB tests")
TEST_EMAIL_DOMAIN = "@v2test.invalid"

# Copied shape from: backend/db/models.py @ <sha> (User, Workspace) — test-only stand-ins, created only if absent.
STANDIN_DDL = (
    """CREATE TABLE IF NOT EXISTS users (id uuid PRIMARY KEY, email varchar(255) UNIQUE NOT NULL,
       password_hash varchar(255) NOT NULL, name varchar(255) NOT NULL, is_active boolean DEFAULT true,
       created_at timestamptz, updated_at timestamptz)""",
    """CREATE TABLE IF NOT EXISTS workspaces (id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
       name varchar(255) NOT NULL, agent_type varchar(50) NOT NULL DEFAULT 'tally', config jsonb NOT NULL DEFAULT '{}',
       memory jsonb NOT NULL DEFAULT '{}', is_deleted boolean DEFAULT false, created_at timestamptz,
       updated_at timestamptz)""",
)


async def _existing(url: str) -> set[str]:
    eng = create_async_engine(url)
    async with eng.connect() as c:
        rows = await c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        names = {r[0] for r in rows}
    await eng.dispose()
    return names


async def _exec(url: str, *sql: str) -> None:
    eng = create_async_engine(url)
    async with eng.begin() as c:
        for s in sql:
            await c.execute(text(s))
    await eng.dispose()


@pytest.fixture(scope="session")
def v2_schema():
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    before = asyncio.run(_existing(TEST_DB))
    created = [t for t in ("users", "workspaces") if t not in before]
    asyncio.run(_exec(TEST_DB, *STANDIN_DDL))
    migrate(TEST_DB)
    yield {"created_standins": created}
    downgrade(TEST_DB, "base")
    asyncio.run(_exec(TEST_DB, "DROP TABLE IF EXISTS alembic_version_v2",
                      f"DELETE FROM workspaces WHERE user_id IN (SELECT id FROM users WHERE email LIKE '%{TEST_EMAIL_DOMAIN}')",
                      f"DELETE FROM users WHERE email LIKE '%{TEST_EMAIL_DOMAIN}'",
                      *[f"DROP TABLE IF EXISTS {t}" for t in reversed(created)]))


@pytest.fixture
async def engine(v2_schema):
    eng = create_async_engine(TEST_DB)
    async with eng.begin() as c:           # every test starts from empty v2 tables + no test users
        await c.execute(text("TRUNCATE " + ", ".join(V2_TABLES) + " CASCADE"))
        await c.execute(text(f"DELETE FROM workspaces WHERE user_id IN (SELECT id FROM users WHERE email LIKE '%{TEST_EMAIL_DOMAIN}')"))
        await c.execute(text(f"DELETE FROM users WHERE email LIKE '%{TEST_EMAIL_DOMAIN}'"))
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


async def make_user(session: AsyncSession, email: str | None = None, password: str = "Passw0rd!Passw0rd",
                    is_active: bool = True) -> uuid.UUID:
    uid = uuid.uuid4()
    await session.execute(text("INSERT INTO users (id, email, password_hash, name, is_active, created_at, updated_at) "
                               "VALUES (:id, :e, :h, 'Owner', :a, now(), now())"),
                          {"id": uid, "e": email or f"{uid.hex[:8]}{TEST_EMAIL_DOMAIN}",
                           "h": _pwd_context.hash(password), "a": is_active})
    await session.commit()
    return uid


async def make_workspace(session: AsyncSession, user_id: uuid.UUID, name: str = "W",
                         is_deleted: bool = False) -> uuid.UUID:
    wid = uuid.uuid4()
    await session.execute(text("INSERT INTO workspaces (id, user_id, name, agent_type, config, memory, is_deleted, "
                               "created_at, updated_at) VALUES (:id, :u, :n, 'tally', '{}', '{}', :d, now(), now())"),
                          {"id": wid, "u": user_id, "n": name, "d": is_deleted})
    await session.commit()
    return wid


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 9, 25, 6, 30, tzinfo=timezone.utc))


@pytest.fixture
def settings():
    return V2Settings(_env_file=None, database_url=TEST_DB, web_jwt_secret="w" * 32, device_token_secret="d" * 32)


@pytest.fixture
async def app_client(engine, settings, clock):
    app = create_app(settings, clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://v2") as c:
        c.app = app
        yield c
    await app.state.engine.dispose()
```

- [ ] **Step 2: Write the failing migration tests** (`v2/tests/cloud/db/test_migration.py`)

```python
import asyncio

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from v2.cloud.cli import downgrade, migrate
from v2.cloud.models import V2_TABLES
from v2.tests.cloud.conftest import TEST_DB, requires_db

pytestmark = requires_db

EXPECTED_COLUMNS = {   # spec §4 — the contract; an extra or missing column fails
    "sync_workspaces": {"workspace_id", "tally_company_guid", "tally_company_name", "previous_company_guids",
        "books_from", "base_currency_name", "sync_state", "restore_reason", "active_device_id", "cursor_alt_vch_id",
        "cursor_alt_mst_id", "cursor_set_at", "last_synced_at", "caught_up_at", "last_seen_at", "last_heartbeat",
        "relink_prompt", "oldest_available_fy", "oldest_complete_fy", "backfill_state", "backfill_percent",
        "last_parity", "ladder", "tb_imbalance_baseline", "quarantine_count", "storage_estimate_bytes",
        "storage_alert", "bound_at", "updated_at"},
    "agent_devices": {"id", "user_id", "workspace_id", "device_name", "agent_version", "refresh_hash",
        "refresh_prev_hash", "refresh_expires_at", "last_login_at", "last_seen_at", "is_active", "revoked_at",
        "revoke_reason", "created_at"},
    "sync_runs": {"id", "workspace_id", "device_id", "kind", "scope", "command_id", "status", "progress_done",
        "progress_total", "batches_declared", "counters_at_start", "cursor_after", "error_code", "started_at",
        "finished_at", "created_at"},
    "sync_batches": {"id", "workspace_id", "run_id", "batch_id", "request_sha256", "object_count", "status",
        "response", "received_at", "created_at"},
    "sync_quarantine": {"id", "workspace_id", "kind", "guid", "code", "detail", "voucher_date", "first_seen_at",
        "last_seen_at", "times_seen", "resolved_at", "created_at"},
    "sync_commands": {"id", "workspace_id", "type", "params", "requested_by", "status", "created_at",
        "delivered_at", "done_at"},
    "sync_fy_coverage": {"id", "workspace_id", "fy_start", "fy_end", "state", "months_done", "months_complete",
        "months_total", "completed_at", "created_at"},
    "tally_ledgers": {"id", "workspace_id", "guid", "alter_id", "is_deleted", "raw", "first_seen_at", "updated_at",
        "created_at", "name", "parent_name", "group_guid", "currency_name", "is_forex", "is_bill_wise", "tax_type",
        "gst_duty_head", "opening_balance", "closing_balance", "opening_fx_amount", "opening_fx_rate",
        "closing_fx_amount", "closing_fx_rate", "fx_currency", "balance_source", "balance_captured_at",
        "balance_text"},
    "tally_voucher_ledger_lines": {"id", "voucher_id", "workspace_id", "line_no", "ledger_name", "ledger_guid",
        "amount", "is_deemed_positive", "fx_currency", "fx_amount", "fx_rate", "voucher_date", "countable",
        "created_at"},
    "parity_lines": {"id", "workspace_id", "run_id", "scope", "guid", "name", "our_amount", "tally_amount", "diff",
        "verdict", "cause", "remediation_status", "unrealised_diff", "our_fx_amount", "tally_fx_amount",
        "as_on_date", "created_at"},
}
# The implementer extends EXPECTED_COLUMNS to every table in V2_TABLES from spec §4.2–§4.7 before implementing
# (21 tables); the test below asserts the dict covers V2_TABLES exactly.


async def _inspect(fn):
    eng = create_async_engine(TEST_DB)
    async with eng.connect() as c:
        out = await c.run_sync(lambda sync: fn(inspect(sync)))
    await eng.dispose()
    return out


def test_expected_columns_cover_every_v2_table(v2_schema):
    assert set(EXPECTED_COLUMNS) == set(V2_TABLES)


def test_every_table_has_exactly_the_spec_columns(v2_schema):
    cols = asyncio.run(_inspect(lambda i: {t: {c["name"] for c in i.get_columns(t)} for t in V2_TABLES}))
    assert cols == EXPECTED_COLUMNS


def test_money_columns_are_numeric_18_2_never_float(v2_schema):
    def check(i):
        bad = []
        for t in V2_TABLES:
            for c in i.get_columns(t):
                if "FLOAT" in str(c["type"]).upper() or "REAL" in str(c["type"]).upper():
                    bad.append((t, c["name"]))
        return bad, {c["name"]: str(c["type"]) for c in i.get_columns("tally_voucher_ledger_lines")}
    bad, line_types = asyncio.run(_inspect(check))
    assert bad == [] and line_types["amount"] == "NUMERIC(18, 2)" and line_types["fx_rate"] == "NUMERIC(18, 6)"


def test_one_active_device_partial_unique_index(v2_schema):
    idx = asyncio.run(_inspect(lambda i: i.get_indexes("agent_devices")))
    uniq = [x for x in idx if x["unique"] and x["column_names"] == ["workspace_id"]]
    assert uniq and "is_active" in str(uniq[0].get("dialect_options", {}).get("postgresql_where", ""))


def test_covering_line_index_exists(v2_schema):
    idx = asyncio.run(_inspect(lambda i: i.get_indexes("tally_voucher_ledger_lines")))
    assert any(x["column_names"] == ["workspace_id", "ledger_guid", "voucher_date"] for x in idx)


def test_version_table_is_v2_and_current_alembic_version_untouched(v2_schema):
    async def read():
        eng = create_async_engine(TEST_DB)
        async with eng.connect() as c:
            v2 = (await c.execute(text("SELECT version_num FROM alembic_version_v2"))).scalar_one()
            has_current = (await c.execute(text("SELECT to_regclass('public.alembic_version')"))).scalar_one()
            current = None
            if has_current:
                current = (await c.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
        await eng.dispose()
        return v2, has_current, current
    v2, has_current, current = asyncio.run(read())
    assert v2 == "v2_001"
    if has_current:                       # the current app's chain is never moved by v2
        assert current is None or not current.startswith("v2_")


def test_down_then_up_round_trip_keeps_users(v2_schema):
    downgrade(TEST_DB, "base")
    left = asyncio.run(_inspect(lambda i: set(i.get_table_names())))
    assert not (set(V2_TABLES) & left) and {"users", "workspaces"} <= left
    migrate(TEST_DB)
    assert set(V2_TABLES) <= asyncio.run(_inspect(lambda i: set(i.get_table_names())))


def test_session_teardown_leaves_no_v2_objects():
    """Review Focus 2. Runs the harness's teardown sequence on a throwaway round and asserts that nothing v2 remains —
    the precondition for the current app's drop_all to succeed on the shared test DB."""
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    migrate(TEST_DB)
    downgrade(TEST_DB, "base")
    left = asyncio.run(_inspect(lambda i: set(i.get_table_names())))
    assert not (set(V2_TABLES) & left)
    migrate(TEST_DB)                      # restore for the rest of the session
```

The implementer must fill `EXPECTED_COLUMNS` for all 21 tables **from spec §4 before writing the models**. The
common columns of §4.1 apply to every master and voucher table. The first test enforces the coverage.

- [ ] **Step 3: Run, verify they fail**

Run: `TEST_DATABASE_URL=postgresql+asyncpg://nuvanta-mac-3@localhost/tallyagent_test uv run --project v2 pytest v2/tests/cloud/db/test_migration.py -q`
Expected: collection error (`v2.cloud.models` missing).

- [ ] **Step 4: Implement models + migration**

The model conventions, shown on one master table (repeat the pattern for all):

```python
# v2/cloud/models/masters.py
import uuid
from datetime import datetime

from sqlalchemy import Boolean, BigInteger, Date, DateTime, ForeignKey, Index, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

MONEY = Numeric(18, 2)
FACE = Numeric(18, 4)
RATE = Numeric(18, 6)


class _MasterCommon:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    guid: Mapped[str] = mapped_column(Text, nullable=False)
    alter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    raw: Mapped[dict | None] = mapped_column(JSONB)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    name: Mapped[str] = mapped_column(Text, nullable=False)


class TallyLedger(_MasterCommon, Base):
    __tablename__ = "tally_ledgers"
    parent_name: Mapped[str] = mapped_column(Text, nullable=False)
    group_guid: Mapped[str | None] = mapped_column(Text)
    currency_name: Mapped[str | None] = mapped_column(Text)
    is_forex: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # ... every §4.3 column; money = MONEY, fx face = FACE, fx rate = RATE
    balance_source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'tally'"))
    __table_args__ = (
        Index("uq_tally_ledgers_ws_guid", "workspace_id", "guid", unique=True),
        Index("ix_tally_ledgers_ws_name_live", "workspace_id", "name", postgresql_where=text("NOT is_deleted")),
        Index("ix_tally_ledgers_ws_group", "workspace_id", "group_guid"),
    )
```

Key constraints the migration must create (hand-written `op.create_table` / `op.create_index`, **not**
autogenerate):
- `agent_devices`: `Index("uq_agent_devices_one_active", "workspace_id", unique=True, postgresql_where=text("is_active"))`,
  and unique `refresh_hash`.
- `sync_batches`: unique `(workspace_id, batch_id)`.
- `sync_quarantine`: unique `(workspace_id, kind, guid) WHERE resolved_at IS NULL`.
- `sync_fy_coverage`: unique `(workspace_id, fy_start)`.
- `sync_runs`: `(workspace_id, started_at DESC)`; partial `(workspace_id) WHERE status = 'running' AND kind = 'first_sync'` **unique** (one open first_sync).
- `tally_vouchers`: unique `(workspace_id, guid)`; `(workspace_id, date)`; `(workspace_id, party_ledger_guid, date)`; `(workspace_id, alter_id)`.
- `tally_voucher_ledger_lines`: `Index("ix_lines_cover", "workspace_id", "ledger_guid", "voucher_date", postgresql_include=["amount", "fx_amount"], postgresql_where=text("countable"))`; FK `voucher_id` → `tally_vouchers.id` `ON DELETE CASCADE`.
- `tally_voucher_inventory_lines`: `(workspace_id, stock_item_guid, voucher_date)`, cascade FK.
- `tally_bill_allocations`: `(workspace_id, ledger_guid, bill_name)`, cascade FK.
- `tally_report_snapshots`: unique `(workspace_id, report_type, as_on_date)`.
- `parity_lines`: `(workspace_id, run_id)`, `(workspace_id, verdict)`; FK `run_id` → `parity_runs.id` cascade.
- `sync_workspaces`: pk `workspace_id` FK workspaces; index `(tally_company_guid)` non-unique.
- `gen_random_uuid()` needs PG ≥ 13 (this Mac: 16). The migration runs `CREATE EXTENSION IF NOT EXISTS pgcrypto`
  only if `gen_random_uuid` is missing. Downgrade never drops the extension.
- Downgrade drops the tables in `V2_TABLES` order and nothing else.

`env.py`:

```python
import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from v2.cloud.models import V2_TABLES, Base

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in V2_TABLES and not (getattr(obj, "info", {}) or {}).get("v2_readonly")
    return True


def _configure(**kw):
    context.configure(target_metadata=target_metadata, version_table="alembic_version_v2",
                      include_object=include_object, **kw)


async def _online(url: str) -> None:
    eng = create_async_engine(url)
    async with eng.connect() as conn:
        await conn.run_sync(lambda c: (_configure(connection=c), context.run_migrations()))
        await conn.commit()
    await eng.dispose()


url = context.config.attributes.get("url") or os.environ["V2_DATABASE_URL"]
if context.is_offline_mode():
    _configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_online(url))
```

`cli.migrate(url)` builds `alembic.config.Config(str(Path(__file__).parent / "alembic.ini"))`, sets
`cfg.attributes["url"] = url`, and calls `command.upgrade(cfg, "head")`. `downgrade` is the same with
`command.downgrade`. `__main__.py`: `python -m v2.cloud migrate [--url URL]` (default `V2Settings().database_url`).

- [ ] **Step 5: Run, verify pass**

Same command as Step 3 → all PASS. Then run the **whole v2 suite without** `TEST_DATABASE_URL` to prove the DB tests
skip cleanly: `uv run --project v2 pytest v2/tests/cloud -q` → only passes + skips.

- [ ] **Step 6: Commit**

```bash
git add v2/cloud/models v2/cloud/alembic.ini v2/cloud/alembic v2/cloud/cli.py v2/cloud/__main__.py \
        v2/tests/cloud/conftest.py v2/tests/cloud/db/__init__.py v2/tests/cloud/db/test_migration.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 3 — migration v2_001 (all S1 tables) on its own alembic_version_v2 chain + DB harness

Only CREATE TABLE/INDEX; users/workspaces reflected read-only; harness leaves the shared test DB as found.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** the reviewer diffs the migration against spec §4 column by column (the test's dict is the
contract), checks that no statement names `users`/`workspaces` except as an FK target, and runs the DB tests once.
Tracker: S1.0 ✅ (with Task 2), S1.1–S1.4 ✅ with proof `v2/tests/cloud/db/test_migration.py` + the log path.

---

## Task 4: Device auth — tokens, rate limits, login/refresh/logout, `/api/devices`, §8.1 dependency chain

**Spec:** §7.1–§7.4, §7.16, §8.1, §9.1, §9.2, §9.4, D6, D24, §11 (auth codes), §15.1 (auth columns).
**Tracker:** S1.5.

**Files:**
- Create: `v2/cloud/auth/device_tokens.py`, `v2/cloud/auth/rate_limit.py`, `v2/cloud/api/__init__.py`,
  `v2/cloud/api/dependencies.py`, `v2/cloud/api/agent_auth.py`, `v2/cloud/api/devices.py`
- Modify: `v2/cloud/main.py` (mount the routers; the limiters live on `app.state`)
- Test: `v2/tests/cloud/unit/test_tokens.py`, `v2/tests/cloud/unit/test_rate_limit.py`,
  `v2/tests/cloud/db/test_auth_api.py`

**Interfaces:**
- Produces:
  - `device_tokens.DeviceClaims(device_id: UUID, user_id: UUID, workspace_id: UUID | None, jti: str)`.
  - `mint_access(device_id, user_id, workspace_id, *, secret, minutes, now) -> str`, with claims `sub`, `uid`, `ws`,
    `typ="v2_device"`, `jti`, `iat`, `exp`.
  - `decode_access(token, *, secret, now) -> DeviceClaims`. It raises `ApiError(401, "token_expired")` /
    `ApiError(401, "token_invalid")` (bad signature, wrong `typ`, a web token).
  - `new_refresh() -> tuple[str, str]` (token, sha256 hex). `hash_refresh(token) -> str`.
  - `rate_limit.SlidingWindow(max_hits: int, window_s: int, clock)` with `.hit(key: str) -> None`, which raises
    `ApiError(429, "rate_limited", retry_after=int)`.
  - `dependencies.web_user(request) -> UUID` (the web JWT). `dependencies.any_device(request, session) ->
    AgentDevice` (checks 1–2 of §8.1 + per-device rate limit). `dependencies.active_device(ws: UUID, request,
    session) -> tuple[AgentDevice, SyncWorkspace]` (checks 1–6 in §8.1 order; on 4 it also revokes the device
    `workspace_deleted`).
  - Routes: `POST /api/agent/auth/login`, `POST /api/agent/auth/refresh`, `POST /api/agent/auth/logout`,
    `GET /api/agent/workspaces`, `GET /api/devices`, `DELETE /api/devices/{id}`.

- [ ] **Step 1: Unit tests** (`test_tokens.py`, `test_rate_limit.py`)

```python
# test_tokens.py
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from v2.cloud.auth.device_tokens import decode_access, hash_refresh, mint_access, new_refresh
from v2.cloud.errors import ApiError

S = "d" * 32
NOW = datetime(2026, 9, 25, 6, 30, tzinfo=timezone.utc)
D, U, W = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_access_round_trip_carries_ws():
    c = decode_access(mint_access(D, U, W, secret=S, minutes=15, now=NOW), secret=S, now=NOW)
    assert (c.device_id, c.user_id, c.workspace_id) == (D, U, W) and c.jti


def test_access_expires_after_15_minutes():
    tok = mint_access(D, U, None, secret=S, minutes=15, now=NOW)
    with pytest.raises(ApiError) as e:
        decode_access(tok, secret=S, now=NOW + timedelta(minutes=15, seconds=1))
    assert e.value.code == "token_expired"


@pytest.mark.parametrize("claims", [
    {"sub": str(U), "type": "access"},                            # a web access token
    {"sub": str(D), "uid": str(U), "ws": None, "typ": "v2_other"},
])
def test_wrong_typ_is_invalid(claims):
    claims = {**claims, "exp": int((NOW + timedelta(minutes=5)).timestamp()), "iat": int(NOW.timestamp())}
    with pytest.raises(ApiError) as e:
        decode_access(jwt.encode(claims, S, algorithm="HS256"), secret=S, now=NOW)
    assert e.value.code == "token_invalid"


def test_web_secret_cannot_sign_a_device_token():
    tok = mint_access(D, U, W, secret="w" * 32, minutes=15, now=NOW)
    with pytest.raises(ApiError) as e:
        decode_access(tok, secret=S, now=NOW)
    assert e.value.code == "token_invalid"


def test_refresh_is_random_and_only_its_hash_is_kept():
    (t1, h1), (t2, h2) = new_refresh(), new_refresh()
    assert t1 != t2 and h1 == hash_refresh(t1) and len(h1) == 64 and t1 not in h1
```

```python
# test_rate_limit.py
from datetime import datetime, timezone

import pytest

from v2.cloud.auth.rate_limit import SlidingWindow
from v2.cloud.clock import FixedClock
from v2.cloud.errors import ApiError


def test_limit_then_retry_after_then_recovers():
    clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
    lim = SlidingWindow(3, 60, clock)
    for _ in range(3):
        lim.hit("k")
    with pytest.raises(ApiError) as e:
        lim.hit("k")
    assert e.value.status == 429 and e.value.extra["retry_after"] == 60
    clock.advance(seconds=61)
    lim.hit("k")


def test_keys_are_independent():
    lim = SlidingWindow(1, 60, FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc)))
    lim.hit("a")
    lim.hit("b")
```

- [ ] **Step 2: Run → FAIL; implement `device_tokens.py`, `rate_limit.py`; run → PASS**

`uv run --project v2 pytest v2/tests/cloud/unit/test_tokens.py v2/tests/cloud/unit/test_rate_limit.py -q`

`decode_access` uses `jwt.decode(..., options={"verify_exp": False})` and then compares `exp` against the injected
`now` itself, so tests can use `FixedClock`.

- [ ] **Step 3: Write the failing API tests** (`v2/tests/cloud/db/test_auth_api.py`)

These are the named tests (each asserts status **and** the JSON `error` code **and**, where state changes, re-reads
the row in a fresh session):
- `test_login_creates_unbound_device_and_returns_tokens`: 200; `device_id`, `expires_in == 900`; the DB row has
  `workspace_id IS NULL`, `refresh_hash == sha256(refresh_token)`, `last_login_at == clock.now()`.
- `test_login_wrong_password_401_invalid_credentials`
- `test_login_inactive_user_403_account_inactive`
- `test_login_rate_limited_after_5_attempts_per_email` → the 6th is 429 `rate_limited` with a `Retry-After` header.
- `test_refresh_rotates_and_old_token_becomes_reuse_signal`: refresh r1 → r2 (200, a new access token). Presenting
  r1 again → 401 `device_revoked`; row `revoke_reason == "refresh_reuse"`; r2 is now also refused.
- `test_refresh_expired_401_refresh_expired`: clock advanced 90 days + 1 s.
- `test_refresh_sliding_window_extends_expiry`: refresh at day 89 → new expiry = day 89 + 90.
- `test_logout_revokes_calling_device_204`: the next call → 401 `device_revoked` with `reason: "logout"`.
- `test_agent_workspaces_lists_live_only_with_binding`: two workspaces, one deleted → one row;
  `bound_company_guid is None`, `active_device is None`.
- `test_web_token_rejected_on_device_endpoint` → 401 `token_invalid`.
- `test_device_token_rejected_on_web_endpoint` (`GET /api/devices`) → 401 `token_invalid`.
- `test_devices_list_and_delete_revokes_user_removed`: web GET lists; DELETE → 204; row `revoke_reason ==
  "user_removed"`; the device's next call → 401 `device_revoked`.
- `test_delete_other_users_device_404`
- `test_per_device_rate_limit_429`: with `settings.device_rate_max = 3`, the 4th `GET /api/agent/workspaces` → 429.

Helper used across later API tests (put it in `conftest.py`):

```python
async def login_device(client, session, *, email=None, password="Passw0rd!Passw0rd", device_name="ACCOUNTS-PC"):
    uid = await make_user(session, email=email, password=password)
    email = (await session.execute(text("SELECT email FROM users WHERE id=:i"), {"i": uid})).scalar_one()
    r = await client.post("/api/agent/auth/login", json={"email": email, "password": password,
                                                        "device_name": device_name, "agent_version": "0.1.0"})
    assert r.status_code == 200, r.text
    body = r.json()
    return uid, body, {"Authorization": f"Bearer {body['access_token']}"}


def web_headers(user_id, secret="w" * 32):
    tok = jwt.encode({"sub": str(user_id), "type": "access", "exp": 4102444800}, secret, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}
```

- [ ] **Step 4: Run → FAIL; implement the routes + dependencies; run → PASS**

Run: `TEST_DATABASE_URL=… uv run --project v2 pytest v2/tests/cloud/db/test_auth_api.py -q 2>&1 | tee logs/v2-s1-db-task4.log`

Implementation notes:
- The login limiter is keyed by lower-cased email, and **every** attempt counts (the copied semantics). The device
  limiter is keyed by device id, checked in `any_device`/`active_device` after check 2.
- Refresh: look up by `refresh_hash`; if not found, look up by `refresh_prev_hash`. A hit there → set `revoked_at`,
  `revoke_reason="refresh_reuse"`, `is_active=false`; emit `v2.ops.integrity`-style log `{event:
  "refresh_reuse", device_id}` (no user data); → 401 `device_revoked`. Otherwise rotate: `refresh_prev_hash = old`,
  `refresh_hash = new`, `refresh_expires_at = now + 90 d`.
- A revoked device's 401 body: `{"error": "device_revoked", "detail": "...", "reason": revoke_reason}`.
- Refresh/logout for a device bound to a deleted workspace → 410 `workspace_deleted` (§15.1 row 1).

- [ ] **Step 5: Commit**

```bash
git add v2/cloud/auth v2/cloud/api v2/cloud/main.py v2/tests/cloud/unit/test_tokens.py \
        v2/tests/cloud/unit/test_rate_limit.py v2/tests/cloud/db/test_auth_api.py v2/tests/cloud/conftest.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 4 — device auth (separate-secret JWT, rotating refresh with reuse revoke), devices API

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** the reviewer checks the §8.1 order in `active_device`, confirms that no plain refresh token
is stored, and that request bodies are not logged. Tracker: S1.5 ✅ (proof: the three test files + log).

---

## Task 5: Binding — `POST /api/sync/company`, one active device, take-over, re-bind-when-empty

**Spec:** §7.5 (+ ambiguity A6 order), §9.3, D5, D7, D14 (GUID only here), Q4, Q30, §8.3 (coverage rows at bind),
§14 scenarios 1–2, §15.1 row 3. **Tracker:** S1.6, S1.7.

**Files:**
- Create: `v2/cloud/sync/__init__.py`, `v2/cloud/sync/binding.py`, `v2/cloud/sync/coverage.py` (only
  `fy_rows_for_bind` in this task; the rest is Task 7), `v2/cloud/api/sync.py` (router with `/api/sync/company`)
- Test: `v2/tests/cloud/unit/test_coverage.py` (bind part), `v2/tests/cloud/db/test_binding_api.py`

**Interfaces:**
- Consumes: `active_device`/`any_device` (Task 4), `mint_access` (Task 4), `current_fy_start`/`ist_date` (Task 2).
- Produces:
  - `coverage.months_between(start: date, end: date) -> int` (inclusive calendar months).
  - `coverage.fy_rows_for_bind(books_from: date, today_ist: date, first_sync_month: date | None) ->
    list[FyRow]`, where `FyRow(fy_start, fy_end, months_total)`. It covers FY(books_from)…FY(today). Window FYs
    (current + previous) get `months_total` = months from `max(fy_start, books_from)` to `min(fy_end,
    first_sync_month or today)`. Older FYs run to `fy_end`.
  - `binding.bind(session, device, body: BindRequest, settings, clock) -> dict`, the response body incl. a fresh
    `access_token` carrying `ws`.

- [ ] **Step 1: Unit tests for coverage-at-bind** (`v2/tests/cloud/unit/test_coverage.py`)

```python
from datetime import date

from v2.cloud.sync.coverage import FyRow, fy_rows_for_bind, months_between


def test_months_between_inclusive():
    assert months_between(date(2022, 4, 1), date(2023, 3, 31)) == 12
    assert months_between(date(2025, 4, 1), date(2025, 9, 25)) == 6


def test_company_b_rows_from_books_from_to_current_fy():
    rows = fy_rows_for_bind(date(2022, 4, 1), date(2026, 9, 25), None)
    assert [r.fy_start for r in rows] == [date(2022, 4, 1), date(2023, 4, 1), date(2024, 4, 1),
                                          date(2025, 4, 1), date(2026, 4, 1)]
    assert rows[-1].months_total == 6 and rows[-2].months_total == 12 and rows[0].months_total == 12


def test_books_from_mid_year_counts_from_books_from():
    rows = fy_rows_for_bind(date(2025, 10, 1), date(2026, 9, 25), None)
    assert rows[0] == FyRow(date(2025, 4, 1), date(2026, 3, 31), 6)


def test_current_fy_uses_ist_date_at_utc_evening_of_31_march():       # Review Focus 4
    from datetime import datetime, timezone
    from v2.cloud.clock import FixedClock, ist_date
    today = ist_date(FixedClock(datetime(2026, 3, 31, 20, 0, tzinfo=timezone.utc)).now())
    rows = fy_rows_for_bind(date(2025, 4, 1), today, None)
    assert rows[-1].fy_start == date(2026, 4, 1) and rows[-1].months_total == 1
```

- [ ] **Step 2: Run → FAIL; implement `months_between`, `FyRow`, `fy_rows_for_bind`; run → PASS**

- [ ] **Step 3: Failing API tests** (`v2/tests/cloud/db/test_binding_api.py`). Every test re-reads
`sync_workspaces`, `agent_devices` and `sync_fy_coverage` in a **fresh session** after the call (§14 "each re-reads
from a fresh session").

- `test_bind_unbound_workspace_creates_state_coverage_and_activates_device` (§14.1): 200 `{"bound": true,
  "sync_state": "awaiting_first_connection"}`, 5 coverage rows `pending` for company B's `books_from 20220401`; the
  device `is_active`, `workspace_id` set; the returned `access_token` decodes with `ws == workspace_id`.
- `test_rebind_same_guid_same_device_is_noop` (§14.1): same response; the row count doesn't change; `bound_at`
  doesn't change.
- `test_other_active_device_without_takeover_409` → 409 `takeover_required`, `active_device.device_name ==
  "ACCOUNTS-PC"`.
- `test_takeover_with_fresh_login_revokes_old` (§14.2): the old device has `revoked_at`, `revoke_reason ==
  "taken_over"`, `is_active false`; the new one is active; the response has `cursors` and `coverage`.
- `test_takeover_with_login_older_than_10_minutes_401_reauth_required`: `clock.advance(minutes=11)` before the bind.
- `test_partial_unique_index_rejects_second_active_row` (§14.2): a raw SQL `UPDATE` setting a second device active
  raises `IntegrityError`.
- `test_concurrent_takeovers_leave_exactly_one_active` (§16): two devices bind with `takeover=true` at once via
  `asyncio.gather`; exactly one `is_active`, and the other is revoked or got 409.
- `test_workspace_of_other_user_404_workspace_not_found`, `test_deleted_workspace_410_workspace_deleted`.
- `test_company_bound_elsewhere_409_names_the_workspace` (A6): the user's W1 is bound to GUID g; binding W2 to g →
  409 `company_bound_elsewhere` `{"workspace_id": W1}`, **even though W2 is unbound**.
- `test_rebind_different_guid_allowed_while_no_batch_accepted`: coverage is re-created from the new `books_from`,
  and the name/GUID are replaced.
- `test_rebind_different_guid_refused_once_data_exists_409`: insert one `sync_batches` row with `status='accepted'`
  directly → 409 `workspace_bound_to_other_company`.
- `test_bind_never_writes_workspaces_table`: a snapshot of the `workspaces` rows (all columns) before and after → equal.

- [ ] **Step 4: Run → FAIL; implement `binding.bind` and the route; run → PASS**

```bash
TEST_DATABASE_URL=postgresql+asyncpg://nuvanta-mac-3@localhost/tallyagent_test \
  uv run --project v2 pytest v2/tests/cloud/unit/test_coverage.py v2/tests/cloud/db/test_binding_api.py -q \
  2>&1 | tee logs/v2-s1-db-task5.log
```

Transaction outline (one transaction, the order is A6):

```python
async def bind(session, device, body, settings, clock):
    ws = await _load_workspace(session, body.workspace_id)          # workspaces_table, read-only
    if ws is None or ws.user_id != device.user_id: raise ApiError(404, "workspace_not_found")
    if ws.is_deleted: raise ApiError(410, "workspace_deleted")
    elsewhere = await _bound_elsewhere(session, device.user_id, body.company_guid, body.workspace_id)
    if elsewhere: raise ApiError(409, "company_bound_elsewhere", workspace_id=str(elsewhere))
    sw = await session.get(SyncWorkspace, body.workspace_id, with_for_update=True)
    if sw is None:
        sw = await _create_binding(session, device, body, clock)            # + coverage rows, device active
    elif sw.tally_company_guid != body.company_guid:
        if await _has_accepted_batch(session, sw.workspace_id):
            raise ApiError(409, "workspace_bound_to_other_company")
        await _rebind(session, sw, body, clock)                              # coverage recreated
        await _activate(session, sw, device, clock)
    else:
        await _same_guid(session, sw, device, body.takeover, settings, clock)  # no-op / 409 / 401 / take-over
    await session.commit()
    return {"bound": True, "sync_state": sw.sync_state, "cursors": _cursors(sw),
            "coverage": await _coverage_json(session, sw.workspace_id),
            "access_token": mint_access(device.id, device.user_id, sw.workspace_id,
                                        secret=settings.device_token_secret,
                                        minutes=settings.device_access_minutes, now=clock.now())}
```

`_activate` first sets the previous active device `is_active=false, revoked_at=now, revoke_reason='taken_over'`,
**flushes**, and then sets the new one active (the partial unique index would otherwise fire inside the
transaction).

- [ ] **Step 5: Commit**

```bash
git add v2/cloud/sync v2/cloud/api/sync.py v2/cloud/main.py v2/tests/cloud/unit/test_coverage.py \
        v2/tests/cloud/db/test_binding_api.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 5 — bind to an existing workspace, one active device, take-over, re-bind while empty

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** D5 (no `workspaces` write, proven by a test), A6 ordering, the race test is real
concurrency (two sessions), IST date for "current FY". Tracker: S1.6 ✅, S1.7 ✅.

---

## Task 6: Heartbeat, `/state`, commands, restore detection, re-link, `sync-status`

**Spec:** §7.6, §7.7, §7.13, §7.15, §8.2, §8.4, §8.6, D16, D17, D20 (hook only), D21, Q1, Q25, §14 scenario 14 (the
restore half), §15.1 rows 4–5. **Tracker:** S1.16, S1.17 (cursor exposure), S1.18, S1.19.

**Files:**
- Create: `v2/cloud/sync/state.py`, `v2/cloud/sync/commands.py`, `v2/cloud/sync/maintenance.py` (in this task
  `run_slice` only records that it ran, `SliceReport(ran=True, purged=0)`; Task 11 fills the slices),
  `v2/cloud/api/web_sync.py`
- Modify: `v2/cloud/api/sync.py` (heartbeat, state, relink routes), `v2/cloud/main.py`
- Test: `v2/tests/cloud/db/test_heartbeat_api.py`

**Interfaces:**
- Produces:
  - `state.TRANSITIONS: dict[tuple[str, str], str]` (the §8.2 table, keyed `(from_state, event)`).
    `state.transition(sw, event: str) -> None` raises `ValueError` for an undefined pair, except that the "any" rows
    are expanded explicitly.
  - `state.heartbeat(session, sw, device, body: HeartbeatRequest, settings, clock) -> dict`.
  - `state.detect_restore(sw, counters: dict) -> bool` (pure).
  - `commands.enqueue(session, ws_id, type_, params, requested_by) -> SyncCommand`;
    `commands.deliver_pending(session, ws_id, clock) -> list[dict]` (pending → delivered);
    `commands.ack(session, ws_id, ids, clock)` (delivered → done).
  - `state.apply_relink(session, sw, new_guid, name, clock) -> None` (shared by the device and web paths).
  - `state.sync_status(session, sw) -> dict` (the §7.13 shape; `suspect` is reported as `ok`).
  - `maintenance.run_slice(session, sw, settings, clock) -> SliceReport`.

- [ ] **Step 1: Failing tests** (`v2/tests/cloud/db/test_heartbeat_api.py`)

- `test_heartbeat_sets_last_seen_and_clock_skew_but_not_last_synced`: `pc_clock` 3 s ahead → `clock_skew_s == 3`;
  `last_synced_at` stays NULL.
- `test_heartbeat_counters_equal_cursors_sets_caught_up_at` (D21).
- `test_heartbeat_counters_below_cursors_restore_detected` (§8.6, probe 13 values): set cursors `alt_vch_id` 50 /
  `alt_mst_id` 265 from `p13_A_before_backup_counters.xml` (via `transcode.counters_from_xml`), then heartbeat with
  the counters from `p13_A_after_restore_counters.xml` → `sync_state == "restore_detected"`, `restore_reason ==
  "counters_backwards"`, `ladder.resync_offered == {"scope": "company", "reason": "restore"}`.
- `test_heartbeat_not_ours_never_triggers_restore`: `tally_status: "other_company"` with low counters → the state is
  unchanged.
- `test_no_company_status_stored_for_secured_prompt`: body `tally_status: "no_company"` →
  `last_heartbeat.tally_status == "no_company"` (fixture justification in the docstring:
  `p24_C_security_login_pending_company_list.xml` is an empty list).
- `test_other_company_same_name_sets_relink_prompt`.
- `test_commands_delivered_once_then_acked_done`: web `POST /sync/commands {"type": "recheck_now"}` → the next
  heartbeat returns it (`delivered`); the following heartbeat returns `[]`; a heartbeat with `acked_commands: [id]` →
  `done`.
- `test_relink_requires_prompt_and_password`: without a prompt → 409 `relink_not_prompted`; wrong password → 401
  `invalid_credentials`; OK → GUID replaced, the old one is in `previous_company_guids`, `sync_state ==
  "restore_detected"`, `restore_reason == "relink"`.
- `test_web_confirm_relink_same_service_as_device`.
- `test_state_endpoint_returns_cursors_coverage_open_runs_commands`.
- `test_sync_status_shape_and_suspect_reported_as_ok`: set `last_parity.state = "suspect"` → the response has `"ok"`;
  all §7.13 keys are present; `first_sync` is `null` unless the state is `first_sync`.
- `test_sync_status_404_for_non_owner`.
- `test_heartbeat_runs_one_maintenance_slice` (the hook): monkeypatch `maintenance.run_slice` to count calls → 1.
- §15.1 row 4 cells: `test_sync_endpoint_matrix_row` parametrized over `(expired, revoked, unbound, wrong_ws,
  deleted_ws, not_active, web_jwt)` → `(401 token_expired, 401 device_revoked, 403 wrong_workspace, 403
  wrong_workspace, 410 workspace_deleted, 409 not_active_device, 401 token_invalid)` on `POST .../heartbeat`.

- [ ] **Step 2: Run → FAIL; implement; run → PASS**

`TEST_DATABASE_URL=… uv run --project v2 pytest v2/tests/cloud/db/test_heartbeat_api.py -q 2>&1 | tee logs/v2-s1-db-task6.log`

`TRANSITIONS` literal (the §8.2 table; the "any" rows are written out for each state):

```python
STATES = ("awaiting_first_connection", "first_sync", "ready", "error", "restore_detected")
TRANSITIONS = {
    ("awaiting_first_connection", "first_sync_opened"): "first_sync",
    ("first_sync", "first_sync_completed_window_complete"): "ready",
    ("first_sync", "fatal"): "error",
    ("error", "run_completed_window_complete"): "ready",
    ("error", "run_completed_window_incomplete"): "first_sync",
    ("ready", "counters_backwards"): "restore_detected",
    ("error", "counters_backwards"): "restore_detected",
    ("restore_detected", "company_resync_completed"): "ready",
    **{(s, "relink"): "restore_detected" for s in STATES},
}
```

- [ ] **Step 3: Commit**

```bash
git add v2/cloud/sync/state.py v2/cloud/sync/commands.py v2/cloud/sync/maintenance.py v2/cloud/api \
        v2/cloud/main.py v2/tests/cloud/db/test_heartbeat_api.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 6 — heartbeat, state, server->agent commands, restore detection, re-link, sync-status

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** a heartbeat never moves `last_synced_at`; `restore_detected` only on `ours`; relink always
needs the password. Tracker: S1.16 ✅, S1.18 ✅, S1.19 ✅, S1.17 🟡 (cursor move is Task 7).

---
## Task 7: Runs + cursors (D15, D16) + coverage + both edges + backfill copy

**Spec:** §7.8, §7.10, §8.2 (run-driven transitions), §8.3, §15.3, §15.4 (ambiguity A10), D15, D16, §14 scenarios
3, 12, 13, 14 (the resync half). **Tracker:** S1.8, S1.13, S1.17.

**Files:**
- Create: `v2/cloud/sync/runs.py`
- Modify: `v2/cloud/sync/coverage.py` (transitions, edges, backfill), `v2/cloud/api/sync.py` (runs + coverage routes)
- Test: `v2/tests/cloud/unit/test_coverage.py` (extend), `v2/tests/cloud/db/test_runs_api.py`

**Interfaces:**
- Produces (pure, `coverage.py`):
  - `@dataclass class Cov: fy_start: date; state: str; months_done: list[str]; months_total: int`
  - `COVERAGE_MATRIX: dict[tuple[str, str], str]`
  - `apply(cov: Cov, event: str, month: str | None = None) -> Cov`. Events: `month_ack`, `company_resync_start`,
    `fy_resync_start`, `add_fy`. `last_month_ack` / `ack_replay` are derived from the data.
  - `edges(rows: list[Cov], current_fy: date) -> tuple[date | None, date | None]` (available, verified).
  - `backfill(rows, books_from: date, current_fy: date) -> tuple[str, Decimal]` (state, percent).
- Produces (`runs.py`): `open_run(session, sw, device, body: RunCreate, clock) -> SyncRun`,
  `patch_run(session, sw, device, run_id, body: RunPatch, clock) -> SyncRun`, `FATAL_RUN_CODES`.
- Consumed by Task 8: `runs.require_open_run(session, sw, device, run_id) -> SyncRun` (409 `run_closed` if not
  running; 403 `wrong_workspace` if it's another device's run).

- [ ] **Step 1: Failing unit tests** (append to `test_coverage.py`)

```python
import pytest
from datetime import date
from decimal import Decimal

from v2.cloud.sync.coverage import COVERAGE_MATRIX, Cov, apply, backfill, edges

# (from_state, event) -> to_state; 4 states x 6 events = 24 cells (ambiguity A10)
EXPECTED = {
    ("pending", "month_ack"): "running", ("pending", "last_month_ack"): "complete",
    ("pending", "company_resync_start"): "pending", ("pending", "fy_resync_start"): "pending",
    ("pending", "add_fy"): "pending", ("pending", "ack_replay"): "running",
    ("running", "month_ack"): "running", ("running", "last_month_ack"): "complete",
    ("running", "company_resync_start"): "pending", ("running", "fy_resync_start"): "pending",
    ("running", "add_fy"): "running", ("running", "ack_replay"): "running",
    ("resyncing", "month_ack"): "resyncing", ("resyncing", "last_month_ack"): "complete",
    ("resyncing", "company_resync_start"): "resyncing", ("resyncing", "fy_resync_start"): "resyncing",
    ("resyncing", "add_fy"): "resyncing", ("resyncing", "ack_replay"): "resyncing",
    ("complete", "month_ack"): "complete", ("complete", "last_month_ack"): "complete",
    ("complete", "company_resync_start"): "resyncing", ("complete", "fy_resync_start"): "resyncing",
    ("complete", "add_fy"): "complete", ("complete", "ack_replay"): "complete",
}


def test_matrix_is_the_spec_table():
    assert COVERAGE_MATRIX == EXPECTED


def _row(state, done, total=3):
    return Cov(date(2024, 4, 1), state, list(done), total)


@pytest.mark.parametrize("cell", sorted(EXPECTED))
def test_every_cell(cell):
    state, event = cell
    done = {"pending": [], "running": ["2024-04"], "resyncing": ["2024-04"],
            "complete": ["2024-04", "2024-05", "2024-06"]}[state]
    if event == "month_ack":
        out = apply(_row(state, done), "month_ack", "2024-05")
    elif event == "last_month_ack":
        out = apply(_row(state, done, total=len(done) + 1), "month_ack", "2024-07")
    elif event == "ack_replay":
        out = apply(_row(state, done), "month_ack", done[0] if done else "2024-04")
    else:
        out = apply(_row(state, done), event)
    assert out.state == EXPECTED[cell]
    if event in ("company_resync_start", "fy_resync_start") and state != "pending":
        assert out.months_done == []


def test_replay_counts_once():                                        # §14 scenario 12
    r = apply(apply(_row("running", ["2024-04"]), "month_ack", "2024-05"), "month_ack", "2024-05")
    assert r.months_done == ["2024-04", "2024-05"]


def test_edges_available_counts_resyncing_verified_does_not():        # §14 scenario 13
    rows = [Cov(date(2023, 4, 1), "complete", [], 12), Cov(date(2024, 4, 1), "resyncing", [], 12),
            Cov(date(2025, 4, 1), "complete", [], 12), Cov(date(2026, 4, 1), "complete", [], 6)]
    assert edges(rows, date(2026, 4, 1)) == (date(2023, 4, 1), date(2025, 4, 1))


def test_edges_stop_at_first_gap():
    rows = [Cov(date(2023, 4, 1), "complete", [], 12), Cov(date(2024, 4, 1), "pending", [], 12),
            Cov(date(2025, 4, 1), "complete", [], 12), Cov(date(2026, 4, 1), "complete", [], 6)]
    assert edges(rows, date(2026, 4, 1)) == (date(2025, 4, 1), date(2025, 4, 1))


def test_edges_none_when_current_fy_not_complete():
    rows = [Cov(date(2026, 4, 1), "running", ["2026-04"], 6)]
    assert edges(rows, date(2026, 4, 1)) == (None, None)


def test_backfill_young_company_is_100_complete():
    rows = [Cov(date(2025, 4, 1), "complete", [], 12), Cov(date(2026, 4, 1), "complete", [], 6)]
    assert backfill(rows, date(2025, 4, 1), date(2026, 4, 1)) == ("complete", Decimal("100.00"))


def test_backfill_percent_over_pre_window_fys():
    rows = [Cov(date(2022, 4, 1), "pending", [], 12),
            Cov(date(2023, 4, 1), "running", ["2023-04", "2023-05", "2023-06"], 12),
            Cov(date(2024, 4, 1), "pending", [], 12),
            Cov(date(2025, 4, 1), "complete", [], 12), Cov(date(2026, 4, 1), "complete", [], 6)]
    state, pct = backfill(rows, date(2022, 4, 1), date(2026, 4, 1))
    assert state == "running" and pct == Decimal("8.33")         # 3 / 36 pre-window months
```

- [ ] **Step 2: Run → FAIL; implement; run → PASS**

```python
COVERAGE_MATRIX = {**{(s, e): t for (s, e), t in {
    ("pending", "month_ack"): "running", ("pending", "last_month_ack"): "complete",
    ("pending", "ack_replay"): "running",
    ("running", "month_ack"): "running", ("running", "last_month_ack"): "complete", ("running", "ack_replay"): "running",
    ("resyncing", "month_ack"): "resyncing", ("resyncing", "last_month_ack"): "complete",
    ("resyncing", "ack_replay"): "resyncing",
    ("complete", "month_ack"): "complete", ("complete", "last_month_ack"): "complete",
    ("complete", "ack_replay"): "complete",
}.items()},
    ("pending", "company_resync_start"): "pending", ("running", "company_resync_start"): "pending",
    ("resyncing", "company_resync_start"): "resyncing", ("complete", "company_resync_start"): "resyncing",
    ("pending", "fy_resync_start"): "pending", ("running", "fy_resync_start"): "pending",
    ("resyncing", "fy_resync_start"): "resyncing", ("complete", "fy_resync_start"): "resyncing",
    **{(s, "add_fy"): s for s in ("pending", "running", "resyncing", "complete")},
}


def apply(cov: Cov, event: str, month: str | None = None) -> Cov:
    if event == "month_ack":
        replay = month in cov.months_done
        done = sorted(set(cov.months_done) | {month})
        last = not replay and len(done) >= cov.months_total
        key = "ack_replay" if replay and cov.state != "pending" else ("last_month_ack" if last else "month_ack")
        return Cov(cov.fy_start, COVERAGE_MATRIX[(cov.state, key)], done, cov.months_total)
    new_state = COVERAGE_MATRIX[(cov.state, event)]
    done = [] if event in ("company_resync_start", "fy_resync_start") else list(cov.months_done)
    return Cov(cov.fy_start, new_state, done, cov.months_total)
```

The edges walk from the current FY backwards over contiguous FY starts. The available edge accepts `complete |
resyncing`, the verified edge `complete` only, and each stops at the first row that fails. `backfill`: pre-window =
FYs older than the previous FY; `resyncing` if any pre-window row is `resyncing`; `complete` when the verified edge
== FY(books_from); percent = `Σ len(months_done) / Σ months_total` over the pre-window rows, quantised to 0.01 (100
when there are none).

- [ ] **Step 3: Failing API tests** (`v2/tests/cloud/db/test_runs_api.py`)

- `test_first_sync_run_opens_and_moves_state_to_first_sync`.
- `test_second_first_sync_returns_the_open_one_with_coverage` (resume).
- `test_first_sync_refused_outside_awaiting_or_first_sync` → 409 `run_kind_not_allowed`.
- `test_full_resync_without_confirmed_command_409_resync_not_confirmed`, and
  `test_full_resync_with_pending_resync_command_allowed`.
- `test_incremental_refused_in_restore_detected_409_restore_detected`.
- `test_complete_run_with_missing_batches_409_batches_missing`: `batches_declared=3`, 2 accepted → `{"missing": 1}`.
- `test_first_sync_scenario_24_months_to_ready` (§14 scenario 3): company B bound, `today` 2026-09-25: window FYs
  2025-26 (12) + 2026-27 (6); first sync opened with `counters_at_start {965, 412}`; ack all 18 window months with
  `PATCH /coverage`; complete the run with `batches_declared=0` → `sync_state == "ready"`, cursors `(965, 412)`,
  run `completed`. Re-read everything from a fresh session. (§14 says "24 months" for a September start the window
  is 18; the test computes `months_total` from `fy_rows_for_bind` rather than hard-coding 24, and says so in its
  docstring.)
- `test_incremental_completion_sets_cursor_after` / `test_backfill_completion_leaves_cursor` /
  `test_single_fy_resync_leaves_cursor` / `test_company_resync_sets_counters_at_start_and_ready` (§15.3, one per
  row).
- `test_coverage_patch_idempotent_and_returns_edges` (§14.12).
- `test_company_resync_start_moves_complete_to_resyncing_and_running_to_pending` (§14.13), driven through
  `POST /runs {kind: full_resync, scope: {company: true}, command_id}`.
- `test_add_fy_creates_complete_row`.
- `test_failed_first_sync_with_fatal_code_sets_error` (A8) and
  `test_failed_first_sync_with_other_code_keeps_first_sync`.

- [ ] **Step 4: Run → FAIL; implement `runs.py` + the routes; run → PASS**

`TEST_DATABASE_URL=… uv run --project v2 pytest v2/tests/cloud/unit/test_coverage.py v2/tests/cloud/db/test_runs_api.py -q 2>&1 | tee logs/v2-s1-db-task7.log`

The cursor rule on `PATCH status=completed` (§15.3):

```python
def cursor_on_completion(run: SyncRun, body: RunPatch) -> dict | None:
    if run.kind == "incremental":
        return body.cursor_after
    if run.kind == "first_sync" or (run.kind == "full_resync" and (run.scope or {}).get("company")):
        return run.counters_at_start
    return None            # backfill, single-FY full_resync: unchanged
```

- [ ] **Step 5: Commit**

```bash
git add v2/cloud/sync/runs.py v2/cloud/sync/coverage.py v2/cloud/api/sync.py \
        v2/tests/cloud/unit/test_coverage.py v2/tests/cloud/db/test_runs_api.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 7 — runs with server-side cursors (D15/D16), FY coverage matrix, both edges, backfill copy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** all 24 matrix cells; cursors never move on a batch; `full_resync` needs a command.
Tracker: S1.8 ✅, S1.13 ✅, S1.17 ✅.

---

## Task 8a: Ingest — parse + validate wire objects, rung 0 (pure)

**Spec:** §5.3, §11 (per-object codes), §12 steps 5 and 7, D3, D12 (code classes), D23, LESSONS rule 18.
**Tracker:** S1.11 (rung 0).

**Files:**
- Create: `v2/cloud/ingest/__init__.py`, `v2/cloud/ingest/parsed.py`, `v2/cloud/ingest/validate.py`,
  `v2/cloud/parity/__init__.py`, `v2/cloud/parity/rung0.py`
- Test: `v2/tests/cloud/unit/test_validate.py`, `v2/tests/cloud/unit/test_rung0.py`

**Interfaces:**
- Produces (`parsed.py`, frozen dataclasses):
  - `PMaster(index: int, kind: str, guid: str, alter_id: int, name: str, parent: str | None, fields: dict[str,
    Any], raw: dict)`. `fields` holds the parsed optional values: `Amount` for balances, `bool` for logicals, `str`
    for texts.
  - `PBalance(index, kind, guid, name, captured_at: datetime, closing: Amount | None, opening: Amount | None,
    qty: Decimal | None, qty_text: str, raw)`.
  - `PBill(name, bill_type: str | None, amount: Amount | None, credit_days: int | None, credit_text: str,
    bill_date: date | None)`.
  - `PLine(line_no, ledger_name, amount: Amount, is_deemed_positive: bool, ledger_guid_hint: str | None, bills:
    list[PBill])`.
  - `PInv(line_no, stock_item_name, amount: Amount, actual_qty, billed_qty, qty_text, rate, rate_text,
    is_deemed_positive: bool | None)`.
  - `PVoucher(index, guid, master_id, alter_id, date, effective_date, voucher_type_name, voucher_number,
    reference, party_ledger_name, narration, is_cancelled, is_optional, is_post_dated, is_invoice, lines:
    list[PLine], inventory: list[PInv], has_forex: bool, raw: dict)`.
  - `ObjectError(index: int, kind: str, guid: str | None, code: str, detail: str)`. `detail` names **fields**,
    never values (decision 14).
  - `ObjectWarning(index: int, code: str)`.
- Produces (`validate.py`): `parse_objects(objects: list[dict]) -> tuple[list[PMaster | PBalance | PVoucher],
  list[ObjectError], list[ObjectWarning]]`. It collects every error and doesn't stop at the first.
- Produces (`rung0.py`): `voucher_balances(v: PVoucher) -> bool` (Σ `line.amount.inr` == `Decimal("0.00")`
  **exactly**).

- [ ] **Step 1: Failing tests** (`test_validate.py`)

The test module builds its inputs from real captures through the transcoder, then mutates **copies** for the
negative cases:

```python
import copy
from decimal import Decimal
from pathlib import Path

import pytest

from v2.cloud.ingest.validate import parse_objects
from v2.cloud.parity.rung0 import voucher_balances
from v2.contract import transcode

SYNC = Path(__file__).resolve().parents[2] / "fixtures" / "sync"


def _vouchers(name):
    return transcode.vouchers_from_xml((SYNC / name).read_text(encoding="utf-8"))


def _codes(errors):
    return sorted({(e.index, e.code) for e in errors})


def test_all_b_month_09_vouchers_parse_and_balance():
    parsed, errors, _ = parse_objects(_vouchers("p21_B_fy2022_month_09.xml"))
    assert errors == [] and all(voucher_balances(v) for v in parsed)


def test_usd_sale_lines_carry_fx_and_stated_base():
    parsed, errors, _ = parse_objects(_vouchers("p22_B_forex_sales.xml"))
    usd = next(v for v in parsed if v.guid.endswith("-000003c1"))
    assert usd.has_forex and [(l.amount.inr, l.amount.fx_amount) for l in usd.lines] == [
        (Decimal("-37216.04"), Decimal("-448.44")), (Decimal("37216.04"), Decimal("448.44"))]


def test_cancelled_voucher_without_lines_or_party_is_valid():
    parsed, errors, _ = parse_objects(_vouchers("p03_B_flagged_month_2023_02.xml"))
    assert errors == []
    cancelled = [v for v in parsed if v.is_cancelled]
    assert cancelled and all(v.lines == [] and v.party_ledger_name == "" for v in cancelled)


def test_missing_required_key_is_missing_field():
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:2])
    del objs[1]["data"]["alterid"]
    _, errors, _ = parse_objects(objs)
    assert _codes(errors) == [(1, "missing_field")] and "alterid" in errors[0].detail


def test_unbalanced_voucher_by_one_paisa():
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledger_entries"][0]["amount"] = "-16538.67"
    _, errors, _ = parse_objects(objs)
    assert _codes(errors) == [(0, "unbalanced_voucher")]


def test_forex_line_without_base_is_forex_base_missing():
    objs = copy.deepcopy([v for v in _vouchers("p22_B_forex_sales.xml") if v["data"]["guid"].endswith("-000003c1")])
    objs[0]["data"]["ledger_entries"][0]["amount"] = "-$448.44 @ ? 82.99/$"
    _, errors, _ = parse_objects(objs)
    assert (0, "forex_base_missing") in _codes(errors)


@pytest.mark.parametrize("field, value, code", [
    ("date", "2022-13-40", "invalid_date"), ("iscancelled", "Maybe", "invalid_logical"),
])
def test_bad_header_values(field, value, code):
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"][field] = value
    _, errors, _ = parse_objects(objs)
    assert (0, code) in _codes(errors)


def test_bad_line_amount_is_unparseable_amount():
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledger_entries"][1]["amount"] = "14,0l5.82"
    _, errors, _ = parse_objects(objs)
    assert (0, "unparseable_amount") in _codes(errors)


def test_ledgerentries_list_key_is_duplicate_posting_list():
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledgerentries_list"] = objs[0]["data"]["ledger_entries"]
    _, errors, _ = parse_objects(objs)
    assert (0, "duplicate_posting_list") in _codes(errors)


def test_unknown_kind():
    _, errors, _ = parse_objects([{"kind": "godown", "data": {"guid": "g"}}])
    assert _codes(errors) == [(0, "unknown_kind")]


def test_errors_are_collected_not_first_only():
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:3])
    del objs[0]["data"]["date"]
    objs[2]["data"]["iscancelled"] = "x"
    _, errors, _ = parse_objects(objs)
    assert {e.index for e in errors} == {0, 2}


def test_sign_disagreeing_with_isdeemedpositive_is_warning_only():   # D23
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledger_entries"][1]["isdeemedpositive"] = "Yes"      # credit amount flagged deemed-positive
    parsed, errors, warnings = parse_objects(objs)
    assert errors == [] and (0, "sign_vs_deemed_positive") in {(w.index, w.code) for w in warnings}


def test_error_detail_never_contains_amounts_or_names():              # decision 14
    objs = copy.deepcopy(_vouchers("p22_B_forex_sales.xml")[:1])
    objs[0]["data"]["ledger_entries"][0]["amount"] = "-16538.67"
    _, errors, _ = parse_objects(objs)
    assert "16538" not in errors[0].detail and "Indore" not in errors[0].detail


def test_masters_parse_with_reserved_prefix_cleaned():
    groups = transcode.masters_from_xml((SYNC / "p25_A_groups.xml").read_text(encoding="utf-8"), "group")
    for g in groups:                         # p25 lacks GUID/AlterID (fixture gap A4): add test identities
        g["data"].setdefault("guid", "t-" + g["data"]["name"])
        g["data"].setdefault("alterid", " 1")
    parsed, errors, _ = parse_objects(groups)
    assert errors == []
    assert next(p for p in parsed if p.name == "Current Assets").parent == "Primary"


def test_ledger_balance_expression_parsed():                           # p22_B_usd_ledger.xml
    obj = {"kind": "ledger_balance", "data": {"guid": "g1", "name": "Gulf Office Supplies LLC (USD)",
           "closingbalance": "-$1609.71 @ ? 82.58/$ = -? 132929.85", "captured_at": "2026-09-25T16:52:49+05:30"}}
    (p,), errors, _ = parse_objects([obj])
    assert errors == [] and p.closing.inr == Decimal("-132929.85") and p.closing.fx_amount == Decimal("-1609.71")


def test_ledger_balance_expression_without_base_is_accepted_as_needs_tb():   # D3 ledger rule
    obj = {"kind": "ledger", "data": {"guid": "g1", "alterid": " 3", "name": "USD Party", "parent": "Sundry Debtors",
           "closingbalance": "-$1609.71 @ ? 82.58/$", "captured_at": "2026-09-25T16:52:49+05:30"}}
    (p,), errors, _ = parse_objects([obj])
    assert errors == [] and p.fields["closingbalance"].inr is None and p.fields["closingbalance"].stated is False
```

`test_rung0.py`: `test_rung0_exact_zero`, `test_rung0_one_paisa_off_fails`, `test_rung0_on_forex_bases` (USD sale →
True), `test_rung0_empty_lines_balances` (a cancelled voucher → True), and `test_p06_all_50_balance_under_all_only`
(`p06` through transcode + `parse_objects` with the AlterIDs/flags joined from `p04_A_voucher_full.xml` /
`p16_A_vouchers_fy.xml` by GUID; the join helper is a local function in the test that only copies keys; it
docstrings the A4 gap).

- [ ] **Step 2: Run → FAIL; implement; run → PASS**

`uv run --project v2 pytest v2/tests/cloud/unit/test_validate.py v2/tests/cloud/unit/test_rung0.py -q`

Rules inside `parse_objects`:
- Unknown `kind` → `unknown_kind`. Missing `REQUIRED_KEYS` / `LINE_REQUIRED` / `INVENTORY_REQUIRED` →
  `missing_field` (detail = the key names).
- Wrap every parser call and map `WireParseError.code` to the object error.
- A line / bill / inventory `Amount` with `stated is False` → `forex_base_missing`.
- A ledger/stock master or `*_balance` balance with `stated is False` → **accepted** (D3 ledger rule; Task 8c
  stores it NULL with `balance_source='needs_tb'`).
- A voucher with a `ledgerentries_list` key → `duplicate_posting_list`.
- After a voucher parses cleanly, `voucher_balances` must hold, else `unbalanced_voucher`.
- `name`/`parent`/`ledgername`/`partyledgername`/`stockitemname`/`vouchertypename` pass through `parse.name` (D31).
- `has_forex` = any line amount has `fx_amount is not None`.
- Warnings: `sign_vs_deemed_positive` when `is_deemed_positive` is Yes and `inr > 0`, or No and `inr < 0`.

- [ ] **Step 3: Commit**

```bash
git add v2/cloud/ingest v2/cloud/parity v2/tests/cloud/unit/test_validate.py v2/tests/cloud/unit/test_rung0.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 8a — wire object validation (all errors collected) and rung 0 on INR bases

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** no error detail leaks a value; forex without base splits correctly between lines (reject) and
balances (accept). Tracker: S1.11 ✅ (pure; the endpoint wiring is proven in 8c).

---

## Task 8b: Ingest — name → GUID resolution, derivations (pure)

**Spec:** §4.4, §12 steps 6, 8, 10, 14; D13, D14, D30, D31; §15.2 rows 2–4. **Tracker:** S1.10 (partial).

**Files:**
- Create: `v2/cloud/ingest/resolve.py`, `v2/cloud/ingest/derive.py`
- Test: `v2/tests/cloud/unit/test_resolve.py`, `v2/tests/cloud/unit/test_derive.py`

**Interfaces:**
- Produces (`resolve.py`):
  - `class NameIndex` built by `NameIndex.from_rows(rows: Iterable[tuple[kind, guid, name, is_deleted]])`.
  - `.add(kind, guid, name)` (a master of this batch), `.rename(kind, guid, new_name)`.
  - `.resolve(kind, name) -> str`: `ResolveError("missing_master")` for no live match,
    `ResolveError("ambiguous_master")` for more than one. Resolution is per kind, so the group `Capital Account` and
    the ledger `Capital Account` never collide.
  - `order_objects(parsed) -> list`: the §12 step 6 kind order, then `alter_id` ascending within a kind; vouchers
    last.
  - `guid_prefix_warning(company_guid, obj_guid) -> bool` (D14).
- Produces (`derive.py`):
  - `nature_walk(group: str, parents: dict[str, str]) -> Derived(primary_group: str | None, nature: str | None,
    warning: str | None)`.
  - `base_type_walk(vtype: str, parents: dict[str, str], reserved: dict[str, str | None]) -> str | None`.
  - `is_forex_ledger(currency_name: str | None, base_currency_name: str | None, ever_expression: bool) -> bool`.
  - `is_base_currency(expanded_symbol: str) -> bool`.
  - `descendants(name: str, parents: dict[str, str]) -> set[str]` (for re-derivation).

- [ ] **Step 1: Failing tests**

```python
# test_derive.py
from pathlib import Path

from v2.cloud.ingest.derive import base_type_walk, descendants, is_forex_ledger, nature_walk
from v2.contract import parse, transcode

SYNC = Path(__file__).resolve().parents[2] / "fixtures" / "sync"


def _parents(fixture, kind):
    objs = transcode.masters_from_xml((SYNC / fixture).read_text(encoding="utf-8"), kind)
    return {parse.name(o["data"]["name"]): parse.name(o["data"].get("parent", "")) for o in objs}


def test_custom_subgroups_walk_to_nature_company_a():                  # p25_A_groups.xml
    parents = _parents("p25_A_groups.xml", "group")
    assert nature_walk("North Zone Debtors", parents).nature == "assets"
    assert nature_walk("National Creditors", parents).primary_group == "Current Liabilities"
    assert nature_walk("Sales Accounts", parents).nature == "income"


def test_all_company_a_groups_classified():
    parents = _parents("p25_A_groups.xml", "group")
    unmapped = [g for g in parents if nature_walk(g, parents).nature is None]
    assert unmapped == []


def test_cycle_and_unmapped_primary_warn():
    assert nature_walk("X", {"X": "Y", "Y": "X"}).warning == "unmapped_primary"
    assert nature_walk("Odd", {"Odd": "Primary"}).nature is None


def test_sales_gst_base_type_company_b():                               # p25_B_voucher_types.xml
    parents = _parents("p25_B_voucher_types.xml", "voucher_type")
    assert base_type_walk("Sales - GST", parents, {}) == "Sales"
    assert base_type_walk("Contra", parents, {}) == "Contra"


def test_unresolvable_base_type_is_none():
    assert base_type_walk("Mystery", {"Mystery": "Other"}, {}) is None


def test_is_forex():
    assert is_forex_ledger("$", "?", False) and not is_forex_ledger("?", "?", False)
    assert not is_forex_ledger(None, "?", False) and is_forex_ledger(None, "?", True)


def test_descendants_for_rederivation():
    parents = {"A": "Sundry Debtors", "B": "A", "C": "B", "D": "Sundry Creditors"}
    assert descendants("A", parents) == {"B", "C"}
```

```python
# test_resolve.py
import pytest

from v2.cloud.ingest.resolve import NameIndex, ResolveError, guid_prefix_warning


def test_group_and_ledger_may_share_a_name():                          # company A "Capital Account"
    idx = NameIndex.from_rows([("group", "g1", "Capital Account", False), ("ledger", "l1", "Capital Account", False)])
    assert idx.resolve("group", "Capital Account") == "g1" and idx.resolve("ledger", "Capital Account") == "l1"


def test_missing_and_ambiguous():
    idx = NameIndex.from_rows([("ledger", "l1", "Cash", False), ("ledger", "l2", "Cash", False),
                               ("ledger", "l3", "Old", True)])
    with pytest.raises(ResolveError) as e:
        idx.resolve("ledger", "Cash")
    assert e.value.code == "ambiguous_master"
    with pytest.raises(ResolveError) as e:
        idx.resolve("ledger", "Old")                                   # deleted rows never resolve
    assert e.value.code == "missing_master"


def test_rename_in_batch_moves_the_name():
    idx = NameIndex.from_rows([("ledger", "l1", "Old Name", False)])
    idx.rename("ledger", "l1", "New Name")
    assert idx.resolve("ledger", "New Name") == "l1"
    with pytest.raises(ResolveError):
        idx.resolve("ledger", "Old Name")


def test_names_are_exact_code_points():                                # probe 15
    idx = NameIndex.from_rows([("ledger", "h1", "शर्मा ट्रेडर्स", False)])
    assert idx.resolve("ledger", "शर्मा ट्रेडर्स") == "h1"


def test_guid_prefix_warning():
    c = "138b7373-753c-4dbe-aa63-b802035f0ba9"
    assert not guid_prefix_warning(c, c + "-000003c1") and guid_prefix_warning(c, "710de34a-x-00000001")
```

- [ ] **Step 2: Run → FAIL; implement; run → PASS**

`uv run --project v2 pytest v2/tests/cloud/unit/test_resolve.py v2/tests/cloud/unit/test_derive.py -q`

```python
def nature_walk(group, parents):
    seen, name = [], group
    while True:
        if name in seen:
            return Derived(None, None, "unmapped_primary")
        seen.append(name)
        parent = parents.get(name)
        if parent is None or parent in ("", "Primary"):
            nature = PRIMARY_NATURE.get(name)
            return Derived(name if nature else None, nature, None if nature else "unmapped_primary")
        name = parent
```

`base_type_walk` stops at `name == parent`, or at a name in `RESERVED_VOUCHER_TYPES`, or at `reserved[name]` (the
exported `RESERVEDNAME`, when non-empty). A cycle → `None`.

- [ ] **Step 3: Commit**

```bash
git add v2/cloud/ingest/resolve.py v2/cloud/ingest/derive.py v2/tests/cloud/unit/test_resolve.py \
        v2/tests/cloud/unit/test_derive.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 8b — per-type name->GUID resolver and nature/base-type/forex derivations

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** resolution is per kind and live-only; derivations run on the real `p25_*` captures.
Tracker: S1.10 🟡.

---
## Task 8c: Ingest — store, pipeline, `/batches` endpoint, limits, idempotency, quarantine

**Spec:** §7.9, §8.5, §12 (all 15 steps + performance budget), §4.5 (lines replaced, `countable`), §4.9 (the `raw`
window at insert), D12, D14, D19, D23, §11, §14 scenarios 4, 5, 6, 8, 9, 10, 11, 19, §15.2 (every row).
**Tracker:** S1.9, S1.10, S1.12.

**Files:**
- Create: `v2/cloud/ingest/store.py`, `v2/cloud/ingest/pipeline.py`
- Modify: `v2/cloud/api/sync.py` (`POST /api/sync/{ws}/batches`)
- Test: `v2/tests/cloud/db/test_ingest_api.py`, `v2/tests/cloud/db/test_ingest_scenarios.py`

**Interfaces:**
- Consumes: `parse_objects` (8a), `NameIndex`, `order_objects`, `nature_walk`, `base_type_walk`,
  `is_forex_ledger` (8b), `require_open_run` (7), `active_device` (4).
- Produces:
  - `pipeline.read_body(request, settings) -> dict`. It enforces the gzip size, inflates in 64 KiB chunks with a
    running total (abort as soon as the total passes `ingest_max_decompressed_bytes`), parses the JSON, and checks the
    object count, raising 413 `payload_too_large`. No `Content-Encoding` → plain JSON with the same limits.
  - `pipeline.ingest_batch(session, sw, device, body: BatchRequest, raw_sha256: str, settings, clock) -> tuple[int,
    dict]` (HTTP status, response JSON). Everything runs in one transaction, and a rejection rolls it all back, then
    writes only the `sync_batches` row with `status='rejected'` in a **second** short transaction.
  - `store.upsert_master(session, ws_id, p: PMaster, derived: dict) -> Literal["inserted", "updated",
    "skipped_older"]`.
  - `store.apply_balance(session, ws_id, p: PBalance | PMaster) -> Literal["applied", "stale"]`.
  - `store.upsert_voucher(session, ws_id, v: PVoucher, resolved: ResolvedVoucher, run_id, keep_raw: bool) ->
    Literal["inserted", "updated", "skipped_older"]`.
  - `store.raw_window_fys(session, ws_id, today_ist) -> set[date]` (the newest 2 FY starts of the coverage).

- [ ] **Step 1: Failing tests** (`test_ingest_api.py`, the §15.2 matrix and the limits)

Common setup fixture `bound_b(app_client, session)`: user + workspace + login + bind company B
(`138b7373-753c-4dbe-aa63-b802035f0ba9`, `books_from 20220401`) + an open `first_sync` run. It returns `(ws_id,
headers, run_id)`. The masters batch helper `b_masters()` builds company B's masters from the captures
(`p18_B_group_list.xml`, `p16_B_ledgers.xml`, `p25_B_voucher_types.xml`, `p22_B_currencies.xml`,
`p15_B_compound_unit_item.xml`) through the transcoder. Where a capture has no GUID/AlterID, it reads the `s1_B_*_ids`
Task 0 capture, else it assigns the A4 synthetic identity (the Task 12 assembler; until Task 12 lands, the helper
lives in `v2/tests/cloud/realdata.py` with just `b_masters()` and `a_masters()`, and Task 12 extends it). `post_batch`
gzips JSON and sets `Content-Encoding: gzip`.

Tests (each re-reads the DB in a fresh session where it asserts state):
- `test_masters_only_batch_accepted_counts_inserted`
- `test_vouchers_only_batch_after_masters_accepted` (`p21_B_fy2022_month_09.xml`, 20 vouchers)
- `test_mixed_batch_masters_applied_first`: a voucher referencing a ledger defined **later in the same batch** →
  accepted (step 6).
- `test_unknown_ledger_422_missing_master_retryable`: the body lists `{"index", "kind", "guid", "code":
  "missing_master"}`, and nothing is stored.
- `test_two_live_ledgers_same_name_422_ambiguous_master`
- `test_unbalanced_422_deterministic_and_nothing_stored` (§14.10 first half)
- `test_forex_voucher_stored_with_fx_columns`: lines `-37216.04 / 37216.04`, `fx_amount -448.44/448.44`,
  `fx_rate 82.99`, `fx_currency "$"`, `has_forex true`.
- `test_cancelled_voucher_no_lines_empty_party_accepted`
- `test_optional_voucher_lines_not_countable` / `test_post_dated_voucher_lines_countable`
  (`p16_A_post_dated_voucher.xml` needs company A masters; bind A in that test).
- `test_older_alter_id_skipped_older` (§14.5): store v@105, send v@104 → `skipped_older 1`; lines unchanged.
- `test_wrong_company_guid_409_company_mismatch_nothing_stored`: the `company_guid` of `p02_A_active_b.xml`.
- `test_replay_same_body_returns_stored_response_replayed_true` (§14.4)
- `test_reused_batch_id_different_body_409_batch_id_reused`
- `test_concurrent_identical_batches_store_once` (Review Focus 5): two `post_batch` calls with the same body via
  `asyncio.gather`. Exactly one voucher row set; statuses `{200}`; one response `replayed: true`. Implementation:
  `INSERT … ON CONFLICT (workspace_id, batch_id) DO NOTHING` on a `sync_batches` "claim" row at step 2, **before**
  any data write, inside the batch transaction. The loser waits on the row lock, then re-reads and replays.
- `test_oversize_gzip_413`, `test_too_many_objects_413` (501 objects), `test_zip_bomb_is_413_before_full_inflate`
  (a 60 MB zero-filled JSON string gzips to ~60 KB; assert 413 and, via `tracemalloc`, peak allocation < 70 MB),
  `test_uncompressed_json_body_is_accepted`.
- `test_incremental_batch_in_restore_detected_409`
- `test_batch_on_completed_run_409_run_closed`, `test_batch_on_other_devices_run_403`.
- `test_guid_prefix_foreign_is_warning_not_error` (D14): an object GUID with another prefix → 200 with
  `warnings[].code == "guid_prefix_foreign"`.
- `test_ledger_guid_cross_check_mismatch_warns` (step 14).
- `test_quarantine_resend_stores_rest_and_records_row` (§14.10): 10 vouchers, one unbalanced → 422. Resend 9 +
  `quarantine: [{kind, guid, code: "unbalanced_voucher", voucher_date}]` → 200. 9 stored, a `sync_quarantine` row,
  `quarantine_count == 1`. A later batch with a good copy of that GUID → the quarantine row `resolved_at` is set
  and the count is back to 0.
- `test_quarantine_retryable_code_refused` (A7): a `missing_master` code in `quarantine` → 422 with
  `quarantine_code_not_allowed`.
- `test_last_synced_at_moved_by_first_sync_and_incremental_not_backfill` (§14.11 + §8.5): a backfill run's batch
  leaves it; an incremental's moves it; a company `full_resync` batch moves it only if its `chunk` is inside the
  window or it carries masters.
- `test_ingest_performance_500_vouchers_under_5s`: 500 FakeBooks-shaped vouchers (built by cloning
  `p21_B_fy2022_month_09.xml` vouchers with new GUIDs `…-9xxxxxxx` and dates within Sept 2022 — the only synthetic
  content here is identity and date) → `time.perf_counter()` < 5.0.

`test_ingest_scenarios.py` (§14 state scenarios; each asserts the **whole** stored state after a fresh-session
re-read):
- `test_voucher_edit_replaces_lines_and_bills_exactly` (§14.6): v@105 has 4 lines + 1 bill. v@106 has 3 lines and
  different amounts → exactly 3 lines, with those amounts; bills replaced.
- `test_rename_keeps_guid_and_old_lines_join` (§14.8, from `p08_A_rename_before.xml` / `p08_A_rename_after.xml` +
  `_vouchers`): one ledger row with the new name; old lines keep `ledger_name` as exported and the same
  `ledger_guid`.
- `test_mirrored_balance_newer_wins_stale_counted` (§14.9): t2 then t1 → t2 stays, `balances_stale == 1`.
- `test_numeric_round_trip_exact` (§14.19): `-16538.66`, `132929.85`, `0.01`, face `-1609.71`, rate `82.58` read
  back as equal `Decimal`s.
- `test_group_parent_change_rederives_descendants` (step 10): move `North Zone Debtors` under `Sundry Creditors` →
  its nature and every descendant's become `liabilities`.
- `test_ledger_balance_without_stated_base_stored_null_needs_tb` (D3).
- `test_raw_kept_only_for_newest_two_fys_at_insert` (§4.9): a FY 2022-23 voucher in a workspace whose coverage
  runs to FY 2026-27 → `raw IS NULL`; a FY 2025-26 voucher → `raw` present.

- [ ] **Step 2: Run → FAIL**

`TEST_DATABASE_URL=… uv run --project v2 pytest v2/tests/cloud/db/test_ingest_api.py v2/tests/cloud/db/test_ingest_scenarios.py -q`

- [ ] **Step 3: Implement `store.py`, `pipeline.py`, the route**

The pipeline in §12 step order (one function per step, each unit-sized):

```python
async def ingest_batch(session, sw, device, body, raw_sha256, settings, clock):
    claimed = await _claim_batch_id(session, sw.workspace_id, body.batch_id, raw_sha256, clock)   # step 2
    if claimed.replay is not None:
        return 200, {**claimed.replay, "replayed": True}
    if claimed.conflict:
        raise ApiError(409, "batch_id_reused")
    if body.company_guid != sw.tally_company_guid:                                               # step 3
        raise ApiError(409, "company_mismatch")
    run = await require_open_run(session, sw, device, body.run_id)                               # step 4
    if sw.sync_state == "restore_detected" and run.kind == "incremental":
        raise ApiError(409, "restore_detected")
    _check_quarantine_codes(body.quarantine)                                                     # A7
    parsed, errors, warnings = parse_objects([o.model_dump() for o in body.objects])             # steps 5 + 7
    ordered = order_objects(parsed)                                                              # step 6
    index = await _load_name_index(session, sw.workspace_id)
    resolved, res_errors = _resolve_all(ordered, index, sw)                                      # step 8
    errors += res_errors
    if errors:
        return await _reject(session, sw, body, raw_sha256, errors, clock)
    counts = await _store_all(session, sw, run, ordered, resolved, clock)                        # steps 9–12
    await _record_quarantine(session, sw, body.quarantine, stored_guids=_guids(ordered), clock=clock)   # step 13
    warnings += _warnings(sw, ordered, resolved)                                                 # step 14
    await _touch_last_synced(sw, run, body, ordered, clock)                                      # step 15, §8.5
    response = {"batch_id": body.batch_id, "status": "accepted", "replayed": False, "counts": counts,
                "warnings": [w.__dict__ for w in warnings], "reread_ledgers": []}
    await _finish_claim(session, claimed, "accepted", len(body.objects), response)
    await session.commit()
    return 200, response
```

`_reject` rolls back, then in a new transaction upserts the `sync_batches` row `status='rejected'` with the
response `{"error": "batch_rejected", "objects": [...]}` and returns 422. A replay of a **rejected** batch returns
the stored 422 body (idempotent). Lines: `DELETE FROM tally_voucher_ledger_lines WHERE voucher_id = :id` (and the
inventory/bill tables), then bulk `insert()` in one statement per table (the performance budget).
`countable = not is_cancelled and not is_optional`. `voucher_date = v.date`.

- [ ] **Step 4: Run → PASS**

`TEST_DATABASE_URL=… uv run --project v2 pytest v2/tests/cloud/db/test_ingest_api.py v2/tests/cloud/db/test_ingest_scenarios.py -q 2>&1 | tee logs/v2-s1-db-task8c.log`

- [ ] **Step 5: Commit**

```bash
git add v2/cloud/ingest/store.py v2/cloud/ingest/pipeline.py v2/cloud/api/sync.py v2/tests/cloud/realdata.py \
        v2/tests/cloud/db/test_ingest_api.py v2/tests/cloud/db/test_ingest_scenarios.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 8c — /batches ingest pipeline: limits, idempotent claim, atomic store, quarantine

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** the reviewer checks atomicity (a mid-batch failure leaves zero rows), the concurrency test
really races, bodies never logged, and that the zip-bomb guard streams. Tracker: S1.9 ✅, S1.10 ✅, S1.11 ✅ (endpoint
proof), S1.12 ✅.

---

## Task 9: Reconcile + snapshots

**Spec:** §7.11, §7.12, §4.6, D8, D10 (imbalance computed here), D19, D25, D28, LESSONS rules 19 and 30, §14
scenarios 7 and 15, §15.2. **Tracker:** S1.14, S1.15.

**Files:**
- Create: `v2/cloud/ingest/reconcile.py`, `v2/cloud/ingest/snapshots.py`
- Modify: `v2/cloud/api/sync.py`
- Test: `v2/tests/cloud/unit/test_snapshot_rows.py`, `v2/tests/cloud/db/test_reconcile_snapshots_api.py`

**Interfaces:**
- Produces:
  - `snapshots.parse_cells(report_type: str, cells: list[dict]) -> ParsedSnapshot(rows: list[dict], synthetic:
    list[str], imbalance: Decimal | None)`. TB rows are `{"name", "amount": str(Decimal) | None, "notes": [...]}`,
    where `amount` = Σ of the present `dspcldramta`/`dspclcramta` (debit negative, as exported). The amount cells
    use `parse.amount` (D3: an expression's stated base; an unstated expression → `unparseable_amount` 422 for
    snapshots). `imbalance` (for `trial_balance` only) = Σ **first-occurrence primary-group rows** + the
    `Unadjusted Forex Gain/Loss` row (A18). `Opening Stock` is **not** added: it is nested inside the
    stock-bearing group's row. `synthetic` lists both names when present. For `trial_balance_ledgerwise`,
    imbalance = Σ of all rows.
  - `snapshots.store(session, sw, body: SnapshotRequest, clock) -> dict`: D8 (`from_date == fy_start_of(as_on)`,
    else 422 `bad_period`), `as_on >= books_from`, upsert on the key, newer `captured_at` wins.
  - `reconcile.reconcile(session, sw, device, body: ReconcileRequest, clock) -> dict`.

- [ ] **Step 1: Failing unit tests** (`test_snapshot_rows.py`, all on real captures)

```python
from decimal import Decimal
from pathlib import Path

import pytest

from v2.cloud.ingest.snapshots import parse_cells
from v2.contract import transcode

SYNC = Path(__file__).resolve().parents[2] / "fixtures" / "sync"


def _cells(name, rt):
    return transcode.report_cells((SYNC / name).read_text(encoding="utf-8"), rt)


def test_b_tb_2023_nets_to_zero_with_forex_row():                     # §10.1 item 6 real value
    snap = parse_cells("trial_balance", _cells("p18_B_tb_asof_2023-03-31.xml", "trial_balance"))
    assert snap.imbalance == Decimal("0.00")
    assert set(snap.synthetic) == {"Opening Stock", "Unadjusted Forex Gain/Loss"}


def test_b_tb_without_forex_row_is_out_by_18387():                     # LESSONS rule 29(b) + the S1 note
    cells = [c for c in _cells("p18_B_tb_asof_2023-03-31.xml", "trial_balance")
             if c["dspdispname"] != "Unadjusted Forex Gain/Loss"]
    assert parse_cells("trial_balance", cells).imbalance == Decimal("183.87")


def test_a_tb_fy_end_imbalance_non_zero():                             # company A seed opening defect (D10)
    assert parse_cells("trial_balance", _cells("p16_A_tb_fy_end.xml", "trial_balance")).imbalance == Decimal("3305800.00")


def test_opening_stock_is_nested_not_added():                          # A18: adding it would give -24450.00 on B
    snap = parse_cells("trial_balance", _cells("p18_B_tb_asof_2023-03-31.xml", "trial_balance"))
    assert snap.imbalance == Decimal("0.00") and "Opening Stock" in snap.synthetic


def test_first_occurrence_capital_account_row():                       # p16_A_tb_fy_end.xml
    rows = parse_cells("trial_balance", _cells("p16_A_tb_fy_end.xml", "trial_balance")).rows
    assert [r["name"] for r in rows].count("Capital Account") == 2


@pytest.mark.parametrize("fixture, rt", [
    ("p12_A_stock_summary_today.xml", "stock_summary"), ("p23_B_bills_receivable_due.xml", "bills_receivable"),
    ("p12_A_bs_today.xml", "balance_sheet"), ("p12_A_pl_fy2025.xml", "profit_and_loss"),
    ("p17_A_tb_exploded_isledgerwise.xml", "trial_balance_ledgerwise"),
])
def test_every_report_type_parses(fixture, rt):
    assert parse_cells(rt, _cells(fixture, rt)).rows


def test_a_bills_residuals_match_seed_anchors():                        # Part 1 §8: ₹9,70,537 / ₹18,34,142
    rec = parse_cells("bills_receivable", _cells("p12_A_bills_receivable_today.xml", "bills_receivable")).rows
    pay = parse_cells("bills_payable", _cells("p12_A_bills_payable_today.xml", "bills_payable")).rows
    assert abs(sum(Decimal(r["amount"]) for r in rec)) == Decimal("970537.00")
    assert abs(sum(Decimal(r["amount"]) for r in pay)) == Decimal("1834142.00")
```

If a residual's exact paise differ from the round rupee in the capture, the assertion uses the capture's own total.
The Part 1 anchors are rupee-rounded; record the exact value in the test comment.

- [ ] **Step 2: Run → FAIL; implement `parse_cells`; run → PASS**

- [ ] **Step 3: Failing DB tests** (`test_reconcile_snapshots_api.py`)

- `test_snapshot_recapture_replaces_and_round_trips` (§14.15): post the B TB as-on 31-03-2023 twice (the second
  with a newer `captured_at`) → one row, `replaced: true`, and the `cells` + `rows` read back equal.
- `test_older_capture_does_not_replace`.
- `test_snapshot_bad_period_422` (D8: `from_date` 01-05-2022).
- `test_snapshot_before_books_from_422_bad_period`.
- `test_snapshot_company_mismatch_409` (the counters' GUID vs the bound one; the snapshot body carries no GUID, so the
  check is the bound workspace only; the test posts via a device of another workspace → 403 `wrong_workspace`).
- `test_reconcile_soft_deletes_absent_voucher_and_returns_touched_ledgers` (§14.7, `p07_A_throwaway_created.xml` →
  `p07_A_after_delete.xml`): the voucher has `is_deleted`, `deleted_at`; its lines/inventory/bills are gone;
  `reread_ledgers` lists the ledgers of its lines (guid + name).
- `test_reconcile_refetch_for_unknown_or_newer_alter_id`.
- `test_reconcile_guard_20pct_and_50_rows_409_reconcile_too_large` (D28) and `_confirm_large_allows`.
- `test_reconcile_master_in_use_409` (LESSONS rule 30).
- `test_resent_deleted_voucher_is_undeleted` (S1-R8).
- `test_reconcile_scope_limits_to_date_range`: a voucher outside `from..to` is never touched.

- [ ] **Step 4: Run → FAIL; implement; run → PASS**

`TEST_DATABASE_URL=… uv run --project v2 pytest v2/tests/cloud/unit/test_snapshot_rows.py v2/tests/cloud/db/test_reconcile_snapshots_api.py -q 2>&1 | tee logs/v2-s1-db-task9.log`

- [ ] **Step 5: Commit**

```bash
git add v2/cloud/ingest/reconcile.py v2/cloud/ingest/snapshots.py v2/cloud/api/sync.py \
        v2/tests/cloud/unit/test_snapshot_rows.py v2/tests/cloud/db/test_reconcile_snapshots_api.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 9 — reconcile (soft-delete, guard, reread ledgers) and report snapshots (cells+rows, imbalance)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** B's TB nets to exactly 0.00 with the forex row (A18: Opening Stock nested, not added); the reconcile guard; D19 hard-deletes lines
but keeps the voucher. Tracker: S1.14 ✅, S1.15 ✅.

---
## Task 10a: Parity core (pure) — model, anchors (D9), rung 1, forex (C47, D4), rung 2

**Spec:** §10.2, §10.4, §10.5, §10.6, D4, D9, D11, D29, D30, Q19, §15.5 (verdict columns), §16 "Cloud unit tests"
(rungs). **Tracker:** S1.20, S1.21.

**Files:**
- Create: `v2/cloud/parity/model.py`, `anchors.py`, `rung1.py`, `forex.py`, `rung2.py`
- Test: `v2/tests/cloud/unit/test_anchors.py`, `test_rung1.py`, `test_forex.py`, `test_rung2.py`

**Interfaces:**
- Produces (`model.py`, frozen dataclasses; all amounts `Decimal`, debit negative):
  - `LedgerIn(guid, name, group_guid, primary_group: str | None, nature: str | None, is_forex: bool,
    mirrored_closing: Decimal | None, closing_fx: Decimal | None, closing_fx_rate: Decimal | None,
    opening_fx: Decimal | None, balance_source: str, in_capture: bool)`.
  - `Sums(total: dict[guid, Decimal], face: dict[guid, Decimal], face_complete: dict[guid, bool], fy_total:
    dict[guid, Decimal])`. `total` covers countable lines in [E, as_on]; `face`/`fy_total` cover [FY(as_on).start,
    as_on]; `face_complete[g]` is False if any such line on g has no `fx_amount`.
  - `TbRow(name: str, amount: Decimal)`.
  - `Line(scope: str, guid: str | None, name: str, our: Decimal | None, tally: Decimal | None, diff: Decimal |
    None, verdict: str, cause: str | None, unrealised: Decimal | None = None, our_fx: Decimal | None = None,
    tally_fx: Decimal | None = None)`. `verdict` is one of `match`, `mismatch`, `match_revalued`,
    `not_applicable`, `missing_in_db`, `missing_in_tally`.
  - `TOL = Decimal("1.00")` default; every function takes `tol: Decimal`.
- Produces (`anchors.py`): `AnchorPlan(as_on: date, subtract_lines_dated: date | None)`;
  `plan(verified_edge: date, books_from: date) -> AnchorPlan`; `anchor_amounts(ledgerwise_rows: list[TbRow],
  index: NameIndex, books_from_line_sums: dict[guid, Decimal] | None) -> dict[guid, Decimal]`. Synthetic rows are
  skipped. Rows that resolve to no ledger are returned separately as `unresolved: list[str]`.
- Produces (`rung1.py`): `rung1(ledgers: list[LedgerIn], anchors: dict[guid, Decimal] | None, sums: Sums,
  ledgerwise_tb: dict[guid, Decimal], unresolved_tb_names: list[str], tol) -> list[Line]`.
- Produces (`forex.py`): `face_check(l: LedgerIn, sums: Sums) -> tuple[bool, str | None]` (ok, cause);
  `set_rule(raw_diffs: dict[guid, Decimal], unadjusted: Decimal | None, tol) -> bool`;
  `forex_lines(forex_ledgers, anchors, sums, tb_by_guid, unadjusted, tol) -> tuple[list[Line], Decimal]` (the lines
  and `forex_unrealised_total`).
- Produces (`rung2.py`): `rung2(ledgers, rung1_lines, forex_lines, group_rows: dict[str, Decimal],
  opening_stock: Decimal | None, stock_bearing_primary: str, nominal_tb: dict[guid, Decimal], sums: Sums,
  unresolved_nominal: list[str], tol) -> list[Line]`.

- [ ] **Step 1: Failing tests**

```python
# test_anchors.py
from datetime import date

from v2.cloud.parity.anchors import AnchorPlan, plan


def test_anchor_is_tb_as_on_day_before_verified_edge():
    assert plan(date(2024, 4, 1), date(2022, 4, 1)) == AnchorPlan(date(2024, 3, 31), None)


def test_anchor_at_books_from_subtracts_own_first_day_lines():         # D9, C43-safe
    assert plan(date(2022, 4, 1), date(2022, 4, 1)) == AnchorPlan(date(2022, 4, 1), date(2022, 4, 1))
```

```python
# test_rung1.py
from decimal import Decimal as D

import pytest

from v2.cloud.parity.model import LedgerIn, Sums
from v2.cloud.parity.rung1 import rung1

TOL = D("1.00")


def L(guid, nature="assets", closing=D("-100.00"), **kw):
    base = dict(guid=guid, name=guid, group_guid="g", primary_group="Current Assets", nature=nature, is_forex=False,
                mirrored_closing=closing, closing_fx=None, closing_fx_rate=None, opening_fx=None,
                balance_source="tally", in_capture=True)
    base.update(kw)
    return LedgerIn(**base)


def S(total):
    return Sums(total=total, face={}, face_complete={}, fy_total={})


@pytest.mark.parametrize("ours, verdict", [(D("-99.01"), "match"), (D("-99.00"), "match"), (D("-98.99"), "mismatch")])
def test_tolerance_boundaries(ours, verdict):                           # §15.5 last row: 0.99 / 1.00 / 1.01
    (line,) = rung1([L("a")], {"a": D("0")}, S({"a": ours}), {}, [], TOL)
    assert line.verdict == verdict


def test_debit_is_negative_anchor_plus_lines():
    (line,) = rung1([L("a", closing=D("-150.00"))], {"a": D("-100.00")}, S({"a": D("-50.00")}), {}, [], TOL)
    assert (line.our, line.tally, line.verdict) == (D("-150.00"), D("-150.00"), "match")


def test_nominal_ledger_is_not_applicable_never_match():
    (line,) = rung1([L("s", nature="income")], {"s": D("0")}, S({"s": D("-100.00")}), {}, [], TOL)
    assert (line.verdict, line.cause) == ("not_applicable", "nominal")


def test_pl_account_not_applicable():                                   # D11
    (line,) = rung1([L("pl", name="Profit & Loss A/c", nature="liabilities")], {}, S({}), {}, [], TOL)
    assert (line.verdict, line.cause) == ("not_applicable", "pl_account")


def test_unclassified_group_not_applicable():
    (line,) = rung1([L("u", nature=None)], {}, S({}), {}, [], TOL)
    assert line.cause == "unclassified_group"


def test_no_ledger_anchor_route_makes_bs_ledgers_not_applicable():      # §15.5 row 12
    lines = rung1([L("a"), L("b")], None, S({"a": D("-100"), "b": D("0")}), {}, [], TOL)
    assert {(l.verdict, l.cause) for l in lines} == {("not_applicable", "no_ledger_anchor")}


def test_needs_tb_takes_tally_figure_from_ledgerwise_tb():              # D3 ledger rule
    (line,) = rung1([L("a", mirrored_closing=None, balance_source="needs_tb")], {"a": D("0")},
                    S({"a": D("-100.00")}), {"a": D("-100.00")}, [], TOL)
    assert line.verdict == "match" and line.tally == D("-100.00")


def test_ledger_absent_from_capture_missing_in_tally():
    (line,) = rung1([L("a", in_capture=False)], {"a": D("0")}, S({}), {}, [], TOL)
    assert line.verdict == "missing_in_tally"


def test_tb_row_resolving_to_no_ledger_missing_in_db():
    lines = rung1([], {}, S({}), {}, ["Ghost Ledger"], TOL)
    assert [(l.name, l.verdict) for l in lines] == [("Ghost Ledger", "missing_in_db")]


def test_anchor_absent_ledger_counts_zero():
    (line,) = rung1([L("a", closing=D("-50.00"))], {}, S({"a": D("-50.00")}), {}, [], TOL)
    assert line.verdict == "match"


def test_mid_backfill_anchor_correct_all_time_far_off_is_match():        # R30 regression (§15.5)
    """All-time Σ lines would be off by lakhs (older FYs not synced); the anchor at E-1 absorbs it."""
    (line,) = rung1([L("a", closing=D("-900000.00"))], {"a": D("-850000.00")}, S({"a": D("-50000.00")}), {}, [], TOL)
    assert line.verdict == "match"
```

```python
# test_forex.py — company B real numbers (§10.5): p22_B_forex_sales.xml, p22_B_usd_ledger.xml,
# p18_B_tb_asof_2023-03-31.xml
from decimal import Decimal as D

from v2.cloud.parity.forex import face_check, forex_lines, set_rule
from v2.cloud.parity.model import LedgerIn, Sums

TOL = D("1.00")
USD = LedgerIn(guid="usd", name="Gulf Office Supplies LLC (USD)", group_guid="sd", primary_group="Current Assets",
               nature="assets", is_forex=True, mirrored_closing=D("-132929.85"), closing_fx=D("-1609.71"),
               closing_fx_rate=D("82.58"), opening_fx=D("0"), balance_source="tally", in_capture=True)
SUMS = Sums(total={"usd": D("-133113.72")}, face={"usd": D("-1609.71")}, face_complete={"usd": True},
            fy_total={"usd": D("-133113.72")})


def test_face_and_self_consistency_hold_on_real_numbers():
    assert face_check(USD, SUMS) == (True, None)


def test_face_mismatch_when_a_usd_sale_is_missing():                     # §15.5 "forex sale missing"
    missing = Sums(total={"usd": D("-37216.04")}, face={"usd": D("-448.44")}, face_complete={"usd": True},
                   fy_total={"usd": D("-37216.04")})
    assert face_check(USD, missing) == (False, "forex_face_mismatch")


def test_plain_inr_line_on_forex_ledger_face_incomplete():
    partial = Sums(total=SUMS.total, face=SUMS.face, face_complete={"usd": False}, fy_total=SUMS.fy_total)
    assert face_check(USD, partial) == (False, "forex_face_incomplete")


def test_set_rule_accepts_18387_against_unadjusted_row():
    assert set_rule({"usd": D("183.87")}, D("-183.87"), TOL)


def test_set_rule_rejects_without_unadjusted_row():
    assert not set_rule({"usd": D("183.87")}, None, TOL)


def test_forex_lines_real_b_match_revalued_and_total():
    lines, total = forex_lines([USD], {"usd": D("0")}, SUMS, {"usd": D("-132929.85")}, D("-183.87"), TOL)
    (line,) = lines
    assert (line.verdict, line.unrealised, total) == ("match_revalued", D("183.87"), D("183.87"))


def test_forex_lines_unexplained_when_row_removed():                     # §16 "row removed" case
    lines, _ = forex_lines([USD], {"usd": D("0")}, SUMS, {"usd": D("-132929.85")}, None, TOL)
    assert (lines[0].verdict, lines[0].cause) == ("mismatch", "forex_revaluation_unexplained")


def test_tb_row_as_expression_or_plain_both_supported():                 # S1-R2, G2 decides the real path
    lines, _ = forex_lines([USD], {"usd": D("0")}, SUMS, {"usd": D("-132929.85")}, D("-183.87"), TOL)
    assert lines[0].verdict == "match_revalued"
```

`test_rung2.py`:
- `test_primary_group_first_occurrence_only`: rows `[("Capital Account", -X group), ("Capital Account", ledger)]` →
  only the first is used (write the rows from `p16_A_tb_fy_end.xml` via `transcode.report_cells` +
  `snapshots.parse_cells`).
- `test_opening_stock_added_only_to_stock_bearing_group`.
- `test_bs_group_includes_accepted_forex_unrealised`: the B numbers above under `Current Assets` → group `match`.
- `test_nominal_ledgers_compared_per_ledger_from_fy_start`: a `Sales` ledger with `fy_total` vs the ledgerwise TB
  row → `match`, and ±1.01 → `mismatch`.
- `test_nominal_tb_row_unresolved_missing_in_db`.
- `test_group_mismatch_with_all_ledgers_matching_is_group_walk_wrong`.
- `test_explodeflag_tb_never_used_as_ledger_level`: feed `p17_A_tb_exploded_explodeflag.xml` rows as if they were a
  ledgerwise TB → `rung2` refuses with `ValueError("not a ledger-level TB")`. The caller passes `request_flags`, and
  only `{"ISLEDGERWISE": "Yes"}` is accepted as ledger-level (the guard lives in `anchors.require_ledgerwise(flags)`,
  tested here too).

- [ ] **Step 2: Run → FAIL; implement; run → PASS**

`uv run --project v2 pytest v2/tests/cloud/unit/test_anchors.py v2/tests/cloud/unit/test_rung1.py v2/tests/cloud/unit/test_forex.py v2/tests/cloud/unit/test_rung2.py -q`

`forex.py` core (the only multiplication in S1, per the Global Constraints):

```python
FACE_TOL = Decimal("0.01")


def face_check(l: LedgerIn, sums: Sums) -> tuple[bool, str | None]:
    if not sums.face_complete.get(l.guid, True):
        return False, "forex_face_incomplete"
    if l.closing_fx is None or l.closing_fx_rate is None or l.mirrored_closing is None:
        return False, "forex_face_incomplete"
    face_ours = (l.opening_fx or Decimal("0")) + sums.face.get(l.guid, Decimal("0"))
    if abs(face_ours - l.closing_fx) > FACE_TOL:
        return False, "forex_face_mismatch"
    if abs(l.closing_fx * l.closing_fx_rate - l.mirrored_closing) > FACE_TOL:
        return False, "forex_face_mismatch"
    return True, None


def set_rule(raw_diffs: dict[str, Decimal], unadjusted: Decimal | None, tol: Decimal) -> bool:
    return abs(sum(raw_diffs.values(), Decimal("0")) + (unadjusted or Decimal("0"))) <= tol
```

`forex_lines`: for each forex ledger, `computed = anchor + total`. Where `tb_by_guid` has the ledger, `raw_diff = tb
− computed`. `set_ok = set_rule(raw_diffs, unadjusted)`. Per ledger: (a) when `face_check` is False with
`forex_face_mismatch` → `mismatch` (cause `forex_face_mismatch`); when `forex_face_incomplete` → `not_applicable`
unless (b) covers it; then (b): `set_ok` → `match_revalued` with `unrealised = raw_diff`; not ok and `raw_diff != 0`
→ `mismatch`, cause `forex_revaluation_unexplained`. `forex_unrealised_total` = Σ of the accepted `unrealised`
values.

- [ ] **Step 3: Commit**

```bash
git add v2/cloud/parity/model.py v2/cloud/parity/anchors.py v2/cloud/parity/rung1.py v2/cloud/parity/forex.py \
        v2/cloud/parity/rung2.py v2/tests/cloud/unit/test_anchors.py v2/tests/cloud/unit/test_rung1.py \
        v2/tests/cloud/unit/test_forex.py v2/tests/cloud/unit/test_rung2.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 10a — parity core: D9 anchors, rung 1, C47 forex face + set rule, rung 2 with ledger resolution

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** a nominal ledger is never `match` in rung 1; `no_ledger_anchor` is never `match`; non-forex
ledgers never get an allowance; the ₹183.87 numbers match spec §10.5. Tracker: S1.20 ✅, S1.21 🟡 (the engine wiring
is 10c).

---

## Task 10b: Classifier, escalation ladder, month-bisect, ops signal (pure)

**Spec:** §10.7, §10.8, §10.9, §10.10 (ops signal), decision 14, §15.5 (ladder column), §16 "Classifier" /
"Ladder" / "Ops signal". **Tracker:** S1.22, S1.24.

**Files:**
- Create: `v2/cloud/parity/classify.py`, `ladder.py`, `bisect.py`, `opsignal.py`
- Test: `v2/tests/cloud/unit/test_classify.py`, `test_ladder.py`, `test_bisect.py`, `test_opsignal.py`

**Interfaces:**
- Produces:
  - `classify.Context(anchor_rows: dict[guid, Decimal], flagged_amounts: dict[guid, set[Decimal]], forex_guids:
    set[str], fy_start: date, verified_edge: date, month_ends_without_tb: list[date])`.
  - `classify.classify(lines: list[Line], ctx: Context) -> tuple[list[Line], list[Remediation]]`. It sets
    `line.cause` for each mismatch / missing, in §10.7 order. `Remediation(id: str, action: str, params: dict)`;
    the ids are deterministic `sha256(action + json(params))[:16]`.
  - `ladder.step(prev: dict, *, had_mismatch: bool, remediation_done: list[str], issued_ids: list[str],
    confirmed_fy_resync_completed: bool, fy_for_offer: date) -> dict` (the new ladder dict), plus
    `ladder.visible_state(ladder) -> str`.
  - `bisect.first_diverging_month(evaluations: list[tuple[date, bool]]) -> date | None` (ordered oldest first;
    `bool` = had a mismatch).
  - `opsignal.integrity_event(ws_id, run_id, rung, status, lines) -> dict` and
    `opsignal.emit(event: dict) -> None` (`logging.getLogger("v2.ops.integrity").info(json.dumps(event))`).
    `bucket(d: Decimal) -> str` is one of `"<₹1k"`, `"<₹1L"`, `"<₹1Cr"`, `"≥₹1Cr"`.

- [ ] **Step 1: Failing tests**

`test_classify.py`: one test per §10.7 row, each built from minimal `Line`s:
- `test_tb_row_to_no_ledger_is_masters_gap_refetch_masters`
- `test_live_ledger_absent_is_ledger_deleted_reconcile_masters`
- `test_diff_equal_to_one_cancelled_voucher_amount_is_flag_filter_inverted_no_remediation`
- `test_equal_and_opposite_pair_is_voucher_missed_or_duplicated_month_bisect` (params list only the month-ends
  without a stored TB)
- `test_three_bs_ledgers_diff_like_their_anchor_rows_is_anchor_wrong`
- `test_single_ledger_diff_is_ledger_gap_refetch_ledger_vouchers`
- `test_forex_mismatch_is_forex_gap`
- `test_group_only_is_group_walk_wrong_no_remediation`
- `test_order_is_first_match_wins`: a line that fits both the pair rule and the single-ledger rule → pair.
- `test_remediation_ids_are_stable`.

`test_ladder.py`, every §10.8 transition:
- `test_clean_run_resets_to_ok_and_clears_offer`
- `test_first_mismatch_after_ok_is_suspect_heal_0`
- `test_mismatch_after_suspect_with_remediation_done_is_alert_heal_1`
- `test_mismatch_without_remediation_done_keeps_state_and_reissues`
- `test_heal_attempts_2_offers_resync_for_fy`
- `test_after_confirmed_fy_resync_still_mismatch_is_hard_alert`
- `test_visible_state_hides_suspect`.

`test_bisect.py`: `test_first_diverging_month_oldest_first`, `test_none_when_all_clean`.

`test_opsignal.py`:

```python
import json
import logging
from decimal import Decimal as D

from v2.cloud.parity.model import Line
from v2.cloud.parity.opsignal import bucket, emit, integrity_event


def test_buckets():
    assert [bucket(D(x)) for x in ("999.99", "1000", "99999.99", "100000", "9999999.99", "10000000")] == [
        "<₹1k", "<₹1L", "<₹1L", "<₹1Cr", "<₹1Cr", "≥₹1Cr"]


def test_event_has_no_names_guids_or_exact_amounts(caplog):
    lines = [Line("ledger", "710de34a-…-000000dd", "Apex Technologies Pvt Ltd", D("-62800.00"), D("-62700.00"),
                  D("-100.00"), "mismatch", "ledger_gap")]
    ev = integrity_event("ws-1", "run-1", 1, "suspect", lines)
    text = json.dumps(ev)
    assert "Apex" not in text and "000000dd" not in text and "62800" not in text and "100.00" not in text
    assert ev["cause_counts"] == {"ledger_gap": 1} and ev["mismatch_count"] == 1 and ev["max_abs_diff_bucket"] == "<₹1k"
    with caplog.at_level(logging.INFO, logger="v2.ops.integrity"):
        emit(ev)
    assert caplog.records[-1].name == "v2.ops.integrity"
```

- [ ] **Step 2: Run → FAIL; implement; run → PASS**

`uv run --project v2 pytest v2/tests/cloud/unit/test_classify.py v2/tests/cloud/unit/test_ladder.py v2/tests/cloud/unit/test_bisect.py v2/tests/cloud/unit/test_opsignal.py -q`

- [ ] **Step 3: Commit**

```bash
git add v2/cloud/parity/classify.py v2/cloud/parity/ladder.py v2/cloud/parity/bisect.py v2/cloud/parity/opsignal.py \
        v2/tests/cloud/unit/test_classify.py v2/tests/cloud/unit/test_ladder.py v2/tests/cloud/unit/test_bisect.py \
        v2/tests/cloud/unit/test_opsignal.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 10b — parity cause classifier, escalation ladder, month-bisect, ops signal (no business data)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** §10.7 order; the ladder never starts a resync; the ops signal leaks nothing. Tracker: S1.22
✅, S1.24 ✅.

---

## Task 10c: Parity engine + `POST /parity` + `last_parity` + preconditions

**Spec:** §7.14, §10.1 (all six preconditions), §10.2, §10.9 (bisect scope), §10.10, D10, §14 scenario 16, §15.5
(status column, every row). **Tracker:** S1.21, S1.23.

**Files:**
- Create: `v2/cloud/parity/engine.py`
- Modify: `v2/cloud/api/sync.py`
- Test: `v2/tests/cloud/db/test_parity_api.py`

**Interfaces:**
- Consumes: everything from 10a/10b, snapshots (9), coverage `edges` (7).
- Produces: `engine.run_parity(session, sw, body: ParityRequest, settings, clock) -> dict` (the §7.14 response).
  Internally: `_preconditions(...) -> str | None` (the abort status), `_load_inputs(...) -> (ledgers, sums,
  snapshots)` using the covering index (one `SUM … GROUP BY ledger_guid` per range), `_store(run, lines)`, and a
  `last_parity` update in the same transaction.

- [ ] **Step 1: Failing DB tests** (`test_parity_api.py`). Seed data through the real `/batches` +
`/snapshots` endpoints, never by direct inserts, so ingest and parity are proven together. Build the fixtures with
the Task 8c helpers.

- `test_counters_moved_aborted_moving_no_lines_ladder_unchanged` (`p19_A_capture_moving_counters_start/end.xml`
  counters).
- `test_quiet_counters_pass_quiescence` (`p19_A_capture_quiet_1_counters_*`).
- `test_counters_before_not_cursor_aborted_behind`.
- `test_no_verified_span_aborted_incomplete`, and `test_first_sync_state_aborted_incomplete`.
- `test_missing_anchor_snapshot_aborted_incomplete_with_capture_remediation` → `remediation[0].action ==
  "capture_snapshot"`, `params == {"report_type": "trial_balance_ledgerwise", "as_on": "<E-1>"}`.
- `test_tb_imbalance_changed_same_altmstid_discarded_stale` (D10): the first run records the baseline; the second
  with the same `alt_mst_id` and an imbalance moved by ₹5 → `discarded_stale`; the ladder is unchanged.
- `test_imbalance_moved_with_new_altmstid_rebaselines_and_runs`.
- `test_all_match_ok_and_last_parity_persisted` (§14.16): a small synthetic-free B slice. Masters + the Sept 2022
  vouchers + mirrored balances computed **from the same captures'** figures (`p16_B_ledgers_asof_2023-03-31.xml` is
  C45-clamped: do **not** use it; use the ledgerwise TB of G2 or its FakeBooks twin) → `ok`. The `parity_runs` row
  plus one `parity_lines` row per compared ledger/group; `last_parity` equals the run; re-read in a fresh session.
- `test_forex_revaluation_ok_with_match_revalued_18387` (§15.5 row 2).
- `test_forex_sale_missing_suspect_face_mismatch` (row 3): omit `[S0-B:101]` from ingest.
- `test_one_voucher_missing_suspect_voucher_missed_or_duplicated` (row 4).
- `test_ladder_alert_then_resync_offered_after_two_heals` (row 5): three runs with `remediation_done` = the
  previous ids.
- `test_hard_alert_after_confirmed_fy_resync_still_wrong` (row 6).
- `test_resyncing_fy_excluded_from_verified_span` (row 13).
- `test_bisect_scope_returns_first_diverging_month`, and `test_bisect_missing_month_ends_aborted_incomplete`.
- `test_parity_runs_persist_across_requests_and_ladder_survives_restart`: dispose the engine, build a new
  `create_app`, `GET sync-status` → the same `last_parity`, and `ladder` read from the DB.
- `test_ops_signal_emitted_once_per_non_ok_run` (caplog on `v2.ops.integrity`).
- `test_sync_status_shows_ok_for_suspect`.

- [ ] **Step 2: Run → FAIL; implement `engine.py` + the route; run → PASS**

`TEST_DATABASE_URL=… uv run --project v2 pytest v2/tests/cloud/db/test_parity_api.py -q 2>&1 | tee logs/v2-s1-db-task10c.log`

The precondition order is literal, §10.1 1→6, and each abort stores a `parity_runs` row with that status and zero
lines, and returns without touching the ladder.

- [ ] **Step 3: Commit**

```bash
git add v2/cloud/parity/engine.py v2/cloud/api/sync.py v2/tests/cloud/db/test_parity_api.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 10c — parity engine + POST /parity (quiescence/behind/incomplete/stale guards, last_parity)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** every §15.5 row has a test; aborts never change the ladder; parity inputs come from the
covering index (`EXPLAIN` in a test comment). Tracker: S1.21 ✅, S1.23 ✅.

---

## Task 11: Maintenance slices + `purge` CLI

**Spec:** §4.9, D20, Q5, Q21, Q22, Q23, §14 scenarios 16 (retention), 18, 20, S1-R9. **Tracker:** S1.23 (retention
part), S1.25 (partial).

**Files:**
- Modify: `v2/cloud/sync/maintenance.py`, `v2/cloud/cli.py`, `v2/cloud/__main__.py`
- Create: `v2/cloud/sync/purge.py`
- Test: `v2/tests/cloud/db/test_maintenance_purge.py`

**Interfaces:**
- Produces:
  - `maintenance.run_slice(session, sw, settings, clock) -> SliceReport(raw_nulled: int, parity_runs_pruned: int,
    parity_lines_pruned: int, batches_pruned: int, storage_estimate_bytes: int, storage_alert: bool,
    elapsed_s: float)`. Each sub-step uses `LIMIT settings.maintenance_slice_rows` and stops once the elapsed time
    passes `maintenance_slice_seconds`.
  - `maintenance.AVG_ROW_BYTES: dict[str, int]` (a constant table: measure `avg(pg_column_size(t.*))` on the
    Task 12 B dataset once, write the numbers in with a dated comment).
  - `purge.purge(session_factory, *, workspace_id: UUID | None, now: bool, clock) -> dict[str, int]` (row counts per
    table, FK order `V2_TABLES`). CLI: `python -m v2.cloud purge [--workspace ID] [--now]`, which logs counts only.

- [ ] **Step 1: Failing tests**

- `test_raw_window_follows_ist_fy` (Review Focus 4): coverage to FY 2026-27, clock 2027-03-31T20:00Z (= 1 April
  2027 IST) and `add_fy 2027-04-01` → the FY 2025-26 vouchers' `raw` is nulled by slices; FY 2026-27 is kept.
- `test_raw_purge_is_bounded_per_slice` (§14.20): 12 vouchers, `maintenance_slice_rows=5` → 5, 5, 2 across three
  heartbeats.
- `test_parity_retention_7_days_matches_90_days_mismatches` (§14.16): lines aged 8 d match → pruned; 8 d mismatch
  → kept; 91 d runs → pruned with their lines.
- `test_batch_log_retention_90_days`.
- `test_storage_alert_sets_flag_over_threshold` (`storage_alert_bytes=1`) and emits an ops signal with no names.
- `test_purge_now_deletes_only_that_workspaces_rows` (§14.18): two bound workspaces with data; W1 soft-deleted;
  `purge --now --workspace W1` → every v2 table has 0 rows for W1 and W2's counts are unchanged; `users` /
  `workspaces` rows untouched.
- `test_purge_respects_30_day_grace`: deleted 29 days ago → nothing; 31 days → purged. The grace clock is
  `workspaces.updated_at` of the deleted row (the only timestamp the current app sets on soft-delete; say so in the
  test docstring).
- `test_deleted_workspace_next_call_410_and_device_revoked` (§14.18 first half).
- `test_purge_cli_logs_counts_only` (`capsys`: no GUIDs, no names).

- [ ] **Step 2: Run → FAIL; implement; run → PASS**

`TEST_DATABASE_URL=… uv run --project v2 pytest v2/tests/cloud/db/test_maintenance_purge.py -q 2>&1 | tee logs/v2-s1-db-task11.log`

- [ ] **Step 3: Commit**

```bash
git add v2/cloud/sync/maintenance.py v2/cloud/sync/purge.py v2/cloud/cli.py v2/cloud/__main__.py \
        v2/tests/cloud/db/test_maintenance_purge.py
git commit -F - <<'MSG'
feat(bi/v2): S1 task 11 — bounded heartbeat maintenance (raw window, retention, storage alert) and purge CLI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** every slice bounded by rows **and** time; purge touches v2 tables only, in FK order.
Tracker: S1.23 ✅ (retention), S1.25 🟡.

---
## Task 12: Real-data parity (A, B) + FakeBooks end-to-end through the API + restart round-trips

**Spec:** §0 success criteria 1–3, §13.1, §13.2, §13.3 (fallbacks), §16 "Real-data parity", "API integration",
CLAUDE.md "Test reality" rules 2, 3, 6 and 7 (adapted: restart instead of page reload), Review Focus 1, ambiguities
A4 and A5. **Tracker:** S1.25, S1.20–S1.23 (real-data proof).

**Files:**
- Modify/Create: `v2/tests/cloud/realdata.py` (the full assembler), `v2/tests/cloud/fakeb.py`
- Test: `v2/tests/cloud/unit/test_assembler.py`, `v2/tests/cloud/unit/test_realdata_parity_a.py`,
  `v2/tests/cloud/unit/test_realdata_parity_b.py`, `v2/tests/cloud/unit/test_realdata_midbackfill.py`,
  `v2/tests/cloud/db/test_state_matrix_api.py`, `v2/tests/cloud/db/test_end_to_end_fakeb.py`,
  `v2/tests/cloud/db/test_restart_roundtrip.py`

**Interfaces:**
- Produces (`realdata.py`):
  - `Assembled(company_guid, books_from, masters: list[dict], vouchers: list[dict], synthetic_ids: dict[str,
    str], sources: dict[str, list[str]])`. `synthetic_ids` maps name → the gap id (`"G4"`); `sources` maps each wire
    key to the capture file(s) that supplied it.
  - `assemble_a() -> Assembled`, `assemble_b_fy2022() -> Assembled`, `b_masters()`, `a_masters()` (used since 8c).
  - The join rules (A4): vouchers by `guid` (p06 lines ⊕ `p04_A_voucher_full` `alterid` ⊕ `p16_A_vouchers_fy`
    flags); masters by cleaned `name` per kind (the `p04_A_*_full` identity ⊕ the `p25_A`/`p16_A` fields ⊕ the
    `s1_*` G4 identity). The assembler **only copies keys**; a key no capture holds stays absent, except `guid`/
    `alterid` under the A4 synthetic-identity rule.
  - `pure_parity(assembled, *, snapshots: dict, mirrored: dict, as_on: date, verified_edge: date) -> ParityResult`
    runs 8a/8b/10a/10b in memory (no DB) for the unit-level real-data tests.
- Produces (`fakeb.py`): `fakeb_company_b(tmp_path_factory) -> FakeB` (session-scoped and cached). It loads company
  B into `FakeBooks` via `load_company_b` with the `_empty_b`/operator pattern of `v2/tests/probes/test_company_b.py`.
  `FakeB.month_xml(fy_start, month) -> str` uses `v2.probes.reads.fill_month_request` + `VOUCHER_MONTH_FIELDS`
  through `FakeBooks.transport()`. `FakeB.tb_xml(as_on, ledgerwise: bool)`, `FakeB.ledgers_xml()` and
  `FakeB.counters()` work the same way. **Test-only**: `v2.cloud` never imports it (the isolation test covers
  that).

- [ ] **Step 1: Failing assembler tests** (`test_assembler.py`, Review Focus 1)

```python
from v2.tests.cloud.realdata import CAPTURES_A, assemble_a, assemble_b_fy2022


def test_assembler_never_fills_a_value_no_capture_holds():
    a = assemble_a()
    for v in a.vouchers:
        d = v["data"]
        for key in d:
            if key in ("ledger_entries", "inventory_entries"):
                continue
            assert a.sources.get(key), f"{key} has no source capture"
    assert all(set(a.sources[k]) <= set(CAPTURES_A) for k in a.sources)


def test_assembler_marks_synthetic_identities_by_gap_id():
    b = assemble_b_fy2022()
    assert set(b.synthetic_ids.values()) <= {"G4"}
    for m in b.masters:
        if m["data"]["name"] in b.synthetic_ids:
            assert "-fx-" in m["data"]["guid"]


def test_company_a_has_50_vouchers_with_alterid_and_flags_joined():
    a = assemble_a()
    assert len(a.vouchers) == 50 and all({"alterid", "iscancelled", "isoptional", "ispostdated"} <= set(v["data"])
                                         for v in a.vouchers)


def test_company_b_fy2022_has_240_vouchers_incl_two_usd():             # exit gate note: FY 2022-23 now 240
    b = assemble_b_fy2022()
    assert len(b.vouchers) == 240
    assert sum(1 for v in b.vouchers if any("@" in e["amount"] for e in v["data"]["ledger_entries"])) == 2
```

(`CAPTURES_A` is the literal tuple of company A capture filenames the assembler may read, defined in
`realdata.py` and imported. Check the 240 count against the twelve `p21_B_fy2022_month_*` files at run time: if
they sum to another number, the test asserts that sum and says why in a comment. The exit gate records 240.)

- [ ] **Step 2: Run → FAIL; implement the assembler; run → PASS**

`uv run --project v2 pytest v2/tests/cloud/unit/test_assembler.py -q`

- [ ] **Step 3: Real-data parity tests** (pure, no DB)

`test_realdata_parity_a.py`:
- `test_company_a_fy2025_ok` (§0 criterion 2): the assembled A + mirrored `p16_A_ledgers.xml` + group TB
  `p16_A_tb_fy_end.xml` + ledgerwise `p17_A_tb_exploded_isledgerwise.xml` + anchor G1
  (`s1_A_tb_ledgerwise_asof_2025-04-01.xml`, skip if absent) → every BS ledger `match`, nominal rung-1
  `not_applicable`, rung-2 nominal `match`, `Profit & Loss A/c` `not_applicable`, status `ok`.
- `test_company_a_fy2025_ok_anchor_derived_fallback` (runs always; A5-style weak fallback, docstring says so):
  anchor_L = ledgerwise TB FY-end row − Σ our FY lines. This proves the mirrored-vs-TB consistency and the plumbing,
  **not** the anchor.
- `test_company_a_imbalance_non_zero_accepted_as_baseline` (D10).
- `test_company_a_post_dated_and_future_vouchers_counted` (`p16_A_post_dated_voucher.xml`,
  `p16_A_ledgers_with_post_dated.xml`).
- `test_company_a_bills_snapshots_parse_to_seed_residuals` (points at the Task 9 test; one assertion that the A
  bills totals are available to the parity inputs).

`test_realdata_parity_b.py`:
- `test_company_b_2023_rung2_ok_with_forex_18387` (§0 criterion 2): assembled B FY 2022-23 + TB
  `p18_B_tb_asof_2023-03-31.xml` + ledgerwise G2 (`s1_B_tb_ledgerwise_asof_2023-03-31.xml`, skip if absent) +
  books-start anchor G6 or the dataset (A5, the test id carries `anchor_source`) → status `ok`, the USD party
  `match_revalued` with `unrealised 183.87`, `Export Sales` `match`, `forex_unrealised_total == 183.87`.
- `test_company_b_2023_row_removed_forex_revaluation_unexplained`: the same inputs with the `Unadjusted Forex
  Gain/Loss` cell dropped → the USD party `mismatch`, cause `forex_revaluation_unexplained`.
- `test_company_b_forex_tb_row_form_matches_g2` (S1-R2): if G2 exists, assert which form the USD ledger's TB row
  has (plain INR vs expression) and that `parse_cells` handles it. Skip otherwise, with reason `"G2 not captured"`.
- `test_company_b_seeded_faults` (the §15 fixture matrix; parametrized, each fault applied to a **copy** of the
  assembled data): `drop_usd_sale_101` → `forex_face_mismatch`; `drop_one_inr_sale` →
  `voucher_missed_or_duplicated`; `duplicate_one_receipt` → `voucher_missed_or_duplicated`;
  `flip_cancelled_flag_on_201` → `flag_filter_inverted`; `drop_a_ledger_master` → `masters_gap`;
  `shift_anchor_by_1000_on_3_ledgers` → `anchor_wrong`.

`test_realdata_midbackfill.py` (FakeBooks B, §16 "Mid-backfill (R30 regression)"):
- `test_mid_backfill_ok_via_anchor_as_on_31_03_2024`: window FY 2024-25/2025-26 complete, older pending; all-time
  balances far from the window sum → `ok` using the FakeBooks ledgerwise TB as-on 31-03-2024.
- `test_complete_history_switches_to_books_start_anchor` (D9): mark all FYs complete → `plan()` gives `as_on ==
  books_from`, and the result is still `ok`.

Run: `uv run --project v2 pytest v2/tests/cloud/unit/test_realdata_parity_a.py v2/tests/cloud/unit/test_realdata_parity_b.py v2/tests/cloud/unit/test_realdata_midbackfill.py -q -rs`
(`-rs` prints every skip reason, so a missing G-capture is visible in the log).

- [ ] **Step 4: API-level suites** (DB)

`test_state_matrix_api.py` covers the full §15.1 matrix as one parametrized test per endpoint group:
`test_auth_matrix`, `test_agent_workspaces_matrix`, `test_bind_matrix`, `test_sync_endpoints_matrix` (each of
heartbeat/state/runs/batches/coverage/reconcile/snapshots/parity/relink × the 8 columns), `test_web_matrix`. The
cell values are exactly the spec table's. `—` cells are omitted, with a comment.

`test_end_to_end_fakeb.py`:
- `test_whole_first_sync_of_fakebooks_b_through_real_endpoints` (§16): login → bind → `POST /runs first_sync` →
  for each window month: masters batch (first month only), month vouchers batch via the transcoder from
  `FakeB.month_xml`, `PATCH /coverage` → mirrored `ledger_balance` batch → snapshots (TB, ledgerwise TB, anchor TB) →
  `PATCH /runs completed` → `POST /parity` → `sync_state == "ready"`, `last_parity.state == "ok"`. Then **backfill**
  one older FY (kind `backfill`) and assert `last_synced_at` didn't move and `oldest_complete_fy` did.
- `test_incremental_after_first_sync_moves_cursor_and_last_synced`.

`test_restart_roundtrip.py` (CLAUDE.md "Test reality" rule 7 adapted, spec §16). A helper `restart(client) ->
client2` disposes the engine and builds a **new** `create_app` on the same DB. After **every** state-changing call
in this list, restart and re-assert the **whole** user-visible state via `GET /state` + `GET sync-status` +
direct row counts:
- `test_restart_after_bind`
- `test_restart_after_takeover`
- `test_restart_after_batch_and_replay_still_replays` (the replay response survives the restart)
- `test_restart_after_coverage_ack`
- `test_restart_after_run_completion_cursor_persisted`
- `test_restart_after_restore_detected`
- `test_restart_after_relink`
- `test_restart_after_parity_ladder_state`
- `test_restart_after_quarantine_count`
- `test_restart_after_command_delivery_not_redelivered`

Each asserts the exact set of values (not only the attribute the call changed), and the message/command counts are
exactly one per action.

- [ ] **Step 5: Run the DB suites**

```bash
TEST_DATABASE_URL=postgresql+asyncpg://nuvanta-mac-3@localhost/tallyagent_test \
  uv run --project v2 pytest v2/tests/cloud/db/test_state_matrix_api.py v2/tests/cloud/db/test_end_to_end_fakeb.py \
  v2/tests/cloud/db/test_restart_roundtrip.py -q -rs 2>&1 | tee logs/v2-s1-db-task12.log
```

- [ ] **Step 6: Commit**

```bash
git add v2/tests/cloud/realdata.py v2/tests/cloud/fakeb.py v2/tests/cloud/unit/test_assembler.py \
        v2/tests/cloud/unit/test_realdata_parity_a.py v2/tests/cloud/unit/test_realdata_parity_b.py \
        v2/tests/cloud/unit/test_realdata_midbackfill.py v2/tests/cloud/db/test_state_matrix_api.py \
        v2/tests/cloud/db/test_end_to_end_fakeb.py v2/tests/cloud/db/test_restart_roundtrip.py
git commit -F - <<'MSG'
test(bi/v2): S1 task 12 — real-data parity on A and B (incl. the 183.87 forex revaluation), seeded faults,
FakeBooks B first sync through the API, full §15.1 matrix, restart round-trips

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** the reviewer checks every real-data test against Review Focus 1 (no invented values; every
skip names its G-gap), that each restart test asserts the whole state, and that the seeded-fault causes match
§10.7. Tracker: S1.25 🟡 → ✅ after Task 15's full run; S1.20–S1.23 proof columns get the real-data test names.

---

## Task 13: Whole-branch code review → `docs/code-review-bi-s1-<date>.md`, fix round

**Spec:** §18 item 13; CLAUDE.md "Code review after every implementation". **Tracker:** S1.26.

**Files:**
- Create: `docs/code-review-bi-s1-<YYYY-MM-DD>.md` (the date the review runs)
- Modify: whatever the fixes touch (in `v2/` only)

- [ ] **Step 1: Dispatch the whole-branch review**

Use superpowers:requesting-code-review with the most capable model. The scope is `git diff <task-1-start-sha>..HEAD
-- v2/`, plus this plan, the spec, and the per-task Minors collected at each checkpoint. The reviewer brief must
cover:
1. Global Constraints, one by one, with evidence (grep for `Float`, `backend`, `v2.agent` in `v2/cloud`, `logger`
   calls with a request body, and face × rate outside `forex.face_check`).
2. The spec's §14 scenarios 1–20 → the test that proves each (a table in the review doc).
3. The §15 matrices → coverage (cells with no test = findings).
4. Transaction boundaries in ingest/binding/parity (a partial write on error = Critical).
5. Security: token handling, cross-tenant (R19), refresh reuse, zip bomb, rate limits, SQL built from input.
6. Review Focus items 1–5.
7. `git diff --name-only <base>..HEAD | grep -v '^v2/\|^docs/'` must be empty (§0 criterion 4).

- [ ] **Step 2: Write the review doc**

`docs/code-review-bi-s1-<date>.md` has these sections: Scope (base..head SHAs), Verdict, Critical / Important / Minor
findings (each with file:line, why, fix), a §14 scenario → test table, a §15 matrix coverage table, "Suites NOT run"
(`e2e_live`, tier B/C, the current app's suites if not run), and Review Focus results.

- [ ] **Step 3: Fix round**

Fix every Critical and Important finding, one commit per finding, message `fix(bi/v2): S1 review <ID> — <what>`
(+ trailer). Each fix gets a failing test first (superpowers:test-driven-development). Minors are fixed or listed as
deferred in the review doc with a reason. Re-run the affected suites after each fix.

- [ ] **Step 4: Commit the review doc**

```bash
git add docs/code-review-bi-s1-<date>.md
git commit -F - <<'MSG'
docs(bi/v2): S1 whole-branch code review + fix round record

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```

**Review checkpoint:** the controller reads the review doc and confirms that zero Critical/Important findings remain
open. Tracker: S1.26 ✅ with the doc path + fix SHAs.

---

## Task 14: S1 hardening backlog from S0 (probe harness, not S1 code)

**Spec:** §18 item 14; S0 exit gate exceptions (f) and (g); `docs/code-review-bi-s0-part2-retro-2026-09-25.md`
M2, M3, M7, M8. **Tracker:** the S0 exceptions (f)/(g) note ("M2/M3/M7/M8 done before any tier-C A re-run").

These are four separate small fixes in `v2/probes`. Each follows TDD with its own commit. (g) M1–M6 and (f) M1, M4–M6,
M9, M10 stay deferred. List them in the Task 15 report unchanged.

**Files / tests per item:**

- [ ] **M2 — probe 13 must see the counters rise before judging "fall back"**
  - Modify: `v2/probes/p13_backup_restore.py` (≈ lines 61-62, 85-88)
  - Test: `v2/tests/probes/test_p13_backup_restore.py::test_counters_not_risen_is_blocked_not_different` and
    `::test_missing_altvchid_is_blocked_not_different`. They drive the probe with a FakeBooks whose throwaway
    doesn't move the counter / whose counters read lacks `ALTVCHID` → the outcome is `BLOCKED` (or inconclusive),
    never `DIFFERENT`.
  - Fix: `if not rose or before is None or after is None: return ctx.block("counters did not rise / unreadable")`.
  - Run: `uv run --project v2 pytest v2/tests/probes/test_p13_backup_restore.py -q`
  - Commit: `fix(bi/v2): S0 retro M2 — probe 13 blocks when counters never rose or are unreadable` + trailer.

- [ ] **M3 — copy to a sibling temp folder, then swap; restart Tally in `finally`**
  - Modify: `v2/probes/operator/company_a.py` (`replace_company_folder` ≈16-25, `backup_company` ≈63-71)
  - Test: `v2/tests/probes/test_company_a.py::test_failed_copy_leaves_target_intact` (monkeypatch
    `shutil.copytree` to raise halfway → the original target is byte-identical afterwards),
    `::test_backup_reusing_tag_keeps_old_backup_until_new_copy_succeeds`,
    `::test_backup_restarts_tally_even_when_copy_fails`.
  - Fix: `tmp = target.with_name(target.name + ".tmp-<pid>")`, `copytree(source, tmp)`, then `os.replace` via a
    rename of the old → `.old`, `tmp` → target, `rmtree(.old)`; wrap in `try/finally: control.start(...)`.
  - Commit: `fix(bi/v2): S0 retro M3 — company folder restore/backup copy-then-swap, Tally restarted in finally` +
    trailer.

- [ ] **M7 — probe 10's "popup not raised" path must leave a cleanup note**
  - Modify: `v2/probes/p10_error_shapes.py` (≈57-70), `v2/probes/setup/writes.py` (≈737-747)
  - Test: `v2/tests/probes/test_p10_error_shapes.py::test_popup_not_raised_records_stock_group_cleanup_note`: a
    FakeBooks without the stock group "Electronics" → the probe result has an `on_abort`/cleanup note naming
    "delete stock group 'Electronics' created by probe 10".
  - Commit: `fix(bi/v2): S0 retro M7 — probe 10 notes the stock group it created when no popup was raised` + trailer.

- [ ] **M8 — the auto-operator forgets voucher refs after a restore**
  - Modify: `v2/probes/operator/auto.py` (`_restore_company`, `_restore_seed`: `self.vouchers.clear()`)
  - Test: `v2/tests/probes/test_auto_operator.py::test_probe13_twice_in_one_process_does_not_block_on_ref`, and
    `::test_restore_clears_voucher_refs`.
  - Commit: `fix(bi/v2): S0 retro M8 — auto-operator clears voucher refs on restore` + trailer.

- [ ] **Step 5: Run the probe suite**

`uv run --project v2 pytest v2/tests/probes -q` → all pass (the count = baseline probe count + the new tests).

**Review checkpoint:** one reviewer pass over the four commits (task-scoped). These are probe-harness changes, so the
reviewer confirms no probe verdict logic changed beyond the named minors, and that no live run is needed (tier A).
Tracker: exceptions (f) note updated, "M2/M3/M7/M8 ✅ <SHAs>".

---

## Task 15: Final verification — full v2 suite, DB suite, current DB suite unaffected, docs

**Spec:** §0 success criteria 1–4, §16 "Regression / isolation", "Not run in S1". CLAUDE.md "Feature development
lifecycle" steps 7–8. **Tracker:** S1.25 ✅; the S1 stage row; the "Resume here" block.

- [ ] **Step 1: Full v2 suite without a DB** (the DB tests skip)

```bash
uv run --project v2 pytest v2/tests -q -rs 2>&1 | tee logs/v2-s1-final-nodb.log
```
Expected: 0 failures. Passed = pre-flight baseline + every new non-DB test. Every skip reason is a DB-unset reason
or a named G-gap.

- [ ] **Step 2: Full v2 suite with the DB**

```bash
TEST_DATABASE_URL=postgresql+asyncpg://nuvanta-mac-3@localhost/tallyagent_test \
  uv run --project v2 pytest v2/tests -q -rs 2>&1 | tee logs/v2-s1-final-db.log
```
Expected: 0 failures, skips only for missing G-captures.

- [ ] **Step 3: The current app's DB suite still runs on the shared test DB** (Review Focus 2)

Run it **after** Step 2 has finished (never concurrently). This is the CLAUDE.md command, ~14 min:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://nuvanta-mac-3@localhost/tallyagent_test ANTHROPIC_API_KEY=test-key \
  PYTHONPATH=. uv run pytest tests/integration/ tests/e2e/test_db_smoke.py tests/e2e/test_db_data_entry.py \
  tests/e2e/test_db_data_entry_group_b.py tests/unit/test_dedup.py -q 2>&1 | tee logs/db-suite-run-after-s1.log
```
Expected: the same result as the last recorded green run (249 passed on 2026-06-23 per CLAUDE.md; compare with the
current count). A `drop_all` failure mentioning a `v2` table = the v2 teardown is broken → fix it in `conftest.py`
before continuing.

- [ ] **Step 4: Isolation and diff scope** (§0 criterion 4)

```bash
uv run --project v2 pytest v2/tests/test_isolation.py -q
git diff --name-only <pre-flight sha>..HEAD | grep -v '^v2/\|^docs/' && echo "SCOPE VIOLATION" || echo "scope ok"
git status --short | grep -v '^??'      # nothing staged or modified that is untracked noise
```

- [ ] **Step 5: Manual real-app pass** (CLAUDE.md "Test reality" rule 6, adapted: there is no UI in S1)

Start the app against the **dev** DB only after a `python -m v2.cloud migrate` there. **Ask the user first:** that
creates v2 tables in the dev `tallyagent` DB, a durable change. Then:
`V2_DEVICE_TOKEN_SECRET=… PYTHONPATH=. uv run --project v2 uvicorn v2.cloud.main:app --port 8100 2>&1 | tee logs/v2-s1-manual.log`
(`main.py` exposes a module-level `app = create_app()` for uvicorn). With `curl`, walk through: login with a real dev
user → `GET /api/agent/workspaces` → bind a real workspace → heartbeat → `GET sync-status` with the web token from
the current app. Record the responses (no bodies with business data) in the log. If the user declines, record
"manual pass not run — reason" in the report.

- [ ] **Step 6: Docs** (the controller does the tracker/roadmap; the implementer drafts only if asked)

- Tracker `docs/plans/2026-09-22-bi-part1-tracker.md`: rows S1.0–S1.26 ✅ with proof (test file names, log paths,
  SHAs); a dated change-log row; "Resume here" rewritten to "S1 built; next: S2 spec/plan", listing open G-gaps
  and the deferred Minors.
- `docs/roadmap.md` Set C: S1 closed.
- Part 1 spec header: a dated "Changed <date> (S1 built)" line if any decision was reversed during the build (list
  which, else "no design change").
- `LESSONS.md` §15: add a rule only for a **new** Tally fact learned during S1 (for example the G2 answer: "a
  forex ledger's ledger-level TB row exports as <form>"). Nothing else.
- `v2/README.md`: a "Cloud (S1)" section with `python -m v2.cloud migrate|purge`, the `uvicorn` line, the port, and
  the `V2_*` settings table.

- [ ] **Step 7: Commit**

```bash
git add v2/README.md
git commit -F - <<'MSG'
docs(bi/v2): S1 cloud — README run/migrate/purge instructions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
MSG
```
(The controller commits the tracker/roadmap/spec/LESSONS updates separately.)

- [ ] **Step 8: Report**

Report: the final counts (no-DB / DB / current DB suite), the log paths, the review doc path, the G-gaps still open,
the deferred Minors, and **suites NOT run**: live Tally (tier B) apart from Task 0, tier C, `e2e_live`, the eval
framework, the frontend suites (untouched by S1). Then finish the branch per superpowers:finishing-a-development-branch
(feature branch → `dev`, never `master`).

**Review checkpoint:** the controller verifies each claimed count against the logs before any "done" statement
(superpowers:verification-before-completion).

---

## Spec coverage map (self-review)

| Spec section | Task(s) |
|---|---|
| §1.1 contract / app / migration | 1, 2, 3 |
| §1.1 device auth, binding | 4, 5 |
| §1.1 bookkeeping (runs, cursors, coverage, state, heartbeat, restore, snapshots, reconcile) | 6, 7, 9 |
| §1.1 ingest (rules, limits, rung 0, quarantine, idempotency) | 8a, 8b, 8c |
| §1.1 parity (rungs 1–2, anchors, C47, guards, classifier, ladder, bisect, retention, ops signal) | 10a, 10b, 10c, 11 |
| §1.1 web reads/actions (`sync-status`, devices, commands) | 4, 6 |
| §1.1 ops CLI (`migrate`, `purge`) | 3, 11 |
| §2.1 Q1/Q4/Q5/Q6/Q19/Q21/Q22/Q23/Q25/Q28/Q30 | 6 / 5 / 11 / 8c+10b / 10a / 11 / 8c+11 / 11 / 6 / 4+6 / 5 |
| §2.2 D1–D32 | D1–D3: 1, 8a · D4: 10a · D5: 5 · D6–D7: 4, 5 · D8: 9 · D9: 10a · D10: 9, 10c · D11: 10a · D12: 8c · D13–D14: 8b, 8c · D15–D16: 7 · D17: 6 · D18–D19: 3, 8c, 9 · D20: 6, 11 · D21: 6 · D22: 3 · D23: 8a · D24: 4 · D25: 9 · D26: 1 (raw only) · D27: 2 · D28: 9 · D29: 10a · D30: 8b · D31: 1, 8a · D32: 5 (agent pick via `/api/agent/workspaces`) |
| §7.1–§7.16 | 4 (7.1–7.4, 7.16), 5 (7.5), 6 (7.6, 7.7, 7.13, 7.15), 7 (7.8, 7.10), 8c (7.9), 9 (7.11, 7.12), 10c (7.14) |
| §8.1–§8.6 | 4 (8.1), 6 (8.2, 8.4, 8.6), 7 (8.3), 8c (8.5) |
| §10.1–§10.10 | 10c (10.1, 10.2, 10.9), 8a (10.3), 10a (10.4–10.6), 10b (10.7, 10.8, 10.10) |
| §11 error codes | 4–10c (each code has a named test) |
| §12 steps 1–15 + performance | 8a (5, 7), 8b (6, 8, 10, 14), 8c (1–4, 9, 11–13, 15, perf) |
| §13 fixtures + gaps | 0, 1, 12 |
| §14 scenarios 1–20 | 5 (1, 2), 7 (3, 12, 13), 8c (4, 5, 6, 8, 9, 10, 11, 19, 20 insert side), 9 (7, 15), 6+7 (14), 10c+11 (16), 4/6 (17), 11 (18, 20) |
| §15.1–§15.5 | 12 (15.1 full), 8c (15.2), 7 (15.3, 15.4), 10a/10c/12 (15.5) |
| §16 test plan | 1 (contract), 2–11 (unit + DB), 12 (real-data, API, restart), 15 (regression) |
| §18 items 0–14 | P, 0–15 (item 13 → Task 13, item 14 → Task 14) |
