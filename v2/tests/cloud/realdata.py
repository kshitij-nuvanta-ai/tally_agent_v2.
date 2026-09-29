"""Real-capture fixture assembler (plan ambiguity A4; pre-flight F8, F20, F23). Task 8c creates it with just the
master sets the ingest tests need (`b_masters()`, `a_masters()`) plus a few voucher helpers; Task 12 extends it into
the full `Assembled` / `assemble_a()` / `assemble_b_fy2022()` assembler.

Rules (A4, "test reality"):
- Every wire object is built from an S0 / Task 0 capture through the transcoder (`v2.contract.transcode`), never
  written by hand.
- Captures are joined by cleaned `name` per kind. The assembler only **copies keys**: a key no capture holds stays
  absent. The single exception is identity: a master with no `guid` / `alterid` in any capture gets the
  deterministic synthetic identity `<company guid>-fx-<kind>-<sha8(name)>` with alterid `"1"` (gap G4), and
  `synthetic_names()` reports which ones did.
- `captured_at` on a balance-carrying master is the capture's own time, the sidecar `.json`'s `sent_at` (it is not
  in Tally's answer; F23).
- A missing capture raises `FileNotFoundError` -- a real-data test fails, it never skips (F1).
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from v2.contract import transcode
from v2.contract.parse import name as parse_name
from v2.contract.parse import tally_date

SYNC = Path(__file__).resolve().parents[1] / "fixtures" / "sync"
COMPANY_A_GUID = "710de34a-3661-4a7b-8148-c2206c3b3e17"
COMPANY_B_GUID = "138b7373-753c-4dbe-aa63-b802035f0ba9"
KIND_ORDER = ("currency", "group", "voucher_type", "unit", "stock_group", "ledger", "stock_item")


def read_capture(file_name: str) -> str:
    path = SYNC / file_name
    if not path.exists():
        raise FileNotFoundError(f"real-data capture missing: {path}")   # F1: fail, never skip
    return path.read_text(encoding="utf-8")


def sent_at(file_name: str) -> str:
    """The capture's own time: the sidecar `<file>.json`'s `sent_at` (F23)."""
    sidecar = SYNC / f"{file_name}.json"
    if not sidecar.exists():
        raise FileNotFoundError(f"capture sidecar missing: {sidecar}")
    return json.loads(sidecar.read_text(encoding="utf-8"))["sent_at"]


def masters(file_name: str, kind: str) -> list[dict]:
    return transcode.masters_from_xml(read_capture(file_name), kind)


def vouchers(file_name: str) -> list[dict]:
    return transcode.vouchers_from_xml(read_capture(file_name))


def synthetic_identity(company_guid: str, kind: str, master_name: str) -> tuple[str, str]:
    digest = hashlib.sha256(parse_name(master_name).encode("utf-8")).hexdigest()[:8]
    return f"{company_guid}-fx-{kind}-{digest}", "1"


def synthetic_names(objs: list[dict]) -> set[tuple[str, str]]:
    """(kind, name) of every object that carries the A4 synthetic identity (gap G4)."""
    return {(o["kind"], o["data"]["name"]) for o in objs if "-fx-" in o["data"].get("guid", "")}


class _Track:
    """What the Task 12 assembler reports (A4, Review Focus 1): ``sources`` -- each wire key -> the capture file(s)
    that supplied it (voucher keys bare, master keys as ``<kind>.<key>``, e.g. ``ledger.closingbalance``); ``synthetic`` -- ``(kind, name)`` -> the gap id (``"G4"``) of every master whose GUID and/or
    AlterID no capture holds (ruling F13: keyed by kind too -- group and ledger ``Capital Account`` collide)."""

    def __init__(self) -> None:
        self.sources: dict[str, list[str]] = {}
        self.synthetic: dict[tuple[str, str], str] = {}

    def source(self, key: str, file_name: str) -> None:
        files = self.sources.setdefault(key, [])
        if file_name not in files:
            files.append(file_name)


def _join(company_guid: str, kind: str, base: list[str], *extra: str, captured_at: str | None = None,
          track: _Track | None = None) -> list[dict]:
    """``base`` capture files define the set of masters (the first file holding a name wins on every key it holds);
    each ``extra`` capture only fills keys the base lacks, matched by cleaned name. No key is invented except the A4
    identity (``guid`` / ``alterid``), which is reported to ``track`` as gap G4."""
    by_name = [(f, {parse_name(o["data"]["name"]): o["data"] for o in masters(f, kind)}) for f in extra]
    out: list[dict] = []
    seen: set[str] = set()
    for base_file in base:
        for obj in masters(base_file, kind):
            data = dict(obj["data"])
            key = parse_name(data["name"])
            if key in seen:                               # the same master in two base captures: first one wins
                continue
            seen.add(key)
            origin = {k: base_file for k in data}
            for extra_file, index in by_name:
                for k, v in index.get(key, {}).items():
                    if k not in data:
                        data[k] = v
                        origin[k] = extra_file
            if "guid" not in data or "alterid" not in data:
                guid, alter = synthetic_identity(company_guid, kind, data["name"])
                data.setdefault("guid", guid)
                data.setdefault("alterid", alter)
                if track is not None:
                    track.synthetic[(kind, data["name"])] = "G4"
            if captured_at is not None and any(k in data for k in ("closingbalance", "closingvalue")):
                data.setdefault("captured_at", captured_at)
                origin.setdefault("captured_at", base_file)       # the sidecar of the balance capture (F23)
            if track is not None:
                for k, f in origin.items():
                    track.source(f"{kind}.{k}", f)
            out.append({"kind": kind, "data": data})
    return out


def b_masters(track: _Track | None = None) -> list[dict]:
    """Company B (`Sharma & Sons' Probe Traders`) masters, §12 step 6 kind order (F8: incl. stock items + units)."""
    g, t = COMPANY_B_GUID, track
    return [
        *_join(g, "currency", ["p22_B_currencies.xml"], "s1_B_currencies.xml", track=t),
        *_join(g, "group", ["p18_B_group_list.xml"], track=t),                                   # G4: no ids
        *_join(g, "voucher_type", ["p25_B_voucher_types.xml"], "s1_B_voucher_types.xml", track=t),
        *_join(g, "unit", ["s1_B_units.xml"], track=t),
        *_join(g, "ledger", ["p16_B_ledgers.xml"], "s1_B_usd_ledger.xml", "s1_B_ledgers_touched.xml",
               captured_at=sent_at("p16_B_ledgers.xml"), track=t),
        *_join(g, "stock_item", ["p11_B_stock_openings.xml", "p15_B_compound_unit_item.xml"], track=t),  # G4: no ids
    ]


def a_masters(track: _Track | None = None) -> list[dict]:
    """Company A (`Bharat Traders…` S0 company) masters, §12 step 6 kind order (F8: incl. stock items + units)."""
    g, t = COMPANY_A_GUID, track
    return [
        *_join(g, "currency", ["s1_A_currencies.xml"], track=t),
        *_join(g, "group", ["p25_A_groups.xml"], "p04_A_group_full.xml", track=t),
        *_join(g, "voucher_type", ["p25_A_voucher_types.xml"], "s1_A_voucher_types.xml", track=t),
        *_join(g, "unit", ["s1_A_units.xml"], track=t),
        *_join(g, "stock_group", ["s1_A_stock_groups.xml"], track=t),
        *_join(g, "ledger", ["p16_A_ledgers.xml"], "p04_A_ledger_full.xml", "p23_A_gst_ledgers.xml",
               captured_at=sent_at("p16_A_ledgers.xml"), track=t),
        *_join(g, "stock_item", ["p18_A_stock_item_openings.xml"], "p04_A_stockitem_full.xml", track=t),
    ]


def ledger_balance_delta(before_file: str, after_file: str) -> dict[str, tuple[str, str]]:
    """Ledgers whose closing balance differs between two ledger captures: name -> (before, after) text."""
    before = {o["data"]["name"]: o["data"].get("closingbalance") for o in masters(before_file, "ledger")}
    after = {o["data"]["name"]: o["data"].get("closingbalance") for o in masters(after_file, "ledger")}
    return {n: (before[n], after[n]) for n in before if before[n] != after.get(n)}


def _probe16_voucher(voucher_file: str, ledgers_file: str, narration: str) -> dict:
    delta = ledger_balance_delta("p16_A_ledgers.xml", ledgers_file)
    assert delta["Cash"] == ("23000.00", "23001.00") and delta["Electricity"] == ("-12000.00", "-12001.00"), delta
    obj = next(v for v in vouchers(voucher_file) if v["data"].get("narration") == narration)
    data = dict(obj["data"])
    data.setdefault("alterid", "1")
    data.setdefault("iscancelled", "No")
    data.setdefault("isoptional", "No")
    data["ledger_entries"] = [
        {"ledgername": "Electricity", "isdeemedpositive": "Yes", "amount": "-1.00"},
        {"ledgername": "Cash", "isdeemedpositive": "No", "amount": "1.00"},
    ]
    return {"kind": "voucher", "data": data}


def a_post_dated_voucher() -> dict:
    """Probe 16's post-dated throwaway voucher (`p16_A_post_dated_voucher.xml`, `S0-throwaway 16 post-dated`).

    That capture holds the voucher's identity, date, type and `ISPOSTDATED = Yes`, but its request fetched no
    ledger entries, no AlterID and no cancelled/optional flags. The postings are Tally's own evidence, not invented:
    `p16_A_ledgers.xml` -> `p16_A_ledgers_with_post_dated.xml` (the same probe, before/after creating it) differ on
    exactly two balance-sheet/P&L ledgers, `Cash` 23000.00 -> 23001.00 and `Electricity` -12000.00 -> -12001.00
    (plus `Profit & Loss A/c`, the derived P&L roll-up). So the voucher credits Cash 1.00 and debits Electricity
    1.00, and -- because it moved the closing balances -- it is neither cancelled nor optional (either would leave
    them untouched). AlterID is the A4 synthetic `1`.
    """
    obj = _probe16_voucher("p16_A_post_dated_voucher.xml", "p16_A_ledgers_with_post_dated.xml",
                           "S0-throwaway 16 post-dated")
    assert obj["data"]["ispostdated"] == "Yes"
    return obj


def a_future_voucher() -> dict:
    """Probe 16's other throwaway (`p16_A_future_voucher.xml`, `S0-throwaway 16 future`, dated 31-03-2026 -- after
    Tally's F2 date, not flagged post-dated). Same evidence rule as ``a_post_dated_voucher``: its postings are the
    `p16_A_ledgers.xml` -> `p16_A_ledgers_with_future_voucher.xml` delta (Cash +1.00, Electricity -1.00)."""
    obj = _probe16_voucher("p16_A_future_voucher.xml", "p16_A_ledgers_with_future_voucher.xml",
                           "S0-throwaway 16 future")
    assert obj["data"]["ispostdated"] == "No"
    return obj


def g5_ledger_balances() -> list[dict]:
    """G5 (`s1_B_ledgers_touched.xml`): the mirrored ledger re-read, as `ledger_balance` objects whose `captured_at`
    is the sidecar's `sent_at` (F23)."""
    name = "s1_B_ledgers_touched.xml"
    return transcode.balances_from_xml(read_capture(name), sent_at(name))


