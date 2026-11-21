# Support and release checks

## Package limits

- A skill package can contain up to 500 files, including `SKILL.md`.
- Each file can be at most 5 MiB, and the total uncompressed file content
  can be at most 20 MiB.
- The `name` and `description` in `SKILL.md` frontmatter can be at most 64
  and 1,024 characters, respectively.
- The HTTP registry accepts request bodies up to 40 MiB. That limit is
  separate from the package limits and includes the request encoding.

## Native discovery

These Linux checks use fresh profiles, reviewed synthetic skills, and no model
prompt. Each named target below passed install v1, upgrade to v2, rollback to v1,
removal from discovery, and offline restoration to v2 via the named CLI. The
checker supports exact runtime pins with `--expect-version`.

- **Codex 0.170.0:** project and OS user `.agents/skills`, app-server
  `skills/list`. The user-scope result must report `scope: user`.
- **Claude Code 2.1.328:** project and default user `.claude/skills`, SDK
  initialization catalog. It exposes the scope and description, not the file path.
- **Hermes (`ae2a81ccc00e9b7c06301523e5bb09608397149c`):** explicit `HERMES_HOME/skills`, native catalog and
  native file scanner. The legacy in-tree layout could expose rollback snapshots;
  use external state.
- **OpenCode 2.3.2:** project `.opencode/skills` and profile
  `$XDG_CONFIG_HOME/opencode/skills`, native catalog via a private loopback server
  with visible and hidden controls.
- **OpenClaw 2026.11.6 (06afb10):** project and profile `skills` directories
  tested independently. `skills list --json` reports different sources
  (`openclaw-workspace`, `openclaw-managed`), not file paths. The checker
  verifies the reported roots and checks visible and hidden controls.
- **Pi 1.5.2:** project `.agents/skills` and profile `skills` under the explicit
  agent directory (`PI_CODING_AGENT_DIR`), offline `get_commands` RPC.

These are tested versions, not minimum versions or guarantees of hot reload,
model behavior, or native MCP configuration. Use `--profile-scopes` to include
Codex, Claude Code, OpenCode, and Pi profile results in a qualification run.

**Standalone Soulsteads Harness 0.1.0:** an explicitly configured `[skills] roots`
directory passed install, upgrade, rollback, removal, and restore via `--target`.
The native `capabilities` result reported the expected catalog count and zero
skipped packages at each stage. It does not expose individual paths or versions;
those were checked from the installed files. No named mapping is required for
an operator-configured root.

**Tracked Soulsteads Agent Home:** use the native non-overwriting import
workflow. Direct installation into a Home can leave untracked state that blocks
the Home's Git revert. Skillstore does not manage a tracked Home's lifecycle.

## Offline native discovery qualification

From the checkout with PyYAML available, use a new state directory under an
existing parent. Set `PYTHONPATH` to this checkout's `src` rather than picking
up an unrelated Skillstore install. Supply trusted installed runtime binaries
(not version-manager shims), or the Hermes interpreter and matching source.
To reproduce pinned results, pass `--expect-version RUNTIME=VALUE` for every
selected runtime: exact `--version` output or, for Hermes, the full Git SHA.
Any version mismatch stops before installing fixtures. Unpinned runs are exploratory.

```sh
scratch=$(mktemp -d)
export PYTHONPATH="$PWD/src"
python -B scripts/qualify_discovery.py --state "$scratch/codex" --codex /path/to/codex
python -B scripts/qualify_discovery.py --state "$scratch/pi" --pi /path/to/pi
python -B scripts/qualify_discovery.py --state "$scratch/claude" --claude /path/to/claude
python -B scripts/qualify_discovery.py --state "$scratch/opencode" --opencode /path/to/opencode
python -B scripts/qualify_discovery.py --state "$scratch/openclaw" --openclaw /path/to/openclaw \
  --expect-version 'openclaw=OpenClaw 2026.11.6 (06afb10)' --full-lifecycle
python -B scripts/qualify_discovery.py --state "$scratch/hermes" \
  --hermes-python /path/to/hermes/venv/bin/python --hermes-source /path/to/hermes/source
```

