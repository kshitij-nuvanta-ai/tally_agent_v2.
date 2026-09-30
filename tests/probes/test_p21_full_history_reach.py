import json
import re
import xml.etree.ElementTree as ET

from probes import p21_full_history_reach as p21
from probes.reads import parse_vouchers

HINDI_BLOCK = ('<VOUCHER VCHTYPE="Sales"><DATE>20230601</DATE><GUID>g-0001</GUID>'
               "<NARRATION>[S0-B:281] Sale to शर्मा ट्रेडर्स&#4;</NARRATION>"
               "<ALLLEDGERENTRIES.LIST><LEDGERNAME>शर्मा ट्रेडर्स</LEDGERNAME><AMOUNT>-11.80</AMOUNT>"
               "<BILLALLOCATIONS.LIST><NAME>Inv/281</NAME><BILLTYPE>New Ref</BILLTYPE><AMOUNT>-11.80</AMOUNT>"
               "</BILLALLOCATIONS.LIST></ALLLEDGERENTRIES.LIST>"
               "<ALLLEDGERENTRIES.LIST><LEDGERNAME>Domestic Sales</LEDGERNAME><AMOUNT>10.00</AMOUNT>"
               "<BILLALLOCATIONS.LIST>  </BILLALLOCATIONS.LIST></ALLLEDGERENTRIES.LIST>"
               "<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>USB Cable</STOCKITEMNAME><ACTUALQTY> 1 Nos</ACTUALQTY>"
               "<RATE>10.00/Nos</RATE><AMOUNT>10.00</AMOUNT></ALLINVENTORYENTRIES.LIST>"
               "<REFERENCE></REFERENCE></VOUCHER>")


def _response(*blocks: str) -> str:
    return ("<ENVELOPE><BODY><DESC><CMPINFO><VOUCHER>0</VOUCHER></CMPINFO></DESC><DATA><COLLECTION>"
            + "".join(blocks) + "</COLLECTION></DATA></BODY></ENVELOPE>")


def test_voucher_blocks_skip_the_cmpinfo_counter_and_count_utf8_bytes():
    blocks = p21.voucher_blocks(_response(HINDI_BLOCK, HINDI_BLOCK.replace("281", "282")))
    assert len(blocks) == 2 and blocks[0] == HINDI_BLOCK
    assert p21.xml_bytes(HINDI_BLOCK) == len(HINDI_BLOCK.encode("utf-8")) > len(HINDI_BLOCK)


def test_element_json_drops_placeholders_keeps_lists_and_survives_control_chars():
    from agent.tally.xml_utils import sanitize_xml
    doc = p21.element_json(ET.fromstring(sanitize_xml(HINDI_BLOCK)))
    assert doc["@"] == {"VCHTYPE": "Sales"}
    assert doc["NARRATION"] == "[S0-B:281] Sale to शर्मा ट्रेडर्स"
    assert "REFERENCE" not in doc                                                  # empty scalar dropped
    assert len(doc["ALLLEDGERENTRIES.LIST"]) == 2
    assert "BILLALLOCATIONS.LIST" not in doc["ALLLEDGERENTRIES.LIST"][1]           # empty placeholder dropped
    assert doc["ALLLEDGERENTRIES.LIST"][0]["BILLALLOCATIONS.LIST"][0]["NAME"] == "Inv/281"


