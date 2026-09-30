from calendar import monthrange
from datetime import date
import hashlib
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

import pytest

from probes.setup.company_b_data import (
    BillSpec, LineSpec, BASE_UNIT, BOX_UNIT, COMPOUND_UNIT, FORMERLY_SKIPPED_TAGS, FOREX_BASE_SYMBOL, FOREX_FORM,
    HINDI_DEBTOR, NON_BILLWISE_DEBTOR, SALES_GST_VOUCHER_TYPE, USD_CURRENCY, USD_DEBTOR, USD_EXPORT_PARTY,
    expected_figures, generate, gstin,
)

CASH_AND_BANK = ("Cash", "HDFC Bank Current A/c")
EXPENSE_LEDGERS = ("Office Rent", "Bank Charges")


def test_the_same_seed_generates_an_identical_dataset():
    assert generate() == generate()          # frozen dataclasses compare by value


def test_every_voucher_balances_to_zero():
    for v in generate().vouchers:
        total = sum((line.amount for line in v.lines), Decimal("0.00"))
        assert total == Decimal("0.00"), f"{v.tag} {v.kind} does not balance: {total}"


def test_sales_and_purchase_lines_pin_the_op6_op7_sign_convention():
    """F12: a sum-to-zero invariant (the test above) is UNCHANGED under a global sign flip, so it can't tell the
    real convention from its mirror image — that's exactly how the whole dataset ended up sign-inverted against
    the only permutation Tally accepts. This test pins the actual convention instead, per
    docs/tally-write-exploration-v4.md's Op 6 (sales) / Op 7 (purchase, the inverse) table:

        | line                | Sales (Op 6)                          | Purchase (Op 7)                       |
        |---------------------|----------------------------------------|-----------------------------------------|
        | party               | ISDEEMEDPOSITIVE=Yes, AMOUNT=NEGATIVE | ISDEEMEDPOSITIVE=No,  AMOUNT=POSITIVE |
        | nominal/GST         | ISDEEMEDPOSITIVE=No,  AMOUNT=POSITIVE | ISDEEMEDPOSITIVE=Yes, AMOUNT=NEGATIVE |

    Op 7's own note: three other sign permutations were tried on purchase and only this one passed CREATED=1.

    ONE ARM PER BUILDER (final review, I2). The original version of this test picked its sale with
    `kind == "sales" and v.inventory`, which silently excluded `_build_usd_sale` (the probe-22 fixture), and had
    no Op 8/9 arm at all — so `_build_usd_sale` (2 vouchers), `_build_receipt` (192) and `_build_payment` (96)
    could each be mirrored into the exact permutation Tally answers with EXCEPTIONS=1 and the whole suite stayed
    green. Every builder now has its own arm, pinned to its Op, plus the cross-cutting invariant at the end:
    across Op 6, 7, 8 and 9 alike, ISDEEMEDPOSITIVE=Yes always carries a NEGATIVE amount and No a POSITIVE one
    (docs/tally-write-exploration-v4.md; "AMOUNT is positive on the side that grows").
    """
    ds = generate()
    sale = next(v for v in ds.vouchers if v.kind == "sales" and v.inventory)
    party_line = next(line for line in sale.lines if line.ledger == sale.party)
    other_lines = [line for line in sale.lines if line.ledger != sale.party]
    assert party_line.deemed_positive is True and party_line.amount < 0
    assert other_lines and all(line.deemed_positive is False and line.amount > 0 for line in other_lines)

    purchase = next(v for v in ds.vouchers if v.kind == "purchase")
    party_line = next(line for line in purchase.lines if line.ledger == purchase.party)
    other_lines = [line for line in purchase.lines if line.ledger != purchase.party]
    assert party_line.deemed_positive is False and party_line.amount > 0
    assert other_lines and all(line.deemed_positive is True and line.amount < 0 for line in other_lines)

    # `_build_usd_sale` — the zero-rated export (probe 22's fixture; written with forex since plan part 7): same Op 6
    # convention, no GST lines.
    usd_sale = next(v for v in ds.vouchers if v.currency == "USD")
    usd_party = next(line for line in usd_sale.lines if line.ledger == usd_sale.party)
    usd_nominal = next(line for line in usd_sale.lines if line.ledger == "Export Sales")
    assert usd_party.deemed_positive is True and usd_party.amount < 0
    assert usd_nominal.deemed_positive is False and usd_nominal.amount > 0

    # `_build_receipt` — Op 8, doc line 298 verbatim: "Cash debit (Yes/−), party credit (No/+)".
    receipt = next(v for v in ds.vouchers if v.kind == "receipt")
    receipt_party = next(line for line in receipt.lines if line.ledger == receipt.party)
    receipt_bank = next(line for line in receipt.lines if line.ledger in CASH_AND_BANK)
    assert receipt_bank.deemed_positive is True and receipt_bank.amount < 0
    assert receipt_party.deemed_positive is False and receipt_party.amount > 0

    # `_build_payment` — Op 8/9's family, mirrored onto the other side: the ledger being PAID is the debit
    # (Yes/−) and cash/bank the credit (No/+), matching `create_payment`'s own live-verified envelope.
    payment = next(v for v in ds.vouchers if v.kind == "payment" and v.party not in EXPENSE_LEDGERS)
    payment_party = next(line for line in payment.lines if line.ledger == payment.party)
    payment_bank = next(line for line in payment.lines if line.ledger in CASH_AND_BANK)
    assert payment_party.deemed_positive is True and payment_party.amount < 0
    assert payment_bank.deemed_positive is False and payment_bank.amount > 0

    # `_build_expense_payment` — the same shape with an expense ledger in the party slot.
    expense = next(v for v in ds.vouchers if v.kind == "payment" and v.party in EXPENSE_LEDGERS)
    expense_line = next(line for line in expense.lines if line.ledger == expense.party)
    expense_bank = next(line for line in expense.lines if line.ledger in CASH_AND_BANK)
    assert expense_line.deemed_positive is True and expense_line.amount < 0
    assert expense_bank.deemed_positive is False and expense_bank.amount > 0

    # And the invariant the four Ops share, over every line of all 960 vouchers — so a builder added later
    # cannot slip a mirrored permutation past the arms above.
    for v in ds.vouchers:
        for line in v.lines:
            assert line.deemed_positive is (line.amount < 0), f"[S0-B:{v.tag}] {line.ledger}: {line}"


