# Working with shared GitHub changes

Keep the remote version and local work visible before editing, and preserve teammates' changes when publishing.

## Start with the remote

1. Read the shared coordination log and current file claims.
2. Run `git status --short`, `git fetch origin`, and inspect the default branch plus relevant teammate branches and pull requests.
3. Compare local and remote commits. If a teammate has not uploaded a change, record it as unavailable to synchronize.
4. Use a clean worktree based on the fetched remote when the working directory contains unfinished work. Preserve that directory and its running services.

## Integrate reviewed changes

- Record the source commit, selected paths and file hashes before transferring a patch.
- Include dependencies and independently reviewable tests. Keep secrets, private datasets, protected evaluation references and active peer work out of the upload.
- Review both sides of overlapping changes. Preserve teammate UI behavior and styling; avoid replacing a remote file with an old local copy.
- Use explicit paths for staging and committing. Do not use blanket staging, destructive resets or shared-branch force pushes.
- Run the project's complete permitted offline checks from the integration checkout. Test results belong to that exact candidate, not all files on the machine.

## Upload and verify

Fetch again before upload. If the remote has advanced, integrate the new commits and repeat the relevant verification. Push a dedicated branch and open a pull request for review. Merging or deploying to the default branch requires the applicable user instruction and review process.

Verify the pushed SHA, pull-request diff and CI result. Keep UI assets unchanged unless they are an intentional part of the reviewed patch. A successful branch upload does not prove that a running application has been redeployed.

## Task tracking

Use **Backlog → In Progress → Ready for Review → Ready for Merge → Done**. Link the same task to its Notion card, GitHub issue or PR, and deliverable. Review is assigned to Wilson or the TPM; management confirms Done. Leave unassigned people and unconfirmed dates blank.

For documents without code to merge, record merge as not applicable and obtain review and management confirmation. Do not infer approval from an existing commit or a passing check.
