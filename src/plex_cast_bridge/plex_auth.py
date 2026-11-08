"""Plex JWT OAuth linking, server selection, and credential refresh."""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from plexapi.myplex import MyPlexAccount, MyPlexJWTLogin

from . import plex_connection_store as store
from .config import Settings
from .plex_service import PlexService

log = logging.getLogger(__name__)

DEFAULT_SCOPES = ["username", "email", "friendly_name", "restricted", "anonymous", "joinedAt"]


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _device_headers(client_identifier: str) -> dict[str, str]:
    return {
        "X-Plex-Product": store.PRODUCT_NAME,
        "X-Plex-Device": store.PRODUCT_NAME,
        "X-Plex-Device-Name": store.PRODUCT_NAME,
        "X-Plex-Client-Identifier": client_identifier,
        "X-Plex-Platform": "Linux",
        "X-Plex-Platform-Version": "1",
        "X-Plex-Version": "0.2.2",
    }


class PlexAuthService:
    """Manages JWT OAuth sessions and the active PlexService instance."""

    def __init__(self):
        self._lock = threading.RLock()
        self._login: MyPlexJWTLogin | None = None
        self._login_error: str | None = None
        self._account: MyPlexAccount | None = None
        self._pending_servers: list[dict[str, Any]] = []

    def public_status(self, plex: PlexService | None) -> dict[str, Any]:
        data = store.load_connection()
        status = store.public_status(data, plex_ready=plex is not None)
        if plex is not None and not status.get("server_name"):
            try:
                status["server_name"] = getattr(plex.server, "friendlyName", None)
            except Exception:
                pass
        return status

    def start_connect(self, forward_url: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self._login is not None:
                try:
                    self._login.stop()
                except Exception:
                    pass
            self._login = None
            self._login_error = None
            self._account = None
            self._pending_servers = []

            client_id = store.ensure_client_identifier()
            priv, pub = store.keypair_paths()
            headers = _device_headers(client_id)

            # Reuse the persisted device keypair when present. Regenerating (or
            # calling generateKeypair with overwrite=False) fails after the first
            # Connect and can trip Plex "legacy token disabled" / device mismatch.
            existing = priv.exists() and pub.exists()
            login = MyPlexJWTLogin(
                oauth=True,
                headers=headers,
                scopes=DEFAULT_SCOPES,
                keypair=(str(priv), str(pub)) if existing else None,
            )
            if not existing:
                login.generateKeypair(keyfiles=(str(priv), str(pub)), overwrite=False)
            for path in (priv, pub):
                if path.exists():
                    store._chmod_secret(path)

            login.run(timeout=180)
            auth_url = login.oauthUrl(forwardUrl=forward_url)
            self._login = login
            store.save_connection(
                {
                    "client_identifier": client_id,
                    "updated_at": _utcnow_iso(),
                }
            )
            return {"auth_url": auth_url, "state": "pending"}

    def connect_status(self) -> dict[str, Any]:
        with self._lock:
            login = self._login
            if self._login_error:
                return {"state": "error", "detail": self._login_error}
            if login is None:
                data = store.load_connection()
                if data and data.get("jwt_token"):
                    return {"state": "authorized", "detail": "Already authorized"}
                return {"state": "idle"}

            if getattr(login, "expired", False):
                return {"state": "expired", "detail": "Login timed out"}

            token = getattr(login, "jwtToken", None)
            finished = bool(getattr(login, "finished", False))
            if token or finished:
                try:
                    self._finalize_login(login)
                except Exception as exc:
                    self._login_error = str(exc)
                    log.exception("Failed to finalize Plex JWT login")
                    return {"state": "error", "detail": str(exc)}
                return {"state": "authorized"}

            return {"state": "pending"}

    def _finalize_login(self, login: MyPlexJWTLogin) -> None:
        token = login.jwtToken
        if not token:
            raise RuntimeError("Plex authorization finished without a JWT")
        account = MyPlexAccount(token=token)
        username = getattr(account, "username", None) or getattr(account, "email", None)
        friendly = getattr(account, "friendlyName", None) or username
        store.save_connection(
            {
                "mode": "jwt",
                "jwt_token": token,
                "username": username,
                "friendly_name": friendly,
                "updated_at": _utcnow_iso(),
            }
        )
        self._account = account
        self._pending_servers = self._list_servers_from_account(account)
        # Keep login object for a moment then clear active poller.
        try:
            login.stop()
        except Exception:
            pass
        self._login = None

    def _list_servers_from_account(self, account: MyPlexAccount) -> list[dict[str, Any]]:
        servers: list[dict[str, Any]] = []
        for resource in account.resources():
            provides = getattr(resource, "provides", "") or ""
            if "server" not in str(provides):
                continue
            servers.append(
                {
                    "id": getattr(resource, "clientIdentifier", None) or getattr(resource, "id", None),
                    "name": getattr(resource, "name", None) or "Plex Media Server",
                    "product": getattr(resource, "product", None),
                    "owned": bool(getattr(resource, "owned", False)),
                    "provides": provides,
                }
            )
        return servers

    def _ensure_account(self) -> MyPlexAccount:
        if self._account is not None:
            return self._account
        data = store.load_connection()
        if not data or not data.get("jwt_token"):
            raise RuntimeError("Plex account is not linked")
        priv, pub = store.keypair_paths()
        jwt_token = data["jwt_token"]
        if priv.exists() and pub.exists():
            login = MyPlexJWTLogin(
                jwtToken=jwt_token,
                keypair=(str(priv), str(pub)),
                headers=_device_headers(data.get("client_identifier") or store.ensure_client_identifier()),
                scopes=DEFAULT_SCOPES,
            )
            if not login.verifyJWT(refreshWithinDays=1):
                jwt_token = login.refreshJWT()
                store.save_connection({"jwt_token": jwt_token, "updated_at": _utcnow_iso()})
        self._account = MyPlexAccount(token=jwt_token)
        return self._account

    def list_servers(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._pending_servers:
                return list(self._pending_servers)
            account = self._ensure_account()
            self._pending_servers = self._list_servers_from_account(account)
            return list(self._pending_servers)

    def _find_resource(self, account: MyPlexAccount, server_id: str):
        for resource in account.resources():
            provides = getattr(resource, "provides", "") or ""
            if "server" not in str(provides):
                continue
            rid = getattr(resource, "clientIdentifier", None) or getattr(resource, "id", None)
            if str(rid) == str(server_id):
                return resource
        raise ValueError(f"Plex server '{server_id}' not found on this account")

    def list_sections(self, server_id: str) -> list[dict[str, Any]]:
        with self._lock:
            account = self._ensure_account()
            resource = self._find_resource(account, server_id)
            # Prefer local HTTP so Cast devices can pull streams from the same LAN URL.
            server = resource.connect(ssl=False, locations=["local", "remote", "relay"])
            sections = []
            for section in server.library.sections():
                stype = getattr(section, "type", None) or getattr(section, "TYPE", None)
                if stype and str(stype).lower() not in ("artist", "music"):
                    # Keep non-music listed but mark type so UI can prefer music.
                    pass
                sections.append(
                    {
                        "title": section.title,
                        "type": str(stype) if stype else "unknown",
                        "uuid": getattr(section, "uuid", None),
                    }
                )
            return sections

    def select_server(self, server_id: str, section: str) -> PlexService:
        with self._lock:
            account = self._ensure_account()
            resource = self._find_resource(account, server_id)
            server = resource.connect(ssl=False, locations=["local", "remote", "relay"])
            # Validate section exists
            try:
                server.library.section(section)
            except Exception as exc:
                raise ValueError(f"Music library '{section}' not found on this server") from exc

            url = str(server._baseurl).rstrip("/")
            token = server._token
            plex = PlexService(url, token, section)
            plex.refresh()

            store.save_connection(
                {
                    "mode": "jwt",
                    "server_name": getattr(resource, "name", None) or getattr(server, "friendlyName", None),
                    "server_client_id": str(server_id),
                    "server_url": url,
                    "access_token": token,
                    "music_section": section,
                    "username": getattr(account, "username", None),
                    "friendly_name": getattr(account, "friendlyName", None),
                    "updated_at": _utcnow_iso(),
                }
            )
            self._pending_servers = []
            return plex

    def disconnect(self) -> None:
        with self._lock:
            if self._login is not None:
                try:
                    self._login.stop()
                except Exception:
                    pass
            self._login = None
            self._login_error = None
            self._account = None
            self._pending_servers = []
            store.clear_connection()

    def refresh_library(self, plex: PlexService | None) -> dict[str, Any]:
        if plex is None:
            raise RuntimeError("Plex is not linked")
        return plex.refresh()

    def build_plex_from_store(self) -> PlexService | None:
        """Reconnect using saved JWT connection (refresh JWT if needed)."""
        data = store.load_connection()
        if not data or data.get("mode") != "jwt":
            return None
        if not data.get("server_url") or not data.get("music_section"):
            return None

        jwt_token = data.get("jwt_token")
        access_token = data.get("access_token")
        priv, pub = store.keypair_paths()
        client_id = data.get("client_identifier") or store.ensure_client_identifier()

        if jwt_token and priv.exists() and pub.exists():
            try:
                login = MyPlexJWTLogin(
                    jwtToken=jwt_token,
                    keypair=(str(priv), str(pub)),
                    headers=_device_headers(client_id),
                    scopes=DEFAULT_SCOPES,
                )
                if not login.verifyJWT(refreshWithinDays=1):
                    jwt_token = login.refreshJWT()
                    store.save_connection({"jwt_token": jwt_token, "updated_at": _utcnow_iso()})
                # Re-resolve server via account for a fresh access token when possible.
                account = MyPlexAccount(token=jwt_token)
                self._account = account
                server_id = data.get("server_client_id")
                if server_id:
                    try:
                        resource = self._find_resource(account, server_id)
                        server = resource.connect(ssl=False, locations=["local", "remote", "relay"])
                        url = str(server._baseurl).rstrip("/")
                        token = server._token
                        store.save_connection(
                            {
                                "server_url": url,
                                "access_token": token,
                                "updated_at": _utcnow_iso(),
                            }
                        )
                        plex = PlexService(url, token, data["music_section"])
                        plex.refresh()
                        return plex
                    except Exception as exc:
                        log.warning("Could not re-connect Plex resource; using stored URL: %s", exc)
            except Exception as exc:
                log.warning("Plex JWT refresh/reconnect failed; trying stored URL: %s", exc)

        token = access_token or jwt_token
        if not token:
            return None
        plex = PlexService(data["server_url"], token, data["music_section"])
        plex.refresh()
        return plex

    def build_plex_from_env(self, settings: Settings) -> PlexService | None:
        token = settings.plex_token.get_secret_value() if settings.plex_token else ""
        if not token:
            return None
        plex = PlexService(settings.plex_url, token, settings.plex_music_section)
        plex.refresh()
        store.save_connection(
            {
                "mode": "env",
                "server_url": settings.plex_url.rstrip("/"),
                "music_section": settings.plex_music_section,
                "server_name": settings.plex_url,
                "updated_at": _utcnow_iso(),
            }
        )
        return plex


def init_plex_service(settings: Settings, auth: PlexAuthService, attempts: int = 5, base_delay_sec: float = 1.0):
    """Try JWT store, then env token, with bounded retries. Returns None if unlinked."""
    last_exc: Exception | None = None

    # Prefer saved JWT connection when present.
    data = store.load_connection()
    if data and data.get("mode") == "jwt" and data.get("jwt_token"):
        for attempt in range(1, attempts + 1):
            try:
                plex = auth.build_plex_from_store()
                if plex is not None:
                    if attempt > 1:
                        log.info("Plex JWT connection succeeded on attempt %d/%d", attempt, attempts)
                    return plex
                break
            except Exception as exc:
                last_exc = exc
                if attempt >= attempts:
                    break
                delay = base_delay_sec * (2 ** (attempt - 1))
                log.warning(
                    "Plex JWT startup attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
        log.warning("Saved Plex JWT connection could not be restored: %s", last_exc)

    token = settings.plex_token.get_secret_value() if settings.plex_token else ""
    if token:
        for attempt in range(1, attempts + 1):
            try:
                plex = auth.build_plex_from_env(settings)
                if attempt > 1:
                    log.info("Plex env token connection succeeded on attempt %d/%d", attempt, attempts)
                return plex
            except Exception as exc:
                last_exc = exc
                if attempt >= attempts:
                    break
                delay = base_delay_sec * (2 ** (attempt - 1))
                log.warning(
                    "Plex env startup attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
        raise RuntimeError(f"Plex unavailable after {attempts} startup attempts: {last_exc}") from last_exc

    log.info("Plex is not linked. Use Settings → Connect with Plex, or set PLEX_TOKEN in the environment.")
    return None
