"""Pure logic in ``v2.cloud.sync.state`` (S1 spec §8.2, §8.6): the ``sync_state`` machine and restore
detection. No DB — ``detect_restore`` takes a plain object with the two cursor attributes, and ``transition``
mutates a bare namespace with a ``sync_state`` attribute."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from v2.cloud.sync.state import STATES, TRANSITIONS, detect_restore, transition


@dataclass
class _FakeSw:
    sync_state: str
    cursor_alt_vch_id: int | None = None
    cursor_alt_mst_id: int | None = None


# --- §8.2 table coverage (controller ruling F11) --------------------------------------------------------

SPEC_8_2_TRANSITIONS = [
    # (from, event, to) — every row of the §8.2 table that `transition()` is responsible for. The "(none) ->
    # bind -> awaiting_first_connection" row and the "any -> device revoked/taken over -> unchanged" row are
    # not `transition()` calls (no prior state / no state change), so they're not listed here.
    ("awaiting_first_connection", "first_sync_opened", "first_sync"),
    # Task 7 / F12 (controller ruling): a failed first_sync leaves the workspace `error`; the spec's own §8.2
    # table has no `error -> first_sync` row, but refusing a NEW first_sync from `error` (while cursors are
    # still NULL — a first sync has never completed) would leave the workspace stuck forever. `runs.open_run`
    # only fires this event under that NULL-cursor guard.
    ("error", "first_sync_opened", "first_sync"),
    ("first_sync", "first_sync_completed_window_complete", "ready"),
    ("first_sync", "fatal", "error"),
    ("error", "run_completed_window_complete", "ready"),
    ("error", "run_completed_window_incomplete", "first_sync"),
    ("ready", "counters_backwards", "restore_detected"),
    ("error", "counters_backwards", "restore_detected"),
    ("restore_detected", "company_resync_completed", "ready"),
    ("ready", "company_resync_completed", "ready"),
    ("error", "company_resync_completed", "ready"),
] + [(s, "relink", "restore_detected") for s in STATES]


@pytest.mark.parametrize("from_state, event, to_state", SPEC_8_2_TRANSITIONS)
def test_every_spec_8_2_transition_is_defined(from_state, event, to_state):
    sw = _FakeSw(sync_state=from_state)
    transition(sw, event)
    assert sw.sync_state == to_state


def test_transitions_dict_has_no_extra_undocumented_rows():
    assert set(TRANSITIONS.items()) == {
        ((f, e), t) for f, e, t in SPEC_8_2_TRANSITIONS
    }


def test_undefined_pair_raises_value_error():
    sw = _FakeSw(sync_state="awaiting_first_connection")
    with pytest.raises(ValueError):
        transition(sw, "counters_backwards")  # only defined from ready/error


def test_relink_defined_from_every_state():
    for s in STATES:
        sw = _FakeSw(sync_state=s)
        transition(sw, "relink")
        assert sw.sync_state == "restore_detected"


# --- §8.6 detect_restore (pure) -----------------------------------------------------------------------------


def test_detect_restore_false_when_no_cursors_set_yet():
    sw = _FakeSw(sync_state="ready")
    assert detect_restore(sw, {"alt_vch_id": 1, "alt_mst_id": 1}) is False


def test_detect_restore_true_on_vch_backwards():
    sw = _FakeSw(sync_state="ready", cursor_alt_vch_id=79, cursor_alt_mst_id=269)
    assert detect_restore(sw, {"alt_vch_id": 78, "alt_mst_id": 269}) is True


def test_detect_restore_true_on_mst_backwards():
    sw = _FakeSw(sync_state="ready", cursor_alt_vch_id=79, cursor_alt_mst_id=269)
    assert detect_restore(sw, {"alt_vch_id": 79, "alt_mst_id": 268}) is True


def test_detect_restore_false_when_counters_equal_or_ahead():
    sw = _FakeSw(sync_state="ready", cursor_alt_vch_id=79, cursor_alt_mst_id=269)
    assert detect_restore(sw, {"alt_vch_id": 79, "alt_mst_id": 269}) is False
    assert detect_restore(sw, {"alt_vch_id": 80, "alt_mst_id": 270}) is False


def test_detect_restore_never_mutates_sw():
    sw = _FakeSw(sync_state="ready", cursor_alt_vch_id=79, cursor_alt_mst_id=269)
    detect_restore(sw, {"alt_vch_id": 1, "alt_mst_id": 1})
    assert sw.sync_state == "ready" and sw.cursor_alt_vch_id == 79