def test_opening_balances_net_to_zero():
    """F9/F13: the same double-entry invariant as `test_every_voucher_balances_to_zero`, for the OPENING
    position rather than the movements — a Task 2 anchor that probes 16/18 build on, so it must hold exactly.

    CONVENTION THIS TEST ASSUMES (do not "fix" this back to unsigned openings — F13 confirmed it's right):
    a debit is NEGATIVE, a credit is POSITIVE, matching `LineSpec.amount` after F12's sign fix
    (docs/tally-write-exploration-v4.md Op 6/7's live-verified convention). Under this convention the three
    asset ledgers (the two debtors, the bank) and opening stock are debits and carry negative openings; Capital
    Account is a credit and stays positive. It is ALSO the wire convention: `create_party_ledger` (writes.py)
    sends the signed opening as is, because Tally reads OPENINGBALANCE's sign — negative = Dr (Ruling C30,
    2026-09-24, overturning Op 5's "Tally infers the side from the parent group" / C21 / F11).

    Ledger openings use this signed convention; opening stock (an asset, from each StockItemSpec's
    opening_qty * opening_rate) is a debit too and is not carried as a ledger opening at all, so it's added into
    the same invariant separately, also on the debit (negative) side."""
    ds = generate()
    ledger_total = sum((l.opening or Decimal("0.00") for l in ds.ledgers), Decimal("0.00"))
    stock_total = sum((i.opening_qty * i.opening_rate for i in ds.items
                       if i.opening_qty is not None and i.opening_rate is not None), Decimal("0.00"))
    assert ledger_total - stock_total == Decimal("0.00")


def test_the_calendar_covers_four_financial_years_at_twenty_a_month():
    ds = generate()
    assert len(ds.vouchers) == 960
    months = {(v.date.year, v.date.month) for v in ds.vouchers}
    assert len(months) == 48
    assert min(ds.vouchers, key=lambda v: v.date).date >= date(2022, 4, 1)
    assert max(ds.vouchers, key=lambda v: v.date).date <= date(2026, 3, 31)


def test_educational_mode_uses_only_dates_tally_accepts_and_keeps_the_tags_stable():
    licensed, educational = generate(), generate(licence="educational")
    for v in educational.vouchers:
        allowed = {1, 2, 31} if monthrange(v.date.year, v.date.month)[1] == 31 else {1, 2}
        assert v.date.day in allowed
    assert [v.tag for v in licensed.vouchers] == [v.tag for v in educational.vouchers]


def test_the_dataset_carries_the_messy_shapes_the_probes_need():
    ds = generate()
    by_name = {l.name: l for l in ds.ledgers}
    assert by_name[NON_BILLWISE_DEBTOR].bill_wise is False
    assert by_name[HINDI_DEBTOR].bill_wise is True
    assert {g.name for g in ds.groups} >= {"National Creditors", "Local Creditors"}
    assert all(g.parent == "Sundry Creditors" for g in ds.groups)
    assert any(u.name == COMPOUND_UNIT and u.conversion == 10 for u in ds.units)
    assert any(i.unit == COMPOUND_UNIT for i in ds.items)
    assert not any(i.name == "Primary" for i in ds.items)                      # LESSONS §15 r13


def test_the_compound_unit_is_box_of_10_nos_built_from_two_simple_units_created_first():
    """C31: live Tally rejected BASEUNITS=Nos + ADDITIONALUNITS=Nos ("Next Unit already contains the First
    unit!"). A compound unit is first unit (a simple unit that must already exist) x CONVERSION = second unit, and
    Tally NAMES it "<first> of <conversion> <second>" — the loader's read-back looks for exactly that name."""
    ds = generate()
    names = [u.name for u in ds.units]
    compound = next(u for u in ds.units if u.name == COMPOUND_UNIT)
    assert (compound.first_unit, compound.conversion, compound.second_unit) == (BOX_UNIT, 10, BASE_UNIT)
    assert compound.first_unit != compound.second_unit
    assert compound.name == f"{compound.first_unit} of {compound.conversion} {compound.second_unit}"
    for simple in (BOX_UNIT, BASE_UNIT):
        spec = next(u for u in ds.units if u.name == simple)
        assert (spec.first_unit, spec.second_unit, spec.conversion) == (None, None, None)
        assert names.index(simple) < names.index(COMPOUND_UNIT)