_A_VOUCHER_ID_SOURCES = ("p03_A_vouchers_ids_flags.xml", "p04_A_voucher_full.xml")
_A_JOIN_KEYS = ("alterid", "iscancelled", "isoptional", "ispostdated")


def a_vouchers(file_name: str) -> list[dict]:
    """Company A vouchers from `file_name` joined by GUID with the keys only other A captures hold (A4): AlterID and
    the cancelled / optional / post-dated flags from `p03_A_vouchers_ids_flags.xml` (then `p04_A_voucher_full.xml`
    for AlterID). A key the voucher's own capture already holds is never overwritten."""
    sources = [{v["data"]["guid"]: v["data"] for v in vouchers(name)} for name in _A_VOUCHER_ID_SOURCES]
    out = []
    for obj in vouchers(file_name):
        data = dict(obj["data"])
        for source in sources:
            for key in _A_JOIN_KEYS:
                if key not in data and key in source.get(data["guid"], {}):
                    data[key] = source[data["guid"]][key]
        out.append({"kind": "voucher", "data": data})
    return out


# --- Task 12: the full assembler (A4, Review Focus 1) + in-memory parity over it ------------------------------------

# Every company A capture the assembler may read (test_assembler checks each wire key's source is one of these).
CAPTURES_A = (
    "s1_A_counters.xml",
    "p06_A_vouchers_nested.xml", "p04_A_voucher_full.xml", "p16_A_vouchers_fy.xml",
    "s1_A_currencies.xml", "p25_A_groups.xml", "p04_A_group_full.xml", "p25_A_voucher_types.xml",
    "s1_A_voucher_types.xml", "s1_A_units.xml", "s1_A_stock_groups.xml", "p16_A_ledgers.xml",
    "p04_A_ledger_full.xml", "p23_A_gst_ledgers.xml", "p18_A_stock_item_openings.xml", "p04_A_stockitem_full.xml",
)
# A4 voucher join for company A: p06's lines (the base; it holds every key it has) + p04's AlterID + p16's flags.
A_VOUCHER_BASE = "p06_A_vouchers_nested.xml"
A_VOUCHER_EXTRAS = ("p04_A_voucher_full.xml", "p16_A_vouchers_fy.xml")
B_FY2022_MONTHS = tuple(f"p21_B_fy2022_month_{m:02d}.xml" for m in range(1, 13))


