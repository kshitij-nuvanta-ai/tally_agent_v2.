"""Ingest step 1: parse and validate wire objects (S1 spec §5.3, §11, §12 steps 5 and 7).

Pure: no DB, no HTTP. `parse_objects` collects every error across the whole batch -- it never stops at the first
bad object (§12 step 5) -- but within one object it stops at the first bad field: once a field fails to parse, the
rest of that object can't be trusted, so the object is dropped from `parsed` and the batch moves to the next
object. `ObjectError.detail` always names the failing **field**, never the value that failed to parse (decision
14) -- the field name is threaded through every call site below instead of ever formatting the exception text.
"""
from __future__ import annotations

import logging
from datetime import datetime

from v2.cloud.ingest.parsed import ObjectError, ObjectWarning, PBalance, PBill, PInv, PLine, PMaster, PVoucher
from v2.cloud.parity.rung0 import voucher_balances
from v2.contract.parse import WireParseError, amount, counter, credit_period, logical, name as parse_name, quantity
from v2.contract.parse import rate as parse_rate, tally_date

logger = logging.getLogger(__name__)

# S1 spec §5.3: required keys per master kind (missing -> `missing_field`).
MASTER_REQUIRED: dict[str, tuple[str, ...]] = {
    "currency": ("guid", "alterid", "name", "expandedsymbol"),
    "group": ("guid", "alterid", "name", "parent"),
    "voucher_type": ("guid", "alterid", "name", "parent"),
    "ledger": ("guid", "alterid", "name", "parent"),
    "stock_group": ("guid", "alterid", "name", "parent"),
    "unit": ("guid", "alterid", "name"),
    "stock_item": ("guid", "alterid", "name", "parent", "baseunits"),
}
BALANCE_REQUIRED: dict[str, tuple[str, ...]] = {
    "ledger_balance": ("guid", "name", "closingbalance", "captured_at"),
    "stock_balance": ("guid", "name", "closingvalue", "captured_at"),
}
VOUCHER_REQUIRED = ("guid", "masterid", "alterid", "date", "vouchertypename", "iscancelled", "isoptional",
                    "ispostdated", "ledger_entries")
LINE_REQUIRED = ("ledgername", "amount", "isdeemedpositive")
INVENTORY_REQUIRED = ("stockitemname", "amount")

# Field-name -> parsed-type routing for the "extra" (non-core) keys on a master (§5.3 optional columns). A
# `stock_item` master's `closingbalance` / `openingbalance` are quantities (the same key `stock_balance` marks
# "(qty)" in §5.3), not money -- unlike every other kind, where those two keys ARE money (review Important #2).
_AMOUNT_FIELDS = frozenset({"openingbalance", "closingbalance", "closingvalue"})
_LOGICAL_FIELDS = frozenset({"isrevenue", "affectsgrossprofit", "isdeemedpositive", "isbillwiseon", "issimpleunit"})
_STOCK_ITEM_QUANTITY_FIELDS = frozenset({"openingbalance", "closingbalance"})
_MASTER_CORE = frozenset({"guid", "alterid", "name", "parent"})


class _Bad(Exception):
    """Raised once a field in the current object fails to parse; caught by `parse_objects` to move to the next
    object. The error that caused it is already appended to the batch's error list before this is raised."""


def _missing(data: dict, required: tuple[str, ...]) -> list[str]:
    return [key for key in required if key not in data]


def _field(index: int, kind: str, guid: str | None, errors: list[ObjectError], field: str, parser, *args):
    """Run one wire parser; on `WireParseError` record an `ObjectError` naming `field` (never the value) and abort
    the rest of this object via `_Bad`. A wrong-typed value (e.g. a wire `int` where every parser expects `str`)
    is caught before the parser ever sees it -- `str.strip()` etc. would otherwise raise `AttributeError`, an
    uncaught exception that becomes a retried 500 instead of a quarantinable per-object error (review Important
    #3; controller ruling: `invalid_field_type`, deterministic, quarantinable -- same class as `invalid_counter`,
    `invalid_captured_at` below and `unexpected_parse_error` in `parse_objects`)."""
    for value in args:
        if value is not None and not isinstance(value, str):
            errors.append(ObjectError(index, kind, guid, "invalid_field_type", field))
            raise _Bad
    try:
        return parser(*args)
    except WireParseError as exc:
        errors.append(ObjectError(index, kind, guid, exc.code, field))
        raise _Bad from None