def test_the_non_billwise_debtor_never_gets_a_bill():
    for v in generate().vouchers:
        if v.party == NON_BILLWISE_DEBTOR:
            assert v.bills == ()


def test_the_special_vouchers_sit_on_fixed_tags():
    by_tag = {v.tag: v for v in generate().vouchers}
    assert by_tag[101].currency == "USD" and by_tag[101].fx_amount is not None
    assert by_tag[102].currency == "USD"
    assert by_tag[201].cancelled and by_tag[202].cancelled
    assert by_tag[301].optional and by_tag[302].optional
    assert any(v.vch_type == SALES_GST_VOUCHER_TYPE for v in by_tag.values())
    assert any(HINDI_DEBTOR in v.narration or "शर्मा" in v.narration for v in by_tag.values())


def test_every_narration_carries_its_idempotency_tag():
    for v in generate().vouchers:
        assert v.narration.startswith(f"[S0-B:{v.tag}]")


def test_gstin_check_digit_matches_the_official_algorithm():
    # Verified against a known-real GSTIN (27AAPFU0939F1ZV) with the same algorithm; the brief's literal
    # expectation for this specific body ("...1ZV") did not match its own reference implementation's
    # output ("...1ZE") — see task-1-report.md for the cross-check.
    assert gstin("27", "AAAPL1234C") == "27AAAPL1234C1ZE"
    assert gstin("27", "AAPFU0939F") == "27AAPFU0939F1ZV"


def test_month_end_balances_equal_openings_plus_the_running_sum():
    ds = generate()
    exp = expected_figures(ds)
    # USD_EXPORT_PARTY is not a running sum of INR bases: Tally revalues it at the latest voucher rate (C47, below).
    for ledger in ("Domestic Sales", "Capital Account", USD_DEBTOR):
        for (name, day), value in exp.ledger_month_end.items():
            if name != ledger:
                continue
            opening = next((l.opening or Decimal("0.00") for l in ds.ledgers if l.name == name), Decimal("0.00"))
            moved = sum((line.amount for v in ds.vouchers if v.date <= day and not (v.cancelled or v.optional)
                         and not v.skip_reason for line in v.lines if line.ledger == name), Decimal("0.00"))
            assert value == opening + moved


def test_cancelled_vouchers_do_not_move_a_balance_but_are_still_counted():
    ds = generate()
    exp = expected_figures(ds)
    # plan part 7: 101/102 written with forex (was C36-skipped)
    assert sum(exp.voucher_count_by_month.values()) == len(ds.vouchers)
    assert exp.voucher_count_by_fy["2022-23"] == 240

    # M4 — the balance half. Recompute every month-end independently from openings + NON-flagged lines only (C42:
    # optional vouchers post nothing either — see the optional test below).
    cancelled = [v for v in ds.vouchers if v.cancelled]
    assert len(cancelled) == 2 and all(any(l.amount for l in v.lines) for v in cancelled)   # not vacuous
    touched = {l.ledger for v in cancelled for l in v.lines}
    last_month = max((v.date.year, v.date.month) for v in ds.vouchers)
    y, m = last_month
    fy_end = date(y, m, monthrange(y, m)[1])
    for name in touched:
        opening = next((l.opening or Decimal("0.00")) for l in ds.ledgers if l.name == name)
        live = opening + sum((l.amount for v in ds.vouchers if not (v.cancelled or v.optional) and not v.skip_reason
                              for l in v.lines if l.ledger == name),
                             Decimal("0.00"))
        with_cancelled = live + sum((l.amount for v in cancelled for l in v.lines if l.ledger == name),
                                    Decimal("0.00"))
        assert live != with_cancelled, f"{name}: a cancelled line must be non-zero for this check to bite"
        assert exp.ledger_month_end[(name, fy_end)] == live, name
        # and at the cancelled vouchers' own month-end, not just at the very end
        for cv in cancelled:
            cy, cm = cv.date.year, cv.date.month
            month_end = date(cy, cm, monthrange(cy, cm)[1])
            upto = opening + sum((l.amount for v in ds.vouchers if not (v.cancelled or v.optional) and not v.skip_reason
                                  and v.date <= month_end
                                  for l in v.lines if l.ledger == name), Decimal("0.00"))
            assert exp.ledger_month_end[(name, month_end)] == upto, (name, month_end)


