from pathlib import Path
from types import SimpleNamespace

import pytest

from skillstore.errors import UsageError


def launcher(tmp_path):
    path = tmp_path / "bin" / "soulsteads-skillstore"
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_service_install_parser_defaults_to_user():
    from skillstore.cli import parser

    args = parser().parse_args(["--store", "/registry", "service", "install", "--dry-run"])

    assert (args.command, args.service_command, args.scope, args.dry_run) == (
        "service", "install", "user", True,
    )


def test_user_service_preview_uses_xdg_and_private_loopback_listener(tmp_path):
    from skillstore.service import plan_install

    store = tmp_path / "registry"
    store.mkdir()
    config = tmp_path / "xdg"
    plan = plan_install(
        store=str(store), scope="user", run_as=None, executable=str(launcher(tmp_path)),
        config_home=str(config),
    )

    assert plan.path == config / "systemd" / "user" / "soulsteads-agent-skillstore.service"
    assert not plan.path.exists()
    assert 'ExecStart="' + str(launcher(tmp_path)) + '"' in plan.unit
    assert '"--store" "' + str(store) + '" "serve"' in plan.unit
    assert '"--host" "127.0.0.1" "--port" "8765" "--private-read"' in plan.unit
    assert "User=" not in plan.unit
    assert "SKILLSTORE_TOKEN" not in plan.unit


def test_user_install_writes_once_and_refuses_a_symlink_conflict(tmp_path):
    from skillstore.service import install

    store = tmp_path / "registry"
    store.mkdir()
    config = tmp_path / "xdg"
    path = install(
        store=str(store), scope="user", run_as=None, executable=str(launcher(tmp_path)),
        config_home=str(config), dry_run=False,
    ).path
    assert path.is_file() and not path.is_symlink()
    before = path.read_text(encoding="utf-8")

    with pytest.raises(UsageError, match="already exists"):
        install(
            store=str(store), scope="user", run_as=None, executable=str(launcher(tmp_path)),
            config_home=str(config), dry_run=False,
        )
    assert path.read_text(encoding="utf-8") == before


def test_user_install_never_replaces_a_preexisting_symlink(tmp_path):
    from skillstore.service import UNIT_NAME, install

    store = tmp_path / "registry"
    store.mkdir()
    config = tmp_path / "xdg"
    unit_dir = config / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("preserve me", encoding="utf-8")
    final = unit_dir / UNIT_NAME
    final.symlink_to(sentinel)

    with pytest.raises(UsageError, match="already exists"):
        install(str(store), "user", None, str(launcher(tmp_path)), str(config))
    assert final.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "preserve me"


def test_service_refuses_missing_store_unsafe_launcher_and_root_system_account(tmp_path):
    from skillstore.service import plan_install

    missing = tmp_path / "missing"
    with pytest.raises(UsageError, match="existing directory"):
        plan_install(str(missing), "user", None, str(launcher(tmp_path)), str(tmp_path / "xdg"))

    store = tmp_path / "registry"
    store.mkdir()
    unsafe = tmp_path / "source-launcher"
    unsafe.write_text("not executable", encoding="utf-8")
    with pytest.raises(UsageError, match="executable regular file"):
        plan_install(str(store), "user", None, str(unsafe), str(tmp_path / "xdg"))
    with pytest.raises(UsageError, match="non-root"):
        plan_install(str(store), "system", "root", str(launcher(tmp_path)), str(tmp_path / "xdg"))


def test_system_unit_requires_named_nonroot_account(tmp_path):
    from skillstore.service import plan_install

    store = tmp_path / "registry"
    store.mkdir()
    with pytest.raises(UsageError, match="--run-as"):
        plan_install(str(store), "system", None, str(launcher(tmp_path)), str(tmp_path / "xdg"))


def test_invoked_executable_resolves_a_uv_style_path_shim(tmp_path, monkeypatch):
    from skillstore.service import invoked_executable

    target = launcher(tmp_path)
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    shim = shim_dir / "soulsteads-skillstore"
    shim.symlink_to(target)
    monkeypatch.setenv("PATH", str(shim_dir))

    assert invoked_executable("soulsteads-skillstore") == target.resolve()


def test_unit_escapes_systemd_dollar_and_percent_specifiers(tmp_path):
    from skillstore.service import plan_install

    store = tmp_path / "registry$private%h"
    store.mkdir()
    executable = tmp_path / "bin" / "soulsteads$private%h"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    plan = plan_install(str(store), "user", None, str(executable), str(tmp_path / "xdg"))

    assert "$$private%%h" in plan.unit


def test_install_refuses_symlinked_destination_parent(tmp_path):
    from skillstore.errors import UsageError
    from skillstore.service import UNIT_NAME, install

    store = tmp_path / "registry"
    store.mkdir()
    config = tmp_path / "xdg"
    config.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (config / "systemd").symlink_to(redirected, target_is_directory=True)

    with pytest.raises(UsageError, match="symlink"):
        install(str(store), "user", None, str(launcher(tmp_path)), str(config))
    assert not (redirected / "user" / UNIT_NAME).exists()


def test_failed_write_leaves_no_final_unit(tmp_path, monkeypatch):
    from skillstore import service

    store = tmp_path / "registry"
    store.mkdir()
    config = tmp_path / "xdg"
    monkeypatch.setattr(service.os, "write", lambda *_: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        service.install(str(store), "user", None, str(launcher(tmp_path)), str(config))
    assert not (config / "systemd" / "user" / service.UNIT_NAME).exists()


def test_real_system_install_requires_root_but_dry_run_does_not(tmp_path, monkeypatch):
    from skillstore import service

    store = tmp_path / "registry"
    store.mkdir()
    monkeypatch.setattr(service, "SYSTEM_UNIT_DIR", tmp_path / "etc" / "systemd" / "system")
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(service.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1000))

    with pytest.raises(UsageError, match="must be run as root"):
        service.install(str(store), "system", "worker", str(launcher(tmp_path)))
    assert not (service.SYSTEM_UNIT_DIR / service.UNIT_NAME).exists()
    assert service.install(str(store), "system", "worker", str(launcher(tmp_path)), dry_run=True).path == (
        service.SYSTEM_UNIT_DIR / service.UNIT_NAME
    )


def test_real_user_install_rejects_root_but_dry_run_does_not(tmp_path, monkeypatch):
    from skillstore import service

    store = tmp_path / "registry"
    store.mkdir()
    config = tmp_path / "xdg"
    monkeypatch.setattr(service.os, "geteuid", lambda: 0)

    with pytest.raises(UsageError, match="must not run as root"):
        service.install(str(store), "user", None, str(launcher(tmp_path)), str(config))
    assert not (config / "systemd" / "user" / service.UNIT_NAME).exists()
    assert service.install(str(store), "user", None, str(launcher(tmp_path)), str(config), dry_run=True).path == (
        config / "systemd" / "user" / service.UNIT_NAME
    )
