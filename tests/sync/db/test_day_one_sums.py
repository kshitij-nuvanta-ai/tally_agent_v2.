"""The D9 books-start anchor's day-one subtraction is the same figure in-memory and in the DB (Task 12 fix round 1,
controller ruling): ``realdata.pure_parity`` subtracts ``build_sums`` over the countable line facts dated books_from;
the engine subtracts ``_ranged_sums(session, ws, d1, d1)`` (the covering-index SQL over ``countable`` lines). Both are
fed the same real company B vouchers dated 01-04-2022, plus one CANCELLED and one OPTIONAL voucher dated that day --
neither may count on either side."""
from __future__ import annotations

import copy
from datetime import date

from sqlalchemy import text

from backend.sync.parity.engine import _ranged_sums
from backend.sync.parity.model import build_sums
from tests.sync import parity_realdata as prd
from tests.sync import realdata
from tests.sync.conftest import requires_db
from tests.sync.db.ingest_helpers import bound, fresh, post_ok

pytestmark = requires_db

D1 = date(2022, 4, 1)


def _day_one_vouchers() -> list[dict]:
    b = realdata.assemble_b_fy2022()
    real = [v for v in b.vouchers if v["data"]["date"] == "20220401"]
    assert real, "company B has vouchers dated books_from (G6 shows day-1 activity)"
    optional = copy.deepcopy(real[0])
    optional["data"]["guid"] += "-optional"
    optional["data"]["isoptional"] = "Yes"                # lines kept: an optional voucher exports its amounts
    cancelled = copy.deepcopy(real[1])
    cancelled["data"]["guid"] += "-cancelled"
    cancelled["data"]["iscancelled"] = "Yes"
    cancelled["data"]["ledger_entries"] = []              # LESSONS rule 23: a cancelled voucher exports no amounts
    cancelled["data"]["partyledgername"] = ""
    cancelled["data"].pop("inventory_entries", None)
    return [*real, optional, cancelled]


async def test_pure_day_one_sum_equals_engine_ranged_sums_incl_cancelled_and_optional(app_client, session, engine):
    vouchers = _day_one_vouchers()
    masters = realdata.b_masters()
    ws, headers, run_id, _ = await bound(app_client, session)
    await post_ok(app_client, ws, headers, run_id, masters)
    await post_ok(app_client, ws, headers, run_id, vouchers)

    index, _, _, _ = prd._index_and_ledgers(masters)
    facts = prd._line_facts(prd._parse_vouchers(vouchers), index)
    pure = build_sums([f for f in facts if f.voucher_date == D1], verified_edge=D1, as_on=D1).total

    async with fresh(engine) as s:
        db = {g: v[0] for g, v in (await _ranged_sums(s, ws, D1, D1)).items()}
    assert pure and db == pure

    # the optional voucher's lines really were stored (not countable), so the equality is a filter check, not luck
    opt_guid = next(v["data"]["guid"] for v in vouchers if v["data"].get("isoptional") == "Yes")
    async with fresh(engine) as s:
        flags = (await s.execute(text(
            "SELECT count(*) FILTER (WHERE NOT l.countable), count(*) FROM tally_voucher_ledger_lines l "
            "JOIN tally_vouchers v ON v.id = l.voucher_id WHERE v.workspace_id = :w AND v.guid = :g"),
            {"w": ws, "g": opt_guid})).one()
    assert flags[0] == flags[1] > 0
    with_optional = build_sums([f for f in prd._line_facts(prd._parse_vouchers(
        [dict(v, data={**v["data"], "isoptional": "No"}) for v in vouchers]), index) if f.voucher_date == D1],
        verified_edge=D1, as_on=D1).total
    assert with_optional != pure                         # counting the optional voucher WOULD change the sum