def test_optional_vouchers_do_not_move_a_balance_but_are_still_counted():
    """C42 (live 2026-09-24, run 4): Tally leaves an optional voucher out of every balance, exactly like a cancelled
    one — Sundry Debtors fell short of the everything-posts figure by the party amounts of 201/202 AND 301/302."""
    for licence in ("licensed", "educational"):
        ds = generate(licence)
        exp = expected_figures(ds)
        optional = [v for v in ds.vouchers if v.optional]
        assert len(optional) == 2 and all(any(l.amount for l in v.lines) for v in optional)     # not vacuous
        # plan part 7: 101/102 written with forex (was C36-skipped)
        assert sum(exp.voucher_count_by_month.values()) == len(ds.vouchers)                      # still counted
        posting = [v for v in ds.vouchers if not (v.cancelled or v.optional or v.skip_reason)]
        for ov in optional:
            month_end = date(ov.date.year, ov.date.month, monthrange(ov.date.year, ov.date.month)[1])
            for name in {l.ledger for l in ov.lines}:
                opening = next((l.opening or Decimal("0.00")) for l in ds.ledgers if l.name == name)
                for day in (month_end, date(2026, 3, 31)):
                    upto = opening + sum((l.amount for v in posting if v.date <= day
                                          for l in v.lines if l.ledger == name), Decimal("0.00"))
                    assert exp.ledger_month_end[(name, day)] == upto, (licence, name, day)


def test_fy_openings_are_the_previous_month_end():
    ds = generate()
    exp = expected_figures(ds)
    assert exp.ledger_fy_opening[("Domestic Sales", date(2023, 4, 1))] == exp.ledger_month_end[("Domestic Sales", date(2023, 3, 31))]


def test_every_bill_is_a_magnitude_matching_its_party_line():
    """C34: BillSpec.amount is a magnitude — the writer applies the party line's sign. A signed bill here would be
    refused by create_b_voucher, and a mismatch would split the bill-wise balance away from the ledger.

    C35 (review #5): this used to pass with 288/288 Agst Refs naming a bill that was never opened — it checked
    sign and total, never the name. It now also requires every bill to resolve (see `_settlement_walk`)."""
    for licence in ("educational", "licensed"):
        assert _settlement_walk(generate(licence))[1] == [], licence
    for v in generate("educational").vouchers + generate("licensed").vouchers:
        if not v.bills:
            continue
        party_line = next(l for l in v.lines if l.ledger == v.party)
        assert all(b.amount > 0 for b in v.bills), v.tag
        assert sum(b.amount for b in v.bills) == abs(party_line.amount), v.tag


# --- live-state pin: [S0-B:1] is already in company B (2026-09-24); the loader skips it by tag -----------------
def test_tag_1_is_pinned_to_exactly_what_live_company_b_already_holds():
    """[S0-B:1] was written live on 2026-09-24. The loader skips an existing tag, so if the dataset drifted the
    live voucher and the dataset would silently disagree forever. Pinned field by field, both licences (only the
    date differs: licensed day 2, educational day 1)."""
    from probes.setup.company_b_data import BillSpec, InventorySpec, LineSpec
    for licence, day in (("licensed", 2), ("educational", 1)):
        v = next(v for v in generate(licence).vouchers if v.tag == 1)
        assert (v.kind, v.vch_type, v.date, v.party) == ("sales", "Sales", date(2022, 4, day), USD_DEBTOR)
        assert v.narration == f"[S0-B:1] Sale to {USD_DEBTOR}"
        assert v.lines == (
            LineSpec(USD_DEBTOR, Decimal("-9861.74"), True),
            LineSpec("Domestic Sales", Decimal("8357.40"), False),
            LineSpec("Output CGST", Decimal("752.17"), False),
            LineSpec("Output SGST", Decimal("752.17"), False),
        )
        assert v.inventory == (InventorySpec("Wireless Mouse", Decimal("12"), Decimal("696.45"), Decimal("8357.40")),)
        assert v.bills == (BillSpec("Inv/1", "New Ref", Decimal("9861.74"), "30 Days"),)
        assert (v.cancelled, v.optional) == (False, False)


# --- C35: every Agst Ref settles a real, earlier, open bill of the same party --------------------------------------
def _settlement_walk(ds):
    """Replay the dataset's bills in date order: returns (open New Refs by name, list of problems)."""
    from probes.setup.company_b_data import OPENING_BILL_DATE
    open_bills: dict[str, dict] = {}
    for l in ds.ledgers:
        if l.opening_bill:
            open_bills[l.opening_bill] = {"party": l.name, "date": OPENING_BILL_DATE, "left": abs(l.opening)}
    problems: list[str] = []
    for v in sorted(ds.vouchers, key=lambda v: (v.date, v.tag)):
        if v.cancelled or v.optional or v.skip_reason:
            continue
        for b in v.bills:
            if b.bill_type == "New Ref":
                open_bills[b.name] = {"party": v.party, "date": v.date, "left": b.amount}
            elif b.bill_type == "Agst Ref":
                target = open_bills.get(b.name)
                if target is None:
                    problems.append(f"[{v.tag}] {b.name} was never opened")
                elif target["party"] != v.party:
                    problems.append(f"[{v.tag}] {b.name} belongs to {target['party']}, not {v.party}")
                elif not target["date"] < v.date:
                    problems.append(f"[{v.tag}] {b.name} dated {target['date']} is not before {v.date}")
                elif b.amount > target["left"]:
                    problems.append(f"[{v.tag}] {b.name} over-settled: {b.amount} > {target['left']}")
                else:
                    target["left"] -= b.amount
            elif b.bill_type != "On Account":
                problems.append(f"[{v.tag}] unknown bill type {b.bill_type!r}")
    return open_bills, problems


