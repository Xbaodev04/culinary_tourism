"""Optional REAL MongoDB replica-set transaction integration smoke test.

RUN_MONGO_TX_INTEGRATION=1 pytest -q tests/test_mongo_transaction_integration.py
The normal test suite does not require a database server.
"""
import os
import uuid
from contextlib import suppress
import pytest

pytestmark = pytest.mark.skipif(os.getenv('RUN_MONGO_TX_INTEGRATION') != '1',
    reason='Requires a real MongoDB replica set; set RUN_MONGO_TX_INTEGRATION=1')


@pytest.mark.asyncio
async def test_registration_description_commits_with_created_poi(monkeypatch):
    from motor.motor_asyncio import AsyncIOMotorClient
    from bson import ObjectId
    from app.core import database
    from app.core.config import settings
    from app.modules.admin import registrations

    client = AsyncIOMotorClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
    db_name = f'quan4_registration_narration_{uuid.uuid4().hex[:10]}'
    db = client[db_name]
    monkeypatch.setattr(database, 'mongo_client', client)
    monkeypatch.setattr(database, 'db', db)
    owner, rid = ObjectId(), ObjectId()
    narration = 'The description submitted by the restaurant owner.'
    try:
        await db.users.insert_one({'_id': owner, 'role': 'poi_owner'})
        await db.poi_owner_registrations.insert_one({'_id': rid, 'user_id': owner, 'status': 'pending',
            'poi_name': 'Restaurant', 'poi_address': '12 Vinh Khanh', 'poi_description': narration,
            'poi_location': {'type': 'Point', 'coordinates': [106.703, 10.76]}})
        for collection in ('poi', 'owner_notifications', 'audit_logs', 'system_meta'):
            await db.create_collection(collection)
        admin = {'id': str(ObjectId()), 'permissions': ['poi:create', 'poi:update', 'poi:toggle']}
        audit = registrations.write_audit_event

        async def fail_audit(*args, **kwargs):
            raise RuntimeError('registration audit unavailable')

        monkeypatch.setattr(registrations, 'write_audit_event', fail_audit)
        with pytest.raises(RuntimeError, match='registration audit unavailable'):
            await registrations.review_registration(str(rid), 'approve', '', admin, None)
        assert await db.poi.count_documents({}) == 0
        assert (await db.poi_owner_registrations.find_one({'_id': rid}))['status'] == 'pending'
        monkeypatch.setattr(registrations, 'write_audit_event', audit)
        approved = await registrations.review_registration(str(rid), 'approve', '', admin, None)
        poi = await db.poi.find_one({'_id': ObjectId(approved['poi_id'])})
        assert poi['original_description'] == narration and poi['is_active'] is True
    finally:
        with suppress(Exception):
            await client.drop_database(db_name)
        client.close()


@pytest.mark.asyncio
async def test_owner_source_update_and_queue_refresh_are_atomic(monkeypatch):
    from datetime import timedelta
    from motor.motor_asyncio import AsyncIOMotorClient
    from bson import ObjectId
    from app.core import database
    from app.core.config import settings
    from app.modules.admin.poi_management import invalidate_narrations
    from app.modules.audio.source_version import source_fingerprint
    from app.modules.audio.task_store import now

    client = AsyncIOMotorClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
    db_name = f'quan4_owner_audio_update_{uuid.uuid4().hex[:10]}'
    db = client[db_name]
    monkeypatch.setattr(database, 'mongo_client', client)
    monkeypatch.setattr(database, 'db', db)
    pid, tid = ObjectId(), ObjectId()
    try:
        await db.poi.insert_one({'_id': pid, 'name': 'Old', 'original_description': 'Narration'})
        await db.poi_localizations.insert_one({'poi_id': pid, 'lang': 'en', 'title': 'Old English',
            'description': 'Narration English', 'audio_url': '/static/audio/existing.mp3', 'audio_status': 'completed',
            'source_hash': source_fingerprint('Old', 'Narration')})
        await db.audio_tasks.insert_one({'_id': tid, 'poi_id': str(pid), 'status': 'running',
            'done': 1, 'lease_token': 'old-worker', 'lease_until': now() + timedelta(minutes=5)})

        async def edit(session):
            await db.poi.update_one({'_id': pid}, {'$set': {'name': 'New'}}, session=session)
            await invalidate_narrations(db, pid, 'New', 'Narration', name_changed=True, desc_changed=False,
                previous_name='Old', previous_description='Narration', session=session)

        with pytest.raises(RuntimeError, match='rollback'):
            async with database.mongo_transaction() as session:
                await edit(session)
                raise RuntimeError('rollback')
        assert (await db.poi.find_one({'_id': pid}))['name'] == 'Old'
        assert (await db.audio_tasks.find_one({'_id': tid}))['lease_token'] == 'old-worker'
        assert (await db.poi_localizations.find_one({'poi_id': pid, 'lang': 'en'}))['title'] == 'Old English'

        async with database.mongo_transaction() as session:
            await edit(session)
        task = await db.audio_tasks.find_one({'_id': tid})
        assert task['status'] == 'queued' and task['done'] == 0 and 'lease_token' not in task
        localization = await db.poi_localizations.find_one({'poi_id': pid, 'lang': 'en'})
        assert localization['title'] is None and localization['audio_url'] == '/static/audio/existing.mp3'
    finally:
        with suppress(Exception):
            await client.drop_database(db_name)
        client.close()


