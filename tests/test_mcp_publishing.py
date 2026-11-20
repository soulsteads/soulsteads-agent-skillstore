import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest

from skillstore import package as pkg
from skillstore.contribution import ContributionSource
from skillstore.errors import PermissionDenied, ValidationFailed
from skillstore.remote import RemoteStore
from skillstore.server import add_token, make_server
from skillstore.store import LocalStore, make_actor


NAME = 'note-summary'
START = b'---\nname: note-summary\ndescription: Summarize a note.\n---\n'


@pytest.fixture
def registry(tmp_path):
    store = LocalStore.init(tmp_path / 'registry', ['reviewer'])
    base = pkg.pack({'SKILL.md': (START + b'Write one line.\n', False)})
    store.submit(base, '1.0.0', make_actor('original-author'))
    store.review(NAME, '1.0.0', 'approve', make_actor('reviewer'), 'Reviewed the fixture')
    agent_token = add_token(store.root, 'hermes-agent', 'agent')
    reviewer_token = add_token(store.root, 'reviewer', 'human')
    server = make_server(str(store.root), quiet=True, require_token_for_read=True)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        yield url, agent_token, RemoteStore(url, reviewer_token), store
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def mcp(tmp_path, url, token, options, operations, *, target_options=None):
    if target_options is None:
        target_options = ['--target', tmp_path / 'target']
    commands = [('initialize', {}), *operations]
    payload = ''.join(json.dumps({'jsonrpc': '2.0', 'id': n, 'method': method, 'params': args}) + '\n'
                      for n, (method, args) in enumerate(commands))
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
           'SKILLSTORE_TOKEN': token, 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, '-B', '-m', 'skillstore', '--store', url,
                             'mcp', *target_options, *options],
                            cwd=tmp_path, env=env, input=payload, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row['id'] for row in rows] == list(range(len(commands)))
    return [r['result'] for r in rows[1:]]


def tool(operation, **arguments):
    return 'tools/call', {'name': f'skillstore_{operation}', 'arguments': arguments}


def proposal(path, **kwargs):
    params = {'path': path, 'name': NAME, 'base_version': '1.0.0', 'rationale': 'Clarify the output.'}
    return tool('propose', **{**params, **kwargs})


def content(response, *, error=False):
    assert response['isError'] is error, response
    return json.loads(response['content'][0]['text'])


def create_candidate(root):
    package = root / 'writing' / NAME
    package.mkdir(parents=True)
    (package / 'SKILL.md').write_bytes(START + b'Write one checked sentence.\n')
    (package / 'reference.txt').write_bytes(b'Synthetic supporting material.\n')
    return package


def test_mcp_remote_access_requires_token_and_installs_on_agent_host(tmp_path, registry):
    url, token, _, store = registry
    profile = tmp_path / 'hermes-profile'
    profile.mkdir()
    target = ['--harness', 'hermes', '--scope', 'profile', '--root', profile]
    search = tool('search', query=NAME)
    denied, = mcp(tmp_path, url, '', [], [search], target_options=target)
    assert content(denied, error=True)['error'] == 'permission_denied'
    audit = store.audit()
    listing, found, installed = mcp(tmp_path, url, token, [], [
        ('tools/list', {}), search, tool('install', name=NAME, version='1.0.0'),
    ], target_options=target)
    assert {t['name'] for t in listing['tools']} == {
        'skillstore_search', 'skillstore_inspect', 'skillstore_preview', 'skillstore_install',
        'skillstore_rollback', 'skillstore_uninstall', 'skillstore_status',
    }
    assert [row['name'] for row in content(found)] == [NAME]
    assert content(installed)['digest'] == store.release(NAME, '1.0.0')['digest']
    assert (profile / 'skills' / NAME / 'SKILL.md').read_bytes() == START + b'Write one line.\n'
    assert store.audit() == audit


