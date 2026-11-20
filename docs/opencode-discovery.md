# OpenCode 2.3.2 project discovery

A disposable Linux fixture passed native discovery at `.opencode/skills` on
OpenCode 2.3.2. A fresh OpenCode process was started for each observation.

- Before install: the native catalog contained the visible control skill, but
  not the Skillstore fixture.
- Install v1 → upgrade to v2 → roll back to v1: at each step the catalog
  returned exactly one Skillstore fixture, at the active `SKILL.md` path, with
  the expected version-specific description. The Skillstore receipt and a
  nested supporting file independently matched the expected version.
- A distinct control skill under `.skillstore` was absent after upgrade and
  rollback. Moving that same directory into the visible skill root made it
  appear. This checks for metadata leakage even when the native catalog would
  deduplicate two copies of the same skill ID.

## Reproducing the observation

Run the offline checker in a filesystem- and network-isolated sandbox with
loopback enabled and a trusted OpenCode 2.x executable:

```sh
python -B scripts/qualify_discovery.py --state /fixture/opencode-check \
  --opencode /path/to/opencode
```

The checker runs the three lifecycle phases, checking a visible positive
control and a distinct hidden metadata control each time. The separate
manual fixture additionally checked the pre-install catalog and moved the
hidden control into the visible skills root to prove it could be detected.

### Native interface

Use a disposable project and home with network egress blocked and no inherited
credentials or user configuration. Put a distinct positive control at
`.opencode/skills/<control>/SKILL.md`. Start the installed OpenCode executable
with `serve --stdio --hostname 127.0.0.1 --port 0` and a fresh, in-memory
`OPENCODE_PASSWORD`. Keep stdin open for the server's lifetime. Use the URL
from its JSON handshake only if it is a loopback address; send authenticated
`GET /api/skill?directory=<encoded-project-path>` requests to that private
server. Never attach to an existing OpenCode service.

Require the exact project in `location.directory` and wait, with a deadline,
for the positive control's ID and path to appear. OpenCode 2.3.2 returned HTTP
200 with an empty catalog before runtime initialization had completed, so a
one-shot `api --standalone skill.list` or a successful empty response does not
establish that a skill is missing. Compare the fixture's ID, name, description,
and path only after the positive control appears. Close stdin and reap the
server before changing the fixture for the next phase.

This was a cold-start catalog check, not a test of hot reload, model-facing
skill selection, or profile-scope discovery. The named project target now uses
external state and can be tested through removal and offline restore by adding
`--full-lifecycle`. The historical `.skillstore` control described above now
lives in the external state directory when run with the current checker.
