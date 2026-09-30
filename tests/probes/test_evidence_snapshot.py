"""The 2026-09-23 company-A parity captures, moved (git mv, byte-identical) into their own folder beside the live
fixtures before plan part 5 re-runs 16/17/18.

They were sent with UNTYPED date variables (Ruling C33), so they are history, not current evidence: the tests that
pin what that run saw read them from here, and the re-run writes fresh flat `p16_A_*` … files.
"""
import json
from datetime import date
from pathlib import Path

from probes.reads import parse_vouchers, tally_date

SYNC = Path(__file__).resolve().parents[1] / "fixtures" / "sync"
SNAPSHOT = SYNC / "c33_untyped_2026-09-23"


def test_the_2026_09_23_parity_evidence_is_kept_beside_the_live_fixtures():
    names = {p.name for p in SNAPSHOT.iterdir()}
    for required in ("p16_A_ledgers_asof_2025-10-31.xml", "p16_A_tb_fy_end.xml", "p17_A_tb_exploded_isledgerwise.xml",
                     "p17_A_tb_exploded_explodealllevels.xml", "p18_A_bills_receivable_asof_2025-09-30.xml",
                     "p18_A_vouchers_to_2025-10-31.xml", "p18_A_stock_summary_asof_2025-09-30.xml"):
        assert required in names and f"{required}.json" in names, required


def test_the_snapshot_really_is_the_untyped_run():
    sidecar = json.loads((SNAPSHOT / "p16_A_ledgers_asof_2025-10-31.xml.json").read_text(encoding="utf-8"))
    assert "<SVTODATE>31-10-2025</SVTODATE>" in sidecar["request_xml"]
    assert 'TYPE="Date"' not in sidecar["request_xml"]


def test_the_untyped_voucher_read_answered_for_the_whole_fy_c33():
    """Why probe 18's 'full period' evidence held on 2026-09-23: the untyped to-date was ignored."""
    vouchers = parse_vouchers((SNAPSHOT / "p18_A_vouchers_to_2025-10-31.xml").read_text(encoding="utf-8"))
    assert max(tally_date(v["header"]["DATE"]) for v in vouchers) > date(2025, 10, 31)