@dataclass
class Assembled:
    company_guid: str
    books_from: date
    masters: list[dict]
    vouchers: list[dict]
    synthetic_ids: dict[tuple[str, str], str]
    sources: dict[str, list[str]]

    def copy(self) -> "Assembled":
        return copy.deepcopy(self)


def _company(counters_file: str, track: _Track) -> tuple[str, date]:
    c = transcode.counters_from_xml(read_capture(counters_file))
    track.source("company_guid", counters_file)
    track.source("booksfrom", counters_file)
    return c["guid"], tally_date(c["booksfrom"])


def _join_vouchers(base: str, extras: tuple[str, ...], track: _Track) -> list[dict]:
    """Vouchers by GUID: ``base`` defines the set and wins on every key it holds; each extra capture only fills a
    key the voucher still lacks. Nothing is synthesised."""
    indexes = [(f, {v["data"]["guid"]: v["data"] for v in vouchers(f)}) for f in extras]
    out = []
    for obj in vouchers(base):
        data = dict(obj["data"])
        origin = {k: base for k in data}
        for extra_file, index in indexes:
            for k, v in index.get(data["guid"], {}).items():
                if k not in data:
                    data[k] = v
                    origin[k] = extra_file
        for k, f in origin.items():
            track.source(k, f)
        out.append({"kind": "voucher", "data": data})
    return out


