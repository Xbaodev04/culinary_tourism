"""Cloud media contracts: uploads, restart-safe cache, auth, URL safety and migration.

No Cloudinary credentials or network needed; mock only the external SDK boundary.
"""
import asyncio
from hashlib import sha256
from pathlib import Path
from threading import get_ident

import cloudinary.uploader
import pytest
from fastapi import HTTPException

from app.core import database, media_storage
from app.core.config import settings
from app.modules.audio import service as audio_module
from app.modules.audio.service import TTSService, cache_key
from app.modules.audio.task_manager import AudioTaskManager
from app.modules.owner import service as owner_service
from scripts.migrate_media_to_cloudinary import migrate


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_STORAGE", "cloudinary")
    monkeypatch.setattr(settings, "CLOUDINARY_CLOUD_NAME", "test-cloud")
    monkeypatch.setattr(settings, "CLOUDINARY_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CLOUDINARY_API_SECRET", "test-secret")
    monkeypatch.setattr(settings, "CLOUDINARY_FOLDER", "quan4")
    calls = []

    def upload(source, **options):
        content = source.read() if hasattr(source, "read") else Path(source).read_bytes()
        calls.append({**options, "content": content, "thread": get_ident()})
        resource = options["resource_type"]
        extension = "mp3" if resource == "video" else "png"
        return {"public_id": options["public_id"], "resource_type": resource, "bytes": len(content),
                "secure_url": f"https://res.cloudinary.com/test-cloud/{resource}/upload/v123/{options['public_id']}.{extension}"}

    monkeypatch.setattr(cloudinary.uploader, "upload", upload)
    return calls


async def test_images_upload_and_keep_metadata_without_local_disk(client, cloud, tmp_path, monkeypatch):
    monkeypatch.setattr(owner_service, "IMAGE_DIR", tmp_path / "unused")
    content = b"\x89PNG\r\n\x1a\nimage-data"
    url = await owner_service.store_menu_image(content)
    assert url.startswith("https://res.cloudinary.com/test-cloud/image/upload/")
    assert not (tmp_path / "unused").exists()
    assert len(cloud) == 1 and cloud[0]["thread"] != get_ident()
    assert cloud[0]["public_id"] == f"quan4/images/menu/{sha256(content).hexdigest()}"
    assert cloud[0]["overwrite"] is False
    metadata = await database.get_db()["media_assets"].find_one({"secure_url": url})
    assert metadata["public_id"] == cloud[0]["public_id"]
    assert metadata["bytes"] == len(content)
    assert await owner_service.store_menu_image(content) == url
    assert len(cloud) == 1


async def test_image_validation_and_provider_failure_do_not_fall_back_to_disk(client, cloud, monkeypatch):
    for content, status in [(b"bad-image", 415), (b"x" * (5 * 1024 * 1024 + 1), 413)]:
        with pytest.raises(HTTPException) as error:
            await owner_service.store_menu_image(content)
        assert error.value.status_code == status
    assert not cloud

    def fail(*args, **kwargs):
        raise RuntimeError("provider error with test-secret")

    monkeypatch.setattr(cloudinary.uploader, "upload", fail)
    with pytest.raises(HTTPException) as error:
        await owner_service.store_menu_image(b"\x89PNG\r\n\x1a\nvalid")
    assert error.value.status_code == 502
    assert "test-secret" not in str(error.value)
    assert await database.get_db()["media_assets"].count_documents({}) == 0


async def test_audio_survives_fresh_service_and_empty_disk(client, cloud, tmp_path, monkeypatch):
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    generations = []

    async def generate(self, text, lang, file):
        generations.append((text, lang))
        await asyncio.sleep(0.01)
        file.write_bytes(b"ID3cloud-mp3")

    monkeypatch.setattr(TTSService, "_generate", generate)
    service = TTSService()
    results = await asyncio.gather(*(service.synthesize("Xin chao", "vi") for _ in range(3)))
    assert [result.cache_hit for result in results] == [False, True, True]
    assert len(generations) == len(cloud) == 1
    assert cloud[0]["resource_type"] == "video"
    assert list(tmp_path.iterdir()) == []
    # A fresh process has neither service locks nor files, but Mongo retains the cache.
    again = await TTSService().synthesize("Xin chao", "vi")
    assert again.cache_hit and again.url == results[0].url
    assert len(generations) == 1


