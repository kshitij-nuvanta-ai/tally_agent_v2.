"""Tests for Tally writer — validation and write orchestration."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from tally_bridge.writer import TallyWriter, TallyWriteError, ValidationError


class TestDryRunValidation:
    def test_valid_payment_passes(self):
        writer = TallyWriter.__new__(TallyWriter)
        voucher = {
            "voucher_type": "Payment",
            "date": "20260404",
            "narration": "Test expense",
            "ledger_entries": [
                {"ledger": "Travel Expenses", "amount": -500.00, "is_debit": True},
                {"ledger": "Cash", "amount": 500.00, "is_debit": False},
            ],
        }
        errors = writer.validate_voucher(voucher, known_ledgers=["Travel Expenses", "Cash"])
        assert errors == []

    def test_unbalanced_amounts_fails(self):
        writer = TallyWriter.__new__(TallyWriter)
        voucher = {
            "voucher_type": "Payment",
            "date": "20260404",
            "narration": "Bad entry",
            "ledger_entries": [
                {"ledger": "Travel", "amount": -500.00, "is_debit": True},
                {"ledger": "Cash", "amount": 400.00, "is_debit": False},
            ],
        }
        errors = writer.validate_voucher(voucher, known_ledgers=["Travel", "Cash"])
        assert any("balance" in e.lower() for e in errors)

    def test_unknown_ledger_fails(self):
        writer = TallyWriter.__new__(TallyWriter)
        voucher = {
            "voucher_type": "Payment",
            "date": "20260404",
            "narration": "Test",
            "ledger_entries": [
                {"ledger": "Unknown Ledger", "amount": -500.00, "is_debit": True},
                {"ledger": "Cash", "amount": 500.00, "is_debit": False},
            ],
        }
        errors = writer.validate_voucher(voucher, known_ledgers=["Cash"])
        assert any("Unknown Ledger" in e for e in errors)

    def test_missing_narration_fails(self):
        writer = TallyWriter.__new__(TallyWriter)
        voucher = {
            "voucher_type": "Payment",
            "date": "20260404",
            "narration": "",
            "ledger_entries": [
                {"ledger": "Travel", "amount": -500.00, "is_debit": True},
                {"ledger": "Cash", "amount": 500.00, "is_debit": False},
            ],
        }
        errors = writer.validate_voucher(voucher, known_ledgers=["Travel", "Cash"])
        assert any("narration" in e.lower() for e in errors)

    def test_fewer_than_two_entries_fails(self):
        writer = TallyWriter.__new__(TallyWriter)
        voucher = {
            "voucher_type": "Payment",
            "date": "20260404",
            "narration": "Test",
            "ledger_entries": [
                {"ledger": "Travel", "amount": -500.00, "is_debit": True},
            ],
        }
        errors = writer.validate_voucher(voucher, known_ledgers=["Travel"])
        assert any("two" in e.lower() or "entries" in e.lower() for e in errors)

    def test_missing_date_fails(self):
        writer = TallyWriter.__new__(TallyWriter)
        voucher = {
            "voucher_type": "Payment",
            "date": "",
            "narration": "Test",
            "ledger_entries": [
                {"ledger": "Travel", "amount": -500.00, "is_debit": True},
                {"ledger": "Cash", "amount": 500.00, "is_debit": False},
            ],
        }
        errors = writer.validate_voucher(voucher, known_ledgers=["Travel", "Cash"])
        assert any("date" in e.lower() for e in errors)

    def test_missing_amount_key_does_not_crash(self):
        writer = TallyWriter.__new__(TallyWriter)
        voucher = {
            "voucher_type": "Payment",
            "date": "20260404",
            "narration": "Test",
            "ledger_entries": [
                {"ledger": "Travel"},  # Missing amount
                {"ledger": "Cash", "amount": 500.00},
            ],
        }
        # Must not raise — should return errors
        errors = writer.validate_voucher(voucher, known_ledgers=["Travel", "Cash"])
        assert any("amount" in e.lower() for e in errors)

    def test_missing_ledger_key_does_not_crash(self):
        writer = TallyWriter.__new__(TallyWriter)
        voucher = {
            "voucher_type": "Payment",
            "date": "20260404",
            "narration": "Test",
            "ledger_entries": [
                {"amount": -500.00},  # Missing ledger
                {"ledger": "Cash", "amount": 500.00},
            ],
        }
        errors = writer.validate_voucher(voucher, known_ledgers=["Cash"])
        assert any("ledger" in e.lower() for e in errors)

    def test_case_insensitive_ledger_match(self):
        writer = TallyWriter.__new__(TallyWriter)
        voucher = {
            "voucher_type": "Payment",
            "date": "20260404",
            "narration": "Test",
            "ledger_entries": [
                {"ledger": "Travel Expenses", "amount": -500.00},
                {"ledger": "Cash", "amount": 500.00},
            ],
        }
        # Pass ledger names in different case
        errors = writer.validate_voucher(voucher, known_ledgers=["travel expenses", "CASH"])
        assert errors == []

    def test_whitespace_narration_fails(self):
        writer = TallyWriter.__new__(TallyWriter)
        voucher = {
            "voucher_type": "Payment",
            "date": "20260404",
            "narration": "   ",
            "ledger_entries": [
                {"ledger": "Travel", "amount": -500.00},
                {"ledger": "Cash", "amount": 500.00},
            ],
        }
        errors = writer.validate_voucher(voucher, known_ledgers=["Travel", "Cash"])
        assert any("narration" in e.lower() for e in errors)

    def test_all_zero_amount_voucher_fails(self):
        """A balanced-but-all-zero voucher (e.g. no-rate FX → ₹0) must be rejected.

        Finding 1 (defense in depth): a Payment of 0 is never valid. The entries
        balance (0 == 0) so the balance check alone passes; the magnitude
        invariant must catch it.
        """
        writer = TallyWriter.__new__(TallyWriter)
        voucher = {
            "voucher_type": "Payment",
            "date": "20260404",
            "narration": "No-rate USD entry",
            "ledger_entries": [
                {"ledger": "Travel Expenses", "amount": 0.0},
                {"ledger": "Cash", "amount": 0.0},
            ],
        }
        errors = writer.validate_voucher(voucher, known_ledgers=["Travel Expenses", "Cash"])
        assert any("zero" in e.lower() or "greater than" in e.lower() for e in errors)


class TestWriteVoucher:
    @pytest.mark.asyncio
    async def test_successful_write(self):
        mock_client = AsyncMock()
        mock_client.post_xml.return_value = """<RESPONSE>
<CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>
<LASTVCHID>12345</LASTVCHID><LASTMID>0</LASTMID>
<COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS>
</RESPONSE>"""
        writer = TallyWriter(client=mock_client, company="Test Co")
        result = await writer.create_payment_voucher(
            date="20260404",
            debit_ledger="Travel Expenses",
            credit_ledger="Cash",
            amount=500.00,
            narration="Test",
        )
        assert result["success"] is True
        assert result["created"] == 1
        mock_client.post_xml.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_with_validation_error(self):
        mock_client = AsyncMock()
        writer = TallyWriter(client=mock_client, company="Test Co")
        # Unknown ledger should fail validation before calling Tally
        with pytest.raises(ValidationError, match="not found"):
            await writer.create_payment_voucher(
                date="20260404",
                debit_ledger="Nonexistent",
                credit_ledger="Cash",
                amount=500.00,
                narration="Test",
                known_ledgers=["Cash"],
            )
        mock_client.post_xml.assert_not_called()

    @pytest.mark.asyncio
    async def test_exceptions_response_raises_write_error(self):
        """EXCEPTIONS=1 from Tally (silent failure) must now raise TallyWriteError."""
        mock_client = AsyncMock()
        mock_client.post_xml.return_value = """<RESPONSE>
<CREATED>0</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>
<LASTVCHID>0</LASTVCHID><LASTMID>0</LASTMID>
<COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS>
<CANCELLED>0</CANCELLED><EXCEPTIONS>1</EXCEPTIONS>
</RESPONSE>"""
        writer = TallyWriter(client=mock_client, company="Test Co")
        with pytest.raises(TallyWriteError, match="silently failed"):
            await writer.create_payment_voucher(
                date="20260404",
                debit_ledger="Travel Expenses",
                credit_ledger="Cash",
                amount=500.00,
                narration="Test",
            )
        # Tally was still called (validation passed; the failure came from Tally)
        mock_client.post_xml.assert_called_once()

    @pytest.mark.asyncio
    async def test_line_error_response_raises_write_error(self):
        """LINEERROR + EXCEPTIONS=1 from Tally must raise TallyWriteError with the error detail."""
        mock_client = AsyncMock()
        mock_client.post_xml.return_value = """<RESPONSE>
