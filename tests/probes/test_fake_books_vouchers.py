from tally_bridge.envelopes import COMPANY_PLACEHOLDER
from probes.companies import COMPANIES
from probes.p01_company_counters import COUNTERS_REQUEST
from probes.reads import (FROM_PLACEHOLDER, TO_PLACEHOLDER, VOUCHER_MONTH_FIELDS, fill_month_request,
                             parse_vouchers, untyped_period_vars, voucher_request)
from tally_bridge.xml_utils import read_objects
from tests.probes.fake_books import FakeBooks, seed_company_b, sync_client

B = COMPANIES["B"]
TEMPLATE = voucher_request("S0VoucherMonth", VOUCHER_MONTH_FIELDS, COMPANY_PLACEHOLDER,
                           from_date=FROM_PLACEHOLDER, to_date=TO_PLACEHOLDER)


def _post(books: FakeBooks, xml: str) -> str:
    with sync_client(books.transport()) as client:
        return client.post("/", content=xml.encode("utf-8")).text


def _seeded(licence="educational") -> FakeBooks:
    books = FakeBooks(name=B)
    seed_company_b(books, licence)
    return books


def test_seeded_company_b_holds_every_written_voucher_with_its_flags():
    state = _seeded().state
    by_tag = {v["narration"].split("]")[0] + "]": v for v in state["vouchers"].values()}
    assert len(state["vouchers"]) == 960                              # plan part 7: 101/102 written with forex (was C36-skipped)
    assert by_tag["[S0-B:101]"]["lines"][0]["amount_text"] == "-$448.44 @ ? 82.99/$ = -? 37216.04"
    assert by_tag["[S0-B:201]"]["cancelled"] == "Yes" and by_tag["[S0-B:302]"]["optional"] == "Yes"
    assert by_tag["[S0-B:1]"]["cancelled"] == by_tag["[S0-B:1]"]["optional"] == "No"
    assert state["books_from"] == "20220401"


def test_a_typed_month_returns_exactly_that_month_in_full():
    # C43: an educational fake honours only day 1/2/31 period variables, so June is bounded by 01-06..02-06.
    vouchers = parse_vouchers(_post(_seeded(), fill_month_request(TEMPLATE, B, "01-06-2023", "02-06-2023")))
    assert len(vouchers) == 20
    assert {v["header"]["DATE"][:6] for v in vouchers} == {"202306"}
    sale = next(v for v in vouchers if v["header"]["NARRATION"].startswith("[S0-B:281]"))
    assert sale["header"]["VOUCHERTYPENAME"] in ("Sales", "Sales - GST") and sale["header"]["GUID"]
    assert sale["ledger_lines"][0]["bills"][0]["BILLTYPE"] == "New Ref"
    assert len(sale["inventory"]) == 1


def test_an_untyped_window_silently_answers_for_the_current_period():
    xml = untyped_period_vars(fill_month_request(TEMPLATE, B, "01-06-2023", "30-06-2023"))
    dates = sorted(v["header"]["DATE"] for v in parse_vouchers(_post(_seeded(), xml)))
    assert len(dates) == 240 and (dates[0], dates[-1]) == ("20250401", "20260331")


def _dates(books: FakeBooks, start: str, end: str) -> list[str]:
    return sorted(v["header"]["DATE"] for v in parse_vouchers(_post(books, fill_month_request(TEMPLATE, B, start, end))))


def test_c43_educational_ignores_a_typed_to_date_on_the_30th_like_live_run_1():
    """C43 (live 2026-09-24, run 1): typed 01-06-2023..30-06-2023 on Educational Tally answered 680 vouchers
    2023-06-01..2026-03-31 — the from-date honoured, the 30th silently replaced by the current period's end."""
    dates = _dates(_seeded(), "01-06-2023", "30-06-2023")
    assert len(dates) == 680 and (dates[0], dates[-1]) == ("20230601", "20260331")


def test_c43_educational_honours_a_to_date_on_the_2nd_or_31st():
    assert len(_dates(_seeded(), "01-06-2023", "02-06-2023")) == 20
    july = _dates(_seeded(), "01-07-2023", "31-07-2023")
    assert len(july) == 20 and (july[0], july[-1]) == ("20230701", "20230731")
    assert len(_dates(_seeded(), "01-07-2023", "30-07-2023")) == 660
    assert _dates(_seeded(), "01-06-2023", "01-06-2023") == ["20230601"] * 10


def test_c43_educational_ignores_a_typed_from_date_off_1_2_31_too():
    dates = _dates(_seeded(), "15-06-2023", "31-03-2026")          # from falls back to the current period's start
    assert len(dates) == 240 and (dates[0], dates[-1]) == ("20250401", "20260331")


def test_c43_a_licensed_fake_honours_any_valid_typed_date():
    books = FakeBooks(name=B, educational=False)
    seed_company_b(books, "licensed")
    dates = _dates(books, "01-06-2023", "30-06-2023")
    assert len(dates) == 20 and dates[0] >= "20230601" and dates[-1] <= "20230630"
    assert all("20230603" <= d <= "20230630" for d in _dates(books, "03-06-2023", "30-06-2023"))


def test_counters_report_books_from_from_state():
    def books_from(books):
        return read_objects(_post(books, COUNTERS_REQUEST), "COMPANY", ["BooksFrom"])[0]["BooksFrom"]
    assert books_from(FakeBooks()) == "20250401"
    assert books_from(_seeded()) == "20220401"
