"""S1 build task 0: the read-only fixture capture tool (S1 spec §13.3 G1–G5). Offline — FakeBooks, never live Tally."""
import json
import re
from pathlib import Path

import httpx
import pytest

from v2.agent.tally.client import TallyClient
from v2.probes.companies import COMPANIES
from v2.probes.results import ResultsStore
from v2.probes.safety import GuardError
from v2.probes.setup import s1_capture as s1
from v2.probes.setup.company_b_data import USD_DEBTOR, USD_EXPORT_PARTY
from v2.tests.probes.fake_books import GUID, FakeBooks, seed_company_b
from v2.tests.probes.fakes import COMPANY_LIST_MARKER, FakeTally, ready_store

A, B = COMPANIES["A"], COMPANIES["B"]
FIXTURES = Path(__file__).parent.parent / "fixtures" / "sync"
REAL_RESULTS = Path(__file__).resolve().parents[2] / "probes" / "results" / "results.json"
B_FILES = ("counters", "tb_ledger_2025-04-01_2026-03-31", "tb_ledger_asof_2023-03-31", "tb_group_asof_2026-03-31",
           "tb_ledger_asof_2022-04-01",
           "voucher_types", "currencies", "stock_groups", "units", "usd_ledger", "ledgers_touched")
A_FILES = ("counters", "tb_ledger_asof_2025-04-01", "voucher_types", "currencies", "stock_groups", "units")


def _store(tmp_path, *, licence="educational", ledger_tb=True) -> ResultsStore:
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence=licence)
    if ledger_tb:                                     # the REAL confirmed template (probe 17), not a copy
        real = json.loads(REAL_RESULTS.read_text(encoding="utf-8"))["confirmed_requests"]["ledger_level_tb"]
        store.confirm_request("ledger_level_tb", 17, real["xml_template"])
    return store


def _books_b(**knobs) -> FakeBooks:
    books = FakeBooks(name=B, educational=True, **knobs)
    seed_company_b(books, "educational", masters=True)
    return books


async def _run(tmp_path, books, company="B", **kwargs):
    out = tmp_path / "out"
    names = await s1.run(TallyClient(transport=books.transport()), company, out,
                         store=kwargs.pop("store", None) or _store(tmp_path), say=lambda _m: None, **kwargs)
    return out, names


def _sidecar(out: Path, company: str, step: str) -> dict:
    return json.loads((out / f"s1_{company}_{step}.xml.json").read_text(encoding="utf-8"))


def _vars(xml: str) -> dict[str, str]:
    return dict(re.findall(r"<(SV[A-Z]*DATE|ISLEDGERWISE|EXPLODEFLAG)\b[^>]*>([^<]*)<", xml))


def _non_list(books) -> list[str]:
    return [r for r in books.requests if COMPANY_LIST_MARKER not in r]


# --- company B: every gap ------------------------------------------------------------------------------------------
async def test_company_b_writes_every_capture_with_the_standard_sidecar(tmp_path):
    out, names = await _run(tmp_path, _books_b())
    assert names == [f"s1_B_{step}.xml" for step in B_FILES]
    for step in B_FILES:
        assert (out / f"s1_B_{step}.xml").read_bytes()
        side = _sidecar(out, "B", step)
        assert side["probe"] is None and side["part"] == "B" and side["step"] == step
        assert side["company_name"] == B and side["company_guid"] == GUID
        assert side["capture"] == "s1_task0" and side["environment"]["licence"] == "educational"
        assert side["request_xml"] and "observations" in side
    assert {_sidecar(out, "B", s)["gap"] for s in B_FILES} == {"context", "G2", "G3", "G4", "G5", "G6"}


async def test_only_export_requests_are_sent(tmp_path):
    books = _books_b()
    await _run(tmp_path, books)
    assert books.requests
    for body in books.requests:
        assert "<TALLYREQUEST>Export</TALLYREQUEST>" in body
        assert "Import" not in body


