# S1 (BI v2 cloud sync server) — whole-branch code review

**Date:** 2026-09-29 · **Reviewer:** Task 13 final whole-branch reviewer (read-only) · **Plan:** `docs/plans/2026-09-25-bi-s1-cloud-plan.md` Task 13 Steps 1–2 · **Spec:** `docs/specs/2026-09-25-bi-s1-cloud-design.md`

## Scope

- **Range:** `dd81030..ee57571` (branch `feat/bi-s1-cloud`), `v2/` only. 127 files changed, +22,120 / −11.
- **How it was reviewed:** in passes, by reading the source directly (the diff is 1.2 MB). The passes were:
  1. `v2/contract/*`
  2. `v2/cloud` skeleton, auth and dependencies
  3. binding, state, commands, runs and coverage
  4. the ingest pipeline, validate, resolve, derive, store, reconcile and snapshots
  5. the parity engine, model, anchors, rungs 1–2, forex, classify, ladder, bisect and opsignal
  6. maintenance, purge, models, the migration and Alembic env
  7. the tests: a name inventory of all 268 DB tests plus targeted reads
  8. the per-task review files, for the deferred minors
- **Tests run for this review:** one temporary probe file (3 tests) was used to confirm I2, I3, I4, I6 and M1. It was written under `v2/tests/cloud/db/`, run, then deleted, and `git status v2/` is clean. The full suite was not re-run. The last green run is at 41ab9a4: 1883 passed (`logs/v2-s1-task12-fix1.log`).
- **§0 criterion 4:** `git diff --name-only dd81030..ee57571 | grep -v '^v2/\|^docs/'` prints nothing (grep exit 1). **Confirmed.**

## Verdict

**Ready to merge: With fixes. There are 0 Critical and 8 Important findings.**

The core of S1 is sound:
- Ingest is atomic.
- The idempotency claim row is correct under concurrency.
- The zip-bomb guard streams.
- Money is `Decimal`/`Numeric` end to end.
- Parity matches Tally on both real companies, including the ₹183.87 forex revaluation.
- Every cross-tenant check is in place.

The Important findings fall into three groups:
- **Sync can stop and not recover without help.** A resync the agent acknowledged cannot be opened (I3). A first sync sent without counters leaves NULL cursors, so no incremental or new first sync can run (I2).
- **The state shown to the user is wrong after recovery.** A completed resync stays "offered" (I4). An FY can be marked complete for months that were never synced (I6). Renaming a ledger produces a permanent false mismatch that climbs to `hard_alert` (I7).
- **Security and privacy gaps.** Business values can reach the server logs on a database error (I1). The password check on relink has no attempt limit (I5). The cross-tenant scenario §14.17 is only half-proven (I8).

Each has a small, local fix.

### Strengths (accurate praise, so the rest can be trusted)
- **Atomic, race-safe ingest.** Each batch is one transaction: the claim row, steps 9–13, the `sync_workspaces` updates and the `accepted` row (`pipeline.py:435-488`). A rejection rolls everything back, then records only the `rejected` row in a second short transaction, which never overwrites an `accepted` one (`pipeline.py:175-195`). `test_mid_batch_failure_leaves_zero_rows` proves it. The claim row takes `INSERT … ON CONFLICT DO NOTHING` + `FOR UPDATE` and has deterministic forced-order tests (Review Focus 5).
- **Streaming zip-bomb guard.** The inflater is bounded per 64 KiB (`pipeline.py:55-95`), and the wire-byte limit is enforced while the body streams.
- **Money and forex rules hold.** There is no `Float` and no face × rate outside `forex.face_check` (see the Global Constraints table). D3's rule is clear: a stated base becomes money; face, rate and currency go in their own columns.
- **Faithful parity engine.** It follows §10.1's precondition order. Every abort writes a run row and nothing else. The D10 baseline moves only in the store block. The ladder, `last_parity` and the lines are committed in one transaction (`engine.py:393-523`).
- **Strong test base.**
  - The full §15.1 matrix is tested at API level, over 9 routes × 8 auth states.
  - The 24-cell coverage matrix is pinned as a literal.
  - Real-data parity runs on A and B.
  - There are restart round-trips after every state-changing call.

## Global Constraints — evidence

| Constraint | Evidence | Result |
|---|---|---|
| Isolation: diff only `v2/`+`docs/` | grep above prints nothing | ✅ |
| No `backend`/`scripts`/`tests` imports; cloud ↛ agent/probes/tests; contract ↛ cloud/agent/probes | `grep -rnE "^\s*(from\|import) (backend\|scripts\|tests)\b" v2/cloud v2/contract`: none. `grep -rn "v2\.(agent\|probes)" v2/cloud v2/contract`: none. The `test_isolation.py::test_layer_rules_hold` and `test_layer_scanner_flags_each_rule` tests exist. | ✅ |
| `# Copied from:` headers | Present on `contract/{parse,tally_rules,transcode}.py`, `cloud/auth/{web_jwt,passwords}.py`. The two cloud files are **not** in `test_copied_headers.COPIED` (M15). | ✅ (M15) |
| Current tables read-only; only CREATE of new tables | No `INSERT/UPDATE/DELETE` against `users`/`workspaces` in `v2/cloud` (grep). `models/current.py` declares plain `Table`s with `info={"v2_readonly": True}`, and the Alembic `include_object` excludes them. `test_bind_never_writes_workspaces_table` covers it. No static "no write-mapped model" test (M15). | ✅ |
| Own Alembic chain | `alembic/env.py` `version_table="alembic_version_v2"`; `test_version_table_is_v2_and_current_alembic_version_untouched` | ✅ |
| Money: Decimal, `Numeric(18,2/4/6)`, never Float | `grep -rn "Float\|float" v2/cloud v2/contract` finds only non-money hits: `maintenance_slice_seconds`, rate-limit timestamps, and the `sync-status` display percent (`state.py:300`). Migration `MONEY/FACE/RATE/QTY` at `v2_001:22-25`. `test_money_columns_are_numeric_18_2_never_float`. | ✅ |
| Parse failure is an error, never 0; `""`→None; missing stays absent | `parse.amount` raises `unparseable_amount`. `_money_cols`/`apply_balance` never write 0 for `""` (F14). `rate("NaN/Nos")` → `Decimal('NaN')` is a deferred minor (Task 1). | ✅ (deferred) |
| Sign: debit negative | `rung1`/`model` docstrings plus `test_debit_is_negative_anchor_plus_lines`. D23 warning in `validate.py:183-184`. | ✅ |
| Forex D3: no face × rate outside the self-check | Multiplication grep: the only hit is `forex.py:47` (`closing_fx * closing_fx_rate`). The `parse.py:58,65` hits are sign multiplications (`* (-1 if …)`). | ✅ |
| Posting rule | `transcode.vouchers_from_xml` emits `ALLLEDGERENTRIES.LIST` only. `validate.py:229-231` rejects `ledgerentries_list` → `duplicate_posting_list`. | ✅ |
| D31 U+0004 strip | `parse.name` + `NameIndex` clean both sides (`resolve.py:55-80`) | ✅ |
| Tolerance ₹1.00 inclusive, face 0.01 | `model.compare` `<= tol`; `forex.FACE_TOL`; `test_tolerance_boundaries`. `classify` uses the hard-coded `model.TOL`, not the setting (M9). | ✅ (M9) |
| Retention 90/7/90, raw 2 FYs, batches 90 d | `maintenance.py:124-241` plus the tests in `test_maintenance_purge.py` | ✅ |
| Limits: 5 MB gzip / 50 MB inflated / 500 objects; 15 min access; 90 d sliding refresh, rotated; take-over login ≤ 10 min | `config.py:29-36`, `pipeline.read_body`, `agent_auth.refresh`, `binding._guard_takeover` | ✅ |
| No business data in logs | No logger call carries a request body. Every one was checked: `validate.py:315` (class/kind/index), `agent_auth.py:121,159` (device id), `state.py:153`, `maintenance.py:313`, `purge.py:68,76`, `opsignal.emit`. **But the SQLAlchemy engine does not set `hide_parameters=True`, so any DB error's traceback carries bound business values (I1).** | ❌ (I1) |
| App: port 8100, `V2_` prefix, no scheduler, slices ≤ 2 s / 5,000 rows | `config.py`; `maintenance.run_slice` with `_Budget`. Cascaded line deletes are unbounded (M18). | ✅ (M18) |
| Tests offline; `TEST_DATABASE_URL` only | `conftest.requires_db`; no live Tally/Claude in `v2/tests/cloud` | ✅ |

