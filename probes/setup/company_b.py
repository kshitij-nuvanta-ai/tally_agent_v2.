"""The idempotent company B loader (S0-B spec §4.3). Lists first, creates only what's missing, reads back
every write, and turns anything XML can't set into a pause step naming the object.

Round 1 fixes (coordinator review, see .superpowers/sdd/2026-09-23-bi-s0-company-b-loader/task-6-report.md
"Fix round 1" appendix for the full rationale of each):

- C1: every master create is guarded — a `WriteFailed` (e.g. Tally's "BAD UNIT NAME" rejecting the compound
  unit `Box of 10 Nos`, which has spaces) becomes a pause naming the object, a re-read, and only THEN a
  `CompanyBLoadError` if it's still missing (Ruling C11: a failed create must never crash the loader mid-load).
- C3: the balance half of verification IS implemented, at group level only (see `_verify_balances` below) —
  `TallyWriter.b_trial_balance` + `exploded_tb_rows`/`primary_group_rows` (reads.py). Ledger-level balances are
  still out of scope (S0-D7: a probe never guesses a request another probe must confirm — that's probes 16/17).
- I1: the flag-settle pause re-reads after `io.wait()` returns (spec §4.3) and reports a `problems` line if the
  flag is still wrong, instead of trusting the pause silently.
- I2: drift detection no longer flags a legitimately-interrupted first run. It compares a FY's pre-run count to
  the *chronologically latest already-complete* FY — only a gap at or before that point is drift (see
  `_latest_complete_fy`); a gap strictly after it is presumed to be a load still in progress.
- I3: the four cancelled/optional-tagged vouchers (S0-B spec §4.3) are never auto-recreated once at least one FY
  is already complete — probe 3 hasn't established whether a cancelled/optional voucher stays visible to a plain
  Voucher-collection read, so "missing" there is ambiguous between "genuinely gone" and "hidden by its own flag".
  A missing flagged voucher becomes a `problems` line + a pause asking the operator to confirm, never a create.
- I4: an existing master under the wrong parent (or wrong base unit, for items) is flagged in `problems`, not
  silently accepted as "already there" — company B's real shape has custom creditor sub-groups precisely so this
  kind of mismatch is possible (CLAUDE.md "Test reality, not an ideal").
- I5: the opening-bill pause is now conditional on a real read (`writer.b_bills_receivable`, `parse_bills`) —
  it only fires when the bill is actually absent from Bills Receivable as on 01-04-2022, not on every run.
- I7: every read in `_verify` (and in `_settle_flags`) is wrapped — a `WriteFailed`/`WriteTimeout` becomes one
  `problems` line instead of discarding the whole report (including its `pauses`).

`create_b_voucher` never writes `ISCANCELLED` (its own docstring: "cancelling is not reliably settable on
import"), so the two cancelled-tag vouchers always fail their flag re-read UNLESS the operator honours the
pause and sets the flag by hand in the Tally UI — that's real behaviour, not a fake artefact, and it's exactly
what `io.wait` is for. A caller (test or live run) that never answers the pause will always see the `problems`
line; one that does (see `test_the_run_ends_by_checking_counts_and_balances_against_the_expected_figures`'s
`on_wait` in the test file) genuinely clears it.

Round 2 fixes (F9 — company_b_data.py's opening balances were unsigned; F10 — deleted a `problems`-whitelist
in favour of simulating the operator honouring a flag pause, `test_p08_ledger_rename.py`-style) and round 3
(F11 — `create_party_ledger` sent `abs(opening)` on the wire, believing Tally infers the side from the parent
group, docs/tally-write-exploration-v4.md Op 5) are covered in task-6-report.md's "Fix round 2/3" appendices.
**Changed 2026-09-24 (Ruling C30):** F11 was wrong — Tally reads the wire sign (negative = Dr), as company A's
abs()'d bank openings landing as credits showed. `create_party_ledger` now sends the signed opening.

Round 4 fixes (coordinator review round 4/5) — the sign convention itself was inverted:

- F12: every `LineSpec.amount` in `company_b_data.py` was the mirror image of Op 6 (sales) / Op 7 (purchase)'s
  live-verified convention — `deemed_positive` was already right, only the amounts were flipped. Fixed there
  (`test_sales_and_purchase_lines_pin_the_op6_op7_sign_convention` pins the actual convention now, since a
  sum-to-zero invariant can't tell a convention from its mirror image).
- F13: round 2's signed openings (debit negative) were and remain CORRECT under this convention — see
  `test_opening_balances_net_to_zero`'s comment. (F11's `abs()` on the wire was later overturned — C30: the
  signed opening goes on the wire as is.)
- F14/F15: see `_verify_balances`'s own docstring for exactly what the balance check does and does not prove.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from tally_bridge.sync_reports import parse_bills
from tally_bridge.xml_utils import read_objects
from probes.companies import COMPANIES
from probes.console import ProbeIO
from probes.reads import (exploded_tb_rows, forex_base, parse_forex_amount, parse_vouchers, primary_group_rows,
                             primary_lines, voucher_request)
from probes.setup.company_b_data import (
    FOREX_BASE_SYMBOL,
    FOREX_FORM,
    FORMERLY_SKIPPED_TAGS,
    SALES_GST_VOUCHER_TYPE,
    Dataset,
    VoucherSpec,
    expected_figures,
    fy_label,
    generate,
    quantity_unit,
)
from probes.setup.writes import (
    B_READBACK_FROM,
    B_READBACK_TO,
    ForexLine,
    TallyWriter,
    b_day_voucher_request,
    WriteFailed,
    WriteTimeout,
    check_writable,
    currency_matches,
    validate_b_voucher,
)

F2_INSTRUCTION = ("In TallyPrime press F2 and set the working date to 31-03-2026 or later, then press Enter here. "
                  "Vouchers dated after F2 are silently dropped (LESSONS §15 rule 14).")
BILLS_RECEIVABLE_AS_ON = "01-04-2022"
BALANCE_TOLERANCE = Decimal("1.00")

_TAG_RE = re.compile(r"^\[S0-B:(\d+)\]")
_VOUCHER_LIST_FIELDS = ["MasterId", "Narration", "Date"]
_VOUCHER_FLAG_FIELDS = ["MasterId", "Narration", "IsCancelled", "IsOptional"]
_MASTER_KINDS = ("currencies", "groups", "units", "items", "ledgers", "vouchers")


class CompanyBLoadError(Exception):
    """The loader can't continue — a prerequisite the operator must fix first."""


