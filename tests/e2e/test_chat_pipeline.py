"""End-to-end tests for the chat pipeline using mock Claude API + mock Tally."""

import pytest
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from tally_bridge.exceptions import TallyConnectionError, TallyResponseError
from tests.mocks.mock_claude_api import (
    make_classification_response,
    make_text_response,
    make_tool_call_response,
)


@pytest.mark.asyncio
async def test_greeting_returns_canned_response(e2e_client):
    """Greeting query -> orchestrator classifies -> canned greeting returned."""
    client, set_responses, mock_clients = e2e_client

    set_responses(
        orchestrator_responses=[make_classification_response("greeting")],
    )

    with patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]):
        response = await client.post("/api/chat", json={"message": "Hello!"})

    assert response.status_code == 200
    data = response.json()
    assert data["message"].startswith("Hello!")
    assert data["session_id"]
    assert data["data"] is None
    assert data["chart"] is None


@pytest.mark.asyncio
async def test_simple_lookup_trial_balance(e2e_client):
    """Simple lookup -> classify -> query agent (tool call) -> analysis agent -> response."""
    client, set_responses, mock_clients = e2e_client

    set_responses(
        orchestrator_responses=[make_classification_response("simple_lookup")],
        query_agent_responses=[
            make_tool_call_response("get_trial_balance", {"from_date": "01-04-2025", "to_date": "31-03-2026"}),
            make_text_response("Here is the trial balance for FY 2025-26."),
        ],
        analysis_agent_responses=[
            make_text_response("Here is the trial balance for FY 2025-26."),
        ],
    )

    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
    ):
        response = await client.post("/api/chat", json={"message": "Show trial balance"})

    assert response.status_code == 200
    data = response.json()
    assert "trial balance" in data["message"].lower()
    assert data["session_id"]


@pytest.mark.asyncio
async def test_clarification_returns_question(e2e_client):
    """Ambiguous query -> orchestrator classifies as clarification_needed."""
    client, set_responses, mock_clients = e2e_client

    set_responses(
        orchestrator_responses=[
            make_classification_response(
                "clarification_needed",
                clarification_question="Which balance are you looking for? Cash, bank, or trial balance?",
            ),
        ],
    )

    with patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]):
        response = await client.post("/api/chat", json={"message": "Show me the balance"})

    assert response.status_code == 200
    data = response.json()
    assert "balance" in data["message"].lower()
    assert data["data"] is None


@pytest.mark.asyncio
async def test_simple_lookup_balance_sheet(e2e_client):
    """Balance sheet lookup -> classify -> query agent (tool call) -> analysis agent -> response."""
    client, set_responses, mock_clients = e2e_client

    set_responses(
        orchestrator_responses=[make_classification_response("simple_lookup")],
        query_agent_responses=[
            make_tool_call_response("get_balance_sheet", {"from_date": "01-04-2025", "to_date": "31-03-2026"}),
            make_text_response("Here is the balance sheet for FY 2025-26."),
        ],
        analysis_agent_responses=[
            make_text_response("Here is the balance sheet for FY 2025-26."),
        ],
    )

    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
    ):
        response = await client.post("/api/chat", json={"message": "Show balance sheet"})

    assert response.status_code == 200
    data = response.json()
    assert "balance sheet" in data["message"].lower()
    assert data["session_id"]


@pytest.mark.asyncio
async def test_comparison_with_analysis_and_chart(e2e_client):
    """Comparison query -> query agent -> analysis agent -> chart agent."""
    client, set_responses, mock_clients = e2e_client

    set_responses(
        orchestrator_responses=[make_classification_response("comparison", requires_chart=True)],
        query_agent_responses=[
            make_tool_call_response("get_profit_and_loss", {"from_date": "01-04-2025", "to_date": "30-09-2025"}),
            make_text_response("Sales in H1: 20,00,000. Purchases: 15,00,000."),
        ],
        analysis_agent_responses=[
            make_text_response(
                "Comparison analysis:\n- Sales exceeded purchases by 5,00,000\nchart_suggestion: grouped_bar"
            ),
        ],
    )

    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
    ):
        response = await client.post("/api/chat", json={"message": "Compare sales and purchases"})

    assert response.status_code == 200
    data = response.json()
    assert data["session_id"]


