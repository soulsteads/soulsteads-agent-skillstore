---
name: manage-skills
description: Discover and selectively install reviewed portable skills from a configured Soulsteads Skillstore; inspect provenance and updates, preserve local edits, and recover prior versions.
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

Do not overwrite local edits to get an update through. Preserve them in a copy
or submit an improvement proposal with `propose NAME PATH --base-version VERSION
--rationale TEXT`. Proposals require an independent authorized reviewer before
publication; MCP deliberately has no review or publication tool.

Before a session uses a managed target, register it with
`skillstore session-begin LABEL --target DIR`; retain the returned session ID.
End it with `session-end ID --target DIR` only after the session has stopped.
Registered sessions block installation, rollback and removal. The store cannot detect unregistered harness
sessions, so stop those before changing their skill directory.

Recover using `skillstore_rollback` or `skillstore rollback NAME VERSION
--target DIR`. Recovery verifies the cached version and still refuses local
changes. Use `skillstore_uninstall` or `skillstore uninstall NAME --target DIR`
only when removal is intended. It removes the active copy, retains the prior
versions offline and refuses unowned or edited skills. Use rollback to restore
a removed version. `status --target DIR` shows receipts and sessions. A failed
mutation may have committed; inspect status before retrying. Installation
recovery runs on the next target operation, and registry metadata recovery runs
on opening the store or taking its next write lock.
