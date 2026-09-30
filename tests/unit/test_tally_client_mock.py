"""Tests for TallyClient mock_mode switching."""

import pytest
from tally_bridge.client import TallyClient


class TestTallyClientMockMode:
    def test_mock_mode_defaults_to_false(self):
        client = TallyClient()
        assert client.mock_mode is False

    def test_mock_mode_can_be_set(self):
        client = TallyClient()
        client.mock_mode = True
        assert client.mock_mode is True

    @pytest.mark.asyncio
    async def test_post_xml_mock_mode_returns_fixture(self):
        client = TallyClient()
        client.mock_mode = True
        xml = '<REPORTNAME>List of Companies</REPORTNAME>'
        result = await client.post_xml(xml)
        assert "Bharat Traders" in result
        await client.close()

    @pytest.mark.asyncio
    async def test_post_xml_mock_mode_unknown_request(self):
        client = TallyClient()
        client.mock_mode = True
        xml = '<REPORTNAME>NonexistentReport</REPORTNAME>'
        result = await client.post_xml(xml)
        assert "Unknown" in result
        await client.close()

    @pytest.mark.asyncio
    async def test_health_check_mock_mode_returns_true(self):
        client = TallyClient()
        client.mock_mode = True
        result = await client.health_check()
        assert result is True
        await client.close()
