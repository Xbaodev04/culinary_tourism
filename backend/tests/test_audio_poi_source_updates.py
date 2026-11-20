"""Approved owner edits must not turn valid audio work into failed tasks."""
import asyncio
from datetime import timedelta

import pytest
from bson import ObjectId
from pymongo.errors import OperationFailure

from app.core import database
from app.modules.admin.poi_management import update_poi, save_english
from app.modules.admin.schemas import POIUpdateRequest, EnglishLocalizationRequest
from app.modules.admin.submissions import review_submission
from app.modules.audio import service as speech_module, task_manager as manager_module
from app.modules.audio.service import SynthResult, cache_key
from app.modules.audio.source_version import source_fingerprint
from app.modules.audio.task_store import audio_task_store, now
from app.modules.owner.schemas import POISubmissionRequest
from app.modules.owner.service import create_poi_submission
from app.modules.owner.service import list_my_pois


class RecordingSpeech:
    def __init__(self, directory):
        self.directory = directory
        self.translations = []
        self.generated = []
        self.block_language = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def translate(self, text, lang):
        self.translations.append((text, lang))
        return f"[{lang}] {text}"

    async def synthesize(self, text, lang):
        if lang == self.block_language:
            self.block_language = None
            self.entered.set()
            await self.release.wait()
        file = self.directory / f"{cache_key(text, lang)}.mp3"
        hit = file.exists()
        if not hit:
            self.generated.append((text, lang))
            file.write_bytes(b"ID3-test-narration")
        return SynthResult(f"/static/audio/{file.name}", hit)


@pytest.fixture
async def setup_audio(client, monkeypatch, tmp_path):
    fake = RecordingSpeech(tmp_path)
    monkeypatch.setattr(manager_module, "tts_service", fake)
    monkeypatch.setattr(speech_module, "AUDIO_DIR", tmp_path)
    manager = manager_module.AudioTaskManager()
    owner = ObjectId()
    pid = (await database.get_db()["poi"].insert_one({
        "name": "Old restaurant", "original_description": "Original narration",
        "address": "Original address", "is_active": False, "owner_id": owner,
        "revision": 1, "created_at": now(), "updated_at": now(),
    })).inserted_id
    try:
        yield manager, fake, pid, owner
    finally:
        await manager.shutdown()


async def wait_task(task_id, state="completed"):
    for _ in range(400):
        task = await audio_task_store.get(task_id)
        if task["status"] == state:
            return task
        if task["status"] == "failed" and state != "failed":
            pytest.fail(task.get("error"))
        await asyncio.sleep(.01)
    pytest.fail(f"Task did not reach {state}: {task}")


async def approve(pid, owner, changes):
    db = database.get_db()
    poi = await db["poi"].find_one({"_id": pid})
    submission = (await db["poi_submissions"].insert_one({
        "poi_id": pid, "owner_id": owner, "proposed_changes": changes,
        "status": "pending", "poi_revision": poi["revision"], "created_at": now(),
    })).inserted_id
    return await review_submission(str(submission), "approve", "", poi["revision"], {"id": "test-admin"}, None)


async def test_sending_owner_proposal_leaves_poi_and_running_task_unchanged(setup_audio):
    manager, fake, pid, owner = setup_audio
    fake.block_language = "en"
    task_id = await manager.enqueue_poi_audio_task(str(pid), ["en"])
    await asyncio.wait_for(fake.entered.wait(), 2)
    before = await database.get_db()["poi"].find_one({"_id": pid})
    task_before = await audio_task_store.get(task_id)
    result = await create_poi_submission(
        POISubmissionRequest(poi_id=str(pid), proposed_changes={"name": "Proposed name"}),
        {"id": str(owner), "is_poi_owner_verified": True}, None,
    )
    assert result["status"] == "pending"
    assert await database.get_db()["poi"].find_one({"_id": pid}) == before
    after = await audio_task_store.get(task_id)
    assert after["source_hash"] == task_before["source_hash"] and after["status"] == "running"
    fake.release.set()
    await wait_task(task_id)


