# Copied from: backend/tally_bridge/client.py @ c04d7d2
# Changes: no mock mode (the mock handler module is never copied into v2/agent); per-request timeout (default 30 s, max 90 s);
# post_xml returns a TallyResponse with elapsed ms and the raw bytes; timeouts raise TallyTimeoutError;
# optional httpx transport for tests; a connect timeout raises a distinct TallyTimeoutError message; trust_env=False
# (proxy env vars must not apply to a local XML server).
"""Async HTTP client for TallyPrime's XML server (read requests only)."""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from agent.tally.envelopes import build_company_list
from agent.tally.exceptions import TallyConnectionError, TallyResponseError, TallyTimeoutError

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
    def __init__(self, host: str = "localhost", port: int = 9000, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = f"http://{host}:{port}"
        self._client = httpx.AsyncClient(transport=transport, trust_env=False)

    async def post_xml(self, xml_payload: str, timeout: float | None = None) -> TallyResponse:
        seconds = min(timeout or DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)
        started = time.perf_counter()
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
            result = await self.post_xml(build_company_list())
        except (TallyConnectionError, TallyResponseError):
            return False
        return "COMPANY" in result.text.upper()
