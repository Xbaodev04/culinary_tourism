import assert from 'node:assert/strict';
import test from 'node:test';
import { currentPoiTasks, poiTaskKey } from '../src/features/admin-dashboard/utils/currentPoiTasks.js';

const old = { id: '1', poi_id: 'poi-1', poi_name: 'Cơm gà Tư Điền', status: 'completed', created_at: '2026-11-10T00:00:00Z' };
const latest = { ...old, id: '2', status: 'queued', created_at: '2026-11-11T00:00:00Z' };

test('refresh replaces completed row before pagination and status filtering', () => {
  const rows = currentPoiTasks([old, latest]);
  assert.deepEqual(rows, [latest]);
  assert.deepEqual(rows.filter((row) => row.status === 'completed'), []);
  assert.equal(rows.slice(0, 15).length, 1);
});

test('completed refresh remains the single row even if snapshots are unsorted', () => {
  const completed = { ...latest, status: 'completed' };
  assert.deepEqual(currentPoiTasks([completed, old]), [completed]);
  assert.deepEqual(currentPoiTasks([old, completed]), [completed]);
});

test('restaurants with identical names retain separate rows for separate POI IDs', () => {
  const other = { ...old, id: '3', poi_id: 'poi-2' };
  assert.equal(currentPoiTasks([old, latest, other]).length, 2);
});

test('paused or retried active work takes priority over newer finished history', () => {
  for (const status of ['paused', 'running', 'queued']) {
    const active = { ...old, status };
    assert.deepEqual(currentPoiTasks([active, { ...latest, status: 'completed' }]), [active]);
  }
});

test('failed refresh is visible instead of the previous successful audio job', () => {
  const failed = { ...latest, status: 'failed' };
  assert.deepEqual(currentPoiTasks([old, failed]), [failed]);
});

test('dialog identity follows the same POI when its current task changes', () => {
  const rows = currentPoiTasks([old, latest]);
  assert.equal(rows.find((row) => poiTaskKey(row) === poiTaskKey(old)), latest);
  assert.equal(poiTaskKey({ ...old, poi_id: 'ABC123' }), poiTaskKey({ ...latest, poi_id: 'abc123' }));
});
