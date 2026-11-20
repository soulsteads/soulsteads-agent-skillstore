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

### Connect from another machine

Run `skillstore serve --private-read` bound to loopback on the registry host,
behind an access-restricted TLS reverse proxy. Forward `POST /v1/rpc` and its
`Authorization` header to the server. The built-in server does not supply TLS;
do not expose its raw port to other hosts. Put only the agent's token in the
client's private `SKILLSTORE_TOKEN` environment. Then use:

```sh
skillstore --store https://skills.example.org search
```

Use the hostname on the proxy's TLS certificate. If it is signed by a private
CA, add that CA to the agent host's trust store or set `SSL_CERT_FILE` to an
absolute path to a trusted PEM bundle. Do not bypass certificate checks or
switch to plain HTTP to resolve trust errors.

Omitting `--private-read` permits anonymous inspection, including pending
candidates and proposals. Keep separate tokens for agents and reviewers.

## MCP

For MCP, launch `skillstore --store PATH_OR_URL mcp --target SKILLS_DIRECTORY`
as a stdio server using your harness's supported native configuration. It offers
search, inspection, preview, install, rollback, uninstall, and status by default.
The bundled [operating skill](../skills/manage-skills/SKILL.md)
describes this workflow. Neither configuring MCP nor copying a skill changes
native execution permissions. Start fresh harness sessions after adoption. The
MCP server uses the same registry and target rules as the CLI. Set `command`
to the path of the installed `skillstore` executable and pass the arguments
`--store`, `PATH_OR_URL`, `mcp`, `--target`, `SKILLS_DIRECTORY` in that order
in its native configuration. Use absolute paths for local stores and targets,
and pass remote tokens through a private environment rather than arguments.

### Connect directly over Streamable HTTP

Run a dedicated, read-only MCP listener on the registry host. This is a native
MCP endpoint; the client does not need a local `skillstore` subprocess to
search and inspect reviewed releases.

```sh
skillstore --store ./registry --kind agent token catalog-agent --token-file ./catalog-agent.token
skillstore --store ./registry serve-mcp --port 8766
```

The listener binds to `127.0.0.1` and requires a valid registry bearer token
on every request, including tool discovery. It does not serve `/v1/rpc`.
For another machine, put it behind a trusted, access-restricted HTTPS reverse
proxy. Route `/mcp` to `http://127.0.0.1:8766/mcp`, set the upstream `Host`
header to `127.0.0.1:8766`, and preserve `Authorization`, `Origin` (if sent),
`Accept`, and `MCP-Protocol-Version`. Do not strip `Origin` to enable browser
requests: this endpoint is for native clients and rejects requests with an
`Origin` header. Disable caching and apply request limits at the proxy. Do
not expose the raw HTTP port or proxy the separate registry RPC as part of
this setup. Use a distinct non-reviewer token per client; registry tokens
are shared credentials if you also run the registry RPC server.

In Hermes, put the token in the profile's private `.env` as
`SKILLSTORE_MCP_TOKEN` and add this entry to `config.yaml`:

```yaml
mcp_servers:
  skillstore_catalog:
    url: https://skills.example.org/mcp
    headers:
      Authorization: "Bearer ${SKILLSTORE_MCP_TOKEN}"
```

Use `hermes mcp test skillstore_catalog` to check discovery, then call
`skillstore_search` and `skillstore_inspect` through the connection. Other
harnesses need native Streamable HTTP and support for bearer headers. This
endpoint returns JSON responses without SSE streams, browser CORS, or OAuth
discovery. Use a trusted certificate on the proxy. Revoke the client with
`skillstore --store ./registry revoke catalog-agent`; new requests using
that actor's token will be denied without restarting the listener.

The network endpoint does not expose drafts, submissions, approvals,
installs, local targets, or private tracking snapshots. Use stdio on the
agent's host for installation, contributions, and local backups. With
network MCP, omitting `version` from `skillstore_inspect` returns the latest
reviewed release; it does not list draft or withdrawn versions.

### Hermes with a remote registry and local backups

On the **agent host**, install `skillstore` and select the active Hermes
profile. Create an owner-only backup directory (mode `0700`) outside that
profile. Use existing, absolute, non-symlink paths. Put the agent's registry
token under `SKILLSTORE_TOKEN` in the profile's private `.env`, then set this
in the profile's `config.yaml`, replacing the example paths and URL:

```yaml
mcp_servers:
  skillstore:
    command: /opt/skillstore/bin/skillstore
    args:
      - --store
      - https://skills.example.org
      - mcp
      - --harness
      - hermes
      - --scope
      - profile
      - --root
      - /path/to/hermes-profile
      - --backup-dir
      - /path/to/private-backups
    env:
      SKILLSTORE_TOKEN: "${SKILLSTORE_TOKEN}"
```

