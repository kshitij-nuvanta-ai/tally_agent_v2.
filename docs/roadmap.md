# Roadmap

Single source of truth for what's planned next. Closed phases live in [`CLAUDE.md` § Implementation Phases](../CLAUDE.md). Parked / deferred items live in [`open-items-parked.md`](open-items-parked.md).

---

## Operating Mode

**SaaS + DB mode is the default and only maintained mode.** All new work targets DB mode (Postgres + JWT auth + per-workspace conversations). Legacy mode (in-memory sessions, no auth) is frozen — bug fixes only, no new features.

- Enable DB mode: set `DATABASE_URL` and `JWT_SECRET` in `.env`, run `alembic upgrade head`, and run frontend with `VITE_DB_MODE=true`. See [`README.md`](../README.md#running-with-auth--persistence-db-mode).

---

## Feature Sets

The product evolves along three feature sets. Set A is foundational; B and C can proceed in parallel once A is in place.

### Set A — SaaS Platform

| Sub | Scope | Status |
|---|---|---|
| **A1** | Auth (signup/login), workspaces (one per Tally company), persistent conversation history | ✅ Complete — merged. Spec: `specs/2026-04-02-set-a1-auth-persistence-design.md`. |
| **A2** | Billing (Razorpay — India), team management, Tally connection config UI | Not started |

### Set B — Compliance & Automation

| Sub | Scope | Status |
|---|---|---|
| **B1a** | Expense receipt entry (file upload → Vision extract → Tally write — Payment voucher only) | ✅ Complete — merged. Spec: `specs/2026-04-04-set-b1a-expense-entry-plan.md`. |
| **B1b** | Sales / Purchase voucher builders (extends B1a pipeline) | ✅ Complete — merged 2026-06-09 (Group B). |
| **B1c** | Debit Note / Credit Note voucher builders | ✅ Complete — merged 2026-06-09 (Group B). |
| **B1d** | Bank statement import + reconciliation | Deferred. Needs its own feasibility probe (BANKALLOCATIONS / BANKDATE / INSTRUMENTNO primitives unvalidated). See [`open-items-parked.md`](open-items-parked.md). |
| **Group A** | UI polish (F4 deferred conversation creation + workspace landing, F5 voucher button disable, F6 responsive drawer) | ✅ Complete 2026-04-12. Spec: `specs/2026-04-12-group-a-ui-enhancements-design.md`. |
| **Group B** | F1 (company dropdown) + F2 (multi-currency) + F3 (5-type classification) + B1b (Purchase/Sales) + B1c (DN/CN) + DB audit | ✅ **Complete — merged 2026-06-09.** Tasks 0–16. Spec: `specs/2026-04-12-group-b-voucher-types-design.md`; review: `code-review-group-b-2026-06-09.md`. (GST-on-invoices + DN/CN-direction now done — see Active Work.) |

### Set C — Sync & Store

Redesigned 2026-09-15 → 2026-09-21 as the **BI layer**: a Windows sync agent copies Tally into our Postgres,
and chat / dashboards answer from our DB. Sync-only replaces the old real-time tunnel idea (old C1/C2 rows retired).
Three parts: Part 1 syncing (`specs/2026-09-21-bi-part1-sync-design.md`), Part 2 AI DB queries, Part 3 UI + dashboard API.
**Merged into the main code 2026-09-30** on branch `feat/merge-v2-into-backend` (not yet pushed, no PR): one app on port 8000, one settings class, one Alembic chain (revision `006`), one uv project, one top-level `tally_bridge/`. Sync code is in `backend/sync/`, `backend/db/sync_models/`, `backend/api/`; `contract/`, `agent/` (placeholder) and `probes/` are top-level packages. Spec `specs/2026-09-30-v2-merge-design.md`; **merge status lives in [`plans/2026-09-30-v2-merge-tracker.md`](plans/2026-09-30-v2-merge-tracker.md)**.
**Target architecture (senior dev, 2026-09-30): installer → agent → tally bridge**; the cloud app talks only to its own Postgres. Not done in the merge: legacy (non-DB) mode still exists (removal is a separate later step); chat still reads Tally live through `tally_bridge` (the synced tables are not read by chat); the agent and installer are not built.
*(Superseded 2026-09-30, v2 merge: "**Built as v2 under `v2/` — no current code is changed** (Part 1 spec §5 "Code isolation (v2)"); merging into current code is a later, separate step.")*
**Item-level status lives in [`plans/2026-09-22-bi-part1-tracker.md`](plans/2026-09-22-bi-part1-tracker.md)** — this table is stage-level only.

| Sub | Scope | Status |
|---|---|---|
| **S0** | Live-Tally probes 0–25 + real fixtures (Part 1 §12) | ✅ **Closed 2026-09-25: the S0 exit gate PASSED with recorded exceptions** (`docs/bi-s0-exit-gate-2026-09-25.md` § "Final verdict"). Exceptions: Educational/Wine only (R8 open); probes 9/20 ⏭ tier C; probes 11 B/16 B need an expression-form balance parser before any re-run; B verdicts of 3/11/14/16/25 were judged on the pre-forex dataset; review minors deferred to S1 hardening; `CLAUDE.md` `dc9bf02` accepted; probe logs local only. **Next: S1.** **Previous status (superseded 2026-09-25, S0 exit gate):** 🟡 **Plan part 7 done 2026-09-25** (`docs/plans/2026-09-25-bi-s0-probes-plan-part7.md`) — **probe 22 (forex) CONFIRMED**: a forex voucher line states its INR base in the export (`-$448.44 @ ? 82.99/$ = -? 37216.04`), so decision 15 holds (no conversion at read time). C36 lifted: the 2 USD export sales are loaded as forex on a dedicated `Gulf Office Supplies LLC (USD)` ledger; the `$` currency master is UI-only. **New Ruling C47:** a forex ledger is valued at its latest voucher rate, not the sum of the bases (TB out by the unrealised difference, ₹183.87), and its balances export as an expression string. Probes 21 B and 18 B re-run CONFIRMED on the new dataset. **Every tier-B probe now has an outcome.** **Remaining:** the S0 exit-gate check (S0 spec §10); then the S1 spec (it must decide how to parse expression-form forex balances; probes 16 B / 11 need that before any re-run). Timing probes 9 / 20 / 21-timing deferred (Q29). **Previous status (superseded 2026-09-25, plan part 7):** 🟡 **Plan part 6 done 2026-09-25** (`docs/plans/2026-09-25-bi-s0-probes-plan-part6.md`) — B parts of 3 (CONFIRMED: cancelled/optional vouchers come with their flags), 23 (CONFIRMED: credit period + Bills due dates agree), 25 (DIFFERENT; **R9 CONFIRMED — Tally refuses a duplicate ledger name**), probe 11 re-run under C46 (DIFFERENT: ledger **and** stock master openings are current-period — corrects the 2026-09-24 reading), company C (`Probe Vault Co`, 100001) + probe 24 (CONFIRMED: security/TallyVault leave the export unchanged; a pending prompt reads as no company open). Every built probe has now run live. **Remaining:** probe 22 BLOCKED on the USD-sales (forex) write shape (C36); the S0 exit-gate check (S0 spec §10); then the S1 spec. Timing probes 9 / 20 / 21-timing deferred (Q29). **Previous status (superseded 2026-09-25):** Plan part 5 done 2026-09-24 — probes 16/17/18 A re-run typed, probes 11/14/15/18 B built and run live on company B (`bi-s0-probe-results-2026-09-24.md`). Outcomes: 17 ✅, 18 (A+B) ✅, 14 ✅, 15 ✅, 16 FAILED (typed SVTODATE works only inside the current period — **new finding, Ruling C45** — clamps to the period start for an earlier date), 11 FAILED stock-only (**new finding, Ruling C46** — `StockItem.OpeningBalance` is the current period's opening, not books-start). The 2026-09-23 "Bills/Stock ignore the as-on date" finding (LESSONS rule 20) is **superseded** — it was a Ruling C43 artefact of an Educational-ignored date, not a Tally limitation; re-measured at a valid date it matches exactly. C44 (company-switch-on-restart) live-verified both directions. Every probe through 21 has now run live except 3/23/25's B parts and probe 22 (BLOCKED, C36) and probe 24 (company C). Next: **plan part 6** — B parts of probes 3, 23, 25; then company C (probe 24). Timing probes 9 / 20 / 21-timing deferred (Q29) |
| **S1** | Cloud: sync tables, device auth, ingest API, parity engine (Part 1) | ✅ **Built 2026-09-29, merged into the backend 2026-09-30** on `feat/merge-v2-into-backend` (commits `13d6bbd`, `7d5d64e`, `12c36ba`, `23c6401`; **not yet pushed, no PR**). Whole suite after the merge: 3186 passed / 594 skipped without a DB, 3770 passed / 10 skipped with the test DB. Still open in the merge tracker: docs, real-app pass, code review. **Next:** legacy-mode removal; DB-backed reads (chat and reports answer from the `tally_*` tables); agent + installer spec. **Previous status (superseded 2026-09-30, v2 merge):** ✅ **Built 2026-09-29** on `feat/bi-s1-cloud` (not yet merged — PR to `dev` pending): plan Tasks P, 0–15 done; whole-branch review `docs/code-review-bi-s1-2026-09-29.md` (0 C / 8 I fixed); v2 suite 1948 passed with DB; real-data parity `ok` on companies A and B (B's books-start anchor from Tally's own G6 TB). Open: manual dev-DB pass (dev DB location unclear), deferred minors in the tracker. **Next: PR, then the S2 spec.** *(Superseded 2026-09-29: "Building (2026-09-28)".)* |
| **S2** | Windows agent (Part 1) | Not started. `agent/` is a placeholder package. **Next: an agent + installer spec** under the 2026-09-30 direction (installer → agent → tally bridge: the bridge runs on the Tally PC, the backend stops importing `tally_bridge`, the write path goes through the agent) — after the merge lands. *(Superseded 2026-09-30: "next after S1's PR".)* S1 spec carries the S2 contract notes (as_on = current-period end, confirm id recovery via `/state`, quarantine re-offer) |
| **S3–S5** | Part 2 (AI DB queries) and Part 3 (UI + dashboard API) | Not started — after S1 |

---

## Active Work — Next Up

The documented **Group B** (`specs/2026-04-12-group-b-voucher-types-design.md`) is now built and merged. We implemented exactly the doc (upload-driven, 5-type classification) — not the earlier A–F slice reframing.

| Item | Scope | Status |
|---|---|---|
| **F2 (Slice A)** | Foreign-currency → INR: original currency+amount, code-computed rate, chat-only override, INR-only into Tally, no-rate writes blocked | ✅ **Merged 2026-06-08.** Spec `specs/2026-06-08-fx-inr-conversion-design.md`; review `code-review-fx-inr-2026-06-08.md`. |
| **Group B (F1+F3+B1b/B1c)** | Upload → Vision classifies `payment/purchase/sales/debit_note/credit_note` → route to builder → party + GST + bill allocation → write; company-name dropdown (test-connection); DB audit (UploadedFile + VoucherEntry) | ✅ **Merged 2026-06-09.** Plan `plans/2026-04-12-group-b-voucher-types-plan.md` Tasks 0–16; review `code-review-group-b-2026-06-09.md`. |
| **GST on invoices** | Map extracted GST line items → Input (Purchase/DN) / Output (Sales/CN) GST ledgers, resolved from the workspace's Duties & Taxes ledgers | ✅ **Merged 2026-06-09.** Live-verified (CGST/SGST Input/Output post correctly). |
| **DN/CN posting direction** | Verify a Debit Note reduces the payable and a Credit Note reduces the receivable | ✅ **Fixed + live-verified 2026-06-09** (was inverted; caught by live manual test). `code-review-group-b-2026-06-09.md`. |
| **Write-flow eval** | Eval framework extended for upload→review→write (upload/action turns, `voucher_correctness` rubric, DB-mode login) | ✅ **Merged 2026-06-09.** `docs/eval-write-flow-2026-06-09.md`. |
| **End-to-end live verification** | Real document/PDF → real Claude Vision → classify → live Tally write → read-back → cleanup, all 5 types | ✅ **Verified 2026-06-09** (`logs/manual_test_*_live.log`). |

### UI / persistence (2026-06-12)

- **Upload/voucher review cards survive reload** ✅ **Merged 2026-06-12 into `dev`.**
  `/chat/upload` now persists user+assistant Messages (was audit-row only) and `/chat/voucher-action`
  persists written/discarded status (threading `conversation_id` via the request). Review
  `code-review-upload-voucher-persistence-2026-06-12.md`; lesson `LESSONS.md` §17.
- **Sidebar conversation actions — rename + delete** ✅ **Merged 2026-06-12 into `dev`.**
  ChatGPT-style per-conversation kebab (⋯) menu: inline Rename (PATCH) + Delete with confirm (soft-delete);
  deleting the active chat returns to the workspace landing; menu flips up near the list bottom; a11y roles added.
  Frontend-only (backend `DELETE`/`PATCH` pre-existed). Spec `specs/2026-06-12-sidebar-conversation-actions-design.md`;
  review `code-review-sidebar-conversation-actions-2026-06-12.md`. Tests: 351 FE unit, +3 backend E2E, 12 Playwright (×3 viewports).
- **Connect-modal improvements** ✅ **Built — `feat/connect-modal-improvements`, pending merge.**
  "Test Connection" → "Check Connection" (neutral restyle); setup-steps panel on a failed live check;
  restored the optional workspace **Name (nickname)** field; sidebar shows `Company (nickname)` only when the
  nickname differs (no more `Company (Company)` duplication). Frontend-only. Spec
  `specs/2026-06-12-connect-modal-improvements-design.md`; review `code-review-connect-modal-improvements-2026-06-12.md`.
  Tests: 363 FE unit; rewrote 2 stale connect-company Playwright specs + added connect-company-steps (9 × 3 viewports).

### Write-flow edit fixes + edit-history + file_id linkage (2026-06-29)

- **Classifier company-anchoring** ✅ **Committed locally (`1735e8c` on `feat/connect-modal-improvements`).**
  Vision prompt anchors purchase-vs-sales to the workspace's own company; hierarchy-aware ledger matching
  (parties under custom sub-groups like National/Local Creditors now match instead of being flagged new);
  existence-safe `create_ledger` (name+parent match, company-scoped, fail-safe); party ledgers `is_billwise`.
  Spec `specs/2026-06-19-classifier-company-anchoring-design.md`; review `code-review-write-flow-fixes-2026-06-19.md`.
- **Edit-flow correctness (manual-testing-driven)** ✅ **Committed locally (`1735e8c`).**
  Edit **Save** = local merge (no premature Tally write); draft edits + voucher-action result messages now
  **persist across page refresh** (new `save_draft` action + `_update_persisted_voucher_entry` + `_persist_action_response`);
  inventory edit **recomputes total + GST** proportionally; upload intro text no longer restates a mutable amount/date.
- **Edit-history version trail** ✅ **Committed locally (`1735e8c`).** New `voucher_entry_revisions` table (migration **004**)
  snapshots every version (`upload → edit → write`); original stays immutable in `uploaded_files.extracted_data`,
  current card in `messages.data`, full trail queryable in `voucher_entry_revisions`.
- **`file_id` linkage** ✅ **Committed locally (`1735e8c`).** Migration **005** + entry now carries `file_id`, so
  original (`uploaded_files`) ↔ card ↔ revisions ↔ audit (`voucher_entries`) all join on `file_id`. Backfill
  `scripts/backfill_entry_file_id.py` for legacy rows.
- **Tests** ✅ refresh round-trip (act→reload→re-assert whole state), mock-Claude + live-Tally e2e
  (`tests/e2e_live/test_db_write_live.py`, 5 voucher types, with cleanup), eval sales/DN/CN anchoring scenarios.
  2,006 automated passing (1,625 backend + 381 frontend, 0 failures). Doc rules added: CLAUDE.md "Test reality" +
  "refresh round-trip"; LESSONS.md §15 (ISINVOICE GST recompute, bill-wise reporting).

**Group B Task 0 feasibility probes — DONE (2026-06-08).** Results: [`group-b-task0-probe-results-2026-06-08.md`](group-b-task0-probe-results-2026-06-08.md). E1–E8 verified (DN/CN creation, company list, party-voucher filter). Two extra probes: **TDS journals persist** (T1a/T1b) and **bank instrument details persist** via `BANKALLOCATIONS.LIST` (B1) — but the **bank-reconciliation date does NOT persist** via voucher import, so reconciliation needs Tally's dedicated mechanism.

### Next up — from the write-flow code analysis (`docs/code-analysis-write-flow-2026-06-09.md`)

Correctness is proven live; these are hardening/quality items:
- **SSRF (security, HIGH):** `tally_host`/`tally_port` are user-set with no validation — add an allowlist / block private+link-local ranges.
- **Entry-dict typing + sign-convention single-source** — wire up the `VoucherReviewEntry` Pydantic model in `voucher_action`; extract the per-type debit/credit polarity to one config.
- **Cheap correctness warnings:** DN/CN with no reference (silent On-Account), Vision currency missing → defaults INR, GST ledger missing → warn-not-block.
- **Eval mock GST ledgers** — add CGST/SGST/IGST Input+Output to the mock Tally handler so mock-mode eval scores GST.

### Future work (beyond the current doc — need their own spec)

- **Invoice entry Phase 1 — Supplier Invoice No. + duplicate blocking** ✅ **Merged 2026-06-10.** Supplier
  invoice no. written to Tally's `REFERENCE` (live-verified) + shown on the card; duplicate **hard block**
  (file-hash + party/invoice-no vs DB & Tally, **server-side re-derived** so it's not client-bypassable,
  per-workspace). Spec `specs/2026-06-10-invoice-entry-phase1-design.md`; review `code-review-invoice-dedup-2026-06-10.md`.
- **Invoice entry Phase 2 — Inventory line items** ✅ **Merged 2026-06-11.** Goods invoices now post line
  items into the Tally **stock grid** (qty/rate) via the stock-based builder, with Vision unit extraction,
  a stock-item fuzzy resolver, goods-vs-services routing, and match/create-in-card UI. Create-new items:
  GST-Not-Applicable when no HSN, non-reserved group, only-create-missing masters (fixed a duplicate-master
  modal that froze Tally). **Live-verified 5/5** (items in grid, balanced voucher, books restored).
  Spec `specs/2026-06-10-invoice-entry-phase2-inventory-design.md`; review `code-review-invoice-inventory-2026-06-10.md`.
  Follow-up: on-the-fly stock-item creation needs HSN to set master GST (currently GST-NA + voucher-level GST);
  add an HSN field to the create-new card if master-level GST is wanted.
  - **Post-merge live-testing fixes (2026-06-11/12)** — found by manual upload, each TDD'd + live-verified:
    - **Invoice-number extraction.** A purchase/sales invoice's OWN number was never captured (the prompt only had
      `original_invoice_ref`, scoped to DN/CN). Added a distinct `invoice_number` field → drives the card "Invoice #",
      the Tally `REFERENCE`, and the party+invoice-no dedup key (DN/CN keep `original_invoice_ref` for the against-bill).
      Real Vision verified extracting `invoice_number`. (`fix/invoice-number-extraction`, merged.)
    - **Master-create idempotency + no false success.** A duplicate-master CREATE froze Tally with a modal; an existing
      group returning `altered=1` aborted the write. Now: list items+groups (incl. empty groups via new
      `list_stock_groups`) and only create missing masters; treat `altered=1, errors=0` as already-exists success;
      frontend reverts the optimistic "Written" badge on a `voucher_error`. See [`LESSONS.md` §15 rules 10–14](../LESSONS.md).
      (`fix/inventory-master-altered-idempotent`, merged.)
    - **Tally gotchas captured** in `LESSONS.md` §15 (duplicate-master modal freeze, GST-needs-HSN, reserved "Primary"
      group, future-dated silent drop, Sales hides REFERENCE).
  - **Open follow-up (declined this session):** also append the invoice number to the **Narration** so it's visible
    on the Sales voucher screen (Tally hides REFERENCE there by default). Not implemented.
  - **Upload/voucher review cards vanished on reload (2026-06-12)** ✅ **Fixed on `fix/upload-voucher-persistence`,
    pending merge.** `/chat/upload` persisted only an `UploadedFile` audit row (no `Message` rows), so review cards
    disappeared on refresh/relogin; `/chat/voucher-action` never persisted the written/discarded status (and its
    `conversation_id` gate read a key the production entry dict never has — also a silent no-op for the `VoucherEntry`
    audit row). Now persists user+assistant Messages + titles the conversation, and threads `conversation_id` via the
    request to persist status. New lesson [`LESSONS.md` §17](../LESSONS.md); review
    [`code-review-upload-voucher-persistence-2026-06-12.md`](code-review-upload-voucher-persistence-2026-06-12.md).

<!-- superseded — was: -->
- **(superseded) Inventory line items** — original gap note. The agent invoice write-path used the
  **accounting-only** ledger builder (`_build_ledger_invoice_voucher`): it lumps the base on a single
  purchase/sales ledger and puts the extracted item names in the **narration** — no stock items, qty, or
  rate in the inventory grid. Vision already extracts `line_items` (description/qty/rate/amount); they're
  just not mapped to Tally stock items. The **inventory-capable builder exists** (`build_create_purchase_voucher`
  / `..._sales_voucher`, used by the seeder) but isn't wired to the upload flow. Needs a spec covering:
  stock-item resolution/creation (+ units), qty×rate per line, per-item GST, and writing the
  **Supplier Invoice No.** field. Also fix **ledger auto-mapping category** (e.g. paper → "Purchase - Electronics").
  Discovered 2026-06-10 via live upload (voucher posted accounting-only).
- **Supplier payment against an outstanding bill** (Agst Ref settlement of an existing payable), **TDS journals** (sale/purchase each need invoice + settlement + TDS journal), and **bank-statement reconciliation** (the reconciliation *date* needs Tally's dedicated mechanism — probe-confirmed it doesn't persist via voucher import).

> **Ops note:** restart the backend after merging write-path features — a stale server (pre-GST-merge)
> served accounting-only/no-GST vouchers on 2026-06-10 even though the merged code was correct.

---

## Test Data

Group B work uses the `Bharat Traders Private Limited` demo company. Backup committed at `seed_data/TDBK1800_100003.001`. Restore + reseed procedures in [`seed-data-setup.md`](seed-data-setup.md).

---

## Key Decisions (Locked)

- **Postgres + pgvector** for cloud DB (covers chat history, agent memory, future Tally data sync)
- **Razorpay** for billing (India market — not Stripe)
- **Existing HTTP XML bridge** reused as local connector (not ODBC)
- **DB mode is default**; legacy mode is frozen

---

## Maintenance Convention

This file is the single source of truth for "what's planned next." Update it when:
- A feature set closes (move from active → ✅ Complete, then summarize in `CLAUDE.md § Implementation Phases`)
- A new feature set is added
- Priority changes (re-order Active Work section)
- A probe / gating step is added or resolved

Don't track active roadmap in `CLAUDE.md` (status only) or in individual spec/plan docs (those are per-feature).
