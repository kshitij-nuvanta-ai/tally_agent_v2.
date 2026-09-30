"""Company B's dataset (S0 spec §4.3). Pure and offline: no Tally, no I/O.

Company B is the messy-shapes company: custom creditor sub-groups, a non-bill-wise debtor, a Hindi-named
debtor, a compound unit, a zero-rated USD export, cancelled and optional vouchers. Tasks 2, 5 and 6 build
on this module's `Dataset` to compute expected figures, drive the loader, and probe against live Tally.
"""
from __future__ import annotations

import random
from calendar import monthrange
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

SEED = 20260923
# C41: purchases draw from their OWN stream, so planning their stock can never move a sale (the shared `rng` feeds the
# sales, receipts and expense payments, several of which are already live).
PURCHASE_SEED = SEED + 41
COMPANY_B_BOOKS_FROM = date(2022, 4, 1)
COMPANY_B_LAST_MONTH = date(2026, 3, 1)
TAG_PREFIX = "S0-B"
VOUCHERS_PER_MONTH = 20

NON_BILLWISE_DEBTOR = "Kolhapur Retail Mart"     # LESSONS §15 r16 — never assert this one in a bills report
USD_DEBTOR = "Gulf Office Supplies LLC"
# Plan part 7, H1 = S-B: the USD export sales go to their own USD-currency ledger. Gulf (USD_DEBTOR) carries 87 live
# INR vouchers from the debtor rotation, and a currency change could re-cast them, so Gulf is never altered.
USD_EXPORT_PARTY = "Gulf Office Supplies LLC (USD)"
HINDI_DEBTOR = "शर्मा ट्रेडर्स"
BASE_UNIT = "Nos"
BOX_UNIT = "Box"
# C31: Tally NAMES a compound unit "<first unit> of <conversion> <second unit>" — here Box x 10 = Nos.
COMPOUND_UNIT = f"{BOX_UNIT} of 10 {BASE_UNIT}"
SALES_GST_VOUCHER_TYPE = "Sales - GST"           # created in the Tally UI, never by the loader
# The opening bill (e.g. Op/2022-001) is dated the day before the books begin (entered in the UI, 31-3-2022).
OPENING_BILL_DATE = date(2022, 3, 31)

