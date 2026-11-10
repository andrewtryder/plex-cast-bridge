"""Short-lived account-link approval, shared across gateway instances."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from fastapi import HTTPException

COLLECTION = "plex_cast_bridge"
DOCUMENT = "oauth_approval"
LIFETIME_SECONDS = 300


class ApprovalStore:
    def __init__(self, firestore_client=None, *, mock: bool = False):
        self._fs = firestore_client
        self._mock = mock
        self._lock = threading.RLock()
        self._memory: dict[str, Any] = {}

    def _mutate(self, operation):
        if self._mock:
            with self._lock:
                return operation(self._memory)
        if self._fs is None:
            raise HTTPException(status_code=503, detail="Approval storage unavailable")
        from google.cloud import firestore
        ref = self._fs.collection(COLLECTION).document(DOCUMENT)

        @firestore.transactional
        def apply(tx):
            snap = ref.get(transaction=tx)
            data = snap.to_dict() if snap.exists else {}
            result = operation(data)
            tx.set(ref, data)
            return result

        return apply(self._fs.transaction())

    def _read(self, operation):
        if self._mock:
            with self._lock:
                return operation(self._memory)
        if self._fs is None:
            raise HTTPException(status_code=503, detail="Approval storage unavailable")
        snapshot = self._fs.collection(COLLECTION).document(DOCUMENT).get()
        return operation(snapshot.to_dict() if snapshot.exists else {})

    def create(self, *, client_id: str, redirect_uri: str, state: str, scope: str) -> dict[str, str]:
        ticket = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(1000000):06d}"
        def write(data):
            data.clear()
            data.update(ticket=ticket, code=code, client_id=client_id, redirect_uri=redirect_uri,
                        state=state, scope=scope, expires_at=time.time() + LIFETIME_SECONDS,
                        status="pending")
        self._mutate(write)
        return {"ticket": ticket, "code": code}

    @staticmethod
    def _matches(data: dict[str, Any], ticket: str) -> bool:
        stored = data.get("ticket") or ""
        return bool(ticket and stored and secrets.compare_digest(ticket, stored) and data.get("expires_at", 0) > time.time())

    def pending(self) -> dict[str, Any] | None:
        def read(data):
            if data.get("status") != "pending" or data.get("expires_at", 0) <= time.time():
                return None
            return {"ticket": data["ticket"], "code": data["code"], "expires_at": data["expires_at"]}
        return self._read(read)

    def approve(self, ticket: str, code: str) -> bool:
        def update(data):
            if not self._matches(data, ticket) or data.get("status") != "pending":
                return False
            if not secrets.compare_digest(data.get("code") or "", code):
                return False
            data["status"] = "approved"
            return True
        return self._mutate(update)

    def status(self, ticket: str) -> str:
        def read(data):
            if not self._matches(data, ticket):
                return "expired"
            return data.get("status", "expired")
        return self._read(read)

    def consume(self, ticket: str) -> dict[str, Any] | None:
        def update(data):
            if not self._matches(data, ticket) or data.get("status") != "approved":
                return None
            result = {k: data[k] for k in ("client_id", "redirect_uri", "state", "scope")}
            data["status"] = "consumed"
            return result
        return self._mutate(update)
