"""Write helpers for the automated operator (S0-D9, spec §5.8). Verified shapes only:

- Payment via ALLLEDGERENTRIES.LIST + Accounting Voucher View (docs/tally-write-exploration-v4.md Op 8; live 2026-09-22)
- header-field alter and delete by Master ID, DATE="DD-MMM-YYYY" (v4 "Voucher delete"; live 2026-09-22)
- ledger create / delete with NAME.LIST, rename with NAME.LIST (v4 "Ledger delete", "permanent lock"), EMAIL alter
- company rename with a <NAME> child (live 2026-09-22 — the NAME.LIST variant does nothing)

Every write is read back (LESSONS §12). Writes go only to companies with "Probe" in the name; the one exception is
renaming the fresh seed copy to company A, which the operator does only on its own s0probe Tally.

**The dataset<->wire sign boundary (bitten three times — Task 5's inventory unit suffix, Task 6's F11 opening
balance, and F11 itself being wrong, C30):** company_b_data.py signs LEDGER OPENINGS debit-negative, credit-positive,
and that is ALSO what Tally reads on the wire. Two places are signed on the wire:
- `create_b_voucher`'s `lines` (AMOUNT + ISDEEMEDPOSITIVE) — Op 6/7's sign convention (Ruling C22, live-verified).
- `create_party_ledger`'s `opening` → OPENINGBALANCE, sent SIGNED: negative = Dr, positive = Cr (Ruling C30,
  2026-09-24). Tally does NOT infer the side from the parent group — Op 5's gotcha / Ruling C21 / F11 were never
  tested (Op 5 only checked that Capital Account existed). Live evidence: tally_bridge/import_builder.py's
  `abs(opening)` landed company A's HDFC −5,00,000 and SBI −2,00,000 as CREDITS
  (docs/specs/2026-09-21-bi-part1-sync-design.md "Settled 2026-09-23";
  tests/fixtures/sync/c33_untyped_2026-09-23/p18_A_ledger_list.xml shows them positive, like Capital Account). Never re-introduce `abs(opening)`.
A stock opening (`create_stock_item`'s `opening_qty`/`opening_rate`) is a quantity and a rate — never negative — but
its OPENINGVALUE is signed on the wire like a ledger opening: the stock is a debit, so it goes out NEGATIVE (C39).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Callable

import httpx

from tally_bridge.envelopes import build_company_list, formula_string, wrap_collection, wrap_report
from tally_bridge.xml_utils import parse_company_list, read_objects, sanitize_xml
from probes.companies import COMPANIES, POPUP_STOCK_GROUP, SEED_COMPANY, THROWAWAY_DATE, THROWAWAY_DATE_TEXT
from probes.licence import LICENCE_REQUEST, LicenceInfo, parse_licence_info
from probes.reads import PRIMARY_NATURE, TB_EXPLODE_VARS, VOUCHER_MONTH_FIELDS, voucher_request
from probes.safety import check_request
from probes.setup.company_b_data import CurrencySpec
from probes.setup.import_xml import ImportResult, esc, wrap_import

READBACK_FROM, READBACK_TO = "01-04-2025", "31-03-2026"
B_READBACK_FROM, B_READBACK_TO = "01-04-2022", "31-03-2026"   # company B's date window (S0-B spec §4)
VOUCHER_FIELDS = ["MasterId", "Narration", "Date", "IsPostDated"]
LEDGER_FIELDS = ["Name", "Parent", "Email", "AlterID"]
GROUP_FIELDS = ["Name", "Parent"]
UNIT_FIELDS = ["Name", "BaseUnits", "Conversion"]
ITEM_FIELDS = ["Name", "BaseUnits", "Parent"]
VOUCHER_TYPE_FIELDS = ["Name", "Parent"]
CURRENCY_FIELDS = ["Name", "MailingName", "ExpandedSymbol", "DecimalSymbol", "OriginalName", "IsSuffix",
                   "HasSpace", "DecimalPlaces"]                     # candidates — plan part 7 Task 2 step 3 compares them
LEDGER_DETAIL_FIELDS = ["Name", "Parent", "CurrencyName", "IsBillWiseOn", "OpeningBalance", "ClosingBalance"]
_PAISA = Decimal("0.01")


# M1: each reserved group's natural side, for `check_opening_side`'s dataset sanity check (the WIRE side comes from
# the sign alone, C30). Primary groups come from reads.PRIMARY_NATURE; the reserved sub-groups map to the primary
# they sit under. A custom group (e.g. "Local Creditors") is deliberately absent: its nature is its root's, which
# only a Tally read can tell, so an opening under one is refused rather than guessed.
_RESERVED_SUBGROUP_PRIMARY: dict[str, str] = {
    "Bank Accounts": "Current Assets", "Cash-in-Hand": "Current Assets", "Deposits (Asset)": "Current Assets",
    "Loans & Advances (Asset)": "Current Assets", "Stock-in-Hand": "Current Assets",
    "Sundry Debtors": "Current Assets",
    "Duties & Taxes": "Current Liabilities", "Provisions": "Current Liabilities",
    "Sundry Creditors": "Current Liabilities",
    "Bank OD A/c": "Loans (Liability)", "Secured Loans": "Loans (Liability)", "Unsecured Loans": "Loans (Liability)",
    "Reserves & Surplus": "Capital Account",
}
_DEBIT_NATURES = frozenset({"assets", "expenses"})
# m1: groups whose ledgers normally carry an opening on EITHER side — Duties & Taxes holds Input GST (a debit: ITC
# carried forward) as well as Output GST (a credit).
_EITHER_SIDE_GROUPS = frozenset({"Duties & Taxes"})


def check_opening_side(name: str, parent: str, opening: Decimal) -> None:
    """Raise ValueError if `opening` (debit negative — Rulings C19/C22/C30) opposes `parent`'s nature.

    A DATASET sanity check, not a wire constraint: since C30 the wire is signed, so a contra-natural opening — a bank
    overdraft under Bank Accounts, a debtor in credit, drawings under Capital Account — would land exactly where its
    sign says. In a generated dataset, though, one is far likelier a sign slip than intended, so it is refused. A
    zero opening has no side and always passes; Duties & Taxes accepts either side (m1)."""
    if opening == 0 or parent in _EITHER_SIDE_GROUPS:
        return
    primary = _RESERVED_SUBGROUP_PRIMARY.get(parent, parent)
    nature = PRIMARY_NATURE.get(primary)
    if nature is None:
        raise ValueError(f"Ledger {name!r}: cannot send an opening under {parent!r} — its nature is unknown here "
                         "(a custom group), so its sign cannot be sanity-checked")
    debit_group = nature in _DEBIT_NATURES
    if (opening < 0) != debit_group:
        side, natural = ("debit", "credit") if opening < 0 else ("credit", "debit")
        raise ValueError(f"Ledger {name!r}: a {side} opening of {opening} under {parent!r} (a {natural}-nature group) "
                         "is contra-natural — almost certainly a sign slip in the dataset (debit negative, credit positive)")


class WriteRefused(Exception):
    """The mutation guard refused; nothing was sent."""


class WriteFailed(Exception):
    """Tally didn't confirm the write, or the read-back didn't show it."""


class WriteTimeout(WriteFailed):
    """Tally didn't answer in time (a modal may be open)."""