def _parse_captured_at(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise WireParseError("invalid_captured_at", text) from None  # ruled deterministic/quarantinable, S1 task 8a review


def _parse_extra_fields(index: int, kind: str, guid: str | None, data: dict, errors: list[ObjectError],
                         core: frozenset[str]) -> dict:
    quantity_fields = _STOCK_ITEM_QUANTITY_FIELDS if kind == "stock_item" else frozenset()
    fields: dict = {}
    for key, text in data.items():
        if key in core:
            continue
        if key in quantity_fields:
            fields[key], _ = _field(index, kind, guid, errors, key, quantity, text)
        elif key in _AMOUNT_FIELDS:
            fields[key] = _field(index, kind, guid, errors, key, amount, text)
        elif key in _LOGICAL_FIELDS:
            fields[key] = _field(index, kind, guid, errors, key, logical, text)
        else:
            fields[key] = text                          # raw text: no parser defined for it in spec §5.3
    return fields


def _parse_master(index: int, kind: str, guid: str | None, data: dict, errors: list[ObjectError]) -> PMaster | None:
    missing = _missing(data, MASTER_REQUIRED[kind])
    if missing:
        errors.append(ObjectError(index, kind, guid, "missing_field", ", ".join(sorted(missing))))
        return None
    try:
        master_name = _field(index, kind, guid, errors, "name", parse_name, data["name"])
        alter_id = _field(index, kind, guid, errors, "alterid", counter, data["alterid"])
        parent = _field(index, kind, guid, errors, "parent", parse_name, data["parent"]) if "parent" in data \
            else None
        fields = _parse_extra_fields(index, kind, guid, data, errors, _MASTER_CORE)
    except _Bad:
        return None
    return PMaster(index=index, kind=kind, guid=data["guid"], alter_id=alter_id, name=master_name, parent=parent,
                   fields=fields, raw=data)


def _parse_balance(index: int, kind: str, guid: str | None, data: dict, errors: list[ObjectError]) -> PBalance | \
        None:
    required = BALANCE_REQUIRED[kind]
    missing = _missing(data, required)
    if missing:
        errors.append(ObjectError(index, kind, guid, "missing_field", ", ".join(sorted(missing))))
        return None
    closing_key = "closingbalance" if kind == "ledger_balance" else "closingvalue"
    try:
        balance_name = _field(index, kind, guid, errors, "name", parse_name, data["name"])
        closing = _field(index, kind, guid, errors, closing_key, amount, data[closing_key])
        opening = None
        if data.get("openingbalance"):
            opening = _field(index, kind, guid, errors, "openingbalance", amount, data["openingbalance"])
        qty, qty_text = (None, "")
        if kind == "stock_balance":                      # spec §5.3: stock_balance's optional "closingbalance" is
            qty, qty_text = quantity(data.get("closingbalance"))   # the quantity, not an amount (unlike ledger_balance)
        captured_at = _field(index, kind, guid, errors, "captured_at", _parse_captured_at, data["captured_at"])
    except _Bad:
        return None
    return PBalance(index=index, kind=kind, guid=data["guid"], name=balance_name, captured_at=captured_at,
                     closing=closing, opening=opening, qty=qty, qty_text=qty_text, raw=data)


def _parse_bills(index: int, guid: str | None, allocations: list[dict], errors: list[ObjectError]) -> list[PBill]:
    bills: list[PBill] = []
    for b in allocations:
        bill_name = parse_name(b.get("name", ""))
        bill_type = b.get("billtype") or None
        bill_amount = None
        if b.get("amount"):
            bill_amount = _field(index, "voucher", guid, errors, "bill_allocations.amount", amount, b["amount"])
            if bill_amount is not None and not bill_amount.stated:      # D3: forex without a base is rejected on
                errors.append(ObjectError(index, "voucher", guid, "forex_base_missing",   # a bill, same as a line
                                           "bill_allocations.amount"))
                raise _Bad
        credit_days, credit_text = credit_period(b.get("billcreditperiod"))
        bill_date = None
        if b.get("billdate"):
            bill_date = _field(index, "voucher", guid, errors, "bill_allocations.billdate", tally_date,
                                b["billdate"])
        bills.append(PBill(name=bill_name, bill_type=bill_type, amount=bill_amount, credit_days=credit_days,
                            credit_text=credit_text, bill_date=bill_date))
    return bills


def _parse_lines(index: int, guid: str | None, entries: list[dict], errors: list[ObjectError],
                  warnings: list[ObjectWarning]) -> tuple[list[PLine], bool]:
    lines: list[PLine] = []
    for line_no, entry in enumerate(entries):
        missing = _missing(entry, LINE_REQUIRED)
        if missing:
            errors.append(ObjectError(index, "voucher", guid, "missing_field", ", ".join(sorted(missing))))
            raise _Bad
        ledger_name = _field(index, "voucher", guid, errors, "ledger_entries.ledgername", parse_name,
                              entry["ledgername"])
        line_amount = _field(index, "voucher", guid, errors, "ledger_entries.amount", amount, entry["amount"])
        if line_amount is None:                          # a required amount can't legitimately be blank
            errors.append(ObjectError(index, "voucher", guid, "unparseable_amount", "ledger_entries.amount"))
            raise _Bad
        if not line_amount.stated:                        # forex without a stated base (D3): reject on a line
            errors.append(ObjectError(index, "voucher", guid, "forex_base_missing", "ledger_entries.amount"))
            raise _Bad
        is_deemed_positive = _field(index, "voucher", guid, errors, "ledger_entries.isdeemedpositive", logical,
                                     entry["isdeemedpositive"])
        if (is_deemed_positive and line_amount.inr > 0) or (not is_deemed_positive and line_amount.inr < 0):
            warnings.append(ObjectWarning(index, "sign_vs_deemed_positive"))                          # D23
        bills = _parse_bills(index, guid, entry.get("bill_allocations") or [], errors)
        lines.append(PLine(line_no=line_no, ledger_name=ledger_name, amount=line_amount,
                            is_deemed_positive=is_deemed_positive, ledger_guid_hint=entry.get("ledgerguid"),
                            bills=bills))
    has_forex = any(line.amount.fx_amount is not None for line in lines)
    return lines, has_forex


def _parse_inventory(index: int, guid: str | None, entries: list[dict], errors: list[ObjectError]) -> list[PInv]:
    inventory: list[PInv] = []
    for line_no, entry in enumerate(entries):
        missing = _missing(entry, INVENTORY_REQUIRED)
        if missing:
            errors.append(ObjectError(index, "voucher", guid, "missing_field", ", ".join(sorted(missing))))
            raise _Bad
        stock_item_name = _field(index, "voucher", guid, errors, "inventory_entries.stockitemname", parse_name,
                                  entry["stockitemname"])
        inv_amount = _field(index, "voucher", guid, errors, "inventory_entries.amount", amount, entry["amount"])
        if inv_amount is None:
            errors.append(ObjectError(index, "voucher", guid, "unparseable_amount", "inventory_entries.amount"))
            raise _Bad
        if not inv_amount.stated:
            errors.append(ObjectError(index, "voucher", guid, "forex_base_missing", "inventory_entries.amount"))
            raise _Bad
        actual_qty, _ = quantity(entry.get("actualqty"))
        billed_qty, billed_text = quantity(entry.get("billedqty"))
        qty_text = billed_text or (entry.get("actualqty") or "")
        rate_value, _, rate_text = parse_rate(entry.get("rate"))
        is_deemed_positive = None
        if entry.get("isdeemedpositive"):
            is_deemed_positive = _field(index, "voucher", guid, errors, "inventory_entries.isdeemedpositive",
                                         logical, entry["isdeemedpositive"])
        inventory.append(PInv(line_no=line_no, stock_item_name=stock_item_name, amount=inv_amount,
                               actual_qty=actual_qty, billed_qty=billed_qty, qty_text=qty_text, rate=rate_value,
                               rate_text=rate_text, is_deemed_positive=is_deemed_positive))
    return inventory


def _parse_voucher(index: int, guid: str | None, data: dict, errors: list[ObjectError],
                    warnings: list[ObjectWarning]) -> PVoucher | None:
    missing = _missing(data, VOUCHER_REQUIRED)
    if missing:
        errors.append(ObjectError(index, "voucher", guid, "missing_field", ", ".join(sorted(missing))))
        return None
    if "ledgerentries_list" in data:                     # LESSONS rule 18 / transcode: this key is never sanctioned
        errors.append(ObjectError(index, "voucher", guid, "duplicate_posting_list", "ledgerentries_list"))
        return None
    try:
        alter_id = _field(index, "voucher", guid, errors, "alterid", counter, data["alterid"])
        voucher_date = _field(index, "voucher", guid, errors, "date", tally_date, data["date"])
        effective_date = None
        if data.get("effectivedate"):
            effective_date = _field(index, "voucher", guid, errors, "effectivedate", tally_date,
                                     data["effectivedate"])
        voucher_type_name = _field(index, "voucher", guid, errors, "vouchertypename", parse_name,
                                    data["vouchertypename"])
        is_cancelled = _field(index, "voucher", guid, errors, "iscancelled", logical, data["iscancelled"])
        is_optional = _field(index, "voucher", guid, errors, "isoptional", logical, data["isoptional"])
        is_post_dated = _field(index, "voucher", guid, errors, "ispostdated", logical, data["ispostdated"])
        is_invoice = None
        if data.get("isinvoice"):
            is_invoice = _field(index, "voucher", guid, errors, "isinvoice", logical, data["isinvoice"])
        party_ledger_name = parse_name(data.get("partyledgername", ""))
        lines, has_forex = _parse_lines(index, guid, data.get("ledger_entries") or [], errors, warnings)
        inventory = _parse_inventory(index, guid, data.get("inventory_entries") or [], errors)
    except _Bad:
        return None

    voucher = PVoucher(index=index, guid=data["guid"], master_id=data["masterid"], alter_id=alter_id,
                        date=voucher_date, effective_date=effective_date, voucher_type_name=voucher_type_name,
                        voucher_number=data.get("vouchernumber", ""), reference=data.get("reference", ""),
                        party_ledger_name=party_ledger_name, narration=data.get("narration", ""),
                        is_cancelled=is_cancelled, is_optional=is_optional, is_post_dated=is_post_dated,
                        is_invoice=is_invoice, lines=lines, inventory=inventory, has_forex=has_forex, raw=data)
    if not voucher_balances(voucher):                    # rung 0, §10.3 / §12 step 7
        errors.append(ObjectError(index, "voucher", guid, "unbalanced_voucher", "ledger_entries"))
        return None
    return voucher


def _parse_one(index: int, obj, errors: list[ObjectError], warnings: list[ObjectWarning]):
    """Dispatch one wire object by `kind`. Guards the two shapes a batch can legitimately hand us that aren't a
    `WireParseError` at all: `obj` itself not a mapping, and `obj["data"]` not a mapping (`WireObject.data` is
    typed `dict[str, Any]`, so pydantic lets a list or a scalar through) -- review Important #3, controller
    ruling: `invalid_field_type`, deterministic/quarantinable, detail names the field ("object" / "data")."""
    if not isinstance(obj, dict):
        errors.append(ObjectError(index, "", None, "invalid_field_type", "object"))
        return None
    kind = obj.get("kind", "")
    kind = kind if isinstance(kind, str) else ""
    data = obj.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        errors.append(ObjectError(index, kind, None, "invalid_field_type", "data"))
        return None
    guid = data.get("guid")
    guid = guid if guid is None or isinstance(guid, str) else None
    if kind == "voucher":
        return _parse_voucher(index, guid, data, errors, warnings)
    if kind in MASTER_REQUIRED:
        return _parse_master(index, kind, guid, data, errors)
    if kind in BALANCE_REQUIRED:
        return _parse_balance(index, kind, guid, data, errors)
    errors.append(ObjectError(index, kind, guid, "unknown_kind", "kind"))
    return None


def parse_objects(objects: list[dict]) -> tuple[list[PMaster | PBalance | PVoucher], list[ObjectError],
                                                 list[ObjectWarning]]:
    """S1 spec §12 step 5: parse and validate every wire object. Collects every error across the batch -- a bad
    object never stops the rest from being checked -- and returns only the objects that parsed cleanly."""
    parsed: list[PMaster | PBalance | PVoucher] = []
    errors: list[ObjectError] = []
    warnings: list[ObjectWarning] = []
    for index, obj in enumerate(objects):
        try:
            result = _parse_one(index, obj, errors, warnings)
        except Exception as exc:
            # Safety net (review Important #3): every *named* bad shape is already handled above with its own
            # code/field (`invalid_field_type`). This only ever fires for something genuinely unanticipated -- a
            # real programming bug, not a deliberate rejection -- so it gets its own code (round-2 ruling:
            # `unexpected_parse_error`, deterministic/quarantinable like `invalid_field_type`, `invalid_counter`
            # and `invalid_captured_at`) and is logged, so it's never silently indistinguishable from a typed-field
            # guard. Even so the batch must not raise (a raise here becomes a 500 that §11 retries forever and
            # D12 can never quarantine) -- one object's bug can't block the rest of the batch or the company's
            # sync. The log carries only the exception's class name plus this object's kind and index -- never a
            # field value, name, or any other business data (decision 14).
            kind = obj.get("kind", "") if isinstance(obj, dict) else ""
            kind = kind if isinstance(kind, str) else ""
            logger.warning("v2.ingest.unexpected_parse_error", extra={"exception_class": type(exc).__name__,
                                                                        "kind": kind, "index": index})
            errors.append(ObjectError(index, kind, None, "unexpected_parse_error", "object"))
            result = None
        if result is not None:
            parsed.append(result)
    return parsed, errors, warnings
