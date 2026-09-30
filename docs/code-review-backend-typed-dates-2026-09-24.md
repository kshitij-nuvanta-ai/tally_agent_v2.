# Code review — `fix/backend-typed-date-vars` (C33 typed date vars, party-voucher window, company escaping)

- **Date:** 2026-09-24
- **Range:** `dev..HEAD` = `91376ab` (fix) + `824dc23` (LESSONS.md)
- **Reviewer approach:** `superpowers:requesting-code-review` code-reviewer template. Read-only review, done in one pass with no subagents. No Tally (`localhost:9000`) traffic.
- **Context:** audit `docs/backend-date-vars-audit-2026-09-24.md`; live check `logs/backend-date-fix-live-check-2026-09-24.log`; raw audit log `logs/backend-date-audit-2026-09-24.log` (main checkout).

## Verdict

**Ready to merge with fixes.** Neither fix below needs a code change. The core fix is correct, small, and well-targeted:
- every backend read builder now sends `TYPE="Date"` through one helper;
- every read envelope escapes the company name;
- the hard-coded FY 2025-26 party-voucher default is gone.

The tests use the real builder → mock Tally → real parser path and fail if the type is dropped. Two Important items remain, both about verification rather than code: (1) confirm the DB-gated suite actually finished green; (2) run one read-only live check of the new *default* party-voucher window shape before relying on it for the dedup hard block.

## What was checked

| Focus | Result |
|---|---|
| (1) `party_voucher_window` semantics + call sites | Correct for the stated cases (see matrix below). All 4 call sites pass the document's own date: `orchestrator.py:349` (DN), `:376` (CN), `:459` (upload dedup), and `chat.py:636` (write-time re-check, entry `date` = YYYYMMDD, parsed). The DN/CN lookups still omit `company` (pre-existing gap, see M1). |
| (2) Mock C33 fidelity | Untyped or absent collection vars return `MOCK_CURRENT_PERIOD`, and the response is the **byte-identical** current-FY fixture. Typed current-FY also returns the byte-identical fixture. Typed sub-windows return a rebuilt envelope that parses to the same rows after the Python filter. Reports read typed and untyped `SVTODATE` identically. Demo and eval current-period answers are unchanged. Eval DN/CN mock parties (`Bharat Paper Supplies`, `Sunrise Construction Ltd`) are not in `_MOCK_PARTY_VOUCHERS`, so those scenarios are unaffected. |
| (3) Prior-FY fixture realism | Good. It uses the mock company's real parties and ledgers: `Samsung India Electronics` sits under the custom sub-group `National Creditors`, and `Sharma &amp; Sons Traders` exercises escaping. It covers Sales, Purchase, Payment and Receipt, plus one voucher 2 FYs back (`S2324-011`). Gaps are listed in M4. |
| (4) Remaining untyped vars / company escaping | `backend/`: none. All dated reads go through `_date_vars`, and all `SVCurrentCompany` go through `_company_var` (escaped). `import_builder` escapes with `_esc`, and `SVSTOCKGROUP` is escaped. `scripts/`: untyped dates remain only in probe, manual-test and one-off maintenance scripts, none of which the app uses (M6). |
| (5) Tests passing for the wrong reason | None found. `test_dedup_window::test_prior_fy_invoice_reuploaded_in_new_fy_is_blocked` fails without `doc_date` (the default window starts 01-04-2025 and excludes 20240515). `test_mock_c33_date_vars` fails if the type is dropped. The orchestrator routing tests patch function-local imports correctly. No date-dependent time bombs were found. |
| (6) Current-period answer changes | None. Live audit log (`backend-date-audit…log` L512ff): on Educational, a typed mid-month *from*-date falls back to the current period **start**, not the end, so a typed current-period window is always a superset of the answer, and the Python filter gives the same answer as before. Reports are SHA-identical typed vs untyped (audit §2b). |

### `party_voucher_window` edge-case matrix (today = 2026-09-24 unless stated)

