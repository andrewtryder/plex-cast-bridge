# Verified release process

This is the maintainer process for a public Plex Cast Bridge release. It does
not replace the local-upgrade guide: users should follow
[upgrading.md](upgrading.md), while maintainers must complete the gates below
before announcing a release.

## Candidate gates

1. Merge only reviewed changes and let Release Please update its release PR.
   Release Please prepares the version and changelog, but does **not** create a
   tag or GitHub release.
2. Run the full Linux CI matrix and the disposable Docker upgrade suite on the
   exact reviewed commit. A Docker Desktop result of **NOT TESTED** is not a
   substitute for Linux Docker acceptance.
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

1. builds and pushes only the versioned and major/minor GHCR tags;
2. resolves the immutable digest and requires `linux/amd64` and `linux/arm64`;
3. requires OCI version and revision labels for both platforms;
4. creates `vVERSION` and the GitHub release only after those checks pass;
5. promotes that verified digest to `latest`.

If image build or verification fails, do not create a substitute GitHub release
and do not tell users to use `latest`. Preserve the workflow logs and fix the
publication failure before retrying with a new reviewed release process.

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
