# Operator guide

Start with the [quickstart](../README.md#quickstart) for a disposable local
registry and a first install. For tested versions, see [support](support.md).

## Shared registries

Directory access is a trusted local administration boundary: `--actor` is a
declared identity, not authentication. For clients that must not impersonate a
reviewer, keep the store directory inaccessible and use the HTTP server with
separate tokens. The server derives actor and kind from the token, not from
client-supplied identity fields. Unknown request fields are rejected. The
example assumes you have created `./registry` using the quickstart.

```sh
skillstore --store ./registry --kind agent token agent --token-file ./agent.token
skillstore --store ./registry serve --host 127.0.0.1 --port 8765 --private-read
```

In a second terminal, from the same directory, pass the token file's contents
to this command only. Run this with shell tracing off (`set +x`):

```sh
SKILLSTORE_TOKEN="$(cat ./agent.token)" \
  skillstore --store http://127.0.0.1:8765 search
```

Tokens are written only to newly created mode-0600 files. Registry storage keeps
hashes. Stop the demo server with Ctrl-C, then run
`skillstore --store ./registry revoke agent` to revoke that actor's tokens.
Use a protected TLS reverse
proxy for remote clients; the built-in server supplies no TLS. Do not expose a
registry publicly by accident. Anonymous reads are allowed unless `--private-read`
is supplied, including inspection of pending candidates and proposals.

## MCP

For MCP, launch `skillstore --store PATH_OR_URL mcp --target SKILLS_DIRECTORY`
as a stdio server using your harness's supported native configuration. It offers
search, inspection, preview, install, rollback, uninstall, and status. It has
no publication or review tools. The bundled [operating skill](../skills/manage-skills/SKILL.md)
describes this workflow. Neither configuring MCP nor copying a skill changes
native execution permissions. Start fresh harness sessions after adoption. The
MCP server uses the same registry and target rules as the CLI. Set `command`
to the path of the installed `skillstore` executable and pass the arguments
`--store`, `PATH_OR_URL`, `mcp`, `--target`, `SKILLS_DIRECTORY` in that order
in its native configuration. Use absolute paths for local stores and targets,
and pass remote tokens through a private environment rather than arguments.

## Native targets

Use `target` to print a JSON plan with the harness, scope, absolute root,
absolute target, and `managed_install` flag. Supply an existing directory
other than `/`. Named paths reject `..`, symlinks, and non-directory path
components. Planning leaves native configuration untouched and works without
profile lookup, store access, or directory creation.

```sh
skillstore target --harness codex --scope project --root .
skillstore --store ./registry install demo-skill --version 1.0.0 \
  --harness codex --scope project --root .
```

| Harness ID | Scope | Supply as `--root` | Directory under root | `managed_install` |
| --- | --- | --- | --- | --- |
| `codex` | project | Project directory | `.agents/skills` | true |
| `codex` | profile | OS user home | `.agents/skills` | true |
| `claude-code` | project | Project directory | `.claude/skills` | true |
| `claude-code` | profile | OS user home (default config layout) | `.claude/skills` | true |
| `hermes` | profile | Explicit `HERMES_HOME` | `skills` | true |
| `openclaw` | project | Explicit workspace directory | `skills` | true |
| `openclaw` | profile | Explicit `OPENCLAW_STATE_DIR` | `skills` | true |
| `opencode` | project | Project directory | `.opencode/skills` | true |
| `opencode` | profile | Exact XDG `opencode` config directory | `skills` | true |
| `pi` | project | Project directory | `.agents/skills` | true |
| `pi` | profile | Explicit Pi agent directory, e.g. `~/.pi/agent` | `skills` | true |
| `soulsteads` | profile | Exact tracked Agent Home directory | `skills` | false |

Managed targets accept the named arguments on `install`, `preview`, `status`,
`rollback`, `uninstall`, `migrate-state`, `session-begin`, `session-end`, and
`mcp`. Named, explicit, and MCP operations for the same path share the same
receipts and lock. Advisory entries give the intended path but do not enable
the managed lifecycle. See [tested runtime versions](support.md#native-discovery).

Explicit `--target` remains available for staging or operator-configured
roots. Verify the runtime scans only the intended root and not its sibling state.

## Tracked Agent Homes

Do not use generic target management to bypass ownership in a tracked Agent
Home. Stage the reviewed package outside the Home and import its skill directory.

```sh
skillstore --store ./registry install my-skill --version 1.0.0 --target STAGING
soulsteads skill install AGENT STAGING/my-skill
```

Import only the skill directory and leave Skillstore's state beside staging. For the
native Soulsteads Harness or an unlisted harness, use an operator-configured
`--target` with a compatible loader or stage the package for native import.
Revalidate native discovery and behavior before adoption.

## Updates, proposals, and sessions

Submit a new version, have a different authorized human reviewer approve it,
then preview and install it selectively. `install` targets one exact version;
there is no automatic update service. It refuses to overwrite unowned
directories or local edits. Keep edits in a separate copy before restoring the
installed version to its original bytes or submitting a proposal.
`diff NAME OLD NEW` shows release changes. `propose NAME DIRECTORY
--base-version VERSION --rationale TEXT` creates
an unpublishable proposal. Review its `proposal-diff ID`; an authorized reviewer
uses `decide ID accept --version NEW_VERSION --note TEXT` to publish, or `reject`
to retain the rejected proposal without releasing it. Agent identities cannot
approve proposals or versions. `withdraw NAME VERSION --reason TEXT` prevents
new installation of a reviewed version while retaining evidence.

`session-begin LABEL --target DIR` registers use of a target and returns an ID.
Install, rollback, removal, and state migration are refused while any registered
session remains active.
Use `session-end ID --target DIR` after it exits. The registry cannot detect
unregistered sessions: stop them before changing their skill directory. A stale
registration is deliberately retained after process loss; inspect `status` and
end it explicitly only when its consumer is stopped.

## Recovery and backup

`rollback NAME VERSION --target DIR` restores an earlier local snapshot, including
its recorded provenance, without contacting the registry. Local edits still
block replacement: preserve them first. History is retained, including the bad
version, for inspection. A rollback may intentionally recover a version later
withdrawn upstream; the offline receipt is historical evidence, not current approval.

`uninstall NAME --target DIR` removes a clean, owned skill and keeps the last
version and prior history. `status` shows it under `removed`, and `rollback`
can restore any retained version without contacting the registry. Removal
refuses unowned files, local edits and active registrations.

Interrupted install/update/removal operations retain `transaction.json` in the
state directory and any staged or previous trees. The next target operation
verifies digests and completes that transaction. It refuses conflicting edits
instead of deleting them. Do not manually delete the metadata or backups.
Registry review/publication metadata uses a write-ahead journal and idempotent
audit events. Reopening the store completes an interrupted metadata transaction.
Any failed mutation may have taken effect: inspect before retrying.

`status` does not initialize an unused target. On an existing managed target it
can complete an interrupted transaction. Stop clients and the registry server
before copying the registry, the entire target, and its sibling state directory
(`.skillstore-<target basename>`) to a private backup. For a legacy target include
its internal `.skillstore`. Restore the corresponding directories together to
empty locations, preserving their relative names and private state permissions.
Run `skillstore --store RESTORED verify` and `skillstore status --target RESTORED_TARGET`
and inspect sessions before resuming. State and skills must be on the same
filesystem. Hash chains detect accidental edits; they are not signatures or
protection against an administrator replacing the whole store.

### Migrating legacy targets

Stop the harness and all Skillstore clients (including long-running MCP
servers). Back up the target first. Inspect with explicit `status --target DIR`
to finish any pending transaction, then end old session registrations only
after their consumers stop. Run `skillstore migrate-state --target DIR`, then
check `status` again. The command moves state and its lock as one directory on
the same filesystem; it leaves the installed skills and retained receipt bytes
unchanged. If it reports an I/O failure, inspect and retry the same command;
do not move or merge state by hand. Both placements being present is a conflict.
Do not use an older Skillstore executable to access external state. New named
targets refuse legacy state until migration (the existing Codex project target
also accepts legacy state for compatibility).
