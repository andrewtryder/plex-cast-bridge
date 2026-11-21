# Phase 2: local dashboard route modularization

Baseline: `main` at `437201f` (merge of Phase 1 PR #65). Production `v0.5.0` tag
and published images are unchanged by this phase.

## Module map (post-refactor)

| Module | Responsibility |
| --- | --- |
| `routes.py` | Compatibility composition; re-exports `router`, `play`, `_require_plex`, `DATA_DIR`, `persist_discovered_speakers_to_catalog` |
| `local_routes.py` | Dashboard HTML, static assets, `/api/activity` |
| `playback_routes.py` | Command, transport, library browse/play/search, queue, status, artwork, sync catalog |
| `speaker_routes.py` | Speaker CRUD, discovery refresh, toggle, test tone |
| `settings_routes.py` | `/api/settings` GET/PATCH |
| `diagnostics_routes.py` | `/api/diagnostics` |
| `backup_routes.py` | Backup export/restore/preview |

`main.py` continues to import `from .routes import router` only.

## Compatibility seams preserved

- `plex_cast_bridge.routes` attribute patches in tests (`DATA_DIR`, `_require_plex`, `play`, `persist_discovered_speakers_to_catalog`).
- Lazy `from . import routes` in playback, speaker, and backup handlers for runtime indirection.
- Sub-routers are included in the same precedence order as the monolithic module (settings → diagnostics → backup → playback → speaker → dashboard).

## OpenAPI equivalence

The normalized local-dashboard OpenAPI subset (`tests/fixtures/local_dashboard_openapi.json`)
matches `main` at `437201f` byte-for-byte after modularization.

Contract tests: `tests/test_local_route_openapi_contract.py`.

## Test inventory

Phase 1 inventory remains **463** nodes (**433** non-Docker, **30** `docker_integration`).

## Complexity (Radon)

Run on the branch:

```bash
uv run radon cc src/plex_cast_bridge/*routes*.py -s
uv run radon raw src/plex_cast_bridge/*routes*.py -s
```

| Module | Lines (approx.) | Notes |
| --- | ---: | --- |
| `routes.py` (main) | 825 | Monolithic |
| `routes.py` (compat) | 35 | Composition only |
| `playback_routes.py` | 320 | Largest extracted surface |
| `speaker_routes.py` | 195 | Speaker catalog |
| `local_routes.py` | 58 | Static/dashboard |
| `settings_routes.py` | 111 | Existing extraction |
| `diagnostics_routes.py` | 80 | Existing extraction |
| `backup_routes.py` | 152 | Existing extraction |

McCabe scores for individual handlers are unchanged; only file placement moved.

## CI performance follow-up: Compose recreation/switch Docker test

`tests/test_upgrade_compose_selection.py::test_selection_survives_restart_recreation_and_successive_switches`
is the long-running Compose selection case (reported ~88.76 s in a full Docker job).

Observed cost drivers (no code changes in this PR):

1. **`docker pull` of `PCB_TEST_UPGRADE_TARGET`** — one network-bound image fetch per test invocation.
2. **Multiple `docker compose up -d` cycles** — seven deploy passes exercising override-managed image switches, rollback, and `--force-recreate`.
3. **`docker restart`** — full container restart between switch assertions.
4. **`docker compose down`** — teardown in `finally` (project-scoped).

The lighter `test_actual_compose_lifecycle_recreation` in `test_docker_portainer_upgrade.py` (~22 s locally per Phase 1 doc) covers a single up → force-recreate path.

Recommended follow-ups (separate PR):

- Reuse a pre-pulled digest pin in CI instead of `docker pull` inside the test body when `PCB_TEST_UPGRADE_TARGET` is already materialized by the workflow.
- Collapse redundant `up -d` calls only where assertions still cover upgrade, rollback, persistent mount, and configuration-protection paths.
- Add phase-level timing logs around pull / up / recreate / down for job artifacts.

Do **not** enable `pytest-xdist` across shared-Docker tests without proving isolation.

## Remaining risks and next targets

- `local_routes.py` naming is slightly misleading (dashboard-only); consider `dashboard_routes.py` in a later pass with a deprecation alias.
- `playback_routes` ↔ `speaker_routes` share `_require_plex`; a tiny `local_api_guards.py` would remove the cross-import if complexity grows.
- Cast threading in `playback_service` and `speaker_queue` remain intentionally untouched.
