# Phase 11 — Docker CI performance (measurement)

Source: GitHub Actions runs on `main` post-Phase 8 merge (`45066817996` @ `7a56ee4`, 2026-11-22).

Docker job total: **~881s (14m41s)** for `pytest -m docker_integration` (30 tests, 531 deselected in job).

## Slowest tests (call phase)

| Rank | Duration | Test |
|------|----------|------|
| 1 | 142.94s | `test_upgrade_end_to_end.py::test_reviewed_upgrade_then_image_rollback_keeps_existing_data[False]` |
| 2 | 136.37s | `test_upgrade_end_to_end.py::test_reviewed_upgrade_then_image_rollback_keeps_existing_data[True]` |
| 3 | 114.18s | `test_upgrade_end_to_end.py::test_failed_candidate_recovers_old_image_without_overwriting_newer_data[health]` |
| 4 | 104.41s | `test_upgrade_end_to_end.py::test_failed_candidate_recovers_old_image_without_overwriting_newer_data[startup]` |
| 5 | 74.70s | `test_failed_candidate_recovers...` `[interruption]` |
| 6 | 63.49s | `test_failed_candidate_recovers...` `[creation]` |
| 7 | 55.07s | `test_upgrade_real_bridge.py::test_real_bridge_upgrade_preserves_plex_configuration[True]` |
| 8 | 53.34s | `test_upgrade_real_bridge.py::...` `[False]` |
| 9 | 44.18s | `test_docker_portainer_upgrade.py::test_real_older_image_to_candidate_upgrade_and_preservation` |
| 10 | 19.63s | `test_failed_candidate_recovers...` `[pull]` |

**Compose selection** (Phase 6 fix retained): **9.09s** — no longer a top bottleneck.

## Cost classification

| Category | Dominant tests | Notes |
|----------|----------------|-------|
| Upgrade + rollback matrix | `test_upgrade_end_to_end` (parametrized) | Required safety coverage; ~75% of wall time |
| Real-bridge upgrade | `test_upgrade_real_bridge` | Two parameter variants |
| Published-image preservation | `test_docker_portainer_upgrade::test_real_older_image...` | Single heavy case |
| Image pull within tests | `[pull]` mode ~20s | Candidate usually pre-built in job |
| Fixture/setup | Job preamble (build candidate, registry) | Outside pytest durations |

## Safe optimization candidates (future PR only)

1. **Share one upgrade scenario artifact** across parametrized rollback tests where assertions are identical except for `PCB_UPGRADE` flag — only after proving isolation.
2. **Assert image presence** before internal pulls inside `test_failed_candidate_recovers...` `[pull]` (similar to compose-selection Phase 6 pattern).
3. **Structured phase timers** in Docker job summary for build vs pytest vs teardown — observability only.

Do **not** remove rollback/upgrade matrix cases or parallelize shared-daemon tests without per-project isolation proof.
