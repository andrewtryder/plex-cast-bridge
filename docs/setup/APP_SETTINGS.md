# App settings (`data/app_settings.json`)

Non-secret runtime knobs editable from **Settings → Playback**. Secrets stay in `.env` / the Google Home wizard.

## First boot

The file is **optional**. Until you click **Save volumes**, the bridge uses `DEFAULT_CAST_VOLUME` / `MAX_CAST_VOLUME` from `.env` (or code defaults).

After the first save, `DATA_DIR/app_settings.json` is created (mode `0600` when the filesystem allows) and **overrides** those env values on every startup.

## Schema (v1)

```json
{
  "schema_version": 1,
  "default_cast_volume": 0.2,
  "max_cast_volume": 0.85,
  "night_cast_volume": 0.15
}
```

Volumes are floats from `0` to `1`. `default_cast_volume` is clamped so it never exceeds `max_cast_volume`. `night_cast_volume` is optional.

Checked-in example: [`app_settings.example.json`](app_settings.example.json) (copy into `DATA_DIR` only if you want a starting file before using the UI). Live `data/app_settings.json` is under the gitignored `data/` directory.

## API

- `GET /api/settings` — effective volumes + library artist/album/track summary
- `PATCH /api/settings` — body may include `default_cast_volume`, `max_cast_volume`, `night_cast_volume`; applies to the live Cast manager without restart

Requires the same local API auth as other control endpoints (`X-API-Key` / `LOCAL_API_KEY`).

## Backup

`GET /api/backup` includes `app_settings.json` when present, plus `settings_public.json` (effective volumes and non-secret cloud flags). Tokens, PEMs, and service-account keys are excluded.

## What stays out of this file

| Stays in | Examples |
|---|---|
| `.env` / Compose | `LOCAL_API_KEY`, Plex JWT store, `CATALOG_WRITE_KEY`, Pub/Sub |
| Google Home wizard + companion | OAuth client, Cloud Run URL, GCP project |
| Player UI | Live per-session Cast volume slider |
