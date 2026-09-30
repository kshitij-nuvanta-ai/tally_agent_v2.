"""Task 2: the C43 guard sits in ProbeContext, so no probe (old constant or new code) can send an off-day date to an
Educational Tally and read the silent current-period fallback as data."""
from tally_bridge.envelopes import wrap_report
from probes.core import Outcome, PartResult, Probe
from probes.runner import run_probe
from tests.probes.fakes import ScriptedIO, a_tally, make_harness, ready_store, tb_xml


def _probe() -> Probe:
    async def part(ctx):
        await ctx.send("tb", wrap_report("Trial Balance", "01-04-2025", "30-09-2025", ctx.company_name))
        return PartResult(Outcome.CONFIRMED, "sent")
    return Probe(id=97, name="c43_guard_test", question="?", feeds=("test",), parts={"A": part})


async def _run(tmp_path, licence):
    fake, _ = a_tally()
    fake.route("<ID>Trial Balance</ID>", lambda body: tb_xml([("Capital Account", "", "1.00")]))
    client, store, capture = make_harness(tmp_path, fake)
    ready_store(store, licence=licence)
    await run_probe(_probe(), labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    return fake, store.probe_entry(97)["parts"]["A"]


async def test_an_off_day_date_blocks_the_part_before_anything_is_sent(tmp_path):
    fake, part = await _run(tmp_path, "educational")
    assert part["outcome"] == "BLOCKED" and "SVTODATE=30-09-2025" in part["summary"]
    assert not any("<ID>Trial Balance</ID>" in body for body in fake.requests)


async def test_a_licensed_tally_gets_the_same_request(tmp_path):
    fake, part = await _run(tmp_path, "licensed")
    assert part["outcome"] == "CONFIRMED"
    assert any("<ID>Trial Balance</ID>" in body for body in fake.requests)


# --- Ruling Q4: ONE C43 check (one implementation, one exception type, one date parser) ----------------------------


def test_company_b_view_check_date_vars_is_the_safety_implementation():
    import pytest
    from probes import company_b_view, safety
    assert company_b_view.EDUCATIONAL_DATE_VAR_DAYS is safety.EDUCATIONAL_DATE_VAR_DAYS
    with pytest.raises(safety.GuardError, match="C43"):
        company_b_view.check_date_vars("educational", "30-06-2023")
    with pytest.raises(safety.GuardError, match="30-Jun-2023"):         # one date parser: every format Tally reads
        company_b_view.check_date_vars("educational", "01-06-2023", "30-Jun-2023")
    company_b_view.check_date_vars("educational", "01-06-2023", "02-06-2023", "31-07-2023")
    company_b_view.check_date_vars("licensed", "30-06-2023")


def test_one_switch_turns_off_both_call_sites(monkeypatch):
    """History-reproduction tests bypass the rule once, in one place, for the request-level guard and fetch_window."""
    from probes import company_b_view, safety
    monkeypatch.setattr(safety, "EDUCATIONAL_DATE_VAR_DAYS", tuple(range(1, 32)))
    company_b_view.check_date_vars("educational", "30-06-2023")
    safety.check_educational_dates(wrap_report("Trial Balance", "30-06-2023", "30-06-2023", "Co"), "educational")


# --- Ruling Q5: the anchors send path goes through the C43 guard too ------------------------------------------------


async def _anchor_run(tmp_path, monkeypatch, licence):
    from probes import anchors
    from probes.runner import run_anchor_check
    monkeypatch.setattr(anchors, "ANCHOR_DATE", "30-03-2026")      # an off-day date, as a future edit might pick
    fake, _ = a_tally()
    fake.route("<ID>Bills Receivable</ID>", lambda body: "<ENVELOPE></ENVELOPE>")
    fake.route("<ID>Bills Payable</ID>", lambda body: "<ENVELOPE></ENVELOPE>")
    fake.route("<ID>Trial Balance</ID>", lambda body: tb_xml([("Capital Account", "", "1.00")]))
    client, store, _ = make_harness(tmp_path, fake)
    ready_store(store, baseline={"Capital Account": "1.00"}, licence=licence)
    ok = await run_anchor_check("anchors:before_parity", "A", client=client, store=store, io=ScriptedIO())
    return ok, fake, store.anchor_checks[-1]


async def test_the_anchors_check_refuses_an_off_day_date_on_an_educational_tally(tmp_path, monkeypatch):
    ok, fake, check = await _anchor_run(tmp_path, monkeypatch, "educational")
    assert ok is False and "GuardError" in check["problems"][0] and "SVTODATE=30-03-2026" in check["problems"][0]
    assert not any("<ID>Bills" in body or "<ID>Trial Balance</ID>" in body for body in fake.requests)


async def test_the_anchors_check_sends_the_same_dates_to_a_licensed_tally(tmp_path, monkeypatch):
    _, fake, check = await _anchor_run(tmp_path, monkeypatch, "licensed")
    assert not any("GuardError" in p for p in check["problems"])
    assert any("<ID>Trial Balance</ID>" in body for body in fake.requests)
