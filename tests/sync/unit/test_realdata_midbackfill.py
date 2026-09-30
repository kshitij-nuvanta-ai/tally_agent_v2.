"""Mid-backfill parity on FakeBooks company B (S1 spec §16 "Mid-backfill (R30 regression)", §15.5 row 11, D9).
Pure: ``fakeb`` reads company B back out of FakeBooks with the agent's own requests, ``realdata.pure_parity`` runs
the engine's pure pieces over it. FakeBooks' current period (ruling F25) is FY 2025-26, so as_on = 31-03-2026.

- Window FY 2024-25 / 2025-26 complete, older FYs pending: the verified edge is 01-04-2024, the anchor is the
  ledger-level TB as-on 31-03-2024, and the result is ``ok`` although the all-time balances are far from anything
  the window's lines alone add up to (R30: an anchor-less or wrongly-anchored engine would mismatch).
- Complete history (every FY complete): ``plan()`` switches to the books-start anchor (as_on == books_from, our own
  day-one lines subtracted) and the result is still ``ok``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from backend.sync.parity.anchors import plan
from backend.sync.coverage import Cov, edges
from tests.sync import realdata as rd
from tests.sync.fakeb import BOOKS_FROM, fakeb_company_b, fy_months

AS_ON = date(2026, 3, 31)
CURRENT_FY = date(2026, 4, 1)          # the conftest clock's IST FY (2026-27): coverage is walked back from it
CAPTURED = datetime(2026, 9, 25, 6, tzinfo=timezone.utc)
FYS = [date(y, 4, 1) for y in range(2022, 2027)]


@pytest.fixture(scope="module")
def fb():
    return fakeb_company_b()


def _coverage(complete_from: date) -> list[Cov]:
    rows = []
    for fy in FYS:
        months = fy_months(fy, until=date(2026, 9, 30))
        done = months if fy >= complete_from else []
        rows.append(Cov(fy, "complete" if fy >= complete_from else "pending", done, len(months)))
    return rows


def _run(fb, edge: date) -> tuple[rd.ParityResult, rd.Assembled]:
    anchor_on = plan(edge, BOOKS_FROM).as_on
    b = fb.assembled(edge, AS_ON, CAPTURED)
    snaps = {"trial_balance": fb.tb_snapshot(AS_ON, False), "trial_balance_ledgerwise": fb.tb_snapshot(AS_ON, True),
             "anchor": fb.tb_snapshot(anchor_on, True)}
    return rd.pure_parity(b, snapshots=snaps, mirrored=rd.mirrored_from_masters(b), as_on=AS_ON,
                          verified_edge=edge), b


def _breakdown(result: rd.ParityResult) -> list:
    return [(l.scope, l.name, l.our, l.tally, l.diff, l.verdict, l.cause) for l in result.problems()]


def test_mid_backfill_ok_via_anchor_as_on_31_03_2024(fb):
    _, verified = edges(_coverage(date(2024, 4, 1)), CURRENT_FY)
    assert verified == date(2024, 4, 1)
    assert plan(verified, BOOKS_FROM).as_on == date(2024, 3, 31)
    result, b = _run(fb, verified)
    assert result.status == "ok", _breakdown(result)
    assert result.anchor_as_on == date(2024, 3, 31)
    # R30: our = anchor + Σ window lines, so Tally - Σ window lines = the anchor (to within tolerance) -- for these
    # matched balance-sheet ledgers the window's lines alone are more than ₹1,000 off Tally's figure.
    bs = [l for l in result.lines if l.scope == "ledger" and l.verdict == "match" and l.cause is None
          and l.our is not None and l.tally is not None]
    far = [l for l in bs if abs(l.tally - (l.our - result.anchors.get(l.guid, D("0")))) > D("1000")]
    assert len(far) >= 5, [(l.name, l.our, l.tally) for l in bs]
    assert all(rd.tally_date(v["data"]["date"]) >= verified for v in b.vouchers)


def test_complete_history_switches_to_books_start_anchor(fb):
    """D9: every FY complete -> the verified edge reaches FY(books_from) -> the anchor is the ledger-level TB as-on
    books_from minus our own lines dated books_from; still `ok`."""
    _, verified = edges(_coverage(FYS[0]), CURRENT_FY)
    assert verified == BOOKS_FROM
    anchor_plan = plan(verified, BOOKS_FROM)
    assert (anchor_plan.as_on, anchor_plan.subtract_lines_dated) == (BOOKS_FROM, BOOKS_FROM)
    result, _ = _run(fb, verified)
    assert result.status == "ok", _breakdown(result)
    assert result.anchor_as_on == BOOKS_FROM
