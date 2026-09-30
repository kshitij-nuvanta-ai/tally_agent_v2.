"""Group B WRITE-DIRECTION manual test against a LIVE Tally — proves voucher polarity.

Uses the REAL production write code (``tally_bridge.writer.TallyWriter`` — the
exact methods ``backend/api/chat.py`` voucher_action dispatches to) to land a Purchase,
Debit Note, Sales and Credit Note against the live "Bharat Traders Private Limited"
company, reading bills_payable / bills_receivable back after every write to PROVE the
direction of each voucher. In particular:

  * a Debit Note must REDUCE the supplier's payable, and
  * a Credit Note must REDUCE the customer's receivable.

If either moves the wrong way the script prints a LOUD "DIRECTION WRONG" failure.

Everything it writes is cleaned up in a ``finally`` block (delete-by-Master-ID, mirroring
``scripts/probe_group_b.py``), and bills_payable / bills_receivable are re-read at the end
to assert the books are restored to baseline (±1).

Grounded in:
  - tally_bridge/writer.py            (create_purchase_voucher_ledger / _sales_ / debit_note / credit_note)
  - backend/api/chat.py                       (voucher_action dispatch — exact call shapes mirrored here)
  - tally_bridge/queries/reports.py   (bills_payable / bills_receivable → list[OutstandingBill])
  - tally_bridge/request_builder.py   (build_list_ledgers)
  - tally_bridge/response_parser.py   (parse_ledger_list — parent_group field)
  - scripts/probe_group_b.py                  (delete-by-Master-ID cleanup + arg parsing + post_write timeout)
  - LESSONS.md §15                            (write safety: read-back, DD-MMM-YYYY delete date)

WARNING: this WRITES to a live Tally. It targets "Bharat Traders Private Limited", uses a
"_ManualTestGB" narration prefix and small amounts, and cleans up after itself.

Usage:
    PYTHONPATH=. uv run python scripts/manual_test_group_b_live.py --host localhost --port 9000 \
        2>&1 | tee docs/manual-test-group-b-live.log
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from tally_bridge.client import TallyClient
from tally_bridge.import_builder import _esc, _wrap_import
from tally_bridge.models import OutstandingBill
from tally_bridge.queries.reports import bills_payable, bills_receivable
from tally_bridge.request_builder import build_list_ledgers
from tally_bridge.response_parser import parse_import_response, parse_ledger_list
from tally_bridge.writer import TallyWriter

COMPANY = "Bharat Traders Private Limited"
NPFX = "_ManualTestGB"  # narration prefix so leftovers are mechanically identifiable

# FY-internal dates (seed company FY = Apr 2025 – Mar 2026).
# Tally import dates are YYYYMMDD; delete envelopes want DD-MMM-YYYY (LESSONS §15 / v4).
VCH_DATE = "20250620"
VCH_DATE_DISPLAY = datetime.strptime(VCH_DATE, "%Y%m%d").strftime("%d-%b-%Y")  # 20-Jun-2025
# bills_payable / bills_receivable take a DD-MM-YYYY "as on" date (request_builder format).
AS_ON = "31-03-2026"

# Bill references for the new bills we raise (and that DN/CN settle against).
PUR_BILL_REF = f"{NPFX}-PUR-1"
SAL_BILL_REF = f"{NPFX}-SAL-1"

# Amounts.
PUR_AMOUNT = 5000.0
DN_AMOUNT = 2000.0
SAL_AMOUNT = 8000.0
CN_AMOUNT = 3000.0

TOLERANCE = 1.0  # rupees — accept ±1 rounding noise on read-backs

# Fallback ledger names if dynamic discovery comes up empty.
FALLBACK_SUPPLIER = "Bharat Paper Supplies"
FALLBACK_CUSTOMER = "Apex Technologies Pvt Ltd"
FALLBACK_PURCHASE = "Purchase Accounts"
FALLBACK_SALES = "Sales Accounts"


# ─────────────────────────────────────────────────────────────────────────────
# Result tracking
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class StepResult:
    step: str
    voucher: str
    expected: str
    observed: str
    passed: bool


RESULTS: list[StepResult] = []
MASTER_IDS: list[tuple[str, str]] = []  # (voucher_type, master_id) captured for cleanup


def record(step: str, voucher: str, expected: str, observed: str, passed: bool) -> None:
    RESULTS.append(StepResult(step, voucher, expected, observed, passed))
    print(f"\n  >>> {'PASS' if passed else 'FAIL'}: {step} ({voucher})")
    print(f"      expected: {expected}")
    print(f"      observed: {observed}")


def banner(s: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {s}")
    print("=" * 78)


def _short(text: str, n: int = 800) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + f"... <{len(text) - n} chars truncated>"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper — extend write timeout (mirrors probe_group_b.post_write)
# ─────────────────────────────────────────────────────────────────────────────
async def post_write(client: TallyClient, xml: str) -> str:
    saved = client._client.timeout
    client._client.timeout = httpx.Timeout(90.0, connect=5.0)
    try:
        return await client.post_xml(xml)
    finally:
        client._client.timeout = saved


# ─────────────────────────────────────────────────────────────────────────────
# Read-back helpers — total pending for a single party from bills_payable / _receivable
# ─────────────────────────────────────────────────────────────────────────────
def _party_pending(bills: list[OutstandingBill], party: str) -> float:
    """Sum pending_amount across all bills for `party` (case-insensitive match)."""
    p = party.strip().lower()
    return sum(b.pending_amount for b in bills if b.party_name.strip().lower() == p)


def _bill_present(bills: list[OutstandingBill], party: str, bill_ref: str) -> bool:
    p = party.strip().lower()
    r = bill_ref.strip().lower()
    return any(
        b.party_name.strip().lower() == p and b.bill_number.strip().lower() == r
        for b in bills
    )


async def read_payable(client: TallyClient, supplier: str) -> tuple[float, list[OutstandingBill]]:
    bills = await bills_payable(client, AS_ON, COMPANY)
    pending = _party_pending(bills, supplier)
    print(f"  [read bills_payable as-on {AS_ON}] {supplier!r} pending = {pending:,.2f} "
          f"({len(bills)} total bills in report)")
    return pending, bills


async def read_receivable(client: TallyClient, customer: str) -> tuple[float, list[OutstandingBill]]:
    bills = await bills_receivable(client, AS_ON, COMPANY)
    pending = _party_pending(bills, customer)
    print(f"  [read bills_receivable as-on {AS_ON}] {customer!r} pending = {pending:,.2f} "
          f"({len(bills)} total bills in report)")
    return pending, bills


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup helper — delete a voucher by Master ID (LASTVCHID), DD-MMM-YYYY date.
# (mirrors probe_group_b.cleanup_voucher + import_builder.build_delete_voucher)
# ─────────────────────────────────────────────────────────────────────────────
async def cleanup_voucher(client: TallyClient, voucher_type: str, master_id: str | None,
                          label: str) -> None:
    if not master_id or master_id == "0":
        print(f"  [skip cleanup — no Master ID for {label}]")
        return
    voucher_xml = (
        f'<VOUCHER DATE="{_esc(VCH_DATE_DISPLAY)}" TAGNAME="Master ID" '
        f'TAGVALUE="{_esc(master_id)}" VCHTYPE="{_esc(voucher_type)}" ACTION="Delete">\n'
        f'</VOUCHER>'
    )
    xml = _wrap_import("Vouchers", COMPANY, voucher_xml)
    try:
        raw = await post_write(client, xml)
        parsed = parse_import_response(raw)
        if parsed.get("deleted", 0) >= 1:
            print(f"  [cleanup OK] deleted {label} (mid={master_id}, deleted={parsed['deleted']})")
        else:
            print(f"  [cleanup WARN] {label} (mid={master_id}) NOT deleted: {_short(raw, 300)}")
    except Exception as e:  # noqa: BLE001 — cleanup must never abort the run
        print(f"  [cleanup ERROR] {label}: {type(e).__name__}: {e}")


def _capture_master_id(result: dict, voucher_type: str, label: str) -> str | None:
    """Capture Master ID from a create response (LASTVCHID) for later cleanup.

    Mirrors how probe_group_b captures parsed['last_vch_id'] and chat.py reads
    result['last_vch_id'] as the persisted voucher id.
    """
    mid = result.get("last_vch_id")
    if mid and mid != "0":
        MASTER_IDS.append((voucher_type, mid))
        print(f"  [captured Master ID] {label}: {voucher_type} mid={mid}")
    else:
        print(f"  [WARN] {label}: no Master ID in create response (last_vch_id={mid!r}) "
              f"— cleanup of this voucher will be skipped")
    return mid


# ─────────────────────────────────────────────────────────────────────────────
# Discover real ledgers from the live company.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Ledgers:
    supplier: str   # Sundry Creditors party
    customer: str   # Sundry Debtors party
    purchase: str   # purchase / expense ledger
    sales: str      # sales / income ledger
    all_names: list[str] = field(default_factory=list)


async def discover_ledgers(client: TallyClient) -> Ledgers:
    banner("Discovering ledgers from live company (build_list_ledgers / parse_ledger_list)")
    raw = await client.post_xml(build_list_ledgers())
    ledgers = parse_ledger_list(raw)
    names = [l["name"] for l in ledgers]

    def _by_group(*groups: str) -> str | None:
        wanted = {g.lower() for g in groups}
        return next(
            (l["name"] for l in ledgers if (l.get("parent_group") or "").lower() in wanted),
            None,
        )

    # Supplier MUST be a Sundry Creditor that is maintained BILL-BY-BILL — otherwise the
    # Purchase "New Ref" bill allocation never shows up in bills_payable and the bill-wise
    # read-back asserts payable 0.00 -> 0.00 / bill present=False (e.g. "Acme Computer
    # Distributors Pvt Ltd" carries an on-account balance but is NOT bill-wise). So instead
    # of taking the first sundry creditor, pick one that already appears in bills_payable
    # as-on AS_ON (proves it is bill-wise); fall back to FALLBACK_SUPPLIER ("Bharat Paper
    # Supplies", known bill-wise) if none is found.
    payable_bills = await bills_payable(client, AS_ON, COMPANY)
    bill_wise_parties = {b.party_name for b in payable_bills}
    supplier = next(
        (l["name"] for l in ledgers
         if (l.get("parent_group") or "").lower() == "sundry creditors"
         and l["name"] in bill_wise_parties),
        FALLBACK_SUPPLIER,
    )
    customer = _by_group("sundry debtors") or FALLBACK_CUSTOMER
    purchase = (
        _by_group("purchase accounts", "direct expenses", "indirect expenses")
        or FALLBACK_PURCHASE
    )
    sales = _by_group("sales accounts", "direct incomes", "indirect incomes") or FALLBACK_SALES

    print(f"  Found {len(ledgers)} ledgers")
    print("  Chosen ledgers:")
    print(f"    supplier (Sundry Creditors party) = {supplier!r}")
    print(f"    customer (Sundry Debtors party)   = {customer!r}")
    print(f"    purchase / expense ledger         = {purchase!r}")
    print(f"    sales / income ledger             = {sales!r}")
    return Ledgers(supplier=supplier, customer=customer, purchase=purchase,
                   sales=sales, all_names=names)


# ─────────────────────────────────────────────────────────────────────────────
# Main sequence
# ─────────────────────────────────────────────────────────────────────────────
async def run(host: str, port: int) -> None:
    client = TallyClient(host=host, port=port)
    print(f"Group B Write-DIRECTION manual test — {datetime.now().isoformat()}")
    print(f"Target: {host}:{port} | Company: {COMPANY}")
    print(f"Voucher date: {VCH_DATE} (display {VCH_DATE_DISPLAY}) | bills as-on: {AS_ON}")

    # Connectivity check.
    try:
        await client.post_xml(build_list_ledgers())
    except Exception as e:  # noqa: BLE001
        print(f"ABORT: Tally not responsive: {e}")
        await client.close()
        sys.exit(1)

    # Baselines, captured for the final restore assertion.
    base_payable = 0.0
    base_receivable = 0.0
    # Per-party expected running totals relative to baseline.
    led: Ledgers | None = None

    try:
        led = await discover_ledgers(client)
        writer = TallyWriter(client, COMPANY)
        known = led.all_names  # for validate_voucher ledger-existence check

        # ── Step 1: baseline ──────────────────────────────────────────────
        banner("Step 1 — Baseline pending payable / receivable")
        base_payable, _ = await read_payable(client, led.supplier)
        base_receivable, _ = await read_receivable(client, led.customer)
        record(
            "1-baseline", "(read only)",
            "capture baseline pending for supplier and customer",
            f"supplier payable baseline = {base_payable:,.2f}; "
            f"customer receivable baseline = {base_receivable:,.2f}",
            True,
        )

        # ── Step 2: Purchase (raises a NEW payable bill, increases payable) ─
        try:
            banner(f"Step 2 — Purchase {PUR_AMOUNT:,.0f} (New Ref {PUR_BILL_REF}) "
                   f"→ payable should INCREASE")
            print(f"  payable BEFORE: {base_payable:,.2f}")
            result = await writer.create_purchase_voucher_ledger(
                date=VCH_DATE,
                party_ledger=led.supplier,
                purchase_ledger=led.purchase,
                amount=PUR_AMOUNT,
                narration=f"{NPFX} purchase test",
                bill_ref=PUR_BILL_REF,
                known_ledgers=known,
            )
            print(f"  RAW create response (parsed): {json.dumps(result, indent=2)}")
            _capture_master_id(result, "Purchase", "Step 2 Purchase")
            after_pur, pay_bills = await read_payable(client, led.supplier)
            delta = after_pur - base_payable
            present = _bill_present(pay_bills, led.supplier, PUR_BILL_REF)
            ok = abs(delta - PUR_AMOUNT) <= TOLERANCE and present
            record(
                "2-purchase", "Purchase",
                f"payable +{PUR_AMOUNT:,.0f} and bill {PUR_BILL_REF} present",
                f"payable {base_payable:,.2f} -> {after_pur:,.2f} (delta {delta:+,.2f}); "
                f"bill present={present}",
                ok,
            )
        except Exception as e:  # noqa: BLE001
            record("2-purchase", "Purchase", f"payable +{PUR_AMOUNT:,.0f}",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Step 3: Debit Note (settles payable; payable should DECREASE) ──
        try:
            banner(f"Step 3 — Debit Note {DN_AMOUNT:,.0f} (Agst Ref {PUR_BILL_REF}) "
                   f"→ payable should DECREASE  [CRITICAL DIRECTION CHECK]")
            before_dn, _ = await read_payable(client, led.supplier)
            print(f"  payable BEFORE DN: {before_dn:,.2f}")
            result = await writer.create_debit_note(
                date=VCH_DATE,
                party_ledger=led.supplier,
                purchase_ledger=led.purchase,
                amount=DN_AMOUNT,
                narration=f"{NPFX} debit note test",
                bill_ref=PUR_BILL_REF,
                known_ledgers=known,
            )
            print(f"  RAW create response (parsed): {json.dumps(result, indent=2)}")
            _capture_master_id(result, "Debit Note", "Step 3 Debit Note")
            after_dn, _ = await read_payable(client, led.supplier)
            delta = after_dn - before_dn  # expect ≈ -DN_AMOUNT
            net_vs_baseline = after_dn - base_payable  # expect ≈ +3000
            decreased = delta < 0 and abs(delta + DN_AMOUNT) <= TOLERANCE
            if not decreased:
                print("\n" + "!" * 78)
                print("  !!!  DN DIRECTION WRONG  !!!")
                print(f"  Debit Note did NOT reduce the payable as required.")
                print(f"  payable {before_dn:,.2f} -> {after_dn:,.2f} (delta {delta:+,.2f}); "
                      f"expected delta ≈ {-DN_AMOUNT:+,.0f}")
                print("!" * 78)
            record(
                "3-debit-note", "Debit Note",
                f"payable DECREASES by ~{DN_AMOUNT:,.0f} "
                f"(net ~{PUR_AMOUNT - DN_AMOUNT:,.0f} above baseline)",
                f"payable {before_dn:,.2f} -> {after_dn:,.2f} (delta {delta:+,.2f}); "
                f"net vs baseline {net_vs_baseline:+,.2f}"
                + ("" if decreased else "  <-- DN DIRECTION WRONG"),
                decreased,
            )
        except Exception as e:  # noqa: BLE001
            record("3-debit-note", "Debit Note", f"payable -{DN_AMOUNT:,.0f}",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Step 4: Sales (raises a NEW receivable bill, increases receivable) ─
        try:
            banner(f"Step 4 — Sales {SAL_AMOUNT:,.0f} (New Ref {SAL_BILL_REF}) "
                   f"→ receivable should INCREASE")
            print(f"  receivable BEFORE: {base_receivable:,.2f}")
            result = await writer.create_sales_voucher_ledger(
                date=VCH_DATE,
                party_ledger=led.customer,
                sales_ledger=led.sales,
                amount=SAL_AMOUNT,
                narration=f"{NPFX} sales test",
                bill_ref=SAL_BILL_REF,
                known_ledgers=known,
            )
            print(f"  RAW create response (parsed): {json.dumps(result, indent=2)}")
            _capture_master_id(result, "Sales", "Step 4 Sales")
            after_sal, recv_bills = await read_receivable(client, led.customer)
            delta = after_sal - base_receivable
            present = _bill_present(recv_bills, led.customer, SAL_BILL_REF)
            ok = abs(delta - SAL_AMOUNT) <= TOLERANCE and present
            record(
                "4-sales", "Sales",
                f"receivable +{SAL_AMOUNT:,.0f} and bill {SAL_BILL_REF} present",
                f"receivable {base_receivable:,.2f} -> {after_sal:,.2f} (delta {delta:+,.2f}); "
                f"bill present={present}",
                ok,
            )
        except Exception as e:  # noqa: BLE001
            record("4-sales", "Sales", f"receivable +{SAL_AMOUNT:,.0f}",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Step 5: Credit Note (settles receivable; should DECREASE) ─────
        try:
            banner(f"Step 5 — Credit Note {CN_AMOUNT:,.0f} (Agst Ref {SAL_BILL_REF}) "
                   f"→ receivable should DECREASE  [CRITICAL DIRECTION CHECK]")
            before_cn, _ = await read_receivable(client, led.customer)
            print(f"  receivable BEFORE CN: {before_cn:,.2f}")
            result = await writer.create_credit_note(
                date=VCH_DATE,
                party_ledger=led.customer,
                sales_ledger=led.sales,
                amount=CN_AMOUNT,
                narration=f"{NPFX} credit note test",
                bill_ref=SAL_BILL_REF,
                known_ledgers=known,
            )
            print(f"  RAW create response (parsed): {json.dumps(result, indent=2)}")
            _capture_master_id(result, "Credit Note", "Step 5 Credit Note")
            after_cn, _ = await read_receivable(client, led.customer)
            delta = after_cn - before_cn  # expect ≈ -CN_AMOUNT
            net_vs_baseline = after_cn - base_receivable  # expect ≈ +5000
            decreased = delta < 0 and abs(delta + CN_AMOUNT) <= TOLERANCE
            if not decreased:
                print("\n" + "!" * 78)
                print("  !!!  CN DIRECTION WRONG  !!!")
                print(f"  Credit Note did NOT reduce the receivable as required.")
                print(f"  receivable {before_cn:,.2f} -> {after_cn:,.2f} (delta {delta:+,.2f}); "
                      f"expected delta ≈ {-CN_AMOUNT:+,.0f}")
                print("!" * 78)
            record(
                "5-credit-note", "Credit Note",
                f"receivable DECREASES by ~{CN_AMOUNT:,.0f} "
                f"(net ~{SAL_AMOUNT - CN_AMOUNT:,.0f} above baseline)",
                f"receivable {before_cn:,.2f} -> {after_cn:,.2f} (delta {delta:+,.2f}); "
                f"net vs baseline {net_vs_baseline:+,.2f}"
                + ("" if decreased else "  <-- CN DIRECTION WRONG"),
                decreased,
            )
        except Exception as e:  # noqa: BLE001
            record("5-credit-note", "Credit Note", f"receivable -{CN_AMOUNT:,.0f}",
                   f"EXCEPTION {type(e).__name__}: {e}", False)

    finally:
        # ── Step 6: Cleanup — delete all captured vouchers, verify restore ──
        banner("Step 6 — CLEANUP (delete all captured vouchers by Master ID)")
        if not MASTER_IDS:
            print("  No Master IDs captured — nothing to delete.")
        for vtype, mid in MASTER_IDS:
            await cleanup_voucher(client, vtype, mid, f"{vtype} (mid={mid})")

        # Re-read to confirm books restored to baseline (±TOLERANCE).
        if led is not None:
            banner("Step 6 — Verify books restored to baseline")
            try:
                final_payable, _ = await read_payable(client, led.supplier)
                final_receivable, _ = await read_receivable(client, led.customer)
                pay_ok = abs(final_payable - base_payable) <= TOLERANCE
                recv_ok = abs(final_receivable - base_receivable) <= TOLERANCE
                restored = pay_ok and recv_ok
                if restored:
                    print("\n  ✅ books restored — payable & receivable back to baseline.")
                else:
                    print("\n  ⚠️  RESIDUE REMAINS — books NOT fully restored:")
                    if not pay_ok:
                        print(f"     payable: baseline {base_payable:,.2f} vs now "
                              f"{final_payable:,.2f} (residue {final_payable - base_payable:+,.2f})")
                    if not recv_ok:
                        print(f"     receivable: baseline {base_receivable:,.2f} vs now "
                              f"{final_receivable:,.2f} (residue {final_receivable - base_receivable:+,.2f})")
                record(
                    "6-cleanup", "(delete + read-back)",
                    "payable & receivable restored to baseline (±1)",
                    f"payable {base_payable:,.2f}->{final_payable:,.2f}; "
                    f"receivable {base_receivable:,.2f}->{final_receivable:,.2f}; "
                    + ("books restored" if restored else "RESIDUE REMAINS"),
                    restored,
                )
            except Exception as e:  # noqa: BLE001
                print(f"  [restore-check ERROR] {type(e).__name__}: {e}")
                record("6-cleanup", "(delete + read-back)",
                       "books restored to baseline", f"EXCEPTION {type(e).__name__}: {e}", False)

        # ── Final results table ──
        banner("RESULTS TABLE")
        print(f"{'STEP':<16} {'VOUCHER':<14} {'PASS/FAIL':<10} {'EXPECTED':<46} OBSERVED")
        print("-" * 140)
        for r in RESULTS:
            status = "PASS" if r.passed else "FAIL"
            print(f"{r.step:<16} {r.voucher:<14} {status:<10} {_short(r.expected, 44):<46} "
                  f"{_short(r.observed, 70)}")
        n_fail = sum(1 for r in RESULTS if not r.passed)
        print("-" * 140)
        print(f"{len(RESULTS)} steps — {len(RESULTS) - n_fail} pass, {n_fail} fail")

        # ── Overall DN/CN direction verdict ──
        dn = next((r for r in RESULTS if r.step == "3-debit-note"), None)
        cn = next((r for r in RESULTS if r.step == "5-credit-note"), None)
        banner("OVERALL VERDICT — Debit Note / Credit Note DIRECTION")
        dn_ok = dn is not None and dn.passed
        cn_ok = cn is not None and cn.passed
        print(f"  Debit Note  reduces PAYABLE   : {'PASS' if dn_ok else 'FAIL'}")
        print(f"  Credit Note reduces RECEIVABLE: {'PASS' if cn_ok else 'FAIL'}")
        if dn_ok and cn_ok:
            print("\n  ✅ VERDICT: Group B write directions are CORRECT "
                  "(DN reduces payable, CN reduces receivable).")
        else:
            print("\n  ❌ VERDICT: Group B write DIRECTION is WRONG — see flagged steps above.")

        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Group B Tally WRITE-direction manual test (Purchase/DN/Sales/CN, live, self-cleaning)"
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    asyncio.run(run(args.host, args.port))
