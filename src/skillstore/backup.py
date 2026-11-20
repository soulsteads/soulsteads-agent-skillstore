"""Private, local-only backups of the ``skills`` subtree of a Hermes profile.

The caller supplies an already-quiescent, explicit profile directory.  This module
binds source and destination directories through file descriptors and archives
only regular files and directories below ``profile_root/skills``.  It is not a
cross-machine snapshot protocol and does not attempt to make a hostile,
same-UID source tree atomic.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


_MAX_FILES = 10_000
_MAX_ENTRIES = 20_000
_MAX_DEPTH = 32
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_OUTPUT_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


@dataclass
class _ArchiveBudget:
    files: int = 0
    entries: int = 0
    total_bytes: int = 0


def _absolute_components(path: str, label: str) -> tuple[str, tuple[str, ...]]:
    """Return a strict absolute spelling suitable for descriptor walking."""
    if not isinstance(path, str) or not path or not path.startswith("/"):
        raise ValueError(f"{label} must be an absolute path")
    if path == "/" or path.endswith("/") or "//" in path:
        raise ValueError(f"{label} must use a canonical absolute path")
    components = tuple(path[1:].split("/"))
    if any(component in ("", ".", "..") for component in components):
        raise ValueError(f"{label} must use a canonical absolute path")
    return path, components


def _open_absolute_directory(path: str, label: str) -> int:
    """Open every component of *path* without following an ancestor symlink."""
    _, components = _absolute_components(path, label)
    current = os.open("/", _DIR_FLAGS)
    try:
        for component in components:
            child = os.open(component, _DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _tar_info(name: str, source: os.stat_result, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = source.st_mode & 0o777
    info.mtime = int(source.st_mtime)
    info.uid = 0
    info.gid = 0
    if kind == tarfile.REGTYPE:
        info.size = source.st_size
    return info


def _add_tree(
    archive: tarfile.TarFile,
    directory_fd: int,
    archive_name: str,
    budget: _ArchiveBudget,
    depth: int = 0,
) -> None:
    """Copy a descriptor-held directory recursively, rejecting unsafe entries."""
    names = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            # Legacy lifecycle metadata is not part of a skills-only backup.
            if depth == 0 and entry.name == ".skillstore":
                continue
            budget.entries += 1
            if budget.entries > _MAX_ENTRIES or depth >= _MAX_DEPTH:
                raise ValueError("skills tree exceeds archive safety limit")
            names.append(entry.name)
    for name in sorted(names):
        if any(c in "\\:" or ord(c) < 32 or 0x7f <= ord(c) <= 0x9f or 0xd800 <= ord(c) <= 0xdfff for c in name):
            raise ValueError("unsafe archive member name")
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        child_name = f"{archive_name}/{name}"
        mode = before.st_mode
        if stat.S_ISDIR(mode):
            child_fd = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if not stat.S_ISDIR(opened.st_mode) or not _same_object(before, opened):
                    raise ValueError(f"source entry changed while archiving: {child_name}")
                archive.addfile(_tar_info(child_name, opened, tarfile.DIRTYPE))
                _add_tree(archive, child_fd, child_name, budget, depth + 1)
                if not _same_object(opened, os.stat(name, dir_fd=directory_fd, follow_symlinks=False)):
                    raise ValueError(f"source entry changed while archiving: {child_name}")
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(mode) or before.st_nlink != 1:
            raise ValueError(f"unsupported source entry: {child_name}")
        if before.st_size > _MAX_FILE_BYTES:
            raise ValueError(f"source file exceeds size limit: {child_name}")
        if budget.files >= _MAX_FILES or budget.total_bytes + before.st_size > _MAX_TOTAL_BYTES:
            raise ValueError("skills tree exceeds archive safety limit")

        child_fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
        try:
            opened = os.fstat(child_fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or not _same_object(before, opened):
                raise ValueError(f"source entry changed while archiving: {child_name}")
            reader = io.FileIO(child_fd, mode="rb", closefd=False)
            try:
                archive.addfile(_tar_info(child_name, opened, tarfile.REGTYPE), reader)
            finally:
                reader.close()
            if (not _same_object(opened, os.fstat(child_fd)) or
                    not _same_object(opened, os.stat(name, dir_fd=directory_fd, follow_symlinks=False))):
                raise ValueError(f"source entry changed while archiving: {child_name}")
            budget.files += 1
            budget.total_bytes += opened.st_size
        finally:
            os.close(child_fd)


def _sha256_from_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _directory_key(fd):
    st = os.fstat(fd)
    return st.st_dev, st.st_ino


def _require_current_directory(path, fd):
    current = _open_absolute_directory(path, "backup directory binding")
    try:
        if _directory_key(current) != _directory_key(fd):
            raise ValueError("backup directory binding changed while archiving")
    finally:
        os.close(current)


def _private_directory(fd):
    st = os.fstat(fd)
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise ValueError("output parent must be an operator-owned private directory")


class HermesBackup:
    """Bind the operator's configured directories, not MCP caller paths."""

    def __init__(self, profile_root, destination):
        self.profile, _ = _absolute_components(profile_root, "profile_root")
        self.destination, _ = _absolute_components(destination, "backup directory")
        if self.destination == self.profile or self.destination.startswith(self.profile + "/"):
            raise ValueError("output must not be within profile_root")
        source_fd = _open_absolute_directory(self.profile, "profile_root")
        try:
            destination_fd = _open_absolute_directory(self.destination, "backup directory")
            try:
                _private_directory(destination_fd)
                skills_fd = os.open("skills", _DIR_FLAGS, dir_fd=source_fd)
                try:
                    self._keys = (_directory_key(source_fd), _directory_key(destination_fd), _directory_key(skills_fd))
                finally:
                    os.close(skills_fd)
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)

    def create(self):
        date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = f"{self.destination}/hermes-skills-{date}-{uuid.uuid4().hex}.tar.gz"
        return _backup_hermes(self.profile, output, keys=self._keys)


