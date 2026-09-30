"""Probe 3 — voucher IDs and flags can be fetched (S0 spec §7 "Probe 3", A part). Feeds R6, R16.

The B part (plan part 6) judges the cancelled / optional flags on company B. What B still measures (S0-D7): probe
21's live run already saw the cancelled pair 201/202 come back from probe 5's confirmed month request (recorded, not
judged), so B does not re-ask "are cancelled vouchers returned?". It judges the flags on the extractor's own request
for the two months that hold flagged vouchers (Feb 2023: 201/202; Jul 2023: 301/302 — no probe has read July 2023),
and books-wide on this probe's header collection for everything else.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

from agent.tally.xml_utils import read_objects
from probes.company_b_view import (B_BOOKS_FROM, B_BOOKS_TO, fetch_window, flagged_tags, loaded_licence,
                                      month_window, tag_of, written_vouchers)
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.reads import parse_vouchers, voucher_request

FIELDS = ["GUID", "MasterID", "AlterID", "Date", "VoucherTypeName", "VoucherNumber", "Reference", "PartyLedgerName",
          "Narration", "IsCancelled", "IsOptional", "IsPostDated"]
FLAG_CANDIDATES = ("IsCancelled", "IsOptional", "IsPostDated")
EXPECTED_VOUCHERS = 50                                   # the seed company (docs/seed-data-setup.md)
INVOICE_IN_NARRATION = re.compile(r"Invoice #(?P<ref>[SP]\d{3})")


async def run_a(ctx: ProbeContext) -> PartResult:
    rows = read_objects(await ctx.send("vouchers_ids_flags", voucher_request("S0P03Vouchers", FIELDS, ctx.company_name)),
                        "VOUCHER", FIELDS)
    ids = {}
    for key in ("GUID", "MasterID", "AlterID"):
        values = [row[key] for row in rows]
        ids[key] = {"empty": sum(1 for value in values if not value), "duplicates": len(values) - len(set(values))}
    flags = {flag: {"empty": sum(1 for row in rows if not row[flag]), "yes": sum(1 for row in rows if row[flag] == "Yes")}
             for flag in FLAG_CANDIDATES}
    wrong_references = []
    for row in rows:
        if row["VoucherTypeName"] not in ("Sales", "Purchase"):
            continue
        match = INVOICE_IN_NARRATION.search(row["Narration"])
        expected = match.group("ref") if match else ""
        if row["Reference"] != expected:
            wrong_references.append({"narration": row["Narration"], "reference": row["Reference"], "expected": expected})
    ctx.observe("count", len(rows))
    ctx.observe("ids", ids)
    ctx.observe("flags", flags)
    ctx.observe("wrong_references", wrong_references)

    missing_flags = [flag for flag, entry in flags.items() if entry["empty"]]
    bad_ids = [key for key in ("GUID", "MasterID") if ids[key]["empty"] or ids[key]["duplicates"]]
    if ids["AlterID"]["empty"]:
        bad_ids.append("AlterID")
    if missing_flags or bad_ids or not rows:
        problems = ([f"flag(s) {', '.join(missing_flags)} don't export on every voucher"] if missing_flags else []) + \
                   ([f"{', '.join(bad_ids)} empty or duplicated"] if bad_ids else []) + \
                   (["no vouchers came back"] if not rows else [])
        impacts = (["R16 filtering (cancelled / optional / post-dated) is redesigned before S1."] if missing_flags
                   else []) + (["R6: vouchers can't be keyed and change-tracked by GUID / MasterID / AlterID as "
                                "designed; S1's voucher key is revisited."] if bad_ids or not rows else [])
        return PartResult(Outcome.FAILED, "; ".join(problems), spec_impact=" ".join(impacts))
    differences, impacts = [], []
    if len(rows) != EXPECTED_VOUCHERS:
        differences.append(f"{len(rows)} vouchers came back, not {EXPECTED_VOUCHERS}")
        impacts.append("The voucher collection doesn't return exactly the company's vouchers: the extractor's "
                       "completeness check (count vs CMPINFO / AltVchId) is revisited.")
    if wrong_references:
        differences.append(f"Reference ≠ the invoice number on {len(wrong_references)} Sales/Purchase voucher(s)")
        impacts.append("S1 can't take the invoice number from Reference alone; the recorded cases decide the source "
                       "(LESSONS §9).")
    if differences:
        return PartResult(Outcome.DIFFERENT, "; ".join(differences), spec_impact=" ".join(impacts))
    return PartResult(Outcome.CONFIRMED, f"{len(rows)} vouchers: GUID / MasterID unique, AlterID present, the three flags "
                                         "export everywhere, Reference = invoice number")


FLAGGED_MONTHS = ((2023, 2), (2023, 7))       # the dataset's cancelled 201/202 and optional 301/302
HEADER_TIMEOUT = 60.0                          # ~958 headers at ~1.6 KB (probe 3 A: 78,911 bytes for 50)
B_CONFIRMED_IMPACT = ("R16: the extractor's month request returns cancelled and optional vouchers WITH their flags "
                      "(a cancelled one exports no ledger lines); S1 ingests them and keeps both out of balances by "
                      "IsCancelled / IsOptional, as Tally does (C42). No other voucher carries a flag.")
B_WRONG_FLAG_IMPACT = ("R16 filtering is redesigned before S1: a flagged voucher comes back without its flag, so "
                       "ingest can't tell it from a posting voucher.")
B_CANCELLED_LINES_IMPACT = ("R16: a cancelled voucher comes back from the extractor's month request WITH ledger lines "
                            "({tags}), unlike probe 21's live capture; S1 must drop a cancelled voucher's postings by "
                            "IsCancelled, never rely on an empty line list.")
B_NOT_LISTED_IMPACT = ("The extractor's request never returns {kinds} vouchers: S1 never ingests them, and when a person "
                       "turns one into a regular voucher it arrives as a new voucher (AltVchId moves). R16's filter "
                       "only has to handle what is listed.")
B_HEADER_ONLY_IMPACT = ("R16: probe 5's month request (the extractor's) returns {kinds} vouchers WITH their flags, so S1 "
                        "ingests them and filters by IsCancelled / IsOptional; only 3 B's own plain Voucher header "
                        "collection leaves them out. No S1 read may rely on a plain Voucher collection to list them "
                        "(e.g. a count check against the month request).")


def _wanted(tag: int, cancelled: frozenset[int]) -> tuple[str, str]:
    return ("Yes", "No") if tag in cancelled else ("No", "Yes")        # (IsCancelled, IsOptional)


def month_flag_rows(raw: str) -> dict[int, dict[str, Any]]:
    """tag → flags and line counts from a full voucher export (probe 5's month request)."""
    out: dict[int, dict[str, Any]] = {}
    for v in parse_vouchers(raw):
        tag = tag_of(v["header"].get("NARRATION", ""))
        if tag is not None:
            out[tag] = {"IsCancelled": v["header"].get("ISCANCELLED", ""),
                        "IsOptional": v["header"].get("ISOPTIONAL", ""),
                        "ledger_lines": len(v["ledger_lines"]),
                        "amounts": sum(1 for line in v["ledger_lines"] if line["amount"] is not None),
                        "party": v["header"].get("PARTYLEDGERNAME", "")}
    return out


def flag_readings(rows: dict[int, dict], tags: Iterable[int], cancelled: frozenset[int]) -> dict[str, dict]:
    """str(tag) → returned? its two flags, and whether they are the ones the dataset set."""
    out: dict[str, dict] = {}
    for tag in sorted(tags):
        row = rows.get(tag)
        if row is None:
            out[str(tag)] = {"returned": False}
            continue
        got = (row.get("IsCancelled", ""), row.get("IsOptional", ""))
        out[str(tag)] = {"returned": True, "IsCancelled": got[0], "IsOptional": got[1],
                         "flags_ok": got == _wanted(tag, cancelled)}
    return out


async def run_b(ctx: ProbeContext) -> PartResult:
    licence = loaded_licence(ctx.store.environment)
    confirmed = ctx.store.confirmed("voucher_month")
    if confirmed is None:
        raise ProbeBlocked("No confirmed month request: run probe 5 first — 3 B judges the flags on the extractor's "
                           "own request (S0-D7).")
    written = written_vouchers(licence)
    cancelled, optional = flagged_tags(licence)
    flagged = cancelled | optional
    rows = read_objects(await ctx.send("vouchers_flags", voucher_request(
        "S0P03BVouchers", FIELDS, ctx.company_name, from_date=B_BOOKS_FROM, to_date=B_BOOKS_TO),
        timeout=HEADER_TIMEOUT), "VOUCHER", FIELDS)
    tags = [tag_of(row["Narration"]) for row in rows]
    counts = Counter(tag for tag in tags if tag is not None)
    untagged, extra = tags.count(None), sorted(set(counts) - set(written))
    duplicates = sorted(tag for tag, n in counts.items() if n > 1)
    if untagged or extra or duplicates:
        raise ProbeBlocked(f"Company B differs from the dataset in vouchers_flags: untagged {untagged}, extra "
                           f"{extra[:5]}, duplicates {duplicates[:5]} — re-run `setup-b` verify or restore the backup.")
    by_tag = {tag: row for tag, row in zip(tags, rows)}
    missing = sorted(set(written) - flagged - set(by_tag))
    if missing:
        raise ProbeBlocked(f"The books-wide read ({B_BOOKS_FROM}..{B_BOOKS_TO}) didn't return {len(missing)} written "
                           f"voucher(s) {missing[:5]} — company B drifted or the request didn't reach them; re-run "
                           "`setup-b` verify.")
    false_flags = sorted(tag for tag, row in by_tag.items() if tag not in flagged
                         and "Yes" in (row["IsCancelled"], row["IsOptional"], row["IsPostDated"]))
    if false_flags:
        raise ProbeBlocked(f"Voucher(s) {false_flags[:5]} carry a flag the dataset doesn't set — company B drifted "
                           "(a UI edit?); re-run `setup-b` verify or restore the backup.")
    empty = [flag for flag in FLAG_CANDIDATES if any(not row[flag] for row in rows)]
    header = flag_readings(by_tag, flagged, cancelled)

    months: dict[str, dict] = {}
    for year, month in FLAGGED_MONTHS:
        start, end = month_window(year, month, licence)
        step = f"flagged_month_{year}_{month:02d}"
        result, raw = await fetch_window(ctx, step, confirmed["xml_template"], licence,
                                         start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y"))
        if not result["reach_ok"] or result["drifted"]:
            raise ProbeBlocked(f"{step}: probe 5's confirmed request didn't return that window exactly (missing "
                               f"{result['missing'][:5]}, extra {result['extra'][:5]}, untagged {result['untagged']}) — "
                               "re-run `setup-b` verify / probe 5.")
        found = month_flag_rows(raw)
        in_month = {tag for tag in flagged if start <= written[tag].date <= end}
        months[f"{year}-{month:02d}"] = {
            "returned": result["returned"], "flags": flag_readings(found, in_month, cancelled),
            "lines": {str(tag): {k: found[tag][k] for k in ("ledger_lines", "amounts", "party")}
                      for tag in in_month if tag in found}}
    month_flags = {tag: reading for m in months.values() for tag, reading in m["flags"].items()}
    month_lines = {tag: lines for m in months.values() for tag, lines in m["lines"].items()}
    # Ruling S2: the CONFIRMED impact says "a cancelled one exports no ledger lines" -- measure it, and record the
    # party name (live p21: empty PARTYLEDGERNAME on a cancelled voucher; S1 ingest needs to know).
    cancelled_lines = {str(tag): month_lines[str(tag)] for tag in sorted(cancelled) if str(tag) in month_lines}
    with_lines = [k for k, lines in cancelled_lines.items() if lines["ledger_lines"]]

    ctx.observe("count", len(rows))
    ctx.observe("flags", {flag: {"empty": sum(1 for r in rows if not r[flag]), "yes": sum(1 for r in rows if r[flag] == "Yes")}
                          for flag in FLAG_CANDIDATES})
    ctx.observe("header_flagged", header)
    ctx.observe("months", months)
    ctx.observe("others", len(by_tag) - len(flagged & set(by_tag)))
    ctx.observe("cancelled_party_names", {k: lines["party"] for k, lines in cancelled_lines.items()})
    ctx.observe("prior_evidence", "probe 21 (2026-09-24): 201/202 returned by probe 5's month request, "
                                  "p21_B_fy2022_month_02.xml — recorded there, judged here")

    if empty or not rows:
        return PartResult(Outcome.FAILED, f"flag(s) {', '.join(empty) or 'all'} don't export on every voucher",
                          spec_impact=B_WRONG_FLAG_IMPACT)
    keys = sorted(str(tag) for tag in flagged)
    wrong = [k for k in keys if (header[k]["returned"] and not header[k]["flags_ok"])
             or (month_flags.get(k, {}).get("returned") and not month_flags[k]["flags_ok"])]
    if wrong:
        return PartResult(Outcome.FAILED, f"flagged voucher(s) {', '.join(wrong)} came back without their flag",
                          spec_impact=B_WRONG_FLAG_IMPACT)
    # Review I3: two different reads. Only the month request (the extractor's) decides B_NOT_LISTED_IMPACT; an
    # omission by 3 B's own header collection alone is its own finding.
    unlisted_month = [k for k in keys if not month_flags.get(k, {}).get("returned")]
    unlisted_header = [k for k in keys if not header[k]["returned"]]
    header_only = [k for k in unlisted_header if k not in unlisted_month]
    ctx.observe("unlisted_month", unlisted_month)
    ctx.observe("unlisted_header", unlisted_header)
    kinds_of = lambda ks: " and ".join(sorted({"cancelled" if int(k) in cancelled else "optional" for k in ks}))
    texts: list[str] = []
    impacts: list[str] = []
    if unlisted_month:
        text = (f"{kinds_of(unlisted_month)} voucher(s) {', '.join(unlisted_month)} are not returned by probe 5's "
                "month request (the extractor's)")
        both = [k for k in unlisted_month if k in unlisted_header]
        if both:
            text += f", nor by 3 B's header read ({', '.join(both)})"
        texts.append(text)
        impacts.append(B_NOT_LISTED_IMPACT.format(kinds=kinds_of(unlisted_month)))
    if header_only:
        texts.append(f"{kinds_of(header_only)} voucher(s) {', '.join(header_only)} are missing from 3 B's own header "
                     "read only — probe 5's month request returns them with their flags")
        impacts.append(B_HEADER_ONLY_IMPACT.format(kinds=kinds_of(header_only)))
    if with_lines:
        texts.append(f"cancelled voucher(s) {', '.join(with_lines)} carry their flags but export ledger lines on "
                     "probe 5's month request")
        impacts.append(B_CANCELLED_LINES_IMPACT.format(tags=", ".join(with_lines)))
    if texts:
        return PartResult(Outcome.DIFFERENT, "; ".join(texts), spec_impact=" ".join(impacts))
    parties = sorted({lines["party"] for lines in cancelled_lines.values()})
    party_text = "an empty PartyLedgerName" if parties == [""] else f"PartyLedgerName {parties}"
    return PartResult(Outcome.CONFIRMED, f"cancelled {sorted(cancelled)} and optional {sorted(optional)} carry their "
                                         f"flags on both reads (the cancelled ones with no ledger lines and "
                                         f"{party_text}); the other {len(by_tag) - len(flagged)} vouchers carry none",
                      spec_impact=B_CONFIRMED_IMPACT)


PROBE = Probe(
    id=3,
    name="voucher_ids_flags",
    question="Can voucher GUID / MasterID / AlterID / Reference and the cancelled / optional / post-dated flags be fetched?",
    feeds=("R6", "R16"),
    parts={"A": run_a, "B": run_b},
    planned_parts=("A", "B"),
    requires=(0,),
)