def test_raw_json_bytes_is_the_compact_utf8_json():
    from agent.tally.xml_utils import sanitize_xml
    expected = json.dumps(p21.element_json(ET.fromstring(sanitize_xml(HINDI_BLOCK))), ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    assert p21.raw_json_bytes(HINDI_BLOCK) == len(expected)


def test_column_bytes_counts_rows_and_sizes_referenced_guids_like_the_vouchers_own():
    short = parse_vouchers(HINDI_BLOCK)[0]
    longer = parse_vouchers(HINDI_BLOCK.replace("<GUID>g-0001</GUID>", "<GUID>g-0001-0123456789</GUID>"))[0]
    (short_bytes, rows), (long_bytes, _) = p21.column_bytes(short), p21.column_bytes(longer)
    assert rows == 5                                  # voucher + 2 ledger lines + 1 inventory line + 1 bill
    assert short_bytes > rows * p21.PG_ROW_OVERHEAD_BYTES
    # Growing the voucher's own GUID by 11 chars grows every GUID-sized reference by 11 bytes too (P5 ruling: each
    # child row also carries a voucher_id column, sized like the voucher's own GUID — Part 1 spec §5 L493-495).
    # Header row: the voucher's own GUID + voucher_type_guid + party_ledger_guid = 3 occurrences.
    # 2 ledger-line rows: each carries ledger_guid + voucher_id = 2 occurrences per row = 4.
    # 1 inventory row: stock_item_guid + voucher_id = 2 occurrences.
    # 1 bill row: (bill) ledger_guid + voucher_id = 2 occurrences.
    # Total = 3 + 4 + 2 + 2 = 11 occurrences, each growing by 11 bytes.
    assert long_bytes - short_bytes == 11 * 11


def test_measure_groups_by_kind_rounds_half_up_and_ignores_unlabelled_tags():
    blocks = {281: HINDI_BLOCK, 282: HINDI_BLOCK.replace("281", "282"), 999: HINDI_BLOCK}
    stats = p21.measure(blocks, {281: "sales+inventory+bills", 282: "sales+inventory+bills"})
    s = stats["sales+inventory+bills"]
    assert list(stats) == ["sales+inventory+bills"] and s["count"] == 2
    assert s["xml_bytes"] == 2 * p21.xml_bytes(HINDI_BLOCK)          # 281 and 282 have the same length
    assert s["xml_per_voucher"] == p21.xml_bytes(HINDI_BLOCK)
    assert s["rows_per_voucher"] == "5.00"


def test_storage_table_from_hand_numbers():
    stats = {"k": {"count": 2, "xml_bytes": 2000, "json_bytes": 1200, "column_bytes": 800, "rows": 10}}
    out = p21.storage_table(stats)
    assert out["per_voucher_bytes"] == {"xml": "1000.0", "raw": "600.0", "columns": "400.0"}
    rows = {(r["vouchers_per_year"], r["years"]): r for r in out["table"]}
    assert len(rows) == 9
    assert rows[(10_000, 2)] == {"vouchers_per_year": 10_000, "years": 2, "vouchers": 20_000, "with_raw_mb": "20.0",
                                 "without_raw_mb": "8.0", "raw_recent_2_fy_only_mb": "20.0"}
    assert rows[(10_000, 5)]["raw_recent_2_fy_only_mb"] == "32.0"          # 50k×400 + 20k×600
    assert rows[(200_000, 10)]["with_raw_mb"] == "2000.0"
    assert out["q22"]["raw_share_pct"] == "60.0"
    assert out["q22"]["per_fy_mb"]["50000"] == {"with_raw": "50.0", "without_raw": "20.0"}
    assert out["q22"]["saving_if_raw_dropped_beyond_2_fy_mb"]["10000"] == {"5": "18.0", "10": "48.0"}


def test_storage_caveats_flag_unmodelled_jsonb_overhead_and_guid_sized_fks():
    stats = {"k": {"count": 2, "xml_bytes": 2000, "json_bytes": 1200, "column_bytes": 800, "rows": 10}}
    caveats = p21.storage_table(stats)["q23"]["caveats"]
    assert caveats == [
        "indexes excluded",
        "JSONB binary overhead and TOAST compression (values over ~2 kB) are not modelled, so `raw` is an "
        "estimate in either direction",
        "child-row voucher_id and referenced GUIDs are sized as GUID strings — an upper bound if S1 uses "
        "bigint FKs",
        "company B's mix: one stock line per invoice — scale with block_bytes",
    ]


def test_headline_calls_the_storage_figures_upper_bounds():
    stats = {"k": {"count": 2, "xml_bytes": 2000, "json_bytes": 1200, "column_bytes": 800, "rows": 10}}
    storage = p21.storage_table(stats)
    assert "upper bounds" in p21._headline(storage)


def test_month_chunks_flag_volumes_over_the_chunk_cap():
    stats = {"k": {"count": 1, "xml_bytes": 1000, "json_bytes": 600, "column_bytes": 400, "rows": 5}}
    chunks = {c["vouchers_per_year"]: c for c in p21.storage_table(stats)["q23"]["month_chunks"]}
    assert chunks[10_000] == {"vouchers_per_year": 10_000, "vouchers_per_month": 834, "month_xml_mb": "0.8",
                              "over_chunk_cap": False}
    assert chunks[50_000]["vouchers_per_month"] == 4167 and not chunks[50_000]["over_chunk_cap"]
    assert chunks[200_000]["vouchers_per_month"] == 16667 and chunks[200_000]["over_chunk_cap"]


def test_mean_block_bytes_ignores_empty_placeholders():
    blocks = [HINDI_BLOCK]
    bill = ("<BILLALLOCATIONS.LIST><NAME>Inv/281</NAME><BILLTYPE>New Ref</BILLTYPE><AMOUNT>-11.80</AMOUNT>"
            "</BILLALLOCATIONS.LIST>")
    assert p21.mean_block_bytes(blocks, "BILLALLOCATIONS.LIST") == len(bill.encode("utf-8"))
    assert p21.mean_block_bytes([], "BILLALLOCATIONS.LIST") == 0


import pytest

from agent.tally.client import TallyClient
from probes import p05_voucher_month_bounds as p05
from probes.capture import TIMING_NOTE, Capture
from probes.companies import COMPANIES
from probes.results import ResultsStore
from probes.runner import run_probe
from tests.probes.fake_books import FakeBooks, seed_company_b
from tests.probes.fakes import ScriptedIO, ready_store

B = COMPANIES["B"]
KINDS = {"sales+inventory+bills", "purchase+inventory+bills", "receipt+bills", "payment+bills", "sales+inventory",
         "payment", "receipt", "sales"}          # "sales": plan part 7's USD export sales 101/102 (no stock, no bills)


def _books(licence: str = "educational") -> FakeBooks:
    books = FakeBooks(name=B, educational=licence == "educational")     # C43: the fake's edition matches the books
    seed_company_b(books, licence)
    return books


async def _run(tmp_path, books, io=None, *, with_probe_5=True, licence="educational"):
    store = ResultsStore(tmp_path / "results.json")
    ready_store(store, licence=licence)
    store.update_environment(company_b_loaded_at="2026-09-24T13:02:33+05:30")
    client, capture = TallyClient(transport=books.transport()), Capture(tmp_path / "fixtures")
    if with_probe_5:
        await run_probe(p05.PROBE, labels=None, client=client, store=store, capture=capture, io=ScriptedIO())
    io = io or ScriptedIO(answers=[""])
    await run_probe(p21.PROBE, labels=None, client=client, store=store, capture=capture, io=io)
    return store.probe_entry(21)["parts"]["B"], io


async def test_fy2022_is_reached_month_by_month_and_sizes_are_measured(tmp_path):
    part, _ = await _run(tmp_path, _books())
    assert part["outcome"] == "CONFIRMED", part["summary"]
    obs = part["observations"]
    assert obs["books_from"] == {"exported": "20220401", "expected": "01-04-2022", "match": True}
    assert len(obs["months"]) == 12 and all(m["match"] for m in obs["months"].values())
    assert obs["months"]["fy2022_month_09"]["expected_written"] == 20                     # plan part 7: 101/102 written with forex (was C36-skipped)
    assert obs["months"]["fy2022_month_02"]["flagged_returned"] == [201, 202]
    assert set(obs["kinds"]) == KINDS
    assert sum(k["count"] for k in obs["kinds"].values()) == 238                          # 240 minus the cancelled pair (plan part 7: 101/102 written with forex (was C36-skipped))
    assert set(obs["block_bytes"]) == {"ALLLEDGERENTRIES.LIST", "ALLINVENTORYENTRIES.LIST", "BILLALLOCATIONS.LIST"}
    assert len(obs["storage"]["table"]) == 9 and obs["storage"]["mix_vouchers"] == 238
    assert set(obs["storage"]["q22"]) == {"raw_share_pct", "per_fy_mb", "saving_if_raw_dropped_beyond_2_fy_mb"}
    assert obs["timings_ms"]["note"] == TIMING_NOTE and "fy2022_month_04" in obs["timings_ms"]
    assert obs["current_fy_sample"]["month"]["match"]
    assert obs["period_lock"] == {"status": "not attempted", "answer": ""}
    assert "⏭" in part["summary"] and "Q22" in part["spec_impact"]
    assert part["fixtures"][:2] == ["p21_B_books_from.xml", "p21_B_fy2022_month_04.xml"]
    assert "p21_B_fy2022_month_03.xml" in part["fixtures"] and "p21_B_fy2025_month_03.xml" in part["fixtures"]


async def test_a_hand_entered_voucher_makes_its_month_drift_and_blocks(tmp_path):
    """I1: a hand-entered voucher inside a bounded, otherwise-complete FY2022 month is books drift (company B no
    longer matches its generated dataset) — not a request/reach failure, so it must never read FAILED/REACH_IMPACT."""
    books = _books()
    books.edit_state(lambda s: s["vouchers"].__setitem__("99999", {
        "narration": "typed in by hand", "date": "20220815", "post_dated": "No", "cancelled": "No",
        "optional": "No", "vch_type": "Journal", "lines": [], "inventory": [], "bills": []}))
    part, _ = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED", part["summary"]
    assert part["observations"]["months"]["fy2022_month_08"]["drifted"] is True
    assert part["observations"]["months"]["fy2022_month_08"]["untagged"] == 1
    assert "fy2022_month_08" in part["summary"] and "untagged 1" in part["summary"]


async def test_a_duplicate_tagged_voucher_in_fy2022_drifts_and_blocks(tmp_path):
    books = _books()

    def duplicate_one(s: dict) -> None:
        mid, v = next((m, v) for m, v in s["vouchers"].items() if v["date"].startswith("202208"))
        s["vouchers"]["99999"] = dict(v)

    books.edit_state(duplicate_one)
    part, _ = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED", part["summary"]
    assert part["observations"]["months"]["fy2022_month_08"]["drifted"] is True
    assert part["observations"]["months"]["fy2022_month_08"]["duplicates"]
    assert "fy2022_month_08" in part["summary"] and "duplicates" in part["summary"]


async def test_a_missing_tag_inside_a_bounded_month_is_still_a_reach_failure(tmp_path):
    """I1's other half: a tag the dataset expects but that never comes back (not merely extra baggage) is a real
    reach failure of the request under test, and stays FAILED with REACH_IMPACT."""
    books = _books()

    def drop_one(s: dict) -> None:
        mid = next(m for m, v in s["vouchers"].items() if v["date"].startswith("202208"))
        del s["vouchers"][mid]

    books.edit_state(drop_one)
    part, _ = await _run(tmp_path, books)
    assert part["outcome"] == "FAILED"
    assert part["observations"]["months"]["fy2022_month_08"]["missing"]
    assert not part["observations"]["months"]["fy2022_month_08"]["drifted"]
    assert "fy2022_month_08" in part["summary"] and "Decision 7b" in part["spec_impact"]


async def test_books_from_other_than_2022_is_different(tmp_path):
    books = _books()
    books.edit_state(lambda s: s.update(books_from="20230401"))
    part, _ = await _run(tmp_path, books)
    assert part["outcome"] == "DIFFERENT"
    assert "BooksFrom" in part["summary"] and "books-beginning" in part["spec_impact"]


async def test_a_timeout_mid_year_blocks_with_the_popup_hint_and_is_not_retried(tmp_path):
    books = _books()
    books.before_request = lambda body: setattr(books, "popup", True) if ">01-10-2022<" in body else None
    part, _ = await _run(tmp_path, books)
    assert part["outcome"] == "BLOCKED" and "popup" in part["summary"]
    assert "p21_B_fy2022_month_09.xml" in part["fixtures"]
    assert "p21_B_fy2022_month_10.xml.json" in part["fixtures"]
    assert sum(">01-10-2022<" in body for body in books.requests) == 1


async def test_a_locked_period_is_read_again_and_then_unlocked(tmp_path):
    part, io = await _run(tmp_path, _books(), ScriptedIO(answers=["locked"]))
    lock = part["observations"]["period_lock"]
    assert lock["status"] == "locked" and lock["read"]["match"] and lock["same_as_unlocked"]
    assert "p21_B_period_locked_read.xml" in part["fixtures"]
    assert any("Unlock" in w for w in io.waits)
    assert "cleanup_needed" not in part["observations"]


async def test_an_edition_without_a_period_lock_is_recorded(tmp_path):
    part, _ = await _run(tmp_path, _books(), ScriptedIO(answers=["none"]))
    assert part["observations"]["period_lock"] == {"status": "no period lock in this edition"}
    assert part["outcome"] == "CONFIRMED"


@pytest.mark.parametrize("io, status", [
    (ScriptedIO(interactive=False), "not attempted (non-interactive)"),
    (ScriptedIO(run_mode="auto"), "not attempted (auto mode: a person must lock the period in the Tally UI)"),
])
async def test_period_lock_is_not_attempted_without_a_person(tmp_path, io, status):
    part, used = await _run(tmp_path, _books(), io)
    assert part["outcome"] == "CONFIRMED", part["summary"]
    assert part["observations"]["period_lock"] == {"status": status}
    assert used.asks == []


async def test_probe_21_blocks_until_probe_5_has_confirmed_the_month_request(tmp_path):
    books = _books()
    part, _ = await _run(tmp_path, books, with_probe_5=False)
    assert part["outcome"] == "BLOCKED" and "probe(s) 5" in part["summary"]
    assert not any("<TYPE>Voucher</TYPE>" in body for body in books.requests)


_DATE_VAR = re.compile(r'<(SVFROMDATE|SVTODATE) TYPE="Date">(\d\d)-(\d\d)-(\d{4})</')


def _p21_voucher_date_vars(books: FakeBooks) -> list[tuple[str, str]]:
    return [(name, f"{d}-{m}-{y}") for body in books.requests if "S0VoucherMonth" in body
            for name, d, m, y in _DATE_VAR.findall(body)]


async def test_c43_every_educational_month_request_ends_on_an_allowed_day(tmp_path):
    """C43: an educational Tally ignores a date variable off day 1/2/31, so FY 2022-23's 30-day months (and Feb)
    end on the 2nd; the 31-day months end on the 31st; every month is still exact."""
    books = _books()
    part, _ = await _run(tmp_path, books, ScriptedIO(answers=["locked"]))
    assert part["outcome"] == "CONFIRMED", part["summary"]
    sent = _p21_voucher_date_vars(books)
    assert sent and all(int(value[:2]) in (1, 2, 31) for _, value in sent), sent
    to_dates = [value for name, value in sent if name == "SVTODATE"]
    for expected in ("02-04-2022", "31-05-2022", "02-06-2022", "02-02-2023", "31-03-2023", "31-03-2026"):
        assert expected in to_dates, expected
    assert "30-04-2022" not in to_dates and "28-02-2023" not in to_dates
    assert part["observations"]["period_lock"]["read"]["match"]


async def test_c43_a_licensed_run_asks_for_whole_calendar_months(tmp_path):
    books = _books("licensed")
    part, _ = await _run(tmp_path, books, licence="licensed")
    assert part["outcome"] == "CONFIRMED", part["summary"]
    to_dates = [value for name, value in _p21_voucher_date_vars(books) if name == "SVTODATE"]
    assert "30-04-2022" in to_dates and "28-02-2023" in to_dates and "02-04-2022" not in to_dates