async def test_g2_ledger_level_tbs_use_the_confirmed_template_with_typed_dates(tmp_path):
    out, _ = await _run(tmp_path, _books_b())
    current = _sidecar(out, "B", "tb_ledger_2025-04-01_2026-03-31")["request_xml"]
    past = _sidecar(out, "B", "tb_ledger_asof_2023-03-31")["request_xml"]
    assert _vars(current) == {"SVFROMDATE": "01-04-2025", "SVTODATE": "31-03-2026", "ISLEDGERWISE": "Yes"}
    assert _vars(past) == {"SVFROMDATE": "01-04-2022", "SVTODATE": "31-03-2023", "ISLEDGERWISE": "Yes"}
    for xml in (current, past):
        assert '<SVFROMDATE TYPE="Date">' in xml and '<SVTODATE TYPE="Date">' in xml
        assert "<SVCurrentCompany>Sharma &amp; Sons&apos; Probe Traders</SVCurrentCompany>" in xml
        assert "__COMPANY__" not in xml


async def test_g2_records_a_plain_usd_tb_row(tmp_path):
    out, _ = await _run(tmp_path, _books_b(forex_tb_row="plain"))
    for step in ("tb_ledger_2025-04-01_2026-03-31", "tb_ledger_asof_2023-03-31"):
        row = _sidecar(out, "B", step)["observations"]["usd_row"]
        assert row["ledger"] == USD_EXPORT_PARTY and row["found"] is True
        assert row["form"] == "plain"
    assert _sidecar(out, "B", "tb_ledger_asof_2023-03-31")["observations"]["usd_row"]["debit_text"] == "-132929.85"


async def test_g2_records_an_expression_usd_tb_row(tmp_path):
    out, _ = await _run(tmp_path, _books_b(forex_tb_row="expression"))
    row = _sidecar(out, "B", "tb_ledger_asof_2023-03-31")["observations"]["usd_row"]
    assert row["form"] == "expression"
    assert row["debit_text"].startswith("-$1609.71 @")


async def test_g3_group_tb_uses_explodeflag_as_on_fy_end(tmp_path):
    out, _ = await _run(tmp_path, _books_b())
    side = _sidecar(out, "B", "tb_group_asof_2026-03-31")
    assert _vars(side["request_xml"]) == {"SVFROMDATE": "01-04-2025", "SVTODATE": "31-03-2026", "EXPLODEFLAG": "Yes"}
    assert "unadjusted_forex_row" in side["observations"]


def test_g3_observer_reads_the_unadjusted_forex_row_from_a_real_capture():
    raw = (FIXTURES / "p18_B_tb_asof_2023-03-31.xml").read_text(encoding="utf-8")
    row = s1.tb_row(raw, s1.UNADJUSTED_FOREX_ROW)
    assert row == {"ledger": "Unadjusted Forex Gain/Loss", "found": True, "debit_text": "-183.87",
                   "credit_text": "", "form": "plain"}
    assert s1.tb_row(raw, "No Such Row")["found"] is False
    assert s1.tb_row(raw, "No Such Row")["form"] == "absent"


def test_amount_form():
    assert s1.amount_form("") == "empty"
    assert s1.amount_form("-1,33,113.72") == "plain"
    assert s1.amount_form("-$1609.71 @ ? 82.58/$ = -? 132929.85") == "expression"
    assert s1.amount_form("abc") == "other"


async def test_g4_masters_carry_guid_alterid_masterid(tmp_path):
    out, _ = await _run(tmp_path, _books_b())
    types = {"voucher_types": "VoucherType", "currencies": "Currency", "stock_groups": "StockGroup", "units": "Unit"}
    for step, tdl_type in types.items():
        side = _sidecar(out, "B", step)
        xml = side["request_xml"]
        assert f"<TYPE>{tdl_type}</TYPE>" in xml
        for field in ("GUID", "AlterID", "MasterID", "Name"):
            assert f"<NATIVEMETHOD>{field}</NATIVEMETHOD>" in xml
        assert "SVFROMDATE" not in xml and "SVTODATE" not in xml         # never period vars on a master collection
        obs = side["observations"]
        assert obs["rows"] > 0, step
        assert obs["missing_guid"] == [] and obs["missing_alterid"] == [] and obs["missing_masterid"] == []
    usd = _sidecar(out, "B", "usd_ledger")
    assert f'$Name = "{USD_EXPORT_PARTY}"' in usd["request_xml"]
    assert usd["observations"]["rows"] == 1
    assert usd["observations"]["found"] == [USD_EXPORT_PARTY]
    assert usd["observations"]["missing_guid"] == [] and usd["observations"]["missing_masterid"] == []


