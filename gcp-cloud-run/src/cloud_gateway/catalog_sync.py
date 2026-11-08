"""Versioned cloud catalog storage (GCS + Firestore) and HomeGraph Request Sync."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
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
    linked: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "etag": self.etag,
            "gcs_object": self.gcs_object,
            "linked": self.linked,
            "agent_user_id": AGENT_USER_ID,
        }


class CatalogCloudStore:
    """Persists catalog JSON in GCS with Firestore metadata. Supports in-memory mock mode."""

    def __init__(
        self,
        project_id: str,
        bucket_name: str | None = None,
        mock: bool = False,
    ):
        self.project_id = project_id
        self.bucket_name = bucket_name or f"{project_id}-{DEFAULT_BUCKET_SUFFIX}"
        self.mock = mock
        self._lock = threading.RLock()
        self._memory_catalog: dict[str, Any] | None = None
        self._meta = CatalogMeta()
        self._cache: dict[str, Any] | None = None
        self._cache_version: int = -1
        self._cache_at: float = 0.0
        self._cache_ttl_sec = 5.0
        self._last_sync_at: float = 0.0
        self._sync_debounce_sec = 5.0
        self._pending_sync = False
        self._gcs = None
        self._fs = None
        if not mock:
            try:
                from google.cloud import firestore, storage

                self._gcs = storage.Client(project=project_id)
                self._fs = firestore.Client(project=project_id)
            except Exception as exc:
                log.warning("Cloud catalog clients unavailable (%s); using memory store", exc)
                self.mock = True

    def get_meta(self) -> CatalogMeta:
        with self._lock:
            if self.mock:
                return CatalogMeta(
                    version=self._meta.version,
                    updated_at=self._meta.updated_at,
                    etag=self._meta.etag,
                    gcs_object=self._meta.gcs_object,
                    linked=self._meta.linked,
                )
            assert self._fs is not None
            doc = self._fs.collection(META_COLLECTION).document(META_DOC).get()
            if not doc.exists:
                return CatalogMeta()
            data = doc.to_dict() or {}
            return CatalogMeta(
                version=int(data.get("version") or 0),
                updated_at=data.get("updated_at"),
                etag=data.get("etag"),
                gcs_object=data.get("gcs_object"),
                linked=bool(data.get("linked", True)),
            )

    def set_linked(self, linked: bool) -> None:
        with self._lock:
            self._meta.linked = linked
            if self.mock:
                return
            assert self._fs is not None
            self._fs.collection(META_COLLECTION).document(LINK_DOC).set(
                {"linked": linked, "updated_at": _utcnow_iso()},
                merge=True,
            )
            self._fs.collection(META_COLLECTION).document(META_DOC).set(
                {"linked": linked, "updated_at": _utcnow_iso()},
                merge=True,
            )

    def is_linked(self) -> bool:
        meta = self.get_meta()
        if self.mock:
            return meta.linked
        assert self._fs is not None
        link = self._fs.collection(META_COLLECTION).document(LINK_DOC).get()
        if link.exists:
            return bool((link.to_dict() or {}).get("linked", True))
        return meta.linked

    def put_catalog(self, catalog: dict[str, Any], version: int, etag: str | None = None) -> CatalogMeta:
        if not isinstance(catalog, dict):
            raise ValueError("catalog must be an object")
        with self._lock:
            current = self.get_meta()
            if version <= current.version:
                raise ValueError(
                    f"stale catalog version {version}; cloud has {current.version}"
                )
            object_name = f"catalog/v{version}.json"
            payload = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if self.mock:
                self._memory_catalog = catalog
                self._meta = CatalogMeta(
                    version=version,
                    updated_at=_utcnow_iso(),
                    etag=etag or f"v{version}",
                    gcs_object=object_name,
                    linked=self._meta.linked,
                )
                self._cache = catalog
                self._cache_version = version
                self._cache_at = time.time()
                return self._meta

            assert self._gcs is not None and self._fs is not None
            bucket = self._gcs.bucket(self.bucket_name)
            if not bucket.exists():
                bucket = self._gcs.create_bucket(self.bucket_name, location="us-central1")
            blob = bucket.blob(object_name)
            blob.upload_from_string(payload, content_type="application/json")
            # Also write latest pointer for simple readers.
            latest = bucket.blob("catalog/latest.json")
            latest.upload_from_string(payload, content_type="application/json")
            meta = CatalogMeta(
                version=version,
                updated_at=_utcnow_iso(),
                etag=etag or blob.etag or f"v{version}",
                gcs_object=object_name,
                linked=current.linked,
            )
            self._fs.collection(META_COLLECTION).document(META_DOC).set(meta.to_dict())
            self._meta = meta
            self._cache = catalog
            self._cache_version = version
            self._cache_at = time.time()
            return meta

    def load_catalog(self, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            if self._cache is not None and (now - self._cache_at) < self._cache_ttl_sec:
                return self._cache
            meta = self.get_meta()
            if self.mock:
                if self._memory_catalog is not None:
                    self._cache = self._memory_catalog
                    self._cache_version = meta.version
                    self._cache_at = now
                    return self._memory_catalog
                return fallback or {}

            if meta.version <= 0 or not meta.gcs_object:
                return fallback or {}
            assert self._gcs is not None
            bucket = self._gcs.bucket(self.bucket_name)
            blob = bucket.blob(meta.gcs_object)
            if not blob.exists():
                blob = bucket.blob("catalog/latest.json")
            if not blob.exists():
                return fallback or {}
            data = json.loads(blob.download_as_text())
            if not isinstance(data, dict):
                return fallback or {}
            self._cache = data
            self._cache_version = meta.version
            self._cache_at = now
            return data

    def request_sync(self, force: bool = False) -> dict[str, Any]:
        """Call HomeGraph Request Sync if linked. Debounced unless force=True."""
        with self._lock:
            if not self.is_linked():
                return {"state": "skipped", "detail": "account unlinked"}
            now = time.time()
            if not force and (now - self._last_sync_at) < self._sync_debounce_sec:
                self._pending_sync = True
                return {"state": "pending", "detail": "debounced"}
            self._last_sync_at = now
            self._pending_sync = False

        if self.mock:
            return {"state": "succeeded", "detail": "mock requestSync", "agentUserId": AGENT_USER_ID}

        try:
            from google.auth import default
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = default(scopes=["https://www.googleapis.com/auth/homegraph"])
            session = AuthorizedSession(credentials)
            resp = session.post(
                "https://homegraph.googleapis.com/v1/devices:requestSync",
                json={"agentUserId": AGENT_USER_ID, "async": True},
                timeout=30,
            )
            if resp.status_code == 404:
                return {
                    "state": "failed",
                    "detail": "HomeGraph 404 — account may not be linked yet",
                    "http_status": 404,
                }
            if resp.status_code == 429:
                return {
                    "state": "failed",
                    "detail": "HomeGraph rate limited",
                    "http_status": 429,
                }
            if resp.status_code >= 400:
                return {
                    "state": "failed",
                    "detail": resp.text[:300],
                    "http_status": resp.status_code,
                }
            return {"state": "succeeded", "agentUserId": AGENT_USER_ID}
        except Exception as exc:
            log.exception("Request Sync failed")
            return {"state": "failed", "detail": str(exc)}
