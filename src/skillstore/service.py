"""Safe, opt-in systemd unit rendering for a local Skillstore listener.

The installer writes a unit only.  It never initializes, opens, or starts the
registry, and deliberately leaves systemctl manager actions to the operator.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import secrets

from .errors import UsageError


UNIT_NAME = "soulsteads-agent-skillstore.service"
SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
USER_UNIT_RELATIVE = Path("systemd/user")
_SERVICE_ACCOUNT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,31}\Z")


@dataclass(frozen=True)
class ServicePlan:
    """A rendered unit and the only path the installer may create."""

    path: Path
    unit: str


def _quoted(value: str) -> str:
    """Return one systemd ExecStart argument without invoking a shell."""
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise UsageError("service arguments must be non-empty printable paths")
    return '"' + (value.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("$", "$$").replace("%", "%%")) + '"'


def _store_path(raw: str | None) -> Path:
    if not raw:
        raise UsageError("service install requires an explicit absolute --store path")
    path = Path(raw)
    if not path.is_absolute():
        raise UsageError("service --store path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise UsageError("service --store path must be an existing directory") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise UsageError("service --store path must be an existing directory, not a symlink")
    return path.resolve(strict=True)


def _launcher(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise UsageError("installed service launcher must be an absolute path")
    try:
        target = path.resolve(strict=True)
        metadata = target.stat()
    except OSError as exc:
        raise UsageError("installed service launcher is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(target, os.X_OK):
        raise UsageError("installed service launcher must be an executable regular file")
    return target


def invoked_executable(argv0: str) -> Path:
    """Resolve the installed console script, including uv's symlinked shim."""
    candidate = Path(argv0)
    if not candidate.is_absolute():
        if candidate.parent != Path("."):
            raise UsageError("service install must not use a relative launcher path")
        located = shutil.which(argv0)
        if located is None:
            raise UsageError("installed service launcher is unavailable on PATH")
        candidate = Path(located)
    return _launcher(str(candidate))


def _service_account(account: str | None) -> str:
    if not account:
        raise UsageError("system service install requires --run-as NON_ROOT_USER")
    if not _SERVICE_ACCOUNT.fullmatch(account):
        raise UsageError("--run-as must be a local account name")
    try:
        entry = pwd.getpwnam(account)
    except KeyError as exc:
        raise UsageError("--run-as must name an existing local account") from exc
    if entry.pw_uid == 0:
        raise UsageError("system service install requires a non-root --run-as account")
    return account


def _user_unit_dir(config_home: str | None) -> Path:
    raw = config_home or os.environ.get("XDG_CONFIG_HOME")
    if raw is None:
        raw = str(Path.home() / ".config")
    path = Path(raw)
    if not path.is_absolute():
        raise UsageError("XDG_CONFIG_HOME must be absolute for service install")
    return path / USER_UNIT_RELATIVE


def _unit(path: Path, store: Path, launcher: Path, scope: str, run_as: str | None) -> str:
    command = " ".join(_quoted(str(value)) for value in (
        launcher, "--store", store, "serve", "--host", "127.0.0.1", "--port", "8765", "--private-read",
    ))
    lines = [
        "[Unit]",
        "Description=Soulsteads Agent Skillstore private registry listener",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
    ]
    if scope == "system":
        lines.append(f"User={run_as}")
    lines.extend((
        f"ExecStart={command}",
        "Restart=on-failure",
        "RestartSec=5s",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "",
        "[Install]",
        "WantedBy=" + ("multi-user.target" if scope == "system" else "default.target"),
        "",
    ))
    return "\n".join(lines)


def plan_install(store: str | None, scope: str, run_as: str | None, executable: str,
                 config_home: str | None = None) -> ServicePlan:
    """Validate inert inputs and render the unit without touching systemd or a store."""
    registry = _store_path(store)
    launcher = _launcher(executable)
    if scope == "user":
        if run_as is not None:
            raise UsageError("--run-as is only valid with --system")
        directory = _user_unit_dir(config_home)
    elif scope == "system":
        run_as = _service_account(run_as)
        directory = SYSTEM_UNIT_DIR
    else:
        raise UsageError("service scope must be user or system")
    path = directory / UNIT_NAME
    return ServicePlan(path=path, unit=_unit(path, registry, launcher, scope, run_as))


def _open_destination_directory(path: Path) -> int:
    """Open or create an absolute directory one non-symlink component at a time."""
    path = Path(os.path.abspath(path))
    if not path.is_absolute():
        raise UsageError("service unit destination must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise UsageError("service unit destination must not traverse symlinks or non-directories") from exc
            os.close(directory_fd)
            directory_fd = child_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _write_new(path: Path, content: str) -> None:
    """Publish a fully synced unit without replacing a file or following a path link."""
    parent_fd = _open_destination_directory(path.parent)
    temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    temporary_created = False
    try:
        fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        temporary_created = True
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise UsageError("refusing non-regular service unit destination")
            data = content.encode("utf-8")
            total = 0
            while total < len(data):
                total += os.write(fd, data[total:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temporary_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False)
        except FileExistsError as exc:
            raise UsageError("service unit already exists; refusing to overwrite it") from exc
        os.unlink(temporary_name, dir_fd=parent_fd)
        temporary_created = False
        os.fsync(parent_fd)
    finally:
        if temporary_created:
            os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)


def _require_write_privileges(scope: str) -> None:
    """Keep real system and user writes in the privilege domain systemd expects."""
    euid = os.geteuid()
    if scope == "system" and euid != 0:
        raise UsageError("system service installation must be run as root")
    if scope == "user" and euid == 0:
        raise UsageError("user service installation must not run as root")


def install(store: str | None, scope: str, run_as: str | None, executable: str,
            config_home: str | None = None, dry_run: bool = False) -> ServicePlan:
    plan = plan_install(store, scope, run_as, executable, config_home)
    if not dry_run:
        _require_write_privileges(scope)
        _write_new(plan.path, plan.unit)
    return plan
