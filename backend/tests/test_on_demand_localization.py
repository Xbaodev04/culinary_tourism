"""Tier 1.5 contract, disk cache, concurrency and failure regressions (no network)."""
import asyncio
from datetime import timedelta

import pytest

from app.core import database
from app.core.config import settings
from app.modules.audio import service as audio_module
from app.modules.audio import task_manager as task_module
from app.modules.audio.service import SynthResult, TranslationRateLimited, cache_key
from app.modules.audio.source_version import source_fingerprint
from app.modules.audio.task_store import now
from app.modules.localization import service as localization_module

ENDPOINT = "/api/v1/localizations/on-demand"


@pytest.fixture
def speech(monkeypatch, tmp_path):
    class FakeSpeech:
        def __init__(self):
            self.translations = []
            self.syntheses = []
            self.during_synthesis = None
            self.error = None

        async def translate(self, text, lang):
            if self.error:
                raise self.error
            self.translations.append((text, lang))
            return text if lang == "vi" else f"[{lang}] {text}"

        async def synthesize(self, text, lang):
            self.syntheses.append((text, lang))
            if self.during_synthesis:
                await self.during_synthesis()
            await asyncio.sleep(0.01)
            name = f"{cache_key(text, lang)}.mp3"
            path = tmp_path / name
            hit = path.exists()
            path.write_bytes(b"ID3-test-audio")
            return SynthResult(f"/static/audio/{name}", hit)

    fake = FakeSpeech()
    monkeypatch.setattr(task_module, "tts_service", fake)
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    return fake


async def make_poi(**overrides):
    poi = {"name": "Quan Oc", "original_description": "Thuyet minh tieng Viet", "is_active": True}
    poi.update(overrides)
    result = await database.get_db()["poi"].insert_one(poi)
    return result.inserted_id


async def request_audio(client, poi_id, lang="en"):
    return await client.post(ENDPOINT, json={"poi_id": str(poi_id), "lang": lang})


async def test_generates_persists_then_hits_cache_and_mobile_can_read(client, speech):
    poi_id = await make_poi()
    first = await request_audio(client, poi_id)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["lang"] == "en" and data["is_fallback"] is False
    assert data["translated_text"] == "[en] Thuyet minh tieng Viet"
    assert data["audio_status"] == "completed" and data["cache_hit"] is False
    loc = await database.get_db()["poi_localizations"].find_one({"poi_id": poi_id, "lang": "en"})
    assert loc["source_hash"] == source_fingerprint("Quan Oc", "Thuyet minh tieng Viet")
    assert loc["audio_url"] == data["audio_url"]
    second = await request_audio(client, poi_id)
    assert second.status_code == 200 and second.json()["cache_hit"] is True
    assert len(speech.syntheses) == 1
    mobile = await client.get(f"/api/v1/mobile/pois/{poi_id}/content", headers={"Accept-Language": "en"})
    content = mobile.json()["data"][0]
    assert content["audio_url"] == data["audio_url"] and content["is_fallback"] is False


@pytest.mark.parametrize("payload", [
    {}, {"poi_id": "x"}, {"poi_id": " ", "lang": "en"},
    {"poi_id": "x", "lang": "fr"}, {"poi_id": "x", "lang": "en", "text": "untrusted"},
])
async def test_invalid_contract_rejected(client, payload):
    assert (await client.post(ENDPOINT, json=payload)).status_code == 422


@pytest.mark.parametrize("overrides,status", [
    ({"is_active": False}, 404), ({"original_description": ""}, 422),
    ({"original_description": "x" * 10001}, 422),
])
async def test_unavailable_source_rejected(client, speech, overrides, status):
    poi_id = await make_poi(**overrides)
    assert (await request_audio(client, poi_id)).status_code == status
    assert not speech.syntheses


async def test_unknown_poi_returns_404(client, speech):
    assert (await request_audio(client, "not-a-real-poi")).status_code == 404


async def test_fresh_manual_translation_is_used_without_redoing_translation(client, speech):
    poi_id = await make_poi()
    await database.get_db()["poi_localizations"].insert_one({
        "poi_id": poi_id, "lang": "en", "title": "Reviewed title", "description": "Reviewed narration",
        "source_hash": source_fingerprint("Quan Oc", "Thuyet minh tieng Viet"),
        "translation_origin": "manual", "audio_status": "pending",
    })
    result = await request_audio(client, poi_id)
    assert result.status_code == 200, result.text
    assert result.json()["translated_text"] == "Reviewed narration"
    assert not speech.translations
    assert speech.syntheses == [("Reviewed narration", "en")]


async def test_stale_source_invalidates_previous_audio(client, speech):
    poi_id = await make_poi()
    first = (await request_audio(client, poi_id)).json()
    await database.get_db()["poi"].update_one({"_id": poi_id}, {"$set": {"original_description": "New source"}})
    second = await request_audio(client, poi_id)
    assert second.status_code == 200
    assert second.json()["translated_text"] == "[en] New source"
    assert second.json()["audio_url"] != first["audio_url"]


async def test_missing_disk_cache_is_regenerated_without_retranslation(client, speech, tmp_path):
    poi_id = await make_poi()
    first = (await request_audio(client, poi_id)).json()
    (tmp_path / first["audio_url"].rsplit("/", 1)[-1]).unlink()
    translations = len(speech.translations)
    second = await request_audio(client, poi_id)
    assert second.status_code == 200 and second.json()["cache_hit"] is False
    assert len(speech.translations) == translations and len(speech.syntheses) == 2


