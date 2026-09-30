"""One-shot LIVE shape probe for a forex voucher (plan part 7, Ruling C36's unblocker). Not wired into the CLI —
run by hand against company B (Task 2's runbook), like sign_check.py. Everything it creates is a throwaway, read
back and deleted, EXCEPT the Currency master, which the forex sales need. Every raw read-back is saved to `out_dir`
(the committed evidence folder) with a summary.json, which is written on EVERY path that gets past the guards —
before the cleanup and again after it (review 2026-09-25 M1/I3).

Review fix round 2026-09-25 (docs/code-review-bi-s0-part7-task1-2026-09-25.md): read-backs are judged on their
VALUES against what was sent (I1); V0 dropped = F2, not a broken shape (I2); a ledger is registered for cleanup the
moment Tally has it (I3); a voucher is deleted only when its read-back MasterID is the LASTVCHID (I4); the currency
is matched on symbol or formal name and an unlisted create is "unverified", never retried (I5); voucher numbers
Sep-2022..FY end are compared before/after (M6). Rulings R-SYM (the base symbol is DISCOVERED — live B's is "?") and
R-F2 (`f2_confirm` replaces the input() pause; refused without a terminal).
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable

from tally_bridge.amounts import AmountParseError, parse_decimal
from tally_bridge.envelopes import wrap_collection
from tally_bridge.xml_utils import read_objects
from probes.reads import ForexAmount, forex_base, parse_forex_amount, parse_vouchers
from probes.safety import check_company
from probes.setup.company_b_data import USD_CURRENCY, CurrencySpec
from probes.setup.writes import (CURRENCY_FIELDS, ForexLine, TallyWriter, WriteFailed, WriteRefused, WriteTimeout,
                                    WriteUnverified, b_day_voucher_request, currency_request, ledger_detail_request)

DAY = "01-09-2022"                 # C43-safe (day 1), inside B's books, the day tag 101 lands on (Educational)
DATE = "20220901"
DATE_TEXT = "1-Sep-2022"
# M6: every voucher from the throwaways' day to FY 2022-23's end — where an automatic Sales renumbering would show.
# Both ends are C43-safe (days 1 and 31); header fields only.
NUMBERS_FROM, NUMBERS_TO = DAY, "31-03-2023"
NUMBER_FIELDS = ["MasterID", "VoucherNumber", "AlterID", "Date", "VoucherTypeName", "Narration"]
USD_PARTY = "ZZ Forex Probe USD Debtor"
INR_PARTY = "ZZ Forex Probe INR Debtor"
NOMINAL = "Export Sales"
FX_AMOUNT, RATE = Decimal("448.44"), Decimal("82.99")          # tag 101's own figures
INR = Decimal("37216.04")
NARRATION = "S0-throwaway forex {}"
THROWAWAY_PREFIX = "S0-throwaway forex"
# Candidate Company fields for "is multi-currency on?" — recorded, never judged (Ruling P7-6).
COMPANY_FEATURE_FIELDS = ["Name", "BaseCurrencySymbol", "BaseCurrencyName", "IsMultiCurrencyOn",
                          "UseMultiCurrency", "MultiCurrencyOn"]
F2_TEXT = ("In TallyPrime check F2 (Current Date) is 31-03-2026 or later — a restart resets it. "
           "Vouchers dated after F2 are silently dropped (LESSONS §15 rule 14).")
POPUP_HINT = "Tally timed out — check it for an open popup/modal, dismiss it (or restart Tally), then restore if needed."
RESTORE_HINT = "restore the pre-forex backup (plan part 7 Task 2 step 7)"
STORED = ("forex_full", "forex_no_base", "plain_with_forex_field")


def _console_f2_confirm() -> None:
    input(f"\n>>> {F2_TEXT}\n    Press Enter when F2 is set... ")


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


@dataclass
class VariantResult:
    id: str
    party: str
    form: str | None
    base_symbol: str | None = None                          # the base symbol the rate was WRITTEN with (R-SYM)
    import_error: str = ""
    master_id: str = ""
    lines: list[dict] = field(default_factory=list)       # {"ledger", "amount_raw", "fields", "classification", "problems"}
    classification: str = "not_run"                        # the party line's class; forex_mismatch / refused / dropped


@dataclass
class ShapeReport:
    outcome: str = "not_run"
    base_symbol: str = ""                                # the base currency's NAME as Tally lists it (R-SYM)
    currency: dict = field(default_factory=dict)
    variants: list[VariantResult] = field(default_factory=list)
    chosen: dict | None = None
    numbering: dict = field(default_factory=dict)       # M6
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {**asdict(self), "variants": [asdict(v) for v in self.variants]}

    def summary(self) -> str:
        rows = ", ".join(f"{v.id}={v.classification}" for v in self.variants)
        changed = self.numbering.get("changed")
        return (f"outcome={self.outcome} chosen={self.chosen} base_symbol={self.base_symbol!r} "
                f"numbering_changed={changed} variants: {rows or '—'}")


def base_currency(rows: list[dict[str, str]]) -> dict[str, str] | None:
    """R-SYM: the company's base currency row — the one whose formal name reads INR (live B: NAME "?", MAILINGNAME
    and EXPANDEDSYMBOL "INR"), else the only row. None when that is ambiguous or the row has no Name."""
    inr = [r for r in rows if "INR" in (r.get("MailingName"), r.get("ExpandedSymbol"), r.get("OriginalName"))]
    row = inr[0] if len(inr) == 1 else (rows[0] if len(rows) == 1 else None)
    return row if row is not None and row.get("Name") else None


def _stored(v: VariantResult) -> bool:
    return v.classification in STORED


def _numbers(raw: str) -> dict[str, dict[str, str]]:
    return {r["MasterID"]: {"number": r["VoucherNumber"], "alter_id": r["AlterID"], "date": r["Date"],
                            "type": r["VoucherTypeName"]}
            for r in read_objects(raw, "VOUCHER", NUMBER_FIELDS)
            if r["MasterID"] and not r["Narration"].startswith(THROWAWAY_PREFIX)}


def numbering_diff(before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]) -> dict:
    """M6: the real (non-throwaway) vouchers' numbers and AlterIDs, before vs after the run. Any difference means
    company B changed beyond the currency master — restore."""
    both = sorted(set(before) & set(after), key=lambda m: int(m) if m.isdigit() else 0)
    diff = {"window": f"{NUMBERS_FROM}..{NUMBERS_TO}", "before": len(before), "after": len(after),
            "number_changed": [m for m in both if before[m]["number"] != after[m]["number"]],
            "alter_id_changed": [m for m in both if before[m]["alter_id"] != after[m]["alter_id"]],
            "missing": sorted(set(before) - set(after)), "added": sorted(set(after) - set(before))}
    diff["changed"] = any(diff[k] for k in ("number_changed", "alter_id_changed", "missing", "added"))
    return diff


def forex_problems(fa: ForexAmount, sent_inr: Decimal, *, base_symbol: str, symbol: str = "$",
                   fx: Decimal = FX_AMOUNT, rate: Decimal = RATE) -> list[str]:
    """I1 (review 2026-09-25): how a read-back forex expression differs from what was SENT. Empty = it is the sent
    voucher line, value for value: the currency, the face, the rate, the sign, the INR base (stated, or face × rate
    when not stated — no rounding tie here, fact 4) and a base symbol that is the discovered one or none (R-SYM)."""
    problems = []
    if fa.currency != symbol:
        problems.append(f"currency {fa.currency!r} ≠ {symbol!r}")
    if abs(fa.fx) != fx:
        problems.append(f"face {abs(fa.fx)} ≠ {fx}")
    if fa.rate != rate:
        problems.append(f"rate {fa.rate} ≠ {rate}")
    if (fa.fx < 0) != (sent_inr < 0):
        problems.append(f"face sign {fa.fx} ≠ the sent INR's sign ({sent_inr})")
    if fa.rate_symbol not in (base_symbol, ""):
        problems.append(f"base symbol {fa.rate_symbol!r} is neither the discovered {base_symbol!r} nor none")
    base, how = forex_base(fa)
    if base != sent_inr:
        problems.append(f"{how} base {base} ≠ the sent {sent_inr}")
    return problems


def _field_carries_forex(line: dict, symbol: str, fx: Decimal) -> bool:
    """I1: a strict field route — a non-AMOUNT leaf whose text carries the face value AND the currency symbol."""
    face = f"{fx:.2f}"
    return any(face in text and symbol in text for key, text in line["fields"].items() if key != "AMOUNT")


def classify(line: dict, sent_inr: Decimal, *, base_symbol: str, symbol: str = "$", fx: Decimal = FX_AMOUNT,
             rate: Decimal = RATE) -> str:
    """One read-back line, judged ONLY on its values against what was sent (I1): `forex_full` / `forex_no_base` (an
    expression that is exactly the sent line), `forex_mismatch` (an expression that differs — see
    `forex_problems`), `plain_with_forex_field` (the sent INR plus a field carrying the face value and symbol),
    `plain_inr` (the sent INR and nothing forex — the C36 failure on a forex variant), `other`."""
    fa = parse_forex_amount(line["amount_raw"])
    if fa is not None:
        if forex_problems(fa, sent_inr, base_symbol=base_symbol, symbol=symbol, fx=fx, rate=rate):
            return "forex_mismatch"
        return "forex_full" if fa.base is not None else "forex_no_base"
    try:
        value = parse_decimal(line["amount_raw"])
    except AmountParseError:
        return "other"
    if value is None or value != sent_inr:
        return "other"
    return "plain_with_forex_field" if _field_carries_forex(line, symbol, fx) else "plain_inr"


def run(writer: TallyWriter, company: str, out_dir: Path, *, currency: CurrencySpec = USD_CURRENCY,
        f2_confirm: Callable[[], None] = _console_f2_confirm) -> ShapeReport:
    """R-F2: `f2_confirm` is called once, after the currency step and before the first throwaway. The default asks
    on the console, so it needs a terminal; without one the run is refused before any request. A caller that has set
    F2 itself passes `f2_confirm=lambda: None`."""
    if f2_confirm is _console_f2_confirm and not _stdin_is_tty():
        raise WriteRefused("forex_shape.run: stdin is not a terminal, so the F2 confirmation can't be asked. Set F2 "
                           "(>= 31-03-2026) in TallyPrime yourself and pass f2_confirm=lambda: None. Nothing was sent.")
    if (out_dir / "summary.json").exists():
        raise FileExistsError(f"{out_dir} already holds a run's evidence — pick a new folder, never overwrite it")
    check_company(writer.company_names(), company, mutating=True)          # one company, B, "Probe" in the name
    out_dir.mkdir(parents=True, exist_ok=True)
    report = ShapeReport()
    made: list[str] = []
    numbers_before: dict | None = None

    def save(step: str, xml: str) -> str:
        text = writer.post(xml)
        (out_dir / f"{step}.xml").write_text(text, encoding="utf-8")
        return text

    def write_summary() -> None:
        (out_dir / "summary.json").write_text(json.dumps(report.to_json(), indent=2, ensure_ascii=False, default=str),
                                              encoding="utf-8")

    def create_ledger(name: str, cur: str | None) -> None:
        """I3: the ledger joins `made` as soon as Tally has it — even when the create's own read-back then raises
        (a CURRENCYNAME that didn't stick), so the cleanup always deletes it."""
        try:
            writer.create_party_ledger(company, name, parent="Sundry Debtors", bill_wise=False, currency=cur)
        except WriteTimeout:
            raise
        except WriteFailed:
            if writer.ledger(company, name) is not None:            # absent before (leftover guard): ours
                made.append(name)
            raise
        made.append(name)

    def variant(vid: str, party: str, form: str | None, base_symbol: str | None) -> VariantResult:
        result = VariantResult(id=vid, party=party, form=form, base_symbol=base_symbol)
        report.variants.append(result)
        forex = (None if form is None else
                 ForexLine(currency.symbol, FX_AMOUNT, RATE, form=form, base_symbol=base_symbol))
        narration = NARRATION.format(vid)
        try:
            result.master_id = writer.create_b_voucher(
                company, vch_type="Sales", date=DATE, narration=narration, party=party,
                lines=[(party, -INR, True), (NOMINAL, INR, False)], forex=forex)
        except WriteTimeout:
            raise
        except WriteFailed as exc:
            result.import_error, result.classification = str(exc), "refused"
            return result
        raw = save(f"variant_{vid}", b_day_voucher_request(company, DAY))
        found = [v for v in parse_vouchers(raw) if v["header"].get("NARRATION") == narration]
        if not found:
            result.classification = "dropped"          # created=1 but not there: F2 or date (rule 14)
            return result
        for line in found[0]["ledger_lines"]:
            ledger = line["fields"].get("LEDGERNAME", "")
            sent = -INR if ledger == party else INR
            fa = parse_forex_amount(line["amount_raw"])
            result.lines.append({
                "ledger": ledger, "amount_raw": line["amount_raw"], "fields": line["fields"],
                "classification": classify(line, sent, base_symbol=report.base_symbol, symbol=currency.symbol),
                "problems": forex_problems(fa, sent, base_symbol=report.base_symbol, symbol=currency.symbol)
                if fa is not None else []})
        party_line = next((l for l in result.lines if l["ledger"] == party), None)
        result.classification = party_line["classification"] if party_line else "other"
        if any(l["classification"] == "forex_mismatch" for l in result.lines):
            result.classification = "forex_mismatch"          # I1: any line off the sent values
            report.notes.append(f"{vid}: read back but not as sent — "
                                + "; ".join(f"{l['ledger']}: {', '.join(l['problems'])}"
                                            for l in result.lines if l["problems"]))
        # I4: the delete goes by Master ID. Only delete when the voucher READ BACK by its narration is the one
        # LASTVCHID named — otherwise that ID could be a real company-B voucher.
        read_back_id = found[0]["header"].get("MASTERID", "")
        if len(found) != 1 or read_back_id != result.master_id:
            raise WriteFailed(f"{vid}: read-back MasterID {read_back_id!r} (of {len(found)} voucher(s) named "
                              f"{narration!r}) ≠ LASTVCHID {result.master_id!r} — not deleting anything; the "
                              f"throwaway stays in B: {RESTORE_HINT}")
        writer.delete_b_voucher(company, result.master_id, vch_type="Sales", day=DAY, date_text=DATE_TEXT)
        return result

    def body() -> None:
        nonlocal numbers_before
        # Leftovers from an aborted run: refuse, never adopt (a leftover could be the wrong shape) — sign_check pattern.
        ledgers = writer.list_ledgers(company)
        day_rows = parse_vouchers(writer.post(b_day_voucher_request(company, DAY)))
        if USD_PARTY in ledgers or INR_PARTY in ledgers or any(
                v["header"].get("NARRATION", "").startswith(THROWAWAY_PREFIX) for v in day_rows):
            report.outcome = "leftover_found"
            raise WriteFailed("A leftover from an earlier forex shape run exists — delete it in the UI or restore the "
                              "pre-forex backup first (plan part 7 Task 2).")

        save("company_features", wrap_collection("S0FxCompany", "Company", COMPANY_FEATURE_FIELDS, company))
        before = read_objects(save("currencies_before", currency_request(company)), "CURRENCY", CURRENCY_FIELDS)
        save("export_sales_ledger", ledger_detail_request(company, NOMINAL))
        numbers_before = _numbers(save("numbers_before", _numbers_request(company)))
        base = base_currency(before)
        if base is None:
            report.outcome = "base_currency_unknown"
            report.notes.append("No single base (INR) currency in the Currency list — see currencies_before.xml")
            return
        report.base_symbol = base["Name"]

        popup = False
        try:
            created = writer.create_currency(company, currency)
        except WriteTimeout:
            popup = True
            report.outcome = "popup"
            report.notes.append(POPUP_HINT)
            return
        except WriteUnverified as exc:
            report.outcome = "currency_unverified"
            report.notes.append(f"Currency {currency.symbol!r}: {exc}")
            return
        except WriteFailed as exc:
            report.outcome = "currency_refused"
            report.notes.append(f"Currency {currency.symbol!r}: {exc}")
            return
        finally:
            if not popup:
                save("currencies_after", currency_request(company))       # I5: evidence on every non-modal path
        report.currency = {"symbol": currency.symbol, "created_now": created}
        f2_confirm()

        for name, cur in ((INR_PARTY, None), (USD_PARTY, currency.symbol)):
            try:
                create_ledger(name, cur)
            except WriteTimeout:
                raise
            except WriteFailed as exc:
                report.outcome = "ledger_currency_refused" if cur else "ledger_refused"
                report.notes.append(f"Ledger {name!r}: {exc}")
                save("ledgers_after_create", ledger_detail_request(company, name))
                return
        save("ledgers_after_create", ledger_detail_request(company, USD_PARTY))

        v0 = variant("V0", INR_PARTY, None, None)
        if v0.classification == "dropped":                  # I2: every voucher would vanish — F2, not the shape
            report.outcome = "f2_or_date_dropped"
            report.notes.append(f"V0 answered created=1 but is not on {DAY}: {F2_TEXT}")
            return
        if v0.classification != "plain_inr":
            report.outcome = "control_failed"
            report.notes.append("V0 (plain INR, non-inventory, no-GST Sales) did not post cleanly — the base "
                                "voucher shape is wrong, so no forex result would mean anything.")
            return
        # R-SYM order: the discovered base symbol, then the rate with no base symbol, then no stated base.
        v1 = variant("V1", USD_PARTY, "full", report.base_symbol)
        best = v1 if _stored(v1) else None
        if best is None:
            v1b = variant("V1b", USD_PARTY, "full", "")
            best = v1b if _stored(v1b) else None
        if best is None:
            v2 = variant("V2", USD_PARTY, "no_base", report.base_symbol)
            best = v2 if _stored(v2) else None
        form, base_symbol = (best.form, best.base_symbol) if best else ("full", report.base_symbol)
        variant("V3", INR_PARTY, form, base_symbol)

        if any(v.classification == "dropped" for v in report.variants):
            report.notes.append(f"A throwaway answered created=1 but was not on {DAY} — {F2_TEXT}")
        stored = [v for v in report.variants if _stored(v)]
        usd = [v for v in stored if v.party == USD_PARTY]
        forex_variants = [v for v in report.variants if v.form]
        if usd or stored:
            chosen = (usd or stored)[0]
            report.outcome = "stored_forex"
            report.chosen = {"variant": chosen.id,
                             "party_currency": currency.symbol if chosen.party == USD_PARTY else None,
                             "form": chosen.form, "base_symbol": chosen.base_symbol}
        elif any(v.classification == "forex_mismatch" for v in forex_variants):
            report.outcome = "forex_mismatch"
        elif any(v.classification == "plain_inr" for v in forex_variants):
            report.outcome = "forex_dropped"
        elif any(v.classification == "dropped" for v in forex_variants):
            report.outcome = "f2_or_date_dropped"
        elif any(v.classification == "other" for v in forex_variants):
            report.outcome = "unrecognised"
        else:
            report.outcome = "refused"

    error: BaseException | None = None
    try:
        body()
    except WriteTimeout:
        report.outcome = "popup"
        report.notes.append(POPUP_HINT)
    except Exception as exc:                              # noqa: BLE001 — recorded, summary written, then re-raised
        error = exc
        report.notes.append(f"aborted ({report.outcome}): {type(exc).__name__}: {exc}")
        report.outcome = "aborted"
    write_summary()                                       # M1: before the cleanup, so a cleanup failure keeps it
    if report.outcome == "popup":
        return report                                    # Tally is behind a modal: no more requests; restore
    cleanup_error: WriteFailed | None = None
    for name in reversed(made):
        try:
            writer.delete_ledger(company, name)
        except WriteFailed as exc:
            # Live Tally refuses to delete a ledger that still has a voucher (a delete that did not stick):
            # record it; the FIRST error is the one raised (D3).
            report.notes.append(f"cleanup: {exc} — {RESTORE_HINT}")
            cleanup_error = cleanup_error or exc
    if numbers_before is not None:
        try:
            report.numbering = numbering_diff(numbers_before, _numbers(save("numbers_after",
                                                                            _numbers_request(company))))
            if report.numbering["changed"]:
                report.notes.append(f"company B's voucher numbers/AlterIDs changed in {report.numbering['window']} "
                                    f"(see numbering) — B changed beyond the currency: {RESTORE_HINT}")
        except WriteFailed as exc:
            report.notes.append(f"numbers_after not read: {exc}")
    write_summary()
    if error is not None:
        raise error
    if cleanup_error is not None:
        raise cleanup_error
    return report


def _numbers_request(company: str) -> str:
    return wrap_collection("S0FxNumbers", "Voucher", NUMBER_FIELDS, company,
                           static_vars={"SVFROMDATE": NUMBERS_FROM, "SVTODATE": NUMBERS_TO},
                           extra_collection_xml="<CHILDOF>$$VchTypeAllVouchers</CHILDOF>")
