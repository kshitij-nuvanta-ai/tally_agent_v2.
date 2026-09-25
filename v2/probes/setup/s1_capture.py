"""S1 build task 0: capture the S1 fixture gaps G1–G5 from live Tally — READ-ONLY (S1 spec §13.3, §5.3, §10.5).

    uv run --project v2 python -m v2.probes.setup.s1_capture --company B      # G2, G3, G4, G5
    uv run --project v2 python -m v2.probes.setup.s1_capture --company A      # G1, G4

Every capture lands in `--out` (default `v2/tests/fixtures/sync/`) as `s1_<company>_<step>.xml` (the raw response
bytes) plus `.xml.json` — the same sidecar `Capture` writes for the S0 probes, with three extra keys: `capture`
("s1_task0"), `gap` (G1…G5, or "context" for the counters read) and `observations` (what the capture shows, e.g. G2's
"is the USD ledger's TB row plain or an expression?").

Guards (a guard failure sends nothing):
- every request is built and checked BEFORE the first one is sent: Export only, never Import (`check_export_only`),
  the S0 request guard (`safety.check_request`) and C43 (`safety.check_educational_dates` — a licence not recorded in
  results.json is treated as Educational, the stricter rule);
- the open company list must be exactly the expected company, re-checked (non-mutating) before every capture and
  once after the last (`safety.check_company`);
- no period variable on a master collection (`reads.master_request` refuses them — LESSONS §15 rule 17);
- existing `s1_<company>_*` captures are never overwritten without `--overwrite`;
- a transport error saves an error sidecar and stops the run: nothing more is sent (a timeout usually means a modal).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from v2.agent.tally.amounts import AmountParseError, parse_decimal
from v2.agent.tally.client import TallyClient, TallyResponse
from v2.agent.tally.envelopes import COMPANY_PLACEHOLDER, build_company_list, esc, formula_string, wrap_report
from v2.agent.tally.exceptions import TallyConnectionError, TallyResponseError, TallyTimeoutError
from v2.agent.tally.xml_utils import detect_error, parse_company_list, read_objects, sanitize_xml
from v2.probes.capture import Capture
from v2.probes.companies import COMPANIES
from v2.probes.context import ENVIRONMENT_SIDECAR_KEYS, POPUP_HINT
from v2.probes.reads import TB_EXPLODE_VARS, master_request, parse_forex_amount, tally_date
from v2.probes.results import ResultsStore
from v2.probes.safety import GuardError, check_company, check_educational_dates, check_request
from v2.probes.setup.company_b_data import USD_DEBTOR, USD_EXPORT_PARTY

V2_ROOT = Path(__file__).resolve().parents[2]
RESULTS_PATH = V2_ROOT / "probes" / "results" / "results.json"
FIXTURES_DIR = V2_ROOT / "tests" / "fixtures" / "sync"
CAPTURE_TAG = "s1_task0"
COMPANY_KEYS = ("A", "B")

# Dates — every one on day 1 or 31 (C43: Educational Tally ignores a date variable on any other day).
A_BOOKS_FROM = "01-04-2025"                              # G1: D9's books-start anchor, as on books_from
B_TB_CURRENT = ("01-04-2025", "31-03-2026")              # G2: the current FY to its end
B_TB_PAST = ("01-04-2022", "31-03-2023")                 # G2: as on 31-03-2023 (p18_B's date, §10.5's real numbers)
B_GROUP_TB = ("01-04-2025", "31-03-2026")                # G3: the group TB as on 31-03-2026
BOOKS_FROM = {"A": "20250401", "B": "20220401"}          # what the counters read should say (spec §4 `books_from`)

UNADJUSTED_FOREX_ROW = "Unadjusted Forex Gain/Loss"
# G5: the S2 "touched ledgers" re-read. The USD party, its counter-ledger, the INR party with the same trading name,
# and Local Purchases — the Purchase Accounts ledger every inventory purchase posts to (the stock-bearing side).
TOUCHED_LEDGERS = (USD_EXPORT_PARTY, "Export Sales", USD_DEBTOR, "Local Purchases")
ID_FIELDS = ["GUID", "AlterID", "MasterID"]
TOUCHED_FIELDS = ["GUID", "AlterID", "Name", "Parent", "OpeningBalance", "ClosingBalance"]
USD_LEDGER_FIELDS = ["Name", "Parent", "CurrencyName", "OpeningBalance", "ClosingBalance", *ID_FIELDS]
# G4 (§5.3): (step, TDL type, XML tag, fields besides the IDs) — the master kinds S0 never captured GUID/AlterID for.
MASTER_KINDS = (
    ("voucher_types", "VoucherType", "VOUCHERTYPE", ["Name", "Parent", "ReservedName"]),
    ("currencies", "Currency", "CURRENCY", ["Name", "MailingName", "ExpandedSymbol", "OriginalName", "DecimalPlaces"]),
    ("stock_groups", "StockGroup", "STOCKGROUP", ["Name", "Parent"]),
    ("units", "Unit", "UNIT", ["Name", "IsSimpleUnit", "BaseUnits", "AdditionalUnits", "Conversion"]),
)

_TALLYREQUEST = re.compile(r"<TALLYREQUEST>\s*([^<]*?)\s*</TALLYREQUEST>", re.IGNORECASE)
_IMPORT_BODY = re.compile(r"<(?:IMPORTDATA|REQUESTDATA|TALLYMESSAGE)\b", re.IGNORECASE)
_TYPED_VAR = r'<{name} TYPE="Date">[^<]*</{name}>'


class CaptureAborted(Exception):
    """A request failed in transport; its error sidecar is saved and nothing more was sent."""


@dataclass(frozen=True)
class CaptureSpec:
    step: str
    gap: str
    xml: str
    observe: Callable[[str], dict[str, Any]] | None = None


# --- guards ----------------------------------------------------------------------------------------------------------
def check_export_only(xml: str) -> None:
    """Refuse anything but a single `<TALLYREQUEST>Export</TALLYREQUEST>` request with no import payload."""
    kinds = _TALLYREQUEST.findall(xml)
    if kinds != ["Export"]:
        raise GuardError(f"Request refused: TALLYREQUEST {kinds or 'missing'} — the S1 capture sends Export (read) "
                         "requests only, never Import")
    if _IMPORT_BODY.search(xml):
        raise GuardError("Request refused: an Export envelope carrying an Import payload — the S1 capture never "
                         "writes to Tally")


async def check_open_company(client: TallyClient, company: str) -> None:
    """The open company list is exactly `company` (non-mutating guard). Not captured."""
    text = (await client.post_xml(build_company_list())).text
    check_company(parse_company_list(sanitize_xml(text)), company, mutating=False)


# --- requests ------------------------------------------------------------------------------------------------------
def ledger_tb_request(template: str, company: str, from_date: str, to_date: str) -> str:
    """Probe 17's confirmed `ledger_level_tb` template with its typed dates and company substituted. The template is
    refused if it has drifted from the confirmed shape (ISLEDGERWISE, typed dates, the company placeholder, Export)."""
    check_export_only(template)
    problems = []
    if template.count("<ISLEDGERWISE>Yes</ISLEDGERWISE>") != 1:
        problems.append("no ISLEDGERWISE=Yes")
    if "EXPLODEFLAG" in template or "EXPLODEALLLEVELS" in template:
        problems.append("an EXPLODE flag (never a ledger-level TB, probe 17)")
    for name in ("SVFROMDATE", "SVTODATE"):
        if len(re.findall(_TYPED_VAR.format(name=name), template)) != 1:
            problems.append(f"no single typed {name} (C33)")
    if COMPANY_PLACEHOLDER not in template:
        problems.append(f"no {COMPANY_PLACEHOLDER}")
    if problems:
        raise GuardError(f"The confirmed ledger_level_tb template has drifted: {'; '.join(problems)}")
    xml = re.sub(_TYPED_VAR.format(name="SVFROMDATE"), f'<SVFROMDATE TYPE="Date">{esc(from_date)}</SVFROMDATE>',
                 template)
    xml = re.sub(_TYPED_VAR.format(name="SVTODATE"), f'<SVTODATE TYPE="Date">{esc(to_date)}</SVTODATE>', xml)
    return xml.replace(COMPANY_PLACEHOLDER, esc(company))


def group_tb_request(company: str, from_date: str, to_date: str) -> str:
    """The group-level TB (EXPLODEFLAG, as p16_A_tb_fy_end / p18_B_tb_asof — S1's rung-2 request)."""
    return wrap_report("Trial Balance", from_date, to_date, company, extra_vars=TB_EXPLODE_VARS)


def masters_request(kind: str, tdl_type: str, fields: list[str], company: str) -> str:
    collection = "S1Cap" + "".join(part.title() for part in kind.split("_"))
    return master_request(collection, tdl_type, [*fields, *ID_FIELDS], company)


def usd_ledger_request(company: str) -> str:
    return master_request("S1CapUsdLedger", "Ledger", USD_LEDGER_FIELDS, company,
                          filters=[("S1CapIsUsdParty", f"$Name = {formula_string(USD_EXPORT_PARTY)}")])


def touched_ledgers_request(company: str, names: tuple[str, ...] = TOUCHED_LEDGERS) -> str:
    """The S2 re-read of the ledgers a sync touched: a Ledger collection filtered to those names, no period
    variables (a master collection's balances are today's — LESSONS §15 rule 17)."""
    formula = " OR ".join(f"$Name = {formula_string(name)}" for name in names)
    return master_request("S1CapTouchedLedgers", "Ledger", TOUCHED_FIELDS, company,
                          filters=[("S1CapIsTouched", formula)])


def counters_request(template: str, company: str) -> str:
    return template.replace(COMPANY_PLACEHOLDER, esc(company))


# --- observations --------------------------------------------------------------------------------------------------
def amount_form(text: str | None) -> str:
    """'empty', 'plain' (an INR number), 'expression' (a forex `-$1609.71 @ ? 82.58/$ = -? 132929.85`) or 'other'."""
    if not (text or "").strip():
        return "empty"
    try:
        parse_decimal(text)
        return "plain"
    except AmountParseError:
        pass
    return "expression" if parse_forex_amount(text) is not None else "other"


def _tb_rows(raw: str) -> list[dict[str, str]]:
    """name, debit_text, credit_text for every DSPDISPNAME at any depth — the RAW cell text (no parsing)."""
    rows: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for element in ET.fromstring(sanitize_xml(raw)).iter():
        if element.tag == "DSPDISPNAME":
            current = {"name": (element.text or "").strip(), "debit_text": "", "credit_text": ""}
            rows.append(current)
        elif current is not None and element.tag == "DSPCLDRAMTA":
            current["debit_text"] = (element.text or "").strip()
        elif current is not None and element.tag == "DSPCLCRAMTA":
            current["credit_text"] = (element.text or "").strip()
    return rows


def tb_row(raw: str, name: str) -> dict[str, Any]:
    """The first TB row called `name`, with its raw cells and their form (§10.5: plain INR or an expression)."""
    for row in _tb_rows(raw):
        if row["name"] == name:
            cell = row["debit_text"] or row["credit_text"]
            return {"ledger": name, "found": True, "debit_text": row["debit_text"],
                    "credit_text": row["credit_text"], "form": amount_form(cell)}
    return {"ledger": name, "found": False, "debit_text": "", "credit_text": "", "form": "absent"}


def _tb_observer(*named_rows: tuple[str, str]) -> Callable[[str], dict[str, Any]]:
    def observe(raw: str) -> dict[str, Any]:
        return {"rows": len(_tb_rows(raw)), **{key: tb_row(raw, name) for key, name in named_rows}}
    return observe


def _masters_observer(tag: str, fields: list[str]) -> Callable[[str], dict[str, Any]]:
    def observe(raw: str) -> dict[str, Any]:
        rows = read_objects(raw, tag, [*fields, *ID_FIELDS])
        out: dict[str, Any] = {"rows": len(rows), "found": [r["Name"] for r in rows]}
        for field in ID_FIELDS:
            out[f"missing_{field.lower()}"] = [r["Name"] for r in rows if not r[field]]
        if tag == "LEDGER":
            out["forms"] = {r["Name"]: {"opening": amount_form(r["OpeningBalance"]),
                                        "closing": amount_form(r["ClosingBalance"])} for r in rows}
        return out
    return observe


def _touched_observer(names: tuple[str, ...]) -> Callable[[str], dict[str, Any]]:
    def observe(raw: str) -> dict[str, Any]:
        rows = read_objects(raw, "LEDGER", TOUCHED_FIELDS)
        returned = [r["Name"] for r in rows]
        return {"requested": list(names), "returned": returned,
                "missing": [n for n in names if n not in returned],
                "unexpected": [n for n in returned if n not in names],
                "missing_guid": [r["Name"] for r in rows if not r["GUID"]],
                "closing_forms": {r["Name"]: amount_form(r["ClosingBalance"]) for r in rows},
                "opening_forms": {r["Name"]: amount_form(r["OpeningBalance"]) for r in rows}}
    return observe


def _counters_observer(company: str, company_key: str, fields: list[str]) -> Callable[[str], dict[str, Any]]:
    def observe(raw: str) -> dict[str, Any]:
        rows = [r for r in read_objects(raw, "COMPANY", ["Name", *fields]) if r["Name"] == company]
        row = rows[0] if rows else {}
        books_from = row.get("BooksFrom", "")
        expected = BOOKS_FROM[company_key]
        return {"found": bool(rows), "guid": row.get("GUID") or None, "counters": row,
                "books_from": books_from, "books_from_expected": expected,
                "books_from_matches": tally_date(books_from) is not None
                and tally_date(books_from) == tally_date(expected)}
    return observe


# --- the plan ------------------------------------------------------------------------------------------------------
def plan(company_key: str, company: str, ledger_tb_template: str,
         counters: dict[str, Any] | None) -> list[CaptureSpec]:
    """Every capture for one company, in send order. Pure — nothing is sent here."""
    specs: list[CaptureSpec] = []
    if counters is not None:
        specs.append(CaptureSpec("counters", "context", counters_request(counters["xml_template"], company),
                                 _counters_observer(company, company_key, list(counters.get("fields", ["GUID"])))))
    if company_key == "A":
        specs.append(CaptureSpec("tb_ledger_asof_2025-04-01", "G1",
                                 ledger_tb_request(ledger_tb_template, company, A_BOOKS_FROM, A_BOOKS_FROM),
                                 _tb_observer(("opening_stock_row", "Opening Stock"))))
    else:
        usd = ("usd_row", USD_EXPORT_PARTY)
        specs += [
            CaptureSpec("tb_ledger_2025-04-01_2026-03-31", "G2",
                        ledger_tb_request(ledger_tb_template, company, *B_TB_CURRENT), _tb_observer(usd)),
            CaptureSpec("tb_ledger_asof_2023-03-31", "G2",
                        ledger_tb_request(ledger_tb_template, company, *B_TB_PAST), _tb_observer(usd)),
            CaptureSpec("tb_group_asof_2026-03-31", "G3", group_tb_request(company, *B_GROUP_TB),
                        _tb_observer(("unadjusted_forex_row", UNADJUSTED_FOREX_ROW), usd)),
        ]
    for kind, tdl_type, tag, fields in MASTER_KINDS:
        specs.append(CaptureSpec(kind, "G4", masters_request(kind, tdl_type, fields, company),
                                 _masters_observer(tag, fields)))
    if company_key == "B":
        specs.append(CaptureSpec("usd_ledger", "G4", usd_ledger_request(company),
                                 _masters_observer("LEDGER", [f for f in USD_LEDGER_FIELDS if f not in ID_FIELDS])))
        specs.append(CaptureSpec("ledgers_touched", "G5", touched_ledgers_request(company),
                                 _touched_observer(TOUCHED_LEDGERS)))
    return specs


def fixture_file(company_key: str, step: str) -> str:
    return f"s1_{company_key}_{step}.xml"


def _observe(spec: CaptureSpec, text: str) -> dict[str, Any]:
    error = None
    try:
        error = detect_error(text)
        return {**(spec.observe(text) if spec.observe else {}), **({"tally_error": error} if error else {})}
    except (ET.ParseError, ValueError) as exc:          # recorded, never fatal: the raw bytes are the evidence
        return {"observe_error": f"{type(exc).__name__}: {exc}", **({"tally_error": error} if error else {})}


def _summary(name: str, gap: str, observations: dict[str, Any]) -> str:
    return f"{name} [{gap}] {json.dumps(observations, ensure_ascii=False, default=str)}"


# --- run -----------------------------------------------------------------------------------------------------------
async def run(client: TallyClient, company_key: str, out_dir: Path, *, store: ResultsStore,
              say: Callable[[str], None] = print, overwrite: bool = False) -> list[str]:
    """Capture every gap for company `company_key` ("A" or "B") into `out_dir`. Returns the fixture names."""
    if company_key not in COMPANY_KEYS:
        raise GuardError(f"Company {company_key!r}: the S1 capture reads company A or B only")
    company = COMPANIES[company_key]
    licence = store.environment.get("licence") or "educational"      # unknown → the stricter C43 rule
    confirmed = store.confirmed("ledger_level_tb")
    if confirmed is None:
        raise GuardError("No confirmed ledger_level_tb request in results.json (probe 17) — nothing was sent")
    specs = plan(company_key, company, confirmed["xml_template"], store.confirmed("company_counters"))
    for spec in specs:                                   # every guard, for every request, before the first send
        check_export_only(spec.xml)
        check_request(spec.xml)
        check_educational_dates(spec.xml, licence)
    names = [fixture_file(company_key, spec.step) for spec in specs]
    if not overwrite:
        existing = [n for n in names if (out_dir / n).exists() or (out_dir / f"{n}.json").exists()]
        if existing:
            raise FileExistsError(f"{out_dir} already holds {', '.join(existing)} — pass --overwrite to replace them")

    capture = Capture(out_dir)
    env = store.environment
    environment = {k: env[k] for k in ENVIRONMENT_SIDECAR_KEYS if k in env}
    company_guid: str | None = None
    for spec, name in zip(specs, names):
        await check_open_company(client, company)
        common = dict(probe_id=None, part=company_key, step=spec.step, company_name=company,
                      company_guid=company_guid, request_xml=spec.xml, sent_at=datetime.now().astimezone(),
                      environment=environment, name=name)
        try:
            response: TallyResponse = await client.post_xml(spec.xml)
        except (TallyConnectionError, TallyResponseError) as exc:
            kind = ("timeout" if isinstance(exc, TallyTimeoutError)
                    else "refused" if isinstance(exc, TallyConnectionError) else "http")
            capture.save(**common, error={"kind": kind, "message": str(exc)},
                         extra={"capture": CAPTURE_TAG, "gap": spec.gap, "observations": {}})
            hint = f" {POPUP_HINT}" if kind == "timeout" else ""
            raise CaptureAborted(f"{name}: {kind} — {exc}.{hint} Nothing more was sent "
                                 "(a timeout usually means a popup/modal).") from exc
        observations = _observe(spec, response.text)
        if spec.step == "counters":
            company_guid = observations.get("guid")
            common["company_guid"] = company_guid
        capture.save(**common, response=response,
                     extra={"capture": CAPTURE_TAG, "gap": spec.gap, "observations": observations})
        say(_summary(name, spec.gap, observations))
    await check_open_company(client, company)            # still the same company at the end
    return names


# --- CLI -----------------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m v2.probes.setup.s1_capture",
                                     description="S1 task 0: read-only capture of fixture gaps G1–G5")
    parser.add_argument("--company", choices=COMPANY_KEYS, required=True,
                        help="A: G1 + G4; B: G2, G3, G4 (with the USD ledger), G5")
    parser.add_argument("--out", type=Path, default=FIXTURES_DIR)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--results", type=Path, default=RESULTS_PATH,
                        help="results.json holding the confirmed requests and the recorded licence (read only)")
    parser.add_argument("--overwrite", action="store_true", help="replace existing s1_<company>_* captures")
    return parser


def main(argv: list[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = ResultsStore(args.results)
    client = TallyClient(args.host, args.port, transport=transport)

    async def go() -> list[str]:
        try:
            return await run(client, args.company, args.out, store=store, overwrite=args.overwrite)
        finally:
            await client.close()

    try:
        names = asyncio.run(go())
    except (GuardError, FileExistsError) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    except CaptureAborted as exc:
        print(f"Aborted: {exc}", file=sys.stderr)
        return 1
    except (TallyConnectionError, TallyResponseError) as exc:
        print(f"Tally not reachable: {exc}", file=sys.stderr)
        return 1
    print(f"{len(names)} capture(s) for company {args.company} in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
