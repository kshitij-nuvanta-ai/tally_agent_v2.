# Open Items — Parked

_Last updated: 2026-09-30 (section "From the v2 merge" added; the rest is as of 2026-06-03)_

Items from Phases 1–16 deferred while focus is on SaaS (Set A) and compliance (Set B). Current active work is tracked in [`roadmap.md`](roadmap.md). Closed phases are summarized in [`CLAUDE.md`](../CLAUDE.md).

**Sources:**
- Architecture explorations (F1–F4, Company Selector): [`plans/2026-03-12-agent-architecture-exploration.md`](plans/2026-03-12-agent-architecture-exploration.md)
- Phase 8 backlog (conversation memory, cached ledger list, GST reports, export, WhatsApp, cross-agent context): [`TALLYPRIME_AGENT_PLAN.md` §9](../TALLYPRIME_AGENT_PLAN.md) (archived spec)

## Completed Since This Was Parked

- **Eval framework** — all 7/7 turns pass on stock_reorder_mock (Run 31). All eval scenarios green.
- **F2-A (pass last N messages)** — implemented in Phase 14.
- **Agent architecture C1 refinement** — implemented in Phase 12.
- **Date-relative intelligence** — implemented in Phase 7.

## Parked Architecture Items

### F1: Merge QueryAgent + AnalysisAgent
- Would eliminate handover gaps, reduce token waste from two agent histories
- Risk: 18+ tools in one agent may confuse model
- Status: Open — revisit if handover bugs resurface

### F2-B/C: Session Data Cache (cleaner options)
- F2-A (pass last N messages) is done in Phase 14
- Cleaner options: shared session cache (B) or merged agents (C) still deferred
- Status: Open — current approach works for 5–7 turn sessions

### F3: Frontend Streaming (SSE/WebSocket)
- Backend streaming done (AnalysisAgent uses `messages.stream()`)
- Frontend still waits for full response; 480s timeout is workaround
- Progressive UI ("Fetching... Analyzing... Charting...") not built
- Status: Open — biggest UX win remaining

### F4: XML-Tagged Structured Output
- Replace `STRUCTURED_RESULT:` prefix with XML tags for robust parsing
- Status: Open — current approach works, XML would be more robust

### Company Selector End-to-End
- Dropdown exists but is display-only
- `<SVCurrentCompany>` plumbing exists in request_builder but is never wired
- Medium-high priority for multi-company deployments
- Status: Open — required before real deployment

### Latency Optimization
- Turns with 76k–106k input context take 240s+
- Frontend streaming would help perceived latency
- Context pruning or summarization not explored
- Status: Open — addressed partially by 480s timeout

## Parked Phase 8 Backlog Items

From [`TALLYPRIME_AGENT_PLAN.md` §9](../TALLYPRIME_AGENT_PLAN.md):

| Item | Status | Notes |
|------|--------|-------|
| Conversation memory (last 10 messages) | Done (DB mode) | Persistent conversations land via Set A1. Legacy mode remains in-memory. |
| Cached ledger list | Open | Would speed up fuzzy matching / suggestions |
| GST reports (GSTR-1, GSTR-3B, tax liability) | Open | Superseded by compliance feature set (Set B) |
| Export (PDF/Excel) | Open | — |
| WhatsApp integration | Open | Twilio/Meta webhook, text-only responses |
| Cross-agent context preservation | Partial | F2-A done (last N messages), longer sessions untested |

## Parked Tally Write-Agent Items

### Per-voucher-type external-doc-date toggles (Sales/Receipt/Payment)

Purchase has a "Use supplier invoice date" toggle (Voucher Type → Configuration) that gates `<REFERENCE>`/`<REFERENCEDATE>` writes. When OFF, REFERENCEDATE writes are silently overwritten with the voucher's main `<DATE>` field (no error, `altered=1`, readback looks healthy — most insidious silent-failure mode encountered). When ON, the same XML envelope sticks correctly.

Sales (customer PO date), Receipt and Payment (cheque date for post-dated cheques) likely have analogous per-voucher-type toggles, since REFERENCE/REFERENCEDATE have natural meaning on those types too — but the toggle locations and exact behavior have not yet been probed. Required before write-agent supports these fields on those voucher types.

- Owner: write-agent design phase.
- Cross-ref: `LESSONS.md` §14, `docs/tally-write-exploration-v4.md` § "REFERENCEDATE — supplier invoice date (toggle-gated)".
- Status: Open.