| Case | Window | OK? |
|---|---|---|
| No or unparseable anchor | 01-04-2025..31-03-2027 | yes: prior FY + current FY |
| Doc in current FY | 01-04-2025..31-03-2027 | yes |
| Doc Mar-2026, today 10-Apr-2026 (1 April boundary) | 01-04-2024..31-03-2027 | yes: covers the original across 31-Mar |
| Doc 01-04-2026 exactly | FY start 01-04-2026 → from 01-04-2025 | yes: `get_fy_start` uses `month >= 4` |
| Doc dated in a future FY (2027-05) | 01-04-2025..31-03-2028 | yes |
| Very old back-dated invoice (2023-06) | 01-04-2022..31-03-2027 | yes: dedup anchors on the doc date, so its own FY is covered |
| DN today whose original invoice is 2+ FYs back | from = start of the FY before the DN date | **misses it** (M2). This is still strictly better than the old fixed window. |
| Vision misread year (e.g. 1925 / 2205) | 100+-year window | works but is unbounded (M3) |
| Year `0001` | `ValueError` from `date(0,4,1)` | dedup catches it; the DN/CN path does not (M3) |

## Critical

None.

## Important

**I1. DB-gated coverage for this change is unconfirmed.**
- **Where:** `tests/integration/test_upload_voucher_persistence.py::test_write_time_dedup_recheck_anchors_on_entry_date` (new), plus `tests/unit/test_dedup.py` and the DB e2e tests that exercise the dedup path.
- **Failure scenario:** these skip without `TEST_DATABASE_URL`. In my run they were among the 117 skipped. The only DB-suite evidence is `logs/db-suite-backend-date-fix-2026-09-24.log`, which contains progress dots but **no pytest summary line** (it was still running or was cut off). So "DB suite green" is not proven for the `chat.py` / `dedup.py` changes.
- **Fix:** let the full DB suite (CLAUDE.md command) finish and record the `N passed` line in the log/tracker before merging.

**I2. The new default party-voucher window shape was never exercised against live Tally, yet it feeds the dedup hard block, which fails open.**
- **Where:** `backend/tally_bridge/queries/vouchers.py:36-61` (`party_voucher_window`), consumed by `services/dedup.py:100` (wrapped in `except Exception → warning`, so any Tally error means **no duplicate block**).
- **Failure scenario:** the live check ran `build_party_vouchers` only with an explicit in-books window (FY 2022-23). The production default is different: typed, it starts **before the seed company's books** (01-04-2024; books begin 01-04-2025) and ends in a **future FY with no data** (31-03-2027). If Tally rejects or mis-handles a typed from-date before books-beginning on this collection, the result is an error or empty. Dedup then silently passes, and a duplicate purchase invoice gets written to Tally (LESSONS §15 write safety).
- **Fix:** one read-only live call, `get_party_vouchers(client, <bill-wise seed party>, ["Purchase"], company=<seed>)` with no explicit dates, against `Bharat Traders Private Limited`. Assert it returns the same rows as the explicit 01-04-2025..31-03-2026 window, and append the result to the live-check log. If it misbehaves, clamp `from` to the company's books-beginning.

## Minor

**M1. DN/CN party lookups don't pass `company` (pre-existing).**
- **Where:** `backend/agents/orchestrator.py:347-350` and `:374-377`.
- **Failure scenario:** with more than one company loaded, the "against which invoice" list comes from Tally's active company. The windows are now binding, but the company scope is still not set. Separately, the lookup uses the raw `party_name` rather than the resolved party ledger (`voucher.party_ledger`), so a custom-sub-group or fuzzy-matched party returns nothing.
- **Fix (follow-up):** pass `company=getattr(session, "company", None)` and use the resolved ledger name.

**M2. A DN/CN whose original invoice is 2+ FYs older than the note is not surfaced.**
- **Where:** `vouchers.py:55-61`.
- **Fix:** document this as a known limit in the docstring, or anchor DN/CN on `original_invoice_date` when Vision extracts one. Low priority; the old behaviour was worse.