@pytest.mark.parametrize("changes", [
    {"address": "New address"}, {"images": ["/static/images/new.jpg"]}, {"audio_priority": 8}, {"trigger_radius": 75},
])
async def test_metadata_approval_during_synthesis_does_not_restart_or_fail(setup_audio, changes):
    manager, fake, pid, owner = setup_audio
    fake.block_language = "en"
    task_id = await manager.enqueue_poi_audio_task(str(pid), ["en"])
    await asyncio.wait_for(fake.entered.wait(), 2)
    await approve(pid, owner, changes)
    fake.release.set()
    task = await wait_task(task_id)
    assert task.get("source_refreshes", 0) == 0
    assert len(fake.generated) == 1


async def test_rename_preserves_existing_mp3_and_manual_narration(setup_audio):
    manager, fake, pid, owner = setup_audio
    await wait_task(await manager.enqueue_poi_audio_task(str(pid), ["vi", "en"]))
    db = database.get_db()
    old = {loc["lang"]: loc async for loc in db["poi_localizations"].find({"poi_id": pid})}
    # Also exercise lazy upgrading of old combined provenance.
    await db["poi_localizations"].update_one({"poi_id": pid, "lang": "en"}, {
        "$set": {"translation_origin": "manual"},
        "$unset": {"description_source_hash": "", "title_source_hash": ""},
    })
    await approve(pid, owner, {"name": "New restaurant"})
    for lang in ("vi", "en"):
        current = await db["poi_localizations"].find_one({"poi_id": pid, "lang": lang})
        assert current["audio_url"] == old[lang]["audio_url"]
        assert current["description"] == old[lang]["description"]
    generated = list(fake.generated)
    translations = len(fake.translations)
    task = await wait_task(await manager.enqueue_poi_audio_task(str(pid), ["vi", "en"]))
    assert fake.generated == generated and task["cache_hits"] == 2
    assert fake.translations[translations:] == [("New restaurant", "en")]
    en = await db["poi_localizations"].find_one({"poi_id": pid, "lang": "en"})
    assert en["title"] == "[en] New restaurant" and en["translation_origin"] == "manual"


@pytest.mark.parametrize("changes", [{"name": "New restaurant"}, {"original_description": "Updated narration"}])
async def test_source_approval_in_flight_refreshes_same_job_without_stale_writes(setup_audio, changes):
    manager, fake, pid, owner = setup_audio
    fake.block_language = "en"
    task_id = await manager.enqueue_poi_audio_task(str(pid), ["en"])
    await asyncio.wait_for(fake.entered.wait(), 2)
    await approve(pid, owner, changes)
    waiting = await audio_task_store.get(task_id)
    assert waiting["status"] == "queued" and waiting["done"] == 0
    assert not waiting.get("lease_token")
    fake.release.set()
    final = await wait_task(task_id)
    assert final["source_refreshes"] == 1
    assert await database.get_db()["audio_tasks"].count_documents({"poi_id": str(pid)}) == 1
    loc = await database.get_db()["poi_localizations"].find_one({"poi_id": pid, "lang": "en"})
    assert loc["title"] == "[en] " + changes.get("name", "Old restaurant")
    assert loc["description"] == "[en] " + changes.get("original_description", "Original narration")


async def test_source_change_while_paused_stays_paused_until_resume(setup_audio):
    manager, fake, pid, owner = setup_audio
    fake.block_language = "en"
    task_id = await manager.enqueue_poi_audio_task(str(pid), ["vi", "en"])
    await asyncio.wait_for(fake.entered.wait(), 2)
    await manager.control(task_id, "pause")
    await approve(pid, owner, {"original_description": "Updated narration"})
    fake.release.set()
    for _ in range(100):
        if task_id not in manager.workers:
            break
        await asyncio.sleep(.01)
    assert (await audio_task_store.get(task_id))["status"] == "paused"
    await manager.control(task_id, "resume")
    task = await wait_task(task_id)
    assert task["done"] == 5 and task["source_refreshes"] == 1


async def test_cancelling_refreshed_job_never_restarts_it(setup_audio):
    manager, fake, pid, owner = setup_audio
    fake.block_language = "en"
    task_id = await manager.enqueue_poi_audio_task(str(pid), ["en"])
    await asyncio.wait_for(fake.entered.wait(), 2)
    await approve(pid, owner, {"original_description": "Updated narration"})
    worker = manager.workers[task_id]
    await manager.control(task_id, "cancel")
    await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 2)
    await asyncio.sleep(0)  # allow the worker's completion callback to unregister it
    assert (await audio_task_store.get(task_id))["status"] == "cancelled"
    assert not manager.workers
    assert not await database.get_db()["poi_localizations"].find_one({"poi_id": pid, "lang": "en"})


