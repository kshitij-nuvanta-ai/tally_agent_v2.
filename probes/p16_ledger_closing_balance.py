"""Probe 16 — does LEDGER.ClosingBalance hold Tally's own ledger balance? (S0 spec §7 "Probe 16", A part)

Feeds decision 11, R30 and Part 2 Rule 1 (Part 1 §6 rung 1). The B part is `run_b` (plan part 5).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from agent.tally.envelopes import wrap_report
from agent.tally.reports import parse_ledger_list
from probes.actions import Action
from probes.anchors import SEED_PAYABLE, SEED_RECEIVABLE
from probes.companies import THROWAWAY_DATE, THROWAWAY_DATE_TEXT, THROWAWAY_EXPENSE_LEDGER
from probes.company_b_view import (B_BOOKS_FROM_DATE, B_CURRENT_PERIOD, ledger_balances_at, ledger_openings_at,
                                      loaded_licence)
from probes.context import ProbeContext
from probes.core import Outcome, PartResult, Probe, ProbeBlocked
from probes.p01_company_counters import LAST_VOUCHER_DATE_BASELINE
from probes.reads import (A_FY_FROM, A_FY_TO, OPENING_STOCK_ROW, PL_PRIMARY_GROUPS, TB_EXPLODE_VARS, ZERO,
                             ancestors, dmy, exploded_tb_rows, ledger_movements, master_request, opening_stock_row,
                             parse_parents, parse_vouchers, postings, primary_group_rows, signed_ui_amount,
                             stock_bearing_groups, top_group, voucher_request)

LEDGER_FIELDS = ["Name", "Parent", "GUID", "OpeningBalance", "ClosingBalance"]
VOUCHER_FIELDS = ["Date", "VoucherTypeName", "MasterId", "Narration", "IsCancelled", "IsOptional", "IsPostDated",
                  "AllLedgerEntries", "LedgerEntries", "AllInventoryEntries"]
THROWAWAY_FIELDS = ["Date", "MasterId", "Narration", "IsPostDated"]
UI_LEDGERS = ("Apex Technologies Pvt Ltd", "HP India Sales Pvt Ltd", "HDFC Bank - Current A/c")
AS_ON_FROM, AS_ON_TO = "01-10-2025", "31-10-2025"
SVTODATE_STEP = "ledgers_asof_2025-10-31"
SVFROMDATE_STEP = "ledgers_svfromdate_2025-10-01"
SVFROMDATE_TIMEOUT_S = 20.0
RISKY_HINT = "`run 16 --company A --rerun --allow-risky`"
SVFROMDATE_SKIPPED = ("not sent: SVFROMDATE on a Ledger collection froze Tally's XML server behind a modal (live "
                      "2026-09-23, twice — sent untyped, before C33 was known; the typed form has not been "
                      f"re-measured) and needed a Tally restart. Opt in with {RISKY_HINT}.")
WEDGE_SUMMARY = ("the opt-in SVFROMDATE ledger read left Tally unresponsive — restart Tally before anything else "
                 "(this reproduces the 2026-09-23 wedge; it is the finding, not a harness fault)")
WEDGE_IMPACT = ("SVFROMDATE on a Ledger collection freezes Tally's XML server: the agent never sends a period "
                "variable on a master collection, and per-ledger opening anchors during the backfill depend on "
                "probe 17's exploded ledger-level TB (decision 11, Part 1 §16).")
AS_ON_IMPACT = ("Per-ledger as-on balances are unobtainable from the Ledger collection (a typed SVTODATE is silently "
                "ignored; SVFROMDATE froze Tally when sent untyped on 2026-09-23), so rung 1's per-ledger opening "
                "anchor during the backfill depends on probe 17's exploded ledger-level TB (decision 11) — that route "
                "is impossible, not merely unattractive. Reports (TYPE=Data) are unaffected.")
CASH = "Cash"
FUTURE_REF, POST_DATED_REF = "p16-future", "p16-post-dated"
FUTURE_NARRATION = "S0-throwaway 16 future"
POST_DATED_NARRATION = "S0-throwaway 16 post-dated"
# The throwaway "future" voucher is dated 31-03-2026 — company A's period end. It can only separate "as of the period
# end" from "as of Tally's current (F2) date" while the F2 date is BEFORE it; books that already reach that date mean
# Tally opened the company on it, so both readings would include the voucher and the step decides nothing.
FUTURE_CUTOFF = THROWAWAY_DATE
RERUN_HINT = ("run `uv run python -m probes reset-a`, then re-run probe 1 and probe 16 on the fresh "
              "copy (`run 1 --company A --rerun`, then `run 16 --company A --rerun`)")
FAILED_IMPACT = ("LEDGER.ClosingBalance isn't Tally's ledger balance: rung 1 collapses to group level, month-bisect "
                 "becomes the main localiser, and Part 2 Rule 1 falls back to group-level forward computation "
                 "(decision 11, Part 1 §16).")


def _ledgers_request(company: str, static_vars: dict[str, str] | None = None, *, name: str = "S0P16Ledgers") -> str:
    """The Ledger collection. Probe 16 is the one deliberate exception to the master-collection period-variable guard
    in reads.master_request: it is the probe that measures what those variables do (reads.MASTER_PERIOD_VARS_ERROR)."""
    return master_request(name, "Ledger", LEDGER_FIELDS, company, static_vars=static_vars,
                          allow_period_vars=static_vars is not None)


def _balances(text: str) -> dict[str, dict]:
    return {row["name"]: row for row in parse_ledger_list(text)}


def _value(value: Decimal | None) -> Decimal:
    return value if value is not None else ZERO


def _kinds(ledgers: dict[str, dict], groups: dict[str, str]) -> dict[str, str]:
    """ledger → 'bs' | 'pl' | 'special' (the Profit & Loss A/c sits directly under Primary)."""
    kinds: dict[str, str] = {}
    for name, row in ledgers.items():
        parent = row["parent_group"]
        if parent in ("", "Primary"):
            kinds[name] = "special"
        else:
            kinds[name] = "pl" if top_group(parent, groups) in PL_PRIMARY_GROUPS else "bs"
    return kinds


def _nominal(ledgers: dict[str, dict], kinds: dict[str, str]) -> dict:
    zero, empty, nonzero = [], [], {}
    for name, kind in sorted(kinds.items()):
        if kind != "pl":
            continue
        closing = ledgers[name]["closing_balance"]
        if closing is None:
            empty.append(name)
        elif closing == ZERO:
            zero.append(name)
        else:
            nonzero[name] = closing
    return {"zero": zero, "empty": empty, "nonzero": nonzero}


def _lines_check(ledgers: dict[str, dict], kinds: dict[str, str],
                 movements: dict[str, Decimal]) -> tuple[dict[str, dict], list[str]]:
    """Balance-sheet ledgers whose ClosingBalance ≠ OpeningBalance + Σ lines; and those exported empty for zero."""
    bad: dict[str, dict] = {}
    empty_for_zero: list[str] = []
    for name, kind in sorted(kinds.items()):
        if kind != "bs":
            continue
        closing = ledgers[name]["closing_balance"]
        computed = _value(ledgers[name]["opening_balance"]) + movements.get(name, ZERO)
        if closing is None and computed == ZERO:
            empty_for_zero.append(name)
        elif closing != computed:
            bad[name] = {"closing": closing, "computed": computed}
    return bad, empty_for_zero


def _group_compare(ledgers: dict[str, dict], kinds: dict[str, str], groups: dict[str, str],
                   tb_rows: dict[str, dict], opening_stock: Decimal | None) -> dict[str, dict]:
    """TB group row vs the rollup of its balance-sheet ledgers, with the stock-bearing group's known gap allowed for.

    That gap is the TB's OWN synthetic `Opening Stock` row, read from the very same exploded TB response (live
    2026-09-23, company A: Current Assets row 26,05,093 = ledger rollup 7,49,293 + Opening Stock 18,55,800, exactly).
    It is deliberately NOT the Stock Summary's closing total: that is a different quantity (-9,89,462.31 the same
    day), it comes from a second response that can drift from this one, and comparing against it left a genuine
    reconciliation looking like a mismatch.
    """
    rollups: dict[str, Decimal] = {}
    for name, kind in kinds.items():
        if kind == "bs":
            top = top_group(ledgers[name]["parent_group"], groups)
            rollups[top] = rollups.get(top, ZERO) + _value(ledgers[name]["closing_balance"])
    stock_groups = stock_bearing_groups(groups)
    result: dict[str, dict] = {}
    for group in sorted(set(rollups) | {g for g in tb_rows if g not in PL_PRIMARY_GROUPS}):
        row = tb_rows.get(group)
        tb, rollup = row["closing_balance"] if row else None, rollups.get(group, ZERO)
        entry: dict = {"tb": tb, "rollup": rollup, "match": _value(tb) == rollup}
        if not entry["match"] and group in stock_groups and tb is not None:
            entry["opening_stock"] = opening_stock
            entry["stock_gap"] = tb - rollup
            entry["stock_row_debit"] = row.get("debit_amount") if row else None
            entry["stock_row_credit"] = row.get("credit_amount") if row else None
            entry["explained_by_stock"] = opening_stock is not None and tb - rollup == opening_stock
        entry["reconciled"] = entry["match"] or bool(entry.get("explained_by_stock"))
        result[group] = entry
    return result


def _party_totals(ledgers: dict[str, dict], groups: dict[str, str]) -> tuple[Decimal, Decimal]:
    debtors = creditors = ZERO
    for row in ledgers.values():
        if row["parent_group"] in ("", "Primary"):
            continue
        chain = ancestors(row["parent_group"], groups)
        if "Sundry Debtors" in chain:
            debtors += _value(row["closing_balance"])
        elif "Sundry Creditors" in chain:
            creditors += _value(row["closing_balance"])
    return debtors, creditors


def _ui_compare(ctx: ProbeContext, ledgers: dict[str, dict]) -> dict[str, dict]:
    ui: dict[str, dict] = {}
    for name in UI_LEDGERS:
        typed = ctx.ask(f"In Tally, open ledger {name!r} (Display More Reports → Account Books → Ledger) and type the "
                        f"closing balance it shows, e.g. '62,800.00 Dr' — or press Enter to skip:",
                        Action("ui_closing_balance", {"ledger": name}))
        value = signed_ui_amount(typed)
        closing = ledgers.get(name, {}).get("closing_balance")
        ui[name] = {"typed": typed, "ui": value, "closing": closing,
                    "match": None if value is None else value == closing}
    return ui


async def _as_on(ctx: ProbeContext, ledgers: dict[str, dict], kinds: dict[str, str], vouchers: list[dict]) -> dict:
    """The safe half of the as-on question: the control read (already taken) vs an SVTODATE-only read.

    Compared per ledger BY VALUE, never by byte count: live 2026-09-23 the SVTODATE response was byte-identical to
    the control and 0 of 35 closing balances had moved — a byte-count (or "it answered 200") check would have
    produced a confident WRONG verdict. SVFROMDATE is the dangerous half and lives in _svfromdate_attempt.
    """
    up_to = ledger_movements(vouchers, up_to=dmy(AS_ON_TO))
    as_of = _balances(await ctx.send(SVTODATE_STEP, _ledgers_request(ctx.company_name, {"SVTODATE": AS_ON_TO})))
    bs = sorted(name for name, kind in kinds.items() if kind == "bs")

    def opening(name: str) -> Decimal:
        return _value(ledgers[name]["opening_balance"])

    changed = [n for n in bs if as_of.get(n, {}).get("closing_balance") != ledgers[n]["closing_balance"]]
    closing_ok = all(_value(as_of.get(n, {}).get("closing_balance")) == opening(n) + up_to.get(n, ZERO) for n in bs)
    return {"closing_follows_svtodate": closing_ok and bool(changed), "closing_matches_lines": closing_ok,
            "closing_changed": bool(changed), "ledgers_compared": len(bs), "closing_changed_count": len(changed),
            "closing_changed_ledgers": changed[:10]}


async def _svfromdate_attempt(ctx: ProbeContext, step: str = SVFROMDATE_STEP,
                              static_vars: dict[str, str] | None = None,
                              collection: str = "S0P16Ledgers") -> tuple[dict, dict[str, dict]]:
    """(record, parsed ledgers) — the dangerous half: SVFROMDATE on a Ledger collection, OFF unless --allow-risky.

    Live 2026-09-23 (twice) this wedged Tally: the read timed out and an unrelated counters read then timed out too,
    i.e. the whole XML server was blocked behind a modal until Tally was restarted. So it runs last (everything else
    is already recorded), with a short timeout, and checks afterwards whether Tally survived — the pattern probe 17
    uses for its exploded-TB candidates.
    """
    if not ctx.allow_risky:
        return {"attempted": False, "note": SVFROMDATE_SKIPPED}, {}
    variables = static_vars or {"SVFROMDATE": AS_ON_FROM, "SVTODATE": AS_ON_TO}
    text, error = await ctx.try_send(step, _ledgers_request(ctx.company_name, variables, name=collection),
                                     timeout=SVFROMDATE_TIMEOUT_S)
    if error is None:
        rows = _balances(text)
        return ({"attempted": True, "error": None, "wedged": False, "ledgers": len(rows),
                 "elapsed_ms": ctx.last_response.elapsed_ms, "tally_after": "answered (the read didn't hang)"}, rows)
    try:
        names = await ctx.company_names()
    except ProbeBlocked as exc:
        return {"attempted": True, "error": error, "wedged": True, "tally_after": f"no answer: {exc}"}, {}
    return {"attempted": True, "error": error, "wedged": False, "tally_after": f"answered: {names}"}, {}


def _as_on_note(as_on: dict, svfromdate: dict) -> str:
    """What the run PROVED about as-on reading — never a sentence about a variable it didn't send."""
    counts = (f"SVTODATE moved {as_on['closing_changed_count']} of {as_on['ledgers_compared']} balance-sheet closing "
              f"balances")
    if svfromdate.get("attempted"):
        tail = (" SVFROMDATE was sent (opt-in) and "
                + ("WEDGED Tally — restart it." if svfromdate.get("wedged") else f"{svfromdate['tally_after']}."))
    else:
        tail = f" SVFROMDATE {SVFROMDATE_SKIPPED}"
    if as_on["closing_follows_svtodate"]:
        return (f"As-on closing works (ClosingBalance follows SVTODATE; {counts}): per-ledger anchors can use the "
                f"as-on closing of the day before the window (decision 11).{tail}")
    return (f"As-on reading doesn't work on the Ledger collection — SVTODATE is silently ignored ({counts}), so "
            f"ClosingBalance can only ever mean now: per-ledger opening anchors during the backfill depend on probe "
            f"17's exploded ledger-level TB (decision 11).{tail}")


