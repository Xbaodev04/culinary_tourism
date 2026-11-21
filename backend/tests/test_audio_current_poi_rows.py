"""Audio Studio shows current work once per POI while retaining job history."""
import json
from datetime import timedelta

import pytest
from bson import ObjectId

from app.core.database import get_db
from app.modules.audio.sse import audio_tasks_event_stream
from app.modules.audio.task_store import audio_task_store, now


async def job(pid, status, age=0, name="Cơm gà Tư Điền"):
    stamp = now() - timedelta(minutes=age)
    return (await get_db()["audio_tasks"].insert_one({
        "poi_id": pid, "poi_name": name, "status": status, "languages": ["vi", "en", "zh", "ja", "ko"],
        "done": 5 if status == "completed" else 0, "total": 5,
        "created_at": stamp, "updated_at": stamp,
    })).inserted_id


async def test_rest_and_stream_both_replace_old_row_for_same_poi(client, admin_creds):
    pid = str(ObjectId())
    old = await job(pid, "completed", age=10)
    current = await job(pid, "queued")
    assert (await client.post("/api/v1/admin/auth/login", json=admin_creds)).status_code == 200
    response = await client.get("/api/v1/admin/audio-tasks")
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert len(rows) == 1 and rows[0]["id"] == str(current)
    assert rows[0]["poi_id"] == pid and rows[0]["status"] == "queued"
    assert "_poi_key" not in rows[0] and "_active_priority" not in rows[0]

    class Connected:
        async def is_disconnected(self):
            return False

    stream = audio_tasks_event_stream(Connected(), interval=.01)
    try:
        await anext(stream)  # retry hint
        frame = await anext(stream)
        assert json.loads(frame.split("data: ")[1]) == rows
        await audio_task_store.update(str(current), status="completed", done=5)
        updated = json.loads((await anext(stream)).split("data: ")[1])
        assert len(updated) == 1 and updated[0]["id"] == str(current)
        assert updated[0]["done"] == 5 and updated[0]["status"] == "completed"
    finally:
        await stream.aclose()
    assert await audio_task_store.get(str(old)) is not None
    assert await get_db()["audio_tasks"].count_documents({"poi_id": pid}) == 2


@pytest.mark.parametrize("status", ["queued", "running", "paused"])
async def test_active_work_has_priority_over_newer_finished_history(client, status):
    pid = str(ObjectId())
    active = await job(pid, status, age=20)
    await job(pid, "completed")
    rows = await audio_task_store.list()
    assert len(rows) == 1 and rows[0]["id"] == str(active)


async def test_poi_references_are_grouped_by_id_not_restaurant_name(client):
    first, second = ObjectId(), ObjectId()
    await job(str(first), "completed", age=10)
    current = await job(first, "completed", age=5)
    other = await job(str(second), "completed")  # Same name, different real restaurant.
    rows = await audio_task_store.list()
    assert {row["id"] for row in rows} == {str(current), str(other)}


async def test_unique_poi_limit_is_applied_after_grouping(client):
    first, second = str(ObjectId()), str(ObjectId())
    await job(second, "completed", age=60)
    newest = None
    for age in range(55, 0, -1):
        newest = await job(first, "completed", age=age)
    rows = await audio_task_store.list(limit=2)
    assert len(rows) == 2 and rows[0]["id"] == str(newest)
    assert {row["poi_id"] for row in rows} == {first, second}


async def test_latest_failed_refresh_is_not_replaced_by_old_success(client):
    pid = str(ObjectId())
    await job(pid, "completed", age=10)
    failed = await job(pid, "failed")
    rows = await audio_task_store.list()
    assert len(rows) == 1 and rows[0]["id"] == str(failed) and rows[0]["status"] == "failed"
