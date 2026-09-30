"""
Core async HTTP client for TallyPrime communication.
TallyPrime runs as an HTTP server. We POST XML requests and parse responses.
CONNECTION: http://<TALLY_HOST>:<TALLY_PORT> (default: localhost:9000)

Two ways to send a request:
- ``post_xml`` returns the response text. It honours mock mode and uses the client-wide 90 s timeout (writes can be
  slow); a timeout raises ``TallyConnectionError``. The app's chat, upload and write paths use it.
- ``post`` returns a ``TallyResponse`` (text, raw bytes, elapsed ms), takes a per-request timeout (default 30 s,
  capped at 90 s) and raises ``TallyTimeoutError`` on a timeout. The sync path and the probes use it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from tally_bridge.exceptions import TallyConnectionError, TallyResponseError, TallyTimeoutError
from tally_bridge.request_builder import build_list_companies

DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 90.0
CONNECT_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class TallyResponse:
    text: str
    raw: bytes
    elapsed_ms: int

    @property
    def response_bytes(self) -> int:
        return len(self.raw)


class TallyClient:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 9000,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        trust_env: bool = True,
    ):
        """``transport`` injects an httpx transport (tests). ``trust_env=False`` stops proxy environment variables
        from applying to the request (they must not apply to a local XML server); the default keeps httpx's own."""
        self.base_url = f"http://{host}:{port}"
        self.timeout = httpx.Timeout(90.0, connect=5.0)  # 90s — writes can be slow per docs/tally-write-exploration-v4.md
        self._client = httpx.AsyncClient(timeout=self.timeout, transport=transport, trust_env=trust_env)
        self.mock_mode: bool = False

    async def post_xml(self, xml_payload: str) -> str:
        if self.mock_mode:
            from tally_bridge.mock_handler import mock_tally_request
            return mock_tally_request(xml_payload)
        try:
            response = await self._client.post(
                self.base_url,
                content=xml_payload,
                headers={"Content-Type": "text/xml; charset=utf-8"},
            )
            response.raise_for_status()
            return response.text
        except httpx.ConnectError:
            raise TallyConnectionError(
                f"Cannot connect to TallyPrime at {self.base_url}. "
                "Ensure Tally is running with a company loaded and port is configured."
            )
        except httpx.TimeoutException:
            raise TallyConnectionError(
                f"TallyPrime at {self.base_url} timed out. "
                "The request may be too heavy or Tally is busy."
            )
        except httpx.HTTPStatusError as e:
            raise TallyResponseError(f"Tally returned HTTP {e.response.status_code}")
        except httpx.TransportError as e:
            raise TallyConnectionError(
                f"Transport error communicating with TallyPrime at {self.base_url}: {e}"
            )

    async def post(self, xml_payload: str, timeout: float | None = None) -> TallyResponse:
        """Send a request and return the text, the raw bytes and the elapsed milliseconds.

        ``timeout`` is the per-request read timeout in seconds (default 30, capped at 90). A timeout raises
        ``TallyTimeoutError`` (a ``TallyConnectionError``), so it can be told apart from a refused connection.
        In mock mode the built-in mock handler answers, as in ``post_xml``.
        """
        seconds = min(timeout or DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)
        started = time.perf_counter()
        if self.mock_mode:
            from tally_bridge.mock_handler import mock_tally_request
            text = mock_tally_request(xml_payload)
            return TallyResponse(text=text, raw=text.encode("utf-8"),
                                 elapsed_ms=round((time.perf_counter() - started) * 1000))
        try:
            response = await self._client.post(
                self.base_url,
                content=xml_payload.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8"},
                timeout=httpx.Timeout(seconds, connect=CONNECT_TIMEOUT_S),
            )
            response.raise_for_status()
        except httpx.ConnectTimeout as exc:
            raise TallyTimeoutError(f"Could not connect to TallyPrime at {self.base_url} within "
                                    f"{CONNECT_TIMEOUT_S:.0f}s") from exc
        except httpx.TimeoutException as exc:
            raise TallyTimeoutError(f"TallyPrime at {self.base_url} did not answer within {seconds:.0f}s") from exc
        except httpx.ConnectError as exc:
            raise TallyConnectionError(f"Cannot connect to TallyPrime at {self.base_url}") from exc
        except httpx.HTTPStatusError as exc:
            raise TallyResponseError(f"Tally returned HTTP {exc.response.status_code}") from exc
        except httpx.TransportError as exc:
            raise TallyConnectionError(f"Transport error talking to TallyPrime at {self.base_url}: {exc}") from exc
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return TallyResponse(text=response.text, raw=response.content, elapsed_ms=elapsed_ms)

    async def close(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> bool:
        try:
            result = await self.post_xml(build_list_companies())
            return "<COMPANY>" in result or "COMPANY" in result.upper()
        except (TallyConnectionError, TallyResponseError):
            return False