async def _throwaway(ctx: ProbeContext, *, ref: str, narration: str, post_dated: bool, step: str, ledger_step: str,
                     cash_before: Decimal | None) -> dict:
    company = ctx.company_name
    note = f"Delete the voucher with narration {narration!r} if it still exists"
    ctx.on_abort(note)
    kind = "marked post-dated (Ctrl+T on the voucher screen)" if post_dated else "an ordinary (not post-dated) voucher"
    ctx.pause(f"Create a Payment voucher dated {THROWAWAY_DATE_TEXT}, {kind}: Cash → {THROWAWAY_EXPENSE_LEDGER}, ₹1, "
              f"narration {narration!r}. Save it. (company: {company!r})",
              Action("create_voucher", {"company": company, "ref": ref, "ledger": THROWAWAY_EXPENSE_LEDGER,
                                        "amount": "1.00", "narration": narration, "post_dated": post_dated}))
    found = [v for v in parse_vouchers(await ctx.send(step, voucher_request("S0P16Throwaway", THROWAWAY_FIELDS, company)))
             if v["header"].get("NARRATION") == narration]
    after = _balances(await ctx.send(ledger_step, _ledgers_request(company)))
    cash_after = after.get(CASH, {}).get("closing_balance")
    ctx.pause(f"Delete the voucher with narration {narration!r} (open it, Alt+D). (company: {company!r})",
              Action("delete_voucher", {"company": company, "ref": ref}))
    ctx.resolve_abort(note)
    return {"found": bool(found), "is_post_dated_exported": found[0]["header"].get("ISPOSTDATED", "") if found else "",
            "cash_before": cash_before, "cash_after": cash_after, "included_in_closing": cash_after != cash_before}