def test_every_agst_ref_settles_an_earlier_open_bill_of_the_same_party_without_over_settling():
    for licence in ("licensed", "educational"):
        open_bills, problems = _settlement_walk(generate(licence))
        assert problems == [], (licence, problems[:5])
        assert all(b["left"] >= 0 for b in open_bills.values())


def test_receipts_and_payments_mix_full_and_part_settlements():
    for licence in ("licensed", "educational"):
        ds = generate(licence)
        open_bills, _ = _settlement_walk(ds)
        agst = [b for v in ds.vouchers if v.kind in ("receipt", "payment") for b in v.bills if b.bill_type == "Agst Ref"]
        assert agst, licence
        fully = [n for n, b in open_bills.items() if b["left"] == 0]
        partly = {b.name for b in agst} - set(fully)
        assert fully and partly, licence                          # both full and part settlements exist
        receipts = [v for v in ds.vouchers if v.kind == "receipt" and v.bills]
        payments = [v for v in ds.vouchers if v.kind == "payment" and v.bills]
        assert any(b.bill_type == "Agst Ref" for v in receipts for b in v.bills)
        assert any(b.bill_type == "Agst Ref" for v in payments for b in v.bills)


def test_the_opening_bill_is_settled_by_a_later_receipt():
    ds = generate()
    hits = [v for v in ds.vouchers if v.kind == "receipt" for b in v.bills if b.name == "Op/2022-001"]
    assert hits and all(v.party == "Pune Digital Solutions" for v in hits)


def test_receipts_from_the_non_billwise_debtor_carry_no_bills():
    receipts = [v for v in generate().vouchers if v.kind == "receipt" and v.party == NON_BILLWISE_DEBTOR]
    assert receipts and all(v.bills == () for v in receipts)


def test_expected_bills_outstanding_are_the_residual_of_every_open_bill():
    for licence in ("licensed", "educational"):
        ds = generate(licence)
        open_bills, _ = _settlement_walk(ds)
        want = {(b["party"], name): b["left"] for name, b in open_bills.items() if b["left"] != 0}
        assert expected_figures(ds).bills_outstanding == want


# --- plan part 7: the two USD export sales are written with forex (C36 lifted) --------------------------------------
# sha256 over every voucher but 101/102, both licences, at P7_BASE (95941a1; plan part 7 Step 3.1). Covers the C35
# settlement pool, C41's purchase plan and the debtor rotation: adding the USD party must move none of them.
UNTOUCHED_SHA = {"educational": "dd7f6d44ba52a5a42637edf441213533ba1b163750364bc69392800b8558d5be",
                 "licensed": "546acc0609f63330c6d6c6d1bd9eddf3a6b95102c3914c913cd6e2c462b0da23"}


@pytest.mark.parametrize("licence", ["educational", "licensed"])
def test_every_other_voucher_is_byte_identical_to_p7_base(licence):
    rows = [repr((v.tag, v.kind, v.vch_type, v.date, v.party, v.narration, v.lines, v.inventory, v.bills,
                  v.cancelled, v.optional, v.currency, v.fx_amount))
            for v in generate(licence).vouchers if v.tag not in (101, 102)]
    assert hashlib.sha256("\n".join(rows).encode()).hexdigest() == UNTOUCHED_SHA[licence]   # C35/C41 pools unmoved


@pytest.mark.parametrize("licence, days", [("educational", (1, 2)), ("licensed", (2, 5))])
def test_usd_sales_are_written_with_forex(licence, days):
    by_tag = {v.tag: v for v in generate(licence).vouchers}
    for tag, day, fx, rate, inr in ((101, days[0], "448.44", "82.99", "37216.04"),
                                    (102, days[1], "1161.27", "82.58", "95897.68")):
        v = by_tag[tag]
        assert (v.skip_reason, v.currency, v.currency_symbol) == (None, "USD", USD_CURRENCY.symbol)
        assert (v.fx_amount, v.fx_rate, v.date) == (Decimal(fx), Decimal(rate), date(2022, 9, day))
        assert v.party == USD_EXPORT_PARTY and v.bills == () and v.inventory == ()
        assert [(l.ledger, l.amount, l.deemed_positive) for l in v.lines] == [
            (USD_EXPORT_PARTY, -Decimal(inr), True), ("Export Sales", Decimal(inr), False)]
        assert v.narration == f"[S0-B:{tag}] Export sale to {USD_EXPORT_PARTY}"


def test_usd_sale_bases_have_no_rounding_tie():
    forex = [v for v in generate("educational").vouchers if v.fx_rate is not None]
    assert [v.tag for v in forex] == [101, 102]
    for v in forex:
        product = v.fx_amount * v.fx_rate
        assert product.quantize(Decimal("0.01"), ROUND_HALF_UP) == product.quantize(Decimal("0.01"), ROUND_HALF_EVEN)


