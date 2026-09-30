import pytest
from tally_bridge.client import TallyClient
from tally_bridge.exceptions import TallyConnectionError


@pytest.fixture
def client():
    return TallyClient(host="localhost", port=9000)


def test_client_base_url(client):
    assert client.base_url == "http://localhost:9000"


def test_client_custom_host_port():
    c = TallyClient(host="192.168.1.100", port=9001)
    assert c.base_url == "http://192.168.1.100:9001"


@pytest.mark.asyncio
async def test_post_xml_connection_error():
    c = TallyClient(host="localhost", port=19999)
    with pytest.raises(TallyConnectionError):
        await c.post_xml("<ENVELOPE></ENVELOPE>")


@pytest.mark.asyncio
async def test_health_check_returns_false_when_unreachable():
    c = TallyClient(host="localhost", port=19999)
    result = await c.health_check()
    assert result is False


@pytest.mark.asyncio
async def test_transport_error_raises_connection_error():
    """Issue #9: httpx.TransportError variants (ReadError, etc.) must be caught."""
    from unittest.mock import AsyncMock, patch
    import httpx

    c = TallyClient(host="localhost", port=9000)

    mock_client_instance = AsyncMock()
    mock_client_instance.post = AsyncMock(
        side_effect=httpx.ReadError("Connection reset by peer")
    )

    with patch.object(c, "_client", mock_client_instance):
        with pytest.raises(TallyConnectionError, match="Transport error"):
            await c.post_xml("<ENVELOPE></ENVELOPE>")


def test_client_reuses_http_client():
    """Issue #19: TallyClient should create httpx.AsyncClient once and reuse it."""
    import httpx
    c = TallyClient(host="localhost", port=9000)
    assert hasattr(c, "_client")
    assert isinstance(c._client, httpx.AsyncClient)


@pytest.mark.asyncio
async def test_client_close_method_exists():
    """Issue #19: TallyClient should have an async close() method."""
    import asyncio
    c = TallyClient(host="localhost", port=9000)
    assert hasattr(c, "close")
    assert asyncio.iscoroutinefunction(c.close)
    await c.close()
