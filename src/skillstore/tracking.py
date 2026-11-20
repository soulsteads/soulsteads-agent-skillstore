"""Local snapshot history; never exposes archives as registry releases."""

from datetime import datetime, timezone
import os
import re
import stat

from . import backup
from .errors import IntegrityError, UsageError
from .restore import _MAX_TAR_BYTES, _restore_archive

_PREFIX = 'snapshot-'
_SUFFIX = '.tar.gz'
_ID = re.compile(r'([0-9]{8}T[0-9]{12}Z)-([0-9a-f]{64})')
_STAMP_FORMAT = '%Y%m%dT%H%M%S%fZ'
_MAX_DIRECTORY_ENTRIES = 20_000


def _disjoint(left, right):
    if left == right or left.startswith(right + '/') or right.startswith(left + '/'):
        raise UsageError('the snapshot directory must be separate from skills and recovery directories')


def _archive_name(snapshot):
    return _PREFIX + snapshot + _SUFFIX


def _snapshot_parts(snapshot):
    match = _ID.fullmatch(snapshot)
    if match is not None:
        try:
            stamp = datetime.strptime(match[1], _STAMP_FORMAT).replace(tzinfo=timezone.utc)
            return stamp.isoformat(), match[2]
        except ValueError:
            pass
    raise UsageError('use an exact snapshot ID from tracking list')


def create_snapshot(directory, skills_root):
    backup._absolute_components(directory, 'snapshot directory')
    backup._absolute_components(skills_root, 'skills root')
    _disjoint(directory, skills_root)
    now = datetime.now(timezone.utc)
    stamp = now.strftime(_STAMP_FORMAT)
    receipt = backup._archive_skills(skills_root, directory,
                                    name_for_digest=lambda sha: _archive_name(f'{stamp}-{sha}'))
    return {**receipt, 'snapshot': f'{stamp}-{receipt["sha256"]}', 'created_at': now.isoformat()}


def list_snapshots(directory):
    fd = backup._open_absolute_directory(directory, 'snapshot directory')
    try:
        backup._private_directory(fd)
        rows = []
        with os.scandir(fd) as entries:
            for count, entry in enumerate(entries, 1):
                if count > _MAX_DIRECTORY_ENTRIES:
                    raise UsageError('too many entries in snapshot directory')
                name = entry.name
                if not (name.startswith(_PREFIX) and name.endswith(_SUFFIX)):
                    continue  # E.g. a retained .partial or a standalone Hermes archive.
                snapshot = name[len(_PREFIX):-len(_SUFFIX)]
                created, digest = _snapshot_parts(snapshot)
                meta = entry.stat(follow_symlinks=False)
                if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_uid != os.geteuid()
                        or stat.S_IMODE(meta.st_mode) & 0o077 or not 0 < meta.st_size <= _MAX_TAR_BYTES):
                    raise IntegrityError('unsafe snapshot entry in tracking directory')
                rows.append({'snapshot': snapshot, 'created_at': created, 'sha256': digest,
                             'size_bytes': meta.st_size})
        backup._require_current_directory(directory, fd)
        backup._private_directory(fd)
        return {'snapshots': sorted(rows, key=lambda row: row['snapshot'], reverse=True)}
    finally:
        os.close(fd)


def restore_snapshot(directory, snapshot, output):
    backup._absolute_components(directory, 'snapshot directory')
    backup._absolute_components(output, 'recovery output')
    _disjoint(directory, output)
    _, digest = _snapshot_parts(snapshot)
    receipt = _restore_archive(f'{directory}/{_archive_name(snapshot)}', digest, output, private_source=True)
    return {**receipt, 'snapshot': snapshot}
