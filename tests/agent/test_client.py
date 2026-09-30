from pathlib import Path

import httpx
import pytest

from agent.tally import client as client_module
from agent.tally.client import TallyClient
from agent.tally.exceptions import TallyConnectionError, TallyResponseError, TallyTimeoutError


def _client(handler):
    return TallyClient(transport=httpx.MockTransport(handler))


async def test_post_returns_text_raw_bytes_and_elapsed():
    c = _client(lambda request: httpx.Response(200, content="<E>शर्मा&#4;</E>".encode("utf-8")))
    response = await c.post_xml("<ENVELOPE/>")
    assert response.text == "<E>शर्मा&#4;</E>"
    assert response.raw == "<E>शर्मा&#4;</E>".encode("utf-8")
    assert response.response_bytes == len(response.raw)
    assert response.elapsed_ms >= 0


async def test_request_body_is_utf8():
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, content=b"<E/>")

    await _client(handler).post_xml("<X>शर्मा ट्रेडर्स</X>")
    assert "शर्मा ट्रेडर्स".encode("utf-8") in seen["body"]


async def test_timeout_raises_timeout_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TallyTimeoutError):
        await _client(handler).post_xml("<X/>")


async def test_connect_timeout_raises_distinct_message():
    def handler(request):
        raise httpx.ConnectTimeout("slow to connect", request=request)

    with pytest.raises(TallyTimeoutError, match=r"Could not connect to TallyPrime at .* within 5s"):
        await _client(handler).post_xml("<X/>")


async def test_refused_raises_connection_error_not_timeout():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TallyConnectionError) as info:
        await _client(handler).post_xml("<X/>")
    assert not isinstance(info.value, TallyTimeoutError)


async def test_http_error_raises_response_error():
    with pytest.raises(TallyResponseError):
        await _client(lambda request: httpx.Response(500)).post_xml("<X/>")


async def test_timeout_is_clamped_to_max():
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, content=b"<E/>")

    await _client(handler).post_xml("<X/>", timeout=500)
    assert seen["timeout"]["read"] == client_module.MAX_TIMEOUT_S
    assert seen["timeout"]["connect"] == client_module.CONNECT_TIMEOUT_S


async def test_default_timeout():
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, content=b"<E/>")

    await _client(handler).post_xml("<X/>")
    assert seen["timeout"]["read"] == client_module.DEFAULT_TIMEOUT_S


async def test_health_check():
    ok = _client(lambda request: httpx.Response(200, content=b"<ENVELOPE><COMPANY NAME='x'/></ENVELOPE>"))
    assert await ok.health_check() is True

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    assert await _client(down).health_check() is False


async def test_no_mock_mode_in_v2_client():
    c = TallyClient()
    try:
        assert not hasattr(c, "mock_mode")
    finally:
        await c.close()
    source = Path(client_module.__file__).read_text(encoding="utf-8")
    assert "mock_mode" not in source
    assert "mock_handler" not in source


async def test_trust_env_is_false():
    c = TallyClient()
    try:
        assert c._client.trust_env is False
    finally:
        await c.close()
