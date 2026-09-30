"""Date helpers the bridge's queries need: the Indian financial year and Tally's DD-MM-YYYY request format.

``backend.utils.date_utils`` re-exports them beside the app's date-range resolution.
"""
from datetime import date


def get_fy_start(ref: date) -> date:
    """Return April 1 of the current Indian financial year."""
    if ref.month >= 4:
        return date(ref.year, 4, 1)
    return date(ref.year - 1, 4, 1)


def get_fy_end(ref: date) -> date:
    """Return March 31 of the current Indian financial year."""
    if ref.month >= 4:
        return date(ref.year + 1, 3, 31)
    return date(ref.year, 3, 31)


def format_for_tally(d: date) -> str:
    """Format date as DD-MM-YYYY for Tally requests."""
    return d.strftime("%d-%m-%Y")