**M3. The window anchor is unbounded, and odd types can raise.**
- **Where:** `vouchers.py:24-33` and `:55`.
- **Failure scenarios:**
  - A misread year (1925 / 2205) produces a window of a century or more. Harmless for correctness, but it reads the party's whole history.
  - Year `0001` raises `ValueError` in `get_fy_start`. The DN/CN call path does not catch it, so the upload fails.
  - A `datetime` anchor passes the `isinstance(date)` check and then `min(datetime, date)` raises `TypeError`.
- **Fix:** ignore anchors outside, say, today ±10 years (treat them as None), and normalise `datetime` to `.date()`.

**M4. Prior-FY fixture and party-voucher mock gaps.**
- **Where:** `tests/fixtures/vouchers_prior_fy.xml`, `mock_handler._MOCK_PARTY_VOUCHERS`.
- **Gaps:**
  - No voucher exactly on 01-04 or 31-03, so the inclusive window bounds in `_voucher_collection_response` and the Python filter are never exercised at the edge.
  - No Debit Note / Credit Note / Journal vouchers.
  - No `REFERENCE` elements in the prior-FY day-book vouchers.
  - Only Apex has a prior-FY party-voucher row. There is no prior-FY purchase row for a party under a custom sub-group (e.g. `Samsung India Electronics` / `National Creditors`), which is the dedup case that matters most for purchases.
- **Fix:** add a 01-04-2024 and a 31-03-2025 voucher, plus a prior-FY `Samsung India Electronics` Purchase row, with a dedup test for it.

**M5. The mock prior-FY picture is internally inconsistent.**
- **Where:** `mock_handler.STATIC_FIXTURES` and `_generate_cumulative_pnl`.
- **Failure scenario:** in demo mode, "sales FY 2024-25" now returns 3 invoices, but "P&L FY 2024-25" (computed from the current-FY fixture only), TB and BS show zero or current-FY figures. An eval judge could mark a cross-report prior-year comparison as incoherent. Separately, `trends_and_breakdowns_mock`'s "no prior month for comparison" criterion may now see prior-FY months if the agent widens the window.
- **Fix:** note this in the mock docstring, or feed `vouchers_prior_fy.xml` into the cumulative P&L generator. Watch the next mock eval for judge flags.

**M6. `scripts/` still has untyped dated collection reads and unescaped company names.**
- **Where:**
  - `scripts/delete_seeded_vouchers.py:55-57`: untyped, with a raw `{company}`. It deletes whatever Tally returns, which is the *current period* rather than FROM/TO_DATE.
  - `scripts/patch_bill_allocations.py:59-61`
  - `scripts/manual_test_gst_live.py:275`, `scripts/manual_test_inventory_live.py:211-213`
  - `scripts/probe_*.py`
  - `scripts/seed_tally_data.py:42`: raw company name in a write envelope.
- **Impact:** the app does not use any of these. `delete_seeded_vouchers.py` is the one with write-safety relevance: on a company whose F2 period ≠ FY 2025-26 it deletes a different set of vouchers than its constants say.
- **Fix:** type the dates and escape the company name in `delete_seeded_vouchers.py` and `patch_bill_allocations.py` at least.

**M7. LESSONS.md wording on C43 from-dates is wrong.**
- **Where:** `LESSONS.md` (the new "C33/C43" block) says a non-1/2/31 from-date falls back "the same way" as the to-date, i.e. to the current period's **end**.
- **Evidence:** in the live audit log (single day 15-06-2023, typed) the from-date fell back to the current period **start** (241 vouchers, 20250401..20260331).
- **Why it matters:** the wording implies that current-period questions with a mid-month from-date would break on Educational. They don't.
- **Fix:** say "a from-date falls back to the current period's start".

**M8. The mock party-voucher lookup doesn't unescape the party name (pre-existing).**
- **Where:** `mock_handler._handle_party_vouchers` regex `\$PartyLedgerName = "(.*?)"` returns the XML-escaped name, e.g. `Sharma &amp; Sons…`.
- **Impact:** any future `_MOCK_PARTY_VOUCHERS` key containing `&` or `"` would never match.
- **Fix:** apply `html.unescape` to the captured name.

