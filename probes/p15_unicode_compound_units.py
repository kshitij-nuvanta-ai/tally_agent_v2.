"""Probe 15: Hindi text and a compound-unit stock item (S0 spec §7 "Probe 15", B). Feeds R14, R15.

"Byte-exact" is judged on the PARSED text (code-point equality with the dataset); whether the raw response carried
the Hindi as UTF-8 bytes or as numeric character references is recorded, not judged — both parse to the same text
(Ruling Q8, spec §7 probe 15 Changed 2026-09-24).
Vouchers are fetched with probe 5's confirmed month request as one-day windows (S0-D7); dataset dates sit on days
1/2/31, so no C43 clamp is needed. The Stock Summary is read at the books' end (31-03-2026): it asks for the current
position, not an as-on one (that question is probe 18's).
"""
from __future__ import annotations

import re

from tally_bridge.envelopes import formula_string, wrap_report
from tally_bridge.xml_utils import read_objects
from probes.company_b_view import (B_BOOKS_FROM, B_BOOKS_TO, COMPOUND_UNIT, HINDI_DEBTOR, drift_message,
                                      fetch_window, first_voucher, item_specs, loaded_licence, stock_qty_at, tag_of)
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.reads import dmy, master_request, parse_vouchers, qty_number, stock_rows_any_depth

LEDGER_FIELDS = ["Name", "Parent"]
ITEM_FIELDS = ["Name", "Parent", "BaseUnits", "OpeningBalance"]
OK_IMPACT = ("Hindi names and narrations round-trip exactly through the XML export and a compound-unit item's "
             "quantities read back in its first unit (C40): S1 stores text as UTF-8 unchanged and parses quantities by "
             "their leading number (R14, R15).")
TEXT_FAILED_IMPACT = ("Hindi text doesn't round-trip exactly: S1's text columns and the agent's parser are revisited "
                      "before S1 (R14).")
QTY_FAILED_IMPACT = ("A compound-unit quantity doesn't read back as written: the agent can't take compound quantities by "
                     "their leading number; S1 stores the raw quantity text and R15 is revisited.")
UNIT_IMPACT = ("The compound unit's name doesn't export as created ('Box of 10 Nos'): S1 stores units by the exported "
               "string and never re-derives it (R15).")


_CHAR_REF = re.compile(r"&#(x[0-9a-fA-F]+|[0-9]+);")


def _unref(text: str) -> str:
    return _CHAR_REF.sub(lambda m: chr(int(m.group(1)[1:], 16) if m.group(1)[0] == "x" else int(m.group(1))), text)


def text_check(parsed: str | None, expected: str, raw: bytes) -> dict:
    """Ruling Q8: `exact` (the verdict) is code-point equality of the PARSED text with the dataset. `encoding` records
    the transport form found in the raw response — "utf-8 bytes", "character references" (decimal or hex), or "not in
    the response" (e.g. mangled to `?????`) — and is never judged."""
    in_raw = expected.encode("utf-8") in raw
    if in_raw:
        encoding = "utf-8 bytes"
    elif expected in _unref(raw.decode("utf-8", errors="replace")):
        encoding = "character references"
    else:
        encoding = "not in the response"
    return {"exact": parsed == expected, "utf8_bytes_in_raw": in_raw, "encoding": encoding}


async def _one_voucher(ctx: ProbeContext, step: str, template: str, licence: str, spec) -> dict:
    day = spec.date.strftime("%d-%m-%Y")
    result, raw = await fetch_window(ctx, step, template, licence, day, day)
    if result["drifted"]:
        raise ProbeBlocked(drift_message(step, result))
    found = [v for v in parse_vouchers(raw) if tag_of(v["header"].get("NARRATION", "")) == spec.tag]
    if not found:
        raise ProbeBlocked(f"{step}: probe 5's request returned no voucher tagged {spec.tag} on {day} — company B "
                           "differs from the dataset, or the request stopped bounding a day (re-run probe 5).")
    return found[0]


