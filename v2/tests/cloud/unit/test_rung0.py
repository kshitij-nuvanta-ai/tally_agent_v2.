"""Rung 0 (S1 spec §10.3): Sigma line.amount.inr over a voucher's ledger_entries must be exactly 0.00, on the INR
base. First three tests build `PVoucher`/`PLine`/`Amount` directly so `voucher_balances` is exercised in isolation;
the last two go through the real transcoder + `parse_objects` against S0 captures (CLAUDE.md "test reality")."""
import copy
from datetime import date
from decimal import Decimal
from pathlib import Path

from v2.cloud.ingest.parsed import PLine, PVoucher
from v2.cloud.ingest.validate import parse_objects
from v2.cloud.parity.rung0 import voucher_balances
from v2.contract import transcode
from v2.contract.parse import Amount

SYNC = Path(__file__).resolve().parents[2] / "fixtures" / "sync"


def _vouchers(name):
    return transcode.vouchers_from_xml((SYNC / name).read_text(encoding="utf-8"))


def _amt(inr):
    return Amount(inr=Decimal(inr), fx_currency=None, fx_amount=None, fx_rate=None, stated=True, text=str(inr))


def _line(no, inr, deemed_positive):
    return PLine(line_no=no, ledger_name=f"L{no}", amount=_amt(inr), is_deemed_positive=deemed_positive,
                 ledger_guid_hint=None, bills=[])


def _voucher(lines):
    return PVoucher(index=0, guid="g", master_id=" 1", alter_id=1, date=date(2026, 1, 1), effective_date=None,
                     voucher_type_name="Sales", voucher_number="1", reference="", party_ledger_name="P",
                     narration="", is_cancelled=False, is_optional=False, is_post_dated=False, is_invoice=None,
                     lines=lines, inventory=[], has_forex=False, raw={})


def test_rung0_exact_zero():
    v = _voucher([_line(0, "-100.00", True), _line(1, "60.00", False), _line(2, "40.00", False)])
    assert voucher_balances(v) is True


def test_rung0_one_paisa_off_fails():
    v = _voucher([_line(0, "-100.00", True), _line(1, "60.00", False), _line(2, "40.01", False)])
    assert voucher_balances(v) is False


def test_rung0_on_forex_bases():
    objs = [v for v in _vouchers("p22_B_forex_sales.xml") if v["data"]["guid"].endswith("-000003c1")]
    parsed, errors, _ = parse_objects(objs)
    assert errors == []
    (v,) = parsed
    assert v.has_forex and voucher_balances(v) is True


def test_rung0_empty_lines_balances():
    parsed, errors, _ = parse_objects(_vouchers("p03_B_flagged_month_2023_02.xml"))
    assert errors == []
    cancelled = next(v for v in parsed if v.is_cancelled)
    assert cancelled.lines == [] and voucher_balances(cancelled) is True


def _join(base_objs, *sources, keys):
    """Copy `keys` from each source list into `base_objs`, matched by GUID. p06 lacks AlterID/flags (fixture gap
    A4: not every capture request asked for every field the wire needs) -- test-only identity join, no production
    equivalent."""
    by_guid: dict[str, dict] = {}
    for source in sources:
        for obj in source:
            by_guid.setdefault(obj["data"]["guid"], {}).update(
                {k: obj["data"][k] for k in keys if k in obj["data"]})
    out = copy.deepcopy(base_objs)
    for obj in out:
        obj["data"].update(by_guid.get(obj["data"]["guid"], {}))
    return out


def test_p06_all_50_balance_under_all_only():
    base = _vouchers("p06_A_vouchers_nested.xml")
    joined = _join(base, _vouchers("p04_A_voucher_full.xml"), _vouchers("p16_A_vouchers_fy.xml"),
                    keys=("alterid", "iscancelled", "isoptional", "ispostdated"))
    parsed, errors, _ = parse_objects(joined)
    assert errors == []
    assert len(parsed) == 50
    assert all(voucher_balances(v) for v in parsed)
