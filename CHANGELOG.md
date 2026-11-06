# Changelog

## [0.2.2](https://github.com/andrewtryder/plex-cast-bridge/compare/v0.2.1...v0.2.2) (2026-11-06)


### Bug Fixes

* resolve CodeQL alerts and update github actions ([#9](https://github.com/andrewtryder/plex-cast-bridge/issues/9)) ([37e37a1](https://github.com/andrewtryder/plex-cast-bridge/commit/37e37a18416b374866f6076e21a9660f9e5a7857))


### Documentation

* require X-API-Key in Proxmox curl example ([0fc72b6](https://github.com/andrewtryder/plex-cast-bridge/commit/0fc72b6bd86ba12eb8c788dbc73c249d89c718f6))
* require X-API-Key in Proxmox curl playback example ([7dce626](https://github.com/andrewtryder/plex-cast-bridge/commit/7dce626a72e7e9e95ece73d03eb9e80d3c93f3d0))

## [0.2.1](https://github.com/andrewtryder/plex-cast-bridge/compare/plex-cast-bridge-v0.2.0...plex-cast-bridge-v0.2.1) (2026-11-06)


### Features

* add visual setup docs, local speaker web UI, room-aware routing, and 0ms artist fast-path ([0502088](https://github.com/andrewtryder/plex-cast-bridge/commit/0502088b051190cac7c581b7525a83afe9495513))
* **audio:** cross-platform speaker discovery and headless Linux support ([3dc4ce5](https://github.com/andrewtryder/plex-cast-bridge/commit/3dc4ce504f30ebf13f0119da11455c5fcf806f3e))
* **cast:** resilient direct IP speaker resolution and catalog fallback ([5e93b11](https://github.com/andrewtryder/plex-cast-bridge/commit/5e93b11d8cbc1448fc08982123c63c163feb47c6))
* **deploy:** harden Dockerfile, add non-root user, /data persistence, GHCR multi-arch workflow, and Proxmox deployment guide ([cf62359](https://github.com/andrewtryder/plex-cast-bridge/commit/cf623595a2368d97e0926b73a0e6de67d59b0038))
* Plex Cast Bridge v0.2.0 (GCP Cloud Run Gateway, Gemini Smart DJ, Cast Manager) ([71e6aa7](https://github.com/andrewtryder/plex-cast-bridge/commit/71e6aa7c839ba4e64cbc733926fc606e0108003b))


### Bug Fixes

* close Cast stale-failure race and related review findings ([3b3d355](https://github.com/andrewtryder/plex-cast-bridge/commit/3b3d35548808e2b17e0e9807b42f2f2e09a95d0a))
* correlate Cast failures without discarding replacement loads ([db0281c](https://github.com/andrewtryder/plex-cast-bridge/commit/db0281cae1f32a7b4acefab0fadb0d4d972aec8c))
* **deploy:** add /health endpoint, remove redundant .env mount, fix credentials permissions, and update cutover command ([1b49700](https://github.com/andrewtryder/plex-cast-bridge/commit/1b497002d3e4b663f4ba4058eb054dbbedc271bb))
* **discovery:** merge catalog speakers and probe TCP reachability in discover() ([842f4a0](https://github.com/andrewtryder/plex-cast-bridge/commit/842f4a0bcc1069128eb79eb38d340dab8b345f4b))
* harden local auth, Cast races, and Cloud Run secret handling ([7b2b64d](https://github.com/andrewtryder/plex-cast-bridge/commit/7b2b64d9f8b2e64e2b449e6312191fbaad66ef32))
* ignore unidentified superseded Cast failures until watchdog ([8cf839b](https://github.com/andrewtryder/plex-cast-bridge/commit/8cf839b926c759a9b46c5a4f8514d5d61c0764fd))
* **ops:** fail closed on firestore outage in production, verify subscription dlq, and persist dlq volume ([b19c59e](https://github.com/andrewtryder/plex-cast-bridge/commit/b19c59e114e267f11c12f55a3074c1efcae2da84))
* **reliability:** add distributed firestore jti store, pubsub dlq, and command-only state handling ([c6059f3](https://github.com/andrewtryder/plex-cast-bridge/commit/c6059f389a3e573c4697a1eae979ca329c8773b0))
* security and correctness remediations ([59ccb0a](https://github.com/andrewtryder/plex-cast-bridge/commit/59ccb0aadde935025001cb81af409ac046539316))
* **security:** resolve all code review findings (P0-P2) ([ed30781](https://github.com/andrewtryder/plex-cast-bridge/commit/ed30781918e01ac206eabf85d3f492edd78d9369))
* **transport:** handle media controller timeouts gracefully during transport actions ([d74d22e](https://github.com/andrewtryder/plex-cast-bridge/commit/d74d22ec8be15b1dacd547b00704dd9d6f0d930b))
* **transport:** use media_controller.play for resume and alias next to skip ([881b5b1](https://github.com/andrewtryder/plex-cast-bridge/commit/881b5b181eed9d9dfcdc36474633cf0b883c708f))


### Documentation

* document unprivileged Proxmox Debian LXC deployment and resilience patterns ([ac35f8a](https://github.com/andrewtryder/plex-cast-bridge/commit/ac35f8af0679d9d75f98ead52c286388bedad018))
* public release readiness ([88fbd01](https://github.com/andrewtryder/plex-cast-bridge/commit/88fbd010982626ed9efffb7d5ada7707dcfe470d))
* public release readiness for Docker-first onboarding ([6ab3f22](https://github.com/andrewtryder/plex-cast-bridge/commit/6ab3f22f56d03b5153a0965f98c63b106dfd2415))
