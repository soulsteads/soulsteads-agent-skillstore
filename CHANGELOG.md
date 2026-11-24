# Changelog

## 0.2.0 (proposed)

No previous GitHub tag exists. This candidate covers the complete public default
branch history through `d5c2c366efaed95e9571baf0cbc453d4cd6a001f` and adds the
following release-preparation changes.

### Highlights

- Rename the canonical executable to `soulsteads-skillstore`; keep `skillstore`
  as an explicit compatibility alias for existing Hermes and MCP configurations.
- Add a Linux systemd listener installer for the private loopback registry. It
  writes a non-clobbering unit only, requires an existing absolute store and a
  viable installed executable, keeps secrets out of the unit, and never starts
  or enables systemd itself.
- Support user units at the XDG configuration location and system units at the
  fixed root-protected system unit directory, requiring a named non-root system
  service account.
- Smoke the canonical executable in the published-release artifact workflow.

### Included public commits

All commits already on the public default branch in this release range are
included below. No unmerged branch is represented.

- [`1850585`](https://github.com/soulsteads/soulsteads-agent-skillstore/commit/1850585b2e41bc4737c1f7f8981309858f377fc0) Publish the Skillstore source preview
- [`10a949a`](https://github.com/soulsteads/soulsteads-agent-skillstore/commit/10a949a77ea0c6111e79768655b3ff0689f2ea78) Update the Skillstore source preview
- [`c4ef359`](https://github.com/soulsteads/soulsteads-agent-skillstore/commit/c4ef359a8764671d5998d20942196b12c133ce67) feat: add private skill tracking and network MCP
- [`0008f35`](https://github.com/soulsteads/soulsteads-agent-skillstore/commit/0008f35bec05440a5fdf189e55ecf58ee7a3aa36) chore(release): prepare v0.1.1
- [`7b24bb6`](https://github.com/soulsteads/soulsteads-agent-skillstore/commit/7b24bb6eaf566ab004b6b30591a5369a6a4ec53a) ci: qualify Skillstore packages and Home rollback
- [`95f468c`](https://github.com/soulsteads/soulsteads-agent-skillstore/commit/95f468cf5946831a1e7678462aceada582f88c4e) feat: add Docker and Podman deployment
- [`c066445`](https://github.com/soulsteads/soulsteads-agent-skillstore/commit/c066445ce019e089ff84ee7c5b188827b16b95b5) docs: require Docker 28 for loopback port publishing
- [`d5c2c36`](https://github.com/soulsteads/soulsteads-agent-skillstore/commit/d5c2c366efaed95e9571baf0cbc453d4cd6a001f) ci: build assets when a GitHub release is published