def assemble_a() -> Assembled:
    """Company A (`Bharat Traders Probe Copy`): 50 vouchers + masters, joined per A4."""
    t = _Track()
    guid, books_from = _company("s1_A_counters.xml", t)
    assert guid == COMPANY_A_GUID, guid
    ms = a_masters(t)
    vs = _join_vouchers(A_VOUCHER_BASE, A_VOUCHER_EXTRAS, t)
    return Assembled(guid, books_from, ms, vs, dict(t.synthetic), t.sources)


def assemble_b_fy2022() -> Assembled:
    """Company B (`Sharma & Sons' Probe Traders`) FY 2022-23: probe 21's twelve month captures (the extractor's own
    month request, so every voucher key -- AlterID and the flags included -- is in the capture) + B's masters."""
    t = _Track()
    guid, books_from = _company("s1_B_counters.xml", t)
    assert guid == COMPANY_B_GUID, guid
    ms = b_masters(t)
    vs = []
    for f in B_FY2022_MONTHS:
        for obj in vouchers(f):
            for k in obj["data"]:
                t.source(k, f)
            vs.append(obj)
    return Assembled(guid, books_from, ms, vs, dict(t.synthetic), t.sources)


# --- in-memory parity (8a/8b/10a/10b pieces, no DB) -----------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """One TB snapshot as the agent would post it: the transcoded ``cells`` and the ``request_flags``."""
    cells: list[dict]
    flags: dict[str, str]
    source: str = ""

    @classmethod
    def capture(cls, file_name: str) -> "Snapshot":
        from v2.tests.cloud import parity_realdata as prd
        cells = transcode.report_cells(read_capture(file_name), "trial_balance")
        return cls(cells, prd.request_flags(file_name), file_name)

    @property
    def rows(self):
        from v2.cloud.ingest.snapshots import parse_cells
        from v2.cloud.parity.model import tb_rows
        return tb_rows(parse_cells("trial_balance", self.cells).rows)

    def without(self, row_name: str) -> "Snapshot":
        """A copy with every cell whose name is ``row_name`` removed (a seeded fault, never a real capture)."""
        from v2.cloud.ingest.snapshots import NAME_KEYS
        key = NAME_KEYS["trial_balance"]
        return Snapshot([c for c in self.cells if parse_name(c.get(key, "")) != row_name], dict(self.flags),
                        f"{self.source} minus {row_name!r}")