**M9. The mock `LedgerVchs` ignores the ledger filter (pre-existing).**
- **Where:** `mock_handler.VOUCHER_FIXTURES["LedgerVchs"]`.
- **Impact:** it now returns all prior-FY vouchers as well as the whole current day book, whatever ledger was asked for. Nothing parses a ledger filter on the Python side.
- **Fix:** later, filter by `PARTYLEDGERNAME` in the mock.

## Strengths

- A single `_date_vars` / `_company_var` helper removes all the copy-pasted envelopes, so they can't drift apart again.
- The mock models the real failure mode instead of hiding it. The `_untyped()` helper in `test_mock_c33_date_vars.py` pins the regression to the exact bytes that shipped before the fix.
- The tests stub only the network edge (a `TallyClient` in mock mode). Builder, parser and dedup service all run for real, as CLAUDE.md "Test reality" requires.
- The window bounds are always day 1 / 31, so they are C43-safe.
- LESSONS.md explains the "safety-net turns wrong into empty" mechanism well.

## Declined to judge

- **C43 clamping or warnings for reports on Educational Tally.** The audit made this an explicit non-goal (no edition notion in the backend).
- **Performance of multi-MB tail reads under C43.** It is Educational-only and pre-existing.
- **Mock P&L `SVFROMDATE` semantics** (LESSONS §2, cumulative from FY start). Unchanged by this diff.

## Test suites

**Run by this reviewer (in the worktree):**
- `tests/unit/` + `tests/integration/test_mock_c33_date_vars.py` + `tests/integration/test_mock_format_parity.py`: **1464 passed, 16 skipped**.
- `tests/integration/` + `tests/e2e/` without `TEST_DATABASE_URL`: **216 passed, 117 skipped** (the DB-gated tests).

**Not run by this reviewer:**
- DB-gated suites (`TEST_DATABASE_URL`: integration auth/workspace/conversation/upload-voucher-persistence, `tests/e2e/test_db_*`, `tests/unit/test_dedup.py` DB paths). The implementer's log has no summary line (I1).
- `tests/e2e_live/` (real Tally + Claude API).
- Eval (`tests/eval/collect.py` / `judge.py`, including `write_flow_*_mock`, `trends_and_breakdowns_mock`).
- Frontend Vitest / Playwright. There are no frontend changes, so this is not needed.
- A live read-only check of the default party-voucher window (I2).

## Resolution (controller, 2026-09-24)
- **I1 resolved:** full DB suite on this branch — `283 passed, 3 warnings in 836.29s` (TEST_DATABASE_URL=…:5434/tallyagent_test; log `logs/db-suite-backend-date-fix-2026-09-24.log` in the main checkout).
- **I2 resolved (live, read-only, company B, books from 01-04-2022):** `build_party_vouchers(['Sales','Receipt'])` — 01-04-2021..31-03-2023 (starts before books) = 14 = exact FY22-23; 01-04-2025..31-03-2027 (future FY) = 14 = exact FY25-26; 01-04-2021..31-03-2027 = 55 (whole history); default window for today 01-04-2025..31-03-2027 = 14; all < 0.2 s, Tally alive after. The dedup window cannot fail open from its shape.
- Live FY 2022-23 verification of every dated builder: day book 238, sales 71, purchases 60, party/ledger vouchers 14; company name with `&`/`'` → no "Unknown Request".
- **Observed, not verified:** a party-vouchers request with voucher_types including the custom name "Sales - GST" (→ `CHILDOF $$VchTypeSales - GST`) was followed by Tally timing out and closing. Production only passes fixed type names ("Sales", "Purchase", …), so not reachable today; treat custom type names in `$$VchType…` as unsafe until probed.
- Minors: deferred (not blocking) — see list above.
- Suites not run: e2e_live, eval (collect/judge), frontend (no frontend change).
