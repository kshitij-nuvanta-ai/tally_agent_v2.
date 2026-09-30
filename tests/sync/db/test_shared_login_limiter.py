"""One per-email login limiter (v2 merge M8): web login (``POST /api/auth/login``), agent login
(``POST /api/agent/auth/login``) and the relink password re-check count failures in ONE store, so five failures in
any mix lock all of them for that email. Each endpoint's own answer at the limit is what it was before the merge.

Run on the real app (``backend.main``) with a real database."""
from __future__ import annotations

from datetime import datetime, timezone

from tests.sync.conftest import TEST_EMAIL_DOMAIN, make_user, requires_db, serve_main
from tests.sync.db.ingest_helpers import B_BIND, bind

pytestmark = requires_db

PASSWORD = "Passw0rd!Passw0rd"
WEB_LIMIT = {"detail": "Too many login attempts. Try again later."}
NEW_GUID = "new-guid-0000"


async def web_login(c, email: str, password: str = PASSWORD):
    return await c.post("/api/auth/login", json={"email": email, "password": password})


async def agent_login(c, email: str, password: str = PASSWORD):
    return await c.post("/api/agent/auth/login", json={"email": email, "password": password,
                                                       "device_name": "ACCOUNTS-PC", "agent_version": "0.1.0"})


def assert_agent_limited(r) -> None:
    assert r.status_code == 429, r.text
    body = r.json()
    assert set(body) == {"error", "detail", "retry_after"}
    assert (body["error"], body["detail"]) == ("rate_limited", "Too many requests")
    assert 0 < body["retry_after"] <= 900 and r.headers["Retry-After"] == str(body["retry_after"])


def assert_web_limited(r) -> None:
    assert (r.status_code, r.json()) == (429, WEB_LIMIT)
    assert "retry-after" not in r.headers


async def test_web_failures_count_against_agent_login(engine, session, monkeypatch):
    email = f"web-to-agent{TEST_EMAIL_DOMAIN}"
    await make_user(session, email=email, password=PASSWORD)
    async with serve_main(monkeypatch) as c:
        for _ in range(5):
            r = await web_login(c, email, "wrong-password")
            assert (r.status_code, r.json()) == (401, {"detail": "Invalid email or password"})
        assert_agent_limited(await agent_login(c, email))             # the right password, still locked out
        assert_web_limited(await web_login(c, email))


async def test_agent_failures_count_against_web_login(engine, session, monkeypatch):
    email = f"agent-to-web{TEST_EMAIL_DOMAIN}"
    await make_user(session, email=email, password=PASSWORD)
    async with serve_main(monkeypatch) as c:
        for _ in range(5):
            r = await agent_login(c, email, "wrong-password")
            assert (r.status_code, r.json()) == (401, {"error": "invalid_credentials", "detail": ""})
        assert_web_limited(await web_login(c, email))                 # the right password, still locked out
        assert_agent_limited(await agent_login(c, email))


async def test_a_mix_of_failures_shares_one_budget_per_email(engine, session, monkeypatch):
    email, other = f"mixed{TEST_EMAIL_DOMAIN}", f"mixed-other{TEST_EMAIL_DOMAIN}"
    await make_user(session, email=email, password=PASSWORD)
    await make_user(session, email=other, password=PASSWORD)
    async with serve_main(monkeypatch) as c:
        for login in (web_login, agent_login, web_login, agent_login):
            assert (await login(c, email, "wrong-password")).status_code == 401
        # four failures: both still open, and a success does not reset the count on either endpoint
        assert (await web_login(c, email)).status_code == 200
        assert (await agent_login(c, email)).status_code == 200
        # the key is the normalised email on both: this fifth failure is the same bucket
        assert (await agent_login(c, f"  {email.upper()} ", "wrong-password")).status_code == 401
        assert_web_limited(await web_login(c, email))
        assert_agent_limited(await agent_login(c, email))
        # per email: another user is not affected
        assert (await web_login(c, other)).status_code == 200
        assert (await agent_login(c, other)).status_code == 200


async def test_failures_for_an_unknown_email_are_counted_on_both(engine, monkeypatch):
    """As before on each endpoint: a failed attempt is recorded whether or not the account exists."""
    email = f"nobody{TEST_EMAIL_DOMAIN}"
    async with serve_main(monkeypatch) as c:
        for login in (web_login, agent_login, web_login, agent_login, web_login):
            assert (await login(c, email)).status_code == 401
        assert_web_limited(await web_login(c, email))
        assert_agent_limited(await agent_login(c, email))


async def test_each_endpoints_own_answers_below_the_limit_are_unchanged(engine, session, monkeypatch):
    email = f"shapes{TEST_EMAIL_DOMAIN}"
    uid = await make_user(session, email=email, password=PASSWORD)
    inactive = f"inactive{TEST_EMAIL_DOMAIN}"
    await make_user(session, email=inactive, password=PASSWORD, is_active=False)
    async with serve_main(monkeypatch) as c:
        r = await web_login(c, email)
        assert r.status_code == 200 and set(r.json()) == {"user", "access_token"}
        assert r.json()["user"] == {"id": str(uid), "email": email, "name": "Owner"}
        assert "refresh_token=" in r.headers["set-cookie"]
        r = await agent_login(c, email)
        assert r.status_code == 200
        assert set(r.json()) == {"device_id", "access_token", "expires_in", "refresh_token", "user"}
        # a deactivated account is refused without a hit being recorded, on both (six tries, never a 429)
        for _ in range(6):
            r = await web_login(c, inactive)
            assert (r.status_code, r.json()) == (403, {"detail": "Account is deactivated"})
            r = await agent_login(c, inactive)
            assert (r.status_code, r.json()) == (403, {"error": "account_inactive", "detail": ""})


async def _prompt_relink(c, ws, headers) -> None:
    r = await c.post(f"/api/sync/{ws}/heartbeat", headers=headers, json={
        "tally_status": "other_company_same_name", "pc_clock": datetime.now(timezone.utc).isoformat(),
        "seen_company": {"guid": NEW_GUID, "name": B_BIND["company_name"]}})
    assert r.status_code == 200, r.text


async def _relink(c, ws, headers, password: str):
    return await c.post(f"/api/sync/{ws}/relink", headers=headers, json={
        "new_company_guid": NEW_GUID, "company_name": B_BIND["company_name"], "password": password})


async def test_web_failures_count_against_the_relink_password_recheck(engine, session, monkeypatch):
    async with serve_main(monkeypatch) as c:
        uid, ws, headers = await bind(c, session)
        email = (await session.execute(_email_of(), {"i": uid})).scalar_one()
        await _prompt_relink(c, ws, headers)
        for _ in range(5):
            assert (await web_login(c, email, "wrong-password")).status_code == 401
        assert_agent_limited(await _relink(c, ws, headers, PASSWORD))      # right password, locked out


async def test_relink_failures_count_against_web_login(engine, session, monkeypatch):
    async with serve_main(monkeypatch) as c:
        uid, ws, headers = await bind(c, session)
        email = (await session.execute(_email_of(), {"i": uid})).scalar_one()
        await _prompt_relink(c, ws, headers)
        for _ in range(5):
            r = await _relink(c, ws, headers, "wrong-password")
            assert (r.status_code, r.json()["error"]) == (401, "invalid_credentials")
        assert_web_limited(await web_login(c, email))


def _email_of():
    from sqlalchemy import text

    return text("SELECT email FROM users WHERE id = :i")
