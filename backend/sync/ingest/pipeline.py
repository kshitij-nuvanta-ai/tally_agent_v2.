"""`POST /api/sync/{ws}/batches` ingest pipeline (S1 spec §7.9, §8.5, §8.6, §11, §12 all 15 steps, D12, D14, D15,
D23; pre-flight F10, F14, F17; plan A7, A8).

One DB transaction per batch. Any rejection rolls the whole batch back; then a second, short transaction records
only the `sync_batches` row as `rejected` (with the 422 body, so a same-body replay is idempotent). Bodies and
business values are never logged (decision 14).
"""
from __future__ import annotations

import hashlib
import json
import uuid
import zlib
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime

from fastapi import Request
from sqlalchemy import func, literal_column, select, text, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.sync.clock import Clock, fy_start_of, ist_date
from backend.config import Settings
from backend.sync.errors import ApiError
from backend.sync.ingest import store
from backend.sync.ingest.derive import is_base_currency, is_forex_ledger
from backend.sync.ingest.parsed import ObjectError, ObjectWarning, PBalance, PMaster, PVoucher
from backend.sync.ingest.resolve import NameIndex, ResolveError, guid_prefix_warning, order_objects
from backend.sync.ingest.store import MASTER_MODELS, ResolvedVoucher
from backend.sync.ingest.validate import parse_objects
from backend.db.sync_models import AgentDevice, SyncBatch, SyncQuarantine, SyncRun, SyncWorkspace
from backend.sync.parity import opsignal
from backend.sync import state
from backend.sync.runs import require_open_run
from contract.models import DETERMINISTIC_CODES, BatchRequest, QuarantineEntry
from contract.parse import WireParseError, name as parse_name, tally_date
from contract.tally_rules import PRIMARY_PARENT

# D12: only deterministic codes may be quarantined -- §11's set plus Task 8a's four (the same bytes never parse
# on a retry: controller rulings, Task 8a review rounds 1-2).
QUARANTINABLE_CODES: frozenset[str] = DETERMINISTIC_CODES | frozenset(
    {"invalid_counter", "invalid_captured_at", "invalid_field_type", "unexpected_parse_error"})
_CHUNK = 64 * 1024
_PARENT_KIND = {"group": "group", "stock_group": "stock_group", "ledger": "group", "stock_item": "stock_group"}


# --- step 1: limits ------------------------------------------------------------------------------------------------


def _too_large() -> ApiError:
    return ApiError(413, "payload_too_large")


async def read_body(request: Request, settings: Settings) -> dict:
    """§12 step 1. Streams the request: the on-the-wire body may not pass `ingest_max_gzip_bytes`; a gzip body is
    inflated in 64 KiB steps with a running total that aborts as soon as it passes `ingest_max_decompressed_bytes`
    (the zip-bomb guard never inflates past the cap); then the JSON is parsed and the object count checked. No
    `Content-Encoding` -> plain JSON under the same limits."""
    gzipped = request.headers.get("content-encoding", "").strip().lower() == "gzip"
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS) if gzipped else None
    received = 0
    out = bytearray()

    def take(data: bytes) -> None:
        if len(out) + len(data) > settings.INGEST_MAX_DECOMPRESSED_BYTES:
            raise _too_large()
        out.extend(data)

    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > settings.INGEST_MAX_GZIP_BYTES:
                raise _too_large()
            if inflater is None:
                take(chunk)
                continue
            data = chunk
            while data:
                take(inflater.decompress(data, _CHUNK))
                data = inflater.unconsumed_tail
        if inflater is not None:
            take(inflater.flush())
    except zlib.error:
        raise ApiError(422, "invalid_body", "gzip") from None
    try:
        body = json.loads(bytes(out))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(422, "invalid_body", "json") from None
    if not isinstance(body, dict):
        raise ApiError(422, "invalid_body", "json")
    objects = body.get("objects")
    if isinstance(objects, list) and len(objects) > settings.INGEST_MAX_OBJECTS:
        raise _too_large()
    return body