def books_reach_before_run(ctx: ProbeContext, own_reading: str) -> dict:
    """How far company A's books reached BEFORE this run wrote anything — the F2-date discriminator.

    Tally fixes its current (F2) date when the company opens, so books that stopped short of 31-03-2026 mean the F2
    date is short of it too, and the throwaway voucher really is future data. Probe 16's OWN LastVoucherDate reading
    can't say that: probe 1 runs immediately before it (probe 16 needs the counters request probe 1 confirms) and its
    own 31-03-2026 throwaway leaves LastVoucherDate at 20260331 for good — it does not roll back when the voucher is
    deleted (probe 1, live 2026-09-22). Probe 1's recorded baseline is taken before that voucher, so it is the reading
    to use; probe 16's own reading is only a fallback, and being at or after the true reach it can make this check
    stricter, never looser.
    """
    part = (ctx.store.probe_entry(1) or {}).get("parts", {}).get("A") or {}
    recorded = (part.get("observations") or {}).get(LAST_VOUCHER_DATE_BASELINE)
    if isinstance(recorded, dict):
        value = recorded.get("value") or ""
        if value and recorded.get("available"):
            return {"value": value, "available": True, "source": "probe 1's baseline, before its throwaway voucher"}
        return {"value": "", "available": False,
                "source": "not available: probe 1 recorded no LastVoucherDate for this company"}
    if own_reading:
        return {"value": own_reading, "available": True,
                "source": "probe 16's own reading — probe 1 recorded no baseline, so this may already include this "
                          "run's throwaway vouchers"}
    return {"value": "", "available": False, "source": "not available: LastVoucherDate isn't exported"}


