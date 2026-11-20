"""QR targets follow Admin publication, including legacy mobile aliases."""
import pytest
from bson import ObjectId

from app.core.database import get_db


@pytest.mark.parametrize("resource", ["pois", "stalls"])
@pytest.mark.parametrize("suffix", ["", "/content", "/content/available-languages"])
async def test_hidden_qr_target_returns_closed_without_content(client, resource, suffix):
    pid = (await get_db()["poi"].insert_one({
        "name": "Private restaurant", "original_description": "Private narration",
        "is_active": False, "is_open": True,
    })).inserted_id
    response = await client.get(f"/api/v1/mobile/{resource}/{pid}{suffix}")
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": {"code": "poi_not_public", "message": "Quán đã đóng cửa"}}
    assert "Private" not in response.text


@pytest.mark.parametrize("collection", ["poi", "pois"])
async def test_hidden_qr_slug_and_string_id_cannot_bypass_publication(client, collection):
    await get_db()[collection].insert_one({
        "_id": "legacy-restaurant", "slug": "quan-an", "name": "Private", "is_active": False,
    })
    for target in ("legacy-restaurant", "quan-an"):
        response = await client.get(f"/api/v1/mobile/pois/{target}")
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "poi_not_public"


@pytest.mark.parametrize("publication", [{"is_active": True}, {}])
async def test_public_and_legacy_qr_targets_open_normally(client, publication):
    pid = (await get_db()["poi"].insert_one({
        "name": "Quán công khai", "original_description": "Thuyết minh",
        # Opening hours are independent from the Admin publication switch.
        "is_open": False, **publication,
    })).inserted_id
    response = await client.get(f"/api/v1/mobile/pois/{pid}")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["id"] == str(pid)
    assert response.json()["data"]["is_active"] is True
    assert (await client.get(f"/api/v1/mobile/pois/{pid}/content")).status_code == 200


@pytest.mark.parametrize("resource", ["pois", "stalls"])
async def test_missing_qr_target_remains_not_found(client, resource):
    response = await client.get(f"/api/v1/mobile/{resource}/{ObjectId()}")
    assert response.status_code == 404
    assert "poi_not_public" not in response.text


async def test_qr_access_updates_immediately_when_admin_toggles_publication(client, admin_creds):
    assert (await client.post("/api/v1/admin/auth/login", json=admin_creds)).status_code == 200
    created = await client.post("/api/v1/admin/pois", json={
        "name": "QR restaurant", "address": "12 Vĩnh Khánh", "ward": "Phường 10",
        "location": {"type": "Point", "coordinates": [106.703, 10.76]},
        "original_description": "Thuyết minh của quán", "is_active": True,
    })
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    for active in (True, False, True):
        changed = await client.patch(f"/api/v1/admin/pois/{pid}", json={"is_active": active})
        assert changed.status_code == 200, changed.text
        detail = await client.get(f"/api/v1/mobile/pois/{pid}")
        assert detail.status_code == (200 if active else 403), detail.text
        listing = (await client.get("/api/v1/mobile/pois")).json()["data"]["items"]
        assert (pid in [poi["id"] for poi in listing]) is active
