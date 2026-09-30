"""Probe 21: full-history reach and size (S0 spec §7 "Probe 21", B part). Feeds decision 7b, Q22, Q23, R27.

Reach: fetch FY 2022-23 (company B's first year, three FYs behind the current one) month by month with probe 5's
confirmed request, each month asked as company_b_view.month_window (C43: under an educational licence a month ends on
the 2nd or the 31st, because Tally ignores other-day date variables; still exact). Every month's tags must equal the dataset's (company_b_view.compare_tags; the cancelled pair is
reported, not judged, because probe 3's B part owns the flags, S0-D7).
Size: measure bytes per voucher by kind in three forms. Raw XML; a generic JSON of the whole voucher (the stand-in for
S1's `raw` JSONB); and the Part 1 §5 minimum columns plus a per-row overhead (the stand-in for S1's typed rows). Then
extrapolate to 10k/50k/200k vouchers a year × 2/5/10 years, with and without `raw`. Those are the numbers Q22 and Q23
wait on.
Timings are recorded and labelled "Wine — not representative". The tier-C timing half stays ⏭ (Q29).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

from agent.tally.xml_utils import sanitize_xml
from probes.capture import TIMING_NOTE
from probes.company_b_view import (B_BOOKS_FROM, B_BOOKS_FROM_DATE, drift_message, expect_window, fetch_window,
                                      kind_label, loaded_licence, month_window, tag_of)
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.reads import parse_vouchers, primary_lines, tally_date

PG_ROW_OVERHEAD_BYTES = 28      # PostgreSQL heap tuple header (23 B, aligned to 24) + 4-byte line pointer
VOLUMES = (10_000, 50_000, 200_000)
YEARS = (2, 5, 10)
RECENT_FYS_WITH_RAW = 2         # Q22's option: keep `raw` only for the 2-FY window
CHUNK_CAP_VOUCHERS = 5_000      # Part 1 §5 extractor: day/month chunks capped at about 5k vouchers
BLOCK_TAGS = ("ALLLEDGERENTRIES.LIST", "ALLINVENTORYENTRIES.LIST", "BILLALLOCATIONS.LIST")
_MB = Decimal(10) ** 6
_BLOCK = re.compile(r"<VOUCHER\b[^>]*>(.*?)</VOUCHER>", re.S)


def voucher_blocks(raw_text: str) -> list[str]:
    """Each <VOUCHER>…</VOUCHER> exactly as sent. CMPINFO's childless <VOUCHER>0</VOUCHER> counter is skipped."""
    return [m.group(0) for m in _BLOCK.finditer(raw_text) if "<" in m.group(1)]


def xml_bytes(block: str) -> int:
    return len(block.encode("utf-8"))


def element_json(element: ET.Element) -> Any:
    """A generic XML → JSON walk: `.LIST` children become arrays, attributes go under "@", and empty placeholders and
    empty scalars are dropped. This is the stand-in for S1's `raw` JSONB."""
    children = [c for c in element if len(c) or (c.text or "").strip()]
    text = (element.text or "").strip()
    if not children:
        return {"@": dict(element.attrib), "#": text} if element.attrib else text
    out: dict[str, Any] = {"@": dict(element.attrib)} if element.attrib else {}
    for child in children:
        value = element_json(child)
        if child.tag.endswith(".LIST"):
            out.setdefault(child.tag, []).append(value)
        else:
            out[child.tag] = value
    return out


