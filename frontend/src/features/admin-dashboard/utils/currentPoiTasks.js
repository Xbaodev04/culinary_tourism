const ACTIVE = new Set(['queued', 'running', 'paused']);

export function poiTaskKey(task) {
  return String(task.poi_id || task.id).trim().toLowerCase();
}

function compareCurrent(first, second) {
  const active = Number(ACTIVE.has(second.status)) - Number(ACTIVE.has(first.status));
  const created = (Date.parse(second.created_at) || 0) - (Date.parse(first.created_at) || 0);
  return active || created || String(second.id).localeCompare(String(first.id));
}

// Also handle snapshots from older servers during deployment/reconnection.
// Group before filtering or pagination so old completed work cannot reappear.
export function currentPoiTasks(snapshot) {
  const rows = new Map();
  for (const task of [...snapshot].sort(compareCurrent)) {
    const key = poiTaskKey(task);
    if (!rows.has(key)) rows.set(key, task);
  }
  return [...rows.values()];
}
