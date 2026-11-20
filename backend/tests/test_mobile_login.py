"""Mobile login with production-like indexes and both UUID/ObjectId accounts."""
import asyncio

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from google.auth.exceptions import TransportError

from app.core import database
from app.core.config import settings
from app.core.database import ensure_user_email_index
from app.modules.mobile.services import auth_service as auth_module

BASE = "/api/v1/mobile/auth"


async def test_email_index_migrates_without_losing_email_uniqueness(client):
    users = database.get_db().users
    await users.create_index("email", unique=True, name="email_unique")
    await users.insert_one({"_id": "old-guest", "device_id": "legacy-device"})
    await ensure_user_email_index(database.get_db())
    await ensure_user_email_index(database.get_db())
    await users.insert_one({"_id": "new-guest", "device_id": "new-device"})
    await users.insert_one({"_id": "null-guest", "email": None})
    with pytest.raises(DuplicateKeyError):
        await users.insert_one({"email": settings.SUPERADMIN_EMAIL.lower()})
    assert "email_unique" not in await users.index_information()


async def test_multiple_devices_login_and_receive_real_profile(client):
    await ensure_user_email_index(database.get_db())
    await database.get_db().users.create_index("username", unique=True, sparse=True, name="username_unique")
    ids = []
    for device in ["installation-one", "installation-two"]:
        response = await client.post(f"{BASE}/login-device", json={"device_id": device})
        assert response.status_code == 200, response.text
        profile = await client.get(f"{BASE}/me", headers={"Authorization": f"Bearer {response.json()['accessToken']}"})
        assert profile.status_code == 200
        ids.append(profile.json()["data"]["id"])
    assert ids[0] != ids[1]


async def test_concurrent_device_login_reuses_one_account(client):
    await ensure_user_email_index(database.get_db())
    responses = await asyncio.gather(*(client.post(f"{BASE}/login-device", json={"device_id": "same-device"}) for _ in range(5)))
    assert all(response.status_code == 200 for response in responses)
    assert await database.get_db().users.count_documents({"device_id": "same-device"}) == 1


@pytest.mark.parametrize("payload", [{}, {"device_id": " "}, {"device_id": "x" * 257}])
async def test_empty_or_invalid_device_is_rejected(client, payload):
    assert (await client.post(f"{BASE}/login-device", json=payload)).status_code == 422


async def test_mobile_refresh_uses_refresh_signing_key(client, monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET", "mobile-access-test-key-12345678901234567890")
    monkeypatch.setattr(settings, "REFRESH_TOKEN_SECRET", "mobile-refresh-test-key-12345678901234567890")
    login = (await client.post(f"{BASE}/login-device", json={"device_id": "refresh-device"})).json()
    refreshed = await client.post(f"{BASE}/refresh", json={"refreshToken": login["refreshToken"]})
    assert refreshed.status_code == 200, refreshed.text
    me = await client.get(f"{BASE}/me", headers={"Authorization": f"Bearer {refreshed.json()['accessToken']}"})
    assert me.status_code == 200
    bad = await client.post(f"{BASE}/refresh", json={"refreshToken": login["accessToken"]})
    assert bad.status_code == 401


@pytest.fixture
def google_identity(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-web-client.apps.googleusercontent.com")
    identity = {"sub": "google-subject", "email": "visitor@example.com", "email_verified": True, "name": "Visitor"}

    def verify(token, request, audience):
        assert token == "test-google-token"
        assert audience == settings.GOOGLE_CLIENT_ID
        return identity

    monkeypatch.setattr(auth_module.google_id_token, "verify_oauth2_token", verify)
    return identity


async def test_google_login_then_me_and_repeat_login(client, google_identity):
    await ensure_user_email_index(database.get_db())
    ids = []
    for _ in range(2):
        result = await client.post(f"{BASE}/google-login", json={"idToken": "test-google-token"})
        assert result.status_code == 200, result.text
        me = await client.get(f"{BASE}/me", headers={"Authorization": f"Bearer {result.json()['accessToken']}"})
        assert me.status_code == 200
        ids.append(me.json()["data"]["id"])
    assert ids[0] == ids[1]


async def test_google_can_read_existing_objectid_profile(client, google_identity):
    oid = ObjectId()
    await database.get_db().users.insert_one({"_id": oid, "email": google_identity["email"], "full_name": "Existing user"})
    result = await client.post(f"{BASE}/google-login", json={"idToken": "test-google-token"})
    assert result.status_code == 200
    me = await client.get(f"{BASE}/me", headers={"Authorization": f"Bearer {result.json()['accessToken']}"})
    assert me.status_code == 200, me.text
    assert me.json()["data"]["id"] == str(oid)


async def test_google_requires_configured_audience(client, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "")
    result = await client.post(f"{BASE}/google-login", json={"idToken": "anything"})
    assert result.status_code == 503


async def test_google_invalid_token_is_rejected(client, google_identity, monkeypatch):
    def invalid(*args, **kwargs):
        raise ValueError("Invalid token")
    monkeypatch.setattr(auth_module.google_id_token, "verify_oauth2_token", invalid)
    assert (await client.post(f"{BASE}/google-login", json={"idToken": "bad"})).status_code == 401


async def test_google_rejects_unverified_email(client, google_identity):
    google_identity["email_verified"] = False
    assert (await client.post(f"{BASE}/google-login", json={"idToken": "test-google-token"})).status_code == 400


async def test_google_provider_network_failure_is_not_an_invalid_token(client, google_identity, monkeypatch):
    def unavailable(*args, **kwargs):
        raise TransportError("upstream unavailable")
    monkeypatch.setattr(auth_module.google_id_token, "verify_oauth2_token", unavailable)
    assert (await client.post(f"{BASE}/google-login", json={"idToken": "test-google-token"})).status_code == 503


async def test_disabled_device_account_cannot_login(client):
    await database.get_db().users.insert_one({"device_id": "disabled-device", "is_active": False})
    assert (await client.post(f"{BASE}/login-device", json={"device_id": "disabled-device"})).status_code == 403
