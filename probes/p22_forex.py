"""Probe 22: do forex vouchers expose the INR base amount? (S0 spec §7 "Probe 22", B). Feeds decision 15.

Reads the USD export sales (101/102) with probe 5's confirmed month request — the extractor's own request (S0-D7) —
for the Sep-2022 window (C43-safe: educational 01..02-09-2022). Records every line's raw AMOUNT and any forex-only
fields; `amounts.parse_decimal` raising on an expression is expected (§11.2) and recorded. The forex party's ledger
and the Currency masters are recorded, not judged (plan part 7 Rulings P7-13, P7-14).

Live write shape (plan part 7 Task 2, forex_shape_2026-09-25_run2): company B exports a forex line as
`-$448.44 @ ? 82.99/$ = -? 37216.04` — its base currency is NAMEd "?" (R-SYM) and written with a space. A live voucher
also exports both ALLLEDGERENTRIES.LIST and LEDGERENTRIES.LIST for the same lines, so a voucher is judged on its
primary lines only (`reads.primary_lines`), each posting once."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from agent.tally.amounts import AmountParseError, parse_decimal
from agent.tally.envelopes import formula_string
from agent.tally.xml_utils import read_objects
from probes.company_b_view import (USD_EXPORT_PARTY, drift_message, fetch_window, forex_vouchers, loaded_licence,
                                      month_window, tag_of)
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.reads import forex_base, master_request, parse_forex_amount, parse_vouchers, primary_lines

LEDGER_FIELDS = ["Name", "Parent", "CurrencyName", "OpeningBalance", "ClosingBalance"]
CURRENCY_FIELDS = ["Name", "MailingName", "ExpandedSymbol", "DecimalPlaces"]
OK_IMPACT = ("Forex voucher lines carry the INR base amount in the export (the AMOUNT's stated base, or a plain field): "
             "S1 stores the base in every amount column and keeps face value + rate in raw — decision 15 holds, no "
             "conversion at read time.")
COMPUTED_IMPACT = ("The export gives only the forex face value and rate: S2 computes base = face × rate (ROUND_HALF_UP to "
                   "paise) at extract time and stores it; decision 15 holds with one documented computation, and "
                   "parity must tolerate Tally's own rounding of that product.")
FAILED_IMPACT = ("The INR base can't be read from a forex voucher line unambiguously, or it doesn't balance: decision 15 "
                 "is revisited before S1 (Part 1 probe 22).")
UNEXPECTED_IMPACT = ("Tally adds ledger line(s) to a forex voucher that were never sent (e.g. a rounding or exchange "
                     "line): S2 must expect lines beyond the entered ones on a forex voucher, and decision 15's base "
                     "handling is revisited before S1 (Part 1 probe 22).")
REVALUATION_NOTE = (" The forex party's balance is its face total at the latest voucher rate, not the sum of the "
                    "vouchers' INR bases (C47): S1's per-ledger and TB parity must expect that unrealised forex "
                    "difference (the TB is out by it).")
LEDGER_EXPRESSION_NOTE = (" The forex party's ClosingBalance exports as an expression, not a number: S1 parity (decision "
                          "11) must parse its base part the same way, or read that ledger's balance from the TB.")


def revaluation(closing: str | None, specs: dict) -> dict:
    """C47 (live 2026-09-25): TallyPrime values a forex ledger at the rate of its LATEST-dated forex voucher, not at
    the sum of the vouchers' INR bases. Recorded, never judged: the ledger's closing base (the stated base of an
    expression, or a plain number), whether it equals face total × the latest dataset rate, and its difference from
    the sum of the party lines' bases (the unrealised forex difference that leaves the TB out)."""
    ordered = sorted(specs.values(), key=lambda v: (v.date, v.tag))
    party_lines = [l for v in ordered for l in v.lines if l.ledger == v.party]
    face = sum(((-v.fx_amount if l.amount < 0 else v.fx_amount) for v in ordered for l in v.lines
                if l.ledger == v.party), Decimal("0.00"))
    bases = sum((l.amount for l in party_lines), Decimal("0.00"))
    expected = (face * ordered[-1].fx_rate).quantize(Decimal("0.01"), ROUND_HALF_UP) if ordered else None
    fa = parse_forex_amount(closing)
    try:
        value = fa.base if fa is not None else parse_decimal(closing)
    except AmountParseError:
        value = None
    return {"closing_base": None if value is None else f"{value:.2f}", "bases_total": f"{bases:.2f}",
            "revalued_at_latest_rate": None if value is None else value == expected,
            "revaluation_difference": None if value is None else f"{value - bases:.2f}"}