<LINEERROR>Ledger 'Nonexistent' does not exist!</LINEERROR>
<CREATED>0</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>
<LASTVCHID>0</LASTVCHID><LASTMID>0</LASTMID>
<COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS>
<CANCELLED>0</CANCELLED><EXCEPTIONS>1</EXCEPTIONS>
</RESPONSE>"""
        writer = TallyWriter(client=mock_client, company="Test Co")
        with pytest.raises(TallyWriteError, match="silently failed"):
            await writer.create_payment_voucher(
                date="20260404",
                debit_ledger="Travel Expenses",
                credit_ledger="Cash",
                amount=500.00,
                narration="Test",
            )


class TestWriteErrorAssertion:
    """Tests for the _assert_created guard — the fix for silent-drop incidents."""

    @pytest.mark.asyncio
    async def test_create_unit_raises_on_silent_drop(self):
        """Tally returns EXCEPTIONS=1, CREATED=0 → writer must raise TallyWriteError."""
        class FakeClient:
            async def post_xml(self, xml):
                return (
                    '<RESPONSE>'
                    '<LINEERROR>Voucher date is missing</LINEERROR>'
                    '<CREATED>0</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>'
                    '<ERRORS>0</ERRORS><EXCEPTIONS>1</EXCEPTIONS>'
                    '<LASTVCHID>0</LASTVCHID>'
                    '</RESPONSE>'
                )
        writer = TallyWriter(client=FakeClient(), company="X")
        with pytest.raises(TallyWriteError, match="silently failed"):
            await writer.create_unit("Nos", "Numbers")

    @pytest.mark.asyncio
    async def test_create_unit_raises_on_zero_created(self):
        """Tally returns success=False equivalent but CREATED=0 → writer must raise."""
        class FakeClient:
            async def post_xml(self, xml):
                return (
                    '<RESPONSE>'
                    '<CREATED>0</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>'
                    '<ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS>'
                    '<LASTVCHID>0</LASTVCHID>'
                    '</RESPONSE>'
                )
        writer = TallyWriter(client=FakeClient(), company="X")
        with pytest.raises(TallyWriteError):
            await writer.create_unit("Nos", "Numbers")

    @pytest.mark.asyncio
    async def test_create_unit_succeeds_when_created(self):
        """Sanity: CREATED=1 → no exception, returns parsed dict."""
        class FakeClient:
            async def post_xml(self, xml):
                return (
                    '<RESPONSE>'
                    '<CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>'
                    '<ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS>'
                    '<LASTVCHID>0</LASTVCHID>'
                    '</RESPONSE>'
                )
        writer = TallyWriter(client=FakeClient(), company="X")
        result = await writer.create_unit("Nos", "Numbers")
        assert result["created"] == 1

    @pytest.mark.asyncio
    async def test_create_stock_item_accepts_empty_hsn(self):
        """Vision often has no HSN — create_stock_item must not raise on empty hsn."""
        class FakeClient:
            async def post_xml(self, xml):
                return (
                    '<RESPONSE>'
                    '<CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>'
                    '<ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS>'
                    '<LASTVCHID>0</LASTVCHID>'
                    '</RESPONSE>'
                )
        writer = TallyWriter(client=FakeClient(), company="X")
        result = await writer.create_stock_item(
            "Generic Widget", group="Electronics", uom="Nos",
            opening_qty=0, opening_rate=0, hsn_code="", gst_rate=18,
        )
        assert result["created"] == 1

    @pytest.mark.asyncio
    async def test_create_sales_voucher_raises_on_silent_drop(self):
        """Specifically the failure mode hit live: voucher silently dropped."""
        class FakeClient:
            async def post_xml(self, xml):
                return (
                    '<RESPONSE>'
                    "<LINEERROR>Voucher date is missing for: 'Sales' voucher S010</LINEERROR>"
                    '<CREATED>0</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>'
                    '<ERRORS>0</ERRORS><EXCEPTIONS>1</EXCEPTIONS>'
                    '<LASTVCHID>0</LASTVCHID>'
                    '</RESPONSE>'
                )
        writer = TallyWriter(client=FakeClient(), company="X")
        with pytest.raises(TallyWriteError, match="silently failed"):
            await writer.create_sales_voucher(
                date="20251215", voucher_number="S010", party="Sunrise Electronics Mumbai",
                items=[("HP Laptop 15s", 1, 45000, "Sales - Electronics", "Nos", 18)],
                narration="X",
            )


class TestMasterCreateIdempotentAltered:
    """BUG 1: a master pre-flight create against an ALREADY-EXISTING master makes
    Tally answer CREATED=0, ALTERED=1, ERRORS=0, EXCEPTIONS=0. That is benign
    ("already there") and the master create methods must treat it as success —
    not raise TallyWriteError on CREATED<1.
    """

    def _altered_client(self):
        class FakeClient:
            async def post_xml(self, xml):
                return (
                    '<RESPONSE>'
                    '<CREATED>0</CREATED><ALTERED>1</ALTERED><DELETED>0</DELETED>'
                    '<ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS>'
                    '<LASTVCHID>0</LASTVCHID>'
                    '</RESPONSE>'
                )
        return FakeClient()

    @pytest.mark.asyncio
    async def test_create_stock_group_accepts_altered_as_success(self):
        writer = TallyWriter(client=self._altered_client(), company="X")
        result = await writer.create_stock_group("AI Imported Items", "")
        assert result["altered"] == 1
        assert result["created"] == 0

    @pytest.mark.asyncio
    async def test_create_unit_accepts_altered_as_success(self):
        writer = TallyWriter(client=self._altered_client(), company="X")
        result = await writer.create_unit("Nos", "Numbers")
        assert result["altered"] == 1

    @pytest.mark.asyncio
    async def test_create_stock_item_accepts_altered_as_success(self):
        writer = TallyWriter(client=self._altered_client(), company="X")
        result = await writer.create_stock_item(
            "Generic Widget", group="Electronics", uom="Nos",
            opening_qty=0, opening_rate=0, hsn_code="", gst_rate=18,
        )
        assert result["altered"] == 1

    @pytest.mark.asyncio
    async def test_create_stock_group_still_raises_on_nothing_persisted(self):
        """CREATED=0 AND ALTERED=0 (errors=0) is a genuine silent drop — raise."""
        class FakeClient:
            async def post_xml(self, xml):
                return (
                    '<RESPONSE>'
                    '<CREATED>0</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>'
                    '<ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS>'
                    '<LASTVCHID>0</LASTVCHID>'
                    '</RESPONSE>'
                )
        writer = TallyWriter(client=FakeClient(), company="X")
        with pytest.raises(TallyWriteError):
            await writer.create_stock_group("AI Imported Items", "")

    @pytest.mark.asyncio
    async def test_create_stock_group_still_raises_on_exception(self):
        """EXCEPTIONS=1 must always raise, even with ALTERED=1."""
        class FakeClient:
            async def post_xml(self, xml):
                return (
                    '<RESPONSE>'
                    '<LINEERROR>bad group</LINEERROR>'
                    '<CREATED>0</CREATED><ALTERED>1</ALTERED><DELETED>0</DELETED>'
                    '<ERRORS>0</ERRORS><EXCEPTIONS>1</EXCEPTIONS>'
                    '<LASTVCHID>0</LASTVCHID>'
                    '</RESPONSE>'
                )
        writer = TallyWriter(client=FakeClient(), company="X")
        with pytest.raises(TallyWriteError):
            await writer.create_stock_group("AI Imported Items", "")


class TestCreateLedgerExistenceSafe:
    """Part B: create_ledger must never re-parent an existing ledger.

    Tally treats an import for an existing-name ledger as an ALTER, which can
    re-parent live data. create_ledger must check existence first (via
    masters.list_ledgers, case-insensitive) and skip the post when present.
    """

    def _make_ledger(self, name, parent="Sundry Creditors"):
        from tally_bridge.models import Ledger
        return Ledger(name=name, parent_group=parent)

    @pytest.mark.asyncio
    async def test_create_ledger_skips_post_when_already_exists(self, monkeypatch):
        mock_client = AsyncMock()
        writer = TallyWriter(client=mock_client, company="Test Co")
        existing = [self._make_ledger("Purchase - Electronics", "Sundry Creditors")]
        monkeypatch.setattr(
            "tally_bridge.writer.list_ledgers",
            AsyncMock(return_value=existing),
        )
        result = await writer.create_ledger(name="Purchase - Electronics", parent="Sundry Creditors")
        assert result["already_exists"] is True
        assert result["created"] == 0
        assert result["altered"] == 0
        assert result["success"] is True
        mock_client.post_xml.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_ledger_existence_check_is_case_insensitive(self, monkeypatch):
        """Both name AND parent comparisons are case-insensitive."""
        mock_client = AsyncMock()
        writer = TallyWriter(client=mock_client, company="Test Co")
        existing = [self._make_ledger("Purchase - Electronics", "Sundry Creditors")]
        monkeypatch.setattr(
            "tally_bridge.writer.list_ledgers",
            AsyncMock(return_value=existing),
        )
        result = await writer.create_ledger(name="purchase - electronics", parent="sundry creditors")
        assert result["already_exists"] is True
        mock_client.post_xml.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_ledger_conflicts_when_same_name_different_parent(self, monkeypatch):
        """Same name under a DIFFERENT parent is a real conflict: fail, don't skip, don't post."""
        mock_client = AsyncMock()
        writer = TallyWriter(client=mock_client, company="Test Co")
        existing = [self._make_ledger("Acme Corp", "Sundry Debtors")]
        monkeypatch.setattr(
            "tally_bridge.writer.list_ledgers",
            AsyncMock(return_value=existing),
        )
        result = await writer.create_ledger(name="Acme Corp", parent="Sundry Creditors")
        assert result["success"] is False
        assert result["already_exists"] is False
        assert result["errors"] == 1
        assert "Sundry Debtors" in result["error_message"]
        assert "Sundry Creditors" in result["error_message"]
        mock_client.post_xml.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_ledger_proceeds_when_existence_read_raises(self, monkeypatch):
        """#3 fail-safe: a read-side error must NOT block a valid write."""
        mock_client = AsyncMock()
        mock_client.post_xml.return_value = (
            '<RESPONSE>'
            '<CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>'
            '<ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS>'
            '<LASTVCHID>0</LASTVCHID>'
            '</RESPONSE>'
        )
        writer = TallyWriter(client=mock_client, company="Test Co")
        monkeypatch.setattr(
            "tally_bridge.writer.list_ledgers",
            AsyncMock(side_effect=RuntimeError("tally read blew up")),
        )
        result = await writer.create_ledger(name="Brand New Co", parent="Sundry Creditors")
        assert result["created"] >= 1
        mock_client.post_xml.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_ledger_existence_read_scoped_to_company(self, monkeypatch):
        """#2: existence read must be company-scoped to match the company-scoped write."""
        mock_client = AsyncMock()
        mock_client.post_xml.return_value = (
            '<RESPONSE>'
            '<CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>'
            '<ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS>'
            '<LASTVCHID>0</LASTVCHID>'
            '</RESPONSE>'
        )
        writer = TallyWriter(client=mock_client, company="Bharat Traders")
        mock_list = AsyncMock(return_value=[])
        monkeypatch.setattr("tally_bridge.writer.list_ledgers", mock_list)
        await writer.create_ledger(name="New Co", parent="Sundry Creditors")
        assert mock_list.await_args.kwargs.get("company") == "Bharat Traders"

    @pytest.mark.asyncio
    async def test_create_ledger_posts_when_absent(self, monkeypatch):
        mock_client = AsyncMock()
        mock_client.post_xml.return_value = (
            '<RESPONSE>'
            '<CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>'
            '<ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS>'
            '<LASTVCHID>0</LASTVCHID>'
            '</RESPONSE>'
        )
        writer = TallyWriter(client=mock_client, company="Test Co")
        existing = [self._make_ledger("Some Other Ledger")]
        monkeypatch.setattr(
            "tally_bridge.writer.list_ledgers",
            AsyncMock(return_value=existing),
        )
        result = await writer.create_ledger(name="Brand New Supplier Co", parent="Sundry Creditors")
        assert result["created"] >= 1
        mock_client.post_xml.assert_called_once()