def as_of_verdict(future: dict, reach: dict) -> tuple[str, str]:
    """(verdict, sentence) for "ClosingBalance is as of …". Never decides what the run can't tell apart."""
    if not future["included_in_closing"]:
        # Sound whatever the F2 date is: a balance as of the period end would contain a voucher dated on it.
        return "current_date", (f"Tally's current date (F2), not the period end — the throwaway voucher dated "
                                f"{THROWAWAY_DATE_TEXT}, the period end itself, stayed out of ClosingBalance")
    if reach["available"] and reach["value"] < FUTURE_CUTOFF:
        return "period_end", (f"the period end ({A_FY_TO}), not Tally's current date — the books reached only "
                              f"{reach['value']} before this run, so Tally's current date is before "
                              f"{THROWAWAY_DATE_TEXT} and the throwaway voucher was future data it still counted")
    detail = (f"the books already reached {reach['value']} before this run ({reach['source']})"
              if reach["available"] else f"the pre-run LastVoucherDate is {reach['source']}")
    return "inconclusive", (f"inconclusive: the throwaway voucher dated {THROWAWAY_DATE_TEXT} is in ClosingBalance, but "
                            f"{detail}, so Tally's current date may be on that date too and the two readings can't be "
                            f"told apart. To settle it, {RERUN_HINT}")