@dataclass
class LoadReport:
    created: dict[str, int] = field(default_factory=lambda: {k: 0 for k in _MASTER_KINDS})
    skipped: dict[str, int] = field(default_factory=lambda: {k: 0 for k in _MASTER_KINDS})
    pauses: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)          # F14: observations, not action items or failures


def load_company_b(writer: TallyWriter, io: ProbeIO, *, licence: str = "licensed",
                    company: str = COMPANIES["B"]) -> LoadReport:
    check_writable(company)                       # before any request at all
    dataset = generate(licence=licence)
    report = LoadReport()
    _require_voucher_type(writer, io, company, report)
    _require_currencies(writer, company, dataset, report)
    _load_masters(writer, io, company, dataset, report)
    _load_openings(writer, io, company, dataset, report)
    report.pauses.append(F2_INSTRUCTION)           # M1: mirror the other pauses
    io.wait(F2_INSTRUCTION)
    _load_vouchers(writer, io, company, dataset, report)
    _settle_flags(writer, io, company, dataset, report)
    _verify(writer, company, dataset, report)
    return report


# --- stage 1: preflight -----------------------------------------------------------------------------------------
def _require_voucher_type(writer: TallyWriter, io: ProbeIO, company: str, report: LoadReport) -> None:
    """`Sales - GST` is a custom voucher type; LESSONS §15 rule 4 — a voucher-type CREATE via XML looks successful
    but doesn't change Tally's behaviour, so it can only be created by the operator, in the UI."""
    if SALES_GST_VOUCHER_TYPE in writer.list_voucher_types(company):
        return
    msg = (f"TallyPrime has no voucher type named {SALES_GST_VOUCHER_TYPE!r}. Create it in the UI "
           "(Accounts Info > Voucher Types > Create, parented to Sales) — voucher-type writes via XML look "
           "successful but do not change behaviour (LESSONS §15 rule 4). Then press Enter here.")
    report.pauses.append(msg)
    io.wait(msg)
    if SALES_GST_VOUCHER_TYPE not in writer.list_voucher_types(company):
        raise CompanyBLoadError(
            f"{SALES_GST_VOUCHER_TYPE!r} still missing after the operator pause — cannot continue.")


def _require_currencies(writer: TallyWriter, company: str, dataset: Dataset, report: LoadReport) -> None:
    """Plan part 7: the Currency masters the forex sales need must already exist — made in the UI. The loader never
    sends a Currency create: live on 2026-09-25 the XML create of `$` was refused (EXCEPTIONS=1, no LINEERROR) AND
    left `$` behind as an import-exception master that then made the UI refuse the create too
    (tests/fixtures/sync/forex_shape_2026-09-25_run1_currency_refused/). A missing currency stops the load here,
    before any write. "Present" is `currency_matches` on any listed row: the symbol or the formal name."""
    rows = writer.currency_rows(company)
    for c in dataset.currencies:
        if any(currency_matches(row, c) for row in rows):
            report.skipped["currencies"] += 1
            continue
        raise CompanyBLoadError(
            f"Currency {c.symbol!r} ({c.formal_name}) is not in {company!r}, and setup-b never creates it over XML "
            "(live 2026-09-25: the create is refused and leaves an import-exception master that blocks the UI create "
            f"— restore the backup first if that happened). In TallyPrime create `{c.symbol}` (Formal name "
            f"{c.formal_name}, ISO {c.formal_name}) in the UI: Create → Currency. Then re-run setup-b. Nothing was "
            "written.")