async def test_local_audio_promoted_without_tts_and_failed_upload_not_cached(client, cloud, tmp_path, monkeypatch):
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    digest = cache_key("Old local audio", "vi")
    file = tmp_path / f"{digest}.mp3"
    file.write_bytes(b"ID3-old-local")

    def fail(*args, **kwargs):
        raise RuntimeError("test-secret")

    with monkeypatch.context() as patch:
        patch.setattr(cloudinary.uploader, "upload", fail)
        with pytest.raises(media_storage.MediaStorageError):
            await TTSService().synthesize("Old local audio", "vi")
    assert await database.get_db()["media_assets"].count_documents({}) == 0
    result = await TTSService().synthesize("Old local audio", "vi")
    assert result.cache_hit and result.url.endswith(f"{digest}.mp3")
    assert file.read_bytes() == b"ID3-old-local"


async def test_language_task_persists_remote_audio_and_reuses_it(client, cloud, tmp_path, monkeypatch):
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    generations = []

    async def generate(text, lang, file):
        generations.append(lang)
        file.write_bytes(b"ID3-language")

    monkeypatch.setattr(audio_module.tts_service, "_generate", generate)
    poi = {"name": "Quan an", "original_description": "Mon an Viet Nam"}
    poi["_id"] = (await database.get_db()["poi"].insert_one(poi)).inserted_id
    result = await AudioTaskManager._process_language(poi, poi["original_description"], "vi")
    saved = await database.get_db()["poi_localizations"].find_one({"poi_id": poi["_id"]})
    assert saved["audio_status"] == "completed" and saved["audio_url"] == result.url
    cached = await AudioTaskManager._process_language(poi, poi["original_description"], "vi")
    assert cached.cache_hit and cached.url == result.url and generations == ["vi"]


async def test_admin_audio_redirects_only_after_auth_and_rejects_foreign_urls(client, admin_creds, cloud, tmp_path):
    file = tmp_path / "sample.mp3"
    file.write_bytes(b"ID3-preview")
    asset = await media_storage.upload_audio(file, "a" * 32)
    poi_id = (await database.get_db()["poi"].insert_one({"name": "Cloud POI"})).inserted_id
    localizations = database.get_db()["poi_localizations"]
    await localizations.insert_one({"poi_id": poi_id, "lang": "vi", "audio_status": "completed", "audio_url": asset.url})
    base = f"/api/v1/admin/pois/{poi_id}/audios"
    assert (await client.get(f"{base}/vi/play")).status_code == 401
    assert (await client.post("/api/v1/admin/auth/login", json=admin_creds)).status_code == 200
    items = (await client.get(base)).json()["items"]
    assert items[0]["ready"] and items[0]["size_bytes"] == len(b"ID3-preview")
    played = await client.get(f"{base}/vi/play", follow_redirects=False)
    assert played.status_code == 307 and played.headers["location"] == asset.url
    downloaded = await client.get(f"{base}/vi/download", follow_redirects=False)
    assert downloaded.status_code == 307
    assert f"fl_attachment:quan4_{poi_id}_vi/" in downloaded.headers["location"]
    for url in [asset.url.replace("test-cloud", "other-cloud"),
                asset.url.replace("res.cloudinary.com", "res.cloudinary.com.evil.test"),
                asset.url.replace("/upload/", "/upload/../"),
                asset.url.replace("a" * 32, "b" * 32),
                asset.url + "?redirect=http://localhost", "https://[invalid", "file:///etc/passwd"]:
        await localizations.update_one({"poi_id": poi_id}, {"$set": {"audio_url": url}})
        assert (await client.get(f"{base}/vi/play")).status_code == 404
    await localizations.update_one({"poi_id": poi_id}, {"$set": {"audio_url": asset.url, "audio_status": "pending"}})
    assert (await client.get(f"{base}/vi/play")).status_code == 404


