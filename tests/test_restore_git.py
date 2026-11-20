import gzip
import hashlib

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from skillstore.backup import backup_hermes
from skillstore.errors import NotFound
from skillstore.store import LocalStore


def cli(*args, cwd):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    return subprocess.run([sys.executable, '-B', '-m', 'skillstore', *args], cwd=cwd,
                          env=env, capture_output=True, text=True, timeout=15)


def test_restore_round_trip_without_registry_or_overwrite(tmp_path):
    profile = tmp_path / 'profile'
    skills = profile / 'skills'
    skills.mkdir(parents=True)
    (profile / 'auth.json').write_text('excluded profile bytes')
    (skills / 'demo').mkdir()
    script = skills / 'demo' / 'tool.sh'
    script.write_bytes(b'#!/bin/sh\ntrue\n')
    script.chmod(0o755)
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    archive = private / 'fixture.tar.gz'
    backup = backup_hermes(str(profile), str(archive))
    output = private / 'recovered'
    args = ['restore-hermes', archive, '--sha256', backup['sha256'], '--output', output]
    result = cli(*args, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['files'] == 1
    restored = output / 'skills' / 'demo' / 'tool.sh'
    assert restored.read_bytes() == script.read_bytes()
    assert restored.stat().st_mode & 0o100
    assert not (output / 'auth.json').exists()
    restored.write_bytes(b'preserve local edit')
    result = cli(*args, cwd=tmp_path)
    assert result.returncode != 0 and not result.stdout
    assert restored.read_bytes() == b'preserve local edit'


@pytest.mark.parametrize('case', ['digest', 'link', 'outside-root', 'duplicate', 'trailing-data'])
def test_restore_rejects_invalid_archives_before_creating_destination(tmp_path, case):
    tmp_path.chmod(0o700)
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        root = tarfile.TarInfo('skills')
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        if case == 'link':
            bad = tarfile.TarInfo('skills/link')
            bad.type = tarfile.SYMTYPE
            bad.linkname = '../outside'
            tar.addfile(bad)
        elif case == 'outside-root':
            tar.addfile(tarfile.TarInfo('not-skills'))
        elif case == 'duplicate':
            tar.addfile(root)
    if case == 'trailing-data':
        archive.write_bytes(gzip.compress(gzip.decompress(archive.read_bytes()) + b'not tar padding'))
    archive.chmod(0o600)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest() if case != 'digest' else 'a' * 64
    output = tmp_path / 'recovered'
    result = cli('restore-hermes', archive, '--sha256', digest, '--output', output, cwd=tmp_path)
    assert result.returncode != 0 and not result.stdout
    assert not output.exists()
    assert 'invalid choice' not in result.stderr


def git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    return subprocess.check_output(['git', '-C', str(repo), *args], env=env, text=True, stderr=subprocess.PIPE).strip()


def test_git_submission_pins_committed_tree_and_still_requires_review(tmp_path):
    repo = tmp_path / 'source'
    repo.mkdir()
    git(repo, 'init', '--quiet')
    skill = repo / 'skills' / 'demo'
    skill.mkdir(parents=True)
    initial = b'---\nname: demo\ndescription: One fixture.\n---\nCommitted text.\n'
    (skill / 'SKILL.md').write_bytes(initial)
    helper = skill / 'helper.sh'
    helper.write_text('#!/bin/sh\ntrue\n')
    helper.chmod(0o755)
    git(repo, 'add', 'skills')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-qm', 'initial skill')
    revision = git(repo, 'rev-parse', 'HEAD')
    (skill / 'SKILL.md').write_bytes(initial.replace(b'Committed', b'Uncommitted'))
    (skill / 'untracked.txt').write_text('not part of the package')
    registry = LocalStore.init(tmp_path / 'registry', ['reviewer'])
    sub = ['--store', registry.root, '--actor', 'author', 'submit-git', 'skills/demo', '1.0.0', '--repo', repo,
           '--source', 'https://example.org/skills.git']
    result = cli(*sub, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    release = json.loads(result.stdout)
    assert release['status'] == 'pending'
    assert release['provenance']['source'] == {'type': 'git', 'uri': 'https://example.org/skills.git',
                                               'revision': revision, 'path': 'skills/demo'}
    assert set(release['files']) == {'SKILL.md', 'helper.sh'}
    assert release['files']['helper.sh']['mode'] == '755'
    assert release['files']['SKILL.md']['sha256'] == hashlib.sha256(initial).hexdigest()
    assert 'Uncommitted' in (skill / 'SKILL.md').read_text()
    query = ['--store', registry.root, 'git-status', 'demo', '--version', '1.0.0', '--repo', repo]
    result = cli(*query, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['revision'] == revision and not report['content_changed']
    git(repo, 'add', 'skills/demo/SKILL.md')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-qm', 'update skill')
    result = cli(*query, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['revision'] == git(repo, 'rev-parse', 'HEAD') and report['content_changed']
    assert registry.release('demo', '1.0.0')['status'] == 'pending'
    invalid = cli(*sub[:5], 'skills/../demo', '1.1.0', *sub[7:], cwd=tmp_path)
    assert invalid.returncode != 0 and not invalid.stdout and 'relative directory' in invalid.stderr
    outside = tmp_path / 'private.md'
    outside.write_text('outside the checkout')
    (skill / 'linked.md').symlink_to(outside)
    git(repo, 'add', 'skills/demo/linked.md')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-qm', 'test links')
    rejected = cli(*sub[:6], '1.1.0', *sub[7:], cwd=tmp_path)
    assert rejected.returncode != 0 and 'without links' in rejected.stderr and not rejected.stdout
    with pytest.raises(NotFound):
        registry.release('demo', '1.1.0')