def _parts_match(line: dict, spec) -> dict[str, bool | None]:
    """Review M1: the forex parts of a line against the dataset — currency symbol, face value, rate. None when the
    line carries no forex expression (plain, `field` or unparsed: nothing to compare)."""
    fa = parse_forex_amount(line["amount_raw"])
    if fa is None:
        return {"currency_matches": None, "fx_matches": None, "rate_matches": None}
    return {"currency_matches": fa.currency == spec.currency_symbol, "fx_matches": abs(fa.fx) == spec.fx_amount,
            "rate_matches": fa.rate == spec.fx_rate}


def _route(line: dict) -> tuple[str, Decimal | None, bool, list[str]]:
    """How one ledger line's INR base can be read (plan part 7 Task 4's table): `stated` / `computed` (a forex
    expression with or without "= base"), `field` (a plain number plus a leaf field mentioning `$` or `@`),
    `plain_no_forex` (a plain number and nothing forex), `unparsed`."""
    text = line["amount_raw"]
    try:
        plain, raises = parse_decimal(text), False
    except AmountParseError:
        plain, raises = None, True
    hints = sorted(k for k, v in line["fields"].items() if k != "AMOUNT" and ("$" in v or "@" in v))
    fa = parse_forex_amount(text)
    if fa is not None:
        base, how = forex_base(fa)
        return how, base, raises, hints
    if plain is not None:
        return ("field" if hints else "plain_no_forex"), plain, raises, hints
    return "unparsed", None, raises, hints


def judge_voucher(voucher: dict, spec, *, party_alias: str | None = None) -> dict:
    """One forex voucher against its dataset spec: every primary line's route and base, whether the bases sum to
    0.00, and whether each |base| equals the dataset's INR amount for that ledger. `party_alias` lets the live
    throwaway's party (a `ZZ Forex Probe …` ledger) stand in for the dataset's. Review fix round: the currency, face
    and rate of each forex line are compared too (M1), and a ledger the dataset voucher doesn't have is recorded as
    `unexpected_ledgers` (M3) so it isn't mistaken for a plain-INR store."""
    expected = {l.ledger: abs(l.amount) for l in spec.lines}
    if party_alias and party_alias not in expected:
        expected[party_alias] = expected.pop(spec.party)
    lines = []
    for line in primary_lines(voucher):
        route, base, raises, hints = _route(line)
        lines.append({"ledger": line["fields"].get("LEDGERNAME", ""), "amount_raw": line["amount_raw"],
                      "route": route, "base": None if base is None else f"{base:.2f}",
                      "parse_decimal_raises": raises, "forex_fields": hints, **_parts_match(line, spec)})
    bases = [Decimal(l["base"]) for l in lines if l["base"] is not None]
    known = bool(lines) and len(bases) == len(lines)
    unexpected = sorted({l["ledger"] for l in lines} - set(expected))
    ours = [l for l in lines if l["ledger"] in expected]
    mismatched = sorted({part for l in ours for part, key in (("currency", "currency_matches"), ("face", "fx_matches"),
                                                              ("rate", "rate_matches")) if l[key] is False})
    return {"tag": spec.tag, "lines": lines, "routes": sorted({l["route"] for l in ours}),
            "unexpected_ledgers": unexpected, "forex_mismatch": mismatched,
            "balanced": known and sum(bases, Decimal("0.00")) == 0,
            "base_matches_dataset": known and {l["ledger"] for l in ours} == set(expected) and all(
                abs(Decimal(l["base"])) == expected[l["ledger"]] for l in ours),
            "forex_matches_dataset": not mismatched}


def verdict(judged: list[dict]) -> tuple[Outcome, str, str]:
    """First match wins (plan part 7 Task 4 / Ruling P7-13). BLOCKED rows raise `ProbeBlocked`.
    Review fix round: the C36 row looks at the dataset's own ledgers only; a line the dataset voucher doesn't have is
    its own FAILED row (M3, after C36); a face/rate/currency that differs from the dataset is drift, BLOCKED like a
    base that differs (M1, row 4)."""
    for j in judged:
        if "plain_no_forex" in j["routes"]:
            raise ProbeBlocked(f"forex_sales: tag {j['tag']} has no forex in the export — it was written as plain INR "
                               "(the C36 failure). Re-run plan part 7's shape probe / setup-b before judging.")
    extra = {j["tag"]: j["unexpected_ledgers"] for j in judged if j["unexpected_ledgers"]}
    if extra:
        return (Outcome.FAILED, "unexpected line(s) on forex voucher(s): "
                + "; ".join(f"tag {t}: {', '.join(names)}" for t, names in sorted(extra.items())), UNEXPECTED_IMPACT)
    bad = [j["tag"] for j in judged if "unparsed" in j["routes"]]
    if bad:
        return Outcome.FAILED, f"forex line(s) of {bad} parse to no base", FAILED_IMPACT
    bad = [j["tag"] for j in judged if not j["balanced"]]
    if bad:
        return Outcome.FAILED, f"forex voucher(s) {bad} don't balance to 0.00 in INR", FAILED_IMPACT
    bad = [j["tag"] for j in judged if not j["base_matches_dataset"]]
    if bad:
        raise ProbeBlocked(f"forex_sales: Tally's stored base for {bad} differs from the dataset — the expected "
                           "figures probes 16/18/21 use are wrong; re-run `setup-b` verify or restore the backup.")
    bad = {j["tag"]: j["forex_mismatch"] for j in judged if j["forex_mismatch"]}
    if bad:
        raise ProbeBlocked("forex_sales: Tally's stored " + "; ".join(
            f"{'/'.join(parts)} for {t}" for t, parts in sorted(bad.items())) + " differs from the dataset — the "
            "forex voucher isn't the one setup-b wrote; re-run `setup-b` verify or restore the backup.")
    routes = {r for j in judged for r in j["routes"]}
    if routes <= {"stated", "field"}:
        return Outcome.CONFIRMED, f"INR base read from the export ({', '.join(sorted(routes))})", OK_IMPACT
    return Outcome.DIFFERENT, "INR base only computable as face × rate", COMPUTED_IMPACT