def _compact(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def raw_json_bytes(block: str) -> int:
    return _compact(element_json(ET.fromstring(sanitize_xml(block))))


def column_bytes(voucher: dict) -> tuple[int, int]:
    """(bytes, rows) of the Part 1 §5 minimum columns for one voucher: compact JSON of the values, plus
    PG_ROW_OVERHEAD_BYTES per row. Referenced GUIDs are sized like the voucher's own. Indexes are excluded.

    Part 1 spec §5 (docs/specs/2026-09-21-bi-part1-sync-design.md L493-495): `tally_voucher_ledger_lines`,
    `tally_voucher_inventory_lines` and `tally_bill_allocations` each carry a `voucher_id` column (the child row's
    foreign key back to its voucher). It is sized like the voucher's own exported GUID, same as every other
    referenced GUID column here.
    """
    h = voucher["header"]
    ref = "g" * len(h.get("GUID", ""))
    lines = primary_lines(voucher)
    rows: list[list[str]] = [[h.get("GUID", ""), h.get("MASTERID", ""), h.get("ALTERID", ""), h.get("DATE", ""),
                              ref, h.get("VOUCHERTYPENAME", ""), h.get("VOUCHERNUMBER", ""), h.get("REFERENCE", ""),
                              ref, h.get("NARRATION", ""), h.get("ISCANCELLED", ""), h.get("ISOPTIONAL", ""),
                              h.get("ISPOSTDATED", ""), "No"]]
    rows += [[ref, ref, ln["fields"].get("LEDGERNAME", ""), ln["fields"].get("AMOUNT", ""),
              ln["fields"].get("ISDEEMEDPOSITIVE", "")] for ln in lines]
    rows += [[ref, ref, inv["fields"].get("ACTUALQTY", ""), inv["fields"].get("RATE", ""),
              inv["fields"].get("AMOUNT", "")] for inv in voucher["inventory"]]
    rows += [[ref, ref, bill.get("NAME", ""), bill.get("BILLTYPE", ""), bill.get("AMOUNT", ""),
              bill.get("BILLCREDITPERIOD", "")] for ln in lines for bill in ln["bills"]]
    return sum(_compact(row) for row in rows) + PG_ROW_OVERHEAD_BYTES * len(rows), len(rows)


def _mean(total: int, count: int) -> int:
    return int((Decimal(total) / count).quantize(Decimal("1"), ROUND_HALF_UP)) if count else 0


def measure(blocks: dict[int, str], labels: dict[int, str]) -> dict[str, dict[str, Any]]:
    """Per kind label: totals and per-voucher means. Tags without a label (flagged, untagged, outside the window)
    are left out."""
    stats: dict[str, dict[str, Any]] = {}
    for tag, block in sorted(blocks.items()):
        label = labels.get(tag)
        if label is None:
            continue
        cols, rows = column_bytes(parse_vouchers(block)[0])
        s = stats.setdefault(label, {"count": 0, "xml_bytes": 0, "json_bytes": 0, "column_bytes": 0, "rows": 0})
        s["count"] += 1
        s["xml_bytes"] += xml_bytes(block)
        s["json_bytes"] += raw_json_bytes(block)
        s["column_bytes"] += cols
        s["rows"] += rows
    for s in stats.values():
        s["xml_per_voucher"] = _mean(s["xml_bytes"], s["count"])
        s["json_per_voucher"] = _mean(s["json_bytes"], s["count"])
        s["column_per_voucher"] = _mean(s["column_bytes"], s["count"])
        s["rows_per_voucher"] = str((Decimal(s["rows"]) / s["count"]).quantize(Decimal("0.01"), ROUND_HALF_UP))
    return stats


def mean_block_bytes(blocks: list[str], list_tag: str) -> int:
    """Mean bytes of one non-empty `list_tag` block. It lets S1 scale to invoices with more lines than company B's
    (every company-B invoice carries exactly one stock line)."""
    pattern = re.compile(rf"<{re.escape(list_tag)}>(.*?)</{re.escape(list_tag)}>", re.S)
    found = [m.group(0) for block in blocks for m in pattern.finditer(block) if m.group(1).strip()]
    return _mean(sum(xml_bytes(f) for f in found), len(found))


def _mb(n: Decimal) -> str:
    return str((n / _MB).quantize(Decimal("0.1"), ROUND_HALF_UP))


def storage_table(stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Q22/Q23 inputs from the measured mix. Weights = the measured kinds' counts (company B's FY 2022-23)."""
    n = sum(s["count"] for s in stats.values())
    per = {key: Decimal(sum(s[total] for s in stats.values())) / n
           for key, total in (("xml", "xml_bytes"), ("raw", "json_bytes"), ("columns", "column_bytes"))}
    with_raw = per["columns"] + per["raw"]
    table = []
    for volume in VOLUMES:
        for years in YEARS:
            vouchers = Decimal(volume * years)
            recent = Decimal(volume * min(years, RECENT_FYS_WITH_RAW))
            table.append({"vouchers_per_year": volume, "years": years, "vouchers": volume * years,
                          "with_raw_mb": _mb(vouchers * with_raw), "without_raw_mb": _mb(vouchers * per["columns"]),
                          "raw_recent_2_fy_only_mb": _mb(vouchers * per["columns"] + recent * per["raw"])})
    chunks = []
    for volume in VOLUMES:
        per_month = int((Decimal(volume) / 12).quantize(Decimal("1"), ROUND_CEILING))
        chunks.append({"vouchers_per_year": volume, "vouchers_per_month": per_month,
                       "month_xml_mb": _mb(per_month * per["xml"]), "over_chunk_cap": per_month > CHUNK_CAP_VOUCHERS})
    return {
        "per_voucher_bytes": {k: str(v.quantize(Decimal("0.1"), ROUND_HALF_UP)) for k, v in per.items()},
        "mix_vouchers": n,
        "table": table,
        "q22": {"raw_share_pct": str((per["raw"] / with_raw * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)),
                "per_fy_mb": {str(v): {"with_raw": _mb(v * with_raw), "without_raw": _mb(v * per["columns"])}
                              for v in VOLUMES},
                "saving_if_raw_dropped_beyond_2_fy_mb": {
                    str(v): {str(y): _mb(Decimal(v * (y - RECENT_FYS_WITH_RAW)) * per["raw"])
                             for y in YEARS if y > RECENT_FYS_WITH_RAW} for v in VOLUMES}},
        "q23": {"per_extra_fy_with_raw_mb": {str(v): _mb(v * with_raw) for v in VOLUMES},
                "per_extra_fy_without_raw_mb": {str(v): _mb(v * per["columns"]) for v in VOLUMES},
                "month_chunks": chunks,
                "caveats": ["indexes excluded",
                            "JSONB binary overhead and TOAST compression (values over ~2 kB) are not modelled, "
                            "so `raw` is an estimate in either direction",
                            "child-row voucher_id and referenced GUIDs are sized as GUID strings — an upper "
                            "bound if S1 uses bigint FKs",
                            "company B's mix: one stock line per invoice — scale with block_bytes"]},
    }


FY2022_MONTHS = [(2022, m) for m in range(4, 13)] + [(2023, m) for m in range(1, 4)]
FY2022 = (date(2022, 4, 1), date(2023, 3, 31))
CURRENT_FY_SAMPLE = (2026, 3)             # one month of the current FY: "do closed years behave differently?"
LOCK_FROM, LOCK_TO = "01-04-2022", "31-03-2023"
LOCK_PROMPT = ("Probe 21 step 4, period lock: if this TallyPrime can lock a period for company B, lock "
               f"{LOCK_FROM} to {LOCK_TO} now and type 'locked'. Type 'none' if it has no period lock, or just press "
               "Enter to skip.")
LOCK_NOTE = "Company B: unlock FY 2022-23 again (probe 21 locked it)."
REACH_IMPACT = ("Decision 7b: probe 5's month request can't fetch FY 2022-23 exactly, so the backfill can't walk back "
                "to books_from with it; the extractor needs another route before S1 (R27, R29).")
BOOKS_FROM_IMPACT = ("The Company collection's BooksFrom isn't the books-beginning date, so the backfill's floor "
                     "(decision 7b, Part 1 §4 'Where history starts') needs another source before S2.")
LOCK_IMPACT = ("A locked period doesn't read back the same: the backfill must treat locked years as unreadable and "
               "say so on the History card (decision 7b, R29).")
TIMING_TAIL = f"Timings recorded ({TIMING_NOTE}); the tier-C timing half stays ⏭ (Q29)."


def _dmy(day: date) -> str:
    return day.strftime("%d-%m-%Y")


def _blocks_by_tag(raw_text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for block in voucher_blocks(raw_text):
        tag = tag_of(parse_vouchers(block)[0]["header"].get("NARRATION", ""))
        if tag is not None:
            out[tag] = block
    return out


def _labels(licence: str, start: date, end: date) -> dict[int, str]:
    window = expect_window(licence, start, end)
    return {tag: kind_label(v) for tag, v in window.written.items() if tag not in window.flagged}


async def _month(ctx: ProbeContext, template: str, licence: str, step: str, start: date,
                 end: date) -> tuple[dict[str, Any], dict[int, str]]:
    # M2/P8b: goes through company_b_view.fetch_window, the SAME decode path probe 5 uses — not a second copy that
    # can drift from probe 5's own compare (the raw text it hands back is also what `_blocks_by_tag` measures).
    result, raw_text = await fetch_window(ctx, step, template, licence, _dmy(start), _dmy(end))
    result["bytes"] = ctx.last_response.response_bytes
    return result, _blocks_by_tag(raw_text)


async def _period_lock(ctx: ProbeContext, template: str, licence: str, unlocked: dict[str, Any]) -> dict[str, Any]:
    if not ctx.io.interactive:
        return {"status": "not attempted (non-interactive)"}
    if ctx.run_mode == "auto":
        return {"status": "not attempted (auto mode: a person must lock the period in the Tally UI)"}
    answer = ctx.ask(LOCK_PROMPT).strip().lower()
    if answer == "none":
        return {"status": "no period lock in this edition"}
    if answer != "locked":
        return {"status": "not attempted", "answer": answer}
    ctx.on_abort(LOCK_NOTE)
    read, _ = await _month(ctx, template, licence, "period_locked_read", *month_window(2022, 4, licence))
    ctx.pause(f"Unlock {LOCK_FROM} to {LOCK_TO} for company B again, then press Enter.")
    ctx.resolve_abort(LOCK_NOTE)
    return {"status": "locked", "read": read,
            "same_as_unlocked": read["match"] and read["returned"] == unlocked["returned"]}


def _headline(storage: dict[str, Any]) -> str:
    row = next(r for r in storage["table"] if (r["vouchers_per_year"], r["years"]) == (200_000, 10))
    per = storage["per_voucher_bytes"]
    return (f"Q22/Q23 inputs (company B's mix, sizes under Wine): {per['columns']} B/voucher without raw, raw JSON "
            f"{per['raw']} B more ({storage['q22']['raw_share_pct']}% of the total); 200k vouchers/yr × 10 yr = "
            f"{row['with_raw_mb']} MB with raw, {row['without_raw_mb']} MB without, {row['raw_recent_2_fy_only_mb']} MB "
            "keeping raw for the recent 2 FYs only (indexes excluded); upper bounds — JSONB overhead/TOAST "
            "compression and bigint-vs-GUID FK sizing are unmodelled (see caveats). S1 decides Q22/Q23 on "
            "these.")


async def run_b(ctx: ProbeContext) -> PartResult:
    licence = loaded_licence(ctx.store.environment)
    confirmed = ctx.store.confirmed("voucher_month")
    if confirmed is None:
        raise ProbeBlocked("No confirmed month request: run probe 5 first.")
    template = confirmed["xml_template"]

    exported = (await ctx.counters("books_from")).get("BooksFrom", "")
    books_from_ok = tally_date(exported) == B_BOOKS_FROM_DATE
    ctx.observe("books_from", {"exported": exported, "expected": B_BOOKS_FROM, "match": books_from_ok})

    months: dict[str, dict[str, Any]] = {}
    timings: dict[str, Any] = {"note": TIMING_NOTE}
    blocks: dict[int, str] = {}
    for year, month in FY2022_MONTHS:
        step = f"fy2022_month_{month:02d}"
        months[step], found = await _month(ctx, template, licence, step, *month_window(year, month, licence))
        timings[step] = ctx.last_response.elapsed_ms
        blocks.update(found)
    ctx.observe("months", months)

    # I1: a month that bounded and returned every expected tag, but also holds an untagged/extra/duplicate row, is
    # books drift (company B no longer matches its generated dataset) — never a reach failure of the request under
    # test. Check every month before anything else, and block on drift rather than reporting FAILED/REACH_IMPACT.
    drifted_steps = [step for step, result in months.items() if result["drifted"]]
    if drifted_steps:
        raise ProbeBlocked("; ".join(drift_message(step, months[step]) for step in drifted_steps))

    labels = _labels(licence, *FY2022)
    stats = measure(blocks, labels)
    sized = [block for tag, block in blocks.items() if tag in labels]
    ctx.observe("kinds", stats)
    ctx.observe("block_bytes", {tag: mean_block_bytes(sized, tag) for tag in BLOCK_TAGS})
    storage = storage_table(stats) if stats else None
    ctx.observe("storage", storage)

    sample_window = month_window(*CURRENT_FY_SAMPLE, licence)
    sample, sample_blocks = await _month(ctx, template, licence, "fy2025_month_03", *sample_window)
    timings["fy2025_month_03"] = ctx.last_response.elapsed_ms
    ctx.observe("current_fy_sample", {
        "month": sample,
        "kinds": {label: {"xml_per_voucher": s["xml_per_voucher"], "json_per_voucher": s["json_per_voucher"]}
                  for label, s in measure(sample_blocks, _labels(licence, *sample_window)).items()},
        "note": "Part 1 probe 21's 'do closed years behave differently?': compare with `kinds`; no verdict."})
    ctx.observe("timings_ms", timings)

    lock = await _period_lock(ctx, template, licence, months["fy2022_month_04"])
    ctx.observe("period_lock", lock)

    inexact = [step for step, result in months.items() if not result["reach_ok"]]
    if inexact:
        return PartResult(Outcome.FAILED, f"FY 2022-23 not reached exactly: {', '.join(inexact)} differ from the "
                                          f"dataset (see observations.months). {TIMING_TAIL}", spec_impact=REACH_IMPACT)
    notes, impacts = [], []
    if not books_from_ok:
        notes.append(f"BooksFrom exported {exported!r}, not {B_BOOKS_FROM}")
        impacts.append(BOOKS_FROM_IMPACT)
    if lock["status"] == "locked" and not lock["same_as_unlocked"]:
        notes.append("the locked FY 2022-23 no longer reads back the same")
        impacts.append(LOCK_IMPACT)
    if notes:
        return PartResult(Outcome.DIFFERENT, "; ".join(notes) + f". {TIMING_TAIL}", spec_impact=" ".join(impacts))
    total = sum(result["returned"] for result in months.values())
    return PartResult(Outcome.CONFIRMED, f"FY 2022-23 reached month by month: 12/12 months exact ({total} vouchers; "
                                         f"the cancelled pair reported, not judged); BooksFrom {B_BOOKS_FROM}; sizes "
                                         f"for {len(stats)} voucher kinds. {TIMING_TAIL}",
                      spec_impact=_headline(storage))


PROBE = Probe(
    id=21,
    name="full_history_reach",
    question="Can every month of an FY three years back be fetched exactly, and what do vouchers cost to store "
             "over 2/5/10 years with and without raw?",
    feeds=("decision 7b", "Q22", "Q23", "R27"),
    parts={"B": run_b},
    requires=(0, 1, 5),
)