def test_mcp_proposal_can_be_reviewed_and_adopted_without_agent_approval(tmp_path, registry):
    url, token, reviewer, store = registry
    root = tmp_path / 'publish'
    package = create_candidate(root)
    listing, response = mcp(tmp_path, url, token, ['--source-root', root],
                            [('tools/list', {}), proposal('writing/note-summary')])
    names = {item['name'] for item in listing['tools']}
    assert 'skillstore_propose' in names
    assert not names.intersection({'skillstore_review', 'skillstore_decide', 'skillstore_publish'})
    p = content(response)
    assert p['status'] == 'open' and p['base_version'] == '1.0.0'
    assert p['submitted_by'] == {'actor': 'hermes-agent', 'kind': 'agent', 'authenticated': True}
    assert 'reference.txt' in reviewer.proposal_diff(p['id'])['added']
    with pytest.raises(PermissionDenied):
        RemoteStore(url, token).decide_proposal(p['id'], 'accept', note='Self approval', version='1.1.0')
    content(mcp(tmp_path, url, token, [], [tool('decide', id=p['id'])])[0], error=True)
    reviewer.decide_proposal(p['id'], 'accept', note='Independently checked', version='1.1.0')
    adopted = mcp(tmp_path, url, token, [], [tool('install', name=NAME, version='1.1.0')])[0]
    assert content(adopted)['digest'] == p['digest']
    live = tmp_path / 'target' / NAME
    assert (live / 'reference.txt').read_bytes() == (package / 'reference.txt').read_bytes()
    assert (live / 'SKILL.md').read_bytes() == (package / 'SKILL.md').read_bytes()
    assert store.release(NAME, '1.1.0')['provenance']['origin']['proposal_id'] == p['id']


def test_mcp_submits_pending_version_with_explicit_provenance(tmp_path, registry):
    url, token, reviewer, store = registry
    root = tmp_path / 'publish'
    package = create_candidate(root)
    (package / 'SKILL.md').write_bytes(START.replace(b'note-summary', b'note-brief') + b'Write a brief.\n')
    package.rename(package.with_name('note-brief'))
    submit = tool('submit', name='note-brief', path='writing/note-brief', version='1.0.0',
                  source_uri='https://example.org/skills.git', revision='fixture-commit')
    listing, response = mcp(tmp_path, url, token, ['--source-root', root], [('tools/list', {}), submit])
    assert any(t['name'] == 'skillstore_submit' for t in listing['tools'])
    sent = content(response)
    assert sent['status'] == 'pending'
    assert sent['provenance']['source'] == {'type': 'source', 'uri': 'https://example.org/skills.git',
                                             'revision': 'fixture-commit'}
    install = tool('install', name='note-brief', version='1.0.0')
    rejected = content(mcp(tmp_path, url, token, [], [install])[0], error=True)
    assert rejected['error'] == 'not_reviewed'
    reviewer.review('note-brief', '1.0.0', 'approve', note='Independently checked')
    assert content(mcp(tmp_path, url, token, [], [install])[0])['digest'] == sent['digest']
    assert store.verify()['ok']


def test_mcp_contributions_disabled_without_source_root(tmp_path, registry):
    url, token, _, store = registry
    create_candidate(tmp_path / 'publish')
    listing, response = mcp(tmp_path, url, token, [], [('tools/list', {}), proposal('writing/note-summary')])
    assert not {'skillstore_propose', 'skillstore_submit'} & {t['name'] for t in listing['tools']}
    assert content(response, error=True)['error'] == 'usage_error'
    assert store.list_proposals() == []


def test_mcp_source_rejects_paths_and_files_outside_the_staging_tree(tmp_path, registry):
    url, token, _, store = registry
    root = tmp_path / 'publish'
    actual = create_candidate(tmp_path / 'private')
    root.mkdir()
    (root / 'linked').symlink_to(actual.parent, target_is_directory=True)
    copied = create_candidate(root)
    (copied / 'unrelated.txt').hardlink_to(actual / 'reference.txt')
    ops = [proposal(str(actual)), proposal('../private/writing/note-summary'), proposal('linked/note-summary'),
           proposal('writing/note-summary'), proposal('writing/note-summary', actor='reviewer')]
    audit = store.audit()
    for response in mcp(tmp_path, url, token, ['--source-root', root], ops):
        content(response, error=True)
    assert store.list_proposals() == [] and store.audit() == audit


def test_mcp_refuses_hidden_files_in_a_candidate(tmp_path, registry):
    url, token, _, store = registry
    root = tmp_path / 'publish'
    candidate = create_candidate(root)
    (candidate / '.env').write_text('SYNTHETIC_PRIVATE_MARKER\n')
    result, = mcp(tmp_path, url, token, ['--source-root', root], [proposal('writing/note-summary')])
    assert 'hidden' in content(result, error=True)['message']
    assert store.list_proposals() == []


def test_source_refuses_root_replacement_after_packaging(tmp_path, monkeypatch):
    root = tmp_path / 'publish'
    create_candidate(root)
    source = ContributionSource(str(root))
    pack = pkg.pack

    def replace_after_pack(files):
        data = pack(files)
        root.rename(tmp_path / 'moved')
        create_candidate(root)  # The same bytes at a different identity don't restore authority.
        return data

    monkeypatch.setattr(pkg, 'pack', replace_after_pack)
    with pytest.raises(ValidationFailed, match='source path changed'):
        source.package('writing/note-summary', NAME)
