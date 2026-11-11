"""Durable, revocable Google Home refresh-token state.

Only a hash of the bearer token is stored. One Google Home subscriber is supported
per gateway installation, so one Firestore document identifies the active token.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

COLLECTION = "plex_cast_bridge"
DOCUMENT = "oauth_refresh_state"
OPAQUE_PREFIX = "pcb1."


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class MemoryRefreshTokenStore:
    """Test/development store. Pass the same dict to simulate a new revision."""

    def __init__(self, state: dict[str, Any] | None = None):
        self.state = state if state is not None else {}

    def activate(self, token: str, token_format: str) -> None:
        self.state.clear()
        self.state.update({
            "token_hash": _token_hash(token),
            "token_format": token_format,
            "created_at": datetime.now(UTC).isoformat(),
            "revoked_at": None,
        })

    def is_active(self, token: str, token_format: str) -> bool:
        return (
            self.state.get("token_hash") == _token_hash(token)
            and self.state.get("token_format") == token_format
            and not self.state.get("revoked_at")
        )

    def migrate_legacy(self, token: str) -> bool:
        token_hash = _token_hash(token)
        if self.state:
            return (
                self.state.get("token_hash") == token_hash
                and self.state.get("token_format") == "legacy-jwt"
                and not self.state.get("revoked_at")
            )
        self.activate(token, "legacy-jwt")
        return True

    def issue(self) -> str:
        token = OPAQUE_PREFIX + secrets.token_urlsafe(48)
        self.activate(token, "opaque-v1")
        return token

    def revoke_current(self) -> None:
        if self.state:
            self.state["revoked_at"] = datetime.now(UTC).isoformat()


class FirestoreRefreshTokenStore:
    """Firestore-backed active-token record shared by all Cloud Run revisions."""

    def __init__(self, client: Any):
        if client is None:
            raise RuntimeError("Firestore is required for durable OAuth refresh tokens")
        self._document = client.collection(COLLECTION).document(DOCUMENT)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def activate(self, token: str, token_format: str) -> None:
        self._document.set({
            "token_hash": _token_hash(token),
            "token_format": token_format,
            "created_at": self._now(),
            "revoked_at": None,
        })

    def is_active(self, token: str, token_format: str) -> bool:
        snapshot = self._document.get()
        state = snapshot.to_dict() if snapshot.exists else {}
        return (
            state.get("token_hash") == _token_hash(token)
            and state.get("token_format") == token_format
            and not state.get("revoked_at")
        )

    def migrate_legacy(self, token: str) -> bool:
        from google.api_core.exceptions import AlreadyExists

        token_hash = _token_hash(token)
        try:
            self._document.create({
                "token_hash": token_hash,
                "token_format": "legacy-jwt",
                "created_at": self._now(),
                "revoked_at": None,
            })
            return True
        except AlreadyExists:
            snapshot = self._document.get()
            state = snapshot.to_dict() if snapshot.exists else {}
            return (
                state.get("token_hash") == token_hash
                and state.get("token_format") == "legacy-jwt"
                and not state.get("revoked_at")
            )

    def issue(self) -> str:
        token = OPAQUE_PREFIX + secrets.token_urlsafe(48)
        self.activate(token, "opaque-v1")
        return token

    def revoke_current(self) -> None:
        snapshot = self._document.get()
        state = snapshot.to_dict() if snapshot.exists else {}
        if state and not state.get("revoked_at"):
            self._document.update({"revoked_at": self._now()})


def validate_refresh_token(token: str, settings: Any, store: Any) -> None:
    """Validate opaque credentials or migrate an existing signed JWT once.

    Expiration is bypassed only for a legacy JWT whose exact hash was already
    persisted as the active credential. New and unregistered JWTs still require
    their ordinary signature and expiration checks.
    """
    import jwt

    secret = settings.oauth_signing_secret.get_secret_value()
    if token.startswith(OPAQUE_PREFIX):
        if not store.is_active(token, "opaque-v1"):
            raise HTTPException(status_code=400, detail="Invalid or revoked refresh token")
        return

    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        expired = False
    except jwt.ExpiredSignatureError:
        try:
            payload = jwt.decode(
                token, secret, algorithms=["HS256"], options={"verify_exp": False}
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=400, detail="Invalid refresh token") from exc
        expired = True
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=400, detail="Invalid refresh token") from exc

    if payload.get("type") != "refresh" or not payload.get("jti"):
        raise HTTPException(status_code=400, detail="Token is not a refresh token")
    if store.is_active(token, "legacy-jwt"):
        return
    if expired or not store.migrate_legacy(token):
        raise HTTPException(status_code=400, detail="Invalid or revoked refresh token")