def _rule(future: dict, post_dated: dict, as_of: str) -> str:
    if not post_dated["found"]:
        pd = "the post-dated voucher wasn't found after the create, so the post-dated rule is still open"
    elif post_dated["is_post_dated_exported"] != "Yes":
        pd = (f"IsPostDated exported {post_dated['is_post_dated_exported'] or '(empty)'!r}, not 'Yes': the flag didn't "
              "stick, so the post-dated rule is still open")
    else:
        pd = f"post-dated vouchers are {'included' if post_dated['included_in_closing'] else 'excluded'}; IsPostDated exported 'Yes'"
    return f"Rung 1 rule (Part 1 §6): ClosingBalance is as of {as_of}; {pd}."


async def run_a(ctx: ProbeContext) -> PartResult:
    company = ctx.company_name
    last_voucher_date = (await ctx.counters()).get("LastVoucherDate", "")
    ctx.observe("last_voucher_date", last_voucher_date)
    ledgers = _balances(await ctx.send("ledgers", _ledgers_request(company)))
    if not ledgers:
        return PartResult(Outcome.BLOCKED, "The Ledger collection came back empty — is company A open?")
    groups = parse_parents(await ctx.send("groups", master_request("S0P16Groups", "Group", ["Name", "Parent"], company)))
    vouchers = parse_vouchers(await ctx.send("vouchers_fy", voucher_request("S0P16Vouchers", VOUCHER_FIELDS, company)))
    # One exploded TB, not a TB plus a separate Stock Summary: the group rows AND the `Opening Stock` row that
    # explains the stock-bearing group's gap then come from the same response and cannot drift apart.
    tb_all_rows = exploded_tb_rows(await ctx.send(
        "tb_fy_end", wrap_report("Trial Balance", A_FY_FROM, A_FY_TO, company, extra_vars=TB_EXPLODE_VARS)))
    tb_rows = primary_group_rows(tb_all_rows)
    stock_row = opening_stock_row(tb_all_rows)
    opening_stock = stock_row["closing_balance"] if stock_row else None

    kinds = _kinds(ledgers, groups)
    nominal = _nominal(ledgers, kinds)
    bad_lines, empty_for_zero = _lines_check(ledgers, kinds, ledger_movements(vouchers))
    # postings() defaults to reads.PROBE_POSTING_RULE ('all_only'). Under the old 'default' rule this listed all 24
    # inventory vouchers, an artifact of the nominal ledger being exported twice -- not genuinely unbalanced books.
    unbalanced_vouchers = [v for v in vouchers if sum((amt for _, amt in postings(v) if amt is not None), ZERO) != ZERO]
    group_cmp = _group_compare(ledgers, kinds, groups, tb_rows, opening_stock)
    debtors, creditors = _party_totals(ledgers, groups)
    ctx.observe("ledger_kinds", {k: sum(1 for v in kinds.values() if v == k) for k in ("bs", "pl", "special")})
    ctx.observe("nominal", nominal)
    ctx.observe("closing_vs_lines", {"mismatches": bad_lines, "empty_closing_for_zero": empty_for_zero,
                                     "vouchers": len(vouchers)})
    ctx.observe("unbalanced_vouchers", [v["header"].get("MASTERID", "") for v in unbalanced_vouchers])
    ctx.observe("tb_vs_rollup", group_cmp)
    ctx.observe("tb_opening_stock_row", {"present": stock_row is not None, "closing_balance": opening_stock,
                                         "tb_rows": len(tb_all_rows), "primary_group_rows": len(tb_rows)})
    ctx.observe("party_totals", {"debtors": debtors, "creditors": creditors})
    ctx.observe("special_ledgers", {n: ledgers[n]["closing_balance"] for n, k in kinds.items() if k == "special"})

    ui = _ui_compare(ctx, ledgers)
    ctx.observe("ui_compare", ui)
    as_on = await _as_on(ctx, ledgers, kinds, vouchers)
    ctx.observe("as_on", as_on)
    cash_before = ledgers.get(CASH, {}).get("closing_balance")
    future = await _throwaway(ctx, ref=FUTURE_REF, narration=FUTURE_NARRATION, post_dated=False, step="future_voucher",
                              ledger_step="ledgers_with_future_voucher", cash_before=cash_before)
    post_dated = await _throwaway(ctx, ref=POST_DATED_REF, narration=POST_DATED_NARRATION, post_dated=True,
                                  step="post_dated_voucher", ledger_step="ledgers_with_post_dated",
                                  cash_before=cash_before)
    ctx.observe("future_voucher", future)
    ctx.observe("post_dated_voucher", post_dated)
    reach = books_reach_before_run(ctx, last_voucher_date)
    verdict, as_of = as_of_verdict(future, reach)
    ctx.observe("as_of_basis", {"verdict": verdict, "books_reach_before_run": reach["value"],
                                "available": reach["available"], "source": reach["source"]})
    svfromdate, _ = await _svfromdate_attempt(ctx)       # last: a wedge can't cost the checks already recorded
    ctx.observe("as_on_svfromdate", svfromdate)
    rule, as_on_note = _rule(future, post_dated, as_of), _as_on_note(as_on, svfromdate)
    if svfromdate.get("wedged"):
        return PartResult(Outcome.FAILED, WEDGE_SUMMARY, spec_impact=f"{WEDGE_IMPACT} {rule} {as_on_note}")

    failures: list[str] = []
    if bad_lines and not unbalanced_vouchers:
        failures.append(f"ClosingBalance ≠ opening + Σ lines for {len(bad_lines)} balance-sheet ledger(s) "
                        f"({', '.join(sorted(bad_lines)[:5])})")
    if -debtors != SEED_RECEIVABLE:
        failures.append(f"Σ debtors {-debtors} ≠ the Bills Receivable anchor {SEED_RECEIVABLE}")
    if creditors != SEED_PAYABLE:
        failures.append(f"Σ creditors {creditors} ≠ the Bills Payable anchor {SEED_PAYABLE}")
    ui_bad = sorted(name for name, entry in ui.items() if entry["match"] is False)
    if ui_bad:
        failures.append(f"the Tally UI shows another balance for {', '.join(ui_bad)}")
    if failures:
        return PartResult(Outcome.FAILED, "; ".join(failures), spec_impact=f"{FAILED_IMPACT} {rule} {as_on_note}")

    differences: list[str] = []
    impacts: list[str] = []
    notes: list[str] = []
    if bad_lines and unbalanced_vouchers:
        differences.append(f"{len(unbalanced_vouchers)} voucher(s) don't balance under postings(), so the lines check "
                           f"for {len(bad_lines)} balance-sheet ledger(s) is inconclusive")
        impacts.append("posting rule unverified (probe 6 decides); lines check inconclusive")
    if not as_on["closing_follows_svtodate"]:
        differences.append(f"per-ledger as-on balances can't be read from the Ledger collection: SVTODATE changed "
                           f"{as_on['closing_changed_count']} of {as_on['ledgers_compared']} closing balances")
        impacts.append(AS_ON_IMPACT)
    if nominal["nonzero"]:
        differences.append(f"{len(nominal['nonzero'])} nominal ledger(s) have a non-zero ClosingBalance")
        impacts.append("Rung 1 picks balance-sheet ledgers by group nature, never by 'ClosingBalance = 0' (Part 1 §6).")
    by_stock = sorted(group for group, entry in group_cmp.items() if entry.get("explained_by_stock"))
    unreconciled = sorted(group for group, entry in group_cmp.items() if not entry["reconciled"])
    if by_stock:
        # Reconciled, not a mismatch: the gap IS a TB row, so rung 2 gets a rule rather than an open question.
        notes.append(f"Rung 2 compares the stock-bearing group as ledger rollup + the TB's own {OPENING_STOCK_ROW!r} "
                     f"row, which no ledger carries and which is NOT the Stock Summary closing total "
                     f"({', '.join(by_stock)}; Part 1 §6).")
    if unreconciled:
        differences.append(f"TB row(s) {', '.join(unreconciled)} ≠ ledger rollup")
        impacts.append("Rung 2's group rollup doesn't reproduce these TB rows; the recorded per-group figures decide the "
                       "comparison rule before S1 (Part 1 §6).")
    if differences:
        return PartResult(Outcome.DIFFERENT, "; ".join(differences),
                          spec_impact=" ".join([*impacts, *notes, rule, as_on_note]))
    ui_note = "" if any(entry["match"] is not None for entry in ui.values()) else \
        " (UI balances not read — automated or skipped; a later manual check)"
    stock_note = f" (+ the TB's own {OPENING_STOCK_ROW} row on {', '.join(by_stock)})" if by_stock else ""
    return PartResult(Outcome.CONFIRMED, "ClosingBalance = opening + Σ lines for every balance-sheet ledger; debtors and "
                                         f"creditors match the anchors; TB rows = ledger rollup{stock_note}" + ui_note,
                      spec_impact=" ".join([*notes, rule, as_on_note]))