@dataclass(frozen=True)
class ParityResult:
    status: str                         # ok | suspect | aborted_incomplete (no balance-sheet figure verified)
    lines: list                         # classified (§10.7)
    raw_lines: list                     # before the classifier (forex causes still forex_face_mismatch / ..._unexplained)
    remediations: list
    forex_unrealised_total: Decimal
    summary: dict
    imbalance: Decimal | None
    anchor_as_on: date
    anchor_source: str
    anchors: dict                       # ledger guid -> the anchor amount rung 1 used

    def ledger(self, name: str, *, raw: bool = False):
        found = [l for l in (self.raw_lines if raw else self.lines) if l.scope == "ledger" and l.name == name]
        assert len(found) == 1, (name, found)
        return found[0]

    def group(self, name: str):
        (found,) = [l for l in self.lines if l.scope == "group" and l.name == name]
        return found

    def problems(self) -> list:
        from v2.cloud.parity.model import PROBLEM_VERDICTS
        return [l for l in self.lines if l.verdict in PROBLEM_VERDICTS]


def mirrored_from_masters(assembled: Assembled) -> dict[str, str | None]:
    """The mirrored closing text per ledger name, as the ledger capture exported it (``""`` stays ``""``)."""
    return {m["data"]["name"]: m["data"].get("closingbalance") for m in assembled.masters if m["kind"] == "ledger"}


def mirrored_from_tb(snapshot: Snapshot) -> dict[str, str | None]:
    """Ruling F3 (B as-on 31-03-2023): the rung-1 "mirrored" figure for a past as-on is the ledger-level TB row."""
    return {r.name: str(r.amount) for r in snapshot.rows}


def _stock_bearing(groups: list[dict]) -> str:
    from v2.cloud.ingest.derive import nature_walk
    from v2.contract.tally_rules import PRIMARY_PARENT
    parents = {parse_name(g["name"]): parse_name(g.get("parent")) or PRIMARY_PARENT for g in groups}
    if "Stock-in-Hand" in parents:
        derived = nature_walk("Stock-in-Hand", parents)
        if derived.primary_group:
            return derived.primary_group
    return "Current Assets"


