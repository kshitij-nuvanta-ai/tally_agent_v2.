from probes import p03_voucher_ids_flags as p03
from probes.runner import run_probe
from tests.probes.fakes import ScriptedIO, a_tally, make_harness, objects_xml, ready_store


def _rows():
    rows = []
    for i in range(1, 51):
        kind = "Sales" if i <= 16 else "Purchase" if i <= 24 else "Payment" if i <= 40 else "Receipt"
        ref = f"S{i:03d}" if kind == "Sales" else f"P{i - 16:03d}" if kind == "Purchase" else ""
        rows.append({"GUID": f"g-1-{i:08x}", "MasterID": str(i), "AlterID": str(100 + i), "Date": "20251001",
                     "VoucherTypeName": kind, "VoucherNumber": str(i), "Reference": ref, "PartyLedgerName": "Apex",
                     "Narration": f"Invoice #{ref} - goods" if ref else f"{kind} {i}",
                     "IsCancelled": "No", "IsOptional": "No", "IsPostDated": "No"})
    return rows


async def _run(tmp_path, rows):
    fake, _ = a_tally()
    fake.route("S0P03Vouchers", lambda body: objects_xml("VOUCHER", rows))
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    await run_probe(p03.PROBE, labels=["A"], client=client, store=store, capture=capture, io=ScriptedIO())
    return store.probe_entry(3)


async def test_ids_unique_flags_present_references_right(tmp_path):
    entry = await _run(tmp_path, _rows())
    part = entry["parts"]["A"]
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert entry["remaining"] == ["B"]
    assert part["observations"]["count"] == 50
    assert part["fixtures"] == ["p03_A_vouchers_ids_flags.xml"]


async def test_a_flag_that_does_not_export_fails(tmp_path):
    rows = _rows()
    for row in rows:
        row["IsOptional"] = ""
    part = (await _run(tmp_path, rows))["parts"]["A"]
    assert part["outcome"] == "FAILED"
    assert "IsOptional" in part["summary"] and "R16" in part["spec_impact"]


async def test_reference_not_the_invoice_number_is_different(tmp_path):
    rows = _rows()
    rows[0]["Reference"] = "1"
    part = (await _run(tmp_path, rows))["parts"]["A"]
    assert part["outcome"] == "DIFFERENT"
    assert part["observations"]["wrong_references"][0]["expected"] == "S001"


async def test_duplicate_guid_fails(tmp_path):
    rows = _rows()
    rows[1]["GUID"] = rows[0]["GUID"]
    part = (await _run(tmp_path, rows))["parts"]["A"]
    assert part["outcome"] == "FAILED"
    assert "GUID" in part["summary"] and "R6" in part["spec_impact"]