async def test_public_tts_uses_get_redirect_for_cloud_audio(client, cloud, monkeypatch, tmp_path):
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)

    async def generate(text, lang, file):
        file.write_bytes(b"ID3-public")

    monkeypatch.setattr(audio_module.tts_service, "_generate", generate)
    response = await client.post("/api/v1/audio/tts", json={"text": "Hello", "lang": "en"}, follow_redirects=False)
    assert response.status_code == 303  # A 307 would incorrectly replay POST against the CDN.
    assert response.headers["location"].startswith("https://res.cloudinary.com/test-cloud/video/upload/")


async def test_failed_upload_never_completes_language_or_leaves_temporary_files(client, cloud, tmp_path, monkeypatch):
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)

    async def generate(text, lang, file):
        file.write_bytes(b"ID3-upload-fails")

    def fail(*args, **kwargs):
        raise RuntimeError("test-secret")

    monkeypatch.setattr(audio_module.tts_service, "_generate", generate)
    monkeypatch.setattr(cloudinary.uploader, "upload", fail)
    poi = {"name": "Cloud error", "original_description": "Thuyet minh"}
    poi["_id"] = (await database.get_db()["poi"].insert_one(poi)).inserted_id
    with pytest.raises(media_storage.MediaStorageError):
        await AudioTaskManager._process_language(poi, poi["original_description"], "vi")
    assert await database.get_db()["poi_localizations"].count_documents({"audio_status": "completed"}) == 0
    assert await database.get_db()["media_assets"].count_documents({}) == 0
    assert list(tmp_path.iterdir()) == []


async def test_invalid_provider_url_is_not_registered(client, cloud, monkeypatch):
    def invalid(*args, **kwargs):
        return {"public_id": kwargs["public_id"], "resource_type": "image",
                "secure_url": "https://example.com/not-our-asset.png"}

    monkeypatch.setattr(cloudinary.uploader, "upload", invalid)
    with pytest.raises(media_storage.MediaStorageError):
        await media_storage.upload_image(b"png", "menu", "d" * 64)
    assert await database.get_db()["media_assets"].count_documents({}) == 0


async def test_migration_dry_run_then_apply_handles_nested_references(client, cloud, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STATIC_DIR", str(tmp_path))
    image = tmp_path / "images" / "poi" / "old.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\nmigrate-image")
    audio = tmp_path / "audio" / f"{'c' * 32}.mp3"
    audio.parent.mkdir()
    audio.write_bytes(b"ID3-migrate")
    poi_id = (await database.get_db()["poi"].insert_one({
        "images": ["/static/images/poi/old.png", "/static/images/poi/missing.png"],
    })).inserted_id
    await database.get_db()["poi_submissions"].insert_one({"proposed_changes": {"images": ["/static/images/poi/old.png"]}})
    await database.get_db()["poi_localizations"].insert_one({"audio_url": f"/static/audio/{audio.name}"})
    dry = await migrate()
    assert dry["uploaded"] == dry["updated_documents"] == 0 and dry["missing"] == 1
    assert not cloud
    applied = await migrate(apply=True)
    assert applied["uploaded"] == 2 and applied["updated_documents"] == 3 and applied["missing"] == 1
    poi = await database.get_db()["poi"].find_one({"_id": poi_id})
    assert poi["images"][0].startswith("https://res.cloudinary.com/")
    assert poi["images"][1] == "/static/images/poi/missing.png"
    assert image.exists() and audio.exists()
    assert (await migrate(apply=True))["updated_documents"] == 0 and len(cloud) == 2


def test_cloudinary_configuration_fails_early(cloud, monkeypatch):
    media_storage.validate_media_config()
    monkeypatch.setattr(settings, "CLOUDINARY_API_SECRET", "")
    with pytest.raises(RuntimeError, match="CLOUDINARY_API_SECRET"):
        media_storage.validate_media_config()
