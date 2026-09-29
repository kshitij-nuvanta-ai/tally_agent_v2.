"""Parity engine -- the DB wiring of the pure core (S1 spec §7.14, §10.1-§10.10, D9, D10, §15.5).

``run_parity`` checks §10.1's preconditions **in order** (1 counters moved -> ``aborted_moving``; 2 counters not the
stored cursors -> ``aborted_behind``; 3 no verified span / ``first_sync`` / ``restore_detected`` ->
``aborted_incomplete``; 4 a required snapshot missing -> ``aborted_incomplete`` + ``capture_snapshot``; 5 ledgers not in
this capture are kept for rung 1's ``missing_in_tally``; 6 the D10 TB-imbalance guard -> ``discarded_stale``). Every
abort stores one ``parity_runs`` row with that status and **zero** lines, and never touches the ladder or
``last_parity`` (§10.8 last bullet).

Otherwise it loads the inputs (§10.2) -- live ledgers + their groups' derived nature, the snapshots, and the line sums
through the covering index ``ix_lines_cover`` (one ``SUM ... GROUP BY ledger_guid`` per date range) -- runs rung 1,
forex (C47), rung 2, the classifier and the ladder, and in ONE transaction writes the run, one ``parity_lines`` row per
ledger/group line, ``last_parity`` and the ladder. After the commit it emits the ops signal (§10.10, decision 14)
once per non-``ok`` run, and ``engineering_flag`` on entering ``hard_alert``.

Decisions made here (see the task-10c report):
- "Tally's current period" (F25) is the request's ``as_on`` for ``daily | recheck | post_resync`` (§10.2: as_on =
  the current period's end, where the mirrored balances and their face fields live). ``bisect`` evaluates past
  month-ends: every Tally figure comes from that month-end's ledger-level TB and no face field is used (10c carry
  "Past-FY as_on").
- Forex set rule with an anchor at E−1: the anchor TB's rows are already revalued by Tally, so the revaluation the
  run must explain is the as-on TB's ``Unadjusted Forex Gain/Loss`` row MINUS the anchor TB's (0 when E =
  books_from and no forex line precedes it -- exactly §10.5's formula then).
- ``bisect`` runs don't step the ladder (they are a diagnostic); they record ``bisect_month`` in it for §10.8's
  resync offer, and never update ``last_parity``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from v2.cloud.clock import Clock, fy_end_of, fy_start_of, ist_date
from v2.cloud.config import V2Settings
from v2.cloud.errors import ApiError
from v2.cloud.ingest import snapshots as snapshots_mod
from v2.cloud.ingest.resolve import NameIndex, ResolveError
from v2.cloud.models import (ParityLine, ParityRun, SyncFyCoverage, SyncRun, SyncWorkspace, TallyGroup, TallyLedger,
                             TallyReportSnapshot, TallyVoucher)
from v2.cloud.parity import bisect as bisect_mod
from v2.cloud.parity import classify as classify_mod
from v2.cloud.parity import ladder as ladder_mod
from v2.cloud.parity import opsignal
from v2.cloud.parity.anchors import AnchorPlan, anchor_amounts, plan, require_ledgerwise, resolve_rows
from v2.cloud.parity.forex import forex_lines
from v2.cloud.parity.model import (BS_NATURES, PROBLEM_VERDICTS, ZERO, LedgerIn, Line, Sums, TbRow, bs_verified,
                                   has_problem, is_pl_account, synthetic_amount, tb_rows)
from v2.cloud.parity.rung1 import rung1
from v2.cloud.parity.rung2 import primary_group_rows, rung2
from v2.cloud.sync import coverage
from v2.contract.models import ParityRequest
from v2.contract.parse import WireParseError, tally_date
from v2.contract.tally_rules import OPENING_STOCK_ROW, UNADJUSTED_FOREX_ROW

SCOPES = ("daily", "recheck", "bisect", "post_resync")
LADDER_STATES = ("ok", "suspect", "alert", "hard_alert")
LW = "trial_balance_ledgerwise"
TB = "trial_balance"
CENT = Decimal("0.01")
STOCK_IN_HAND = "Stock-in-Hand"
DEFAULT_STOCK_BEARING = "Current Assets"
BLOCKING_STATES = ("awaiting_first_connection", "first_sync", "restore_detected")

# The covering-index read (§4.5 `ix_lines_cover`: (workspace_id, ledger_guid, voucher_date) INCLUDE (amount,
# fx_amount) WHERE countable). One statement per date range; `test_parity_sums_use_the_covering_index` EXPLAINs it.
SUMS_SQL = text(
    "SELECT ledger_guid, SUM(amount) AS total, SUM(fx_amount) AS face, COUNT(*) - COUNT(fx_amount) AS no_face "
    "FROM tally_voucher_ledger_lines "
    "WHERE workspace_id = :w AND countable AND voucher_date BETWEEN :a AND :b "
    "GROUP BY ledger_guid")


# --- small pure helpers (unit-tested in test_parity_engine_pure.py) ----------------------------------------------------


def quantize(d: Decimal | None) -> Decimal | None:
    """Imbalances compare as Decimals at 0.01 (10c carry): "0" == "0.00"."""
    return None if d is None else Decimal(d).quantize(CENT)


def imbalance_moved(baseline: dict | None, alt_mst_id: int, imbalance: Decimal, tol: Decimal) -> str:
    """D10 on a stored baseline ``{"alt_mst_id", "imbalance"}``: ``"stale"`` if the masters didn't change
    (same ``alt_mst_id``) but the imbalance moved past the tolerance; ``"rebaseline"`` when there is no baseline or
    ``alt_mst_id`` moved; else ``"same"``."""
    if not baseline or baseline.get("alt_mst_id") != alt_mst_id:
        return "rebaseline"
    if abs(quantize(imbalance) - quantize(Decimal(str(baseline["imbalance"])))) > tol:
        return "stale"
    return "same"


def effective_edge(verified_fy: date, books_from: date) -> date:
    """E for D9: the verified edge is an FY start, but the books may begin mid-FY (`books_from` 15-06-2022 ->
    FY(books_from) = 01-04-2022); nothing precedes books_from, so E is the later of the two -- and then E =
    books_from takes D9's books-start anchor (TB as-on books_from minus that day's lines)."""
    return max(verified_fy, books_from)


def last_parity_view(prev: dict | None, status: str, checked_at: datetime, as_on: date, verified_from: date,
                     mismatch_count: int, run_id: str) -> dict:
    """``last_parity`` (§7.13). Review I2 / controller ruling: ``suspect`` is invisible, so a suspect run writes the
    ``ok`` view -- ``mismatch_count`` 0 and the PREVIOUS visible run's ``run_id`` (None if there was none); only
    ``checked_at`` / ``as_on`` / ``verified_from`` describe this run."""
    if status == "suspect":
        return {"state": "ok", "checked_at": checked_at.isoformat(), "as_on": as_on.isoformat(), "mismatch_count": 0,
                "verified_from": verified_from.isoformat(), "run_id": (prev or {}).get("run_id")}
    return {"state": status, "checked_at": checked_at.isoformat(), "as_on": as_on.isoformat(),
            "mismatch_count": mismatch_count, "verified_from": verified_from.isoformat(), "run_id": run_id}


def month_ends(start: date, end: date) -> list[date]:
    """Every calendar month-end d with start <= d <= end, oldest first."""
    out: list[date] = []
    y, m = start.year, start.month
    while True:
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        me = nxt - timedelta(days=1)
        if me > end:
            return out
        if me >= start:
            out.append(me)
        y, m = nxt.year, nxt.month


def summary(lines: list[Line], unrealised_total: Decimal) -> dict:
    return {
        "ledgers_compared": sum(1 for l in lines if l.scope == "ledger" and l.verdict != "not_applicable"),
        "groups_compared": sum(1 for l in lines if l.scope == "group" and l.verdict != "not_applicable"),
        "mismatches": sum(1 for l in lines if l.verdict in PROBLEM_VERDICTS),
        "match_revalued": sum(1 for l in lines if l.verdict == "match_revalued"),
        "forex_unrealised_total": str(quantize(unrealised_total)),
    }


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def _date(v) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(v)


def _rem_json(r: classify_mod.Remediation) -> dict:
    return {"id": r.id, "action": r.action, "params": r.params}


def _capture(report_type: str, as_on: date) -> classify_mod.Remediation:
    return classify_mod._remediation("capture_snapshot", {"report_type": report_type, "as_on": as_on.isoformat()})


# --- inputs ------------------------------------------------------------------------------------------------------------


@dataclass
class _Snap:
    rows: list[TbRow]
    cells: list[dict]
    flags: dict | None
    counters: dict | None = None

    @property
    def ledgerwise(self) -> bool:
        try:
            require_ledgerwise(self.flags)
            return True
        except ValueError:
            return False


async def _snapshots(session: AsyncSession, ws: uuid.UUID, keys: list[tuple[str, date]]) -> dict[tuple[str, date], _Snap]:
    if not keys:
        return {}
    t = TallyReportSnapshot
    rows = (await session.execute(select(t.report_type, t.as_on_date, t.rows, t.cells, t.request_flags, t.counters)
                                  .where(t.workspace_id == ws, t.report_type.in_({k[0] for k in keys}),
                                         t.as_on_date.in_({k[1] for k in keys})))).all()
    return {(r.report_type, r.as_on_date): _Snap(tb_rows(r.rows or []), list(r.cells or []), r.request_flags,
                                                 r.counters)
            for r in rows if (r.report_type, r.as_on_date) in set(keys)}


def _stale_after_master_change(snap: _Snap, unresolved: list[str], cursor_alt_mst_id: int | None) -> bool:
    """S1 review I7: a STORED ledger-level TB (the D9 anchor, a bisect month-end) is captured once and reused, but a
    ledger rename is exported retroactively (probe 8, §12 step 9) -- the stored row keeps the old name and resolves
    to nothing, which would read as a false ``masters_gap`` + ``ledger_gap`` mismatch that no remediation fixes
    (the ladder then climbs to ``hard_alert``). Such a snapshot is treated as stale -- re-capture it -- when it has
    rows resolving to no live ledger AND it was not captured at the workspace's current master cursor
    (``snap.counters.alt_mst_id != sw.cursor_alt_mst_id``).

    Task 14b C1 (I7 residual): the bound is the workspace's OWN current counter space, not the mirrored ledgers'
    ``alter_id``s -- after a restore (counters go backwards) or a relink (another company's counter space) the
    mirror still holds ledgers above the new Tally's ``AltMstID``, and comparing against them made every re-capture
    stale again (an endless ``anchor_stale`` loop). Parity already requires ``counters_before == cursor``
    (precondition 2), so a snapshot re-captured now carries at least the cursor and is never stale: a genuinely
    missing master causes at most one re-capture, then flows to the normal ``masters_gap`` classification.

    Task 14b fix round 1 (controller ruling): ``!=``, not ``<``. At parity time ``counters_before == cursor`` and
    Tally's counters only fall on a restore / relink, so a snapshot ABOVE the cursor comes from a counter space
    that no longer exists (e.g. an anchor re-captured after a post-backup rename, then the backup restored and the
    confirmed resync putting the old name back): it is re-captured too, never computed into a false mismatch."""
    if not unresolved or cursor_alt_mst_id is None:
        return False
    mst = (snap.counters or {}).get("alt_mst_id")
    if mst is None:
        return False
    return int(mst) != int(cursor_alt_mst_id)


async def _ranged_sums(session: AsyncSession, ws: uuid.UUID, a: date, b: date) -> dict[str, tuple]:
    if a > b:
        return {}
    rows = (await session.execute(SUMS_SQL, {"w": ws, "a": a, "b": b})).all()
    return {r.ledger_guid: (Decimal(r.total), None if r.face is None else Decimal(r.face), r.no_face) for r in rows}


async def load_sums(session: AsyncSession, ws: uuid.UUID, verified_edge: date, as_on: date) -> Sums:
    """§10.2 windows from the covering index: ``total`` over [E, as_on]; ``fy_total`` / ``face`` /
    ``face_complete`` over [FY(as_on).start, as_on] -- the same shape ``model.build_sums`` makes from line facts."""
    whole = await _ranged_sums(session, ws, verified_edge, as_on)
    fy = await _ranged_sums(session, ws, fy_start_of(as_on), as_on)
    return Sums(total={g: v[0] for g, v in whole.items()},
                face={g: v[1] for g, v in fy.items() if v[1] is not None},
                face_complete={g: v[2] == 0 for g, v in fy.items()},
                fy_total={g: v[0] for g, v in fy.items()})


@dataclass
class _Ledgers:
    ins: list[LedgerIn]
    index: NameIndex
    stock_bearing: str


async def _ledgers(session: AsyncSession, ws: uuid.UUID, *, capture_started_at: datetime | None) -> _Ledgers:
    """Live ledgers as parity sees them. ``capture_started_at=None`` is the bisect form: no mirrored figure and no
    face field (a month-end is not the mirror's date), every live ledger counted as present (its TB row is the
    evidence)."""
    l, g = TallyLedger, TallyGroup
    rows = (await session.execute(
        select(l.guid, l.name, l.group_guid, g.primary_group, g.nature, l.is_forex, l.closing_balance,
               l.closing_fx_amount, l.closing_fx_rate, l.opening_fx_amount, l.opening_balance, l.balance_source,
               l.balance_captured_at)
        .select_from(l)
        .outerjoin(g, (g.workspace_id == l.workspace_id) & (g.guid == l.group_guid) & g.is_deleted.is_(False))
        .where(l.workspace_id == ws, l.is_deleted.is_(False)).order_by(l.name))).all()
    ins: list[LedgerIn] = []
    for r in rows:
        if capture_started_at is None:
            ins.append(LedgerIn(r.guid, r.name, r.group_guid, r.primary_group, r.nature, r.is_forex, None, None, None,
                                None, "needs_tb", True))
            continue
        opening_fx = r.opening_fx_amount
        if opening_fx is None and r.opening_balance is not None and Decimal(r.opening_balance) == 0:
            opening_fx = Decimal("0")                 # a plain zero opening is a known face (no foreign amount)
        in_capture = r.balance_captured_at is not None and r.balance_captured_at >= capture_started_at   # §10.1(5)
        ins.append(LedgerIn(
            r.guid, r.name, r.group_guid, r.primary_group, r.nature, r.is_forex,
            None if r.closing_balance is None else Decimal(r.closing_balance),
            None if r.closing_fx_amount is None else Decimal(r.closing_fx_amount),
            None if r.closing_fx_rate is None else Decimal(r.closing_fx_rate),
            None if opening_fx is None else Decimal(opening_fx), r.balance_source, in_capture))
    index = NameIndex.from_rows([("ledger", x.guid, x.name, False) for x in ins])
    stock = (await session.execute(select(g.primary_group).where(
        g.workspace_id == ws, g.is_deleted.is_(False), g.name == STOCK_IN_HAND))).scalar_one_or_none()
    return _Ledgers(ins, index, stock or DEFAULT_STOCK_BEARING)


async def _flagged_amounts(session: AsyncSession, ws: uuid.UUID, a: date, b: date) -> dict[str, set[Decimal]]:
    """Ruling F7: only OPTIONAL vouchers' amounts (a cancelled voucher exports none)."""
    rows = (await session.execute(text(
        "SELECT ll.ledger_guid, ll.amount FROM tally_voucher_ledger_lines ll JOIN tally_vouchers v ON v.id = "
        "ll.voucher_id WHERE ll.workspace_id = :w AND v.is_optional AND NOT v.is_deleted "
        "AND ll.voucher_date BETWEEN :a AND :b"), {"w": ws, "a": a, "b": b})).all()
    out: dict[str, set[Decimal]] = {}
    for guid, amount in rows:
        out.setdefault(guid, set()).update({Decimal(amount), -Decimal(amount)})
    return out


async def _require_as_on_not_past_mirrored_fy(session: AsyncSession, ws: uuid.UUID, as_on: date,
                                              today: date) -> None:
    """S1 review M20 (rulings F3/F25): outside bisect, ``as_on`` must lie in Tally's current period -- the FY the
    mirrored ledger balances (and their face fields) describe; a daily run at a past ``as_on`` would face-check a
    current-period opening against another FY's lines (a false ``forex_face_mismatch``). The server stores no
    "current period", so it takes the FY of the latest voucher it holds as the lower bound of that period (Tally's
    period can't end before a voucher it contains): ``FY(as_on)`` older than that FY -> 422
    ``as_on_not_current_period``, nothing stored. A later ``as_on`` (e.g. a new FY with no voucher yet) is never
    refused here.

    Task 14b C4 (controller ruling): the bound counts only books data that proves the period -- a voucher that is
    not deleted, not post-dated, not optional (§10.2: optional vouchers are not books data, and are often dated
    ahead) and dated on or before ``today`` (IST): a voucher mistyped into a future FY must not refuse every
    daily run until someone finds it."""
    v = TallyVoucher
    latest = (await session.execute(select(func.max(v.date)).where(
        v.workspace_id == ws, v.is_deleted.is_(False), v.is_post_dated.is_(False), v.is_optional.is_(False),
        v.date <= today))).scalar_one_or_none()
    if latest is not None and fy_start_of(as_on) < fy_start_of(latest):
        raise ApiError(422, "as_on_not_current_period")


async def _verified_edge(session: AsyncSession, ws: uuid.UUID, clock: Clock) -> date | None:
    rows = (await session.execute(select(SyncFyCoverage).where(SyncFyCoverage.workspace_id == ws))).scalars().all()
    _, verified = coverage.edges([coverage.to_cov(r) for r in rows], fy_start_of(ist_date(clock.now())))
    return verified


def _group_anchors(rows: list[TbRow], ledgers: list[LedgerIn], day_one: dict[str, Decimal] | None,
                   stock_bearing: str) -> dict[str, Decimal]:
    """The group-level anchor (10c carry): the group TB as-on E−1, first-occurrence primary rows, net of that TB's
    own Opening Stock (rung 2 adds the as-on TB's) and, for the books_from plan, of our day-one lines."""
    out = dict(primary_group_rows(rows))
    stock = synthetic_amount(rows, OPENING_STOCK_ROW)
    if stock is not None:
        out[stock_bearing] = out.get(stock_bearing, ZERO) - stock
    for l in ledgers:
        if day_one and l.guid in day_one and l.primary_group is not None and not is_pl_account(l):
            out[l.primary_group] = out.get(l.primary_group, ZERO) - day_one[l.guid]
    return out


def _net_unadjusted(as_on_rows: list[TbRow], anchor_rows: list[TbRow] | None) -> Decimal | None:
    now = synthetic_amount(as_on_rows, UNADJUSTED_FOREX_ROW)
    then = synthetic_amount(anchor_rows or [], UNADJUSTED_FOREX_ROW)
    if now is None and then is None:
        return None
    return (now or ZERO) - (then or ZERO)


@dataclass
class _Evaluation:
    lines: list[Line]
    unrealised_total: Decimal
    anchors: dict[str, Decimal]


def _evaluate(led: _Ledgers, anchors: dict[str, Decimal] | None, anchor_unresolved: list[str], sums: Sums,
              lw: _Snap, group: _Snap | None, group_anchors: dict[str, Decimal] | None, unadjusted: Decimal | None,
              tol: Decimal) -> _Evaluation:
    ledgerwise_tb, lw_unresolved = resolve_rows(lw.rows, led.index)
    r1 = rung1(led.ins, anchors, sums, ledgerwise_tb, [*anchor_unresolved, *lw_unresolved], tol,
               ledgerwise_flags=lw.flags)
    fl, unrealised = forex_lines([l for l in led.ins if l.is_forex], anchors, sums, ledgerwise_tb, unadjusted, tol)
    group_unrealised = None
    if anchors is None and group_anchors is not None:
        # No ledger anchor: the revaluation can't be split per ledger; it is attributable only when every
        # balance-sheet forex ledger sits under one primary group (§10.5(b) set sum = −unadjusted).
        primaries = {l.primary_group for l in led.ins if l.is_forex and l.in_capture and l.nature in BS_NATURES
                     and not is_pl_account(l)}
        group_unrealised = {next(iter(primaries)): -(unadjusted or ZERO)} if len(primaries) == 1 else {}
    group_rows = primary_group_rows(group.rows) if group is not None else {}
    opening_stock = synthetic_amount(group.rows if group is not None else lw.rows, OPENING_STOCK_ROW)
    r2 = rung2(led.ins, r1, fl, group_rows, opening_stock, led.stock_bearing, ledgerwise_tb, sums, [], tol,
               ledgerwise_flags=lw.flags, group_anchors=group_anchors, group_unrealised=group_unrealised)
    if group is None:                          # a bisect month-end with no stored group TB: no group comparison
        r2 = [l for l in r2 if l.scope != "group"]
    return _Evaluation([*r1, *fl, *r2], unrealised, anchors or {})


# --- storage -----------------------------------------------------------------------------------------------------------


def _line_row(ws: uuid.UUID, run_id: uuid.UUID, as_on: date, l: Line) -> dict:
    return {"id": uuid.uuid4(), "workspace_id": ws, "run_id": run_id, "scope": l.scope, "guid": l.guid,
            "name": l.name, "our_amount": l.our, "tally_amount": l.tally, "diff": l.diff, "verdict": l.verdict,
            "cause": l.cause, "remediation_status": "issued" if l.verdict in PROBLEM_VERDICTS else None,
            "unrealised_diff": l.unrealised, "our_fx_amount": l.our_fx, "tally_fx_amount": l.tally_fx,
            "as_on_date": as_on}


def _ladder_view(ladder: dict) -> dict:
    return {"state": ladder.get("state", "ok"), "heal_attempts": ladder.get("heal_attempts", 0),
            "resync_offered_fy": ladder.get("resync_offered_fy")}


class _Run:
    """The ``parity_runs`` row plus what the response needs."""

    def __init__(self, sw: SyncWorkspace, body: ParityRequest, as_on: date, now: datetime):
        self.sw = sw
        self.row = ParityRun(id=uuid.uuid4(), workspace_id=sw.workspace_id, as_on_date=as_on, rung=2, scope=body.scope,
                             verified_from=as_on, counters_before=body.counters_before.model_dump(),
                             counters_after=body.counters_after.model_dump(), started_at=now)
        self.events: list[dict] = []


async def _finish_abort(session: AsyncSession, run: _Run, status: str, reason: str, now: datetime,
                        remediation: list[classify_mod.Remediation] | None = None) -> dict:
    """Preconditions 1-4, 6 (and the no-BS-verified guard): one run row with that status, zero lines, ladder,
    ``last_parity`` and the D10 baseline untouched (§10.1, §10.8), no ops event."""
    rems = remediation or []
    run.row.status, run.row.abort_reason, run.row.finished_at = status, reason, now
    run.row.lines_compared, run.row.mismatch_count = 0, 0
    run.row.remediation = [_rem_json(r) for r in rems]
    session.add(run.row)
    await session.flush()
    # Review I1 / controller ruling: an abort is "no alert" (§10.1) -- no integrity ops event (decision 14's
    # signal is for integrity alerts, i.e. computed non-ok runs only).
    return {"parity_run_id": str(run.row.id), "status": status, "abort_reason": reason, "summary": None,
            "remediation": [_rem_json(r) for r in rems], "ladder": _ladder_view(run.sw.ladder or {})}


# --- the engine --------------------------------------------------------------------------------------------------------


async def run_parity(session: AsyncSession, sw: SyncWorkspace, body: ParityRequest, settings: V2Settings,
                     clock: Clock) -> dict:
    if body.scope not in SCOPES:
        raise ApiError(422, "invalid_scope")
    try:
        as_on = tally_date(body.as_on_date)
    except WireParseError:
        raise ApiError(422, "bad_as_on_date") from None
    fy_arg: date | None = None
    if body.scope == "bisect":
        try:
            fy_arg = date.fromisoformat(body.fy_start or "")
        except ValueError:
            raise ApiError(422, "fy_start_required") from None
        if fy_arg != fy_start_of(fy_arg):
            raise ApiError(422, "fy_start_required")
    else:
        await _require_as_on_not_past_mirrored_fy(session, sw.workspace_id, as_on,
                                                  ist_date(clock.now()))      # review M20, C4

    await session.refresh(sw, with_for_update=True)     # one parity run at a time per workspace (ladder)
    tol = Decimal(settings.parity_tolerance_paise) / 100
    now = clock.now()
    run = _Run(sw, body, as_on, now)
    before, after = body.counters_before, body.counters_after

    try:
        result = await _run(session, sw, body, run, as_on, fy_arg, tol, now, clock, before, after)
    except ResolveError as exc:                                  # ambiguous name: retryable (§11), nothing stored
        raise ApiError(409, exc.code) from None
    await session.commit()
    for event in run.events:
        opsignal.emit(event)
    return result


async def _run(session, sw, body, run: _Run, as_on, fy_arg, tol, now, clock, before, after) -> dict:
    ws = sw.workspace_id
    # 1. quiescence (probe 19)
    if before.model_dump() != after.model_dump():
        return await _finish_abort(session, run, "aborted_moving", "counters_moved", now)
    # 2. the server's own proof the outbox is drained and the cursor caught up
    if (before.alt_vch_id, before.alt_mst_id) != (sw.cursor_alt_vch_id, sw.cursor_alt_mst_id):
        return await _finish_abort(session, run, "aborted_behind", "cursor_behind", now)
    # 3. a verified span (current FY complete), not first_sync / restore_detected
    edge = await _verified_edge(session, ws, clock)
    if edge is None or sw.sync_state in BLOCKING_STATES or as_on < edge:
        return await _finish_abort(session, run, "aborted_incomplete", "no_verified_span", now)
    edge = effective_edge(edge, sw.books_from)
    run.row.verified_from = edge
    anchor_plan = plan(edge, sw.books_from)
    run.row.anchor_as_on = anchor_plan.as_on
    if body.scope == "bisect":
        return await _bisect(session, sw, run, as_on, fy_arg, edge, anchor_plan, tol, now)

    # 4. required snapshots
    snaps = await _snapshots(session, ws, [(TB, as_on), (LW, as_on), (LW, anchor_plan.as_on), (TB, anchor_plan.as_on)])
    tb_now, lw_now = snaps.get((TB, as_on)), snaps.get((LW, as_on))
    lw_anchor, tb_anchor = snaps.get((LW, anchor_plan.as_on)), snaps.get((TB, anchor_plan.as_on))
    ledger_anchor_ok = lw_anchor is not None and lw_anchor.ledgerwise
    missing: list[classify_mod.Remediation] = []
    if tb_now is None:
        missing.append(_capture(TB, as_on))
    if lw_now is None or not lw_now.ledgerwise:
        missing.append(_capture(LW, as_on))
    if not ledger_anchor_ok and tb_anchor is None:
        missing.append(_capture(LW, anchor_plan.as_on))
    if missing:
        return await _finish_abort(session, run, "aborted_incomplete", "snapshot_missing", now, missing)

    # 5. (ledgers with balance_captured_at < capture_started_at -> rung 1 missing_in_tally; see _ledgers)
    # 6. D10: recompute the imbalance from the snapshot's cells + the CURRENT masters (10c carry)
    top_groups = await snapshots_mod._top_level_group_names(session, ws) or None
    top_ledgers = await snapshots_mod._top_level_ledger_names(session, ws) or None
    imbalance = quantize(snapshots_mod.parse_cells(TB, tb_now.cells, top_groups, top_ledgers).imbalance)
    run.row.tb_imbalance = imbalance
    if imbalance is None:                                  # 10c carry: NULL = unusable TB, never `ok`
        return await _finish_abort(session, run, "aborted_incomplete", "tb_imbalance_unknown", now,
                                   [_capture(TB, as_on)])
    verdict = imbalance_moved(sw.tb_imbalance_baseline, before.alt_mst_id, imbalance, tol)
    if verdict == "stale":
        return await _finish_abort(session, run, "discarded_stale", "stale_tally", now,
                                   [classify_mod._remediation("tally_notice", {"notice": "restart"})])
    new_baseline = None
    if verdict == "rebaseline":                 # review M1: applied only in the store block, never by an abort
        new_baseline = {"alt_mst_id": before.alt_mst_id, "imbalance": str(imbalance), "recorded_at": now.isoformat()}

    # --- compute ---
    led = await _ledgers(session, ws, capture_started_at=body.capture_started_at)
    sums = await load_sums(session, ws, edge, as_on)
    day_one = None
    if anchor_plan.subtract_lines_dated is not None:
        d1 = anchor_plan.subtract_lines_dated
        day_one = {g: v[0] for g, v in (await _ranged_sums(session, ws, d1, d1)).items()}
    if ledger_anchor_ok:
        anchors, anchor_unresolved = anchor_amounts(lw_anchor.rows, led.index, day_one,
                                                    ledgerwise_flags=lw_anchor.flags)
        if _stale_after_master_change(lw_anchor, anchor_unresolved, sw.cursor_alt_mst_id):     # review I7, C1
            return await _finish_abort(session, run, "aborted_incomplete", "anchor_stale", now,
                                       [_capture(LW, anchor_plan.as_on)])
        group_anchors, anchor_rows = None, lw_anchor.rows
    else:                                                  # §10.4 fallback: the group-anchor route (10c carry)
        anchors, anchor_unresolved = None, []
        group_anchors, anchor_rows = _group_anchors(tb_anchor.rows, led.ins, day_one, led.stock_bearing), tb_anchor.rows
    unadjusted = _net_unadjusted(tb_now.rows, anchor_rows)
    ev = _evaluate(led, anchors, anchor_unresolved, sums, lw_now, tb_now, group_anchors, unadjusted, tol)

    fy_start = fy_start_of(as_on)
    stored_lw = {k[1] for k in (await _snapshots(session, ws, [(LW, d) for d in month_ends(fy_start, as_on)]))}
    ctx = classify_mod.Context(
        anchor_rows=ev.anchors, flagged_amounts=await _flagged_amounts(session, ws, edge, as_on),
        forex_guids={l.guid for l in led.ins if l.is_forex}, fy_start=fy_start, verified_edge=edge,
        month_ends_without_tb=[d for d in month_ends(fy_start, as_on) if d not in stored_lw],
        bs_guids=frozenset(l.guid for l in led.ins if l.nature in BS_NATURES and not is_pl_account(l)),
        anchor_tb_rows=resolve_rows(lw_anchor.rows, led.index)[0] if ledger_anchor_ok else {})
    lines, remediations = classify_mod.classify(ev.lines, ctx)

    had_mismatch = has_problem(lines)
    if not had_mismatch and not bs_verified(lines):
        # 10c carry: never `ok` when no balance-sheet figure was verified at either rung.
        return await _finish_abort(session, run, "aborted_incomplete", "no_balance_sheet_verified", now,
                                   [_capture(LW, anchor_plan.as_on)])

    # --- ladder (§10.8) ---
    prev = dict(sw.ladder or {})
    confirmed = await _confirmed_fy_resync(session, sw, prev)
    offer_fy = fy_start_of(_date(prev["bisect_month"])) if prev.get("bisect_month") else fy_start
    new = ladder_mod.step({**prev, "resync_offered_fy": _date(prev.get("resync_offered_fy"))},
                          had_mismatch=had_mismatch, remediation_done=list(body.remediation_done),
                          issued_ids=[r.id for r in remediations], confirmed_fy_resync_completed=confirmed,
                          fy_for_offer=offer_fy)
    ladder = {**prev, **new, "resync_offered_fy": _iso(new["resync_offered_fy"]), "last_run_id": str(run.row.id),
              "last_run_at": now.isoformat()}
    if not had_mismatch:
        ladder.pop("bisect_month", None)
    offer = ladder.get("resync_offered")
    if ladder["resync_offered_fy"]:
        if not offer or offer.get("reason") == "parity":                   # a restore/relink offer wins
            ladder["resync_offered"] = {"scope": "fy", "fy_start": ladder["resync_offered_fy"], "reason": "parity"}
    elif offer and offer.get("reason") == "parity":
        ladder.pop("resync_offered")
    status = new["state"]
    if status == "hard_alert" and prev.get("state") != "hard_alert":
        run.events.append({"workspace_id": str(ws), "run_id": str(run.row.id), "event": "engineering_flag",
                           "reason": "hard_alert"})

    # --- store (one transaction: run, lines, last_parity, ladder) ---
    problems = [l for l in lines if l.verdict in PROBLEM_VERDICTS]
    diffs = [l.diff for l in problems if l.diff is not None]
    run.row.status, run.row.finished_at = status, now
    run.row.lines_compared, run.row.mismatch_count = len(lines), len(problems)
    run.row.max_abs_diff = max((abs(d) for d in diffs), default=None)
    run.row.net_diff = sum(diffs, ZERO) if diffs else None
    run.row.forex_unrealised_total = quantize(ev.unrealised_total)
    run.row.remediation = [_rem_json(r) for r in remediations]
    session.add(run.row)
    await session.flush()
    if lines:
        await session.execute(insert(ParityLine), [_line_row(ws, run.row.id, as_on, l) for l in lines])
    sw.ladder = ladder
    if new_baseline is not None:
        sw.tb_imbalance_baseline = new_baseline
    sw.last_parity = last_parity_view(sw.last_parity, status, now, as_on, edge, len(problems), str(run.row.id))
    sw.updated_at = now
    await session.flush()
    if status != "ok":
        run.events.append(opsignal.integrity_event(str(ws), str(run.row.id), 2, status, lines))
    return {"parity_run_id": str(run.row.id), "status": status, "abort_reason": None,
            "summary": summary(lines, ev.unrealised_total), "remediation": [_rem_json(r) for r in remediations],
            "ladder": _ladder_view(ladder)}


async def _confirmed_fy_resync(session: AsyncSession, sw: SyncWorkspace, prev: dict) -> bool:
    """§10.8: a confirmed single-FY ``full_resync`` of the OFFERED FY completed since the last ladder run."""
    offered = prev.get("resync_offered_fy")
    if not offered:
        return False
    q = select(SyncRun.scope, SyncRun.finished_at).where(
        SyncRun.workspace_id == sw.workspace_id, SyncRun.kind == "full_resync", SyncRun.status == "completed",
        SyncRun.command_id.is_not(None))
    since = datetime.fromisoformat(prev["last_run_at"]) if prev.get("last_run_at") else None
    for scope, finished in (await session.execute(q)).all():
        if (scope or {}).get("fy_start") == offered and (since is None or (finished and finished > since)):
            return True
    return False


async def _bisect(session, sw, run: _Run, as_on: date, fy: date, edge: date, anchor_plan: AnchorPlan, tol: Decimal,
                  now: datetime) -> dict:
    """§10.9: rungs 1-2 at every month-end of ``fy`` (from E, up to as_on) with a ledger-level TB, oldest first; the
    first diverging month comes back as ``refetch_month``. Missing month-ends -> ``aborted_incomplete`` with the
    ``capture_snapshot`` list."""
    ws = sw.workspace_id
    # review M2a: E may be a mid-FY books_from (effective_edge) -- the FY is verified if it is E's FY or later, and
    # its months start at E (nothing precedes books_from).
    if fy < fy_start_of(edge):
        return await _finish_abort(session, run, "aborted_incomplete", "no_verified_span", now)
    ends = month_ends(max(fy, edge), min(fy_end_of(fy), as_on))
    keys = [(LW, anchor_plan.as_on), (TB, anchor_plan.as_on), *[(LW, d) for d in ends], *[(TB, d) for d in ends]]
    snaps = await _snapshots(session, ws, keys)
    lw_anchor = snaps.get((LW, anchor_plan.as_on))
    missing = [] if lw_anchor is not None and lw_anchor.ledgerwise else [_capture(LW, anchor_plan.as_on)]
    missing += [_capture(LW, d) for d in ends if not (snaps.get((LW, d)) and snaps[(LW, d)].ledgerwise)]
    if missing:
        return await _finish_abort(session, run, "aborted_incomplete", "month_ends_missing", now, missing)

    led = await _ledgers(session, ws, capture_started_at=None)
    day_one = None
    if anchor_plan.subtract_lines_dated is not None:
        d1 = anchor_plan.subtract_lines_dated
        day_one = {g: v[0] for g, v in (await _ranged_sums(session, ws, d1, d1)).items()}
    anchors, anchor_unresolved = anchor_amounts(lw_anchor.rows, led.index, day_one, ledgerwise_flags=lw_anchor.flags)
    stale = [anchor_plan.as_on] if _stale_after_master_change(lw_anchor, anchor_unresolved,
                                                              sw.cursor_alt_mst_id) else []
    for me in ends:                                   # review I7: every stored month-end TB is reused the same way
        if _stale_after_master_change(snaps[(LW, me)], resolve_rows(snaps[(LW, me)].rows, led.index)[1],
                                      sw.cursor_alt_mst_id):
            stale.append(me)
    if stale:
        return await _finish_abort(session, run, "aborted_incomplete", "anchor_stale", now,
                                   [_capture(LW, d) for d in stale])
    evaluations: list[tuple[date, bool]] = []
    all_lines: list[tuple[date, Line]] = []
    for me in ends:
        lw = snaps[(LW, me)]
        ev = _evaluate(led, anchors, anchor_unresolved, await load_sums(session, ws, edge, me), lw,
                       snaps.get((TB, me)), None, _net_unadjusted(lw.rows, lw_anchor.rows), tol)
        evaluations.append((me, has_problem(ev.lines)))
        all_lines += [(me, l) for l in ev.lines]
    first = bisect_mod.first_diverging_month(evaluations)
    remediations = [] if first is None else [classify_mod._remediation("refetch_month", {"month": first.strftime("%Y-%m")})]
    status = "ok" if first is None else "suspect"

    problems = [l for _, l in all_lines if l.verdict in PROBLEM_VERDICTS]
    run.row.status, run.row.finished_at = status, now
    run.row.lines_compared, run.row.mismatch_count = len(all_lines), len(problems)
    diffs = [l.diff for l in problems if l.diff is not None]
    run.row.max_abs_diff = max((abs(d) for d in diffs), default=None)
    run.row.net_diff = sum(diffs, ZERO) if diffs else None
    run.row.remediation = [_rem_json(r) for r in remediations]
    session.add(run.row)
    await session.flush()
    if all_lines:
        await session.execute(insert(ParityLine), [_line_row(ws, run.row.id, me, l) for me, l in all_lines])
    if first is not None:
        sw.ladder = {**(sw.ladder or {}), "bisect_month": first.isoformat()}
        sw.updated_at = now
    await session.flush()
    if status != "ok":
        run.events.append(opsignal.integrity_event(str(ws), str(run.row.id), 2, status, [l for _, l in all_lines]))
    return {"parity_run_id": str(run.row.id), "status": status, "abort_reason": None,
            "bisect": {"fy_start": fy.isoformat(), "months_evaluated": [d.isoformat() for d in ends],
                       "first_diverging_month": _iso(first)},
            "summary": None, "remediation": [_rem_json(r) for r in remediations],
            "ladder": _ladder_view(sw.ladder or {})}


__all__ = ["run_parity", "load_sums", "imbalance_moved", "effective_edge", "month_ends", "SUMS_SQL"]