# --- stage 2: groups -> units -> items -> ledgers ----------------------------------------------------------------
def _create_or_pause(writer: TallyWriter, io: ProbeIO, report: LoadReport, kind: str, name: str,
                     create_fn, exists_fn) -> bool:
    """Never let a failed master create crash the load (Ruling C11). A `WriteFailed` (e.g. Tally's "BAD UNIT
    NAME" rejecting a compound unit with spaces) becomes a pause naming the object; the operator creates it by
    hand in the UI, we re-read, and only raise if it's genuinely still missing. Returns True if this call's own
    XML create succeeded (i.e. it should count as "created"), False if the operator's UI action is what did it
    (counts as "skipped" — this run's XML made no lasting change)."""
    try:
        create_fn()
        return True
    except WriteFailed as exc:
        msg = f"Create {kind} {name!r} in the Tally UI — the XML create failed: {exc}"
        report.pauses.append(msg)
        io.wait(msg)
        if not exists_fn():
            raise CompanyBLoadError(f"{kind} {name!r} still missing after the operator pause — cannot continue.")
        return False


def _load_masters(writer: TallyWriter, io: ProbeIO, company: str, dataset: Dataset, report: LoadReport) -> None:
    existing_groups = writer.list_groups(company)
    for g in dataset.groups:
        if g.name in existing_groups:
            if existing_groups[g.name] != g.parent:                                                       # I4
                report.problems.append(
                    f"group {g.name!r}: parent is {existing_groups[g.name]!r} in Tally, expected {g.parent!r}")
            report.skipped["groups"] += 1
            continue
        if _create_or_pause(writer, io, report, "group", g.name,
                            lambda g=g: writer.create_group(company, g.name, g.parent),
                            lambda g=g: g.name in writer.list_groups(company)):
            report.created["groups"] += 1
        else:
            report.skipped["groups"] += 1

    existing_units = set(writer.list_units(company))
    for u in dataset.units:
        if u.name in existing_units:
            report.skipped["units"] += 1
            continue
        if _create_or_pause(writer, io, report, "unit", u.name,
                            lambda u=u: writer.create_unit(company, u.name, first_unit=u.first_unit,
                                                               second_unit=u.second_unit, conversion=u.conversion),
                            lambda u=u: u.name in set(writer.list_units(company))):
            report.created["units"] += 1
        else:
            report.skipped["units"] += 1

    existing_items = writer.list_stock_items(company)
    for i in dataset.items:
        if i.name in existing_items:
            if existing_items[i.name] != i.unit:                                                           # I4
                report.problems.append(
                    f"item {i.name!r}: base unit is {existing_items[i.name]!r} in Tally, expected {i.unit!r}")
            report.skipped["items"] += 1
            continue
        if _create_or_pause(writer, io, report, "stock item", i.name,
                            lambda i=i: writer.create_stock_item(company, i.name, unit=i.unit, hsn=i.hsn,
                                                                 opening_qty=i.opening_qty,
                                                                 opening_rate=i.opening_rate,
                                                                 qty_unit=quantity_unit(dataset.units, i.unit)),
                            lambda i=i: i.name in writer.list_stock_items(company)):
            report.created["items"] += 1
        else:
            report.skipped["items"] += 1

    existing_ledgers = writer.list_ledgers(company)
    for l in dataset.ledgers:
        if l.name in existing_ledgers:
            if existing_ledgers[l.name] != l.parent:                                                       # I4
                report.problems.append(
                    f"ledger {l.name!r}: parent is {existing_ledgers[l.name]!r} in Tally, expected {l.parent!r}")
            if l.currency:                                                                         # plan part 7
                details = writer.ledger_details(company, l.name)
                got = details.get("CurrencyName", "") if details else None
                if got != l.currency:
                    report.problems.append(
                        f"ledger {l.name!r}: currency is {got!r} in Tally, expected {l.currency!r} — never altered by "
                        "the loader (a currency change re-casts the ledger's vouchers); fix it in the UI or restore")
            report.skipped["ledgers"] += 1
            continue
        if _create_or_pause(writer, io, report, "ledger", l.name,
                            lambda l=l: writer.create_party_ledger(company, l.name, parent=l.parent,
                                                                   bill_wise=l.bill_wise, opening=l.opening,
                                                                   gstin=l.gstin, currency=l.currency),
                            lambda l=l: l.name in writer.list_ledgers(company)):
            report.created["ledgers"] += 1
        else:
            report.skipped["ledgers"] += 1


# --- stage 3: opening bill --------------------------------------------------------------------------------------
def _load_openings(writer: TallyWriter, io: ProbeIO, company: str, dataset: Dataset, report: LoadReport) -> None:
    """The opening balance itself is written by `_load_masters` (create_party_ledger's `opening` argument). The
    bill-wise BILL reference tying that opening balance to a named bill (e.g. "Op/2022-001") has no verified
    Op in docs/tally-write-exploration-v4.md, so it is never attempted by XML. I5: this now reads Bills
    Receivable as on 01-04-2022 (`writer.b_bills_receivable`, live-verified shape per anchors.py) and pauses
    only when the bill genuinely isn't there — not unconditionally on every run."""
    for l in dataset.ledgers:
        if not l.opening_bill:
            continue
        try:
            raw = writer.b_bills_receivable(company, BILLS_RECEIVABLE_AS_ON)
        except (WriteFailed, WriteTimeout) as exc:
            report.problems.append(f"Bills Receivable read failed while checking {l.opening_bill!r}: {exc}")
            continue
        bill_numbers = {b.get("bill_number") for b in parse_bills(raw)}
        if l.opening_bill in bill_numbers:
            continue
        msg = (f"Opening bill {l.opening_bill!r} for {l.name!r} (₹{l.opening}) is not in Bills Receivable as on "
               f"{BILLS_RECEIVABLE_AS_ON} — the bill-wise opening-balance shape is not a verified Op "
               "(docs/tally-write-exploration-v4.md) and was not written by XML. Create it in the UI, then "
               "press Enter here.")
        report.pauses.append(msg)
        io.wait(msg)


