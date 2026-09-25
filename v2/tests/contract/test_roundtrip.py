import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from v2.contract import transcode
from v2.contract.models import (BatchRequest, BindRequest, HeartbeatRequest, ReconcileRequest, RunCreate, RunPatch,
                                   SnapshotRequest, WireObject)

SYNC = Path(__file__).resolve().parents[1] / "fixtures" / "sync"


@pytest.mark.parametrize("fixture", ["p06_A_vouchers_nested.xml", "p22_B_forex_sales.xml",
                                     "p15_B_hindi_narration.xml", "p03_B_flagged_month_2023_02.xml"])
def test_voucher_wire_json_model_is_lossless(fixture):
    for obj in transcode.vouchers_from_xml((SYNC / fixture).read_text(encoding="utf-8")):
        again = WireObject.model_validate(json.loads(json.dumps(obj, ensure_ascii=False))).model_dump()
        assert again == obj


def test_masters_and_balances_wire_json_model_is_lossless():            # p25_A_groups.xml (U+0004), G5
    objs = transcode.masters_from_xml((SYNC / "p25_A_groups.xml").read_text(encoding="utf-8"), "group")
    objs += transcode.balances_from_xml((SYNC / "s1_B_ledgers_touched.xml").read_text(encoding="utf-8"),
                                        "2026-09-25T18:43:45+05:30")
    for obj in objs:
        assert WireObject.model_validate(json.loads(json.dumps(obj, ensure_ascii=False))).model_dump() == obj


def test_spec_7_examples_validate():                                    # spec §7.5, §7.6, §7.8, §7.9, §7.11, §7.12
    batch = BatchRequest.model_validate({
        "batch_id": "0b8e", "run_id": "r1", "company_guid": "138b7373-753c-4dbe-aa63-b802035f0ba9",
        "chunk": {"from": "2022-09-01", "to": "2022-09-02"}, "objects": [{"kind": "voucher", "data": {}}],
        "quarantine": []})
    assert batch.chunk.from_.isoformat() == "2022-09-01"
    assert batch.model_dump(by_alias=True)["chunk"]["from"].isoformat() == "2022-09-01"
    HeartbeatRequest.model_validate({
        "agent_version": "0.1.0", "tally_version": "TallyPrime 7.0", "tally_status": "ours",
        "seen_company": {"guid": "138b…0ba9", "name": "Sharma & Sons' Probe Traders"},
        "counters": {"alt_vch_id": 965, "alt_mst_id": 412}, "last_error_code": None, "breaker": "closed",
        "outbox_depth": 0, "pc_clock": "2026-09-25T18:02:11+05:30", "acked_commands": ["c1"]})
    RunCreate.model_validate({"kind": "incremental", "scope": None, "command_id": None,
                              "counters_at_start": {"alt_vch_id": 965, "alt_mst_id": 412}, "progress_total": None})
    RunPatch.model_validate({"status": "completed", "progress_done": 24, "progress_total": 24,
                             "batches_declared": 37, "cursor_after": {"alt_vch_id": 970, "alt_mst_id": 415}})
    ReconcileRequest.model_validate({
        "run_id": "r1", "scope": {"kind": "vouchers", "from": "2023-02-01", "to": "2023-02-28"},
        "present": [{"guid": "138b…-000000c9", "alter_id": 963}], "present_count": 81, "confirm_large": False})
    SnapshotRequest.model_validate({
        "report_type": "trial_balance", "from_date": "01-04-2022", "as_on_date": "31-03-2023",
        "request_flags": {"EXPLODEFLAG": "Yes"}, "purpose": "anchor", "captured_at": "2026-09-25T16:52:49+05:30",
        "counters": {"alt_vch_id": 965, "alt_mst_id": 412},
        "cells": [{"dspdispname": "Capital Account", "dspcldramta": "", "dspclcramta": "1000000.00"}]})
    BindRequest.model_validate({
        "workspace_id": "w1", "company_guid": "138b7373-753c-4dbe-aa63-b802035f0ba9",
        "company_name": "Sharma & Sons' Probe Traders", "books_from": "20220401", "base_currency_name": "?",
        "takeover": False})
    with pytest.raises(ValidationError):                                # bodies are strict: no unknown keys
        RunPatch.model_validate({"status": "completed", "cursor": {}})
