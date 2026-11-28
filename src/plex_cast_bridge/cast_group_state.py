"""Conservative, maintained Cast Group state for voice cold-start authorization."""

from __future__ import annotations

import threading
import time
from uuid import UUID

from pychromecast.controllers.media import MediaStatusListener
from pychromecast.controllers.multizone import (
    MultizoneController,
    MultiZoneControllerListener,
)
from pychromecast.controllers.receiver import CastStatusListener
from pychromecast.socket_client import ConnectionStatusListener, CONNECTION_STATUS_CONNECTED


MAX_GROUP_STATUS_AGE = 90.0
GROUP_REFRESH_INTERVAL = 30.0
MEDIA_NAMESPACE = "urn:x-cast:com.google.cast.media"


class _GroupListener(
    CastStatusListener,
    ConnectionStatusListener,
    MediaStatusListener,
    MultiZoneControllerListener,
):
    def __init__(self, owner: "CastGroupState", group_cast):
        self.owner = owner
        self.cast = group_cast
        self.group_uuid = str(group_cast.uuid)
        self.controller = MultizoneController(group_cast.uuid)
        group_cast.register_status_listener(self)
        group_cast.register_connection_listener(self)
        group_cast.media_controller.register_status_listener(self)
        group_cast.register_handler(self.controller)
        self.controller.register_listener(self)

    def new_cast_status(self, status):
        self.owner._cast_status(self.group_uuid)

    def new_connection_status(self, status):
        if status.status == CONNECTION_STATUS_CONNECTED:
            self.controller.update_members()
        else:
            self.controller.reset_members()

    def new_media_status(self, status):
        self.owner._media(self.group_uuid, getattr(status, "player_state", None))

    def load_media_failed(self, queue_item_id, error_code):
        pass

    def multizone_member_added(self, member_uuid):
        self.owner._membership(str(member_uuid), self.group_uuid, True)

    def multizone_member_removed(self, member_uuid):
        self.owner._membership(str(member_uuid), self.group_uuid, False)

    def multizone_status_received(self):
        self.owner._membership_status(self.group_uuid)

    def refresh(self):
        """Refresh status only over this existing connection; never launch an app."""
        socket_client = getattr(self.cast, "socket_client", None)
        if not getattr(socket_client, "is_connected", False):
            return
        try:
            self.controller.update_members()
            socket_client.receiver_controller.update_status(
                callback_function=lambda success, response: self.owner._cast_status(self.group_uuid)
                if success is True and isinstance(response, dict)
                and response.get("type") == "RECEIVER_STATUS"
                else None
            )
            namespaces = getattr(socket_client, "app_namespaces", ()) or ()
            if MEDIA_NAMESPACE in namespaces:
                self.cast.media_controller.update_status(
                    callback_function=lambda success, response: self.owner._media(
                        self.group_uuid,
                        (response.get("status") or [{}])[0].get("playerState")
                        if success is True and isinstance(response, dict)
                        and response.get("type") == "MEDIA_STATUS"
                        and isinstance(response.get("status"), list)
                        and response.get("status") else None,
                    )
                )
        except Exception:
            self.owner._expire_group(self.group_uuid)