# --- stage 4: vouchers -------------------------------------------------------------------------------------------
def _parse_tag(narration: str) -> int | None:
    match = _TAG_RE.match(narration or "")
    return int(match.group(1)) if match else None


@dataclass
class VoucherRows:
    """I4: keying by tag is last-wins, so a dict alone cannot see a DUPLICATE — 960 vouchers plus 5 duplicates
    read as exactly 960. Ruling C17's whole rationale is that a duplicate is worse than a gap, so the raw rows
    (one entry per copy) and the tags seen more than once are carried alongside the by-tag view."""
    by_tag: dict[int, dict[str, str]]
    rows: list[dict[str, str]]                   # raw, pre-dedup: a duplicated tag appears once per copy
    duplicate_tags: dict[int, int]               # tag -> number of copies, for tags with more than one

    def __contains__(self, tag: int) -> bool:
        return tag in self.by_tag


def _read_vouchers(writer: TallyWriter, company: str, fields: list[str]) -> VoucherRows:
    """Every company-B voucher over its full date window (S0-B spec §4), keyed by its `[S0-B:n]` tag — and the
    raw rows behind that key, so duplicates stay visible (I4)."""
    xml = voucher_request("S0BVouchers", fields, company, from_date=B_READBACK_FROM, to_date=B_READBACK_TO)
    raw = read_objects(writer.post(xml), "VOUCHER", fields)
    by_tag: dict[int, dict[str, str]] = {}
    tagged: list[dict[str, str]] = []
    seen: dict[int, int] = {}
    for row in raw:
        tag = _parse_tag(row.get("Narration", ""))
        if tag is None:
            continue
        by_tag[tag] = row
        tagged.append(row)
        seen[tag] = seen.get(tag, 0) + 1
    return VoucherRows(by_tag, tagged, {tag: n for tag, n in seen.items() if n > 1})


def _pre_run_fy_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        d = _parse_tally_date(row.get("Date", ""))
        if d is not None:
            label = fy_label(d)
            counts[label] = counts.get(label, 0) + 1
    return counts


def _latest_complete_fy(pre_by_fy: dict[str, int], expected_by_fy: dict[str, int]) -> str | None:
    """The chronologically latest FY that was already fully loaded before this run. FY labels ("2022-23", …,
    "2025-26") sort lexicographically the same as chronologically across this dataset's span, so a plain string
    max is enough."""
    complete = [fy for fy, count in expected_by_fy.items() if pre_by_fy.get(fy, 0) == count]
    return max(complete) if complete else None


def _flag_predated_drift(pre_by_fy: dict[str, int], expected_by_fy: dict[str, int], latest_complete: str | None,
                         report: LoadReport, *, present: set[int] | None = None,
                         dataset: Dataset | None = None) -> None:
    """I2: a partial (or zero) FY strictly AFTER the latest already-complete FY is presumed to be a load still
    in progress (an interrupted first run over ~1000 requests into Wine-hosted Tally is the likely path) — not
    flagged. A gap AT OR BEFORE that point is drift: a date-ordered loader would never complete a later FY while
    leaving an earlier one short, so something that was there got removed. `pre_count == 0` for an earlier FY
    is drift too (not just partial counts) — the same reasoning applies to a whole FY vanishing.

    Plan part 7 (Ruling P7-9): a gap made ONLY of `FORMERLY_SKIPPED_TAGS` (101/102, absent from live B under C36) is a
    note, not drift. Any other missing tag in that FY keeps the problem line exactly as before."""
    if latest_complete is None:
        return
    written = {v.tag: v for v in dataset.vouchers if not v.skip_reason} if dataset is not None else {}
    for fy, expected_count in sorted(expected_by_fy.items()):
        if fy > latest_complete:
            continue
        pre_count = pre_by_fy.get(fy, 0)
        if pre_count != expected_count and present is not None and written:
            missing = sorted(t for t, v in written.items() if fy_label(v.date) == fy and t not in present)
            if missing and set(missing) <= FORMERLY_SKIPPED_TAGS and pre_count + len(missing) == expected_count:
                report.notes.append(f"[S0-B:{', '.join(map(str, missing))}] written now — skipped under C36 until "
                                    "plan part 7 proved a forex write shape; not drift.")
                continue
        if pre_count != expected_count:
            report.problems.append(
                f"FY {fy}: voucher count was {pre_count}/{expected_count} before this run (Tally was missing "
                f"{expected_count - pre_count}) — recreated the gap now; investigate why they went missing.")