Run `hermes mcp test skillstore` in that profile to check discovery. The URL is
the registry base URL, **not** a remote `/mcp` endpoint. The MCP process runs
locally over stdio; it searches and downloads from the remote registry but
installs into the agent's local skill directory.
If the child process needs a separate private-CA bundle, list `SSL_CERT_FILE`
under the same `env` mapping with its absolute path. Follow discovery with an
actual search; listing MCP tools does not test registry credentials or TLS.

Once configured, ask Hermes to back up its skills. The opt-in
`skillstore_backup_hermes` tool takes no paths, saves a new archive in the
configured directory, and returns its local path, SHA-256, and file count.
Omit `--backup-dir` to leave the tool disabled. Backups do not contact the
registry, even when it is configured for the other tools. If you also enable
contributions, their staging directory must not overlap the backup directory.

### Contribute a skill through MCP

Add `--source-root` and an existing, absolute staging directory to the MCP
server arguments to enable `skillstore_submit` and `skillstore_propose`. This
works with the same stdio transport, including Hermes. Give this server a
separate agent token when using a shared registry; configure the registry
with `--private-read` if drafts must not be visible anonymously.

1. Put a copy of the intended skill and its supporting files in a dedicated
   staging directory, separate from your live skill root. Check its contents
   for private material. Do not use your home, whole profile, or backup
   directory as the source root.
2. To submit a new version, call `skillstore_submit` with `name`, `path`,
   `version`, `source_uri`, and `revision`. The source URI is a credential-free
   HTTPS URL; the revision identifies the source and is recorded, not fetched.
   To propose a change to an existing release, use `skillstore_propose` with
   `name`, `path`, `base_version`, and `rationale`. In both cases `path` is the
   relative directory of **one** skill, for example `writing/demo-skill`.
   Hidden entries, symlinks, hard links, and special files are refused.
3. Review the pending version or proposal separately. The authorized reviewer
   uses `review` or `proposal-diff` and `decide` through the CLI. Only then
   does the version become installable. MCP never exposes review, approval,
   or withdrawal tools, even if it is started with a reviewer token.

With a directory store, the local process is still trusted. MCP records a
declared `agent` identity (`--actor`, default `mcp-contributor`); it isn't a
security boundary against a process that can write the store directory.

## Tracking-only mode

Use `skillstore tracking` when you want private history and recovery, not a
shared catalog. Tracking does not use a registry, reviewers, Git, or the
network. It can save unpublished skills, local edits, and supporting files.
It never publishes snapshots or makes them eligible for a registry install.
Publishing to a registry still requires review.

Make a dedicated owner-only snapshot directory and a separate private parent
for recoveries. Use absolute paths and point `--skills-root` at the **skills
directory itself**, not the containing profile, home, or project. For Hermes
that is `$HERMES_HOME/skills`; for other harnesses use the actual skills
directory, for example a project's `.agents/skills`. Stop writers to the
skills tree while backing it up.

```sh
mkdir -m 700 "$HOME/skill-snapshots" "$HOME/skill-recoveries"
skillstore tracking --directory "$HOME/skill-snapshots" backup --skills-root "$HERMES_HOME/skills"
skillstore tracking --directory "$HOME/skill-snapshots" list
# Set SNAPSHOT to the snapshot ID printed by backup or list.
skillstore tracking --directory "$HOME/skill-snapshots" restore "$SNAPSHOT" \
  --output "$HOME/skill-recoveries/recovered-skills"
```

Each backup makes a new owner-only archive. Keep one snapshot directory per
skills tree so their histories stay separate. `list` shows IDs, timestamps,
recorded digests and archive sizes, newest first; it does not verify archive
contents. Restore checks the archive against the SHA-256 in the selected ID
and writes a new `recovered-skills/skills` tree. It does not merge into a live
profile or overwrite an existing directory. Inspect the recovered files before
moving them into use. Preserve the original snapshot ID if you copy an archive
to another private location; keep its filename unchanged.

Only ordinary files and directories in the selected tree are backed up.
Legacy `.skillstore` state at the root is excluded and unsafe entries such as
symlinks are refused. If you put secrets inside your skills tree, the archives
can contain them. Backups are not encrypted or uploaded by Skillstore; don't
put them in a public or synced directory. There is no automatic schedule or
deletion of old snapshots. If a disk error leaves an incomplete recovery
directory, inspect it and retry using a different output path.

## Track skills from Git

Keep each skill in a directory such as `skills/demo-skill/` with its own
`SKILL.md`. Choose either a local checkout or a hosted Git repository. The Git
source and the Skillstore `--store` are separate: either source can be used
with a local or remote registry.

### Local checkout (including a repo with no remote)

```sh
export SKILLSTORE_GIT_REPO=/path/to/skills-checkout
skillstore --store REGISTRY --actor contributor submit-git skills/demo-skill 1.0.0
skillstore --store REGISTRY git-status demo-skill --version 1.0.0
```