B_LEDGERS = "S0P16BLedgers"
B_FY2024_START = date(2024, 4, 1)
B_FY2024_FROM, B_FY2024_TO = "01-04-2024", "31-03-2025"     # C43: days 1 and 31
B_ASON_2023 = "31-03-2023"                                    # FY 2022-23, the closed first year
SCOPE_IMPACT = {
    "books": ("Ledger OpeningBalance is the books-start opening whatever the period (R5): S1 stores it once per ledger "
              "as the books-start opening, and FY openings are computed from lines."),
    "fy": ("Ledger OpeningBalance is the opening of the FY holding the read's period (R5): a per-FY opening anchor is "
           "readable, the books-start one only with the period in the first FY."),
}
NEITHER_IMPACT = ("Ledger OpeningBalance is neither the books-start nor the FY opening: R5's opening anchor can't come "
                  "from the Ledger collection; S1 takes openings from probe 17's ledger-level TB instead.")
AS_ON_WRONG_IMPACT = ("A dated Ledger-collection read returns neither the as-on balance nor today's: the agent never "
                      "sends a period variable on a master collection (LESSONS §15 rule 17 stands), and per-ledger "
                      "anchors come from probe 17's ledger-level TB (decision 11).")
AS_ON_WORKS_IMPACT = ("A typed SVTODATE on the Ledger collection gives true as-on closing balances on company B, in a "
                      "closed year and in an older FY: per-ledger opening anchors exist during the backfill without "
                      "probe 17 (decision 11, Part 1 §16).")