### Set B1d feasibility probe — bank reconciliation primitives

Set B1d (bank statement import + reconciliation) is deferred per Group B design but will need its own feasibility probe before implementation, similar to Group B Task 0. Several primitives are currently unvalidated against live Tally:

- `BANKALLOCATIONS.LIST` block structure inside Receipt/Payment bank LEDGERENTRY
- `BANKDATE` field — sets reconciled-with-bank date on a voucher (silent-failure risk per LESSONS §14 if there's a toggle gate)
- `INSTRUMENTNO` and `INSTRUMENTDATE` fields — cheque/UTR no + cheque date
- Whether `BANKDATE` can be set via ALTER on existing Receipt/Payment vouchers (similar to REFERENCEDATE pattern), or only on Create
- Tally's "Bank Statement" import envelope (separate from voucher import) — does it exist as a documented import flavor, or is the right approach to ALTER bank-side fields directly?

- Owner: Set B1d design phase.
- Cross-ref: `LESSONS.md` §14 (silent-overwrite warning), `docs/tally-write-exploration-v4.md` (general write findings).
- Status: Open.

### Connect-company: multi-company Tally selector

`ConnectCompanyModal` currently takes `companies[0].name` from `GET /api/companies` as the actual Tally company when verifying a connection. If a Tally instance has multiple companies loaded, the user can't pick which one. Add a company selector (dropdown of the returned `companies`) in the modal's form step, storing the chosen name in `config.tally_company`.

- Owner: FE follow-up.
- Cross-ref: `docs/plans/2026-06-04-uiux-fixes-otel-heartbeat.md` Task 4.
- Status: Open.

### Frontend production build (`tsc -b`) is broken on master

`npm run build` (`tsc -b && vite build`) fails on `master` (pre-dates branch `fix/uiux-nav-heartbeat`): the build tsconfig is stricter than the dev/test `npx tsc --noEmit` gate and includes test files, surfacing errors in `ChartRenderer.tsx` (recharts Formatter typing), `ChatWindow.tsx` (`m.data as Record<string,unknown>` cast of `TableData|TableData[]`), and unused-import errors in test files. The project's working gate is `npx tsc --noEmit` (clean) + Vitest; CI/build via `tsc -b` does not currently pass. Fix the build tsconfig (separate app vs test typecheck, relax test-file unused-locals, or correct the recharts/TableData casts) so `npm run build` is green.

- Owner: FE infra follow-up.
- Status: Open.

### Write-flow eval coverage

The eval framework (`tests/eval/`) covers the query flow only. Write paths (Set B1a Payment, Group B Sales/Purchase/DN/CN, and now the connect→Start chat flow) have no eval scenarios. Add write-flow eval scenarios with golden assertions on voucher creation outcomes.

- Owner: eval framework follow-up.
- Status: Open.

## From the v2 merge (2026-09-30)

Left open on purpose by the merge of the sync service into the main code (spec [`specs/2026-09-30-v2-merge-design.md`](specs/2026-09-30-v2-merge-design.md), status [`plans/2026-09-30-v2-merge-tracker.md`](plans/2026-09-30-v2-merge-tracker.md)). The merge moved code and changed no behaviour; these are the duplicates and gaps it kept.

### Sync request models defined twice (contract vs. API code)

`contract/models.py` holds the typed wire models. The sync API imports only four of them (`BatchRequest`, `ParityRequest`, `ReconcileRequest`, `SnapshotRequest` in `backend/api/sync.py`). The other bodies are validated by looser local models: `SeenCompany`, `Counters`, `HeartbeatRequest`, `RelinkRequest`, `CoveragePatchRequest` in `backend/api/sync.py`; `WebCommandRequest` in `backend/api/workspace_sync.py`; `LoginBody`, `RefreshBody` in `backend/api/agent_auth.py`; `BindRequest` in `backend/sync/binding.py`; `RunCreate`, `RunPatch` in `backend/sync/runs.py`. The contract has its own `SeenCompany`, `Counters`, `HeartbeatRequest`, `RelinkRequest`, `CoveragePatch`, `WebCommand`, `LoginRequest`, `BindRequest`, `RunCreate`, `RunPatch`. An agent built against the contract and a server validating with the local models can drift.

- First raised in the S1 review (`code-review-bi-s1-2026-09-29.md`, finding I2: "the contract's typed model is not the model the route uses"; M7 is the related contract-vs-spec drift).
- Fix: make the routes use the contract models, or delete the unused contract ones. Do it with the agent + installer spec, when the agent side becomes real.
- Status: Open.

### The 9 app tables' ORM models differ from their own migrations (27 catalog details)

`alembic upgrade head` and `Base.metadata.create_all` build the same 30 tables, but for the 9 pre-merge app tables 27 catalog entries differ. They are pinned exactly as `KNOWN_APP_TABLE_DRIFT` in `tests/sync/db/test_migration.py` (`test_migrated_schema_equals_model_metadata`), so a new difference fails the test. What differs: server defaults present in the migrations but not in the models (`created_at` on all 9 tables, `updated_at` on 4, `is_active`, `is_deleted` ×2, `agent_type`, `config`, `memory`, two `status` columns, `confidence`, `use_count`), with nullability differences on the timestamp and boolean columns; and migration `005` naming the `voucher_entry_revisions.file_id` foreign key and index differently from the model's default names. The 21 sync tables have no drift.

- Fix: align the models (add `server_default` / names) or write a migration, then empty the dict.
- Status: Open.

### Two `build_company_list` and two `parse_company_list`

- `tally_bridge/request_builder.py::build_company_list` (app path) and `tally_bridge/envelopes.py::build_company_list` (used by the probes) both exist; the merge kept both because they differ.
- `tally_bridge/response_parser.py::parse_company_list` (the app's connect-company dropdown) also takes a COMPANY element's inline text as a name, so CMPINFO's `<COMPANY>0</COMPANY>` counter comes back as a company called "0". `tally_bridge/xml_utils.py::parse_company_list` reads only a NAME child or attribute and does not have that defect. The app's one was left unchanged (no behaviour change in the merge).
- Fix: move the app to the `xml_utils` parser and one builder, with the company-dropdown tests updated.
- Status: Open.

### Float and Decimal amount parsing side by side

`tally_bridge/response_parser.py::parse_amount` returns `float` (chat path, report parsers unchanged). `tally_bridge/amounts.py` (`parse_decimal`, `parse_amount` returning `Decimal` + forex parts) and `tally_bridge/sync_reports.py` serve the sync path. Two functions named `parse_amount` with different return types live in one package (merge spec M11).

- Fix: belongs to the DB-reads step — once chat answers from the synced tables the float parsers can go.
- Status: Open.

### `tally_bridge/mock_handler.py` loads `tests/fixtures` at run time

The mock handler reads fixture files from `tests/fixtures/` and loads `tests/fixtures/generate_fixtures.py` by file path. It is the one recorded exception to the layer rule (`RUNTIME_FILE_LOADS` in `tests/test_layers.py`). A packaged `tally_bridge` (e.g. inside the desktop agent) would not have `tests/` next to it.

- Fix: move the mock data into the package, or move the mock handler out of `tally_bridge`.
- Status: Open — predates the merge.

### S1 review minors M1–M19 still open

The deferred Minor findings of `code-review-bi-s1-2026-09-29.md` (table "Minor", M1–M19) were not touched by the merge; the file paths in that table are pre-merge (`sync/…` → `backend/sync/…`, `api/web_sync.py` → `backend/api/workspace_sync.py`, `api/dependencies.py` → `backend/api/sync_dependencies.py`, `tests/cloud/…` → `tests/sync/…`). M15 names `tests/test_copied_headers.py` / `test_isolation.py` and `web_jwt.py` / `passwords.py`, all deleted by the merge, so it needs re-reading rather than fixing as written. The review's own spec-edit notes record which of them were settled by a spec edit instead of code.

- Status: Open.

### Code review minors parked (2026-09-30, `code-review-v2-merge-2026-09-30.md`)
- **M7:** no startup check that the database is at revision `006` when the sync routes are mounted; a server started
  against an unmigrated database answers every sync call with 500 `internal_error`.
- **M8:** `TallyClient.health_check` for probe callers now sends the app's company-list request with the 90 s timeout
  (no caller found today).
- **M9:** the login limiter lives on the module-level app; e2e DB tests that share that app and an email can carry
  failed-login counts between tests.
- **M4 rest:** `V2_ROOT` constant in `probes/__main__.py` / `probes/setup/s1_capture.py`; stale "Task 5 lands binding.py"
  docstring in `backend/sync/__init__.py`.
