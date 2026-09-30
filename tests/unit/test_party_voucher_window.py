"""Party-voucher lookup window (write-flow dedup + DN/CN "against which invoice").

``get_party_vouchers`` used a hard-coded 01-04-2025..31-03-2026 default. With
TYPED date vars (C33) Tally honours that window literally, so from 1-Apr-2026
duplicate detection would stop seeing the current year's invoices. The window
is now derived: from the start of the FY *before* the earlier of (document
date, today) to the end of the FY of the later of the two. Both bounds fall on
day 1 / 31 (safe under C43 on Educational Tally too).
"""
from __future__ import annotations

from datetime import date

import pytest

from backend.tally_bridge.client import TallyClient
from backend.tally_bridge.queries import vouchers as vq
from backend.tally_bridge.queries.vouchers import party_voucher_window


class TestPartyVoucherWindow:
    def test_no_anchor_covers_prior_and_current_fy_of_today(self):
        assert party_voucher_window(None, today=date(2026, 9, 24)) == ("01-04-2025", "31-03-2027")

    def test_anchor_in_current_fy(self):
        assert party_voucher_window(date(2026, 5, 2), today=date(2026, 9, 24)) == ("01-04-2025", "31-03-2027")

    def test_old_invoice_uploaded_after_year_end_reaches_its_prior_fy(self):
        # March-2026 invoice (FY 2025-26) uploaded in April 2026: window must
        # cover FY 2024-25 (DN/CN original / cross-year duplicate) .. FY 2026-27.
        assert party_voucher_window(date(2026, 3, 15), today=date(2026, 4, 10)) == ("01-04-2024", "31-03-2027")

    def test_much_older_document(self):
        assert party_voucher_window(date(2023, 6, 15), today=date(2026, 9, 24)) == ("01-04-2022", "31-03-2027")

    def test_future_dated_document_extends_the_end(self):
        assert party_voucher_window(date(2027, 5, 1), today=date(2026, 9, 24)) == ("01-04-2025", "31-03-2028")

    def test_january_today_is_still_in_the_fy_that_started_last_april(self):
        assert party_voucher_window(None, today=date(2027, 1, 15)) == ("01-04-2025", "31-03-2027")

    @pytest.mark.parametrize("anchor", ["2026-03-15", "20260315", "15-03-2026"])
    def test_string_anchor_formats(self, anchor):
        assert party_voucher_window(anchor, today=date(2026, 4, 10)) == ("01-04-2024", "31-03-2027")

    @pytest.mark.parametrize("anchor", ["", "garbage", "2026-13-45"])
    def test_unparseable_anchor_falls_back_to_today(self, anchor):
        assert party_voucher_window(anchor, today=date(2026, 9, 24)) == ("01-04-2025", "31-03-2027")

    def test_bounds_are_c43_safe_days(self):
        frm, to = party_voucher_window(date(2024, 11, 17), today=date(2026, 2, 28))
        assert frm[:2] == "01" and to[:2] == "31"

    def test_default_uses_real_today(self):
        frm, to = party_voucher_window(None)
        today = date.today()
        fy_start_year = today.year if today.month >= 4 else today.year - 1
        assert frm == f"01-04-{fy_start_year - 1}"
        assert to == f"31-03-{fy_start_year + 1}"


class _CapturingClient(TallyClient):
    """Real mock-mode client that also records the XML sent (network edge)."""

    def __init__(self):
        super().__init__()
        self.mock_mode = True
        self.sent: list[str] = []

    async def post_xml(self, xml_payload: str) -> str:
        self.sent.append(xml_payload)
        return await super().post_xml(xml_payload)


class TestGetPartyVouchersWindow:
    async def test_no_hard_coded_fy_2025_26_default(self):
        client = _CapturingClient()
        await vq.get_party_vouchers(client, "Apex Technologies Pvt Ltd", ["Sales"])
        frm, to = party_voucher_window(None)
        assert f">{frm}</SVFROMDATE>" in client.sent[0]
        assert f">{to}</SVTODATE>" in client.sent[0]
        await client.close()

    async def test_anchor_date_drives_window(self):
        client = _CapturingClient()
        await vq.get_party_vouchers(client, "Apex Technologies Pvt Ltd", ["Sales"], anchor_date="2025-04-05")
        assert "01-04-2024</SVFROMDATE>" in client.sent[0]
        await client.close()

    async def test_explicit_window_still_honoured(self):
        client = _CapturingClient()
        await vq.get_party_vouchers(
            client, "Apex Technologies Pvt Ltd", ["Sales"],
            from_date="01-04-2022", to_date="31-03-2023", anchor_date="2025-04-05",
        )
        assert "01-04-2022</SVFROMDATE>" in client.sent[0]
        assert "31-03-2023</SVTODATE>" in client.sent[0]
        await client.close()

    async def test_prior_fy_invoice_found_for_early_new_year_upload(self):
        # Invoice INV-APX-2425-004 was entered in FY 2024-25 (mock Tally).
        # Uploading a doc dated 05-Apr-2025 must still see it.
        client = _CapturingClient()
        rows = await vq.get_party_vouchers(
            client, "Apex Technologies Pvt Ltd", ["Sales"], anchor_date="2025-04-05",
        )
        assert "INV-APX-2425-004" in {r["reference"] for r in rows}
        assert {"INV-APX-001", "INV-APX-002"} <= {r["reference"] for r in rows}
        await client.close()
