"""Escalation ladder (S1 spec §10.8, §15.5, decision 12). Every transition, plus "no ``remediation_done`` ->
state kept" (§16)."""
from datetime import date

from v2.cloud.parity.ladder import step, visible_state

OK = {"state": "ok", "heal_attempts": 0, "resync_offered_fy": None, "pending_remediation_ids": []}
FY = date(2025, 4, 1)


def test_clean_run_resets_to_ok_and_clears_offer():
    dirty = {"state": "alert", "heal_attempts": 3, "resync_offered_fy": FY, "pending_remediation_ids": ["abc"]}
    out = step(dirty, had_mismatch=False, remediation_done=[], issued_ids=[],
               confirmed_fy_resync_completed=False, fy_for_offer=FY)
    assert out == {"state": "ok", "heal_attempts": 0, "resync_offered_fy": None, "pending_remediation_ids": []}


def test_first_mismatch_after_ok_is_suspect_heal_0():
    out = step(OK, had_mismatch=True, remediation_done=[], issued_ids=["r1"],
               confirmed_fy_resync_completed=False, fy_for_offer=FY)
    assert out["state"] == "suspect"
    assert out["heal_attempts"] == 0
    assert out["pending_remediation_ids"] == ["r1"]


def test_mismatch_after_suspect_with_remediation_done_is_alert_heal_1():
    suspect = {"state": "suspect", "heal_attempts": 0, "resync_offered_fy": None, "pending_remediation_ids": ["r1"]}
    out = step(suspect, had_mismatch=True, remediation_done=["r1"], issued_ids=["r2"],
               confirmed_fy_resync_completed=False, fy_for_offer=FY)
    assert out["state"] == "alert"
    assert out["heal_attempts"] == 1
    assert out["resync_offered_fy"] is None
    assert out["pending_remediation_ids"] == ["r2"]


def test_mismatch_without_remediation_done_keeps_state_and_reissues():
    suspect = {"state": "suspect", "heal_attempts": 0, "resync_offered_fy": None, "pending_remediation_ids": ["r1"]}
    out = step(suspect, had_mismatch=True, remediation_done=[], issued_ids=["r1"],
               confirmed_fy_resync_completed=False, fy_for_offer=FY)
    assert out["state"] == "suspect"
    assert out["heal_attempts"] == 0
    assert out["pending_remediation_ids"] == ["r1"]

    alert = {"state": "alert", "heal_attempts": 1, "resync_offered_fy": None, "pending_remediation_ids": ["r2"]}
    out2 = step(alert, had_mismatch=True, remediation_done=["some_other_id"], issued_ids=["r2"],
                confirmed_fy_resync_completed=False, fy_for_offer=FY)
    assert out2["state"] == "alert"
    assert out2["heal_attempts"] == 1


def test_heal_attempts_2_offers_resync_for_fy():
    alert = {"state": "alert", "heal_attempts": 1, "resync_offered_fy": None, "pending_remediation_ids": ["r2"]}
    out = step(alert, had_mismatch=True, remediation_done=["r2"], issued_ids=["r3"],
               confirmed_fy_resync_completed=False, fy_for_offer=FY)
    assert out["state"] == "alert"
    assert out["heal_attempts"] == 2
    assert out["resync_offered_fy"] == FY


def test_after_confirmed_fy_resync_still_mismatch_is_hard_alert():
    offered = {"state": "alert", "heal_attempts": 2, "resync_offered_fy": FY, "pending_remediation_ids": ["r3"]}
    out = step(offered, had_mismatch=True, remediation_done=["r3"], issued_ids=["r4"],
               confirmed_fy_resync_completed=True, fy_for_offer=FY)
    assert out["state"] == "hard_alert"


