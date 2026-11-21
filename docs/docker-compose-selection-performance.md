# Docker Compose selection test — performance investigation

Baseline data from Phase 1–2 CI and local measurement. No workflow changes were applied in the modernization PRs; this document records intrinsic costs and safe optimization candidates.

## Target test

`tests/test_upgrade_compose_selection.py::test_selection_survives_restart_recreation_and_successive_switches`

- Marked `docker_integration`; runs in the sequential Docker job.
- Reported CI duration: **~88.76 s** (full matrix context).
- Lighter related case: `test_actual_compose_lifecycle_recreation` (**~21.9 s** locally per Phase 1 doc).

## Intrinsic work (must retain)

| Assertion | Why it cannot be dropped |
| --- | --- |
| Migration override creation | Proves managed upgrade path from “running candidate on old compose line” |
| `--force-recreate` after migration | Validates persistent `/data` mount across recreation |
| Image switch old ↔ target (digest refs) | Rollback / forward compatibility |
| `docker restart` mid-sequence | Confirms override survives non-compose restarts |
| Base `compose.yaml` byte equality | Configuration protection |
| Sentinel file in bind-mounted `data/` | Mount preservation |

## Measured cost drivers

| Operation | Occurrences in test | Typical cost | Notes |
| --- | ---: | --- | --- |
| `docker pull` (`PCB_TEST_UPGRADE_TARGET`) | 1 | 15–45 s | Network-bound; CI often pre-builds candidate — pull inside test is redundant when digest already on daemon |
| `compose up -d` | 7 | ~3–8 s each | Container create + start (`network_mode: none`, sleep command) |
| `docker compose up -d --force-recreate` | 1 | ~5–10 s | Recreate path |
| `docker restart` | 1 | ~2–5 s | |
| `docker image inspect` / `compose config` | Many | &lt;1 s cumulative | JSON config for switches |
| `compose down --remove-orphans` | 1 | ~2–5 s | Teardown |

**Total:** Pull + (7× up) + recreate + restart + down explains most of the **~88 s** wall time.

## Avoidable repetition (candidates — separate PR only)

1. **Skip in-test `docker pull`** when `PCB_TEST_UPGRADE_TARGET` is already loaded (CI sets this after “Build local bridge candidate once”). Replace with `docker image inspect` guard or rely on job-level pull.
2. **Instrument phases** with `time.monotonic()` and emit structured logs in CI artifacts for regression tracking.
3. **Share immutable digests** across matrix jobs via workflow env (already partially done for candidate); ensure selection test never re-pulls the same digest.
4. Do **not** parallelize this test with other shared-project Docker tests without per-project `COMPOSE_PROJECT_NAME` isolation proof (test already uses random project names; parallelization risk is daemon-wide lock contention).

## Benchmark recipe (local)

```bash
export PCB_TEST_COMPOSE_RECREATE=1
export PCB_TEST_UPGRADE_TARGET='ghcr.io/…@sha256:…'
uv run python -m pytest tests/test_upgrade_compose_selection.py::test_selection_survives_restart_recreation_and_successive_switches -v --durations=20
```

Wrap each `subprocess.run([...docker...])` block with timers when prototyping (not committed unless logging is permanent).

## Recommended Phase 6 work

1. Open **`perf/compose-selection-ci-pull`** PR: remove redundant pull; assert image present before `up`.
2. Add **job summary timing** for Docker sub-steps (pull / up / recreate / down).
3. Re-measure Docker job critical path; target **15–25%** reduction without losing matrix coverage.
