import httpx

from agent.tally.client import TallyClient
from probes import p10_error_shapes as p10
from probes.capture import Capture
from probes.operator.auto import build_auto_operator
from probes.operator.tally_control import TallyProcess
from probes.results import ResultsStore
from probes.runner import run_probe
from tests.probes.fake_books import OWN_COMMAND, FakeBooks, FakeRunner, tmp_config
from tests.probes.fakes import COMPANY_A, ScriptedIO, a_tally, make_harness, objects_xml, ready_store, tb_xml

NO_COMPANY = "<ENVELOPE><BODY><DESC><CMPINFO><COMPANY>0</COMPANY></CMPINFO></DESC></BODY></ENVELOPE>"
LICENCE = ("<ENVELOPE><HEADER><VERSION>1</VERSION><PRODMAJORREL>7</PRODMAJORREL><PRODMINORREL>0</PRODMINORREL></HEADER>"
           "<BODY><DATA><RESULT>Yes</RESULT></DATA></BODY></ENVELOPE>")


def _fake(no_company_data=False, stuck_after_popup=False):
    fake, _ = a_tally()
    state = {"popup": False}

    def cheap(body):
        if state["popup"]:
            return httpx.ReadTimeout
        return objects_xml("COMPANY", [{"Name": COMPANY_A, "GUID": "g-1"}] if fake.companies else [])

    def ledgers(body):
        if fake.companies or no_company_data:
            return objects_xml("LEDGER", [{"Name": "Cash"}])
        return NO_COMPANY

    fake.route("S0ActiveCompany", cheap)
    fake.route("S0P10Ledgers", ledgers)
    fake.route("<ID>Trial Balance</ID>",
               lambda body: tb_xml([("Capital Account", "", "1.00")]) if fake.companies else "<ENVELOPE></ENVELOPE>")
    fake.route("<TYPE>Function</TYPE>", lambda body: LICENCE)

    def on_action(action):
        if action.kind == "raise_popup":
            state["popup"] = True
        elif action.kind == "dismiss_popup":
            state["popup"] = stuck_after_popup
            fake.companies = [COMPANY_A]
        elif action.kind == "close_all_companies":
            fake.companies = []
        elif action.kind == "quit_tally":
            fake.down = True
        elif action.kind == "open_company":
            fake.down = False
            fake.companies = [COMPANY_A]

    return fake, on_action