@pytest.mark.asyncio
async def test_approved_description_and_new_audio_job_commit_atomically(monkeypatch):
    from motor.motor_asyncio import AsyncIOMotorClient
    from bson import ObjectId
    from app.core import database
    from app.core.config import settings
    from app.modules.admin import audit
    from app.modules.admin.submissions import review_submission

    client = AsyncIOMotorClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
    db_name = f'quan4_approved_audio_{uuid.uuid4().hex[:10]}'
    db = client[db_name]
    monkeypatch.setattr(database, 'mongo_client', client)
    monkeypatch.setattr(database, 'db', db)
    pid, sid = ObjectId(), ObjectId()
    try:
        await db.poi.insert_one({'_id': pid, 'name': 'Restaurant', 'original_description': 'Old', 'revision': 1})
        await db.poi_submissions.insert_one({'_id': sid, 'poi_id': pid, 'status': 'pending',
            'proposed_changes': {'original_description': 'Approved narration'}})
        for collection in ('poi_localizations', 'audio_tasks', 'system_meta', 'audit_logs'):
            await db.create_collection(collection)
        await db.audio_tasks.create_index('poi_id', unique=True,
            partialFilterExpression={'status': {'$in': ['queued', 'running', 'paused']}})
        real_audit = audit.write_audit_event

        async def fail_audit(*args, **kwargs):
            raise RuntimeError('approval audit unavailable')

        monkeypatch.setattr(audit, 'write_audit_event', fail_audit)
        with pytest.raises(RuntimeError, match='approval audit unavailable'):
            await review_submission(str(sid), 'approve', '', 1, {'id': str(ObjectId())}, None)
        assert (await db.poi.find_one({'_id': pid}))['original_description'] == 'Old'
        assert (await db.poi_submissions.find_one({'_id': sid}))['status'] == 'pending'
        assert await db.audio_tasks.count_documents({}) == 0
        assert await db.poi_localizations.count_documents({}) == 0

        monkeypatch.setattr(audit, 'write_audit_event', real_audit)
        result = await review_submission(str(sid), 'approve', '', 1, {'id': str(ObjectId())}, None)
        task = await db.audio_tasks.find_one({'_id': ObjectId(result['audio_task_id'])})
        assert task['status'] == 'queued' and task['languages'] == ['vi', 'en', 'zh', 'ja', 'ko']
        assert (await db.poi.find_one({'_id': pid}))['original_description'] == 'Approved narration'
        assert (await db.poi_submissions.find_one({'_id': sid}))['status'] == 'approved'
    finally:
        with suppress(Exception):
            await client.drop_database(db_name)
        client.close()


@pytest.mark.asyncio
async def test_rollback_across_collections(monkeypatch):
    from motor.motor_asyncio import AsyncIOMotorClient
    from app.core import database
    from app.core.config import settings

    client = AsyncIOMotorClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
    name = f'quan4_transaction_smoke_{uuid.uuid4().hex[:10]}'
    db = client[name]
    monkeypatch.setattr(database, 'mongo_client', client)
    monkeypatch.setattr(database, 'db', db)
    try:
        await db['one'].insert_one({'_id': 'seed'})  # create collections before transaction
        await db['two'].insert_one({'_id': 'seed'})
        with pytest.raises(RuntimeError, match='rollback'):
            async with database.mongo_transaction() as tx:
                await db['one'].insert_one({'_id': 'rollback-a'}, session=tx)
                await db['two'].insert_one({'_id': 'rollback-b'}, session=tx)
                raise RuntimeError('rollback')
        assert await db['one'].count_documents({'_id': 'rollback-a'}) == 0
        assert await db['two'].count_documents({'_id': 'rollback-b'}) == 0
        async with database.mongo_transaction() as tx:
            await db['one'].insert_one({'_id': 'commit-a'}, session=tx)
            await db['two'].insert_one({'_id': 'commit-b'}, session=tx)
        assert await db['one'].count_documents({'_id': 'commit-a'}) == 1
        assert await db['two'].count_documents({'_id': 'commit-b'}) == 1
    finally:
        with suppress(Exception):
            await client.drop_database(name)
        client.close()