def _skip_notes(dataset: Dataset) -> list[str]:
    """C36: one note per skip reason, naming every tag it covers — skipped vouchers are never silently dropped."""
    by_reason: dict[str, list[int]] = {}
    for v in dataset.vouchers:
        if v.skip_reason:
            by_reason.setdefault(v.skip_reason, []).append(v.tag)
    return [f"[S0-B:{', '.join(str(t) for t in sorted(tags))}] not written: {reason}"
            for reason, tags in by_reason.items()]


def validate_dataset(dataset: Dataset) -> list[str]:
    """C37 (review #7): every check `create_b_voucher` would make before sending, run over every voucher the loader
    will write — so a bad voucher stops the load BEFORE the first send, never mid-run. Returns one line per bad
    tag (empty = all good). Includes M2's ordering contract, which `create_b_voucher` itself cannot check."""
    bad: list[str] = []
    unit_by_item = {i.name: i.unit for i in dataset.items}
    for v in sorted(dataset.vouchers, key=lambda v: (v.date, v.tag)):
        if v.skip_reason:
            continue
        lines = [(line.ledger, line.amount, line.deemed_positive) for line in v.lines]
        if v.inventory and (lines[0][0] != v.party or len(lines) < 2 or lines[1][0] == v.party):
            # M2: create_b_voucher's ordering contract (party first, then the nominal ledger) is enforced only by
            # "len(lines) >= 2" at that layer — a violation silently misallocates stock to whatever ledger sits
            # second (e.g. a GST line) with no error.
            bad.append(f"[S0-B:{v.tag}] {v.narration}: an inventory voucher must list the party {v.party!r} first "
                       f"and the nominal ledger second; got {[ledger for ledger, _, _ in lines[:2]]}. Loading it "
                       "would misallocate the stock to whatever ledger sits second.")
            continue
        try:
            validate_b_voucher(
                vch_type=v.vch_type, narration=v.narration, party=v.party, lines=lines,
                inventory=[(inv.item, unit_by_item.get(inv.item, ""), inv.qty, inv.rate, inv.amount)
                           for inv in v.inventory],
                bills=[(b.name, b.bill_type, b.amount, b.credit_period) for b in v.bills], forex=_forex_of(v))
        except ValueError as exc:
            bad.append(f"[S0-B:{v.tag}] {exc}")
    return bad


def _forex_of(v: VoucherSpec) -> ForexLine | None:
    """Plan part 7: how a foreign-currency voucher goes on the wire — the shape live company B stored (V1b: the full
    form, the rate without a base symbol; company_b_data.FOREX_FORM / FOREX_BASE_SYMBOL)."""
    if v.currency == "INR":
        return None
    return ForexLine(symbol=v.currency_symbol, fx_amount=v.fx_amount, rate=v.fx_rate, form=FOREX_FORM,
                     base_symbol=FOREX_BASE_SYMBOL)


