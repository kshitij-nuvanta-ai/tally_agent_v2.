from probes import p25_masters_classification as p25
from probes.runner import run_probe
from tests.probes.fakes import ScriptedIO, a_tally, make_harness, objects_xml, ready_store

VOUCHER_TYPES = [{"Name": "Sales", "Parent": "Sales", "ReservedName": "Sales"},
                 {"Name": "Payment", "Parent": "Payment", "ReservedName": "Payment"}]


def _groups(with_fields=True, with_parent=True):
    base = [("Current Liabilities", "Primary", "Current Liabilities", "No"),
            ("Sundry Creditors", "Current Liabilities", "Current Liabilities", "No"),
            ("National Creditors", "Sundry Creditors", "Current Liabilities", "No"),
            ("Sales Accounts", "Primary", "Sales Accounts", "Yes")]
    return [{"Name": name, "Parent": parent if with_parent else "", "_PrimaryGroup": primary if with_fields else "",
             "IsRevenue": revenue if with_fields else ""} for name, parent, primary, revenue in base]


async def _run(tmp_path, groups):
    fake, _ = a_tally()
    fake.route("S0P25Groups", lambda body: objects_xml("GROUP", groups))
    fake.route("S0P25VoucherTypes", lambda body: objects_xml("VOUCHERTYPE", VOUCHER_TYPES))
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    await run_probe(p25.PROBE, labels=["A"], client=client, store=store, capture=capture, io=ScriptedIO())
    return store.probe_entry(25)["parts"]["A"]


async def test_nature_and_base_type_come_from_fields(tmp_path):
    part = await _run(tmp_path, _groups())
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert part["observations"]["nature_field"] == "_PrimaryGroup"
    assert part["observations"]["base_types"] == {"Sales": "Sales", "Payment": "Payment"}
    assert part["fixtures"] == ["p25_A_groups.xml", "p25_A_voucher_types.xml"]


async def test_missing_fields_fall_back_to_walking_parent(tmp_path):
    part = await _run(tmp_path, _groups(with_fields=False))
    assert part["outcome"] == "DIFFERENT"
    assert part["observations"]["walk"] == ["National Creditors", "Sundry Creditors", "Current Liabilities"]
    assert "walking Parent" in part["spec_impact"]


async def test_parent_not_exported_fails(tmp_path):
    part = await _run(tmp_path, _groups(with_fields=False, with_parent=False))
    assert part["outcome"] == "FAILED"
