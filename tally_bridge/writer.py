"""Tally write operations — validates and submits import XML.

Wraps import_builder (XML generation) and response_parser (response parsing)
with validation and error handling.

Pre-flight validation runs locally before any Tally call:
- Date present
- Narration present
- At least 2 ledger entries
- Entries balance to zero
- All referenced ledgers exist (when known_ledgers provided)
"""
from __future__ import annotations

import logging

from tally_bridge.client import TallyClient
from tally_bridge.import_builder import (
    build_cancel_voucher,
    build_create_credit_note,
    build_create_debit_note,
    build_create_group,
    build_create_gst_ledger,
    build_create_journal_voucher,
    build_create_ledger,
    build_create_payment_voucher,
    build_create_purchase_voucher,
    build_create_purchase_voucher_ledger,
    build_create_receipt_voucher,
    build_create_sales_voucher,
    build_create_sales_voucher_ledger,
    build_create_stock_group,
    build_create_stock_item,
    build_create_unit,
    build_delete_group,
    build_delete_ledger,
    build_delete_voucher,
)
from tally_bridge.queries.masters import list_ledgers
from tally_bridge.response_parser import parse_import_response

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Raised when voucher data fails dry-run validation."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"Validation failed: {'; '.join(errors)}")


class TallyWriteError(Exception):
    """Raised when Tally accepts the request but doesn't actually persist
    the entity (silent drop) — distinct from connection/transport failures."""


def _assert_created(parsed: dict, op: str) -> dict:
    """Raise TallyWriteError if Tally returned CREATED=0 or success=False.

    Tally accepts malformed envelopes with EXCEPTIONS=1, ERRORS=0, CREATED=0
    — i.e. the request was syntactically OK but no entity got persisted.
    Without this guard, callers can't distinguish 'wrote 1 entity' from
    'silently dropped'.
    """
    if not parsed.get("success", False):
        msg = parsed.get("error_message") or "Tally returned EXCEPTIONS=1 with no error message"
        raise TallyWriteError(f"{op} silently failed: {msg} (parsed={parsed})")
    if parsed.get("created", 0) < 1:
        raise TallyWriteError(
            f"{op} did not create an entity (CREATED=0). parsed={parsed}"
        )
    return parsed


def _assert_master_persisted(parsed: dict, op: str) -> dict:
    """Like ``_assert_created`` but for idempotent master pre-flight creates.

    A master CREATE for an entity that ALREADY EXISTS (e.g. a stock group seeded
    earlier) makes Tally answer CREATED=0, ALTERED=1, ERRORS=0, EXCEPTIONS=0 —
    benign ("already there"). For these idempotent masters that is success, not a
    silent drop. So accept CREATED>=1 OR ALTERED>=1 (with no errors/exceptions).
    A genuine drop (CREATED=0 AND ALTERED=0) or any ERRORS/EXCEPTIONS still raises.

    Voucher creates keep using the strict ``_assert_created`` (CREATED>=1).
    """
    if not parsed.get("success", False):
        msg = parsed.get("error_message") or "Tally returned EXCEPTIONS=1 with no error message"
        raise TallyWriteError(f"{op} silently failed: {msg} (parsed={parsed})")
    if parsed.get("created", 0) < 1 and parsed.get("altered", 0) < 1:
        raise TallyWriteError(
            f"{op} did not persist an entity (CREATED=0, ALTERED=0). parsed={parsed}"
        )
    return parsed


