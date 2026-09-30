"""E2E smoke test — DB mode (auth + persistence). Requires TEST_DATABASE_URL."""
import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set — skipping DB E2E tests",
)

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest_asyncio.fixture
async def db_app(monkeypatch):
    """
    Stand up a full FastAPI app in DB mode for one test.

    We deliberately AVOID ``importlib.reload`` here. Reloading
    ``backend.config`` / ``backend.api.*`` rebinds module-level objects
    (``settings``, ``get_client``, the routers' ``Depends`` targets) to NEW
    identities. Other already-imported test modules keep references to the
    ORIGINAL objects, so their ``dependency_overrides`` (keyed by identity) and
    ``monkeypatch.setattr(settings, ...)`` silently stop matching — which is
    exactly the cross-file contamination that broke ``test_fx_upload_flow.py``.

    Instead we mutate the shared ``settings`` singleton in place (every module
    that did ``from backend.config import settings`` sees it, because
    ``db_mode`` is a live-computed property of ``DATABASE_URL``) and assemble a
    fresh ``FastAPI`` app with the routers included explicitly — the same
    no-reload pattern the other integration suites use.

    Because httpx ASGITransport does NOT trigger ASGI lifespan events, we
    manually wire up app.state (tally_client, session_store) and call
    init_engine() ourselves — exactly what the lifespan would do.
    """
    from backend.config import settings

    # 1. Flip the shared singleton into DB mode (restored automatically by
    #    monkeypatch in teardown). db_mode is derived live from DATABASE_URL.
    monkeypatch.setattr(settings, "DATABASE_URL", _TEST_DB_URL)
    monkeypatch.setattr(settings, "JWT_SECRET", "a" * 64)
    monkeypatch.setattr(settings, "TALLY_MODE", "mock")
    monkeypatch.setattr(
        settings, "ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "test-key")
    )

    # 2. Create test DB tables
    from backend.db.models import Base
    setup_engine = create_async_engine(_TEST_DB_URL)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await setup_engine.dispose()

    # 3. Assemble a fresh app with all routers (DB-mode routers included
    #    unconditionally — auth enforcement is decided at request time by
    #    get_current_user reading settings.db_mode).
    from fastapi import FastAPI

    from backend.api import (
        auth,
        chat,
        companies,
        conversations,
        health,
        reports,
        tally_mode,
        usage,
        workspaces,
    )

    app = FastAPI()
    for module in (chat, health, companies, reports, tally_mode,
                   auth, workspaces, conversations, usage):
        app.include_router(module.router, prefix="/api")

    # 4. Manually initialise what lifespan would do (ASGITransport skips lifespan)
    from backend.agents.context import SessionStore
    from backend.tally_bridge.client import TallyClient
    from backend.db.engine import init_engine

    tally_client = TallyClient(host="localhost", port=9000)
    tally_client.mock_mode = True
    app.state.tally_client = tally_client
    app.state.session_store = SessionStore(ttl_minutes=60)
    init_engine(_TEST_DB_URL)

    # Reset auth's in-memory rate-limit state. The old reload-based fixture
    # reset these implicitly by reloading backend.api.auth; without the reload
    # we must clear them explicitly so per-IP register/login throttling doesn't
    # accumulate across tests (every test registers from the same test client IP).
    auth._register_attempts.clear()

    yield app

    # 5. Teardown — close engine + drop tables. settings restored by monkeypatch.
    auth._register_attempts.clear()
    await tally_client.close()
    from backend.db.engine import close_engine
    await close_engine()

    teardown_engine = create_async_engine(_TEST_DB_URL)
    async with teardown_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await teardown_engine.dispose()


