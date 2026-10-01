# Code review — tally_bridge dedupe 1 (2026-10-01)

- **Branch:** `chore/tally-bridge-dedupe-1`, cut from `feat/merge-v2-into-backend` @ `72edd3f`. Not pushed.
- **Range reviewed:** `72edd3f..ca43433` (parts A–D). Fix round for the review: `85eb0cc`.
- **Reviewer:** independent subagent, read-only, given the intended change as claims to check.
- **Verdict:** 0 Critical, 0 Important, 4 Minor — all fixed in `85eb0cc`.

## What changed

| Part | Commit | Change |
|---|---|---|
| A | `19c7d69` | Connect-company dropdown (`queries/masters.py::get_company_list`) uses `envelopes.build_company_list` + `xml_utils.parse_company_list`. Deleted `request_builder.build_company_list` and `response_parser.parse_company_list`. Fixes the company called "0" (CMPINFO's `<COMPANY>0</COMPANY>` counter read as a name). |
| B | `dcc734f` | Chat tool `list_companies` (and `/api/companies`) builds `Company` models from `get_company_list`; its own inline parsing loop is gone. `TallyClient.health_check` posts `envelopes.build_company_list()`. Deleted `request_builder.build_list_companies`. |
| C | `3c6a64e` | Guard `tests/tally_bridge/test_no_duplicate_names.py`: no top-level name defined in two `tally_bridge` modules, except the five float/Decimal parsers (merge spec M11), each pinned to its exact modules. |
| D | `ca43433` | Deleted the unused `tally_bridge/models.py::VoucherEntry` and its test. `backend/db/models.py::VoucherEntry` untouched. |

After A–D, `tally_bridge` has one company-list request and one company-list parser.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| M1 | Minor | `docs/open-items-parked.md` section "Two `build_company_list` and two `parse_company_list`" stale | **Fixed** (`85eb0cc`): marked Done with commits |
| M2 | Minor | Parked item M8 still spoke of "the app's company-list request" | **Fixed** (`85eb0cc`): reworded |
| M3 | Minor | `envelopes.py` docstring said "sync path and probes" only; it now also holds the app's company-list request | **Fixed** (`85eb0cc`) |
| M4 | Minor | Guard allow-list was a name set: a third copy of an allowed name, or an alias assignment, went unnoticed | **Fixed** (`85eb0cc`): allow-list maps each name to its exact modules; top-level alias assignments counted. Proved by temporarily adding a third `parse_bills`, a new duplicate `async def`, and an alias — each failed the guard, then reverted |

## Checked and found sound (reviewer)

No remaining caller of the deleted functions or of `tally_bridge.models.VoucherEntry`; old and new parsers compared on all 210 recorded XML replies under `tests/` and `probes/` that contain `COMPANY` — identical on every company-list reply except the dropped CMPINFO counters (the fix); error handling of `list_companies`, `/api/companies`, `/api/tally/test-connection`, `health_check` unchanged; mock routing (`"List of Companies"` substring) unchanged; no assertion about real behaviour weakened; layer rule holds.

## Verification

| Check | Result |
|---|---|
| Tests written first and seen failing | A: `['0', …] == […]`; B: posted request mismatch; C: guard failed on deliberate duplicates |
| Full suite, no DB | **3212 passed / 606 skipped / 0 failed** (`logs/dedupe1-nodb.log`; baseline 3209 / 606 with one flaky failure) |
| Full suite, `TEST_DATABASE_URL` | **3808 passed / 10 skipped / 0 failed** (`logs/dedupe1-db.log`; baseline 3806 / 10). Net +2 = +5 company-list tests, +2 guard, −1 envelopes, −3 request_builder, −1 models. `tallyagent_test` left as found |
| After `85eb0cc` | `tests/tally_bridge` + `tests/test_layers.py`: 124 passed |
| Real TallyPrime (Wine, localhost:9000, two companies), read-only | **Before:** dropdown `['0', 'Bharat Traders Probe Copy', "Sharma & Sons' Probe Traders"]`; old and new request got byte-identical replies (`logs/tally-dedupe-baseline/`). **After A–D:** dropdown and `list_companies` return the two companies only; `health_check()` True (`logs/dedupe1-real-tally.log`) |
| Real app, mock mode (backend on 8011 against `tallyagent_fork`, mock Tally server) | `/api/tally/test-connection` → `connected: true, ["Bharat Traders Pvt Ltd"]`; `/api/health` → healthy, mock; chat tool `list_companies` → the company; `health_check()` True |

## Incident during verification

The first DB-suite run was started while real TallyPrime listened on `localhost:9000`. Some integration tests
(`tests/integration/test_invoice_dedup_flow.py`, `test_invoice_inventory_flow.py`) build a non-mock
`TallyClient("localhost", 9000)` and rely on the connection being refused; their read requests reached Tally, which
crashed (Memory Access Violation, `tallyerr.log` 15:37:23; the same crash is logged under Wine on 2026-09-29) and the
run stalled. **No data changed:** `tally.imp` last written 2026-09-25, and no company file changed after the companies
were opened at 15:25:09. A replay of those tests against a stub that refuses :9000 showed reads only. The suite was
re-run with Tally stopped. Parked as a test-isolation gap (`docs/open-items-parked.md`).

## Not reviewed / not run

`e2e_live`, eval, Playwright, frontend; Windows TallyPrime (only TallyPrime under Wine — probe E7 covered Windows earlier).
