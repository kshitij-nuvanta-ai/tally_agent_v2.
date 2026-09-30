"""Company C's tiny, idempotent loader (S0 spec §4.4; plan part 6): one expense ledger and one Payment voucher.

The company shell is created by hand in the TallyPrime UI (company creation can't be scripted), security and TallyVault
are switched on by hand during probe 24, and no credential ever passes through here. Writes use only verified shapes
(`TallyWriter.create_ledger`, `create_payment`, docs/tally-write-exploration-v4.md) and read each one back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from probes.companies import (COMPANIES, COMPANY_C_LEDGER, COMPANY_C_LEDGER_PARENT, COMPANY_C_VOUCHER_AMOUNT,
                                 COMPANY_C_VOUCHER_DATE, COMPANY_C_VOUCHER_NARRATION)
from probes.setup.writes import TallyWriter, check_writable


class CompanyCLoadError(Exception):
    """setup-c refused or found company C in a state it won't write over."""


@dataclass
class CReport:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def load_company_c(writer: TallyWriter, company: str = COMPANIES["C"]) -> CReport:
    if company != COMPANIES["C"]:                             # review M1: setup-c writes to company C only
        raise CompanyCLoadError(f"setup-c loads only {COMPANIES['C']!r}, not {company!r}.")
    check_writable(company)                                   # before any request at all
    loaded = writer.company_names()
    if loaded != [company]:
        raise CompanyCLoadError(f"Tally has {loaded} open; open only {company!r} (K: Company → Select) and shut every "
                                "other company first.")
    report = CReport()
    ledger = writer.ledger(company, COMPANY_C_LEDGER)
    if ledger is None:
        writer.create_ledger(company, COMPANY_C_LEDGER, COMPANY_C_LEDGER_PARENT)
        report.created.append("ledger")
    elif ledger["Parent"] != COMPANY_C_LEDGER_PARENT:
        raise CompanyCLoadError(f"Ledger {COMPANY_C_LEDGER!r} exists under {ledger['Parent']!r}, not "
                                f"{COMPANY_C_LEDGER_PARENT!r} — fix it in the UI; setup-c never re-creates a master "
                                "(LESSONS §15 rule 10).")
    else:
        report.skipped.append("ledger")
    if any(row["Narration"] == COMPANY_C_VOUCHER_NARRATION for row in writer.list_vouchers(company)):
        report.skipped.append("voucher")
    else:
        writer.create_payment(company, ledger=COMPANY_C_LEDGER, amount=Decimal(COMPANY_C_VOUCHER_AMOUNT),
                              narration=COMPANY_C_VOUCHER_NARRATION, date=COMPANY_C_VOUCHER_DATE)
        report.created.append("voucher")
    return report