_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Fixed-tag specials (S0 spec §4.3): tags land wherever they naturally fall in the calendar (always a sales
# slot, see the loop below) and are overridden in place, so the surrounding month's shape stays untouched.
_USD_TAGS = (101, 102)
# Plan part 7 (lifts C36, which skipped 101/102 because they would have gone out as plain INR sales): written as
# forex vouchers with the shape live company B stored. The loader's pre-run drift check treats a gap made ONLY of
# these tags as a note, not drift (Ruling P7-9) — live B had them absent under C36.
FORMERLY_SKIPPED_TAGS = frozenset(_USD_TAGS)
# Live write shape (plan part 7 Task 2, tests/fixtures/sync/forex_shape_2026-09-25_run2/summary.json, chosen V1b):
# the full AMOUNT form ("-$448.44 @ 82.99/$ = -37216.04") with the rate written WITHOUT a base symbol. V1, the rate
# with company B's base symbol "?", got EXCEPTIONS=1. Tally exports it back as "-$448.44 @ ? 82.99/$ = -? 37216.04".
FOREX_FORM = "full"
FOREX_BASE_SYMBOL = ""
_CANCELLED_TAGS = (201, 202)
_OPTIONAL_TAGS = (301, 302)
# C41: the month's five purchase slots are i = 8..12. Licensed, slot 8 is dated the 1st (it used to be the 26th) so
# every month opens with a purchase before its first sale on the 2nd — above all 1-Apr-2022, which has to cover
# [S0-B:1]'s 12 Wireless Mouse against a zero opening. Educational dates are untouched (_educational_days).
_FIRST_PURCHASE_SLOT = 8
_PURCHASE_BUFFER = (2, 10)              # units bought over the window's shortfall, so no purchase is ever empty
_PURCHASE_MARGIN_PCT = (70, 85)         # purchase rate = this % of the period's lowest sale rate for the item


@dataclass(frozen=True)
class GroupSpec:
    name: str
    parent: str


@dataclass(frozen=True)
class UnitSpec:
    """A simple unit (all three compound fields None) or a Tally compound unit: `first_unit` x `conversion` =
    `second_unit` (C31 — e.g. Box x 10 = Nos, which Tally names "Box of 10 Nos"). Both parts are simple units
    that must exist before the compound is created, and they must differ ("Next Unit already contains the First
    unit!" otherwise — live 2026-09-24)."""
    name: str
    first_unit: str | None = None
    second_unit: str | None = None
    conversion: int | None = None


@dataclass(frozen=True)
class CurrencySpec:
    """A Tally Currency master (plan part 7). `symbol` is the master's NAME and what a forex AMOUNT is written in;
    `formal_name` is its expanded name. Field names on the wire are candidates until Task 2 measures them."""
    symbol: str
    formal_name: str
    decimal_symbol: str = "cent"
    decimal_places: int = 2


USD_CURRENCY = CurrencySpec(symbol="$", formal_name="USD")


@dataclass(frozen=True)
class StockItemSpec:
    name: str
    unit: str
    hsn: str | None
    opening_qty: Decimal | None
    opening_rate: Decimal | None


@dataclass(frozen=True)
class LedgerSpec:
    name: str
    parent: str
    bill_wise: bool
    opening: Decimal | None
    gstin: str | None
    opening_bill: str | None
    currency: str | None = None          # plan part 7: a Currency master's symbol; None = the base currency (INR)


@dataclass(frozen=True)
class LineSpec:
    ledger: str
    amount: Decimal
    deemed_positive: bool


@dataclass(frozen=True)
class InventorySpec:
    item: str
    qty: Decimal
    rate: Decimal
    amount: Decimal


@dataclass(frozen=True)
class BillSpec:
    name: str
    bill_type: str
    amount: Decimal          # a MAGNITUDE: the writer signs it like the party line it nests under (C34)
    credit_period: str | None


@dataclass(frozen=True)
class VoucherSpec:
    tag: int
    kind: str
    vch_type: str
    date: date
    party: str
    narration: str
    lines: tuple[LineSpec, ...]
    inventory: tuple[InventorySpec, ...]
    bills: tuple[BillSpec, ...]
    cancelled: bool = False
    optional: bool = False
    currency: str = "INR"
    fx_amount: Decimal | None = None
    currency_symbol: str | None = None   # plan part 7: the Currency master a forex voucher is written in ("$")
    fx_rate: Decimal | None = None       # plan part 7: base-currency units per foreign unit
    # C36: set => the loader never writes this voucher and expected_figures ignores it. The tag stays in the
    # dataset so no other tag renumbers (tags are the loader's idempotency key and several are already live).
    # A general mechanism since plan part 7: no voucher uses it (101/102 are written with forex).
    skip_reason: str | None = None


@dataclass(frozen=True)
class Dataset:
    groups: tuple[GroupSpec, ...]
    units: tuple[UnitSpec, ...]
    items: tuple[StockItemSpec, ...]
    ledgers: tuple[LedgerSpec, ...]
    vouchers: tuple[VoucherSpec, ...]
    licence: str
    currencies: tuple[CurrencySpec, ...] = ()     # plan part 7: must exist in Tally (made in the UI) before a load


@dataclass(frozen=True)
class Expected:
    ledger_month_end: dict[tuple[str, date], Decimal]   # (ledger, last day of month) -> balance
    ledger_fy_opening: dict[tuple[str, date], Decimal]  # (ledger, 1 April) -> balance
    voucher_count_by_month: dict[tuple[int, int], int]
    voucher_count_by_fy: dict[str, int]                 # "2022-23" -> n
    # C35: (party, bill name) -> outstanding MAGNITUDE after every voucher, for bills not fully settled
    bills_outstanding: dict[tuple[str, str], Decimal]
    # C41: (stock item, last day of month) -> closing quantity (in the item's quantity unit, C40); flagged/skipped
    # vouchers move no stock
    stock_month_end: dict[tuple[str, date], Decimal] = field(default_factory=dict)
    # C47: last day of month -> Σ over currency ledgers of (balance at the latest voucher rate − Σ the vouchers' INR
    # bases). Tally's Trial Balance is out by exactly this (the unrealised forex difference; 183.87 at the books' end).
    forex_revaluation: dict[date, Decimal] = field(default_factory=dict)


def gstin(state_code: str, pan: str) -> str:
    """A GSTIN with a correct check digit, so Tally's format validation can't reject it."""
    body = f"{state_code}{pan}1Z"
    total = 0
    for i, ch in enumerate(body):
        value = _GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += value // 36 + value % 36
    return body + _GSTIN_CHARS[(36 - total % 36) % 36]


def quantity_unit(units: tuple[UnitSpec, ...], unit_name: str) -> str:
    """C40 (live 2026-09-24): the unit a QUANTITY or a rate's denominator is written in. For a compound unit that
    is its FIRST unit — "15 Box", "950.00/Box" — because "15 Box of 10 Nos" was accepted (created=1) and silently
    stored no opening at all. A simple (or unknown) unit is its own name."""
    spec = next((u for u in units if u.name == unit_name), None)
    return spec.first_unit if spec is not None and spec.first_unit else unit_name


def _educational_days(year: int, month: int) -> tuple[int, ...]:
    """Educational Tally accepts the 1st, 2nd and 31st only (live 2026-09-22, see companies.py)."""
    return (1, 2, 31) if monthrange(year, month)[1] == 31 else (1, 2)


def _months() -> list[tuple[int, int]]:
    out, y, m = [], COMPANY_B_BOOKS_FROM.year, COMPANY_B_BOOKS_FROM.month
    while (y, m) <= (COMPANY_B_LAST_MONTH.year, COMPANY_B_LAST_MONTH.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _gst(goods: Decimal) -> tuple[Decimal, Decimal]:
    half = (goods * Decimal("0.09")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return half, half


def _groups() -> tuple[GroupSpec, ...]:
    return (
        GroupSpec(name="National Creditors", parent="Sundry Creditors"),
        GroupSpec(name="Local Creditors", parent="Sundry Creditors"),
    )


def _units() -> tuple[UnitSpec, ...]:
    return (
        UnitSpec(name=BASE_UNIT),
        UnitSpec(name=BOX_UNIT),                                     # C31: the compound's first unit
        UnitSpec(name=COMPOUND_UNIT, first_unit=BOX_UNIT, second_unit=BASE_UNIT, conversion=10),
    )


def _items() -> tuple[StockItemSpec, ...]:
    return (
        StockItemSpec(name="USB Cable Type-C", unit=BASE_UNIT, hsn="8544",
                       opening_qty=Decimal("120"), opening_rate=Decimal("85.00")),
        StockItemSpec(name="Wireless Mouse", unit=BASE_UNIT, hsn="8471",
                       opening_qty=None, opening_rate=None),
        StockItemSpec(name="A4 Paper Ream", unit=COMPOUND_UNIT, hsn="4802",
                       opening_qty=Decimal("15"), opening_rate=Decimal("950.00")),
        StockItemSpec(name="Office Stapler", unit=BASE_UNIT, hsn=None,
                       opening_qty=None, opening_rate=None),
        StockItemSpec(name="Whiteboard Marker Set", unit=BASE_UNIT, hsn="9608",
                       opening_qty=None, opening_rate=None),
    )


def _ledgers() -> tuple[LedgerSpec, ...]:
    debtors = (
        LedgerSpec(name=NON_BILLWISE_DEBTOR, parent="Sundry Debtors", bill_wise=False,
                   opening=Decimal("-45000.00"), gstin=None, opening_bill=None),
        LedgerSpec(name=USD_DEBTOR, parent="Sundry Debtors", bill_wise=True,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name=HINDI_DEBTOR, parent="Sundry Debtors", bill_wise=True,
                   opening=None, gstin=gstin("27", "AABCS1234D"), opening_bill=None),
        LedgerSpec(name="Nagpur Wholesale Traders", parent="Sundry Debtors", bill_wise=True,
                   opening=None, gstin=gstin("27", "AAECN5678E"), opening_bill=None),
        LedgerSpec(name="Pune Digital Solutions", parent="Sundry Debtors", bill_wise=True,
                   opening=Decimal("-62500.00"), gstin=gstin("27", "AAFCP4321F"),
                   opening_bill="Op/2022-001"),
        LedgerSpec(name="Indore Home Needs", parent="Sundry Debtors", bill_wise=True,
                   opening=None, gstin=None, opening_bill=None),
        # Plan part 7 (H1 = S-B, Ruling P7-3): the forex-only party, LAST so the rotation's indices never move (and
        # `_vouchers` keeps currency ledgers out of the rotation anyway). Not bill-wise: forex bills are unverified.
        LedgerSpec(name=USD_EXPORT_PARTY, parent="Sundry Debtors", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None, currency=USD_CURRENCY.symbol),
    )
    creditors = (
        LedgerSpec(name="Delhi Metal Traders", parent="National Creditors", bill_wise=True,
                   opening=None, gstin=gstin("07", "AAACD1234E"), opening_bill=None),
        LedgerSpec(name="Chennai Components Ltd", parent="National Creditors", bill_wise=True,
                   opening=None, gstin=gstin("33", "AABCC5678F"), opening_bill=None),
        LedgerSpec(name="Kolhapur Hardware Suppliers", parent="Local Creditors", bill_wise=True,
                   opening=None, gstin=gstin("27", "AAFHK4321G"), opening_bill=None),
        LedgerSpec(name="Satara Packaging Co", parent="Local Creditors", bill_wise=True,
                   opening=None, gstin=None, opening_bill=None),
    )
    other = (
        # The balancing figure of the opening trial balance (capital introduced minus everything else already
        # committed elsewhere — debtors + opening stock — sits in the bank): -45000 (Kolhapur) - 62500 (Pune
        # Digital Solutions) - 24450 (opening stock: 120 USB Cable Type-C @ 85 + 15 A4 Paper Ream @ 950) -
        # 868050 (this ledger) + 1000000 (Capital Account) == 0.00 (F9; test_opening_balances_net_to_zero).
        LedgerSpec(name="HDFC Bank Current A/c", parent="Bank Accounts", bill_wise=False,
                   opening=Decimal("-868050.00"), gstin=None, opening_bill=None),
        LedgerSpec(name="Cash", parent="Cash-in-Hand", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Domestic Sales", parent="Sales Accounts", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Export Sales", parent="Sales Accounts", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Local Purchases", parent="Purchase Accounts", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Freight & Forwarding", parent="Indirect Expenses", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Office Rent", parent="Indirect Expenses", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Bank Charges", parent="Indirect Expenses", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Input CGST", parent="Duties & Taxes", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Input SGST", parent="Duties & Taxes", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Input IGST", parent="Duties & Taxes", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Output CGST", parent="Duties & Taxes", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Output SGST", parent="Duties & Taxes", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Output IGST", parent="Duties & Taxes", bill_wise=False,
                   opening=None, gstin=None, opening_bill=None),
        LedgerSpec(name="Capital Account", parent="Capital Account", bill_wise=False,
                   opening=Decimal("1000000.00"), gstin=None, opening_bill=None),
    )
    return debtors + creditors + other


def _build_sales(rng: random.Random, tag: int, d: date, party: str, vch_type: str,
                  item_names: list[str], item_rate_hint: dict[str, int],
                  bill_wise: dict[str, bool]) -> VoucherSpec:
    item_name = item_names[tag % len(item_names)]
    qty = Decimal(rng.randint(1, 20))
    rate = Decimal(rng.randint(*item_rate_hint[item_name])) / 100
    goods = (qty * rate).quantize(Decimal("0.01"))
    cgst, sgst = _gst(goods)
    party_amount = goods + cgst + sgst
    # F12: docs/tally-write-exploration-v4.md Op 6 — sales party ISDEEMEDPOSITIVE=Yes with AMOUNT NEGATIVE;
    # GST/nominal lines ISDEEMEDPOSITIVE=No with AMOUNT POSITIVE. `deemed_positive` was already right; the
    # amounts were the mirror image of this (Op 7's note: only this exact sign permutation gets CREATED=1).
    lines = (
        LineSpec(ledger=party, amount=-party_amount, deemed_positive=True),
        LineSpec(ledger="Domestic Sales", amount=goods, deemed_positive=False),
        LineSpec(ledger="Output CGST", amount=cgst, deemed_positive=False),
        LineSpec(ledger="Output SGST", amount=sgst, deemed_positive=False),
    )
    inventory = (InventorySpec(item=item_name, qty=qty, rate=rate, amount=goods),)
    bills = (BillSpec(name=f"Inv/{tag}", bill_type="New Ref", amount=party_amount,
                       credit_period="30 Days"),) if bill_wise.get(party, False) else ()
    narration = f"[{TAG_PREFIX}:{tag}] Sale to {party}"
    return VoucherSpec(tag=tag, kind="sales", vch_type=vch_type, date=d, party=party, narration=narration,
                        lines=lines, inventory=inventory, bills=bills)


def _build_usd_sale(rng: random.Random, tag: int, d: date) -> VoucherSpec:
    """The zero-rated export sale: party + sales only, no GST lines (S0 spec §4.3)."""
    qty = Decimal(rng.randint(5, 50))
    rate_usd = Decimal(rng.randint(500, 5000)) / 100
    fx_amount = (qty * rate_usd).quantize(Decimal("0.01"))
    fx_rate = Decimal(rng.randint(8000, 8500)) / 100
    inr_amount = (fx_amount * fx_rate).quantize(Decimal("0.01"))
    lines = (                                                                                                # F12
        LineSpec(ledger=USD_EXPORT_PARTY, amount=-inr_amount, deemed_positive=True),
        LineSpec(ledger="Export Sales", amount=inr_amount, deemed_positive=False),
    )
    narration = f"[{TAG_PREFIX}:{tag}] Export sale to {USD_EXPORT_PARTY}"
    return VoucherSpec(tag=tag, kind="sales", vch_type="Sales", date=d, party=USD_EXPORT_PARTY, narration=narration,
                        lines=lines, inventory=(), bills=(), currency="USD", fx_amount=fx_amount,
                        currency_symbol=USD_CURRENCY.symbol, fx_rate=fx_rate)


def _build_purchase(tag: int, d: date, party: str, inventory: tuple[InventorySpec, ...],
                     bill_wise: dict[str, bool]) -> VoucherSpec:
    """C41: a goods purchase that brings stock in. Before C41 it posted a random amount to Local Purchases with no
    inventory, so every sale drove its item further negative. The goods figure is now the inventory rows' total."""
    goods = -sum((inv.amount for inv in inventory), Decimal("0.00"))      # the rows are signed Op 7 (−goods)
    cgst, sgst = _gst(goods)
    creditor_amount = goods + cgst + sgst
    # F12: Op 7 inverts Op 6 — nominal/GST lines ISDEEMEDPOSITIVE=Yes with AMOUNT NEGATIVE; purchase party
    # ISDEEMEDPOSITIVE=No with AMOUNT POSITIVE. C41: with inventory the order is create_b_voucher's contract (M2) —
    # party first, then the nominal ledger (which C32 carries only in each row's ACCOUNTINGALLOCATIONS), then GST.
    lines = (
        LineSpec(ledger=party, amount=creditor_amount, deemed_positive=False),
        LineSpec(ledger="Local Purchases", amount=-goods, deemed_positive=True),
        LineSpec(ledger="Input CGST", amount=-cgst, deemed_positive=True),
        LineSpec(ledger="Input SGST", amount=-sgst, deemed_positive=True),
    )
    bills = (BillSpec(name=f"Pur/{tag}", bill_type="New Ref", amount=creditor_amount,
                       credit_period="45 Days"),) if bill_wise.get(party, False) else ()
    narration = f"[{TAG_PREFIX}:{tag}] Purchase from {party}"
    return VoucherSpec(tag=tag, kind="purchase", vch_type="Purchase", date=d, party=party, narration=narration,
                        lines=lines, inventory=inventory, bills=bills)


@dataclass(frozen=True)
class _Slot:
    """A purchase / receipt / payment whose content depends on something only known once the whole calendar is laid
    out (C41: purchases need every later sale; payments settle those purchases' bills). `drawn` is the shared-stream
    draw taken at the slot's own place in the calendar, exactly as before C41."""
    kind: str
    tag: int
    day: date
    party: str
    item_slot: int = 0
    drawn: Decimal | None = None


def _plan_purchases(items: tuple[StockItemSpec, ...], purchases: list[_Slot], sales: list[VoucherSpec],
                    prng: random.Random) -> dict[int, tuple[InventorySpec, ...]]:
    """C41: each purchase slot stocks one item — slot j buys item (j + 1) % 5, the item the month's sale in slot j
    sells (sale i sells items[(tag) % 5] and 20 % 5 == 0, so slot i always sells item (i + 1) % 5). The quantity
    covers every sale of that item from this purchase up to (not including) the day of its next purchase, less
    what is already in stock, plus a small buffer — so the day-close stock never goes negative. Every sale counts,
    cancelled and optional ones included: the operator flags a cancelled voucher only after its create, so its stock
    is out for a while. The rate is a 70-85% margin on the lowest sale rate of the item in the purchase's month and
    in the window it stocks."""
    names = [i.name for i in items]
    demand: dict[str, list[tuple[date, Decimal, Decimal]]] = {n: [] for n in names}
    for v in sales:
        if v.skip_reason:
            continue
        for inv in v.inventory:
            demand[inv.item].append((v.date, inv.qty, inv.rate))
    planned: dict[int, tuple[InventorySpec, ...]] = {}
    for item in items:
        schedule = sorted((p for p in purchases if names[(p.item_slot + 1) % len(names)] == item.name),
                          key=lambda p: (p.day, p.tag))
        sold = demand[item.name]
        bought = Decimal("0")
        last_ref = min(rate for _, _, rate in sold)
        for k, p in enumerate(schedule):
            until = schedule[k + 1].day if k + 1 < len(schedule) else date.max
            on_hand = (item.opening_qty or Decimal("0")) + bought - sum(
                (qty for d, qty, _ in sold if d < p.day), Decimal("0"))
            needed = sum((qty for d, qty, _ in sold if p.day <= d < until), Decimal("0"))
            qty = max(needed - on_hand, Decimal("0")) + prng.randint(*_PURCHASE_BUFFER)
            period = [rate for d, _, rate in sold
                      if (d.year, d.month) == (p.day.year, p.day.month) or p.day <= d < until]
            last_ref = min(period) if period else last_ref
            rate = (last_ref * prng.randint(*_PURCHASE_MARGIN_PCT) / 100).quantize(Decimal("0.01"),
                                                                                  rounding=ROUND_DOWN)
            # Op 7 / C32: a purchase row's AMOUNT (and its ACCOUNTINGALLOCATIONS) goes out ISDEEMEDPOSITIVE=Yes and
            # NEGATIVE — the signed figure validate_b_voucher checks against the Local Purchases line (−goods).
            amount = -(qty * rate).quantize(Decimal("0.01"))
            planned[p.tag] = (InventorySpec(item=item.name, qty=qty, rate=rate, amount=amount),)
            bought += qty
    return planned


@dataclass
class _OpenBill:
    """A New Ref bill (or the opening bill) still carrying an outstanding magnitude — C35's settlement pool."""
    name: str
    day: date
    left: Decimal


def _settle(party: str, d: date, drawn: Decimal, bill_wise: dict[str, bool],
            open_bills: dict[str, list[_OpenBill]]) -> tuple[Decimal, tuple[BillSpec, ...]]:
    """C35 (review 2026-09-24 #1): a receipt/payment settles a REAL open bill of the same party. The oldest bill
    dated strictly before `d` with anything outstanding is picked (FIFO); the amount is the drawn figure capped at
    what that bill still owes — so a large draw pays it off in full and a small one pays part of it. A bill-wise
    party with nothing open is paid On Account (no bill named). A non-bill-wise party carries no bills at all
    (LESSONS §15 r16). The pool itself is only updated by the caller, once the voucher is known to post."""
    if not bill_wise.get(party, False):
        return drawn, ()
    target = next((b for b in open_bills.get(party, []) if b.day < d and b.left > 0), None)
    if target is None:
        return drawn, (BillSpec(name="On Account", bill_type="On Account", amount=drawn, credit_period=None),)
    amount = min(drawn, target.left)
    return amount, (BillSpec(name=target.name, bill_type="Agst Ref", amount=amount, credit_period=None),)


def _build_receipt(drawn: Decimal, tag: int, d: date, party: str, bill_wise: dict[str, bool],
                   open_bills: dict[str, list[_OpenBill]]) -> VoucherSpec:
    amount, bills = _settle(party, d, drawn, bill_wise, open_bills)
    bank_or_cash = "HDFC Bank Current A/c" if tag % 3 else "Cash"
    # F12: Op 8 — cash/bank debit ISDEEMEDPOSITIVE=Yes AMOUNT=NEGATIVE, party credit ISDEEMEDPOSITIVE=No
    # AMOUNT=POSITIVE ("Cash debit (Yes/−), party credit (No/+)", docs/tally-write-exploration-v4.md Op 8).
    lines = (
        LineSpec(ledger=bank_or_cash, amount=-amount, deemed_positive=True),
        LineSpec(ledger=party, amount=amount, deemed_positive=False),
    )
    narration = f"[{TAG_PREFIX}:{tag}] Receipt from {party}"
    return VoucherSpec(tag=tag, kind="receipt", vch_type="Receipt", date=d, party=party, narration=narration,
                        lines=lines, inventory=(), bills=bills)


def _build_payment(drawn: Decimal, tag: int, d: date, party: str, bill_wise: dict[str, bool],
                   open_bills: dict[str, list[_OpenBill]]) -> VoucherSpec:
    amount, bills = _settle(party, d, drawn, bill_wise, open_bills)
    bank_or_cash = "HDFC Bank Current A/c" if tag % 2 else "Cash"
    # F12: Op 8/9 family — the ledger being paid ISDEEMEDPOSITIVE=Yes AMOUNT=NEGATIVE, cash/bank
    # ISDEEMEDPOSITIVE=No AMOUNT=POSITIVE (matches `create_payment`'s own live-verified convention).
    lines = (
        LineSpec(ledger=party, amount=-amount, deemed_positive=True),
        LineSpec(ledger=bank_or_cash, amount=amount, deemed_positive=False),
    )
    narration = f"[{TAG_PREFIX}:{tag}] Payment to {party}"
    return VoucherSpec(tag=tag, kind="payment", vch_type="Payment", date=d, party=party, narration=narration,
                        lines=lines, inventory=(), bills=bills)


def _build_expense_payment(rng: random.Random, tag: int, d: date) -> VoucherSpec:
    expense = "Office Rent" if tag % 2 else "Bank Charges"
    amount = Decimal(rng.randint(100000, 800000)) / 100
    lines = (                                                                                                # F12
        LineSpec(ledger=expense, amount=-amount, deemed_positive=True),
        LineSpec(ledger="HDFC Bank Current A/c", amount=amount, deemed_positive=False),
    )
    narration = f"[{TAG_PREFIX}:{tag}] Payment for {expense}"
    return VoucherSpec(tag=tag, kind="payment", vch_type="Payment", date=d, party=expense, narration=narration,
                        lines=lines, inventory=(), bills=())


def _vouchers(licence: str, rng: random.Random, ledgers: tuple[LedgerSpec, ...],
              items: tuple[StockItemSpec, ...]) -> tuple[VoucherSpec, ...]:
    bill_wise = {l.name: l.bill_wise for l in ledgers}
    # Plan part 7: a currency ledger (the USD export party) is kept out of the rotation — a 7th name would move
    # `% len(debtor_names)` for every sale and receipt (pinned by test_every_other_voucher_is_byte_identical_to_p7_base).
    debtor_names = [l.name for l in ledgers if l.parent == "Sundry Debtors" and l.currency is None]
    creditor_names = [l.name for l in ledgers if l.parent in ("National Creditors", "Local Creditors")]
    item_names = [i.name for i in items]
    item_rate_hint = {i.name: (5000, 200000) for i in items}  # 50.00-2000.00 rupees, in paise

    # C35: the settlement pool, seeded with each ledger's opening bill (its opening balance's magnitude).
    open_bills: dict[str, list[_OpenBill]] = {
        l.name: [_OpenBill(l.opening_bill, OPENING_BILL_DATE, abs(l.opening))]
        for l in ledgers if l.opening_bill and l.opening is not None}

    # Pass 1 (C41): lay out the calendar and take every shared-stream draw in exactly its pre-C41 place. Sales and
    # expense payments are final here; purchases, receipts and payments are deferred to pass 2 as `_Slot`s.
    laid_out: list[VoucherSpec | _Slot] = []
    tag = 0
    sales_counter = 0
    receipt_counter = 0
    for (y, m) in _months():
        for i in range(VOUCHERS_PER_MONTH):
            tag += 1
            if licence == "educational":
                allowed = _educational_days(y, m)
                day = allowed[i % len(allowed)]
            else:
                day = 1 if i == _FIRST_PURCHASE_SLOT else 2 + (i * 3) % 26                               # C41
            d = date(y, m, day)

            if tag in _USD_TAGS:
                laid_out.append(_build_usd_sale(rng, tag, d))
            elif i < 8:
                sales_counter += 1
                vch_type = SALES_GST_VOUCHER_TYPE if sales_counter % 4 == 0 else "Sales"
                party = HINDI_DEBTOR if sales_counter % 7 == 0 else debtor_names[sales_counter % len(debtor_names)]
                laid_out.append(_build_sales(rng, tag, d, party, vch_type, item_names, item_rate_hint, bill_wise))
            elif i < 13:
                # C41: the pre-C41 random goods figure. Unused now (the stock plan sets the amount) but still drawn,
                # so the shared stream — and every sale, receipt and expense after this slot — is unmoved.
                rng.randint(1000000, 8000000)
                party = creditor_names[(tag + i) % len(creditor_names)]
                laid_out.append(_Slot("purchase", tag, d, party, item_slot=i - 8))
            elif i < 17:
                # C35: `(tag + i) % 6` is always odd here, so receipts only ever reached 3 of the 6 debtors — never
                # Pune Digital Solutions (whose opening bill then could never be settled) nor the non-bill-wise
                # debtor. A plain rotation reaches all six; it draws nothing from `rng`, so no sale moves.
                receipt_counter += 1
                party = debtor_names[receipt_counter % len(debtor_names)]
                drawn = Decimal(rng.randint(500000, 5000000)) / 100    # one draw, as before C35
                laid_out.append(_Slot("receipt", tag, d, party, drawn=drawn))
            elif i < 19:
                party = creditor_names[(tag + i) % len(creditor_names)]
                drawn = Decimal(rng.randint(500000, 4000000)) / 100    # one draw, as before C35
                laid_out.append(_Slot("payment", tag, d, party, drawn=drawn))
            else:
                laid_out.append(_build_expense_payment(rng, tag, d))

    # C41: purchases are planned against every sale, from their own stream.
    sales = [v for v in laid_out if isinstance(v, VoucherSpec) and v.kind == "sales"]
    purchase_slots = [s for s in laid_out if isinstance(s, _Slot) and s.kind == "purchase"]
    stock = _plan_purchases(items, purchase_slots, sales, random.Random(PURCHASE_SEED))

    # Pass 2: build the deferred vouchers in tag order, settling against the C35 pool as before.
    vouchers: list[VoucherSpec] = []
    for entry in laid_out:
        if isinstance(entry, _Slot):
            if entry.kind == "purchase":
                v = _build_purchase(entry.tag, entry.day, entry.party, stock[entry.tag], bill_wise)
            elif entry.kind == "receipt":
                v = _build_receipt(entry.drawn, entry.tag, entry.day, entry.party, bill_wise, open_bills)
            else:
                v = _build_payment(entry.drawn, entry.tag, entry.day, entry.party, bill_wise, open_bills)
        else:
            v = entry

        if v.tag in _CANCELLED_TAGS:
            v = replace(v, cancelled=True)
        elif v.tag in _OPTIONAL_TAGS:
            v = replace(v, optional=True)

        if not (v.cancelled or v.optional or v.skip_reason):
            _post_to_pool(v, open_bills)
        vouchers.append(v)
    return tuple(vouchers)


def _post_to_pool(v: VoucherSpec, open_bills: dict[str, list[_OpenBill]]) -> None:
    """A New Ref opens a bill; an Agst Ref reduces the one it names. Cancelled/optional vouchers never get here —
    neither posts to the books, so neither opens nor settles anything."""
    for b in v.bills:
        if b.bill_type == "New Ref":
            open_bills.setdefault(v.party, []).append(_OpenBill(b.name, v.date, b.amount))
        elif b.bill_type == "Agst Ref":
            target = next(o for o in open_bills[v.party] if o.name == b.name)
            target.left -= b.amount


def fy_label(day: date) -> str:
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def expected_figures(dataset: Dataset) -> Expected:
    """Balances and counts computed from the dataset alone — the anchor probes 16/18 check Tally against."""
    running: dict[str, Decimal] = {l.name: (l.opening or Decimal("0.00")) for l in dataset.ledgers}
    month_end: dict[tuple[str, date], Decimal] = {}
    fy_opening: dict[tuple[str, date], Decimal] = {}
    by_month: dict[tuple[int, int], int] = {}
    by_fy: dict[str, int] = {}
    bills: dict[tuple[str, str], Decimal] = {(l.name, l.opening_bill): abs(l.opening)
                                             for l in dataset.ledgers if l.opening_bill and l.opening is not None}
    stock: dict[str, Decimal] = {i.name: (i.opening_qty or Decimal("0")) for i in dataset.items}       # C41
    stock_month_end: dict[tuple[str, date], Decimal] = {}
    # C47 (live 2026-09-25, logs/setup-b-forex-live-2026-09-25.log): TallyPrime values a forex ledger at the rate of
    # its LATEST-dated forex voucher — `-$1609.71 @ ? 82.58/$ = -? 132929.85`, not the sum of the INR bases
    # (-133113.72). Its counter-ledger (Export Sales) keeps the bases. Per currency ledger: signed face total and the
    # latest rate so far (vouchers are walked in (date, tag) order).
    currency_ledgers = {l.name for l in dataset.ledgers if l.currency}
    fx_face: dict[str, Decimal] = {}
    fx_rate: dict[str, Decimal] = {}
    revaluation_at: dict[date, Decimal] = {}

    forex_bases: dict[str, Decimal] = {}

    def _revaluation(name: str) -> Decimal:
        """Face total × latest rate (HALF_UP to paise; 1609.71 × 82.58 has no tie) − the bases posted so far."""
        revalued = (fx_face[name] * fx_rate[name]).quantize(Decimal("0.01"), ROUND_HALF_UP)
        return revalued - forex_bases[name]

    def value(name: str) -> Decimal:
        return running[name] + (_revaluation(name) if name in fx_face else Decimal("0.00"))
    for (year, month) in _months():
        last = date(year, month, monthrange(year, month)[1])
        if month == 4:
            for name in running:
                fy_opening[(name, date(year, 4, 1))] = value(name)
        for v in sorted(dataset.vouchers, key=lambda v: (v.date, v.tag)):
            if (v.date.year, v.date.month) != (year, month) or v.skip_reason:     # C36: never written
                continue
            by_month[(year, month)] = by_month.get((year, month), 0) + 1
            by_fy[fy_label(v.date)] = by_fy.get(fy_label(v.date), 0) + 1
            # Counted, but moves nothing. C42 (live run 4, 2026-09-24): Tally leaves an OPTIONAL voucher out of
            # every balance too, not just out of bills/stock — the optional check used to sit below the ledger lines.
            if v.cancelled or v.optional:
                continue
            for line in v.lines:
                running[line.ledger] = running.get(line.ledger, Decimal("0.00")) + line.amount
                if line.ledger in currency_ledgers and v.fx_rate is not None:                         # C47
                    fx_face[line.ledger] = fx_face.get(line.ledger, Decimal("0.00")) + (
                        -v.fx_amount if line.amount < 0 else v.fx_amount)
                    fx_rate[line.ledger] = v.fx_rate
                    forex_bases[line.ledger] = forex_bases.get(line.ledger, Decimal("0.00")) + line.amount
            for inv in v.inventory:              # C41: purchases bring stock in, sales take it out
                stock[inv.item] += inv.qty if v.kind == "purchase" else -inv.qty
            for b in v.bills:
                if b.bill_type == "New Ref":
                    bills[(v.party, b.name)] = b.amount
                elif b.bill_type == "Agst Ref":
                    bills[(v.party, b.name)] -= b.amount
        for name in running:
            month_end[(name, last)] = value(name)
        revaluation_at[last] = sum((_revaluation(name) for name in fx_face), Decimal("0.00"))
        for name, qty in stock.items():
            stock_month_end[(name, last)] = qty
    outstanding = {key: left for key, left in bills.items() if left != 0}
    return Expected(month_end, fy_opening, by_month, by_fy, outstanding, stock_month_end, revaluation_at)


def generate(licence: str = "licensed") -> Dataset:
    rng = random.Random(SEED)
    groups = _groups()
    units = _units()
    items = _items()
    ledgers = _ledgers()
    vouchers = _vouchers(licence, rng, ledgers, items)
    return Dataset(groups=groups, units=units, items=items, ledgers=ledgers, vouchers=vouchers, licence=licence,
                   currencies=(USD_CURRENCY,))
