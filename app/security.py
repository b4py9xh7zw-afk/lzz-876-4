"""Password hashing (PBKDF2-HMAC-SHA256 from stdlib) and signed-cookie auth."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Role, User

SESSION_COOKIE = "compliance_session"
SESSION_MAX_AGE = 8 * 3600
_SECRET = os.environ.get(
    "COMPLIANCE_SESSION_SECRET",
    "dev-insecure-secret-change-me-please-0123456789",
)
serializer = URLSafeTimedSerializer(_SECRET, salt="compliance-auth")

PBKDF2_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def create_session_token(user_id: int) -> str:
    return serializer.dumps({"uid": user_id})


def read_session_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    user = db.get(User, data.get("uid"))
    if user is None or not user.is_active:
        return None
    request.state.now = datetime.utcnow()
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    return read_session_user(request, db)


def require_login(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


def require_role(*roles: Role):
    allowed = set(roles)

    def checker(user: User = Depends(require_login)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=403,
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        return user

    return checker