class CastGroupState:
    """Own group Cast connections and maintain member/group status snapshots.

    A completed, nonempty mDNS census is required before a negative answer is
    trusted. Missing, stale, or disconnected group evidence is always unknown.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.groups: dict[str, object] = {}
        self.controllers: dict[str, _GroupListener] = {}
        self.memberships: dict[str, set[str]] = {}
        self.media: dict[str, tuple[str, float]] = {}
        self.cast_status: dict[str, float] = {}
        self.membership_status: dict[str, float] = {}
        self.census_at: float | None = None
        self.census_complete = False
        self._inventory_signature: frozenset[str] | None = None
        self._stable_census_count = 0
        self._refresh_stop = threading.Event()
        self._refresh_thread: threading.Thread | None = None

    def register_member(self, uuid) -> None:
        try:
            normalized = str(UUID(str(uuid)))
        except (ValueError, TypeError, AttributeError):
            return
        with self.lock:
            self.memberships.setdefault(normalized, set())

    def reconcile(
        self,
        groups: list[object],
        *,
        complete: bool,
        inventory_uuids: set[str] | None = None,
    ) -> None:
        """Attach newly discovered group controllers; remove vanished groups."""
        seen = {str(c.uuid): c for c in groups if getattr(c, "uuid", None) is not None}
        with self.lock:
            for group_uuid in (set(self.groups) - set(seen)) if complete else ():
                old = self.groups.pop(group_uuid, None)
                controller = self.controllers.pop(group_uuid, None)
                if controller is not None:
                    controller.controller.reset_members()
                    controller.controller.tear_down()
                self.media.pop(group_uuid, None)
                self.cast_status.pop(group_uuid, None)
                self.membership_status.pop(group_uuid, None)
                if old is not None:
                    try:
                        old.disconnect()
                    except Exception:
                        pass
            for group_uuid, cast in seen.items():
                if group_uuid not in self.groups:
                    self.groups[group_uuid] = cast
                    self.controllers[group_uuid] = _GroupListener(self, cast)
                    try:
                        cast.wait(timeout=2)
                        self.controllers[group_uuid].controller.update_members()
                    except Exception:
                        self.census_complete = False
                elif cast is not self.groups[group_uuid]:
                    # The maintained controller owns its socket across scans;
                    # close the duplicate returned by this mDNS browse.
                    try:
                        cast.disconnect()
                    except Exception:
                        pass
            inventory = frozenset(inventory_uuids or ())
            if complete and inventory:
                if inventory == self._inventory_signature:
                    self._stable_census_count += 1
                else:
                    self._inventory_signature = inventory
                    self._stable_census_count = 1
                self.census_at = time.monotonic()
                self.census_complete = self._stable_census_count >= 2
            else:
                self._stable_census_count = 0
                self._inventory_signature = None
                self.census_complete = False
                self.census_at = None
            if self.controllers and self._refresh_thread is None:
                self._refresh_thread = threading.Thread(
                    target=self._refresh_loop,
                    name="cast-group-status-refresh",
                    daemon=True,
                )
                self._refresh_thread.start()

    def _refresh_loop(self):
        while not self._refresh_stop.wait(GROUP_REFRESH_INTERVAL):
            with self.lock:
                controllers = list(self.controllers.values())
            for controller in controllers:
                try:
                    controller.refresh()
                except Exception:
                    self._expire_group(controller.group_uuid)

    def close(self):
        self.reconcile([], complete=True)
        self._refresh_stop.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=2)
            self._refresh_thread = None

    def _membership(self, member, group, added):
        try:
            member = str(UUID(str(member)))
        except (ValueError, TypeError, AttributeError):
            return
        with self.lock:
            memberships = self.memberships.setdefault(member, set())
            (memberships.add if added else memberships.discard)(str(group))

    def _media(self, group, player_state):
        with self.lock:
            self.media[str(group)] = (str(player_state or "UNKNOWN").upper(), time.monotonic())

    def _cast_status(self, group):
        with self.lock:
            self.cast_status[str(group)] = time.monotonic()

    def _expire_group(self, group):
        with self.lock:
            self.cast_status.pop(str(group), None)
            self.media.pop(str(group), None)
            self.membership_status.pop(str(group), None)

    def _membership_status(self, group):
        with self.lock:
            self.membership_status[str(group)] = time.monotonic()

    def classify(self, uuid) -> str:
        """Return clear, active, or unknown for a physical Cast member."""
        try:
            member = str(UUID(str(uuid)))
        except (ValueError, TypeError, AttributeError):
            return "unknown"
        now = time.monotonic()
        with self.lock:
            if not self.census_complete or self.census_at is None or now - self.census_at > MAX_GROUP_STATUS_AGE:
                return "unknown"
            memberships = self.memberships.get(member)
            if memberships is None:
                return "unknown"
            if not memberships:
                if any(
                    group not in self.membership_status
                    or now - self.membership_status[group] > MAX_GROUP_STATUS_AGE
                    for group in self.groups
                ):
                    return "unknown"
                return "clear"
            states = []
            for group in memberships:
                cast = self.groups.get(group)
                media = self.media.get(group)
                status_time = self.cast_status.get(group)
                if (cast is None or not getattr(getattr(cast, "socket_client", None), "is_connected", False)
                        or group not in self.membership_status
                        or now - self.membership_status[group] > MAX_GROUP_STATUS_AGE
                        or media is None or now - media[1] > MAX_GROUP_STATUS_AGE
                        or status_time is None or now - status_time > MAX_GROUP_STATUS_AGE):
                    return "unknown"
                states.append(media[0])
            if any(state in {"PLAYING", "PAUSED", "BUFFERING"} for state in states):
                return "active"
            if any(state not in {"IDLE", "FINISHED"} for state in states):
                return "unknown"
            return "clear"