async def _run(tmp_path, licence="educational", popup_raised=None, **kw):
    fake, on_action = _fake(**kw)
    io = ScriptedIO(on_action=on_action)
    if popup_raised is not None:
        io.popup_raised = popup_raised      # what the auto operator reports about the duplicate create
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store, licence=licence)
    store.confirm_request("active_company", 2, p10.fallback_cheap_read().replace(
        "S0P10Cheap", "S0ActiveCompany"), candidate="a")
    await run_probe(p10.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    return store.probe_entry(10)["parts"]["A"], io


async def test_each_condition_has_its_own_shape(tmp_path):
    part, io = await _run(tmp_path)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    rows = {row["condition"]: row for row in part["observations"]["gate_table"]}
    assert rows["popup / modal open"]["transport"] == "timeout"
    assert rows["no company open (collection)"]["body"] == "empty"
    assert rows["Tally not running"]["transport"] == "refused"
    assert rows["Educational mode"]["body"] == "educational=True, release 7.0"
    assert [a.kind for a in io.actions] == ["raise_popup", "dismiss_popup", "close_all_companies", "quit_tally",
                                            "open_company"]
    assert part["fixtures"] == ["p10_A_popup_read.xml.json", "p10_A_after_popup.xml", "p10_A_no_company_collection.xml",
                                "p10_A_no_company_report.xml", "p10_A_tally_quit.xml.json",
                                "p10_A_educational_licence_info.xml"]


async def test_no_company_answering_with_data_is_different(tmp_path):
    part, _ = await _run(tmp_path, no_company_data=True)
    assert part["outcome"] == "DIFFERENT"
    assert "company list" in part["spec_impact"]


async def test_licensed_tally_marks_educational_not_applicable(tmp_path):
    part, _ = await _run(tmp_path, licence="licensed")
    rows = {row["condition"]: row for row in part["observations"]["gate_table"]}
    assert rows["Educational mode"]["body"] == "not applicable"
    assert "p10_A_educational_licence_info.xml" not in part["fixtures"]


async def test_tally_not_recovering_after_the_popup_blocks_with_cleanup(tmp_path):
    part, _ = await _run(tmp_path, stuck_after_popup=True)
    assert part["outcome"] == "BLOCKED"
    assert part["observations"]["cleanup_needed"] == [p10.POPUP_NOTE.format(company=COMPANY_A)]


# --- M9: the operator records whether the duplicate create actually raised the popup ----------------------------


async def _run_with_auto_operator(tmp_path, books):
    op = build_auto_operator(config=tmp_config(tmp_path), transport=books.transport(),
                             runner=FakeRunner(books, [TallyProcess(8, OWN_COMMAND)]), echo=lambda line: None)
    store = ResultsStore(tmp_path / "r.json")
    ready_store(store, licence="licensed")
    await run_probe(p10.PROBE, labels=None, client=TallyClient(transport=books.transport()), store=store,
                    capture=Capture(tmp_path / "fixtures"), io=op)
    return store.probe_entry(10)["parts"]["A"]


async def test_auto_mode_popup_actually_raised_keeps_the_back_off_gate_action(tmp_path):
    books = FakeBooks(name=COMPANY_A)   # default seed: "Electronics" already exists, so the duplicate create times out
    part = await _run_with_auto_operator(tmp_path, books)
    rows = {row["condition"]: row for row in part["observations"]["gate_table"]}
    assert part["observations"]["popup_raised"] is True
    assert rows["popup / modal open"]["transport"] == "timeout"
    assert rows["popup / modal open"]["gate_action"] == \
        "back off; tell the user to check Tally for an open popup (LESSONS §15 rule 10)"


async def test_auto_mode_popup_not_raised_says_so_in_the_gate_table(tmp_path):
    books = FakeBooks(name=COMPANY_A)
    books._memory["stock_groups"] = []   # "Electronics" doesn't exist yet: the duplicate create succeeds, no popup
    part = await _run_with_auto_operator(tmp_path, books)
    rows = {row["condition"]: row for row in part["observations"]["gate_table"]}
    assert part["observations"]["popup_raised"] is False
    assert rows["popup / modal open"]["transport"] == "ok"
    assert rows["popup / modal open"]["gate_action"] == "popup not raised"


# --- M12: the popup row's gate action follows popup_raised, not the timeout alone ------------------------------------


async def test_timeout_with_no_popup_raised_does_not_tell_the_user_to_look_for_a_popup(tmp_path):
    """The duplicate create succeeded (popup_raised False) but the read still timed out: the row that gets
    transcribed into the spec must not claim a popup is open."""
    part, _ = await _run(tmp_path, popup_raised=False)
    rows = {row["condition"]: row for row in part["observations"]["gate_table"]}
    assert part["observations"]["popup_raised"] is False
    assert rows["popup / modal open"]["transport"] == "timeout"
    assert "check Tally for an open popup" not in rows["popup / modal open"]["gate_action"]
    assert rows["popup / modal open"]["gate_action"] == (
        "back off; but no popup was raised (the duplicate create succeeded), so this row says nothing about "
        "popups — the read timed out for another reason")


async def test_timeout_with_a_popup_raised_keeps_the_popup_wording(tmp_path):
    part, _ = await _run(tmp_path, popup_raised=True)
    rows = {row["condition"]: row for row in part["observations"]["gate_table"]}
    assert rows["popup / modal open"]["transport"] == "timeout"
    assert rows["popup / modal open"]["gate_action"] == \
        "back off; tell the user to check Tally for an open popup (LESSONS §15 rule 10)"


# --- M7 (retro): a "popup not raised" run created the stock group — leave a cleanup note -----------------------------


async def test_popup_not_raised_records_stock_group_cleanup_note(tmp_path):
    part, _ = await _run(tmp_path, popup_raised=False)
    note = "delete stock group 'Electronics' created by probe 10"
    assert note in part["observations"]["cleanup_needed"][0]


async def test_popup_raised_records_no_stock_group_cleanup_note(tmp_path):
    part, _ = await _run(tmp_path, popup_raised=True)
    assert "cleanup_needed" not in part["observations"]