@pytest.mark.asyncio
async def test_audio_completion_and_dataset_version_commit_or_rollback_together(monkeypatch):
    """Real replica-set test: version update failure MUST roll back task completion."""
    from datetime import timedelta
    from motor.motor_asyncio import AsyncIOMotorClient
    from bson import ObjectId
    from app.core import database
    from app.core.config import settings
    from app.modules.audio.task_manager import AudioTaskManager
    from app.modules.audio.task_store import now

    client = AsyncIOMotorClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
    db_name = f'quan4_audio_atomic_{uuid.uuid4().hex[:10]}'
    db = client[db_name]
    monkeypatch.setattr(database, 'mongo_client', client)
    monkeypatch.setattr(database, 'db', db)
    manager = AudioTaskManager()
    task_id = str(ObjectId())
    try:
        await db['audio_tasks'].insert_one({
            '_id': ObjectId(task_id), 'status': 'running', 'lease_token': 'test-token',
            'lease_until': now() + timedelta(minutes=5), 'done': 1, 'total': 1,
        })
        await db['system_meta'].insert_many([
            {'_id': 'dataset', 'version': 1}, {'_id': 'collision', 'version': 2},
        ])
        await db['system_meta'].create_index('version', unique=True)
        with pytest.raises(Exception):
            # Duplicate-key on version 2 forces transaction abort.
            await manager._finish_transaction(task_id, 'test-token')
        assert (await db['audio_tasks'].find_one({'_id': ObjectId(task_id)}))['status'] == 'running'
        assert (await db['system_meta'].find_one({'_id': 'dataset'}))['version'] == 1
        await db['system_meta'].delete_one({'_id': 'collision'})
        assert await manager._finish_transaction(task_id, 'test-token') is True
        assert (await db['audio_tasks'].find_one({'_id': ObjectId(task_id)}))['status'] == 'completed'
        assert (await db['system_meta'].find_one({'_id': 'dataset'}))['version'] == 2
    finally:
        with suppress(Exception):
            await client.drop_database(db_name)
        client.close()


@pytest.mark.asyncio
async def test_audio_enqueue_rolls_back_if_audit_insert_fails(monkeypatch):
    """A real Mongo replica set must never commit an unaudited audio enqueue."""
    from motor.motor_asyncio import AsyncIOMotorClient
    from bson import ObjectId
    from app.core import database
    from app.core.config import settings
    import app.modules.audio.task_manager as manager_module

    client = AsyncIOMotorClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
    db_name = f'quan4_audit_atomic_{uuid.uuid4().hex[:10]}'
    db = client[db_name]
    monkeypatch.setattr(database, 'mongo_client', client)
    monkeypatch.setattr(database, 'db', db)
    manager = manager_module.AudioTaskManager()
    monkeypatch.setattr(manager, '_start', lambda _id: None)
    poi_id, admin_id = ObjectId(), ObjectId()
    try:
        await db['poi'].insert_one({'_id': poi_id, 'name': 'Quán thử'})
        await db['users'].insert_one({'_id': admin_id, 'email': 'audit@test.local', 'role': 'super_admin'})

        async def unavailable(*args, **kwargs):
            raise RuntimeError('audit write unavailable')

        actual = manager_module.write_audit_event
        monkeypatch.setattr(manager_module, 'write_audit_event', unavailable)
        with pytest.raises(RuntimeError, match='audit write unavailable'):
            await manager.enqueue_poi_audio_task(str(poi_id), ['vi'], actor_id=str(admin_id), actor_ip='127.0.0.1')
        assert await db['audio_tasks'].count_documents({}) == 0
        assert await db['audit_logs'].count_documents({}) == 0

        monkeypatch.setattr(manager_module, 'write_audit_event', actual)
        task_id = await manager.enqueue_poi_audio_task(str(poi_id), ['vi'], actor_id=str(admin_id), actor_ip='127.0.0.1')
        assert await db['audio_tasks'].count_documents({}) == 1
        assert await db['audit_logs'].count_documents({'resource': f'audio_task:{task_id}'}) == 1
    finally:
        with suppress(Exception):
            await client.drop_database(db_name)
        client.close()