This mode works offline and defaults to the checkout's `HEAD`; it ignores
uncommitted and untracked files. Use `--revision` to select a full commit ID,
`refs/heads/...`, `refs/tags/...`, or an already fetched `refs/remotes/...`
ref. The registry records `local:git` instead of your filesystem path. You
can optionally record a credential-free HTTPS provenance URL with `--source`
or `SKILLSTORE_GIT_SOURCE`; checkout mode never contacts that URL. For a
private or SSH-only remote, clone/fetch using your usual Git credentials and
use checkout mode with the desired local ref.

### Hosted repository (direct HTTPS)

No existing checkout is needed for a Git endpoint readable over HTTPS
without credentials. Use `--git-remote` (or `SKILLSTORE_GIT_REMOTE`):

```sh
skillstore --store REGISTRY --actor contributor submit-git skills/demo-skill 1.0.0 \
  --git-remote https://git.example.org/team/skills.git --revision refs/heads/main
skillstore --store REGISTRY git-status demo-skill --version 1.0.0 \
  --git-remote https://git.example.org/team/skills.git --revision refs/heads/main
```

Each invocation fetches one shallow revision into a temporary bare repo on
the CLI host and discards it without checking anything out. Use the final
credential-free HTTPS URL (no redirects) and, for a private CA, set
`SSL_CERT_FILE` to an absolute path to its trusted PEM bundle. `--revision`
defaults to the remote's `HEAD`; explicit branch and tag refs and full commit
IDs are supported, subject to the Git server allowing the fetch. For
`git-status`, pass the same Git URL recorded at submission. A URL returned by
the registry is never used to initiate a fetch on its own. Set only one Git
source environment variable at a time; an explicit `--repo` or `--git-remote`
overrides the environment selection.

In either mode, `git-status` compares both the resolved commit and the skill's
package digest with the recorded version, and does not submit anything. To
publish an update for review, submit the new revision as a new version. Every
submission stays pending until independently approved. No background polling,
Git pushes, automatic approval, or installation occurs. Keep private skill
backups separate from version-controlled skills.

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

### Back up one Hermes profile's skills

You can create the same archive without MCP, a registry, or a token. The output
parent must already exist, be owner-private, and be outside the profile. Use
absolute paths with no symlink components:

```sh
skillstore backup-hermes --profile-root /path/to/hermes-profile \
  --output /path/to/private-backups/hermes-skills.tar.gz
```

The mode-`0600` archive contains the configured profile's `skills/` tree,
including locally edited skills and their supporting files. It excludes the
rest of the profile, project skill roots, and Skillstore lifecycle state. Any
private material stored *inside a skill* remains in the archive. No upload,
encryption, or automatic pruning is performed. Quiesce skill writers while
backing up; this is not an atomic snapshot. Links and special files are
refused. Limits are 10,000 files, 20,000 total entries, 32 levels, 64 MiB per
file and 512 MiB total input. A failed write can leave a private `.partial`
file but never overwrites an archive.

Restore a trusted archive to a **new** directory in an existing private
parent, using the digest recorded when you made the backup:

```sh
skillstore restore-hermes /path/to/private-backups/hermes-skills.tar.gz \
  --sha256 "$BACKUP_SHA256" --output /path/to/private-backups/recovered
```

The command validates the archive before creating `recovered/skills`. It
restores ordinary files with owner-only permissions and preserves executable
bits for the owner. It does not merge, execute files, or restore Skillstore
receipts. Inspect the staged skills before moving them into a stopped,
unmanaged or fresh Hermes profile. If a disk write fails, discard or inspect
the incomplete staging directory and retry with a different output path.

### Native Hermes backup plugin

The plugin in [`plugins/skillstore-backup`](../plugins/skillstore-backup)
works without a registry or MCP server. Install Skillstore, then copy that
directory to `<HERMES_HOME>/plugins/skillstore-backup`. The wheel also includes
it under `skillstore/hermes_plugins/skillstore-backup` in the installed Python
environment. Enable it by merging these settings into the selected profile's
`config.yaml`. Use the actual absolute path to the installed executable and
an existing owner-only backup directory outside the profile.

```yaml
plugins:
  enabled:
    - skillstore-backup
  entries:
    skillstore-backup:
      settings:
        executable: /opt/skillstore/bin/skillstore
        backup_dir: /path/to/private-backups
        agent_backup: false
```

```sh
hermes skillstore-backup backup
hermes skillstore-backup restore ARCHIVE_BASENAME --sha256 "$BACKUP_SHA256"
```

The restore command creates a fresh recovery directory alongside backups; it
never overwrites a live skill. To let the agent itself create backups, set
`agent_backup: true` and reload the plugin. This exposes only the no-argument
`skillstore_backup_skills` tool. Restore remains a terminal command.

### Recover a managed target or registry

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
