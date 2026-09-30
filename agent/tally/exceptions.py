# Copied from: backend/tally_bridge/exceptions.py @ c04d7d2
# Changes: added TallyTimeoutError so a timeout is told apart from a refused connection.
"""Exceptions raised by the v2 Tally read client."""


class TallyConnectionError(Exception):
    """Raised when TallyPrime is unreachable."""


class TallyTimeoutError(TallyConnectionError):
    """Raised when TallyPrime did not connect or answer in time."""


class TallyResponseError(Exception):
    """Raised when TallyPrime returns an HTTP error or an unparseable response."""
