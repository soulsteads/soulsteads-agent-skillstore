import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile

import pytest

from skillstore import backup as backups
from skillstore.backup import HermesBackup, backup_hermes


def _cli(tmp_path, *args, stdin=None):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    return subprocess.run([sys.executable, "-B", "-m", "skillstore", *args], cwd=tmp_path,
                          env=env, input=stdin, capture_output=True, text=True, timeout=10)


def test_cli_backups_need_no_registry(tmp_path):
    profile = tmp_path / "profile"
    (profile / "skills").mkdir(parents=True)
    (profile / "skills" / "SKILL.md").write_text("private skill")
    output = _private_destination(tmp_path) / "skills.tar.gz"
    result = _cli(tmp_path, "--store", str(tmp_path / "no-store"), "backup-hermes",
                  "--profile-root", str(profile), "--output", str(output))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "archive": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "files": 1,
    }
    assert not (tmp_path / "no-store").exists()


def test_mcp_can_back_up_when_remote_registry_is_unavailable(tmp_path):
    profile = tmp_path / "profile"
    (profile / "skills").mkdir(parents=True)
    (profile / "skills" / "SKILL.md").write_bytes(b"synthetic private skill")
    (profile / "auth.json").write_bytes(b"excluded fixture credential")
    destination = _private_destination(tmp_path)
    calls = [("initialize", {}), ("tools/list", {}),
             ("tools/call", {"name": "skillstore_backup_hermes", "arguments": {"output": str(tmp_path / "outside.tar.gz")}}),
             ("tools/call", {"name": "skillstore_backup_hermes", "arguments": {}})]
    requests = "".join(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params}) + "\n"
                       for n, (method, params) in enumerate(calls))
    # A non-serving registry must not prevent an already configured local snapshot.
    result = _cli(tmp_path, "--store", "http://127.0.0.1:0", "mcp", "--harness", "hermes",
                  "--scope", "profile", "--root", str(profile), "--backup-dir", str(destination), stdin=requests)
    assert result.returncode == 0, result.stderr
    _, listing, denied, backup = [json.loads(line)["result"] for line in result.stdout.splitlines()]
    tool, = [t for t in listing["tools"] if t["name"] == "skillstore_backup_hermes"]
    assert tool["inputSchema"]["properties"] == {} and not tool["annotations"]["openWorldHint"]
    assert denied["isError"] and not (tmp_path / "outside.tar.gz").exists()
    assert not backup["isError"], backup
    receipt = json.loads(backup["content"][0]["text"])
    archive_path, = destination.iterdir()
    assert receipt == {"archive": str(archive_path), "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(), "files": 1}
    with tarfile.open(archive_path, "r:gz") as archive:
        assert archive.getnames() == ["skills", "skills/SKILL.md"]
        skill = archive.extractfile("skills/SKILL.md")
        assert skill is not None and skill.read() == b"synthetic private skill"
    assert "synthetic private skill" not in result.stdout and "excluded fixture credential" not in result.stdout


def test_backup_archives_only_skills_with_bytes_and_executable_modes(tmp_path):
    profile = tmp_path / "profile"
    skills = profile / "skills"
    nested = skills / "category" / "nested"
    nested.mkdir(parents=True)
    payload = b"#!/bin/sh\nprintf 'private skill data\\n'\n"
    script = nested / "run.sh"
    script.write_bytes(payload)
    script.chmod(0o751)
    (skills / "top.md").write_bytes(b"skill only\n")
    (profile / "auth.json").write_text("do not archive")
    (profile / "sessions.sqlite").write_text("do not archive")
    (profile / "project").mkdir()
    (profile / "project" / "outside.md").write_text("do not archive")
    (skills / ".skillstore").mkdir()
    (skills / ".skillstore" / "receipt.json").write_text("legacy state must be excluded")

    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    output = destination / "skills.tar.gz"

    receipt = backup_hermes(str(profile), str(output))

    assert receipt["archive"] == str(output)
    assert receipt["files"] == 2
    assert receipt["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(output, "r:gz") as archive:
        assert archive.getnames() == [
            "skills",
            "skills/category",
            "skills/category/nested",
            "skills/category/nested/run.sh",
            "skills/top.md",
        ]
        if sys.version_info >= (3, 12):
            archive.extractall(extracted, filter="data")
        else:
            archive.extractall(extracted)

    restored = extracted / "skills" / "category" / "nested" / "run.sh"
    assert restored.read_bytes() == payload
    assert stat.S_IMODE(restored.stat().st_mode) & 0o111 == 0o111
    assert not (extracted / "auth.json").exists()
    assert not (extracted / "sessions.sqlite").exists()
    assert not (extracted / "project").exists()


def _private_destination(tmp_path):
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    return destination


@pytest.mark.parametrize("entry_kind", ["symlink", "hardlink", "fifo"])
def test_backup_refuses_links_and_special_entries_without_reading_targets(tmp_path, entry_kind):
    profile = tmp_path / "profile"
    skills = profile / "skills"
    skills.mkdir(parents=True)
    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"outside bytes must not be read")
    entry = skills / "unsafe"
    if entry_kind == "symlink":
        entry.symlink_to(outside)
    elif entry_kind == "hardlink":
        entry.hardlink_to(outside)
    else:
        os.mkfifo(entry)

    output = _private_destination(tmp_path) / f"{entry_kind}.tar.gz"
    with pytest.raises((ValueError, OSError)):
        backup_hermes(str(profile), str(output))
    assert not output.exists()
    assert outside.read_bytes() == b"outside bytes must not be read"


def test_backup_rejects_unsafe_archive_member_names(tmp_path):
    profile = tmp_path / "profile"
    skills = profile / "skills"
    skills.mkdir(parents=True)
    (skills / r"..\outside.md").write_bytes(b"synthetic")
    output = _private_destination(tmp_path) / "skills.tar.gz"
    with pytest.raises(ValueError, match="unsafe archive member"):
        backup_hermes(str(profile), str(output))
    assert not output.exists()


def test_backup_limits_empty_directory_inventory(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    skills = profile / "skills"
    skills.mkdir(parents=True)
    for name in ("one", "two", "three"):
        (skills / name).mkdir()
    monkeypatch.setattr(backups, "_MAX_ENTRIES", 2, raising=False)
    output = _private_destination(tmp_path) / "skills.tar.gz"
    with pytest.raises(ValueError, match="archive safety limit"):
        backup_hermes(str(profile), str(output))
    assert not output.exists()


def test_mcp_backup_refuses_replaced_skills_root_after_configuration(tmp_path):
    profile = tmp_path / "profile"
    skills = profile / "skills"
    skills.mkdir(parents=True)
    destination = _private_destination(tmp_path)
    configured = HermesBackup(str(profile), str(destination))
    skills.rename(profile / "old-skills")
    skills.mkdir()
    (skills / "SKILL.md").write_text("outside the configured skills tree")
    with pytest.raises(ValueError, match="configured backup directories changed"):
        configured.create()
    assert list(destination.iterdir()) == []


def test_backup_never_overwrites_output_that_appears_during_archive(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    (profile / "skills").mkdir(parents=True)
    output = _private_destination(tmp_path) / "skills.tar.gz"
    digest = backups._sha256_from_fd

    def occupy_name(fd):
        with output.open("xb") as stream:
            stream.write(b"concurrent backup must survive")
        return digest(fd)

    monkeypatch.setattr(backups, "_sha256_from_fd", occupy_name)
    with pytest.raises(FileExistsError):
        backup_hermes(str(profile), str(output))
    assert output.read_bytes() == b"concurrent backup must survive"


def test_backup_never_overwrites_an_existing_output_or_accepts_source_output(tmp_path):
    profile = tmp_path / "profile"
    (profile / "skills").mkdir(parents=True)
    (profile / "skills" / "SKILL.md").write_text("safe skill")
    destination = _private_destination(tmp_path)
    output = destination / "existing.tar.gz"
    output.write_bytes(b"operator backup that must survive")

    with pytest.raises(FileExistsError):
        backup_hermes(str(profile), str(output))
    assert output.read_bytes() == b"operator backup that must survive"

    with pytest.raises(ValueError, match="within profile_root"):
        backup_hermes(str(profile), str(profile / "inside.tar.gz"))


def test_backup_requires_a_private_existing_destination_and_owner_only_archive(tmp_path):
    profile = tmp_path / "profile"
    (profile / "skills").mkdir(parents=True)
    (profile / "skills" / "SKILL.md").write_bytes(b"skill")
    public_destination = tmp_path / "public"
    public_destination.mkdir(mode=0o755)
    public_destination.chmod(0o755)

    with pytest.raises(ValueError, match="private"):
        backup_hermes(str(profile), str(public_destination / "skills.tar.gz"))

    destination = _private_destination(tmp_path)
    prior_umask = os.umask(0)
    try:
        receipt = backup_hermes(str(profile), str(destination / "private.tar.gz"))
    finally:
        os.umask(prior_umask)
    assert receipt["files"] == 1
    assert stat.S_IMODE((destination / "private.tar.gz").stat().st_mode) == 0o600


@pytest.mark.parametrize("alias_location", ["profile", "skills"])
def test_backup_refuses_symlinked_source_path_components(tmp_path, alias_location):
    real_profile = tmp_path / "real-profile"
    (real_profile / "skills").mkdir(parents=True)
    (real_profile / "skills" / "SKILL.md").write_text("private skill")
    if alias_location == "profile":
        profile = tmp_path / "profile-alias"
        profile.symlink_to(real_profile, target_is_directory=True)
    else:
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / "skills").symlink_to(real_profile / "skills", target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        backup_hermes(str(profile), str(_private_destination(tmp_path) / "skills.tar.gz"))


def test_backup_refuses_a_symlinked_destination_without_touching_its_target(tmp_path):
    profile = tmp_path / "profile"
    (profile / "skills").mkdir(parents=True)
    (profile / "skills" / "SKILL.md").write_text("private skill")
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    external.chmod(0o700)
    alias = tmp_path / "backup-alias"
    alias.symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        backup_hermes(str(profile), str(alias / "skills.tar.gz"))
    assert list(external.iterdir()) == []
