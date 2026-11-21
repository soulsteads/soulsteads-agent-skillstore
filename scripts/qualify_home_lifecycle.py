#!/usr/bin/env python3
"""Exercise reviewed Skillstore -> Soulsteads Home import and rollback offline.

Both ``skillstore`` and ``soulsteads`` must be installed in this interpreter.
All package, registry, staging, and Agent Home files are created under the
existing private scratch directory supplied by ``--scratch``.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

SKILL = "d2-home-synthetic"
AGENT = "d2-home-qualification"
AUTHOR = "synthetic-author"
REVIEWER = "synthetic-reviewer"
PROVENANCE = "https://example.invalid/d2-home-synthetic.git"
SUBPROCESS_TIMEOUT = 60
LINKS = (".agents/skills", ".claude/skills")


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scratch",
        required=True,
        type=Path,
        help="existing private directory; all disposable evidence stays below it",
    )
    parser.add_argument(
        "--home-revision",
        help="optional immutable Soulsteads Home commit recorded in the report",
    )
    return parser


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _private_directory(path):
    mode = stat.S_IMODE(path.stat().st_mode)
    if path.stat().st_uid != os.geteuid() or mode & 0o077:
        raise SystemExit(f"scratch must be owned by this user and mode 0700 or stricter: {path}")


def _write_skill(path, version, text):
    path.mkdir(parents=True, mode=0o700)
    content = (
        f"---\nname: {SKILL}\ndescription: Synthetic Home lifecycle fixture {version}.\n"
        f"---\n\n{text}"
    ).encode("utf-8")
    (path / "SKILL.md").write_bytes(content)
    return content


def _main():
    args = _parser().parse_args()
    scratch = args.scratch.resolve(strict=True)
    if not scratch.is_dir():
        raise SystemExit(f"scratch is not a directory: {scratch}")
    _private_directory(scratch)
    if args.home_revision and not re.fullmatch(r"[0-9a-f]{40}", args.home_revision):
        raise SystemExit("--home-revision must be a full 40-character lowercase Git SHA")

    os.umask(0o077)
    work = scratch / f"home-lifecycle-{uuid.uuid4().hex[:12]}"
    work.mkdir(mode=0o700)
    for name in ("registry", "agents", "stage-v1", "stage-unreviewed", "stage-v2"):
        (work / name).mkdir(mode=0o700)
    source_v1 = work / "source-v1" / SKILL
    source_v2 = work / "source-v2" / SKILL
    bytes_v1 = _write_skill(source_v1, "v1", "Synthetic v1 instructions; never execute.\n")
    bytes_v2 = _write_skill(source_v2, "v2", "Synthetic v2 instructions; never execute.\n")

    home = work / "isolated-user-home"
    home.mkdir(mode=0o700)
    private_git_config = home / ".gitconfig"
    private_git_config.touch(mode=0o600)
    private_tmp = work / "tmp"
    private_tmp.mkdir(mode=0o700)
    for private_xdg in (
        home / ".config",
        home / ".cache",
        home / ".local" / "share",
        home / ".local" / "state",
        home / ".runtime",
    ):
        private_xdg.mkdir(parents=True, mode=0o700, exist_ok=True)
    env = {}
    env.update(
        {
            "HOME": str(home),
            "PATH": os.pathsep.join(
                (str(Path(sys.executable).resolve().parent), "/usr/local/bin", "/usr/bin", "/bin")
            ),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "XDG_RUNTIME_DIR": str(home / ".runtime"),
            "TMPDIR": str(private_tmp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(private_git_config),
            "GIT_AUTHOR_NAME": "D2 synthetic qualification",
            "GIT_AUTHOR_EMAIL": "d2-qualification@example.invalid",
            "GIT_COMMITTER_NAME": "D2 synthetic qualification",
            "GIT_COMMITTER_EMAIL": "d2-qualification@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    transcript = work / "commands.jsonl"

    def invoke(module, argv, label, expect_success=True):
        entrypoint = f"from {module} import main; raise SystemExit(main())"
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", entrypoint, *map(str, argv)],
            cwd=work,
            env=env,
            text=True,
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
        record = {
            "label": label,
            "argv": [module, *map(str, argv)],
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        if expect_success and result.returncode != 0:
            raise RuntimeError(
                f"{label} failed ({result.returncode}): {result.stdout}{result.stderr}"
            )
        if not expect_success and result.returncode == 0:
            raise AssertionError(f"{label} unexpectedly succeeded: {result.stdout}")
        return result

    def skillstore(*argv, label, expect_success=True):
        return invoke("skillstore.cli", argv, label, expect_success)

    def soulsteads(*argv, label, expect_success=True):
        return invoke("soulsteads.cli", argv, label, expect_success)

    def git(*argv):
        result = subprocess.run(
            ["git", *map(str, argv)],
            cwd=work,
            env=env,
            text=True,
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"git {' '.join(map(str, argv))}: {result.stderr}")
        return result.stdout.strip()

    registry = work / "registry"
    skillstore(
        "init",
        registry,
        "--reviewer",
        REVIEWER,
        "--reviewer",
        AUTHOR,
        label="initialize synthetic registry with both fixture roles",
    )
    skillstore(
        "--store",
        registry,
        "--actor",
        AUTHOR,
        "submit",
        source_v1,
        "1.0.0",
        "--source",
        PROVENANCE,
        "--revision",
        "d2-synthetic-v1",
        label="submit synthetic v1",
    )
    pending_install = skillstore(
        "--store",
        registry,
        "install",
        SKILL,
        "--version",
        "1.0.0",
        "--target",
        work / "stage-unreviewed",
        label="reject install of pending v1 before review",
        expect_success=False,
    )
    pending_error = pending_install.stderr.strip() or pending_install.stdout.strip()
    try:
        pending_error_payload = json.loads(pending_error)
    except json.JSONDecodeError as exc:
        raise AssertionError("pending v1 install failure was not JSON") from exc
    if pending_error_payload.get("error") != "not_reviewed":
        raise AssertionError("pending v1 install must fail with not_reviewed")
    if any((work / "stage-unreviewed").iterdir()):
        raise AssertionError("a not-reviewed v1 install staged files")

    skillstore(
        "--store",
        registry,
        "--actor",
        REVIEWER,
        "review",
        SKILL,
        "1.0.0",
        "approve",
        "--note",
        "Reviewed synthetic v1 for lifecycle qualification",
        label="independent reviewer approves v1",
    )
    skillstore(
        "--store",
        registry,
        "install",
        SKILL,
        "--version",
        "1.0.0",
        "--target",
        work / "stage-v1",
        label="stage reviewed v1 outside Agent Home",
    )

    rejected = skillstore(
        "--store",
        registry,
        "--actor",
        AUTHOR,
        "propose",
        SKILL,
        source_v2,
        "--base-version",
        "1.0.0",
        "--rationale",
        "Deliberately rejected synthetic proposal",
        label="submit proposal for reviewer rejection",
    )
    rejected_id = json.loads(rejected.stdout)["id"]
    skillstore(
        "--store",
        registry,
        "proposal-diff",
        rejected_id,
        label="inspect rejected proposal diff",
    )
    self_approval = skillstore(
        "--store",
        registry,
        "--actor",
        AUTHOR,
        "decide",
        rejected_id,
        "accept",
        "--version",
        "1.1.0",
        "--note",
        "Self-review must be rejected",
        label="reject author self-review",
        expect_success=False,
    )
    self_error = self_approval.stderr.strip() or self_approval.stdout.strip()
    self_error_payload = json.loads(self_error)
    if (
        self_error_payload.get("error") != "permission_denied"
        or "submitted this content and cannot also review it" not in self_error
    ):
        raise AssertionError("author self-review did not reach the dedicated self-review denial")
    reviewer_rejection = skillstore(
        "--store",
        registry,
        "--actor",
        REVIEWER,
        "decide",
        rejected_id,
        "reject",
        "--note",
        "Independent reviewer rejects the first synthetic proposal",
        label="independent reviewer rejects proposal",
    )
    if json.loads(reviewer_rejection.stdout).get("proposal", {}).get("status") != "rejected":
        raise AssertionError("the independent reviewer did not reject the synthetic proposal")

    accepted = skillstore(
        "--store",
        registry,
        "--actor",
        AUTHOR,
        "propose",
        SKILL,
        source_v2,
        "--base-version",
        "1.0.0",
        "--rationale",
        "Second synthetic proposal for the approved Home lifecycle",
        label="submit proposal for independent approval",
    )
    accepted_id = json.loads(accepted.stdout)["id"]
    skillstore(
        "--store",
        registry,
        "proposal-diff",
        accepted_id,
        label="inspect approved proposal diff",
    )
    skillstore(
        "--store",
        registry,
        "--actor",
        REVIEWER,
        "decide",
        accepted_id,
        "accept",
        "--version",
        "1.1.0",
        "--note",
        "Independent review of synthetic v2",
        label="independent reviewer approves v2",
    )
    reviewed = skillstore(
        "--store",
        registry,
        "inspect",
        SKILL,
        "--version",
        "1.1.0",
        label="verify v2 is reviewed before staging",
    )
    if json.loads(reviewed.stdout).get("status") != "reviewed":
        raise AssertionError("accepted synthetic version is not marked reviewed")
    skillstore(
        "--store",
        registry,
        "install",
        SKILL,
        "--version",
        "1.1.0",
        "--target",
        work / "stage-v2",
        label="stage reviewed v2 outside Agent Home",
    )

    agents = work / "agents"
    soulsteads(
        "--agents-dir",
        agents,
        "create",
        AGENT,
        "--runtime",
        "codex",
        label="create disposable tracked Agent Home through Home CLI",
    )
    agent_home = agents / AGENT
    if not agent_home.is_dir():
        raise AssertionError("Home CLI did not create the requested Agent Home")
    for stage in (work / "stage-v1", work / "stage-v2"):
        if stage.resolve().is_relative_to(agent_home.resolve()):
            raise AssertionError("Skillstore staging must remain outside the Agent Home")

    rev0 = git("-C", agent_home, "rev-parse", "HEAD")
    soulsteads(
        "--agents-dir",
        agents,
        "skill",
        "install",
        AGENT,
        work / "stage-v1" / SKILL,
        label="explicitly import reviewed v1 through Home skill install",
    )
    soulsteads(
        "--agents-dir",
        agents,
        "commit",
        AGENT,
        "-m",
        "Commit reviewed synthetic v1 through Home API",
        label="commit v1 through Home API",
    )
    rev1 = git("-C", agent_home, "rev-parse", "HEAD")

    def verify_home_version(expected, label):
        canonical = agent_home / "skills" / SKILL / "SKILL.md"
        if canonical.read_bytes() != expected:
            raise AssertionError(f"{label}: canonical portable skill bytes differ")
        for relative in LINKS:
            alias = agent_home / relative
            if not alias.is_symlink():
                raise AssertionError(f"{label}: Home did not maintain alias symlink {relative}")
            if alias.resolve() != (agent_home / "skills").resolve():
                raise AssertionError(f"{label}: alias {relative} does not point to portable skills")
            if (alias / SKILL / "SKILL.md").read_bytes() != expected:
                raise AssertionError(f"{label}: aliased skill bytes differ at {relative}")

    verify_home_version(bytes_v1, "v1 import")
    if (work / "stage-unreviewed" / SKILL).exists():
        raise AssertionError("an unreviewed version was staged")

    soulsteads(
        "--agents-dir",
        agents,
        "skill",
        "install",
        AGENT,
        work / "stage-v2" / SKILL,
        label="reject in-place Home skill overwrite",
        expect_success=False,
    )
    if git("-C", agent_home, "rev-parse", "HEAD") != rev1:
        raise AssertionError("failed in-place install changed the tracked Home revision")
    verify_home_version(bytes_v1, "after rejected in-place install")

    soulsteads(
        "--agents-dir",
        agents,
        "revert",
        AGENT,
        rev0,
        label="revert Home to pre-skill revision through Home API",
    )
    if (agent_home / "skills" / SKILL).exists():
        raise AssertionError("Home revert to the initial revision left the skill installed")
    soulsteads(
        "--agents-dir",
        agents,
        "skill",
        "install",
        AGENT,
        work / "stage-v2" / SKILL,
        label="explicitly import reviewed v2 through Home skill install",
    )
    soulsteads(
        "--agents-dir",
        agents,
        "commit",
        AGENT,
        "-m",
        "Commit reviewed synthetic v2 through Home API",
        label="commit v2 through Home API",
    )
    rev2 = git("-C", agent_home, "rev-parse", "HEAD")
    verify_home_version(bytes_v2, "v2 import")

    soulsteads(
        "--agents-dir",
        agents,
        "revert",
        AGENT,
        rev1,
        label="restore previously tracked v1 through Home revert API",
    )
    restored_head = git("-C", agent_home, "rev-parse", "HEAD")
    if restored_head == rev1:
        raise AssertionError("Home revert should record restoration as a new commit")
    if git("-C", agent_home, "status", "--porcelain"):
        raise AssertionError("Home API lifecycle left uncommitted tracked changes")
    verify_home_version(bytes_v1, "restored prior tracked v1")

    report = {
        "qualification": "synthetic reviewed Skillstore -> Agent Home lifecycle",
        "home_revision_label": args.home_revision,
        "skill": SKILL,
        "agent": AGENT,
        "synthetic_reviewer": REVIEWER,
        "synthetic_author": AUTHOR,
        "rejected_proposal_id": rejected_id,
        "approved_proposal_id": accepted_id,
        "v1_sha256": _digest(bytes_v1),
        "v2_sha256": _digest(bytes_v2),
        "staging_outside_agent_home": True,
        "home_revisions": {
            "initial": rev0,
            "v1_installed": rev1,
            "v2_installed": rev2,
            "restored_v1_new_commit": restored_head,
        },
        "portable_and_alias_bytes_verified": list(LINKS),
        "native_discovery_probe": "not run; filesystem byte checks do not prove native OpenCode discovery",
        "work_directory": str(work),
        "command_transcript": str(transcript),
    }
    report_path = work / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.chmod(0o600)
    print(
        "PASS synthetic review/import/revert; "
        f"v1_sha256={report['v1_sha256']} v2_sha256={report['v2_sha256']} "
        f"native_probe={report['native_discovery_probe']}; report={report_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