def test_usd_export_party_and_currency():
    ds = generate("educational")
    assert ds.currencies == (USD_CURRENCY,)
    party = next(l for l in ds.ledgers if l.name == USD_EXPORT_PARTY)
    assert (party.parent, party.bill_wise, party.currency, party.opening, party.gstin) == (
        "Sundry Debtors", False, USD_CURRENCY.symbol, None, None)
    gulf = next(l for l in ds.ledgers if l.name == USD_DEBTOR)
    assert gulf.currency is None                                 # fact 1: Gulf's 87 live vouchers stay INR
    assert not any(v.party == USD_EXPORT_PARTY for v in ds.vouchers if v.tag not in (101, 102))   # out of rotation


def test_expected_counts_include_the_usd_sales():
    ds = generate("educational")
    exp = expected_figures(ds)
    assert not any(v.skip_reason for v in ds.vouchers) and FORMERLY_SKIPPED_TAGS == {101, 102}
    assert [v.tag for v in ds.vouchers] == list(range(1, 961))              # nothing renumbered
    assert sum(exp.voucher_count_by_month.values()) == len(ds.vouchers) == 960
    assert exp.voucher_count_by_fy["2022-23"] == 240
    last = max(day for _, day in exp.ledger_month_end)
    # C47 (live 2026-09-25): both sales, never settled, valued at the LATEST voucher rate — $1609.71 × 82.58.
    assert exp.ledger_month_end[(USD_EXPORT_PARTY, last)] == Decimal("-132929.85")
    assert exp.ledger_month_end[("Export Sales", date(2022, 9, 30))] == Decimal("133113.72")   # keeps the bases


def test_the_forex_write_shape_is_the_live_one():
    """forex_shape_2026-09-25_run2/summary.json chose V1b: the full form, the rate written without a base symbol."""
    import json
    from pathlib import Path
    chosen = json.loads((Path(__file__).parent.parent / "fixtures" / "sync" / "forex_shape_2026-09-25_run2"
                         / "summary.json").read_text(encoding="utf-8"))["chosen"]
    assert (FOREX_FORM, FOREX_BASE_SYMBOL, USD_CURRENCY.symbol) == (chosen["form"], chosen["base_symbol"],
                                                                    chosen["party_currency"])


# --- C40: quantities of a compound-unit item are written in the compound's FIRST unit ------------------------------
def test_quantity_unit_resolves_a_compound_to_its_first_unit():
    from probes.setup.company_b_data import quantity_unit
    ds = generate()
    assert quantity_unit(ds.units, COMPOUND_UNIT) == BOX_UNIT
    assert quantity_unit(ds.units, BASE_UNIT) == BASE_UNIT
    assert quantity_unit(ds.units, "Unknown") == "Unknown"


# --- live-state pin: [S0-B:2] is already in company B too (2026-09-24) ----------------------------------------------
def test_tag_2_is_pinned_to_exactly_what_live_company_b_already_holds():
    """C41: [S0-B:2] (the first A4 Paper Ream sale) is live as well. Purchases now draw stock, so nothing about the
    sales stream may move — pinned field by field, both licences (licensed day 5, educational day 2)."""
    from probes.setup.company_b_data import BillSpec, InventorySpec, LineSpec
    for licence, day in (("licensed", 5), ("educational", 2)):
        v = next(v for v in generate(licence).vouchers if v.tag == 2)
        assert (v.kind, v.vch_type, v.date, v.party) == ("sales", "Sales", date(2022, 4, day), HINDI_DEBTOR)
        assert v.narration == f"[S0-B:2] Sale to {HINDI_DEBTOR}"
        assert v.lines == (
            LineSpec(HINDI_DEBTOR, Decimal("-12041.32"), True),
            LineSpec("Domestic Sales", Decimal("10204.50"), False),
            LineSpec("Output CGST", Decimal("918.41"), False),
            LineSpec("Output SGST", Decimal("918.41"), False),
        )
        assert v.inventory == (InventorySpec("A4 Paper Ream", Decimal("10"), Decimal("1020.45"), Decimal("10204.50")),)
        assert v.bills == (BillSpec("Inv/2", "New Ref", Decimal("12041.32"), "30 Days"),)
        assert (v.cancelled, v.optional, v.currency, v.fx_amount, v.skip_reason) == (False, False, "INR", None, None)