class WriteUnverified(WriteFailed):
    """Tally answered created/altered=1 but the read-back doesn't show the object. It may well exist under another
    name: never re-send the create (a duplicate master create raises a blocking modal, LESSONS §15 rule 10)."""


def check_writable(company: str) -> None:
    if "Probe" not in company:
        raise WriteRefused(f"Refusing to write to {company!r}: only companies with 'Probe' in the name.")


def currency_matches(row: dict[str, str], spec: CurrencySpec) -> bool:
    """I5 (review 2026-09-25): a Currency row is `spec` if its symbol or formal name appears in ANY of the listed
    fields — Tally may key the master by the formal name, or return the symbol under another tag (all candidates
    until plan part 7 Task 2). Used for list-before-create and for the read-back."""
    wanted = {spec.symbol, spec.formal_name}
    return any(row.get(name, "") in wanted for name in CURRENCY_FIELDS if row.get(name, ""))


def currency_request(company: str) -> str:
    return wrap_collection("S0BCurrencies", "Currency", CURRENCY_FIELDS, company)


def ledger_detail_request(company: str, name: str) -> str:
    return wrap_collection("S0BLedgerDetail", "Ledger", LEDGER_DETAIL_FIELDS, company,
                           filters=[("S0BLedgerDetailOnly", f"$Name = {formula_string(name)}")])


def b_day_voucher_request(company: str, day: str) -> str:
    """Company B's vouchers on ONE day (DD-MM-YYYY, typed — C33; the day must be 1/2/31 under Educational — C43),
    fetched whole like probe 5's month request, so a forex line comes back exactly as the extractor would see it."""
    return voucher_request("S0FxDay", VOUCHER_MONTH_FIELDS, company, from_date=day, to_date=day)


@dataclass(frozen=True)
class ForexLine:
    """How a forex voucher's lines go on the wire (plan part 7). `fx_amount` is the voucher's face value as a
    MAGNITUDE — every line of a 2-line export sale carries it, signed like that line's INR amount. `form` "full"
    states the base ("… = ₹37216.04", candidate F1); "no_base" leaves Tally to compute it (F2). Both are
    CANDIDATES until plan part 7 Task 2 reads a live forex voucher back.
    `base_symbol` has NO default (Ruling R-SYM, 2026-09-25): it is the NAME of the company's base currency as read
    from Tally — company B's is a literal "?" (logs/p7-forex-discovery-2026-09-25.log) — or "" for the rate written
    without a base symbol. Never hard-code "₹"."""
    symbol: str
    fx_amount: Decimal
    rate: Decimal
    form: str = "full"
    base_symbol: str = field(kw_only=True)


def forex_amount_text(inr: Decimal, forex: ForexLine) -> str:
    """One line's AMOUNT text: `-$448.44 @ ₹82.99/$ = -₹37216.04` (F1) or without the `= …` part (F2). The sign
    is the INR line's sign (debit negative, C22)."""
    sign = "-" if inr < 0 else ""
    text = f"{sign}{forex.symbol}{forex.fx_amount:.2f} @ {forex.base_symbol}{forex.rate:.2f}/{forex.symbol}"
    if forex.form == "full":
        text += f" = {sign}{forex.base_symbol}{abs(inr):.2f}"
    return text


@dataclass(frozen=True)
class CheckedVoucher:
    """What `validate_b_voucher` proved and derived — exactly what `create_b_voucher` then renders."""
    sent_lines: list[tuple[str, Decimal, bool]]
    signed_bills: list[tuple[str, str, Decimal, str | None]]
    nominal_ledger: str
    is_purchase_type: bool
    is_invoice_type: bool
    forex: ForexLine | None = None