def backup_hermes(profile_root: str, output: str) -> dict:
    """Create an owner-private gzip tar containing exactly ``profile_root/skills``.

    Both arguments must be strict absolute paths.  ``profile_root`` and the
    destination parent are opened through no-follow descriptor walks; the
    destination must already exist, belong to the effective user, and have no
    group or other permissions.  Source files must be regular and singly
    linked. Existing outputs are never overwritten. Both trees are trusted
    local directories; concurrent modification by the same OS user is outside
    the trust boundary. Failed writes may leave a hidden owner-private partial.

    The source is assumed quiescent while this function runs.  Descriptor
    checks reject link/special-file entries and observed replacements, but this
    is deliberately not represented as a hostile-same-UID atomic snapshot.
    """
    return _backup_hermes(profile_root, output)


def _backup_hermes(profile_root: str, output: str, *, keys=None) -> dict:
    source_path, _ = _absolute_components(profile_root, "profile_root")
    output_path, _ = _absolute_components(output, "output")
    if output_path.startswith(source_path + "/") or output_path == source_path:
        raise ValueError("output must not be within profile_root")

    destination_path = output_path.rsplit("/", 1)[0] or "/"
    output_name = output_path.rsplit("/", 1)[1]
    source_fd = _open_absolute_directory(source_path, "profile_root")
    destination_fd = -1
    skills_fd = -1
    output_fd = -1
    try:
        destination_fd = _open_absolute_directory(destination_path, "output parent")
        _private_directory(destination_fd)


        skills_before = os.stat("skills", dir_fd=source_fd, follow_symlinks=False)
        if not stat.S_ISDIR(skills_before.st_mode):
            raise ValueError("profile_root/skills must be an existing directory")
        skills_fd = os.open("skills", _DIR_FLAGS, dir_fd=source_fd)
        skills_opened = os.fstat(skills_fd)
        if not _same_object(skills_before, skills_opened):
            raise ValueError("profile_root/skills changed while opening")
        if keys is not None and keys != (_directory_key(source_fd), _directory_key(destination_fd), _directory_key(skills_fd)):
            raise ValueError("configured backup directories changed; check them before restarting MCP")

        try:
            os.stat(output_name, dir_fd=destination_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("backup output already exists")

        partial = f".skillstore-backup-{uuid.uuid4().hex}.partial"
        output_fd = os.open(partial, _OUTPUT_FLAGS, 0o600, dir_fd=destination_fd)
        created = os.fstat(output_fd)
        if not stat.S_ISREG(created.st_mode) or created.st_nlink != 1:
            raise ValueError("created output is not a private regular file")
        os.fchmod(output_fd, 0o600)
        writer = io.FileIO(output_fd, mode="w+b", closefd=False)
        try:
            with tarfile.open(fileobj=writer, mode="w:gz") as archive:
                archive.addfile(_tar_info("skills", skills_opened, tarfile.DIRTYPE))
                budget = _ArchiveBudget()
                _add_tree(archive, skills_fd, "skills", budget)
        finally:
            writer.close()
        os.fsync(output_fd)
        written = os.fstat(output_fd)
        archive_sha256 = _sha256_from_fd(output_fd)
        _require_current_directory(source_path, source_fd)
        if not _same_object(skills_opened, os.stat("skills", dir_fd=source_fd, follow_symlinks=False)):
            raise ValueError("profile_root/skills changed while archiving")
        _require_current_directory(destination_path, destination_fd)
        _private_directory(destination_fd)
        if (written.st_nlink != 1 or stat.S_IMODE(written.st_mode) != 0o600 or
                not _same_object(written, os.stat(partial, dir_fd=destination_fd, follow_symlinks=False))):
            raise ValueError("backup staging file changed while archiving")
        # Same directory, atomic no-replace. A failure before linking never
        # exposes an incomplete archive under its final name.
        os.link(partial, output_name, src_dir_fd=destination_fd, dst_dir_fd=destination_fd, follow_symlinks=False)
        named_output = os.stat(output_name, dir_fd=destination_fd, follow_symlinks=False)
        if named_output.st_nlink != 2 or not _same_object(os.fstat(output_fd), named_output):
            raise ValueError("output changed while publishing backup")
        # Cleanup is only on the success path in an operator-owned private directory.
        os.unlink(partial, dir_fd=destination_fd)
        os.fsync(destination_fd)
        _require_current_directory(destination_path, destination_fd)
        _private_directory(destination_fd)
        named_output = os.stat(output_name, dir_fd=destination_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(named_output.st_mode)
            or named_output.st_nlink != 1
            or stat.S_IMODE(named_output.st_mode) != 0o600
            or (created.st_dev, created.st_ino) != (named_output.st_dev, named_output.st_ino)
            or (written.st_size, written.st_mtime_ns) != (named_output.st_size, named_output.st_mtime_ns)
        ):
            raise ValueError("output changed while archiving")
        return {"archive": output_path, "sha256": archive_sha256, "files": budget.files}
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        if skills_fd >= 0:
            os.close(skills_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)
