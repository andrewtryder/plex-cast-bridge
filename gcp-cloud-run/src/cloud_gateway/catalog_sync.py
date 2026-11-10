"""Versioned cloud catalog storage (GCS + Firestore) and HomeGraph Request Sync."""

from __future__ import annotations

import json
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

AGENT_USER_ID = "plex-personal-user"
META_COLLECTION = "plex_cast_bridge"
META_DOC = "catalog_meta"
LINK_DOC = "home_link"
DEFAULT_BUCKET_SUFFIX = "catalog"


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class CatalogMeta:
    version: int = 0
    updated_at: str | None = None
    etag: str | None = None
    gcs_object: str | None = None
    # Default unlinked until a verified OAuth link event.
    linked: bool = False
    pending_sync: bool = False
    last_sync_at: str | None = None
    last_sync_state: str | None = None
    last_sync_detail: str | None = None
    storage_degraded: bool = False
    catalog_digest: str | None = None
    sync_generation: int = 0
    accepted_generation: int = 0
    link_epoch: int = 0
    lease_id: str | None = None
    lease_until: float = 0.0
    retry_attempt: int = 0
    retry_after: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "etag": self.etag,
            "gcs_object": self.gcs_object,
            "linked": self.linked,
            "pending_sync": self.pending_sync,
            "last_sync_at": self.last_sync_at,
            "last_sync_state": self.last_sync_state,
            "last_sync_detail": self.last_sync_detail,
            "storage_degraded": self.storage_degraded,
            "agent_user_id": AGENT_USER_ID,
            "catalog_digest": self.catalog_digest,
            "sync_generation": self.sync_generation,
            "accepted_generation": self.accepted_generation,
            "link_epoch": self.link_epoch,
            "lease_id": self.lease_id,
            "lease_until": self.lease_until,
            "retry_attempt": self.retry_attempt,
            "retry_after": self.retry_after,
        }


