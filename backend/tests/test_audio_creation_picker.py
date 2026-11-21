"""Only POIs needing audio are selectable; default creation covers all five voices."""
import pytest
from bson import ObjectId

from app.core.database import get_db
from app.modules.admin import audio_preview
from app.modules.audio.service import DEFAULT_NEURAL_VOICES, cache_key
from app.modules.audio.source_version import source_fingerprint
from app.modules.audio.task_manager import audio_task_manager
from app.modules.audio.task_store import audio_task_store

TASKS = "/api/v1/admin/audio-tasks"


@pytest.fixture
async def picker(client, admin_creds, monkeypatch, tmp_path):
    monkeypatch.setattr(audio_preview, "AUDIO_DIR", tmp_path)
    monkeypatch.setattr(audio_task_manager, "_start", lambda _: None)
    assert (await client.post("/api/v1/admin/auth/login", json=admin_creds)).status_code == 200
    return get_db(), tmp_path


async def poi(name="Quán cá kho"):
    doc = {"name": name, "original_description": "Thuyết minh", "address": "Vĩnh Khánh"}
    doc["_id"] = (await get_db()["poi"].insert_one(doc.copy())).inserted_id
    return doc


async def ready(poi, directory, *, omit=None, missing_file=None, status="completed", source=None):
    for lang in DEFAULT_NEURAL_VOICES:
        if lang == omit:
            continue
        filename = cache_key(poi["original_description"], lang) + ".mp3"
        if lang != missing_file:
            (directory / filename).write_bytes(b"ID3-test-audio")
        await get_db()["poi_localizations"].insert_one({
            "poi_id": poi["_id"], "lang": lang, "audio_status": status,
            "audio_url": "/static/audio/" + filename,
            "source_hash": source or source_fingerprint(poi["name"], poi["original_description"]),
        })


async def test_completed_five_language_audio_is_excluded_and_stale_selection_is_rejected(client, picker):
    _, directory = picker
    complete = await poi()
    await ready(complete, directory)
    missing = await poi("Quán chưa có audio")
    result = await client.get(TASKS + "/eligible-pois")
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 1
    assert result.json()["items"][0]["id"] == str(missing["_id"])
    response = await client.post(TASKS, json={"poi_id": str(complete["_id"])})
    assert response.status_code == 409
    assert await get_db()["audio_tasks"].count_documents({}) == 0


@pytest.mark.parametrize("options", [
    {"omit": "ko"}, {"missing_file": "en"}, {"status": "failed"}, {"source": "outdated-description"},
])
async def test_incomplete_invalid_or_outdated_audio_remains_repairable(client, picker, options):
    _, directory = picker
    target = await poi()
    await ready(target, directory, **options)
    result = (await client.get(TASKS + "/eligible-pois")).json()
    assert result["total"] == 1 and result["items"][0]["id"] == str(target["_id"])


@pytest.mark.parametrize("status", ["queued", "running", "paused"])
async def test_active_tasks_are_excluded_from_picker(client, picker, status):
    target = await poi()
    task_id = await audio_task_store.create(str(target["_id"]), target["name"], ["en"])
    await audio_task_store.update(task_id, status=status)
    assert (await client.get(TASKS + "/eligible-pois")).json()["total"] == 0
    assert (await client.post(TASKS, json={"poi_id": str(target["_id"])})).status_code == 409


async def test_completed_task_history_without_ready_files_does_not_hide_a_poi(client, picker):
    target = await poi()
    task_id = await audio_task_store.create(str(target["_id"]), target["name"], ["en"])
    await audio_task_store.update(task_id, status="completed")
    assert (await client.get(TASKS + "/eligible-pois")).json()["total"] == 1


async def test_default_creation_needs_only_poi_id_and_excludes_new_task(client, picker):
    target = await poi()
    response = await client.post(TASKS, json={"poi_id": str(target["_id"])})
    assert response.status_code == 202, response.text
    task = await audio_task_store.get(response.json()["id"])
    assert task["languages"] == list(DEFAULT_NEURAL_VOICES) and task["total"] == 5
    assert (await client.get(TASKS + "/eligible-pois")).json()["total"] == 0


async def test_search_and_pagination_apply_after_eligibility_filtering_across_batches(client, picker):
    _, directory = picker
    complete = await poi("Quán 000")
    await ready(complete, directory)
    await get_db()["poi"].insert_many([
        {"name": f"Quán {index:03}", "original_description": "Description"} for index in range(1, 206)
    ])
    first = (await client.get(TASKS + "/eligible-pois", params={"q": "quan", "limit": 2})).json()
    assert first["total"] == 205
    assert [p["name"] for p in first["items"]] == ["Quán 001", "Quán 002"]
    last = (await client.get(TASKS + "/eligible-pois", params={"q": "quan", "limit": 2, "skip": 204})).json()
    assert last["total"] == 205 and [p["name"] for p in last["items"]] == ["Quán 205"]


async def test_creation_picker_requires_admin_authentication(client):
    assert (await client.get(TASKS + "/eligible-pois")).status_code == 401
