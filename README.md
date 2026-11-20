# Soulsteads Agent Skillstore

**A registry and installer for reviewed, versioned Agent Skills.** Share a
skill across projects or harnesses, see exactly what was approved, and choose
when to update. Run it locally or serve a shared registry. Neither Agent Home
nor a model account is required.

[Quickstart](#quickstart) · [Operator guide](docs/usage.md) · [Tested harnesses](docs/support.md#native-discovery)

**Source preview:** Tested on Linux. macOS qualification is pending; Windows
is not supported. No tagged release yet.

## What it does

- **Review before adoption.** Submit a package with its source revision and
  approve a specific version. Check its review, digest, and proposed file
  changes before installing it. Contributors can propose improvements for review.
- **Update on your terms.** Install the exact version you choose. Skillstore
  refuses to overwrite local edits or directories it doesn't manage.
- **Recover offline.** Roll back an update or restore a removed skill using
  snapshots stored beside the active skills directory. Receipts record what
  was installed and where it came from.

## Install from source

From a checkout of the revision you want to try, with Python 3.11 or later:

```sh
uv tool install .
```

Alternatively, run `python3 -m pip install .` in an activated virtual environment.
No model account or harness is needed for the quickstart.

## Quickstart

Publish and install a sample skill, then recover it without the registry.
Run both blocks in the same shell. They work in a disposable directory and
leave your harness configuration untouched. The `--actor` names in this local
example are labels, not authentication. For separate permissions, see
[shared registries](docs/usage.md#shared-registries).

```sh
set -eu
work=$(mktemp -d)
work=$(cd "$work" && pwd -P)
cd "$work"
mkdir demo-skill
cat > demo-skill/SKILL.md <<'EOF'
---
name: demo-skill
description: Summarize a short note in one sentence.
---
Read the note and write one sentence that preserves its main point.
EOF

# Create a registry, submit the package, and approve the exact version.
skillstore init ./registry --reviewer demo-reviewer
skillstore --store ./registry --actor demo-author submit ./demo-skill 1.0.0 \
  --source https://example.org/demo-skills.git --revision quickstart-v1
skillstore --store ./registry --actor demo-reviewer review demo-skill 1.0.0 approve \
  --note 'Reviewed the demonstration instructions'

# Discover the release, preview the file changes, and install it.
skillstore --store ./registry search demo-skill
skillstore --store ./registry inspect demo-skill --version 1.0.0
skillstore --store ./registry preview demo-skill --version 1.0.0 --target ./skills
skillstore --store ./registry install demo-skill --version 1.0.0 --target ./skills
skillstore status --target ./skills
```

`status` now shows `demo-skill` at version `1.0.0` with a clean receipt. The
installed instructions are in `skills/demo-skill/SKILL.md`. Installation copies
files; it does not execute them. For a real package, record the actual source
URL and an immutable revision.

### Remove and restore offline

Remove the active copy, then restore it from the local snapshot. Neither command
needs access to the registry:

```sh
skillstore uninstall demo-skill --target ./skills
skillstore rollback demo-skill 1.0.0 --target ./skills
skillstore status --target ./skills
```

Keep the skill root and its sibling `.skillstore-skills` together for recovery.
Only the active copy is installed in the skill root.

## Use with a harness

- [Choose a native target](docs/usage.md#native-targets) for Codex, Claude Code,
  Hermes, OpenClaw, OpenCode, or Pi. Use explicit `--target` for a standalone
  Soulsteads Harness or another operator-configured skill root.
- [Configure the stdio MCP server](docs/usage.md#mcp) in a compatible harness.
  Skillstore does not edit native configuration or grant execution permissions.
- [Connect Hermes to a shared registry](docs/usage.md#hermes-with-a-remote-registry-and-local-backups)
  and enable private skills-only backups on the agent host.
- [Submit skill updates through MCP](docs/usage.md#contribute-a-skill-through-mcp)
  from an explicitly configured staging directory. Approval stays separate.
- [Track skills from Git](docs/usage.md#track-skills-from-git) using an offline
  checkout or a hosted HTTPS repository. Submit exact committed versions for review.
- [Install the native Hermes backup plugin](docs/usage.md#native-hermes-backup-plugin)
  to back up a profile's skills and restore them into a separate directory.
- [Import into a tracked Soulsteads Agent Home](docs/usage.md#tracked-agent-homes)
  through Home's native import. Skillstore does not manage the tracked lifecycle.

See the [operator guide](docs/usage.md) for updates, proposals, local edits,
backups, and migration, and [support and release checks](docs/support.md) for
the exact versions tested and the limits of those checks.

## License

Skillstore and its bundled `manage-skills` skill are licensed under the
[Apache License 2.0](LICENSE). Skills distributed through the registry retain
their own licenses.
