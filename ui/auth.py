"""
ui/auth.py — JWT 認證工具
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import settings

_bearer = HTTPBearer(auto_error=False)


def verify_password(plain: str) -> bool:
    if not settings.AUTH_PASSWORD_HASH:
        return False
    return bcrypt.checkpw(plain.encode(), settings.AUTH_PASSWORD_HASH.encode())


def create_token() -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=settings.AUTH_TOKEN_EXPIRE_HOURS)
    return jwt.encode({"exp": exp}, settings.AUTH_SECRET_KEY, algorithm="HS256")


def _decode(token: str) -> bool:
    try:
        jwt.decode(token, settings.AUTH_SECRET_KEY, algorithms=["HS256"])
        return True
    except jwt.PyJWTError:
        return False


def require_auth(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if not credentials or not _decode(credentials.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def ws_require_auth(token: str = Query(default="")) -> None:
    if not _decode(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