class TallyWriter:
    """Validates and writes vouchers/masters to Tally."""

    def __init__(self, client: TallyClient, company: str):
        self.client = client
        self.company = company

    def validate_voucher(
        self, voucher: dict, known_ledgers: list[str] | None = None,
    ) -> list[str]:
        """Dry-run validation. Returns list of error strings (empty = valid).

        Never raises — always returns a list. Caller decides whether to abort.
        """
        errors = []

        if not voucher.get("date"):
            errors.append("Date is required")

        if not voucher.get("narration", "").strip():
            errors.append("Narration is required")

        entries = voucher.get("ledger_entries", [])
        if len(entries) < 2:
            errors.append("At least two ledger entries required")

        # Per-entry validation: check for required keys
        valid_entries = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"Entry {i} is not a dict")
                continue
            if "amount" not in entry or entry["amount"] is None:
                errors.append(f"Entry {i} is missing 'amount'")
                continue
            ledger_name = entry.get("ledger")
            if not ledger_name or not str(ledger_name).strip():
                errors.append(f"Entry {i} is missing 'ledger'")
                continue
            valid_entries.append(entry)

        # Balance check (only on valid entries)
        if valid_entries:
            try:
                total = sum(float(e["amount"]) for e in valid_entries)
                if abs(total) > 0.01:
                    errors.append(f"Ledger entries do not balance (sum={total:.2f})")
                # Magnitude invariant: an all-zero voucher balances (0 == 0) but is
                # never valid — e.g. a foreign entry with no resolvable rate yields
                # amount 0. Reject it (defense in depth for the FX no-rate guard).
                gross = sum(abs(float(e["amount"])) for e in valid_entries)
                if gross <= 0.01:
                    errors.append("Voucher total must be greater than zero")
            except (TypeError, ValueError) as e:
                errors.append(f"Invalid amount in ledger entries: {e}")

        # Ledger existence check (only on valid entries)
        if known_ledgers is not None and valid_entries:
            known_set = {name.lower() for name in known_ledgers}
            for entry in valid_entries:
                if entry["ledger"].lower() not in known_set:
                    errors.append(f"Ledger \"{entry['ledger']}\" not found in Tally")

        return errors

    async def create_payment_voucher(
        self,
        date: str,
        debit_ledger: str,
        credit_ledger: str,
        amount: float,
        narration: str,
        gst_entries: list[dict] | None = None,
        known_ledgers: list[str] | None = None,
        bill_allocations: list[dict] | None = None,
        reference: str | None = None,
        reference_date: str | None = None,
    ) -> dict:
        """Create a Payment voucher in Tally with validation."""
        # Build voucher dict for validation
        gst_total = sum(e["amount"] for e in (gst_entries or []))
        base_amount = amount - gst_total
        entries = [
            {"ledger": debit_ledger, "amount": -base_amount, "is_debit": True},
        ]
        for gst in gst_entries or []:
            entries.append({"ledger": gst["ledger"], "amount": -gst["amount"], "is_debit": True})
        entries.append({"ledger": credit_ledger, "amount": amount, "is_debit": False})

        voucher = {
            "voucher_type": "Payment",
            "date": date,
            "narration": narration,
            "ledger_entries": entries,
        }
        errors = self.validate_voucher(voucher, known_ledgers)
        if errors:
            raise ValidationError(errors)

        xml = build_create_payment_voucher(
            date=date,
            debit_ledger=debit_ledger,
            credit_ledger=credit_ledger,
            amount=amount,
            narration=narration,
            company=self.company,
            gst_entries=gst_entries,
            bill_allocations=bill_allocations,
            reference=reference,
            reference_date=reference_date,
        )
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_payment_voucher")

    async def create_ledger(
        self, name: str, parent: str, gstin: str | None = None,
        state: str | None = None, gst_reg_type: str | None = None,
        opening_balance: float | None = None, is_billwise: bool = False,
    ) -> dict:
        """Create a ledger master in Tally.

        Existence-safe (defense in depth): Tally treats a create-import for a
        name that already exists as an ALTER, which silently RE-PARENTS the
        existing ledger — live data corruption. So we check existence first
        (case-insensitive, scoped to the same company we write to) and:
          - skip the post (benign already_exists) when a same-name ledger
            exists under the SAME parent — safe idempotent no-op;
          - FAIL when a same-name ledger exists under a DIFFERENT parent —
            skipping would reuse the wrong-group ledger and a CREATE would
            re-parent/corrupt it;
          - otherwise post + assert.
        A read-side error must never block a valid write: on read failure we
        log a warning and fall through to the post path (the post +
        _assert_master_persisted already guard persistence).
        """
        name_lower = (name or "").strip().lower()
        parent_lower = (parent or "").strip().lower()
        try:
            existing = await list_ledgers(self.client, company=self.company)
        except Exception as exc:  # read-side failure must not block the write
            logger.warning(
                "create_ledger existence pre-check failed for %r (proceeding to post): %s",
                name, exc,
            )
        else:
            for l in existing:
                if (l.name or "").strip().lower() != name_lower:
                    continue
                existing_parent = (l.parent_group or "").strip()
                if existing_parent.lower() == parent_lower:
                    return {
                        "success": True, "created": 0, "altered": 0,
                        "already_exists": True, "errors": 0, "exceptions": 0,
                        "deleted": 0, "last_vch_id": None, "error_message": None,
                    }
                return {
                    "success": False, "created": 0, "altered": 0,
                    "already_exists": False, "errors": 1, "exceptions": 0,
                    "deleted": 0, "last_vch_id": None,
                    "error_message": (
                        f"Ledger '{name}' already exists under "
                        f"'{existing_parent}', cannot create under '{parent}'."
                    ),
                }
        xml = build_create_ledger(
            name, parent, self.company, gstin=gstin, state=state,
            gst_reg_type=gst_reg_type, opening_balance=opening_balance,
            is_billwise=is_billwise,
        )
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_ledger")

    async def create_unit(self, name: str, formal_name: str) -> dict:
        """Create a unit of measure in Tally."""
        xml = build_create_unit(name, formal_name, self.company)
        response_xml = await self.client.post_xml(xml)
        return _assert_master_persisted(parse_import_response(response_xml), "create_unit")

    async def create_stock_group(self, name: str, parent: str = "") -> dict:
        """Create a stock group in Tally."""
        xml = build_create_stock_group(name, parent, self.company)
        response_xml = await self.client.post_xml(xml)
        return _assert_master_persisted(parse_import_response(response_xml), "create_stock_group")

    async def create_stock_item(
        self, name: str, group: str, uom: str, opening_qty: float,
        opening_rate: float, hsn_code: str, gst_rate: int,
    ) -> dict:
        """Create a stock item in Tally."""
        xml = build_create_stock_item(
            name, group, uom, opening_qty, opening_rate, hsn_code, gst_rate, self.company,
        )
        response_xml = await self.client.post_xml(xml)
        return _assert_master_persisted(parse_import_response(response_xml), "create_stock_item")

    async def create_gst_ledger(self, name: str, duty_head: str) -> dict:
        """Create a GST duty ledger in Tally."""
        xml = build_create_gst_ledger(name, duty_head, self.company)
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_gst_ledger")

    async def create_sales_voucher(
        self, date: str, voucher_number: str, party: str, items: list[tuple],
        narration: str, gst_mode: str = "intra",
        bill_allocations: list[dict] | None = None,
        reference: str | None = None,
        reference_date: str | None = None,
    ) -> dict:
        """Create a Sales voucher in Tally."""
        xml = build_create_sales_voucher(
            date, voucher_number, party, items, narration, gst_mode, self.company,
            bill_allocations=bill_allocations,
            reference=reference, reference_date=reference_date,
        )
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_sales_voucher")

    async def create_purchase_voucher(
        self, date: str, voucher_number: str, party: str, items: list[tuple],
        narration: str, gst_mode: str = "intra",
        bill_allocations: list[dict] | None = None,
        reference: str | None = None,
        reference_date: str | None = None,
    ) -> dict:
        """Create a Purchase voucher in Tally."""
        xml = build_create_purchase_voucher(
            date, voucher_number, party, items, narration, gst_mode, self.company,
            bill_allocations=bill_allocations,
            reference=reference, reference_date=reference_date,
        )
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_purchase_voucher")

    async def create_receipt_voucher(
        self, date: str, voucher_number: str, party: str, bank_ledger: str,
        amount: float, narration: str,
        bill_allocations: list[dict] | None = None,
    ) -> dict:
        """Create a Receipt voucher in Tally."""
        xml = build_create_receipt_voucher(
            date, voucher_number, party, bank_ledger, amount, narration, self.company,
            bill_allocations=bill_allocations,
        )
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_receipt_voucher")

    async def create_journal_voucher(
        self, date: str, voucher_number: str, debit_ledger: str, credit_ledger: str,
        amount: float, narration: str,
    ) -> dict:
        """Create a Journal voucher in Tally."""
        xml = build_create_journal_voucher(
            date, voucher_number, debit_ledger, credit_ledger, amount, narration, self.company,
        )
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_journal_voucher")

    async def create_debit_note(
        self, date: str, party_ledger: str, purchase_ledger: str,
        amount: float, narration: str,
        gst_entries: list[dict] | None = None,
        bill_ref: str | None = None,
        known_ledgers: list[str] | None = None,
        reference: str | None = None,
        reference_date: str | None = None,
    ) -> dict:
        """Create a Debit Note in Tally with validation (mirrors Purchase polarity).

        Validation legs: purchase ledger debit (-base), GST input debit (-),
        party credit (+amount). These balance to zero.
        """
        gst_total = sum(e["amount"] for e in (gst_entries or []))
        base_amount = amount - gst_total
        entries = [
            {"ledger": purchase_ledger, "amount": -base_amount, "is_debit": True},
        ]
        for gst in gst_entries or []:
            entries.append({"ledger": gst["ledger"], "amount": -gst["amount"], "is_debit": True})
        entries.append({"ledger": party_ledger, "amount": amount, "is_debit": False})

        errors = self.validate_voucher(
            {"voucher_type": "Debit Note", "date": date, "narration": narration,
             "ledger_entries": entries},
            known_ledgers,
        )
        if errors:
            raise ValidationError(errors)

        xml = build_create_debit_note(
            date=date, party_ledger=party_ledger, purchase_ledger=purchase_ledger,
            amount=amount, narration=narration, company=self.company,
            gst_entries=gst_entries, bill_ref=bill_ref,
            reference=reference, reference_date=reference_date,
        )
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_debit_note")

    async def create_credit_note(
        self, date: str, party_ledger: str, sales_ledger: str,
        amount: float, narration: str,
        gst_entries: list[dict] | None = None,
        bill_ref: str | None = None,
        known_ledgers: list[str] | None = None,
        reference: str | None = None,
        reference_date: str | None = None,
    ) -> dict:
        """Create a Credit Note in Tally with validation (mirrors Sales polarity).

        Validation legs: party debit (-amount), GST output credit (+),
        sales ledger credit (+base). These balance to zero.
        """
        gst_total = sum(e["amount"] for e in (gst_entries or []))
        base_amount = amount - gst_total
        entries = [
            {"ledger": party_ledger, "amount": -amount, "is_debit": True},
        ]
        for gst in gst_entries or []:
            entries.append({"ledger": gst["ledger"], "amount": gst["amount"], "is_debit": False})
        entries.append({"ledger": sales_ledger, "amount": base_amount, "is_debit": False})

        errors = self.validate_voucher(
            {"voucher_type": "Credit Note", "date": date, "narration": narration,
             "ledger_entries": entries},
            known_ledgers,
        )
        if errors:
            raise ValidationError(errors)

        xml = build_create_credit_note(
            date=date, party_ledger=party_ledger, sales_ledger=sales_ledger,
            amount=amount, narration=narration, company=self.company,
            gst_entries=gst_entries, bill_ref=bill_ref,
            reference=reference, reference_date=reference_date,
        )
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_credit_note")

    async def create_purchase_voucher_ledger(
        self, date: str, party_ledger: str, purchase_ledger: str,
        amount: float, narration: str,
        gst_entries: list[dict] | None = None,
        bill_ref: str | None = None,
        known_ledgers: list[str] | None = None,
        reference: str | None = None,
        reference_date: str | None = None,
    ) -> dict:
        """Create a ledger-only Purchase voucher (Group B document path).

        Mirrors Purchase polarity: purchase ledger debit (-base), GST input
        debit (-), party (Sundry Creditors) credit (+amount). These balance to
        zero. Distinct from the stock-based ``create_purchase_voucher`` (seeder).
        """
        gst_total = sum(e["amount"] for e in (gst_entries or []))
        base_amount = amount - gst_total
        entries = [
            {"ledger": purchase_ledger, "amount": -base_amount, "is_debit": True},
        ]
        for gst in gst_entries or []:
            entries.append({"ledger": gst["ledger"], "amount": -gst["amount"], "is_debit": True})
        entries.append({"ledger": party_ledger, "amount": amount, "is_debit": False})

        errors = self.validate_voucher(
            {"voucher_type": "Purchase", "date": date, "narration": narration,
             "ledger_entries": entries},
            known_ledgers,
        )
        if errors:
            raise ValidationError(errors)

        xml = build_create_purchase_voucher_ledger(
            date=date, party_ledger=party_ledger, purchase_ledger=purchase_ledger,
            amount=amount, narration=narration, company=self.company,
            gst_entries=gst_entries, bill_ref=bill_ref,
            reference=reference, reference_date=reference_date,
        )
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_purchase_voucher_ledger")

    async def create_sales_voucher_ledger(
        self, date: str, party_ledger: str, sales_ledger: str,
        amount: float, narration: str,
        gst_entries: list[dict] | None = None,
        bill_ref: str | None = None,
        known_ledgers: list[str] | None = None,
        reference: str | None = None,
        reference_date: str | None = None,
    ) -> dict:
        """Create a ledger-only Sales voucher (Group B document path).

        Mirrors Sales polarity: party (Sundry Debtors) debit (-amount), GST
        output credit (+), sales ledger credit (+base). These balance to zero.
        Distinct from the stock-based ``create_sales_voucher`` (seeder).
        """
        gst_total = sum(e["amount"] for e in (gst_entries or []))
        base_amount = amount - gst_total
        entries = [
            {"ledger": party_ledger, "amount": -amount, "is_debit": True},
        ]
        for gst in gst_entries or []:
            entries.append({"ledger": gst["ledger"], "amount": gst["amount"], "is_debit": False})
        entries.append({"ledger": sales_ledger, "amount": base_amount, "is_debit": False})

        errors = self.validate_voucher(
            {"voucher_type": "Sales", "date": date, "narration": narration,
             "ledger_entries": entries},
            known_ledgers,
        )
        if errors:
            raise ValidationError(errors)

        xml = build_create_sales_voucher_ledger(
            date=date, party_ledger=party_ledger, sales_ledger=sales_ledger,
            amount=amount, narration=narration, company=self.company,
            gst_entries=gst_entries, bill_ref=bill_ref,
            reference=reference, reference_date=reference_date,
        )
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_sales_voucher_ledger")

    async def create_group(self, name: str, parent: str) -> dict:
        """Create an account group in Tally."""
        xml = build_create_group(name, parent, self.company)
        response_xml = await self.client.post_xml(xml)
        return _assert_created(parse_import_response(response_xml), "create_group")

    async def cancel_voucher(
        self, voucher_type: str, master_id: str, date: str, narration: str = "",
    ) -> dict:
        """Cancel a voucher in Tally (preferred for undo — preserves audit trail)."""
        xml = build_cancel_voucher(voucher_type, master_id, date, self.company, narration)
        response_xml = await self.client.post_xml(xml)
        return parse_import_response(response_xml)

    async def delete_voucher(
        self, voucher_type: str, master_id: str, date: str,
    ) -> dict:
        """Delete a voucher from Tally."""
        xml = build_delete_voucher(voucher_type, master_id, date, self.company)
        response_xml = await self.client.post_xml(xml)
        return parse_import_response(response_xml)

    async def delete_ledger(self, name: str) -> dict:
        """Delete a ledger master from Tally."""
        xml = build_delete_ledger(name, self.company)
        response_xml = await self.client.post_xml(xml)
        return parse_import_response(response_xml)

    async def delete_group(self, name: str) -> dict:
        """Delete an account group from Tally."""
        xml = build_delete_group(name, self.company)
        response_xml = await self.client.post_xml(xml)
        return parse_import_response(response_xml)