async def test_concurrent_requests_generate_once_and_release_leases(client, speech):
    poi_id = await make_poi()
    responses = await asyncio.gather(*(request_audio(client, poi_id) for _ in range(4)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert len(speech.syntheses) == 1
    assert sum(r.json()["cache_hit"] for r in responses) == 3
    assert await database.get_db()["localization_generation_leases"].count_documents({}) == 0


@pytest.mark.parametrize("error,status", [(RuntimeError("upstream"), 502), (TranslationRateLimited("429"), 503)])
async def test_provider_failure_does_not_publish_success(client, speech, error, status):
    poi_id = await make_poi()
    speech.error = error
    result = await request_audio(client, poi_id)
    assert result.status_code == status
    if status == 503:
        assert result.headers["retry-after"] == "90"
    assert await database.get_db()["poi_localizations"].count_documents({"poi_id": poi_id}) == 0
    assert await database.get_db()["localization_generation_leases"].count_documents({}) == 0


async def test_source_changed_during_synthesis_is_not_published(client, speech):
    poi_id = await make_poi()

    async def edit():
        await database.get_db()["poi"].update_one({"_id": poi_id}, {"$set": {"original_description": "Edited"}})

    speech.during_synthesis = edit
    assert (await request_audio(client, poi_id)).status_code == 409
    assert await database.get_db()["poi_localizations"].count_documents({"poi_id": poi_id}) == 0


async def test_rate_limit_works_without_redis(client, speech, monkeypatch):
    monkeypatch.setattr(settings, "ON_DEMAND_RATE_LIMIT_PER_10MIN", 2)
    monkeypatch.setattr(database, "redis_client", None)
    poi_id = await make_poi()
    assert (await request_audio(client, poi_id)).status_code == 200
    assert (await request_audio(client, poi_id)).status_code == 200
    limited = await request_audio(client, poi_id)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0


async def test_timeout_releases_both_leases(client, speech, monkeypatch):
    poi_id = await make_poi()
    monkeypatch.setattr(localization_module, "ON_DEMAND_TIMEOUT_SECONDS", 0.05)

    async def slow():
        await asyncio.sleep(1)

    speech.during_synthesis = slow
    assert (await request_audio(client, poi_id)).status_code == 504
    db = database.get_db()
    assert await db["localization_generation_leases"].count_documents({}) == 0
    assert await db["audio_worker_slots"].count_documents({"token": {"$exists": True}}) == 0
    assert await db["poi_localizations"].count_documents({"poi_id": poi_id}) == 0


async def test_expired_generation_lease_can_be_recovered(client, speech):
    poi_id = await make_poi()
    await database.get_db()["localization_generation_leases"].insert_one({
        "_id": f"{poi_id}:en", "token": "crashed", "expires_at": now() - timedelta(seconds=1),
    })
    assert (await request_audio(client, poi_id)).status_code == 200


async def test_content_without_audio_triggers_fallback(client):
    poi_id = await make_poi()
    response = await client.get(f"/api/v1/mobile/pois/{poi_id}/content")
    content = response.json()["data"][0]
    assert content["audio_url"] is None and content["is_fallback"] is True


async def test_live_generation_lease_is_not_stolen(client, speech, monkeypatch):
    poi_id = await make_poi()
    monkeypatch.setattr(localization_module, "ON_DEMAND_TIMEOUT_SECONDS", 0.05)
    leases = database.get_db()["localization_generation_leases"]
    await leases.insert_one({"_id": f"{poi_id}:en", "token": "live-owner",
                             "expires_at": now() + timedelta(seconds=90)})
    assert (await request_audio(client, poi_id)).status_code == 504
    assert not speech.syntheses
    assert (await leases.find_one({"_id": f"{poi_id}:en"}))["token"] == "live-owner"


async def test_on_demand_waits_for_global_admin_audio_slots(client, speech, monkeypatch):
    poi_id = await make_poi()
    monkeypatch.setattr(localization_module, "ON_DEMAND_TIMEOUT_SECONDS", 0.05)
    slots = database.get_db()["audio_worker_slots"]
    for i in range(3):
        await slots.insert_one({"_id": f"slot-{i}", "token": "admin-worker",
                                "expires_at": now() + timedelta(seconds=90)})
    assert (await request_audio(client, poi_id)).status_code == 504
    assert not speech.syntheses
    assert await slots.count_documents({"token": "admin-worker"}) == 3


async def test_concurrent_manual_edit_is_preserved(client, speech):
    poi_id = await make_poi()
    localizations = database.get_db()["poi_localizations"]
    await localizations.insert_one({
        "poi_id": poi_id, "lang": "en", "title": "Reviewed title", "description": "Old manual narration",
        "source_hash": source_fingerprint("Quan Oc", "Thuyet minh tieng Viet"),
        "translation_origin": "manual", "audio_status": "pending",
    })

    async def edit():
        await localizations.update_one({"poi_id": poi_id, "lang": "en"},
                                       {"$set": {"description": "New manual narration"}})

    speech.during_synthesis = edit
    assert (await request_audio(client, poi_id)).status_code == 409
    loc = await localizations.find_one({"poi_id": poi_id, "lang": "en"})
    assert loc["description"] == "New manual narration" and loc["audio_status"] == "pending"
    assert not loc.get("audio_url")
