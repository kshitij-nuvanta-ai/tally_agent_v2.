# Copied from: backend/tally_bridge/response_parser.py @ c04d7d2
# Changes: sanitize_xml, detect_error and _get_text (now get_text) as-is; parse_company_list only counts COMPANY
# elements with a NAME child or attribute (the source also counts CMPINFO's <COMPANY>0</COMPANY> as a company);
# added read_objects.
"""XML helpers for Tally responses."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET


def sanitize_xml(raw_xml: str) -> str:
    """Remove invalid XML character references that TallyPrime sometimes emits (e.g. &#4;)."""
    return re.sub(r"&#([0-8]|1[0-1]|1[4-9]|2[0-9]|3[0-1]);", "", raw_xml)


def detect_error(raw_xml: str) -> str | None:
    try:
        root = ET.fromstring(sanitize_xml(raw_xml))
    except ET.ParseError:
        return "Invalid XML response from Tally"
    error_el = root.find(".//LINEERROR")
    if error_el is not None and error_el.text:
        return error_el.text.strip()
    errors_el = root.find(".//ERRORS")
    if errors_el is not None and errors_el.text and errors_el.text.strip() != "0":
        return f"Tally reported {errors_el.text.strip()} error(s)"
    return None


def get_text(element: ET.Element, tag: str) -> str:
    child = element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def parse_company_list(raw_xml: str) -> list[str]:
    """Names of the companies in a company-list response. CMPINFO's bare count elements are ignored."""
    root = ET.fromstring(sanitize_xml(raw_xml))
    names: list[str] = []
    for comp in root.iter("COMPANY"):
        name = (get_text(comp, "NAME") or comp.get("NAME", "")).strip()
        if name:
            names.append(name)
    return names


def read_objects(raw_xml: str, tag: str, fields: list[str]) -> list[dict[str, str]]:
    """Read `fields` (Tally method names, any case) from every `tag` element; a missing field is "".

    Elements with neither children nor attributes (e.g. CMPINFO's <COMPANY>0</COMPANY>) are skipped.
    NAME falls back to the element's NAME attribute.
    """
    root = ET.fromstring(sanitize_xml(raw_xml))
    rows: list[dict[str, str]] = []
    for element in root.iter(tag.upper()):
        if len(element) == 0 and not element.attrib:
            continue
        row: dict[str, str] = {}
        for field in fields:
            value = get_text(element, field.upper())
            if not value and field.upper() == "NAME":
                value = element.get("NAME", "").strip()
            row[field] = value
        rows.append(row)
    return rows