def pure_parity(assembled: Assembled, *, snapshots: dict[str, Snapshot], mirrored: dict[str, str | None],
                as_on: date, verified_edge: date, dataset_anchor: dict[str, Decimal] | None = None,
                tol: Decimal = Decimal("1.00")) -> ParityResult:
    """Parity over ``assembled`` in memory -- the same pure pieces the DB engine runs (``engine._evaluate`` = rung 1
    + forex + rung 2, then the classifier and the ladder's first step), fed from captures instead of tables.

    ``snapshots``: ``trial_balance`` and ``trial_balance_ledgerwise`` as-on ``as_on`` (required) and ``anchor`` (the
    ledger-level TB as-on the D9 anchor date). ``dataset_anchor`` (plan ambiguity A5, company B only, G6 not
    captured): ledger name -> opening amount from ``company_b_data``; it replaces the anchor TB and is named in the
    result (``anchor_source="dataset"``). ``mirrored``: ledger name -> Tally's exported closing text; a ledger
    absent from it has no mirrored figure (rung 1 then reads the ledger-level TB row). A voucher line naming a
    ledger no master holds raises (ingest would have refused that batch: ``missing_master``)."""
    from v2.cloud.clock import fy_start_of
    from v2.cloud.ingest.resolve import ResolveError
    from v2.cloud.ingest.snapshots import parse_cells
    from v2.cloud.parity import classify as classify_mod
    from v2.cloud.parity import engine
    from v2.cloud.parity import ladder as ladder_mod
    from v2.cloud.parity.anchors import anchor_amounts, plan
    from v2.cloud.parity.model import bs_verified, build_sums, has_problem
    from v2.contract.parse import amount as parse_amount
    from v2.contract.tally_rules import PRIMARY_PARENT
    from v2.tests.cloud import parity_realdata as prd

    index, ledgers, parents, base = prd._index_and_ledgers(assembled.masters)
    parsed = prd._parse_vouchers(assembled.vouchers)
    facts = prd._line_facts(parsed, index)
    anchor_plan = plan(verified_edge, assembled.books_from)
    sums = build_sums(facts, verified_edge=verified_edge, as_on=as_on)

    if dataset_anchor is not None:
        anchors, anchor_unresolved = {}, []
        for name, amt in dataset_anchor.items():
            try:
                anchors[index.resolve("ledger", name)] = amt
            except ResolveError as exc:
                if exc.code != "missing_master":
                    raise
                anchor_unresolved.append(name)
        anchor_rows, anchor_source = [], "dataset"
    elif "anchor" in snapshots:
        day_one = None
        if anchor_plan.subtract_lines_dated is not None:
            d1 = anchor_plan.subtract_lines_dated
            day_one = build_sums([f for f in facts if f.voucher_date == d1], verified_edge=d1, as_on=d1).total
        snap = snapshots["anchor"]
        anchors, anchor_unresolved = anchor_amounts(snap.rows, index, day_one, ledgerwise_flags=snap.flags)
        anchor_rows, anchor_source = snap.rows, snap.source or "tb"
    else:
        anchors, anchor_unresolved, anchor_rows, anchor_source = None, [], [], "none"

    ledger_ins = []
    for l in ledgers:
        closing = parse_amount(mirrored.get(l["name"]))
        source = "needs_tb" if closing is not None and not closing.stated else "tally"
        ledger_ins.append(prd._ledger_in(l, parents, base, closing.inr if closing is not None else None, source,
                                         as_on))
    groups = [m["data"] for m in assembled.masters if m["kind"] == "group"]
    led = engine._Ledgers(ledger_ins, index, _stock_bearing(groups))
    tb, lw = snapshots["trial_balance"], snapshots["trial_balance_ledgerwise"]
    tb_snap = engine._Snap(tb.rows, tb.cells, tb.flags)
    lw_snap = engine._Snap(lw.rows, lw.cells, lw.flags)
    unadjusted = engine._net_unadjusted(tb_snap.rows, anchor_rows)
    ev = engine._evaluate(led, anchors, anchor_unresolved, sums, lw_snap, tb_snap, None, unadjusted, tol)

    fy_start = fy_start_of(as_on)
    flagged: dict[str, set[Decimal]] = {}
    for v in parsed:
        if v.is_optional and verified_edge <= v.date <= as_on:
            for line in v.lines:
                flagged.setdefault(index.resolve("ledger", line.ledger_name), set()).update(
                    {line.amount.inr, -line.amount.inr})
    ctx = classify_mod.Context(
        anchor_rows=ev.anchors, flagged_amounts=flagged, forex_guids={l.guid for l in ledger_ins if l.is_forex},
        fy_start=fy_start, verified_edge=verified_edge,
        month_ends_without_tb=[d for d in engine.month_ends(fy_start, as_on) if d != as_on])
    lines, remediations = classify_mod.classify(ev.lines, ctx)

    top_groups = {parse_name(g["name"]) for g in groups if (parse_name(g.get("parent")) or PRIMARY_PARENT)
                  == PRIMARY_PARENT}
    top_ledgers = {parse_name(l["name"]) for l in ledgers if (parse_name(l.get("parent")) or PRIMARY_PARENT)
                   == PRIMARY_PARENT}
    imbalance = engine.quantize(parse_cells("trial_balance", tb.cells, top_groups, top_ledgers).imbalance)

    had_mismatch = has_problem(lines)
    if not had_mismatch and not bs_verified(lines):
        status = "aborted_incomplete"
    else:
        status = ladder_mod.step({}, had_mismatch=had_mismatch, remediation_done=[],
                                 issued_ids=[r.id for r in remediations], confirmed_fy_resync_completed=False,
                                 fy_for_offer=fy_start)["state"]
    return ParityResult(status, lines, ev.lines, remediations, engine.quantize(ev.unrealised_total),
                        engine.summary(lines, ev.unrealised_total), imbalance, anchor_plan.as_on, anchor_source,
                        dict(ev.anchors))

