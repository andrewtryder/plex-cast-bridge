# Verified release process

This is the maintainer process for a public Plex Cast Bridge release. It does
not replace the local-upgrade guide: users should follow
[upgrading.md](upgrading.md), while maintainers must complete the gates below
before announcing a release.

## Candidate gates

1. Merge only reviewed changes and let Release Please update its release PR.
   Release Please prepares the version and changelog, but does **not** create a
   tag or GitHub release.
2. Run the full Linux CI suite (Python 3.14) and the disposable Docker upgrade
   suite on the exact reviewed commit. A Docker Desktop result of **NOT TESTED**
   is not a substitute for Linux Docker acceptance.
3. Collect private local-upgrade evidence using `release_acceptance.py` and
   confirm the before/after comparison, backup, image-only rollback, and
   Compose recreation checks. A real Proxmox Docker-in-LXC test remains a
   separate operator-approved gate.
4. Complete the separate Cloud Run acceptance plan when Cloud Run code is in
   the release. Do not deploy, relink users, rotate secrets, or run first-time
   provisioning as part of a local Docker release.

## Publish after approval

After the reviewed release commit is on `main`, dispatch **Publish Verified
Release** from GitHub Actions with the numeric semver version and the reviewed
commit or `main` ref. The workflow:

1. verifies that the selected revision is merged into `main` and matches the release version;
2. refuses an existing tag or versioned GHCR image (unless recovering via `promote_latest_only`);
3. builds and pushes a candidate multi-architecture GHCR image (`candidate-VERSION`);
4. resolves the immutable digest and requires `linux/amd64` and `linux/arm64` manifests;
5. requires OCI version and revision labels for both platforms;
6. publishes verified version and major/minor tags (`VERSION`, `MAJOR.MINOR`) from the verified digest;
7. creates `vVERSION` and the GitHub release only after those checks pass;
8. promotes that verified digest to `latest`.

The older Docker workflow is a manual build diagnostic only; it cannot publish
GHCR tags. Do not restore automatic `main` or tag publishing, because that can
move `latest` to an unreviewed commit or bypass these checks.

If image build or verification fails, no versioned image, Git tag, or GitHub
release is created or published. Users never see unverified candidate images,
and the workflow can be safely retried once the failure is resolved.

If GitHub release creation succeeds but the subsequent `latest` promotion step
fails, the workflow supports recovery: dispatch **Publish Verified Release**
with `promote_latest_only` enabled. It re-verifies the published versioned
image and commit ancestry before updating `latest`.

## Historical artifact limitation

GitHub release `v0.2.2` has no resolvable matching GHCR version tag. It cannot
serve as upgrade evidence or an upgrade target. This happened because its tag
was created by Release Please with `GITHUB_TOKEN`, which did not trigger the
separate tag-push Docker workflow. The verified workflow above avoids that
dependency.

## Post-publication record

Record the GitHub release URL, exact image digest, both platform manifests,
OCI revision label, CI run URLs, and private local/Cloud Run acceptance report
references. Do not mark the release ready until all required entries are
available.
