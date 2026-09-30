from pathlib import Path

from agent.tally.xml_utils import detect_error, parse_company_list, read_objects, sanitize_xml

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "tally_samples"


def test_company_list_live_skips_cmpinfo_counter():
    xml = (SAMPLES / "company_list_live.xml").read_text(encoding="utf-8")
    assert parse_company_list(xml) == ["NUVANTA AI TECHNOLOGIES PRIVATE LIMITED"]


def test_company_list_reads_name_child_and_name_attribute():
    xml = (
        "<ENVELOPE><CMPINFO><COMPANY>2</COMPANY></CMPINFO>"
        '<COMPANY NAME="Alpha &amp; Co"><X/></COMPANY>'
        "<COMPANY><NAME>Beta</NAME></COMPANY></ENVELOPE>"
    )
    assert parse_company_list(xml) == ["Alpha & Co", "Beta"]


def test_sanitize_strips_control_character_references():
    assert sanitize_xml("<A>x&#4;y</A>") == "<A>xy</A>"


def test_detect_error_finds_lineerror_and_error_count():
    assert detect_error("<ENVELOPE><LINEERROR>Bad date</LINEERROR></ENVELOPE>") == "Bad date"
    assert detect_error("<ENVELOPE><ERRORS>2</ERRORS></ENVELOPE>") == "Tally reported 2 error(s)"
    assert detect_error("<ENVELOPE><ERRORS>0</ERRORS></ENVELOPE>") is None
    assert detect_error("not xml") == "Invalid XML response from Tally"


def test_read_objects_reads_fields_any_case_and_skips_bare_counters():
    xml = (
        "<ENVELOPE><CMPINFO><COMPANY>0</COMPANY></CMPINFO><COLLECTION>"
        '<COMPANY NAME="Beta"><GUID TYPE="String">g-1</GUID><ALTVCHID> 12</ALTVCHID></COMPANY>'
        "</COLLECTION></ENVELOPE>"
    )
    rows = read_objects(xml, "COMPANY", ["Name", "GUID", "AltVchId", "AltMstId"])
    assert rows == [{"Name": "Beta", "GUID": "g-1", "AltVchId": "12", "AltMstId": ""}]
