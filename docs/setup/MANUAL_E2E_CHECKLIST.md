# Manual end-to-end checklist (Proxmox / Google Home)

Automated tests use mocks. Before calling a personal deploy “done”, walk this list on the real LAN.

## Local bridge

- [ ] Docker / LXC container healthy (`/health`)
- [ ] Settings → Connect with Plex succeeds and survives restart
- [ ] Cast discovery lists rooms; enable at least one speaker
- [ ] Player transport + Smart DJ playback of a real track
- [ ] Settings → Playback volume save creates `data/app_settings.json`
- [ ] Cover art loads via `/api/artwork` (no Plex token in browser URLs)
- [ ] Queue remove / clear behave as expected

## Optional Google Home

- [ ] Companion started on Mac; pairing token entered in wizard
- [ ] Provision preview shown; billing account selected explicitly
- [ ] Cloud Run health returns ok (or degraded with clear catalog_storage error)
- [ ] OAuth link with correct client secret; wrong secret rejected
- [ ] Catalog upload after link; Request Sync succeeds
- [ ] Voice command reaches Pub/Sub worker and plays
- [ ] Google Home DISCONNECT stops Request Sync; upload does not silently relink
- [ ] Service restart recovers Plex JWT + speakers without re-auth

## Backup / upgrade

- [ ] `/api/backup` excludes tokens
- [ ] Restore preview rejects path traversal; restore requires confirm
- [ ] `scripts/proxmox_upgrade.sh` records previous image; failed health attempts rollback
