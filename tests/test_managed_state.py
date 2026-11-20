from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from skillstore import package as pkg
from skillstore.errors import Conflict, NotFound
from skillstore.install import Installation
from skillstore.store import LocalStore, make_actor


@pytest.fixture
def registry(tmp_path):
    store = LocalStore.init(tmp_path / "registry", ["reviewer"])
    for version in ("1.0.0", "2.0.0"):
        tree = {"SKILL.md": (f"---\nname: demo\ndescription: Demo test skill.\n---\n{version}\n".encode(), False)}
        store.submit(pkg.pack(tree), version, make_actor("author"))
        store.review("demo", version, "approve", make_actor("reviewer"), "Test approval")
    return store


def test_unused_status_does_not_create_paths(tmp_path):
    target = tmp_path / "parent" / "skills"
    assert Installation(target).status() == {"target": str(target), "installed": [], "sessions": {}}
    assert not target.parent.exists()


def test_external_history_and_offline_remove_restore(tmp_path, registry):
    target = tmp_path / "skills"
    install = Installation(target)
    first = install.install(registry, "demo", "1.0.0")
    second = install.install(registry, "demo", "2.0.0")
    assert list(target.iterdir()) == [target / "demo"]
    assert install.meta.parent == target.parent
    assert list(install.meta.glob("backup-*/SKILL.md"))
    session = install.session_begin("in use")["session"]
    with pytest.raises(Conflict, match="active registered sessions"):
        install.uninstall("demo")
    install.session_end(session)
    assert install.uninstall("demo")["digest"] == second["digest"]
    assert not list(target.iterdir())
    assert install.status()["installed"] == []
    assert install.rollback("demo", "1.0.0")["digest"] == first["digest"]
    assert install.rollback("demo", "2.0.0")["digest"] == second["digest"]
    installed = target / "demo" / "SKILL.md"
    installed.write_text("local changes")
    with pytest.raises(Conflict, match="local changes"):
        install.uninstall("demo")
    assert installed.read_text() == "local changes"
    with pytest.raises(NotFound):
        install.uninstall("unknown")


def test_explicit_migration_retains_legacy_lock_and_history(tmp_path, registry):
    root = tmp_path / "skills"
    legacy = root / ".skillstore"
    legacy.mkdir(parents=True, mode=0o700)
    target = Installation(root)
    first = target.install(registry, "demo", "1.0.0")
    target.install(registry, "demo", "2.0.0")
    assert target.meta == legacy
    lock_ino = (legacy / "lock").stat().st_ino
    token = target.session_begin("busy")["session"]
    with pytest.raises(Conflict, match="active registered sessions"):
        target.migrate_state()
    assert target.meta == legacy
    target.session_end(token)
    migrated = target.migrate_state()
    assert not legacy.exists()
    assert migrated["state"] == str(target.meta)
    assert (target.meta / "lock").stat().st_ino == lock_ino
    assert Installation(root).migrate_state() == migrated
    assert Installation(root).rollback("demo", "1.0.0")["digest"] == first["digest"]


def test_remove_recovers_after_rename(tmp_path, registry, monkeypatch):
    import skillstore.install as module

    install = Installation(tmp_path / "skills")
    install.install(registry, "demo", "1.0.0")
    write = module.write_json

    def fail(path, data):
        if Path(path).name == "state.json":
            raise OSError("test failure before receipt")
        write(path, data)

    with monkeypatch.context() as m:
        m.setattr(module, "write_json", fail)
        with pytest.raises(OSError, match="before receipt"):
            install.uninstall("demo")
    assert not (install.root / "demo").exists()
    restarted = Installation(install.root)
    assert restarted.status()["installed"] == []
    assert restarted.rollback("demo", "1.0.0")["version"] == "1.0.0"


@pytest.mark.parametrize("cut", ["pre-rename", "post-rename"])
def test_migration_can_retry_on_either_side_of_rename(tmp_path, registry, monkeypatch, cut):
    import skillstore.install as module

    root = tmp_path / "skills"
    legacy = root / ".skillstore"
    legacy.mkdir(parents=True, mode=0o700)
    install = Installation(root)
    original = install.install(registry, "demo", "1.0.0")
    rename = module.os.rename

    def fail(src, dst):
        assert src == legacy and dst == install.external
        if cut == "post-rename":
            rename(src, dst)
        raise OSError("test cut at migration rename")

    with monkeypatch.context() as m:
        m.setattr(module.os, "rename", fail)
        with pytest.raises(OSError, match="test cut"):
            install.migrate_state()
    assert legacy.exists() is (cut == "pre-rename")
    assert install.external.exists() is (cut == "post-rename")
    restart = Installation(root)
    assert restart.migrate_state()["state"] == str(install.external)
    assert restart.status()["installed"] == [{**original, "local": "clean"}]


