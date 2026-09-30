# S1 — Cloud: sync tables, device auth, ingest, parity (BI Part 1, v2)

> **Design spec — built (S1, 2026-09-29, `v2/cloud/` + `v2/contract/`).** Written before the code; the
> "Changed 2026-09-29" block below records where the build changed it.
> **Written 2026-09-25 on the user's standing instruction** ("you can do everything yourself"): every design
> decision was taken without waiting for the user and is listed for review in §2 "Decisions taken on the user's
> standing instruction (2026-09-25) — review". Each one names the alternative and what it costs to reverse.
> **Parent spec:** [`2026-09-21-bi-part1-sync-design.md`](2026-09-21-bi-part1-sync-design.md) (Part 1). S1 is the
> Part 1 sub-project in its §9: cloud sync tables, device auth (incl. one active device), ingest API + ingest rules
> + limits, heartbeat and snapshot endpoints, sync runs, parity tables + comparison engine + `/parity` endpoint.
> **Inputs:** Part 1 §2 decisions, §4, §5 (incl. "Code isolation (v2)"), §6 (incl. the 2026-09-25 exit-gate text:
> Rung 1 rule, C45, C47, books-start anchors from a TB / Stock Summary as-on the books start), §8, §11, §13, §15;
> the S0 probe spec [`2026-09-22-bi-s0-probes-design.md`](2026-09-22-bi-s0-probes-design.md); the S0 exit gate
> [`../bi-s0-exit-gate-2026-09-25.md`](../bi-s0-exit-gate-2026-09-25.md) (exceptions (d), (f), (g));
> [`LESSONS.md`](../../LESSONS.md) §15 rules 17–30; the real captures in `v2/tests/fixtures/sync/`.
> **Changed 2026-09-28 (S1 build, Task 10c review — controller ruling):** §10.5(b) compares the **change** of the
> TB's `Unadjusted Forex Gain/Loss` row since the anchor date E, not its as-on value: an E−1 anchor row is already
> revalued by Tally and the row is cumulative (live company B's FY 2025-26 TBs still carry −183.87), so the as-on value
> alone falsely rejects every run with E > books_from. Forex `unrealised` / `match_revalued` now mean "since E". Other
> S1 build rulings awaiting their spec edit (Task 15) are listed in the tracker change log and the SDD ledger.
> **Changed 2026-09-29 (S1 build — as-built edits, Task 15):** S1 is built (`v2/cloud/`, `v2/contract/`). The
> spec now describes the code; each change is marked inline "*(Changed 2026-09-29)*". Sources: the SDD ledger
> rulings, `docs/code-review-bi-s1-2026-09-29.md` (fix record), the Task 14b report.
> 1. **§10.7 row 5 `anchor_wrong`** — fires on a shifted anchor (≥3 anchored BS ledgers share one diff) *or* a
>    dropped / double-counted one (each differs by ± its own anchor row); the (a)-only reading made a shift unreachable.
> 2. **§10.1 / §10.2 anchor staleness** — a stored anchor / month-end TB with unresolved rows, captured at another
>    `alt_mst_id` than the cursor → `aborted_incomplete` `anchor_stale` + re-capture (review I7, C1); a fresh
>    re-capture is never stale, so no loop.
> 3. **§10.1 M20 guard** — outside bisect, 422 `as_on_not_current_period` when FY(`as_on`) is older than the latest
>    books voucher's FY; best-effort, the binding rule is the S2 contract (§17.3).
> 4. **§12 steps 9/12, §8.7, §14 scenario 21** — inside a `full_resync` (always user-confirmed, D16) the alter_id
>    rule is suspended per object within the run's scope (C2): Tally is authoritative after a restore/relink.
> 5. **§7.6 / §7.7 / §7.15 ack semantics** — an ack means "received"; a `confirm_resync` stays open until its run
>    completes or a same-or-wider confirm supersedes it (`cancelled`); `/state` lists it (review I3, C3, C5).
> 6. **§7.8 / §8.2** — a completed company `full_resync` clears `restore_reason` and the restore/relink offer
>    (review I4); sync-status stopped offering a resync that had just run.
> 7. **§7.15 relink** — the password re-check shares the per-email login limiter (review I5: no password oracle).
> 8. **§7.10 coverage** — acks are typed and range-checked (`month_out_of_range`); a re-opened `first_sync` drops a
>    stale `complete` window FY back to `running` (review I6: the verified edge overclaimed).
> 9. **§7.8 runs** — typed bodies; `counters_at_start` required; `cursor_after_required` (review I2: a wedged workspace).
> 10. **§11** — the full as-built code list, incl. `internal_error`, `cursor_after_required`, `month_out_of_range`,
>     `anchor_stale`, `as_on_not_current_period` and every code the build added; `company_mismatch` is batches-only.
> 11. **§4.2** — `ladder` also carries `pending_remediation_ids`, `storage_estimated_at`, `last_run_at`,
>     `bisect_month`, `resync_offered`; `last_heartbeat`, `relink_prompt`, `last_parity`, `sync_commands` as built.
> 12. **§4.9** — `AVG_RAW_BYTES = 4170`; estimate refreshed ≤ hourly inside the slice budget; quarantine retention.
> 13. **§7.5** — D7's take-over guard applies to any bind that displaces a live active device, whatever the GUID.
> 14. **D30** — "ever carried an expression amount" = the ledger's own balances, never voucher lines.
> 15. **§10.1 step 6 / §10.6** — the TB imbalance nets the workspace's own top-level groups (custom ones included);
>     rung-2 row matching stays reserved-name.
> 16. **§15.1** — two unspecified cells pinned (logout × expired access; devices × deleted workspace).
> 17. **§17.1 S1-R10** — the shared per-email limiter can lock an agent login out.
> 18. **§17.3 (new)** — contract notes S2 must follow.
> 19. **§13.3** — G1–G5 captured 2026-09-25; G6 captured 2026-09-29: a ledgerwise TB as on a date is closing-only.
> 20. **§16** — known passlib `crypt` DeprecationWarning (note only).
>
> Also brought in line with the code (not in the queued list; verified in `v2/cloud/`): §10.7 rows 3, 4 and the
> `ledger_gap` fallback; §10.1's extra aborts (`tb_imbalance_unknown`, `no_balance_sheet_verified`) and §10.2's
> group-anchor input for the no-ledger-anchor route; review M7's contract drift (§4.2 command type, `requested_by`,
> `relink_prompt` keys, `last_parity.run_id`); review M8 (§11 `company_mismatch` not on snapshots).
> **Changed 2026-09-30 (v2 merged into the main code — [`2026-09-30-v2-merge-design.md`](2026-09-30-v2-merge-design.md)):** superseded by that spec and not edited in the body below: **D27** (port 8100, `V2_` settings prefix — now the one app on port 8000 and unprefixed names such as `DEVICE_TOKEN_SECRET`, with the `V2_*` names still accepted), the **separate Alembic chain** (`alembic_version_v2` / `v2_001` — now revision `006` of the one chain) and the **read-only reflections of `users` / `workspaces`** (D5's mechanism — the sync models now reference the real tables on the one `Base`; binding still attaches to an existing workspace only). Module paths moved: `v2/cloud/` → `backend/sync/`, `backend/db/sync_models/`, `backend/api/{agent_auth,devices,sync,workspace_sync,sync_dependencies}.py` (`web_sync.py` is now `workspace_sync.py`), `backend/utils/{device_tokens,rate_limit}.py`; `v2/contract/` → `contract/`; `v2/tests/cloud/` → `tests/sync/`; `v2/tests/fixtures/sync/` → `tests/fixtures/sync/`.
> **Status tracking:** [`../plans/2026-09-22-bi-part1-tracker.md`](../plans/2026-09-22-bi-part1-tracker.md) §4 (S1
> rows). Not in this file.
>
> **Scope caveat carried from S0:** every Tally behaviour this spec relies on was measured on TallyPrime 7.0 Edit Log,
> **Educational**, under **Wine 11.0** (S0 exit gate exception (b)). Where a rule could differ on a licensed Windows
> Tally, S1 is written so that the difference shows up as a parity finding, never as silently wrong data.

---

## 0. Understanding (the brief this design answers)

**What the user said (via Part 1 and the S0 gate).** Build the cloud half of the sync: a separate v2 FastAPI app
that authenticates one Windows agent per workspace, accepts Tally data exactly as Tally exports it, stores it in
Postgres with INR-only `Numeric(18,2)` money, and proves the copy is right with parity rungs 0–2 — without touching
any current code.

**Constraints (hard).**
- v2 isolation: everything in `v2/cloud/` (+ a shared `v2/contract/`), its own Alembic chain (`alembic_version_v2`),
  only **new** tables; `users` / `workspaces` are read, never altered, never written (§2 D5).
- No scheduler exists (Part 1 §6 "Schedule and trigger") — everything is request-driven.
- Money is `Decimal` end to end; a parse failure is an error, never zero (Part 1 §13).
- Tally quirks from S0 are facts, not hopes: posting rule `ALLLEDGERENTRIES.LIST` only (LESSONS rule 18), synthetic
  TB rows (rule 19, and the forex row found in §6.5 below), typed period variables (rule 21), C43, C45, C46, C47.
- Tests are offline (tier A) and built on the S0 captures, not on idealised fixtures (CLAUDE.md "Test reality").

**Success criteria.**
1. Every S0 voucher/master/report capture listed in §13 transcodes to the wire format and ingests without loss:
   company A (50 vouchers), company B (FY 2022-23 months incl. the two USD sales, flagged vouchers, Hindi, compound
   units).
2. The parity engine returns `ok` on the real captures where Tally's own figures are known (company A FY 2025-26;
   company B as-on 31-03-2023 **including the ₹183.87 forex revaluation**), and returns the right cause for every
   seeded fault in the §15 fixture matrix.
3. A replayed, reordered, truncated or cross-tenant request never corrupts stored data (DB persistence scenarios §14).
4. `git diff` for S1 touches only `v2/` and `docs/`.

