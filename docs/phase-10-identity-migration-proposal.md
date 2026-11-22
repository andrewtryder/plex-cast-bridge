# Phase 10 — Identity migration (proposal)

## Findings (characterized in #73)

1. **UUID-present records** — `new_google_home_device_id(cast_uuid)` is deterministic and stable across migrations.
2. **Legacy room IDs** — `plex-jukebox:<room>` preserved when `id` or `google_home_device_id` already uses that prefix.
3. **Opaque ID churn risk** — `migrate_speaker_record` without `cast_uuid` calls `new_google_home_device_id()` with `secrets.token_hex`, producing a **new** `plex-jukebox:id-*` on every migration pass (see `tests/test_speaker_migration_characterization.py`).
4. **Catalog duplicate IDs** — `migrate_catalog_speakers` hides duplicates, assigns new IDs, and sets `needs_review` (idempotent thereafter).
5. **Visibility contract** — `enabled` vs `google_home_visible` remains independent; disabled speakers may stay SYNC-visible when configured.

## Recommended implementation (future)

- Persist first-assigned opaque IDs in-catalog (or derive from stable legacy fields only).
- Never regenerate IDs for speakers that already have `google_home_device_id` unless operator explicitly re-keys.
- Add differential tests against frozen catalogs before enabling automatic migrate-on-load in production.

**This branch is proposal-only. No production migration or catalog writes.**
