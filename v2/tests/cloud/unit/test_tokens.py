import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from v2.cloud.auth.device_tokens import decode_access, hash_refresh, mint_access, new_refresh
from v2.cloud.errors import ApiError

S = "d" * 32
NOW = datetime(2026, 9, 25, 6, 30, tzinfo=timezone.utc)
D, U, W = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_access_round_trip_carries_ws():
    c = decode_access(mint_access(D, U, W, secret=S, minutes=15, now=NOW), secret=S, now=NOW)
    assert (c.device_id, c.user_id, c.workspace_id) == (D, U, W) and c.jti


def test_access_expires_after_15_minutes():
    tok = mint_access(D, U, None, secret=S, minutes=15, now=NOW)
    with pytest.raises(ApiError) as e:
        decode_access(tok, secret=S, now=NOW + timedelta(minutes=15, seconds=1))
    assert e.value.code == "token_expired"


@pytest.mark.parametrize("claims", [
    {"sub": str(U), "type": "access"},                            # a web access token
    {"sub": str(D), "uid": str(U), "ws": None, "typ": "v2_other"},
])
def test_wrong_typ_is_invalid(claims):
    claims = {**claims, "exp": int((NOW + timedelta(minutes=5)).timestamp()), "iat": int(NOW.timestamp())}
    with pytest.raises(ApiError) as e:
        decode_access(jwt.encode(claims, S, algorithm="HS256"), secret=S, now=NOW)
    assert e.value.code == "token_invalid"


def test_web_secret_cannot_sign_a_device_token():
    tok = mint_access(D, U, W, secret="w" * 32, minutes=15, now=NOW)
    with pytest.raises(ApiError) as e:
        decode_access(tok, secret=S, now=NOW)
    assert e.value.code == "token_invalid"


def test_refresh_is_random_and_only_its_hash_is_kept():
    (t1, h1), (t2, h2) = new_refresh(), new_refresh()
    assert t1 != t2 and h1 == hash_refresh(t1) and len(h1) == 64 and t1 not in h1


def test_access_valid_when_minted_with_clock_ahead_of_real_wall_time():
    """Task 7 review, Minor 1: `mint_access` stamps `iat` from the injected clock; `decode_access` must
    validate it against that SAME injected clock, not PyJWT's own real-wall-clock check (which would otherwise
    raise `ImmatureSignatureError` whenever the injected clock is set ahead of the actual real time — as any
    fixed-clock test that advances into the future does)."""
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)  # certainly ahead of real wall-clock time
    tok = mint_access(D, U, W, secret=S, minutes=15, now=far_future)
    c = decode_access(tok, secret=S, now=far_future)
    assert (c.device_id, c.user_id, c.workspace_id) == (D, U, W)


def test_access_not_yet_valid_per_injected_clock_is_invalid():
    """The flip side: `now` before the token's own `iat` (per the injected clock) is still rejected — the fix
    only stops PyJWT comparing `iat` against the wrong (real) clock, it doesn't drop the check."""
    tok = mint_access(D, U, W, secret=S, minutes=15, now=NOW)
    with pytest.raises(ApiError) as e:
        decode_access(tok, secret=S, now=NOW - timedelta(seconds=1))
    assert e.value.code == "token_invalid"