@pytest.mark.asyncio
async def test_multi_dataset_response_does_not_500(e2e_client):
    """Multiple tool calls producing multiple datasets should return 200, not 500.

    Regression test: ChatResponse.data typed as dict|None caused a 500 when
    the orchestrator passed a list[dict] from multiple tool results.
    Now all queries with data route through analysis agent.
    """
    client, set_responses, mock_clients = e2e_client

    # simple_lookup with two sequential tool calls -> two datasets in tool_results.
    # Analysis agent processes both datasets and returns merged result.
    set_responses(
        orchestrator_responses=[make_classification_response("simple_lookup")],
        query_agent_responses=[
            make_tool_call_response(
                "get_profit_and_loss",
                {"from_date": "01-04-2025", "to_date": "30-09-2025"},
                extra_text="Let me fetch H1 P&L first.",
            ),
            make_tool_call_response(
                "get_profit_and_loss",
                {"from_date": "01-10-2025", "to_date": "31-03-2026"},
                extra_text="Now fetching H2 P&L.",
            ),
            make_text_response("Here is the P&L for both halves of the financial year."),
        ],
        analysis_agent_responses=[
            make_text_response("Here is the P&L comparison for H1 and H2."),
        ],
    )

    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
    ):
        response = await client.post(
            "/api/chat",
            json={"message": "Show P&L for H1 and H2 of this FY"},
        )

    assert response.status_code == 200, f"Expected 200 but got {response.status_code}: {response.text}"
    data = response.json()
    assert data["message"]
    assert data["session_id"]


@pytest.mark.asyncio
async def test_top_n_query(e2e_client):
    """Top-N query -> query agent -> analysis agent -> ranked response."""
    client, set_responses, mock_clients = e2e_client

    set_responses(
        orchestrator_responses=[make_classification_response("top_n")],
        query_agent_responses=[
            make_tool_call_response("get_sales_register", {"from_date": "01-04-2025", "to_date": "31-03-2026"}),
            make_text_response("Sales data retrieved."),
        ],
        analysis_agent_responses=[
            make_text_response("Top 5 customers by sales:\n1. HCODE - 15,00,000\n2. SMARTBIKE - 10,00,000"),
        ],
    )

    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
    ):
        response = await client.post("/api/chat", json={"message": "Top 5 customers by sales"})

    assert response.status_code == 200
    data = response.json()
    assert data["message"]


@pytest.mark.asyncio
async def test_multi_turn_conversation_preserves_session(e2e_client):
    """Two sequential messages with same session_id -> context preserved."""
    client, set_responses, mock_clients = e2e_client

    # First turn
    set_responses(orchestrator_responses=[make_classification_response("greeting")])
    with patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]):
        r1 = await client.post("/api/chat", json={"message": "Hi"})

    assert r1.status_code == 200
    session_id = r1.json()["session_id"]

    # Second turn — reuse session
    set_responses(
        orchestrator_responses=[make_classification_response("simple_lookup")],
        query_agent_responses=[
            make_tool_call_response("get_trial_balance", {"from_date": "01-04-2025", "to_date": "31-03-2026"}),
            make_text_response("Here is the trial balance."),
        ],
        analysis_agent_responses=[
            make_text_response("Here is the trial balance."),
        ],
    )
    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
    ):
        r2 = await client.post("/api/chat", json={"message": "Show trial balance", "session_id": session_id})

    assert r2.status_code == 200
    assert r2.json()["session_id"] == session_id


