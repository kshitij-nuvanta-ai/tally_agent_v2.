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

import hashlib
import json
from pathlib import Path

from v2.contract import transcode
from v2.contract.parse import name as parse_name

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


def _join(company_guid: str, kind: str, base: list[dict], *extra: list[dict],
          captured_at: str | None = None) -> list[dict]:
    """`base` defines the set of masters (and wins on every key it holds); each `extra` list only fills keys the
    base lacks, matched by cleaned name. No key is invented except the A4 identity."""
    by_name = [{parse_name(o["data"]["name"]): o["data"] for o in objs} for objs in extra]
    out: list[dict] = []
    seen: set[str] = set()
    for obj in base:
        data = dict(obj["data"])
        key = parse_name(data["name"])
        if key in seen:                                   # the same master in two base captures: first one wins
            continue
        seen.add(key)
        for index in by_name:
            for k, v in index.get(key, {}).items():
                data.setdefault(k, v)
        if "guid" not in data or "alterid" not in data:
            guid, alter = synthetic_identity(company_guid, kind, data["name"])
            data.setdefault("guid", guid)
            data.setdefault("alterid", alter)
        if captured_at is not None and any(k in data for k in ("closingbalance", "closingvalue")):
            data.setdefault("captured_at", captured_at)
        out.append({"kind": kind, "data": data})
    return out


def b_masters() -> list[dict]:
    """Company B (`Sharma & Sons' Probe Traders`) masters, §12 step 6 kind order (F8: incl. stock items + units)."""
    g = COMPANY_B_GUID
    return [
        *_join(g, "currency", masters("p22_B_currencies.xml", "currency"), masters("s1_B_currencies.xml", "currency")),
        *_join(g, "group", masters("p18_B_group_list.xml", "group")),                           # G4: no ids
        *_join(g, "voucher_type", masters("p25_B_voucher_types.xml", "voucher_type"),
               masters("s1_B_voucher_types.xml", "voucher_type")),
        *_join(g, "unit", masters("s1_B_units.xml", "unit")),
        *_join(g, "ledger", masters("p16_B_ledgers.xml", "ledger"), masters("s1_B_usd_ledger.xml", "ledger"),
               masters("s1_B_ledgers_touched.xml", "ledger"), captured_at=sent_at("p16_B_ledgers.xml")),
        *_join(g, "stock_item", masters("p11_B_stock_openings.xml", "stock_item")
               + masters("p15_B_compound_unit_item.xml", "stock_item")),                        # G4: no ids
    ]


def a_masters() -> list[dict]:
    """Company A (`Bharat Traders…` S0 company) masters, §12 step 6 kind order (F8: incl. stock items + units)."""
    g = COMPANY_A_GUID
    return [
        *_join(g, "currency", masters("s1_A_currencies.xml", "currency")),
        *_join(g, "group", masters("p25_A_groups.xml", "group"), masters("p04_A_group_full.xml", "group")),
        *_join(g, "voucher_type", masters("p25_A_voucher_types.xml", "voucher_type"),
               masters("s1_A_voucher_types.xml", "voucher_type")),
        *_join(g, "unit", masters("s1_A_units.xml", "unit")),
        *_join(g, "stock_group", masters("s1_A_stock_groups.xml", "stock_group")),
        *_join(g, "ledger", masters("p16_A_ledgers.xml", "ledger"), masters("p04_A_ledger_full.xml", "ledger"),
               masters("p23_A_gst_ledgers.xml", "ledger"), captured_at=sent_at("p16_A_ledgers.xml")),
        *_join(g, "stock_item", masters("p18_A_stock_item_openings.xml", "stock_item"),
               masters("p04_A_stockitem_full.xml", "stock_item")),
    ]


def ledger_balance_delta(before_file: str, after_file: str) -> dict[str, tuple[str, str]]:
    """Ledgers whose closing balance differs between two ledger captures: name -> (before, after) text."""
    before = {o["data"]["name"]: o["data"].get("closingbalance") for o in masters(before_file, "ledger")}
    after = {o["data"]["name"]: o["data"].get("closingbalance") for o in masters(after_file, "ledger")}
    return {n: (before[n], after[n]) for n in before if before[n] != after.get(n)}


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
    delta = ledger_balance_delta("p16_A_ledgers.xml", "p16_A_ledgers_with_post_dated.xml")
    assert delta["Cash"] == ("23000.00", "23001.00") and delta["Electricity"] == ("-12000.00", "-12001.00"), delta
    obj = next(v for v in vouchers("p16_A_post_dated_voucher.xml") if v["data"].get("ispostdated") == "Yes")
    data = dict(obj["data"])
    data.setdefault("alterid", "1")
    data.setdefault("iscancelled", "No")
    data.setdefault("isoptional", "No")
    data["ledger_entries"] = [
        {"ledgername": "Electricity", "isdeemedpositive": "Yes", "amount": "-1.00"},
        {"ledgername": "Cash", "isdeemedpositive": "No", "amount": "1.00"},
    ]
    return {"kind": "voucher", "data": data}


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
