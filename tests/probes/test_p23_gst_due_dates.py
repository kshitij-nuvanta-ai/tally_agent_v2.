from probes import p23_gst_due_dates as p23
from probes.runner import run_probe
from tests.probes.fakes import ScriptedIO, a_tally, make_harness, objects_xml, ready_store

HEADS = {"CGST": "Central Tax", "SGST": "State Tax", "IGST": "Integrated Tax"}


def _rows(tax_type="GST", duty=True, type_of_duty=""):
    rows = [{"Name": f"{kind} {side}", "Parent": "Duties & Taxes", "TaxType": tax_type,
             "GSTDutyHead": HEADS[kind] if duty else "", "TypeOfDutyTax": type_of_duty}
            for kind in HEADS for side in ("Input", "Output")]
    rows += [{"Name": name, "Parent": parent, "TaxType": "", "GSTDutyHead": "",
              "TypeOfDutyTax": "Others" if type_of_duty else ""}
             for name, parent in (("Cash", "Cash-in-Hand"), ("Rent", "Indirect Expenses"), ("Apex", "Sundry Debtors"))]
    return rows


async def _run(tmp_path, rows):
    fake, _ = a_tally()
    fake.route("S0P23Ledgers", lambda body: objects_xml("LEDGER", rows))
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    await run_probe(p23.PROBE, labels=["A"], client=client, store=store, capture=capture, io=ScriptedIO())
    return store.probe_entry(23)


async def test_taxtype_identifies_the_gst_ledgers(tmp_path):
    entry = await _run(tmp_path, _rows())
    part = entry["parts"]["A"]
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert part["observations"]["rule_field"] == "TaxType"
    assert part["observations"]["duty_head_ok"] is True
    assert entry["remaining"] == ["B"]


async def test_type_of_duty_tax_alone_is_enough(tmp_path):
    part = (await _run(tmp_path, _rows(tax_type="", duty=False, type_of_duty="GST")))["parts"]["A"]
    assert part["outcome"] == "CONFIRMED"
    assert part["observations"]["rule_field"] == "TypeOfDutyTax"
    assert part["observations"]["duty_head_ok"] is False


async def test_no_candidate_field_fails(tmp_path):
    part = (await _run(tmp_path, _rows(tax_type="", duty=False)))["parts"]["A"]
    assert part["outcome"] == "FAILED"
    assert "GST position tile is dropped" in part["spec_impact"]