def opening_scope(opening: dict[str, Decimal | None], books: dict[str, Decimal], fy: dict[str, Decimal]) -> dict:
    """Books- or FY-scoped, judged on the ledgers whose books-start and FY openings differ ("telling"); a ledger whose
    two openings agree must still match them."""
    telling = sorted(n for n in opening if books[n] != fy[n])
    as_books = [n for n in telling if _value(opening[n]) == books[n]]
    as_fy = [n for n in telling if _value(opening[n]) == fy[n]]
    same_bad = sorted(n for n in opening if books[n] == fy[n] and _value(opening[n]) != books[n])
    if not telling:
        verdict = "undecided"
    elif len(as_books) == len(telling) and not same_bad:
        verdict = "books"
    elif len(as_fy) == len(telling) and not same_bad:
        verdict = "fy"
    else:
        verdict = "neither"
    mismatched = sorted(set(telling) - set(as_books) - set(as_fy)) + same_bad
    return {"verdict": verdict, "telling": len(telling), "as_books": len(as_books), "as_fy": len(as_fy),
            "mismatched": mismatched[:10]}


def compare_closing(read: dict[str, dict], names: list[str], want: dict[str, Decimal]) -> dict:
    bad = {n: {"tally": read.get(n, {}).get("closing_balance"), "dataset": want[n]}
           for n in names if _value(read.get(n, {}).get("closing_balance")) != want[n]}
    return {"compared": len(names), "mismatches": bad}


def as_on_state(read: dict[str, dict], control: dict[str, dict], names: list[str], want: dict[str, Decimal]) -> dict:
    """works = every balance-sheet closing equals the dataset as-on; ignored = nothing moved from the control read;
    wrong = moved, but not to the as-on value."""
    mismatches = compare_closing(read, names, want)["mismatches"]
    moved = sum(1 for n in names if read.get(n, {}).get("closing_balance") != control[n]["closing_balance"])
    state = "works" if not mismatches else ("ignored" if moved == 0 else "wrong")
    return {"state": state, "compared": len(names), "moved": moved, "mismatches": dict(list(mismatches.items())[:10])}


def judged_scope(fy2024: dict, fy2024_scope: dict, now_scope: dict) -> dict:
    """Spec §7 probe 16 B (Ruling Q9): the scope is judged from the typed read INSIDE FY 2024-25 when Tally honoured
    its period. The current-FY no-variable read is supporting evidence; it stands in only when the FY 2024-25 read's
    period was not honoured (the spec's read is then impossible on this Tally, and the verdict says so)."""
    if fy2024["state"] == "works":
        judged = {**fy2024_scope, "source": "fy2024_read"}
    else:
        judged = {**now_scope, "source": "current_fy_read",
                  "why": f"the FY 2024-25 read's period was not honoured (SVTODATE {fy2024['state']})"}
    judged["supporting"] = {"fy2024_read": fy2024_scope["verdict"], "current_fy_read": now_scope["verdict"]}
    judged["reads_agree"] = fy2024_scope["verdict"] == now_scope["verdict"]
    return judged


async def _b_read(ctx: ProbeContext, step: str, to_date: str) -> dict[str, dict]:
    return _balances(await ctx.send(step, _ledgers_request(ctx.company_name, {"SVTODATE": to_date}, name=B_LEDGERS)))


