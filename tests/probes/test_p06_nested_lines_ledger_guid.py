import httpx

from probes import p06_nested_lines_ledger_guid as p06
from probes.runner import run_probe
from tests.probes.fakes import ScriptedIO, a_tally, line, make_harness, objects_xml, ready_store, vch, vouchers_xml

GUIDS = {"Apex": "g-apex", "Samsung": "g-sam", "CGST Output": "g-cgo", "SGST Output": "g-sgo", "CGST Input": "g-cgi",
         "SGST Input": "g-sgi", "Cash": "g-cash", "HDFC": "g-hdfc", "Rent": "g-rent",
         "Sales - Electronics": "g-sales", "Purchase - Electronics": "g-purch"}


def _inventory(amount, inward):
    return [{"STOCKITEMNAME": "HP Laptop 15s", "ISDEEMEDPOSITIVE": "Yes" if inward else "No", "ACTUALQTY": "1 Nos",
             "BILLEDQTY": "1 Nos", "RATE": "100.00/Nos", "AMOUNT": amount,
             "accounting": [line("Purchase - Electronics" if inward else "Sales - Electronics", amount)],
             "batches": [{"GODOWNNAME": "Main Location", "BATCHNAME": "Primary Batch", "AMOUNT": amount}]}]


def _vouchers(receipt_bill_type="Agst Ref", bills=True, sales_party="-118.00"):
    def bill(name, kind, amount):
        return [{"NAME": name, "BILLTYPE": kind, "AMOUNT": amount, "BILLCREDITPERIOD": ""}] if bills else []
    return vouchers_xml([
        vch({"DATE": "20251001", "VOUCHERTYPENAME": "Sales", "MASTERID": "1"},
            lines=[line("Apex", sales_party, bills=bill("S001", "New Ref", "-118.00"), _list="LEDGERENTRIES.LIST"),
                   line("CGST Output", "9.00", _list="LEDGERENTRIES.LIST"),
                   line("SGST Output", "9.00", _list="LEDGERENTRIES.LIST")],
            inventory=_inventory("100.00", inward=False)),
        vch({"DATE": "20250928", "VOUCHERTYPENAME": "Purchase", "MASTERID": "2"},
            lines=[line("Samsung", "118.00", bills=bill("P001", "New Ref", "118.00"), _list="LEDGERENTRIES.LIST"),
                   line("CGST Input", "-9.00", _list="LEDGERENTRIES.LIST"),
                   line("SGST Input", "-9.00", _list="LEDGERENTRIES.LIST")],
            inventory=_inventory("-100.00", inward=True)),
        vch({"DATE": "20251020", "VOUCHERTYPENAME": "Receipt", "MASTERID": "3"},
            lines=[line("Cash", "-118.00"), line("Apex", "118.00", bills=bill("S001", receipt_bill_type, "118.00"))]),
        vch({"DATE": "20251010", "VOUCHERTYPENAME": "Payment", "MASTERID": "4"},
            lines=[line("Samsung", "-50.00", bills=bill("P001", "Agst Ref", "-50.00")), line("HDFC", "50.00")]),
        vch({"DATE": "20251130", "VOUCHERTYPENAME": "Payment", "MASTERID": "5"},
            lines=[line("Rent", "-75.00"), line("HDFC", "75.00")]),
    ])


TDL_OK = ("<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(
    f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{name}</LEDGERNAME><S0LEDGERGUID>{guid}</S0LEDGERGUID></ALLLEDGERENTRIES.LIST>"
    for name, guid in [("Cash", "g-cash"), ("Apex", "g-apex"), ("HDFC", "g-hdfc")]) + "</COLLECTION></DATA></BODY></ENVELOPE>")


