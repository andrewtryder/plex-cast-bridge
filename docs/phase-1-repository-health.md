# Phase 1 repository health baseline

Baseline: `main` at `8eb6297` (the public `v0.5.0` release remains
`e847594`). This phase deliberately changes test/CI architecture only; it does
not refactor production modules.

## Inventory and test overlap

`pytest --collect-only -q` collected **463** nodes before the change. The
same command after marker registration collects **463** nodes: **435** `unit`
and **28** `docker_integration`. The Docker subset is also tagged
`upgrade_e2e` where it exercises an upgrade boundary. `workflow` is an
orthogonal label for release-workflow contract tests.

| CI execution | Selection | Nodes | Relationship to another execution |
| --- | --- | ---: | --- |
| Python 3.14 | `-m 'not docker_integration'` | 435 | Separate unit/process/workflow tests; Docker nodes are excluded. |
| Local bridge Docker integration | `-m docker_integration` | 28 | Runs each Docker node once with a cached candidate image. |
| Older published image -> candidate | `test_upgrade_real_bridge...[True]` | 1 | Intentional compatibility matrix: unlike the published-target run, target is the PR candidate and verifies Python 3.14. |
| Cloud Run gateway image | Build only | n/a | Independent gateway container validation; it does not execute local-bridge tests. |

The prior Python job collected all 463 nodes and, on a Docker-capable runner,
could execute the Portainer upgrade tests. The Docker job then selected that
file again. The explicit published-release `legacy_env=True` invocation was an
**exact duplicate** of the consolidated Docker selection and has been removed.
The candidate invocation remains **intentional different-environment coverage**.
The diagnostic, backup, SQLite, Compose, older-image, candidate-image and
rollback cases remain in the Docker suite; no test was deleted.

## Measured local baseline

On Python 3.14, before the partition change, the full suite completed with
**445 passed, 18 skipped in 218.45 s**. Slowest calls were:

| Test | Seconds |
| --- | ---: |
| `test_real_older_image_to_candidate_upgrade_and_preservation` | 49.79 |
| `test_actual_compose_lifecycle_recreation` | 21.91 |
| `test_storage_safety_wrong_data_path_rejected_in_container` | 16.53 |
| `test_storage_safety_unexpectedly_empty_data_rejected_in_container` | 15.96 |
| `test_failure_behavior_unmounted_data_refusal` | 13.30 |

Those are Docker/shared-daemon tests and remain sequential. No `pytest-xdist`
is enabled: process isolation has not been demonstrated for the shared Docker,
filesystem, service-port, and concurrency-sensitive scenarios.

## Measured post-change validation

CI run [#291](https://github.com/andrewtryder/plex-cast-bridge/actions/runs/44908941854)
validated commit `e829755` successfully. It took **15m47s wall-clock** from
workflow-job creation to the final Docker job completion; the critical-path
Docker job was **15m45s**. The independent jobs were intentionally concurrent:

| Job or step | Measured duration | Result |
| --- | ---: | --- |
| Pre-commit & Linters | 12 s | success |
| Python 3.14 test suite | 36 s (tests: 28 s) | success |
| Cloud Run requirements drift | 7 s | success |
| Markdown relative links | 6 s | success |
| Cloud Run gateway image build | 20 s (Buildx step: 5 s) | success |
| Local bridge candidate Buildx build | 9 s | success |
| Sequential Docker-integration matrix | 14m20s | success |
| Published older image -> Python 3.14 candidate | 58 s | success |

This is an initial cache-population measurement, not a before/after speed
claim. Buildx GHA cache import/export uses the separate `local-bridge` and
`cloud-run-gateway` scopes, but the public job metadata does not expose a
per-layer cache hit/miss count. Consequently the report records the observed
build durations and **does not claim a cache-hit performance improvement**.
The 9-second local candidate build confirms that Docker integration tests reused
the prebuilt image rather than triggering another candidate build.

## CI changes

* PR-only concurrency cancellation prevents obsolete PR runs from consuming
  runners. Pushes to `main` are retained; manually dispatched candidate and
  promotion release work is not in this group.
* The existing `Docker build (local + Cloud Run)` check name is retained to
  avoid an unannounced branch-protection migration. Cloud Run has a new,
  independent `Cloud Run gateway image build` check.
* Normal PR builds use Buildx GHA cache scopes `local-bridge` and
  `cloud-run-gateway`, preventing unrelated Dockerfiles from evicting each
  other. `setup-uv` dependency caching is enabled in each applicable job.
* The local bridge is built once, loaded under the test tag, and reused by
  image integration tests through `PCB_TEST_PREBUILT_IMAGE=1`.
* The Cloud Run job stages the repository sample catalog in its otherwise
  isolated build context, matching the prior build contract.

## Complexity and refactoring map

Radon (`radon cc -s -n C src gcp-cloud-run/src scripts`) identifies these
highest-priority production seams: `wizard_verify.verify_step` (84),
`plex_service.PlexService.resolve` (60), `config_drift.diagnose` (47),
`speaker_registry.reconcile_speakers` (42), and
`cast_discovery.discover_via_dnssd` (23). Ruff's McCabe rule is enabled with a
50 baseline ratchet; existing hotspots are reported, but Phase 1 does not
change their behavior just to meet a threshold.

Largest modules are `scripts/upgrade.py` (1,262 lines),
`scripts/release_acceptance.py` (855), `speaker_queue.py` (851), `routes.py`
(825), `cast_manager.py` (676), and Cloud Run `smart_home.py` (655). The most
import-dense modules are `main.py` (27 imports), `scripts/upgrade.py` (27),
and `routes.py` (19), making them sensible dependency-boundary review targets.

Priority after Phase 1 review:

1. Extract pure verification/planning helpers from upgrade and release scripts.
2. Split speaker reconciliation and Plex resolution into identity, transport,
   and persistence operations with focused contract tests.
3. Separate startup wiring from runtime lifecycle in local and Cloud Run mains.
4. Evaluate unused-code candidates only with static analysis plus runtime/API
   references; none are deleted in this phase.

Ruff's unused-import checks found no current production candidate. An
import-graph-only scan would flag standalone operational entry points such as
`scripts/test_live_cast.py`, `scripts/test_voice_fulfillment.py`, and
`scripts/release_acceptance.py`; those are not dead code merely because they
are CLI scripts, so they are explicitly retained pending invocation and user
workflow evidence.

## Quality-gate status

The local baseline above includes all existing storage and rollback tests.
This PR adds Radon, Ruff McCabe, duration reporting, and coverage contexts.
Run CodeQL, actionlint/pre-commit, release-workflow tests, Docker/image tests,
and Cloud Run build checks in GitHub Actions; no deployment, Firestore write,
credential change, linking change, or release publication is authorized.