def validate_b_voucher(*, vch_type: str, narration: str, party: str, lines: list[tuple[str, Decimal, bool]],
                       inventory: list[tuple[str, str, Decimal, Decimal, Decimal]] = (),
                       bills: list[tuple[str, str, Decimal, str | None]] = (),
                       forex: ForexLine | None = None) -> CheckedVoucher:
    """Every pre-send check of `create_b_voucher`, as a pure function (C37, review #7): no request, no company.
    The loader runs it over the whole dataset BEFORE the first voucher is sent, so a bad voucher stops the load up
    front instead of crashing it mid-run with a ValueError. Raises ValueError naming the voucher."""
    total = sum((amount for _, amount, _ in lines), Decimal("0.00"))
    if total != Decimal("0.00"):
        raise ValueError(f"Voucher {narration!r} does not balance: {total}")
    if forex is not None:
        if inventory or bills:
            raise ValueError(f"Voucher {narration!r}: a forex voucher here carries no inventory and no bills "
                             "(plan part 7 Ruling P7-3: forex bill allocations are unverified)")
        if forex.form not in ("full", "no_base"):
            raise ValueError(f"Voucher {narration!r}: unknown forex form {forex.form!r}")
        product = forex.fx_amount * forex.rate
        base = product.quantize(_PAISA, rounding=ROUND_HALF_UP)
        if base != product.quantize(_PAISA, rounding=ROUND_HALF_EVEN):
            raise ValueError(f"Voucher {narration!r}: {forex.fx_amount} × {forex.rate} = {product} is a rounding tie "
                             "— the stored base would depend on Tally's unmeasured rounding rule")
        wrong = [ledger for ledger, amount, _ in lines if abs(amount) != base]
        if wrong:
            raise ValueError(f"Voucher {narration!r}: lines {wrong} are not face × rate = {base}")

    # Op 6/7 (docs/tally-write-exploration-v4.md) — confirmed 2026-09-23 by tally_bridge/import_builder.py,
    # the production writer that has actually landed invoices in live Tally: stock+GST Sales/Purchase are
    # live-verified only under unprefixed LEDGERENTRIES.LIST + Invoice Voucher View + ISINVOICE=Yes +
    # ISPARTYLEDGER=Yes on the party line. ALLLEDGERENTRIES.LIST + Accounting Voucher View (used here for
    # Receipt/Payment/Journal/Contra, matching create_payment/Op 8/9) is reserved for the non-invoice path.
    # Defined once and reused by the inventory-placement guard below so the two checks cannot drift apart again.
    # I1 (final review): the inventory block's ISDEEMEDPOSITIVE must be derived from the SAME predicate, not
    # re-decided with `vch_type == "Purchase"` — exact equality there meant "Purchase - GST" + inventory
    # emitted No/negative, the mismatched permutation Op 7 records as EXCEPTIONS=1. Same drift Ruling C14
    # fixed one expression over.
    is_purchase_type = vch_type.startswith("Purchase")
    is_invoice_type = is_purchase_type or vch_type.startswith("Sales")

    if inventory and not is_invoice_type:
        raise ValueError("Inventory lines belong on Sales/Purchase only")
    if inventory and len(lines) < 2:
        raise ValueError("Inventory lines need a party line and a nominal ledger line in `lines`")

    # The nominal (goods) ledger for every inventory row's ACCOUNTINGALLOCATIONS.LIST: the first line that isn't
    # the party line. `lines` is expected to list party first, then the nominal Sales/Purchase ledger, then any
    # GST lines (matches Op 6/7 and both this method's callers' test fixtures) — Task 6 must keep that ordering.
    nominal_index = next((i for i, (ledger, _, _) in enumerate(lines) if ledger != party), 0)
    nominal_ledger, nominal_amount, nominal_deemed_positive = lines[nominal_index]
    # C32 (live 2026-09-24, logs/debug-vch1-*.log): with inventory the nominal ledger is carried ONLY by the
    # inventory rows' ACCOUNTINGALLOCATIONS — sending it as a ledger line as well makes Tally count the goods
    # twice (EXCEPTIONS=1, no LINEERROR). Production build_create_sales/purchase_voucher emit party + GST +
    # inventory only. So the nominal line is not sent, and the allocations must carry its amount EXACTLY
    # (signed: Op 6 sale +goods, Op 7 purchase −goods) — then the voucher Tally totals (party + GST +
    # allocations) balances exactly when `lines` does.
    if inventory:
        # Review #8: the nominal line is dropped, so its own flag never reaches Tally — the inventory rows and their
        # allocations go out with the flag the voucher TYPE implies (sale No/+goods, purchase Yes/−goods). A nominal
        # line that disagrees means the caller signed the voucher some other way; Tally would answer EXCEPTIONS=1.
        if nominal_deemed_positive is not is_purchase_type or (nominal_amount < 0) is not is_purchase_type:
            raise ValueError(f"Voucher {narration!r}: the {nominal_ledger!r} line is ISDEEMEDPOSITIVE="
                             f"{'Yes' if nominal_deemed_positive else 'No'} / {nominal_amount}, but a {vch_type} "
                             f"inventory row goes out ISDEEMEDPOSITIVE={'Yes' if is_purchase_type else 'No'} "
                             f"({'−' if is_purchase_type else '+'}goods)")
        allocated = sum((amount for *_, amount in inventory), Decimal("0.00"))
        if allocated != nominal_amount:
            raise ValueError(f"Voucher {narration!r}: inventory allocations to {nominal_ledger!r} total "
                             f"{allocated}, but its line says {nominal_amount} — Tally would not balance")
    sent_lines = [line for i, line in enumerate(lines) if not (inventory and i == nominal_index)]

    # C34 (live UI 2026-09-24): Tally files a bill by the SIGN of its BILLALLOCATIONS AMOUNT — [S0-B:1] sent
    # +9,861.74 under a −9,861.74 sales party line and Inv/1 landed in Bills PAYABLE. Like production
    # _render_bill_allocations, `bills` carry magnitudes and each AMOUNT takes the sign of the party line it
    # nests under (sale Dr −, purchase Cr +, receipt/payment Agst Ref mirror their party line too). The signed
    # bills must add up to that line's amount, or the bill-wise split would not match the ledger.
    signed_bills: list[tuple[str, str, Decimal, str | None]] = []
    if bills:
        for name, _, bill_amount, _ in bills:
            if bill_amount < 0:
                raise ValueError(f"Voucher {narration!r}: bill {name!r} amount {bill_amount} must be a magnitude "
                                 "— the sign is mirrored from the party line (C34)")
        party_amounts = [amount for ledger, amount, _ in sent_lines if ledger == party]
        if len(party_amounts) != 1:
            raise ValueError(f"Voucher {narration!r}: bills need exactly one {party!r} line, got {len(party_amounts)}")
        sign = Decimal("-1") if party_amounts[0] < 0 else Decimal("1")
        signed_bills = [(name, bill_type, sign * bill_amount, credit_period)
                        for name, bill_type, bill_amount, credit_period in bills]
        bill_total = sum((amount for _, _, amount, _ in signed_bills), Decimal("0.00"))
        if bill_total != party_amounts[0]:
            raise ValueError(f"Voucher {narration!r}: bill allocations total {bill_total}, but the {party!r} "
                             f"line is {party_amounts[0]}")

    return CheckedVoucher(sent_lines, signed_bills, nominal_ledger, is_purchase_type, is_invoice_type, forex=forex)