# --- C41: purchases carry stock, so no item ever closes a day negative ---------------------------------------------
# sha256 over repr() of every voucher of the kind, in tag order, as generated at c866d74 (before C41). Receipts and
# expense payments ride the same shared RNG stream as the sales, so they are pinned too: if the purchase rewrite
# drew one number more or less from that stream, every one of these would move.
_HEAD_DIGESTS = {
    # plan part 7 (pre-flight F1): re-based at 271ff01 — where the c866d74 digests above still held — onto the
    # pre-part-7 fields only (VoucherSpec gained currency_symbol/fx_rate, which changes every repr), with 101/102 left
    # out (changed on purpose; pinned by test_usd_sales_are_written_with_forex).
    ("licensed", "sales"): (382, "e4165a242b29e2e19e4b12d1e3a0a0f51f12ade077f7d6bcccb3fef030dec87a"),
    ("licensed", "receipt"): (192, "49374b8f40001714f9fe07c87191cc5a50600311208754553b3e2590c34e6ccf"),
    ("licensed", "expense"): (48, "10a771985647411516dee3318b5b002dd053e88ba5cc7ce61bf281e98f887cc4"),
    ("educational", "sales"): (382, "195bd2af5e151d07289b5a8f7fe1fb28fcdc9894abe5827cdc5f8b2f1478a004"),
    ("educational", "receipt"): (192, "5702e1f6c1dd809e0b84a63924c6e449e654c1f3c5c626e86159d4bb0a0d5410"),
    ("educational", "expense"): (48, "718d0328557092d25dc9803f4901f891c123e8aa98f61187e8812b1ec8ab9fdc"),
}


def _digest(vouchers) -> tuple[int, str]:
    rows = [repr((v.tag, v.kind, v.vch_type, v.date, v.party, v.narration, v.lines, v.inventory, v.bills,
                  v.cancelled, v.optional, v.currency, v.fx_amount, v.skip_reason)) for v in vouchers]
    return len(rows), hashlib.sha256("\n".join(rows).encode()).hexdigest()


def test_every_sale_receipt_and_expense_payment_is_byte_identical_to_before_c41():
    for licence in ("licensed", "educational"):
        ds = generate(licence)
        picks = {
            "sales": [v for v in ds.vouchers if v.kind == "sales" and v.tag not in (101, 102)],
            "receipt": [v for v in ds.vouchers if v.kind == "receipt"],
            "expense": [v for v in ds.vouchers if v.kind == "payment" and v.party in EXPENSE_LEDGERS],
        }
        for kind, vouchers in picks.items():
            assert _digest(vouchers) == _HEAD_DIGESTS[(licence, kind)], (licence, kind)


def _stock_walk(ds, *, include_flagged: bool):
    """Independent replay: opening qty, then every day's purchases (+) and sales (-); returns each item's minimum
    day-close, its closing, and every (item, day, qty) that closed negative. Flagged (cancelled/optional) vouchers
    move no stock in Tally; `include_flagged` also counts them, the stricter case (the operator flags a cancelled
    voucher only AFTER its create, so it moves stock for a while)."""
    level = {i.name: (i.opening_qty or Decimal("0")) for i in ds.items}
    lowest = dict(level)
    negative: list[tuple[str, date, Decimal]] = []
    by_day: dict[date, list] = {}
    for v in ds.vouchers:
        if v.skip_reason or (not include_flagged and (v.cancelled or v.optional)):
            continue
        by_day.setdefault(v.date, []).append(v)
    for day in sorted(by_day):
        for v in by_day[day]:
            sign = Decimal("1") if v.kind == "purchase" else Decimal("-1")
            for inv in v.inventory:
                level[inv.item] += sign * inv.qty
        for item, qty in level.items():
            lowest[item] = min(lowest[item], qty)
            if qty < 0:
                negative.append((item, day, qty))
    return lowest, level, negative


def test_no_item_closes_any_day_negative_in_either_licence():
    for licence in ("licensed", "educational"):
        ds = generate(licence)
        for include_flagged in (False, True):
            _, _, negative = _stock_walk(ds, include_flagged=include_flagged)
            assert negative == [], (licence, include_flagged, negative[:5])


def test_a_purchase_on_1_april_2022_covers_tag_1s_twelve_wireless_mice():
    for licence in ("licensed", "educational"):
        ds = generate(licence)
        tag_1 = next(v for v in ds.vouchers if v.tag == 1)
        bought = sum((inv.qty for v in ds.vouchers if v.kind == "purchase" and v.date == date(2022, 4, 1)
                      for inv in v.inventory if inv.item == "Wireless Mouse"), Decimal("0"))
        assert bought >= tag_1.inventory[0].qty == Decimal("12"), licence


