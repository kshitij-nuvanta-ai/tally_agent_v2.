# LESSONS.md — Tally API Learnings

Hard-won lessons from building the TallyPrime AI Agent. These are NOT in Tally's official docs.

---

## 1. P&L TYPE=Data is unreliable for partial periods

**Discovery date:** 2026-03-15
**Diagnostic script:** `test_scripts/test_pnl_period.py` (15 tests against live Tally)
**Logs:** `test_scripts/logs/pnl_period_debug.log`

### The problem

Tally's XML API `TYPE=Data` for Profit & Loss returns **non-monotonic, unreliable values** when you query partial date ranges (e.g., Apr 1 → Jul 31).

Tested cumulative P&L for each month-end across a full FY. Expected monotonically increasing values for Sales. Got:

| Month-end | Expected (cumulative) | Actual from API |
|-----------|----------------------|-----------------|
| Apr 30 | 0 | ₹49,97,900 (full-year total!) |
| May 31 | 0 | 0 |
| Jun 30 | 0 | ₹49,97,900 |
| Jul 31 | ₹2,95,000 | ₹2,95,000 |
| Aug 31 | ₹5,95,000 | ₹5,95,000 |
| ... | ... | ... |
| Mar 31 | ₹49,97,900 | ₹49,97,900 ✓ |

Values jump between the full-year total and 0 for months with no transactions, instead of returning 0 consistently. This makes any subtraction-based approach (cumulative[month] − cumulative[month−1]) produce garbage.

### What works

- **Full-FY P&L** (Apr 1 → Mar 31): Always returns correct cumulative total. ✓
- **Voucher registers** (`get_sales_register`, `get_purchase_register`): Return transaction-level data with dates. Sum by month for accurate breakdowns. ✓
- **Day book** (`get_day_book`): Also transaction-level, works for expense trends. ✓

### What doesn't work

- **Partial-period P&L subtraction**: Garbage in → garbage out. The inputs are unreliable.
- **TYPE=Collection for P&L ledgers**: Revenue/expense ledgers are nominal accounts — ClosingBalance is always 0 by design (auto-closed to "Profit & Loss A/c").
- **Group objects via Collection**: CHILDOF on Group objects with Revenue/Expense filters returned empty results.

### Our fix

`profit_and_loss_period()` raises `TallyResponseError` for non-full-FY date ranges, with a message directing the agent to use voucher registers instead. Full-FY queries pass through unchanged.

---

## 2. SVFROMDATE is ignored for P&L reports

P&L reports always return cumulative data from FY start regardless of SVFROMDATE. Only SVTODATE is respected. This is why the "query each month separately" approach doesn't isolate monthly data — each call returns cumulative-to-date, not month-specific.

**Workaround:** Don't use P&L for period breakdowns. Use voucher registers.

---

## 3. `$$InDateRange` crashes Tally

The TDL function `$$InDateRange` is documented in some Tally resources but crashes TallyPrime 7.0 when used in Collection filters. Use `SVFROMDATE`/`SVTODATE` envelope parameters + Python-side filtering instead.

---

## 4. TYPE=Collection vs TYPE=Data

| Aspect | TYPE=Data | TYPE=Collection |
|--------|-----------|-----------------|
| Use case | Display reports (TB, P&L, BS) | Object queries (ledgers, vouchers) |
| XML structure | Sibling pairs (DSPACCNAME → DSPACCINFO) | Named fields (Name, Parent, ClosingBalance) |
| P&L reliability | Full-FY only | Always 0 for P&L ledgers (nominal accounts) |
| Voucher data | Summaries only | Full transaction detail with NATIVEMETHOD |

**Rule of thumb:** Use TYPE=Data only for aggregate report snapshots. Use TYPE=Collection for anything transactional.

---

## 5. XML field conventions

- Never use `*` in NATIVEMETHOD FETCH — crashes Tally. List specific fields.
- Sanitize `&#4;` control chars from XML responses before parsing.
- Dates always DD-MM-YYYY format.
- Indian FY: April 1 → March 31. Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar.

---

## 6. P&L parser is correct (BSMAINAMT/PLSUBAMT)

After extensive testing, the `parse_profit_and_loss()` parser in `response_parser.py` was confirmed correct. It uses `BSMAINAMT` with `PLSUBAMT` fallback — this matches Tally's actual XML structure. The earlier hypothesis about sign alternation bugs was disproved.

---

## 7. Voucher-based monthly P&L (ground truth)

For the test company (NUVANTA AI TECHNOLOGIES PRIVATE LIMITED, FY 2025-26):

```
Jul: ₹2,95,000   Aug: ₹3,00,000   Sep: ₹6,00,000   Oct: ₹5,75,000
Nov: ₹5,00,000   Dec: ₹8,82,500   Jan: ₹6,11,850   Feb: ₹12,33,550
Full year total: ₹49,97,900
```

These values from `get_sales_register` match Tally desktop exactly.

---

## 8. `<VOUCHERNUMBER>` only sticks under specific UI-set numbering modes

**Discovery date:** 2026-05-05
**Probe logs:** `docs/probe-voucher-numbering.log`, `docs/probe-tally-config.log`, `docs/probe-reference-alter.log`
**Canonical evidence:** `docs/tally-write-exploration-v4.md` § "ALTER and config-write findings"

### Truth table (live-verified)

| NumberingMethod | How set | VOUCHERNUMBER on Create | VOUCHERNUMBER on ALTER |
|---|---|---|---|
| `Automatic` (default) | either | ❌ ignored | ❌ ignored |
| `Automatic (Manual Override)` | UI-set | ✅ honored | ? (likely honored) |
| `Automatic (Manual Override)` | XML-set | ❌ no runtime effect | ❌ |
| `Manual` | UI-set | ✅ honored | ✅ honored |
| `Manual` | XML-set | ❌ no runtime effect | ❌ |

**`Default` value is READ-ONLY** — Tally's "as-shipped" state. Cannot be set as a write value (silently coerced to `None`).

### What this means

- For default Tally companies, never trust `<VOUCHERNUMBER>` on import — Tally auto-overwrites with its sequential counter.
- `<REFERENCE>` is the right field for user-supplied invoice/bill numbers (works under any mode, see §9).
- For voucher-number control on imports, the user must change the voucher-type config via Tally UI (not XML — see §11).

---

## 9. `<REFERENCE>` is the canonical user-supplied invoice number field

**Discovery date:** 2026-05-05
**Probe log:** `docs/probe-reference-alter.log`

- Survives Create AND ALTER under any numbering mode.
- Mutable: subsequent ALTER overwrites the previous value cleanly.
- Minimal ALTER envelope (verified — `altered=1`, readback confirms):

```xml
<VOUCHER DATE="01-Oct-2025" TAGNAME="Master ID" TAGVALUE="20"
         VCHTYPE="Sales" ACTION="Alter">
  <REFERENCE>S001</REFERENCE>
</VOUCHER>
```

- **Visibility caveat**: `<REFERENCE>` is hidden in Tally voucher screens by default. Per-voucher-type F12 toggle ("Show Reference" / equivalent) must be enabled by the user. Storage is unaffected — agent can write/read regardless.
- For "find voucher S012" use cases, query Tally on the REFERENCE field, not VOUCHERNUMBER.