class CatalogCloudStore:
    """Persists catalog JSON in GCS with Firestore metadata. Mock mode is explicit only."""

    def __init__(
        self,
        project_id: str,
        bucket_name: str | None = None,
        mock: bool = False,
        *,
        allow_memory_fallback: bool = False,
    ):
        self.project_id = project_id
        self.bucket_name = bucket_name or f"{project_id}-{DEFAULT_BUCKET_SUFFIX}"
        self.mock = mock
        self.allow_memory_fallback = allow_memory_fallback
        self._lock = threading.RLock()
        self._memory_catalog: dict[str, Any] | None = None
        self._meta = CatalogMeta()
        self._cache: dict[str, Any] | None = None
        self._cache_version: int = -1
        self._cache_at: float = 0.0
        self._cache_ttl_sec = 5.0
        self._gcs = None
        self._fs = None
        self.storage_ok = True
        self.init_error: str | None = None
        if not mock:
            try:
                from google.cloud import firestore, storage

                self._gcs = storage.Client(project=project_id)
                self._fs = firestore.Client(project=project_id)
            except Exception as exc:
                self.init_error = str(exc)
                self.storage_ok = False
                if allow_memory_fallback:
                    log.warning(
                        "Cloud catalog clients unavailable (%s); using memory store (explicit fallback)",
                        exc,
                    )
                    self.mock = True
                    self._meta.storage_degraded = True
                else:
                    log.error(
                        "Cloud catalog clients unavailable (%s); persistent sync unavailable",
                        exc,
                    )

    def health(self) -> dict[str, Any]:
        return {
            "storage_ok": self.storage_ok and (self.mock or (self._gcs is not None and self._fs is not None)),
            "mock": self.mock,
            "init_error": self.init_error,
            "pending_sync": self.get_meta().pending_sync if self.storage_ok or self.mock else False,
            "linked": self.is_linked() if self.storage_ok or self.mock else False,
        }

    def _require_storage(self) -> None:
        if self.mock:
            return
        if not self.storage_ok or self._gcs is None or self._fs is None:
            raise RuntimeError(
                f"Cloud catalog storage unavailable: {self.init_error or 'not initialized'}"
            )

    @staticmethod
    def _meta_from_dict(data: dict[str, Any]) -> CatalogMeta:
        allowed = {item.name for item in fields(CatalogMeta)}
        return CatalogMeta(**{k: v for k, v in data.items() if k in allowed})

    def _transaction(self, change):
        """Mutate a single authoritative document, retrying Firestore conflicts."""
        if self.mock:
            with self._lock:
                state = self._meta.to_dict()
                result = change(state)
                self._meta = self._meta_from_dict(state)
                return result
        self._require_storage()
        from google.cloud import firestore

        ref = self._fs.collection(META_COLLECTION).document(META_DOC)
        link_ref = self._fs.collection(META_COLLECTION).document(LINK_DOC)

        @firestore.transactional
        def apply(transaction):
            snap = ref.get(transaction=transaction)
            state = snap.to_dict() if snap.exists else {}
            if "linked" not in state:
                legacy = link_ref.get(transaction=transaction)
                state["linked"] = bool((legacy.to_dict() or {}).get("linked")) if legacy.exists else False
            result = change(state)
            transaction.set(ref, state, merge=True)
            transaction.set(link_ref, {"linked": bool(state.get("linked"))}, merge=True)
            return result

        return apply(self._fs.transaction())

    def get_meta(self) -> CatalogMeta:
        if self.mock:
            with self._lock:
                return self._meta_from_dict(self._meta.to_dict())
        self._require_storage()
        doc = self._fs.collection(META_COLLECTION).document(META_DOC).get()
        return self._meta_from_dict((doc.to_dict() or {}) if doc.exists else {})

    def set_linked(self, linked: bool) -> None:
        def update(state):
            if bool(state.get("linked")) == linked:
                return
            state["linked"] = linked
            state["link_epoch"] = int(state.get("link_epoch") or 0) + 1
            state["lease_id"] = None
            state["lease_until"] = 0.0
            state["retry_after"] = 0.0
            state["retry_attempt"] = 0
            if linked:
                state["sync_generation"] = int(state.get("sync_generation") or 0) + 1
                state["pending_sync"] = True
                state["last_sync_state"] = "pending"
            else:
                state["pending_sync"] = False
                state["last_sync_state"] = "skipped"
                state["last_sync_detail"] = "account unlinked"
        self._transaction(update)

    def is_linked(self) -> bool:
        return self.get_meta().linked

    def put_catalog(self, catalog: dict[str, Any], version: int, etag: str | None = None) -> CatalogMeta:
        if not isinstance(catalog, dict):
            raise ValueError("catalog must be an object")
        payload = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        digest = hashlib.sha256(payload).hexdigest()
        # Unique objects are immutable: a losing transaction can never replace the winner.
        object_name = f"catalog/v{version}-{secrets.token_hex(16)}.json"
        if not self.mock:
            self._require_storage()
            blob = self._gcs.bucket(self.bucket_name).blob(object_name)
            blob.upload_from_string(payload, content_type="application/json", if_generation_match=0)

        def publish(state):
            current = int(state.get("version") or 0)
            if version == current and state.get("catalog_digest") == digest:
                return self._meta_from_dict(state)
            if version <= current:
                raise ValueError(f"stale catalog version {version}; cloud has {current}")
            state.update({"version": version, "gcs_object": object_name, "catalog_digest": digest,
                          "etag": etag or f"v{version}", "updated_at": _utcnow_iso(),
                          "sync_generation": int(state.get("sync_generation") or 0) + 1,
                          "pending_sync": bool(state.get("linked")), "retry_after": 0.0,
                          "retry_attempt": 0})
            state["last_sync_state"] = "pending" if state["pending_sync"] else "skipped"
            state["last_sync_detail"] = None
            if self.mock:
                self._memory_catalog = catalog
            return self._meta_from_dict(state)

        meta = self._transaction(publish)
        self._cache = None
        return meta

    def load_catalog(self, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        meta = self.get_meta()
        if self.mock:
            return self._memory_catalog if self._memory_catalog is not None else (fallback or {})
        if not meta.gcs_object:
            return fallback or {}
        if self._cache is not None and self._cache_version == meta.version:
            return self._cache
        blob = self._gcs.bucket(self.bucket_name).blob(meta.gcs_object)
        data = json.loads(blob.download_as_text())
        if not isinstance(data, dict):
            raise ValueError("stored catalog is not an object")
        self._cache, self._cache_version = data, meta.version
        return data

    def reconcile(self, *, force: bool = False) -> dict[str, Any]:
        """Claim a generation transactionally, then call HomeGraph outside the transaction."""
        now = time.time()
        lease = secrets.token_hex(16)

        def claim(state):
            if not state.get("linked"):
                return {"state": "skipped", "detail": "account unlinked"}
            if not state.get("pending_sync"):
                if not force:
                    return {"state": "idle"}
                state["sync_generation"] = int(state.get("sync_generation") or 0) + 1
                state["pending_sync"] = True
            if float(state.get("lease_until") or 0) > now:
                return {"state": "pending", "detail": "leased"}
            if not force and float(state.get("retry_after") or 0) > now:
                return {"state": "pending", "detail": "backoff"}
            state["lease_id"] = lease
            state["lease_until"] = now + 90
            return {"state": "claimed", "generation": int(state.get("sync_generation") or 0),
                    "epoch": int(state.get("link_epoch") or 0)}

        claimed = self._transaction(claim)
        if claimed["state"] != "claimed":
            return claimed
        result = self._call_homegraph()
        finished_at = time.time()

        def finish(state):
            if state.get("lease_id") != lease:
                return {"state": "pending" if state.get("linked") else "skipped", "detail": "lease superseded"}
            state["lease_id"] = None
            state["lease_until"] = 0.0
            if (not state.get("linked") or int(state.get("link_epoch") or 0) != claimed["epoch"]
                    or int(state.get("sync_generation") or 0) != claimed["generation"]):
                return {"state": "pending" if state.get("linked") else "skipped", "detail": "state changed"}
            state["last_sync_at"] = _utcnow_iso()
            state["last_sync_state"] = result["state"]
            state["last_sync_detail"] = result.get("detail")
            if result["state"] == "accepted":
                state["accepted_generation"] = claimed["generation"]
                state["pending_sync"] = False
                state["retry_attempt"] = 0
                state["retry_after"] = 0.0
            else:
                attempt = int(state.get("retry_attempt") or 0) + 1
                state["retry_attempt"] = attempt
                state["retry_after"] = finished_at + min(300, 5 * 2 ** min(attempt - 1, 6))
                state["pending_sync"] = True
            return result

        return self._transaction(finish)

    def request_sync(self, force: bool = False) -> dict[str, Any]:
        return self.reconcile(force=force)

    def _call_homegraph(self) -> dict[str, Any]:
        if self.mock:
            return {"state": "accepted", "detail": "mock requestSync"}
        try:
            from google.auth import default
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = default(scopes=["https://www.googleapis.com/auth/homegraph"])
            response = AuthorizedSession(credentials).post(
                "https://homegraph.googleapis.com/v1/devices:requestSync",
                json={"agentUserId": AGENT_USER_ID, "async": True}, timeout=30,
            )
            if response.status_code >= 400:
                return {"state": "failed", "detail": f"HomeGraph HTTP {response.status_code}"}
            return {"state": "accepted"}
        except Exception:
            log.exception("Request Sync failed")
            return {"state": "failed", "detail": "HomeGraph request failed"}
