import io
import json
import sys

import pytest

from skillstore import cli, package as pkg
from skillstore.store import LocalStore, make_actor


@pytest.fixture
def run_cli(monkeypatch, capsys):
    def run(*args, exit_code=0):
        monkeypatch.setattr(sys, "argv", ["skillstore", *map(str, args)])
        try:
            result = cli.main()
        except SystemExit as exc:
            result = exc.code
        captured = capsys.readouterr()
        assert result == exit_code, captured.err
        if exit_code:
            assert not captured.out
            return json.loads(captured.err)
        assert not captured.err
        return json.loads(captured.out)

    return run


def submit_release(store, version, body):
    files = {
        "SKILL.md": (b"---\nname: target-check\ndescription: A target test.\n---\n" + body, False),
        "scripts/check.sh": (b"#!/bin/sh\nprintf 'test\\n'\n", True),
    }
    store.submit(pkg.pack(files), version, make_actor("author"), {"revision": version})
    return files


def approve_release(store, version):
    return store.review("target-check", version, "approve", make_actor("reviewer"), "Test review")


def test_target_plan_is_read_only(run_cli, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    registry = tmp_path / "unused-registry"
    assert run_cli("--store", registry, "target", "--harness", "codex", "--scope", "project", "--root", root) == {
        "harness": "codex",
        "scope": "project",
        "root": str(root),
        "target": str(root / ".agents/skills"),
        "managed_install": True,
    }
    assert list(root.iterdir()) == []
    assert not registry.exists()


@pytest.mark.parametrize(("harness", "directory"), [
    ("codex", ".agents/skills"), ("claude-code", ".claude/skills"),
    ("pi", "skills"), ("opencode", "skills"),
])
def test_qualified_profile_targets_are_managed_and_leave_unused_roots_read_only(run_cli, tmp_path, harness, directory):
    named = ("--harness", harness, "--scope", "profile", "--root", tmp_path)
    plan = run_cli("target", *named)
    assert plan["managed_install"] is True
    assert plan["target"] == str(tmp_path / directory)
    assert run_cli("status", *named) == {"target": plan["target"], "installed": [], "sessions": {}}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", [
    ("install", "target-check"), ("preview", "target-check"), ("status",),
    ("rollback", "target-check", "1.0.0"), ("uninstall", "target-check"), ("migrate-state",), ("session-begin", "test"),
    ("session-end", "unused"), ("mcp",),
])
def test_advisory_target_does_not_open_store_or_write(run_cli, tmp_path, monkeypatch, command):
    def unexpected_store(*args):
        pytest.fail("advisory target opened the store")

    monkeypatch.setattr(cli, "open_store", unexpected_store)
    named = ("--harness", "soulsteads", "--scope", "profile", "--root", tmp_path)
    plan = run_cli("target", *named)
    assert plan["target"] == str(tmp_path / "skills")
    assert plan["managed_install"] is False
    assert run_cli(*command, *named, exit_code=64)["error"] == "usage_error"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("symlink_in_root", [True, False])
def test_symlink_target_is_refused(run_cli, tmp_path, symlink_in_root):
    real = tmp_path / "real"
    real.mkdir()
    root = tmp_path / "workspace"
    if symlink_in_root:
        root.symlink_to(real, target_is_directory=True)
    else:
        root.mkdir()
        (root / ".agents").symlink_to(real, target_is_directory=True)
    named = ("--harness", "codex", "--scope", "project", "--root", root)
    assert run_cli("target", *named, exit_code=64)["error"] == "usage_error"
    assert run_cli("status", *named, exit_code=64)["error"] == "usage_error"
    assert list(real.iterdir()) == []


def test_named_root_must_be_an_existing_directory(run_cli, tmp_path):
    file_path = tmp_path / "file"
    file_path.touch()
    for root in ("/", tmp_path / ".." / tmp_path.name, tmp_path / "missing", file_path):
        named = ("--harness", "codex", "--scope", "project", "--root", root)
        assert run_cli("target", *named, exit_code=64)["error"] == "usage_error"
    assert list(tmp_path.iterdir()) == [file_path]


def test_named_and_explicit_targets_share_state(run_cli, tmp_path, monkeypatch):
    store = LocalStore.init(tmp_path / "registry", ["reviewer"])
    first = submit_release(store, "1.0.0", b"First version.\n")
    submit_release(store, "2.0.0", b"Second version.\n")
    approve_release(store, "1.0.0")
    approve_release(store, "2.0.0")
    root = tmp_path / "workspace"
    root.mkdir()
    named = ("--harness", "codex", "--scope", "project", "--root", root)
    target = root / ".agents/skills"
    explicit = ("--target", target)
    registry = ("--store", store.root)
    receipt = run_cli(*registry, "install", "target-check", "--version", "1.0.0", *named)
    assert receipt["version"] == "1.0.0"
    assert pkg.read_tree(target / "target-check") == first
    state = run_cli("status", *explicit)
    assert state["target"] == str(target)
    assert state["installed"][0]["version"] == "1.0.0"
    assert run_cli("status", *named) == state
    session = run_cli("session-begin", "test", *named)["session"]
    assert session in run_cli("status", *explicit)["sessions"]
    assert run_cli("session-end", session, *named) == {"ended": session}
    preview = run_cli(*registry, "preview", "target-check", "--version", "2.0.0", *named)
    assert preview["current"]["version"] == "1.0.0"
    run_cli(*registry, "install", "target-check", "--version", "2.0.0", *explicit)
    assert run_cli("rollback", "target-check", "1.0.0", *named)["version"] == "1.0.0"
    assert pkg.read_tree(target / "target-check") == first

    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "skillstore_status", "arguments": {}}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    result = run_cli(*registry, "mcp", *named)["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == run_cli("status", *explicit)


def test_named_hermes_requires_explicit_migration(run_cli, tmp_path):
    root = tmp_path / "profile"
    target = root / "skills"
    legacy = target / ".skillstore"
    legacy.mkdir(parents=True, mode=0o700)
    named = ("--harness", "hermes", "--scope", "profile", "--root", root)
    explicit = ("--target", target)
    store = LocalStore.init(tmp_path / "registry", ["reviewer"])
    for version in ("1.0.0", "2.0.0"):
        submit_release(store, version, f"Version {version}\n".encode())
        approve_release(store, version)
    args = ("--store", store.root, "install", "target-check", "--version")
    run_cli(*args, "1.0.0", *explicit)
    before = run_cli("status", *explicit)
    assert "migrate-state" in run_cli("status", *named, exit_code=3)["message"]
    assert "migrate-state" in run_cli(*args, "2.0.0", *named, exit_code=3)["message"]
    assert not (root / ".skillstore-skills").exists()
    assert run_cli("migrate-state", *named)["state"] == str(root / ".skillstore-skills")
    assert not legacy.exists()
    assert run_cli("status", *named) == before
    run_cli(*args, "2.0.0", *named)
    assert run_cli("rollback", "target-check", "1.0.0", *named)["version"] == "1.0.0"