def _load_vouchers(writer: TallyWriter, io: ProbeIO, company: str, dataset: Dataset, report: LoadReport) -> None:
    # C36 / review #2, plan part 7: a foreign-currency voucher goes out as forex (`_forex_of`) — without its fx fields
    # it would go out as a plain INR one, the C36 failure. Unless the dataset skips it, that is a stop — before
    # anything is read or written.
    for v in dataset.vouchers:
        if v.currency != "INR" and not v.skip_reason and (v.currency_symbol is None or v.fx_amount is None
                                                          or v.fx_rate is None):
            raise CompanyBLoadError(f"[S0-B:{v.tag}] {v.narration}: currency {v.currency} without its fx fields "
                                    "(currency_symbol, fx_amount, fx_rate) — it would go out as a plain INR voucher.")
    bad = validate_dataset(dataset)                                                                      # C37
    if bad:
        raise CompanyBLoadError(f"{len(bad)} voucher(s) fail pre-send validation — no voucher was sent:\n  "
                                + "\n  ".join(bad))
    report.notes.extend(_skip_notes(dataset))

    # Not guarded like `_verify`'s reads (I7): if Tally can't even be read at the START of the voucher stage,
    # blind-creating without knowing what already exists risks duplicates — a hard stop here is the safer choice.
    existing = _read_vouchers(writer, company, _VOUCHER_LIST_FIELDS)
    expected = expected_figures(dataset)
    pre_by_fy = _pre_run_fy_counts(existing.rows)
    latest_complete = _latest_complete_fy(pre_by_fy, expected.voucher_count_by_fy)
    _flag_predated_drift(pre_by_fy, expected.voucher_count_by_fy, latest_complete, report,
                         present=set(existing.by_tag), dataset=dataset)

    # C40: the unit each inventory row's ACTUALQTY/BILLEDQTY/RATE is written in — a compound item's FIRST unit
    # ("5 Box", "950.00/Box"). Live-verified for stock-item openings (2026-09-24); for voucher rows it is inferred
    # from that and not yet live-verified (tag 2 is the first A4 Paper Ream sale).
    unit_by_item = {i.name: quantity_unit(dataset.units, i.unit) for i in dataset.items}
    for v in sorted(dataset.vouchers, key=lambda v: (v.date, v.tag)):
        if v.skip_reason:                                                                                    # C36
            continue
        if v.tag in existing:
            report.skipped["vouchers"] += 1
            continue
        if (v.cancelled or v.optional) and latest_complete is not None and fy_label(v.date) <= latest_complete:
            # I3: probe 3 hasn't established whether a cancelled/optional voucher stays visible to a plain
            # Voucher-collection read once the operator sets the flag in the UI — "missing" here is ambiguous
            # between "genuinely gone" and "hidden by its own flag". Never guess; never auto-recreate (a
            # duplicate voucher is worse than a gap) — report it and ask the operator to confirm.
            msg = (f"[S0-B:{v.tag}] {v.narration}: missing from Tally, but it carries a cancelled/optional flag "
                   "that may make it invisible to a plain Voucher-collection read (probe 3) rather than actually "
                   "gone. NOT auto-recreating it — confirm in the Tally UI whether it exists, then press Enter "
                   "here.")
            report.problems.append(msg)
            report.pauses.append(msg)
            io.wait(msg)
            continue

        lines = [(line.ledger, line.amount, line.deemed_positive) for line in v.lines]
        inventory = [(inv.item, unit_by_item[inv.item], inv.qty, inv.rate, inv.amount) for inv in v.inventory]
        bills = [(b.name, b.bill_type, b.amount, b.credit_period) for b in v.bills]
        try:
            writer.create_b_voucher(company, vch_type=v.vch_type, date=v.date.strftime("%Y%m%d"),
                                    narration=v.narration, party=v.party, lines=lines,
                                    inventory=inventory, bills=bills, optional=v.optional, forex=_forex_of(v))
        except WriteFailed as exc:                                                                          # C1
            msg = f"[S0-B:{v.tag}] {v.narration}: voucher create failed — create it manually in the Tally UI: {exc}"
            report.pauses.append(msg)
            io.wait(msg)
            if v.tag not in _read_vouchers(writer, company, _VOUCHER_LIST_FIELDS).by_tag:
                raise CompanyBLoadError(f"[S0-B:{v.tag}] still missing after the operator pause — cannot continue.")
            report.skipped["vouchers"] += 1
            continue
        report.created["vouchers"] += 1


# --- stage 5: flags that don't stick ------------------------------------------------------------------------------
def _flag_is_set(by_tag: dict[int, dict[str, str]], tag: int, read_field: str) -> bool:
    row = by_tag.get(tag)
    return row is not None and row.get(read_field) == "Yes"


def _settle_flags(writer: TallyWriter, io: ProbeIO, company: str, dataset: Dataset, report: LoadReport) -> None:
    flagged = [(v.tag, v.narration, "ISCANCELLED", "IsCancelled") for v in dataset.vouchers if v.cancelled]
    flagged += [(v.tag, v.narration, "ISOPTIONAL", "IsOptional") for v in dataset.vouchers if v.optional]
    if not flagged:
        return
    try:
        by_tag = _read_vouchers(writer, company, _VOUCHER_FLAG_FIELDS).by_tag
    except (WriteFailed, WriteTimeout) as exc:                                                              # I7
        report.problems.append(f"Flag read-back failed: {exc}")
        return

    still_wrong = [item for item in flagged if not _flag_is_set(by_tag, item[0], item[3])]
    for tag, narration, xml_tag, _read_field in still_wrong:
        msg = (f"[S0-B:{tag}] {narration}: {xml_tag} did not stick on read-back. Set it manually on this "
               "voucher in the Tally UI, then press Enter here.")
        report.pauses.append(msg)
        io.wait(msg)
    if not still_wrong:
        return

    # I1 / spec §4.3: "asks you to set it in the UI, then reads it back" — re-read after the pauses rather than
    # trust that the operator's fix took, and surface anything still wrong as a problem, not silently.
    try:
        by_tag = _read_vouchers(writer, company, _VOUCHER_FLAG_FIELDS).by_tag
    except (WriteFailed, WriteTimeout) as exc:                                                              # I7
        report.problems.append(f"Flag re-read after the pause failed: {exc}")
        return
    for tag, narration, xml_tag, read_field in still_wrong:
        if not _flag_is_set(by_tag, tag, read_field):
            report.problems.append(f"[S0-B:{tag}] {narration}: {xml_tag} is still not set after the operator pause.")


