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
2. checks whether an existing Git tag or GHCR versioned image already exists (refusing by default unless `allow_recovery` is enabled);
3. builds and pushes a candidate multi-architecture GHCR image (`candidate-VERSION`);
4. resolves the immutable digest and requires `linux/amd64` and `linux/arm64` manifests;
5. requires OCI version and revision labels for both platforms;
6. publishes verified version and major/minor tags (`VERSION`, `MAJOR.MINOR`) from the verified digest;
7. creates `vVERSION` and the GitHub release only after those checks pass;
8. promotes that verified digest to `latest`.

The older Docker workflow is a manual build diagnostic only; it cannot publish
GHCR tags. Do not restore automatic `main` or tag publishing, because that can
move `latest` to an unreviewed commit or bypass these checks.

## Partial-publication recovery procedure

If a publication workflow run fails partway through, the release process is fully
recoverable without risk of releasing unverified code or overwriting unrelated commits:

1. **Candidate image verification failure (before stable tags exist)**:
   No public image, Git tag, or GitHub release was created. Fix the failure on `main`
   and dispatch **Publish Verified Release** normally.
2. **Versioned GHCR image exists but Git tag creation failed**:
   Dispatch with `allow_recovery: true`. The workflow verifies that the existing GHCR
   image contains both required platforms, the expected version, and the exact commit
   revision, skips image rebuild, creates the missing Git tag and GitHub release, and
   promotes `latest`.
3. **Git tag exists but GitHub release creation failed**:
   Dispatch with `allow_recovery: true`. The workflow validates that the existing Git
   tag resolves to the exact reviewed commit on `main`, verifies the GHCR image, creates
   the missing GitHub release, and promotes `latest`.
4. **GitHub release exists but `latest` promotion failed**:
   Dispatch with `allow_recovery: true`. The workflow re-verifies the Git tag and image
   integrity, skips release creation, and promotes the verified digest to `latest`.
5. **Mismatched artifact protection**:
   If an existing Git tag points to a different commit, or an existing GHCR image has a
   mismatched OCI revision, version label, or platforms, the workflow **hard fails**
   and refuses to proceed. It never silently overwrites an existing published release.

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
