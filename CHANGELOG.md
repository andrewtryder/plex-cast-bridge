# Changelog

## [0.2.3](https://github.com/andrewtryder/plex-cast-bridge/compare/v0.2.2...v0.2.3) (2026-11-12)


### Features

* add diagnostics, validated backup restore, and Proxmox rollback ([6678cf9](https://github.com/andrewtryder/plex-cast-bridge/commit/6678cf9fa74d0d59d2de269971de022fec1e8245))
* add optional local music index foundation ([#31](https://github.com/andrewtryder/plex-cast-bridge/issues/31)) ([3fc2e11](https://github.com/andrewtryder/plex-cast-bridge/commit/3fc2e1104efc851bec2be72f699ea5c115a4013a))
* add password-free dashboard sessions ([#23](https://github.com/andrewtryder/plex-cast-bridge/issues/23)) ([7eaa9da](https://github.com/andrewtryder/plex-cast-bridge/commit/7eaa9da5714671c25be22e738792ff920ae135db))
* add Plex JWT connect and minimal Player/DJ UI ([2b28aa4](https://github.com/andrewtryder/plex-cast-bridge/commit/2b28aa4bc5c9235e876c8681a8a47e77bb0b89f2))
* add queue, library browse, artwork proxy, and volume settings ([1f2640a](https://github.com/andrewtryder/plex-cast-bridge/commit/1f2640ad3fea090f08e32170737d157f397a493e))
* add release safety and acceptance tooling ([#43](https://github.com/andrewtryder/plex-cast-bridge/issues/43)) ([02cfd5b](https://github.com/andrewtryder/plex-cast-bridge/commit/02cfd5bd60e1475f11ff1971b15e6d0f52b013e2))
* approve Google Home links without passwords ([11040b3](https://github.com/andrewtryder/plex-cast-bridge/commit/11040b3029ce46337feabdc77f52a6e8a14d80ef))
* diagnostics, backup restore, and Proxmox rollback ([87e1623](https://github.com/andrewtryder/plex-cast-bridge/commit/87e16236c83f6688854291e5d3e3434c5e3a9e9c))
* Google Home catalog sync reliability ([5eeaada](https://github.com/andrewtryder/plex-cast-bridge/commit/5eeaadae4a885acc3704fa696e02072b98e2dba2))
* harden Google Home catalog sync lifecycle and Request Sync ([0d50ee4](https://github.com/andrewtryder/plex-cast-bridge/commit/0d50ee4826c9063af74f75c5e0e762f2dee86413))
* move speaker management into Settings ([#26](https://github.com/andrewtryder/plex-cast-bridge/issues/26)) ([521b108](https://github.com/andrewtryder/plex-cast-bridge/commit/521b108d47fd1a4532112a9a52ebe0e867ffd1fb))
* music player queue, library, artwork, and volume settings ([a4759d3](https://github.com/andrewtryder/plex-cast-bridge/commit/a4759d381c45dbad7524c33d411bd15222dcc3be))
* per-speaker virtual Google Home devices ([#19](https://github.com/andrewtryder/plex-cast-bridge/issues/19)) ([c5f005c](https://github.com/andrewtryder/plex-cast-bridge/commit/c5f005c95d30836cc83275a483b5edb8b17a17b4))
* Plex Connect and minimal Player/DJ UI ([8388bb0](https://github.com/andrewtryder/plex-cast-bridge/commit/8388bb08f3f47e55085afe0ed25a323eb752d9de))
* polish UI static assets and document manual E2E validation ([194e541](https://github.com/andrewtryder/plex-cast-bridge/commit/194e541a96b6b0c1141017822b38c43627de8a73))
* preview and manage bridge mixes ([#32](https://github.com/andrewtryder/plex-cast-bridge/issues/32)) ([4304883](https://github.com/andrewtryder/plex-cast-bridge/commit/4304883a5170e295181b2d04d638777ca14e31f9))
* restore cast volume and support voice queue controls ([fb29f58](https://github.com/andrewtryder/plex-cast-bridge/commit/fb29f580413bbc6724d5c13354e8fec67a745fdf))
* secure Google Home setup wizard ([e2dced7](https://github.com/andrewtryder/plex-cast-bridge/commit/e2dced7a755ec49fc0e6cfbc7dff779a57dc59ae))
* secure Google Home wizard, companion pairing, and OAuth validation ([e053c4a](https://github.com/andrewtryder/plex-cast-bridge/commit/e053c4aaa3da2a3253d38ab8be18c3ecbc5ee0a3))
* simplify trusted LAN dashboard navigation ([#30](https://github.com/andrewtryder/plex-cast-bridge/issues/30)) ([dfb5cd2](https://github.com/andrewtryder/plex-cast-bridge/commit/dfb5cd207cc010bb79605d2c27bea5c7d2b0b534))
* UI polish, tutorials, and manual E2E checklist ([ba8dec1](https://github.com/andrewtryder/plex-cast-bridge/commit/ba8dec16d939a026cfc8d13873414bbfee82937a))
* update Proxmox upgrade workflow with verified rollback and explicit image pull ([#24](https://github.com/andrewtryder/plex-cast-bridge/issues/24)) ([7d3cff6](https://github.com/andrewtryder/plex-cast-bridge/commit/7d3cff674f1744dcb2b9a9d84f51ebd3d2a4cb08))


### Bug Fixes

* declare google-cloud-storage in cloud-gateway deps ([96be518](https://github.com/andrewtryder/plex-cast-bridge/commit/96be518a39a64dd40c2719ae62e3cb885b964030))
* declare google-cloud-storage in cloud-gateway deps ([64ad2f1](https://github.com/andrewtryder/plex-cast-bridge/commit/64ad2f116d80ede8c21d9bd438c32564bec770fc))
* declare google-cloud-storage in cloud-gateway deps ([6ad7b04](https://github.com/andrewtryder/plex-cast-bridge/commit/6ad7b04678eae2edc4b59603cfa5114230b6da89))
* declare google-cloud-storage in cloud-gateway deps ([bdeade7](https://github.com/andrewtryder/plex-cast-bridge/commit/bdeade74750a8831e4925603354620b3cdc57a30))
* emit Google Home command logs to Docker ([abbb5f9](https://github.com/andrewtryder/plex-cast-bridge/commit/abbb5f9b84374714377971de2115601997ac2428))
* emit Google Home worker logs to container ([7ebed41](https://github.com/andrewtryder/plex-cast-bridge/commit/7ebed418283e0313c364148df263d40c05fc040a))
* handle Cloud Run traffic without percentages ([#44](https://github.com/andrewtryder/plex-cast-bridge/issues/44)) ([4798d99](https://github.com/andrewtryder/plex-cast-bridge/commit/4798d990db106662c2b848e6ec646aa768b5b601))
* implement Google Home volume controls ([#42](https://github.com/andrewtryder/plex-cast-bridge/issues/42)) ([7fe3331](https://github.com/andrewtryder/plex-cast-bridge/commit/7fe33314597e94a31e0bdc999b42bee3ddc29072))
* merge dashboard injection safeguards ([bf05432](https://github.com/andrewtryder/plex-cast-bridge/commit/bf0543218e06e3be7e5131ed9259cf4952d85a61))
* persist and revoke Google Home refresh tokens ([#37](https://github.com/andrewtryder/plex-cast-bridge/issues/37)) ([2dff5c6](https://github.com/andrewtryder/plex-cast-bridge/commit/2dff5c6a33c582c8d26f888cb5531b3f66c9aca6))
* protect persistent state during upgrades ([#29](https://github.com/andrewtryder/plex-cast-bridge/issues/29)) ([d77fdcf](https://github.com/andrewtryder/plex-cast-bridge/commit/d77fdcfbd97380c8a09464e7f5dbd2d0be7e20fd))
* redact gateway key from sync responses ([#28](https://github.com/andrewtryder/plex-cast-bridge/issues/28)) ([6f06ce5](https://github.com/andrewtryder/plex-cast-bridge/commit/6f06ce59d9b2c17398710ecf8341826f8061679e))
* render dashboard data without html injection ([c5abadc](https://github.com/andrewtryder/plex-cast-bridge/commit/c5abadcb29f7a197d3a07756ae00ea4a26f52107))
* require fresh Plex revision for scan skips ([#33](https://github.com/andrewtryder/plex-cast-bridge/issues/33)) ([053ca35](https://github.com/andrewtryder/plex-cast-bridge/commit/053ca35e77743b3e75717c2e80597a3fc7196b32))
* reset cloud catalog store per test client ([7792414](https://github.com/andrewtryder/plex-cast-bridge/commit/7792414cbaf41971140bbb48b5ff00433c430ab6))
* reset cloud catalog store per test client ([947ef14](https://github.com/andrewtryder/plex-cast-bridge/commit/947ef14c5c8e140be98879d11de24578078334ad))
* reset cloud catalog store per test client ([0ea7b20](https://github.com/andrewtryder/plex-cast-bridge/commit/0ea7b207d8a18f3293a699c5877b1657532fa00e))
* reset cloud catalog store per test client ([fb6d042](https://github.com/andrewtryder/plex-cast-bridge/commit/fb6d042b0bc84c76f3489bedc5e79c3852f5579d))
* restrict diagnostic errors and cors headers ([f7105d0](https://github.com/andrewtryder/plex-cast-bridge/commit/f7105d00e1005c00146f90352e73d5e6bcac826d))
* retry Cast group connections on port 8009 ([#36](https://github.com/andrewtryder/plex-cast-bridge/issues/36)) ([c69e67e](https://github.com/andrewtryder/plex-cast-bridge/commit/c69e67e45451c6ac52c51b52b3b3fbba341cd5a3))
* secure Proxmox upgrade backups ([#34](https://github.com/andrewtryder/plex-cast-bridge/issues/34)) ([a80ede0](https://github.com/andrewtryder/plex-cast-bridge/commit/a80ede0ae68ff2f140971ab17372777e1b7e6eb4))
* serialize Cast device discovery ([#38](https://github.com/andrewtryder/plex-cast-bridge/issues/38)) ([d8cb70b](https://github.com/andrewtryder/plex-cast-bridge/commit/d8cb70b5ce477d17f904eef238adacb62f1fd2a4))
* tolerate untitled Plex tracks in index scan ([#35](https://github.com/andrewtryder/plex-cast-bridge/issues/35)) ([ba9093f](https://github.com/andrewtryder/plex-cast-bridge/commit/ba9093f0a584071b86f799d35b221cb0c4dedd51))


### Documentation

* drop Grok Bot tutorial link from enhancement report ([9642fe9](https://github.com/andrewtryder/plex-cast-bridge/commit/9642fe913af7b0813b540df3a27bdd4162011900))
* drop Grok Bot tutorial link from enhancement report ([fa4e9df](https://github.com/andrewtryder/plex-cast-bridge/commit/fa4e9df7930a9377a627b1b76525fb4d18f5654e))
* retire obsolete Google Home rebuild guidance ([#25](https://github.com/andrewtryder/plex-cast-bridge/issues/25)) ([d6c0cd2](https://github.com/andrewtryder/plex-cast-bridge/commit/d6c0cd2a9e4ea2fb3d9172d0168f0eee2b0fe233))

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
