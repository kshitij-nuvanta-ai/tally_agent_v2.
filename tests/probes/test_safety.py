import pytest

from probes.safety import GuardError, check_company, check_mutation_allowed, check_request

A = "Bharat Traders Probe Copy"


@pytest.mark.parametrize("xml", [
    "<COLLECTION><NATIVEMETHOD>*</NATIVEMETHOD></COLLECTION>",
    "<NATIVEMETHOD> AllLedgerEntries.* </NATIVEMETHOD>",
    "<FETCHLIST><FETCH>*</FETCH></FETCHLIST>",
    "<SYSTEM>$$InDateRange:$Date:$$Date:1:$$Date:2</SYSTEM>",
    "<system>$$indaterange</system>",
    '<NATIVEMETHOD TYPE="String">*</NATIVEMETHOD>',
    "<FETCH >*</FETCH>",
    "<NATIVEMETHOD>&#42;</NATIVEMETHOD>",
    "<FETCH>&#x2A;</FETCH>",
])
def test_forbidden_requests_refused(xml):
    with pytest.raises(GuardError):
        check_request(xml)


def test_native_method_with_attribute_and_real_value_allowed():
    check_request('<NATIVEMETHOD TYPE="String">GUID</NATIVEMETHOD>')


def test_clean_request_allowed():
    check_request("<NATIVEMETHOD>GUID</NATIVEMETHOD><SYSTEM>$AlterID > 5</SYSTEM>")


def test_company_guard_cases():
    check_company([A], A, mutating=False)
    check_company([A], A, mutating=True)
    with pytest.raises(GuardError, match="No company open"):
        check_company([], A, mutating=False)
    with pytest.raises(GuardError, match="2 companies are loaded"):
        check_company([A, "Other"], A, mutating=False)
    with pytest.raises(GuardError, match="expects"):
        check_company(["Bharat Traders Private Limited"], A, mutating=False)


def test_mutation_guard_needs_probe_in_name():
    with pytest.raises(GuardError, match="only companies with 'Probe'"):
        check_company(["Bharat Traders Private Limited"], "Bharat Traders Private Limited", mutating=True)


def test_check_mutation_allowed_standalone():
    check_mutation_allowed(A, mutating=True)
    check_mutation_allowed("Plain Co", mutating=False)
    with pytest.raises(GuardError, match="only companies with 'Probe'"):
        check_mutation_allowed("Plain Co", mutating=True)


def test_educational_guard_refuses_an_off_day_date_variable_typed_or_not():
    import pytest
    from agent.tally.envelopes import wrap_report
    from probes.reads import untyped_period_vars
    from probes.safety import GuardError, check_educational_dates
    xml = wrap_report("Bills Receivable", "30-09-2025", "30-09-2025", "Co")
    with pytest.raises(GuardError, match="SVFROMDATE=30-09-2025"):
        check_educational_dates(xml, "educational")
    with pytest.raises(GuardError, match="C43"):
        check_educational_dates(untyped_period_vars(xml), "educational")


def test_educational_guard_passes_days_1_2_31_placeholders_licensed_and_unknown():
    from agent.tally.envelopes import wrap_report
    from probes.safety import check_educational_dates
    for day in ("01-04-2025", "02-06-2023", "31-10-2025"):
        check_educational_dates(wrap_report("Trial Balance", day, day, "Co"), "educational")
    check_educational_dates(wrap_report("Trial Balance", "__FROM__", "__TO__", "Co"), "educational")
    check_educational_dates(wrap_report("Trial Balance", "30-09-2025", "30-09-2025", "Co"), "licensed")
    check_educational_dates(wrap_report("Trial Balance", "30-09-2025", "30-09-2025", "Co"), None)


def test_educational_guard_reads_every_date_format_tally_accepts():
    from probes.safety import educational_ignored_dates
    assert educational_ignored_dates('<SVTODATE TYPE="Date">20250930</SVTODATE>') == ["SVTODATE=20250930"]
    assert educational_ignored_dates("<SVFROMDATE>30-Sep-2025</SVFROMDATE>") == ["SVFROMDATE=30-Sep-2025"]
    assert educational_ignored_dates("<SVCURRENTDATE>1-Apr-25</SVCURRENTDATE>") == []