def test_persistent_no_remediation_mismatch_climbs_to_alert_and_becomes_visible():
    # Review I1 / controller ruling: a flag_filter_inverted-style cause carries NO remediation (classify() returns
    # issued_ids=[] for it). Without the fix this mismatch sits in invisible `suspect` forever, since
    # `remediation_done` can never cover an empty pending list. The fix: no remediation issued counts as
    # "remediation done" by default, so a persistent mismatch still climbs the §10.8 ladder on run count alone.
    run1 = step(OK, had_mismatch=True, remediation_done=[], issued_ids=[],
                confirmed_fy_resync_completed=False, fy_for_offer=FY)
    assert run1["state"] == "suspect" and run1["heal_attempts"] == 0 and run1["pending_remediation_ids"] == []
    assert visible_state(run1) == "ok"                      # still invisible after the first mismatch

    run2 = step(run1, had_mismatch=True, remediation_done=[], issued_ids=[],
                confirmed_fy_resync_completed=False, fy_for_offer=FY)
    assert run2["state"] == "alert" and run2["heal_attempts"] == 1
    assert visible_state(run2) == "alert"                    # now visible


def test_hard_alert_stays_hard_alert_even_with_remediation_done():
    # Review I2 / controller ruling: hard_alert is lowered ONLY by a clean ok run -- not by remediation.
    hard = {"state": "hard_alert", "heal_attempts": 2, "resync_offered_fy": FY, "pending_remediation_ids": ["r4"]}
    out = step(hard, had_mismatch=True, remediation_done=["r4"], issued_ids=["r5"],
               confirmed_fy_resync_completed=False, fy_for_offer=FY)
    assert out["state"] == "hard_alert"


def test_hard_alert_clears_to_ok_on_a_clean_run():
    hard = {"state": "hard_alert", "heal_attempts": 2, "resync_offered_fy": FY, "pending_remediation_ids": ["r4"]}
    out = step(hard, had_mismatch=False, remediation_done=[], issued_ids=[],
               confirmed_fy_resync_completed=False, fy_for_offer=FY)
    assert out == {"state": "ok", "heal_attempts": 0, "resync_offered_fy": None, "pending_remediation_ids": []}


def test_visible_state_hides_suspect():
    suspect = {"state": "suspect", "heal_attempts": 0, "resync_offered_fy": None, "pending_remediation_ids": []}
    assert visible_state(suspect) == "ok"
    for visible in ("ok", "alert", "hard_alert"):
        assert visible_state({"state": visible, "heal_attempts": 0, "resync_offered_fy": None,
                               "pending_remediation_ids": []}) == visible


def test_no_ladder_transition_ever_starts_a_resync():
    # Decision 12: the ladder may only OFFER a resync (set resync_offered_fy) -- it never starts one. The return
    # value carries no action/started field, and a resync is offered only via that one field, never triggered.
    allowed_keys = {"state", "heal_attempts", "resync_offered_fy", "pending_remediation_ids"}
    scenarios = [
        (OK, dict(had_mismatch=False, remediation_done=[], issued_ids=[],
                  confirmed_fy_resync_completed=False, fy_for_offer=FY)),
        (OK, dict(had_mismatch=True, remediation_done=[], issued_ids=["r1"],
                  confirmed_fy_resync_completed=False, fy_for_offer=FY)),
        ({"state": "alert", "heal_attempts": 1, "resync_offered_fy": None, "pending_remediation_ids": ["r2"]},
         dict(had_mismatch=True, remediation_done=["r2"], issued_ids=["r3"],
              confirmed_fy_resync_completed=False, fy_for_offer=FY)),
        ({"state": "alert", "heal_attempts": 2, "resync_offered_fy": FY, "pending_remediation_ids": ["r3"]},
         dict(had_mismatch=True, remediation_done=["r3"], issued_ids=["r4"],
              confirmed_fy_resync_completed=True, fy_for_offer=FY)),
    ]
    for prev, kwargs in scenarios:
        out = step(prev, **kwargs)
        assert set(out.keys()) == allowed_keys
        assert out["state"] in ("ok", "suspect", "alert", "hard_alert")