def test_every_purchase_buys_stock_on_the_op7_invoice_shape():
    from probes.setup.writes import validate_b_voucher
    for licence in ("licensed", "educational"):
        ds = generate(licence)
        units = {i.name: i.unit for i in ds.items}
        purchases = [v for v in ds.vouchers if v.kind == "purchase"]
        assert len(purchases) == 240 and all(v.inventory for v in purchases), licence
        for v in purchases:
            goods = -sum((inv.amount for inv in v.inventory), Decimal("0.00"))     # Op 7: rows signed −goods
            half = (goods * Decimal("0.09")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            assert [line.ledger for line in v.lines] == [v.party, "Local Purchases", "Input CGST", "Input SGST"]
            assert v.lines[1:] == (LineSpec("Local Purchases", -goods, True), LineSpec("Input CGST", -half, True),
                                   LineSpec("Input SGST", -half, True)), v.tag
            assert v.lines[0].amount == goods + 2 * half and v.lines[0].deemed_positive is False
            for inv in v.inventory:
                assert inv.qty > 0 and inv.qty == inv.qty.to_integral_value() and inv.rate > 0
                assert inv.amount == -(inv.qty * inv.rate).quantize(Decimal("0.01")), v.tag
            assert v.vch_type == "Purchase"
            # every creditor is bill-wise: one New Ref, a magnitude (C34)
            assert v.bills == (BillSpec(f"Pur/{v.tag}", "New Ref", v.lines[0].amount, "45 Days"),), v.tag
            validate_b_voucher(vch_type=v.vch_type, narration=v.narration, party=v.party,
                               lines=[(l.ledger, l.amount, l.deemed_positive) for l in v.lines],
                               inventory=[(i.item, units[i.item], i.qty, i.rate, i.amount) for i in v.inventory],
                               bills=[(b.name, b.bill_type, b.amount, b.credit_period) for b in v.bills])


def test_purchase_rates_sit_below_the_same_periods_sale_rates():
    """A purchase row's rate is under every sale rate of that item in the purchase's calendar month AND in the window
    it stocks (up to the item's next purchase) — a positive margin — and not absurdly far under (>= 60%)."""
    for licence in ("licensed", "educational"):
        ds = generate(licence)
        sales = [(v.date, inv) for v in ds.vouchers if v.kind == "sales" for inv in v.inventory]
        rows = sorted(((v.date, v.tag, inv) for v in ds.vouchers if v.kind == "purchase" for inv in v.inventory),
                      key=lambda r: (r[0], r[1]))
        for k, (day, tag, inv) in enumerate(rows):
            nxt = next((d for d, _, other in rows[k + 1:] if other.item == inv.item and d > day), date.max)
            period = [s.rate for d, s in sales if s.item == inv.item
                      and ((d.year, d.month) == (day.year, day.month) or day <= d < nxt)]
            if period:
                assert inv.rate < min(period), (licence, tag, inv, min(period))
                assert inv.rate >= min(period) * Decimal("0.6"), (licence, tag, inv, min(period))


def test_payments_settle_real_purchase_bills():
    for licence in ("licensed", "educational"):
        ds = generate(licence)
        purchase_bills = {b.name: v for v in ds.vouchers if v.kind == "purchase" for b in v.bills}
        agst = [(v, b) for v in ds.vouchers if v.kind == "payment" for b in v.bills if b.bill_type == "Agst Ref"]
        assert agst, licence
        for v, b in agst:
            assert b.name in purchase_bills and purchase_bills[b.name].party == v.party, (v.tag, b)
            assert purchase_bills[b.name].date < v.date
        _, problems = _settlement_walk(ds)
        assert problems == [], problems[:5]


def test_expected_stock_month_ends_match_an_independent_replay():
    for licence in ("licensed", "educational"):
        ds = generate(licence)
        exp = expected_figures(ds)
        assert exp.stock_month_end, licence
        assert all(qty >= 0 for qty in exp.stock_month_end.values())
        _, closing, _ = _stock_walk(ds, include_flagged=False)
        for item, qty in closing.items():
            assert exp.stock_month_end[(item, date(2026, 3, 31))] == qty, (licence, item)
        # and one mid-way month-end, recomputed from scratch
        cut = date(2023, 9, 30)
        for i in ds.items:
            moved = sum(((1 if v.kind == "purchase" else -1) * inv.qty for v in ds.vouchers
                         if v.date <= cut and not (v.cancelled or v.optional or v.skip_reason)
                         for inv in v.inventory if inv.item == i.name), Decimal("0"))
            assert exp.stock_month_end[(i.name, cut)] == (i.opening_qty or 0) + moved, (licence, i.name)


# --- C47 (live 2026-09-25, logs/setup-b-forex-live-2026-09-25.log): a forex ledger is valued at the latest rate ---------
LIVE_USD_PARTY_CLOSING = "-$1609.71 @ ? 82.58/$ = -? 132929.85"      # ledger_details, live company B after the load


def test_forex_ledger_is_valued_at_the_latest_voucher_rate_c47():
    """TallyPrime values a forex ledger's balance at the rate of its latest-dated forex voucher (1609.71 × 82.58 =
    132929.85), not at the sum of the vouchers' INR bases (133113.72). Export Sales keeps the bases, so the books'
    Trial Balance is out by the unrealised difference, 183.87."""
    from probes.reads import parse_forex_amount
    live = parse_forex_amount(LIVE_USD_PARTY_CLOSING)
    for licence in ("educational", "licensed"):
        ds = generate(licence)
        exp = expected_figures(ds)
        forex = sorted((v for v in ds.vouchers if v.fx_rate is not None), key=lambda v: (v.date, v.tag))
        face = sum((v.fx_amount for v in forex), Decimal("0.00"))
        assert (face, forex[-1].fx_rate) == (abs(live.fx), live.rate)
        for (name, day), value in exp.ledger_month_end.items():
            if name != USD_EXPORT_PARTY:
                continue
            assert value == (live.base if day >= date(2022, 9, 30) else Decimal("0.00")), (licence, day)
        assert exp.forex_revaluation[date(2026, 3, 31)] == Decimal("183.87")
        assert exp.forex_revaluation[date(2022, 8, 31)] == Decimal("0.00")
        assert exp.ledger_fy_opening[(USD_EXPORT_PARTY, date(2023, 4, 1))] == live.base
