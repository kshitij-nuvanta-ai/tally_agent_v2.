"""Tests for code review fixes."""
import time
import pytest


class TestRegisterRateLimit:
    """I1: Register rate limiting."""

    def test_register_rate_limit_blocks_after_max(self):
        from backend.api.auth import _check_register_rate_limit, _register_attempts, _REGISTER_RATE_LIMIT_MAX
        ip = "192.168.1.100"
        _register_attempts[ip] = [time.time() for _ in range(_REGISTER_RATE_LIMIT_MAX)]
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _check_register_rate_limit(ip)
        assert exc_info.value.status_code == 429
        del _register_attempts[ip]

    def test_register_rate_limit_allows_under_threshold(self):
        from backend.api.auth import _check_register_rate_limit, _register_attempts
        ip = "192.168.1.101"
        _register_attempts[ip] = [time.time()]
        _check_register_rate_limit(ip)  # Should not raise
        del _register_attempts[ip]

    def test_register_rate_limit_expired_entries_ignored(self):
        from backend.api.auth import _check_register_rate_limit, _register_attempts, _REGISTER_RATE_LIMIT_MAX
        ip = "192.168.1.102"
        # All entries expired (2 hours ago)
        _register_attempts[ip] = [time.time() - 7200 for _ in range(_REGISTER_RATE_LIMIT_MAX)]
        _check_register_rate_limit(ip)  # Should not raise — expired entries pruned
        del _register_attempts[ip]


class TestLoginRateLimitCleanup:
    """S4: Rate limit memory leak prevention."""

    def test_login_rate_limit_cleanup_on_threshold(self):
        # The store is the app's one login limiter (shared with agent login, v2 merge M8), not a dict in
        # backend.api.auth any more; the cleanup rule and its threshold (1000) are the same.
        from backend.api.auth import _check_rate_limit
        from backend.sync.clock import SystemClock
        from backend.utils.rate_limit import DEFAULT_CLEANUP_THRESHOLD, SlidingWindow
        assert DEFAULT_CLEANUP_THRESHOLD == 1000
        limiter = SlidingWindow(5, 15 * 60, SystemClock())
        # Fill with expired entries
        old_time = time.time() - 3600  # 1 hour ago (beyond 15 min window)
        for i in range(DEFAULT_CLEANUP_THRESHOLD + 10):
            limiter._hits[f"expired-{i}@test.com"] = [old_time]
        # This call should trigger cleanup
        _check_rate_limit(limiter, "new@test.com")
        # Expired entries should be pruned
        assert len(limiter._hits) < DEFAULT_CLEANUP_THRESHOLD


class TestLoginRequestEmailValidation:
    """I3: Email normalization on login."""

    def test_login_email_normalized(self):
        from backend.api.models import LoginRequest
        req = LoginRequest(email="  Test@Example.COM  ", password="test")
        assert req.email == "test@example.com"

    def test_login_email_invalid_raises(self):
        from backend.api.models import LoginRequest
        with pytest.raises(Exception):  # Pydantic validation error
            LoginRequest(email="notanemail", password="test")


class TestOrchestratorUsage:
    """C1: Orchestrator returns usage key."""

    @pytest.mark.asyncio
    async def test_greeting_result_has_usage_key(self):
        """Verify greeting response includes usage list."""
        from unittest.mock import AsyncMock, patch, MagicMock
        from backend.agents.orchestrator import Orchestrator
        from backend.agents.context import SessionContext
        from backend.tally_bridge.client import TallyClient

        orchestrator = Orchestrator()
        session = SessionContext(company="Test")
        client = MagicMock(spec=TallyClient)

        # Mock the classifier to return greeting
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text='{"query_type": "greeting"}')]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)

        with patch("backend.agents.orchestrator.anthropic_client") as mock_client:
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            result = await orchestrator.process_query("hello", client, session)

        assert "usage" in result
        assert isinstance(result["usage"], list)
        # Should have classifier usage
        assert len(result["usage"]) == 1
        assert result["usage"][0]["agent"] == "classifier"
        assert result["usage"][0]["input_tokens"] == 10
        assert result["usage"][0]["output_tokens"] == 5


class TestChartAdvisorUsageTuple:
    """C1: Chart advisor returns (advice, usage) tuple."""

    @pytest.mark.asyncio
    async def test_chart_advisor_returns_tuple(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        from backend.agents.chart_advisor import get_chart_advice

        tables = [{"headers": ["Month", "Revenue"], "rows": [["Jan", "100"], ["Feb", "200"]]}]

        mock_response = MagicMock()
        mock_tool_block = MagicMock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.input = {
            "table_index": 0,
            "x_column": "Month",
            "y_columns": ["Revenue"],
            "chart_type": "bar",
            "chart_title": "Revenue by Month",
        }
        mock_response.content = [mock_tool_block]
        mock_response.usage = MagicMock(input_tokens=50, output_tokens=20)

        with patch("backend.agents.chart_advisor.anthropic_client") as mock_client:
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            result = await get_chart_advice(tables, "show revenue by month")

        assert isinstance(result, tuple)
        advice, usage = result
        assert advice is not None
        assert advice["chart_type"] == "bar"
        assert usage is not None
        assert usage["agent"] == "chart_advisor"
        assert usage["input_tokens"] == 50

    @pytest.mark.asyncio
    async def test_chart_advisor_no_tables_returns_none_tuple(self):
        from backend.agents.chart_advisor import get_chart_advice

        result = await get_chart_advice([], "test")
        assert result == (None, None)


class TestCorsOrigins:
    """I2: Configurable CORS origins."""

    def test_cors_origins_setting_exists(self):
        from backend.config import Settings
        s = Settings(ANTHROPIC_API_KEY="test")
        assert hasattr(s, "CORS_ORIGINS")
        assert s.CORS_ORIGINS == ""  # default empty = allow all