def test_waiter_cannot_use_detached_legacy_lock(tmp_path, registry, monkeypatch):
    import skillstore.install as module

    root = tmp_path / "skills"
    legacy = root / ".skillstore"
    legacy.mkdir(parents=True, mode=0o700)
    install = Installation(root)
    install.install(registry, "demo", "1.0.0")
    flock = module.fcntl.flock

    def move_while_waiting(fd, op):
        legacy.rename(install.external)
        flock(fd, op)

    with monkeypatch.context() as m:
        m.setattr(module.fcntl, "flock", move_while_waiting)
        with pytest.raises(Conflict, match="while waiting"):
            install.session_begin("must not write")
    assert Installation(root).status()["sessions"] == {}
    assert not legacy.exists()


def test_split_state_refuses_to_select_or_migrate(tmp_path, registry):
    root = tmp_path / "skills"
    target = Installation(root)
    target.install(registry, "demo", "1.0.0")
    before = (target.meta / "state.json").read_bytes()
    legacy = root / ".skillstore"
    legacy.mkdir(mode=0o700)
    with pytest.raises(Conflict, match="both legacy and external"):
        target.migrate_state()
    assert list(legacy.iterdir()) == []
    assert (target.external / "state.json").read_bytes() == before


def test_migration_refuses_legacy_pending_transaction(tmp_path, registry, monkeypatch):
    root = tmp_path / "skills"
    legacy = root / ".skillstore"
    legacy.mkdir(parents=True, mode=0o700)
    target = Installation(root)
    target.install(registry, "demo", "1.0.0")
    with monkeypatch.context() as m:
        def fail(*args):
            raise OSError("before upgrade publish")

        m.setattr("skillstore.install.os.rename", fail)
        with pytest.raises(OSError, match="before upgrade publish"):
            target.install(registry, "demo", "2.0.0")
    journal = legacy / "transaction.json"
    before = journal.read_bytes()
    with pytest.raises(Conflict, match="finish the pending transaction"):
        target.migrate_state()
    assert journal.read_bytes() == before
    assert not target.external.exists()
    assert target.status()["installed"][0]["version"] == "2.0.0"
    target.migrate_state()
    assert target.rollback("demo", "1.0.0")["version"] == "1.0.0"


def test_external_backup_restores_to_empty_parent(tmp_path, registry):
    target = Installation(tmp_path / "skills")
    target.install(registry, "demo", "1.0.0")
    target.install(registry, "demo", "2.0.0")
    target.uninstall("demo")
    restored = Installation(tmp_path / "restore" / target.root.name)
    shutil.copytree(target.root, restored.root)
    shutil.copytree(target.meta, restored.meta)
    assert restored.status()["removed"][0]["version"] == "2.0.0"
    assert restored.rollback("demo", "1.0.0")["version"] == "1.0.0"


@pytest.mark.parametrize("kind", ["symlink", "file", "public-directory"])
def test_invalid_sibling_state_refuses_without_mutation(tmp_path, registry, kind):
    target = Installation(tmp_path / "skills")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    if kind == "symlink":
        target.external.symlink_to(foreign, target_is_directory=True)
    elif kind == "file":
        target.external.write_text("keep me")
    else:
        target.external.mkdir()
        target.external.chmod(0o755)
    for operation in (target.status, target.migrate_state, lambda: target.install(registry, "demo", "1.0.0")):
        with pytest.raises(Conflict):
            operation()
    assert list(foreign.iterdir()) == []
    assert not target.root.exists()
    if kind == "file":
        assert target.external.read_text() == "keep me"


def test_removal_receipt_survives_committed_journal_retry(tmp_path, registry, monkeypatch):
    target = Installation(tmp_path / "skills")
    target.install(registry, "demo", "1.0.0")
    journal = target.meta / "transaction.json"
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == journal:
            raise OSError("cut after receipt")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", fail)
        with pytest.raises(OSError, match="cut after receipt"):
            target.uninstall("demo")
    receipt = (target.meta / "state.json").read_bytes()
    assert journal.exists()
    restart = Installation(target.root)
    assert len(restart.status()["removed"][0]["history"]) == 1
    assert (target.meta / "state.json").read_bytes() == receipt
    assert not journal.exists()
    assert restart.rollback("demo", "1.0.0")["version"] == "1.0.0"


def test_cross_device_migration_leaves_legacy_state_in_place(tmp_path, registry, monkeypatch):
    root = tmp_path / "skills"
    legacy = root / ".skillstore"
    legacy.mkdir(parents=True, mode=0o700)
    install = Installation(root)
    install.install(registry, "demo", "1.0.0")
    expected = install.status()
    stat = Path.stat

    def mounted(path, *args, **kwargs):
        if path == legacy and kwargs.get("follow_symlinks", True):
            return SimpleNamespace(st_dev=stat(path).st_dev + 1)
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "stat", mounted)
        with pytest.raises(Conflict, match="same filesystem"):
            install.migrate_state()
    assert not install.external.exists()
    assert install.status() == expected
