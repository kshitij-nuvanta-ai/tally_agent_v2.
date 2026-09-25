# Copied from: backend/utils/auth.py @ 9335469
# Changes: only _pwd_context + verify_password (S1 task 2 needs no hashing/token-creation — those stay in the
# current app; the cloud app only ever verifies a password the current app already hashed).
from __future__ import annotations

from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)