async def test_g5_touched_ledgers_reread_is_filtered_to_the_named_ledgers(tmp_path):
    out, _ = await _run(tmp_path, _books_b())
    side = _sidecar(out, "B", "ledgers_touched")
    xml = side["request_xml"]
    assert "<TYPE>Ledger</TYPE>" in xml and "SVTODATE" not in xml and "SVFROMDATE" not in xml
    for field in ("GUID", "Name", "OpeningBalance", "ClosingBalance"):
        assert f"<NATIVEMETHOD>{field}</NATIVEMETHOD>" in xml
    for name in s1.TOUCHED_LEDGERS:
        assert f"$Name = &quot;{name}&quot;" in xml or f'$Name = "{name}"' in xml
    assert " OR " in xml
    obs = side["observations"]
    assert obs["requested"] == list(s1.TOUCHED_LEDGERS)
    assert sorted(obs["returned"]) == sorted(s1.TOUCHED_LEDGERS) and obs["missing"] == []
    assert obs["closing_forms"][USD_EXPORT_PARTY] == "expression"
    assert obs["closing_forms"][USD_DEBTOR] in ("plain", "empty")
    assert set(s1.TOUCHED_LEDGERS) >= {USD_EXPORT_PARTY, "Export Sales", USD_DEBTOR}


# --- company A -----------------------------------------------------------------------------------------------------
async def test_company_a_writes_g1_and_g4_only(tmp_path):
    books = FakeBooks(name=A, educational=True)
    out, names = await _run(tmp_path, books, company="A")
    assert names == [f"s1_A_{step}.xml" for step in A_FILES]
    g1 = _sidecar(out, "A", "tb_ledger_asof_2025-04-01")
    assert g1["gap"] == "G1"
    assert _vars(g1["request_xml"]) == {"SVFROMDATE": "01-04-2025", "SVTODATE": "01-04-2025", "ISLEDGERWISE": "Yes"}
    assert "<SVCurrentCompany>Bharat Traders Probe Copy</SVCurrentCompany>" in g1["request_xml"]
    assert _sidecar(out, "A", "counters")["observations"]["books_from"] == "20250401"
    assert _sidecar(out, "A", "counters")["observations"]["books_from_matches"] is True
    assert not any(p.name.startswith("s1_B_") for p in out.iterdir())
    assert all(USD_EXPORT_PARTY not in r for r in books.requests)


# --- guards: nothing (or only the company list) is sent ------------------------------------------------------------
async def test_wrong_company_open_is_refused_after_only_the_company_list(tmp_path):
    books = FakeBooks(name=A, educational=True)
    with pytest.raises(GuardError, match="expects"):
        await _run(tmp_path, books, company="B")
    assert _non_list(books) == []
    assert not (tmp_path / "out").exists()


async def test_two_companies_loaded_is_refused(tmp_path):
    fake = FakeTally(companies=[B, A])
    with pytest.raises(GuardError, match="2 companies"):
        await s1.run(TallyClient(transport=fake.transport()), "B", tmp_path / "out", store=_store(tmp_path),
                     say=lambda _m: None)
    assert fake.probe_requests() == []


async def test_no_company_open_is_refused(tmp_path):
    fake = FakeTally(companies=[])
    with pytest.raises(GuardError, match="No company"):
        await s1.run(TallyClient(transport=fake.transport()), "A", tmp_path / "out", store=_store(tmp_path),
                     say=lambda _m: None)
    assert fake.probe_requests() == []


async def test_a_c43_unsafe_date_is_refused_before_any_request(tmp_path, monkeypatch):
    monkeypatch.setattr(s1, "B_TB_PAST", ("01-04-2022", "15-03-2023"))
    books = _books_b()
    with pytest.raises(GuardError, match="C43"):
        await _run(tmp_path, books)
    assert books.requests == []


