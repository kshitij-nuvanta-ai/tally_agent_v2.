"""D28 guard boundary tests (S1 spec §7.11, controller ruling 3 / fix-round-1 I4) -- pure function, no DB.

The rule: refuse (``409 reconcile_too_large``) only when a reconcile would soft-delete MORE than 50 rows AND
MORE than 20% of the scope. Both thresholds use strict ``>`` (never ``>=``), and the guard uses integer
arithmetic (``would_delete * 5 > scope_total``) so there is no float rounding at the boundary.
"""
import pytest

from v2.cloud.errors import ApiError
from v2.cloud.ingest.reconcile import _check_guard


def test_50_rows_over_20pct_allowed():
    """Exactly 50 rows is never guarded, however large the percentage -- the rule needs MORE than 50."""
    _check_guard(50, 60, confirm_large=False)               # 50/60 ~= 83% but 50 is not > 50


def test_51_rows_at_exactly_20pct_allowed():
    """51 * 5 = 255, which is NOT > 255 (scope_total=255 means exactly 20%) -- allowed."""
    _check_guard(51, 255, confirm_large=False)


def test_51_rows_just_over_20pct_refused():
    """scope_total=254: 51 * 5 = 255 > 254, i.e. just over 20% -- refused."""
    with pytest.raises(ApiError) as exc:
        _check_guard(51, 254, confirm_large=False)
    assert exc.value.status == 409
    assert exc.value.code == "reconcile_too_large"
    assert exc.value.extra == {"would_delete": 51, "scope_total": 254}


def test_51_rows_just_over_20pct_confirm_large_allows():
    """The same refused case, resent with `confirm_large: true` after the agent re-reads its own list."""
    _check_guard(51, 254, confirm_large=True)


def test_51_rows_well_under_20pct_allowed():
    _check_guard(51, 1000, confirm_large=False)             # 51 > 50, but 51*5=255 is not > 1000


def test_zero_would_delete_never_guarded():
    _check_guard(0, 0, confirm_large=False)
