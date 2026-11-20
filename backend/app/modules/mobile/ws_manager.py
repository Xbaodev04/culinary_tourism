"""Connection ownership is per session; counts are shared through MongoDB."""
from uuid import uuid4

from app.modules.mobile.presence import online_snapshot, touch_session


class ConnectionManager:
    def __init__(self):
        self.active_connections = {}
        self.online_users = {}

    async def connect(self, websocket, device_id, user_id=None, platform=None):
        await websocket.accept()
        session_id = uuid4().hex
        self.active_connections[session_id] = websocket
        self.online_users[session_id] = {"device_id": device_id, "platform": platform}
        try:
            await self.heartbeat(session_id)
            await self.broadcast_online_count()
        except Exception:
            await self.disconnect(session_id)
            raise
        return session_id

    async def heartbeat(self, session_id):
        info = self.online_users.get(session_id)
        if info:
            await touch_session(session_id, info["device_id"])

    async def disconnect(self, session_id):
        self.active_connections.pop(session_id, None)
        self.online_users.pop(session_id, None)
        # Release the socket, but retain the visit until its 30-minute expiry.

    async def broadcast_online_count(self):
        snapshot = await online_snapshot()
        # Broadcast aggregates only; clients never receive other device IDs.
        for session_id, websocket in list(self.active_connections.items()):
            try:
                await websocket.send_json({"type": "online_count", "count": snapshot["online_count"]})
            except Exception:
                await self.disconnect(session_id)

    async def get_online_count(self):
        return (await online_snapshot())["online_count"]


manager = ConnectionManager()
