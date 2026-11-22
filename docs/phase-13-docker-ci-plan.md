# Phase 13 — Docker CI optimization plan

No code changes in this branch (measurement-only).

## Measured baseline (main @ ee152d7, run 45066817996)

- Docker integration pytest: **881.08s** (30 tests)
- Top cost: `test_upgrade_end_to_end` rollback matrix (~280s combined for True/False)
- `test_failed_candidate_recovers...` modes: ~375s combined
- `test_upgrade_real_bridge`: ~104s (2 params)
- Compose selection: **8.66s** (already optimized)

## Safe next steps (future PR)

1. Instrument upgrade tests with phase timers in CI artifacts.
2. Skip redundant in-test `docker pull` when digest already loaded (per-mode, isolated).
3. Evaluate splitting **rollback** vs **failed-candidate** matrices into separate jobs with isolated project names — only after isolation proof.

**Do not merge without measured benefit and safety review.**
