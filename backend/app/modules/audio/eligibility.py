"""Current five-language audio readiness and the Admin creation picker."""
from app.core.database import get_db
from app.modules.admin.audio_preview import _safe_audio_file
from app.modules.admin.poi_management import vietnamese_search_regex
from app.modules.audio.service import DEFAULT_NEURAL_VOICES
from app.modules.audio.source_version import field_is_current
from app.modules.audio.task_store import ACTIVE_STATUSES


def has_complete_audio(poi, localizations):
    name = poi.get("name", "")
    description = (poi.get("original_description") or poi.get("description") or "").strip()
    by_lang = {loc.get("lang"): loc for loc in localizations}
    if not description:
        description = (by_lang.get("vi", {}).get("description") or "").strip()
    return all(
        lang in by_lang
        and field_is_current(by_lang[lang], "description", name, description)
        and _safe_audio_file(by_lang[lang]) is not None
        for lang in DEFAULT_NEURAL_VOICES
    )


async def poi_has_complete_audio(poi):
    pid = poi["_id"]
    localizations = [loc async for loc in get_db()["poi_localizations"].find({
        "poi_id": {"$in": [pid, str(pid)]}, "lang": {"$in": list(DEFAULT_NEURAL_VOICES)},
    })]
    return has_complete_audio(poi, localizations)


async def list_audio_candidates(q=None, limit=30, skip=0):
    """Filter before pagination; finished task history alone is not readiness.

    Batch related documents to avoid a query per POI. Missing/stale/deleted MP3s
    remain repairable, while active tasks and five ready languages are excluded.
    This list is deliberately uncached because task completion changes eligibility.
    """
    db = get_db()
    query = {"name": {"$regex": vietnamese_search_regex(q.strip()), "$options": "i"}} if q and q.strip() else {}
    cursor = db["poi"].find(query).sort([("name", 1), ("_id", 1)])
    total, items = 0, []
    while batch := await cursor.to_list(length=200):
        ids = [poi["_id"] for poi in batch]
        keys = ids + [str(pid) for pid in ids]
        active = {str(task["poi_id"]) async for task in db["audio_tasks"].find({
            "poi_id": {"$in": keys}, "status": {"$in": list(ACTIVE_STATUSES)},
        }, {"poi_id": 1})}
        localizations = {}
        async for loc in db["poi_localizations"].find({
            "poi_id": {"$in": keys}, "lang": {"$in": list(DEFAULT_NEURAL_VOICES)},
        }):
            localizations.setdefault(str(loc["poi_id"]), []).append(loc)
        for poi in batch:
            pid = str(poi["_id"])
            if pid in active or has_complete_audio(poi, localizations.get(pid, [])):
                continue
            if skip <= total < skip + limit:
                items.append({"id": pid, "name": poi.get("name", ""), "address": poi.get("address", "")})
            total += 1
    return {"items": items, "total": total, "limit": limit, "skip": skip}
