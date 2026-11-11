import base64
import hashlib
import hmac
import logging
import secrets
import threading
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Callable

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
_LOCAL_EXCHANGE_ORDER = 0
_LOCAL_EXCHANGE_ORDER_LOCK = threading.Lock()

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

def _check_local_jti(jti: str, exp: int | None, refresh_token_hash: str | None = None) -> bool:
    _cleanup_expired_jtis()
    existing = _REDEEMED_JTIS.get(jti)
    if existing is not None:
        existing_hash = existing.get("refresh_token_hash") if isinstance(existing, dict) else None
        if refresh_token_hash and existing_hash and hmac.compare_digest(existing_hash, refresh_token_hash):
            return True
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authorization code has already been redeemed (single-use violated)",
        )
    _REDEEMED_JTIS[jti] = {
        "exp": exp or (int(datetime.now(UTC).timestamp()) + 600),
        "refresh_token_hash": refresh_token_hash,
    }
    return False


# In-memory store for redeemed authorization code JTIs and idempotent exchanges.
_REDEEMED_JTIS: dict[str, dict[str, Any] | int] = {}

ALLOWED_REDIRECT_DOMAINS = {
    "oauth-redirect.googleusercontent.com",
    "oauth-redirect-sandbox.googleusercontent.com",
}


def _cleanup_expired_jtis():
    now_ts = int(datetime.now(UTC).timestamp())
    expired = [
        jti for jti, entry in _REDEEMED_JTIS.items()
        if (entry.get("exp", 0) if isinstance(entry, dict) else entry) < now_ts
    ]
    for jti in expired:
        _REDEEMED_JTIS.pop(jti, None)


def _wall_clock_exchange_order() -> int:
    now = datetime.now(UTC)
    return int(now.timestamp()) * 1_000_000 + now.microsecond


def _allocate_exchange_order(settings: Settings) -> int:
    """Allocate a durable monotonic order for authorization exchanges.

    Wall-clock timestamps alone can move backwards between Cloud Run instances.
    The Firestore transaction serializes code issuance and also seeds its counter
    above any timestamp-order value written by an older revision.
    """
    global _LOCAL_EXCHANGE_ORDER
    db = get_firestore_client()
    is_dev = getattr(settings, "environment", "production").lower() in {
        "development", "dev", "local", "test", "testing"
    }
    if db is None:
        if not is_dev:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Distributed authorization-code order is unavailable",
            )
        with _LOCAL_EXCHANGE_ORDER_LOCK:
            _LOCAL_EXCHANGE_ORDER = max(_LOCAL_EXCHANGE_ORDER + 1, _wall_clock_exchange_order())
            return _LOCAL_EXCHANGE_ORDER

    try:
        from google.cloud import firestore

        sequence_ref = db.collection("oauth_exchange_state").document("authorization_sequence")
        refresh_ref = db.collection("plex_cast_bridge").document("oauth_refresh_state")

        @firestore.transactional
        def allocate(transaction):
            sequence_snapshot = sequence_ref.get(transaction=transaction)
            refresh_snapshot = refresh_ref.get(transaction=transaction)
            sequence_state = sequence_snapshot.to_dict() if sequence_snapshot.exists else {}
            refresh_state = refresh_snapshot.to_dict() if refresh_snapshot.exists else {}
            previous = max(
                int(sequence_state.get("last_order") or 0),
                int(refresh_state.get("exchange_order") or 0),
                int(refresh_state.get("exchange_issued_at_us") or 0),
            )
            order = max(previous + 1, _wall_clock_exchange_order())
            transaction.set(sequence_ref, {"last_order": order})
            return order

        return int(allocate(db.transaction()))
    except Exception as exc:
        if not is_dev:
            log.error("Could not allocate distributed OAuth exchange order (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Distributed authorization-code order is temporarily unavailable",
            ) from exc
        log.warning("Firestore exchange-order allocation failed (%s); using local test order", exc)
        with _LOCAL_EXCHANGE_ORDER_LOCK:
            _LOCAL_EXCHANGE_ORDER = max(_LOCAL_EXCHANGE_ORDER + 1, _wall_clock_exchange_order())
            return _LOCAL_EXCHANGE_ORDER


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
    now = datetime.now(UTC)
    exchange_order = _allocate_exchange_order(settings)
    exp = int((now + timedelta(minutes=settings.auth_code_expire_minutes)).timestamp())
    payload = {
        "sub": "admin",
        "type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "iat": int(now.timestamp()),
        "exchange_order": exchange_order,
        "exp": exp,
        "jti": secrets.token_hex(16),
    }
    return jwt.encode(payload, settings.oauth_signing_secret.get_secret_value(), algorithm="HS256")