async def _run(tmp_path, monkeypatch, vouchers=None, tdl=TDL_OK, fetch=None):
    monkeypatch.setattr(p06, "EXPECTED_PARTY_PAYMENTS", 1)
    monkeypatch.setattr(p06, "EXPECTED_EXPENSE_PAYMENTS", 1)
    fake, _ = a_tally()
    fake.route("S0P06Vouchers", lambda body: vouchers or _vouchers())
    default_fetch = vouchers_xml([vch({"DATE": "20251130", "VOUCHERTYPENAME": "Payment"},
                                      lines=[line("Rent", "-75.00"), line("HDFC", "75.00")])])
    fake.route("S0P06LineFetch", lambda body: default_fetch if fetch is None else fetch)
    fake.route("S0P06LineTdl", lambda body: tdl)
    fake.route("S0P06LedgerGuids", lambda body: objects_xml("LEDGER", [{"Name": n, "GUID": g} for n, g in GUIDS.items()]))
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store)
    await run_probe(p06.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    return store.probe_entry(6)["parts"]["A"]


async def test_nested_lists_balance_and_the_tdl_route_gives_ledger_guids(tmp_path, monkeypatch):
    part = await _run(tmp_path, monkeypatch)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    obs = part["observations"]
    assert obs["balancing_rule"] == "default"
    assert obs["line_guid"]["route"] == "tdl"
    assert obs["presence"]["batch"]["GODOWNNAME"] == 2
    assert "inline TDL" in part["summary"]
    assert part["fixtures"] == ["p06_A_vouchers_nested.xml", "p06_A_ledger_guids.xml", "p06_A_line_guid_fetch.xml",
                                "p06_A_line_guid_tdl.xml"]


async def test_no_ledger_guid_on_lines_is_different(tmp_path, monkeypatch):
    part = await _run(tmp_path, monkeypatch, tdl="<ENVELOPE></ENVELOPE>")
    assert part["outcome"] == "DIFFERENT"
    assert "name resolution stays" in part["spec_impact"]


async def test_unbalanced_voucher_fails(tmp_path, monkeypatch):
    part = await _run(tmp_path, monkeypatch, vouchers=_vouchers(sales_party="-117.00"))
    assert part["outcome"] == "FAILED"
    assert "Rung 0" in part["spec_impact"]


async def test_missing_bill_allocations_fail(tmp_path, monkeypatch):
    part = await _run(tmp_path, monkeypatch, vouchers=_vouchers(bills=False))
    assert part["outcome"] == "FAILED"
    assert "S1 schema" in part["spec_impact"]


async def test_bill_types_not_matching_the_seed_are_different(tmp_path, monkeypatch):
    part = await _run(tmp_path, monkeypatch, vouchers=_vouchers(receipt_bill_type="New Ref"))
    assert part["outcome"] == "DIFFERENT"
    assert part["observations"]["structure"]["receipts_agst_ref"] is False


# --- M8: a transport error on a line-GUID candidate blocks, it isn't "no route" ---------------------------------------


async def test_line_guid_transport_error_blocks_instead_of_no_route(tmp_path, monkeypatch):
    part = await _run(tmp_path, monkeypatch, tdl="<ENVELOPE></ENVELOPE>", fetch=httpx.ReadTimeout)
    assert part["outcome"] == "BLOCKED"
    assert p06.POPUP_HINT in part["summary"]
    assert "no route gives a ledger GUID" not in part["summary"]
    # The evidence already collected survives the BLOCKED return — including the transport error itself.
    obs = part["observations"]
    assert obs["voucher_count"] == 5
    assert obs["presence"]["line"]["total"] > 0
    assert obs["structure"]["sales_have_inventory"] is True
    assert obs["balance"]["rule"] == "default" and obs["balancing_rule"] == "default"
    assert obs["line_guid"]["route"] is None
    assert obs["line_guid"]["fetch_error"]["kind"] == "timeout"
    assert obs["line_guid"]["tdl_pairs"] == 0


async def test_line_guid_refused_also_blocks(tmp_path, monkeypatch):
    part = await _run(tmp_path, monkeypatch, tdl="<ENVELOPE></ENVELOPE>", fetch=httpx.ConnectError)
    assert part["outcome"] == "BLOCKED"
    assert "refused" in part["summary"]
