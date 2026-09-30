"""Tests for S1 task 8b derivations (spec §4.4, §12 steps 8/10, probe 25). Pure -- no DB, no HTTP."""
from pathlib import Path

from backend.sync.ingest.derive import base_type_walk, descendants, is_base_currency, is_forex_ledger, nature_walk
from contract import parse, transcode

SYNC = Path(__file__).resolve().parents[2] / "fixtures" / "sync"


def _parents(fixture, kind):
    objs = transcode.masters_from_xml((SYNC / fixture).read_text(encoding="utf-8"), kind)
    return {parse.name(o["data"]["name"]): parse.name(o["data"].get("parent", "")) for o in objs}


def test_custom_subgroups_walk_to_nature_company_a():                  # p25_A_groups.xml
    parents = _parents("p25_A_groups.xml", "group")
    assert nature_walk("North Zone Debtors", parents).nature == "assets"
    assert nature_walk("National Creditors", parents).primary_group == "Current Liabilities"
    assert nature_walk("Sales Accounts", parents).nature == "income"


def test_all_company_a_groups_classified():
    parents = _parents("p25_A_groups.xml", "group")
    unmapped = [g for g in parents if nature_walk(g, parents).nature is None]
    assert unmapped == []


# Hand-derived from p18_B_group_list.xml's PARENT chains (spec §4.4 rules + PRIMARY_NATURE), not computed by the
# code under test. Every one of the fixture's 30 GROUP elements is walked and checked against this table.
# "Primary" below stands for the fixture's literal "&#4; Primary" parent (a group that is itself a primary group).
_B_GROUP_EXPECTED: dict[str, tuple[str, str]] = {
    "Bank Accounts": ("Current Assets", "assets"),                    # -> Current Assets(primary) -> assets
    "Bank OD A/c": ("Loans (Liability)", "liabilities"),               # -> Loans (Liability)(primary) -> liabilities
    "Branch / Divisions": ("Branch / Divisions", "liabilities"),       # primary itself
    "Capital Account": ("Capital Account", "liabilities"),             # primary itself
    "Cash-in-Hand": ("Current Assets", "assets"),
    "Current Assets": ("Current Assets", "assets"),                   # primary itself
    "Current Liabilities": ("Current Liabilities", "liabilities"),    # primary itself
    "Deposits (Asset)": ("Current Assets", "assets"),
    "Direct Expenses": ("Direct Expenses", "expenses"),                # primary itself
    "Direct Incomes": ("Direct Incomes", "income"),                    # primary itself
    "Duties & Taxes": ("Current Liabilities", "liabilities"),          # -> Current Liabilities(primary)
    "Fixed Assets": ("Fixed Assets", "assets"),                        # primary itself
    "Indirect Expenses": ("Indirect Expenses", "expenses"),            # primary itself
    "Indirect Incomes": ("Indirect Incomes", "income"),                # primary itself
    "Investments": ("Investments", "assets"),                          # primary itself
    "Loans & Advances (Asset)": ("Current Assets", "assets"),
    "Loans (Liability)": ("Loans (Liability)", "liabilities"),        # primary itself
    "Local Creditors": ("Current Liabilities", "liabilities"),        # -> Sundry Creditors -> Current Liabilities
    "Misc. Expenses (ASSET)": ("Misc. Expenses (ASSET)", "assets"),    # primary itself
    "National Creditors": ("Current Liabilities", "liabilities"),     # -> Sundry Creditors -> Current Liabilities
    "Provisions": ("Current Liabilities", "liabilities"),              # -> Current Liabilities(primary)
    "Purchase Accounts": ("Purchase Accounts", "expenses"),            # primary itself
    "Reserves & Surplus": ("Capital Account", "liabilities"),          # -> Capital Account(primary)
    "Sales Accounts": ("Sales Accounts", "income"),                    # primary itself
    "Secured Loans": ("Loans (Liability)", "liabilities"),             # -> Loans (Liability)(primary)
    "Stock-in-Hand": ("Current Assets", "assets"),
    "Sundry Creditors": ("Current Liabilities", "liabilities"),        # -> Current Liabilities(primary)
    "Sundry Debtors": ("Current Assets", "assets"),                    # -> Current Assets(primary)
    "Suspense A/c": ("Suspense A/c", "liabilities"),                   # primary itself
    "Unsecured Loans": ("Loans (Liability)", "liabilities"),           # -> Loans (Liability)(primary)
}


