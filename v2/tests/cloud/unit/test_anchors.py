"""Parity opening anchors (S1 spec D9, §10.2, §10.4) -- `plan`, `anchor_amounts`, `resolve_rows`,
`require_ledgerwise`. Real captures: `s1_A_tb_ledger_asof_2025-04-01.xml` (G1), `p17_A_*`, `s1_B_*` (G2)."""
from datetime import date
from decimal import Decimal as D

import pytest

from v2.cloud.ingest.resolve import NameIndex
from v2.cloud.parity.anchors import AnchorPlan, anchor_amounts, plan, require_ledgerwise, resolve_rows
from v2.cloud.parity.model import TbRow
from v2.tests.cloud import parity_realdata as prd
from v2.tests.cloud import realdata
LW = {"ISLEDGERWISE": "Yes"}


def test_anchor_is_tb_as_on_day_before_verified_edge():
    assert plan(date(2024, 4, 1), date(2022, 4, 1)) == AnchorPlan(date(2024, 3, 31), None)


def test_anchor_at_books_from_subtracts_own_first_day_lines():         # D9, C43-safe
    assert plan(date(2022, 4, 1), date(2022, 4, 1)) == AnchorPlan(date(2022, 4, 1), date(2022, 4, 1))


def test_verified_edge_before_books_from_is_refused():
    with pytest.raises(ValueError):
        plan(date(2021, 4, 1), date(2022, 4, 1))


def _a_index() -> NameIndex:
    return NameIndex.from_rows([("ledger", m["data"]["guid"], m["data"]["name"], False)
                                for m in realdata.a_masters() if m["kind"] == "ledger"])


def test_real_a_books_start_anchor_skips_opening_stock_row():           # G1 capture, LESSONS rule 19
    index = _a_index()
    rows = prd.tb("s1_A_tb_ledger_asof_2025-04-01.xml")
    assert [r.name for r in rows][0] == "Opening Stock"
    anchors, unresolved = anchor_amounts(rows, index, {}, ledgerwise_flags=LW)
    by_name = {index.resolve("ledger", n): n for n in ("Capital Account", "HDFC Bank - Current A/c",
                                                       "SBI Savings A/c")}
    assert {by_name[g]: a for g, a in anchors.items()} == {
        "Capital Account": D("750000.00"), "HDFC Bank - Current A/c": D("500000.00"),
        "SBI Savings A/c": D("200000.00")}
    assert unresolved == []


def test_books_from_line_sums_are_subtracted_incl_ledgers_absent_from_tb():
    index = _a_index()
    hdfc, cash = index.resolve("ledger", "HDFC Bank - Current A/c"), index.resolve("ledger", "Cash")
    anchors, _ = anchor_amounts(prd.tb("s1_A_tb_ledger_asof_2025-04-01.xml"), index,
                                {hdfc: D("-1000.00"), cash: D("1000.00")}, ledgerwise_flags=LW)
    # TB as-on books_from already includes day-one lines; the anchor is the balance BEFORE them
    assert anchors[hdfc] == D("501000.00") and anchors[cash] == D("-1000.00")


def test_tb_row_resolving_to_no_ledger_is_returned_unresolved():
    index = _a_index()
    anchors, unresolved = anchor_amounts([TbRow("Ghost Ledger", D("5.00")), TbRow("Cash", D("1.00"))], index, None, ledgerwise_flags=LW)
    assert unresolved == ["Ghost Ledger"] and list(anchors.values()) == [D("1.00")]


def test_resolve_rows_real_b_2023_ledgerwise_tb_all_resolve():           # G2, incl. the Hindi ledger name
    index = NameIndex.from_rows([("ledger", m["data"]["guid"], m["data"]["name"], False)
                                 for m in realdata.b_masters() if m["kind"] == "ledger"])
    by_guid, unresolved = resolve_rows(prd.tb("s1_B_tb_ledger_asof_2023-03-31.xml"), index)
    assert unresolved == []
    assert by_guid[index.resolve("ledger", "Gulf Office Supplies LLC (USD)")] == D("-132929.85")   # plain, G2
    assert len(by_guid) == 20                            # 22 rows - Opening Stock - Unadjusted Forex Gain/Loss


def test_require_ledgerwise_accepts_only_isledgerwise_yes():
    require_ledgerwise(prd.request_flags("s1_A_tb_ledger_asof_2025-04-01.xml"))
    require_ledgerwise(prd.request_flags("p17_A_tb_exploded_isledgerwise.xml"))
    for capture in ("p17_A_tb_exploded_explodeflag.xml", "p17_A_tb_exploded_ledgerwise.xml", "p16_A_tb_fy_end.xml"):
        with pytest.raises(ValueError, match="not a ledger-level TB"):
            require_ledgerwise(prd.request_flags(capture))
    with pytest.raises(ValueError, match="not a ledger-level TB"):
        require_ledgerwise(None)


def test_anchor_tb_must_be_ledger_level():
    """10c carry: `require_ledgerwise` guards the anchor TB too, not only rung 2's nominal TB (D29)."""
    index = NameIndex.from_rows([("ledger", "cash", "Cash", False)])
    for flags in ({"EXPLODEFLAG": "Yes"}, {}, None, {"ISLEDGERWISE": "No"}):
        with pytest.raises(ValueError, match="not a ledger-level TB"):
            anchor_amounts([TbRow("Cash", D("1.00"))], index, None, ledgerwise_flags=flags)
