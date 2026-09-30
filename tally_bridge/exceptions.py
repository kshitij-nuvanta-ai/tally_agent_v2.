class TallyConnectionError(Exception):
    """Raised when TallyPrime is unreachable or times out."""
    pass


class TallyTimeoutError(TallyConnectionError):
    """Raised by ``TallyClient.post`` when TallyPrime did not connect or answer in time.

    A subclass of ``TallyConnectionError``, so code that catches the parent keeps working.
    ``TallyClient.post_xml`` does not raise it: there a timeout is a plain ``TallyConnectionError``.
    """


class TallyResponseError(Exception):
    """Raised when TallyPrime returns an unparseable or error response."""
    pass