## Transaction boundaries (brief item 4)

| Path | Boundary | Partial write on error? |
|---|---|---|
| Ingest `/batches` | One session transaction: claim → store → quarantine → sw updates → `accepted` row → commit (`pipeline.py:435-481`). Any `ApiError` before commit (company mismatch, run closed, restore) rolls the claim back. A 500 rolls back everything. A rejection rolls back, then commits only the `rejected` log row (by design, D12/F10). | **None** (`test_mid_batch_failure_leaves_zero_rows`, `test_wrong_company_guid_409_company_mismatch_nothing_stored`) |
| Binding | `INSERT … ON CONFLICT DO NOTHING` → `SELECT … FOR UPDATE` → guards → `_rebind`/`_activate` → one commit (`binding.py:273-317`). Guards raise before commit. | None |
| Heartbeat | State + acks + deliveries flushed. Maintenance runs in a SAVEPOINT whose failure is swallowed and logged by class; then one commit (`state.py:141-160`). | None (`test_maintenance_failure_does_not_fail_or_roll_back_the_heartbeat`) |
| Runs open/patch | Checks come before mutation. `_interrupt_other_device_runs` is flushed but rolled back if a later 409 fires (no commit). | None |
| Reconcile | D28 guard and `master_in_use` are evaluated before any UPDATE; the route commits once. | None |
| Snapshots | A single atomic `INSERT … ON CONFLICT … WHERE captured_at < excluded` | None |
| Parity | `FOR UPDATE` on `sync_workspaces`. An abort writes only its run row. A compute writes run + lines + ladder + `last_parity` + baseline in one transaction. A `ResolveError` becomes 409 before commit. Ops events are emitted after the commit. | None |
| Maintenance | Per-sub-step statements inside the heartbeat's savepoint; each WHERE clause is idempotent | None |
| Purge | One transaction per workspace; a failing workspace is logged and skipped | None |

No Critical transaction finding. Lost updates without a lock are noted separately: heartbeat maintenance vs the parity ladder (M12).

## Security (brief item 5)

- **Token handling:**
  - Device access tokens use HS256 with a separate secret. Startup refuses a secret equal to the web secret (`config.py:69-72`). The token needs `typ=v2_device`, and `exp`/`iat` are checked on the injected clock.
  - Refresh tokens are 32 random bytes; only their SHA-256 is stored. Rotation is an atomic conditional UPDATE (`agent_auth.py:143-152`), and the race test is deterministic.
  - Web and device tokens cannot be swapped: both directions are tested.
  - Gap: refresh and `active_device` never re-check `users.is_active` (M10).
- **Refresh reuse:** a previous hash revokes the device (`refresh_reuse`), and the losing side of a race is treated the same way. Only one previous hash is kept, as the spec says. The ops event goes to the module logger, not `v2.ops.integrity` (M11).
- **Cross-tenant (R19):** checked on every `{ws}` route:
  - `active_device` check 3 compares the device row's `workspace_id` with the path.
  - Runs, batches, commands, reconcile, snapshots and parity all filter by `workspace_id`.
  - A run of another device returns 403 and an unknown run 404.
  - `_require_confirmed_resync_command` checks the command's workspace.
  - Web routes are owner-only and return 404.

  The code is correct. The spec's §14.17 test is incomplete (I8).
- **Zip bomb:** streamed and bounded (Review Focus 3 ✅). Non-batch bodies have no size cap (M16).
- **Rate limits:**
  - Per-email login counts failed attempts only, following the legacy behaviour.
  - Per-device limit: 600/min.
  - Refresh has no limit, and none is needed because the token has 256 bits of entropy.
  - **The relink password re-check is not throttled (I5).**
- **SQL built from input:** there is none.
  - `pipeline._load_name_index` builds `literal_column(f"'{kind}'")` from the constant `MASTER_MODELS` keys.
  - `maintenance._storage_estimate` and `purge._purge_workspace` build `f"… {table} …"` from the constant `V2_TABLES`.
  - Every value is a bound parameter.

---

## Critical

None.

## Important

### I1 — DB errors write business values into server logs (the engine lacks `hide_parameters=True`)
- **Where:** `v2/cloud/db.py:11-12` (`create_async_engine(url, pool_pre_ping=True)`). The same applies to the CLI engine built with `make_engine` and to `alembic/env.py:35`.
- **Why:** SQLAlchemy's `StatementError.__str__` appends `[parameters: …]` with the bound values. Any unhandled DB error in ingest becomes a 500 whose traceback uvicorn logs, and that traceback carries voucher narration, party and ledger names and amounts. Known ways to trigger it:
  - Two concurrent batches inserting the same new GUID give an IntegrityError (Task 8c deferred M6).
  - A `Numeric(18,2)` overflow.
  - A non-string `guid`/`vouchernumber` reaching asyncpg.

  This breaks the Global Constraint "No business data in logs" and Q6's R20 handling.
- **Fix:** `create_async_engine(url, pool_pre_ping=True, hide_parameters=True)`. Add a unit test that forces a DataError on a throwaway statement and asserts the logged or `str()`'d exception has no parameter values.

### I2 — Run bodies aren't validated; a `first_sync` without `counters_at_start` wedges the workspace
- **Where:**
  - `v2/cloud/sync/runs.py:55-69`: `RunCreate.counters_at_start: dict | None = None`, `RunPatch.cursor_after: dict | None`, `kind`/`status: str`.
  - `v2/cloud/sync/runs.py:72-81, 425-429`.
  - The contract's typed `v2/contract/models.py:132-145` (`Counters` required) is not the model the route uses.
- **Why:** confirmed with a probe.
  - Opening `first_sync` with no counters succeeds (200).
  - After the window acks, `PATCH completed` returns 200 with `sync_state = ready` and cursors `(NULL, NULL)`.
  - `incremental` then gets 409 `run_kind_not_allowed` because the cursors are NULL.
  - A new `first_sync` also gets 409 `run_kind_not_allowed` because the state is `ready`.
  - Parity then aborts `aborted_behind` forever.

  The only way out is a user-confirmed whole-company resync. Malformed counters (such as strings) become a DB `DataError` 500, which the agent retries forever and which also triggers I1.
- **Fix:**
  - Use the contract's `RunCreate`/`RunPatch`, or equivalent `Counters`-typed pydantic models, in the route.
  - Make `counters_at_start` required for every kind.
  - Make `cursor_after` required when an `incremental` is completed.
  - Use `Literal` for `kind`/`status`.
  - Add tests: missing counters → 422; incremental completed without `cursor_after` → 422.

### I3 — An acknowledged `confirm_resync` command can no longer open the resync (D16 path dead-ends)
- **Where:**
  - `v2/cloud/sync/commands.py:45-74`: any acked `delivered` command becomes `done`.
  - `v2/cloud/sync/runs.py:210-216`: a `full_resync` needs the command in `pending|delivered`.
