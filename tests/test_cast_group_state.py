from types import SimpleNamespace
import time

from plex_cast_bridge.cast_group_state import CastGroupState


def _state():
    state = CastGroupState()
    now = time.monotonic()
    member = "10000000-0000-0000-0000-000000000001"
    group_a = "20000000-0000-0000-0000-000000000001"
    group_b = "20000000-0000-0000-0000-000000000002"
    state.census_complete = True
    state.census_at = now
    state.memberships[member] = {group_a}
    state.groups[group_a] = SimpleNamespace(socket_client=SimpleNamespace(is_connected=True))
    state.membership_status[group_a] = now
    state.cast_status[group_a] = now
    state.media[group_a] = ("IDLE", now)
    return state, member, group_a, group_b, now


def test_empty_discovery_is_not_proof_of_no_groups():
    state, member, *_ = _state()
    state.census_complete = False
    state.census_at = None
    assert state.classify(member) == "unknown"


def test_no_group_inventory_requires_two_stable_nonempty_discoveries():
    state = CastGroupState()
    member = "10000000-0000-0000-0000-000000000001"
    state.register_member(member)
    state.reconcile([], complete=False, inventory_uuids=set())
    assert state.classify(member) == "unknown"
    state.reconcile([], complete=True, inventory_uuids={member})
    assert state.classify(member) == "unknown"
    state.reconcile([], complete=True, inventory_uuids={member})
    assert state.classify(member) == "clear"


def test_overlapping_memberships_block_when_any_group_is_active():
    state, member, _a, group_b, now = _state()
    state.memberships[member].add(group_b)
    state.groups[group_b] = SimpleNamespace(socket_client=SimpleNamespace(is_connected=True))
    state.membership_status[group_b] = now
    state.cast_status[group_b] = now
    state.media[group_b] = ("PLAYING", now)
    assert state.classify(member) == "active"


def test_paused_group_session_is_still_conflicting_activity():
    state, member, group, *_ = _state()
    state.media[group] = ("PAUSED", time.monotonic())
    assert state.classify(member) == "active"


def test_stale_or_disconnected_group_status_is_unknown():
    state, member, group, *_ = _state()
    state.media[group] = ("IDLE", time.monotonic() - 1000)
    assert state.classify(member) == "unknown"
    state.media[group] = ("IDLE", time.monotonic())
    state.groups[group].socket_client.is_connected = False
    assert state.classify(member) == "unknown"


def test_fresh_membership_census_and_idle_group_status_clear_member():
    state, member, *_ = _state()
    assert state.classify(member) == "clear"


def test_member_without_group_membership_requires_fresh_group_census():
    state, member, group, *_ = _state()
    state.memberships[member] = set()
    assert state.classify(member) == "clear"
    state.membership_status[group] = time.monotonic() - 1000
    assert state.classify(member) == "unknown"


def test_group_controller_removal_cleans_state_and_reconnection_is_rediscovered(monkeypatch):
    state = CastGroupState()

    class FakeListener:
        def __init__(self):
            self.controller = SimpleNamespace(
                update_members=lambda: None,
                reset_members=lambda: None,
                tear_down=lambda: None,
            )

    monkeypatch.setattr("plex_cast_bridge.cast_group_state._GroupListener", lambda _owner, _cast: FakeListener())

    class FakeCast:
        uuid = "20000000-0000-0000-0000-000000000001"
        socket_client = SimpleNamespace(is_connected=True)
        multizone_controller = SimpleNamespace(update_members=lambda: None)

        def __init__(self):
            self.disconnected = False

        def wait(self, timeout):
            assert timeout == 2

        def disconnect(self):
            self.disconnected = True

    first = FakeCast()
    state.reconcile([first], complete=True)
    assert str(first.uuid) in state.groups
    state.media[str(first.uuid)] = ("PLAYING", time.monotonic())
    state.reconcile([], complete=False)
    assert str(first.uuid) in state.groups
    assert not first.disconnected
    state.reconcile([], complete=False)
    assert not first.disconnected
    state.reconcile([], complete=True)
    assert first.disconnected
    assert str(first.uuid) not in state.groups
    second = FakeCast()
    state.reconcile([second], complete=True)
    assert state.groups[str(second.uuid)] is second
    state.close()