def _silent_drop_client():
    """Returns a fake client that always returns the live silent-drop response."""
    class FakeClient:
        async def post_xml(self, xml):
            return (
                '<RESPONSE>'
                '<LINEERROR>Silent drop</LINEERROR>'
                '<CREATED>0</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>'
                '<ERRORS>0</ERRORS><EXCEPTIONS>1</EXCEPTIONS>'
                '<LASTVCHID>0</LASTVCHID>'
                '</RESPONSE>'
            )
    return FakeClient()


# Each entry: (method_name, args_tuple, kwargs_dict).
# Covers every TallyWriter.create_* method so a regression that drops
# _assert_created from any of them is caught.
ALL_CREATE_METHODS = [
    ("create_unit",         ("Nos", "Numbers"),                         {}),
    ("create_group",        ("Test Group", "Sundry Debtors"),            {}),
    ("create_stock_group",  ("TestSG",), {"parent": ""}),
    ("create_stock_item",   ("Item",),
        {"group": "Electronics", "uom": "Nos", "opening_qty": 1,
         "opening_rate": 100, "hsn_code": "8528", "gst_rate": 18}),
    ("create_gst_ledger",   ("CGST Output",), {"duty_head": "Central Tax"}),
    ("create_ledger",       ("Test Ledger", "Sundry Debtors"),           {}),
    ("create_payment_voucher", (),
        {"date": "20260101", "debit_ledger": "Travel", "credit_ledger": "Cash",
         "amount": 100.0, "narration": "Test"}),
    ("create_sales_voucher", (),
        {"date": "20260101", "voucher_number": "S001", "party": "Apex",
         "items": [("Item", 1, 100, "Sales - Electronics", "Nos", 18)],
         "narration": "T"}),
    ("create_purchase_voucher", (),
        {"date": "20260101", "voucher_number": "P001", "party": "Samsung",
         "items": [("Item", 1, 100, "Purchase - Electronics", "Nos", 18)],
         "narration": "T"}),
    ("create_receipt_voucher", (),
        {"date": "20260101", "voucher_number": "R001", "party": "Apex",
         "bank_ledger": "HDFC", "amount": 100.0, "narration": "T"}),
    ("create_journal_voucher", (),
        {"date": "20260101", "voucher_number": "J001", "debit_ledger": "Rent",
         "credit_ledger": "HDFC", "amount": 100.0, "narration": "T"}),
]


@pytest.mark.parametrize("method,args,kwargs", ALL_CREATE_METHODS,
                         ids=[m[0] for m in ALL_CREATE_METHODS])
@pytest.mark.asyncio
async def test_every_create_method_raises_on_silent_drop(method, args, kwargs, monkeypatch):
    """Regression guard: every TallyWriter.create_* method must raise
    TallyWriteError on EXCEPTIONS=1/CREATED=0.

    The Stage 1 incident (2026-05-05) was caused by writers returning the
    parsed dict instead of asserting CREATED >= 1. This test ensures every
    create_* method now goes through _assert_created.
    """
    # create_ledger now does an existence pre-check via list_ledgers; stub it to
    # "absent" so the silent-drop POST path is exercised (not the existence path).
    monkeypatch.setattr(
        "tally_bridge.writer.list_ledgers", AsyncMock(return_value=[]),
    )
    writer = TallyWriter(client=_silent_drop_client(), company="X")
    fn = getattr(writer, method)
    with pytest.raises(TallyWriteError):
        await fn(*args, **kwargs)
