import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings

security = HTTPBearer(auto_error=False)

log = logging.getLogger(__name__)

try:
    from google.api_core.exceptions import AlreadyExists
except ImportError:
    class AlreadyExists(Exception):
        pass

_firestore_client = None
_firestore_checked = False

def get_firestore_client():
    global _firestore_client, _firestore_checked
    if not _firestore_checked:
        _firestore_checked = True
        try:
            from google.cloud import firestore
            _firestore_client = firestore.Client()
            log.info("Initialized Google Cloud Firestore for distributed OAuth JTI tracking.")
        except Exception as exc:
            log.debug("Firestore unavailable for OAuth JTI store (%s); using process-local fallback.", exc)
            _firestore_client = None
    return _firestore_client

def _check_local_jti(jti: str, exp: int | None):
    _cleanup_expired_jtis()
    if jti in _REDEEMED_JTIS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authorization code has already been redeemed (single-use violated)",
        )
    _REDEEMED_JTIS[jti] = exp or (int(datetime.now(timezone.utc).timestamp()) + 600)


# In-memory store for redeemed authorization code JTIs: {jti: exp_timestamp}
_REDEEMED_JTIS: dict[str, int] = {}

ALLOWED_REDIRECT_DOMAINS = {
    "oauth-redirect.googleusercontent.com",
    "oauth-redirect-sandbox.googleusercontent.com",
}


def _cleanup_expired_jtis():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    expired = [jti for jti, exp in _REDEEMED_JTIS.items() if exp < now_ts]
    for jti in expired:
        _REDEEMED_JTIS.pop(jti, None)


def validate_redirect_uri(redirect_uri: str, allow_local: bool = False):
    """
    Ensure OAuth redirect URI belongs strictly to approved Google OAuth destinations.
    Prevents open redirect and authorization code interception attacks.
    """
    if not redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing redirect_uri",
        )
    parsed = urllib.parse.urlsplit(redirect_uri)
    if allow_local and parsed.netloc in ("localhost", "127.0.0.1", "testserver"):
        return
    if parsed.scheme != "https":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth redirect_uri must use HTTPS scheme",
        )
    if parsed.netloc.lower() not in ALLOWED_REDIRECT_DOMAINS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unauthorized redirect_uri domain '{parsed.netloc}'. Must match Google OAuth redirect domains.",
        )


def create_authorization_code(
    client_id: str,
    redirect_uri: str,
    settings: Settings,
    scope: str = "smart-home",
) -> str:
    now = datetime.now(timezone.utc)
    exp = int((now + timedelta(minutes=settings.auth_code_expire_minutes)).timestamp())
    payload = {
        "sub": "admin",
        "type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "iat": int(now.timestamp()),
        "exp": exp,
        "jti": secrets.token_hex(16),
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
    if not expected_redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing redirect_uri in token request",
        )
    if payload.get("redirect_uri") and payload.get("redirect_uri") != expected_redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Redirect URI mismatch in authorization code",
        )

    # Enforce single-use authorization code (Distributed via Firestore with atomic create, fallback to local memory)
    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authorization code missing jti identifier",
        )

    is_dev = getattr(settings, "environment", "production").lower() in {
        "development", "dev", "local", "test", "testing"
    }

    db = get_firestore_client()
    if db is not None:
        try:
            from google.cloud import firestore
            server_ts = getattr(firestore, "SERVER_TIMESTAMP", None)
            doc_ref = db.collection("oauth_redeemed_codes").document(jti)
            doc_ref.create({"exp": payload.get("exp"), "redeemed_at": server_ts})
        except AlreadyExists:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Authorization code has already been redeemed (single-use violated)",
            )
        except Exception as exc:
            if not is_dev:
                log.error("Production Firestore JTI write failed (%s); failing closed.", exc)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Distributed authorization code store write failed. Token exchange failed closed for security.",
                ) from exc
            log.warning("Firestore JTI write failed (%s); falling back to local memory in %s mode.", exc, settings.environment)
            _check_local_jti(jti, payload.get("exp"))
    else:
        if not is_dev:
            log.error("Production Firestore client unavailable; failing closed.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Distributed authorization code store is unavailable. Token exchange failed closed for security.",
            )
        log.warning("Firestore client unavailable; falling back to local memory in %s mode.", settings.environment)
        _check_local_jti(jti, payload.get("exp"))

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