- **Why:** confirmed with a probe.
  - Restore detected, then the user confirms the resync.
  - The heartbeat delivers the command. The next heartbeat acks it.
  - `POST /runs full_resync` then gets 409 `resync_not_confirmed`.

  §7.6 invites the agent to ack delivered commands on its next heartbeat, and nothing tells it not to ack a resync until the run exists. An agent that waits (for example for Tally to be `ours` and quiet) before starting a whole-company resync can never start it. Each re-confirm repeats the trap, so the workspace stays in `restore_detected`.
- **Fix:** do not let a heartbeat ack close a `confirm_resync`. Either:
  - ack it to `delivered` only (run completion already marks it `done`, `runs.py:369-373`), or
  - accept `done` commands that have no completed run in `_require_confirmed_resync_command`.

  Add a DB test for the sequence deliver → ack → open. Record the ack semantics for S2 in §7.6.

### I4 — After a confirmed resync completes, the restore/relink offer and `restore_reason` are never cleared
- **Where:**
  - `v2/cloud/sync/runs.py:375-381`: the company-resync completion only transitions the state.
  - The offer is set at `state.py:125-128, 247-249`.
  - `engine.py:489-494` clears only offers with `reason == "parity"`.
- **Why:** confirmed with a probe. After the confirmed whole-company resync completes, `sync-status` returns `sync_state: "ready"` with `restore_reason: "counters_backwards"` and `resync_offered: {scope: company, reason: restore}`. The web (Part 3) would keep offering a resync that has already been done. A user who confirms it again triggers a second full re-read of the company. The same happens after a relink. The restart round-trip tests assert the state *after* restore and relink, but never after the resolution.
- **Fix:** on `company_resync_completed`:
  - clear `restore_reason`;
  - drop `ladder.resync_offered` when its reason is `restore|relink`;
  - mark any other pending `confirm_resync` commands `cancelled`.

  Extend `test_company_resync_sets_counters_at_start_and_ready` to assert the whole `sync-status`.

### I5 — The relink password re-check is an unthrottled password oracle
- **Where:**
  - Device path: `v2/cloud/api/sync.py:119-126`, throttled only by the 600-per-minute per-device limiter.
  - Web path: `v2/cloud/api/web_sync.py:87-97`, not throttled at all.
- **Why:** Q25 demands the website password precisely so that a stolen device token can't re-link the company. But the device path can repeat `verify_password` at 600 attempts a minute per device. A holder of a stolen access or refresh token can also set the `relink_prompt` itself with a heartbeat `other_company_same_name`. A correct guess then yields the user's website password, which means full account takeover.

  This was raised as Task 6 M8 for the web path and deferred. It is promoted here because the device path makes it reachable from a token theft.
- **Fix:** run both paths through the login limiter's per-email `check`/`record`, with failed attempts only, keyed by the user's email or id. Add 429 tests for both paths.

### I6 — Coverage can mark months "done" that were never synced (the verified edge overclaims)
- **Where:**
  - `v2/cloud/sync/coverage.py:231-251`: `ack_month` accepts any `month` string.
  - `v2/cloud/api/sync.py:196-227`: `fy_start` is a str parsed later, so a bad value is a 500.
  - `v2/cloud/sync/runs.py:124-147`: `_recompute_window_months_total` raises `months_total` but leaves a `complete` row `complete`.
- **Why:**
  - **Month validation (confirmed with a probe):** `{"fy_start":"2025-04-01","month":"1999-01"}` → 200, and `months_done` becomes `["1999-01"]`. That bogus month counts toward `months_total`, so an FY reaches `complete`, and the verified edge that gates parity (§10.1/§10.2) includes it, with one real month never synced.
  - **F15 (Task 7 M5, promoted):** suppose a first sync is re-opened in a later month, after an F12 error or a take-over mid first sync. The current FY row, already `complete` at the old total, stays complete. The agent resumes from coverage and skips it. The new run's `counters_at_start` becomes the cursor, so incrementals won't fetch that month either. The result is a silent data gap that only parity notices later, misattributed as `ledger_gap`.
- **Fix:**
  - Type `fy_start: date`.
  - Validate `month` as `YYYY-MM` inside `[max(fy_start, books_from), min(fy_end, today IST)]`, else 422.
  - In `_recompute_window_months_total`, drop a `complete` row back to `running` when `len(months_done) < new months_total`.
  - Add tests for both.

### I7 — A ledger rename after an anchor or month-end TB is stored causes a permanent false mismatch that climbs to `hard_alert`
- **Where:**
  - `v2/cloud/parity/engine.py:452-457` and `anchors.py:40-60`: stored snapshot rows are resolved by name against the **current** live ledger names (`engine._ledgers` builds the `NameIndex` from current names).
  - `classify.py:57-62`: `masters_gap` issues only `refetch_masters`.
  - Precondition 4 (`engine.py:412-425`) re-requests a snapshot only when it is missing.
- **Why:** the anchor TB as-on E−1 (and every stored month-end TB used by bisect) is captured once and reused. Tally exports a rename retroactively (probe 8; spec §12 step 9). After a routine ledger rename:
  - the stored anchor row carries the old name and resolves to nothing, giving `missing_in_db` → `masters_gap` → `refetch_masters`, which fixes nothing;
  - the renamed ledger gets anchor 0, so it mismatches by its whole opening → `ledger_gap` → `refetch_ledger_vouchers`, which also fixes nothing.

  The agent reports the remediations done, and the ladder goes `suspect → alert → resync offered`. The resync doesn't re-capture the anchor either, so it ends in `hard_alert`. This is a false integrity alert on an everyday accounting action. No test exercises a rename together with parity. Raised as Task 10a M7 ("a 10c question") and never resolved.
- **Fix:** when a stored (non-as-on) snapshot has rows that resolve to no live ledger, issue `capture_snapshot {report_type, as_on}` for that snapshot as the remediation, instead of or in addition to `refetch_masters`. The re-capture then replaces it (newer `captured_at` wins). Alternatively, resolve and store ledger GUIDs in the snapshot rows at capture time. Add a DB test: anchor stored → ledger renamed through a master batch → parity asks to re-capture the anchor, not `ok`/`alert` on a false diff.

### I8 — §14 scenario 17 (cross-tenant) is not proven as specified
- **Where:**
  - `v2/tests/cloud/db/test_state_matrix_api.py:92-94, 245-258`: `wrong_ws` is a second, **unbound** workspace of the **same** user, and nothing is re-read afterwards.
  - `test_auth_api.py:387-395` (a probe route, status only).
- **Why:** §14 asks, for every scenario, that the test "re-read from a fresh session and assert the whole state". Scenario 17 is "device of W1 posting to W2 → 403, **W2 unchanged**". R19 is a security requirement, and the brief lists cross-tenant explicitly. Two cases are never tested:
  - W2 bound, with data, owned by **another user**;
  - W2's rows re-read after each refused call.

  The code itself is correct (`dependencies.py:67-68`). This is a gap in the test that proves it, and per CLAUDE.md "a flagged gap is a TODO".
- **Fix:** add one DB test with two users, each with a bound workspace holding data. For every `{ws}` route, D1 → W2 must return 403 `wrong_workspace`. Then compare a fresh-session snapshot of all of W2's v2 rows before and after; they must be equal.

## Minor

