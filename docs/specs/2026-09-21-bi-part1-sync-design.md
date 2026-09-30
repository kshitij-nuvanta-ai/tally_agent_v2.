# Part 1 — Syncing: single-company Windows sync agent → our DB

> **Brainstorm design, not yet implemented.** Docs only; no code changes have been made for this.
> **Implementation status:** tracked in [`plans/2026-09-22-bi-part1-tracker.md`](../plans/2026-09-22-bi-part1-tracker.md), not in this file.
> Split from `2026-09-15-bi-sync-agent-design.md` on 2026-09-21. The original is kept unchanged, and
> its header holds the full change history up to the split.
>
> This part covers getting Tally data into our DB and proving it is correct. The other two parts:
> - **Part 2** (`2026-09-21-bi-part2-ai-db-queries-design.md`): AI chat answers from that data.
> - **Part 3** (`2026-09-21-bi-part3-ui-dashboard-api-design.md`): the UI and the report/dashboard API.
>
> IDs are the same as in the original (decision N, Rn, Qn, probe n). Shared background (context,
> out of scope, R20, Q5 / Q6 / Q10 / Q28) lives here; Parts 2 and 3 link to it.
>
> New in this part, not in the original: §7 "Test environments (team on macOS)", the S0
> environment check (§12), the tier labels in §14, and Q29.
>
> **Added 2026-09-21 after a coverage review:**
> - Heartbeat and snapshot endpoints, the ingest rules, the stored `sync_state` values and a minimum
>   column list (§5 "Cloud").
> - "Sync now", one active device per workspace, where history starts (split and young companies),
>   master deletions and the full first-sync sequence (§4).
> - Agent install, sign-out, outbox purge, proxy and uninstall behaviour (§5 "Windows agent").
> - Post-dated vouchers (§6, probe 16); probes 24–25; DPDP Act (R20, Q6); Q30–Q32.
>
> **Changed 2026-09-22:** §7 now runs live Tally on the Mac under Wine, as `README.md` already
> documents, instead of in a Windows VM. Windows-only checks (installer, service, tray, updater,
> proxy, sleep/resume) and the timing probes move to tier C. Probe 0 and §14 updated to match.
>
> **S0 live results 2026-09-22 (probes 0–2, TallyPrime 7.0 Educational under Wine):** change detection by company
> counters holds (AltVchId moves on every voucher create/alter/delete, AltMstId on every master change; a voucher entry
> does not move the ledger's AlterID — so the mirrored-balance re-read in §4 is needed). `LastVoucherDate` is not
> rolled back after a delete — don't use it as "latest voucher". The S2 gate can read the company GUID with a filtered
> Company collection (~1.7 KB). An XML alter with an empty value is silently ignored (ALTERED=1, nothing changes).
> Details: `docs/bi-s0-probe-results-2026-09-23.md` (the current full snapshot of every probe run so far;
> the `2026-09-22` file is the earlier, superseded snapshot).
> **Settled 2026-09-23 (was provisional):** company A's live group-level TB export has rows with both debit and credit
> columns filled (Current Assets, Current Liabilities) — that is normal, and `debit + credit` is Tally's own signed net
> (negative = Debit, positive = Credit). Its rows net to **₹33,05,800 instead of 0 for two separate reasons, neither of
> them a Tally defect**: (a) ₹18,55,800 of it is the TB's synthetic **`Opening Stock`** row, a real opening position no
> ledger carries; (b) the remaining ₹7,00,000 is a **seed-data defect in our own current write path** —
> `backend/tally_bridge/import_builder.py:680` calls `abs(float(opening_balance))` and so discards the debit sign the
> seeder sets (`HDFC -500000`, `SBI -200000`; the seed file documents "negative = debit"), landing both bank openings
> as **credits**. Hence Σ rows = 750,000 (Capital) + 500,000 (HDFC) + 200,000 (SBI) + 1,855,800 (Opening Stock).
> **That defect is NOT fixed on this branch** (deliberately out of scope): fixing it means re-seeding company A and
> replacing the committed backup `seed_data/TDBK1800_100003.001`, so **company A is not a clean fixture for anything
> that assumes a balanced opening position**. §6's "the TB snapshot itself balances (Σ debit + Σ credit ≈ 0)" assertion
> therefore still can't be used as-is against company A; it needs the `Opening Stock` row allowed for, and a company
> whose openings were entered correctly.
>
> **Changed 2026-09-23 (S0 live, probe 16's as-on question — settled):** per-ledger as-on balances **cannot** be read
> from the Ledger collection. `SVFROMDATE` on a master collection **freezes Tally's XML server** behind a modal until
> Tally is restarted (45 s timeout, and an unrelated read then times out too); `SVTODATE` is **silently ignored** —
> a healthy 200, byte-identical to the control, with **0 of 35** closing balances changed, i.e. today's balances.
> The report route is unaffected (a full-FY and an as-on Trial Balance both answered instantly and genuinely
> differed), and a *Voucher* collection with both variables is fine, so this is neither a date-format nor a general
> period-variable problem. Consequences: (a) **decision 11** — rung 1's per-ledger opening anchor during the backfill
> depends on **probe 17**'s exploded ledger-level TB; the probe-16 route is *impossible*, not merely unattractive, so
> probes 17 and 18 carry it (they are not endangered); (b) **Part 2's "as of last sync"** — `LEDGER.ClosingBalance`
> can only ever mean **now**, which confirms the mirrored-balance re-read in §4. *Scope caveat:* proven on the
> **Ledger** collection only, in TallyPrime 7.0 **Educational** under **Wine**; the code guard
> (`v2/probes/reads.py` `master_request`) is deliberately conservative and refuses both variables on any master
> collection, but the finding claims only what was tested — a licensed Tally on real Windows is an open tier-C check.
> Rule in [`LESSONS.md`](../../LESSONS.md) §15 rule 17; logs in `v2/probes/results/logs/s0-ason-discriminator*-2026-09-23.log`.

> **Changed 2026-09-23 (S0 live, probes 16–18 re-read — three expectations were wrong, and all three reverse in our
> favour):**
> 1. **Posting rule = `ALLLEDGERENTRIES.LIST` only.** Company A's 24 inventory vouchers (16 Sales + 8 Purchase) export
>    the nominal ledger **twice** — once in `ALLLEDGERENTRIES.LIST` and again in each inventory entry's
>    `ACCOUNTINGALLOCATIONS.LIST`. Summing both double-counts Sales/Purchase by exactly **2×** and makes all 24 look
>    unbalanced. `ALLLEDGERENTRIES.LIST` alone is complete (party + nominal + GST): 0 of 50 unbalanced, and it
>    reproduces Tally's own as-on TB rows to the paisa. Ingest and parity must count lines this way
>    (`v2/probes/reads.py` `PROBE_POSTING_RULE`).
> 2. **The TB's stock-bearing group carries a synthetic `Opening Stock` row.** ₹18,55,800 for company A — exactly
>    Σ(stock item opening values), static across as-on dates, and it appears in **no** Ledger collection. Current
>    Assets then reconciles exactly: ledger rollup ₹7,49,293 + ₹18,55,800 = the TB row ₹26,05,093. It is **not** the
>    Stock Summary *closing* total (−₹9,89,462.31 the same day), which is a different quantity and must not be used
>    for this comparison. Rung 2 adds this row to the stock-bearing group before comparing.
> 3. **`ISLEDGERWISE=Yes` IS a working ledger-level Trial Balance** (decision 11, R3 — see below), and **a TB as-on a
>    past date IS correct history** (R30 — see below), while **Bills Receivable/Payable and Stock Summary IGNORE the
>    as-on date** (§4 month-end snapshots, Part 3 tiles — see below).
>
> The earlier probe-16 SVTODATE finding is unchanged and still true: per-ledger as-on balances remain unobtainable
> from the **Ledger collection**. *Scope caveat (all three):* TallyPrime 7.0 Edit Log, **Educational**, under
> **Wine 11.0**, one company; a licensed Windows Tally is an open tier-C check. Rules in
> [`LESSONS.md`](../../LESSONS.md) §15 rules 18–20.

> **Changed 2026-09-22 (code isolation):** Part 1 is built as **v2**, under `v2/` at the repo root, and
> **changes no current code** (§5 "Code isolation (v2)"). v2 copies what it needs from current code instead
> of importing it. The cloud side is a separate FastAPI app with its own Alembic chain on the same Postgres.
> What this spec calls `workspace.config.*` lives in a v2-owned table. Decision 2, §5, §6 "Storage", §7,
> §9, §10, R13, §12, §13 and §14 updated to match. Moving v2 into the current code is a later, separate step.
>
> **Changed 2026-09-24 (S0 live, probes 5 + 21, plan part 4):** §5 "Windows agent" `extractor.py` — month chunks
> use **probe 5's confirmed `voucher_month` request with typed period variables** (`SVFROMDATE`/`SVTODATE
> TYPE="Date"`, C33): sent untyped, Tally silently answers for the company's *current* period instead of erroring,
> so an untyped chunk looks healthy and is wrong. **Day-split finding:** the same typed form also bounds a one-day
> window exactly, so the ~5k-voucher auto-split (§5) can safely fall back to day chunks. In §15, the **Q22** and
> **Q23** rows get `Numbers (probe 21, 2026-09-24): …` — company B's measured mix (Wine, Educational): 1050.3 B/voucher
> without `raw`, `raw` JSON adds 6518.5 B/voucher (86.1% of the total); at 200k vouchers/yr the full 10-year span is
> 15137.5 MB with `raw` vs 2100.5 MB without, vs 4707.9 MB keeping `raw` for the recent 2 FYs only (see storage table,
> `docs/bi-s0-probe-results-2026-09-24.md` probe 21). **These are numbers, not a decision — the Q22/Q23 decisions
> stay open for the user.** In **R27** "Residual", add: probe 21 confirms full-history reach (3+ FYs back, decision
> 7b) and gives the first real bytes-per-voucher figures above; the residual per-customer storage ceiling is still
> not designed for v1. **Open note (not a conclusion):** probe 5's `report_period_vars` evidence (one Trial Balance,
> typed vs untyped `SVFROMDATE`/`SVTODATE` as-on, byte-identical under Educational) is recorded against the open
> question of whether an as-on **report** is silently wrong when untyped the way the voucher collection is (C33) —
> probe 18's B part settles this, not probe 5.
>
> **Changed 2026-09-24 (S0 live, plan part 5 — probes 16/17/18 A re-run typed; 11/14/15 built and run; 18 B built and
> run):** this **corrects** the 2026-09-23 "probe 16's as-on question — settled" block above — that finding was
> measured **untyped** — and **overturns** the 2026-09-23 "Bills Receivable/Payable and Stock Summary IGNORE the
> as-on date" half of the probes 16–18 re-read block above, which was a Ruling C43 artefact (an ignored 30-09-2025
> date), not a genuine limitation.
> 1. **Decision 11 / §6 "Rung 1" / §16 — the per-ledger opening anchor route.** A **typed** `SVTODATE` on a Ledger
>    collection, for a date **inside the current period**, IS honoured (probe 16 A: 19 of 22 balance-sheet ledgers
>    moved to the as-on lines) — the 2026-09-23 "impossible" conclusion is corrected for that scope. For a date
>    **before** the current period, a typed `SVTODATE` on a Ledger collection silently **clamps to the current
>    period's start** instead (**new finding, Ruling C45**; probe 16 B: 31-03-2025 and 31-03-2023 both answered with
>    the 01-04-2025 balances, 10/10 ledgers) — never a valid anchor there. **Net route, unchanged in substance:**
>    rung 1's per-ledger opening anchor **during the backfill still comes from probe 17**'s exploded ledger-level TB
>    (or, per point 3 below, from a dated TB read the way rung 2/probe 18 does it) — the current-period Ledger-as-on
>    route is a same-period bonus, not a backfill-anchor substitute, since the backfill's anchors are by definition
>    for periods before "now." Probe 17 re-run typed: **CONFIRMED unchanged** (`isledgerwise`, 29 ledger rows).
> 2. **R30 / §6 "The opening anchor" — the TB half stands and is now proven on a past-FY date too.** Company B's TB
>    as-on **31-03-2023** (a closed prior FY's end) matched the dataset for all 5 primary groups, stock-bearing via
>    its `Opening Stock` row — the same pattern already proven for company A's within-FY 31-10-2025. **R30 needs no
>    suspension on this ground, on either company.**
> 3. **R30 / §4 month-end snapshots / Part 3 tiles — Bills/Stock Summary as-on a date DO work; the 2026-09-23
>    "ignore the date" finding is superseded.** It used **30-09-2025**, a day Educational TallyPrime silently ignores
>    (Ruling C43, not 1/2/31), falling back to the FY-end position — which is exactly what looked like "the date was
>    ignored." Re-measured at the C43-valid **31-10-2025**: Bills Receivable, Bills Payable and Stock Summary all
>    **matched that as-on date exactly**. **Bills/Stock Summary as-on a valid day ARE a historical snapshot, same as
>    the TB** — the §4 month-end snapshot design and the Part 3 "vs last month-end" tiles can use a dated report
>    directly on a valid day; only an Educational-mode non-1/2/31 date needs the vouchers-based workaround.
> 4. **R5 — probe 11, openings.** ~~Ledger `OpeningBalance` and the debtor's opening bill **CONFIRMED** against
>    setup's books-start values.~~ *(Superseded 2026-09-25 for the ledger half: ledger `OpeningBalance` is the
>    current-FY opening too — see "Changed 2026-09-25 (plan part 6)" below. The opening bill stands.)* Stock `OpeningBalance`/`OpeningRate`/`OpeningValue` **FAILED** — **new finding,
>    Ruling C46**: `StockItem.OpeningBalance` is the **current period's** opening, not books-start (all 5 items'
>    "opening" equalled the dataset's stock at 31-03-2025, not 01-04-2022). **Input for the sync design:** a
>    books-start stock opening must come from a historical report (point 3 above), never the StockItem master.
> 5. **R13 — probe 14, company names.** CONFIRMED: the escaped v2 envelope reaches `Sharma & Sons' Probe Traders`
>    (26 ledgers); an unescaped copy and an unknown-company control both fail/answer as expected; R13 holds.
> 6. **R14 / R15 — probe 15, Hindi/Unicode + compound units.** CONFIRMED: the Hindi ledger and narration round-trip
>    exact (UTF-8 bytes, judged on parsed code-point equality); the compound-unit item reads its quantity by the
>    leading number ("10 Box 0 Nos" on the voucher, "20 Box" in Stock Summary) — S1 stores text as UTF-8 unchanged
>    and parses quantities by their leading number.
>
> **Two inputs for the sync/extractor design (not yet a design decision — recorded here so S1/S2 don't re-derive
> them wrong):**
> - **As-on history for a date before the current period must come from a report (TB / Bills / Stock Summary),
>   never a Ledger/StockItem collection.** A Ledger-collection SVTODATE only works inside the current period (point
>   1); a StockItem's OpeningBalance is always current-period-relative (point 4, C46), never books-start.
> - **`OpeningBalance` on a Ledger or StockItem master is current-period-relative, not books-start.** The extractor
>   must not treat either master's "opening" field as a books-start anchor; it is the account's opening for
>   whichever period Tally currently has open, full stop.
>
> *Scope caveat (all of the above):* TallyPrime 7.0 Edit Log, **Educational**, under **Wine 11.0** — a licensed
> Windows Tally is an open tier-C check, same pattern as the 2026-09-23 blocks above. Rules in
> [`LESSONS.md`](../../LESSONS.md) §15 rules 17, 20, 20b (C46), 22. Spec
> `docs/specs/2026-09-22-bi-s0-probes-design.md` "Changed 2026-09-24 (plan part 5, live run)"; tracker
> `docs/plans/2026-09-22-bi-part1-tracker.md` rows 11/14/15/16/17/18 and decision 11; results
> `docs/bi-s0-probe-results-2026-09-24.md`.

> **Changed 2026-09-25 (S0 live, plan part 6 — probes 3 B, 23 B, 25 B + R9, 11 re-run under C46, 24 on company C):**
> - **R16 (probe 3 B, CONFIRMED):** the extractor's month request returns cancelled and optional vouchers **with**
>   their `IsCancelled`/`IsOptional` flags; a cancelled voucher exports **no ledger lines and an empty
>   `PartyLedgerName`**; no other voucher carries a flag. S1 ingests flagged vouchers and keeps both kinds out of
>   balances by the flags, as Tally does (C42).
> - **Part 3 overdue split (probe 23 B, CONFIRMED):** two agreeing sources — each New Ref bill's `BILLCREDITPERIOD` on
>   the voucher, and Bills Receivable's due-date column (`BILLDUE` = bill date + credit period, 202/202). S1 stores the
>   credit period on the bill row and reads due dates from the Bills snapshot. The overdue split stays in v1.
> - **R9 (probe 25 B, CONFIRMED):** TallyPrime **refuses** a duplicate ledger name within a company even under a
>   different group ("Oops! Name already exists. Enter a different name.", at the Name field). Ledger names are
>   therefore unique per company, across groups: S1 may treat a ledger name as unique within a company, but still
>   keys ledgers by GUID (renames cascade by GUID, probe 8). The "fixtures must include duplicate names under different
>   parents" note in R9 applies across companies or across a rename, not within one company. Custom voucher types
>   resolve to their base type only by the Parent walk (25 B DIFFERENT, same rule as 25 A).
> - **R2 / R26 + onboarding + gate shape (probe 24, CONFIRMED):** once the person has logged in / entered the
>   TallyVault password in TallyPrime, the XML export of a secured or vaulted company is unchanged (same GUID, same
>   data) — **no credentials in the agent**; onboarding says "open the company in TallyPrime as usual". While a login
>   or TallyVault prompt is open, Tally **answers** with an empty company list and no active company — as if no
>   company were open — so S2's gate treats it as "company not open" (skip quietly, retry next cycle). TallyVault
>   encrypts in place (same company folder/number, same GUID); the vaulted name shows as asterisks in Select Company.
> - **R5 (probe 11 re-run under C46, DIFFERENT) — corrects point 4 of the 2026-09-24 block above:** **both** ledger
>   and stock master openings are **current-period** (current-FY) openings — ledger `OpeningBalance` for 14/25 ledgers
>   equals the current FY's opening (probe 16 B agrees, `opening_scope = fy`); stock 5/5 as at 01-04-2025. The
>   debtor's opening bill on the ledger master is CONFIRMED. **Books-start anchors come from the TB / Stock Summary
>   as-on the books start** (probe 18's route), never a master's `OpeningBalance`. (The "two inputs" list above
>   already said this for both masters; point 4's ledger half was the misreading.)
>
> *Scope caveat:* TallyPrime 7.0 Edit Log, **Educational**, under **Wine 11.0** — licensed Windows Tally is an open
> tier-C check. Rules in [`LESSONS.md`](../../LESSONS.md) §15 rules 20b (corrected), 23–26. Spec
> `docs/specs/2026-09-22-bi-s0-probes-design.md` "Changed 2026-09-25 (plan part 6)"; tracker rows 3/11/23/24/25.

> **Changed 2026-09-25 (S0 live, plan part 7 — probe 22 forex, CONFIRMED; new Ruling C47):**
> - **Decision 15 answered — CONFIRMED (the INR base is in the export).** A forex voucher line exports its AMOUNT as
>   one expression holding face value, rate **and** INR base: `-$448.44 @ ? 82.99/$ = -? 37216.04` (the base currency
>   ₹ exports as a literal `?`, followed by a space; the UI shows ₹). The extractor takes the **stated base** (the part
>   after `=`) into every amount column and keeps currency, face value and rate in `raw`; **no conversion at read
>   time**, and no face × rate computation. Both lines of each voucher balance to 0.00 in INR; no extra forex leaf
>   field and no extra ledger line appear. The plain `amounts.parse_decimal` raises on this text, so the extractor
>   needs a forex-aware amount parser (`v2/probes/reads.py` `parse_forex_amount` is the probe-side model).
> - **R5 impact (Ruling C47):** TallyPrime values a forex ledger's balance — Opening/Closing, TB rows, group totals —
>   at its face total × the rate of its **latest-dated** forex voucher (−$1,609.71 × 82.58 = −₹1,32,929.85), while
>   the sales ledger keeps the vouchers' INR bases (₹1,33,113.72). So a forex party's INR balance **cannot be rebuilt
>   by summing voucher bases**, and the TB is out by the unrealised forex difference (₹183.87). S1 reads that
>   ledger's balance from Tally rather than recomputing it, and parity (rungs 1 and 2) must expect the difference on
>   forex ledgers and their groups instead of flagging it.
> - **Decision 11 parity — expression balances.** The forex party's `ClosingBalance` and `OpeningBalance` also export
>   as the expression (`-$1609.71 @ ? 82.58/$ = -? 132929.85`), not a number. The agent's `parse_ledger_list` raises
>   `AmountParseError` on it today. **Open for S1:** parse the stated base the same way as voucher lines, or take
>   that ledger's balance from the ledger-level TB (probe 17's route). Probes 16 B and 11 must learn this before any
>   re-run.
> - Write side (S0 only, not the agent): currency masters are UI-only; multi-currency needs no feature switch in
>   TallyPrime 7.
>
> *Scope caveat:* TallyPrime 7.0 Edit Log, **Educational**, under **Wine 11.0** — licensed Windows Tally is an open
> tier-C check. Rules in [`LESSONS.md`](../../LESSONS.md) §15 rules 28–30. Spec
> `docs/specs/2026-09-22-bi-s0-probes-design.md` "Changed 2026-09-25 (plan part 7)"; tracker row 22 and decision 15.

> **Changed 2026-09-25 (S0 exit gate — item 3 and the audit's stale-text exceptions,
> `docs/bi-s0-exit-gate-2026-09-25.md`):** the dated blocks above already carried these results; this round writes
> them into the **body** so no body line contradicts a header.
> - **§6 "Rung 1" — probe 16's measured rule written in** (S0 spec §7 probe 16 outcome rule): `ClosingBalance` is as
>   of the **current period's end** (31-03-2026 on company A), not Tally's current date; post-dated vouchers **are
>   counted** and export `IsPostDated=Yes` (an ordinary future-dated voucher is counted too, exporting `No`); a typed
>   `SVTODATE` is honoured **inside** the current period; before it, it clamps to the current period's start (C45).
>   The old "Probe 16 settles both" is struck.
> - **§6 "The opening anchor"** — the "as-on per-ledger balances are unobtainable" line and the "books-start
>   `OpeningBalance` (probe 16)" line (and "Probe 16's route is impossible" under "Per-ledger anchors") are struck: master `OpeningBalance` is the **current-FY** opening (probes 11 and
>   16 B), so the books-start anchor comes from the TB / Stock Summary as-on the books start (probe 18's route).
> - **§6 Rungs 1–2 — C47 added:** a forex ledger is valued at its latest voucher rate, so parity expects the
>   unrealised difference; expression-form balances need a parser (open S1 decision).
> - **§11 R30 Handling** — the probe-16 per-ledger route is struck. **§12 probe 11** — the ledger-opening "CONFIRMED"
>   is struck (current-period). **§15 Q22/Q23** — probe 21's 2026-09-25 re-run noted (every figure moved by <0.5%;
>   the decisions hold). **§16 probe 16** — outcome noted.

> **Changed 2026-09-25 (S1 spec written — `docs/specs/2026-09-25-bi-s1-cloud-design.md`, decisions taken on the
> user's standing instruction, listed there for review):** the S1 spec settles Q1, Q4, Q5, Q6, Q10, Q19, Q20, Q21, Q25,
> Q28, Q30, Q32 (§15 rows here are unchanged; the answers live in the S1 spec §2.1 and the tracker §2). It refines
> this spec in these places — the S1 spec wins where they differ:
> - **§6 Rungs 1–2, C47:** a forex ledger gets verdict `match_revalued`; the unrealised difference is proven by a face
>   check on the mirrored balance and, on TB rows, by the TB's own synthetic **`Unadjusted Forex Gain/Loss`** row
>   (found in `p18_B_tb_asof_2023-03-31.xml`: with it the TB nets to 0.00). Expression-form balances: stated base
>   parsed, face/currency/rate kept as columns + `raw`, ledger-level TB as fallback.
> - **§6 Rung 2 "the snapshot itself balances"** becomes a baseline check keyed on `AltMstId` (company A's opening
>   difference and C47 both make a correct TB net to non-zero without the synthetic rows).
> - **§6 opening anchor date convention:** ledger-level TB as-on `E − 1`; at `books_from`, the TB as-on `books_from`
>   minus our own lines of that day. `Profit & Loss A/c` is `not_applicable` in rungs 1–2.
> - **§5 Cloud:** binding attaches to an existing workspace only (no insert into `workspaces`); `ledger_balance`
>   re-reads and snapshots use verbatim Tally text on the wire; new `trial_balance_ledgerwise` snapshot type; the
>   snapshot key keeps `(workspace_id, report_type, as_on_date)` with `from_date` = FY start by convention; cursors
>   advance on run completion; a `full_resync` run needs a user-confirmed command (decision 12, server-enforced);
>   new `caught_up_at` beside `last_synced_at`.
>
> **Changed 2026-09-29 (S1 built — `v2/cloud/`; as-built edits in the S1 spec's "Changed 2026-09-29" block).** No
> Part 1 decision is reversed; the build refines these places (the S1 spec wins where they differ):
> - **§5 Cloud "upsert … only when `alter_id >=`"** — suspended inside a `full_resync` (always user-confirmed,
>   decision 12), per object within the run's scope: company scope → every master and voucher; FY scope → only
>   vouchers dated in that FY. After a restore/relink Tally is authoritative even at lower AlterIDs (S1 §8.7). Ingest
>   still never deletes; post-backup objects leave via `/reconcile`.
> - **§6 "The opening anchor" — the books-start date convention this spec left to S1 is now a measured fact:** a
>   ledger-level TB as-on `books_from` (`SVFROMDATE = SVTODATE`) exports **closing** columns only, i.e. it includes
>   that day's vouchers, so the books-start anchor = that TB **minus our own lines dated `books_from`** (G6, company B,
>   2026-09-29; equals the loader's openings for every ledger; LESSONS §15 rule 31). A forex ledger's ledger-level TB
>   row is a plain INR number (G2).
> - **§6 Cause classifier, "Many ledgers differ by a constant → opening balances wrong"** — made concrete as S1
>   `anchor_wrong`: ≥ 3 anchored balance-sheet ledgers either share one identical diff (shifted anchor) or each differ
>   by ± their own anchor row (dropped / double-counted); action = re-capture the E−1 ledgerwise TB + refetch masters
>   (not "refetch openings": master openings are never an anchor, C46). "Two ledgers equal-and-opposite" is widened to
>   any set of ≥ 2 mismatches netting to 0 (a dropped GST sale moves 4 ledgers).
> - **§6 anchors are reused** — a stored anchor / month-end TB with rows resolving to no live ledger, captured at
>   another master counter than the cursor (a later rename, or a restore), is re-captured (`anchor_stale`) rather than
>   read as a mismatch.

> **Changed 2026-09-30 (v2 merged into the main code — [`2026-09-30-v2-merge-design.md`](2026-09-30-v2-merge-design.md)):**
> §5 "Code isolation (v2)" and §10's "Merging v2 into the current code is a later step with its own spec" are
> **superseded** by that spec; the body below is left as written and still says `v2/`. What changed (branch
> `feat/merge-v2-into-backend`, commits `13d6bbd`, `7d5d64e`, `12c36ba`, `23c6401`):
> - **Paths.** `v2/cloud/` → `backend/sync/` (sync, ingest, parity, clock, errors), `backend/db/sync_models/` (the 21
>   tables), `backend/api/{agent_auth,devices,sync,workspace_sync,sync_dependencies}.py`,
>   `backend/utils/{device_tokens,rate_limit}.py`. `v2/contract/` → `contract/`, `v2/agent/` → `agent/` (placeholder),
>   `v2/probes/` → `probes/`, `v2/tests/` → `tests/{sync,tally_bridge,contract,probes}/`, fixtures →
>   `tests/fixtures/sync/`. `backend/tally_bridge/` plus the agent's copied read code → one top-level `tally_bridge/`
>   (decision 2's "copy, never import" no longer applies; no `# Copied from:` header is left).
> - **One app.** The sync and agent routes are served by `backend/main.py` on port 8000. The separate FastAPI app,
>   port 8100 and `GET /api/v2/health` are gone. One settings class (`backend/config.py`): `DEVICE_TOKEN_SECRET` etc.;
>   the `V2_*` names are still accepted.
> - **One Alembic chain.** Revision `006` creates the 21 sync tables, or adopts them where the old chain
>   (`alembic_version_v2` at `v2_001`) already made them, and drops `alembic_version_v2`. The tables keep their
>   names, columns and constraints, and sit on the app's one `Base` with real foreign keys to `users` / `workspaces`.
> - **`sync_workspaces` stays a table.** §5's "Folding it into `workspace.config` is part of the later merge" is
>   dropped (merge spec M6).
> - **The isolation rule is replaced by a layer rule** (`tests/test_layers.py`, merge spec M12).
> - **Not part of the merge:** legacy (non-DB) mode still exists; chat still reads Tally live through `tally_bridge`
>   (the synced tables are not read by chat); the agent and installer are not built. Target direction (senior dev,
>   2026-09-30): installer → agent → tally bridge, with the cloud app talking only to its own Postgres.

## 1. Context
New product direction:
- **BI layer.** The owner sees their business (sales, profit, who owes money, top customers, stock) and asks the AI chat. Everything is answered from **our DB**.
- **Read-only.** This layer never writes to Tally.
- **Windows sync agent.** A small program on the customer's Tally PC copies Tally data into our cloud Postgres.
- The existing write flow (upload → Vision → voucher card → Write to Tally) stays untouched.
- This realizes roadmap **Set C** (`docs/roadmap.md`, never started). Sync-only replaces the old real-time tunnel idea.
- **Built as v2, beside the current code.** Everything in this part lives under `v2/` and no current file is edited (§5 "Code isolation (v2)").

## 2. Decisions this part implements
Decisions 1, 10 (Part 3) and 8, 13, 15 (Part 2) live in the other parts. The INR-only rule of decision 15 also shapes the schema here (§5 "Cloud").

| # | Decision |
|---|---|
| 2 | Agent in **Python**, reusing `backend/tally_bridge/` read code **by copying it into `v2/`**, never by importing it (§5 "Code isolation (v2)"). Ships as a PyInstaller `--onedir` build + signed installer, runs as a Windows service + tray app (status, last sync, "Sync now"), with silent auto-update. Minimum **Windows 10**. |
| 3 | **Single company, picked once at setup.** The user logs in inside the agent with website email/password, picks **one** company, and we save its **GUID** on the workspace. One company = one workspace. |
| 4 | **Single company open in Tally.** v1 assumes only our company is open. We fetch only when the active company's GUID matches the saved one. Multi-company, several loaded companies, and loaded-but-not-active are **out of scope**. |
| 5 | **Fetch only while connected.** When Tally is reachable and our company is open, fetch. Otherwise **skip quietly** (no error, no alert) and retry next cycle. |
| 6 | Sync masters (groups, ledgers, stock items), all vouchers with lines, and Tally report snapshots (TB, BS, P&L full-FY, Stock Summary, Bills Receivable/Payable) — **current ones on every refresh, plus a routine month-end set** (§4 "Month-end snapshots"). Eventually for **all** financial years (decision 7b), starting with the most recent two. |
| 7 | First sync copies the **current FY + previous FY** — that is what unlocks chat. A **new FY is added automatically** when the date crosses 1 April. |
| 7b | **After the first sync, a background backfill walks history backwards to the company's books-start date** (§4 "Background history backfill"). It is the lowest-priority loading work (§4 "Work priority"), always pre-empted by incremental sync, and never blocks chat. Dates it hasn't reached yet report "still loading", never a partial total. |
| 9 | Change detection uses Tally's change counters (AltVchId/AltMstId + AlterID), plus a deletion check (daily for the 2-FY window, round-robin for older years — §4 "After a gap"). Must be proven on live Tally first (S0). |
| 11 | **Parity scope v1 = rungs 0+1+2** (§6): double-entry invariant on ingest, ledger-level vs mirrored `LEDGER.ClosingBalance`, group-level vs TB snapshot. Rung 3 (P&L / BS / stock / bills totals) is deferred. **Rung 1 needs a per-ledger opening anchor**: it always runs once history is complete, but *during the backfill* only if probe 17 provides per-ledger openings (§6 "The opening anchor") — probe 16's as-on route is **settled impossible** (live 2026-09-23: `SVFROMDATE` freezes Tally on a master collection, `SVTODATE` is silently ignored; see the header). **Probe 17 provides them: `ISLEDGERWISE=Yes` on the `TYPE=Data` Trial Balance returns a ledger-level TB** (live 2026-09-23, company A: 29 ledger rows, ~7.3 KB, ~18 ms, every primary group reconciling exactly), so **rung 1 runs at ledger level mid-backfill** and rung 2 gains ledger resolution. Two caveats carried by that route: the response contains one **non-ledger** row (the synthetic `Opening Stock`), and `EXPLODEFLAG`/`EXPLODEALLLEVELS` are **not** a substitute — they stop at the second group level, so ledgers under a custom sub-group (company A's `National Creditors` / `Local Creditors`) never appear. Since the backfill can take weeks or never finish (R29), **probes 16 and 17 must be settled before S1**. **Changed 2026-09-24 (plan part 5):** probe 16's as-on route is corrected, not "settled impossible" — a typed SVTODATE on a Ledger collection **is** honoured **inside the current period** (probe 16 A), but silently clamps to the current period's start for an earlier date (**Ruling C45**, probe 16 B: FAILED) — so it remains unusable as a *backfill* anchor (those dates are, by definition, before "now"); rung 1's per-ledger opening anchor during the backfill still comes from **probe 17** (re-run typed, CONFIRMED unchanged) or from a dated TB per probe 18 B (also CONFIRMED, 31-03-2023). See the header's dated Changed line. |
| 12 | **No full resync is ever automatic — whatever triggers it.** When targeted remediation fails twice (§6), or counters go backwards after a backup restore (R8), or the user confirms a re-link (§4 "Company identity changes"), we *offer* it and wait for the user. A full resync is hours of Tally load (R3, R22); running it unannounced while the accountant works is how we get uninstalled. |
| 14 | **The internal ops signal for integrity alerts carries counts and causes only** — no ledger names, party names, or amounts tied to identifiable accounts (respects Q6 / R20). |

## 3. Depends on / provides
**Depends on:** nothing. This is the first part.

**Provides** three contracts. Each is defined once, here, and Parts 2 and 3 link to it rather than
restating it.

| Contract | Consumed by | What it covers | Defined in |
|---|---|---|---|
| **Data** | Parts 2, 3 | Tables and minimum columns, incl. group `nature` (balance-sheet vs P&L) and voucher `base_type` — both **derived by S1, not read from Tally** (probe 25); lines resolved to master GUIDs; voucher flags `is_deleted` / `is_cancelled` / `is_optional` / `is_post_dated`; `Numeric(18,2)` INR base amounts; coverage states and the two watermark edges; snapshots in `tally_report_snapshots`; mirrored-balance freshness; what `last_synced_at` means | §4 "Every cycle", §4 "Background history backfill", §4 "What a full resync does…", §4 "Month-end snapshots", §5 "Cloud" |
| **Status** | Part 3 | `GET /api/workspaces/{id}/sync-status`: the stored `sync_state` values (incl. `restore_detected`), backfill `{oldest_available_fy, oldest_complete_fy, books_from, percent, state}`, `last_parity`, `last_seen_at` and the Tally status from the heartbeat, a pending re-link prompt; `GET/DELETE /api/devices` | §5 "Cloud", §4 "Company identity changes" |
| **Integrity** | Parts 2, 3 | Parity states and their visibility rules; per-ledger / per-group verdicts in `parity_lines`; `workspace.config.last_parity` | §6 "Storage", §6 "Surfacing rules" |

## 4. Sync behaviour

### Setup (once)
1. Install the agent (needs an administrator once, §5 "Windows agent", Q31) → log in → the agent lists companies from Tally (v2 copy of `build_company_list`, `request_builder.py:153`).
2. The user picks one → the cloud creates/reuses a workspace with `config = {source:"sync_agent", tally_company, tally_company_guid, device_id, sync_state, books_from, last_synced_at}`. **Where the pick happens (in the agent, or on the web from a list the agent reports) is Q11.** It sits in Part 3 but decides this step.
3. Re-picking the same GUID is a no-op.
4. **One active device per workspace.** If a second PC tries to bind the same company, for example Tally on a shared server reached from two PCs or the owner's and the accountant's machines, the cloud doesn't let both upload: two agents would fight over the same cursors. The new device is asked to take over, and on confirm the old device is revoked. Whether multi-PC and TallyPrime server installs need more than this is Q30.

### Every cycle (~10 min + jitter)
| Tally state | Internet | Action |
|---|---|---|
| Tally closed / unreachable | any | Skip quietly |
| Tally running, but the XML port is closed or a popup is blocking the XML server | any | Skip + backoff. Tray shows "Turn on Tally connectivity" when the port is closed (R10); nothing for a popup, which clears on its own (R26) |
| Tally open, no company open | any | Skip quietly |
| Tally open, different company (GUID mismatch) | any | Skip quietly, never fetch or upload. If the name matches ours, also show the Re-link prompt ("Company identity changes" below) |
| Tally open, **our company**, nothing changed | up | Lower-tier work, top-down ("Work priority" below): user-confirmed work, parity remediation, the daily pass, month-end captures — and only if none of those, **one backfill / re-walk chunk** (the newest FY not yet loaded, newest month first), then the older-year deletion compare. Nothing pending → heartbeat only. Heartbeat after |
| Tally open, **our company**, nothing changed | **down** | Nothing. Lower-tier work needs server acks (coverage, reconcile, parity), so it waits for the internet |
| Tally open, **our company**, changes found | up | Fetch AlterID > cursor → upload → **re-read `ClosingBalance` / `ClosingValue` for the ledgers and stock items those vouchers touch** → refresh snapshots → advance cursor after server ack. **Nothing lower-tier runs this cycle** — fresh data always wins |
| Tally open, **our company**, changes found | **down** | Fetch → re-read the touched balances → store both as gzipped batches in the local **outbox** → **cursor not advanced on the server yet** → upload the outbox automatically when internet returns (oldest first, `batch_id` makes re-sends safe) |

**Heartbeat and "Sync now".**
- The heartbeat is `POST /api/sync/{ws}/heartbeat` (§5 "Cloud"). Whether it is also sent on
  skipped cycles is Q1.
- **"Sync now"** in the tray (decision 2) starts the next cycle immediately instead of waiting for
  the ~10-minute tick. It runs the same cycle, gate and tiers included, so it can't overload Tally.

**Why the re-read (mirrored-balance freshness).** Entering a voucher does not bump the ledger's own
AlterID, so an `AlterID > cursor` masters fetch would never refresh `LEDGER.ClosingBalance` or
`STOCKITEM.ClosingValue`. Every cycle that syncs vouchers therefore re-reads them for the ledgers
and stock items those vouchers touch: one collection call filtered to those names. That includes
vouchers queued in the outbox while offline, and vouchers soft-deleted by the deletion check
("After a gap" below). Probes 1/4 confirm the AlterID behaviour. It is what makes Part 2's "as of
last sync" label true for mirrored figures (Part 2, "Sources").

### First sync (the 2-FY window — this is what unlocks chat)
- Record counters **before** starting → masters → vouchers month by month for 2 FYs (**24 progress units**, fewer for a company younger than that — "Where history starts" below) → report snapshots → `ready`. At `ready`, both window FYs' `sync_fy_coverage` rows are `complete`.
- The snapshot step captures:
  - the current set
  - the most recent completed month-end, and the previous FY's close as-on 31 March (§4 "Month-end snapshots")
  - the opening-anchor TB as-on the day before the window starts (§6 "The opening anchor")
- The web shows progress text: "Connecting to Tally…" → "Syncing your data… 60%" → "Ready".
- The first incremental starts from the recorded counters, so edits made during the first sync are caught.
- **Interrupted by disconnect → resume** from saved chunk status on the next connection, not from zero.
- Chat stays locked until the first sync completes — **and only until then**. The history backfill below never re-locks it.

### Background history backfill (decision 7b)
Once the 2-FY window is `ready`, the agent keeps walking **backwards** through the company's earlier
financial years until it reaches `books_from`, then stops for good.

**Order.** Newest-first, both between FYs and within one: FY−2 before FY−3, and March before April
inside each. The most recently-closed year is the one people actually ask about, so it lands first.

**Where history starts.** `books_from` is the company's own books-beginning date (probe 1), which
isn't always 1 April.
- **Books beginning mid-year:** that FY's `months_total` counts only the months from `books_from`.
- **A company younger than the 2-FY window:** the first sync loads `books_from` to today, with
  fewer than 24 progress units. There is nothing to backfill, so `backfill.state` is `complete` at
  `ready`.
- **Split companies.** Many accountants split a Tally company each year. That creates a new company
  with a new GUID and a new `books_from`, and the years before the split stay in the old company.
  v1 syncs one company (decision 3), so the backfill stops at this company's `books_from` and those
  earlier years are out of scope (§10). The History card (Part 3, "Sync / Settings") must say so
  plainly, e.g. "Full history loaded — this company's books start in Apr 2023", rather than imply
  nothing came before. Linking split companies is Q32.

**Pacing (R3 — the top "customers uninstall" risk).** The lowest-priority loading work, strictly pre-empted:
- At most **one chunk per cycle**, and only on a cycle where incremental found nothing to do.
- Anything in tiers 1–5 ("Work priority" below) — a pending incremental change, a non-empty outbox,
  a parity remediation, the daily pass, a month-end capture — **cancels** the backfill chunk for
  that cycle.
- The same `gate.py` rules apply — one request at a time, chunk auto-split when slow, circuit breaker
  after a timeout. A backfill chunk that trips the breaker is retried next cycle, never immediately.
- Consequence: a ten-year company may take days or weeks to complete. That is the intended trade —
  it is invisible to the accountant, and the data nobody is waiting on arrives when it arrives.

**Watermark.** The authoritative record is the **`sync_fy_coverage`** table (§5 "Cloud") — one row per
FY with `state`, `months_complete`, `months_total`. The agent reports each finished chunk via
`PATCH /api/sync/{ws}/coverage`; `workspace.config.backfill = {oldest_available_fy,
oldest_complete_fy, books_from, percent, state}` is a denormalised copy for cheap status reads.
`oldest_available_fy` is the *available* edge (chat, the History card's available-from date);
`oldest_complete_fy` is the *verified* edge (parity, "Checked against … onwards"). They differ only
while FYs are `resyncing` ("What a full resync does…" below). `backfill.state` is `running |
resyncing | complete`; "stalled" is a UI label derived from no progress for N days (R29), not a
stored state. A month chunk is only marked
complete **after the server acks it**, so an interrupted backfill resumes rather than restarts (same
rule as the first sync). Backfill runs are recorded with `kind = backfill` and **do not advance
`last_synced_at`** — "last synced X ago" must mean fresh data, not old data.

**What queries do before a year lands** — "still loading", never a partial total — is defined in
Part 2, "Rule 2".

**Edits that land in a year not yet loaded.** Incremental sync fetches by AlterID, not by date, so
a back-dated edit can bring in a voucher dated in a `pending` FY. It is stored as usual (upsert,
`alter_id >=`). That FY still answers "still loading" and is not parity-checked until the backfill
completes it, and when the backfill reaches that month the upsert keeps whichever copy is newer.
Not a bug and not a mismatch.

**Interaction with parity.** Parity (§6) runs against the **synced window only**, bounded by the
watermark. Un-backfilled years are not a mismatch — they are simply out of scope for the check until
their FY completes. When an FY completes, it becomes eligible and gets one parity run.

**Completion.** When `oldest_complete_fy == books_from`'s FY, state becomes `complete`; the backfill
never runs again unless a full resync is triggered. Settings shows "Full history loaded".

### After a gap
- Tally not opened for days → the next connection fetches everything with AlterID > cursor (natural catch-up).
- **Deletion check** (GUID + AlterID list compare, one month per unit):
  - **2-FY window:** every day, as part of the daily pass ("Work priority" below, tier 4).
  - **Older years:** round-robin at tier 7, one FY at a time. Each is re-checked once per sweep
    (days to weeks, depending on depth), so the daily cost stays bounded however much history is
    loaded (R3).
  - **Masters** (groups, voucher types, ledgers, stock items): a full GUID + AlterID list compare in
    every daily pass. Masters are small, so they need no month split.
  - Anything missing is soft-deleted via `/reconcile`. Its response lists the ledgers those vouchers
    touched, and the agent re-reads their mirrored balances in the same cycle ("Every cycle" above).

### Company identity changes (backup restore, re-created company)
The agent cannot tell "a restored or re-created copy of our company" apart from "a different
company" by GUID alone, so **a GUID mismatch is always a skip** (cycle table above). Nothing is
ever fetched, uploaded or resynced automatically on a GUID mismatch (R2 outranks R8).

| Situation | What the agent sees | Action |
|---|---|---|
| Different company open | GUID ≠ saved, name ≠ saved | Skip quietly (cycle table) |
| Possibly our company, re-created or restored with a new GUID | GUID ≠ saved, **name = saved** | Skip, **and** the tray + Sync / Settings show "This looks like a new copy of *Bharat Traders* in Tally. Re-link?" Only on the user's confirm: save the new GUID and *offer* a full resync (decision 12). Q25 |
| Backup restored, same GUID | GUID = saved, `AltVchId`/`AltMstId` **below** our cursor | **Stop incremental** (our cursors are meaningless now), set `sync_state = restore_detected`, and *offer* a full resync (decision 12). Chat and dashboards stay available with a caveat ("Tally was restored from a backup — our copy may include entries that no longer exist"), per decision 13. Q26 |

Whether a Tally restore keeps the GUID is what probe 13 settles; both rows are designed so the
answer changes nothing unsafe.

### What a full resync does to the window and the backfill
A resync **never locks chat and never makes loaded data answer "still loading"**. The data is
already there and is refreshed in place, so every FY it touches goes to the coverage state
**`resyncing`**, not back to `pending`/`running`:

- **Backup-restore / re-link resync (whole company):** every `complete` coverage row — the 2-FY
  window and all backfilled years — goes to `resyncing`. **Masters, with their mirrored balances,
  are re-fetched first** (a restore can change ledgers, groups and every balance), then the 2-FY
  window (tier 2, "Work priority" below), chunk by chunk, **without** the first-sync chat lock
  (decision 13). The walker
  then re-walks the older `resyncing` years newest-first, at backfill priority (tier 6). Each FY
  returns to `complete` when its last chunk is acked. Rows not re-seen are soft-deleted by the
  per-chunk reconcile. Throughout, every `resyncing` year answers normally with the restore caveat
  ("Tally was restored from a backup — we're refreshing our copy"). Years that were still `pending`
  before the restore stay `pending` and load as normal backfill. A year that was **half-loaded
  (`running`)** at the time of the restore goes back to `pending` with `months_complete = 0`. Its
  months came from the pre-restore copy, and it was never answerable anyway.
- **Single-FY resync (parity remediation, §6):** only that FY goes `complete` → `resyncing` →
  `complete`. The rest of the window and the backfill watermark are untouched. Answers for that FY
  keep the integrity caveat they already carry (the FY is in `alert`), never "still loading" — even
  when it is the current FY.
- **Two watermark edges.** Both are counted back from the current FY. Queries read the *available*
  edge: the oldest FY reached, going back without a gap, whose rows are all `complete` **or**
  `resyncing`. Parity reads the *verified* edge: the same, counting only `complete` rows. A
  `resyncing` year is answerable but is not parity-checked until it returns to `complete`, then
  gets one run (for a parity-triggered resync, that run is escalation step 4, §6).
  Where these specs say "the watermark" without qualifying it, **query rules mean the available
  edge and parity rules mean the verified edge**.
- **Whole-company resync only:** `workspace.config.backfill.state` becomes `resyncing` and the
  History card shows "Refreshing history after a restore — 40%" (Part 3, "Sync / Settings" and
  "Page × sync state matrix"). A single-FY parity resync leaves `backfill.state` alone and shows
  only on the Data integrity card.
- Both are recorded as `sync_runs.kind = full_resync`. **Unlike backfill** (which never advances
  `last_synced_at`), a full resync advances it — but only for chunks inside the current 2-FY window,
  since those are the ones that bring the data up to date.

### New financial year
- On the first connected cycle on/after 1 April, add the new FY to the sync range (masters unchanged, vouchers for the new FY from its start). Its `sync_fy_coverage` row is created `complete`: it starts empty and incremental sync keeps it current, so the available edge never breaks at the forward end.
- The previous FY is kept. **No year is ever deleted automatically** — the backfill's whole purpose is to accumulate full history (decision 7b).
- A rollover does not restart the backfill; it only extends the *forward* edge of the window.

### Month-end snapshots
Current snapshots answer "as of now"; Part 2's Rule 1 and the dashboard's "vs last month-end"
tiles (Part 3, "Dashboard") also need Tally's own figures **at each month-end**, so they are
captured routinely rather than only when month-bisect happens to run.
- **When:** on the first connected cycle on/after the 1st of each month, capture the month-end set
  **as-on the last day of the previous month** (not as-on today, so a late capture is still correct).
- **What:** TB, Bills Receivable, Bills Payable, Stock Summary — four `TYPE=Data` calls, one at a
  time through `gate.py`, at tier 5 ("Work priority" below) — above the backfill, so a long backfill
  never starves it — spread across cycles if needed.
- **First sync:** also captures the most recent completed month-end **and the previous FY's close
  (31 March)** if that is a different date, so the dashboard has a month-end baseline and a
  year-start stock value on day one.
- **Older month-ends are not captured retroactively.** A TB for such a date is computed (Part 2,
  "Rule 1"). Bills Receivable/Payable and Stock Summary have no computed fallback, so those dates
  get the "I have Tally's own figures for these dates: …" answer.
- **Backfilled years:** every backfill chunk's anchor TB (§6 "The opening anchor") is itself a
  month-end TB, so TB month-ends exist for every backfilled month. When a backfilled FY completes,
  its FY-close Stock Summary and Bills Receivable/Payable (as-on 31 March) are captured once at tier
  5, plus P&L / BS if Q8 says yes.
- **Storage:** `tally_report_snapshots` keyed by `(workspace_id, report_type, as_on_date)`, plus
  `captured_at`, so a re-capture replaces rather than duplicates.
- **Missed month:** if Tally was never open in the month after a month-end, that month-end is captured
  late (the as-on date makes that safe). A tile whose baseline has no snapshot says "no month-end
  figure yet", never a wrong or zero baseline.
- **Settled 2026-09-23 (probe 18, live):** the **TB** honours its as-on date and returns correct history, but
  **Bills Receivable, Bills Payable and Stock Summary IGNORE it** and return the books' *current* position — bills
  dated after the as-on date still appear and the totals equal the period-end anchors, and every stock quantity
  equals opening + the FULL period's movements (the as-on group values were byte-identical to the FY-end capture).
  **Consequence:** those three cannot be snapshotted "as-on a past month-end" from a dated report. Their month-end
  figures must be **computed from vouchers**, or captured *at* the month-end and never re-derived later; a tile with
  no such snapshot says "no month-end figure yet" rather than showing today's position as history.

### Dates and clocks
- Tally dates are calendar dates with no time zone. Every FY, month and "as-on" boundary is worked
  out on those dates, never on timestamps.
- The agent's schedule (local midnight, the 1st of the month, 1 April) runs on the Tally PC's local
  clock. The product is India-only (decision 15), so that is IST in practice.
- The server stores timestamps as UTC (`timestamptz`) and shows "last synced X ago" and "as of
  18 Sep, 9:12 pm" in IST (`Asia/Kolkata`).
- Each heartbeat carries the PC's clock. A skew of more than a few minutes shows up in diagnostics
  (§5 "Windows agent"); the schedule still follows the PC clock.

### Work priority (never slow the accountant)
One request at a time, small chunks, explicit field lists, no imports ever, back off after any
timeout. Every piece of agent work has a tier, and each cycle works top-down:

| Tier | Work | Rule |
|---|---|---|
| 1 | Incremental fetch + mirrored re-read; outbox drain | Always first. If tier 1 had work this cycle, nothing below runs this cycle |
| 2 | User-confirmed work: "Re-check now", a confirmed resync (a whole-company resync's masters and 2-FY pass, or a single-FY resync) | Tiers 2–5 share the cycle budget, top-down |
| 3 | Parity remediation: targeted refetch, month-bisect calls, and the follow-up parity run after a completed resync or a reconcile that soft-deleted rows (§6) | Cycle budget |
| 4 | Daily pass: the masters compare and the 2-FY deletion compare, then the parity capture. Starts on the first connected cycle after local midnight | Cycle budget |
| 5 | Month-end snapshot capture; FY-close capture for a completed backfilled year | Cycle budget |
| 6 | One backfill or re-walk chunk, with its anchor TB | Only on a cycle where tiers 1–5 had nothing to do; at most one per cycle |
| 7 | Older-year deletion compare, round-robin ("After a gap" above) | Same rule as tier 6, after it |

- The **cycle budget** (request count and seconds, sized from probe 9) is shared by tiers 2–5. Any
  timeout ends that cycle's work (circuit breaker in `gate.py`).
- **Internet down:** only tier 1's fetch and mirrored re-read run, into the outbox. Tiers 2–7 need
  server acks, so they wait.
- **"Sync now"** only brings the next cycle forward; it never skips a tier or the gate.
- The history backfill is the lowest-priority loading work (decision 7b), because nobody is waiting
  on a 2019 voucher. Only the older-year deletion compare ranks below it.

## 5. Architecture

### Code isolation (v2) — decided 2026-09-22
- **No change to current code.** Part 1 edits nothing under `backend/`, `frontend/`, `tests/`, `scripts/`,
  `backend/db/migrations/`, or the root `pyproject.toml`. All Part 1 work lives under `v2/` at the repo root.
  Moving it into the current code is a later, separate step with its own spec.
- **Copy, don't import.** v2 code never imports from `backend/`, `tests/` or `scripts/`. What it needs is
  copied into v2 and fixed there: the known gaps in §13 (company-name escaping, float money, `parse_amount`
  turning failures into zero) must not be copied across. Each copied file starts with a comment naming its
  source path and the commit it was copied from, so the later merge can diff it. Current docs and fixtures
  may be read. A current fixture a v2 test needs is copied into `v2/tests/fixtures/`.
- **Layout:**

  ```
  v2/
    README.md         how to run v2, and these isolation rules
    pyproject.toml    v2's own dependencies (keyring, pystray, pywin32, …)
    probes/           S0 probe scripts, run notes and results
    agent/            S2: the sync agent (Mac-runnable core + Windows adapters)
      tally/          copied read code (client, envelopes, helpers) + new sync builders and parsers
    cloud/            S1: separate FastAPI app
      alembic/        v2's own migration chain
    tests/            v2 tests: fixtures/sync/ (S0 captures), v2 mock Tally, unit / DB / E2E
  ```

- **Cloud side: a separate app, same database.** `v2/cloud/` is its own FastAPI app on its own port, using
  the same Postgres as the current app.
  - Its Alembic chain (`v2/cloud/alembic/`) uses its own version table (`alembic_version_v2`) and only
    **creates new tables**. None of the new table names exist today (checked against `backend/db/models.py`).
  - Existing tables (`users`, `workspaces`) are **read** for agent login and company binding. Their schema is
    never altered. Foreign keys from new tables to them are allowed, since that doesn't alter them.
  - **`workspace.config` mapping.** Wherever this spec says `workspace.config.<key>` (for example
    `tally_company_guid`, `sync_state`, `cursors`, `backfill`, `last_parity`), v2 stores it in a v2-owned
    table **`sync_workspaces`**: one row per bound workspace, keyed by `workspace_id`. Folding it into
    `workspace.config` is part of the later merge.
  - Agent login checks the website email/password against `users` with a copied `verify_password`. Calls from
    the web to v2 endpoints such as `sync-status` are checked with a copied JWT check using the same `JWT_SECRET`.
  - Whether binding may insert a new row into the existing `workspaces` table, or only attach to an existing
    one, is decided in the S1 spec.

### Windows agent (Python)
- Reads Tally with **v2 copies** (in `v2/agent/tally/`) of `backend/tally_bridge/{client,request_builder,response_parser,exceptions,models}.py` + helpers `sanitize_xml`, `parse_amount`, `detect_error` (`response_parser.py`), trimmed to the read paths the agent uses, plus new sync builders/parsers beside them. `writer.py`, `import_builder.py` and `mock_handler.py` are never copied, so the agent has no write code at all.
- **Platform adapters (so the agent core runs on a Mac, §7):** everything Windows-only sits behind a small adapter the agent core never imports directly: the Windows service (`service.py`, pywin32), DPAPI token storage (`auth.py` via `keyring`, which falls back to the macOS Keychain in development) and the installer steps in `updater.py`. In development the agent also runs as a foreground CLI.
- **Installer:** a per-machine install that registers the Windows service, so it needs an administrator once (Q31). After that the agent runs without admin rights. **Uninstalling** revokes the device token on the server; if the PC is already gone, "Remove device" in Settings (`DELETE /api/devices`) does the same.
- **Network:** all calls to our cloud (uploads, auth, update checks) use the Windows system proxy settings, so offices behind a proxy work without extra setup.
- `gate.py`: one request at a time, per-request timeouts, backoff, circuit breaker, a cheap health + company-GUID check before any heavy call (LESSONS §15 popup-freeze).
- `extractor.py`: explicit fields (no `*`, LESSONS §5; no `$$InDateRange`, §3). Day/month chunks capped at about 5k vouchers (never more than 10k).
- `change_detector.py`: compare company counters with saved cursors.
- `scheduler.py`: the work-priority tiers and the per-cycle budget (§4 "Work priority").
- `diagnostics.py`: every heartbeat carries the agent version, TallyPrime version, last error code, circuit-breaker state, outbox depth and PC clock, and no business data. A tray action, "Send diagnostics", uploads a redacted log bundle for support: request timings and error shapes, with no voucher, ledger or party content (decision 14, R20).
- `state.py` (SQLite): company, a cached copy of the cursors (the server's copy is authoritative, §5 "Cloud"), first-sync chunk status, outbox of gzipped batches, sync log.
- `uploader.py`: batches of at most 500 objects / 5 MB gzipped (the server's ingest limit), with a `batch_id` idempotency key. The cursor advances only after the server acks. **An outbox batch is deleted as soon as the server acks it**, so nothing but unsent data stays on the PC.
- `auth.py`: device token stored via Windows DPAPI (`keyring`). A short-lived access token plus a rotating refresh token; the S1 spec sets the lifetimes. **On a 401, or once the device is revoked, the agent stops uploading, keeps its outbox, and the tray shows "Signed out — log in again".** Nothing uploads until a fresh login.
- `service.py` (pywin32) + `tray.py` (pystray), talking over localhost / named pipe. A service in session 0 can still reach `localhost:9000`, but Tally itself needs a logged-in Windows session (see R11).
- `updater.py`: version check, signed download, sha256 check, `--selftest` after update, rollback on failed self-test.

### Cloud (FastAPI + Postgres) — the separate v2 app in `v2/cloud/`
- `v2/cloud/api/agent_auth.py`: `POST /api/agent/auth/login` (v2 copies of `verify_password` from `backend/utils/auth.py` and `_check_rate_limit` from `backend/api/auth.py`) and `/refresh` with a rotating, revocable device token (hash stored). Plus `GET/DELETE /api/devices`.
- `v2/cloud/api/sync.py`:
  - `POST /api/sync/company` (bind one workspace)
  - `POST/PATCH /api/sync/{ws}/runs` (progress). Carries `kind` = `first_sync | incremental | backfill | full_resync`, so a slow history backfill is never mistaken for live sync progress.
  - `POST /api/sync/{ws}/batches` (upsert on `(workspace_id, guid)`, only when `alter_id >=` the stored one; voucher lines replaced in the same transaction). Batches are tagged with their run's `kind`; a **`backfill` batch must not advance `last_synced_at`** (decision 7b) — otherwise the UI would claim "synced 2 min ago" while no fresh data arrived. A `full_resync` batch advances it only for chunks inside the current 2-FY window (§4).
  - `PATCH /api/sync/{ws}/coverage` (the agent reports a completed FY/month chunk; the server advances the backfill watermark in `sync_fy_coverage`)
  - `POST /api/sync/{ws}/reconcile` (soft-delete missing records, return mismatches to refetch, and return the ledgers whose mirrored balances the agent must re-read)
  - `POST /api/sync/{ws}/snapshots` — one Tally report per request:
    - `report_type`: `trial_balance | balance_sheet | profit_and_loss | stock_summary | bills_receivable | bills_payable`
    - `as_on_date`, plus `from_date` for period reports
    - `purpose`: `current | month_end | fy_close | anchor | bisect`
    - the parsed rows and `captured_at`

    Upserts on `(workspace_id, report_type, as_on_date)`, so a re-capture replaces the old copy (§4 "Month-end snapshots"). `purpose` is informational: one stored snapshot serves every purpose that shares its key.
  - `POST /api/sync/{ws}/heartbeat` — carries:
    - agent version and TallyPrime version
    - what the agent sees in Tally: closed, no company open, another company, or ours
    - last error code, circuit-breaker state, outbox depth and PC clock

    It sets `last_seen_at`, which is separate from `last_synced_at`: a heartbeat proves the agent is alive, not that data is fresh. Whether it is also sent on skipped cycles is Q1.
  - `POST /api/sync/{ws}/parity` (the agent uploads the capture's snapshots via `/snapshots` and ledger balances via `/batches`, then posts the before/after counters here; the server runs the
    comparison synchronously and returns the remediation list — §6)
  - `GET /api/workspaces/{id}/sync-status` (UI polling; carries the stored `sync_state` (below), the **integrity** state (§6, `last_parity`), the **backfill** state — `{oldest_available_fy, oldest_complete_fy, books_from, percent, state}` — `last_synced_at`, the active device's `last_seen_at` and last reported Tally status (heartbeat), and a pending re-link prompt (§4 "Company identity changes"), for the History card, the Data integrity card's verified span, the connection status card and the identity prompts in Part 3, "Sync / Settings")
- **Stored `sync_state` values:** `awaiting_first_connection | first_sync | ready | error | restore_detected`. The web works out the rest (Part 3, "Web states"):
  - "syncing %" is `first_sync` plus its run's progress
  - "stale" comes from `last_synced_at` (threshold Q3)
  - "Tally offline" and "agent offline" come from `last_seen_at` and the heartbeat's Tally status (Q1)
  - a pending re-link is its own flag, shown alongside any state
- `get_current_device` dependency in `v2/cloud/api/dependencies.py`, beside a v2 copy of `get_current_user` (`backend/api/dependencies.py`). A device may only post to its own workspace, and only the workspace's **active** device may post (§4 "Setup").
- **Ingest rules:**
  - **Masters before vouchers, every cycle.** Within one cycle the agent uploads masters (groups, voucher types, ledgers, stock items) before the vouchers that use them, not only in the first sync.
  - **References are resolved to GUIDs on the server.** Tally exports voucher lines, inventory lines and bill allocations by *name*: `AllLedgerEntries` / `AllInventoryEntries` in `request_builder.py:62`. At ingest the server resolves each name to the master's GUID (`ledger_guid`, `stock_item_guid`, `party_ledger_guid`) and stores both.
    - A name that matches no synced master rejects the batch with `missing_master`, never storing it half-resolved. The agent refetches masters and retries.
    - If probe 6 shows lines can export the ledger's GUID directly, the agent sends it and the name lookup becomes a cross-check.
  - **Mirrored balances: the latest capture wins.** `closing_balance` / `closing_value` change without the master's AlterID moving, so the `alter_id >=` rule can't order them. Each carries `balance_captured_at`, and a re-read only replaces the stored balance if it was captured later.
  - **Cursors live on the server.** The voucher and master cursors (`AltVchId`, `AltMstId`, AlterID) are stored per workspace in `workspace.config.cursors` and advanced on ack; the agent only caches them.
    - A reinstall or a new PC resumes from the server copy (Q4).
    - Restore detection (§4 "Company identity changes") compares Tally's counters with it.
- **Ingest limits:** `/batches` rejects a body over the agent's own cap (5 MB gzipped / 500 objects) with 413. Each device has a request rate limit, and a 429 makes the agent back off as it would after a Tally timeout. Both limits are settings in v2's own `v2/cloud/config.py`, following the current `config.py` convention.
- **v2 migration `v2_001`** (first migration in `v2/cloud/alembic/`, version table `alembic_version_v2`; the current chain in `backend/db/migrations/` is untouched) tables:
  - Agent and sync bookkeeping: `sync_workspaces` (what this spec calls `workspace.config.*`, §5 "Code isolation (v2)"), `agent_devices`, `sync_runs`, `sync_batches`
  - Masters: `tally_groups`, `tally_voucher_types`, `tally_ledgers`, `tally_stock_items` (+ stock groups and units). `tally_ledgers` also stores Tally's own GST classification (`tax_type`, `gst_duty_head` — probe 23) so the GST tile never matches ledgers by name
  - Vouchers: `tally_vouchers`, `tally_voucher_ledger_lines`, `tally_voucher_inventory_lines`, `tally_bill_allocations`
  - Snapshots: `tally_report_snapshots`
  - Integrity: `parity_runs`, `parity_lines` (§6)
  - Coverage / backfill watermark: **`sync_fy_coverage`** — one row per `(workspace_id, fy_start, fy_end)` with `state` (`pending | running | resyncing | complete`), `months_complete`, `months_total`, `completed_at`. `pending`/`running` = never fully loaded (queries answer "still loading"); **`resyncing` = already loaded, being refreshed in place** (queries answer normally with a caveat, parity skips it); `complete` = loaded and eligible for parity. This is the **authoritative watermark** that Part 2's "still loading" rule and §6's watermark-bounded parity both read — through two different edges (§4 "What a full resync does…"); `workspace.config.backfill` is only a denormalised copy for cheap status reads. `sync_runs` gains a `kind` column (`first_sync | incremental | backfill | full_resync`).
  - Every master and voucher row has `guid`, `alter_id`, `is_deleted`, `raw` JSONB, with indexes on `(workspace_id, date)`, `(workspace_id, ledger_guid)` (every join is on GUID, R9) and `(workspace_id, ledger_name)` (name search only).
  - **Minimum columns** (the S1 spec fixes types and further indexes):
    - `agent_devices`: `id`, `workspace_id`, `device_name`, `token_hash`, `refresh_hash`, `is_active` (one active device per workspace), `agent_version`, `last_seen_at`, `revoked_at`.
    - `tally_groups`: `guid`, `name`, `parent_guid`, `nature` (`assets | liabilities | income | expenses`, from the primary group), `is_revenue`, `affects_gross_profit`.
      - `nature` is what tells a balance-sheet ledger from a P&L one (§6 "Rung 1"; Part 2, "Rule 1").
      - **Derived, not read (probe 25 A, live 2026-09-23).** Neither `Nature` nor `PrimaryGroup` exports from the Group
        collection (0 of 32 groups); `Parent` exports for all 32. S1 walks the `Parent` chain up to a reserved primary
        group and maps that through `PRIMARY_NATURE` (e.g. `National Creditors → Sundry Creditors → Current Liabilities
        → liabilities`), so the walk must handle custom sub-groups of any depth. `IsRevenue` and `AffectsGrossProfit`
        DO export (32/32) and agree with the walk, so they are read and used as a cross-check on it.
      - Today `build_list_groups` fetches only `Name` and `Parent` (`request_builder.py:223`), so S1 extends it with
        `IsRevenue` / `AffectsGrossProfit` and computes `nature` itself.
    - `tally_voucher_types`: `guid`, `name`, `parent`, `base_type` (Sales, Purchase, Receipt, Payment, Journal, Contra, Credit Note, Debit Note, Memorandum, …).
      - Custom types such as "Sales – GST" roll up to their base type. That is how Parts 2 and 3 sum sales and purchases and leave Memorandum vouchers out (probe 25).
      - **Derived, not read (probe 25 A, live 2026-09-23).** The VoucherType collection exports only `Parent` and
        `ReservedName` (24/24 each) — there is no base-type field. S1 walks the voucher-type `Parent` chain until it
        reaches one of Tally's 24 reserved types and stores that as `base_type`; the reserved set captured live is in
        probe 25's `base_types` observation (`v2/probes/results/results.json`) and is the seed for the S1 map.
      - `tally_vouchers.base_type` is the denormalised copy of this derived value — the voucher itself carries the
        voucher-type NAME, not a base type.
    - `tally_ledgers`: `guid`, `name`, `group_guid`, `opening_balance`, `closing_balance`, `balance_captured_at`, `is_bill_wise`, `tax_type`, `gst_duty_head`.
    - `tally_stock_items`: `guid`, `name`, `parent_guid`, `base_unit`, `closing_qty`, `closing_value`, `balance_captured_at`.
    - `tally_vouchers`: `guid`, `master_id`, `alter_id`, `date`, `voucher_type_guid`, `base_type`, `voucher_number`, `reference`, `party_ledger_guid`, `narration`, `is_cancelled`, `is_optional`, `is_post_dated`, `is_deleted`.
    - `tally_voucher_ledger_lines`: `voucher_id`, `ledger_guid`, `ledger_name` (as exported), `amount` (INR base amount, debit negative), `is_deemed_positive`.
    - `tally_voucher_inventory_lines`: `voucher_id`, `stock_item_guid`, `quantity`, `rate`, `amount`.
    - `tally_bill_allocations`: `voucher_id`, `ledger_guid`, `bill_name`, `bill_type` (`new_ref | agst_ref | advance | on_account`), `amount`, `due_date` or `credit_period` (probe 23).
    - Lines, inventory lines and bill allocations have no GUID of their own in Tally. They are replaced with their voucher in the same transaction, and they go when the voucher is soft-deleted.
  - If probe 9 / 21 volumes make computed chat queries slow, add a per-ledger monthly totals table maintained on ingest. Decided in the S1 spec.
  - **All money columns are `Numeric(18,2)`, never Float** (§6 "Storage").
  - **All amount columns hold the INR base amount** (decision 15). Voucher lines carry no currency column; where a voucher is in foreign currency, the original currency code, face value and Tally's own rate stay in `raw` JSONB for drill-down and are never aggregated. A probe must confirm the INR base amount is present on forex voucher lines (probe 22). **Changed 2026-09-25 (probe 22 CONFIRMED):** it is — the line AMOUNT's stated base (`… = -? 37216.04`); store it, keep face + rate in `raw`. A forex **ledger's** balance is at the latest voucher rate (C47), not the sum of these bases — see the header's plan-part-7 block.

## 6. The integrity (parity) system

### Why it is not optional
Tally stores no closing balances — it computes them when a report opens (R5). We recompute from
vouchers. Divergence is **silent**: a dashboard shows ₹9,70,537 receivable with full confidence
while the real figure is ₹10,40,000. Parity is the only thing between a sync bug and a wrong number
the owner acts on. It is the primary mitigation for R5 and a partial one for R4, R6, R7, R8, R16, R25.

### What the code already gives us (verified 2026-09-18)
- `build_list_ledgers` (`request_builder.py:220`) already fetches `ClosingBalance` **and**
  `OpeningBalance` per ledger, parsed at `response_parser.py:95-110`. Every masters sync therefore
  yields **Tally's own per-ledger closing balance for free**. This is the best anchor we have, and
  it is why parity is a ladder rather than a single check.
- The TB report is **group-level only**: `tests/fixtures/trial_balance_live.xml` holds 7 top-level
  group rows (Capital Account, Current Liabilities, Fixed Assets, Current Assets, Sales Accounts,
  Purchase Accounts, Indirect Expenses), and `_wrap_report_envelope` (`request_builder.py:111`)
  sets no explode flag. TB alone can never localise a mismatch to a ledger.
- Nominal (P&L) ledgers always read `ClosingBalance = 0` (LESSONS §4 — auto-closed to P&L A/c).
  So rung 1 covers balance-sheet ledgers only, and rung 2 is *required* for Sales / Purchase /
  Expenses. The two rungs are complementary, not redundant.
- **Sign convention is already consistent end to end: debit = negative, credit = positive** — in the
  live TB export (`-1048846.53` in the debit column) and on the write path
  (`import_builder.py:242-243`). No conversion needed, but counter-intuitive enough that it needs an
  explicit test assertion.

### Rung 0 — double-entry invariant (no Tally call, every ingest)
For every voucher in every batch, `SUM(line.amount)` must equal `0.00`. Catches truncated payloads,
a dropped leg, a chunk that split mid-voucher. On failure the **batch is rejected, not stored** —
a half-voucher in the DB is worse than a missing one. Runs continuously, not daily.

### Rung 1 — ledger-level (free, highest resolution)
For each balance-sheet ledger (its group's `nature` is `assets` or `liabilities`, §5 "Cloud"):

```
computed = opening_balance_at_window_start
         + Σ line.amount from tally_voucher_ledger_lines
           where ledger_guid       = :guid
             and voucher.date     <= :as_on
             and voucher.is_cancelled = false
             and voucher.is_optional  = false
             and voucher.is_deleted   = false
```

compared against the **mirrored** `tally_ledgers.closing_balance` captured in the same sync cycle.
Join on **GUID, never name** (R9 — renames and duplicate names under different parents). Lines carry
no `is_deleted` of their own: they are replaced with their voucher and go when it is soft-deleted
(§5 "Cloud").

**Post-dated vouchers.** `:as_on` must be the date Tally's `ClosingBalance` is as-of, which may be
today or the end of the current period, and post-dated vouchers must be counted the way Tally counts
them. Otherwise every ledger with a future-dated voucher shows a false mismatch. ~~Probe 16 settles
both~~ *(settled — see the measured rule below)*, and `is_post_dated` is stored on every voucher (§5 "Cloud").

**Changed 2026-09-25 (S0 exit gate) — probe 16's measured rule** (live 2026-09-24, typed, company A part DIFFERENT
only on the nominal-ledger half, B part FAILED per C45; `v2/probes/results/results.json` `probes.16`, tracker row 16):
- **As-of date = the current period's end.** `LEDGER.ClosingBalance` is as of the end of the period Tally has open
  (company A: **31-03-2026**), **not** Tally's current date — the books had only reached 01-03-2026 before the run,
  yet a throwaway voucher dated 31-03-2026 moved Cash (₹23,000 → ₹23,001). So rung 1 uses **`:as_on` = the current
  period's end date**.
- **Post-dated vouchers are counted.** A voucher marked post-dated (Ctrl+T) was included in `ClosingBalance` and
  exported **`IsPostDated=Yes`**; an ordinary voucher dated after Tally's current date was included too (exporting
  `IsPostDated=No`). So rung 1 does **not** filter on `is_post_dated` — lines of post-dated vouchers are summed like
  any other; the cancelled / optional / deleted filters above still apply.
- **`ClosingBalance` agrees with the lines.** Company A: 0 mismatches between each balance-sheet ledger's
  `ClosingBalance` and its summed `ALLLEDGERENTRIES.LIST` lines (50 vouchers); company B: 19/19 closing balances
  matched the dataset. Rung 1 stands at ledger level.
- **As-on reads (C45).** A **typed** `SVTODATE` on the Ledger collection **is honoured inside the current period**
  (A: 19 of 22 balance-sheet ledgers moved to the as-on lines at 31-10-2025); for a date **before** the current
  period it silently **clamps to the current period's start** (**Ruling C45**, B: 31-03-2025 and 31-03-2023 both
  answered with the 01-04-2025 balances). A dated Ledger collection is therefore a same-period read only, never a
  past-period (backfill) anchor — those come from a TB (see "The opening anchor"). `SVFROMDATE` is never sent on a
  master collection (LESSONS §15 rule 17).
- *Scope caveat:* TallyPrime 7.0 Edit Log, **Educational**, under **Wine 11.0**; the as-of-period-end rule is an open
  tier-C check on a licensed Tally.

**Forex ledgers (C47, added 2026-09-25 S0 exit gate).** Tally values a forex ledger's `ClosingBalance` (and its TB
row) at its face total × the rate of its **latest-dated** forex voucher, while our lines carry each voucher's stated
INR base — so rung 1's computed sum differs from the mirrored balance by the **unrealised forex difference** (probe 22:
₹183.87 on `Gulf Office Supplies LLC (USD)`). Rung 1 must expect that difference on forex ledgers instead of flagging
it, and S1 reads the forex ledger's balance from Tally rather than recomputing it. That ledger's
`ClosingBalance`/`OpeningBalance` also export as an **expression** (`-$1609.71 @ ? 82.58/$ = -? 132929.85`), which the
plain amount parser rejects — **open S1 decision:** parse the stated base (the part after `=`) as for voucher lines,
or take that ledger's balance from the ledger-level TB (probe 17's route).

Nominal ledgers are marked `not_applicable`, **never** `match` — otherwise a future bug hides
behind a false green.

### Rung 2 — group-level (covers what rung 1 cannot)
Roll our ledger balances up the `tally_groups` parent chain to the top-level groups and compare
against the stored TB snapshot. This is the only rung that covers Sales / Purchase / Indirect
Expenses.

It also asserts **the snapshot itself balances** (Σ debit + Σ credit ≈ 0). If Tally's own TB does
not balance, Tally is serving stale data (R4) and the whole run is discarded — not reported as our
error.

**Forex (C47, added 2026-09-25 S0 exit gate).** The TB values a forex ledger at its latest voucher rate (see Rung 1
"Forex ledgers"), so every group that contains a forex ledger — up to its primary group — differs from our rollup by
the same unrealised forex difference (probe 22: ₹183.87 under Sundry Debtors). Rung 2 expects that difference on
those groups instead of flagging it; how it is computed (from Tally's forex ledger balance vs Σ its lines' INR bases)
is part of S1's parity design, together with the expression-form balance parser.

### Rung 3 — statement-level (deferred, decision 11)
Computed P&L net vs the full-FY P&L snapshot; computed asset/liability totals vs the BS snapshot;
Σ `tally_stock_items.closing_value` vs the Stock Summary snapshot; Σ pending bills vs the Bills
Receivable/Payable snapshots. Seed-company anchors kept for when this lands:
**₹9,70,537 receivable / ₹18,34,142 payable**.

### The quiescence guard — how we avoid crying wolf
Vouchers and the snapshot come from **separate** Tally calls. A voucher entered between them
produces a real numeric difference that is not a bug.

> Read the company's `AltVchId` / `AltMstId` **immediately before** the parity capture and **again
> immediately after**. If either moved, **discard the run** (`aborted_moving`) and retry next cycle.
> No alert, no remediation.

Parity also only runs when **the outbox is empty and the cursor is caught up** — otherwise we would
be comparing a knowingly-incomplete DB against a current Tally.

**Parity is bounded by the backfill watermark** (decision 7b). A half-backfilled company is
*expected* to disagree with Tally's all-time ledger closing balances, so:
- **The opening anchor** (parity only; chat computes backwards instead, Part 2 "Rule 1"). Our DB has no vouchers
  before the watermark, so what is missing is the **opening** balance, not the closing one. While
  `backfill.state != complete`:

  ```
  computed = tally_balance_as_on(watermark_date − 1)       ← from a TB snapshot as-on that date
           + Σ our lines where watermark_date <= voucher.date <= :as_on   (same filters as rung 1)
  compare against  current mirrored LEDGER.ClosingBalance (rung 1)  /  current TB (rung 2)
  ```

  The anchor can only come from a **`TYPE=Data` report** (probe 17's exploded ledger-level TB): ~~as-on per-ledger
  balances are unobtainable from the Ledger collection (live 2026-09-23 — see the header).~~ *(Corrected 2026-09-25,
  S0 exit gate: that was measured untyped. A typed `SVTODATE` on the Ledger collection is honoured **inside** the
  current period but clamps to the current period's start for any earlier date (C45, probe 16 B) — and the watermark
  anchor is by definition before the current period, so the Ledger collection still can't supply it. See Rung 1's
  measured rule.)*

  - **Capture:** the anchor TB is taken as-on the day before the window starts at the end of the
    first sync (and again at the end of a whole-company resync's 2-FY pass), and with **each backfill
    or re-walk chunk** as-on the day before that chunk's month, so the anchor moves with the verified
    edge (one extra `TYPE=Data` call per chunk, same gate rules).
  - **Every anchor is kept** in `tally_report_snapshots`, not just the latest. Parity uses the one
    at the verified edge. Each anchor is a genuine month-end TB, so Part 2's Rule 1 can also serve
    "TB as on <that month-end>" from it as a snapshot. It is also what Part 2's probe-16 fallback
    computes forward from.
  - ~~Once history is complete, `tally_balance_as_on(...)` is replaced by the ledger's books-start
    `OpeningBalance` (probe 16) and the comparison is the plain all-time one.~~ *(Corrected 2026-09-25, S0 exit
    gate: a ledger's master `OpeningBalance` is the **current-FY** opening, not books-start — probe 11 re-run under
    C46 (14/25 ledgers at the current-FY opening) and probe 16 B (`opening_scope = fy`). Used as a books-start anchor
    it would double-count every prior FY.)* Once history is complete, `tally_balance_as_on(...)` is replaced by the
    **books-start opening read from a report as-on the books start** — the ledger-level TB (probe 17's
    `ISLEDGERWISE=Yes`) for ledgers and groups, and the Stock Summary for stock — the route probe 18 proved on a
    closed prior FY (company B, 31-03-2023). The comparison is then the plain all-time one. Never a master's
    `OpeningBalance`. The exact as-on date convention (the books-start day itself vs the day before, and C43's
    Educational-mode day restriction) is fixed in S1's parity design.
- **Per-ledger anchors during backfill: available (settled 2026-09-23).** ~~Probe 16's route is impossible, but~~
  *(Corrected 2026-09-25, S0 exit gate: probe 16's route works only inside the current period and clamps before it
  (C45), so it cannot anchor a backfill — same net result)*, but
  **probe 17's is not**: `ISLEDGERWISE=Yes` on the `TYPE=Data` Trial Balance returns a **ledger-level** TB, safely
  and fast (company A: 29 ledger rows, ~7.3 KB, ~18 ms under Wine). So the anchor exists **per ledger** and rung 1
  runs mid-backfill; the fallback below is what happens only if this route fails on a customer's Tally.
  - The consumer must **match rows against the ledger list**: the response also carries the synthetic
    `Opening Stock` row, which is not a ledger.
  - `EXPLODEFLAG` / `EXPLODEALLLEVELS` are **not** an alternative: they stop at the second group level and silently
    omit every ledger under a custom sub-group. `SVEXPLODEFLAG` / `LEDGERWISE` are not recognised at all (the plain
    TB comes back).
  - *Fallback if the route fails:* rung 1 is **suspended** while the backfill runs (ledgers `not_applicable`, never
    `match`), rung 2 carries the check at group level, and month-bisect localises. Chat is unaffected either way:
    it computes ledger balances backwards from mirrored balances, which needs no anchor (Part 2, "Rule 1").
- Rung 2 compares group rollups with the same opening-anchored formula, and **adds the TB's own `Opening Stock` row
  to the stock-bearing group** before comparing (no ledger carries it; it is *not* the Stock Summary closing total).
- An FY becomes parity-eligible when its backfill completes, and gets one run at that point.
- **`resyncing` FYs are skipped.** Parity reads the *verified* edge (oldest contiguous `complete`
  FY, §4 "What a full resync does…"). A year being refreshed in place is half old copy, half new, so
  checking it would manufacture mismatches. It gets one run when it returns to `complete`.
- The Settings card says "Checked against Apr 2024 onwards" until the backfill finishes, so a green
  tick never overstates what was actually verified.

### Cause classifier — mismatch shape → likely cause → action
This is what turns a number into something actionable.

| Signature | Likely cause | Automatic action |
|---|---|---|
| Ledger in Tally, absent in our DB | masters sync gap (R6) | refetch masters |
| Ledger in our DB, absent in Tally | ledger deleted | run `/reconcile` soft-delete |
| One ledger differs | a missed voucher or edit on that ledger | targeted refetch of that ledger's vouchers for the FY |
| Two ledgers differ by equal-and-opposite amounts | one whole voucher missed or duplicated | month-bisect, then refetch that month |
| Many ledgers differ by a constant | opening balances wrong | refetch masters + openings (probe 11) |
| Difference equals an exactly cancelled/optional voucher | our filter is inverted (R16) | engineering flag — code bug, not a sync gap |
| Every ledger matches but our rollup total does not | our group-tree walk is wrong | engineering flag |
| Tally's own TB does not balance | stale long-running Tally (R4) | discard run; tray says "Restart TallyPrime" |

### Month-bisect — locating a mismatch cheaply
Request a TB as-on each month-end of the affected FY (12 cheap `TYPE=Data` calls at tier 3, spread
across cycles, one at a time through `gate.py`) and compare against our computed balances at the same
dates. The first month where they diverge is the month to refetch. Bounded cost: month-ends already
held by the routine month-end set (§4 "Month-end snapshots") are reused, so only missing ones cost a
call, and every TB it fetches is stored in `tally_report_snapshots` for Part 2's Rule 1 to reuse.

### Escalation ladder — confirm before you alarm
1. **First mismatch** → state `suspect`. **Invisible to the user.** Enqueue remediation, re-check
   next cycle.
2. **Still mismatched after remediation** → state `alert`. Now the user sees it.
3. **Two consecutive failed heals** → *offer* a full resync of the affected FY. **The user decides**
   (decision 12) — it is hours of Tally load.
4. **Mismatch survives a full resync** → `hard_alert` + internal engineering flag. This is a real
   bug or a Tally quirk; no amount of refetching will fix it.

Any successful re-check drops the state straight back to `ok`.

### Schedule and trigger — there is no scheduler
Confirmed 2026-09-18: there is **no** Celery / APScheduler / cron / BackgroundTasks anywhere in the
backend. The only lifecycle hook is the FastAPI lifespan (`backend/main.py:24-69`). So parity is
**agent-triggered, server-executed**:

- As the second half of the daily pass (§4 "Work priority", tier 4), after the 2-FY deletion
  compare, the agent captures the snapshots and ledger balances and uploads them via `/snapshots` and
  `/batches` (§5 "Cloud"). It then posts the before/after counters to
  `POST /api/sync/{ws}/parity`. The daily pass starts on the first connected cycle after local
  midnight.
- The server runs the comparison **synchronously inside that request** and returns the remediation
  list, which the agent then executes.
- No new infrastructure, and it naturally cannot run while Tally is closed — which is correct,
  because parity is meaningless then.
- Also triggered by: a completed full resync or a reconcile that soft-deleted rows (tier 3), and a
  "Re-check now" button in Sync / Settings (tier 2).

### Storage (v2 migration `v2_001` additions)

**`parity_runs`** — `id` UUID pk, `workspace_id` FK+index, `sync_run_id`, `as_on_date`, `rung`,
`status` (`ok|suspect|alert|hard_alert|aborted_moving`), `lines_compared`, `mismatch_count`,
`max_abs_diff`, `net_diff`, `counters_before` JSONB, `counters_after` JSONB, `remediation` JSONB,
`started_at`, `finished_at`, `created_at`.

**`parity_lines`** — `id`, `run_id`, `workspace_id`, `scope` (`ledger|group|statement`), `guid`,
`name`, `our_amount`, `tally_amount`, `diff`, `verdict`
(`match|mismatch|missing_in_db|missing_in_tally|not_applicable`), `cause`, `remediation_status`.

Both follow the current migrations' 004/005 conventions exactly, in v2's own chain: UUID pk, `workspace_id` FK + index, `created_at` with
`server_default=sa.func.now()`, compound indexes via `__table_args__`, bare FKs with no
`relationship()`.

- **Money columns are `Numeric(18,2)`, never Float.** Everything in the codebase today is a Python
  float (`parse_amount`, `response_parser.py:19`); comparing floats at paise resolution across
  hundreds of thousands of voucher lines manufactures phantom mismatches.
- `workspace.config` gains `last_parity: {state, checked_at, as_on, mismatch_count}` for a cheap read
  on the status endpoint (matches the existing `sync_state` pattern).
- **Retention:** runs kept 90 days; matching lines pruned after 7 days (a daily ledger-level run on a
  large company is thousands of rows); mismatching lines kept the full 90.
- **Tolerance:** `PARITY_TOLERANCE_PAISE` setting, default `100` (₹1.00), applied per line **and** to
  the grand total. Lives in `v2/cloud/config.py`, following the current `config.py` convention (labelled
  section, inline comment).

### Surfacing rules
Each integrity state is shown in four places. Only the tray belongs to this part:
- **AI Chat:** Part 2, "Caveats in chat".
- **Sync / Settings card and Dashboard / Insights ribbon:** Part 3, "Integrity surfacing in the UI".

| Surface | `ok` | `suspect` | `alert` | `hard_alert` |
|---|---|---|---|---|
| Tray | — | — | only for the stale-Tally cause: "Restart TallyPrime to refresh" | — |

Three rules every surface follows:
- **`suspect` is invisible.** Showing it means an alarm every time someone posts a voucher mid-capture.
- **Chat is never blocked** by an integrity alert (decision 13). A caveated answer beats no answer;
  blocking is reserved for the first sync.
- **The wording never implies their books are wrong.** The suspicion is always on *our copy*.

Implementation note: there is no notification system today — the only backend→frontend state
channel is the 30s health poll in `TallyStatusBadge.tsx:10`. Integrity state rides on
`GET /api/workspaces/{id}/sync-status` (§5 "Cloud").

### Internal ops signal
Per decision 14: workspace id, rung, cause, mismatch count and magnitude only. **No ledger names, no
party names, no amounts tied to identifiable accounts.** Enough to spot a systemic sync bug across
customers without reading anyone's books (R20, Q6).

## 7. Test environments (team on macOS)

**Short answer: TallyPrime runs on the Mac under Wine, and the agent core runs natively.** That is
how the team already works with live Tally (`README.md`, "Installing TallyPrime on macOS"). Only the
Windows-only pieces (installer, service, tray, updater) and the timing probes need real Windows. The
agent core stays Mac-runnable because those pieces sit behind adapters (§5 "Windows agent").

**Installing on a Mac:**
- **TallyPrime:** under Wine, following `README.md` "Installing TallyPrime on macOS": TallyPrime 7
  Edit Log, XML server set to "Both" on port 9000, checked with `curl http://localhost:9000`.
- **Sync agent:** no Wine. It is plain Python and runs directly on the Mac as a foreground CLI. It
  reaches Tally at `localhost:9000` and keeps its device token in the macOS Keychain via `keyring`.
- **Windows `.exe` installer, service, tray and auto-update:** these do not work under Wine, so they
  are tested only on real Windows (tier C).

| Tier | Where | Used for |
|---|---|---|
| **A — Mac native** | The developer's Mac, no Tally | Daily development: agent core against mock Tally and a local FastAPI; most of §8 and §14 |
| **B — Mac + TallyPrime under Wine** | The developer's Mac, Tally installed per `README.md` | Live Tally: the S0 probes (except timing) and the agent core against real Tally |
| **C — Real Windows x64** | CI `windows-latest`, plus one x64 Windows 10 machine (Q29) | Builds and signing; installer, service, tray and updater; the Windows 10 minimum; SmartScreen / antivirus; timing probes; session, proxy and port edge cases |

**Tier A — Mac native.**
- The agent core is plain Python: sync builders and parsers, change detector, scheduler, gate,
  outbox and uploader.
- It runs against a **v2 copy** of the existing mock Tally (`backend/tally_bridge/mock_handler.py`,
  `tests/mocks/mock_tally_server.py`, copied into `v2/tests/mocks/`), extended there with the stateful
  sync mode in §14, and the local v2 cloud app (`v2/cloud/`).
- The Windows-only adapters are swapped for development ones: a foreground CLI instead of the
  service, and the macOS Keychain via `keyring` instead of DPAPI.

**Tier B — Mac + TallyPrime under Wine.**
- TallyPrime 7 Edit Log runs under Wine on the Mac (`README.md`), with the seed backup
  `seed_data/TDBK1800_100003.001` restored.
- The Mac-native agent core talks to it at `localhost:9000`, the same host/port model `tally_host`
  already uses. There is no VM, network bridge or firewall step, and live probes don't need the
  installer at all.
- **Edition:** the team runs the **Edit Log** edition, but customers may run standard TallyPrime.
  Probes where the edition could matter (7 deleted vouchers, 13 backup restore) get one
  cross-check on standard TallyPrime on tier C.
- The S0 environment check (§12, probe 0) records the Wine version and the Tally edition.

**Wine's limits.**
- Tally Solutions does not support Tally under Wine. If a probe result looks odd, confirm it once on
  tier C before designing around it.
- Timings under Wine are **not representative** of customers' PCs. So probes 9, 20 and 21 (latency,
  "can the accountant keep typing", payload sizes) are **tier C only**.
- The Windows service, the signed installer, the tray, the updater and SmartScreen can't be tested
  under Wine.

**Tier C — real Windows x64.**
- PyInstaller does not cross-compile, so the `--onedir` build, code signing and `--selftest` run on
  the `windows-latest` CI job (§14 "Windows CI").
- At least one x64 **Windows 10** machine, a cloud VM or a physical PC (Q29), covers:
  - the signed installer, Windows service, tray and updater, including rollback
  - a non-admin user (Q31), an office proxy, sleep/resume
  - the Windows 10 minimum (decision 2)
  - SmartScreen and antivirus behaviour on a signed installer (R17)
  - timing probes 9, 20 and 21
  - log off vs disconnect (R11)
  - two Tally instances on one PC (R12)
  - the standard-edition cross-check for probes 7 and 13

**Licensing:** each machine or Wine install running Tally needs a TallyPrime licence. **Educational mode**
limits which voucher dates can be entered (R26). That distorts the change-detection, deletion and
parity probes, so Educational mode is only good enough for installer smoke tests. The S0
environment check records which mode each environment runs.

## 8. Testing the integrity system (per CLAUDE.md "test reality, not an ideal")

- **Unit — one fixture per cause signature**, not one happy path: missing ledger, extra ledger,
  single-ledger diff, offsetting pair, constant opening shift, unbalanced Tally TB, all-zero nominal
  ledgers. Tolerance boundaries at ₹0.99 / ₹1.00 / ₹1.01. An explicit **debit-is-negative** assertion.
- **Fixtures mirror the seed company:** custom sub-groups (`National Creditors` / `Local Creditors`),
  duplicate ledger names under different parents, non-bill-wise parties, cancelled **and** optional
  vouchers.
- **Mock Tally:** stateful sync mode returns a deliberately disagreeing TB → assert detect →
  classify → targeted refetch issued → heal → back to `ok`. Plus a counters-moved run asserting
  `aborted_moving` and **no** alert.
- **Mid-backfill parity (R30) — the regression this design exists to prevent:** with
  `backfill.state = running`, a mock company whose all-time `LEDGER.ClosingBalance` is deliberately
  far from the watermark-bounded computed balance must produce **`ok`**, not an alert. Assert the
  comparison used the watermark-dated TB snapshot as the opening and not the all-time figure, and
  that rung-1 lines are `not_applicable` (not `match`) *in the fallback configuration* — probe 17's
  `ISLEDGERWISE` route is proven (2026-09-23), so the default is now that rung 1 DOES run mid-backfill
  at ledger level; the `not_applicable` path is what a customer's Tally falls back to. Then complete the
  backfill and assert the all-time anchor switches on.
- **Identity changes (§4):** GUID mismatch with a different name → skip, nothing uploaded; same name
  with a new GUID → skip + Re-link prompt, no resync until confirmed; counters below the cursor with
  the same GUID → `restore_detected`, incremental stopped, resync *offered*, never started.
- **DB integration** (`TEST_DATABASE_URL`): parity rows written, retention pruning, cross-tenant
  isolation, `Numeric` round-trip. Plus coverage: `PATCH /coverage` advances `sync_fy_coverage`
  idempotently (a replayed chunk ack doesn't double-count), a `kind = backfill` batch **does not**
  move `last_synced_at` while an `incremental` one does, and the watermark a tool reads matches the
  table after a partial FY.
- **Round-trip E2E:** first sync → `ok` → delete a voucher in mock Tally *without* reconcile →
  parity detects it → reconcile → `ok`.
- **Real-data parity:** computed TB vs the seed company's own TB as captured by S0 (company A, probe 0 / 12);
  seed residuals ₹9,70,537 receivable / ₹18,34,142 payable. *(Corrected 2026-09-22: `tests/fixtures/trial_balance_live.xml`
  is from a different company — NUVANTA — so it is a parser fixture only, never a seed-parity anchor.)*

The refresh round-trip and the Playwright integrity-card tests are UI tests (Part 3, "Verification").

## 9. Sub-projects (each: spec → plan → build, later)
S3 and S5 are in Part 3; S4 is in Part 2. S0–S2 all live under `v2/` (§5 "Code isolation (v2)"):
S0 in `v2/probes/` + `v2/tests/fixtures/sync/`, S1 in `v2/cloud/`, S2 in `v2/agent/`.

| # | Scope | Depends on |
|---|---|---|
| S0 | Live-Tally probe + capture real fixtures (environment check + probes 1–25, incl. parity probes 16–20, full-history 21, forex 22, GST / due dates 23, secured companies 24, masters classification 25) | none |
| S1 | Cloud: sync tables, device auth (incl. one active device), ingest API + ingest rules + ingest limits, heartbeat and snapshot endpoints, sync runs, **parity tables + comparison engine + `/parity` endpoint** (§6) | S0 (batch format; probes 6, 25 for references and masters; probes 16, 17, 18 for parity — decision 11) |
| S2 | Windows agent (installer, extractor, change detector, outbox, service/tray incl. "Sync now" and sign-out, updater, proxy support, **work-priority scheduler**, diagnostics, **parity capture + quiescence counters + remediation execution**, **history backfill walker + pre-emption**) | S0 (parallel with S1; probe 21 for backfill) |

Parity (§6) is deliberately **not** its own sub-project: rung 0 belongs to the S1 ingest path, the
comparison engine is S1, the capture and remediation are S2, and the surfacing is S3/S4/S5. Building
it as a separate slice would mean shipping an ingest path we cannot yet verify.

## 10. Out of scope (v1)
Shared by all three parts.

- **Any change to current code** (`backend/`, `frontend/`, `tests/`, `scripts/`, current migrations). Part 1 is v2 only (§5 "Code isolation (v2)"). Merging v2 into the current code is a later step with its own spec.
- More than one company per PC or per workspace; syncing a company that's loaded but not active.
- Any write to Tally from sync-agent workspaces.
- Live Tally queries from chat **on sync-agent workspaces** (everything comes from our DB). Live-connected and demo workspaces keep today's path (decision 8).
- Real-time tunnel (replaced by sync).
- Billing / pricing for the BI layer.
- Mac/Linux agent; Tally versions older than TallyPrime 7. (A Mac-native agent *core* is used for development only, §7.)
- **History that lives in another Tally company** after a company split: v1 syncs only the bound company, back to its own `books_from` (§4 "Background history backfill", Q32).
- Cost-centre reporting, godown-wise stock and batch-wise stock. Those allocations are kept in `raw` for drill-down but not modelled or aggregated; the Stock Summary snapshot is company-level.
- **Non-Indian companies, and any multi-currency reporting** (decision 15). India-only: Indian FY, GST, Indian number formatting, INR-only figures. Forex vouchers are reported at their INR base amount; the foreign face value is drill-down detail in `raw`, never aggregated. No rate tables, no conversion at read time.

## 11. Risks

### 11.1 Summary
R5, R16 and R25 are in Part 2; R24 and R28 are in Part 3.

| ID | Risk | Likelihood | Impact | Status |
|---|---|---|---|---|
| R1 | Tally must be open to sync | Certain | Medium | **Accepted** |
| R2 | Wrong company's data mixed into workspace | Medium | **Critical** | Handled (GUID check) |
| R3 | Agent slows or freezes Tally | High | **Critical** | Mitigated, prove in S0 |
| R4 | Long-running Tally returns stale data | Medium | High | Mitigated |
| R6 | Incremental change detection misses changes | Medium | High | Mitigated, prove in S0 |
| R7 | Deleted vouchers stay in our DB | High | High | Mitigated (daily compare) |
| R8 | Backup restore / counters reset | Medium | High | Mitigated (full resync offered, never automatic) |
| R9 | Ledger renames and duplicate names | Medium | Medium | Unknown, prove in S0 |
| R10 | Tally setup friction (XML server off) | High | Medium | Onboarding |
| R11 | Tally needs a logged-in Windows session | High | Medium | Onboarding + accepted |
| R12 | Two Tally instances → port conflict | Low | Medium | Detect + message |
| R13 | Company names with `&` / quotes break requests | Medium | High | Code fix + probe |
| R14 | Hindi / Unicode text garbled | Medium | Medium | Test in S0 |
| R15 | Compound units crash export | Low | High | Test in S0 |
| R17 | Antivirus / SmartScreen blocks installer | High | High | Signing |
| R18 | Auto-update breaks or is hijacked | Low | **Critical** | Signed + rollback |
| R19 | Device credential stolen / cross-tenant post | Low | **Critical** | Revocable tokens + isolation |
| R20 | Full books stored in our cloud (privacy) | Certain | High | Open question Q6 |
| R21 | Internet down / duplicate or lost uploads | High | High | Outbox + idempotency |
| R22 | First sync very long or never finishes | Medium | High | Resume + chunks |
| R23 | New FY never synced | Medium | High | Auto-add on 1 April |
| R26 | Tally popups / Educational mode / version differences | Medium | Medium | Error shapes in S0 |
| R27 | Data volume and DB cost for big companies | Medium | **High** (raised by decision 7b) | Measure full books span in S0 (probe 21) |
| R29 | Background backfill never completes | Medium | Medium | **Accepted** — honest progress, degrades gracefully |
| R30 | All-time ledger balances invalid as a parity anchor mid-backfill | **Certain** | High | Watermark-bounded parity; **probe 18 settled 2026-09-23 (TB) and 2026-09-24 (Bills/Stock, plan part 5, both companies): as-on TB, Bills Receivable/Payable and Stock Summary are ALL valid history on a C43-valid date, so no suspension on this ground** |

### 11.2 Detail

**R1: Tally must be open to sync (accepted)**
- *What:* all tools need the company open in Tally. If the accountant closes Tally at 6pm, nothing syncs until it's opened again.
- *Evidence:* every Tally sync tool requires the company open (tally-database-loader, Biz Analyst, Suvit help).
- *Handling:* by design (decision 5) we skip quietly. "Last synced X ago" tells the owner how fresh the data is.
- *Residual:* data can be hours or days old. Acceptable for v1.

**R2: Wrong company's data mixed into the workspace**
- *What:* the accountant opens a different company, and the agent uploads its vouchers into our workspace, mixing two sets of books.
- *Impact:* critical. Wrong numbers, and one client's data shown in another's workspace.
- *Handling:* before every fetch, read the open company's GUID and compare it with `tally_company_guid`. On mismatch, skip. The server also rejects batches whose company GUID doesn't match the workspace.
- *Prove in S0:* probe 2 (cheap GUID read, response when no company is open).

**R3: Agent slows or freezes Tally**
- *What:* heavy XML requests freeze Tally's UI while the accountant is typing. Customers uninstall.
- *Evidence:* Tally's XML server handles one request at a time. tally-database-loader batches day-wise, defaults to 5,000 vouchers, and warns "never >10,000 or export may hang". LESSONS §15: popups freeze the XML server.
- *Handling:* `gate.py` sends one request at a time with per-request timeouts, a cheap health check first, and backoff/circuit breaker after any timeout. Chunks are capped at ~5k vouchers and auto-split to half-month/day when slow. Explicit field lists only; no imports.
- *Prove in S0:* probe 9 (latency/size per chunk, can the accountant keep typing) — on tier C only (§7).

**R4: Long-running Tally returns stale data**
- *What:* Tally left open for days can return old data through XML until it's restarted.
- *Evidence:* tally-database-loader notes.
- *Handling:* parity **rung 2** (§6) asserts that Tally's own TB snapshot balances. If it doesn't, Tally is serving stale data: the run is discarded rather than blamed on us, and the tray says "Restart TallyPrime to refresh".
- *Residual:* can't be fully detected — a stale Tally can still return an internally consistent but outdated TB. Document it in onboarding and support notes.

**R6: Incremental change detection misses changes**
- *What:* we use company counters + `$AlterID > cursor`, and some edits don't come through.
- *Evidence:* tally-database-loader uses the same method, but its own notes call incremental "**not stable**" and it later fixed **missed master changes**.
- *Handling:* the GUID + AlterID compare (daily for the 2-FY window, round-robin for older years — §4 "After a gap") catches misses and refetches mismatches. Parity **rung 1** is the backstop that proves it worked: a missed voucher shows up as a single-ledger (or offsetting-pair) difference, which the cause classifier turns into a targeted refetch, and month-bisect localises it (§6). **During the backfill**, unless probe 16 or 17 gives per-ledger openings, rung 1 is suspended (decision 11); the backstop is then rung 2 at group level, with month-bisect doing the localising. Fallback if AlterID filtering fails: rolling re-pull of recent months + daily compare.
- *Prove in S0:* probes 1, 3, 4.

**R7: Deleted vouchers stay in our DB**
- *What:* a deleted voucher simply disappears from Tally, and no change counter tells us which one.
- *Handling:* the GUID list compare per month/master type (daily for the 2-FY window, round-robin for older years — §4 "After a gap") → `/reconcile` soft-deletes anything missing and returns the ledgers whose mirrored balances must be re-read. Parity **rung 1** independently catches a deletion the compare missed (the ledger's computed balance stays high while Tally's mirrored `ClosingBalance` drops), and its classifier routes the fix back through `/reconcile`. During the backfill, while rung 1 is suspended (decision 11), the same deletion shows up at group level in rung 2 and is localised by month-bisect.
- *Residual:* deletions show up within a day in the 2-FY window, and within one round-robin sweep for older years (§4 "After a gap") — not within 10 minutes.
- *Prove in S0:* probe 7 (vanishes, GUID never reused).

**R8: Backup restore / counters reset**
- *What:* the customer restores an older backup, counters go backwards, and our cursors are ahead of Tally.
- *Handling:* §4 "Company identity changes". Counters below our cursor with the same GUID → stop incremental, caveat the data, and **offer** a full resync (decision 12 — never automatic). A changed GUID is always a skip; a same-name, new-GUID company is only re-linked on the user's confirm (Q25), because a GUID mismatch must never be treated as "our company" (R2).
- *Prove in S0:* probe 13 (do GUID/MasterIDs change on restore).

**R9: Ledger renames and duplicate names**
- *What:* vouchers reference ledgers by name. After a rename, old vouchers may export the new name without their AlterID changing, so our copy keeps the old name.
- *Evidence:* tally-database-loader cascades renames into its voucher tables.
- *Handling:* store ledger GUID; on rename, cascade on the server by GUID. Fixtures must include duplicate names under different parents (CLAUDE.md "fixtures mirror the seed company").
- *Prove in S0:* probe 8.

**R10: Tally setup friction**
- *What:* Tally's XML server is **off by default** (F1 > Settings > Connectivity → "Acts as Server/Both", port 9000). Without it the agent sees nothing.
- *Handling:* an onboarding checklist in the agent (checks the port, shows step-by-step screenshots); the tray shows "Turn on Tally connectivity" when the port is closed but Tally is running.

**R11: Tally needs a logged-in Windows session**
- *What:* Tally is a desktop app. If the user logs off Windows, Tally closes. A Windows service (session 0) can still reach `localhost:9000` while Tally runs in the user's session.
- *Handling:* the onboarding note says to "disconnect, don't log off" for remote/RDP setups. Otherwise accepted under R1.

**R12: Two Tally instances → port conflict**
- *What:* two Tally instances (e.g. two versions) on one PC fight over port 9000, and the agent talks to the wrong one.
- *Handling:* the GUID check (R2) prevents wrong data. The tray shows "Tally port busy / wrong company" and lets the user set the port.

**R13: Company names with `&` or quotes break requests**
- *What:* the company name isn't XML-escaped in `_wrap_voucher_collection` (`request_builder.py:69`), `_wrap_report_envelope` (`:112`) and `build_ledger_vouchers` (`:270`), so a name like "A & B Traders" produces invalid XML.
- *Handling:* the v2 builders always escape (as `:31`, `:179` already do), and the v2 copies of the three affected builders are fixed. The current builders are **not** changed (§5 "Code isolation (v2)"); their bug stays in the current code's own backlog.
- *Prove in S0:* probe 14. **Changed 2026-09-24 (plan part 5, live B): CONFIRMED** — the escaped v2 envelope reaches `Sharma & Sons' Probe Traders` (26 ledgers), an unescaped copy fails as recorded, and an `unknown_company_request` control (a company that isn't loaded) answered rather than erroring (one company loaded, so the variable's own selectivity is unmeasured). R13 holds.

**R14: Hindi / Unicode text garbled**
- *What:* party names, narrations and item names in Hindi or other scripts get corrupted by encoding.
- *Handling:* UTF-8 end to end, with Unicode fixtures.
- *Prove in S0:* probe 15. **Changed 2026-09-24 (plan part 5, live B): CONFIRMED** — the Hindi debtor ledger and its narration (tag 7) round-trip exact (UTF-8 bytes; judged on parsed code-point equality, Ruling Q8); no crash, Tally answers a cheap read after.

**R15: Compound units crash export**
- *What:* stock items with compound units (e.g. Box of 10 Nos) have crashed Tally exports in other tools.
- *Handling:* explicit unit field lists; test on an item with compound units; skip and log if a single item fails.
- *Prove in S0:* probe 15. **Changed 2026-09-24 (plan part 5, live B): CONFIRMED, no crash.** The compound-unit item (A4 Paper Ream, "Box of 10 Nos") reads `10 Box 0 Nos` on its voucher and `20 Box` in the Stock Summary — S1 parses quantities by their leading number (C40).

**R17: Antivirus / SmartScreen blocks the installer**
- *What:* PyInstaller/Nuitka `--onefile` builds are commonly flagged, and unsigned installers get the SmartScreen "unknown publisher" warning.
- *Evidence:* since Aug 2024 an **EV certificate no longer skips SmartScreen**; reputation builds over weeks; Microsoft Artifact Signing is recommended (Microsoft Learn).
- *Handling:* `--onedir` build, code signing (Microsoft Artifact Signing), a consistent publisher name, and a support note for early customers. Tested on real x64 Windows (§7, tier C).
- *Residual:* install friction for the first few weeks of customers.

**R18: Auto-update breaks or is hijacked**
- *What:* a bad update bricks every agent, or an attacker pushes a malicious update to PCs holding full books.
- *Handling:* signed updates, sha256 check, `--selftest` after update, automatic rollback, staged rollout.

**R19: Device credential stolen / cross-tenant post**
- *What:* the long-lived device token can read the full books. A bug could let one device post into another customer's workspace.
- *Handling:* the token is stored with DPAPI and the refresh token is rotating and revocable (hash stored). `get_current_device` allows posts only to the device's own workspace. DB tests cover the cross-tenant block and a revoked device getting 401.

**R20: Full books stored in our cloud (privacy)** — shared by all three parts
- *What:* we hold complete accounting data plus `raw` JSONB, including party names, amounts and narrations.
- *Handling:* TLS everywhere; encryption at rest and access policy to decide (Q6).
- *Compliance:* party ledgers often name individuals (sole proprietors, customers), so the synced books include personal data under **India's Digital Personal Data Protection Act, 2023**. Its obligations (purpose, retention, deletion on request, breach notice) are an input to Q5 and Q6.
- *On the PC:* the agent keeps business data only in its outbox, and only until the server acks it (§5 "Windows agent").

**R21: Internet down / duplicate or lost uploads**
- *What:* the upload fails mid-batch, is retried and duplicates data, or batches are lost.
- *Handling:* local outbox, a `batch_id` idempotency key, the cursor advances only after server ack, and upsert with the `alter_id >=` rule. The round-trip E2E restarts the agent and asserts no duplicates.

**R22: First sync very long or never finishes**
- *What:* a big company takes hours, and Tally closes midway, again and again.
- *Handling:* resume from saved chunk status, month chunks auto-split, and a progress screen. What the web shows before and during this is open question Q2 (Part 3).

**R23: New FY never synced**
- *What:* the sync range stays fixed to the first two FYs, so April onwards is missing.
- *Handling:* auto-add the new FY on the first connected cycle on/after 1 April (§4). Unit test with a fake clock.

**R26: Tally popups / Educational mode / version differences**
- *What:* an open popup blocks XML (LESSONS §15), Educational mode limits dates, and older Tally versions return different shapes.
- *Handling:* the gate treats these as "skip + backoff". Record the error shapes in S0 (probe 10). v1 supports TallyPrime 7+ only. Test environments record licensed vs Educational mode (§7).

**R27: Data volume and DB cost**
- *What:* a large trader has hundreds of thousands of vouchers, and `raw` JSONB doubles storage. **Decision 7b (backfill all history to books start) makes this materially worse** — a twelve-year-old company is roughly 6× the two-FY estimate, and it is unbounded by design.
- *Handling:* measure bytes per month chunk in S0 (probe 9) and extrapolate to the **full books span** (probe 21), not just 2 FYs, before S1. Decide whether `raw` is kept permanently — and specifically whether it is kept for backfilled (older) years, where it is least likely to be needed. Q22 tracks this.
- *Residual:* an unusually old or high-volume company may need a per-customer storage ceiling. Not designed for v1; revisit once probe 21 gives real numbers. **Probe 21 (2026-09-24, company B, Wine/Educational — headline):** full-history reach confirmed at least 3 FYs back (238/238 vouchers exact, month by month, decision 7b); `raw` JSON is 86.1% of per-voucher storage, and at 200k vouchers/yr the 10-year span is 15137.5 MB with `raw` vs 2100.5 MB without — Q22/Q23 numbers are in (§15); the ceiling decision itself is unchanged, still open. *(Note 2026-09-25, S0 exit gate: these are the 2026-09-24 figures; the 2026-09-25 re-run moved each by under 0.5% — e.g. 15081.4 MB / 2094.2 MB — see §15 Q22.)*

**R29: Background backfill never completes**
- *What:* the backfill only advances on cycles where nothing else needs doing and Tally is open. A customer who opens Tally for twenty minutes a day, or whose books go back fifteen years, may never reach `books_from`. The UI would sit at "still loading" indefinitely.
- *Likelihood:* Medium. *Impact:* Medium — chat and dashboards work throughout; only deep history is missing.
- *Handling:* it is a deliberate consequence of decision 7b's pacing (fresh data always wins, R3 is the bigger risk). Settings shows honest progress ("2019-20 onwards still loading, 60%") and an estimated remaining span rather than a false ETA. If progress stalls for N days with the agent otherwise healthy, Settings suggests leaving Tally open longer.
- *Residual:* accepted. A never-finished backfill degrades gracefully; a frozen Tally does not.

**R30: All-time ledger balances are not a valid parity anchor mid-backfill**
- *What:* Tally's `LEDGER.ClosingBalance` is an **all-time** figure. Our computed balance only covers the synced window. While the backfill is incomplete these legitimately differ, and a naive rung-1 comparison would raise an integrity alert on every ledger of every customer, every day.
- *Impact:* High — it would make the integrity system worse than useless during the period it is most needed.
- *Handling:* §6 — while `backfill.state != complete`, the opening balance at the watermark comes from a TB snapshot as-on the day before the watermark. Our lines from the watermark onward are added to it, and the result is compared with Tally's current balance. The TB is group-level, so this runs at group level (rung 2) and rung 1 is suspended, unless per-ledger openings come from ~~probe 16 (ledger `OpeningBalance` readable as-on a date) or~~ probe 17 (a ledger-level TB). *(Corrected 2026-09-25, S0 exit gate: probe 16's route is struck — a Ledger collection's `OpeningBalance` is current-FY, and a typed `SVTODATE` before the current period clamps to its start (C45); probe 17's `ISLEDGERWISE=Yes` TB is the per-ledger route and is available, so rung 1 is not suspended — §6 "The opening anchor".)* Once history is complete, the books-start opening balance replaces the watermark anchor *(read from the ledger-level TB / Stock Summary as-on the books start, probe 18's route — never a master `OpeningBalance`)*. The Settings card states the verified span.
- *Prove in S0:* probe 18 (does a TB as-on a past date return correct historical values?). **Settled live 2026-09-23: yes.** Tally honours `SVFROMDATE`/`SVTODATE` on `TYPE=Data` reports, and every primary group reconciles to the vouchers once (a) the nominal ledger is taken from `ALLLEDGERENTRIES.LIST` only and (b) the stock-bearing group's static `Opening Stock` row is added to the ledger rollup. **The as-on TB is a valid parity anchor during the backfill; R30 needs no suspension on this ground.** **Changed 2026-09-24 (plan part 5, live B): re-confirmed on a past-FY date** — company B's TB as-on 31-03-2023 (a closed prior FY's end) matched the dataset for all 5 primary groups the same way. **Also settled: Bills Receivable, Bills Payable and Stock Summary as-on a valid date ARE history too** — the 2026-09-23 "ignore the as-on date" finding was a Ruling C43 artefact (an Educational-ignored 30-09-2025); at the C43-valid 31-10-2025 all three matched exactly (company A). Bills/Stock Summary as-on a valid date need no vouchers-based workaround; only an Educational-mode non-1/2/31 date does.

## 12. S0 probe list (on restored seed backup `seed_data/TDBK1800_100003.001`)
Every probe **saves the raw Tally responses as fixtures** under `v2/tests/fixtures/sync/` for the unit tests. Probe scripts live in `v2/probes/` and use v2 code only (§5 "Code isolation (v2)"). Probes marked **tier C only** must not be timed under Wine (§7).

0. **Environment check (before probe 1):** TallyPrime runs under Wine on the Mac per `README.md` and `curl http://localhost:9000` answers. The seed backup restores. The Wine version, the Tally edition (Edit Log or standard) and licensed vs Educational mode are recorded (R26). If Tally under Wine fails, S0 moves to tier C (§7, Q29).
1. Company GUID, AltVchId, AltMstId, BooksFrom, LastVoucherDate: do they change on voucher create/alter/delete and master alter? (R6)
2. **Cheap read of the open company's GUID; the response when no company is open.** (R2)
3. Voucher GUID/MasterID/AlterID/IsCancelled/IsOptional/IsPostDated/Reference can be fetched. (R6, R16)
4. The `$AlterID > N` filter on Voucher/Ledger/Group/StockItem works, doesn't crash Tally, and is fast enough. (R6)
5. SVFROMDATE/SVTODATE bound a Voucher collection? Safe `$Date` filter alternative.
6. Nested BILLALLOCATIONS, ISDEEMEDPOSITIVE, inventory/batch lines are returned. Can a ledger entry export the ledger's **GUID**, not just its name (§5 "Cloud", ingest rules)? (R5, R9)
7. A deleted voucher vanishes; GUID never reused. (R7)
8. Ledger rename: do old vouchers' names change? Is AlterID bumped? Does the GUID stay stable? (R9)
9. Latency and size per chunk; can the accountant keep typing during a heavy query? (R3, R27) **Tier C only.**
10. Error shapes: Tally closed, company closed, Educational mode, popup open. (R26)
11. Opening balances/bills, stock opening qty/rate/value. (R5) **Changed 2026-09-24 (plan part 5, live B): FAILED, stock only.** ~~Ledger `OpeningBalance` and~~ the debtor's opening bill CONFIRMED against books-start values. *(Corrected 2026-09-25, S0 exit gate: the ledger half was never CONFIRMED — the probe 11 re-run under C46 (run 2026-09-24 20:57) is **DIFFERENT**: ledger `OpeningBalance` is the **current-period** (current-FY) opening, 14/25 ledgers at the current-FY opening, agreeing with probe 16 B's `opening_scope = fy`. Books-start anchors come from the TB / Stock Summary as-on the books start, probe 18's route. See the header's "Changed 2026-09-25 (plan part 6)".)* Stock `OpeningBalance`/`OpeningRate`/`OpeningValue` FAILED — **new finding, Ruling C46**: `StockItem.OpeningBalance` is the current period's opening, not books-start (all 5 items = the dataset's stock at 31-03-2025). A books-start stock opening needs a historical report instead (probe 18's route), never the StockItem master.
12. Report snapshots via SVCurrentCompany at today's date. (R5)
13. Backup restore: do the company GUID and MasterIDs change? (R8)
14. **Company names with `&` / quotes / apostrophes:** escaped requests work, and unescaped ones fail as expected. (R13) **Changed 2026-09-24 (plan part 5, live B): CONFIRMED** — escaped works (26 ledgers incl. the Hindi one), unescaped fails, an unknown-company control answers without erroring (one company loaded), Tally alive after.
15. **Hindi/Unicode names and narrations + a stock item with compound units:** export intact, no crash. (R14, R15) **Changed 2026-09-24 (plan part 5, live B): CONFIRMED** — Hindi ledger + narration (tag 7) round-trip exact; compound-unit item reads "10 Box 0 Nos" on its voucher and "20 Box" in Stock Summary; no crash.
16. **Ledger closing balances (parity rung 1, and the base of every computed balance in chat — Part 2 "Rule 1"):** does `LEDGER.ClosingBalance` from `TYPE=Collection` equal Tally's TB screen? Is it `0` for *all* nominal ledgers? Is `OpeningBalance` FY-scoped or books-scoped — and **can it be read as-on an arbitrary date** (e.g. via `SVFROMDATE`)? **Settled live 2026-09-23: no** — `SVFROMDATE` freezes Tally's XML server on a master collection and `SVTODATE` is silently ignored, so per-ledger opening anchors during the backfill come from probe 17 (decision 11, §6 "The opening anchor"); probe 16 no longer sends `SVFROMDATE` unless the operator passes `--allow-risky`. **Which date is `ClosingBalance` as-of (today, or the end of the current period), and does it include post-dated vouchers?** (§6 "Rung 1") (§6, R5, R30) **Also needed by Part 2.** **Corrected 2026-09-24 (plan part 5, typed re-run, both companies): the 2026-09-23 "no" was measured untyped.** Typed, SVTODATE **is** honoured **inside the current period** (A: 19/22 ledgers moved) but silently clamps to the current period's start for an earlier date (**Ruling C45**, B: FAILED) — so it still cannot anchor the backfill (a past period), only same-period as-on reads. Per-ledger backfill anchors remain probe 17's route (or a dated TB, probe 18 B).
17. **Ledger-level TB:** can an exploded (ledger-level) TB be exported via `TYPE=Data` safely and fast? **Settled live 2026-09-23: yes — `ISLEDGERWISE=Yes`** (29 ledger rows, ~7.3 KB, ~18 ms; every primary group reconciles). Two caveats: one non-ledger row (`Opening Stock`) comes with it, and `EXPLODEFLAG`/`EXPLODEALLLEVELS` stop at the second group level so they miss ledgers under custom sub-groups. Rung 2 gains ledger resolution and month-bisect gets cheaper. (§6, R3)
18. **Historical reports (now load-bearing):** does a TB as-on a *past* month-end return correct historical values — and do **Bills Receivable/Payable and Stock Summary** as-on a past date too? Required for month-bisect, for the routine month-end snapshots (§4) and the dashboard's "vs last month-end" baselines, for answering historical dates from snapshots, **and — since decision 7b — as the only valid parity anchor while the backfill is incomplete** (and the base of the probe-16 fallback for chat). **Settled live 2026-09-23, split:** the **TB part passes** — an as-on TB is correct history and a valid parity anchor (no R30 suspension) — while **Bills Receivable/Payable and Stock Summary ignore the as-on date** and return the current position, so those tiles have no month-end comparison from a dated report and must be computed from vouchers. (§4, §6, **R30**) **Also needed by Parts 2 and 3.** **Changed 2026-09-24 (plan part 5, live A + B): CONFIRMED, no split — the 2026-09-23 "Bills/Stock ignore the as-on date" half is superseded.** It used 30-09-2025, an Educational-ignored day (Ruling C43); re-measured at the C43-valid 31-10-2025, TB, Bills Receivable, Bills Payable and Stock Summary **all** matched that as-on date exactly (company A), and company B's TB as-on 31-03-2023 matched the dataset too. All four report reads are valid historical snapshots on a valid date; no vouchers-based workaround is needed except on an Educational-mode non-1/2/31 date.
19. **Counter stability:** do `AltVchId` / `AltMstId` hold still across a multi-call capture window? This is the quiescence guard that prevents false parity alerts. (§6, R6)
20. **Parity cost:** latency and payload size of a full ledger list + TB on a large company. (R27) **Tier C only.**
21. **Full-history reach (decision 7b):** read `books_from` / the oldest available FY; can vouchers be fetched for an FY several years back, at what latency and payload size, and do closed/audited years behave differently? Extrapolate total storage across the whole books span. (R27, R29) **Tier C only** for the latency and size figures.
22. **Forex vouchers (decision 15):** on a foreign-currency sale/purchase (export/import — an Indian company can still have these), does the voucher's ledger line expose the **INR base amount** alongside the foreign face value, and in which fields? Confirms that aggregating the base amount is safe and nothing needs conversion at read time. **If the base amount is not exposed, decision 15 must be revisited.** (decision 15) **Also needed by Part 2.** **Changed 2026-09-25 (plan part 7, live B): CONFIRMED** — the base is stated in the AMOUNT expression; decision 15 holds. New: C47 (forex ledger valued at the latest voucher rate) and expression-form ledger balances — see the header.
23. **GST classification and due dates:** do ledger masters expose Tally's GST classification (`TaxType`, `GSTDutyHead` or equivalent), so tax ledgers can be identified without name matching? Do Bills Receivable/Payable exports (and bill allocations) carry a due date or credit period, so overdue can be split without guessing? If either is missing, the GST tile or the overdue split is dropped from v1, never approximated. **Needed by Part 3** (GST position tile, overdue split).
24. **Secured companies:** a company with Tally security (username/password) or TallyVault encryption, once it is open in Tally. Does XML export still work without extra credentials, and do the error shapes differ? (R2, R26)
25. **Masters classification:** can Group export its primary group / nature, `IsRevenue` and `AffectsGrossProfit`, and can VoucherType export its parent and base type? Without them, balance-sheet and P&L ledgers can't be told apart (§6 "Rung 1") and custom voucher types can't be summed (§5 "Cloud"). (R5, R16)

## 13. Code gaps (verified 2026-09-15 and 2026-09-18)
The query-side gaps (freshness metadata, the bill-date fallback, the stock key mismatch) are in Part 2, "Code gaps".

These are gaps in the **current** code. Part 1 doesn't fix them there (§5 "Code isolation (v2)"). The v2
copies must not inherit them, and each one gets a v2 unit test proving it's gone.

- Company name not XML-escaped in `_wrap_voucher_collection` (`request_builder.py:69`), `_wrap_report_envelope` (`:112`), `build_ledger_vouchers` (`:270`). Escaped correctly at `:31` and `:179`.
- `build_list_groups` (`:223`), `build_list_stock_items` (`:226`), `build_list_stock_groups` (`:230`) and `masters.list_groups/list_stock_items/list_stock_groups` take no company param.
- `parse_vouchers` (`response_parser.py:291`) drops IDs, bill allocations, cancelled/optional flags.
- `TallyClient` has a single 90s timeout (`client.py:14`) and no retries. No ALTERID/MASTERID/GUID is fetched anywhere today.
- **`parse_amount` returns `0.0` for anything unparseable** (`response_parser.py:19-26`), turning a parse failure into a legitimate-looking zero. That can produce a **false parity match** or a silently wrong balance. Sync parsers must distinguish missing from zero and reject the batch.
- **Money is float everywhere.** Sync tables and parity comparisons need `Decimal` / `Numeric(18,2)`.
- The TB report is **group-level only** and `_wrap_report_envelope` (`request_builder.py:111`) sets no explode flag — see probe 17.

## 14. Verification (when built, later)
Each check runs on the tier named in §7 (A = Mac native, B = Mac + TallyPrime under Wine, C = real Windows x64). Query-side checks are in Part 2, "Verification"; UI checks in Part 3, "Verification".

- **Unit (tier A):** sync builders (escaping, no `*`, no `$$InDateRange`) and parsers on S0 fixtures from `v2/tests/fixtures/sync/` (custom creditor sub-groups, non-bill-wise parties, duplicate names, cancelled/optional, Unicode, compound units); chunk splitter; change detector; outbox; gate (fake clock); new-FY rollover (fake clock); **work priority** (§4): a cycle with tier-1 work runs nothing else, tiers 2–5 consume the budget top-down, tiers 6–7 run only on an otherwise idle cycle, a timeout ends the cycle's work, and internet-down runs only tier 1's fetch; deletion compare covers the 2-FY window daily and rotates through older FYs; dates and FY boundaries come from Tally dates, never from UTC timestamps.
- **Backfill (decision 7b, tier A):** newest-first ordering across FYs and within an FY; **pre-emption** — a cycle with pending incremental work, a non-empty outbox, a parity remediation, the daily pass or a month-end capture runs **no** backfill chunk; watermark advances only after server ack; interrupted backfill resumes rather than restarts; terminates exactly at `books_from` and never runs again once `complete`, unless a whole-company resync re-walks it; a back-dated voucher landing in a `pending` FY is stored but that FY still answers "still loading"; an FY rollover extends the forward edge without restarting the backfill. Fake clock throughout.
- **`resyncing` state transitions (§4, tier A):** after a restore, every previously `complete` year goes `resyncing` (not `pending`), is skipped by parity, returns to `complete` per FY on the last acked chunk, and then gets exactly one parity run; years still `pending` before the restore stay `pending`; a half-loaded `running` year goes back to `pending`; masters are re-fetched before the 2-FY pass. Assert both edges: the available edge counts `resyncing`, the verified edge does not. (How queries answer in these states is Part 2's test.)
- **Mirrored re-read (tier A):** a cycle that syncs a voucher re-reads `ClosingBalance` for exactly the ledgers it touches (and `ClosingValue` for touched stock items). The same holds for vouchers drained from the outbox and for vouchers soft-deleted by the deletion check.
- **Month-end snapshots (§4, tier A):** captured as-on the month-end date even when captured late (fake clock); a re-capture replaces rather than duplicates; month-bisect reuses an existing month-end snapshot instead of re-fetching it.
- **Mock Tally (tier A):**
  - Stateful sync mode in the v2 copy of the mock handler, in `v2/tests/mocks/` (counters, AlterID/date filters, mutate/delete hooks).
  - **Latency / hang / error injection** in the v2 copy of `mock_tally_server.py`.
  - Skip cases: Tally offline, no company open, GUID mismatch.
  - Resume after an interrupted first sync.
- **DB integration (`TEST_DATABASE_URL`, tier A):** idempotent batch replay, older alter_id never overwrites, lines replaced not duplicated, reconcile soft-deletes (and returns the ledgers to re-read), cross-tenant post blocked, revoked device 401, run progress, batch with wrong company GUID rejected, oversize batch 413, per-device rate limit 429.
- **Ingest and agent rules (tier A):**
  - masters are uploaded before vouchers
  - a voucher naming an unknown ledger rejects the batch with `missing_master`; the agent refetches masters and the retry succeeds
  - a mirrored balance never moves backwards in `balance_captured_at`
  - a heartbeat updates `last_seen_at` but never `last_synced_at`
  - a snapshot upserts on `(report_type, as_on_date)`
  - the outbox is empty once every batch is acked
  - a 401 stops uploads, keeps the outbox and shows "Signed out"
  - a second device binding the same company is refused until the take-over is confirmed
  - master deletions are soft-deleted by the daily pass
  - post-dated vouchers follow probe 16's rule in rung 1
  - "Sync now" runs one normal cycle
  - a company younger than two FYs gets fewer progress units and `backfill.state = complete` at `ready`
- **Isolation check (tier A):** a v2 test fails if any file under `v2/` imports from `backend`, `tests` or `scripts`, and `git diff` for the Part 1 work shows no path outside `v2/` and `docs/`.
- **Round-trip E2E (tier A):** agent in-process vs v2 mock Tally + the v2 cloud app (ASGITransport): first sync → assert whole DB state = mock company → mutate → incremental → re-assert → delete → reconcile → re-assert → internet down mid-upload → outbox drains → restart agent → no duplicate uploads.
- **Parity:** see §8 for the full plan (one unit fixture per cause signature, tolerance boundaries, debit-is-negative assertion, mock-Tally detect→classify→heal cycle, counters-moved `aborted_moving` run, retention + cross-tenant DB tests). Anchors: DB-computed TB vs the seed company's TB captured by S0 (not `tests/fixtures/trial_balance_live.xml`, which is another company); seed residuals ₹9,70,537 receivable / ₹18,34,142 payable.
- **Live Tally (tier B):** the S0 probes except the tier-C-only timings, plus the agent core on the Mac talking to Tally under Wine.
- **Windows CI (tier C):** `windows-latest` runs agent tests, PyInstaller `--onedir` build, `--selftest`, service install/uninstall.
- **Manual:** TallyPrime 7 + seed backup, covering:
  - A company with Tally security or TallyVault (probe 24, tier B)
  - Tally restarted mid-sync (tier B)
  - Non-admin user: the installer asks for an administrator once, and the agent then runs without one (Q31, tier C)
  - Behind a proxy (tier C)
  - Antivirus / SmartScreen (tier C)
  - Sleep/resume (tier C)
  - Updater rollback (tier C)
  - Log off vs disconnect (R11, tier C)
  - Two Tally instances (R12, tier C)
  - Probes 7 and 13 on standard TallyPrime, not Edit Log (§7, tier C)

## 15. Open questions (decide before specs)
Q7, Q8, Q26 and Q27 are in Part 2. Q2, Q3, Q9, Q11–Q18 and Q24 are in Part 3. **Q11** (where the
company is picked) sits in Part 3 but decides this part's setup flow (§4 "Setup"), so settle it
together with this part's questions.

| # | Question | Linked risk | Options / notes |
|---|---|---|---|
| Q1 | **Agent offline vs Tally closed:** should the agent send a lightweight heartbeat on skipped cycles too (e.g. "alive, Tally closed")? | R24 | Yes → web can show "Tally closed" vs "Agent offline"; costs one tiny request per cycle |
| Q4 | **Changing company or PC:** the user picked the wrong company, moves to a new PC or reinstalls. How is the saved company re-bound? | R2, R19 | e.g. "Change company" in agent → new workspace, or re-bind with a confirm; old device revoked |
| Q5 | **Removing data:** what happens to synced data on disconnect, uninstall or account deletion? | R20 | Keep read-only / delete after N days / delete immediately |
| Q6 | **Data privacy:** encryption at rest, who at our side can see the books, keep `raw` JSONB or not? | R20, R27 | Must satisfy the DPDP Act, 2023 (R20) |
| Q10 | **Out-of-scope confirmation:** is §10 complete (multi-company, writes, live queries, billing, Mac/Linux, older Tally)? | — | |
| Q19 | **Parity tolerance:** flat ₹1.00 per line (`PARITY_TOLERANCE_PAISE=100`), or percentage-based for large balances? A ₹1 tolerance on a ₹4 crore ledger is effectively exact; on a ₹500 ledger it is 0.2%. | R5 | Flat is simpler and catches more; decide after probe 20 shows real diff magnitudes |
| Q20 | **Rung 3 in or out for v1?** Decision 11 defers it. Does Q8 (previous-FY closing snapshot, Part 2) change that — i.e. do we need statement-level parity to trust a stored FY close? | R5, Q8 | Revisit when Q8 is settled |
| Q21 | **Parity retention:** proposed 90 days for runs, 7 days for matching lines, 90 for mismatching. Enough history to debug a recurring drift without storing a daily full ledger dump forever? | R20, R27 | Size it against probe 20's ledger counts |
| Q22 | **Is `raw` JSONB kept for backfilled years?** Decision 7b makes storage unbounded. Keeping `raw` for the recent 2 FYs but dropping it for older backfilled years would roughly halve the marginal cost of deep history. | R27, R29 | Decide after probe 21. **Numbers (probe 21, 2026-09-24):** `raw` is 86.1% of per-voucher storage; per-FY cost at 10k/50k/200k vouchers-a-year, MB with `raw` = 75.7/378.4/1513.7, without = 10.5/52.5/210.1; saving from dropping `raw` beyond the recent 2 FYs, at 5 yr = 195.6/977.8/3911.1 MB, at 10 yr = 521.5/2607.4/10429.5 MB (`storage.q22`, `v2/probes/results/results.json`). At 200k vouchers/yr × 10 yr: 15137.5 MB with `raw` vs 2100.5 MB without vs 4707.9 MB keeping `raw` for the recent 2 FYs only. These are inputs, not the decision — it stays with the user. **DECIDED 2026-09-24 (user): keep `raw` for the recent 2 FYs only; older backfilled years store the structured columns only (re-fetch from Tally if ever needed).** **Re-run 2026-09-25 (S0 exit gate note):** probe 21's B re-run (plan part 7, 238 unflagged vouchers sized, FY 2022-23 now 240 incl. the USD sales) moved every figure by under 0.5%, in the decision's favour — 1050.3→1047.1 B/voucher without `raw`, 6518.5→6493.6 B `raw` JSON, `raw` share 86.1%→86.1%; at 200k/yr × 10 yr 15137.5→15081.4 MB with `raw`, 2100.5→2094.2 MB without, 4707.9→4691.6 MB `raw` for the recent 2 FYs only; the 200k/yr month chunk 626.9→623.9 MB, still `over_chunk_cap: true`; 10k/50k/yr still under the cap (`docs/bi-s0-exit-gate-2026-09-25.md` item 4). **The decision holds.** |
| Q23 | **Does the backfill need a floor?** Decision 7b says all history to books start. Do we want a safety ceiling (e.g. stop at 10 FYs, or at a storage budget) for pathological companies, and what does the UI say when it stops early? | R27, R29 | Revisit after probe 21 gives real numbers. **Numbers (probe 21, 2026-09-24):** per-extra-FY cost at 10k/50k/200k vouchers-a-year = 10.5/52.5/210.1 MB without `raw`, 75.7/378.4/1513.7 MB with (`storage.q23`); month-chunk XML size at 200k vouchers/yr is 626.9 MB and exceeds the ~5k-row chunk cap (`over_chunk_cap: true`) — a floor or a smaller chunk unit is needed at that volume; 10k and 50k/yr stay under the cap. At 200k vouchers/yr × 10 yr: 15137.5 MB with `raw`, 2100.5 MB without (same table as Q22). The decision on a floor stays with the user. **DECIDED 2026-09-24 (user): no year floor — decision 7b stands (all history to books start); large companies are handled by splitting month chunks into day chunks (probe 5 confirmed day windows) plus a per-company storage alert, not a hard stop.** **Re-run 2026-09-25 (S0 exit gate note):** probe 21's B re-run (plan part 7, 238 unflagged vouchers sized, FY 2022-23 now 240 incl. the USD sales) moved every figure by under 0.5%, in the decision's favour — 1050.3→1047.1 B/voucher without `raw`, 6518.5→6493.6 B `raw` JSON, `raw` share 86.1%→86.1%; at 200k/yr × 10 yr 15137.5→15081.4 MB with `raw`, 2100.5→2094.2 MB without, 4707.9→4691.6 MB `raw` for the recent 2 FYs only; the 200k/yr month chunk 626.9→623.9 MB, still `over_chunk_cap: true`; 10k/50k/yr still under the cap (`docs/bi-s0-exit-gate-2026-09-25.md` item 4). **The decision holds.** |
| Q25 | **Re-link on a new GUID:** is "same company name, different GUID → offer Re-link" (§4 "Company identity changes") the right trigger? Name matching can be fooled by two companies with the same name (e.g. one per FY split). Should re-link need the website password, or just a tray click? | R2, R8 | Default: tray + web prompt, website password required, never automatic |
| Q28 | **Who can see a synced workspace?** v1 assumes the existing workspace ownership model: the account that bound the company. Owners often want their accountant on the same workspace, or the other way round. Share with other logins, and with what roles? | R19, R20 | Decide before the S3 spec (Part 3) |
| Q29 | **Tier C machine (§7):** which x64 Windows machine do we use for everything the Mac can't cover — the installer, service, tray and updater, the Windows 10 minimum, SmartScreen checks and the timing probes — a cloud VM or a physical PC? | R3, R17 | Needed before S0's timing probes (9, 20, 21) and the first installer build |
| Q30 | **Several PCs, one company:** Tally on a shared server reached from several PCs, TallyPrime server / multi-user installs, or the same company on the owner's and the accountant's machines. Is "one active device per workspace, take-over on confirm" (§4 "Setup") enough, or should one designated PC always be the syncing one? | R2, R21 | Default: one active device, take-over prompt; server installs run the agent on the server |
| Q31 | **Admin rights at install:** the service needs an administrator once. Is that acceptable for our customers, or do we also need a per-user mode (no service, runs only while the user is logged in)? | R11, R17 | Default: admin once, service mode only |
| Q32 | **Split companies:** when a company has been split by year, do we later let a workspace link the older Tally company so history continues past the split? | R29 | Default: not in v1; the History card states where this company's books start |

Q1, Q4, Q5, Q25, Q28 and Q30–Q32 are product decisions to settle first, together with Q11 (Part 3); Q29 before S0's timing probes. Q6, Q10 and Q19–Q23 can be settled in the S1/S2 specs, though **Q22/Q23 need probe 21's numbers before S1 commits to a schema**.

## 16. Next step
Brainstorm only so far. When approved: settle Q1, Q4, Q5, Q11 (Part 3), Q25, Q28 and Q29–Q32 → write the S0 live-Tally probe spec (environment check + all probes 1–25, including parity probes 16–20, full-history probe 21, forex probe 22, GST / due-date probe 23, secured companies probe 24 and masters classification probe 25) → S1 and S2 specs → implementation plans. Part 2 (S4) builds on S1; Part 3 (S3, S5) on S1 and Part 2.

The §6 integrity design rests on four probe results (16, 17, 18, 19). Three are load-bearing enough
to run first:

- **Probe 16** — if `LEDGER.ClosingBalance` does **not** agree with Tally's own TB, rung 1 collapses
  and parity falls back to group level only, making month-bisect the primary localisation tool and
  changing the cost profile materially. Chat's computed balances also lose their base and fall back
  to group-level forward computation from stored month-end TBs (Part 2, "Rule 1"). **Settled 2026-09-24
  (typed re-run; noted 2026-09-25, S0 exit gate): it agrees** — balance-sheet ledgers' `ClosingBalance` matched their
  lines (A: 0 mismatches over 50 vouchers; B: 19/19), as of the current period's end with post-dated vouchers
  counted; nominal ledgers are non-zero, so rung 1 selects by group nature. Rule written into §6 "Rung 1". Forex
  ledgers differ by the unrealised difference (C47).
- **Probe 18** — if a TB as-on a past date is unreliable, there is **no valid parity anchor while
  the backfill is incomplete** (R30), and parity would have to be suspended until full history
  lands. **Settled 2026-09-23 (TB) and 2026-09-24 (Bills/Stock, plan part 5): all four report reads
  are reliable on a valid date**, so this cost does not land — the 2026-09-23 "bills/stock must be
  computed from vouchers" fallback was a Ruling C43 artefact of the date used, not a genuine gap.
- **Probe 17, or probe 16's as-on reading** — decides whether rung 1 runs at all during the
  backfill (decision 11). **Settled 2026-09-23: probe 17's `ISLEDGERWISE=Yes` gives per-ledger
  openings**, so rung 1 runs mid-backfill; this cost does not land either. **Probe 16's as-on route,
  re-measured typed (plan part 5, 2026-09-24), turns out to work inside the current period but not
  before it (Ruling C45) — it still doesn't substitute for probe 17 during the backfill.**

**Probe 21** should also run early: decision 7b makes storage unbounded, and Q22/Q23 (drop `raw` for
backfilled years? cap the depth?) must be settled before S1 commits to a schema.