@pytest.mark.parametrize("state", ["queued", "paused"])
async def test_checkpoint_after_approval_survives_recovery(setup_audio, state):
    manager, fake, pid, owner = setup_audio
    task_id = await audio_task_store.create(str(pid), "Old restaurant", ["en"])
    await audio_task_store.update(task_id, status=state, done=1,
        source_hash=source_fingerprint("Old restaurant", "Original narration"),
        retry_at=now() + timedelta(minutes=5))
    await approve(pid, owner, {"name": "Recovered name"})
    refreshed = await audio_task_store.get(task_id)
    assert refreshed["done"] == 0 and refreshed["status"] == state
    await manager.recover()
    if state == "paused":
        assert task_id not in manager.workers
        await manager.control(task_id, "resume")
    task = await wait_task(task_id)
    assert task["poi_name"] == "Recovered name" and task["done"] == 1


async def test_source_changed_without_invalidation_still_refreshes_safely(setup_audio):
    manager, fake, pid, owner = setup_audio
    fake.block_language = "en"
    task_id = await manager.enqueue_poi_audio_task(str(pid), ["en"])
    await asyncio.wait_for(fake.entered.wait(), 2)
    await database.get_db()["poi"].update_one({"_id": pid}, {"$set": {"original_description": "Direct new source"}})
    fake.release.set()
    await wait_task(task_id)
    loc = await database.get_db()["poi_localizations"].find_one({"poi_id": pid, "lang": "en"})
    assert loc["description"] == "[en] Direct new source"


async def test_admin_metadata_update_also_does_not_interrupt_audio(setup_audio):
    manager, fake, pid, owner = setup_audio
    fake.block_language = "en"
    task_id = await manager.enqueue_poi_audio_task(str(pid), ["en"])
    await asyncio.wait_for(fake.entered.wait(), 2)
    await update_poi(str(pid), POIUpdateRequest(address="Admin new address"), {"id": "test-admin"}, None)
    fake.release.set()
    await wait_task(task_id)


async def test_description_edit_keeps_translated_titles_but_rebuilds_speech(setup_audio):
    manager, fake, pid, owner = setup_audio
    languages = ["vi", "en", "zh", "ja", "ko"]
    await wait_task(await manager.enqueue_poi_audio_task(str(pid), languages))
    db = database.get_db()
    before = {loc["lang"]: loc async for loc in db["poi_localizations"].find({"poi_id": pid})}
    titles = {lang: loc["title"] for lang, loc in before.items()}
    count = len(fake.translations)
    result = await approve(pid, owner, {"original_description": "New narration"})
    assert result["poi_id"] == str(pid)
    assert await db["poi"].count_documents({}) == 1
    queued_rows = await audio_task_store.list()
    assert len(queued_rows) == 1 and queued_rows[0]["id"] == result["audio_task_id"]
    await manager.recover()
    task = await wait_task(result["audio_task_id"])
    assert task["languages"] == languages and task["done"] == 5
    assert fake.translations[count:] == [("New narration", lang) for lang in languages if lang != "vi"]
    assert await db["poi_localizations"].count_documents({}) == 5
    assert (await db["poi"].find_one({"_id": pid}))["original_description"] == "New narration"
    rows = await audio_task_store.list()
    assert len(rows) == 1 and rows[0]["poi_id"] == str(pid) and rows[0]["status"] == "completed"
    for loc in [loc async for loc in db["poi_localizations"].find({"poi_id": pid})]:
        assert loc["title"] == titles[loc["lang"]]
        assert loc["_id"] == before[loc["lang"]]["_id"] and loc["poi_id"] == pid
        assert loc["audio_url"] != before[loc["lang"]]["audio_url"]
        assert loc["description"] == ("New narration" if loc["lang"] == "vi" else f"[{loc['lang']}] New narration")


@pytest.mark.parametrize("history", [None, "completed", "failed", "cancelled"])
async def test_description_approval_creates_automatic_task_without_active_job(setup_audio, history):
    manager, fake, pid, owner = setup_audio
    db = database.get_db()
    if history:
        old_id = await audio_task_store.create(str(pid), "Old restaurant", ["en"])
        await audio_task_store.update(old_id, status=history)
    result = await approve(pid, owner, {"original_description": "Approved new narration"})
    task_id = result["audio_task_id"]
    task = await audio_task_store.get(task_id)
    assert task["status"] == "queued" and task["total"] == 5
    if history:
        assert task_id != old_id and (await audio_task_store.get(old_id))["status"] == history
    await manager.recover()
    await wait_task(task_id)
    for lang in ("vi", "en", "zh", "ja", "ko"):
        loc = await db["poi_localizations"].find_one({"poi_id": pid, "lang": lang})
        expected = "Approved new narration" if lang == "vi" else f"[{lang}] Approved new narration"
        assert loc["description"] == expected and loc["audio_status"] == "completed"