async def test_licence_not_recorded_is_treated_as_educational(tmp_path, monkeypatch):
    monkeypatch.setattr(s1, "A_BOOKS_FROM", "15-04-2025")
    store = _store(tmp_path)
    del store.data["environment"]["licence"]
    books = FakeBooks(name=A, educational=True)
    with pytest.raises(GuardError, match="C43"):
        await _run(tmp_path, books, company="A", store=store)
    assert books.requests == []


async def test_licensed_tally_allows_any_valid_date(tmp_path, monkeypatch):
    monkeypatch.setattr(s1, "A_BOOKS_FROM", "15-04-2025")
    books = FakeBooks(name=A, educational=False)
    out, _ = await _run(tmp_path, books, company="A", store=_store(tmp_path, licence="licensed"))
    assert (out / "s1_A_tb_ledger_asof_2025-04-01.xml").exists()      # the step name is fixed, the date moved


def test_check_export_only_refuses_an_import_and_a_non_export():
    s1.check_export_only("<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST></HEADER></ENVELOPE>")
    with pytest.raises(GuardError, match="Import"):
        s1.check_export_only("<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER></ENVELOPE>")
    with pytest.raises(GuardError):
        s1.check_export_only("<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST></HEADER>"
                             "<BODY><IMPORTDATA/></BODY></ENVELOPE>")
    with pytest.raises(GuardError):
        s1.check_export_only("<ENVELOPE><HEADER></HEADER></ENVELOPE>")


async def test_an_import_in_the_plan_is_refused_before_any_request(tmp_path, monkeypatch):
    real = s1.plan

    def with_import(*args, **kwargs):
        specs = real(*args, **kwargs)
        return [*specs, s1.CaptureSpec("sneaky", "G4", "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST>"
                                                        "</HEADER></ENVELOPE>")]
    monkeypatch.setattr(s1, "plan", with_import)
    books = _books_b()
    with pytest.raises(GuardError, match="Import"):
        await _run(tmp_path, books)
    assert books.requests == []


async def test_missing_ledger_tb_template_is_refused(tmp_path):
    books = _books_b()
    with pytest.raises(GuardError, match="ledger_level_tb"):
        await _run(tmp_path, books, store=_store(tmp_path, ledger_tb=False))
    assert books.requests == []


def test_drifted_ledger_tb_template_is_refused():
    real = json.loads(REAL_RESULTS.read_text(encoding="utf-8"))["confirmed_requests"]["ledger_level_tb"]["xml_template"]
    s1.ledger_tb_request(real, B, "01-04-2022", "31-03-2023")
    for drifted in (real.replace("<ISLEDGERWISE>Yes</ISLEDGERWISE>", ""), real.replace(' TYPE="Date"', ""),
                    real.replace("__COMPANY__", "X"), real.replace("Export", "Import Data")):
        with pytest.raises(GuardError):
            s1.ledger_tb_request(drifted, B, "01-04-2022", "31-03-2023")


async def test_existing_captures_are_not_overwritten_unless_asked(tmp_path):
    out, _ = await _run(tmp_path, _books_b())
    books = _books_b()
    with pytest.raises(FileExistsError):
        await s1.run(TallyClient(transport=books.transport()), "B", out, store=_store(tmp_path), say=lambda _m: None)
    assert books.requests == []
    await s1.run(TallyClient(transport=books.transport()), "B", out, store=_store(tmp_path), say=lambda _m: None,
                 overwrite=True)
    assert _non_list(books)


async def test_a_timeout_saves_an_error_sidecar_and_stops(tmp_path):
    books = _books_b()

    def modal_on_group_tb(body: str) -> None:
        if "<EXPLODEFLAG>" in body:
            books.popup = True
    books.before_request = modal_on_group_tb
    with pytest.raises(s1.CaptureAborted, match="popup"):
        await _run(tmp_path, books)
    out = tmp_path / "out"
    side = _sidecar(out, "B", "tb_group_asof_2026-03-31")
    assert side["error"]["kind"] == "timeout"
    assert not (out / "s1_B_tb_group_asof_2026-03-31.xml").exists()
    assert not (out / "s1_B_voucher_types.xml.json").exists()          # nothing after the timeout was sent
    assert sum("<EXPLODEFLAG>" in r for r in books.requests) == 1


