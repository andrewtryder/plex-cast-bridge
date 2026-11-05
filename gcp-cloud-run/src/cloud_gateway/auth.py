import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings

security = HTTPBearer(auto_error=False)


def create_authorization_code(
    client_id: str,
    redirect_uri: str,
    settings: Settings,
    scope: str = "smart-home",
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "admin",
        "type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.auth_code_expire_minutes)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.oauth_signing_secret.get_secret_value(), algorithm="HS256")


def verify_authorization_code(
    code: str,
    expected_client_id: str,
    settings: Settings,
    expected_redirect_uri: str | None = None,
) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            code,
            settings.oauth_signing_secret.get_secret_value(),
            algorithms=["HS256"],
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid authorization code: {exc}",
        ) from exc

    if payload.get("type") != "authorization_code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token is not an authorization code",
        )
    if payload.get("client_id") != expected_client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Client ID mismatch in authorization code",
        )
    if expected_redirect_uri and payload.get("redirect_uri") and payload.get("redirect_uri") != expected_redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Redirect URI mismatch in authorization code",
        )
    return payload


def create_token_pair(settings: Settings) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    access_expires_seconds = settings.token_expire_days * 86400
    access_payload = {
        "sub": "admin",
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.token_expire_days)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    refresh_payload = {
        "sub": "admin",
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.token_expire_days * 2)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    secret = settings.oauth_signing_secret.get_secret_value()
    access_token = jwt.encode(access_payload, secret, algorithm="HS256")
    refresh_token = jwt.encode(refresh_payload, secret, algorithm="HS256")
    return {
        "token_type": "Bearer",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": access_expires_seconds,
    }


def verify_refresh_token(refresh_token: str, settings: Settings) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            refresh_token,
            settings.oauth_signing_secret.get_secret_value(),
            algorithms=["HS256"],
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid refresh token: {exc}",
        ) from exc

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token is not a refresh token",
        )
    return payload


async def verify_bearer_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
) -> dict[str, Any]:
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    settings: Settings = request.app.state.settings
    try:
        payload = jwt.decode(
            token,
            settings.oauth_signing_secret.get_secret_value(),
            algorithms=["HS256"],
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired bearer token: {exc}",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token type is not access",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    return payload


async def verify_webhook_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str:
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )
    settings: Settings = request.app.state.settings
    expected = settings.get_webhook_key()
    if not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-API-Key",
        )
    return x_api_key