async def test_rejected_description_does_not_schedule_audio(setup_audio):
    _, _, pid, owner = setup_audio
    db = database.get_db()
    sid = (await db["poi_submissions"].insert_one({
        "poi_id": pid, "owner_id": owner, "proposed_changes": {"original_description": "Rejected"},
        "status": "pending", "created_at": now(),
    })).inserted_id
    result = await review_submission(str(sid), "reject", "Không phù hợp", 1, {"id": "test-admin"}, None)
    assert result["audio_task_id"] is None
    assert await db["audio_tasks"].count_documents({}) == 0
    assert (await db["poi"].find_one({"_id": pid}))["original_description"] == "Original narration"


async def test_radius_proposal_applies_only_after_approval_without_creating_audio(setup_audio):
    _, _, pid, owner = setup_audio
    user = {"id": str(owner), "is_poi_owner_verified": True}
    before = (await list_my_pois(user))["items"][0]
    assert before["trigger_radius"] == 30
    pending = await create_poi_submission(
        POISubmissionRequest(poi_id=str(pid), proposed_changes={"trigger_radius": 75}), user, None,
    )
    assert (await list_my_pois(user))["items"][0]["trigger_radius"] == 30
    result = await review_submission(pending["id"], "approve", "", 1, {"id": "test-admin"}, None)
    assert result["audio_task_id"] is None
    after = (await list_my_pois(user))["items"][0]
    assert after["trigger_radius"] == 75 and after["original_description"] == before["original_description"]
    assert await database.get_db()["audio_tasks"].count_documents({}) == 0


async def test_title_only_manual_english_edit_keeps_the_mp3(setup_audio):
    manager, fake, pid, owner = setup_audio
    await wait_task(await manager.enqueue_poi_audio_task(str(pid), ["en"]))
    before = await database.get_db()["poi_localizations"].find_one({"poi_id": pid, "lang": "en"})
    await save_english(str(pid), EnglishLocalizationRequest(
        title="Manually improved title", description=before["description"], expected_revision=1,
    ), {"id": "test-admin"}, None)
    after = await database.get_db()["poi_localizations"].find_one({"poi_id": pid, "lang": "en"})
    assert after["audio_url"] == before["audio_url"] and after["audio_status"] == "completed"


async def test_last_moment_source_change_cannot_complete_stale_task(setup_audio, monkeypatch):
    manager, fake, pid, owner = setup_audio
    finish = manager._finish_transaction
    edited = False

    async def change_before_finish(task_id, token):
        nonlocal edited
        if not edited:
            edited = True
            await database.get_db()["poi"].update_one({"_id": pid}, {"$set": {"original_description": "Final source"}})
        return await finish(task_id, token)

    monkeypatch.setattr(manager, "_finish_transaction", change_before_finish)
    task_id = await manager.enqueue_poi_audio_task(str(pid), ["en"])
    await wait_task(task_id)
    en = await database.get_db()["poi_localizations"].find_one({"poi_id": pid, "lang": "en"})
    assert en["description"] == "[en] Final source"


@pytest.mark.parametrize("pause", [False, True])
async def test_transient_transaction_conflict_retries_without_ignoring_pause(setup_audio, monkeypatch, pause):
    manager, fake, pid, owner = setup_audio
    process = manager._process_language
    attempts = 0

    async def conflict_once(poi, base, lang, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if pause:
                await manager.control(kwargs["task_id"], "pause")
            raise OperationFailure("Write conflict", code=112,
                                   details={"errorLabels": ["TransientTransactionError"]})
        return await process(poi, base, lang, **kwargs)

    monkeypatch.setattr(manager, "_process_language", conflict_once)
    task_id = await manager.enqueue_poi_audio_task(str(pid), ["en"])
    if pause:
        await wait_task(task_id, "paused")
        await manager.control(task_id, "resume")
    await wait_task(task_id)
    assert attempts == 2