@pytest.mark.asyncio
async def test_empty_message_returns_422(e2e_client):
    """Empty message should fail validation."""
    client, _, _ = e2e_client
    response = await client.post("/api/chat", json={"message": "   "})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_tally_connection_error_surfaced_in_response(e2e_client):
    """Tally unreachable -> tool returns error -> agent reports it in message."""
    client, set_responses, mock_clients = e2e_client

    set_responses(
        orchestrator_responses=[make_classification_response("simple_lookup")],
        query_agent_responses=[
            make_tool_call_response("get_trial_balance", {"from_date": "01-04-2025", "to_date": "31-03-2026"}),
            make_text_response("I'm unable to connect to Tally. Please check the connection."),
        ],
        analysis_agent_responses=[
            make_text_response("I'm unable to connect to Tally. Please check the connection."),
        ],
    )

    # Patch the tool handler to raise TallyConnectionError
    async def failing_handler(client, **kwargs):
        raise TallyConnectionError("Connection refused: Tally is not running")

    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
        patch("backend.agents.tools.TOOL_HANDLERS", {"get_trial_balance": failing_handler}),
    ):
        response = await client.post("/api/chat", json={"message": "Show trial balance"})

    assert response.status_code == 200
    data = response.json()
    # The agent should have received the error and reported it back
    assert data["message"]
    assert data["session_id"]


@pytest.mark.asyncio
async def test_tally_response_error_surfaced_in_response(e2e_client):
    """Invalid Tally XML -> tool returns error -> agent reports it in message."""
    client, set_responses, mock_clients = e2e_client

    set_responses(
        orchestrator_responses=[make_classification_response("simple_lookup")],
        query_agent_responses=[
            make_tool_call_response("get_balance_sheet", {"from_date": "01-04-2025", "to_date": "31-03-2026"}),
            make_text_response("Tally returned an invalid response. Please try again."),
        ],
        analysis_agent_responses=[
            make_text_response("Tally returned an invalid response. Please try again."),
        ],
    )

    async def bad_response_handler(client, **kwargs):
        raise TallyResponseError("Invalid XML: unexpected tag at line 42")

    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
        patch("backend.agents.tools.TOOL_HANDLERS", {"get_balance_sheet": bad_response_handler}),
    ):
        response = await client.post("/api/chat", json={"message": "Show balance sheet"})

    assert response.status_code == 200
    data = response.json()
    assert data["message"]
    assert data["session_id"]


