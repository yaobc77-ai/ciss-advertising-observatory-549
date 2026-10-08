# Repository collaboration

## GitHub synchronization and teammate changes

- Before editing or preparing an upload, fetch the configured remote, inspect its current default branch and relevant teammate branches/PRs, and compare their commits with the local checkout. A teammate change that has not been uploaded cannot be synchronized; record that boundary.
- Preserve a dirty/shared checkout. Integrate in a clean, isolated worktree based on the fetched remote; transfer only reviewed paths or patches with recorded provenance. Do not reset, clean, blanket-stage, or overwrite collaborators' files.
- Apply local changes onto the current remote version. Review overlapping UI changes hunk by hunk; do not resolve conflicts by replacing entire files with either side. Keep uncommitted and active peer work outside the upload unless explicitly handed off.
- Validate the exact candidate in its isolated checkout. Fetch again immediately before pushing, reconcile any new remote commits, and repeat affected checks. Never force-push shared branches.
- Upload to a dedicated branch and link a PR for review unless the user explicitly requests merging/publishing to the default branch. Verify the remote commit, PR diff and CI after upload; uploading a branch is not an application deployment.
- Follow [Git collaboration](docs/git_collaboration.md). These rules do not create a scheduled automation or authorize paid model calls.
