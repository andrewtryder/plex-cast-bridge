# Changelog

## [0.6.0](https://github.com/andrewtryder/plex-cast-bridge/compare/v0.5.0...v0.6.0) (2026-11-22)


### Features

* feat: add dashboard static modules and playback coverage ([2342185](https://github.com/andrewtryder/plex-cast-bridge/commit/2342185774accd82bee3d8a46262fce502710520))

### Bug Fixes

* fix(speaker-registry): filter discovery before UUID dedupe ([3fbccba](https://github.com/andrewtryder/plex-cast-bridge/commit/3fbccba913acb8003851480f436a1e6755f05898))
* fix: tolerate database_path errors during lifespan shutdown ([d030fd2](https://github.com/andrewtryder/plex-cast-bridge/commit/d030fd2fb747ccd9992bb697330527bd737de1c0))
* fix: restore legacy shutdown order and startup abort cleanup (Phase 8) ([68f4302](https://github.com/andrewtryder/plex-cast-bridge/commit/68f43025c835a5892bbe75a5c1a69dbba1a4b92b))
* fix: speed compose selection test shutdown and CI pytest capture ([466e91f](https://github.com/andrewtryder/plex-cast-bridge/commit/466e91fc148da6030786fc9487873175ca1ee8a7))
* fix: validate immutable image refs before Docker calls ([3956555](https://github.com/andrewtryder/plex-cast-bridge/commit/39565555cd1943deea8b0a658e5dde53eff75047))
* fix: retain prebuilt image across Docker matrix ([e829755](https://github.com/andrewtryder/plex-cast-bridge/commit/e829755acca9f0130f7ce12b65499a8cea3772af))
* fix: stage Cloud Run build catalog ([16ae8a3](https://github.com/andrewtryder/plex-cast-bridge/commit/16ae8a3cbca7a13a6a0d837be7fcb38585d7ef5e))
* fix: remove duplicate cloud gateway pytest import ([45f0e02](https://github.com/andrewtryder/plex-cast-bridge/commit/45f0e02451cba63ca0491b21034fa77e7e3cdb89))

### Performance Improvements

* perf: skip redundant compose-selection image pulls ([b889f9a](https://github.com/andrewtryder/plex-cast-bridge/commit/b889f9ae7970445b823c8917af7fb3c57d0a737a))

### Code Refactoring

* refactor: extract reconcile_speakers helpers (Phase 12) ([2271816](https://github.com/andrewtryder/plex-cast-bridge/commit/22718164946dd056366a1bd1012ef136de8e400f))
* refactor: modularize lifespan tasks and fix Pub/Sub startup leak (Phase 8) ([21528aa](https://github.com/andrewtryder/plex-cast-bridge/commit/21528aac283be3dd9a53ae3e9ca37e80088eb5e3))
* refactor: extract startup guard checks (Phase 8) ([c715656](https://github.com/andrewtryder/plex-cast-bridge/commit/c715656a2c6e229a3e0a809414200a4eaaf2bea2))
* refactor: split config drift classification helpers (Phase 7) ([5a713ce](https://github.com/andrewtryder/plex-cast-bridge/commit/5a713cea8ddda166bc0fa7a86910c67e2bca0b88))
* refactor: modularize release-acceptance verification (Phase 5) ([e60a7bd](https://github.com/andrewtryder/plex-cast-bridge/commit/e60a7bd521b68feb232e80fe64dfdc8a15f473b2))
* refactor: extract Google Home wizard verify steps (Phase 4) ([d29a5a8](https://github.com/andrewtryder/plex-cast-bridge/commit/d29a5a8397cdbe2cc4a8436431fb899f2568bb12))
* refactor: extract Plex music resolution helpers (Phase 3) ([d7690a4](https://github.com/andrewtryder/plex-cast-bridge/commit/d7690a48478c7d18f21971852752d5526b62d8b1))
* refactor: split local dashboard routes into focused modules ([ad78485](https://github.com/andrewtryder/plex-cast-bridge/commit/ad7848542c3483a4b8ea849adf2bc825c7735eda))

### Tests

* test: add extended synthetic lifespan integration coverage ([d57d848](https://github.com/andrewtryder/plex-cast-bridge/commit/d57d8485421b06620cf97aa614802a4c5b5bba5b))
* test: add reconcile snapshots and catalog persistence characterization (Phase 9) ([5d6b6c0](https://github.com/andrewtryder/plex-cast-bridge/commit/5d6b6c09622140c23faabd48a0fcb7801c81efc9))
* test: expand speaker identity and migration characterization (Phase 9) ([6d59b6c](https://github.com/andrewtryder/plex-cast-bridge/commit/6d59b6c1d9e941a7d54f1c8e66b04e8720dcf18f))
* test: characterize speaker reconciliation edge cases (Phase 9) ([f9fc05c](https://github.com/andrewtryder/plex-cast-bridge/commit/f9fc05c0425ede930502c84280c163903e171cba))
* test: expand startup guard and lifespan characterization (Phase 8) ([8f37bf1](https://github.com/andrewtryder/plex-cast-bridge/commit/8f37bf1824b5b2d09c5de5d3fba5c1664a7703ac))
* test: lock config drift diagnose semantics to main baseline ([4dea4a9](https://github.com/andrewtryder/plex-cast-bridge/commit/4dea4a9ac8ab58304ba92201d2fc76c37f6f2616))
* test: isolate ephemeral git repos from workspace config ([77b6d1e](https://github.com/andrewtryder/plex-cast-bridge/commit/77b6d1e50bb1e8826a9a9c50a2ac50b9a92b1c7b))
* test: classify compose selection Docker cases ([d958260](https://github.com/andrewtryder/plex-cast-bridge/commit/d95826015dd6c2fd5ba6dccb9fd13fa6d95c2ba5))

### Documentation

* docs: Phase 13 Docker CI optimization plan (measurement only) ([4beb6ec](https://github.com/andrewtryder/plex-cast-bridge/commit/4beb6ec1756b7d897c1ce274a2cf4063893dbcdc))
* docs: refine Phase 10 migration analysis with call paths and risk matrix ([10062be](https://github.com/andrewtryder/plex-cast-bridge/commit/10062bef0f75ecea5035539131a209141d759f0b))
* docs: record Phase 11 Docker CI slow-test measurements ([215cf5f](https://github.com/andrewtryder/plex-cast-bridge/commit/215cf5fed231a3a9176028f9ccfed3ad75a1be94))
* docs: add Phase 10 identity migration proposal (draft) ([e46937c](https://github.com/andrewtryder/plex-cast-bridge/commit/e46937cd333994772c1b6b3448bbd44ef9deb2b5))
* docs: record Phase 1 CI measurement ([b79a3f4](https://github.com/andrewtryder/plex-cast-bridge/commit/b79a3f40dd2d0a728282505fbe17295234a5cc8f))

### Continuous Integration

* ci: replace Node 20 pre-commit cache action ([37625ee](https://github.com/andrewtryder/plex-cast-bridge/commit/37625ee06ab47756cbe5985fd31bffda9ab68732))
* ci: partition tests and cache candidate builds ([d6177eb](https://github.com/andrewtryder/plex-cast-bridge/commit/d6177eb9b50b47a788e7ffc5326d54eb9fcd30a3))

## [0.5.0](https://github.com/andrewtryder/plex-cast-bridge/compare/v0.2.2...v0.5.0) (2026-11-19)


### Features

* add diagnostics, validated backup restore, and Proxmox rollback ([6678cf9](https://github.com/andrewtryder/plex-cast-bridge/commit/6678cf9fa74d0d59d2de269971de022fec1e8245))
* add optional local music index foundation ([#31](https://github.com/andrewtryder/plex-cast-bridge/issues/31)) ([3fc2e11](https://github.com/andrewtryder/plex-cast-bridge/commit/3fc2e1104efc851bec2be72f699ea5c115a4013a))
* add password-free dashboard sessions ([#23](https://github.com/andrewtryder/plex-cast-bridge/issues/23)) ([7eaa9da](https://github.com/andrewtryder/plex-cast-bridge/commit/7eaa9da5714671c25be22e738792ff920ae135db))
* add Plex JWT connect and minimal Player/DJ UI ([2b28aa4](https://github.com/andrewtryder/plex-cast-bridge/commit/2b28aa4bc5c9235e876c8681a8a47e77bb0b89f2))
* add queue, library browse, artwork proxy, and volume settings ([1f2640a](https://github.com/andrewtryder/plex-cast-bridge/commit/1f2640ad3fea090f08e32170737d157f397a493e))
* add release safety and acceptance tooling ([#43](https://github.com/andrewtryder/plex-cast-bridge/issues/43)) ([02cfd5b](https://github.com/andrewtryder/plex-cast-bridge/commit/02cfd5bd60e1475f11ff1971b15e6d0f52b013e2))
* apply and verify reviewed local upgrades ([00971f8](https://github.com/andrewtryder/plex-cast-bridge/commit/00971f822adb8a3b1dd02432ad090ea1071bc766))
* approve Google Home links without passwords ([11040b3](https://github.com/andrewtryder/plex-cast-bridge/commit/11040b3029ce46337feabdc77f52a6e8a14d80ef))
* diagnostics, backup restore, and Proxmox rollback ([87e1623](https://github.com/andrewtryder/plex-cast-bridge/commit/87e16236c83f6688854291e5d3e3434c5e3a9e9c))
* Google Home catalog sync reliability ([5eeaada](https://github.com/andrewtryder/plex-cast-bridge/commit/5eeaadae4a885acc3704fa696e02072b98e2dba2))
* harden Google Home catalog sync lifecycle and Request Sync ([0d50ee4](https://github.com/andrewtryder/plex-cast-bridge/commit/0d50ee4826c9063af74f75c5e0e762f2dee86413))
* inspect and review immutable upgrade targets ([d3f676a](https://github.com/andrewtryder/plex-cast-bridge/commit/d3f676a3532eac41920bf32b368fe9cb8b51b96f))
* make existing-install upgrades safer and simpler ([d2fd593](https://github.com/andrewtryder/plex-cast-bridge/commit/d2fd593c4bcb52c13cb5842dd879e0b5c7c2cb9c))
* move speaker management into Settings ([#26](https://github.com/andrewtryder/plex-cast-bridge/issues/26)) ([521b108](https://github.com/andrewtryder/plex-cast-bridge/commit/521b108d47fd1a4532112a9a52ebe0e867ffd1fb))
* music player queue, library, artwork, and volume settings ([a4759d3](https://github.com/andrewtryder/plex-cast-bridge/commit/a4759d381c45dbad7524c33d411bd15222dcc3be))
* per-speaker virtual Google Home devices ([#19](https://github.com/andrewtryder/plex-cast-bridge/issues/19)) ([c5f005c](https://github.com/andrewtryder/plex-cast-bridge/commit/c5f005c95d30836cc83275a483b5edb8b17a17b4))
* Plex Connect and minimal Player/DJ UI ([8388bb0](https://github.com/andrewtryder/plex-cast-bridge/commit/8388bb08f3f47e55085afe0ed25a323eb752d9de))
* polish UI static assets and document manual E2E validation ([194e541](https://github.com/andrewtryder/plex-cast-bridge/commit/194e541a96b6b0c1141017822b38c43627de8a73))
* prepare reviewed Compose image selection ([37834d1](https://github.com/andrewtryder/plex-cast-bridge/commit/37834d104a088c30e6422894cac0a0e734e8ada8))
* preview and manage bridge mixes ([#32](https://github.com/andrewtryder/plex-cast-bridge/issues/32)) ([4304883](https://github.com/andrewtryder/plex-cast-bridge/commit/4304883a5170e295181b2d04d638777ca14e31f9))
* **release:** decouple candidate image build from public promotion and sync lockfile ([00a1533](https://github.com/andrewtryder/plex-cast-bridge/commit/00a153304fd78d52a453fbdbbcdee1121af59410))
* **release:** decouple candidate verification from public promotion and sync uv.lock ([8b48b43](https://github.com/andrewtryder/plex-cast-bridge/commit/8b48b43bf3ee5cb132b590fb57a4bebbffd3ec59))
* restore cast volume and support voice queue controls ([fb29f58](https://github.com/andrewtryder/plex-cast-bridge/commit/fb29f580413bbc6724d5c13354e8fec67a745fdf))
* secure Google Home setup wizard ([e2dced7](https://github.com/andrewtryder/plex-cast-bridge/commit/e2dced7a755ec49fc0e6cfbc7dff779a57dc59ae))
* secure Google Home wizard, companion pairing, and OAuth validation ([e053c4a](https://github.com/andrewtryder/plex-cast-bridge/commit/e053c4aaa3da2a3253d38ab8be18c3ecbc5ee0a3))
* simplify trusted LAN dashboard navigation ([#30](https://github.com/andrewtryder/plex-cast-bridge/issues/30)) ([dfb5cd2](https://github.com/andrewtryder/plex-cast-bridge/commit/dfb5cd207cc010bb79605d2c27bea5c7d2b0b534))
* stage verified backups without changing live data ([672c68b](https://github.com/andrewtryder/plex-cast-bridge/commit/672c68b99276b27e1ceb6c5c9c14d724ffa7f843))
* support container-owned credentials and offline data restoration ([221e5d6](https://github.com/andrewtryder/plex-cast-bridge/commit/221e5d667420f840328f10452b6b0cc92af5bffa))
* support image-only Docker and Portainer deployments and upgrades ([8ccbd3e](https://github.com/andrewtryder/plex-cast-bridge/commit/8ccbd3e8fb54f0f69ccd2f5a66a249dfcc99c6b5))
* support image-only Docker and Portainer deployments and upgrades ([802d07b](https://github.com/andrewtryder/plex-cast-bridge/commit/802d07bf463ca7fcc5e0ef2ed4968f5fe931fd35))
* UI polish, tutorials, and manual E2E checklist ([ba8dec1](https://github.com/andrewtryder/plex-cast-bridge/commit/ba8dec16d939a026cfc8d13873414bbfee82937a))
* update Proxmox upgrade workflow with verified rollback and explicit image pull ([#24](https://github.com/andrewtryder/plex-cast-bridge/issues/24)) ([7d3cff6](https://github.com/andrewtryder/plex-cast-bridge/commit/7d3cff674f1744dcb2b9a9d84f51ebd3d2a4cb08))
* verify live music with read-only SQLite snapshots ([055117b](https://github.com/andrewtryder/plex-cast-bridge/commit/055117b2e490d8364b18196403a50e8636a78500))


### Bug Fixes

* add configuration drift to release safety ([c2394f6](https://github.com/andrewtryder/plex-cast-bridge/commit/c2394f6baa5bf73758229f402d4e6127851c3052))
* allocate monotonic OAuth exchange order ([27f0da7](https://github.com/andrewtryder/plex-cast-bridge/commit/27f0da75c55562ae03188117cee44f47938001b4))
* capture release evidence without mutating application state ([bdb765d](https://github.com/andrewtryder/plex-cast-bridge/commit/bdb765d3025c41769832fd5794436ec7e303088b))
* **ci:** close partial-publication recovery gaps and verify Git tag targets ([4240824](https://github.com/andrewtryder/plex-cast-bridge/commit/4240824154226551ac04e7caad4d920a8d0070d7))
* **ci:** verify candidate image before version tagging and add recovery mode ([5be9b68](https://github.com/andrewtryder/plex-cast-bridge/commit/5be9b685d46af9ce5861cc764eb2988a4926cda3))
* confirm Cast playback stopped before acknowledging ([a101505](https://github.com/andrewtryder/plex-cast-bridge/commit/a101505fec04a2204862b9f89e9102503d091c78))
* declare google-cloud-storage in cloud-gateway deps ([96be518](https://github.com/andrewtryder/plex-cast-bridge/commit/96be518a39a64dd40c2719ae62e3cb885b964030))
* declare google-cloud-storage in cloud-gateway deps ([64ad2f1](https://github.com/andrewtryder/plex-cast-bridge/commit/64ad2f116d80ede8c21d9bd438c32564bec770fc))
* declare google-cloud-storage in cloud-gateway deps ([6ad7b04](https://github.com/andrewtryder/plex-cast-bridge/commit/6ad7b04678eae2edc4b59603cfa5114230b6da89))
* declare google-cloud-storage in cloud-gateway deps ([bdeade7](https://github.com/andrewtryder/plex-cast-bridge/commit/bdeade74750a8831e4925603354620b3cdc57a30))
* **deploy:** enable custom image forwarding and harden systemd persistence ([211a054](https://github.com/andrewtryder/plex-cast-bridge/commit/211a0545088ff43b7ea0c486c171cdc06dad4873))
* **deploy:** enable custom image forwarding and harden systemd persistence ([4b7289d](https://github.com/andrewtryder/plex-cast-bridge/commit/4b7289ddc5419752da51c5de840574e1f893e9a7))
* **deploy:** harden storage safety, custom image naming, and Google Home credentials ([b26b19d](https://github.com/andrewtryder/plex-cast-bridge/commit/b26b19dfd73413f7cb41b8668357e4797321f92e))
* **deploy:** preserve legacy native data location precedence in systemd and preflight ([b5eb564](https://github.com/andrewtryder/plex-cast-bridge/commit/b5eb564ded3546addb4023913f7b04c2c23b707f))
* **deploy:** run credential preflight before database recovery ([ca22617](https://github.com/andrewtryder/plex-cast-bridge/commit/ca2261716dd29f8336708671eac03fcb42d2a071))
* emit Google Home command logs to Docker ([abbb5f9](https://github.com/andrewtryder/plex-cast-bridge/commit/abbb5f9b84374714377971de2115601997ac2428))
* emit Google Home worker logs to container ([7ebed41](https://github.com/andrewtryder/plex-cast-bridge/commit/7ebed418283e0313c364148df263d40c05fc040a))
* fail closed for non-Linux upgrade verification ([ac041e5](https://github.com/andrewtryder/plex-cast-bridge/commit/ac041e5b172d0567619f1c75cdd26c5f98dfde82))
* fail closed on partial Home gateway state ([11ae1a5](https://github.com/andrewtryder/plex-cast-bridge/commit/11ae1a5fb0f8e43b8a2a882f511a7b7072bcb6a9))
* fingerprint linked speaker catalog on upgrades ([10ecc59](https://github.com/andrewtryder/plex-cast-bridge/commit/10ecc59adbb041f780b91196edb5c4de4a6ffbae))
* guard automatic image recovery after failed replacement ([518ffa2](https://github.com/andrewtryder/plex-cast-bridge/commit/518ffa2586af03e09e8a052058df8ca5a51edf4c))
* handle Cloud Run traffic without percentages ([#44](https://github.com/andrewtryder/plex-cast-bridge/issues/44)) ([4798d99](https://github.com/andrewtryder/plex-cast-bridge/commit/4798d990db106662c2b848e6ec646aa768b5b601))
* implement Google Home volume controls ([#42](https://github.com/andrewtryder/plex-cast-bridge/issues/42)) ([7fe3331](https://github.com/andrewtryder/plex-cast-bridge/commit/7fe33314597e94a31e0bdc999b42bee3ddc29072))
* include configuration drift in release safety ([6531188](https://github.com/andrewtryder/plex-cast-bridge/commit/6531188fc059c2dd530e8a6abad438bd32959251))
* inspect configuration in older released images ([9204eff](https://github.com/andrewtryder/plex-cast-bridge/commit/9204eff51d0bb4ad5d70e6390e1335aa005b0263))
* integrate confirmed Cast stop ([bcc24cf](https://github.com/andrewtryder/plex-cast-bridge/commit/bcc24cf4e940a91bbbd5d9afe8a69da80b4fb506))
* make Cloud Run upgrades and OAuth exchanges retry-safe ([3023b41](https://github.com/andrewtryder/plex-cast-bridge/commit/3023b415667871965355aef9551b07ed6ce6a147))
* make OAuth code exchanges safely retryable ([c2f05bf](https://github.com/andrewtryder/plex-cast-bridge/commit/c2f05bf780ec2db5dd3a211af6c6aeb57573ff92))
* merge dashboard injection safeguards ([bf05432](https://github.com/andrewtryder/plex-cast-bridge/commit/bf0543218e06e3be7e5131ed9259cf4952d85a61))
* normalize mount order during preflight ([eea6b90](https://github.com/andrewtryder/plex-cast-bridge/commit/eea6b903c8acc3fd91e200503ccf3ed26c8f0a04))
* persist and revoke Google Home refresh tokens ([#37](https://github.com/andrewtryder/plex-cast-bridge/issues/37)) ([2dff5c6](https://github.com/andrewtryder/plex-cast-bridge/commit/2dff5c6a33c582c8d26f888cb5531b3f66c9aca6))
* persist reviewed Compose image selection ([3a6ca59](https://github.com/andrewtryder/plex-cast-bridge/commit/3a6ca590310b4586961d3707cce019f6932cd216))
* preflight Firestore account-link state ([3cbf93f](https://github.com/andrewtryder/plex-cast-bridge/commit/3cbf93f5fde0af8449c6e635355a9797111648d1))
* preserve reviewed configuration and verify backups ([f63a138](https://github.com/andrewtryder/plex-cast-bridge/commit/f63a138b02208c3832c278038f1a564338fab3e1))
* preserve traffic until cloud revision validates ([29bdfbd](https://github.com/andrewtryder/plex-cast-bridge/commit/29bdfbdf3646d52410a3365a78fadbaa77f29170))
* prevent stale concurrent OAuth exchanges ([96846ba](https://github.com/andrewtryder/plex-cast-bridge/commit/96846bacda60e99f4d90f2bdcf78de7624379352))
* protect persistent state during upgrades ([#29](https://github.com/andrewtryder/plex-cast-bridge/issues/29)) ([d77fdcf](https://github.com/andrewtryder/plex-cast-bridge/commit/d77fdcfbd97380c8a09464e7f5dbd2d0be7e20fd))
* quiesce and verify upgrade backups ([3198b53](https://github.com/andrewtryder/plex-cast-bridge/commit/3198b538fdff12236d2cb15f5e7c913461abb370))
* redact gateway key from sync responses ([#28](https://github.com/andrewtryder/plex-cast-bridge/issues/28)) ([6f06ce5](https://github.com/andrewtryder/plex-cast-bridge/commit/6f06ce59d9b2c17398710ecf8341826f8061679e))
* regenerate Cloud Run requirements export ([c9163d5](https://github.com/andrewtryder/plex-cast-bridge/commit/c9163d53cb172874b948bd3fbdbcd25c05cc6ff4))
* regenerate Cloud Run requirements export ([bf92a94](https://github.com/andrewtryder/plex-cast-bridge/commit/bf92a94fb802052ff8a9b9ddeb97991ffdcbb445))
* reject malformed upgrade drift reports ([37606dd](https://github.com/andrewtryder/plex-cast-bridge/commit/37606dd7d3e8598a79960a1f2e0c80f263f90d2c))
* **release:** enforce promotion retry failures, digest verification, and regression tests ([c01230e](https://github.com/andrewtryder/plex-cast-bridge/commit/c01230e4148c07c0d9c365211d9a96a57886b21b))
* render dashboard data without html injection ([c5abadc](https://github.com/andrewtryder/plex-cast-bridge/commit/c5abadcb29f7a197d3a07756ae00ea4a26f52107))
* require fresh Plex revision for scan skips ([#33](https://github.com/andrewtryder/plex-cast-bridge/issues/33)) ([053ca35](https://github.com/andrewtryder/plex-cast-bridge/commit/053ca35e77743b3e75717c2e80597a3fc7196b32))
* require immutable Cloud Run secret versions ([52480f6](https://github.com/andrewtryder/plex-cast-bridge/commit/52480f6e9d8017c3212abf589abbe78f35984228))
* require reviewed Cloud Run preflight baseline ([a7df458](https://github.com/andrewtryder/plex-cast-bridge/commit/a7df4580fba51f86ada697aac2af56a2f52c8194))
* require valid drift evidence in release checks ([5bdbba3](https://github.com/andrewtryder/plex-cast-bridge/commit/5bdbba37e77865bbdc7ca67923b190086353ffe6))
* reset cloud catalog store per test client ([7792414](https://github.com/andrewtryder/plex-cast-bridge/commit/7792414cbaf41971140bbb48b5ff00433c430ab6))
* reset cloud catalog store per test client ([947ef14](https://github.com/andrewtryder/plex-cast-bridge/commit/947ef14c5c8e140be98879d11de24578078334ad))
* reset cloud catalog store per test client ([0ea7b20](https://github.com/andrewtryder/plex-cast-bridge/commit/0ea7b207d8a18f3293a699c5877b1657532fa00e))
* reset cloud catalog store per test client ([fb6d042](https://github.com/andrewtryder/plex-cast-bridge/commit/fb6d042b0bc84c76f3489bedc5e79c3852f5579d))
* restrict diagnostic errors and cors headers ([f7105d0](https://github.com/andrewtryder/plex-cast-bridge/commit/f7105d00e1005c00146f90352e73d5e6bcac826d))
* retry Cast group connections on port 8009 ([#36](https://github.com/andrewtryder/plex-cast-bridge/issues/36)) ([c69e67e](https://github.com/andrewtryder/plex-cast-bridge/commit/c69e67e45451c6ac52c51b52b3b3fbba341cd5a3))
* retry transient OAuth Firestore initialization ([34f256c](https://github.com/andrewtryder/plex-cast-bridge/commit/34f256c06d408030aa983e6d0621221eeb745c53))
* route traffic to ready Cloud Run upgrade ([d4c8b86](https://github.com/andrewtryder/plex-cast-bridge/commit/d4c8b86afce03de374f97fabb9e6a0a2a8740243))
* secure Proxmox upgrade backups ([#34](https://github.com/andrewtryder/plex-cast-bridge/issues/34)) ([a80ede0](https://github.com/andrewtryder/plex-cast-bridge/commit/a80ede0ae68ff2f140971ab17372777e1b7e6eb4))
* serialize Cast device discovery ([#38](https://github.com/andrewtryder/plex-cast-bridge/issues/38)) ([d8cb70b](https://github.com/andrewtryder/plex-cast-bridge/commit/d8cb70b5ce477d17f904eef238adacb62f1fd2a4))
* tolerate untitled Plex tracks in index scan ([#35](https://github.com/andrewtryder/plex-cast-bridge/issues/35)) ([ba9093f](https://github.com/andrewtryder/plex-cast-bridge/commit/ba9093f0a584071b86f799d35b221cb0c4dedd51))
* treat unreadable state as unverified ([89d69b1](https://github.com/andrewtryder/plex-cast-bridge/commit/89d69b14e642a955a42b775ed5e7c0d4607e2da3))
* **upgrade:** enforce persistent mounts, Portainer variable forwarding, and safe rollback ([21486ac](https://github.com/andrewtryder/plex-cast-bridge/commit/21486ac04969baacd911584f70ab42b07f1358f3))
* verify backup integrity and persistent identity ([491b100](https://github.com/andrewtryder/plex-cast-bridge/commit/491b100952ddfeb958be80c93fbefc0c168d5c86))
* verify legacy Plex environment migration safely ([ba2bc92](https://github.com/andrewtryder/plex-cast-bridge/commit/ba2bc925debe1c992fe04aae9b3b759a547222ac))
* verify quiesced state against reviewed plan ([70e9c02](https://github.com/andrewtryder/plex-cast-bridge/commit/70e9c02b722b9fc43639c775314049524d8c76f2))


### Documentation

* clarify Cloud Run preflight and traffic staging ([b519cfe](https://github.com/andrewtryder/plex-cast-bridge/commit/b519cfeac3cd8e4b872c43c974af70ffbf62e4b8))
* clarify offline restore limitations ([ee866da](https://github.com/andrewtryder/plex-cast-bridge/commit/ee866dacc964b213bc3601bc375b856069fb4471))
* clarify upgrade archive and manual music checks ([920159c](https://github.com/andrewtryder/plex-cast-bridge/commit/920159c1dcb7a8671336c81e44d0d119d1a9d6c7))
* describe verified release publication ([10c2125](https://github.com/andrewtryder/plex-cast-bridge/commit/10c212519c91eab57fbb3a4dd97e0cb22d5a14f4))
* document Python 3.14 migration requirements for native installs ([a36e5e0](https://github.com/andrewtryder/plex-cast-bridge/commit/a36e5e0d9e7a94f574e57cd95ef4e38d9cdbe0bd))
* drop Grok Bot tutorial link from enhancement report ([9642fe9](https://github.com/andrewtryder/plex-cast-bridge/commit/9642fe913af7b0813b540df3a27bdd4162011900))
* drop Grok Bot tutorial link from enhancement report ([fa4e9df](https://github.com/andrewtryder/plex-cast-bridge/commit/fa4e9df7930a9377a627b1b76525fb4d18f5654e))
* **release:** distinguish automated preflight checks from live production acceptance ([73abb90](https://github.com/andrewtryder/plex-cast-bridge/commit/73abb90cd557876de733a903932d620e5d693625))
* **release:** document v0.5.0 preflight acceptance matrix across all deployment targets ([20d46a6](https://github.com/andrewtryder/plex-cast-bridge/commit/20d46a6c170069c8dc984ed3e3a9ca6aa50db9b0))
* **release:** document v0.5.0 preflight acceptance matrix across all deployment targets ([5e314b5](https://github.com/andrewtryder/plex-cast-bridge/commit/5e314b59ba4fa8499de3b4d1764819cb2baaef1e))
* retire obsolete Google Home rebuild guidance ([#25](https://github.com/andrewtryder/plex-cast-bridge/issues/25)) ([d6c0cd2](https://github.com/andrewtryder/plex-cast-bridge/commit/d6c0cd2a9e4ea2fb3d9172d0168f0eee2b0fe233))
* verify first OAuth refresh migration ([7c43bc9](https://github.com/andrewtryder/plex-cast-bridge/commit/7c43bc9b60098d9124807c86c411d45d8f504a34))

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
