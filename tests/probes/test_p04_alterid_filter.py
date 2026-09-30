import re

import httpx

from probes import p04_alterid_filter as p04
from probes.runner import run_probe
from tests.probes.fakes import COMPANY_A, ScriptedIO, a_tally, make_harness, objects_xml, ready_store

DATA = {
    "S0P04Voucher": ("VOUCHER", [{"GUID": f"v-{i}", "AlterID": str(i), "MasterId": str(i)} for i in range(1, 11)]),
    "S0P04Ledger": ("LEDGER", [{"Name": f"L{i}", "GUID": f"l-{i}", "AlterID": str(200 + 3 * i)} for i in range(1, 8)]),
    "S0P04Group": ("GROUP", [{"Name": f"G{i}", "GUID": f"gr-{i}", "AlterID": str(300 + i)} for i in range(1, 4)]),
    "S0P04StockItem": ("STOCKITEM", [{"Name": f"I{i}", "GUID": f"s-{i}", "AlterID": str(400 + i)} for i in range(1, 16)]),
}


def _handler(marker, honour_filter=True):
    tag, rows = DATA[marker]

    def handler(body):
        match = re.search(r"\$AlterID &gt; (-?\d+)", body)
        chosen = [r for r in rows if not (match and honour_filter) or int(r["AlterID"]) > int(match.group(1))]
        return objects_xml(tag, chosen)
    return handler


async def _run(tmp_path, honour_filter=True, hang_after=False):
    fake, state = a_tally()
    for marker in DATA:
        fake.route(marker, _handler(marker, honour_filter))
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    if hang_after:
        calls = {"n": 0}
        others = [(m, h) for m, h in fake.routes if m != "S0CompanyCounters"]

        def counters(body):
            calls["n"] += 1
            if calls["n"] > 1:              # the runner's GUID read passes, the cheap read after the filters hangs
                return httpx.ReadTimeout
            return objects_xml("COMPANY", [{"Name": COMPANY_A, "GUID": "g-1", "AltVchId": "50", "AltMstId": "266"}])

        fake.routes = [("S0CompanyCounters", counters), *others]
    await run_probe(p04.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    return store.probe_entry(4)["parts"]["A"]


async def test_filters_return_exactly_the_expected_objects(tmp_path):
    part = await _run(tmp_path)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    ledger = part["observations"]["checks"]["ledger"]
    assert ledger["max_alterid"] == 221
    assert ledger["filters"]["gt_m_minus_5"]["expected"] == 2          # AlterIDs 218 and 221 only
    assert len(part["fixtures"]) == 4 * 4 + 1
    assert "p04_A_stockitem_gt_m_minus_5.xml" in part["fixtures"]


async def test_filter_ignored_fails(tmp_path):
    part = await _run(tmp_path, honour_filter=False)
    assert part["outcome"] == "FAILED"
    assert "rolling re-pull" in part["spec_impact"]


async def test_tally_not_answering_after_the_filters_fails(tmp_path):
    part = await _run(tmp_path, hang_after=True)
    assert part["outcome"] == "FAILED"
    assert "stopped answering" in part["summary"]
