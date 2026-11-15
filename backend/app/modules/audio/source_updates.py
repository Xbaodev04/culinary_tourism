"""Transactional queue updates when approved POI narration sources change."""
from app.modules.audio.source_version import source_fingerprint
from app.modules.audio.task_store import ACTIVE_STATUSES, audio_task_store, now


async def refresh_audio_tasks(db, poi_id, name, description, *, session, task_filter=None, languages=None):
    stamp = now()
    query = {"poi_id": {"$in": [poi_id, str(poi_id)]}, **(task_filter or {})}
    for state in ("queued", "running", "paused"):
        await db["audio_tasks"].update_many(
            {**query, "status": state},
            {"$set": {
                "status": "paused" if state == "paused" else "queued",
                "poi_name": name, "source_hash": source_fingerprint(name, description),
                "done": 0, "cache_hits": 0, "current_language": None, "error": None,
                "translation_retries": 0, "speech_retries": 0,
                "source_refreshed_at": stamp, "updated_at": stamp,
                "refresh_reason": "Nội dung POI đã cập nhật; tác vụ sẽ tiếp tục theo phiên bản mới.",
                **({"languages": languages, "total": len(languages)} if languages is not None else {}),
            }, "$inc": {"source_refreshes": 1},
             "$unset": {"lease_token": "", "lease_until": "", "retry_at": "",
                        "retry_kind": "", "finished_at": ""}},
            session=session,
        )


async def ensure_description_audio_task(db, poi_id, name, description, *, session):
    """Persist an automatic five-language refresh in the approval transaction.

    Reuse active jobs, including a deliberately paused job; keep finished history.
    The supervisor resumes queued jobs even if the API process exits after commit.
    """
    from app.modules.audio.service import DEFAULT_NEURAL_VOICES
    languages = list(DEFAULT_NEURAL_VOICES)
    await refresh_audio_tasks(db, poi_id, name, description, session=session, languages=languages)
    active = await db["audio_tasks"].find_one({
        "poi_id": {"$in": [poi_id, str(poi_id)]}, "status": {"$in": list(ACTIVE_STATUSES)},
    }, session=session)
    if active:
        return str(active["_id"])
    return await audio_task_store.create(str(poi_id), name, languages, session=session, notify=False)
