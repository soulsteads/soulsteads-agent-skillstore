"""Operator-scoped reads for MCP contribution tools; never follow source links."""
from contextlib import contextmanager
import os
from pathlib import PurePosixPath
import stat

from . import package as pkg
from .errors import UsageError, ValidationFailed

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_MAX_DEPTH = 32
_MAX_ENTRIES = 2000


def _parts(value, *, absolute=False):
    p = PurePosixPath(value)
    if (p.is_absolute() != absolute or p.as_posix() != value or not value
            or value.startswith('//') or not any(p.parts[absolute:])
            or any(part in {'.', '..'} for part in p.parts)
            or '\\' in value or any(ord(char) < 32 for char in value)):
        raise UsageError('expected a canonical absolute source root' if absolute else 'expected a relative package directory below the configured source root')
    parts = p.parts[absolute:]
    if not absolute and (len(parts) > _MAX_DEPTH or any(part.startswith('.') for part in parts)):
        raise UsageError('hidden or deeply nested source paths are not allowed')
    return parts


def _directory_identity(st):
    return st.st_dev, st.st_ino


def _file_identity(st):
    return st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns


@contextmanager
def _directory(parts):
    fd = os.open('/', _DIR)
    try:
        chain = [_directory_identity(os.fstat(fd))]
        for part in parts:
            child = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
            chain.append(_directory_identity(os.fstat(fd)))
        yield fd, tuple(chain)
    except OSError as exc:
        raise ValidationFailed('source directory is unavailable or contains an unsafe path component') from exc
    finally:
        os.close(fd)


def _read_file(parent, name, rel, before, budget):
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValidationFailed(f'unsupported source file: {rel}')
    if before.st_size > min(pkg.MAX_FILE_BYTES, budget):
        raise ValidationFailed('source package exceeds size limits')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, 'rb') as stream:
        expected = _file_identity(before)
        if _file_identity(os.fstat(fd)) != expected:
            raise ValidationFailed(f'source file changed: {rel}')
        data = stream.read(before.st_size + 1)
        if len(data) != before.st_size or _file_identity(os.fstat(fd)) != expected:
            raise ValidationFailed(f'source file changed: {rel}')
    return data, bool(before.st_mode & 0o111)


class ContributionSource:
    """A startup-selected staging root. The tool caller cannot supply another root."""

    def __init__(self, root):
        self.parts = _parts(root, absolute=True)
        with _directory(self.parts) as (_, chain):
            self.chain = chain

    def package(self, path, expected_name):
        relative = _parts(path)
        files = {}
        identities = {}
        used = 0
        seen = 0

        def scan(fd, prefix, read):
            nonlocal used, seen
            with os.scandir(fd) as entries:
                for entry in entries:
                    name = entry.name
                    rel = (*prefix, name)
                    label = '/'.join(rel)
                    seen += 1
                    if seen > _MAX_ENTRIES or len(rel) > _MAX_DEPTH:
                        raise ValidationFailed('source package exceeds directory limits')
                    _parts(label)
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(st.st_mode):
                        child = os.open(name, _DIR, dir_fd=fd)
                        try:
                            ident = ('directory', *_directory_identity(st))
                            if _directory_identity(os.fstat(child)) != ident[1:]:
                                raise ValidationFailed(f'source directory changed: {label}')
                            record(label, ident, read)
                            scan(child, rel, read)
                        finally:
                            os.close(child)
                    else:
                        record(label, ('file', *_file_identity(st)), read)
                        if read:
                            if len(files) >= pkg.MAX_FILES:
                                raise ValidationFailed('source package exceeds file count limit')
                            files[label] = _read_file(fd, name, label, st, pkg.MAX_TOTAL_BYTES - used)
                            used += len(files[label][0])

        rechecked = set()

        def record(label, identity, read):
            if read:
                identities[label] = identity
            else:
                if identities.get(label) != identity:
                    raise ValidationFailed(f'source package changed: {label}')
                rechecked.add(label)

        with _directory(self.parts + relative) as (fd, chain):
            if chain[:len(self.chain)] != self.chain:
                raise ValidationFailed('configured source root changed; restart MCP after checking the root')
            scan(fd, (), True)
        pkg.require_valid(files, expected_name=expected_name)
        packed = pkg.pack(files)
        seen = 0
        with _directory(self.parts + relative) as (fd, final_chain):
            if chain != final_chain:
                raise ValidationFailed('source path changed while reading package')
            scan(fd, (), False)
        if rechecked != identities.keys():
            raise ValidationFailed('source package inventory changed')
        return packed