async def test_company_switched_mid_run_is_reported(tmp_path):
    books = _books_b()
    seen = {"n": 0}

    def switch_after_first_capture(body: str) -> None:
        if COMPANY_LIST_MARKER not in body:
            seen["n"] += 1
            if seen["n"] == 3:
                books.edit_state(lambda state: state.update(name=A))
    books.before_request = switch_after_first_capture
    with pytest.raises(GuardError, match="expects"):
        await _run(tmp_path, books)


# --- CLI ------------------------------------------------------------------------------------------------------------
def test_parser_defaults():
    args = s1.build_parser().parse_args(["--company", "B"])
    assert (args.host, args.port, args.out, args.overwrite) == ("localhost", 9000, s1.FIXTURES_DIR, False)
    with pytest.raises(SystemExit):
        s1.build_parser().parse_args(["--company", "C"])


def test_main_runs_end_to_end_and_maps_a_guard_to_exit_2(tmp_path, capsys):
    store = _store(tmp_path)
    store.save()
    books = _books_b()
    argv = ["--company", "B", "--out", str(tmp_path / "out"), "--results", str(store.path)]
    assert s1.main(argv, transport=books.transport()) == 0
    assert (tmp_path / "out" / "s1_B_ledgers_touched.xml").exists()
    assert "usd_row" in capsys.readouterr().out
    wrong = FakeBooks(name=A, educational=True)
    assert s1.main(argv + ["--overwrite"], transport=wrong.transport()) == 2
    assert "Refused" in capsys.readouterr().err


def test_main_maps_unreachable_tally_to_exit_1(tmp_path, capsys):
    store = _store(tmp_path)
    store.save()

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    argv = ["--company", "A", "--out", str(tmp_path / "out"), "--results", str(store.path)]
    assert s1.main(argv, transport=httpx.MockTransport(refuse)) == 1
    assert "not reachable" in capsys.readouterr().err


async def test_g6_b_books_start_ledger_tb_is_as_on_01_04_2022(tmp_path):
    out, _ = await _run(tmp_path, _books_b())
    g6 = _sidecar(out, "B", "tb_ledger_asof_2022-04-01")
    assert g6["gap"] == "G6"
    assert _vars(g6["request_xml"]) == {"SVFROMDATE": "01-04-2022", "SVTODATE": "01-04-2022", "ISLEDGERWISE": "Yes"}
    assert "rows" in g6["observations"]


async def test_only_captures_a_single_step_and_leaves_existing_files_alone(tmp_path):
    out, _ = await _run(tmp_path, _books_b())
    keep = (out / "s1_B_units.xml").read_bytes()
    (out / "s1_B_tb_ledger_asof_2022-04-01.xml").unlink()
    (out / "s1_B_tb_ledger_asof_2022-04-01.xml.json").unlink()
    books = _books_b()
    names = await s1.run(TallyClient(transport=books.transport()), "B", out, store=_store(tmp_path),
                         say=lambda _m: None, only="tb_ledger_asof_2022-04-01")
    assert names == ["s1_B_tb_ledger_asof_2022-04-01.xml"]
    assert len(_non_list(books)) == 1 and (out / "s1_B_units.xml").read_bytes() == keep
    with pytest.raises(FileExistsError):                  # --only still never overwrites without --overwrite
        await s1.run(TallyClient(transport=books.transport()), "B", out, store=_store(tmp_path),
                     say=lambda _m: None, only="tb_ledger_asof_2022-04-01")
    with pytest.raises(GuardError):
        await s1.run(TallyClient(transport=books.transport()), "B", out, store=_store(tmp_path),
                     say=lambda _m: None, only="nope")


def test_parser_only_option():
    assert s1.build_parser().parse_args(["--company", "B", "--only", "units"]).only == "units"
