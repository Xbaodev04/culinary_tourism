"""Mobile visits retained for 30 minutes after the last heartbeat, across workers."""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.database import get_db

PRESENCE_SECONDS = 30 * 60


def stamp():
    return datetime.now(timezone.utc)


async def touch_session(session_id, device_id):
    if device_id.startswith("admin-"):
        return
    current = stamp()
    device_key = hmac.new(settings.SECRET_KEY.encode(), device_id.encode(), hashlib.sha256).hexdigest()
    await get_db()["mobile_presence"].update_one({"_id": session_id}, {"$set": {
        "device_key": device_key, "last_seen": current,
        "expires_at": current + timedelta(seconds=PRESENCE_SECONDS),
    }}, upsert=True)


async def online_snapshot():
    current = stamp()
   
    devices = await get_db()["mobile_presence"].distinct("device_key", {"expires_at": {"$gt": current}})
    
    return {"online_count": len(devices), "generated_at": current.isoformat(),
            "expires_after_seconds": PRESENCE_SECONDS, "unit": "devices"}
#   count_document


# async def online_snapshot():
#     current = stamp()

#     count = await get_db()["mobile_presence"].count_documents(
#         {"expires_at": {"$gt": current}}
#     )

#     return {
#         "online_count": count,
#         "generated_at": current.isoformat(),
#         "expires_after_seconds": PRESENCE_SECONDS,
#         "unit": "sessions"
#     }