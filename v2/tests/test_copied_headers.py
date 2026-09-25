"""Every copied file names its source and commit on line 1 (Part 1 §5 "Code isolation (v2)")."""
import re
from pathlib import Path

import pytest

V2_ROOT = Path(__file__).resolve().parents[1]
HEADER = re.compile(r"^# Copied from: (?P<src>\S+) @ (?P<commit>[0-9a-f]{7,40})$")
COPIED = {
    "agent/tally/exceptions.py": "backend/tally_bridge/exceptions.py",
    "agent/tally/xml_utils.py": "backend/tally_bridge/response_parser.py",
    "agent/tally/envelopes.py": "backend/tally_bridge/request_builder.py",
    "agent/tally/client.py": "backend/tally_bridge/client.py",
    "agent/tally/reports.py": "backend/tally_bridge/response_parser.py",
    "probes/setup/import_xml.py": "backend/tally_bridge/import_builder.py",
    "contract/parse.py": "v2/agent/tally/amounts.py",
    "contract/tally_rules.py": "v2/probes/reads.py",
    "contract/transcode.py": "v2/agent/tally/xml_utils.py",
}


@pytest.mark.parametrize("rel,source", sorted(COPIED.items()))
def test_copied_file_header(rel, source):
    lines = (V2_ROOT / rel).read_text(encoding="utf-8").splitlines()
    match = HEADER.match(lines[0])
    assert match, f"{rel} line 1 must be '# Copied from: <path> @ <commit>'"
    assert match["src"] == source
    assert lines[1].startswith("# Changes:")
