"""The Task 12 real-capture assembler (plan ambiguity A4, Review Focus 1: it never fills a value no capture holds).

Rulings applied: F13 -- ``synthetic_ids`` is keyed by ``(kind, name)`` (group ``Capital Account`` and ledger
``Capital Account`` collide on name alone); a master with a real GUID but no AlterID in any capture keeps its GUID and
gets the synthetic AlterID ``1``, and is marked ``G4`` too.
"""
from __future__ import annotations

from v2.tests.cloud.realdata import (B_FY2022_MONTHS, CAPTURES_A, assemble_a, assemble_b_fy2022, masters,
                                     synthetic_identity, vouchers)


def test_assembler_never_fills_a_value_no_capture_holds():
    a = assemble_a()
    for v in a.vouchers:
        d = v["data"]
        for key in d:
            if key in ("ledger_entries", "inventory_entries"):
                continue
            assert a.sources.get(key), f"{key} has no source capture"
    assert all(set(a.sources[k]) <= set(CAPTURES_A) for k in a.sources)


def test_assembler_master_keys_all_come_from_captures_except_g4_identity():
    """Masters too: every key of every master is in some capture's answer for that master, except a G4 identity."""
    for assembled in (assemble_a(), assemble_b_fy2022()):
        for m in assembled.masters:
            for key in m["data"]:
                if key in ("guid", "alterid") and (m["kind"], m["data"]["name"]) in assembled.synthetic_ids:
                    continue
                assert assembled.sources.get(f"{m['kind']}.{key}"), (m["kind"], m["data"]["name"], key)


def test_assembler_marks_synthetic_identities_by_gap_id():
    b = assemble_b_fy2022()
    assert set(b.synthetic_ids.values()) <= {"G4"}
    assert b.synthetic_ids                                  # B's groups and stock items have no ids in any capture
    for m in b.masters:
        key = (m["kind"], m["data"]["name"])
        if key not in b.synthetic_ids:
            assert "-fx-" not in m["data"]["guid"], key
            continue
        synthetic_guid, synthetic_alter = synthetic_identity(b.company_guid, m["kind"], m["data"]["name"])
        if "-fx-" in m["data"]["guid"]:
            assert (m["data"]["guid"], m["data"]["alterid"]) == (synthetic_guid, synthetic_alter)
        else:                                               # F13: real GUID kept, AlterID alone synthesised
            assert m["data"]["alterid"] == synthetic_alter, key


def test_group_and_ledger_capital_account_are_separate_synthetic_keys():
    """F13: the (kind, name) key -- the group is synthetic (no B group GUIDs), the ledger's GUID is real."""
    b = assemble_b_fy2022()
    assert b.synthetic_ids.get(("group", "Capital Account")) == "G4"
    ledger = next(m["data"] for m in b.masters if m["kind"] == "ledger" and m["data"]["name"] == "Capital Account")
    real = {o["data"]["name"]: o["data"]["guid"] for o in masters("p16_B_ledgers.xml", "ledger")}
    assert ledger["guid"] == real["Capital Account"] and "-fx-" not in ledger["guid"]


def test_company_a_has_50_vouchers_with_alterid_and_flags_joined():
    a = assemble_a()
    assert len(a.vouchers) == 50 and all({"alterid", "iscancelled", "isoptional", "ispostdated"} <= set(v["data"])
                                         for v in a.vouchers)


def test_company_a_join_takes_alterid_from_p04_and_flags_from_p16_by_guid():
    a = assemble_a()
    assert a.sources["alterid"] == ["p04_A_voucher_full.xml"]
    assert {k: a.sources[k] for k in ("iscancelled", "isoptional", "ispostdated")} == \
        {k: ["p16_A_vouchers_fy.xml"] for k in ("iscancelled", "isoptional", "ispostdated")}
    p04 = {v["data"]["guid"]: v["data"]["alterid"] for v in vouchers("p04_A_voucher_full.xml")}
    assert all(v["data"]["alterid"] == p04[v["data"]["guid"]] for v in a.vouchers)
    p06 = {v["data"]["guid"]: v["data"]["ledger_entries"] for v in vouchers("p06_A_vouchers_nested.xml")}
    assert all(v["data"]["ledger_entries"] == p06[v["data"]["guid"]] for v in a.vouchers)


def test_company_b_fy2022_has_240_vouchers_incl_two_usd():             # exit gate note: FY 2022-23 now 240
    b = assemble_b_fy2022()
    # The twelve p21_B_fy2022_month_* captures sum to 240 (checked here, not assumed).
    assert sum(len(vouchers(f)) for f in B_FY2022_MONTHS) == 240
    assert len(b.vouchers) == 240
    assert sum(1 for v in b.vouchers if any("@" in e["amount"] for e in v["data"]["ledger_entries"])) == 2


def test_books_from_and_company_guid_come_from_the_counters_captures():
    a, b = assemble_a(), assemble_b_fy2022()
    assert (a.books_from.isoformat(), b.books_from.isoformat()) == ("2025-04-01", "2022-04-01")
    assert a.sources["booksfrom"] == ["s1_A_counters.xml"] and b.sources["booksfrom"] == ["s1_B_counters.xml"]