# --- stage 6: verification -----------------------------------------------------------------------------------------
def _verify(writer: TallyWriter, company: str, dataset: Dataset, report: LoadReport) -> None:
    """Per-FY voucher counts, and (C3) group-level balances, compared against the dataset. Never raises (I7:
    every read is wrapped) — the operator must see the whole picture in one run."""
    # M7: `expected.ledger_month_end` / `ledger_fy_opening` exist for probes 16/18 — the loader never compares a
    # single LEDGER against them (Ruling C16 / S0-D7). C42: `_verify_balances` does sum the final month-end into
    # GROUP buckets, so expected balances have exactly one source.
    expected = expected_figures(dataset)
    try:
        rows = _read_vouchers(writer, company, _VOUCHER_LIST_FIELDS)
    except (WriteFailed, WriteTimeout) as exc:                                                              # I7
        report.problems.append(f"Verification voucher read failed: {exc}")
        rows = None
    if rows is not None:
        for tag, copies in sorted(rows.duplicate_tags.items()):                                             # I4
            report.problems.append(
                f"[S0-B:{tag}] appears {copies} times in Tally — a duplicate voucher (Ruling C17: worse than a "
                "gap). Delete the extra copies in the Tally UI; the loader never removes a voucher.")
        actual_by_fy: dict[str, int] = {}
        for row in rows.rows:
            d = _parse_tally_date(row.get("Date", ""))
            if d is None:
                continue
            label = fy_label(d)
            actual_by_fy[label] = actual_by_fy.get(label, 0) + 1
        for fy, expected_count in sorted(expected.voucher_count_by_fy.items()):
            actual_count = actual_by_fy.get(fy, 0)
            if actual_count != expected_count:
                report.problems.append(
                    f"FY {fy}: voucher count mismatch — expected {expected_count}, Tally has {actual_count}")

    _verify_balances(writer, company, dataset, report)
    _verify_forex(writer, company, dataset, report)


FOREX_RESTORE_HINT = ("restore `s0probe-backups/100000-pre-forex-with-usd-2026-09-25` — setup-b is keyed by tag and "
                      "never re-writes or alters an existing voucher")


def forex_line_problems(v: VoucherSpec, lines: list[dict]) -> list[str]:
    """Plan part 7 review I1: what is wrong with one forex voucher as Tally stored it, line by line against the
    dataset — the ledgers, and on every line the currency symbol, the face value, the rate and the INR base (stated,
    or face × rate). Empty = stored exactly as sent. `lines` are `reads.primary_lines` (each posting once, D14)."""
    want = {line.ledger: line.amount for line in v.lines}
    got = [l["fields"].get("LEDGERNAME", "") for l in lines]
    issues: list[str] = []
    if sorted(got) != sorted(want):
        issues.append(f"ledgers {sorted(got)} ≠ the dataset's {sorted(want)}")
    for line in lines:
        ledger, text = line["fields"].get("LEDGERNAME", ""), line["amount_raw"]
        if ledger not in want:
            continue
        fa = parse_forex_amount(text)
        if fa is None:
            issues.append(f"{ledger!r} stored as plain INR ({text!r}), no forex — the C36 failure")
            continue
        if fa.currency != v.currency_symbol:
            issues.append(f"{ledger!r}: currency {fa.currency!r} ≠ {v.currency_symbol!r} ({text!r})")
        if abs(fa.fx) != v.fx_amount:
            issues.append(f"{ledger!r}: face {abs(fa.fx)} ≠ {v.fx_amount} ({text!r})")
        if fa.rate != v.fx_rate:
            issues.append(f"{ledger!r}: rate {fa.rate} ≠ {v.fx_rate} ({text!r})")
        base = forex_base(fa)[0]
        if base != want[ledger]:
            issues.append(f"{ledger!r}: base {base} ≠ the dataset's {want[ledger]} ({text!r})")
    return issues


def _verify_forex(writer: TallyWriter, company: str, dataset: Dataset, report: LoadReport) -> None:
    """Plan part 7 review I1 (Global Constraints: read back every write — `created=1` is not proof). A plain-INR store
    (C36) or a forex store with another base posts the same INR amounts, so the counts and the group balances above
    can't see it. Every forex voucher of the dataset — created now or already there, so a re-run after a bad load
    still fails — is read back in its OWN day (C43-safe: 01-09 / 02-09-2022 educational) and checked with
    `forex_line_problems`. Anything wrong is a problem: setup-b exits 1 and does not stamp `company_b_loaded_at`."""
    good: list[int] = []
    for v in sorted((v for v in dataset.vouchers if v.currency != "INR" and not v.skip_reason),
                    key=lambda v: (v.date, v.tag)):
        day = v.date.strftime("%d-%m-%Y")
        try:
            found = [x for x in parse_vouchers(writer.post(b_day_voucher_request(company, day)))
                     if _parse_tag(x["header"].get("NARRATION", "")) == v.tag]
        except (WriteFailed, WriteTimeout) as exc:                                                          # I7
            report.problems.append(f"[S0-B:{v.tag}] forex read-back on {day} failed: {exc}")
            continue
        if len(found) != 1:
            report.problems.append(f"[S0-B:{v.tag}] forex read-back: {len(found)} voucher(s) on {day}, expected 1.")
            continue
        issues = forex_line_problems(v, primary_lines(found[0]))
        if issues:
            report.problems.append(f"[S0-B:{v.tag}] {v.narration}: not stored as the forex voucher that was sent — "
                                   + "; ".join(issues) + f". {FOREX_RESTORE_HINT}.")
        else:
            good.append(v.tag)
    if good:
        report.notes.append(f"[S0-B:{', '.join(map(str, good))}] read back as forex: currency, face, rate and INR base "
                            "as sent (plan part 7 review I1).")