def body_sha256(body: dict) -> str:
    """The idempotency fingerprint of a batch: SHA-256 of its canonical JSON (key order and gzip level never make
    the same batch look different)."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


# --- step 2: idempotency claim -------------------------------------------------------------------------------------


@dataclass
class _Claim:
    row_id: uuid.UUID | None = None
    replay_status: int | None = None
    replay: dict | None = None


async def _existing_run_id(session: AsyncSession, ws_id: uuid.UUID, run_id: str) -> uuid.UUID:
    """The claim row carries a FK to `sync_runs`, so an unknown run is refused (404) before the claim."""
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise ApiError(404, "run_not_found") from None
    found = (await session.execute(select(SyncRun.id).where(SyncRun.id == rid, SyncRun.workspace_id == ws_id))).first()
    if found is None:
        raise ApiError(404, "run_not_found")
    return rid


async def _claim_batch_id(session: AsyncSession, ws_id: uuid.UUID, run_id: uuid.UUID, body: BatchRequest,
                          raw_sha256: str, clock: Clock) -> _Claim:
    """Review Focus 5: `INSERT … ON CONFLICT (workspace_id, batch_id) DO NOTHING` a claim row BEFORE any data write,
    inside the batch transaction. A concurrent identical batch blocks on the unique index until the winner
    commits, then finds the committed row and replays it. F10: replay binds only ACCEPTED batches -- a `rejected`
    row replays its stored 422 for the same body and is taken over (a new attempt) by a different body."""
    t = SyncBatch.__table__
    claimed = (await session.execute(
        pg_insert(t).values(workspace_id=ws_id, run_id=run_id, batch_id=body.batch_id, request_sha256=raw_sha256,
                            object_count=len(body.objects), status="claimed", received_at=clock.now())
        .on_conflict_do_nothing(index_elements=["workspace_id", "batch_id"]).returning(t.c.id))).first()
    if claimed is not None:
        return _Claim(row_id=claimed[0])
    row = (await session.execute(select(t.c.id, t.c.status, t.c.request_sha256, t.c.response).where(
        t.c.workspace_id == ws_id, t.c.batch_id == body.batch_id).with_for_update())).first()
    if row is None:                                   # the conflicting claim was rolled back meanwhile: claim again
        return await _claim_batch_id(session, ws_id, run_id, body, raw_sha256, clock)
    if row.status == "accepted":
        if row.request_sha256 == raw_sha256:
            return _Claim(replay_status=200, replay={**(row.response or {}), "replayed": True})
        raise ApiError(409, "batch_id_reused")
    if row.request_sha256 == raw_sha256 and _all_deterministic(row.response):
        return _Claim(replay_status=422, replay=row.response or {})   # deterministic 422, same body: idempotent
    # A different body -- or the same body after a rejection holding any RETRYABLE code (`missing_master` /
    # `ambiguous_master`: the agent sends the masters, then resends the batch as-is) -- is a new attempt on this
    # batch_id (fix round 1 / review I1, controller ruling: replaying such a 422 would loop forever).
    await session.execute(update(t).where(t.c.id == row.id).values(
        run_id=run_id, request_sha256=raw_sha256, object_count=len(body.objects), status="claimed",
        response=None, received_at=clock.now()))
    return _Claim(row_id=row.id)


_REPLAYABLE_REJECTION_CODES = QUARANTINABLE_CODES | {"quarantine_code_not_allowed"}


def _all_deterministic(response: dict | None) -> bool:
    """True when every per-object code of a stored 422 is deterministic -- the same bytes can never do better, so
    the stored rejection is replayed. Any retryable code (or an unreadable stored body) means re-evaluate."""
    objects = (response or {}).get("objects") or []
    return bool(objects) and all(o.get("code") in _REPLAYABLE_REJECTION_CODES for o in objects)


async def _finish_claim(session: AsyncSession, claim: _Claim, status: str, object_count: int, response: dict) -> None:
    t = SyncBatch.__table__
    await session.execute(update(t).where(t.c.id == claim.row_id).values(status=status, object_count=object_count,
                                                                         response=response))


async def _reject(session: AsyncSession, ws_id: uuid.UUID, run_id: uuid.UUID, body: BatchRequest, raw_sha256: str,
                  errors: list[ObjectError], clock: Clock) -> tuple[int, dict]:
    """Roll the batch back entirely, then record ONLY the `sync_batches` row as `rejected` in a second, short
    transaction (never over an `accepted` row a concurrent request may have committed meanwhile)."""
    response = {"error": "batch_rejected", "detail": "", "batch_id": body.batch_id,
                "objects": [{"index": e.index, "kind": e.kind, "guid": e.guid, "code": e.code, "detail": e.detail}
                            for e in errors]}
    await session.rollback()
    t = SyncBatch.__table__
    stmt = pg_insert(t).values(workspace_id=ws_id, run_id=run_id, batch_id=body.batch_id, request_sha256=raw_sha256,
                               object_count=len(body.objects), status="rejected", response=response,
                               received_at=clock.now())
    stmt = stmt.on_conflict_do_update(
        index_elements=["workspace_id", "batch_id"],
        set_={"run_id": stmt.excluded.run_id, "request_sha256": stmt.excluded.request_sha256,
              "object_count": stmt.excluded.object_count, "status": "rejected", "response": stmt.excluded.response,
              "received_at": stmt.excluded.received_at},
        where=t.c.status != "accepted")
    await session.execute(stmt)
    await session.commit()
    return 422, response


# --- A7: quarantine entries ------------------------------------------------------------------------------------------


def _quarantine_date(entry: QuarantineEntry) -> date | None:
    if not entry.voucher_date:
        return None
    try:
        return date.fromisoformat(entry.voucher_date)
    except ValueError:
        return tally_date(entry.voucher_date)          # raises WireParseError("invalid_date")


def _check_quarantine(entries: list[QuarantineEntry]) -> list[ObjectError]:
    errors: list[ObjectError] = []
    for i, entry in enumerate(entries):
        if entry.code not in QUARANTINABLE_CODES:
            errors.append(ObjectError(i, entry.kind, entry.guid, "quarantine_code_not_allowed", "quarantine"))
            continue
        try:
            _quarantine_date(entry)
        except WireParseError:
            errors.append(ObjectError(i, entry.kind, entry.guid, "invalid_date", "quarantine.voucher_date"))
    return errors


async def _record_quarantine(session: AsyncSession, sw: SyncWorkspace, entries: list[QuarantineEntry],
                             stored_guids: set[str], settings: Settings, clock: Clock) -> None:
    """§12 step 13 / D12: record each quarantined object (one open row per `(kind, guid)`), resolve every open row
    whose GUID this batch stored, recount, and move a first sync to `error` past the threshold (§8.2, A8)."""
    now = clock.now()
    t = SyncQuarantine.__table__
    for entry in entries:
        if entry.guid in stored_guids:
            continue
        stmt = pg_insert(t).values(workspace_id=sw.workspace_id, kind=entry.kind, guid=entry.guid, code=entry.code,
                                   voucher_date=_quarantine_date(entry), first_seen_at=now, last_seen_at=now,
                                   times_seen=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=["workspace_id", "kind", "guid"], index_where=text("resolved_at IS NULL"),
            set_={"code": stmt.excluded.code, "voucher_date": stmt.excluded.voucher_date, "last_seen_at": now,
                  "times_seen": t.c.times_seen + 1})
        await session.execute(stmt)
    if stored_guids:
        await session.execute(update(t).where(t.c.workspace_id == sw.workspace_id, t.c.resolved_at.is_(None),
                                              t.c.guid.in_(stored_guids)).values(resolved_at=now))
    open_count = (await session.execute(select(func.count()).select_from(t).where(
        t.c.workspace_id == sw.workspace_id, t.c.resolved_at.is_(None)))).scalar_one()
    sw.quarantine_count = open_count
    if open_count > settings.QUARANTINE_ERROR_THRESHOLD and (sw.sync_state, "fatal") in state.TRANSITIONS:
        state.transition(sw, "fatal")


# --- step 8: resolution ----------------------------------------------------------------------------------------------


async def _load_name_index(session: AsyncSession, ws_id: uuid.UUID) -> tuple[NameIndex, dict[str, set[str]]]:
    """One name -> GUID map per kind, loaded once per batch (§12 performance note), plus the live GUIDs per kind."""
    parts = [select(literal_column(f"'{kind}'").label("kind"), m.guid, m.name, m.is_deleted).where(m.workspace_id == ws_id)
             for kind, m in MASTER_MODELS.items()]
    rows = (await session.execute(union_all(*parts))).all()
    guids: dict[str, set[str]] = {kind: set() for kind in MASTER_MODELS}
    for kind, guid, _name, deleted in rows:
        if not deleted:
            guids[kind].add(guid)
    return NameIndex.from_rows(rows), guids


def _extra_checks(ordered: list) -> list[ObjectError]:
    """Deterministic shapes 8a leaves to the store: a voucher MASTERID must be a counter, a master `captured_at`
    must be ISO, a bill must carry an amount (its column is NOT NULL and never invented as 0)."""
    errors: list[ObjectError] = []
    for obj in ordered:
        if isinstance(obj, PVoucher):
            try:
                int(obj.master_id.strip())
            except (AttributeError, ValueError):
                errors.append(ObjectError(obj.index, "voucher", obj.guid, "invalid_counter", "masterid"))
            if any(b.amount is None for line in obj.lines for b in line.bills):
                errors.append(ObjectError(obj.index, "voucher", obj.guid, "missing_field", "bill_allocations.amount"))
        elif isinstance(obj, PMaster) and "captured_at" in obj.fields:
            try:
                datetime.fromisoformat(obj.fields["captured_at"])
            except (TypeError, ValueError):
                errors.append(ObjectError(obj.index, obj.kind, obj.guid, "invalid_captured_at", "captured_at"))
    return errors


def _resolve_all(ordered: list, index: NameIndex, guids: dict[str, set[str]]) -> tuple[dict, list[ObjectError],
                                                                                         list[ObjectWarning]]:
    """§12 step 8 (D13). Every master in the batch is added to the index first (a voucher may reference a ledger
    defined later in the same batch, step 6; a group may name a parent defined later in its own kind). Then each
    name resolves per kind among live masters. Carries from Task 8b: (a) a cleaned `Primary` parent is checked
    BEFORE the index -> `parent_guid = NULL` (never `missing_master`), (b) a voucher type whose parent is itself
    resolves to its own GUID."""
    for obj in ordered:
        if isinstance(obj, PMaster):
            index.add(obj.kind, obj.guid, obj.name)
            guids[obj.kind].add(obj.guid)

    resolved: dict[int, object] = {}
    errors: list[ObjectError] = []
    warnings: list[ObjectWarning] = []

    def lookup(obj, kind: str, name: str, field: str, kind_label: str) -> str | None:
        try:
            return index.resolve(kind, name)
        except ResolveError as exc:
            errors.append(ObjectError(obj.index, kind_label, obj.guid, exc.code, field))
            return None

    for obj in ordered:
        if isinstance(obj, PMaster):
            derived: dict = {}
            if obj.kind in _PARENT_KIND:
                key = "group_guid" if obj.kind == "ledger" else "parent_guid"
                parent = obj.parent or ""
                derived[key] = None if parent in ("", PRIMARY_PARENT) else \
                    lookup(obj, _PARENT_KIND[obj.kind], parent, "parent", obj.kind)
            if obj.kind == "voucher_type":
                derived["parent_guid"] = obj.guid if (obj.parent or "") == obj.name else \
                    lookup(obj, "voucher_type", obj.parent or "", "parent", obj.kind)
            if obj.kind == "stock_item":
                unit = (obj.fields.get("baseunits") or "").strip()
                derived["base_unit_guid"] = lookup(obj, "unit", unit, "baseunits", obj.kind) if unit else None
            resolved[obj.index] = derived
        elif isinstance(obj, PBalance):
            kind = "ledger" if obj.kind == "ledger_balance" else "stock_item"
            if obj.guid not in guids[kind]:
                errors.append(ObjectError(obj.index, obj.kind, obj.guid, "missing_master", "guid"))
        elif isinstance(obj, PVoucher):
            vt = lookup(obj, "voucher_type", obj.voucher_type_name, "vouchertypename", "voucher")
            party = lookup(obj, "ledger", obj.party_ledger_name, "partyledgername", "voucher") \
                if obj.party_ledger_name else None
            lines = [lookup(obj, "ledger", line.ledger_name, "ledger_entries.ledgername", "voucher")
                     for line in obj.lines]
            for line, guid in zip(obj.lines, lines):
                if line.ledger_guid_hint and guid and line.ledger_guid_hint != guid:
                    warnings.append(ObjectWarning(obj.index, "ledger_guid_mismatch"))      # step 14 cross-check
                    break
            inventory = [lookup(obj, "stock_item", inv.stock_item_name, "inventory_entries.stockitemname", "voucher")
                         for inv in obj.inventory]
            resolved[obj.index] = ResolvedVoucher(vt, party, lines, inventory)  # type: ignore[arg-type]
    return resolved, _dedupe(errors), warnings


def _dedupe(errors: list[ObjectError]) -> list[ObjectError]:
    seen, out = set(), []
    for e in errors:
        key = (e.index, e.code, e.detail)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


# --- steps 9-12: store ------------------------------------------------------------------------------------------------


async def _base_currency_name(session: AsyncSession, sw: SyncWorkspace) -> str | None:
    """D30: the base currency is the live currency master whose ExpandedSymbol is INR -- its NAME (`?`, LESSONS
    rule 28c) is what ledgers' CurrencyName carries. Falls back to the bind's `base_currency_name`."""
    from backend.db.sync_models import TallyCurrency

    name = (await session.execute(select(TallyCurrency.name).where(
        TallyCurrency.workspace_id == sw.workspace_id, TallyCurrency.is_base.is_(True),
        TallyCurrency.is_deleted.is_(False)).limit(1))).scalar_one_or_none()
    return name if name is not None else sw.base_currency_name


def _ledger_has_expression(p: PMaster) -> bool:
    return any(getattr(p.fields.get(k), "fx_amount", None) is not None for k in ("openingbalance", "closingbalance"))


@dataclass(frozen=True)
class ResyncAuthority:
    """Task 14b C2 + fix round 1 (controller rulings): inside a CONFIRMED ``full_resync`` Tally is authoritative
    (restore / relink / an FY the user chose to re-read), so the §12 step 9 / 12 alter_id rule is suspended --
    decided PER OBJECT and bounded by the run's own scope:

    - whole-company scope: every master and every voucher replaces the stored row even at a lower ``alter_id``;
    - single-FY scope: masters keep the rule (``skipped_older``); a voucher is authoritative only when its date
      lies in the run's FY (``fy_start_of(date) == scope.fy_start``);
    - every other run kind: nothing is authoritative.

    Opening any ``full_resync`` already requires the user's ``confirm_resync`` command (D16,
    ``runs._require_confirmed_resync_command``); the ``command_id`` check below is defensive only."""
    masters: bool = False
    all_vouchers: bool = False
    fy_start: date | None = None

    @classmethod
    def of(cls, run: SyncRun) -> "ResyncAuthority":
        if run.kind != "full_resync" or run.command_id is None:
            return cls()
        scope = run.scope or {}
        if scope.get("company"):
            return cls(masters=True, all_vouchers=True)
        try:
            return cls(fy_start=date.fromisoformat(scope.get("fy_start") or ""))
        except ValueError:
            return cls()

    def voucher(self, v: PVoucher) -> bool:
        return self.all_vouchers or (self.fy_start is not None and fy_start_of(v.date) == self.fy_start)


async def _store_all(session: AsyncSession, sw: SyncWorkspace, run: SyncRun, ordered: list, resolved: dict,
                     clock: Clock) -> tuple[Counter, list[tuple[str, str]]]:
    ws_id = sw.workspace_id
    authority = ResyncAuthority.of(run)                                # C2: alter_id rule suspended per object
    counts: Counter = Counter()
    base_name: str | None = None
    groups_touched = vts_touched = False
    derivation_warnings: list[tuple[str, str]] = []

    masters = [o for o in ordered if isinstance(o, PMaster)]
    for p in masters:                                                   # step 9, in step-6 order
        derived = dict(resolved[p.index])
        if p.kind == "ledger":
            if base_name is None:
                base_name = await _base_currency_name(session, sw)
            currency = parse_name(p.fields["currencyname"]) if p.fields.get("currencyname") else None
            derived["is_forex"] = is_forex_ledger(currency, base_name, _ledger_has_expression(p))
        result = await store.upsert_master(session, ws_id, p, derived, authoritative=authority.masters)
        counts[result] += 1
        if p.kind == "currency" and is_base_currency(p.fields.get("expandedsymbol") or "") and result != \
                "skipped_older":
            base_name = p.name
            if sw.base_currency_name != p.name:
                sw.base_currency_name = p.name
        groups_touched |= p.kind == "group" and result != "skipped_older"
        vts_touched |= p.kind == "voucher_type" and result != "skipped_older"
        if p.kind in ("ledger", "stock_item") and store._balance_view(p) is not None:     # step 11 (master form)
            counts["balances_" + await store.apply_balance(session, ws_id, p)] += 1

    if groups_touched:                                                  # step 10
        derivation_warnings += await store.rederive_groups(session, ws_id)
    if vts_touched:
        derivation_warnings += await store.rederive_voucher_types(session, ws_id)

    for b in (o for o in ordered if isinstance(o, PBalance)):           # step 11
        counts["balances_" + await store.apply_balance(session, ws_id, b)] += 1

    vouchers = [o for o in ordered if isinstance(o, PVoucher)]          # step 12
    if vouchers:
        raw_fys = await store.raw_window_fys(session, ws_id, ist_date(clock.now()))
        items = [(v, resolved[v.index], fy_start_of(v.date) in raw_fys) for v in vouchers]
        base_types = await store.voucher_type_base_types(session, ws_id, {r.voucher_type_guid for _, r, _ in items})
        for result in await store.upsert_vouchers(session, ws_id, items, run.id, base_types,
                                                  authoritative=[authority.voucher(v) for v in vouchers]):
            counts[result] += 1
    return counts, derivation_warnings


# --- step 15: last_synced_at (§8.5) ------------------------------------------------------------------------------------


def _moves_last_synced(run: SyncRun, body: BatchRequest, has_masters: bool, window_start: date) -> bool:
    """§8.5 / §15.3: `first_sync` and `incremental` batches move it; `backfill` never; a `full_resync` batch only
    when its chunk lies inside the current 2-FY window, or (whole-company scope) when it carries masters."""
    if run.kind in ("first_sync", "incremental"):
        return True
    if run.kind != "full_resync":
        return False
    if body.chunk is not None and body.chunk.from_ >= window_start:
        return True
    return has_masters and bool((run.scope or {}).get("company"))


# --- the pipeline ---------------------------------------------------------------------------------------------------


async def ingest_batch(session: AsyncSession, sw: SyncWorkspace, device: AgentDevice, body: BatchRequest,
                       raw_sha256: str, settings: Settings, clock: Clock) -> tuple[int, dict]:
    ws_id = sw.workspace_id
    run_id = await _existing_run_id(session, ws_id, body.run_id)
    claim = await _claim_batch_id(session, ws_id, run_id, body, raw_sha256, clock)          # step 2
    if claim.replay is not None:
        await session.rollback()
        return claim.replay_status, claim.replay                                           # type: ignore[return-value]
    if body.company_guid != sw.tally_company_guid:                                          # step 3 (R2)
        raise ApiError(409, "company_mismatch")
    run = await require_open_run(session, sw, device, run_id)                               # step 4
    if sw.sync_state == "restore_detected" and run.kind == "incremental":                   # §8.6
        raise ApiError(409, "restore_detected")

    errors = _check_quarantine(body.quarantine)                                            # A7
    parsed, parse_errors, warnings = parse_objects([o.model_dump() for o in body.objects])  # steps 5 + 7
    errors += parse_errors
    ordered = order_objects(parsed)                                                        # step 6
    errors += _extra_checks(ordered)
    index, guids = await _load_name_index(session, ws_id)
    resolved, res_errors, res_warnings = _resolve_all(ordered, index, guids)               # step 8
    errors += res_errors
    if errors:
        return await _reject(session, ws_id, run_id, body, raw_sha256, errors, clock)

    counts, derivation_warnings = await _store_all(session, sw, run, ordered, resolved, clock)   # steps 9-12
    stored_guids = {o.guid for o in ordered}
    await _record_quarantine(session, sw, body.quarantine, stored_guids, settings, clock)    # step 13

    by_guid = {o.guid: o.index for o in ordered}                                           # step 14
    warnings += res_warnings
    warnings += [ObjectWarning(o.index, "guid_prefix_foreign") for o in ordered
                 if guid_prefix_warning(sw.tally_company_guid, o.guid)]
    warnings += [ObjectWarning(by_guid[g], code) for g, code in derivation_warnings if g in by_guid]
    warnings.sort(key=lambda w: (w.index, w.code))

    now = clock.now()                                                                      # step 15
    window_start = min(await store.raw_window_fys(session, ws_id, ist_date(now)))
    if _moves_last_synced(run, body, any(isinstance(o, PMaster) for o in ordered), window_start):
        sw.last_synced_at = now
    sw.updated_at = now
    response = {"batch_id": body.batch_id, "status": "accepted", "replayed": False,
                "counts": {k: counts.get(k, 0) for k in ("inserted", "updated", "skipped_older", "balances_applied",
                                                        "balances_stale")},
                "warnings": [asdict(w) for w in warnings], "reread_ledgers": []}
    await _finish_claim(session, claim, "accepted", len(body.objects), response)
    await session.commit()
    # D12 (10b/10c carry): an ACCEPTED batch that quarantined objects -> one counts-by-code ops event, after the
    # commit (a rejected batch returned above and never gets here; a replay returns before step 2). Codes and
    # counts only -- decision 14: no names, GUIDs, amounts or narration.
    quarantined = Counter(e.code for e in body.quarantine if e.guid not in stored_guids)
    if quarantined:
        opsignal.emit(opsignal.quarantine_event(str(ws_id), dict(sorted(quarantined.items()))))
    return 200, response