| ID | Where | What / why | Fix |
|---|---|---|---|
| M1 | `sync/state.py:74-78`; `api/sync.py:67` | A malformed `pc_clock` raises `ValueError` → 500 (confirmed with a probe). The agent retries it forever, and the heartbeat carries `last_seen_at` and maintenance. (Task 6 deferred M2) | Type it `datetime` → 422 |
| M2 | `parity/engine.py:231`; `contract/models.py:181` | A naive `capture_started_at` is compared with an aware `balance_captured_at` → `TypeError` → 500 | Require tz-aware input (422) or default it to UTC |
| M3 | `sync/state.py:136-137`; `api/web_sync.py:99-101` | `relink_prompt` is stored with a `None` GUID, or with the currently bound GUID. The web confirm then writes NULL into `tally_company_guid` → IntegrityError 500. The prompt is never cleared when `ours` is seen again. (Task 6 M4) | Set it only for a non-null, different GUID; clear it on `ours` |
| M4 | `sync/binding.py:146-158` | A different-GUID re-bind (no batch accepted yet) keeps the old company's snapshots, cursors, `running` runs, TB baseline, ladder, `relink_prompt` and `last_heartbeat`. A snapshot taken before any batch would later serve as the new company's anchor. (Task 5 M5) | Reset the bookkeeping columns, interrupt the open runs and delete the snapshots in `_rebind` |
| M5 | `sync/binding.py:300-305`; `coverage.py:40-55` | Same-GUID bind with no coverage rows (a `books_from` after today) → `_activate` without the D7 guard. `books_from > today` also gives 0 coverage rows. (Task 5 M1 + M4) | 422 `invalid_books_from` when `books_from > today IST`; call `_guard_takeover` in that branch too |
| M6 | `sync/coverage.py:254-281` | `add_fy` accepts any date, whether or not it is 1 April or the IST current FY. The raw window follows the coverage rows, so a bad `add_fy` nulls the current FY's `raw`. This leaves Review Focus 4 partial. | Accept only `fy_start == current_fy_start(clock)` (or the next one) |
| M7 | `api/web_sync.py:110-111`, `state.py:137`, `engine.py:103-112` | Contract drift from the spec. The command type is `confirm_resync` (spec `resync`). `requested_by="web"` (spec `user:<id>`). `relink_prompt` keys are `{guid,name}` (spec `{seen_guid, seen_name, seen_at}`). `last_parity.run_id` is not in §7.13. S2 must code against one of these. | Align the code, or record each in the Task 15 spec edit list |
| M8 | spec §11 vs `contract/models.py:100-108`; `test_reconcile_snapshots_api.py::test_snapshot_company_mismatch_409` | §11 lists `company_mismatch` for snapshots, but a snapshot body carries no company GUID, so it can't be checked. The test named `…_company_mismatch_409` asserts 403 `wrong_workspace`. | Add `company_guid` to the snapshot request (preferred: R2), or amend §11; rename the test |
| M9 | `parity/anchors.py:57-58`; `parity/classify.py:88,108,114,121` | A repeated ledger row in a ledger-level TB raises `ValueError` → 500 (retried forever). The classifier uses the hard-coded `model.TOL`, not `settings.parity_tolerance_paise`. | Return an `aborted_incomplete` + re-capture; thread `tol` into `classify` |
| M10 | `api/agent_auth.py:104-171`; `api/dependencies.py:39-51` | A deactivated user (`users.is_active=false`) keeps refreshing and syncing for up to 90 days. (Task 4 M5) | Re-check `is_active` on refresh and in the device dependencies |
| M11 | `api/agent_auth.py:121,159` | The refresh-reuse ops event is logged on the module logger with the message `"v2.ops.integrity"`, so ops filtering by logger name misses it. (Task 4 M8) | `logging.getLogger("v2.ops.integrity")` |
| M12 | `sync/maintenance.py:311`; `sync/state.py:128` vs `engine.py:377,513` | The heartbeat read-modify-writes the `ladder` JSONB (`storage_estimated_at`, restore offer) without a row lock. A concurrent parity run under `FOR UPDATE` can lose its ladder update. (Task 6 M11, Task 11 deferred) | Move `storage_estimated_at` to its own column in the next migration, or lock the row in the heartbeat |
| M13 | `tests/cloud/db/test_ingest_api.py:690-735` | §15.3 row "full_resync (single FY): last_synced_at window chunks only" is untested. Only the company scope is covered, and masters on a single-FY resync must *not* move it. | Add the single-FY cases |
| M14 | `tests/cloud/db/test_runs_api.py:733-752` | §14.13 is partly proven at DB level. The resync start is tested; completion → `complete` and the edges (available counts `resyncing`, verified doesn't) are only unit-tested (`test_coverage.py`). | Extend the DB test through the last-month ack and re-read the edges |
| M15 | `tests/test_copied_headers.py:9-19`; `tests/test_isolation.py` | `cloud/auth/web_jwt.py` and `passwords.py` are not in `COPIED`. §6.2/§16 "no write-mapped model for users/workspaces" has no static test. | Add both |
| M16 | `api/sync.py:258-288` | Non-batch bodies (reconcile `present`, snapshot `cells`, heartbeat `acked_commands`) are uncapped. Only an authenticated device can reach them. | Add a size cap per route, or `max_length` on the lists |
| M17 | `ingest/pipeline.py:83-87` | A legitimate 49 MB body peaks at about 2× plus the parse, because of the `bytes(out)` copy. Trailing gzip members are silently ignored (`unused_data`). (Task 8c deferred) | `json.loads(out)`; 422 when `inflater.unused_data` is non-empty |
| M18 | `sync/maintenance.py:190-206` | A parity-run prune cascades an unbounded number of lines per slice, breaking the "≤ 5,000 rows" rule. (Task 11 M3) | Delete old lines in bounded batches before the runs |
| M19 | `ingest/pipeline.py:42-43` (ruling) | `unexpected_parse_error` is quarantinable. It marks a *server* bug, so after a server fix the same bytes would parse, yet the object stays quarantined unless S2 re-tries quarantined objects. | Keep it retryable, or have S2 re-offer quarantined GUIDs after a server version change (see Rulings) |
| M20 | `parity/engine.py:210-237`; `forex.py:26-36` | Ruling F3's `scope_face` is not wired into the engine. It assumes a daily run's `as_on` is Tally's current period (F25) without checking. A daily run at a past `as_on` would face-check a current-FY opening against another FY's lines → false `forex_face_mismatch`. | Refuse (422) or `scope_face` when `FY(as_on)` ≠ the FY of the mirrored balances |

---

## §14 DB persistence scenarios → proving tests

| # | Scenario | Test(s) | Status |
|---|---|---|---|
| 1 | Bind | `test_binding_api::test_bind_unbound_workspace_creates_state_coverage_and_activates_device`, `test_rebind_same_guid_same_device_is_noop`, `test_restart_roundtrip::test_restart_after_bind` | ✅ |
| 2 | Take-over | `test_takeover_with_fresh_login_revokes_old`, `test_partial_unique_index_rejects_second_active_row`, `test_concurrent_takeovers_leave_exactly_one_active`, `test_restart_after_takeover` | ✅ |
| 3 | First sync | `test_runs_api::test_first_sync_scenario_24_months_to_ready` (18 months, per A17), `test_end_to_end_fakeb::test_whole_first_sync_of_fakebooks_b_through_real_endpoints` (`last_synced_at`) | ✅ |
| 4 | Batch replay | `test_replay_same_body_returns_stored_response_replayed_true`, `test_concurrent_identical_batches_store_once`, `test_restart_after_batch_and_replay_still_replays` | ✅ |
| 5 | Older alter_id | `test_older_alter_id_skipped_older` | ✅ |
| 6 | Voucher edit | `test_ingest_scenarios::test_voucher_edit_replaces_lines_and_bills_exactly` | ✅ |
| 7 | Soft-delete via reconcile | `test_reconcile_soft_deletes_absent_voucher_and_returns_touched_ledgers`, `test_resent_deleted_voucher_is_undeleted` | ✅ |
| 8 | Rename | `test_rename_keeps_guid_and_old_lines_join` (ingest only; rename × parity is I7) | ✅ |
| 9 | Mirrored balance ordering | `test_mirrored_balance_newer_wins_stale_counted` | ✅ |
| 10 | Rejected batch + quarantine + resolve | `test_unbalanced_422_deterministic_and_nothing_stored`, `test_quarantine_resend_stores_rest_and_records_row` (asserts a later good copy resolves it) | ✅ |
| 11 | Backfill vs incremental `last_synced_at` | `test_last_synced_at_moved_by_first_sync_and_incremental_not_backfill` | ✅ |
| 12 | Coverage replay | `test_coverage_patch_idempotent_and_returns_edges`, `test_restart_after_coverage_ack` | ✅ |
| 13 | Whole-company resync | `test_company_resync_start_moves_complete_to_resyncing_and_running_to_pending`, `test_company_resync_sets_counters_at_start_and_ready`; edges unit-only (`test_coverage.py`) | ⚠️ partial (M14) |
| 14 | Restore | `test_heartbeat_counters_below_cursors_restore_detected`, `test_incremental_batch_in_restore_detected_409`, `test_full_resync_without_command_in_restore_detected_is_resync_not_confirmed_not_restore_detected`, `test_full_resync_with_confirmed_command_allowed_in_restore_detected`, `test_restart_after_restore_detected` | ✅ (post-resolution state: I4) |
| 15 | Snapshots | `test_snapshot_recapture_replaces_and_round_trips`, `test_older_capture_does_not_replace`, `test_snapshot_newer_wins_under_concurrent_writes` | ✅ |
| 16 | Parity run | `test_all_match_ok_and_last_parity_persisted`, `test_parity_runs_persist_across_requests_and_ladder_survives_restart`, `test_parity_retention_7_days_matches_90_days_mismatches`, `test_restart_after_parity_ladder_state` | ✅ |
| 17 | Cross-tenant | `test_state_matrix_api::test_sync_endpoints_matrix[wrong_ws]`, `test_auth_api::test_active_device_wrong_workspace_403` — status only; W2 unbound, same user, not re-read | ❌ **I8** |
| 18 | Deleted workspace + purge | `test_deleted_workspace_next_call_410_and_device_revoked`, `test_purge_now_deletes_only_that_workspaces_rows`, `test_purge_respects_30_day_grace` | ✅ |
| 19 | `Numeric` round-trip | `test_numeric_round_trip_exact` | ✅ |
| 20 | Raw retention | `test_raw_kept_only_for_newest_two_fys_at_insert`, `test_raw_window_follows_ist_fy`, `test_raw_purge_is_bounded_per_slice`, `test_out_of_window_voucher_raw_is_sql_null_not_json_null` | ✅ |

## §15 matrices → coverage

| Matrix | Coverage | Gaps |
|---|---|---|
| §15.1 endpoint × auth state | `test_state_matrix_api.py`: 5 rows × 8 columns; `{ws}` row × 9 routes. Every non-`—` cell is asserted with status + error code. | logout × expired and devices × deleted ws are omitted by choice (Task 12 M2, queued for the Task 15 spec edit) |
| §15.2 batch × content | masters/vouchers/mixed (`test_masters_only…`, `test_vouchers_only…`, `test_mixed_batch_masters_applied_first`); unknown ledger (`test_unknown_ledger_422_missing_master_retryable`); ambiguous (`test_two_live_ledgers_same_name_422_ambiguous_master`); deterministic (DB: unbalanced; bad amount / forex without base / bad date in `unit/test_validate.py`, plus DB quarantine acceptance of every code); forex (`test_forex_voucher_stored_with_fx_columns`); cancelled / optional / post-dated; older alter_id; wrong company; replay / reused; oversize bytes / objects / decompressed (`test_zip_bomb…`); restore 409; run closed / other device 403 | Only unbalanced has a 422 DB test (the others are unit + quarantine) — acceptable |
| §15.3 run kind × `last_synced_at` × cursor | cursor: `test_incremental_completion_sets_cursor_after`, `test_backfill_completion_leaves_cursor`, `test_single_fy_resync_leaves_cursor`, `test_company_resync_sets_counters_at_start_and_ready`, first sync; `last_synced_at`: first_sync / incremental / backfill / company full_resync | single-FY `full_resync` `last_synced_at` (M13) |
| §15.4 coverage FY × event (24 cells) | `unit/test_coverage.py` pins `COVERAGE_MATRIX == EXPECTED` (all 24) plus `apply` behaviour | none |
| §15.5 parity × situation | all match A (`test_real_a_every_bs_ledger_matches`, `test_realdata_parity_a`, DB `test_all_match_ok…`); forex B 183.87 (`test_forex_revaluation_ok_with_match_revalued_18387`, `test_realdata_parity_b`); forex sale missing (`test_forex_sale_missing_current_fy_suspect_face_mismatch` / `…_past_fy_is_revaluation_unexplained`); one voucher missing (`test_one_voucher_missing_suspect_voucher_missed_or_duplicated`); alert ×2 + offer (`test_ladder_alert_then_resync_offered_after_two_heals`); hard_alert (`test_hard_alert_after_confirmed_fy_resync_still_wrong`); moved / behind / anchor missing / stale (the four abort tests); mid-backfill R30 (`test_mid_backfill_anchor_correct_all_time_far_off_is_match`, `test_realdata_midbackfill`); no ledger anchor (`test_no_ledger_anchor_group_anchor_route`); resyncing FY (`test_resyncing_fy_excluded_from_verified_span`); unclassifiable (`unit/test_rung1::test_unclassified_group_not_applicable`); tolerance 0.99 / 1.00 / 1.01 (`test_tolerance_boundaries`) | "nature unclassifiable" is unit-only (acceptable); no rename × parity row (I7, not in the matrix) |

## Deferred-minor triage

**Promote** = must fix before merge. **Defer** = acceptable after merge, with the reason given.

| Source | Deferred minor | Triage |
|---|---|---|
| Task 1 | p10 envelope docstring overclaims no-company voucher detection | Defer. Docs only; S2's company gate (LESSONS rule 26) is the real guard. |
| Task 1 | Smoke-test branches weakly asserted | Defer. Test strength only; the real-data parity tests would fail on empty output. |
| Task 1 | `rate('NaN/Nos')`→NaN, `counter('1_000')`→1000 | Defer. Tally never emits these. Cheap regex guard recommended at S2 hardening. |
| Task 1 | `test_copied_headers` missing the contract files | **Resolved**: now listed. Two cloud files are still missing (M15). |
| Task 2 | Secret-length check uses a hard-coded tuple | Defer. There are two secrets today. |
| Task 2 | `main.app=create_app()` reads `.env` at import | Defer. Inert at import (lazy engine). |
| Task 2 | `asyncio_mode=auto` reliance | Defer. Configured in `pyproject`. |
| Ruling | passlib `crypt` DeprecationWarning | Defer to Task 15 (filter) as ruled. |
| Task 3 | Stand-ins leak if migrate fails; nullability / indexes unasserted; nullable timestamps; no RED evidence | Defer. Test harness and process only; `test_models_match_migration_v2_001` compares the metadata. |
| Task 4 | Mixed-case emails can't log in (parked) | Defer. Current-app behaviour and out of v2 scope; note it for the Part 3 merge. |
| Task 4 | Retry-After rounds down; reuse reason when already revoked; DELETE overwrites the revoke reason; login timing leak; malformed web `sub` → 500; N+1 in `/api/devices`; test style / gaps | Defer. Low impact. The revoke-reason overwrite loses forensic evidence, so a cheap idempotent DELETE is recommended. |
| Task 4 | Deactivated users keep refreshing | Defer as M10. The spec is silent; one-line check recommended. |
| Task 4 | Ops event on the wrong logger | Defer as M11. Cheap fix; do it in the fix round if touching `agent_auth`. |
| Task 4 | `_TwoPartyBarrier.wait()` has no timeout | Defer. A hang only on a future regression; add `asyncio.wait_for`. |
| Task 4 | 4 re-review minors | Defer. |
| Task 5 | 8 minors incl. `invalid_books_from` untested; `last_login_at None` fails open; `company_bound_elsewhere` race; `_rebind` leaves runs / prompt; test depth; coverage JSON after commit | Defer, except the `_rebind` residue, which is tracked as M4, and `books_from > today`, tracked as M5. |
| Task 5 | M1 same-GUID bind with no coverage bypasses D7 | Defer as M5. Needs a future `books_from`; fix it together with the 422. |
| Task 5 | M2 concurrent workspace swap can deadlock → 500 | Defer. Postgres resolves the deadlock, and the agent retries. |
| Task 5 | M3 two definitions of "live active device"; M4 dead-device + different-GUID test | Defer. |
| Task 6 | First-sync percent path untested | **Resolved**: `test_sync_status_first_sync_progress_with_real_run_and_batches`. |
| Task 6 | Malformed `pc_clock` → 500 | Defer as M1 (confirmed). Cheap; recommended in the fix round. |
| Task 6 | `tally_status` free string; command delivery order; unused command fields; private `_bound_elsewhere` import | Defer. |
| Task 6 | Relink prompt with a null / equal GUID → 500 | Defer as M3. |
| Task 6 | Web `confirm_relink` password not rate-limited | **Promote to I5.** With the device path it is a password oracle reachable from a stolen device token. |
| Task 6 | `fy_start` (web command) not format-validated | Defer. A bad value gives a 500 at run open; the web UI sends ISO. Fold it into the I6 typing work if convenient. |
| Task 6 | Read-modify-write races on the ladder / relink | Defer as M12. |
| Task 7 | `kind` / `status` not `Literal` | **Promote as part of I2**: untyped run bodies wedge the workspace. |
| Task 7 | M5 F15 recompute doesn't re-open a `complete` FY | **Promote to I6.** Silent data gap in the verified edge. |
| Task 7 | M6 `ack_month` doesn't validate `month` / `fy_start` | **Promote to I6** (confirmed with a probe). |
| Task 7 | M7 `_apply_resync_start` silent no-ops; M8 progress overwritten with None; M9 false comment; M10 wrapper | Defer. |
| Task 7 | Non-numeric `iat`/`exp` → 500; no PATCH-on-interrupted test; take-over vs in-flight PATCH race; concurrent same `command_id` open race | Defer. Needs the secret, or it's a narrow race; the command race could open two resync runs, so add a `FOR UPDATE` on the command at Task 14. |
| Task 8a | 7 minors (codes not in §11; naive `captured_at`; master `captured_at` parse; `masterid`; warning detail; test gaps; readability) | Defer. The naive `captured_at` is handled in `store._captured_at`; `masterid` is validated in `_extra_checks`; the codes go to the Task 15 spec. |
| Task 8b | 7 minors (`is_forex` with an unknown base, test naming, …) | Defer. The base falls back to the bind's `base_currency_name`. |
| Task 8c | D30 sticky flag partial | Defer. Rare; a proper fix needs a column. |
| Task 8c | Balance without `captured_at` dropped silently | Defer. S2 contract: make `captured_at` mandatory in S2's builder, and add a warning when convenient. |
| Task 8c | Quarantine vs object error index collision; stale in-batch name; `apply_balance` ignores `is_deleted` | Defer. Retryable or harmless today. |
| Task 8c | Concurrent same new GUID → 500 (retried) | Defer the 500 itself (self-heals). Its log leak is **I1**. |
| Task 8c | `read_body` copy peak + multi-member gzip | Defer as M17. |
| Task 8c | Rejected row unasserted in the quarantine test; new codes → §7.9/§11 | Defer to the Task 15 spec. |
| Task 8c | No direct `_all_deterministic` tests; three copies of "window FYs"; no RETRYABLE ∩ REPLAYABLE invariant | Defer. Now two copies (`clock.window_fys` is shared by store / maintenance / sync_status / backfill; `runs._window_complete` still has its own); add the invariant test at Task 14. |
| Task 9 | 8 minors (docstring numbers, test naming `…company_mismatch_409` asserting 403, round-trip depth, reconcile `from<=to`, naive `captured_at`, new codes, masters in-use for groups / units) | Defer, except the test naming, tracked as M8. |
| Task 9 | I6 union hardening; no-op response echoes stored row | Defer. |
| Task 10a | Forex TB fallback; misleading cause; `rung2` KeyError; anchors ledgerwise guard; test naming; vacuous `group_walk_wrong` | Defer. The forex fallback and anchor guard are **resolved** in 10c. |
| Task 10a | M7 renamed / deleted ledgers in the anchor TB | **Promote to I7.** A routine rename gives a false `hard_alert`. |
| Task 10b | `anchor_wrong` one sign; no net+anchor overlap test; no paisa netting test; bisect edge tests; step drops `last_run_id`; unused `forex_guids`; duplicate `refetch_masters` ids; opsignal key validation; caplog assertions | Defer. `anchor_wrong` magnitudes are **resolved** in the Task 12 fix; `last_run_id` is **resolved** by the engine's `{**prev, **new}`. |
| Task 10b | TypedDict for the bucket; integrity / quarantine events unwired | Defer. The events are **resolved** (wired in 10c / 8c). |
| Task 10c | M2b bisect skips D10; M3 engineering flags only in `cause_counts`; M4 private cross-module calls; M5 `verified_from` on early aborts; M7 invoice-mode ingest not exercised with parity; M8 extra `run_id` key; M9 docstring | Defer. M8 is tracked as M7 (spec edit). |
| Task 10c | First-run suspect `last_parity` shape untested; suspect mixes two runs; bisect events indistinguishable | Defer. Add the `scope` key to the event at Task 14. |
| Task 11 | Window from coverage (prev-FY resync chunk) | Defer. Covered by `last_synced_at` rules; revisit with M6. |
| Task 11 | Heartbeat-isolation test doesn't assert the ops log | Defer. |
| Task 11 | Storage estimate O(rows) hourly; `pg_column_size` understates | Defer. Documented; revisit at scale. |
| Task 11 | `storage_estimated_at` in the ladder JSONB | Defer as M12 (lost-update surface). |
| Task 11 | Cascaded line deletes unbounded; purge live-workspace untouched only indirect; `purge_cli` test swapped | Defer as M18; the other two are test-depth only. |
| Task 0 | `s1_capture` USD row lookup by suffixed name | Defer. Probe harness, not S1 code. |
| Task 12 | M2 two §15.1 cells omitted; M3 G6 sidecar `company_guid` null; M4 `pure_parity` re-expresses engine orchestration; M5 F13 provenance; M6 `fakeb` arg; M7 passlib | Defer to Task 14 / 15 as ruled. M4's drift risk is partly pinned by `test_day_one_sums`. |
| Task 12 | Row-5 shifted cluster labels as `anchor_wrong` (not `ledger_gap`); dropped-anchor half unit-only; `test_day_one_sums` rebuilds the expression | Defer. The remediation is harmless per the ruling. |

### Rulings flagged against the spec

- **`unexpected_parse_error` quarantinable (Task 8a round-2 ruling):** questionable, not blocking; see M19. D12 defines quarantinable as "the same bytes never parse on retry". That holds for the agent's bytes, but not across a server fix. Either make it retryable-not-quarantinable, or require S2 to re-offer quarantined GUIDs after a server version bump.
- **Ack semantics:** there was no ruling, and I3 shows one is needed. D16's "pending, user-confirmed" plus §7.6's "ack delivered commands" conflict, and the implementation picked the reading that dead-ends.
- All other rulings reviewed are consistent with the spec's intent:
  - D7 on any displacement (Task 5 I3)
  - interrupt other devices' runs (Task 7 I1)
  - incremental needs cursors (Task 7 I2)
  - scope = command scope (Task 7 I4)
  - F2 / I5 / I6 imbalance
  - group-anchor route
  - ladder I1–I3
  - suspect `last_parity`
  - §10.5(b) Δ rule
  - row 5 `anchor_wrong`
  - D30 own balances
  - `present_count`
  - failed-only login counting

## Review Focus results

| # | Focus | Result |
|---|---|---|
| 1 | Capture set split across files: join, never invent | ✅ `unit/test_assembler.py::test_assembler_never_fills_a_value_no_capture_holds`, `test_assembler_marks_synthetic_identities_by_gap_id`. The Task 12 fix records the probe-16 inferred keys as `inferred:<capture>` and marks them synthetic. |
| 2 | Shared test DB left as found | ✅ `test_migration.py::test_session_teardown_leaves_no_v2_objects`, `test_down_then_up_round_trip_keeps_users`. Task 15 still has to run the current DB suite after the v2 one; not run here. |
| 3 | Zip bomb 413 without inflating past the cap; plain JSON accepted | ✅ `test_zip_bomb_is_413_before_full_inflate` (peak < 70 MB asserted; allocation is bounded by the 50 MB cap by design), `test_uncompressed_json_body_is_accepted`. A legitimate near-cap body peaks at about 2× (M17). |
| 4 | FY boundary by IST | ⚠️ Partial. `ist_date` / `current_fy_start` / bind coverage / edges are IST (`test_current_fy_uses_ist_date_at_utc_evening_of_31_march`, `test_ist_date_crosses_midnight_before_utc`). But the raw window follows the coverage rows, i.e. whatever `add_fy` the agent posts, and `add_fy` isn't checked against the IST FY (M6). `test_raw_window_follows_ist_fy` passes whatever `clock2` is. |
| 5 | Same batch twice at once | ✅ `test_concurrent_identical_batches_store_once`, `test_claim_row_blocks_a_concurrent_identical_batch_until_the_winner_commits`, `test_claim_taken_over_when_the_concurrent_winner_rolls_back` |

## Suites NOT run

- **The full v2 suite was not re-run for this review.** The last green run is 1883 passed at 41ab9a4, in `logs/v2-s1-task12-fix1.log`. Only the temporary 3-test probe ran, and it was deleted afterwards.
- **`tests/e2e_live/`**: needs live Tally + the Claude API.
- **Tier B (live Tally) and tier C (licensed Windows Tally)**: not run. S1 is tier A only; G6 was captured read-only in Task 12.
- **The current app's suites** (`tests/unit`, `tests/integration`, `tests/e2e`, the DB suite, frontend Vitest / Playwright, eval): not run. Nothing outside `v2/` and `docs/` changed. Task 15 runs the current DB suite after the v2 one (Review Focus 2).

## Declined to judge

- **Transcoder XML dialect coverage beyond the S0 captures:** it belongs to S2 extraction; S1 tests every §13.1 capture.
- **Performance at 30M-row scale** (storage estimate, sums without a monthly-totals table, D18): the spec defers it to Part 2 measurement.
- **Multi-worker deployment** (in-process rate limits, D24): the spec states one worker in v1.
- **The web-side pick (Q11), workspace creation from the agent (D5) and `caught_up_at` labelling (D21):** each is marked "needs the user" in spec §17.2.
- **Forex revaluation rate semantics (C47 M1):** the spec explicitly doesn't depend on them.

---

## Fix round (2026-09-29)

One commit per finding, each with a failing test first. All DB commands use
`TEST_DATABASE_URL=postgresql+asyncpg://nuvanta-mac-3@localhost/tallyagent_test ANTHROPIC_API_KEY=test-key PYTHONPATH=. uv run --project v2 pytest …`.
After each fix, the affected suites were re-run green (listed per finding).

| ID | Commit | What changed | Covering test(s) | RED → GREEN |
|---|---|---|---|---|
| I1 | `0327c88` | `v2/cloud/db.py:11-14` `make_engine(..., hide_parameters=True)`; `alembic/env.py:34` same. The driver's own message can still quote one value (asyncpg: `invalid input for query argument $1: '<value>'`), which `hide_parameters` doesn't cover. So `v2/cloud/errors.py:33-41` adds a `DBAPIError` handler: it answers 500 `{"error": "internal_error"}` and logs the exception **classes** only, so the traceback never reaches uvicorn's log. | `db/test_db_engine.py::test_db_error_does_not_leak_bound_parameters`, `::test_db_error_in_a_route_logs_no_business_value` | `pytest v2/tests/cloud/db/test_db_engine.py` → `assert 'NARRATION-B...rs-123456.78' not in '(sqlalchemy.../21/dbapi)'` FAILED → 2 passed; cloud suite 693 passed |
| I2 | `0f77049` | `v2/cloud/sync/runs.py:58-76`: `RunCreate.kind: Literal[…]`, `counters_at_start: Counters` (the contract's typed model, **required for every kind**), `RunPatch.status: Literal["completed","failed"]`, `cursor_after: Counters \| None`. `runs.py:445-448`: completing an `incremental` without `cursor_after` → 422 `cursor_after_required`, checked before any mutation. Six existing tests that opened runs without counters (or completed an incremental without `cursor_after`) were updated to send them. | `db/test_runs_api.py::test_open_run_without_counters_at_start_is_422_and_nothing_stored[×4 kinds]`, `::test_open_run_malformed_body_is_422_not_500[×3]`, `::test_incremental_completed_without_cursor_after_is_422_and_run_stays_open`, `::test_patch_run_malformed_body_is_422[×2]` | `pytest … -k "without_counters or malformed or cursor_after_is_422"` → 8 failed, 2 passed → 49 passed (file); cloud suite 703 passed |
| I3 | `86ba0c8` | Ruling applied: **an ack means "received"**. `v2/cloud/sync/commands.py:53-83`: `ack` skips `confirm_resync`, so it stays `delivered` (still usable by `_require_confirmed_resync_command`). Only the completed `full_resync` (`runs.py` → `done`) or a superseding confirm closes it. `commands.py:86-100` adds `cancel_open_resyncs`. `api/web_sync.py:107-109`: a new `confirm_resync` cancels older open ones that no running run uses (status `cancelled`). Other command kinds keep ack → `done`. | `db/test_restart_roundtrip.py::test_acked_confirm_resync_still_opens_the_resync_and_completion_marks_it_done` (restore → confirm → deliver → ack → `POST /runs full_resync` 200 → complete → `done`), `::test_acked_recheck_now_still_goes_done`, `::test_superseding_confirm_resync_cancels_the_open_one` | code stashed → `assert 'done' == 'delivered'`, `assert 'done' == 'cancelled'` (2 failed) → restart + heartbeat + runs + state-matrix: 202 passed |
| I4 | `bf40ec2` | `v2/cloud/sync/runs.py:396-404`: on the company-resync completion, clear `restore_reason`, drop `ladder.resync_offered` when its reason is `restore`/`relink`, and cancel any other open `confirm_resync` (`keep` = the run's own command). A parity offer is left to the engine. | `db/test_restart_roundtrip.py::test_restart_after_confirmed_resync_resolves_restore_or_relink[restore\|relink]`: act → restart → whole-state re-assert (`sync-status` `ready`/None/None/None, commands `[done]`); `::test_confirm_issued_during_the_running_resync_is_cancelled_on_completion`; `db/test_runs_api.py::test_company_resync_sets_counters_at_start_and_ready` extended to assert the whole `sync-status` | `pytest … -k "resolves or during_the_running"` → `'counters_backwards' != None`, `'relink' != None`, `('done','pending') != ('done','cancelled')` → 3 passed; restart + heartbeat + runs + parity: 130 passed |
| I5 | `ba24fe2` | `v2/cloud/api/dependencies.py:33-49` `check_user_password`: the same per-email login limiter (`app.state.login_rate_limiter`, keyed by the user's lower-cased email), checked first, with a hit recorded on a **failed** attempt only. Used by the device `POST /relink` (`api/sync.py:120`) and the web `confirm_relink` (`api/web_sync.py:92`). The two paths and `/login` share one budget. | `db/test_heartbeat_api.py::test_device_relink_wrong_passwords_are_throttled_429`, `::test_web_confirm_relink_wrong_passwords_are_throttled_429`, `::test_relink_failures_share_the_login_budget`, `::test_successful_relink_does_not_consume_the_budget` | `-k "throttled or share_the_login or consume_the_budget"` → 3 failed (`KeyError: 'error'`: the 6th attempt was applied) → heartbeat + auth + state-matrix: 179 passed |
| I6 | `ba67237` | `v2/cloud/api/sync.py:190-193`: `fy_start: date`, and `month` must match `^\d{4}-(0[1-9]\|1[0-2])$` (422 otherwise; a bad `fy_start` was a 500). `v2/cloud/sync/coverage.py:231-241,254`: the month must lie in `[max(fy_start, books_from), min(fy_end, today IST)]`, else 422 `month_out_of_range` before any write. F15: `v2/cloud/sync/runs.py:150-163` drops a `complete` window row back to `running` (and clears `completed_at`) when `len(months_done) < new months_total`, then recomputes both edges and the backfill copy. | `db/test_runs_api.py::test_coverage_ack_bad_month_or_fy_start_is_422_and_nothing_changes[×6]`, `::test_coverage_ack_month_before_books_from_is_422`, `::test_bogus_month_can_no_longer_complete_an_fy`, `::test_reopened_first_sync_in_a_later_month_reopens_a_complete_window_fy` | 9 failed → 9 passed; cloud suite 722 passed |
| I7 | `415d03f` | `v2/cloud/parity/engine.py:188-205` `_stale_after_master_change`: a stored ledger-level TB (the D9 anchor, or a bisect month-end) is stale when both hold: (1) it has rows that resolve to no live ledger; (2) a ledger `alter_id` is above that snapshot's own `counters.alt_mst_id`, meaning a master changed after capture. Both are in Tally's counter space, so no clock is involved. Daily (`engine.py:493-495`) and bisect (`engine.py:608-615`) then abort `aborted_incomplete`/`anchor_stale` with `capture_snapshot` for each stale TB, instead of computing a false mismatch. The ladder, `last_parity` and lines are untouched. The re-capture carries the current counters, so a genuinely missing master triggers one re-capture, never a loop. `_Snap` now carries `counters`. | `db/test_parity_api.py::test_rename_after_anchor_asks_to_recapture_the_anchor_not_a_false_mismatch`: `Cash` renamed through a master batch, then `anchor_stale` + re-capture request, then re-capture, then `ok`, with the renamed line `match`. `::test_rename_after_bisect_month_ends_asks_to_recapture_them` | engine stashed → `assert ('suspect', None) == ('aborted_inc...anchor_stale')` (3 false mismatches) → parity + unit: 344 passed; e2e + day-one + restart: 19 passed |
| I8 | `7d1b90d` | Test only (the code was already correct). `db/test_state_matrix_api.py:303-357`: two users, each with a real bound workspace. W2 holds data: every B master via `/batches`, an open run, a relink prompt. D1 (user 1) calls every `{ws}` route on W2, with W2's own run id in the bodies, and gets 403 `wrong_workspace`. D1's `PATCH /runs/{W2 run}` also gets 403. User 1's web JWT on W2's `sync-status` and all three commands gets 404 `workspace_not_found`, and on W2's device gets 404. A fresh-session snapshot of **every row** of every v2 table for W2 (whole rows), plus user 2's devices and the workspace row, is equal before and after. | `::test_cross_tenant_device_refused_on_every_route_and_other_tenant_unchanged` | Mutation RED: with check 3 (`dependencies.py` `device.workspace_id != ws`) disabled → `assert (409, 'not_active_device') == (403, 'wrong_workspace')` FAILED. Restored → 1 passed |
| M20 | `53b246e` | `v2/cloud/parity/engine.py:278-290,413-414` `_require_as_on_not_past_mirrored_fy`: outside bisect, a request whose `FY(as_on)` is older than the FY of the latest non-deleted, non-post-dated voucher held is refused 422 `as_on_not_current_period` (same style as `bad_as_on_date`/`fy_start_required`), before the row lock, so nothing is stored. | `db/test_parity_api.py::test_daily_as_on_outside_the_mirrored_period_is_422_and_nothing_stored` (edge FY 2024-25, daily at 31-03-2025 → 422, whole parity state unchanged; 31-03-2026 still `ok`) | before: `assert 422 == 200` → parity + e2e + restart + state-matrix: 167 passed |

**Decisions the spec doesn't settle (for the re-review):**
- **I1:** the new 500 body code `internal_error` is not in §11. A 500 is always retried (§11), so the code is informational. It goes on the Task 15 spec-edit list.
- **I3:** there are two new ack semantics. (1) `confirm_resync` stays `delivered` after an ack. (2) A newer confirm supersedes an open one (`cancelled`, a new `sync_commands.status` value). Record both in §7.6/§7.15 at Task 15. `GET /state` still lists `pending` commands only. An agent that restarts after acking a resync learns its id from the next heartbeat only if it's still `pending`. S2 should keep the delivered id in its SQLite until the run opens. Alternatively, `/state` could also list `delivered` resyncs; that is a Task 15 choice.
- **I7:** the staleness test is a ledger `alter_id` above the snapshot's `counters.alt_mst_id`. The alternative, storing ledger GUIDs in snapshot rows, is impossible because a TB export carries no GUID. A group-anchor route (`trial_balance` fallback) is not checked, because its rows are groups.
- **M20:** the server stores no "current period". The first attempt compared each ledger's mirrored ClosingBalance with the as-on ledger-level TB, but that is unusable: FakeBooks' P&L masters carry cumulative closings. The shipped rule is therefore a **lower bound**. A new FY with no voucher yet is never refused. A daily `as_on` inside the right FY but before the period end is not caught. Name `as_on_not_current_period` in §11 at Task 15.

**Minors covered incidentally:** none of M1–M19 beyond the finding scope. The fixes also close these deferred items that were promoted into findings:
- Task 7 `kind`/`status` not `Literal` (into I2).
- Task 7 M5 (F15) and M6 (month/`fy_start` validation) (into I6).
- The log half of Task 8c M6 (the concurrent same-GUID 500) (into I1).

M19 stays as ruled, with no code change.

**Full v2 suite (once, after all fixes, HEAD `53b246e`):** `uv run --project v2 pytest v2/tests -q` → **1918 passed**, 0 failed, 1 warning (passlib `crypt`) in 544.68s. That is 1883 at 41ab9a4 plus 35 new tests. Log: `logs/v2-s1-task13-fixwave.log`. §0 criterion 4 re-checked: `git diff --name-only dd81030..HEAD | grep -v '^v2/\|^docs/'` prints nothing.

**Not run:** `tests/e2e_live/`, tier B/C (live Tally), the current app's suites (nothing outside `v2/` and `docs/` changed).
