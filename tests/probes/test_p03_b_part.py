from pathlib import Path

from tally_bridge.client import TallyClient
from probes import p03_voucher_ids_flags as p03
from probes import p05_voucher_month_bounds as p05
from probes.capture import Capture
from probes.company_b_view import first_voucher
from probes.companies import COMPANIES
from probes.results import ResultsStore
from probes.runner import run_probe
from tests.probes.fake_books import FakeBooks, seed_company_b
from tests.probes.fakes import ScriptedIO, ready_store

B = COMPANIES["B"]
SYNC = Path(__file__).resolve().parents[1] / "fixtures" / "sync"


def _books(**knobs) -> FakeBooks:
    books = FakeBooks(name=B, educational=True, **knobs)
    seed_company_b(books, "educational")
    return books


def _set(books: FakeBooks, tag: int, key: str, value: str) -> None:
    def edit(state):
        for v in state["vouchers"].values():
            if v["narration"].startswith(f"[S0-B:{tag}]"):
                v[key] = value
    books.edit_state(edit)


async def _run(tmp_path, books, *, with_probe_5=True):
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence="educational")
    store.update_environment(company_b_loaded_at="2026-09-24T13:02:33+05:30")
    client, capture = TallyClient(transport=books.transport(), trust_env=False), Capture(tmp_path / "fixtures")
    if with_probe_5:
        await run_probe(p05.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    await run_probe(p03.PROBE, labels=["B"], client=client, store=store, capture=capture, io=ScriptedIO())
    return store.probe_entry(3)["parts"]["B"]


async def test_flags_on_both_reads_are_confirmed(tmp_path):
    part = await _run(tmp_path, _books())
    assert part["outcome"] == "CONFIRMED", part["summary"]
    obs = part["observations"]
    assert obs["header_flagged"]["201"]["flags_ok"] and obs["header_flagged"]["302"]["flags_ok"]
    assert obs["months"]["2023-07"]["flags"]["301"]["IsOptional"] == "Yes" and obs["others"] == 956   # plan part 7: 101/102 written with forex (was C36-skipped)
    assert part["fixtures"] == ["p03_B_vouchers_flags.xml", "p03_B_flagged_month_2023_02.xml",
                                "p03_B_flagged_month_2023_07.xml"]
    # Ruling S2: CONFIRMED measured "a cancelled one exports no ledger lines", and recorded the empty party name.
    lines = obs["months"]["2023-02"]["lines"]
    assert lines["201"]["ledger_lines"] == 0 and lines["202"]["ledger_lines"] == 0
    assert lines["201"]["party"] == "" and obs["cancelled_party_names"] == {"201": "", "202": ""}


async def test_a_cancelled_voucher_that_exports_ledger_lines_is_different(tmp_path, monkeypatch):
    """Ruling S2: the CONFIRMED impact says a cancelled voucher exports no ledger lines -- if the month request
    brings lines back for one, that is a different shape S1 must handle, not a confirmation."""
    real = p03.month_flag_rows

    def with_lines(raw):
        rows = real(raw)
        if 201 in rows:
            rows[201].update(ledger_lines=2, amounts=2, party="Some Debtor")
        return rows
    monkeypatch.setattr(p03, "month_flag_rows", with_lines)
    part = await _run(tmp_path, _books())
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert "201" in part["summary"] and "ledger lines" in part["summary"] and "IsCancelled" in part["spec_impact"]


EXTRACTOR_NEVER = "The extractor's request never returns"


async def test_optional_vouchers_missing_from_both_reads_is_different(tmp_path):
    """Review I3: both reads omit them -> the extractor impact, and the header omission is named alongside."""
    part = await _run(tmp_path, _books(optional_vouchers_listed=False))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert "optional" in part["summary"] and EXTRACTOR_NEVER in part["spec_impact"]
    assert "month request" in part["summary"] and "header read" in part["summary"]
    assert p03.B_HEADER_ONLY_IMPACT.split("{")[0] not in part["spec_impact"]
    obs = part["observations"]
    assert obs["months"]["2023-07"]["flags"]["301"]["returned"] is False
    assert obs["unlisted_month"] == ["301", "302"] and obs["unlisted_header"] == ["301", "302"]


async def test_optional_vouchers_missing_from_the_month_request_only_is_different(tmp_path, monkeypatch):
    """Review I3: only the extractor's request omits them -> the extractor impact; the header read is not blamed."""
    real = p03.month_flag_rows
    monkeypatch.setattr(p03, "month_flag_rows",
                        lambda raw: {t: r for t, r in real(raw).items() if t not in (301, 302)})
    part = await _run(tmp_path, _books())
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert "optional" in part["summary"] and EXTRACTOR_NEVER in part["spec_impact"]
    assert "header read" not in part["summary"]
    obs = part["observations"]
    assert obs["unlisted_month"] == ["301", "302"] and obs["unlisted_header"] == []


async def test_flagged_vouchers_missing_from_the_header_read_only_is_its_own_different(tmp_path):
    """Review I3: 3 B's own header collection omits them but probe 5's month request returns them with their flags
    -> DIFFERENT with its own impact, never "the extractor's request never returns them"."""
    part = await _run(tmp_path, _books(header_lists_flagged=False))
    assert part["outcome"] == "DIFFERENT", part["summary"]
    assert EXTRACTOR_NEVER not in part["spec_impact"]
    assert p03.B_HEADER_ONLY_IMPACT.split("{")[0] in part["spec_impact"]
    assert "header read only" in part["summary"] and "month request returns them" in part["summary"]
    obs = part["observations"]
    assert obs["unlisted_header"] == ["201", "202", "301", "302"] and obs["unlisted_month"] == []
    assert obs["months"]["2023-07"]["flags"]["301"]["flags_ok"]


async def test_a_flag_that_did_not_stick_fails(tmp_path):
    books = _books()
    _set(books, 301, "optional", "No")
    part = await _run(tmp_path, books)
    assert part["outcome"] == "FAILED" and "301" in part["summary"] and "R16" in part["spec_impact"]


async def test_a_flag_the_dataset_does_not_set_blocks_as_drift(tmp_path):
    books = _books()
    _set(books, first_voucher("educational", lambda v: True).tag, "cancelled", "Yes")
    part = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "drifted" in part["summary"]


async def test_an_untagged_voucher_blocks_as_drift(tmp_path):
    books = _books()
    books.edit_state(lambda s: s["vouchers"].__setitem__("99999", {
        "narration": "typed by hand", "date": "20240101", "post_dated": "No", "cancelled": "No", "optional": "No",
        "vch_type": "Journal", "lines": [], "inventory": [], "bills": []}))
    part = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "untagged 1" in part["summary"]


async def test_without_probe_5_the_b_part_blocks(tmp_path):
    part = await _run(tmp_path, _books(), with_probe_5=False)
    assert part["outcome"] == "BLOCKED" and "probe 5" in part["summary"]


def test_probe_21s_capture_already_shows_the_cancelled_pair_flagged():
    """S0-D7: probe 21 saw 201/202 come back (recorded, not judged). 3 B's judging code on those exact live bytes."""
    rows = p03.month_flag_rows((SYNC / "p21_B_fy2022_month_02.xml").read_text(encoding="utf-8"))
    readings = p03.flag_readings(rows, {201, 202}, frozenset({201, 202}))
    assert readings["201"]["flags_ok"] and readings["202"]["flags_ok"]
    assert rows[201]["ledger_lines"] == 0          # live: a cancelled voucher exports no ledger lines
    assert rows[201]["party"] == "" and rows[202]["party"] == ""    # live: and an empty PARTYLEDGERNAME (S2)