class TallyWriter:
    def __init__(self, http: httpx.Client, say: Callable[[str], None]):
        self.http = http
        self.say = say

    # --- transport --------------------------------------------------------------------------------------------------
    def post(self, xml: str, timeout: float = 30.0) -> str:
        check_request(xml)
        try:
            response = self.http.post("/", content=xml.encode("utf-8"),
                                      headers={"Content-Type": "text/xml; charset=utf-8"},
                                      timeout=httpx.Timeout(timeout, connect=5.0))
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise WriteTimeout(f"Tally didn't answer within {timeout:.0f}s ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            raise WriteFailed(f"Tally request failed: {type(exc).__name__}: {exc}") from exc
        return sanitize_xml(response.text)

    def import_(self, report: str, company: str, inner: str, timeout: float = 30.0) -> ImportResult:
        result = ImportResult.parse(self.post(wrap_import(report, company, inner), timeout))
        self.say(f"import ({report}) → created={result.created} altered={result.altered} deleted={result.deleted} "
                 f"errors={result.errors} exceptions={result.exceptions} lastvchid={result.last_vch_id!r}"
                 + (f" lineerror={result.line_error!r}" if result.line_error else ""))
        return result

    # --- read-backs (uncaptured; probes make their own captured reads) -----------------------------------------------
    def company_names(self) -> list[str]:
        return parse_company_list(self.post(build_company_list(), timeout=10.0))

    def list_vouchers(self, company: str) -> list[dict[str, str]]:
        """Every voucher in the read-back window (READBACK_FROM..READBACK_TO, typed — C33), header fields only."""
        xml = wrap_collection("S0OpVouchers", "Voucher", VOUCHER_FIELDS, company,
                              static_vars={"SVFROMDATE": READBACK_FROM, "SVTODATE": READBACK_TO},
                              extra_collection_xml="<CHILDOF>$$VchTypeAllVouchers</CHILDOF>")
        return read_objects(self.post(xml), "VOUCHER", VOUCHER_FIELDS)

    def voucher(self, company: str, master_id: str) -> dict[str, str] | None:
        return next((row for row in self.list_vouchers(company) if row["MasterId"] == master_id), None)

    def ledger(self, company: str, name: str) -> dict[str, str] | None:
        xml = wrap_collection("S0OpLedger", "Ledger", LEDGER_FIELDS, company,
                              filters=[("S0OpOnly", f"$Name = {formula_string(name)}")])
        rows = [r for r in read_objects(self.post(xml), "LEDGER", LEDGER_FIELDS) if r["Name"] == name]
        return rows[0] if rows else None

    # --- vouchers ---------------------------------------------------------------------------------------------------
    def create_payment(self, company: str, *, ledger: str, amount: Decimal, narration: str,
                       cash_ledger: str = "Cash", date: str = THROWAWAY_DATE, post_dated: bool = False) -> str:
        """Cash → `ledger` Payment; returns its Master ID (LASTVCHID)."""
        check_writable(company)
        if amount <= 0:
            raise ValueError("The payment amount must be positive")
        value = f"{amount:.2f}"
        flag = "\n  <ISPOSTDATED>Yes</ISPOSTDATED>" if post_dated else ""
        inner = f"""<VOUCHER VCHTYPE="Payment" ACTION="Create">
  <DATE>{date}</DATE>
  <NARRATION>{esc(narration)}</NARRATION>
  <VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
  <PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>{flag}
  <ALLLEDGERENTRIES.LIST>
    <LEDGERNAME>{esc(ledger)}</LEDGERNAME>
    <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
    <AMOUNT>-{value}</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
  <ALLLEDGERENTRIES.LIST>
    <LEDGERNAME>{esc(cash_ledger)}</LEDGERNAME>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
    <AMOUNT>{value}</AMOUNT>
  </ALLLEDGERENTRIES.LIST>
</VOUCHER>"""
        result = self.import_("Vouchers", company, inner)
        if result.created != 1 or not result.clean or result.last_vch_id in ("", "0"):
            raise WriteFailed(f"Voucher {narration!r} not created: {result}")
        row = self.voucher(company, result.last_vch_id)
        if row is None or row["Narration"] != narration:
            if post_dated:
                self.say(f"post-dated voucher created (LASTVCHID {result.last_vch_id}) but not listed in the Voucher "
                         "collection")
                return result.last_vch_id
            raise WriteFailed(f"Voucher {narration!r} (Master ID {result.last_vch_id}) not found on read-back")
        if post_dated:
            self.say(f"read-back IsPostDated for Master ID {result.last_vch_id}: {row['IsPostDated']!r}")
        return result.last_vch_id

    def alter_voucher_narration(self, company: str, master_id: str, narration: str, *, vch_type: str = "Payment",
                                date_text: str = THROWAWAY_DATE_TEXT) -> None:
        check_writable(company)
        inner = (f'<VOUCHER DATE="{date_text}" TAGNAME="Master ID" TAGVALUE="{esc(master_id)}" '
                 f'VCHTYPE="{esc(vch_type)}" ACTION="Alter">\n<NARRATION>{esc(narration)}</NARRATION>\n</VOUCHER>')
        result = self.import_("Vouchers", company, inner)
        if result.altered != 1 or result.created != 0 or not result.clean:
            raise WriteFailed(f"Voucher {master_id} not altered (or duplicated): {result}")
        row = self.voucher(company, master_id)
        if row is None or row["Narration"] != narration:
            raise WriteFailed(f"Voucher {master_id}: narration unchanged on read-back (altered=1 is not proof, LESSONS §12)")

    def delete_voucher(self, company: str, master_id: str, *, vch_type: str = "Payment",
                       date_text: str = THROWAWAY_DATE_TEXT) -> None:
        check_writable(company)
        inner = (f'<VOUCHER DATE="{date_text}" VCHTYPE="{esc(vch_type)}" TAGNAME="Master ID" '
                 f'TAGVALUE="{esc(master_id)}" ACTION="Delete"></VOUCHER>')
        result = self.import_("Vouchers", company, inner)
        if result.deleted != 1 or not result.clean:
            raise WriteFailed(f"Voucher {master_id} not deleted: {result}")
        if self.voucher(company, master_id) is not None:
            raise WriteFailed(f"Voucher {master_id} still there after the delete")

    # --- vouchers (company B) ---------------------------------------------------------------------------------------
    def create_b_voucher(self, company: str, *, vch_type: str, date: str, narration: str, party: str,
                         lines: list[tuple[str, Decimal, bool]],
                         inventory: list[tuple[str, str, Decimal, Decimal, Decimal]] = (),
                         bills: list[tuple[str, str, Decimal, str | None]] = (),
                         optional: bool = False, forex: ForexLine | None = None) -> str:
        """Sales/Purchase (with stock + GST), Receipt/Payment/Journal — the caller owns the sign convention (docs

        Op 6/7/8; live 2026-09-22): AMOUNT is signed as given per line, ISDEEMEDPOSITIVE is passed as given, and this
        method's own balance check is the only thing that proves the two agree. Nothing is sent to Tally until the
        voucher is proven to balance. `BILLALLOCATIONS.LIST` nests only under the line whose ledger equals `party`;
        each bill amount is a MAGNITUDE and is sent with that party line's sign (C34).
        `ISCANCELLED` is never written here (cancelling is not reliably settable on import — Task 6 pause step).
        `forex` (plan part 7, a CANDIDATE shape until Task 2 measures it): every line's AMOUNT goes out as a forex
        expression (`forex_amount_text`) of that line's INR amount; no inventory, no bills (P7-3).

        **Ordering contract for `lines` when `inventory` is non-empty:** the party line first, then the nominal
        Sales/Purchase ledger, then any GST lines — `ACCOUNTINGALLOCATIONS.LIST` on every inventory row points at
        the first non-party line, so a caller that puts a GST line there instead would silently misallocate goods
        to a tax ledger. That nominal line itself is NOT sent (C32) — its amount must equal the signed sum of the
        inventory amounts, which is checked before anything is sent. Enforced only to the extent that `lines` must have at least 2 entries when `inventory` is
        given (`len(lines) >= 2`); the specific ordering itself cannot be checked at this layer (ledger names carry
        no semantic tag) and is the caller's (Task 6's) responsibility.

        **`is_invoice_type` limitation:** invoice mode is decided from the voucher type NAME (`Sales`, `Purchase`,
        or a name starting with either, e.g. `"Sales - GST"` / `"Purchase - GST"`) — this layer has no voucher-type
        collection to look up a type's PARENT. A custom type whose name does not start with Sales/Purchase while
        its parent does (e.g. `"Export Invoice"` parented to `Sales`) will be misclassified as non-invoice and
        emit `ALLLEDGERENTRIES.LIST` + `Accounting Voucher View`, which Tally will likely answer with
        `EXCEPTIONS=1`. Callers naming such a type must alias it to start with `Sales`/`Purchase`.
        """
        check_writable(company)
        checked = validate_b_voucher(vch_type=vch_type, narration=narration, party=party, lines=lines,
                                     inventory=inventory, bills=bills, forex=forex)
        sent_lines, signed_bills = checked.sent_lines, checked.signed_bills
        nominal_ledger, is_purchase_type, is_invoice_type = (checked.nominal_ledger, checked.is_purchase_type,
                                                             checked.is_invoice_type)
        ledger_tag = "LEDGERENTRIES.LIST" if is_invoice_type else "ALLLEDGERENTRIES.LIST"

        ledger_blocks = []
        for ledger, amount, deemed_positive in sent_lines:
            bill_xml = ""
            if ledger == party and bills:
                bill_xml = "".join(
                    f"\n    <BILLALLOCATIONS.LIST>\n      <NAME>{esc(name)}</NAME>\n      "
                    f"<BILLTYPE>{esc(bill_type)}</BILLTYPE>\n      <AMOUNT>{bill_amount:.2f}</AMOUNT>"
                    + (f"\n      <BILLCREDITPERIOD>{esc(credit_period)}</BILLCREDITPERIOD>" if credit_period else "")
                    + "\n    </BILLALLOCATIONS.LIST>"
                    for name, bill_type, bill_amount, credit_period in signed_bills)
            party_flag = ("\n    <ISPARTYLEDGER>Yes</ISPARTYLEDGER>" if is_invoice_type and ledger == party else "")
            amount_text = forex_amount_text(amount, checked.forex) if checked.forex else f"{amount:.2f}"
            ledger_blocks.append(
                f"""  <{ledger_tag}>
    <LEDGERNAME>{esc(ledger)}</LEDGERNAME>
    <ISDEEMEDPOSITIVE>{"Yes" if deemed_positive else "No"}</ISDEEMEDPOSITIVE>
    <AMOUNT>{esc(amount_text)}</AMOUNT>{party_flag}{bill_xml}
  </{ledger_tag}>""")

        inventory_deemed_positive = "Yes" if is_purchase_type else "No"        # Op 7: goods in (Yes/−)
        inventory_blocks = []
        for item, unit, qty, rate, amount in inventory:
            qty_unit = f"{qty} {esc(unit)}"
            inventory_blocks.append(
                f"""  <ALLINVENTORYENTRIES.LIST>
    <STOCKITEMNAME>{esc(item)}</STOCKITEMNAME>
    <ISDEEMEDPOSITIVE>{inventory_deemed_positive}</ISDEEMEDPOSITIVE>
    <RATE>{rate:.2f}/{esc(unit)}</RATE>
    <AMOUNT>{amount:.2f}</AMOUNT>
    <ACTUALQTY>{qty_unit}</ACTUALQTY>
    <BILLEDQTY>{qty_unit}</BILLEDQTY>
    <ACCOUNTINGALLOCATIONS.LIST>
      <LEDGERNAME>{esc(nominal_ledger)}</LEDGERNAME>
      <ISDEEMEDPOSITIVE>{inventory_deemed_positive}</ISDEEMEDPOSITIVE>
      <AMOUNT>{amount:.2f}</AMOUNT>
    </ACCOUNTINGALLOCATIONS.LIST>
  </ALLINVENTORYENTRIES.LIST>""")

        optional_xml = "\n  <ISOPTIONAL>Yes</ISOPTIONAL>" if optional else ""
        if is_invoice_type:
            header = (f'<VOUCHER VCHTYPE="{esc(vch_type)}" ACTION="Create">\n'
                      f"  <DATE>{date}</DATE>\n  <NARRATION>{esc(narration)}</NARRATION>\n"
                      f"  <VOUCHERTYPENAME>{esc(vch_type)}</VOUCHERTYPENAME>\n"
                      f"  <PARTYLEDGERNAME>{esc(party)}</PARTYLEDGERNAME>\n"
                      f"  <PARTYNAME>{esc(party)}</PARTYNAME>\n"
                      f"  <PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>\n"
                      f"  <ISINVOICE>Yes</ISINVOICE>\n"
                      f"  <EFFECTIVEDATE>{date}</EFFECTIVEDATE>{optional_xml}")
        else:
            header = (f'<VOUCHER VCHTYPE="{esc(vch_type)}" ACTION="Create">\n'
                      f"  <DATE>{date}</DATE>\n  <NARRATION>{esc(narration)}</NARRATION>\n"
                      f"  <VOUCHERTYPENAME>{esc(vch_type)}</VOUCHERTYPENAME>\n"
                      f"  <PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>{optional_xml}")
        inner = f"""{header}
{chr(10).join(ledger_blocks)}
{chr(10).join(inventory_blocks)}
</VOUCHER>"""
        result = self.import_("Vouchers", company, inner)
        if result.created != 1 or not result.clean or result.last_vch_id in ("", "0"):
            raise WriteFailed(f"Voucher {narration!r} not created: {result}")
        return result.last_vch_id

    # --- ledgers ----------------------------------------------------------------------------------------------------
    def create_ledger(self, company: str, name: str, parent: str) -> None:
        check_writable(company)
        if self.ledger(company, name) is not None:
            raise WriteFailed(f"Ledger {name!r} already exists — a duplicate create freezes Tally (LESSONS §15 rule 10)")
        inner = (f'<LEDGER NAME="{esc(name)}" ACTION="Create">\n  <NAME.LIST><NAME>{esc(name)}</NAME></NAME.LIST>\n'
                 f'  <PARENT>{esc(parent)}</PARENT>\n  <ISBILLWISEON>No</ISBILLWISEON>\n</LEDGER>')
        result = self.import_("All Masters", company, inner)
        if result.created != 1 or not result.clean:
            raise WriteFailed(f"Ledger {name!r} not created: {result}")
        row = self.ledger(company, name)
        if row is None or row["Parent"] != parent:
            raise WriteFailed(f"Ledger {name!r} not found under {parent!r} on read-back")

    def alter_ledger_email(self, company: str, name: str, email: str) -> None:
        check_writable(company)
        if not email:
            raise ValueError("An empty-value alter is silently ignored by Tally (live 2026-09-22); never 'clear' a field")
        inner = f'<LEDGER NAME="{esc(name)}" ACTION="Alter">\n<EMAIL>{esc(email)}</EMAIL>\n</LEDGER>'
        result = self.import_("All Masters", company, inner)
        if result.altered != 1 or not result.clean:
            raise WriteFailed(f"Ledger {name!r} not altered: {result}")
        row = self.ledger(company, name)
        if row is None or row["Email"] != email:
            raise WriteFailed(f"Ledger {name!r}: EMAIL unchanged on read-back (altered=1 is not proof, LESSONS §12)")

    def delete_ledger(self, company: str, name: str) -> None:
        check_writable(company)
        inner = f'<LEDGER NAME="{esc(name)}" ACTION="Delete">\n<NAME.LIST><NAME>{esc(name)}</NAME></NAME.LIST>\n</LEDGER>'
        result = self.import_("All Masters", company, inner)
        if result.deleted != 1 or not result.clean:
            raise WriteFailed(f"Ledger {name!r} not deleted: {result}")
        if self.ledger(company, name) is not None:
            raise WriteFailed(f"Ledger {name!r} still there after the delete")

    def rename_ledger(self, company: str, old: str, new: str) -> None:
        check_writable(company)
        if self.ledger(company, new) is not None:
            raise WriteFailed(f"A ledger {new!r} already exists")
        inner = f'<LEDGER NAME="{esc(old)}" ACTION="Alter">\n<NAME.LIST><NAME>{esc(new)}</NAME></NAME.LIST>\n</LEDGER>'
        result = self.import_("All Masters", company, inner)
        if result.altered != 1 or not result.clean:
            raise WriteFailed(f"Ledger {old!r} not renamed: {result}")
        if self.ledger(company, new) is None or self.ledger(company, old) is not None:
            raise WriteFailed(f"Ledger rename {old!r} → {new!r} not visible on read-back")

    # --- masters (company B) -----------------------------------------------------------------------------------------
    def list_groups(self, company: str) -> dict[str, str]:
        xml = wrap_collection("S0BGroups", "Group", GROUP_FIELDS, company)
        return {row["Name"]: row.get("Parent", "") for row in read_objects(self.post(xml), "GROUP", GROUP_FIELDS)}

    def list_units(self, company: str) -> list[str]:
        xml = wrap_collection("S0BUnits", "Unit", UNIT_FIELDS, company)
        return [row["Name"] for row in read_objects(self.post(xml), "UNIT", UNIT_FIELDS)]

    def list_stock_items(self, company: str) -> dict[str, str]:
        xml = wrap_collection("S0BItems", "StockItem", ITEM_FIELDS, company)
        return {row["Name"]: row.get("BaseUnits", "")
                for row in read_objects(self.post(xml), "STOCKITEM", ITEM_FIELDS)}

    def list_ledgers(self, company: str) -> dict[str, str]:
        xml = wrap_collection("S0BLedgers", "Ledger", LEDGER_FIELDS, company)
        return {row["Name"]: row.get("Parent", "") for row in read_objects(self.post(xml), "LEDGER", LEDGER_FIELDS)}

    def list_voucher_types(self, company: str) -> list[str]:
        xml = wrap_collection("S0BVoucherTypes", "VoucherType", VOUCHER_TYPE_FIELDS, company)
        return [row["Name"] for row in read_objects(self.post(xml), "VOUCHERTYPE", VOUCHER_TYPE_FIELDS)]

    def currency_rows(self, company: str) -> list[dict[str, str]]:
        return read_objects(self.post(currency_request(company)), "CURRENCY", CURRENCY_FIELDS)

    def list_currencies(self, company: str) -> dict[str, dict[str, str]]:
        return {row["Name"]: row for row in self.currency_rows(company)}

    def create_currency(self, company: str, spec: CurrencySpec) -> bool:
        """List first (LESSONS §15 rule 10), create, read back. No verified op exists — the tags are candidates
        (plan part 7 Task 2 step 3). Returns False when it was already there (nothing sent). "There" means
        `currency_matches` on any listed row (I5). A create Tally confirms that the read-back can't find raises
        `WriteUnverified` — never a plain refusal, because re-sending it would be a duplicate create."""
        check_writable(company)
        if any(currency_matches(row, spec) for row in self.currency_rows(company)):
            self.say(f"currency {spec.symbol!r} already exists — not re-created")
            return False
        inner = (f'<CURRENCY NAME="{esc(spec.symbol)}" ACTION="Create">\n'
                 f"  <NAME>{esc(spec.symbol)}</NAME>\n"
                 f"  <MAILINGNAME>{esc(spec.formal_name)}</MAILINGNAME>\n"
                 f"  <EXPANDEDSYMBOL>{esc(spec.formal_name)}</EXPANDEDSYMBOL>\n"
                 f"  <DECIMALSYMBOL>{esc(spec.decimal_symbol)}</DECIMALSYMBOL>\n"
                 f"  <DECIMALPLACES>{spec.decimal_places}</DECIMALPLACES>\n"
                 "  <ISSUFFIX>No</ISSUFFIX>\n  <HASSPACE>No</HASSPACE>\n</CURRENCY>")
        result = self.import_("All Masters", company, inner)
        if not ((result.created == 1 or result.altered == 1) and result.clean):          # rule 11
            raise WriteFailed(f"Currency {spec.symbol!r} not created: {result}")
        if not any(currency_matches(row, spec) for row in self.currency_rows(company)):
            raise WriteUnverified(f"Currency {spec.symbol!r}: Tally answered {result} but no listed currency carries "
                                  f"{spec.symbol!r} or {spec.formal_name!r} — do NOT re-run the create; inspect the "
                                  "Currency list (currencies_after.xml) and Tally's Currency masters in the UI")
        return True

    def ledger_details(self, company: str, name: str) -> dict[str, str] | None:
        rows = [r for r in read_objects(self.post(ledger_detail_request(company, name)), "LEDGER",
                                        LEDGER_DETAIL_FIELDS) if r["Name"] == name]
        return rows[0] if rows else None

    def delete_b_voucher(self, company: str, master_id: str, *, vch_type: str, day: str, date_text: str) -> None:
        """Delete by Master ID and verify in the voucher's OWN day. `delete_voucher` verifies through
        `list_vouchers`, i.e. company A's FY 2025-26 window, where a 2022 voucher is never listed — a delete that
        did not stick would pass there (plan part 7, fact 3)."""
        check_writable(company)
        inner = (f'<VOUCHER DATE="{esc(date_text)}" VCHTYPE="{esc(vch_type)}" TAGNAME="Master ID" '
                 f'TAGVALUE="{esc(master_id)}" ACTION="Delete"></VOUCHER>')
        result = self.import_("Vouchers", company, inner)
        if result.deleted != 1 or not result.clean:
            raise WriteFailed(f"Voucher {master_id} not deleted: {result}")
        still = [r for r in read_objects(self.post(b_day_voucher_request(company, day)), "VOUCHER", ["MasterID"])
                 if r.get("MasterID") == master_id]
        if still:
            raise WriteFailed(f"Voucher {master_id} still there on {day} after the delete")

    def b_trial_balance(self, company: str, from_date: str, to_date: str) -> str:
        """An exploded (EXPLODEFLAG=Yes, probe 17) Trial Balance — company_b.py's `_verify` compares
        primary/second-level group totals only from this; ledger-level TB shape is deferred to probes 16/17
        (S0-D7: a probe never guesses a request another probe must confirm)."""
        return self.post(wrap_report("Trial Balance", from_date, to_date, company, extra_vars=TB_EXPLODE_VARS),
                         timeout=60.0)

    def b_bills_receivable(self, company: str, as_on: str) -> str:
        return self.post(wrap_report("Bills Receivable", as_on, as_on, company), timeout=60.0)

    def create_group(self, company: str, name: str, parent: str) -> None:
        check_writable(company)
        if name in self.list_groups(company):
            self.say(f"{name} already exists — not re-created")
            return
        inner = (f'<GROUP NAME="{esc(name)}" ACTION="Create">\n  <NAME.LIST><NAME>{esc(name)}</NAME></NAME.LIST>\n'
                 f'  <PARENT>{esc(parent)}</PARENT>\n</GROUP>')
        result = self.import_("All Masters", company, inner)
        if not ((result.created == 1 or result.altered == 1) and result.clean):
            raise WriteFailed(f"Group {name!r} not created: {result}")
        if name not in self.list_groups(company):
            raise WriteFailed(f"Group {name!r} not found on read-back")

    def create_unit(self, company: str, name: str, *, first_unit: str | None = None, second_unit: str | None = None,
                    conversion: int | None = None) -> None:
        """A simple unit (no compound arguments), or a compound unit (Ruling C31): `first_unit` x `conversion` =
        `second_unit`, sent as BASEUNITS=first, ADDITIONALUNITS=second. Both must already exist as simple units
        and must differ — live Tally answered BASEUNITS=ADDITIONALUNITS=Nos with "Next Unit already contains the
        First unit!" (logs/setup-b-live-2026-09-24.log). Tally names the compound "<first> of <conversion>
        <second>", so `name` must be exactly that or the read-back could never find it."""
        check_writable(company)
        is_compound = any(v is not None for v in (first_unit, second_unit, conversion))
        if is_compound:
            if first_unit is None or second_unit is None or conversion is None:
                raise ValueError(f"Compound unit {name!r} needs first_unit, second_unit and conversion")
            if first_unit == second_unit:
                raise ValueError(f"Compound unit {name!r}: the first and second units must differ")
            tally_name = f"{first_unit} of {conversion} {second_unit}"
            if name != tally_name:
                raise ValueError(f"Compound unit {name!r}: Tally will name it {tally_name!r}")
        if name in self.list_units(company):
            self.say(f"{name} already exists — not re-created")
            return
        compound = (f"\n  <BASEUNITS>{esc(first_unit)}</BASEUNITS>\n  <ADDITIONALUNITS>{esc(second_unit)}</ADDITIONALUNITS>"
                    f"\n  <CONVERSION>{conversion}</CONVERSION>\n  <ISSIMPLEUNIT>No</ISSIMPLEUNIT>"
                    if is_compound else "\n  <ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>")
        inner = f'<UNIT ACTION="Create">\n  <NAME>{esc(name)}</NAME>{compound}\n</UNIT>'
        result = self.import_("All Masters", company, inner)
        if not ((result.created == 1 or result.altered == 1) and result.clean):
            raise WriteFailed(f"Unit {name!r} not created: {result}")
        if name not in self.list_units(company):
            raise WriteFailed(f"Unit {name!r} not found on read-back")

    def create_stock_item(self, company: str, name: str, *, unit: str, hsn: str | None = None,
                          opening_qty: Decimal | None = None, opening_rate: Decimal | None = None,
                          qty_unit: str | None = None) -> None:
        """`unit` is the item's BASEUNITS (for a compound, its full name, e.g. "Box of 10 Nos"). `qty_unit` is the
        unit its opening quantity and rate are written in — for a compound its FIRST unit (C40, live 2026-09-24:
        "15 Box of 10 Nos" answered created=1 and stored no opening; "15 Box" / "950.00/Box" stored it). Defaults
        to `unit`, which is right for a simple unit."""
        check_writable(company)
        qty_unit = qty_unit or unit
        if name in self.list_stock_items(company):
            self.say(f"{name} already exists — not re-created")
            return
        gst = (f"\n  <GSTAPPLICABLE>Applicable</GSTAPPLICABLE>\n  <GSTTYPEOFSUPPLY>Goods</GSTTYPEOFSUPPLY>"
               f"\n  <HSNCODE>{esc(hsn)}</HSNCODE>\n  <HSN>{esc(hsn)}</HSN>\n  "
               f"<HSNDETAILS.LIST><HSNCODE>{esc(hsn)}</HSNCODE></HSNDETAILS.LIST>"
               if hsn else "\n  <GSTAPPLICABLE>Not Applicable</GSTAPPLICABLE>")
        opening = ("" if opening_qty is None else
                   f"\n  <OPENINGBALANCE>{opening_qty} {esc(qty_unit)}</OPENINGBALANCE>"
                   f"\n  <OPENINGRATE>{opening_rate:.2f}/{esc(qty_unit)}</OPENINGRATE>"
                   # C39 (live 2026-09-24, logs/stock-opening-live-check-2026-09-24.log): OPENINGVALUE is read by its
                   # SIGN like a ledger opening (C30) — +10200 landed on the Cr side of Current Assets and the TB did
                   # not close; −10200 read back −10200 and it balanced. Opening stock is an asset: a debit, negative.
                   f"\n  <OPENINGVALUE>{-(opening_qty * opening_rate):.2f}</OPENINGVALUE>")
        inner = (f'<STOCKITEM NAME="{esc(name)}" ACTION="Create">\n  <NAME.LIST><NAME>{esc(name)}</NAME></NAME.LIST>\n'
                 f'  <BASEUNITS>{esc(unit)}</BASEUNITS>{gst}{opening}\n</STOCKITEM>')
        result = self.import_("All Masters", company, inner)
        if not ((result.created == 1 or result.altered == 1) and result.clean):
            raise WriteFailed(f"Stock item {name!r} not created: {result}")
        if name not in self.list_stock_items(company):
            raise WriteFailed(f"Stock item {name!r} not found on read-back")

    def create_party_ledger(self, company: str, name: str, *, parent: str, bill_wise: bool,
                            opening: Decimal | None = None, gstin: str | None = None,
                            allow_contra_natural: bool = False, currency: str | None = None) -> None:
        """`opening` is signed (debit negative, credit positive — company_b_data.py's convention) and goes on the
        wire AS IS: Tally reads OPENINGBALANCE's sign, negative = Dr, positive = Cr (Ruling C30, overturning
        C21/F11 — see the module docstring for the company-A evidence). Never `abs()` it.

        `check_opening_side` (M1) runs FIRST — before the "already exists" skip, on purpose (m4): bad dataset
        signs fail loud on every run, even a re-run where the ledger exists and nothing would be sent. Do not move
        it after the skip. `allow_contra_natural` skips it — ONLY for sign_check.run_positive (C38), whose whole
        point is a credit opening under a debit-natured group.

        `currency` (plan part 7) sends CURRENCYNAME — a candidate tag — and reads it back through `ledger_details`."""
        check_writable(company)
        if opening is not None and not allow_contra_natural:
            check_opening_side(name, parent, opening)
        if self.ledger(company, name) is not None:
            self.say(f"{name} already exists — not re-created")
            return
        extra = "".join(filter(None, [
            f"\n  <OPENINGBALANCE>{opening:.2f}</OPENINGBALANCE>" if opening is not None else "",
            f"\n  <PARTYGSTIN>{esc(gstin)}</PARTYGSTIN>\n  <GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>" if gstin
            else "\n  <GSTREGISTRATIONTYPE>Unregistered</GSTREGISTRATIONTYPE>",
            f"\n  <CURRENCYNAME>{esc(currency)}</CURRENCYNAME>" if currency else "",
        ]))
        inner = (f'<LEDGER NAME="{esc(name)}" ACTION="Create">\n  <NAME.LIST><NAME>{esc(name)}</NAME></NAME.LIST>\n'
                 f'  <PARENT>{esc(parent)}</PARENT>\n  <ISBILLWISEON>{"Yes" if bill_wise else "No"}</ISBILLWISEON>'
                 f'\n  <LEDSTATENAME>Maharashtra</LEDSTATENAME>{extra}\n</LEDGER>')
        result = self.import_("All Masters", company, inner)
        if not ((result.created == 1 or result.altered == 1) and result.clean):
            raise WriteFailed(f"Party ledger {name!r} not created: {result}")
        row = self.ledger(company, name)
        if row is None or row["Parent"] != parent:
            raise WriteFailed(f"Party ledger {name!r} not found under {parent!r} on read-back")
        if currency:
            details = self.ledger_details(company, name)
            if details is None or details["CurrencyName"] != currency:
                raise WriteFailed(f"Party ledger {name!r}: CURRENCYNAME {currency!r} did not stick on read-back")

    # --- company ----------------------------------------------------------------------------------------------------
    def rename_company(self, old: str, new: str) -> None:
        if not (old == SEED_COMPANY and new == COMPANIES["A"]):
            check_writable(old)
            check_writable(new)
        inner = f'<COMPANY NAME="{esc(old)}" ACTION="Alter"><NAME>{esc(new)}</NAME></COMPANY>'
        result = self.import_("All Masters", old, inner)
        if not result.clean:
            raise WriteFailed(f"Company rename refused: {result}")
        names = self.company_names()
        if names != [new]:
            raise WriteFailed(f"Company rename didn't take effect: Tally shows {names}")

    # --- popup, report view, licence ---------------------------------------------------------------------------------
    def raise_duplicate_master_popup(self, company: str, timeout: float = 10.0) -> str:
        """LESSONS §15 rule 10: a CREATE of an existing stock group raises a blocking modal. Returns what happened."""
        check_writable(company)
        inner = (f'<STOCKGROUP NAME="{esc(POPUP_STOCK_GROUP)}" ACTION="Create">\n'
                 f'  <NAME.LIST><NAME>{esc(POPUP_STOCK_GROUP)}</NAME></NAME.LIST>\n  <PARENT/>\n</STOCKGROUP>')
        try:
            result = self.import_("All Masters", company, inner, timeout=timeout)
        except WriteTimeout:
            self.say("duplicate stock-group create timed out — Tally is showing its modal (expected)")
            return "timeout"
        return f"answered: created={result.created} altered={result.altered} errors={result.errors}"

    def export_report(self, company: str, report: str, from_date: str, to_date: str) -> int:
        """A TYPE=Data export — the automated stand-in for 'open this report in the UI'. Returns the response size."""
        return len(self.post(wrap_report(report, from_date, to_date, company), timeout=60.0).encode("utf-8"))

    def licence_info(self) -> LicenceInfo:
        return parse_licence_info(self.post(LICENCE_REQUEST, timeout=10.0))