def verify_authorization_code(
    code: str,
    expected_client_id: str,
    settings: Settings,
    expected_redirect_uri: str | None = None,
    refresh_token_factory: Callable[[str], str] | None = None,
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

    refresh_token = refresh_token_factory(jti) if refresh_token_factory else None
    refresh_token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest() if refresh_token else None
    exchange_replay = False

    is_dev = getattr(settings, "environment", "production").lower() in {
        "development", "dev", "local", "test", "testing"
    }

    db = get_firestore_client()
    if db is not None:
        try:
            from google.cloud import firestore
            server_ts = getattr(firestore, "SERVER_TIMESTAMP", None)
            doc_ref = db.collection("oauth_redeemed_codes").document(jti)
            record = {"exp": payload.get("exp"), "redeemed_at": server_ts}
            if refresh_token_hash:
                record["refresh_token_hash"] = refresh_token_hash
            doc_ref.create(record)
        except AlreadyExists:
            if refresh_token_hash:
                try:
                    snapshot = doc_ref.get()
                except Exception as exc:
                    log.error("Could not inspect prior authorization-code exchange (%s)", exc)
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Distributed authorization code store is temporarily unavailable",
                    ) from exc
                existing = snapshot.to_dict() if snapshot.exists else {}
                stored_hash = (existing or {}).get("refresh_token_hash")
                if isinstance(stored_hash, str) and hmac.compare_digest(stored_hash, refresh_token_hash):
                    exchange_replay = True
                else:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Authorization code has already been redeemed (single-use violated)",
                    )
            else:
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
            exchange_replay = _check_local_jti(jti, payload.get("exp"), refresh_token_hash)
    else:
        if not is_dev:
            log.error("Production Firestore client unavailable; failing closed.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Distributed authorization code store is unavailable. Token exchange failed closed for security.",
            )
        log.warning("Firestore client unavailable; falling back to local memory in %s mode.", settings.environment)
        exchange_replay = _check_local_jti(jti, payload.get("exp"), refresh_token_hash)

    return {
        **payload,
        "_exchange_replay": exchange_replay,
        "_exchange_refresh_token": refresh_token,
    }


def create_access_token(settings: Settings) -> tuple[str, int]:
    now = datetime.now(UTC)
    access_expires_seconds = settings.token_expire_days * 86400
    access_payload = {
        "sub": "admin",
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.token_expire_days)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    secret = settings.oauth_signing_secret.get_secret_value()
    access_token = jwt.encode(access_payload, secret, algorithm="HS256")
    return access_token, access_expires_seconds


def create_legacy_token_pair(settings: Settings) -> dict[str, Any]:
    """Create the old expiring JWT pair for migration tests only."""
    now = datetime.now(UTC)
    access_token, access_expires_seconds = create_access_token(settings)
    refresh_payload = {
        "sub": "admin",
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.token_expire_days * 2)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    secret = settings.oauth_signing_secret.get_secret_value()
    refresh_token = jwt.encode(refresh_payload, secret, algorithm="HS256")
    return {
        "token_type": "Bearer",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": access_expires_seconds,
    }


def create_exchange_refresh_token(settings: Settings, code_jti: str) -> str:
    """Derive a retry-stable opaque token from a verified, random authorization JTI."""
    secret = settings.oauth_signing_secret.get_secret_value().encode("utf-8")
    material = hmac.new(
        secret,
        b"plex-cast-bridge:refresh:v1:" + code_jti.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return "pcb1." + base64.urlsafe_b64encode(material).rstrip(b"=").decode("ascii")


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
    candidates: list[str] = []
    webhook = settings.get_webhook_key()
    if webhook:
        candidates.append(webhook)
    if settings.catalog_write_key:
        catalog_key = settings.catalog_write_key.get_secret_value()
        if catalog_key and catalog_key not in candidates:
            candidates.append(catalog_key)
    # compare_digest requires equal-length strings; check each candidate separately.
    ok = False
    for expected in candidates:
        if len(x_api_key) == len(expected) and secrets.compare_digest(x_api_key, expected):
            ok = True
            break
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-API-Key",
        )
    return x_api_key
