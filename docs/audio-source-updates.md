# POI edits and audio queue behavior

An owner proposal is a pending record. It does not update the master POI or its
audio tasks until an Admin approves it. Rejection leaves both unchanged.

Approved changes behave as follows:

| Change | Localization/audio | Active task |
|---|---|---|
| Address, images, position, category, trigger radius, availability or priority | Unchanged | Continues |
| Restaurant name | Refresh translated titles; retain narration and existing MP3 | Refreshes the same task; reuses valid speech files |
| Approved Vietnamese narration proposal | Invalidate translated narration and MP3; retain unchanged translated titles | Refresh existing task for all five languages, or enqueue a new task if no active task exists |

The title is not concatenated into the spoken text by the audio generator. A
name-only change therefore does not rewrite occurrences of the old name inside
the narration. Change that text explicitly when a new spoken name is required.

`revision` still protects POI editing from concurrent writes. Audio provenance
uses `title_source_hash` and `description_source_hash` separately. The existing
combined `source_hash` is retained for task checkpoints and compatibility. Old
localizations with matching combined provenance are upgraded lazily, preserving
manual narration on title-only edits. Missing/invalid provenance is not reused.

Approval updates the POI, invalidates the relevant localization fields and
refreshes queued/running/paused tasks in one MongoDB transaction. Refreshing a
running task invalidates its worker lease so the old worker cannot publish stale
content. Active tasks keep their ID. An approved description proposal expands
the task to Vietnamese, English, Chinese, Japanese and Korean. A paused task
remains paused. Finished/cancelled task history is preserved; a new approved
description creates a new task when no active task exists. The queue supervisor
claims new work on its next cycle (normally within 15 seconds).

Audio regeneration updates the existing POI ID and the same localization record
for each language. It never inserts another POI. The Audio Studio REST/SSE list
shows one current task per `poi_id`, preferring active work and then the newest
job. Grouping happens before the 50-POI limit; old task history remains stored.
The frontend applies the same rule before filtering/pagination and follows the
current task for the same POI when an open details/preview dialog receives updates.

After a refresh, language progress is recalculated from the start. Valid MP3s
already on disk are reused; updating a translated title does not resynthesize an
unchanged narration. If no active task exists, the Admin can enqueue one normally
to refresh the translated titles. Admin controls publication independently of
translation/audio completeness; source edits do not require hiding a public POI.

Workers check source freshness before persisting a language and again before
marking the whole task complete. Unrelated metadata revisions no longer cause
audio failure. Transient MongoDB transaction conflicts requeue work while
preserving pause/cancel controls. The monitor displays a source-update notice.
Cross-process recovery picks up queued work through the existing supervisor.

The Audio Studio creation dialog submits only `poi_id`, using all five languages
by default. `GET /admin/audio-tasks/eligible-pois` searches and paginates POIs
after excluding active tasks and five current, nonempty MP3 files. Incomplete,
stale or missing files remain repairable. Explicit `languages` is retained in
the task API for existing clients and the separate English localization action.

Owner registration accepts `trigger_radius` in meters (integer 5–500, default
30), including image uploads. Review preserves it when creating or linking a
POI. Radius-only proposals require approval and do not invalidate narration.

Tasks that failed under the old "POI changed" behavior can be retried with the
existing Retry button; the worker refreshes a stale source checkpoint rather
than requiring a new task.

Validation:

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q tests/test_audio_poi_source_updates.py tests/test_audio_tasks.py tests/test_admin_poi_review.py tests/test_audio_retry_visibility_manual_fallback.py
$env:RUN_MONGO_TX_INTEGRATION = '1'
.\.venv\Scripts\python.exe -m pytest -q tests/test_mongo_transaction_integrtion.py
```

The first group uses fake translation/TTS and a mock database. The optional
second group exercises commit/rollback against an isolated real replica-set
database and removes only its temporary test databases afterward.