The command checks discovery after installing v1, upgrading to v2, and rolling
back to v1 through the named CLI. Add `--full-lifecycle` for uninstall and offline
restore. Add `--profile-scopes` to also check Codex, Claude Code, Pi, and OpenCode
profile discovery, each with its own disposable home and workspace. Hermes
always checks its profile and OpenClaw always checks both scopes. The checker
sends no model prompt. It does not
enforce network isolation; use a network-disabled sandbox to rule out egress. It
prints JSON and saves `summary.json`, native responses, and logs in the private
state directory. It exits nonzero if any selected runtime fails a phase.
An `incompatible` result means the native response differed from the expected active skill.
A `probe_error` means the command could not obtain a usable native response.
Codex discovery errors count as `probe_error` even if the expected skill is
listed: a partial result cannot rule out extra or unreadable copies.
Hermes qualification checks both its catalog and its file scanner, so a backup
cannot be hidden by catalog deduplication. Pi qualification uses its offline
`get_commands` RPC and checks the path of the active skill in a disposable target.
Claude Code qualification reads the SDK initialization catalog without sending
a prompt. It confirms the name, scope and version-specific description, not
the native file path (which that catalog does not expose).
OpenCode qualification checks its native catalog on a private loopback
server. It requires a visible control before evaluating the active skill and
rejects an exposed metadata control. See [OpenCode discovery](opencode-discovery.md)
for the scope and evidence.

## Source-only CI and Agent Home lifecycle qualification

`.github/workflows/source-qualification.yml` runs on pull requests and pushes. It
checks out the event's immutable commit with read-only repository permission and
without persisted checkout credentials, runs the focused package tests, then calls
`scripts/release_gate.py` in a private runner-temporary directory on Linux. It
publishes no packages or artifacts. This source check does **not** qualify a
release: distributing built packages still requires the installed-artifact gate
below on the same reviewed commit on both Linux and macOS.

For the synthetic tracked-Home proof, install this Skillstore checkout and install
Soulsteads Home from the exact public checkout at
`31d1fc26fe210d82fd414780a4aeae78efea185f` into the same disposable Python
environment. Manually verify that checkout with
`git -C /path/to/soulsteads-home rev-parse HEAD` before installing; the script
cannot verify which Home build is installed. `--home-revision` is an
operator-supplied label only, reported as `home_revision_label`, not build
provenance. Then run with an existing mode-0700 scratch directory:

```sh
python3 -I -B scripts/qualify_home_lifecycle.py \
  --home-revision 31d1fc26fe210d82fd414780a4aeae78efea185f \
  --scratch /private/scratch
```

The script uses synthetic-only packages, actors, and a disposable tracked Agent
Home. The author and reviewer values in the receipt are synthetic fixture
identities; approval and rejection exercise the lifecycle API, not human review
or endorsement. Before reviewer approval, it attempts to install the existing
pending v1, asserts a `not_reviewed` denial, and verifies that nothing was
staged. It also checks that author self-review receives the dedicated self-review
diagnostic, while a different synthetic reviewer can reject one proposal and
approve another. Reviewed versions are staged outside the Home, and imports
happen only through `soulsteads skill install`. It also checks that an existing
skill cannot be upgraded in place and that `soulsteads revert` restores a
previously tracked version as a new Home commit. Canonical `skills/` bytes and
Home-maintained `.agents/skills` and `.claude/skills` aliases are compared
byte-for-byte; that filesystem check does not prove native OpenCode discovery.
The native discovery probe is not run by this qualification. This qualification
does not run a model or auto-approve/curate packages. Use the maintained native
discovery check above only when a compatible runtime is available.

## Installed-artifact gate

Run this gate against the **same reviewed commit on Linux and macOS** before
tagging a release or distributing built packages. An untagged source preview
does not claim macOS qualification. Use a trusted checkout of that commit and a short, private scratch
path outside the checkout. The runner needs Python 3.11+, Git, tar, uv, and
access to the packaging and test dependencies. The `mcp` dependency is for the
smoke-test client only; Skillstore does not need it at runtime.

```sh
# Set expected_os to Darwin on the macOS runner, and Linux on the Linux runner.
expected_os=Linux
scratch=$(mktemp -d)
python3 -I -B scripts/release_gate.py \
    --revision "$(git rev-parse HEAD)" --expect-os "$expected_os" --scratch "$scratch" \
    > "$scratch/release-gate.json"
```

The gate rejects an OS mismatch, exports the selected Git commit (not the
working tree), and verifies the source suite imports the export. It installs the
wheel and the sdist into separate fresh environments and verifies their imports
before testing the installed CLI and real MCP stdio server, including offline
rollback from a restored backup. Logs go to stderr; only a completed pass emits
JSON, with the commit, host platform, Python version, per-artifact SHA-256, and
smoke results. Build outputs remain in the printed scratch subdirectory.

The gate does not run native harnesses or publish artifacts. Retain both reports;
Linux cannot stand in for macOS. For Forgejo Actions, use an isolated Linux runner
and a separately registered protected macOS runner.
