"""Registration narration reaches audio; publication is an Admin decision."""
import asyncio
import json

import pytest
from bson import ObjectId
from cryptography.fernet import Fernet

from app.core import database
from app.core.config import settings
from app.core.security import hash_password
from app.modules.audio import task_manager as audio_module
from app.modules.audio.task_store import audio_task_store
from app.modules.audio.service import SynthResult
from app.modules.owner import service as owner_module

REGISTER = "/api/v1/admin/auth/register-owner"
POIS = "/api/v1/admin/pois"
NARRATION = "Quán nổi tiếng với món cá kho, không gian gia đình và câu chuyện ẩm thực địa phương."


def payload():
    return {"full_name": "Nguyễn Văn An", "phone": "0901234567", "email": "narration-owner@example.com",
            "password": "StrongOwnerPassword123!", "cccd_number": "079201001234",
            "poi_name": "Quán cá kho", "poi_address": "12 Vĩnh Khánh, Quận 4",
            "poi_description": NARRATION, "trigger_radius": 65,
            "poi_location": {"type": "Point", "coordinates": [106.703, 10.76]}}


@pytest.fixture(autouse=True)
def encryption(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(owner_module, "IMAGE_DIR", tmp_path)


async def admin_login(client, admin_creds):
    assert (await client.post("/api/v1/admin/auth/login", json=admin_creds)).status_code == 200


@pytest.mark.parametrize("description", [None, "", " \n ", "x" * 8001])
@pytest.mark.parametrize("upload", [False, True])
async def test_registration_requires_nonempty_description(client, description, upload):
    body = payload()
    if description is None:
        body.pop("poi_description")
    else:
        body["poi_description"] = description
    if upload:
        response = await client.post(REGISTER + "/upload", data={"payload": json.dumps(body)},
            files={"image": ("shop.png", b"\x89PNG\r\n\x1a\nimage", "image/png")})
    else:
        response = await client.post(REGISTER, json=body)
    assert response.status_code == 422, response.text
    assert await database.get_db()["poi_owner_registrations"].count_documents({}) == 0


@pytest.mark.parametrize("upload", [False, True])
async def test_description_is_saved_reviewed_and_used_by_audio(client, admin_creds, monkeypatch, upload):
    body = payload()
    body["poi_description"] = "  " + NARRATION + "  "
    if upload:
        registered = await client.post(REGISTER + "/upload", data={"payload": json.dumps(body)},
            files={"image": ("shop.png", b"\x89PNG\r\n\x1a\nimage", "image/png")})
    else:
        registered = await client.post(REGISTER, json=body)
    assert registered.status_code == 201, registered.text
    db = database.get_db()
    registration = await db["poi_owner_registrations"].find_one({"poi_name": body["poi_name"]})
    assert registration["poi_description"] == NARRATION
    assert registration["trigger_radius"] == 65
    await admin_login(client, admin_creds)
    listing = (await client.get("/api/v1/admin/registrations")).json()["items"]
    assert listing[0]["poi_description"] == NARRATION
    assert listing[0]["trigger_radius"] == 65
    reviewed = await client.post(f"/api/v1/admin/registrations/{registration['_id']}/review", json={"action": "approve"})
    assert reviewed.status_code == 200, reviewed.text
    poi_id = reviewed.json()["poi_id"]
    poi = await db["poi"].find_one({"_id": ObjectId(poi_id)})
    assert poi["original_description"] == NARRATION
    assert poi["trigger_radius"] == 65

    spoken = []
    class Speech:
        async def synthesize(self, text, lang):
            spoken.append((text, lang))
            return SynthResult("/static/audio/test.mp3", False)
    monkeypatch.setattr(audio_module, "tts_service", Speech())
    manager = audio_module.AudioTaskManager()
    try:
        task_id = await manager.enqueue_poi_audio_task(poi_id, ["vi"])
        for _ in range(200):
            task = await audio_task_store.get(task_id)
            if task["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(.01)
        assert task["status"] == "completed", task
        assert spoken == [(NARRATION, "vi")]
    finally:
        await manager.shutdown()


async def test_matching_poi_receives_registration_description_and_invalidates_old_audio(client, admin_creds):
    body = payload()
    old = (await database.get_db()["poi"].insert_one({"name": body["poi_name"], "address": body["poi_address"],
            "original_description": "Old generated title and address", "revision": 2})).inserted_id
    await database.get_db()["poi_localizations"].insert_one({"poi_id": old, "lang": "en", "description": "Old English",
            "audio_status": "completed", "audio_url": "/static/audio/old.mp3"})
    assert (await client.post(REGISTER, json=body)).status_code == 201
    reg = await database.get_db()["poi_owner_registrations"].find_one({})
    await admin_login(client, admin_creds)
    result = await client.post(f"/api/v1/admin/registrations/{reg['_id']}/review", json={"action": "approve"})
    assert result.status_code == 200, result.text
    assert result.json()["poi_id"] == str(old)
    assert (await database.get_db()["poi"].find_one({"_id": old}))["original_description"] == NARRATION
    assert (await database.get_db()["poi"].find_one({"_id": old}))["trigger_radius"] == 65
    english = await database.get_db()["poi_localizations"].find_one({"poi_id": old, "lang": "en"})
    assert english["audio_status"] == "pending" and english["audio_url"] is None


async def test_legacy_description_can_be_completed_explicitly_without_fabricating_text(client, admin_creds):
    assert (await client.post(REGISTER, json=payload())).status_code == 201
    db = database.get_db()
    reg = await db["poi_owner_registrations"].find_one({})
    await db["poi_owner_registrations"].update_one({"_id": reg["_id"]}, {"$set": {"poi_description": " "}})
    await admin_login(client, admin_creds)
    url = f"/api/v1/admin/registrations/{reg['_id']}/review"
    assert (await client.post(url, json={"action": "approve"})).status_code == 422
    result = await client.post(url, json={"action": "approve", "poi_description": NARRATION})
    assert result.status_code == 200, result.text
    assert (await db["poi"].find_one({"_id": ObjectId(result.json()["poi_id"])}))["original_description"] == NARRATION


async def test_admin_can_publish_and_edit_without_english_or_audio(client, admin_creds):
    await admin_login(client, admin_creds)
    source = payload()
    created = await client.post(POIS, json={"name": source["poi_name"], "address": source["poi_address"],
        "ward": "Phường 10", "location": source["poi_location"], "original_description": NARRATION})
    assert created.status_code == 201
    pid = created.json()["id"]
    assert await database.get_db()["poi_localizations"].count_documents({"poi_id": ObjectId(pid)}) == 0
    for active in (True, False, True):
        result = await client.patch(f"{POIS}/{pid}", json={"is_active": active})
        assert result.status_code == 200 and result.json()["is_active"] is active
    changed = await client.patch(f"{POIS}/{pid}", json={"original_description": NARRATION + " Thực đơn theo mùa."})
    assert changed.status_code == 200 and changed.json()["is_active"] is True
    mobile = await client.get("/api/v1/mobile/pois", params={"search": source["poi_name"]})
    assert mobile.status_code == 200 and pid in str(mobile.json())


async def test_unpinned_registration_draft_uses_owner_description(client, admin_creds):
    body = payload()
    body.pop("poi_location")
    assert (await client.post(REGISTER, json=body)).status_code == 201
    reg = await database.get_db()["poi_owner_registrations"].find_one({})
    await admin_login(client, admin_creds)
    result = await client.post(f"/api/v1/admin/registrations/{reg['_id']}/review", json={
        "action": "approve", "draft": {"ward": "Phường 10", "location": payload()["poi_location"]},
    })
    assert result.status_code == 200, result.text
    poi = await database.get_db()["poi"].find_one({"_id": ObjectId(result.json()["poi_id"])})
    assert poi["original_description"] == NARRATION
    assert poi["trigger_radius"] == 65


@pytest.mark.parametrize("radius", [4, 501, 30.5, None])
@pytest.mark.parametrize("upload", [False, True])
async def test_registration_rejects_invalid_trigger_radius(client, radius, upload):
    body = {**payload(), "trigger_radius": radius}
    if upload:
        response = await client.post(REGISTER + "/upload", data={"payload": json.dumps(body)},
            files={"image": ("shop.png", b"\x89PNG\r\n\x1a\nimage", "image/png")})
    else:
        response = await client.post(REGISTER, json=body)
    assert response.status_code == 422, response.text
    assert await database.get_db()["poi_owner_registrations"].count_documents({}) == 0


async def test_registration_radius_default_preserves_legacy_clients(client):
    body = payload()
    body.pop("trigger_radius")
    assert (await client.post(REGISTER, json=body)).status_code == 201
    registration = await database.get_db()["poi_owner_registrations"].find_one({})
    assert registration["trigger_radius"] == 30


async def test_publication_still_requires_admin_toggle_permission(client):
    db = database.get_db()
    await db["roles"].insert_one({"name": "content_editor", "priority": 3,
        "permissions": ["poi:read", "poi:create", "poi:update"]})
    await db["users"].insert_one({"email": "editor@example.com", "role": "content_editor", "is_active": True,
        "password_hash": hash_password("StrongEditorPassword123!")})
    assert (await client.post("/api/v1/admin/auth/login", json={
        "email": "editor@example.com", "password": "StrongEditorPassword123!"})).status_code == 200
    body = {"name": "Editor restaurant", "address": "12 Vĩnh Khánh", "ward": "Phường 10",
        "location": payload()["poi_location"], "original_description": NARRATION}
    hidden = await client.post(POIS, json=body)
    assert hidden.status_code == 201, hidden.text
    assert (await client.post(POIS, json={**body, "is_active": True})).status_code == 403
    assert (await client.patch(f"{POIS}/{hidden.json()['id']}", json={"is_active": True})).status_code == 403