async def run_b(ctx: ProbeContext) -> PartResult:
    licence = loaded_licence(ctx.store.environment)
    confirmed = ctx.store.confirmed("voucher_month")
    if confirmed is None:
        raise ProbeBlocked("No confirmed month request: run probe 5 first.")
    specs = forex_vouchers(licence)
    months = sorted({(v.date.year, v.date.month) for v in specs.values()})
    found: dict[int, dict] = {}
    inr_fields: set[str] = set()
    for year, month in months:
        start, end = month_window(year, month, licence)
        step = "forex_sales" if len(months) == 1 else f"forex_sales_{year}_{month:02d}"
        result, raw = await fetch_window(ctx, step, confirmed["xml_template"], licence,
                                         start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y"))
        if result["drifted"]:
            raise ProbeBlocked(drift_message(step, result))
        for v in parse_vouchers(raw):
            tag = tag_of(v["header"].get("NARRATION", ""))
            if tag in specs:
                found[tag] = v
            elif v["header"].get("VOUCHERTYPENAME", "").startswith("Sales"):
                inr_fields |= {k for line in primary_lines(v) for k in line["fields"]}
    missing = sorted(set(specs) - set(found))
    if missing:
        raise ProbeBlocked(f"forex_sales: USD export sale(s) {missing} not in Tally — run `setup-b` with the plan "
                           "part 7 loader (Task 6) first.")
    judged = [judge_voucher(found[t], specs[t]) for t in sorted(specs)]
    ctx.observe("vouchers", {str(j["tag"]): j for j in judged})
    ctx.observe("extra_fields", sorted({k for t in found for line in primary_lines(found[t])
                                        for k in line["fields"]} - inr_fields))

    rows = read_objects(await ctx.send("usd_ledger", master_request(
        "S0P22Ledger", "Ledger", LEDGER_FIELDS, ctx.company_name,
        filters=[("S0P22IsParty", f"$Name = {formula_string(USD_EXPORT_PARTY)}")])), "LEDGER", LEDGER_FIELDS)
    rows = [r for r in rows if r.get("Name") == USD_EXPORT_PARTY]
    closing = rows[0]["ClosingBalance"] if rows else None
    try:
        closing_form = "plain" if parse_decimal(closing) is not None else "missing"
    except AmountParseError:
        closing_form = "expression"
    revalued = revaluation(closing, specs) if rows else {}
    ctx.observe("usd_ledger", {"found": bool(rows), "currency_name": rows[0]["CurrencyName"] if rows else None,
                               "closing_text": closing, "closing_form": closing_form, **revalued})
    currencies = read_objects(await ctx.send("currencies", master_request("S0P22Currencies", "Currency",
                              CURRENCY_FIELDS, ctx.company_name)), "CURRENCY", CURRENCY_FIELDS)
    ctx.observe("currencies", currencies)

    outcome, summary, impact = verdict(judged)
    if closing_form == "expression":
        summary += "; the forex party's ClosingBalance exports as an expression"
        impact += LEDGER_EXPRESSION_NOTE
    if revalued.get("revalued_at_latest_rate"):
        summary += (f"; the forex party is valued at the latest voucher rate (C47): "
                    f"{revalued['revaluation_difference']} from the vouchers' bases")
        impact += REVALUATION_NOTE
    return PartResult(outcome, f"{summary} — tags {sorted(specs)}", spec_impact=impact)


PROBE = Probe(
    id=22,
    name="forex",
    question="Does a forex voucher's ledger line expose the INR base amount, and does it balance to 0.00?",
    feeds=("decision 15",),
    parts={"B": run_b},
    requires=(0, 5),
    educational_sensitive=True,
)