def test_company_b_group_list_every_group_matches_hand_derived_table():   # p18_B_group_list.xml
    parents = _parents("p18_B_group_list.xml", "group")
    assert set(parents) == set(_B_GROUP_EXPECTED)                      # the table covers every group in the capture
    for group_name, (expected_primary, expected_nature) in _B_GROUP_EXPECTED.items():
        result = nature_walk(group_name, parents)
        assert (result.primary_group, result.nature) == (expected_primary, expected_nature), group_name


def test_b_creditor_subgroup_walks_to_liabilities():                   # p18_B_group_list.xml, real custom sub-group
    parents = _parents("p18_B_group_list.xml", "group")
    result = nature_walk("Local Creditors", parents)
    assert result.primary_group == "Current Liabilities" and result.nature == "liabilities"


# p04_A_group_full.xml carries no <PARENT> tag at all on any of its GROUP elements (only GUID/ALTERID/name) --
# it documents the GUID/ALTERID shape (§4.3) but has nothing to walk, so no derive test reads it.


def test_cycle_and_unmapped_primary_warn():
    assert nature_walk("X", {"X": "Y", "Y": "X"}).warning == "unmapped_primary"
    assert nature_walk("Odd", {"Odd": "Primary"}).nature is None


def test_reserved_name_stripped_matches_primary():                      # D31, Task 1 carry: strip on both sides
    # p25_A_groups.xml's primary groups all carry a literal "&#4; Primary" parent (transcode/parse already
    # strips it to "Primary" via parse.name -- exercised implicitly by every primary group above). This test
    # pins the behaviour directly against the raw stripped text, not just via the fixture helper.
    parents = {"Suspense A/c": parse.name("\u0004 Primary")}
    result = nature_walk("Suspense A/c", parents)
    assert result.nature == "liabilities" and result.warning is None


def test_creditor_subgroup_several_levels_deep():
    # A synthetic deeper chain than the fixture's 3 hops, to prove the walk isn't hardcoded to a fixed depth.
    parents = {
        "Regional Creditors": "National Creditors",
        "National Creditors": "Sundry Creditors",
        "Sundry Creditors": "Current Liabilities",
        "Current Liabilities": "Primary",
    }
    result = nature_walk("Regional Creditors", parents)
    assert result.nature == "liabilities" and result.primary_group == "Current Liabilities"


def test_duplicate_name_under_different_parents_each_classify_independently():
    # Company A has "Local Creditors" and "National Creditors" as distinct groups both named uniquely, but the
    # nature walk must classify any two groups that happen to *share a name* the same way their own parent
    # chain dictates -- group identity here is by dict key (i.e. by GUID upstream), not display name.
    parents_a = {"Regional": "Sundry Creditors", "Sundry Creditors": "Current Liabilities",
                 "Current Liabilities": "Primary"}
    parents_b = {"Regional": "Sundry Debtors", "Sundry Debtors": "Current Assets", "Current Assets": "Primary"}
    assert nature_walk("Regional", parents_a).nature == "liabilities"
    assert nature_walk("Regional", parents_b).nature == "assets"


def test_sales_gst_base_type_company_b():                               # p25_B_voucher_types.xml
    parents = _parents("p25_B_voucher_types.xml", "voucher_type")
    assert base_type_walk("Sales - GST", parents, {}) == "Sales"
    assert base_type_walk("Contra", parents, {}) == "Contra"


def test_all_company_b_reserved_types_self_resolve():
    parents = _parents("p25_B_voucher_types.xml", "voucher_type")
    reserved = [n for n in parents if n != "Sales - GST"]
    for n in reserved:
        assert base_type_walk(n, parents, {}) == n


def test_unresolvable_base_type_is_none():
    assert base_type_walk("Mystery", {"Mystery": "Other"}, {}) is None


def test_base_type_cycle_is_none():
    assert base_type_walk("A", {"A": "B", "B": "A"}, {}) is None


def test_base_type_reserved_field_short_circuits():
    # reserved[name] (the exported RESERVEDNAME, when non-empty) stops the walk even before the hardcoded table.
    assert base_type_walk("Sales - GST", {"Sales - GST": "Sales"}, {"Sales - GST": "Sales"}) == "Sales"


def test_is_forex():
    assert is_forex_ledger("$", "?", False) and not is_forex_ledger("?", "?", False)
    assert not is_forex_ledger(None, "?", False) and is_forex_ledger(None, "?", True)


def test_is_base_currency():
    assert is_base_currency("INR") and not is_base_currency("$")


def test_descendants_for_rederivation():
    parents = {"A": "Sundry Debtors", "B": "A", "C": "B", "D": "Sundry Creditors"}
    assert descendants("A", parents) == {"B", "C"}


def test_descendants_ignores_cycles_not_reaching_target():
    parents = {"A": "Root", "X": "Y", "Y": "X"}
    assert descendants("A", parents) == set()
