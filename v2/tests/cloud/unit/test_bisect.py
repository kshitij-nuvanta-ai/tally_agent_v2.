"""Month-bisect evaluation (S1 spec §10.9)."""
from datetime import date

from v2.cloud.parity.bisect import first_diverging_month


def test_first_diverging_month_oldest_first():
    evaluations = [
        (date(2025, 4, 30), False),
        (date(2025, 5, 31), False),
        (date(2025, 6, 30), True),
        (date(2025, 7, 31), True),   # later divergence must not win -- oldest first
    ]
    assert first_diverging_month(evaluations) == date(2025, 6, 30)


def test_none_when_all_clean():
    evaluations = [(date(2025, 4, 30), False), (date(2025, 5, 31), False)]
    assert first_diverging_month(evaluations) is None