async def run_b(ctx: ProbeContext) -> PartResult:
    """Spec §7 probe 16 B. The scope question is answered from the typed read with its period inside FY 2024-25
    (SVTODATE 31-03-2025), against the dataset's FY 2024-25 and books-start openings (Ruling Q9: the spec's read). The
    no-variable read (current FY 2025-26, not the books start, so books- and FY-scoped openings differ on every active
    ledger) is kept as supporting evidence and stands in only when Tally ignored the FY 2024-25 period (`judged_scope`).
    The as-on reads send a typed SVTODATE only (SVFROMDATE on a Ledger collection is opt-in, LESSONS §15 rule 17)."""
    licence = loaded_licence(ctx.store.environment)
    company = ctx.company_name
    groups = parse_parents(await ctx.send("groups", master_request("S0P16BGroups", "Group", ["Name", "Parent"], company)))
    control = _balances(await ctx.send("ledgers", _ledgers_request(company, name=B_LEDGERS)))
    if not control:
        return PartResult(Outcome.BLOCKED, "The Ledger collection came back empty — is company B open?")
    books = ledger_openings_at(licence, B_BOOKS_FROM_DATE)
    kinds = _kinds(control, groups)
    drift = sorted({n for n, k in kinds.items() if k != "special"} ^ set(books))
    if drift:
        raise ProbeBlocked(f"Company B's ledgers differ from the dataset ({', '.join(drift[:5])}) — re-run `setup-b` "
                           "verify or restore the backup before trusting this probe.")
    bs = sorted(n for n, k in kinds.items() if k == "bs")
    now_scope = opening_scope({n: control[n]["opening_balance"] for n in bs}, books,
                              ledger_openings_at(licence, B_CURRENT_PERIOD[0]))
    closing_now = compare_closing(control, bs, ledger_balances_at(licence, B_CURRENT_PERIOD[1]))
    fy_read = await _b_read(ctx, "ledgers_fy2024", B_FY2024_TO)
    fy2024 = as_on_state(fy_read, control, bs, ledger_balances_at(licence, dmy(B_FY2024_TO)))
    fy2024_scope = opening_scope({n: fy_read.get(n, {}).get("opening_balance") for n in bs}, books,
                                 ledger_openings_at(licence, B_FY2024_START))
    scope = judged_scope(fy2024, fy2024_scope, now_scope)
    y2023 = as_on_state(await _b_read(ctx, "ledgers_asof_2023-03-31", B_ASON_2023), control, bs,
                        ledger_balances_at(licence, dmy(B_ASON_2023)))
    ctx.observe("ledger_kinds", {k: sum(1 for v in kinds.values() if v == k) for k in ("bs", "pl", "special")})
    ctx.observe("opening_scope", scope)
    ctx.observe("closing_now", closing_now)
    ctx.observe("as_on_fy2024", fy2024)
    ctx.observe("as_on_2023", y2023)
    svfromdate, rows = await _svfromdate_attempt(ctx, "ledgers_fy2024_svfromdate",
                                                 {"SVFROMDATE": B_FY2024_FROM, "SVTODATE": B_FY2024_TO}, B_LEDGERS)
    if rows:
        svfromdate["opening_scope"] = opening_scope({n: rows.get(n, {}).get("opening_balance") for n in bs}, books,
                                                    ledger_openings_at(licence, B_FY2024_START))
    ctx.observe("as_on_svfromdate", svfromdate)
    if svfromdate.get("wedged"):
        return PartResult(Outcome.FAILED, WEDGE_SUMMARY, spec_impact=WEDGE_IMPACT)
    if closing_now["mismatches"]:
        return PartResult(Outcome.FAILED, f"ClosingBalance ≠ the dataset for {len(closing_now['mismatches'])} "
                                          f"balance-sheet ledger(s) ({', '.join(sorted(closing_now['mismatches'])[:5])})",
                          spec_impact=FAILED_IMPACT)
    reads = (("31-03-2025", fy2024), (B_ASON_2023, y2023))
    wrong = [day for day, state in reads if state["state"] == "wrong"]
    if wrong:
        return PartResult(Outcome.FAILED, f"SVTODATE {', '.join(wrong)} moved closing balances, but not to the as-on "
                                          "values", spec_impact=AS_ON_WRONG_IMPACT)
    differences, impacts = [], []
    if scope["verdict"] in ("neither", "undecided"):
        differences.append(f"OpeningBalance scope is {scope['verdict']} ({scope['mismatched'][:5]})")
        impacts.append(NEITHER_IMPACT)
    elif scope["source"] == "fy2024_read" and not scope["reads_agree"]:
        differences.append(f"the OpeningBalance scope reads disagree (FY 2024-25 read: {fy2024_scope['verdict']}, "
                           f"current-FY read: {now_scope['verdict']})")
    ignored = [day for day, state in reads if state["state"] == "ignored"]
    if ignored:
        differences.append(f"a typed SVTODATE ({', '.join(ignored)}) is silently ignored on the Ledger collection")
        impacts.append(AS_ON_IMPACT)
    scope_note = SCOPE_IMPACT.get(scope["verdict"], "")
    if differences:
        return PartResult(Outcome.DIFFERENT, "; ".join(differences), spec_impact=" ".join([*impacts, scope_note]))
    return PartResult(Outcome.CONFIRMED,
                      f"OpeningBalance is {scope['verdict']}-scoped ({scope['telling']} telling ledgers, judged "
                      f"inside FY 2024-25); a typed "
                      f"SVTODATE gives the dataset's as-on closing for all {len(bs)} balance-sheet ledgers at "
                      f"{B_FY2024_TO} and {B_ASON_2023}",
                      spec_impact=f"{scope_note} {AS_ON_WORKS_IMPACT}")


PROBE = Probe(
    id=16,
    name="ledger_closing_balance",
    question="Does LEDGER.ClosingBalance equal Tally's own balances (lines, TB, UI)? Nominal = 0? As-on? As-of date and "
             "post-dated vouchers?",
    feeds=("decision 11", "R30", "Part 2 Rule 1"),
    parts={"A": run_a, "B": run_b},
    planned_parts=("A", "B"),
    requires=(0, 1, 2),
    mutating=True,
    educational_sensitive=True,
)
