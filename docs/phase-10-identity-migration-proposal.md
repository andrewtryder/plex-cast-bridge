# Phase 10 — Identity migration (verified analysis)

## ID generation rules (`migrate_speaker_record`)

| Input state | Resulting `google_home_device_id` | Stable on repeat? |
|-------------|-----------------------------------|-------------------|
| Existing `plex-jukebox:*` in `id` / `google_home_device_id` | Preserved as-is | **Yes** |
| Usable `cast_uuid` (≥8 hex chars) | `plex-jukebox:cast-<uuid>` deterministic | **Yes** |
| No legacy ID, no UUID, **not yet persisted** | `plex-jukebox:id-<random hex>` | **No** (each in-memory call) |
| After `load_catalog_data` / `save_catalog_data` | Written `google_home_device_id` reread as legacy ID | **Yes** |
| Already migrated record with `google_home_device_id` set | Rule 1 | **Yes** |

### Churn scope (precise)

**Affected:** Ephemeral `migrate_speaker_record(raw)` on dicts that lack both `plex-jukebox:*` and `cast_uuid` — each invocation draws a new random ID.

**Not affected:** Normal catalog I/O — `load_catalog_data` migrates and **atomically saves** when `changed`; subsequent loads preserve the assigned ID (`tests/test_speaker_catalog_migration_persist.py` in #73).

**Not affected:** `reconcile_speakers` for UUID-bearing discovery — uses deterministic cast-based IDs for new speakers.

## Persistence call graph

| Caller | Persists? | Notes |
|--------|-----------|-------|
| `load_catalog_data` | **Yes** when migration changes catalog | Atomic JSON replace |
| `save_catalog_data` | **Always** | Re-migrates, writes, may schedule GH push |
| `persist_discovered_speakers_to_catalog` | Via `save_catalog_data` | After LAN discovery |
| `reconcile_speakers` | No (in-memory) | Caller persists via catalog_store |
| Startup / upgrade | Read-only migration via load | No production touch in tests |

## Identity invariants (verified)

- Existing virtual IDs are preserved when present in catalog.
- No UUID-based cross-merge between distinct known Cast UUIDs (`_match_existing`).
- Duplicate virtual IDs in catalog → `needs_review`, hidden, new IDs for collisions.
- New discovery hidden from Google Home by default.
- `enabled` and `google_home_visible` remain independent.
- Offline speakers retained; removed speakers stay removed on rediscovery.

## Risk matrix

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Random ID churn on **unpersisted** migrate | Medium (dev tools only) | Low in production | Catalog load/save path already stabilizes |
| Duplicate legacy catalog IDs | Low | Medium | `migrate_catalog_speakers` flags review |
| Mis-merge by name | Low | High | Ambiguous names skip UUID attach |
| Auto-expose hidden speaker | None observed | High | Tests lock hidden default |

## Implementation recommendation

**No automatic code change required** for production catalogs that follow normal load/save paths.

Future optional hardening (separate implementation PR):

- Derive opaque fallback from stable non-network key **only** when persisting (not on every in-memory normalize).
- Dry-run migration preview API before writing catalog.

**Do not merge implementation without differential harness and operator review.**