async def test_db_health_requires_auth(db_app):
    """In DB mode /api/health requires auth (commit 8478d5a) — unauthenticated → 401."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        resp = await ac.get("/api/health")
    assert resp.status_code == 401


async def test_db_chat_requires_auth(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        resp = await ac.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 401


async def test_db_register_login_flow(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # Register
        resp = await ac.post("/api/auth/register", json={
            "email": "smoke@example.com", "password": "Str0ng!Pass#99", "name": "Smoke Test",
        })
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        assert token

        # /me with token
        resp = await ac.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["email"] == "smoke@example.com"

        # Login
        resp = await ac.post("/api/auth/login", json={
            "email": "smoke@example.com", "password": "Str0ng!Pass#99",
        })
        assert resp.status_code == 200
        assert resp.json()["access_token"]


async def test_db_full_chat_flow(db_app):
    """Register → create workspace → create conversation → chat → verify persistence."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # Register
        resp = await ac.post("/api/auth/register", json={
            "email": "fullflow@example.com", "password": "Str0ng!Pass#99", "name": "Full Flow",
        })
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Create workspace
        resp = await ac.post("/api/workspaces", json={
            "name": "Test Co",
            "config": {"tally_host": "localhost", "tally_port": 9000, "mock_mode": True},
        }, headers=headers)
        assert resp.status_code == 201, resp.text
        workspace_id = resp.json()["id"]

        # Create conversation
        resp = await ac.post(f"/api/workspaces/{workspace_id}/conversations", json={}, headers=headers)
        assert resp.status_code == 201, resp.text
        conv_id = resp.json()["id"]

        # Send chat message
        resp = await ac.post("/api/chat", json={
            "message": "hello",
            "workspace_id": workspace_id,
            "conversation_id": conv_id,
        }, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"]

        # Verify conversation has messages
        resp = await ac.get(f"/api/workspaces/{workspace_id}/conversations/{conv_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        messages = resp.json()["messages"]
        assert len(messages) >= 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"
        assert messages[1]["role"] == "assistant"

        # Verify title auto-generated
        assert resp.json()["title"] == "hello"

        # Verify usage was logged
        resp = await ac.get("/api/usage", headers=headers)
        assert resp.status_code == 200
        usage = resp.json()
        assert usage["total_input_tokens"] >= 0
        assert usage["total_output_tokens"] >= 0
        assert isinstance(usage["by_workspace"], list)
        assert isinstance(usage["by_day"], list)


@pytest.mark.asyncio
async def test_db_usage_endpoint(db_app):
    """Verify usage endpoint returns correct structure."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # Register
        resp = await ac.post("/api/auth/register", json={
            "email": "usage@example.com", "password": "Str0ng!Pass#99", "name": "Usage Test",
        })
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Empty usage before any chat
        resp = await ac.get("/api/usage", headers=headers)
        assert resp.status_code == 200
        usage = resp.json()
        assert usage["total_input_tokens"] == 0
        assert usage["total_output_tokens"] == 0
        assert usage["total_cost_usd"] == 0
        assert usage["by_workspace"] == []
        assert usage["by_day"] == []


async def test_db_weak_password_rejected(db_app):
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        resp = await ac.post("/api/auth/register", json={
            "email": "weak@example.com", "password": "weak", "name": "Weak Pass",
        })
    assert resp.status_code == 422


async def test_db_workspace_isolation(db_app):
    """Users can only see their own workspaces."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # User 1
        resp = await ac.post("/api/auth/register", json={
            "email": "user1@example.com", "password": "Str0ng!Pass#99", "name": "User 1",
        })
        assert resp.status_code == 200, resp.text
        token1 = resp.json()["access_token"]
        await ac.post("/api/workspaces", json={"name": "User1 Co"},
                      headers={"Authorization": f"Bearer {token1}"})

        # User 2
        resp = await ac.post("/api/auth/register", json={
            "email": "user2@example.com", "password": "Str0ng!Pass#99", "name": "User 2",
        })
        assert resp.status_code == 200, resp.text
        token2 = resp.json()["access_token"]

        # User 2 should see 0 workspaces
        resp = await ac.get("/api/workspaces", headers={"Authorization": f"Bearer {token2}"})
        assert resp.status_code == 200
        assert len(resp.json()) == 0


@pytest.mark.asyncio
async def test_db_token_refresh_flow(db_app):
    """Register → get access token → refresh via cookie → use new token to call /me."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # Register — response sets a refresh_token cookie (secure=True, so httpx won't auto-send
        # it over http://test). We extract it manually to simulate browser cookie jar behaviour.
        resp = await ac.post("/api/auth/register", json={
            "email": "refresh@example.com", "password": "Str0ng!Pass#99", "name": "Refresh User",
        })
        assert resp.status_code == 200, resp.text
        original_token = resp.json()["access_token"]
        assert original_token

        # Extract the refresh_token cookie value from the Set-Cookie header
        refresh_cookie = resp.cookies.get("refresh_token")
        assert refresh_cookie, "register must set a refresh_token cookie"

        # Call /refresh — pass the cookie explicitly because httpx won't send secure cookies
        # over a non-HTTPS base_url
        resp = await ac.post("/api/auth/refresh", cookies={"refresh_token": refresh_cookie})
        assert resp.status_code == 200, resp.text
        new_token = resp.json()["access_token"]
        assert new_token  # a valid access token was issued

        # Use new token to call /me
        resp = await ac.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"})
        assert resp.status_code == 200
        assert resp.json()["email"] == "refresh@example.com"


@pytest.mark.asyncio
async def test_db_logout_clears_session(db_app):
    """Register → login → logout → refresh should fail with 401."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # Register and grab the refresh token cookie
        resp = await ac.post("/api/auth/register", json={
            "email": "logout@example.com", "password": "Str0ng!Pass#99", "name": "Logout User",
        })
        assert resp.status_code == 200, resp.text
        refresh_cookie = resp.cookies.get("refresh_token")
        assert refresh_cookie, "register must set a refresh_token cookie"

        # Verify refresh works before logout (pass cookie explicitly)
        resp = await ac.post("/api/auth/refresh", cookies={"refresh_token": refresh_cookie})
        assert resp.status_code == 200, resp.text

        # Logout — clears the refresh_token cookie server-side
        resp = await ac.post("/api/auth/logout")
        assert resp.status_code == 200

        # Refresh should now fail with 401 — send no cookie (simulating cleared state)
        resp = await ac.post("/api/auth/refresh")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_db_login_rate_limiting(db_app):
    """5 failed login attempts → 6th attempt returns 429."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # Register a valid user first
        await ac.post("/api/auth/register", json={
            "email": "ratelimit@example.com", "password": "Str0ng!Pass#99", "name": "Rate Limit",
        })

        email = "ratelimit@example.com"
        # Make 5 failed login attempts with wrong password
        for _ in range(5):
            resp = await ac.post("/api/auth/login", json={
                "email": email, "password": "WrongPassword!1",
            })
            assert resp.status_code == 401

        # 6th attempt should be rate-limited
        resp = await ac.post("/api/auth/login", json={
            "email": email, "password": "WrongPassword!1",
        })
        assert resp.status_code == 429
        assert resp.json() == {"detail": "Too many login attempts. Try again later."}
        assert "retry-after" not in resp.headers
    # No clean-up: the login limiter lives on this test's own app (one store per app, v2 merge M8).


@pytest.mark.asyncio
async def test_db_duplicate_email_rejected(db_app):
    """Registering with an already-used email returns 409."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        payload = {"email": "dupe@example.com", "password": "Str0ng!Pass#99", "name": "Dupe User"}

        resp = await ac.post("/api/auth/register", json=payload)
        assert resp.status_code == 200, resp.text

        # Second registration with same email
        resp = await ac.post("/api/auth/register", json=payload)
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_db_invalid_token_rejected(db_app):
    """Sending a garbage bearer token returns 401."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        resp = await ac.get("/api/auth/me", headers={"Authorization": "Bearer garbage-token"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_db_workspace_crud(db_app):
    """Register → create workspace → list → update name → verify → delete → list empty."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # Register
        resp = await ac.post("/api/auth/register", json={
            "email": "wscrud@example.com", "password": "Str0ng!Pass#99", "name": "WS CRUD",
        })
        assert resp.status_code == 200, resp.text
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        # Create workspace
        resp = await ac.post("/api/workspaces", json={"name": "Original Name"}, headers=headers)
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["id"]
        assert resp.json()["name"] == "Original Name"

        # List — should have 1
        resp = await ac.get("/api/workspaces", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # Update name
        resp = await ac.patch(f"/api/workspaces/{ws_id}", json={"name": "Updated Name"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Name"

        # Verify updated name via list
        resp = await ac.get("/api/workspaces", headers=headers)
        assert resp.json()[0]["name"] == "Updated Name"

        # Delete workspace
        resp = await ac.delete(f"/api/workspaces/{ws_id}", headers=headers)
        assert resp.status_code == 204

        # List — should have 0
        resp = await ac.get("/api/workspaces", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 0


@pytest.mark.asyncio
async def test_db_conversation_crud(db_app):
    """Register → create workspace → create conversation → list → update title → verify → delete → list empty."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # Register
        resp = await ac.post("/api/auth/register", json={
            "email": "convcrud@example.com", "password": "Str0ng!Pass#99", "name": "Conv CRUD",
        })
        assert resp.status_code == 200, resp.text
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        # Create workspace
        resp = await ac.post("/api/workspaces", json={"name": "Conv Test Co"}, headers=headers)
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["id"]

        # Create conversation with a title
        resp = await ac.post(
            f"/api/workspaces/{ws_id}/conversations",
            json={"title": "Initial Title"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        conv_id = resp.json()["id"]
        assert resp.json()["title"] == "Initial Title"

        # List conversations — should have 1
        resp = await ac.get(f"/api/workspaces/{ws_id}/conversations", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # Update title
        resp = await ac.patch(
            f"/api/workspaces/{ws_id}/conversations/{conv_id}",
            json={"title": "Updated Title"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Updated Title"

        # Verify updated title via list
        resp = await ac.get(f"/api/workspaces/{ws_id}/conversations", headers=headers)
        assert resp.json()[0]["title"] == "Updated Title"

        # Delete conversation
        resp = await ac.delete(f"/api/workspaces/{ws_id}/conversations/{conv_id}", headers=headers)
        assert resp.status_code == 204

        # List — should have 0
        resp = await ac.get(f"/api/workspaces/{ws_id}/conversations", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 0


@pytest.mark.asyncio
async def test_db_conversation_rename_reflected_in_detail(db_app):
    """PATCH a conversation title → GET .../conversations/{id} (detail) returns the new title."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # Register
        resp = await ac.post("/api/auth/register", json={
            "email": "convrename@example.com", "password": "Str0ng!Pass#99", "name": "Conv Rename",
        })
        assert resp.status_code == 200, resp.text
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        # Create workspace
        resp = await ac.post("/api/workspaces", json={"name": "Rename Co"}, headers=headers)
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["id"]

        # Create conversation with a title
        resp = await ac.post(
            f"/api/workspaces/{ws_id}/conversations",
            json={"title": "Before Rename"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        conv_id = resp.json()["id"]

        # Rename
        resp = await ac.patch(
            f"/api/workspaces/{ws_id}/conversations/{conv_id}",
            json={"title": "After Rename"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "After Rename"

        # The detail endpoint (GET .../conversations/{id}) must reflect the new title
        resp = await ac.get(
            f"/api/workspaces/{ws_id}/conversations/{conv_id}", headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "After Rename"


@pytest.mark.asyncio
async def test_db_conversation_delete_then_detail_404(db_app):
    """DELETE a conversation → 204; subsequent GET .../conversations/{id} returns 404."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # Register
        resp = await ac.post("/api/auth/register", json={
            "email": "convdel404@example.com", "password": "Str0ng!Pass#99", "name": "Conv Del",
        })
        assert resp.status_code == 200, resp.text
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        # Create workspace + conversation
        resp = await ac.post("/api/workspaces", json={"name": "Del Co"}, headers=headers)
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["id"]
        resp = await ac.post(
            f"/api/workspaces/{ws_id}/conversations",
            json={"title": "Doomed"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        conv_id = resp.json()["id"]

        # Delete → 204
        resp = await ac.delete(
            f"/api/workspaces/{ws_id}/conversations/{conv_id}", headers=headers
        )
        assert resp.status_code == 204

        # Detail endpoint now 404 (soft-deleted rows are filtered out)
        resp = await ac.get(
            f"/api/workspaces/{ws_id}/conversations/{conv_id}", headers=headers
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_db_conversation_delete_scoped_to_owner(db_app):
    """User 2 cannot DELETE User 1's conversation → 404 (not found / not owned), and it survives."""
    async with AsyncClient(transport=ASGITransport(app=db_app), base_url="http://test") as ac:
        # User 1 — owns the workspace + conversation
        resp = await ac.post("/api/auth/register", json={
            "email": "convowner1@example.com", "password": "Str0ng!Pass#99", "name": "Owner 1",
        })
        assert resp.status_code == 200, resp.text
        headers1 = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        resp = await ac.post("/api/workspaces", json={"name": "Owner1 Co"}, headers=headers1)
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["id"]
        resp = await ac.post(
            f"/api/workspaces/{ws_id}/conversations",
            json={"title": "Private Conv"},
            headers=headers1,
        )
        assert resp.status_code == 201, resp.text
        conv_id = resp.json()["id"]

        # User 2 — a different, unrelated user
        resp = await ac.post("/api/auth/register", json={
            "email": "convowner2@example.com", "password": "Str0ng!Pass#99", "name": "Owner 2",
        })
        assert resp.status_code == 200, resp.text
        headers2 = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        # User 2 attempts to delete User 1's conversation → 404 (workspace not owned)
        resp = await ac.delete(
            f"/api/workspaces/{ws_id}/conversations/{conv_id}", headers=headers2
        )
        assert resp.status_code == 404

        # The conversation still exists for its owner
        resp = await ac.get(
            f"/api/workspaces/{ws_id}/conversations/{conv_id}", headers=headers1
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "Private Conv"
