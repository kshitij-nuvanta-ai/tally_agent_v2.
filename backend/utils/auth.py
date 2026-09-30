"""Authentication utilities — password hashing, validation, and JWT tokens."""
import re
from datetime import datetime, timedelta, timezone
import jwt
from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


def validate_password(password: str) -> list[str]:
    errors = []
    if len(password) < 12:
        errors.append("Password must be at least 12 characters long")
    if not re.search(r"[A-Z]", password):
        errors.append("Password must contain at least one uppercase letter")
    if not re.search(r"[a-z]", password):
        errors.append("Password must contain at least one lowercase letter")
    if not re.search(r"\d", password):
        errors.append("Password must contain at least one digit")
    if not re.search(r'[!@#$%^&*(),.?":{}|<>\-_=+\[\]\\/\'`~;]', password):
        errors.append("Password must contain at least one special character")
    return errors


def create_access_token(user_id: str, secret: str, expiry_minutes: int = 30) -> str:
    payload = {
        "sub": user_id,
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def create_refresh_token(user_id: str, secret: str, expiry_days: int = 7) -> str:
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": datetime.now(timezone.utc) + timedelta(days=expiry_days),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"])


class AccessTokenError(Exception):
    """Why a web access token was refused: ``reason`` is ``"expired"``, ``"invalid"`` (bad signature, malformed,
    ...) or ``"wrong_type"`` (a valid token that is not an access token — a refresh token, or a device token)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def decode_access_token(token: str, secret: str) -> str:
    """Check a web access JWT and return its user id. The ONE token check (v2 merge M7): ``get_current_user``
    and the sync routes' ``web_user`` both call it and each turns ``AccessTokenError`` into its own error shape.
    """
    try:
        payload = decode_token(token, secret)
    except jwt.ExpiredSignatureError as exc:
        raise AccessTokenError("expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AccessTokenError("invalid") from exc

    if payload.get("type") != "access":
        raise AccessTokenError("wrong_type")

    return payload["sub"]