async def run_b(ctx: ProbeContext) -> PartResult:
    licence = loaded_licence(ctx.store.environment)
    confirmed = ctx.store.confirmed("voucher_month")
    if confirmed is None:
        raise ProbeBlocked("No confirmed month request: run probe 5 first.")
    template, company = confirmed["xml_template"], ctx.company_name
    compound = next(name for name, spec in item_specs(licence).items() if spec.unit == COMPOUND_UNIT)

    text = await ctx.send("hindi_ledger", master_request("S0P15Ledger", "Ledger", LEDGER_FIELDS, company, filters=[
        ("S0P15IsHindi", f"$Name = {formula_string(HINDI_DEBTOR)}")]))
    names = [row["Name"] for row in read_objects(text, "LEDGER", LEDGER_FIELDS)]
    filter_matched = bool(names)
    raw = ctx.last_response.raw
    if not filter_matched:
        # I1: nobody has proven live that a `$Name = "<non-ASCII>"` TDL formula filter matches. An empty result is
        # the filter's own failure, not evidence of a text round-trip problem — fall back to the unfiltered read
        # (probe 14 already does exactly this read) and judge from THAT instead.
        all_text = await ctx.send("hindi_ledger_unfiltered", master_request("S0P15LedgerAll", "Ledger",
                                                                             LEDGER_FIELDS, company))
        names = [row["Name"] for row in read_objects(all_text, "LEDGER", LEDGER_FIELDS) if row["Name"] == HINDI_DEBTOR]
        raw = ctx.last_response.raw
    hindi_ledger = {"names": names, "filter_matched": filter_matched,
                    **text_check(names[0] if len(names) == 1 else None, HINDI_DEBTOR, raw)}

    hindi_spec = first_voucher(licence, lambda v: not v.narration.isascii())
    got = await _one_voucher(ctx, "hindi_narration", template, licence, hindi_spec)
    hindi_narration = {"tag": hindi_spec.tag, "got": got["header"].get("NARRATION"),
                       **text_check(got["header"].get("NARRATION"), hindi_spec.narration, ctx.last_response.raw)}

    item_rows = read_objects(await ctx.send("compound_unit_item", master_request(
        "S0P15Item", "StockItem", ITEM_FIELDS, company, filters=[("S0P15IsItem", f"$Name = {formula_string(compound)}")])),
        "STOCKITEM", ITEM_FIELDS)
    base_units = item_rows[0]["BaseUnits"] if item_rows else None
    compound_item = {"name": compound, "base_units": base_units, "expected": COMPOUND_UNIT,
                     "opening_text": item_rows[0]["OpeningBalance"] if item_rows else None,
                     "unit_exact": base_units == COMPOUND_UNIT}

    unit_spec = first_voucher(licence, lambda v: v.tag != hindi_spec.tag and any(i.item == compound for i in v.inventory))
    want = next(i.qty for i in unit_spec.inventory if i.item == compound)
    voucher = await _one_voucher(ctx, "compound_unit_voucher", template, licence, unit_spec)
    line = next((inv["fields"] for inv in voucher["inventory"] if inv["fields"].get("STOCKITEMNAME") == compound), {})
    read_qty = qty_number(line.get("ACTUALQTY", ""))
    compound_voucher = {"tag": unit_spec.tag, "actual_qty_text": line.get("ACTUALQTY"),
                        "billed_qty_text": line.get("BILLEDQTY"), "rate_text": line.get("RATE"), "expected_qty": want,
                        "qty_ok": read_qty is not None and abs(read_qty) == want}

    rows = stock_rows_any_depth(await ctx.send("stock_summary", wrap_report("Stock Summary", B_BOOKS_FROM, B_BOOKS_TO,
                                                                            company)))
    row = next((r for r in rows if r["name"] == compound), None)
    want_stock = stock_qty_at(licence, compound, dmy(B_BOOKS_TO))
    stock_summary = {"qty_text": row["qty_text"] if row else None, "expected_qty": want_stock,
                     "qty_ok": row is not None and row["qty"] == want_stock}

    for key, value in (("hindi_ledger", hindi_ledger), ("hindi_narration", hindi_narration),
                       ("compound_item", compound_item), ("compound_voucher", compound_voucher),
                       ("stock_summary", stock_summary)):
        ctx.observe(key, value)
    ctx.observe("tally_after", await ctx.company_names())       # "no crash, Tally answers a cheap read after"

    if not (hindi_ledger["exact"] and hindi_narration["exact"]):
        return PartResult(Outcome.FAILED, f"Hindi text isn't exact (ledger {hindi_ledger['names']}, narration "
                                          f"{hindi_narration['got']!r})", spec_impact=TEXT_FAILED_IMPACT)
    bad_qty = [what for what, ok in (("voucher", compound_voucher["qty_ok"]), ("stock summary", stock_summary["qty_ok"]))
               if not ok]
    if bad_qty:
        return PartResult(Outcome.FAILED, f"{compound} quantity reads back wrong in the {' and '.join(bad_qty)}",
                          spec_impact=QTY_FAILED_IMPACT)
    if not compound_item["unit_exact"]:
        return PartResult(Outcome.DIFFERENT, f"{compound}'s unit exports as {base_units!r}, not {COMPOUND_UNIT!r}",
                          spec_impact=UNIT_IMPACT)
    return PartResult(Outcome.CONFIRMED, f"Hindi ledger and narration (tag {hindi_spec.tag}) exact "
                                         f"({hindi_ledger['encoding']}); {compound} ({COMPOUND_UNIT}) reads "
                                         f"{compound_voucher['actual_qty_text']!r} on tag {unit_spec.tag} and "
                                         f"{stock_summary['qty_text']!r} in the Stock Summary; Tally answered after",
                      spec_impact=OK_IMPACT)


PROBE = Probe(
    id=15,
    name="unicode_compound_units",
    question="Do Hindi names/narrations and a compound-unit item round-trip exactly, without upsetting Tally?",
    feeds=("R14", "R15"),
    parts={"B": run_b},
    requires=(0, 5),
)
