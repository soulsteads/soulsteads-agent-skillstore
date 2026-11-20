"""Exercise Git-backed submissions through a real disposable HTTPS Git server."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
from threading import Thread
from urllib.parse import urlsplit

import pytest

from skillstore.errors import NotFound
from skillstore.store import LocalStore


@pytest.fixture
def sources(tmp_path, monkeypatch):
    for key in os.environ.copy():
        if key.startswith(('GIT_', 'SKILLSTORE_GIT_')):
            monkeypatch.delenv(key)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    repo = tmp_path / 'checkout'
    repo.mkdir()
    run_git(repo, 'init', '-qb', 'main')
    skill = repo / 'skills' / 'demo'
    skill.mkdir(parents=True)
    commit_skill(repo, 'First version.')
    return repo, LocalStore.init(tmp_path / 'registry', ['reviewer'])


def run_git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                          text=True, timeout=10).stdout.strip()


def commit_skill(repo, text):
    path = repo / 'skills/demo/SKILL.md'
    path.write_text(f'---\nname: demo\ndescription: Git fixture.\n---\n{text}\n')
    run_git(repo, 'add', 'skills/demo/SKILL.md')
    run_git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-qm', text)
    return run_git(repo, 'rev-parse', 'HEAD'), hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(store, *args):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    return subprocess.run([sys.executable, '-B', '-m', 'skillstore', '--store', str(store.root),
                           '--actor', 'author', *map(str, args)], env=env, capture_output=True,
                          text=True, timeout=45)


def succeeded(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def git_remote(sources, tmp_path, monkeypatch):
    repo, _ = sources
    openssl = shutil.which('openssl')
    backend = Path(run_git(repo, '--exec-path')) / 'git-http-backend'
    if not openssl or not backend.is_file():
        pytest.skip('HTTPS Git qualification requires OpenSSL and Git smart HTTP')
    assert openssl is not None
    remote_root = tmp_path / 'server'
    remote_root.mkdir()
    remote = remote_root / 'skills.git'
    run_git(repo, 'clone', '--quiet', '--bare', '--no-local', '.', str(remote))
    config, cert, key = [tmp_path / name for name in ('openssl.conf', 'ca.pem', 'key.pem')]
    config.write_text('[req]\nprompt = no\ndistinguished_name = dn\nx509_extensions = ext\n'
                      '[dn]\nCN = Skillstore Git fixture\n'
                      '[ext]\nsubjectAltName = IP:127.0.0.1\nbasicConstraints = critical,CA:TRUE\n')
    subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-config', str(config), '-keyout', str(key), '-out', str(cert)],
                   check=True, capture_output=True, timeout=15)
    monkeypatch.setenv('SSL_CERT_FILE', str(cert))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.serve_git()

        def do_POST(self):
            self.serve_git()

        def serve_git(self):
            info = urlsplit(self.path)
            requests.append(info.path)
            if info.path.startswith('/redirect.git/'):
                self.send_response(302)
                self.send_header('Location', self.path.replace('/redirect.git/', '/skills.git/'))
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if info.path not in ('/skills.git/info/refs', '/skills.git/git-upload-pack'):
                self.send_error(404)
                return
            assert 'Authorization' not in self.headers
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            env = dict(PATH=os.environ['PATH'], GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_PROJECT_ROOT=str(remote_root), GIT_HTTP_EXPORT_ALL='1', PATH_INFO=info.path,
                       QUERY_STRING=info.query, REQUEST_METHOD=self.command, CONTENT_LENGTH=str(len(body)),
                       CONTENT_TYPE=self.headers.get('Content-Type', ''),
                       GIT_PROTOCOL=self.headers.get('Git-Protocol', ''))
            response = subprocess.run([backend], env=env, cwd=remote_root, input=body,
                                      capture_output=True, check=True, timeout=10).stdout
            header, payload = response.split(b'\r\n\r\n', 1)
            self.send_response(200)
            for line in header.decode('ascii').split('\r\n'):
                name, value = line.split(': ', 1)
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f'https://127.0.0.1:{server.server_port}/skills.git', remote, requests
        finally:
            server.shutdown()
            worker.join(timeout=5)


def test_local_checkout_needs_no_remote_and_keeps_path_private(sources, monkeypatch):
    repo, store = sources
    assert not run_git(repo, 'remote')
    monkeypatch.setenv('SKILLSTORE_GIT_REPO', str(repo))
    release = succeeded(invoke(store, 'submit-git', 'skills/demo', '1.0.0'))
    assert release['provenance']['source'] == {'type': 'git', 'uri': 'local:git',
                                               'revision': run_git(repo, 'rev-parse', 'HEAD'),
                                               'path': 'skills/demo'}
    assert str(repo) not in json.dumps(release)
    report = succeeded(invoke(store, 'git-status', 'demo', '--version', '1.0.0'))
    assert not report['content_changed']


def test_remote_ref_refresh_and_content_digest(sources, git_remote, monkeypatch):
    repo, store = sources
    url, remote, requests = git_remote
    revision = run_git(remote, 'rev-parse', 'HEAD')
    run_git(remote, '-c', 'user.name=Fixture', '-c', 'user.email=test@example.org',
            'tag', '-am', 'First version', 'v1.0.0', revision)
    monkeypatch.setenv('SKILLSTORE_GIT_REMOTE', url)
    ref = ['--revision', 'refs/heads/main']
    submit = ['submit-git', 'skills/demo', '1.0.0', *ref]
    released = succeeded(invoke(store, *submit))
    assert released['status'] == 'pending'
    assert released['provenance']['source'] == {'type': 'git', 'uri': url, 'revision': revision,
                                                'path': 'skills/demo'}
    assert set(requests) == {'/skills.git/info/refs', '/skills.git/git-upload-pack'}
    assert released['files']['SKILL.md']['sha256'] == hashlib.sha256(
        (repo / 'skills/demo/SKILL.md').read_bytes()).hexdigest()
    status = ['git-status', 'demo', '--version', '1.0.0']
    assert not succeeded(invoke(store, *status))['content_changed']  # Remote HEAD.
    after, digest = commit_skill(repo, 'Second version.')
    # The author's later commit is invisible until the server ref advances.
    assert not succeeded(invoke(store, *status, *ref))['content_changed']
    run_git(repo, 'push', '--quiet', str(remote), 'refs/heads/main')
    report = succeeded(invoke(store, *status, *ref))
    assert report['content_changed'] and report['revision'] == after
    assert report['recorded_revision'] == revision
    tagged = succeeded(invoke(store, *status, '--revision', 'refs/tags/v1.0.0'))
    assert tagged['revision'] == revision and not tagged['content_changed']
    again = succeeded(invoke(store, 'submit-git', 'skills/demo', '1.1.0', '--revision', after))
    assert again['status'] == 'pending' and again['files']['SKILL.md']['sha256'] == digest
    assert again['provenance']['source']['revision'] == after
    before = requests.copy()
    monkeypatch.delenv('SKILLSTORE_GIT_REMOTE')
    assert invoke(store, *status).returncode != 0  # Provenance is not permission to fetch.
    assert invoke(store, *status, '--git-remote', url.replace('/skills.git', '/other.git')).returncode != 0
    assert requests == before
    # A deliberate checkout override stays offline even with the remote configured.
    monkeypatch.setenv('SKILLSTORE_GIT_REMOTE', url)
    assert succeeded(invoke(store, *status, '--repo', repo))['revision'] == after
    assert requests == before


def test_remote_rejects_bad_trust_redirects_and_missing_refs_without_submission(sources, git_remote, tmp_path,
                                                                               monkeypatch):
    repo, store = sources
    url, _, requests = git_remote
    cert = os.environ['SSL_CERT_FILE']
    empty = tmp_path / 'empty.pem'
    empty.touch()
    monkeypatch.setenv('SSL_CERT_FILE', str(empty))
    monkeypatch.setenv('GIT_SSL_NO_VERIFY', '1')  # Inherited Git settings must not bypass TLS.
    args = ['submit-git', 'skills/demo', '1.0.0', '--git-remote']
    result = invoke(store, *args, url)
    assert result.returncode != 0 and 'fetch' in result.stderr and not requests
    monkeypatch.setenv('SSL_CERT_FILE', cert)
    result = invoke(store, *args, url.replace('/skills.git', '/redirect.git'))
    assert result.returncode != 0 and requests == ['/redirect.git/info/refs']
    result = invoke(store, *args, url, '--revision', 'refs/heads/missing')
    assert result.returncode != 0 and 'fetch' in result.stderr
    before = requests.copy()
    for bad in (url.replace('https:', 'http:'), url.replace('://', '://username:secret@'),
                url + '?secret=token', url + '#secret', 'https://[broken'):
        rejected = invoke(store, *args, bad)
        assert rejected.returncode != 0 and not rejected.stdout
        assert 'credential-free HTTPS' in rejected.stderr and 'secret' not in rejected.stderr
    assert invoke(store, *args, url, '--repo', repo).returncode != 0
    assert requests == before
    with pytest.raises(NotFound):
        store.release('demo', '1.0.0')