**Assumptions (mine, not the user's) — flagged for review in §2 / §17:** the workspace is created on the web first
(D5); the agent picks the company (Q11, D32); production Postgres has storage-level encryption (Q6).

---

## 1. Scope

### 1.1 In scope (S1)
- `v2/cloud/` FastAPI app (own port, default `8100`), config, DB session, v2 Alembic chain, migration `v2_001`.
- `v2/contract/` — the wire format shared by S1 and S2: pydantic models, Tally text parsers (amount incl. the forex
  expression, date, quantity, rate, logical, name clean-up), and the Tally-XML → wire transcoder that S2's extractor
  and S1's tests both use.
- Device auth: login, refresh, logout, device listing/removal, `get_current_device`, one active device + take-over.
- Binding: `POST /api/sync/company`, re-link, the "wrong company, nothing synced yet" re-bind.
- Sync bookkeeping: runs (`kind`), server-side cursors, `sync_fy_coverage` + both watermark edges, `sync_state`,
  heartbeat (+ server → agent commands), restore detection, snapshots, reconcile.
- Ingest: `/batches` with every ingest rule and limit (§7), rung 0, quarantine, idempotency.
- Parity: `parity_runs` / `parity_lines`, rungs 1 and 2, opening anchors, C47 forex handling, quiescence and
  caught-up guards, TB-imbalance guard, cause classifier, escalation ladder, month-bisect evaluation, retention,
  internal ops signal.
- Web-facing reads/actions needed by Part 3 later: `GET /api/workspaces/{id}/sync-status`, `GET/DELETE /api/devices`,
  `POST /api/workspaces/{id}/sync/commands` (Re-check now, confirm resync, confirm re-link).
- Ops CLI: `python -m v2.cloud purge` (Q5) and `python -m v2.cloud migrate`.

### 1.2 Out of scope (explicit)
Everything in Part 1 §10 (confirmed complete, Q10 below), plus, for S1 specifically:
- The agent (S2): extraction, gate, scheduler, outbox, tray, installer, updater. S1 defines only what the agent must
  send and what it gets back.
- Any UI (Part 3) and any chat/query path (Part 2). `sync-status` is a JSON contract, not a screen.
- **Rung 3** (P&L / BS / Stock Summary / Bills totals) — Q20. Their snapshots are stored so rung 3 can be added.
- Stock parity (Σ `closing_value` vs Stock Summary) and books-start **stock** anchors — rung 3 territory.
- Opening-bill tables, godown / batch / cost-centre models — kept in `raw` only (Part 1 §10).
- A per-ledger monthly-totals table (D18).
- Multi-worker rate limiting, a job scheduler, app-level column encryption (Q6), billing.
- Creating rows in the current `workspaces` table (D5), workspace sharing (Q28).
- Merging v2 into current code.
- Any live-Tally run. S1's only tier-B activity is the **read-only fixture capture** in build task 0 (§18), and even
  that is optional: every test has an offline FakeBooks route.

---

## 2. Decisions

### 2.1 Open questions settled here (Part 1 §15)

| Q | Settled as | Source of the choice |
|---|---|---|
| **Q1** heartbeat on skipped cycles | **Yes.** The agent heartbeats every cycle, skipped or not, with a `tally_status` (§8.4). One small request per ~10 min. | Part 1's "Yes" option |
| **Q4** changing company or PC | **New PC / reinstall:** log in, bind the same workspace → take-over (old device revoked) → the agent resumes from the server's cursors and coverage (`GET /state`). **Wrong company picked:** re-bind is allowed only while the workspace has accepted **no** batch; afterwards a bound workspace never changes GUID except by re-link (Q25) — a different company goes to a different workspace. | Part 1 option "re-bind with a confirm; old device revoked", narrowed to the safe case |
| **Q5** data on disconnect / uninstall / account deletion | **Disconnect or uninstall** (device revoked): synced data kept, read-only, for as long as the workspace exists. **Workspace soft-deleted** (current app sets `is_deleted`): every device call is refused at once (410); synced rows are hard-deleted by `python -m v2.cloud purge` after a **30-day grace**, or immediately with `--now` on a deletion request (DPDP). **Account deletion** = all its workspaces. | Most conservative reversible option (nothing is lost by accident; deletion is explicit) |
| **Q6** privacy | TLS for every call; **storage-level encryption at rest is a production requirement** (managed Postgres or encrypted volume), no app-level column encryption in v1; `raw` kept for the recent 2 FYs only (Q22); **no business data in logs** — request bodies are never logged, the ops signal carries counts and causes only (decision 14); human access to v2 tables is break-glass only (process, not code). | Part 1 R20 handling + Q22 |
| **Q10** out-of-scope list | **Confirmed complete** (Part 1 §10), with §1.2's S1 additions. | — |
| **Q19** parity tolerance | **Flat ₹1.00** (`PARITY_TOLERANCE_PAISE=100`) per ledger line, per group row and on the grand total; forex **face** values compared to **0.01** of the foreign unit. Revisit when probe 20 (tier C) gives real diff magnitudes. | Part 1's default |
| **Q20** rung 3 | **Out for v1** (decision 11). Snapshots are stored so it can be added without a migration. | Decision 11 |
| **Q21** parity retention | **90 days** for runs, **7 days** for matching lines, **90 days** for mismatching lines. Pruned lazily (§8.6). | Part 1's proposal |
| **Q22** keep `raw` for backfilled years | **Decided 2026-09-24 (user):** `raw` for the recent 2 FYs only; older years columns only. S1 implements it in §4.9. | User |
| **Q23** backfill floor | **Decided 2026-09-24 (user):** no year floor; day-chunk auto-split (S2) + a per-company storage alert — S1 implements the alert (§4.9). | User |
| **Q25** re-link on a new GUID | Trigger: the agent sees **same company name, different GUID**. Confirm from the tray **or** the web, **website password required** either way, never automatic. After re-link: `sync_state = restore_detected` and a whole-company resync is **offered** (decision 12). | Part 1's default |
| **Q28** who sees a synced workspace | v1 = the existing ownership model: `workspaces.user_id` only. Devices belong to that user. Sharing is Part 3's (S3 spec). | Part 1's v1 assumption |
| **Q30** several PCs, one company | **One active device per workspace, take-over on confirm** (§9.3). TallyPrime server / multi-user installs run the agent on the machine that runs Tally. No designated-PC lock. | Part 1's default |
| **Q32** split companies | Not in v1. `books_from` is stored so Part 3 can say where this company's books start. | Part 1's default |
| Q11 (Part 3) | Not settled here; S1 builds the **agent-side** pick only (D32). | — |
| Q31 (S2) | Not S1's. | — |

### 2.2 Decisions taken on the user's standing instruction (2026-09-25) — review

"Reverse cost" is what changing the decision later would take. **Bold** rows are the ones the controller asked to be
spelled out (expression balances, C47).

| # | Decision | Alternative | Cost of reversing |
|---|---|---|---|
| D1 | **Wire = Tally's own field text.** The agent transcodes XML to JSON with tag names and text **unchanged** (`"amount": "-$448.44 @ ? 82.99/$ = -? 37216.04"`, `"date": "20220901"`, `"actualqty": " 17 Nos"`); the **server** parses. One parser, tested on the real captures, and `raw` is exactly what Tally said. | (a) Agent sends normalised decimals/dates. (b) Agent uploads the raw XML. | Medium: move parsing into the agent and change the wire models; the parsers themselves are reused (they live in `v2/contract`). |
| D2 | **`v2/contract/` is shared by S1 and S2** (models, parsers, transcoder). `v2.cloud` and `v2.agent` never import each other; both may import `v2.contract`. | Duplicate the parsers in agent and cloud. | Low: copy the package into both. |
| **D3** | **Expression-form amounts (decided by the controller):** everywhere an amount can appear (voucher line, bill allocation, ledger Opening/ClosingBalance, report cell), the parser accepts a plain number **or** Tally's forex expression. For an expression it stores the **stated INR base after `=`** in the money column, and **face value, currency, rate** in their own nullable columns, plus `raw`. No face × rate computation. **If an expression has no stated base:** a voucher line / bill → the object is rejected `forex_base_missing` (quarantinable); a ledger balance → stored `NULL` with `balance_source = 'needs_tb'`, and parity takes that ledger's Tally figure from the ledger-level TB (probe 17's route). | Keep face/rate only in `raw` (Part 1's original "never aggregated" wording). | Low: drop three columns; `raw` still has them. Keeping the columns is what makes Q22's raw-drop safe for forex drill-down. |
| **D4** | **C47 parity:** a forex ledger's Tally figure is Tally's **revalued** balance. The engine never flags the unrealised difference; it proves it is a revaluation instead: (1) where Tally's figure states a face value (mirrored balances), **face totals must match to 0.01** and the expression must be self-consistent (face × rate = base ± ₹0.01); (2) where Tally's figure is INR only (TB rows), the per-ledger differences are accepted **only as a set**: Σ over forex ledgers must equal minus the TB's own synthetic **`Unadjusted Forex Gain/Loss`** row (0 if absent). Verdict `match_revalued` with the difference stored. Group rollups add the accepted differences. Full algorithm §10.5. | (a) Model Tally's revaluation (face total × latest-dated rate) ourselves. (b) Exclude forex ledgers from parity. | Medium: (a) needs C47 review M1 (latest-dated vs last-entered) settled live; (b) is a one-line switch but hides real forex misses. |
| D5 | **Binding attaches to an existing workspace only.** v2 never inserts into `workspaces`. The workspace must exist (created on the web in the current app), be owned by the logged-in user, not be deleted, and not be bound to a different GUID. | v2 inserts a `workspaces` row (Part 1 §4 "cloud creates/reuses"). | Low: add a create branch to `POST /api/sync/company`. **Needs the user** (§17): it makes setup a two-step flow until Part 3 / the merge. |
| D6 | **Device tokens:** access = JWT signed with a **separate** secret `V2_DEVICE_TOKEN_SECRET`, `typ = "v2_device"`, 15 min; refresh = opaque 32-byte random, only its SHA-256 stored, **rotated on every refresh**, sliding 90-day life; presenting an already-rotated refresh token **revokes the device** (theft signal). Web JWTs and device tokens can never be swapped (different secret + `typ`). | Reuse `JWT_SECRET` with a `typ` claim; longer access tokens. | Low: config + token helpers. |
| D7 | **Take-over** needs `takeover: true` **and** a login no older than 10 minutes on the new device. The old device is revoked with `reason = taken_over`. | A tray click only. | Low. |
| D8 | **Snapshot key stays `(workspace_id, report_type, as_on_date)`** (Part 1), with a **convention**: `from_date` must be the start of the FY containing `as_on_date` (all S0 captures follow it). A snapshot that breaks it is rejected `bad_period`. | Add `from_date` to the key. | Medium: migration + endpoint change. |
| D9 | **Anchor date convention:** the opening anchor for a verified span starting at `E` is the **ledger-level TB as-on `E − 1`** (always a 31 March, since the verified edge is FY-granular — C43-safe). When `E = books_from`, the anchor is the ledger-level TB **as-on `books_from`** minus our own lines dated `books_from` (no read of a date before the books begin). Never a master `OpeningBalance` (C46). | Anchor at the day before `books_from`. | Low: one function. |
| D10 | **R4 "TB balances" check becomes a baseline check.** A TB's rows (incl. synthetic rows) need not net to 0 — company A's opening-balance difference and C47 prove it. The engine stores the TB **imbalance** with the `AltMstId` it was seen at; a later run with the **same `AltMstId`** and a different imbalance is discarded as stale Tally (`discarded_stale`). | Part 1's "Σ must be ≈ 0" (would discard every run on company A and on any real company with an opening difference). | Low. |
| D11 | **`Profit & Loss A/c`** (a ledger directly under Primary) is `not_applicable` in rungs 1–2 v1: it is statement-level, and if every other row matches and both sides balance, it matches arithmetically. | Model its FY roll-forward now. | Medium: needs rung-3-style logic. |
| D12 | **Quarantine, never silent skip.** A batch is atomic: one bad object rejects it (422, every offender listed). For **deterministic** codes only (§11), the agent may resend with those objects declared in `quarantine: [...]`; the server records them in `sync_quarantine`, counts them on `sync-status`, emits the ops signal, and parity will show the hole. | Reject forever (one odd voucher stops sync) or drop silently. | Low. |
| D13 | **Names → GUIDs on the server, per master type.** Lines, parents and voucher types are resolved by name among the workspace's **live** masters of that type (a group and a ledger may share a name — company A's `Capital Account`). No DB unique constraint on names; two live matches → `ambiguous_master` (retryable). A ledger GUID sent on a line (probe 6's fetch route) is a **cross-check** only. | Trust agent-sent GUIDs; unique-name constraints. | Low. |
| D14 | **Company check:** the batch's `company_guid` must equal the bound GUID — hard reject (R2). The per-object GUID prefix (all S0 GUIDs start with the company GUID, e.g. `138b7373-…-000003c1`) is a **warning** only: imported vouchers and split companies can carry another company's prefix. | Hard-reject on prefix. | Low. |
| D15 | **Cursors move only when a run completes** (`PATCH /runs/{id}` with `status = completed`, `cursor_after`), and only if every batch the run declared was acked. First sync / full resync set the cursor to the counters recorded at their start. | Advance per batch. | Low. |
| D16 | **Server → agent commands ride on the heartbeat response** (`recheck_now`, `resync`, `capture`). A `full_resync` run is **refused** unless it cites a pending, user-confirmed `resync` command — decision 12 enforced by the server, not only by agent discipline. | Agent-side only. | Low. |
| D17 | **Restore detection is server-side:** the heartbeat carries the company counters; counters below the stored cursors → `sync_state = restore_detected` and incremental batches are refused (409) until a confirmed resync. | Agent detects and reports. | Low. |
| D18 | **No monthly-totals table in S1.** Lines denormalise `voucher_date` and `countable`, with a covering index `(workspace_id, ledger_guid, voucher_date) INCLUDE (amount)`, so rung 1 is an index range sum. | Maintain monthly totals on ingest. | Medium: new table + backfill; add when Part 2 measures a slow query. |
| D19 | **Lines, inventory lines and bill allocations are hard-deleted** when their voucher is soft-deleted (Part 1: "they go"); the voucher row stays with `is_deleted = true`. | Soft-delete lines too. | Low. |
| D20 | **Lazy maintenance** (no scheduler): each heartbeat runs one bounded slice (≤ 2 s / 5,000 rows) of: raw purge (Q22), parity retention (Q21), batch-log retention, storage estimate (Q23). | Ops cron. | Low. |
| D21 | **`caught_up_at` alongside `last_synced_at`.** `last_synced_at` keeps Part 1's meaning (fresh data arrived; a heartbeat never moves it). `caught_up_at` = the last heartbeat whose counters equalled the cursors. Part 3 chooses which one "last synced X ago" shows. | Only `last_synced_at` (an idle company looks stale). | Low. **Needs the user** in Part 3 (§17). |
| D22 | **UUID primary keys** everywhere (current migrations 004/005 convention). | bigint keys on the line tables (smaller; probe 21's sizes are upper bounds that exclude this). | High once data exists; revisit only if Part 2 measures storage pain. |
| D23 | A line whose `isdeemedpositive` disagrees with its amount's sign is a **warning**, not a rejection (rung 0 is the hard check). | Reject. | Low. |
| D24 | Rate limits are **in-process** (like the current `_check_rate_limit`), so they hold per worker. | Postgres/Redis-backed. | Low. |
| D25 | Snapshots are stored as **verbatim cells + parsed rows** (JSONB), not raw XML. | Keep the XML. | Low. |
| D26 | Opening bills (probe 11's `BILLALLOCATIONS` on the ledger master), godown/batch allocations, cost centres, inventory accounting allocations: **`raw` only**. | Model them. | Low (additive tables). |
| D27 | v2 app on port **8100**, `V2_` settings prefix, same Postgres as the current app. | — | Trivial. |
| D28 | **Reconcile guard:** a reconcile that would soft-delete more than 20% **and** more than 50 rows of its scope is refused `reconcile_too_large` unless re-sent with `confirm_large: true` after the agent re-reads the list. | No guard. | Trivial. |
| D29 | **Rung 2 compares reserved primary groups + synthetic rows only.** A flat `EXPLODEFLAG` TB can't place custom sub-groups (and `EXPLODEFLAG` misses them anyway, probe 17 caveat 2); rung 2's per-ledger resolution comes from the ledger-level TB instead. | Compare second-level group rows by name. | Low. |
| D30 | **Forex ledger** = a ledger whose `CurrencyName` is not the base currency (base = the currency master whose ExpandedSymbol is `INR`; its NAME exports as `?`, LESSONS rule 28c), **or** one that ever carried an expression amount **in its own balances** (master Opening/ClosingBalance or a mirrored `ledger_balance`; sticky once set). *(Changed 2026-09-29: never from voucher lines — an INR ledger that forex sales pass through, e.g. `Export Sales`, is not forex and must not get `match_revalued`.)* | Currency field only. | Trivial. |
| D31 | **Reserved-name clean-up:** a leading U+0004 (`&#4;`) and following spaces are stripped from every text field before resolution (`&#4; Primary` on primary groups and stock items, `p25_A_groups.xml`, `p15_B_compound_unit_item.xml`). | — | Trivial. |
| D32 | **Company pick happens in the agent** (it lists Tally's companies and posts the pick). The web-side pick (Q11 option) is not built. | Web pick. | Low: `POST /api/sync/company` already takes the pick; add a list-reporting endpoint. |

---

## 3. Approaches considered (for the ingest contract, the one choice everything else hangs on)

1. **Verbatim Tally text on the wire, server parses (chosen, D1).** The agent is a thin, testable transcoder; every
   Tally quirk (expression amounts, `&#4;` prefixes, `" 17 Nos"`, `"30 Days"`, `"-? 37216.04"`) is handled once, on
   the server, and S1's tests run on the actual S0 captures. `raw` needs no reconstruction. Cost: a slightly larger
   payload (strings) and the server does the parsing work.
2. **Agent normalises, server validates.** Smaller payloads; but the parsing logic would live in the agent (S2),
   S1 could not test ingest on the real captures without S2, and a parser bug found later means re-shipping agents.
3. **Agent uploads raw XML.** Simplest agent; but 5 MB gzip batches of Tally XML are ~6× the JSON size (probe 21:
   raw is 86% of per-voucher bytes), and the server would need Tally's whole XML dialect.

Approach 1 plus the shared `v2/contract` package (D2) gives one parser, used by the server for ingest and by the
agent only to build batches (no parsing needed there beyond picking fields).

---

## 4. Data model (migration `v2_001`)

### 4.1 Conventions
- Own chain in `v2/cloud/alembic/`, version table `alembic_version_v2`. Only `CREATE TABLE` / `CREATE INDEX`; no
  statement touches a current table. FKs to `users.id` / `workspaces.id` are allowed (they don't alter them).
- UUID pk `id` (`gen_random_uuid()` server default), `workspace_id` FK + index, `created_at timestamptz
  server_default now()`, compound indexes in `__table_args__`, bare FKs, no `relationship()` (current 004/005
  conventions, Part 1 §6 "Storage").
- **Money:** `Numeric(18,2)`. **Forex face:** `Numeric(18,4)`; **rate:** `Numeric(18,6)`. **Quantities:**
  `Numeric(18,4)`. Never `Float`.
- **Dates** from Tally: `date`. **Timestamps:** `timestamptz`, UTC.
- **Text** stored exactly as parsed (UTF-8, code points preserved — probe 15), after D31's `&#4;` strip.
- Every master and voucher row: `guid text`, `alter_id bigint`, `is_deleted bool default false`, `raw jsonb null`,
  `first_seen_at`, `updated_at`. Unique `(workspace_id, guid)`.

### 4.2 Bookkeeping tables

**`sync_workspaces`** — one row per bound workspace; what Part 1 calls `workspace.config.*`.

| Column | Type | Notes |
|---|---|---|
| `workspace_id` | uuid pk, FK workspaces | |
| `tally_company_guid` | text not null | e.g. `710de34a-3661-4a7b-8148-c2206c3b3e17` (`p01_A_counters_baseline.xml`) |
| `tally_company_name` | text not null | as last seen; used by the re-link trigger |
| `previous_company_guids` | jsonb default `[]` | history of re-links `{guid, until}` |
| `books_from` | date not null | `BOOKSFROM` (`20250401` for A, `01-04-2022` for B) |
| `base_currency_name` | text null | the base currency's NAME (`?`, `p22_B_currencies.xml`) |
| `sync_state` | text not null | `awaiting_first_connection \| first_sync \| ready \| error \| restore_detected` (§8.2) |
| `restore_reason` | text null | `counters_backwards \| relink` |
| `active_device_id` | uuid null FK agent_devices | one active device (§9.3) |
| `cursor_alt_vch_id`, `cursor_alt_mst_id` | bigint null | server-authoritative cursors (D15) |
| `cursor_set_at` | timestamptz null | |
| `last_synced_at` | timestamptz null | Part 1 meaning (§8.5) |
| `caught_up_at` | timestamptz null | D21 |
| `last_seen_at` | timestamptz null | any heartbeat |
| `last_heartbeat` | jsonb null | *(Changed 2026-09-29, as built)* `{agent_version, tally_version, tally_status, seen_company, counters, last_error_code, breaker, outbox_depth, clock_skew_s}` — the raw `pc_clock` is not kept, only the skew |
| `relink_prompt` | jsonb null | *(Changed 2026-09-29, as built)* `{guid, name}` of the seen company |
| `oldest_available_fy`, `oldest_complete_fy` | date null | FY **start** dates; denormalised from coverage (§8.3) |
| `backfill_state` | text null | `running \| resyncing \| complete` |
| `backfill_percent` | numeric(5,2) null | |
| `last_parity` | jsonb null | `{state, checked_at, as_on, mismatch_count, verified_from, run_id}` — a `suspect` run writes the `ok` view and keeps the previous visible `run_id` *(Changed 2026-09-29)* |
| `ladder` | jsonb default `{}` | `{state, heal_attempts, resync_offered_fy, last_run_id}` (§10.8). *(Changed 2026-09-29, as built)* also `pending_remediation_ids` (the last run's issued ids, §10.8), `last_run_at`, `bisect_month` (§10.9), `resync_offered` (`{scope, fy_start?, reason: restore \| relink \| parity}`, §8.6) and `storage_estimated_at` (last Q23 estimate, §4.9) — kept here to avoid a schema change |
| `tb_imbalance_baseline` | jsonb null | `{imbalance, alt_mst_id, recorded_at}` (D10; keys as built, *Changed 2026-09-29*) |
| `quarantine_count` | int default 0 | D12 |
| `storage_estimate_bytes` | bigint default 0 | Q23 alert (§4.9) |
| `storage_alert` | bool default false | |
| `bound_at`, `updated_at` | timestamptz | |

Index: `(tally_company_guid)` (non-unique — two users may sync the same company file, Q28).

**`agent_devices`** — `id`, `user_id` FK users, `workspace_id` FK null (null until bound), `device_name`,
`agent_version`, `refresh_hash` (SHA-256 hex, unique), `refresh_prev_hash` (the hash it replaced, for reuse
detection), `refresh_expires_at`, `last_login_at`, `last_seen_at`,
`is_active` bool (true only for the workspace's active device), `revoked_at`, `revoke_reason`
(`taken_over | user_removed | logout | refresh_reuse | workspace_deleted`), `created_at`.
Partial unique index `(workspace_id) WHERE is_active` — **the DB enforces one active device per workspace.**
(The access token is a stateless JWT, so no access-token hash is stored; Part 1's `token_hash` column is replaced by
`refresh_hash` + the JWT's `jti`.)

**`sync_runs`** — `id`, `workspace_id`, `device_id`, `kind` (`first_sync | incremental | backfill | full_resync`),
`scope` jsonb (`{fy_start}` / `{company: true}` / null), `command_id` null (full_resync only, D16), `status`
(`running | completed | failed | interrupted`), `progress_done`, `progress_total` (int), `batches_declared` int null,
`counters_at_start` jsonb, `cursor_after` jsonb null, `error_code` null, `started_at`, `finished_at`.
Index `(workspace_id, started_at desc)`; partial `(workspace_id) WHERE status = 'running' AND kind = 'first_sync'`.

**`sync_batches`** — `id`, `workspace_id`, `run_id` FK, `batch_id` text (agent's idempotency key), `request_sha256`,
`object_count`, `status` (`accepted | rejected`), `response` jsonb, `received_at`. Unique `(workspace_id, batch_id)`.
Retention 90 days (D20).

**`sync_quarantine`** — `id`, `workspace_id`, `kind`, `guid`, `code`, `detail` text (no amounts/names, decision 14),
`voucher_date` null, `first_seen_at`, `last_seen_at`, `times_seen`, `resolved_at` null. Unique
`(workspace_id, kind, guid) WHERE resolved_at IS NULL`. Resolved when a later batch stores that GUID.

**`sync_commands`** — `id`, `workspace_id`, `type` (`recheck_now | resync | capture`), `params` jsonb,
`requested_by` (`user:<id>` / `server:parity`), `status` (`pending | delivered | done | cancelled`), `created_at`,
`delivered_at`, `done_at`. Index `(workspace_id, status)`.
*(Changed 2026-09-29, as built — review M7:* the web path stores `type` = the web command name (`recheck_now |
confirm_resync`; `confirm_relink` is applied at once, never stored), `params` `{"scope": "company"}` /
`{"scope": "fy", "fy_start"}`, `requested_by = "web"`. Parity never enqueues a command: its resync offer lives in
`ladder.resync_offered` until the user confirms. `cancelled` = superseded by a newer confirm, or made moot by a
completed company resync (§7.15). Status flow: `pending → delivered` (heartbeat) `→ done` (ack) for every type
except `confirm_resync`, which goes `delivered → done` only when its `full_resync` completes (§7.6).*)

**`sync_fy_coverage`** — Part 1 §5 as specified: `id`, `workspace_id`, `fy_start` date, `fy_end` date, `state`
(`pending | running | resyncing | complete`), `months_done` jsonb (sorted list of `YYYY-MM`, so a replayed ack can't
double-count), `months_complete` int, `months_total` int, `completed_at`. Unique `(workspace_id, fy_start)`.

### 4.3 Masters

| Table | Columns beyond the common ones (§4.1) | Source fields (fixture) |
|---|---|---|
| `tally_currencies` | `name`, `mailing_name`, `expanded_symbol`, `decimal_places` int, `is_base` bool (derived, D30) | `NAME`, `MAILINGNAME`, `EXPANDEDSYMBOL`, `DECIMALPLACES` (`p22_B_currencies.xml`) |
| `tally_groups` | `name`, `parent_name` (as exported, cleaned), `parent_guid` null (resolved), `primary_group` (derived), `nature` (`assets \| liabilities \| income \| expenses`, derived), `is_revenue`, `affects_gross_profit`, `is_deemed_positive`, `reserved_name` null, `derivation_warning` null | `NAME`, `PARENT`, `ISREVENUE`, `AFFECTSGROSSPROFIT`, `ISDEEMEDPOSITIVE`, `RESERVEDNAME`, `GUID`, `ALTERID` (`p25_A_groups.xml`, `p04_A_group_full.xml`) |
| `tally_voucher_types` | `name`, `parent_name`, `parent_guid` null, `reserved_name` null, `base_type` (derived) | `PARENT`, `RESERVEDNAME` (`p25_A_voucher_types.xml`, `p25_B_voucher_types.xml`) |
| `tally_ledgers` | `name`, `parent_name`, `group_guid` null, `currency_name` null, `is_forex` bool, `is_bill_wise` null, `tax_type` null, `gst_duty_head` null, `opening_balance` / `closing_balance` Numeric(18,2) null, `opening_fx_amount`, `opening_fx_rate`, `closing_fx_amount`, `closing_fx_rate`, `fx_currency` null, `balance_source` (`tally \| needs_tb`), `balance_captured_at` null, `balance_text` jsonb (the two verbatim strings) | `PARENT`, `CURRENCYNAME`, `OPENINGBALANCE`, `CLOSINGBALANCE`, `ISBILLWISEON`, `TAXTYPE`, `GSTDUTYHEAD` (`p16_A_ledgers.xml`, `p22_B_usd_ledger.xml`, `p23_A_gst_ledgers.xml`) |
| `tally_stock_groups` | `name`, `parent_name`, `parent_guid` null | |
| `tally_units` | `name`, `is_simple` null, `base_units` null, `additional_units` null, `conversion` null | compound `Box of 10 Nos` (`p15_B_compound_unit_item.xml`) |
| `tally_stock_items` | `name`, `parent_name`, `parent_guid` null, `base_unit_name`, `base_unit_guid` null, `closing_qty` Numeric(18,4) null, `closing_qty_text`, `closing_value` null, `balance_captured_at` null | `PARENT` (`&#4; Primary` → no parent), `BASEUNITS`, `CLOSINGBALANCE`, `CLOSINGVALUE` (`p04_A_stockitem_full.xml`, `p15_B_compound_unit_item.xml`) |

Indexes: `(workspace_id, name) WHERE NOT is_deleted` on every master table (non-unique, D13); `(workspace_id,
group_guid)` on ledgers; `(workspace_id, parent_guid)` on groups.

**`opening_balance` is stored but never used as a books-start anchor** (C46, probes 11/16 B: it is the current-FY
opening). Parity uses it only in the forex face check (§10.5), where "current-FY opening" is exactly what is wanted.

### 4.4 Derivations done at ingest (probe 25)

- **`nature` / `primary_group`:** walk `parent_name` up the workspace's live groups until the parent is Primary
  (after D31) — the group itself is then a reserved primary group — and map it through `PRIMARY_NATURE` (the table in
  `v2/probes/reads.py`, copied into `v2/contract`). Handles custom sub-groups of any depth: `National Creditors →
  Sundry Creditors → Current Liabilities → liabilities`, `North Zone Debtors → Sundry Debtors → Current Assets →
  assets` (`p25_A_groups.xml`). `is_revenue` must agree with the nature (`income | expenses` ⇔ `Yes`, 32/32 on A);
  disagreement stores `derivation_warning` and emits a warning, it doesn't reject. A cycle or a primary group not in
  the map → `derivation_warning = 'unmapped_primary'`, `nature = NULL`; parity treats that group's ledgers as
  `not_applicable` with cause `unclassified_group` (never `match`).
- **`base_type`:** walk the voucher-type `parent_name` chain until `name == parent_name` (reserved types are their
  own parent, `Contra → Contra`) or the name is one of the 24 reserved types in probe 25's `base_types`
  observation. `Sales - GST → Sales` (`p25_B_voucher_types.xml`). Unresolvable → `base_type = NULL` + warning.
- **Re-derivation:** when a group or voucher type changes parent, every descendant is re-derived in the same
  transaction.
- **`is_forex`:** D30. **`is_base` currency:** `expanded_symbol == 'INR'`.

### 4.5 Vouchers

**`tally_vouchers`** — `guid`, `master_id` bigint, `alter_id`, `date`, `effective_date` null, `voucher_type_name`,
`voucher_type_guid` null, `base_type` null (denormalised), `voucher_number` text, `reference` text,
`party_ledger_name` text (empty for cancelled vouchers — LESSONS rule 23), `party_ledger_guid` null, `narration`,
`is_cancelled`, `is_optional`, `is_post_dated` (all bool, from `ISCANCELLED` / `ISOPTIONAL` / `ISPOSTDATED`),
`is_invoice` null, `has_forex` bool, `is_deleted`, `deleted_at` null, `raw` jsonb null (Q22), `run_id`.
Indexes: unique `(workspace_id, guid)`; `(workspace_id, date)`; `(workspace_id, party_ledger_guid, date)`;
`(workspace_id, alter_id)`.

**`tally_voucher_ledger_lines`** — `voucher_id` FK, `workspace_id`, `line_no` int, `ledger_name` (as exported),
`ledger_guid` not null (resolved), `amount` Numeric(18,2) (INR base, **debit negative**), `is_deemed_positive`,
`fx_currency`, `fx_amount`, `fx_rate` (null unless the amount was an expression), `voucher_date` (denormalised),
`countable` bool (= not cancelled and not optional, fixed at insert; D18/D19).
Covering index `(workspace_id, ledger_guid, voucher_date) INCLUDE (amount, fx_amount) WHERE countable`.

**`tally_voucher_inventory_lines`** — `voucher_id`, `workspace_id`, `line_no`, `stock_item_name`,
`stock_item_guid`, `actual_qty` / `billed_qty` Numeric(18,4) (leading number, C40: `10 Box 0 Nos` → 10), `qty_text`,
`rate` Numeric(18,4) null (leading number of `824.46/Nos`; null if the rate carries a currency symbol),
`rate_text`, `amount` Numeric(18,2) (expression rule D3), `fx_*` as for ledger lines, `is_deemed_positive`,
`voucher_date`. Index `(workspace_id, stock_item_guid, voucher_date)`.

**`tally_bill_allocations`** — `voucher_id`, `workspace_id`, `ledger_line_no`, `ledger_guid`, `bill_name`,
`bill_type` (`new_ref | agst_ref | advance | on_account`, from `New Ref` / `Agst Ref` / `Advance` / `On Account`),
`amount` (D3), `fx_*`, `credit_period_text` (`30 Days`), `credit_period_days` int null (parsed only from `N Days`),
`bill_date` null (from `BILLDATE` when present), `voucher_date`. Index `(workspace_id, ledger_guid, bill_name)`.
Due dates come from the Bills snapshot (probe 23 B, LESSONS rule 24); the bill row stores the credit period.

Lines, inventory lines and bill allocations have no Tally identity: they are **deleted and re-inserted** with their
voucher in one transaction, and hard-deleted when the voucher is soft-deleted (D19).

**What is summed:** only the wire's `ledger_entries`, which the agent fills from **`ALLLEDGERENTRIES.LIST`
only** (LESSONS rule 18; `PROBE_POSTING_RULE = "all_only"`, probe 6: 50/50 balanced). `LEDGERENTRIES.LIST` and each
inventory entry's `ACCOUNTINGALLOCATIONS.LIST` are the same postings rendered again (`p22_B_forex_sales.xml` has
both lists for every voucher); they may appear in `raw` but are never stored as lines. A wire voucher carrying a
`ledgerentries_list` key is rejected `duplicate_posting_list`.

### 4.6 Snapshots

**`tally_report_snapshots`** — `workspace_id`, `report_type` (`trial_balance | trial_balance_ledgerwise |
balance_sheet | profit_and_loss | stock_summary | bills_receivable | bills_payable`), `from_date`, `as_on_date`,
`purpose` (informational: `current | month_end | fy_close | anchor | bisect | parity`), `request_flags` jsonb
(`{"ISLEDGERWISE": "Yes"}` / `{"EXPLODEFLAG": "Yes"}`), `captured_at`, `counters` jsonb null, `cells` jsonb
(verbatim rows), `rows` jsonb (parsed: name, Decimal strings, parse notes), `row_count`, `synthetic_rows` jsonb
(`Opening Stock`, `Unadjusted Forex Gain/Loss` found in this response), `imbalance` Numeric(18,2) null (TB types).
Unique `(workspace_id, report_type, as_on_date)` (D8); a re-capture replaces (`captured_at` newer wins).
`trial_balance_ledgerwise` is new vs Part 1 (probe 17's `ISLEDGERWISE=Yes`).

### 4.7 Parity

**`parity_runs`** — Part 1 §6 columns plus: `scope` (`daily | recheck | bisect | post_resync`), `verified_from`
date, `anchor_as_on` date null, `status` (`ok | suspect | alert | hard_alert | aborted_moving | aborted_behind |
aborted_incomplete | discarded_stale`), `abort_reason` null, `forex_unrealised_total` Numeric(18,2) null,
`tb_imbalance` Numeric(18,2) null.

**`parity_lines`** — Part 1 columns (`scope ledger | group | statement`, `guid`, `name`, `our_amount`,
`tally_amount`, `diff`, `verdict`, `cause`, `remediation_status`) plus `verdict` value **`match_revalued`** (C47,
D4), `unrealised_diff` Numeric(18,2) null, `our_fx_amount` / `tally_fx_amount` null, `as_on_date` (bisect runs
compare several dates). Index `(workspace_id, run_id)`, `(workspace_id, verdict)`.

### 4.8 What the three Part 1 contracts get
- **Data:** the tables above; `nature`, `base_type` derived; lines resolved to GUIDs; four voucher flags;
  `Numeric(18,2)` INR base; coverage + both edges; snapshots; `balance_captured_at`; `last_synced_at` meaning (§8.5).
- **Status:** `sync-status` (§7.13).
- **Integrity:** `parity_runs` / `parity_lines` / `last_parity`, verdicts incl. `match_revalued`.

### 4.9 Retention and storage (Q21, Q22, Q23)
- **`raw` on vouchers:** kept while the voucher's FY is one of the newest two FYs of the workspace's coverage
  (current FY and previous FY, by IST date). A voucher arriving for an older FY is stored with `raw = NULL`. When a
  new FY row is added (rollover), the FY that fell out is purged in bounded slices by D20. Masters keep `raw` always
  (small).
- **Parity:** Q21 (90 / 7 / 90). **Batches log:** 90 days. **Snapshots, coverage, runs:** kept (small, and
  month-end history is product value). **Quarantine rows:** kept until resolved + 90 days.
- **Q23 storage alert:** `storage_estimate_bytes` = Σ row counts × per-table average bytes (from
  `pg_column_size` sampled at migration time, a constant table in code), refreshed by D20. Over
  `V2_STORAGE_ALERT_BYTES` (default 5 GB) → `storage_alert = true` + ops signal. No hard stop (Q23).
  *(Changed 2026-09-29, as built:)* `AVG_ROW_BYTES` was measured on company B's real ingest (`sync/maintenance.py`);
  `tally_vouchers` is **363 B without `raw`, 4,533 B with**, so the estimate adds **`AVG_RAW_BYTES = 4170`** per
  voucher whose `raw IS NOT NULL`. The count walk is O(rows), so it runs **at most once per
  `storage_estimate_interval_seconds`** (default 3600; last run kept in `ladder.storage_estimated_at`) and only when
  the slice's time budget allows. The ops signal fires on the false → true transition only. `pg_column_size`
  excludes index/page overhead, so the estimate understates disk use (~1.5–2×) — a coarse early warning.
  Quarantine retention as built: **resolved rows pruned 90 days after `resolved_at`**; open rows never.
- **Q5 purge:** `python -m v2.cloud purge [--workspace ID] [--now]` deletes every v2 row of workspaces soft-deleted
  ≥ 30 days ago (or at once with `--now`), in FK order, and logs counts only.

---

## 5. The wire format (`v2/contract`)

### 5.1 Principles
- JSON, gzip-compressed on `/batches` (`Content-Encoding: gzip`). Keys are Tally's tag names in lower case; values
  are Tally's text **unchanged** except XML entity decoding (`&amp;` → `&`). Missing tag → key absent; empty tag →
  `""`. The server distinguishes the two (Part 1 §13: missing ≠ zero).
- Each object: `{"kind": "...", "data": {...}}`. Nested lists are arrays of objects of the same form.
- The transcoder `v2.contract.transcode.voucher_from_xml(element)` (and one per master / report) is the only
  sanctioned way to build a wire object. S1's tests build every wire object from an S0 capture through it.

### 5.2 Parsers (all in `v2/contract/parse.py`; every one raises a typed `WireParseError`, never returns 0)

| Parser | Accepts (with fixture) | Returns |
|---|---|---|
| `amount` | `-62800.00`, `1261.42`, `0.00` (`p16_A_ledgers.xml`); **forex expression** `-$448.44 @ ? 82.99/$ = -? 37216.04` (`p22_B_forex_sales.xml`) and `-$1609.71 @ ? 82.58/$ = -? 132929.85` (`p22_B_usd_ledger.xml`) — base symbol `?`, a space, sign before the symbol; `""` → `None` | `Amount(inr: Decimal \| None, fx_currency, fx_amount, fx_rate, stated: bool, text)` — `inr` is the stated base; an expression without `= base` → `inr=None, stated=False` (D3) |
| `tally_date` | `20220901`, `1-Oct-25`, `15-Dec-25`, `01-10-2025` | `date` |
| `logical` | `Yes` / `No` (`ISCANCELLED`, `ISDEEMEDPOSITIVE`) | `bool`; anything else raises |
| `quantity` | `" 17 Nos"`, `"10 Box 0 Nos"` (C40), `"-2.0000 NOS"`, `""` | `(Decimal \| None, text)` — the **leading number** |
| `rate` | `824.46/Nos` | `(Decimal \| None, unit, text)`; a rate with a currency symbol → `None` + text |
| `credit_period` | `30 Days`, `45 Days` (`p23_B_bills_credit_period.xml`, the `JD` attribute is ignored) | `(days \| None, text)` |
| `name` | any name; strips a leading U+0004 + spaces (D31); keeps Hindi code points exact (`शर्मा ट्रेडर्स`, `p23_B_bills_receivable_due.xml`) | `str` |
| `counter` | `" 50"`, `" 965"` (leading space, `p01_A_counters_baseline.xml`) | `int` |

The forex grammar starts from `v2/probes/reads.py` `_FOREX_RE` / `parse_forex_amount` (tested live in probe 22),
copied with a header naming its source (Part 1 §5 "Copy, don't import").

### 5.3 Object kinds and required fields

| kind | Required keys (missing → `missing_field`) | Optional keys |
|---|---|---|
| `currency` | `guid`, `alterid`, `name`, `expandedsymbol` | `mailingname`, `decimalplaces` |
| `group` | `guid`, `alterid`, `name`, `parent` | `isrevenue`, `affectsgrossprofit`, `isdeemedpositive`, `reservedname` |
| `voucher_type` | `guid`, `alterid`, `name`, `parent` | `reservedname` |
| `ledger` | `guid`, `alterid`, `name`, `parent` | `currencyname`, `openingbalance`, `closingbalance`, `isbillwiseon`, `taxtype`, `gstdutyhead`, `captured_at` |
| `stock_group` | `guid`, `alterid`, `name`, `parent` | |
| `unit` | `guid`, `alterid`, `name` | `issimpleunit`, `baseunits`, `additionalunits`, `conversion` |
| `stock_item` | `guid`, `alterid`, `name`, `parent`, `baseunits` | `closingbalance`, `closingvalue`, `captured_at` |
| `ledger_balance` | `guid`, `name`, `closingbalance`, `captured_at` | `openingbalance` — the mirrored re-read (Part 1 §4) |
| `stock_balance` | `guid`, `name`, `closingvalue`, `captured_at` | `closingbalance` (qty) |
| `voucher` | `guid`, `masterid`, `alterid`, `date`, `vouchertypename`, `iscancelled`, `isoptional`, `ispostdated`, `ledger_entries` (may be `[]`) | `vouchernumber`, `reference`, `partyledgername`, `narration`, `effectivedate`, `isinvoice`, `inventory_entries`, `raw_extra` |
| voucher `ledger_entries[]` | `ledgername`, `amount`, `isdeemedpositive` | `ledgerguid` (cross-check, D13), `bill_allocations[]` (`name`, `billtype`, `amount`, `billcreditperiod`, `billdate`) |
| voucher `inventory_entries[]` | `stockitemname`, `amount` | `actualqty`, `billedqty`, `rate`, `isdeemedpositive`, `batch_allocations[]`, `accounting_allocations[]` (raw only) |

`VOUCHER_MONTH_FIELDS` in `v2/probes/reads.py` (probe 5's confirmed month request) is the field list S2 fetches;
every required voucher key above is in it. Masters need **GUID and AlterID** on every type: proven for groups,
ledgers and stock items (`p04_A_*_full.xml`); **not yet captured** for voucher types, currencies, stock groups and
units — fixture gap G4 (§13.3).

### 5.4 Real examples (verbatim from the captures)

An INR sales voucher (`p22_B_forex_sales.xml`, first voucher; `ALLLEDGERENTRIES.LIST` only, trimmed to the wire keys):

```json
{"kind": "voucher", "data": {
  "guid": "138b7373-753c-4dbe-aa63-b802035f0ba9-00000067", "masterid": " 103", "alterid": " 105",
  "date": "20220901", "vouchertypename": "Sales", "vouchernumber": "32", "reference": "",
  "partyledgername": "Indore Home Needs", "narration": "[S0-B:103] Sale to Indore Home Needs",
  "iscancelled": "No", "isoptional": "No", "ispostdated": "No", "isinvoice": "Yes",
  "ledger_entries": [
    {"ledgername": "Indore Home Needs", "isdeemedpositive": "Yes", "amount": "-16538.66",
     "bill_allocations": [{"name": "Inv/103", "billtype": "New Ref", "billcreditperiod": "30 Days", "amount": "-16538.66"}]},
    {"ledgername": "Domestic Sales", "isdeemedpositive": "No", "amount": "14015.82"},
    {"ledgername": "Output CGST", "isdeemedpositive": "No", "amount": "1261.42"},
    {"ledgername": "Output SGST", "isdeemedpositive": "No", "amount": "1261.42"}],
  "inventory_entries": [
    {"stockitemname": "Office Stapler", "actualqty": " 17 Nos", "billedqty": " 17 Nos",
     "rate": "824.46/Nos", "amount": "14015.82", "isdeemedpositive": "No"}]}}
```

Σ = −16,538.66 + 14,015.82 + 1,261.42 + 1,261.42 = **0.00** (rung 0).

A USD export sale (`p22_B_forex_sales.xml`, `[S0-B:101]`):

```json
{"kind": "voucher", "data": {
  "guid": "138b7373-753c-4dbe-aa63-b802035f0ba9-000003c1", "masterid": " 961", "alterid": " 965",
  "date": "20220901", "vouchertypename": "Sales", "vouchernumber": "73",
  "partyledgername": "Gulf Office Supplies LLC (USD)",
  "iscancelled": "No", "isoptional": "No", "ispostdated": "No",
  "ledger_entries": [
    {"ledgername": "Gulf Office Supplies LLC (USD)", "isdeemedpositive": "Yes",
     "amount": "-$448.44 @ ? 82.99/$ = -? 37216.04"},
    {"ledgername": "Export Sales", "isdeemedpositive": "No",
     "amount": "$448.44 @ ? 82.99/$ = ? 37216.04"}]}}
```

Stored: lines `amount −37216.04 / +37216.04` (Σ 0.00), `fx_currency "$"`, `fx_amount −448.44 / +448.44`,
`fx_rate 82.99`; `has_forex = true`.

A cancelled voucher (`p03_B_flagged_month_2023_02.xml`, `[S0-B:201]`): `"iscancelled": "Yes"`,
`"partyledgername": ""`, `"ledger_entries": []`, voucher type `Sales` — stored with no lines, `countable` irrelevant,
never in any sum (LESSONS rule 23).

A ledger with a custom sub-group (`p16_A_ledgers.xml` + `p04_A_ledger_full.xml`):
`{"kind": "ledger", "data": {"guid": "710de34a-3661-4a7b-8148-c2206c3b3e17-000000dd", "alterid": " 223",
"name": "Apex Technologies Pvt Ltd", "parent": "North Zone Debtors", "openingbalance": "0.00",
"closingbalance": "-62800.00", "captured_at": "2026-09-23T12:25:00+05:30"}}`.

The forex ledger (`p22_B_usd_ledger.xml`): `"currencyname": "$"`, `"parent": "Sundry Debtors"`,
`"closingbalance": "-$1609.71 @ ? 82.58/$ = -? 132929.85"` → `closing_balance −132929.85`, `closing_fx_amount
−1609.71`, `closing_fx_rate 82.58`, `is_forex true`. (This capture has no GUID/AlterID — fixture gap G4.)

A primary group (`p25_A_groups.xml` + `p04_A_group_full.xml`): `{"guid":
"710de34a-3661-4a7b-8148-c2206c3b3e17-00000006", "alterid": " 7", "name": "Current Assets", "parent": "\u0004
Primary", "isrevenue": "No", "affectsgrossprofit": "No"}` → parent cleaned to `Primary`, `nature = assets`.

Company counters (`p01_A_counters_baseline.xml`): `ALTVCHID " 50"`, `ALTMSTID " 265"`, `BOOKSFROM "20250401"`,
`GUID "710de34a-3661-4a7b-8148-c2206c3b3e17"`.

---

## 6. Architecture

### 6.1 Layout

```
v2/
  contract/                 shared by cloud (S1) and agent (S2); imports neither
    models.py               pydantic wire models (§5.3), request/response bodies (§7)
    parse.py                §5.2 parsers (forex grammar copied from v2/probes/reads.py)
    transcode.py            Tally XML element -> wire object, per kind; report cells
    tally_rules.py          PRIMARY_NATURE, reserved voucher types, synthetic TB row names
  cloud/
    main.py                 FastAPI app (port 8100), lifespan: engine + settings check
    config.py               V2_* settings (labelled sections, inline comments — current config.py convention)
    db.py                   async engine/session (asyncpg)
    models/                 SQLAlchemy models for §4 (bare FKs)
    alembic/                chain v2_001…, version table alembic_version_v2
    auth/                   copied verify_password (backend/utils/auth.py), web JWT check, device tokens, rate limits
    api/
      dependencies.py       get_current_user (web JWT copy), get_current_device, require_active_device(ws)
      agent_auth.py         /api/agent/auth/*, /api/agent/workspaces
      devices.py            /api/devices
      sync.py               /api/sync/company, /{ws}/heartbeat|state|runs|batches|coverage|reconcile|snapshots|parity|relink
      web_sync.py           /api/workspaces/{id}/sync-status, /sync/commands
    ingest/
      pipeline.py           §7 ordered steps, one transaction per batch
      resolve.py            name -> GUID per master type (D13)
      derive.py             nature, base_type, is_forex, re-derivation
      store.py              upserts (alter_id rule), line replacement, balances by captured_at
    sync/
      state.py              sync_state machine, cursors, commands, restore detection, re-link
      coverage.py           months_done, both edges, backfill denormalisation
      maintenance.py        D20 slices
    parity/
      engine.py             orchestrates one run (§10)
      anchors.py            D9
      rung0.py, rung1.py, rung2.py, forex.py, classify.py, ladder.py, bisect.py
      opsignal.py           decision 14 log line
    cli.py                  python -m v2.cloud migrate | purge
  tests/cloud/, tests/contract/  (§16)
```

### 6.2 Dependency rules (enforced by `v2/tests/test_isolation.py`, extended)
- Nothing under `v2/` imports `backend`, `tests` or `scripts` (existing rule).
- `v2.cloud` never imports `v2.agent` or `v2.probes`; `v2.agent` never imports `v2.cloud`; `v2.contract` imports
  neither. Tests may import `v2.probes.setup` (FakeBooks, `company_b_data`) as a fixture generator.
- `v2.cloud` touches current tables only through two read-only queries: `users` (login, JWT subject) and
  `workspaces` (ownership, `is_deleted`). A test asserts no SQLAlchemy model in `v2.cloud` maps `users` or
  `workspaces` for writing (they are `Table` objects reflected read-only).

### 6.3 Request flow (one incremental cycle, happy path)

```
agent                                      v2 cloud
 POST /heartbeat {tally_status: ours, counters}  → last_seen_at; counters < cursors? → restore_detected
                                           ← {commands: [], cursors}
 POST /runs {kind: incremental}           → run_id
 POST /batches {run_id, masters…}          → resolve+derive+upsert → 200 accepted
 POST /batches {run_id, vouchers…}         → rung 0 + resolve + upsert; last_synced_at
 POST /batches {run_id, ledger_balance…}   → balances where captured_at newer
 PATCH /runs/{id} {completed, cursor_after, batches_declared} → cursors advance (D15)
 POST /snapshots {trial_balance …}         → upsert on key
```

---

## 7. API contracts

All request/response bodies are JSON. Errors: `{"error": "<code>", "detail": "<text>", ...extra}` (§11).
Device endpoints need `Authorization: Bearer <device access token>`; web endpoints the current app's web JWT.

### 7.1 `POST /api/agent/auth/login`
```json
{"email": "owner@example.com", "password": "…", "device_name": "ACCOUNTS-PC", "agent_version": "0.1.0"}
```
→ `200 {"device_id": "…", "access_token": "…", "expires_in": 900, "refresh_token": "…", "user": {"id": "…", "name": "…"}}`.
Checks: copied `_check_rate_limit` (per email), `verify_password`, `users.is_active`. Creates an **unbound** device.
Errors: 401 `invalid_credentials`, 403 `account_inactive`, 429 `rate_limited`.

### 7.2 `POST /api/agent/auth/refresh`
`{"refresh_token": "…"}` → new access + **new** refresh token (old hash replaced). A token that matches a
**previous** hash (kept in `refresh_prev_hash` for detection) → device revoked `refresh_reuse`, 401
`device_revoked`, ops signal. Expired → 401 `refresh_expired`.

### 7.3 `POST /api/agent/auth/logout` (device token)
Revokes the calling device (`reason = logout`). Used by the uninstaller. 204.

### 7.4 `GET /api/agent/workspaces` (device token, bound or not)
The user's non-deleted workspaces with their binding: `[{"id", "name", "bound_company_guid": null | "…",
"bound_company_name", "active_device": null | {"device_name", "last_seen_at"}}]`. Lets the agent offer "bind to…"
(D5, D32).

### 7.5 `POST /api/sync/company` (device token)
```json
{"workspace_id": "…", "company_guid": "138b7373-753c-4dbe-aa63-b802035f0ba9",
 "company_name": "Sharma & Sons' Probe Traders", "books_from": "20220401",
 "base_currency_name": "?", "takeover": false}
```
Outcomes (first match wins):

| Situation | Response |
|---|---|
| Workspace not the user's / deleted | 404 `workspace_not_found` / 410 `workspace_deleted` |
| Workspace unbound | 200 `{"bound": true, "sync_state": "awaiting_first_connection", "coverage": [...]}`; `sync_fy_coverage` rows created `pending` from `books_from`'s FY to the current FY |
| Bound to the **same** GUID, this device already active | 200 no-op (Part 1 §4 step 3) |
| Bound to the same GUID, another active device, `takeover=false` | 409 `takeover_required` `{"active_device": {"device_name", "last_seen_at"}}` |
| Same, `takeover=true`, login ≤ 10 min old (D7) | 200; old device revoked `taken_over`; this device active; response carries cursors + coverage (Q4 resume) |
| Same, `takeover=true`, login older | 401 `reauth_required` |
| Bound to a **different** GUID, no batch ever accepted | 200 re-bound (Q4 "wrong company"); coverage re-created |
| Bound to a different GUID, data exists | 409 `workspace_bound_to_other_company` |
| This GUID bound to **another workspace of the same user** | 409 `company_bound_elsewhere` `{"workspace_id"}` |

*(Changed 2026-09-29 — Task 5 ruling, as built:)* the rows are evaluated in the order 404/410 →
`company_bound_elsewhere` → the rest. **D7's take-over guard applies to any bind that would displace a live active
device of the workspace, whatever the GUID** — the different-GUID "no data" re-bind runs the same guard
(`takeover_required` / `reauth_required`) before its accepted-batch check. A device that is revoked, inactive, or now
active on another workspace doesn't count as "live active". A `books_from` that doesn't parse → 422
`invalid_books_from`.

### 7.6 `POST /api/sync/{ws}/heartbeat` (active device)
```json
{"agent_version": "0.1.0", "tally_version": "TallyPrime 7.0",
 "tally_status": "ours", "seen_company": {"guid": "138b…0ba9", "name": "Sharma & Sons' Probe Traders"},
 "counters": {"alt_vch_id": 965, "alt_mst_id": 412},
 "last_error_code": null, "breaker": "closed", "outbox_depth": 0,
 "pc_clock": "2026-09-25T18:02:11+05:30", "acked_commands": ["…"]}
```
`tally_status`: `closed | port_closed | popup_blocked | no_company | other_company | other_company_same_name |
ours`. **`no_company` covers a pending security login or TallyVault prompt** — Tally answers an empty company list
(`p24_C_security_login_pending_company_list.xml`, `p24_C_vault_prompt_pending_company_list.xml`) exactly like no
company open (`p02_A_active_a_no_company.xml`) (probe 24, LESSONS rule 26); S1 never distinguishes them.
Server: `last_seen_at`, `last_heartbeat`, `clock_skew_s = pc_clock − server now`; if `ours` and counters < cursors →
§8.6; if `ours` and counters == cursors → `caught_up_at = now`; if `other_company_same_name` → `relink_prompt`.
Runs one D20 slice. Response:
```json
{"server_time": "…", "sync_state": "ready", "cursors": {"alt_vch_id": 965, "alt_mst_id": 412},
 "commands": [{"id": "…", "type": "recheck_now", "params": {}}]}
```
*(Changed 2026-09-29 — review I3, as built:)* **an ack means "received", not "executed".** The server first applies
`acked_commands`, then delivers what is still `pending` (`pending → delivered`), so a command is never delivered
twice. An acked command goes `delivered → done` — **except `confirm_resync`**, which stays `delivered` (it is still
the user's authorisation for the `full_resync` it names, D16) until that run completes (`done`) or a newer confirm of
the same or a wider scope supersedes it (`cancelled`, §7.15). Unknown, foreign or malformed ids in `acked_commands`
are skipped one by one, never failing the rest of the list.

### 7.7 `GET /api/sync/{ws}/state` (active device)
Cursors, coverage rows, open runs, pending commands, `books_from`, `base_currency_name`. The agent calls it at
start-up; its SQLite copy is only a cache (Part 1 §5).
*(Changed 2026-09-29 — Task 14b C5:)* `commands` lists every `pending` command **plus every `delivered`, still-open
`confirm_resync`**, so an agent that acked a confirm and then restarted recovers its id. Every entry carries
`status`: `{"id", "type", "params", "status"}`.

### 7.8 `POST /api/sync/{ws}/runs` and `PATCH /api/sync/{ws}/runs/{run_id}`
POST `{"kind": "incremental", "scope": null, "command_id": null, "counters_at_start": {"alt_vch_id": 965,
"alt_mst_id": 412}, "progress_total": null}` → `{"run_id": "…"}`.
Rules: one open `first_sync` at a time; `first_sync` only in `awaiting_first_connection | first_sync` (resume
re-opens the interrupted one: the response returns the existing run and its `coverage`); `full_resync` needs a
pending `resync` command (D16) → 409 `resync_not_confirmed`; `incremental` refused in `restore_detected` (409).
PATCH `{"status": "completed", "progress_done": 24, "progress_total": 24, "batches_declared": 37,
"cursor_after": {"alt_vch_id": 970, "alt_mst_id": 415}}` → cursors move (D15) if every declared batch is accepted,
else 409 `batches_missing` `{"missing": n}`. Completing a `first_sync` whose window coverage is complete →
`sync_state = ready`. Completing a confirmed whole-company `full_resync` 2-FY pass → `ready`, the command `done`.

*(Changed 2026-09-29 — build rulings + review I2/I4, as built:)*
- **Typed bodies.** POST: `kind` ∈ the four kinds, `counters_at_start` **required for every kind** (the contract's
  `Counters`: a `first_sync` without it would complete with NULL cursors and wedge the workspace). PATCH: `status` ∈
  `completed | failed`; `batches_declared` required when `completed` (422 `batches_declared_required`); completing an
  `incremental` without `cursor_after` → 422 **`cursor_after_required`**. A body that fails pydantic validation gets
  FastAPI's standard 422 (`{"detail": [...]}`), not the §11 envelope.
- `incremental` is refused (409 `run_kind_not_allowed`) while the cursors are NULL (no first sync has completed).
- Opening any run interrupts every **other** device's `running` run (`interrupted`); "resume re-opens the interrupted
  one" applies to the same device only. Opening a `first_sync` recomputes the window FYs' `months_total` to the
  month the run starts; a window FY that was `complete` at the old, smaller total drops back to `running`.
- A `full_resync`'s `scope` must equal its command's confirmed scope (`{"company": true}` ↔ `{"scope": "company"}`,
  `{"fy_start"}` ↔ `{"scope": "fy", "fy_start"}`) and the command may not already drive another `running` run, else
  409 `resync_not_confirmed`. Completing **any** confirmed resync (company or FY) marks its command `done`.
- **Completing a company `full_resync`** also clears `restore_reason`, removes a restore/relink
  `ladder.resync_offered` (a parity offer is kept — it is the ladder's own), and cancels every other open
  `confirm_resync` (review I4).
- `failed` with `error_code` ∈ {`company_mismatch`, `unrecoverable`} on a `first_sync` → `sync_state = error`.

### 7.9 `POST /api/sync/{ws}/batches` (active device; gzip)
```json
{"batch_id": "0b8e…", "run_id": "…", "company_guid": "138b7373-753c-4dbe-aa63-b802035f0ba9",
 "chunk": {"from": "2022-09-01", "to": "2022-09-02"},
 "objects": [ {"kind": "voucher", "data": {…}} ],
 "quarantine": []}
```
200:
```json
{"batch_id": "0b8e…", "status": "accepted", "replayed": false,
 "counts": {"inserted": 18, "updated": 2, "skipped_older": 0, "balances_applied": 0, "balances_stale": 0},
 "warnings": [{"index": 4, "code": "guid_prefix_foreign"}],
 "reread_ledgers": []}
```
A replay of the same `batch_id` with the same body returns the stored response with `"replayed": true`; the same
`batch_id` with a different body → 409 `batch_id_reused`. Rejections: §7.14 and §11.

### 7.10 `PATCH /api/sync/{ws}/coverage` (active device)
`{"fy_start": "2022-04-01", "month": "2022-09", "run_id": "…"}` → adds the month to `months_done` (idempotent),
recomputes `months_complete`, state (`running` → `complete` when all months done), both edges and the backfill copy
(§8.3). `{"fy_start": "2026-04-01", "action": "add_fy"}` (rollover) → a `complete` row (Part 1 §4 "New financial
year") and schedules the raw purge of the FY that left the window (§4.9). Response: the row + both edges.
*(Changed 2026-09-29 — review I6, as built:)* the body is typed (`fy_start` a date, `month` `YYYY-MM`, `action`
only `add_fy`; `month` required unless `add_fy`). An unknown `fy_start` → 404 `fy_not_found`. The month must lie in
**[max(`fy_start`, `books_from`), min(`fy_end`, today IST)]** by calendar month, else 422 **`month_out_of_range`**
before any write — otherwise an unsynced month could complete an FY and move the verified edge parity trusts. (The
`complete → running` drop on a re-opened `first_sync` is in §7.8.)

### 7.11 `POST /api/sync/{ws}/reconcile` (active device)
```json
{"run_id": "…", "scope": {"kind": "vouchers", "from": "2023-02-01", "to": "2023-02-28"},
 "present": [{"guid": "138b…-000000c9", "alter_id": 963}, …], "present_count": 81, "confirm_large": false}
```
or `{"kind": "masters", "master_type": "ledger"}`. Server: rows in scope, not deleted, whose GUID is absent →
soft-deleted (vouchers: lines removed, D19); `present` GUIDs unknown to us or with a higher `alter_id` → `refetch`.
Guard D28. A ledger soft-delete while live lines reference it → 409 `master_in_use` (Tally itself refuses that
delete, LESSONS rule 30). Response `{"soft_deleted": 1, "refetch": ["…"], "reread_ledgers": [{"guid", "name"}]}` —
the ledgers the deleted vouchers touched (Part 1 §4 "After a gap").
*(Changed 2026-09-29, as built:)* `len(present) != present_count` → 422 `reconcile_list_incomplete`, nothing
soft-deleted (S1-R8's truncation guard). **Reconcile is the only way a row leaves after a restore:** a confirmed
resync's ingest never deletes (§8.7), so objects created after the backup and absent from the restored Tally go
through this endpoint — vouchers first, since a ledger still referenced by live lines is 409 `master_in_use` until
its vouchers are reconciled. The `refetch` rule (`present.alter_id > stored`) does not flag a restored *lower*
alter_id; the resync's own batches carry those.

### 7.12 `POST /api/sync/{ws}/snapshots` (active device)
```json
{"report_type": "trial_balance", "from_date": "01-04-2022", "as_on_date": "31-03-2023",
 "request_flags": {"EXPLODEFLAG": "Yes"}, "purpose": "anchor", "captured_at": "2026-09-25T16:52:49+05:30",
 "counters": {"alt_vch_id": 965, "alt_mst_id": 412},
 "cells": [{"dspdispname": "Capital Account", "dspcldramta": "", "dspclcramta": "1000000.00"},
           {"dspdispname": "Current Assets", "dspcldramta": "-1954753.74", "dspclcramta": ""},
           {"dspdispname": "Opening Stock", "dspcldramta": "-24450.00", "dspclcramta": ""},
           {"dspdispname": "Unadjusted Forex Gain/Loss", "dspcldramta": "-183.87", "dspclcramta": ""}]}
```
(cells from `p18_B_tb_asof_2023-03-31.xml`). Cell keys per type: TB `dspdispname / dspcldramta / dspclcramta`;
Stock Summary `dspdispname / dspclqty / dspclrate / dspclamta` (`p12_A_stock_summary_today.xml`); Bills
`billdate / billref / billparty / billcl / billdue / billoverdue` (`p23_B_bills_receivable_due.xml`); BS
`dspdispname / bssubamt / bsmainamt`; P&L `dspdispname / plsubamt / bsmainamt` (`p12_A_*`). Validation: D8 period
convention, `as_on_date >= books_from`, every amount cell parses (D3). Response `{"stored": true, "replaced":
false, "row_count": 18, "synthetic_rows": ["Opening Stock", "Unadjusted Forex Gain/Loss"], "imbalance": "0.00"}`.

### 7.13 `GET /api/workspaces/{id}/sync-status` (web JWT, owner)
```json
{"sync_state": "ready", "restore_reason": null,
 "first_sync": null,
 "last_synced_at": "…", "caught_up_at": "…",
 "agent": {"device_name": "ACCOUNTS-PC", "last_seen_at": "…", "tally_status": "ours", "agent_version": "0.1.0",
           "clock_skew_s": 3},
 "backfill": {"oldest_available_fy": "2022-04-01", "oldest_complete_fy": "2022-04-01",
              "books_from": "2022-04-01", "percent": 100.0, "state": "complete"},
 "last_parity": {"state": "ok", "checked_at": "…", "as_on": "2026-03-31", "mismatch_count": 0,
                 "verified_from": "2022-04-01"},
 "relink_prompt": null, "resync_offered": null, "quarantine_count": 0, "storage_alert": false}
```
`first_sync` = `{"percent": 60.0, "done": 14, "total": 24}` while `sync_state = first_sync`. `suspect` is reported
as `ok` here (Part 1 §6 "`suspect` is invisible").

### 7.14 `POST /api/sync/{ws}/parity` (active device) — §10
```json
{"scope": "daily", "as_on_date": "31-03-2026", "capture_started_at": "…",
 "counters_before": {"alt_vch_id": 965, "alt_mst_id": 412},
 "counters_after":  {"alt_vch_id": 965, "alt_mst_id": 412},
 "remediation_done": []}
```
The agent has already uploaded, in this capture: all ledgers' `ledger_balance` objects, `trial_balance` and
`trial_balance_ledgerwise` as-on `as_on_date`. Response:
```json
{"parity_run_id": "…", "status": "suspect",
 "summary": {"ledgers_compared": 27, "groups_compared": 6, "mismatches": 1, "match_revalued": 1,
             "forex_unrealised_total": "183.87"},
 "remediation": [{"id": "…", "action": "refetch_ledger_vouchers",
                  "params": {"ledger_guid": "…", "fy_start": "2025-04-01"}}],
 "ladder": {"state": "suspect", "heal_attempts": 0, "resync_offered_fy": null}}
```

### 7.15 `POST /api/sync/{ws}/relink` (device) and `POST /api/workspaces/{id}/sync/commands` (web)
Relink: `{"new_company_guid": "…", "company_name": "…", "password": "…"}` → requires a pending `relink_prompt`
with that GUID and the website password (Q25) → GUID replaced, old one appended to `previous_company_guids`,
`sync_state = restore_detected`, `restore_reason = relink`, a `resync` offer recorded (never started).
Web commands: `{"type": "recheck_now"}`, `{"type": "confirm_resync", "scope": "company" | "fy", "fy_start"}`,
`{"type": "confirm_relink", "password": "…"}` → a `sync_commands` row the next heartbeat delivers (relink is applied
at once, same service as the device path).
*(Changed 2026-09-29, as built — reviews I3/I5, Task 14b C3:)*
- **Relink password re-check** (device and web alike) uses the **same per-email limiter as `/login`**: checked first
  (429 `rate_limited` + `Retry-After` at capacity), a hit recorded only on a **failed** attempt (401
  `invalid_credentials`) — a stolen device token can't turn relink into a password oracle, and both paths share one
  budget. No pending `relink_prompt` for that GUID → 409 `relink_not_prompted`; the new GUID bound to another of the
  user's workspaces → 409 `company_bound_elsewhere`.
- **Web commands** are a closed set (`recheck_now | confirm_resync | confirm_relink`; `scope` required for
  `confirm_resync`, `fy_start` for `scope = fy`, `password` for `confirm_relink`); anything else is a 422.
- **Supersede by scope.** A new `confirm_resync` cancels (`cancelled`) the open (`pending`/`delivered`) confirms it
  covers — **same or narrower only**: a company confirm covers every confirm; an FY-X confirm covers only FY X (it
  never cancels an open company confirm). A command bound to a still-`running` run is never cancelled. A completed
  company resync cancels every other open confirm (§7.8).

### 7.16 `GET /api/devices`, `DELETE /api/devices/{id}` (web JWT)
The user's devices (name, workspace, active, last seen, revoked). DELETE revokes (`user_removed`); the device's
next call gets 401 `device_revoked` and the agent shows "Signed out" (Part 1 §5).

---

## 8. Sync bookkeeping

### 8.1 Device checks on every sync call (in order)
1. Access JWT valid, `typ = v2_device` → else 401 `token_invalid` / `token_expired`.
2. Device exists, not revoked → else 401 `device_revoked` (with `reason`).
3. Path `{ws}` = the device's workspace → else 403 `wrong_workspace` (cross-tenant, R19).
4. Workspace not deleted → else 410 `workspace_deleted` (device revoked `workspace_deleted`).
5. Device is the workspace's active device → else 409 `not_active_device`.
6. Per-device rate limit → else 429 `rate_limited` + `Retry-After`.

### 8.2 `sync_state` machine

| From | Event | To |
|---|---|---|
| (none) | bind | `awaiting_first_connection` |
| `awaiting_first_connection` | `first_sync` run opened | `first_sync` |
| `first_sync` | first_sync run completed and window FYs `complete` | `ready` |
| `first_sync` | run `failed` with a fatal code / quarantine > `V2_QUARANTINE_ERROR_THRESHOLD` | `error` |
| `error` | next run completed | `ready` (or `first_sync` if the window isn't complete) |
| `ready` / `error` | heartbeat counters < cursors (§8.6) | `restore_detected` (`counters_backwards`) |
| any bound | re-link applied | `restore_detected` (`relink`) |
| `restore_detected` | confirmed whole-company `full_resync` completes its 2-FY pass | `ready`; `restore_reason` and the restore/relink offer cleared *(Changed 2026-09-29, review I4)* |
| any | device revoked / taken over | unchanged (status shows the agent) |

Chat is never locked by `restore_detected` (decision 13); only `awaiting_first_connection` and `first_sync` lock it
(Part 2/3 read this).

### 8.3 Coverage and the two edges
- Rows created at bind (`pending`) from FY(`books_from`) to the current FY; `months_total` counts months from
  `max(fy_start, books_from)` to `min(fy_end, the month the first sync started)` for window FYs, and to `fy_end` for
  older FYs.
- `running` on the first acked month; `complete` when `months_done` covers `months_total`.
- Whole-company resync: `complete → resyncing`; `running → pending` with `months_done = []`; `pending` stays
  (Part 1 §4). Single-FY resync: that FY only.
- **Available edge** = the oldest FY reached from the current FY going back without a gap whose rows are
  `complete | resyncing`; **verified edge** = same, `complete` only. Both stored as FY start dates.
- `backfill_state`: `resyncing` if any row `resyncing` from a whole-company resync; `complete` when
  `oldest_complete_fy = FY(books_from)`; else `running`. `backfill_percent` = Σ months_complete / Σ months_total
  over pre-window FYs (100 when there are none — a company younger than the window).

### 8.4 Heartbeat → state
See §7.6. `tally_status` values are the agent's reading of Tally (S2's gate); S1 stores them and exposes them. A
missing heartbeat for > 3 cycles is Part 3's "agent offline"; S1 exposes `last_seen_at` only.

### 8.5 `last_synced_at`
Moved by an accepted batch whose run is `first_sync` or `incremental`; by a `full_resync` batch only when its
`chunk` lies inside the current 2-FY window or it carries masters; **never** by `backfill` batches, heartbeats,
snapshots or parity (Part 1 §5, decision 7b). `caught_up_at` (D21) is the heartbeat-side companion.

### 8.6 Restore detection (D17)
Heartbeat `ours` with `alt_vch_id < cursor_alt_vch_id` **or** `alt_mst_id < cursor_alt_mst_id` → `restore_detected`
(`counters_backwards`), a `resync` **offer** in `ladder.resync_offered = {scope: company, reason: restore}`, and
`incremental` runs / batches refused (409 `restore_detected`). Probe 13 (company A): a file restore keeps the GUID
and MasterIDs and the counters fall back (`p13_A_before_backup_counters.xml` → `p13_A_after_restore_counters.xml`).

### 8.7 Authority inside a confirmed resync *(new 2026-09-29 — S1 build, Task 13/14b rulings C2)*
After a restore or a relink, Tally's objects can carry **lower** AlterIDs than the mirror's (the counters went back),
so the §12 step 9/12 rule "lower alter_id → `skipped_older`" would keep the post-backup rows the user just chose to
discard. Inside a `full_resync` run — every one is user-confirmed, since the server refuses one without a confirmed
command (D16) — the alter_id rule is **suspended per object, bounded by the run's scope**:

| Run scope | Masters | Vouchers |
|---|---|---|
| company (`{"company": true}`) | authoritative | authoritative (every date) |
| single FY (`{"fy_start": X}`) | **not** authoritative (keep `skipped_older`) | authoritative only when `fy_start_of(date) == X`; other-FY vouchers keep the rule |
| any other run kind | rule applies | rule applies |

An authoritative object replaces the stored row whatever its alter_id and counts as **`updated`**. Ingest never
deletes; objects that no longer exist in the restored Tally leave via §7.11 reconcile.

---

## 9. Auth and devices

### 9.1 Tokens (D6)
| | Access | Refresh |
|---|---|---|
| Form | JWT HS256, `V2_DEVICE_TOKEN_SECRET` | 32 random bytes, base64url |
| Claims | `sub` device id, `uid` user id, `ws` workspace id or null, `typ: v2_device`, `jti`, `iat`, `exp` | — |
| Life | 15 min (`V2_DEVICE_ACCESS_MINUTES`) | 90 days sliding (`V2_DEVICE_REFRESH_DAYS`) |
| Stored | nothing | SHA-256 (`refresh_hash`), previous hash for reuse detection |
| Rotation | — | every refresh |

`ws` is refreshed into the access token after binding (the bind response returns a new access token).

### 9.2 Device states

| State | Meaning | Can call |
|---|---|---|
| `unbound` | logged in, `workspace_id` null | login/refresh/logout, `GET /api/agent/workspaces`, `POST /api/sync/company` |
| `active` | bound, `is_active` | everything for its workspace |
| `standby` | bound, not active (lost a take-over race) — not reachable in v1, since take-over revokes | — |
| `revoked` | `revoked_at` set | nothing (401 `device_revoked` with reason) |

### 9.3 One active device, take-over
`POST /api/sync/company` in one transaction: lock the `sync_workspaces` row (`SELECT … FOR UPDATE`), revoke the old
active device, set the new one active; the partial unique index `(workspace_id) WHERE is_active` makes a race
impossible. The revoked device's in-flight batch fails at check 2 and stays in its outbox (Part 1 §5: "keeps its
outbox"); nothing it sends later is stored.

### 9.4 Web side
`get_current_user` is a v2 copy of `backend/api/dependencies.py` (same `JWT_SECRET`, `type = access`). Only the
workspace owner (`workspaces.user_id`) may read `sync-status` or post commands (Q28).

---

## 10. Parity engine

### 10.1 Preconditions (checked in this order; each yields a stored run with that status and no alert)
0. *(Changed 2026-09-29 — review M20, Task 14b C4; before any row is stored.)* Outside `bisect`, `as_on` must lie in
   Tally's current period — the FY the mirrored balances (and their face fields) describe. The server stores no
   current period, so the check is best-effort: **422 `as_on_not_current_period`** (no run stored) when FY(`as_on`)
   is **older** than the FY of the latest voucher that is not deleted, not post-dated, not optional and dated
   ≤ today IST. A later `as_on` (a new FY with no voucher yet) is never refused. The binding rule is the S2 contract
   (§17.3): the agent sends Tally's current-period end. Also: unknown `scope` → 422 `invalid_scope`; unparseable
   `as_on_date` → 422 `bad_as_on_date`; `bisect` without an FY-start `fy_start` → 422 `fy_start_required`.
1. `counters_before != counters_after` → `aborted_moving` (quiescence guard; probe 19: quiet captures are stable,
   a mid-capture voucher is detected — `p19_A_capture_moving_counters_*.xml`).
2. `counters_before != stored cursors` → `aborted_behind` (the server's own proof that the outbox is drained and the
   cursor caught up — Part 1 §6 "only when the outbox is empty and the cursor is caught up").
3. No verified span (current FY not `complete`), or `sync_state` in `first_sync | restore_detected` → 
   `aborted_incomplete` `no_verified_span`. *(As built: also `awaiting_first_connection`, and `as_on < E`.)*
4. A required snapshot missing (`trial_balance` / `trial_balance_ledgerwise` as-on `as_on_date`, the anchor TB for
   `E`) → `aborted_incomplete` with remediation `capture_snapshot {report_type, as_on}`. *(As built: the anchor is
   missing only when neither the ledger-level TB nor the group TB as-on the anchor date is stored — the group TB feeds
   the no-ledger-anchor route, §10.2.)*
5. Ledgers whose `balance_captured_at < capture_started_at` → they were not in this capture's ledger list: kept for
   step 10.4 (`missing_in_tally`), not a precondition failure.
6. **TB imbalance guard (D10):** `imbalance` = Σ all parsed rows of the group TB (primary rows + synthetic rows).
   If a baseline exists with the **same** `alt_mst_id` and `|imbalance − baseline| > tolerance` →
   `discarded_stale` (cause `stale_tally`, tray "Restart TallyPrime to refresh", Part 1 R4). If `alt_mst_id` moved or
   no baseline → record the new baseline and continue. Real values: company B **0.00** (`p18_B_tb_asof_2023-03-31.xml`,
   incl. the forex row); company A **non-zero** (seed opening defect, Part 1 header "Settled 2026-09-23").
   *(Changed 2026-09-29, as built — rulings F2, I5, I6:)* "all parsed rows" means: first-occurrence **top-level group**
   rows + top-level **ledger** rows directly under Primary (e.g. `Profit & Loss A/c`) + the `Unadjusted Forex
   Gain/Loss` row (`Opening Stock` is already inside the stock-bearing group's row). **Top-level = the workspace's own
   stored, live masters whose parent is Primary**, so a custom or renamed top-level group counts; before any group
   master is stored it falls back to the 15 reserved primary-group names (and `Profit & Loss A/c`). The imbalance is
   recomputed at parity time from the snapshot's cells with the **current** masters. A TB with no top-level group row
   has imbalance NULL (unknown, never 0) → `aborted_incomplete` `tb_imbalance_unknown` + `capture_snapshot`. A
   rebaseline is written only with a computed run, never by an abort.
7. *(New 2026-09-29 — review I7, Task 14b C1.)* **Stale stored anchor.** A stored ledger-level TB (the D9 anchor; in
   `bisect` also every month-end TB) is captured once and reused, but a later rename is exported retroactively
   (probe 8), so its old-name row resolves to no live ledger — a false `masters_gap` + `ledger_gap` no remediation
   fixes. Such a snapshot is **stale** when it has rows resolving to no live ledger **and** its
   `counters.alt_mst_id != cursor_alt_mst_id` → `aborted_incomplete` **`anchor_stale`** + `capture_snapshot` for it.
   Because precondition 2 already requires `counters_before == cursor`, a re-capture made now is never stale: a
   genuinely missing master costs one re-capture, then classifies as `masters_gap`. `!=` rather than `<`: a snapshot
   *above* the cursor comes from a counter space a restore/relink has since discarded. The group-TB anchor route is
   not checked (deferred minor).
8. *(New 2026-09-29, as built — 10c carry.)* After computing, a run with no mismatch that verified **no**
   balance-sheet figure at either rung → `aborted_incomplete` `no_balance_sheet_verified` + `capture_snapshot` for the
   anchor — never `ok`.

### 10.2 Inputs per run
- `E` = verified edge (FY start). `as_on` = request `as_on_date` = **the current period's end** (probe 16's rule,
  Part 1 §6 "Rung 1": 31-03-2026 on company A).
- Mirrored ledger balances captured at `≥ capture_started_at`.
- TB (group) and ledger-level TB as-on `as_on`, from FY(`as_on`) start (D8).
- Anchor per D9: ledger-level TB as-on `E − 1`; or, when `E = books_from`, as-on `books_from` minus our lines dated
  `books_from`. *(Changed 2026-09-29, as built:)* `E` = max(verified-edge FY start, `books_from`), so a company whose
  books start mid-FY takes the books-start anchor. The books-start subtraction is **required by Tally's export**, not
  a convention: a ledgerwise TB with `SVFROMDATE = SVTODATE = books_from` exports closing columns only, i.e. the
  balance at the **end** of that day including its vouchers (G6, §13.3; LESSONS §15 rule 31). A ledger absent from the
  TB but with day-one lines anchors at minus those lines. **No-ledger-anchor route:** when no ledgerwise anchor TB is
  stored, the **group TB as-on the anchor date** is the input — first-occurrence primary rows, net of that TB's own
  `Opening Stock` and of our day-one lines — and rung 2 compares BS groups as `anchor_g + Σ lines in [E, as_on] +
  unrealised_g + Opening Stock` (§10.6); a group holding forex ledgers whose revaluation can't be attributed stays
  `not_applicable` (`forex_unsplit`).
- Lines: `countable` lines with `E ≤ voucher_date ≤ as_on` (**post-dated included** — probe 16: counted by Tally and
  exported `IsPostDated=Yes`, `p16_A_post_dated_voucher.xml`; cancelled, optional and deleted excluded).

### 10.3 Rung 0 (at ingest, §12 step 7)
Σ `amount` over each voucher's `ledger_entries` (INR base) must be exactly `0.00` → else `unbalanced_voucher`.

### 10.4 Rung 1 — ledger level (balance-sheet ledgers)
For every live ledger whose group `nature ∈ {assets, liabilities}`, not forex (forex → §10.5):
```
computed_L = anchor_L(E) + Σ amount of L's countable lines, E ≤ date ≤ as_on
tally_L    = mirrored closing_balance (this capture)
verdict    = match if |computed_L − tally_L| ≤ tolerance else mismatch
```
`anchor_L(E)`: the ledger-level TB row whose name resolves to L (by the D13 resolver, ledgers only — the TB's
`Opening Stock` and `Unadjusted Forex Gain/Loss` rows are synthetic and never resolve); a ledger absent from the TB
has anchor 0. Other verdicts: nominal ledger → `not_applicable` (cause `nominal`, **never `match`**); `Profit & Loss
A/c` → `not_applicable` (`pl_account`, D11); group `nature` NULL → `not_applicable` (`unclassified_group`);
`balance_source = needs_tb` → Tally figure from the ledger-level TB row; **no ledger-level anchor available** (the
`ISLEDGERWISE` route fails on a customer's Tally, Part 1 §6 fallback) → every BS ledger `not_applicable`
(`no_ledger_anchor`, never `match`) and rung 2 carries the check at group level; a live ledger not in this capture →
`missing_in_tally`; a TB row whose name resolves to no ledger → `missing_in_db`.

### 10.5 Forex ledgers — C47 (D4)
Tally values a forex ledger at face total × its latest voucher rate; our lines carry each voucher's stated base.
The difference is **expected**; the engine proves it is a revaluation and not a missing voucher.

**(a) Mirrored balance (face available).** `tally_L` is the expression `-$1609.71 @ ? 82.58/$ = -? 132929.85`.
```
face_ours  = opening_fx_amount(L)  (the master OpeningBalance's face: current-FY opening, C46 — exactly
                                    right here because the sum below starts at the current FY)
           + Σ fx_amount of L's countable lines, FY(as_on).start ≤ date ≤ as_on
face_check = |face_ours − closing_fx_amount(L)| ≤ 0.01
self_check = |closing_fx_amount × closing_fx_rate − closing_balance| ≤ 0.01
unrealised = tally_L − computed_L          (computed_L as in rung 1, INR)
verdict    = match_revalued (unrealised stored)  if face_check and self_check
           = mismatch, cause forex_face_mismatch  otherwise
```
Any countable line on L **without** a face value (a plain-INR line on a forex ledger) makes the face sum unknowable →
`not_applicable`, cause `forex_face_incomplete`; (b) still covers L.

**(b) TB rows (INR only), and the group check.** Over all forex ledgers F at a TB date `d`:
```
raw_diff_L   = tb_row_L(d) − computed_L(d)                 for L in F
unadjusted_d = the TB's "Unadjusted Forex Gain/Loss" row at d (0 if absent)
Δunadjusted  = unadjusted_d − unadjusted_(E−1)          (Changed 2026-09-28; E−1 = the anchor TB's row;
                                                          at E = books_from: the TB as-on books_from's row, 0 if absent)
accept iff   | Σ raw_diff_L + Δunadjusted | ≤ tolerance
```
*(Changed 2026-09-28: `computed_L` starts from the E−1 anchor, which Tally has already revalued, so only the change of
the cumulative unadjusted row since E belongs in the sum. `raw_diff_L` / `unrealised` are therefore "since E".)*
Accepted → each L `match_revalued` (if not already mismatched by (a)); rejected → every L with a non-zero diff is
`mismatch`, cause `forex_revaluation_unexplained`. **Non-forex ledgers never get an allowance**, so a missed forex
sale still shows at its full base on the INR side (e.g. `Export Sales`) and the set sum moves too.

Real numbers (company B, 31-03-2023, `p18_B_tb_asof_2023-03-31.xml` + `p22_B_*`): ours for the USD party
= −37,216.04 − 95,897.68 = −1,33,113.72; Tally −1,32,929.85; `raw_diff` +183.87; TB row `Unadjusted Forex
Gain/Loss` −183.87, anchor row at books_from 0 → Δ −183.87 → Σ = 0.00 → accepted. Face: −448.44 − 1,161.27 = −1,609.71 = Tally's face; −1,609.71 × 82.58 =
−1,32,929.85 ✓. **New observation from that capture (not recorded in S0):** the TB carries a synthetic top-level
`Unadjusted Forex Gain/Loss` row — Indirect Expenses (−55,333.11) equals its only ledger `Bank Charges`, so the row is
outside it — and with it the TB nets to **0.00**; LESSONS rule 29(b)'s "TB total is out by the difference" holds only
when that row is left out. Added to LESSONS rule 29 as a dated note.

Scope caveat (C47 review M1, exception (g)): which voucher's rate Tally uses (latest-dated vs last-entered) and
whether a later rate revalues earlier as-on dates are unmeasured. The design **doesn't depend on either**: (a)
checks face totals and Tally's own stated rate/base; (b) checks against Tally's own offset row.

### 10.6 Rung 2 — group level, with ledger resolution
At `as_on`:
- **Nominal ledgers (per ledger, from the ledger-level TB):** `computed_L = Σ lines in [FY(as_on).start, as_on]`
  (nominal ledgers restart each FY) vs the TB row → `match | mismatch | missing_in_db`.
- **Primary groups (from the group TB):** rows matched by reserved primary-group name, **first occurrence**
  (`reads.primary_group_rows`: company A has a *ledger* also called `Capital Account` right after the group row,
  `p16_A_tb_fy_end.xml`). *(Changed 2026-09-29, as built:)* row **matching** stays reserved-name only — a custom
  top-level group is not a rung-2 row, and its ledgers have no derivable nature (§4.4 `unmapped_primary`), so they
  are `not_applicable` (`unclassified_group`), never `match`. Custom top-level groups count only in the §10.1 step 6
  imbalance, which nets the workspace's own top-level masters.
  - Balance-sheet group g: `computed_g = Σ computed_L over live ledgers under g (any depth) + Σ accepted forex
    unrealised under g + (the TB's Opening Stock row, if g is the stock-bearing group)`. The Opening Stock row is
    read from the **same** response (LESSONS rule 19) and is not the Stock Summary closing.
  - Nominal group g: `Σ nominal computed_L under g`.
- **Synthetic rows:** `Opening Stock` (added to the stock-bearing group), `Unadjusted Forex Gain/Loss` (used by
  §10.5 (b), not compared on its own).
- Group verdicts use the same tolerance; a group whose ledgers all match but whose rollup doesn't →
  cause `group_walk_wrong` (engineering flag).

### 10.7 Cause classifier (Part 1 §6 table, made concrete)

| Signature (evaluated in order) | Cause | Remediation (returned to the agent) |
|---|---|---|
| A TB row resolves to no live ledger | `masters_gap` | `refetch_masters` |
| A live ledger absent from the capture | `ledger_deleted` | `reconcile_masters {master_type: ledger}` |
| Mismatch diff equals ± the ledger's amount on one cancelled/optional voucher *(as built: optional only — a cancelled voucher exports no amounts, ruling F7)* | `flag_filter_inverted` | none — engineering flag |
| Two ledgers differ by equal and opposite amounts *(as built, ruling F5: any set of ≥ 2 remaining mismatches whose diffs net to 0 ± tolerance — a dropped GST sale moves 4 ledgers)* | `voucher_missed_or_duplicated` | `month_bisect {fy_start, month_ends}` |
| *(Changed 2026-09-29)* ≥ 3 remaining BS ledgers that have an anchor row show a **systematic anchor error**: (a) **shifted** — they share one identical non-zero diff (± tolerance); or (b) **dropped / double-counted** — each diff is ± its own anchor row (from the anchor snapshot's resolved rows or the anchor used): dropped gives `+row` (diff = Tally − ours), double-counted `−row` | `anchor_wrong` | `capture_snapshot {trial_balance_ledgerwise, E−1}` + `refetch_masters` |
| One ledger differs *(as built: also the fallback for every mismatch no earlier row consumed, one remediation per ledger — classify never returns an unlabelled mismatch)* | `ledger_gap` | `refetch_ledger_vouchers {ledger_guid, fy_start}` |
| Forex face mismatch / unexplained revaluation | `forex_gap` | `refetch_ledger_vouchers` for the forex ledger |
| All ledgers match, a group doesn't | `group_walk_wrong` | none — engineering flag |
| TB imbalance moved without a master change | `stale_tally` | run discarded; `tally_notice restart` |

`month_ends` lists only month-ends with no stored TB (month-end snapshots are reused, Part 1 §6 "Month-bisect").

*(Changed 2026-09-29 — row 5, Task 12 fix-round ruling:)* the earlier reading ("each differs by its own anchor row"
only) made a shifted anchor unreachable, which defeated the row. **Known overlap:** three BS ledgers that each miss
a same-amount, non-netting voucher also match (a) and classify `anchor_wrong`; that is harmless — the remediation
(re-capture the E−1 ledgerwise TB + refetch masters) is cheap and non-destructive, and a persisting mismatch climbs
the ladder as usual.

### 10.8 Escalation ladder (per workspace, `ladder` column)
- Run with no mismatch → `ok`, `heal_attempts = 0`, offer cleared.
- Mismatch after `ok` → `suspect` (**invisible**), remediation issued.
- Mismatch after `suspect`/`alert` **and** the request lists the previous remediation ids in `remediation_done` →
  `alert`, `heal_attempts += 1`. Without `remediation_done` the state is kept and the same remediation re-issued.
- `heal_attempts ≥ 2` → `resync_offered_fy` = the bisected month's FY, else the newest verified FY; the web shows the
  offer; nothing starts until confirmed (decision 12).
- A confirmed single-FY `full_resync` completes for that FY and the next run still mismatches → `hard_alert` + ops
  signal `engineering_flag`.
- Aborted / discarded runs never change the ladder.
- *(Changed 2026-09-29, as built — ladder rulings I1/I2:)* "the previous remediation ids" are
  `ladder.pending_remediation_ids`; a previous run that issued **no** remediation counts as done, so a persistent
  mismatch still climbs. `hard_alert` is lowered only by an `ok` run. `bisect` runs don't step the ladder; they
  record `bisect_month`.

### 10.9 Month-bisect evaluation
`scope = bisect` with `fy_start`: the engine evaluates rungs 1–2 at every stored month-end ledger-level TB in that
FY (anchor D9), oldest first, and returns the **first diverging month** as `refetch_month {month}`. Missing
month-ends → `aborted_incomplete` with the `capture_snapshot` list. (Under Educational only 31-day month-ends are
readable, C43 — the agent's concern; the engine uses whatever month-ends exist.)

### 10.10 Storage, `last_parity`, retention, ops signal
Each run writes one `parity_runs` row and one `parity_lines` row per ledger/group compared (mismatches, not
applicable and matches alike; retention Q21 prunes matches after 7 days). `last_parity` is updated in the same
transaction. The **ops signal** (decision 14) is one structured log line, `v2.ops.integrity`:
`{workspace_id, run_id, rung, status, cause_counts, mismatch_count, max_abs_diff_bucket}` with buckets `<₹1k | <₹1L |
<₹1Cr | ≥₹1Cr` — no names, no GUIDs, no exact amounts.

---

## 11. Error handling

| Code | HTTP | Where | Retry? / agent action |
|---|---|---|---|
| `token_invalid`, `token_expired` | 401 | any device call | refresh |
| `refresh_expired`, `device_revoked`, `reauth_required` | 401 | auth | stop uploads, keep outbox, "Signed out" (Part 1 §5) |
| `wrong_workspace` | 403 | sync | none (bug) — ops signal |
| `workspace_not_found` | 404 | bind | re-pick |
| `workspace_deleted` | 410 | any | stop, "Signed out" |
| `not_active_device`, `takeover_required`, `workspace_bound_to_other_company`, `company_bound_elsewhere` | 409 | bind / sync | ask the user |
| `company_mismatch` | 409 | batches, snapshots | stop the cycle (gate bug; R2) — never retried |
| `restore_detected`, `resync_not_confirmed`, `batches_missing`, `batch_id_reused`, `reconcile_too_large`, `master_in_use`, `run_closed` | 409 | as named | per code |
| `batch_rejected` | 422 | batches | per object `code` below |
| `payload_too_large` | 413 | batches (> 5 MB gzip, > 500 objects, > 50 MB decompressed) | split and resend |
| `rate_limited` | 429 | any | back off as after a Tally timeout (Part 1 §5) |
| `bad_period` | 422 | snapshots | bug — ops signal |

Per-object codes inside `batch_rejected` — **retryable** (never quarantined): `missing_master`,
`ambiguous_master` (refetch masters, resend). **Deterministic** (quarantinable, D12): `unbalanced_voucher`,
`unparseable_amount`, `forex_base_missing`, `invalid_date`, `invalid_logical`, `missing_field`,
`duplicate_posting_list`, `unknown_kind`. A 500 is always retried with the same `batch_id` (idempotent).

*(Changed 2026-09-29 — the as-built code list; every code the build added, from `ApiError` call sites in
`v2/cloud/`:)*
- **`company_mismatch`** is raised for **batches only** — a snapshot body carries no company GUID (review M8).
- **500 `internal_error`** (any route): an unhandled DB error. The body is `{"error": "internal_error", "detail":
  ""}`; the log line carries only the exception **class** names, and the engine runs with `hide_parameters=True`, so
  no bound business value reaches a log (review I1). Retried like any 500.
- **409:** `run_kind_not_allowed` (runs: `incremental` with NULL cursors), `relink_not_prompted` (relink without a
  matching `relink_prompt`), `ambiguous_master` from `/parity` (a TB row name matches two live ledgers; retryable).
- **422 (request-level):** `cursor_after_required`, `batches_declared_required`, `invalid_status`, `invalid_run_kind`
  (runs); `month_out_of_range` (coverage); `as_on_not_current_period`, `bad_as_on_date`, `fy_start_required`,
  `invalid_scope` (parity; `invalid_scope` also on reconcile); `reconcile_list_incomplete` (reconcile);
  `invalid_books_from` (bind); `invalid_report_type`, `unparseable_amount` (snapshots); `invalid_body` (batches:
  bad gzip / JSON / shape). A body failing pydantic validation gets FastAPI's standard 422 `{"detail": [...]}`.
- **404:** `run_not_found`, `fy_not_found`, `device_not_found`. **403:** `account_inactive` (login).
- **Parity abort reasons** (`parity_runs.abort_reason`, not HTTP errors): `counters_moved`, `cursor_behind`,
  `no_verified_span`, `snapshot_missing`, `month_ends_missing`, `tb_imbalance_unknown`, **`anchor_stale`**,
  `no_balance_sheet_verified`, `stale_tally` (§10.1).
- **Per-object, deterministic (quarantinable), added:** `invalid_counter`, `invalid_captured_at`,
  `invalid_field_type` (a wrong-typed field or a non-object item) and `unexpected_parse_error` (a parser crash:
  logged by exception class + object kind/index only). `quarantine_code_not_allowed`: a `quarantine` entry citing a
  non-deterministic code. A stored rejection is replayed only when every code in it is deterministic; one holding a
  retryable code is re-evaluated as a fresh attempt on the same `batch_id`.
- **Warnings** (never reject): `guid_prefix_foreign`, `sign_vs_deemed_positive`, `ledger_guid_mismatch`,
  `is_revenue_disagrees`, `base_type_unresolved`.
- `sync_commands.status` includes **`cancelled`** (§4.2, §7.15).

---

## 12. Ingest pipeline and limits

One DB transaction per batch; any rejection rolls back everything.

| Step | Rule | Fixture that proves the shape |
|---|---|---|
| 1 | Size limits: gzip body ≤ `V2_INGEST_MAX_GZIP_BYTES` (5 MB), decompressed ≤ 50 MB (zip-bomb guard), ≤ `V2_INGEST_MAX_OBJECTS` (500) → else 413 | probe 21: a 200k/yr month is 623.9 MB of XML, so the agent must split |
| 2 | Idempotency: `(workspace_id, batch_id)` seen → replay stored response or 409 `batch_id_reused` | — |
| 3 | `company_guid` = bound GUID → else 409 `company_mismatch` (R2) | `p02_A_active_b.xml` (another company's GUID) |
| 4 | Run open, belongs to this device; `kind` rules (`restore_detected` blocks `incremental`) | — |
| 5 | Parse every object (§5.2); collect all errors, don't stop at the first | `p22_B_*` (expressions), `p15_B_*` (Hindi, compound units), `p25_A_groups.xml` (`&#4;`) |
| 6 | Order: currencies → groups → voucher types → units → stock groups → ledgers → stock items → balances → vouchers; masters within a kind by `alter_id` ascending | Part 1 §5 "Masters before vouchers" |
| 7 | Rung 0 per voucher (INR base, exact) | `p06_A_vouchers_nested.xml` (50/50), `p22_B_forex_sales.xml` (forex) |
| 8 | Resolve names per type (D13): parent, ledger, party, voucher type, stock item, unit, currency. Empty party on a cancelled voucher is allowed | `p03_B_flagged_month_2023_02.xml` |
| 9 | Upsert masters: `incoming.alter_id >= stored.alter_id` replaces, lower is `skipped_older`; renames keep the GUID and lines' joins (probe 8: old vouchers export the new name, their AlterIDs don't move, `p08_A_rename_*.xml`). *(Changed 2026-09-29:)* suspended for objects a `full_resync` is authoritative for (§8.7) — they replace at any alter_id and count `updated` | |
| 10 | Derive nature / base_type / is_forex; re-derive descendants of a moved master | `p25_*` |
| 11 | Balances (`ledger` master balances, `ledger_balance`, `stock_*`): applied only if `captured_at > balance_captured_at` (Part 1 "latest capture wins") | `p16_A_ledgers*.xml` |
| 12 | Upsert vouchers by the alter_id rule (*Changed 2026-09-29:* per voucher, suspended where §8.7 makes the run authoritative); replace lines, inventory lines, bill allocations; set `countable`, `voucher_date`, `has_forex`; `raw` per §4.9 | |
| 13 | Quarantine entries recorded; a stored GUID resolves an open quarantine row | — |
| 14 | Warnings: GUID prefix (D14), sign vs `isdeemedpositive` (D23), `is_revenue` disagreement, ledger GUID cross-check mismatch | |
| 15 | `last_synced_at` per §8.5; `sync_batches` row with the response | — |

**Performance budget:** a 500-voucher batch (~4 lines each) must ingest in < 2 s on a dev laptop (the DB test
asserts < 5 s to stay CI-stable); resolution uses one name → GUID map per kind loaded once per batch.

---

## 13. Fixtures

### 13.1 Fixture matrix — S0 captures S1 uses (all in `v2/tests/fixtures/sync/`)

| Category | Variant | Fixture | Used for |
|---|---|---|---|
| Vouchers | 50 vouchers, 4 types, nested bills + inventory + batches, company A | `p06_A_vouchers_nested.xml` | transcoder, rung 0 (50/50), posting rule, bill types (Sales New Ref, Receipt Agst Ref) |
| | FY 2025-26 full set, company A | `p16_A_vouchers_fy.xml`, `p18_A_vouchers_fy.xml` | parity real-data (A) |
| | Post-dated / future-dated voucher | `p16_A_post_dated_voucher.xml`, `p16_A_future_voucher.xml` | rung 1 counts them |
| | Month request (the extractor's), 12 months FY 2022-23, company B | `p21_B_fy2022_month_01..12.xml` | ingest at scale, coverage, parity real-data (B) |
| | Forex: 2 USD sales + INR sales | `p22_B_forex_sales.xml`, `p21_B_fy2022_month_09.xml` | D3, rung 0 on bases, C47 |
| | Cancelled / optional | `p03_B_flagged_month_2023_02.xml`, `p03_B_flagged_month_2023_07.xml`, `p03_B_vouchers_flags.xml` | flags, empty party, no lines |
| | Hindi narration; compound-unit voucher | `p15_B_hindi_narration.xml`, `p15_B_compound_unit_voucher.xml` | UTF-8, `10 Box 0 Nos` |
| | Deleted voucher (before/after) | `p07_A_throwaway_created.xml`, `p07_A_after_delete.xml` | reconcile soft-delete |
| | Rename (before/after, with vouchers) | `p08_A_rename_before*.xml`, `p08_A_rename_after*.xml` | rename by GUID |
| | Bills with credit periods | `p23_B_bills_credit_period.xml` | `credit_period_days` |
| Masters | Groups with custom sub-groups, `&#4; Primary` | `p25_A_groups.xml`, `p04_A_group_full.xml`, `p16_A_groups.xml`, `p18_B_group_list.xml` | nature walk, D31, GUIDs |
| | Voucher types incl. `Sales - GST` | `p25_A_voucher_types.xml`, `p25_B_voucher_types.xml` | base_type walk |
| | Ledgers with balances, custom sub-groups, `Capital Account` ledger = group name | `p16_A_ledgers.xml`, `p04_A_ledger_full.xml`, `p06_A_ledger_guids.xml` | resolver namespaces, mirrored balances |
| | Ledgers after the forex load (27 incl. USD) | `p18_B_ledger_list.xml`, `p22_B_usd_ledger.xml` | expression balances, `is_forex` |
| | GST ledgers (`TaxType = GST`) | `p23_A_gst_ledgers.xml` | `tax_type` |
| | Currencies (`$`, `?` = INR) | `p22_B_currencies.xml` | base currency, D30 |
| | Stock items, compound unit | `p04_A_stockitem_full.xml`, `p15_B_compound_unit_item.xml` | stock items, units |
| | Hindi ledger | `p15_B_hindi_ledger.xml` | UTF-8 names |
| | Duplicate-name refusal read-back | `p25_B_ledgers_after_duplicate_attempt.xml` | R9: names unique per company |
| Counters / company | counters baseline + after each change | `p01_A_counters_*.xml` | heartbeat counters, cursors |
| | restore: counters fall back | `p13_A_before_backup_counters.xml`, `p13_A_after_restore_counters.xml` | restore detection |
| | active company / other company / none | `p02_A_active_a.xml`, `p02_A_active_b.xml`, `p02_A_active_a_no_company.xml` | bind, `company_mismatch` |
| | secured / vault prompt pending (empty list) | `p24_C_security_login_pending_*.xml`, `p24_C_vault_prompt_pending_*.xml` | `tally_status = no_company` |
| | quiescence (quiet ×3, moving) | `p19_A_capture_quiet_*_counters_*.xml`, `p19_A_capture_moving_counters_*.xml` | `aborted_moving` |
| Reports | TB group-level, FY end, company A (`Capital Account` twice, Opening Stock) | `p16_A_tb_fy_end.xml`, `p12_A_tb_today.xml` | rung 2, first-occurrence rule, imbalance ≠ 0 |
| | Ledger-level TB (`ISLEDGERWISE`), company A | `p17_A_tb_exploded_isledgerwise.xml` | rung 1 anchors / rung 2 nominal |
| | EXPLODEFLAG / EXPLODEALLLEVELS (short by sub-group ledgers) | `p17_A_tb_exploded_explodeflag.xml`, `p17_A_tb_exploded_explodealllevels.xml` | negative test: never used as ledger-level |
| | TB as-on a past date, A and B (B incl. forex row) | `p18_A_tb_asof_2025-10-31.xml`, `p18_B_tb_asof_2023-03-31.xml` | anchors, C47 set rule |
| | Stock Summary, Bills Rec/Pay, BS, P&L | `p12_A_*`, `p18_A_*_asof_2025-10-31.xml`, `p23_B_bills_receivable_due.xml`, `p15_B_stock_summary.xml` | snapshot cell keys and parsing |
| Error shapes | no company (collection / report LINEERROR) | `p10_A_no_company_collection.xml`, `p10_A_no_company_report.xml` | transcoder refuses an error envelope |

### 13.2 Generated fixtures (FakeBooks / `company_b_data`, offline)
Built in tests from `v2/probes/setup/company_b_data.py` + `FakeBooks` (already emits Tally-shaped XML incl. the
forex expression for the USD party's balances, `b92a659`): the full company B (960 vouchers, 4 FYs) for the
mid-backfill and complete-history parity scenarios, and seeded faults (§15). Every generated XML passes through the
same transcoder as the real captures.

### 13.3 Fixture gaps (to capture in build task 0 — read-only Tally reads, or fall back to FakeBooks)
- **G1** company A ledger-level TB as-on `books_from` (01-04-2025): the D9 books-start anchor on real data.
- **G2** company B ledger-level TB with the USD ledger (current and as-on 31-03-2023): **is a forex ledger's TB row a
  plain INR number or an expression?** §10.5 handles both; the capture decides which path real data takes.
- **G3** company B group TB as-on 31-03-2026 (current) with the forex row.
- **G4** GUID + AlterID for voucher types, currencies, stock groups, units, and the USD ledger (§5.3).
- **G5** a mirrored `ledger_balance` re-read filtered to touched ledgers (the S2 request shape).
If Tally isn't available, each test that would use G1–G5 uses a FakeBooks-generated equivalent and is marked so;
the gap stays listed in the tracker until captured.

*(Changed 2026-09-29 — captured, as built:)* **G1–G5 were captured on 2026-09-25** (build task 0, read-only;
`v2/tests/fixtures/sync/s1_*.xml` + `.json` sidecars): G1 `s1_A_tb_ledger_asof_2025-04-01.xml`; G2
`s1_B_tb_ledger_asof_2023-03-31.xml` and `s1_B_tb_ledger_2025-04-01_2026-03-31.xml`; G3
`s1_B_tb_group_asof_2026-03-31.xml`; G4 `s1_{A,B}_{voucher_types,currencies,stock_groups,units}.xml`,
`s1_B_usd_ledger.xml`; G5 `s1_B_ledgers_touched.xml`. **G2's answer:** the USD ledger's ledger-level TB row is a
**plain INR number** (`-132929.85`), never an expression — §10.5 (b) is the path real data takes (S1-R2 closed;
LESSONS §15 rule 31).
- **G6** (new) company B ledger-level TB as-on books_from 01-04-2022 — **captured 2026-09-29**,
  `s1_B_tb_ledger_asof_2022-04-01.xml` (`SVFROMDATE = SVTODATE = 01-04-2022`, `ISLEDGERWISE = Yes`). Tally exports
  **closing columns only** (`DSPCLDRAMT` / `DSPCLCRAMT`): the balance at the end of 01-04-2022, **including that
  day's vouchers** — exactly G1's shape. So the books-start anchor is that TB **minus our own lines dated
  books_from** (D9, as for G1), and it equals the loader's ledger openings for every ledger
  (`test_g6_books_start_anchor_reconciles_with_the_dataset_openings`). B's books-start anchor is therefore now a
  **Tally figure** (tests `anchor_source=tally`); the A5 dataset-openings anchor is kept as a variant
  (`anchor_source=dataset`). The USD ledger has no row on 01-04-2022 (no opening; the unsuffixed `Gulf Office
  Supplies LLC` row is the INR debtor).

### 13.4 Mock data for DB/API tests
Users/workspaces are created by the test harness directly in the test DB (current tables' shape via a reflected
`Table`, test-only), never through current app code. Device tokens minted by `v2.cloud.auth`.

---

## 14. DB persistence scenarios (each re-reads from a fresh session and asserts the whole state)

1. **Bind:** `sync_workspaces` row, coverage rows `pending` for every FY from `books_from`, device active; re-read →
   same; re-bind same GUID → no new rows.
2. **Take-over:** old device `revoked_at`, `revoke_reason = taken_over`, `is_active = false`; new active; the
   partial unique index rejects a manual second active row.
3. **First sync:** run `running` → 24 months acked → both window FYs `complete`, run `completed`, cursors = counters
   at start, `sync_state = ready`, `last_synced_at` set; re-read everything.
4. **Batch replay:** same `batch_id` twice → one set of rows, identical response, `replayed: true`.
5. **Older alter_id:** voucher v at alter 105 then 104 → stored stays 105, lines unchanged. *(Changed 2026-09-29:
   every run kind except where §8.7 makes a `full_resync` authoritative — scenario 21.)*
6. **Voucher edit:** alter 106 with one line removed → exactly the new lines (count and amounts), bill allocations
   replaced.
7. **Soft-delete via reconcile:** voucher `is_deleted`, its lines/inventory/bills gone, `reread_ledgers` lists the
   touched ledgers; re-read confirms.
8. **Rename:** ledger GUID g renamed → one ledger row, new name, old lines still join by GUID, `ledger_name` on old
   lines unchanged (as exported).
9. **Mirrored balance ordering:** balance captured at t2 then a stale t1 → t2 stays; `balances_stale = 1`.
10. **Rejected batch:** unbalanced voucher in a batch of 10 → nothing stored, `sync_batches.status = rejected`;
    resend with it quarantined → 9 stored, `sync_quarantine` row, `quarantine_count = 1`; a later good copy resolves
    it.
11. **Backfill vs incremental `last_synced_at`:** backfill batch leaves it unchanged; incremental moves it.
12. **Coverage replay:** the same month acked twice → `months_complete` counts once; edges recomputed.
13. **Whole-company resync:** complete → resyncing; running → pending with `months_done = []`; completion → complete;
    edges: available counts resyncing, verified doesn't.
14. **Restore:** heartbeat counters below cursors → `restore_detected`; incremental batch 409; resync without a
    confirmed command 409; with one → allowed.
15. **Snapshots:** re-capture of `(trial_balance, 31-03-2023)` replaces; `imbalance` stored; cells + rows round-trip.
16. **Parity run:** run + lines persisted; `last_parity` matches the run; ladder transitions persisted across
    requests; retention slice prunes matching lines older than 7 days only.
17. **Cross-tenant:** device of workspace W1 posting to W2 → 403, W2 unchanged.
18. **Deleted workspace:** `is_deleted` flipped → next call 410, device revoked; `purge --now` removes every v2 row
    for it and nothing else.
19. **`Numeric` round-trip:** `-16538.66`, `132929.85`, `0.01` exact; forex face/rate exact.
20. **Raw retention:** voucher in FY−2 stored with `raw = NULL`; after `add_fy`, maintenance slices null the FY that
    left the window, bounded per heartbeat.
21. *(New 2026-09-29, §8.7.)* **Restore round-trip:** post-backup changes (a renamed ledger, altered vouchers in two
    FYs) → restore → `restore_detected` → confirmed company resync posts the older objects at lower alter_ids →
    `updated` (not `skipped_older`), old name and voucher lines back, `restore_reason` and the offer cleared, other
    open confirms `cancelled`. FY-scoped variant: only that FY's vouchers are replaced (`updated` 1, `skipped_older` 2
    for the master and the other-FY voucher). Parity afterwards: an anchor captured after the post-backup rename is
    `anchor_stale` once, then `ok` after re-capture (`db/test_confirmed_resync_ingest.py`,
    `db/test_parity_api.py::test_anchor_recaptured_before_a_restore_is_stale_after_it_then_computed`).

---

## 15. State matrix

### 15.1 Endpoint × auth state

| Endpoint group | valid active | expired access | revoked | unbound device | wrong ws | deleted ws | not active | web JWT instead |
|---|---|---|---|---|---|---|---|---|
| agent auth (refresh/logout) | ✓ | refresh ✓ | 401 | ✓ | — | 410 | ✓ | 401 |
| `/api/agent/workspaces` | ✓ | 401 | 401 | ✓ | — | lists only live | ✓ | 401 |
| `/api/sync/company` | ✓ | 401 | 401 | ✓ | — | 410 | take-over rules | 401 |
| `/api/sync/{ws}/*` | ✓ | 401 | 401 | 403 | 403 | 410 | 409 | 401 |
| web `sync-status`, commands, devices | — | 401 | — | — | 404 (not owner) | 404 | — | ✓ |

*(Changed 2026-09-29 — two cells the table left open, pinned as built:)* **logout × expired access** → 401
`token_expired` (logout authenticates with the device access token, so an expired one is refused like any device
call; the agent refreshes first — from the code, `any_device` → `decode_access`; `test_state_matrix_api.py` omits
this cell). **devices × deleted workspace** → not applicable: `GET /api/devices` names no workspace (the cell is
omitted in `test_state_matrix_api.py`); a device whose workspace is deleted is revoked on its next device call (§8.1
check 4).

### 15.2 Batch × content

| Content | Result |
|---|---|
| masters only / vouchers only / mixed | accepted; masters applied first |
| voucher referencing a ledger in the same batch | accepted (step 6 order) |
| voucher referencing an unknown ledger | 422 `missing_master` (retryable) |
| two live ledgers with that name (transient rename) | 422 `ambiguous_master` |
| unbalanced / bad amount / forex without base / bad date | 422 deterministic; quarantinable |
| forex voucher with stated bases | accepted, fx columns set |
| cancelled voucher with no lines and empty party | accepted |
| optional voucher | accepted, lines `countable = false` |
| post-dated voucher | accepted, `countable = true` |
| older alter_id | accepted, `skipped_older` |
| wrong company GUID | 409, nothing stored |
| replay same / reused id different body | stored response / 409 |
| oversize (bytes / objects / decompressed) | 413 |
| batch during `restore_detected` (incremental) | 409 |
| batch whose run is completed / another device's | 409 `run_closed` / 403 |

### 15.3 Run kind × `last_synced_at` × cursor

| Kind | `last_synced_at` | Cursor on completion |
|---|---|---|
| `first_sync` | per batch | = counters at start |
| `incremental` | per batch | = `cursor_after` |
| `backfill` | never | unchanged |
| `full_resync` (company) | window chunks + masters only | = counters at start |
| `full_resync` (single FY) | window chunks only | unchanged |

### 15.4 Coverage FY × event
(pending, running, resyncing, complete) × (month ack, last month ack, whole-company resync start, single-FY resync
start, add_fy, month ack replay) — every cell has an expected state in §8.3; the unit test enumerates all 24.

### 15.5 Parity × situation

| Situation | Status | Ledger verdicts | Ladder |
|---|---|---|---|
| all match (A, FY 2025-26 real) | `ok` | BS `match`, nominal rung-1 `not_applicable`, rung-2 nominal `match`, P&L A/c `not_applicable` | → ok |
| forex revaluation (B, real) | `ok` | USD party `match_revalued` (183.87), Export Sales `match` | → ok |
| forex sale missing | `suspect` | Export Sales `mismatch`, USD party `mismatch` (`forex_face_mismatch`) | ok → suspect |
| one voucher missing | `suspect` | party + nominal ledgers mismatch (equal/opposite) → `voucher_missed_or_duplicated` | ok → suspect |
| still mismatched after reported remediation ×2 | `alert` | — | heal 2, resync offered |
| after confirmed FY resync, still wrong | `hard_alert` | — | engineering flag |
| counters moved | `aborted_moving` | none stored | unchanged |
| cursor behind | `aborted_behind` | none | unchanged |
| anchor TB missing | `aborted_incomplete` | none | unchanged; `capture_snapshot` |
| TB imbalance changed, same AltMstId | `discarded_stale` | none | unchanged; restart notice |
| mid-backfill, all-time balances far off, anchor correct | `ok` | `match` (R30 regression) | ok |
| mid-backfill, ledger-level anchor route unavailable | `ok`/per rung 2 | rung-1 BS ledgers `not_applicable` (`no_ledger_anchor`), never `match` | per rung 2 |
| `resyncing` FY | verified edge excludes it | lines of that FY not compared | — |
| nature unclassifiable | per others | `not_applicable` `unclassified_group` | — |
| tolerance ₹0.99 / ₹1.00 / ₹1.01 | match / match / mismatch | — | — |

---

## 16. Test plan (all offline, tier A; DB tests need `TEST_DATABASE_URL`, never the dev DB)

**Contract unit tests (`v2/tests/contract/`)**
- Parsers on every value form in §5.2, each from a named capture; the two forex expressions (voucher line and ledger
  balance); expression without base → `stated=False`; junk raises; `""` → `None`, missing → absent (never zero).
- Transcoder: every §13.1 capture → wire objects; the `LEDGERENTRIES.LIST` / `ACCOUNTINGALLOCATIONS.LIST` copies are
  **not** emitted as `ledger_entries` (2× trap, LESSONS rule 18: summing them would give Sales/Purchase exactly 2×);
  an error envelope (`p10_A_no_company_report.xml`) raises.
- Round trip: capture → wire → JSON → wire model is lossless for every key.

**Cloud unit tests (`v2/tests/cloud/unit/`)** — pure functions, no DB:
- Nature walk (custom sub-groups, `&#4; Primary`, cycle, unmapped), base-type walk (`Sales - GST`, reserved self
  parent), `is_forex`, re-derivation on parent change.
- Rung 0 incl. forex bases and a 0.01 imbalance.
- Anchor selection D9 (E = books_from and E > books_from), both edges, coverage transitions (§15.4 all 24 cells).
- Rung 1 / rung 2 / §10.5 on in-memory rows: every §15.5 row; debit-is-negative assertion; tolerance boundaries;
  first-occurrence group rows; Opening Stock added only to the stock-bearing group; EXPLODEFLAG TB never used as
  ledger-level.
- Classifier: one case per signature. Ladder: every transition incl. "no `remediation_done` → state kept".
- Token helpers: expiry, `typ` mismatch, web JWT rejected, refresh rotation, reuse → revoke.
- Ops signal content: no names, GUIDs or exact amounts (asserted on the emitted dict).

**Real-data parity (unit, fixture-driven)**
- **Company A:** ingest `p16_A_vouchers_fy.xml` + masters from `p04_A_*`/`p16_A_*`/`p25_A_*`, mirrored balances
  `p16_A_ledgers.xml`, TB `p16_A_tb_fy_end.xml`, ledger-level TB `p17_A_tb_exploded_isledgerwise.xml`, anchor G1 (or
  FakeBooks) → `ok`; imbalance ≠ 0 accepted as baseline (D10); the Bills Receivable / Payable snapshots
  (`p12_A_bills_receivable_today.xml`, `p12_A_bills_payable_today.xml`) parse to the seed residuals ₹9,70,537 /
  ₹18,34,142 (Part 1 §8 anchors; comparing them with our bills is rung 3, deferred).
- **Company B, 31-03-2023:** ingest `p21_B_fy2022_month_01..12.xml`, masters from `p18_B_*`/`p25_B_*`/`p22_B_*`,
  TB `p18_B_tb_asof_2023-03-31.xml` → rung 2 `ok` with `forex_unrealised_total = 183.87` accepted against the
  `Unadjusted Forex Gain/Loss` row; the same run with the row removed from the cells → `forex_revaluation_unexplained`.
- **Mid-backfill (R30 regression, FakeBooks B):** window FY 2024-25/2025-26 complete, older FYs pending, all-time
  balances far from the window sum → `ok` via the anchor TB as-on 31-03-2024; then complete history and assert the
  books-start anchor (D9) switches on.

**DB integration (`v2/tests/cloud/db/`, `TEST_DATABASE_URL`)** — every §14 scenario; migration up/down on an empty
DB and on a DB that already has the current app's tables (asserts `alembic_version` untouched and only
`alembic_version_v2` written); rate limit 429; oversize 413; ingest performance budget.

**API integration (`httpx.AsyncClient` + ASGITransport, DB-backed)** — the full §15.1 matrix; bind/take-over race
(two concurrent binds → exactly one active); a whole first sync of FakeBooks company B through the real endpoints
(runs, batches, coverage, snapshots, parity) → `ready` + `ok`; **restart round-trip** (CLAUDE.md "Test reality" rule
7 adapted: there is no page to refresh, so after every state-changing call the test disposes the engine, opens a
fresh app instance and re-asserts the whole state from the DB via `GET /state` and `sync-status`).

**Regression / isolation**
- `test_isolation.py` extended: `v2.cloud` ↛ `v2.agent`/`v2.probes`; `v2.agent` ↛ `v2.cloud`; `v2.contract` imports
  neither; no write-mapped model for `users`/`workspaces`.
- `git diff --name-only` for S1 limited to `v2/` and `docs/` (checked in the S1 exit review).
- The existing v2 suite (917) stays green; the current app's suites are not affected (nothing outside `v2/` changes)
  and are not required to run.

**Not run in S1:** live Tally (tier B) except the optional read-only fixture capture; tier C; Part 2/3 UI tests;
Claude-API evals.

*(Changed 2026-09-29 — note only:)* the v2 suite prints one known warning, passlib's `'crypt' is deprecated and slated
for removal in Python 3.13` `DeprecationWarning`, raised by the password helper copied from the current app
(`v2/cloud/auth/passwords.py`, passlib). Accepted, not filtered; it must be resolved (replace passlib's `crypt`
import path or pin the Python version) before v2 moves to Python 3.13.

---

## 17. Risks and things that need the user

### 17.1 S1 risks

| ID | Risk | Handling |
|---|---|---|
| S1-R1 | A licensed Windows Tally exports amounts/rows differently from Educational/Wine | Verbatim wire + strict parsers → an unexpected form is a loud `unparseable_amount`, never a silent zero; tier-C check before release |
| S1-R2 | A forex ledger's ledger-level TB row form is unknown (G2) | §10.5 handles plain and expression; G2 capture decides the real path. *(Closed 2026-09-29: G2 = plain INR number, §13.3.)* |
| S1-R3 | The C47 set rule can hide two offsetting forex errors | Face check (a) runs on the mirrored balance every run; offsetting errors would need equal-and-opposite face errors too |
| S1-R4 | Quarantine hides data | Counted on `sync-status`, ops signal, and parity shows the hole — never silent |
| S1-R5 | UUID keys inflate line-table storage vs probe 21's estimate | Accepted (D22); the Q23 storage alert watches it |
| S1-R6 | In-process rate limits don't hold across workers | One worker in v1 (D24) |
| S1-R7 | `ambiguous_master` loop during renames | Retryable; masters processed by alter_id; the tracker/ops signal counts repeats |
| S1-R8 | Reconcile with a truncated list soft-deletes real rows | D28 guard + soft-delete is reversible (a re-sent voucher is re-stored with `is_deleted = false`) |
| S1-R9 | Lazy maintenance never runs if no heartbeat arrives | Nothing grows without heartbeats either; `purge` CLI covers deletion |
| S1-R10 *(new 2026-09-29)* | **Login lockout:** the per-email login limiter counts failed attempts from `/api/agent/auth/login` **and** from the relink password re-check (device + web, §7.15), so a third party who knows the email — or a stolen device token hammering relink — can exhaust the owner's bucket and block the agent's login for the window (`login_rate_window_s`, 15 min) | Accepted as Minor: it matches the current app's limiter, only failed attempts count, and it closes the relink password-oracle (review I5). Revisit with a per-IP / per-device component if abuse is seen |

### 17.2 Genuinely needs the user
1. **D5 — binding attaches to an existing workspace only.** Setup becomes "create a workspace on the web, then log
   in on the agent" until Part 3 decides Q11. If one-step agent setup is wanted in v2, D5 flips to "insert a
   `workspaces` row" (a write to a current table, which the isolation rule allows only as "read").
2. **D21 — `last_synced_at` vs `caught_up_at`:** an idle company (no entries for a week) shows "last synced 7 days
   ago" under Part 1's definition although we checked 10 minutes ago. Part 3 must pick the label.
3. **Q6 — production hosting:** storage-level encryption at rest is assumed; which provider/volume is an infra
   decision outside the code.
4. **Q5 — the 30-day purge grace** is a product/legal call under the DPDP Act.
5. **Build task 0** reads Tally live (read-only, companies A and B) to close gaps G1–G5; say if it should run or stay
   on FakeBooks. *(Done 2026-09-25; G6 added 2026-09-29 — §13.3.)*

### 17.3 Contract notes for S2 (the agent) *(new 2026-09-29 — S1 build rulings)*
The server enforces what it can; these it cannot, so the agent must:
1. **`as_on` = Tally's current-period end** on every non-bisect `/parity` call (daily, recheck, post_resync). The
   server's `as_on_not_current_period` guard (§10.1 step 0) is best-effort only — it can't catch an early `as_on`
   inside the current FY, which would face-check the mirrored balances against the wrong span.
2. **Keep the `confirm_resync` command id locally** until its `full_resync` completes — the ack only means
   "received" (§7.6). After a restart without it, re-read `GET /state`: open confirms are listed with
   `status: delivered`. Open the run with that `command_id` and the confirmed scope exactly (§7.8).
3. **Re-offer quarantined GUIDs after a server version change.** `unexpected_parse_error` (a server parser crash) is
   quarantinable so one object can't block the sync, but after a server fix the same bytes may parse — only a resend
   un-quarantines them (review M19).
4. **Send `counters` on every snapshot** — they date the snapshot for §10.1 step 7's staleness rule; re-capture the
   TB named by an `anchor_stale` remediation at the current counters.
5. **After a restore-driven resync, reconcile** vouchers first, then masters (§7.11): the resync's ingest never
   deletes.

---

## 18. S1 build order (the plan expands these)

0. **Fixture capture (optional, tier B, read-only):** G1–G5 via the probe harness; commit under
   `v2/tests/fixtures/sync/s1_*`. Else mark the FakeBooks substitutes.
1. **`v2/contract`:** parsers (+ forex grammar copy), wire models, transcoders for every §13.1 capture; contract tests.
2. **`v2/cloud` skeleton:** app, config, db, copied auth helpers, isolation-test extension.
3. **Migration `v2_001`:** all §4 tables and indexes; up/down tests with the current tables present.
4. **Device auth:** login/refresh/logout, tokens, rate limits, `/api/devices`, dependencies (§8.1 order).
5. **Binding + one active device + take-over + re-bind-when-empty + `/api/agent/workspaces`.**
6. **Heartbeat, `/state`, commands, restore detection, re-link, `sync-status`.**
7. **Runs + cursors (D15) + coverage + both edges + backfill copy.**
8. **Ingest pipeline** (§12 steps 1–15): resolver, derivations, upserts, balances, rung 0, quarantine, idempotency,
   limits.
9. **Reconcile + snapshots** (cells → rows, synthetic rows, imbalance).
10. **Parity engine:** preconditions, anchors, rung 1, rung 2, forex (§10.5), classifier, ladder, bisect,
    `/parity`, `last_parity`, ops signal.
11. **Maintenance slices + `purge` CLI** (Q21, Q22, Q23, Q5).
12. **Real-data parity tests (A, B) + FakeBooks end-to-end through the API + restart round-trips.**
13. **Code review → `docs/code-review-bi-s1-*.md`**, fix round, tracker + roadmap + Part 1 header update.
14. **S1 hardening backlog from S0** (exception (f)/(g)): the retro minors apply to the probe harness, not S1 code;
    carry them as a separate small task list in the plan (M2, M3, M7, M8 before any tier-C A re-run).