def _primary_bucket(ledger_parent: str, group_parents: dict[str, str]) -> str:
    """One hop through a dataset-defined custom group (e.g. National Creditors -> Sundry Creditors); anything
    not itself a dataset group is already at the comparison granularity — a Tally-reserved second-level or
    primary group. C3: the TRUE primary-group root (e.g. Sundry Creditors -> Current Liabilities) is
    deliberately NOT resolved here — that's probe 16/17's job (S0-D7)."""
    return group_parents.get(ledger_parent, ledger_parent)


def _expected_group_balances(dataset: Dataset) -> dict[str, Decimal]:
    """Each group bucket's closing balance as on the last day of the dataset, summed from `expected_figures`'
    ledger month-ends — the one source of expected balances (C42).

    C42 (live run 4, 2026-09-24, logs/setup-b-live-2026-09-24-run4.log): this used to be a second, independent
    replay (`_posted_group_balances`) that added EVERY non-skipped voucher's lines, cancelled and optional ones
    included, on the belief that `create_b_voucher` never writes ISCANCELLED so the cancelled ones "post normally".
    But the flag pause makes the operator set ISCANCELLED by hand, and Tally leaves optional vouchers out of
    balances too — live, Sundry Debtors fell short by exactly the party amounts of 201/202/301/302. The fake's Trial
    Balance counted flagged vouchers as well, so the two agreed with each other and the offline suite stayed green."""
    expected = expected_figures(dataset)
    last = max(day for _, day in expected.ledger_month_end)
    group_parents = {g.name: g.parent for g in dataset.groups}
    buckets: dict[str, Decimal] = {}
    for l in dataset.ledgers:
        bucket = _primary_bucket(l.parent, group_parents)
        buckets[bucket] = buckets.get(bucket, Decimal("0.00")) + expected.ledger_month_end[(l.name, last)]
    return buckets


def _verify_balances(writer: TallyWriter, company: str, dataset: Dataset, report: LoadReport) -> None:
    """C3/F14: group-level balance verification, compared by ABSOLUTE MAGNITUDE, not signed value.

    What this DOES prove: that each group's total (Sundry Debtors, Bank Accounts, Sales Accounts, …) is the
    right SIZE — catching a wrong or entirely missing bucket, a mispostred voucher, a duplicate, etc.

    What this does NOT prove: which SIDE (debit/credit) a group nets to, or what the whole statement's Dr=Cr
    total should be. The re-review found company A's own recorded baseline (`probes/results/results.json`)
    has Current Assets positive and Purchase/Indirect Expenses negative — the opposite polarity from what a
    naive "assets negative, expenses negative" reading would predict — and probe 18's own finding is that the
    stock-bearing group needs a synthetic "Opening Stock" row to reconcile at all. The live netting rule is
    genuinely unsettled; establishing it is probes 16/17/18's job, not this loader's to guess (S0-D7). Magnitude
    comparison is sign-convention-independent, so it stays useful regardless of how that unsettled question
    resolves. The statement's observed Dr/Cr total is recorded as `report.notes` — informational, not a
    `problems` line — precisely because asserting it must equal any particular figure would be that guess.
    """
    expected_buckets = _expected_group_balances(dataset)
    try:
        raw = writer.b_trial_balance(company, B_READBACK_FROM, B_READBACK_TO)
    except (WriteFailed, WriteTimeout) as exc:                                                              # I7
        report.problems.append(f"Trial Balance read failed: {exc}")
        return
    rows = exploded_tb_rows(raw)

    primaries = primary_group_rows(rows)
    total = sum((row["closing_balance"] for row in primaries.values() if row["closing_balance"] is not None),
               Decimal("0.00"))
    revaluation = expected_figures(dataset).forex_revaluation
    forex_gap = revaluation[max(revaluation)] if revaluation else Decimal("0.00")
    report.notes.append(
        f"Trial Balance Dr/Cr total observed: {total}; expected {forex_gap} — C47 (live 2026-09-25): a forex ledger is "
        "valued at its latest voucher rate while its sales keep the vouchers' INR bases, so the TB is out by the "
        "unrealised forex difference. The live netting rule for this (including how/whether opening stock "
        "reconciles it) is unsettled — that's probe 16/17/18's job to confirm, not asserted here (S0-D7).")

    actual_by_name: dict[str, Decimal] = {}
    for row in rows:
        name = row["account_name"]
        if name in expected_buckets and name not in actual_by_name and row["closing_balance"] is not None:
            actual_by_name[name] = row["closing_balance"]
    for bucket, expected_amount in sorted(expected_buckets.items()):
        actual_amount = actual_by_name.get(bucket, Decimal("0.00"))
        if abs(abs(actual_amount) - abs(expected_amount)) > BALANCE_TOLERANCE:
            report.problems.append(
                f"{bucket}: balance magnitude mismatch — expected |{expected_amount}|, Tally has |{actual_amount}|")


def _parse_tally_date(text: str) -> date | None:
    cleaned = (text or "").strip()
    if len(cleaned) == 8 and cleaned.isdigit():
        return date(int(cleaned[:4]), int(cleaned[4:6]), int(cleaned[6:8]))
    return None
