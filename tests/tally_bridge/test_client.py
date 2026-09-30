"""`TallyClient.post` (TallyResponse, per-request timeout, TallyTimeoutError) and the constructor options.

`post_xml`, mock mode and `health_check` on the app's paths are covered in tests/unit/test_client.py and
tests/unit/test_tally_client_mock.py; the tests at the bottom pin that `post_xml` did not change in the merge.
"""
import httpx
import pytest

from tally_bridge import client as client_module
from tally_bridge.client import TallyClient
from tally_bridge.exceptions import TallyConnectionError, TallyResponseError, TallyTimeoutError


def _client(handler):
    return TallyClient(transport=httpx.MockTransport(handler), trust_env=False)


async def test_post_returns_text_raw_bytes_and_elapsed():
    c = _client(lambda request: httpx.Response(200, content="<E>शर्मा&#4;</E>".encode("utf-8")))
    response = await c.post("<ENVELOPE/>")
    assert response.text == "<E>शर्मा&#4;</E>"
    assert response.raw == "<E>शर्मा&#4;</E>".encode("utf-8")
    assert response.response_bytes == len(response.raw)
    assert response.elapsed_ms >= 0


async def test_request_body_is_utf8():
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, content=b"<E/>")

    await _client(handler).post("<X>शर्मा ट्रेडर्स</X>")
    assert "शर्मा ट्रेडर्स".encode("utf-8") in seen["body"]


async def test_timeout_raises_timeout_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TallyTimeoutError):
        await _client(handler).post("<X/>")


async def test_connect_timeout_raises_distinct_message():
    def handler(request):
        raise httpx.ConnectTimeout("slow to connect", request=request)

    with pytest.raises(TallyTimeoutError, match=r"Could not connect to TallyPrime at .* within 5s"):
        await _client(handler).post("<X/>")


async def test_refused_raises_connection_error_not_timeout():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TallyConnectionError) as info:
        await _client(handler).post("<X/>")
    assert not isinstance(info.value, TallyTimeoutError)


async def test_http_error_raises_response_error():
    with pytest.raises(TallyResponseError):
        await _client(lambda request: httpx.Response(500)).post("<X/>")


async def test_timeout_is_clamped_to_max():
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, content=b"<E/>")

    await _client(handler).post("<X/>", timeout=500)
    assert seen["timeout"]["read"] == client_module.MAX_TIMEOUT_S
    assert seen["timeout"]["connect"] == client_module.CONNECT_TIMEOUT_S


async def test_default_timeout():
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, content=b"<E/>")

    await _client(handler).post("<X/>")
    assert seen["timeout"]["read"] == client_module.DEFAULT_TIMEOUT_S


async def test_health_check():
    ok = _client(lambda request: httpx.Response(200, content=b"<ENVELOPE><COMPANY NAME='x'/></ENVELOPE>"))
    assert await ok.health_check() is True

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    assert await _client(down).health_check() is False


async def test_post_answers_from_the_mock_handler_in_mock_mode():
    """Was test_no_mock_mode_in_v2_client: the one client keeps the app's mock mode (merge decision M10)."""
    def handler(request):
        raise AssertionError("mock mode must not reach the network")

    c = _client(handler)
    assert c.mock_mode is False
    c.mock_mode = True
    response = await c.post("<REPORTNAME>List of Companies</REPORTNAME>")
    assert "Bharat Traders" in response.text
    assert response.raw == response.text.encode("utf-8")
    assert response.text == await c.post_xml("<REPORTNAME>List of Companies</REPORTNAME>")
    await c.close()


async def test_trust_env_false_is_an_option_and_the_default_is_unchanged():
    """Was test_trust_env_is_false: the former agent client always had it off; now its callers pass the option."""
    off, default = TallyClient(trust_env=False), TallyClient()
    try:
        assert off._client.trust_env is False
        assert default._client.trust_env is True
    finally:
        await off.close()
        await default.close()


def test_probes_build_their_client_with_trust_env_off():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel, count in (("probes/__main__.py", 2), ("probes/setup/s1_capture.py", 1)):
        calls = [line for line in (root / rel).read_text(encoding="utf-8").splitlines() if "TallyClient(" in line]
        assert len(calls) == count, rel
        assert all("trust_env=False" in line for line in calls), rel


# --- post_xml is unchanged by the merge ---------------------------------------------------------------------------------

async def test_post_xml_still_returns_str_and_sends_the_same_bytes():
    seen = {}

    def handler(request):
        seen["body"], seen["timeout"] = request.content, request.extensions["timeout"]
        return httpx.Response(200, content="<E>शर्मा</E>".encode("utf-8"))

    c = _client(handler)
    assert await c.post_xml("<X>शर्मा ट्रेडर्स</X>") == "<E>शर्मा</E>"
    assert seen["body"] == "<X>शर्मा ट्रेडर्स</X>".encode("utf-8")
    assert seen["timeout"]["read"] == 90.0 and seen["timeout"]["connect"] == 5.0


async def test_post_xml_timeout_is_still_a_plain_connection_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(TallyConnectionError, match="timed out") as info:
        await _client(handler).post_xml("<X/>")
    assert type(info.value) is TallyConnectionError


async def test_default_constructor_is_the_apps():
    c = TallyClient()
    try:
        assert c.base_url == "http://localhost:9000"
        assert c.mock_mode is False
        assert c.timeout == httpx.Timeout(90.0, connect=5.0)
    finally:
        await c.close()
