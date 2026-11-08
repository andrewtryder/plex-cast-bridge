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
    # Default unlinked until a verified OAuth link event.
    linked: bool = False
    pending_sync: bool = False
    last_sync_at: str | None = None
    last_sync_state: str | None = None
    last_sync_detail: str | None = None
    storage_degraded: bool = False

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
        self._last_sync_at: float = 0.0
        self._sync_debounce_sec = 5.0
        self._pending_sync = False
        self._flush_timer: threading.Timer | None = None
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
            "pending_sync": self._pending_sync,
            "linked": self.is_linked() if self.storage_ok or self.mock else False,
        }

    def _require_storage(self) -> None:
        if self.mock:
            return
        if not self.storage_ok or self._gcs is None or self._fs is None:
            raise RuntimeError(
                f"Cloud catalog storage unavailable: {self.init_error or 'not initialized'}"
            )

    def get_meta(self) -> CatalogMeta:
        with self._lock:
            if self.mock:
                return CatalogMeta(
                    version=self._meta.version,
                    updated_at=self._meta.updated_at,
                    etag=self._meta.etag,
                    gcs_object=self._meta.gcs_object,
                    linked=self._meta.linked,
                    pending_sync=self._meta.pending_sync or self._pending_sync,
                    last_sync_at=self._meta.last_sync_at,
                    last_sync_state=self._meta.last_sync_state,
                    last_sync_detail=self._meta.last_sync_detail,
                    storage_degraded=self._meta.storage_degraded,
                )
            self._require_storage()
            assert self._fs is not None
            doc = self._fs.collection(META_COLLECTION).document(META_DOC).get()
            if not doc.exists:
                return CatalogMeta(linked=False)
            data = doc.to_dict() or {}
            return CatalogMeta(
                version=int(data.get("version") or 0),
                updated_at=data.get("updated_at"),
                etag=data.get("etag"),
                gcs_object=data.get("gcs_object"),
                linked=bool(data.get("linked", False)),
                pending_sync=bool(data.get("pending_sync", False)),
                last_sync_at=data.get("last_sync_at"),
                last_sync_state=data.get("last_sync_state"),
                last_sync_detail=data.get("last_sync_detail"),
                storage_degraded=bool(data.get("storage_degraded", False)),
            )

    def set_linked(self, linked: bool) -> None:
        with self._lock:
            self._meta.linked = linked
            if not linked:
                self._pending_sync = False
                self._meta.pending_sync = False
                self._cancel_flush_timer()
            if self.mock:
                return
            self._require_storage()
            assert self._fs is not None
            self._fs.collection(META_COLLECTION).document(LINK_DOC).set(
                {"linked": linked, "updated_at": _utcnow_iso()},
                merge=True,
            )
            self._fs.collection(META_COLLECTION).document(META_DOC).set(
                {"linked": linked, "updated_at": _utcnow_iso(), "pending_sync": self._pending_sync},
                merge=True,
            )

    def is_linked(self) -> bool:
        if self.mock:
            return self._meta.linked
        if not self.storage_ok:
            return False
        assert self._fs is not None
        link = self._fs.collection(META_COLLECTION).document(LINK_DOC).get()
        if link.exists:
            return bool((link.to_dict() or {}).get("linked", False))
        meta = self.get_meta()
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
            # Preserve link state — uploads never imply account linking.
            linked = current.linked if not self.mock else self._meta.linked
            if self.mock:
                self._memory_catalog = catalog
                self._meta = CatalogMeta(
                    version=version,
                    updated_at=_utcnow_iso(),
                    etag=etag or f"v{version}",
                    gcs_object=object_name,
                    linked=linked,
                    pending_sync=self._pending_sync,
                    last_sync_at=self._meta.last_sync_at,
                    last_sync_state=self._meta.last_sync_state,
                    last_sync_detail=self._meta.last_sync_detail,
                    storage_degraded=self._meta.storage_degraded,
                )
                self._cache = catalog
                self._cache_version = version
                self._cache_at = time.time()
                return self._meta

            self._require_storage()
            assert self._gcs is not None and self._fs is not None
            bucket = self._gcs.bucket(self.bucket_name)
            if not bucket.exists():
                bucket = self._gcs.create_bucket(self.bucket_name, location="us-central1")
            blob = bucket.blob(object_name)
            blob.upload_from_string(payload, content_type="application/json")
            latest = bucket.blob("catalog/latest.json")
            latest.upload_from_string(payload, content_type="application/json")
            meta = CatalogMeta(
                version=version,
                updated_at=_utcnow_iso(),
                etag=etag or blob.etag or f"v{version}",
                gcs_object=object_name,
                linked=linked,
                pending_sync=self._pending_sync or current.pending_sync,
                last_sync_at=current.last_sync_at,
                last_sync_state=current.last_sync_state,
                last_sync_detail=current.last_sync_detail,
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

            self._require_storage()
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

    def _cancel_flush_timer(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None

    def _schedule_pending_flush(self) -> None:
        self._cancel_flush_timer()
        delay = max(0.1, self._sync_debounce_sec)

        def _flush() -> None:
            try:
                self.request_sync(force=True)
            except Exception as exc:
                log.warning("Pending Request Sync flush failed: %s", exc)

        self._flush_timer = threading.Timer(delay, _flush)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _persist_sync_result(self, result: dict[str, Any], *, pending: bool) -> None:
        self._meta.pending_sync = pending
        self._meta.last_sync_at = _utcnow_iso()
        self._meta.last_sync_state = result.get("state")
        self._meta.last_sync_detail = result.get("detail")
        if self.mock:
            return
        if not self.storage_ok or self._fs is None:
            return
        self._fs.collection(META_COLLECTION).document(META_DOC).set(
            {
                "pending_sync": pending,
                "last_sync_at": self._meta.last_sync_at,
                "last_sync_state": self._meta.last_sync_state,
                "last_sync_detail": self._meta.last_sync_detail,
                "updated_at": _utcnow_iso(),
            },
            merge=True,
        )

    def request_sync(self, force: bool = False) -> dict[str, Any]:
        """Call HomeGraph Request Sync if linked. Debounced unless force=True; pending is flushed."""
        with self._lock:
            if not self.is_linked():
                result = {"state": "skipped", "detail": "account unlinked"}
                self._persist_sync_result(result, pending=False)
                return result
            now = time.time()
            if not force and (now - self._last_sync_at) < self._sync_debounce_sec:
                self._pending_sync = True
                self._persist_sync_result(
                    {"state": "pending", "detail": "debounced"},
                    pending=True,
                )
                self._schedule_pending_flush()
                return {"state": "pending", "detail": "debounced"}
            self._last_sync_at = now
            self._pending_sync = False
            self._cancel_flush_timer()

        if self.mock:
            result = {"state": "succeeded", "detail": "mock requestSync", "agentUserId": AGENT_USER_ID}
            with self._lock:
                self._persist_sync_result(result, pending=False)
            return result

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
                result = {
                    "state": "failed",
                    "detail": "HomeGraph 404 — account may not be linked yet",
                    "http_status": 404,
                }
            elif resp.status_code == 429:
                result = {
                    "state": "failed",
                    "detail": "HomeGraph rate limited",
                    "http_status": 429,
                }
            elif resp.status_code >= 400:
                result = {
                    "state": "failed",
                    "detail": resp.text[:300],
                    "http_status": resp.status_code,
                }
            else:
                result = {"state": "succeeded", "agentUserId": AGENT_USER_ID}
        except Exception as exc:
            log.exception("Request Sync failed")
            result = {"state": "failed", "detail": str(exc)}

        with self._lock:
            self._persist_sync_result(result, pending=False)
        return result