---

## 10. `BILLALLOCATIONS` works on Create only — cannot be retro-fitted via ALTER

**Discovery date:** 2026-05-05
**Probe logs:** `docs/probe-bill-allocations.log` (Create), session inline probes (ALTER)

### What works

- Builder support landed for all 4 voucher types (sales, purchase, payment, receipt) in `import_builder.py`. Pass `bill_allocations=[{"name": ..., "type": "New Ref"|"Agst Ref"|"On Account", "amount": ..., "credit_period": ...}]`.
- `BILLALLOCATIONS.NAME` survives round-trip and is ALWAYS visible in Tally UI (Bills Outstanding, voucher's bill-wise breakdown). No F12 toggle needed.
- Receipts/Payments with `Agst Ref` correctly clear the bill from `bills_receivable`/`bills_payable`.
- Sign convention: `BILLALLOCATIONS.AMOUNT` mirrors the party LEDGERENTRY.AMOUNT.

### What doesn't work — both ALTER paths fail

| Approach | Tally response | Actual result |
|---|---|---|
| Partial ALTER (send only `<ALLLEDGERENTRIES.LIST>` + BILLALLOCATIONS) | `altered=1, errors=0` | Silently ignored. No bill created. |
| Full-body ALTER (resend full voucher with bill_allocations + Master ID handle) | `CREATED=1, ALTERED=0` | Tally **ignores Master ID** and creates a DUPLICATE voucher with the bill. |

### Operational rule

Bills can only be added at voucher Create time. Vouchers seeded without `BILLALLOCATIONS` cannot have bills retro-fitted — only path is delete + recreate.

---

## 11. Voucher-type config writes via XML are unreliable (DANGEROUS)

**Discovery date:** 2026-05-05
**Live evidence**: User UI-set Sales NumberingMethod = "Automatic (Manual Override)" → VOUCHERNUMBER on Create immediately worked. We then XML-altered Purchase NumberingMethod from `Default` → `Manual`. Tally returned `altered=1`, readback showed `Manual`, BUT Tally UI continued to show `Automatic` and runtime behavior didn't change.

### The pattern

`<VOUCHERTYPE ACTION="Alter">` writes go to a "stored but ignored" shadow store:
- Tally always returns `altered=1, errors=0`.
- XML readback reflects the new value.
- **UI/runtime behavior does NOT change.**

Plus: invalid enum values silently coerce to `None` (e.g. `Default` → `None`).

### Operational rule

**Future write-agent must INSTRUCT the user to make voucher-type config changes via Tally UI** (Gateway → Alter → Voucher Types → ...). Do NOT attempt XML writes for behavior-affecting voucher-type fields. They look successful but don't take effect, and invalid values can leave the config in a broken state.

Read-side queries on voucher-type config remain reliable — use `TYPE=Object SUBTYPE=VoucherType ID="<name>" FETCHLIST=*` to inspect before recommending UI changes.

---

## 12. `altered=1` is NOT proof of actual change

**Always readback after ALTER**, especially for:

- Enum fields (Tally silently coerces invalid values).
- Sub-list ALTERs (partial `<ALLLEDGERENTRIES.LIST>` with bill_allocations is silently ignored).
- Voucher-type config (writes appear successful but don't propagate to runtime).
- VOUCHERNUMBER under Automatic mode (silently overwritten).

`altered=1` from Tally means "the request was accepted as well-formed" — not "the field changed". Compare the post-write read against the intent before reporting success to the user.

---

## 13. Read-config pattern is universal and reliable

`TYPE=Object SUBTYPE=<X> ID=<Y> FETCHLIST=*` works for `VoucherType`, `Company`, `StockItem`, `Ledger`. Use this freely to inspect before any write.

Sample envelope:

```xml
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Object</TYPE><SUBTYPE>VoucherType</SUBTYPE>
    <ID TYPE="Name">Sales</ID>
  </HEADER>
  <BODY><DESC>
    <STATICVARIABLES><SVCURRENTCOMPANY>...</SVCURRENTCOMPANY></STATICVARIABLES>
    <FETCHLIST><FETCH>*</FETCH></FETCHLIST>
  </DESC></BODY>
</ENVELOPE>
```

Use this to inspect: NumberingMethod, GST flags, F11/F12 toggles (where exposed), opening balances, etc. Read-side is honest; only write-side has the divergences described above.

---

## 14. REFERENCE / REFERENCEDATE — external document number/date fields, toggle-gated writes

**Discovery date:** 2026-05-07
**Canonical evidence:** `docs/tally-write-exploration-v4.md` § "REFERENCEDATE — supplier invoice date (toggle-gated)"
**Open enumeration:** `docs/open-items-parked.md` — "Per-voucher-type external-doc-date toggles"

### Field meaning + canonical names

- `<REFERENCE>` — external document number (supplier invoice no on Purchase, customer PO/reference on Sales, cheque/instrument no on Receipt/Payment).
- `<REFERENCEDATE>` — external document date (supplier invoice date on Purchase, customer PO date on Sales, cheque date on Receipt/Payment).
- Both are **top-level voucher fields**, siblings of `<DATE>`, `<VOUCHERNUMBER>`, `<NARRATION>`. NOT nested inside `<LEDGERENTRIES.LIST>` or `<BILLALLOCATIONS.LIST>`.
- `<REFERENCEDATE>` format is **`YYYYMMDD`** (e.g. `20250925` for 25-Sep-2025). Same format as `<DATE>` in voucher import envelopes.

### UI labels by voucher type

| Voucher type | REFERENCE label | REFERENCEDATE label | Default visibility |
|---|---|---|---|
| Purchase | "Supplier Invoice No" | "Supplier Invoice Date" | Visible by default once toggle enabled |
| Sales | "Reference" / "Order No." | "Reference Date" / "Order Date" | Hidden — F12 toggle on voucher type |
| Receipt | (bill reference) | (cheque date for post-dated) | Parked — not yet enumerated |
| Payment | (bill reference) | (cheque date for post-dated) | Parked — not yet enumerated |

### The toggle gate (silent-overwrite-to-voucher-DATE failure mode)

**REFERENCE/REFERENCEDATE writes are gated by a per-voucher-type configuration toggle**: Voucher Type → Configuration → "Use supplier invoice date" (label varies by voucher type — likely "Use customer PO date" / "Use cheque date" on others; not yet enumerated).

When the toggle is **OFF** and you send `<REFERENCEDATE>20250925</REFERENCEDATE>` on Create or ALTER:
- Tally returns `created=1` / `altered=1, errors=0` — no rejection, no error.
- Tally **silently overwrites the requested value with the voucher's main `<DATE>` field**. So if `<DATE>20251015</DATE>`, readback shows `REFERENCEDATE = 15-Oct-2025`, not the 25-Sep-2025 you sent.
- Looks like a successful round-trip on cursory inspection (the field is non-empty and well-formed). This is the most insidious silent-failure mode encountered so far.

When the toggle is **ON**, the same XML envelope sticks correctly. Verified empirically (2026-05-07): identical envelope, both outcomes observed before vs after toggling.

**Operational rule:** `altered=1` is NOT proof of correctness here either (cf. §12). For REFERENCEDATE, only readback comparison against the *intended* value confirms — not just that the field has *some* value.

### Voucher-type config is Tally-installation-scoped, not company-scoped

Voucher-type configurations (including these toggles) persist on the **Tally installation**, shared across all companies on the same install. Implication for backup/restore:

- `.tbk` restore **into the same Tally install** → toggles preserved (because they were never company-scoped to begin with).
- `.tbk` restore **into a different Tally install** (fresh install, different machine) → toggles do NOT travel with the backup. The voucher data restores fine, but any subsequent write that depends on the toggle will hit the silent-overwrite mode until the toggle is set manually on the new install.

Critical for any deployment story: customer-side write-agent sessions need the toggles probed/configured before relying on REFERENCE/REFERENCEDATE writes.

### Status across voucher types

- **Purchase** — confirmed (this session). Toggle: "Use supplier invoice date".
- **Sales / Receipt / Payment** — *likely* to have analogous toggles (customer PO date; cheque date for post-dated cheques), since REFERENCE/REFERENCEDATE have natural meaning on those types too. **Not yet probed.** Parked for write-agent design phase.

---

## 15. Tally Write Safety — Operational Rules

Distilled rules for any code path (or agent) that writes to Tally. Most of these are also "why" — the corresponding "what" is documented in §6–§14 above and in [`docs/tally-write-exploration-v4.md`](docs/tally-write-exploration-v4.md).

1. **Read config before writing anything that depends on it.** Fetch the voucher type via `TYPE=Object SUBTYPE=VoucherType ID="<Name>" FETCHLIST=*` and inspect `NUMBERINGMETHOD`, `ALLOWALTERATION`, display toggles, and any per-type "Use … date" gate. Don't assume defaults.
2. **`altered=1` / `created=1` is NOT proof of effect.** It only means the request parsed. Always readback after every ALTER — especially enum fields, sub-list ALTERs, and REFERENCEDATE.
3. **Tally silently coerces invalid enum input to `None`.** No error, no warning. Readback is the only safety net.
4. **Voucher-type config changes must be done in the Tally UI** (Gateway → Alter → Voucher Types → …). XML writes for behavior-affecting voucher-type fields (e.g. `NUMBERINGMETHOD`) appear successful — readback even confirms — but the runtime behavior doesn't change. Instruct the user; don't try to do it via XML.
5. **BILLALLOCATIONS cannot be retro-fitted on existing vouchers.** Partial ALTER silently ignored; full-body ALTER with `TAGNAME="Master ID"` creates a duplicate voucher (`CREATED=1, ALTERED=0`). Bills must be added on the original Create.
6. **REFERENCEDATE silent-overwrite check:** Before writing supplier-invoice-date (or analogous external-doc-date), confirm the per-voucher-type "Use supplier invoice date" toggle is ON. If OFF, Tally substitutes the voucher's main `DATE` field — readback alone won't catch it (the value looks plausible). Detection: write a value that differs from voucher DATE, then readback; if readback equals voucher DATE, the toggle is OFF.
7. **Voucher-type config is Tally-installation-scoped, not company-scoped.** A `.tbk` restored into a different Tally install does not carry the toggles. Fresh installs need toggles set manually before any write that depends on them.
8. **Sales numbering default ("Automatic") silently overwrites `<VOUCHERNUMBER>`.** Only `Manual` and `Automatic (Manual Override)` honor the supplied number — and the toggle change must be made in the UI (per rule 4).
9. **Distinguish "data is stored correctly" from "user can see it in UI."** Some fields (e.g. REFERENCE) are stored under any numbering mode but hidden in the UI unless a per-voucher-type F12 display toggle is on. Surface the relevant toggle to the user when behavior is config-dependent. Concretely (live 2026-06-12): **Purchase** vouchers show REFERENCE as "Supplier Invoice No." on the entry screen; **Sales** vouchers hide it unless F12 → "Provide Reference No." is on (it's still written + visible in print/registers). The seeder also mirrors the invoice no into the **Narration** so it's always visible — a useful pattern.

### Master-creation & inventory write gotchas (live-discovered 2026-06-11/12)

10. **NEVER send a CREATE for a master that already exists.** Re-importing a `<UNIT>`/`<STOCKGROUP>`/`<STOCKITEM>` `ACTION="Create"` for an existing master makes Tally pop a **blocking modal** that freezes the HTTP gateway — reads keep working, the first such write **times out**, and every subsequent request hangs until the modal is dismissed (a full Tally restart may be needed). Mitigation: list existing masters first (`list_stock_items` → item names + their units/groups; `list_stock_groups` → empty groups too) and create only the genuinely-missing ones. This is the #1 cause of "Tally froze" during inventory writes.
11. **`created=0, altered=1, errors=0` on a master CREATE = "already exists" (benign).** When Tally *does* respond (vs modal-hanging per #10), treat altered-only as success, not failure. Don't abort the voucher on it.
12. **A GST-rated stock item REQUIRES an HSN.** Creating a `GSTAPPLICABLE=Applicable` stock item with GST rates but no HSN pops a mandatory "HSN/SAC" modal (freezes per #10). When no HSN is extracted, create the item `GSTAPPLICABLE=Not Applicable` (omit `GSTDETAILS.LIST`/HSN) — the voucher's explicit CGST/SGST/IGST ledger lines still post GST correctly, independent of the item master.
13. **"Primary" is a reserved stock group name** — creating a group literally named "Primary" can modal. Use a non-reserved default (e.g. "AI Imported Items") and create it on demand.
14. **Future-dated vouchers are silently dropped / not visible.** A voucher dated after Tally's working date (F2) or outside the open financial year won't post, or won't show in current-period reports. Before writing, set F2 ≥ the voucher date and ensure the period covers it — otherwise use an in-period date. (Same root cause as the seeder's F2 preflight.)

When advising the user, distinguish these failure modes and surface the relevant Tally config — the agent can't see the modal it triggers without the user reading it on screen.

### GST-invoice recompute & bill-wise reporting (live-discovered 2026-06-23)

15. **`ISINVOICE=Yes` + auto-GST makes Tally RE-COMPUTE the bill total, overriding your `BILLALLOCATIONS` amount.** When a Purchase/Sales posts with `<ISINVOICE>Yes</ISINVOICE>` + `Invoice Voucher View` + GST ledger legs (Duties & Taxes), Tally's GST engine re-derives the assessable value and the party/bill total on its side. Live-observed: we POSTed a ₹11,800 gross (₹10,000 + ₹900 CGST + ₹900 SGST) with a `New Ref` bill of ₹11,800 — the **POSTed XML read back correct at every layer** (party `<AMOUNT>` and `<BILLALLOCATIONS><AMOUNT>` both 11,800), yet Tally **stored the bill as ₹9,440** (it back-computed a different assessable; exact 8,000-base derivation inferred from the stock items' own configured GST rates). **Detection:** read back the *bill's pending amount* (`bills_payable`/`bills_receivable`), not just the POSTed XML or `created=1`. **Mitigation:** goods/inventory invoices must go through the **stock-grid writer** (posts qty/rate; Tally computes consistently — live-verified correct), NOT the ledger-invoice writer (`create_*_voucher_ledger`) — that path with explicit GST legs is for **services** (no stock items). Never assert `bill == gross` for an inventory invoice written via the ledger path.
16. **`bills_payable` / `bills_receivable` only return parties maintained BILL-BY-BILL.** A Sundry Creditor/Debtor with "Maintain balances bill-by-bill" OFF carries its balance **on-account** and never appears in these reports — even with a non-zero closing balance, and even after you post a `New Ref` bill against it (the allocation is silently dropped from the outstanding view). Live-observed: `Acme Computer Distributors Pvt Ltd` had closing ₹25,960 but ₹0 bill-wise pending, while `Bharat Paper Supplies` (bill-wise) showed ₹91,993.60. **Implication for tests/seeders/writers:** any read-back that asserts bill-wise outstanding (payable/receivable delta, "bill present") MUST target a party known to be bill-wise — select one that already appears in `bills_payable`/`_receivable`, don't pick an arbitrary creditor/debtor by group. (This is also why new party ledgers are created `is_billwise=True` — see the write-flow party-ledger path.)

### Read-side freeze: period variables on a master collection (live-discovered 2026-09-23; SVTODATE half corrected 2026-09-24)

17. **NEVER send `SVFROMDATE` on a master (Ledger) collection; `SVTODATE` is honoured, but only for a date inside the current period — never a date in an earlier one.** Same modal-freeze family as rule 10 for `SVFROMDATE`, but on the *read* path. Live 2026-09-23 (reproduced twice, freshly reset seed company; v2 S0 probe harness, logs in `v2/probes/results/logs/s0-ason-discriminator*-2026-09-23.log`), and corrected/extended live 2026-09-24 (S0 plan part 5, typed re-run, logs `logs/p16A-rerun-typed-2026-09-24.log`, `logs/p16B-live-2026-09-24.log`, `logs/p16B-debug-asof-2026-09-24.log`):
    - **`SVFROMDATE` freezes Tally's XML server.** The Ledger-collection read timed out at 45 s and an *unrelated* counters read then timed out too — the whole gateway sits blocked behind a modal until Tally is **restarted**. On a customer's machine that freezes the accountant's Tally, not just our request. This was found on the **untyped** form and has not been re-measured typed (risk > value — a wedge costs a Tally restart); the guard stays conservative either way.
    - **CORRECTED 2026-09-24: a `TYPE="Date"` `SVTODATE`, inside the current period, IS honoured — the 2026-09-23 "silently ignored" finding was measured untyped.** Typed and inside the current period, `SVTODATE` moves per-ledger `ClosingBalance` to the as-on value: live, 19 of 22 balance-sheet ledgers on company A moved to match the as-on lines. So a dated Ledger-collection read **is** a valid same-period as-on route once `TYPE="Date"` is set. The 2026-09-23 evidence (byte-identical to the control, 0 of 35 changed) is kept for context but was an **untyped** read, not a general SVTODATE limitation — see rule 21 (`TYPE="Date"` is mandatory generally) for why that alone explains it.
    - **NEW 2026-09-24, Ruling C45: a typed `SVTODATE` earlier than the current period's start silently clamps to the current period's first day.** Company B, typed `SVTODATE=31-03-2025` and `SVTODATE=31-03-2023` (both valid C43 days, both in *closed* prior periods) each answered with the **same** balances — the ones as of **01-04-2025** (the current period's start) — on all 10 balance-sheet ledgers checked. No error, a healthy fast response, plausible-looking numbers: exactly the "confirms the wrong answer" failure shape rule 17 already warns about, now for a different trigger (period boundary, not typing). **Rule: a dated Ledger-collection read is a valid as-on anchor only for a date inside the *current* period; for any earlier date, use a dated `TYPE=Data` report instead (rule 20) — never a master collection.**
    - **As-on figures for a past period come from `TYPE=Data` reports instead.** Same builder, same DD-MM-YYYY dates: a Trial Balance for the full FY and an as-on Trial Balance both answered instantly and genuinely differed (and did so correctly for both a current-FY date, per rule 20, and a past-FY date, per the 2026-09-24 B run below), and a *Voucher* collection with both variables was fine too. So this is neither a date-format bug nor a general period-variable bug — it is specific to master collections, and further specific to a date outside the current period.
    - **Scope caveat:** proven on the **Ledger** collection only, in TallyPrime 7.0 **Educational** under **Wine**. Our build-time guard (`v2/probes/reads.py` `master_request`) still refuses **both** variables unconditionally on *any* master collection by default — it doesn't itself know "current period" from "past period"; that judgment lives in probe 16's own logic (send SVTODATE only to measure it, never to *rely* on it for an anchor). Whether a licensed Tally on real Windows behaves the same, for either the in-period or the clamp case, is an open tier-C check.

### Read-side arithmetic: what a Tally integrator must know before summing anything (live-discovered 2026-09-23)

These three cost a full probe cycle of wrong conclusions. All were found on the seed company (company A, FY 2025-26, 50 vouchers) with the v2 S0 probe harness. **Scope caveat for all three:** TallyPrime 7.0 Edit Log, **Educational**, under **Wine 11.0**, one company — a licensed Windows Tally is an open tier-C check.

18. **An inventory voucher exports its nominal ledger TWICE — summing both double-counts by exactly 2×.** For Sales and Purchase vouchers the nominal leg (`Sales - Electronics`, `Purchase - Office Supplies`, …) appears in **both** `ALLLEDGERENTRIES.LIST` and each inventory entry's `ACCOUNTINGALLOCATIONS.LIST`. They are the *same* posting rendered twice, not two postings. Live: summing both left **24 of 50** vouchers (all 16 Sales + all 8 Purchase) failing a double-entry check and put Sales/Purchase at exactly 2× Tally's own as-on Trial Balance (−₹23,14,000 vs Tally's −₹11,57,000). **Rule: count `ALLLEDGERENTRIES.LIST` alone** — it is complete (party + nominal + GST legs) and gives 0 of 50 unbalanced, matching the TB to the paisa. Use `ACCOUNTINGALLOCATIONS.LIST` only when you need the per-stock-item split, never additively. (`v2/probes/reads.py` `PROBE_POSTING_RULE`.)

19. **The Trial Balance's stock-bearing group carries a synthetic `Opening Stock` row that NO ledger holds.** Under Current Assets the TB prints an `Opening Stock` row (₹18,55,800 for company A) that appears **zero** times in any Ledger collection. So a TB group row will *never* equal the rollup of its ledgers until you add it: live, Current Assets ₹26,05,093 = ledger rollup ₹7,49,293 + ₹18,55,800, exactly. Two traps: (a) it is a **static opening** figure — identical at different as-on dates — and it is **not** the Stock Summary *closing* total, which was −₹9,89,462.31 the same day and is a different quantity entirely; comparing against the Stock Summary makes a genuine reconciliation look like a mismatch. (b) Read it from the **same** TB response (`EXPLODEFLAG=Yes` exposes it and leaves the group rows byte-identical) so the two figures cannot drift apart. Related: a TB's rows do **not** net to zero — this row is part of why (see `docs/specs/2026-09-21-bi-part1-sync-design.md` for company A's full ₹33,05,800 explanation).

20. **A `TYPE=Data` Trial Balance honours its as-on date — this half stands, and now also confirmed on a past-FY date.** The TB as-on a past month-end really is history — every primary group reconciles to the vouchers up to that date, once rules 18 and 19 are applied. Live-confirmed twice: company A as-on 31-10-2025 (2026-09-23, then re-confirmed 2026-09-24 with the whole FY read explicitly per rule 21) and company B as-on **31-03-2023** — a date in a *closed prior FY* — where all 5 primary groups matched the dataset exactly, the stock-bearing group reconciling via its own `Opening Stock` row (rule 19's pattern, company B). **Rule: a dated `TYPE=Data` TB is a valid historical/parity anchor for any date, current period or earlier.**
    **CORRECTED 2026-09-24 (S0 plan part 5): the "Bills Receivable, Bills Payable and Stock Summary IGNORE the as-on date" half below was a Ruling C43 artefact, not a genuine Tally limitation, and is superseded.** The 2026-09-23 evidence used **30-09-2025** as the as-on date for Bills/Stock — a day whose date is not 1, 2 or 31, which the Educational licence silently ignores (rule 22) and falls back to the current period's end. That fallback is exactly what looked like "the report ignored the date." Re-measured live 2026-09-24 at **31-10-2025** (a C43-valid day): Bills Receivable, Bills Payable and Stock Summary all **matched that as-on date exactly**. <details><summary>2026-09-23 finding (superseded, kept for context)</summary>The other three return the books' **current** position while happily accepting the dates you pass: bills dated *after* the as-on date still came back, both sides' totals equalled the period-end (31-Mar) anchors rather than the as-on ones, and all 14 stock quantities equalled opening + the **full** year's movements, with the as-on group values byte-identical to the FY-end capture.</details> **Rule (corrected): a dated Bills/Stock Summary read as-on a C43-valid day (1st, 2nd or 31st under Educational; any day on a licensed Tally) IS a valid historical snapshot — treat it the same as the TB.** Only an Educational-mode read on a non-1/2/31 day needs the vouchers-based workaround (or the date-clamp of rule 22). (Also settled by the same read: `ISDEEMEDPOSITIVE=Yes` is inward for inventory entries — that rule reproduces Tally's own FY-end quantities exactly.) Logs: `logs/p18A-rerun-typed-2026-09-24.log`, `logs/p18B-live-2026-09-24.log`; spec `docs/specs/2026-09-22-bi-s0-probes-design.md` §7 probe 18, "Changed 2026-09-24 (plan part 5, live run)".

20b. **Master `OpeningBalance` fields are the CURRENT period's opening, not the books-start opening — for `StockItem` (`OpeningBalance`/`OpeningRate`/`OpeningValue`, Ruling C46) AND for `Ledger.OpeningBalance` (corrected 2026-09-25).** Stock: all 5 of company B's items exported the stock as at the current FY's start (01-04-2025), not 01-04-2022 (the books start). Ledgers: **14 of 25** ledgers' `OpeningBalance` differ from the books-start value and all 14 equal the **current FY's opening** (probe 11 re-run 2026-09-25, `results.json` `probes.11.parts.B.observations.ledgers.as_current_fy`); probe 16 B independently says the same (`opening_scope.verdict = "fy"`, 14 as FY, 0 as books). The debtor's opening **bill** on the ledger master (`BillAllocations` on the Ledger collection, `Op/2022-001` ₹62,500 dated 31-03-2022) *does* keep its books-start identity. **Rule: never treat any master's `OpeningBalance` as a books-start anchor.** A books-start opening (ledger or stock) comes from a historical report as-on the books-start date (the TB / Stock Summary route of rule 20). **Scope caveat:** TallyPrime 7.0 Edit Log, Educational, under Wine 11.0, company B (books start 01-04-2022, current FY 2025-26) — a licensed Windows Tally is an open tier-C check. Fixtures `p11_B_ledger_openings.xml`, `p11_B_stock_openings.xml` (2026-09-24 bytes kept in `v2/tests/fixtures/sync/c46_p11_live_2026-09-24/`); logs `logs/p11-live-2026-09-24.log`, `logs/p11-rerun-c46-2026-09-25.log`; spec §7 probe 11, "Changed 2026-09-25 (plan part 6)".
    ~~Superseded 2026-09-25 — original 2026-09-24 text, kept for the record (its ledger sentence was a misreading: the stock FAILED was checked first and returned early, so the summary never showed the ledger half):~~ **`StockItem.OpeningBalance`/`OpeningRate`/`OpeningValue` are the CURRENT period's opening, not the books-start opening (new finding, Ruling C46, live-discovered 2026-09-24, S0 plan part 5, probe 11, company B).** All 5 stock items' exported "opening" figures equalled the dataset's stock **at 31-03-2025** (this FY's start) — not 01-04-2022, the company's actual books-start date. Ledger `OpeningBalance` and the debtor's opening bill (via `BillAllocations` on the Ledger collection) did **not** have this problem — they correctly held the books-start values; this is a **StockItem-specific** failure mode, distinct from rule 17's Ledger-collection finding (though the same underlying idea — a master's "opening" field silently tracks the current period, not history — may generalise; not yet tested on other master types). **Rule: never treat `StockItem.OpeningBalance` as a books-start anchor.** A books-start stock opening (qty/rate/value) must come from a historical report as-on the books-start date (the TB / Stock Summary route of rule 20), not the StockItem master. **Scope caveat:** TallyPrime 7.0 Edit Log, Educational, under Wine 11.0, company B (books start 01-04-2022, current FY start 31-03-2025/01-04-2025) — a licensed Windows Tally, and whether the same clamp-to-current-period pattern holds for Ledger `OpeningBalance` too, are open tier-C checks. Fixtures: `p11_B_stock_openings.xml`; log `logs/p11-live-2026-09-24.log`; spec §7 probe 11, "Changed 2026-09-24 (plan part 5, live run)".

### Date static variables: `TYPE="Date"` is mandatory, and Educational mode clamps them too (live-discovered 2026-09-24, S0 plan part 4, probe 5, company B)

21. **`SVFROMDATE`/`SVTODATE` on a Voucher collection are silently ignored unless typed `TYPE="Date"` (Ruling C33).** Format is irrelevant once typed (`01-04-2022`, `20220401`, `1-Apr-2022` all work); **untyped, Tally silently answers for the company's *current* period** instead of erroring or returning nothing — so an untyped dated read looks healthy (200 OK, plausible-looking data) and is simply wrong. This bit the company-B loader's own verification reads before it was known (see §12 above is unrelated; this is the general rule prior findings 17/20 didn't cover because company A's data happened to sit in the current period). **Rule: always send period static variables with `TYPE="Date"`.**
22. **Under the Educational licence, a *typed* date static variable whose day is not 1, 2 or 31 is ALSO silently ignored (Ruling C43) — same rule already known for voucher dates, now confirmed for static variables too.** A typed `SVTODATE` of `30-06-2023` (or any other non-1/2/31 day) is dropped and Tally falls back to the *current period's* end, exactly like the untyped case in rule 21 — even though `TYPE="Date"` is present and correct. Only days 1, 2 or 31 are honoured. Live: a typed request for 01-06-2023..30-06-2023 returned 680 vouchers spanning 2023-06-01..2026-03-31 instead of June's 20; the same request with `SVTODATE=02-06-2023` (or `31-…` for a 31-day month) returned exactly the expected window. **Rule for any Educational-mode date window: clamp the to-date down to the nearest allowed day (31st, else the 2nd) before sending it**, and treat the request as bounding *at most* that clamped day, not the originally-intended end of month/period. **Scope caveat:** TallyPrime 7.0 Edit Log, **Educational**, under **Wine 11.0** — a licensed Windows Tally is an open tier-C check (same caveat pattern as rules 17–20). Logs: `logs/p05-live-2026-09-24.log` (failure), `logs/p05-debug-todate-2026-09-24.log` (diagnosis), `logs/p05-live-2026-09-24-run2.log` (confirmed after the clamp); spec `docs/specs/2026-09-22-bi-s0-probes-design.md` §4.6.
    **Added 2026-09-24 (S0 plan part 5): the v2 probe harness now refuses an off-day date at send time instead of clamping it silently.** `ProbeContext` (`v2/probes/safety.py`) BLOCKs — nothing sent — any typed date static variable whose day is not 1, 2 or 31, under the Educational licence, before the request goes out; this is one implementation shared by `send`/`try_send`, `_post_uncaptured`, the read-only `anchors` command, and `company_b_view.check_date_vars` (spec §4.5). This is a **harness-side** discipline for probes, not a change to how Tally itself behaves — the extractor's own production clamp-and-continue behaviour (the rule above) is unaffected and remains the right approach for a real sync agent that must keep working through an off day.

### Flags, due dates, duplicate names, secured companies (live 2026-09-24/25, S0 plan part 6, companies B and C)

*Scope caveat for rules 23–27: TallyPrime 7.0 Edit Log, **Educational**, under **Wine 11.0** on macOS — a licensed Windows Tally is an open tier-C check.*

23. **Cancelled and optional vouchers ARE listed by a Voucher collection, with their flags — and a cancelled one has no lines.** Probe 5's typed month request (the extractor's request) returned cancelled vouchers 201/202 with `ISCANCELLED=Yes` and optional 301/302 with `ISOPTIONAL=Yes`, on that read and on a header-only books-wide read; the other 954 of 958 vouchers carried no flag. A **cancelled** voucher exports **zero ledger lines and an empty `PARTYLEDGERNAME`**; an optional one keeps its full lines and party. Tally itself leaves both out of balances (C42). **Rule: ingest flagged vouchers, keep them out of every balance/parity sum by `IsCancelled`/`IsOptional`, and never infer the party of a cancelled voucher from its lines (there are none).** Evidence: probe 3 B, `p03_B_*` fixtures, `logs/p03B-live-2026-09-25.log`.
24. **Bills Receivable's due-date column = bill date + the voucher's credit period, and the credit period itself exports on the bill.** Each New Ref bill allocation carries `BILLCREDITPERIOD` ("30 Days"/"45 Days", 12/12 in June 2023), and Bills Receivable as-on 31-03-2026 gave `BILLDUE` = bill date + credit period in calendar days for 202/202 bills. **Rule: either source can drive an overdue split; they agree.** Evidence: probe 23 B, `p23_B_bills_credit_period.xml`, `p23_B_bills_receivable_due.xml`, `logs/p23B-live-2026-09-25.log`.
25. **TallyPrime refuses a duplicate ledger name within a company, even under a different group (R9).** Creating `Delhi Metal Traders` under Sundry Debtors when it already exists under National Creditors was refused **at the Name field**, before Under/Accept: "Oops! Name already exists. Enter a different name." Read-back: one ledger, parent unchanged. **Rule: a ledger name is unique per company (across groups) — but still key ledgers by GUID (renames).** Measured only in the UI; never attempt it over XML (rule 10 — a duplicate create can raise a blocking modal). Evidence: probe 25 B, `p25_B_ledgers_after_duplicate_attempt.xml`, `logs/p25B-r9-2026-09-25.log`, screenshot `logs/p25B-r9-2026-09-25-tally-refusal.png`.
26. **Security and TallyVault don't change the XML export once the company is open — and a pending login/vault prompt reads as "no company open", not an error.** With security on (after logging in) and with TallyVault on (after entering the vault password), the company list, active GUID, counters, ledgers and vouchers were identical to the unsecured baseline (same GUID). While the login prompt or the TallyVault prompt was open, the company-list and active-company reads were **answered** (no timeout, no error envelope) with an **empty** company list and no active-company rows. **Rule: no credentials ever go into the agent; the gate treats "empty list" as "company not open" and retries.** UI observations (this edition): security is **K: Company → Security** (a separate "Security And User Access" screen: Control User Access, Username (Administrator), Password, Confirm, E-mail ID for Browser Access, Enable Tally Audit features, **Disallow opening in Educational Mode** — must stay **No** on this Educational install — then Add Users after saving); TallyVault is **K: Company → TallyVault**, which warns "This will close all open Reports…" then asks "Do you want to create a copy of <company> (<number>)?" — **No** encrypts in place (same folder); a vaulted company's name shows as **asterisks** in the Select Company list (period blank). **Once:** reopening right after vaulting hung at "Loading Company 73%" with the XML port unresponsive for ~17 min at high CPU; a Tally restart cleared it and later reopens loaded in seconds (Wine, one occurrence — if a vaulted reopen stalls, restart Tally rather than wait). Evidence: probe 24, `p24_C_*` fixtures, `logs/p24-live-2026-09-25.log`, `logs/p24-tally-restart-2026-09-25.log`, screenshots `logs/p24-*.png`.
27. **Driving the TallyPrime UI from macOS under Wine (operator/harness lessons, S0 plan part 6).** (a) Under Wine's mac driver, Alt-menu shortcuts arrive as **Cmd**: Cmd+K opens K: Company; Option+K does nothing. (b) `osascript` System Events **mouse clicks don't reach** the Wine window, but **keystrokes do** — the UI can be driven by System Events keystrokes to the `wine` process, read back with `screencapture`, and a probe's stdin fed through a FIFO. (c) **Guard every keystroke with a frontmost-process (pid) check, and never pause mid-sequence.** Incident 2026-09-25: the user switched to Chrome during a 6 s pause between two fields, and throwaway credentials were typed into Chrome. (d) Tally's screen can **lag ~5 s behind keystrokes** — take another screenshot before re-pressing anything, or you double-apply. (e) `TallyControl.list_tally()` matches any process whose command line contains `tally.exe`, so **don't run the harness from a shell command that itself contains that string** (it will see itself as a Tally process).
28. **Forex over XML: currency masters are UI-only, and a forex line is written with the rate WITHOUT a base-currency symbol.** (a) **Never create a Currency over XML.** `<CURRENCY ACTION="Create">` for `$` answered EXCEPTIONS=1 — and Tally still kept `$` as an import-**exception** master, after which the UI refused to create `$` (screenshot `logs/p7-ui-currency-exists-as-exception-2026-09-25.png`); only a restore cleared it. Create it in the UI (Create → Currency: symbol `$`, Formal name USD, 2 decimals), then list it before any forex write and stop if it's missing. TallyPrime 7 needed **no** company feature switch (F11) for multi-currency. (b) **The line AMOUNT form that stores:** `-$448.44 @ 82.99/$ = -37216.04` (face, rate, INR base — rate and base with **no** base symbol). The same text with the base symbol (`@ ?82.99/$ = -?37216.04`) was refused (EXCEPTIONS=1, no LINEERROR). It stores on a party ledger with `CURRENCYNAME` `$` and also on a plain INR party. (c) **The ₹ base currency exports as a literal `?`** (byte 0x3F; its NAME is `?`, MailingName/ExpandedSymbol `INR`; the UI shows ₹) — not a transport bug (Hindi round-trips). Read the base currency's NAME from the Currency list; never hard-code `₹`. (d) **How it exports:** Tally rewrites the line in its own layout, `-$448.44 @ ? 82.99/$ = -? 37216.04` (base prefix `?` + a space, since the base's HasSpace is Yes) whatever was sent; the INR base is **stated** after `=`; no extra leaf field; `ISDEEMEDPOSITIVE` as for INR. `parse_decimal` raises on it — parse the expression (face, rate, base) and take the base. (e) **Don't alter a ledger's currency once it has vouchers** — give the forex sales their own party ledger (the INR `Gulf Office Supplies LLC` had 87 INR vouchers; a currency change could re-cast them; not measured, avoided by design). **Scope caveat:** TallyPrime 7.0 Edit Log, **Educational**, under **Wine 11.0**, company B — a licensed Windows Tally is an open tier-C check. Evidence: `v2/tests/fixtures/sync/forex_shape_2026-09-25_run1_currency_refused/`, `…_run2/` (`271ff01`), `logs/p7-forex-discovery-2026-09-25.log`, `logs/p7-forex-shape-live-2026-09-25*.log`, screenshots `logs/p7-ui-*.png`; probe 22 (`p22_B_*`, `logs/p22-live-2026-09-25.log`).
29. **A forex ledger's balance is valued at the LATEST voucher rate, not the sum of its vouchers' INR bases (Ruling C47) — and it exports as an expression.** After two USD sales ($448.44 @ 82.99 = ₹37,216.04 and $1,161.27 @ 82.58 = ₹95,897.68, bases ₹1,33,113.72), the USD party's Opening/ClosingBalance read `-$1609.71 @ ? 82.58/$ = -? 132929.85`: face total × the **latest-dated** voucher's rate. The TB rows and group totals use that figure too, while the sales ledger keeps the bases — so the **TB Dr/Cr total is out by the unrealised forex difference** (₹183.87; 0.00 before the forex load). **Rules:** (a) never rebuild a forex ledger's INR balance by summing voucher bases — read it from Tally, or model the latest-rate revaluation; (b) a TB that doesn't net to zero by exactly that difference is correct, not drift; (c) a forex ledger's `ClosingBalance`/`OpeningBalance` are expression strings — a plain amount parser (the agent's `parse_ledger_list`) raises `AmountParseError`; take the stated base after `=`. **Scope caveat:** TallyPrime 7.0 Edit Log, Educational, Wine 11.0; only one revaluation point measured (both vouchers in Sep 2022) — whether a later-dated rate revalues earlier as-on dates is unmeasured. Evidence: `logs/setup-b-forex-live-2026-09-25.log` (the mismatch), `…-rerun.log` (expected 183.87 = observed), probe 22 `usd_ledger` observation, probe 18 B re-run (`logs/p18B-rerun-forex-2026-09-25.log`). **Note 2026-09-25 (S1 spec, from the committed capture `v2/tests/fixtures/sync/p18_B_tb_asof_2023-03-31.xml`):** the TB also carries a synthetic top-level row **`Unadjusted Forex Gain/Loss`** (−₹183.87, debit) that no ledger holds — Indirect Expenses (−₹55,333.11) equals its only ledger `Bank Charges`, so the row sits outside it — and **with that row included the TB nets to 0.00**. So (b) above is true only when that row is left out: treat it like rule 19's `Opening Stock` row (synthetic, read from the same response), and use it as Tally's own statement of the total unrealised difference (`docs/specs/2026-09-25-bi-s1-cloud-design.md` §10.5).
30. **A ledger that ever carried a voucher can't be deleted — even after that voucher is deleted — and verify a voucher delete inside the voucher's own date window.** (a) The throwaway `ZZ Forex Probe USD Debtor` got "Cannot be deleted!" after its only voucher was deleted and read back gone; company B had to be restored. **Rule: never create-then-delete a ledger on a real or baseline company as part of a probe** — take a backup first, and plan to restore rather than delete. (b) A delete's "is it gone?" read must use the voucher's **own** day: a helper that reads a fixed window (e.g. company A's FY) always says "gone" for a voucher dated outside it, so a failed delete passes silently (plan part 7 fact 3; `delete_b_voucher` reads the voucher's day). Under Educational, that day must also be 1, 2 or 31 (rule 22). **Scope caveat:** TallyPrime 7.0 Edit Log, Educational, Wine 11.0. Evidence: `v2/tests/fixtures/sync/forex_shape_2026-09-25_run2/summary.json` notes, `logs/p7-restore-B-after-shape-2026-09-25.log`.
31. **A ledger-level Trial Balance "as on" one date is that day's CLOSING balance — it already includes the vouchers dated that day — and a forex ledger's row in it is a plain INR number.** (a) A `TYPE=Data` Trial Balance with `ISLEDGERWISE=Yes` and `SVFROMDATE = SVTODATE = D` (both typed, rule 21) exports **closing columns only** (`DSPCLDRAMT` / `DSPCLCRAMT`, no opening or period columns): the balance at the **end** of D, D's own vouchers included. So it is **not** an opening balance for D. **Rule: a books-start opening anchor = the ledgerwise TB as on `books_from` MINUS the day's own lines (our vouchers dated `books_from`)**; a ledger with no TB row but with day-one lines anchors at minus those lines. Never read a date before `books_from` to get it, and never use a master's `OpeningBalance` (current-FY opening, C46). Live (G6, company B, 2026-09-29): as on 01-04-2022, 17 rows, and TB minus the 01-04-2022 lines equalled the loader's ledger openings for **every** ledger; company A's G1 (as on 01-04-2025, captured 2026-09-25) has the same shape. (b) **G2:** a forex (USD) ledger's row in a ledger-level TB is a **plain INR figure** (`-132929.85`) — already revalued at the latest rate (rule 29) — never the `-$… @ ? …/$ = -? …` expression the Ledger collection gives (rule 28d); parse it as a plain amount. A ledger with no balance on D has no row at all (Tally omits zero rows). **Scope caveat:** TallyPrime 7.0 Edit Log, **Educational**, under **Wine 11.0** (D = 1 April, an allowed day under rule 22) — a licensed Windows Tally is an open tier-C check. Evidence: `v2/tests/fixtures/sync/s1_B_tb_ledger_asof_2022-04-01.xml` (+ `.json` sidecar, commit `3620693`), `s1_A_tb_ledger_asof_2025-04-01.xml`, `s1_B_tb_ledger_asof_2023-03-31.xml`; tests `v2/tests/cloud/unit/test_realdata_parity_b.py::test_g6_books_start_anchor_reconciles_with_the_dataset_openings`, `::test_company_b_forex_tb_row_form_matches_g2`; S1 spec §10.2 / §13.3 (`docs/specs/2026-09-25-bi-s1-cloud-design.md`).
### C33/C43 — date static variables (2026-09-24)

Read-path rules (live-verified on TallyPrime 7 Educational, company *Sharma & Sons' Probe Traders*, books FY 2022-23..2025-26; evidence `docs/backend-date-vars-audit-2026-09-24.md` + `logs/backend-date-audit-2026-09-24.log`; the same findings, measured by the v2 probe harness, are rules 21–22 above).

- **C33 — type every date variable: `<SVFROMDATE TYPE="Date">dd-mm-yyyy</SVFROMDATE>` / `<SVTODATE TYPE="Date">…`.** On a Voucher **collection** export (day book, sales/purchase/payment/receipt registers, ledger vouchers, party vouchers) Tally honours the window **only when typed**; untyped it silently answers for the company's **current period** (F2 / Alt+F2), whatever you asked. The Python safety net (`_filter_vouchers_by_date`) then turned that into an **empty** success — "last year's sales" = "no sales". Report exports (`TYPE=Data`: TB, P&L, BS, bills, stock, cash flow) honour both forms byte-identically; we type them too for one convention. Fixed in `request_builder._date_vars()` (all dated builders). The seed company masked it (its data = its current period); the mock Tally now models C33 (`mock_handler._collection_window`: untyped → current period) and `tests/integration/test_mock_c33_date_vars.py` goes red on regression.
- **Consequence: typed windows are binding — no hard-coded FY defaults.** `get_party_vouchers` (write-flow dedup + DN/CN "against invoice") had a hard-coded 01-04-2025..31-03-2026 default that worked only because untyped = current period; typed, it would have stopped seeing every invoice after 31-03-2026. Now `party_voucher_window(doc_date)`: start of the FY *before* min(doc date, today) → end of the FY of max(doc date, today).
- **C43 — Educational TallyPrime ignores any date variable whose day is not 1, 2 or 31** (typed or not, reports and collections) and falls back to the **current period's end** (a from-date the same way). E.g. "TB 01-04-2023..30-06-2023" = TB to 31-03-2026; "BS as on 30-06-2023" = BS as on 31-03-2026 — silently wrong, no safety net for reports. Collections survive via the Python filter but pull the whole tail of the books (multi-MB). **Production does not clamp** (licensed Tally behaviour is still an open check, and the backend has no edition/licence notion to gate a warning on); when demoing/evaluating on the Educational box, ask with windows ending on the 1st/2nd/31st, and treat "numbers equal to current-period-end" as a C43 symptom.
- **Escape the company name** in every envelope (`request_builder._company_var`): an unescaped `&` (e.g. `Sharma & Sons' …`) makes Tally answer `Unknown Request, cannot be processed` for every read.

---

## 16. Observability & tooling gotchas (non-Tally)

### `opentelemetry-instrumentation-anthropic` streaming tool-use `KeyError: 'input'`
Version **0.53.0** crashes (caught by its own `@dont_throw`, so it surfaces only as a DEBUG log) inside `_process_response_item` when a streaming response with tools emits an `input_json_delta` before the event dict has an `input` key:
```
complete_response["events"][index]["input"] += item.delta.partial_json   # KeyError
```
Effect: streaming generations that use tools (here: `AnalysisAgent`, the only streaming+tools call) are dropped from Langfuse traces and spam DEBUG noise. Fixed in **0.61.0**, which guards it: `event["input"] = event.get("input", "") + item.delta.partial_json` plus a `tool_use` type check and an `index < len(events)` bounds check. **Rule:** keep `opentelemetry-instrumentation-anthropic >= 0.61.0`. Verify the guard by reading the *installed file* (`<venv>/.../opentelemetry/instrumentation/anthropic/streaming.py`) — `inspect.getsource` on the function returns the `@dont_throw` wrapper, not the real body.

### `python-multipart` must be a declared dependency
FastAPI's `File`/`Form`/`UploadFile` (used by `/api/chat/upload`) require `python-multipart` at import time. It was historically only manually `pip install`ed, never in the lock — so `uv sync` silently removes it and the upload route breaks. It is now a declared core dep in `pyproject.toml`. Don't rely on manually-installed packages; declare them.

### Playwright route mocks must account for query strings
`page.route("**/api/health", ...)` does **not** match `/api/health?host=localhost&port=9000` — Playwright globs don't span the query string. The per-workspace heartbeat badge sends host/port as query params, so the mock never fulfilled and the badge stuck on "Checking…". Use `**/api/health**` (trailing `**`) for any endpoint the frontend calls with query params. Also: for async header badges, add a settle wait (`await expect(badge).not.toContainText("Checking")`) before `toHaveScreenshot` to keep screenshots deterministic.

## 17. DB-mode persistence: every chat-producing endpoint must persist `Message` rows

In DB mode the conversation is rehydrated **solely** from `Message` rows
(`getConversation` → `GET /workspaces/.../conversations/...`). Anything an endpoint
returns to the frontend but does **not** write as a `Message` row is lost on refresh/relogin.

- **`/chat/upload` must persist messages.** It originally wrote only an `UploadedFile`
  audit row, so uploaded `voucher_review` cards vanished on reload. Fix: persist a user
  `Message` (`"<msg> [filename]"`) + assistant `Message` (`data=result["data"]`), set
  `conversation.title`/`updated_at`, and `flush()` the user message before the assistant
  one so the per-row `created_at` default preserves order. (`_chat_db_mode` already does
  all this — mirror it in any new chat-producing endpoint.)
- **Persist *state changes* too, and thread `conversation_id` via the REQUEST.**
  `/chat/voucher-action` must update the originating `voucher_review` Message entry's
  `status` (→ `"written"`/`"deleted"`) so a written/discarded card doesn't reload as
  actionable `"draft"`. In-place JSONB mutation needs
  `sqlalchemy.orm.attributes.flag_modified(msg, "data")` or SQLAlchemy won't detect it.
  **Gotcha:** the production review-card `entry` dict has **no `conversation_id`** (the
  orchestrator never adds it, the frontend passes `entry` through unmodified) — so any
  `entry.get("conversation_id")` gate silently no-ops in production (this had already
  been silently skipping the `VoucherEntry` audit row). Pass `conversation_id` as an
  explicit request field and use `request.conversation_id or entry.get(...)`.
- **Test at the real shape.** A test that injects `conversation_id` *into the entry dict*
  passes while production no-ops. Pass it via the request body, and add a revert
  sanity-check (remove the threading → test must fail). See
  [`docs/code-review-upload-voucher-persistence-2026-06-12.md`](docs/code-review-upload-voucher-persistence-2026-06-12.md).
