from datetime import datetime, timedelta, timezone

from fastapi import WebSocketDisconnect

from app.core.database import get_db
from app.modules.mobile import presence, ws_router
from app.modules.mobile.ws_manager import ConnectionManager


class Socket:
    def __init__(self, messages=()):
        self.messages = iter(messages)
        self.frames = []
        self.closed = False

    async def accept(self):
        pass

    async def send_json(self, value):
        self.frames.append(value)

    async def receive_text(self):
        try:
            return next(self.messages)
        except StopIteration:
            raise WebSocketDisconnect()

    async def close(self, code=1000):
        self.closed = True


async def test_unique_devices_across_workers_and_old_disconnect_preserves_new_session(client):
    first, second = ConnectionManager(), ConnectionManager()
    old_socket, new_socket, other_socket = Socket(), Socket(), Socket()
    old = await first.connect(old_socket, "install_same")
    new = await second.connect(new_socket, "install_same")
    other = await first.connect(other_socket, "install_other")
    assert (await presence.online_snapshot())["online_count"] == 2
    await first.disconnect(old)
    assert (await presence.online_snapshot())["online_count"] == 2
    await second.disconnect(new)
    assert (await presence.online_snapshot())["online_count"] == 2
    await first.disconnect(other)
    assert (await presence.online_snapshot())["online_count"] == 2
    assert not first.active_connections and not second.active_connections
    assert not first.online_users and not second.online_users
    assert all(set(frame) == {"type", "count"} for frame in new_socket.frames)


async def test_expired_presence_is_excluded_before_ttl_cleanup_and_ping_renews(client, monkeypatch):
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(presence, "stamp", lambda: now)
    await presence.touch_session("session", "install_visitor")
    stored = await get_db()["mobile_presence"].find_one({})
    assert "install_visitor" not in str(stored)
    assert (await presence.online_snapshot())["online_count"] == 1
    now += timedelta(minutes=30)
    assert await get_db()["mobile_presence"].count_documents({}) == 1
    assert (await presence.online_snapshot())["online_count"] == 0
    await presence.touch_session("session", "install_visitor")
    assert (await presence.online_snapshot())["online_count"] == 1


async def test_admin_viewers_do_not_increase_mobile_count(client):
    manager = ConnectionManager()
    admin = await manager.connect(Socket(), "admin-dashboard")
    mobile = await manager.connect(Socket(), "install_mobile")
    assert await manager.get_online_count() == 1
    await manager.disconnect(admin)
    await manager.disconnect(mobile)


async def test_mobile_websocket_handles_ping_and_explicit_background_disconnect(client):
    socket = Socket(['[]', 'invalid-json', '{"action":"ping"}', '{"action":"disconnect"}'])
    await ws_router.websocket_online_endpoint("install_mobile", socket)
    assert {"type": "pong", "online_count": 1} in socket.frames
    assert socket.closed and (await presence.online_snapshot())["online_count"] == 1


async def test_dashboard_presence_is_permission_protected_and_aggregate_only(client, admin_creds):
    endpoint = "/api/v1/admin/dashboard/online-visitors"
    assert (await client.get(endpoint)).status_code == 401
    await presence.touch_session("active", "install_current")
    assert (await client.post("/api/v1/admin/auth/login", json=admin_creds)).status_code == 200
    response = await client.get(endpoint)
    assert response.status_code == 200
    assert response.json()["online_count"] == 1 and response.json()["unit"] == "devices"
    assert "install_current" not in response.text and "device_key" not in response.text
    assert response.json()["expires_after_seconds"] == 1800


async def test_abrupt_socket_disconnect_retains_visit(client):
    socket = Socket()
    await ws_router.websocket_online_endpoint("install_mobile", socket)
    assert (await presence.online_snapshot())["online_count"] == 1


async def test_invalid_device_identifier_does_not_create_presence(client):
    socket = Socket()
    await ws_router.websocket_online_endpoint("unknown device", socket)
    assert socket.closed and await get_db()["mobile_presence"].count_documents({}) == 0


async def test_sequential_devices_remain_counted_until_each_visit_expires(client, monkeypatch):
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(presence, "stamp", lambda: now)

    # The first visitor leaves before the second visitor opens the app.
    await ws_router.websocket_online_endpoint("install_first", Socket())
    now += timedelta(minutes=5)
    await ws_router.websocket_online_endpoint("install_second", Socket())
    assert (await presence.online_snapshot())["online_count"] == 2
    assert await get_db()["mobile_presence"].count_documents({}) == 2

    now += timedelta(minutes=25)  # First visit expires exactly at 30 minutes.
    assert (await presence.online_snapshot())["online_count"] == 1
    now += timedelta(minutes=5)
    assert (await presence.online_snapshot())["online_count"] == 0


async def test_reconnect_is_deduplicated_and_heartbeat_extends_visit(client, monkeypatch):
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(presence, "stamp", lambda: now)
    manager = ConnectionManager()
    original = await manager.connect(Socket(), "install_returning")
    await manager.disconnect(original)

    now += timedelta(minutes=10)
    current = await manager.connect(Socket(), "install_returning")
    assert (await presence.online_snapshot())["online_count"] == 1
    now += timedelta(minutes=25)
    await manager.heartbeat(current)
    await manager.disconnect(current)

    # Original and reconnect deadlines have passed, but the last ping renewed it.
    now += timedelta(minutes=29, seconds=59)
    assert (await presence.online_snapshot())["online_count"] == 1
    now += timedelta(seconds=1)
    assert (await presence.online_snapshot())["online_count"] == 0


async def test_socket_timeout_releases_connection_but_retains_visit(client, monkeypatch):
    async def timed_out_receive(self):
        raise TimeoutError()

    monkeypatch.setattr(Socket, "receive_text", timed_out_receive)
    socket = Socket()
    await ws_router.websocket_online_endpoint("install_timeout", socket)
    assert socket.closed
    assert (await presence.online_snapshot())["online_count"] == 1
    assert not ws_router.manager.active_connections
    assert not ws_router.manager.online_users
