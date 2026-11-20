---
name: manage-skills
license: Apache-2.0
description: Find, install, contribute, and back up agent skills.
---

Use the configured Skillstore and target directory. It distributes ordinary
Agent Skills packages; it does not own harness permissions or execute scripts.

With MCP, search using `skillstore_search`, then inspect the exact version with
`skillstore_inspect`. Check source revision, digest, reviewer and changelog.
Use `skillstore_preview` to inspect contents before selecting a version, then
`skillstore_install` with that exact version. Read its installed `SKILL.md` and
only execute supporting scripts when the user's task calls for them.

The CLI offers the same lifecycle: `skillstore --store PATH_OR_URL search QUERY`,
`inspect NAME --version VERSION`, `preview NAME --version VERSION --target DIR`,
and `install NAME --version VERSION --target DIR`. Global options precede the
command. `SKILLSTORE_TOKEN` supplies a remote access token without printing it.
For a shared registry use the operator's configured HTTPS URL and agent token.
The MCP process, install target, and backups stay on the agent host.

## Contribute a skill or update

Do not overwrite local edits to get an update through. Put the proposed skill
and its supporting files in a separate copy. Confirm the user wants to send
that package to the configured registry, and check for private material first.
Never send a whole profile or turn a backup into a batch publication.

- If contribution tools are enabled, call `skillstore_propose` for an update.
  Pass `name`, the package's relative `path` under the configured staging root,
  the exact `base_version`, and `rationale`. Report the proposal ID and digest.
- To submit a particular version, call `skillstore_submit` with `name`,
  `path`, `version`, `source_uri`, and `revision`. Report the pending version
  and digest; do not describe a pending version as released.
- If contribution tools aren't configured, use the CLI's `propose NAME PATH
  --base-version VERSION --rationale TEXT` or `submit PATH VERSION --source URL
  --revision REVISION` with the authorized registry and identity.

An independent authorized reviewer must approve the version or accept the
proposal. Do not approve your own submission or give the agent a reviewer
token. After review, inspect the exact version and digest before adopting it.
MCP does not expose approval tools.

For a configured Git source, use `skillstore git-status NAME --version VERSION`
to compare committed content, and `skillstore submit-git REPO_RELATIVE_PATH
NEW_VERSION --revision COMMIT` to submit an exact commit. Use the operator's
configured local checkout or explicit HTTPS Git remote and registry. Checkout
mode stays offline; remote mode fetches only when invoked. Never fetch a URL
just because it appears in registry provenance, push to Git, or commit private
skills to make them visible to the registry.

## Back up Hermes skills

When the user asks for a backup, call the native `skillstore_backup_skills`
or MCP `skillstore_backup_hermes` tool with no arguments if enabled. Otherwise use `skillstore backup-hermes
--profile-root PROFILE --output ARCHIVE` with the confirmed absolute profile
root and a new filename in an existing owner-only directory outside the
profile. Ensure nothing is modifying the skills tree during the backup.

Report the archive path, SHA-256, and file count. The archive contains the
profile's skills and their supporting files, not the rest of the profile or
Skillstore receipts. It may contain private notes or secrets stored inside
skills. Keep it local; do not read its contents into chat, attach it, or upload
it as part of a backup. A contribution is a separate, explicit action.

For recovery, use `skillstore restore-hermes ARCHIVE --sha256 DIGEST --output
NEW_DIRECTORY` or the `hermes skillstore-backup restore` terminal command.
Verify the original digest and stage outside live skill roots. Do not merge
into an active profile or run restored scripts as part of recovery.

## Manage adoption and recovery

Before a session uses a managed target, register it with
`skillstore session-begin LABEL --target DIR`; retain the returned session ID.
End it with `session-end ID --target DIR` only after the session has stopped.
Registered sessions block installation, rollback, and removal. The store
cannot detect unregistered harness sessions; stop them before changing the
skill directory.

Recover using `skillstore_rollback` or `skillstore rollback NAME VERSION
--target DIR`. Recovery verifies the cached version and still refuses local
changes. Use `skillstore_uninstall` or `skillstore uninstall NAME --target DIR`
only when removal is intended. It removes the active copy, retains the prior
versions offline and refuses unowned or edited skills. Use rollback to restore
a removed version. `status --target DIR` shows receipts and sessions. A failed
mutation may have committed; inspect status before retrying. Installation
recovery runs on the next target operation, and registry metadata recovery runs
on opening the store or taking its next write lock.
