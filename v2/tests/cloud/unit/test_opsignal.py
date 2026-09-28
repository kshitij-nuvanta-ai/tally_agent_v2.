"""Ops signal (S1 spec §10.10, decision 14): counts and causes only -- no names, GUIDs or exact amounts."""
import json
import logging
from decimal import Decimal as D

from v2.cloud.parity.model import Line
from v2.cloud.parity.opsignal import bucket, emit, integrity_event, quarantine_event


def test_buckets():
    assert [bucket(D(x)) for x in ("999.99", "1000", "99999.99", "100000", "9999999.99", "10000000")] == [
        "<₹1k", "<₹1L", "<₹1L", "<₹1Cr", "<₹1Cr", "≥₹1Cr"]


def test_event_has_no_names_guids_or_exact_amounts(caplog):
    lines = [Line("ledger", "710de34a-…-000000dd", "Apex Technologies Pvt Ltd", D("-62800.00"), D("-62700.00"),
                  D("-100.00"), "mismatch", "ledger_gap")]
    ev = integrity_event("ws-1", "run-1", 1, "suspect", lines)
    text = json.dumps(ev)
    assert "Apex" not in text and "000000dd" not in text and "62800" not in text and "100.00" not in text
    assert ev["cause_counts"] == {"ledger_gap": 1} and ev["mismatch_count"] == 1 and ev["max_abs_diff_bucket"] == "<₹1k"
    with caplog.at_level(logging.INFO, logger="v2.ops.integrity"):
        emit(ev)
    assert caplog.records[-1].name == "v2.ops.integrity"


def test_multi_line_event_counts_causes_and_worst_bucket():
    lines = [
        Line("ledger", "g1", "Real Ledger Name One", D("-200000.00"), D("-100000.00"), D("100000.00"),
             "mismatch", "ledger_gap"),
        Line("ledger", "g2", "Real Ledger Name Two", D("-50.00"), D("-52.00"), D("-2.00"),
             "mismatch", "ledger_gap"),
        Line("ledger", None, "Ghost Ledger", None, None, None, "missing_in_db", "masters_gap"),
        Line("ledger", "g3", "Matched Ledger", D("-10.00"), D("-10.00"), D("0.00"), "match", None),
    ]
    ev = integrity_event("ws-2", "run-2", 1, "alert", lines)
    assert ev["cause_counts"] == {"ledger_gap": 2, "masters_gap": 1}
    assert ev["mismatch_count"] == 3
    assert ev["max_abs_diff_bucket"] == "<₹1Cr"          # worst diff is Rs 1,00,000.00


def test_quarantine_event_carries_no_business_data():
    ev = quarantine_event("ws-3", {"unbalanced_voucher": 2, "invalid_date": 1})
    assert ev == {"workspace_id": "ws-3", "event": "quarantine",
                  "counts_by_code": {"unbalanced_voucher": 2, "invalid_date": 1}}
    text = json.dumps(ev)
    for leak in ("Apex", "710de34a", "guid", "62800", "narration", "voucher_number"):
        assert leak not in text