@pytest.mark.asyncio
async def test_date_resolution_month_name(e2e_client):
    """Query agent calls resolve_date_range('April 2025') then fetches P&L."""
    client, set_responses, mock_clients = e2e_client

    set_responses(
        orchestrator_responses=[make_classification_response("simple_lookup")],
        query_agent_responses=[
            make_tool_call_response(
                "resolve_date_range",
                {"description": "April 2025"},
            ),
            make_tool_call_response(
                "get_profit_and_loss",
                {"from_date": "01-04-2025", "to_date": "30-04-2025"},
            ),
            make_text_response("Here is the P&L for April 2025."),
        ],
        analysis_agent_responses=[
            make_text_response("Here is the P&L for April 2025."),
        ],
    )

    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
    ):
        response = await client.post(
            "/api/chat",
            json={"message": "Show P&L for April 2025"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["message"]
    assert data["session_id"]


@pytest.mark.asyncio
async def test_graceful_tool_limit_exceeded(e2e_client):
    """Exceeding the tool call limit returns 200 with partial results, not a crash."""
    client, set_responses, mock_clients = e2e_client

    # Build 26 tool call responses to exceed the default limit of 25
    tool_calls = [
        make_tool_call_response(
            "get_trial_balance",
            {"from_date": "01-04-2025", "to_date": "31-03-2026"},
        )
        for _ in range(26)
    ]

    set_responses(
        orchestrator_responses=[make_classification_response("simple_lookup")],
        query_agent_responses=tool_calls,
        analysis_agent_responses=[
            make_text_response("Here is the trial balance data."),
        ],
    )

    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
    ):
        response = await client.post(
            "/api/chat",
            json={"message": "Show trial balance"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["message"]
    assert data["session_id"]


def test_compute_totals_string_input_no_crash():
    """compute_totals handles string inputs without crashing."""
    from backend.agents.analysis_agent import _tool_compute_totals

    result = _tool_compute_totals(["100", "200", "300"], ["value"])
    assert "error" not in result or result.get("error") is None
    # Should produce a total of 600
    assert result["records"][0]["value"] == 600.0


@pytest.mark.asyncio
async def test_trend_query_with_chart(e2e_client):
    """Trend query -> query agent -> analysis agent -> chart agent -> line chart."""
    client, set_responses, mock_clients = e2e_client

    set_responses(
        orchestrator_responses=[make_classification_response("trend", requires_chart=True)],
        query_agent_responses=[
            make_tool_call_response("get_profit_and_loss", {"from_date": "01-04-2025", "to_date": "31-03-2026"}),
            make_text_response("Monthly sales data: Apr 5L, May 6L, Jun 4L, Jul 7L, Aug 8L, Sep 5L."),
        ],
        analysis_agent_responses=[
            make_text_response(
                "Monthly sales trend analysis:\n"
                "- Sales peaked in August at 8L\n"
                "- Average monthly sales: 5.8L\n"
                "chart_suggestion: line"
            ),
        ],
    )

    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
    ):
        response = await client.post(
            "/api/chat", json={"message": "Show monthly sales trend this FY"}
        )

    assert response.status_code == 200
    data = response.json()
    assert data["message"]
    assert data["session_id"]


@pytest.mark.asyncio
async def test_chart_metadata_stripped_from_response(e2e_client):
    """Analysis agent response with Chart suggestion/Chart title lines -> stripped from final message."""
    client, set_responses, mock_clients = e2e_client

    set_responses(
        orchestrator_responses=[make_classification_response("comparison", requires_chart=True)],
        query_agent_responses=[
            make_tool_call_response("get_profit_and_loss", {"from_date": "01-04-2025", "to_date": "30-09-2025"}),
            make_text_response("Sales: 20,00,000. Purchases: 15,00,000."),
        ],
        analysis_agent_responses=[
            make_text_response(
                "Comparison analysis:\n"
                "- Sales exceeded purchases by 5,00,000\n"
                "- Profit margin is healthy at 25%\n"
                "Chart suggestion: grouped_bar\n"
                "Chart title: Sales vs Purchases H1 FY 2025-26"
            ),
        ],
    )

    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
    ):
        response = await client.post(
            "/api/chat", json={"message": "Compare sales and purchases for H1"}
        )

    assert response.status_code == 200
    data = response.json()
    msg = data["message"]
    # The analysis text should be present
    assert "comparison analysis" in msg.lower() or "sales exceeded" in msg.lower()
    # But the internal chart directives must be stripped
    assert "Chart suggestion:" not in msg
    assert "chart_suggestion:" not in msg
    assert "Chart title:" not in msg
    assert "chart_title:" not in msg


@pytest.mark.asyncio
async def test_trend_total_row_in_table_excluded_from_chart(e2e_client):
    """Trend query with compute_trend tool -> Total row in table, excluded from chart data."""
    client, set_responses, mock_clients = e2e_client

    # The analysis agent will call compute_trend, which produces real table data.
    # Then return a final text response.
    set_responses(
        orchestrator_responses=[make_classification_response("trend", requires_chart=True)],
        query_agent_responses=[
            make_tool_call_response("get_sales_register", {"from_date": "01-04-2025", "to_date": "31-03-2026"}),
            make_text_response("Monthly sales data retrieved."),
        ],
        analysis_agent_responses=[
            # First response: Claude calls compute_trend tool
            make_tool_call_response(
                "compute_trend",
                {
                    "series": [
                        {"period": "Apr", "value": 500000},
                        {"period": "May", "value": 600000},
                        {"period": "Jun", "value": 400000},
                        {"period": "Jul", "value": 700000},
                        {"period": "Aug", "value": 800000},
                    ],
                    "period_key": "period",
                    "value_key": "value",
                    "value_label": "Sales",
                },
            ),
            # Second response: final text with markdown table for chart parsing
            make_text_response(
                "Monthly sales trend:\n\n"
                "| Period | Sales | Change | Change % |\n"
                "|--------|-------|--------|----------|\n"
                "| Apr | 500000 | — | — |\n"
                "| May | 600000 | +100000 | +20.0% |\n"
                "| Jun | 400000 | -200000 | -33.3% |\n"
                "| Jul | 700000 | +300000 | +75.0% |\n"
                "| Aug | 800000 | +100000 | +14.3% |\n"
                "| Total | 3000000 | — | — |\n\n"
                "- Sales peaked in August at 8L\n"
                "- Lowest in June at 4L\n"
                "chart_suggestion: line"
            ),
        ],
    )

    chart_advice = {
        "table_index": 0,
        "x_column": "Period",
        "y_columns": ["Sales"],
        "secondary_y_columns": ["Change %"],
        "chart_type": "composed",
        "chart_title": "Monthly Sales Trend",
    }
    with (
        patch("backend.agents.orchestrator.anthropic_client", mock_clients["orchestrator"]),
        patch("backend.agents.query_agent.anthropic_client", mock_clients["query_agent"]),
        patch("backend.agents.analysis_agent.anthropic_client", mock_clients["analysis_agent"]),
        patch("backend.agents.orchestrator.get_chart_advice", new_callable=AsyncMock, return_value=(chart_advice, {"agent": "chart_advisor", "model": "test", "input_tokens": 0, "output_tokens": 0})),
    ):
        response = await client.post(
            "/api/chat", json={"message": "Show monthly sales trend this FY"}
        )

    assert response.status_code == 200
    data = response.json()
    assert data["message"]

    # Table data should include a Total row (added by _ensure_totals_row for trend queries)
    table_data = data["data"]
    assert table_data is not None, "Expected table data from compute_trend"
    assert "headers" in table_data
    assert "rows" in table_data
    rows = table_data["rows"]
    assert len(rows) >= 2, f"Expected multiple rows, got {len(rows)}"
    last_row = rows[-1]
    assert str(last_row[0]) == "Total", f"Expected last row label 'Total', got {last_row[0]!r}"

    # Chart data should NOT include a "Total" data point
    chart = data["chart"]
    assert chart is not None, "Expected chart for trend query"
    chart_labels = [point.get("label", "") for point in chart["data"]]
    assert "Total" not in chart_labels, f"Chart data should exclude Total row, got labels: {chart_labels}"


class TestChatPipelineMockTally:
    """E2E tests running the full pipeline with mock Tally mode.

    Uses a self-contained fixture (no aiohttp_server dependency) since
    mock mode doesn't need a real HTTP mock Tally server.
    """

    @pytest.fixture
    async def mock_client(self):
        """Create an async test client with mock Tally mode enabled."""
        from backend.main import app
        from tally_bridge.client import TallyClient
        from backend.agents.context import SessionStore

        tally_client = TallyClient()
        tally_client.mock_mode = True
        app.state.tally_client = tally_client
        app.state.session_store = SessionStore()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac

        tally_client.mock_mode = False
        await tally_client.close()

    @pytest.mark.asyncio
    async def test_health_in_mock_mode(self, mock_client):
        resp = await mock_client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tally_connected"] is True
        assert body["mode"] == "mock"

    @pytest.mark.asyncio
    async def test_companies_in_mock_mode(self, mock_client):
        resp = await mock_client.get("/api/companies")
        assert resp.status_code == 200
        companies = resp.json()["companies"]
        assert len(companies) >= 1

    @pytest.mark.asyncio
    async def test_tally_mode_toggle_endpoint(self, mock_client):
        # GET mode
        resp = await mock_client.get("/api/tally-mode")
        assert resp.json()["mode"] == "mock"
        # Toggle to live and back
        resp = await mock_client.post("/api/tally-mode", json={"mode": "live"})
        assert resp.json()["mode"] == "live"
        resp = await mock_client.post("/api/tally-mode", json={"mode": "mock"})
        assert resp.json()["mode"] == "mock"
