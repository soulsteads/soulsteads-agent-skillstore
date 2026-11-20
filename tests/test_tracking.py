"""Private tracking is a separate, approval-free snapshot workflow."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


@pytest.fixture
def paths(tmp_path):
    skills = tmp_path / 'profile' / '.agents' / 'skills'
    skills.mkdir(parents=True)
    private = tmp_path / 'snapshots'
    private.mkdir(mode=0o700)
    recovery = tmp_path / 'recoveries'
    recovery.mkdir(mode=0o700)
    return skills, private, recovery


def run(tmp_path, vault, *args):
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
           'SKILLSTORE_STORE': str(tmp_path / 'registry-does-not-exist')}
    return subprocess.run([sys.executable, '-B', '-m', 'skillstore', 'tracking', '--directory',
                           str(vault), *map(str, args)], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=15)


def ok(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def rejected(result, code=None):
    assert result.returncode != 0 and not result.stdout
    if code is not None:
        assert json.loads(result.stderr)['error'] == code


def test_tracking_keeps_and_restores_distinct_snapshots_without_a_registry(tmp_path, paths):
    skills, vault, recovery = paths
    original = skills / 'notes.md'
    original.write_bytes(b'first version; no package frontmatter required')
    script = skills / 'run.sh'
    script.write_text('#!/bin/sh\ntrue\n')
    script.chmod(0o755)
    (skills / '.skillstore').mkdir()
    (skills / '.skillstore' / 'state').write_text('obsolete receipts')
    (skills.parent.parent / 'auth.json').write_text('not a skill')
    assert ok(run(tmp_path, vault, 'list')) == {'snapshots': []}
    first = ok(run(tmp_path, vault, 'backup', '--skills-root', skills))
    original.write_bytes(b'second version')
    second = ok(run(tmp_path, vault, 'backup', '--skills-root', skills))
    assert first['files'] == second['files'] == 2
    assert first['sha256'] != second['sha256']
    first_archive = Path(first['archive'])
    assert first_archive.parent == vault and first_archive.stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(first_archive.read_bytes()).hexdigest() == first['sha256']
    with tarfile.open(first_archive) as saved:
        assert saved.getnames() == ['skills', 'skills/notes.md', 'skills/run.sh']
    (vault / '.failed.partial').write_text('not a published snapshot')
    listed = ok(run(tmp_path, vault, 'list'))['snapshots']
    assert [row['snapshot'] for row in listed] == [second['snapshot'], first['snapshot']]
    assert [row['sha256'] for row in listed] == [second['sha256'], first['sha256']]
    assert listed[1]['size_bytes'] == first_archive.stat().st_size
    for receipt, content in ((first, b'first version; no package frontmatter required'),
                             (second, b'second version')):
        output = recovery / receipt['snapshot']
        restored = ok(run(tmp_path, vault, 'restore', receipt['snapshot'], '--output', output))
        restored_skills = Path(restored['skills'])
        assert restored['sha256'] == receipt['sha256']
        assert restored['snapshot'] == receipt['snapshot']
        assert (restored_skills / 'notes.md').read_bytes() == content
        assert (restored_skills / 'run.sh').stat().st_mode & 0o777 == 0o700
        assert not (restored_skills / '.skillstore').exists()
        assert not (output / 'auth.json').exists()
        rejected(run(tmp_path, vault, 'restore', receipt['snapshot'], '--output', output), 'conflict')
        assert (restored_skills / 'notes.md').read_bytes() == content
    assert original.read_bytes() == b'second version'
    assert not (tmp_path / 'registry-does-not-exist').exists()


def test_corrupt_archive_fails_digest_verification_before_recovery(tmp_path, paths):
    skills, vault, recovery = paths
    (skills / 'notes.md').write_text('snapshot integrity fixture')
    receipt = ok(run(tmp_path, vault, 'backup', '--skills-root', skills))
    with Path(receipt['archive']).open('ab') as archive:
        archive.write(b'corrupt trailing bytes')
    rejected(run(tmp_path, vault, 'restore', receipt['snapshot'], '--output', recovery / 'staged'),
             'integrity_error')
    assert list(recovery.iterdir()) == []


def test_preserves_private_directory_boundary_and_refuses_recursive_backup(tmp_path, paths):
    skills, vault, recovery = paths
    (skills / 'notes.md').write_text('permissions fixture')
    saved = ok(run(tmp_path, vault, 'backup', '--skills-root', skills))
    archive = Path(saved['archive'])
    incoming = skills / 'nested-vault'
    incoming.mkdir(mode=0o700)
    rejected(run(tmp_path, incoming, 'backup', '--skills-root', skills), 'usage_error')
    assert list(incoming.iterdir()) == []
    rejected(run(tmp_path, vault, 'restore', saved['snapshot'], '--output', vault / 'staged'),
             'usage_error')
    vault.chmod(0o755)
    rejected(run(tmp_path, vault, 'backup', '--skills-root', skills))
    rejected(run(tmp_path, vault, 'list'))
    rejected(run(tmp_path, vault, 'restore', saved['snapshot'], '--output', recovery / 'staged'))
    assert list(vault.iterdir()) == [archive]
    assert list(recovery.iterdir()) == []


@pytest.mark.parametrize('kind', ['traversal', 'symlink', 'hardlink', 'fifo'])
def test_restore_does_not_open_unsafe_snapshot_names_or_targets(tmp_path, paths, kind):
    skills, vault, recovery = paths
    (skills / 'notes.md').write_text('entry safety fixture')
    saved = ok(run(tmp_path, vault, 'backup', '--skills-root', skills))
    archive = Path(saved['archive'])
    outside = recovery / 'sentinel'
    archive.rename(outside)
    before = outside.read_bytes()
    snapshot = saved['snapshot']
    if kind == 'traversal':
        rejected(run(tmp_path, vault, 'restore', '../' + snapshot, '--output', recovery / 'staged'),
                 'usage_error')
    else:
        if kind == 'symlink':
            archive.symlink_to(outside)
        elif kind == 'hardlink':
            archive.hardlink_to(outside)
        else:
            os.mkfifo(archive)
        rejected(run(tmp_path, vault, 'list'), 'integrity_error')
        rejected(run(tmp_path, vault, 'restore', snapshot, '--output', recovery / 'staged'))
    assert outside.read_bytes() == before and not (recovery / 'staged').exists()
