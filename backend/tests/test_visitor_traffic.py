from datetime import datetime, timedelta, timezone
from uuid import uuid4

from bson import ObjectId

from app.core.database import get_db
from app.modules.analytics.service import analytics_service, LOCAL_TZ


async def test_visit_retries_are_idempotent_and_legacy_events_remain_supported(client):
    db = get_db()
    poi = (await db['poi'].insert_one({'is_active': True})).inserted_id
    device, session = str(uuid4()), str(uuid4())
    visit = {
        'event_id': str(uuid4()), 'kind': 'app_visited',
        'device_id': device, 'session_id': session,
        'occurred_at': datetime.now(timezone.utc).isoformat(),
    }
    path = '/api/v1/analytics/collect'
    async def send(events, consent='granted'):
        return await client.post(path, json={'consent_status': consent, 'events': events})

    assert (await send([visit], 'denied')).status_code == 422
    assert (await send([visit])).json()['recorded'] == 1
    # Reload can create another event ID but must not create another visit.
    assert (await send([visit | {'event_id': str(uuid4())}])).json()['recorded'] == 0
    assert (await send([visit | {'session_id': None}])).status_code == 422
    assert (await send([visit | {'poi_id': str(poi)}])).status_code == 422
    view = visit | {'event_id': str(uuid4()), 'kind': 'poi_viewed', 'poi_id': str(poi), 'source': 'qr'}
    assert (await send([view])).json()['recorded'] == 1
    assert (await send([view])).json()['recorded'] == 0
    legacy = {k: v for k, v in view.items() if k not in ('device_id', 'session_id', 'source')}
    legacy['event_id'] = str(uuid4())
    assert (await send([legacy])).json()['recorded'] == 1
    stats = await analytics_service.get_traffic_stats(7)
    assert stats['totals'] == {'devices': 1, 'sessions': 1, 'qr_views': 1}
    assert len(stats['daily']) == 7
    stored = await db['analytics_poi_events'].find_one({'kind': 'app_visited'})
    assert 'expires_at' in stored
    assert not {'ip', 'email', 'name', 'user_agent'} & stored.keys()


async def test_distinct_traffic_over_days_period_filter_and_poi_scope(client, admin_creds):
    db = get_db()
    today = datetime.now(LOCAL_TZ).date()
    device, other_device, session, next_session = map(str, [uuid4(), uuid4(), uuid4(), uuid4()])
    poi, other_poi = ObjectId(), ObjectId()

    def row(day, dev, sess, poi_id=poi, source='direct'):
        return {'day': day.isoformat(), 'device_id': dev, 'session_id': sess,
                'poi_id': poi_id, 'kind': 'poi_viewed', 'source': source}

    await db['analytics_poi_events'].insert_many([
        row(today - timedelta(days=1), device, session, source='qr'),
        row(today, device, session),  # midnight must not inflate distinct period totals
        row(today, device, next_session),
        row(today, other_device, session, other_poi, 'qr'),
        row(today - timedelta(days=10), str(uuid4()), str(uuid4())),
        row(today + timedelta(days=1), str(uuid4()), str(uuid4())),
        {'day': today.isoformat(), 'poi_id': poi, 'kind': 'poi_viewed'},
    ])
    stats = await analytics_service.get_traffic_stats(7)
    assert stats['totals'] == {'devices': 2, 'sessions': 3, 'qr_views': 2}
    assert stats['daily'][-1] == {'date': today.isoformat(), 'devices': 2, 'sessions': 3, 'qr_views': 1}
    own = await analytics_service.get_poi_stats(poi, 7)
    assert own['traffic']['totals'] == {'devices': 1, 'sessions': 2, 'qr_views': 1}
    assert (await analytics_service.get_traffic_stats(30))['totals']['devices'] == 3
    assert (await client.get('/api/v1/admin/dashboard/stats')).status_code == 401
    assert (await client.post('/api/v1/admin/auth/login', json=admin_creds)).status_code == 200
    response = await client.get('/api/v1/admin/dashboard/stats', params={'days': 7})
    assert response.status_code == 200
    assert response.json()['traffic'] == stats
    assert 'device_id' not in str(response.json()['traffic'])
