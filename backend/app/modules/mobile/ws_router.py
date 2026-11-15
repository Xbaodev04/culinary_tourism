import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from app.core.rbac import require_permission
from app.modules.mobile.presence import online_snapshot
from app.modules.mobile.ws_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ws")
SOCKET_IDLE_SECONDS = 90


@router.websocket("/online/{device_id}")
async def websocket_online_endpoint(device_id: str, websocket: WebSocket):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", device_id):
        await websocket.close(code=1008)
        return
    session_id = None
    try:
        session_id = await manager.connect(websocket, device_id)
        while True:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=SOCKET_IDLE_SECONDS)
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(message, dict):
                continue
            action = message.get("action")
            if action == "disconnect":
                break
            if action in ("ping", "update_user"):
                await manager.heartbeat(session_id)
                await websocket.send_json({"type": "pong", "online_count": await manager.get_online_count()})
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception:
        logger.exception("Mobile presence connection failed")
    finally:
        if session_id:
            await manager.disconnect(session_id)
            await manager.broadcast_online_count()
        try:
            await websocket.close()
        except RuntimeError:
            pass


@router.get("/online/count", tags=["Mobile Client (App)"])
async def get_online_count():
    return await online_snapshot()


@router.get("/online/users", tags=["Mobile Client (App)"])
async def get_online_users(_: dict = Depends(require_permission("analytics:view"))):
    # Preserve the URL without exposing individual visitor identities.
    return {**await online_snapshot(), "users": []}
