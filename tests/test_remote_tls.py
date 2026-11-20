"""Qualify the registry client's HTTPS and bearer-token behavior on loopback."""

import shutil
import ssl
import subprocess
from threading import Thread
from typing import cast

import pytest

from skillstore.errors import PermissionDenied, SkillstoreError
from skillstore.remote import RemoteStore
from skillstore.server import Handler, add_token, make_server, revoke_tokens
from skillstore.store import LocalStore


@pytest.fixture
def tls_registry(tmp_path, monkeypatch):
    openssl = shutil.which('openssl')
    if openssl is None:
        pytest.skip('HTTPS qualification requires the openssl CLI')
    assert openssl is not None
    cert, key = tmp_path / 'registry.pem', tmp_path / 'registry-key.pem'
    # Explicit config also works with openssl versions without req -addext.
    config = tmp_path / 'openssl.cnf'
    config.write_text(
        '[req]\nprompt = no\ndistinguished_name = dn\nx509_extensions = ext\n'
        '[dn]\nCN = Skillstore test registry\n'
        '[ext]\nsubjectAltName = IP:127.0.0.1\nbasicConstraints = critical,CA:TRUE\n'
        'keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign\n'
    )
    subprocess.run([
        openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-config', str(config), '-keyout', str(key), '-out', str(cert),
    ], check=True, capture_output=True, timeout=15)
    empty_ca_dir = tmp_path / 'empty-ca'
    empty_ca_dir.mkdir()
    monkeypatch.setenv('SSL_CERT_DIR', str(empty_ca_dir))
    monkeypatch.setenv('no_proxy', '*')
    store = LocalStore.init(tmp_path / 'registry', ['reviewer'])
    token = add_token(store.root, 'agent-client', 'agent')
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server = make_server(str(store.root), quiet=True, require_token_for_read=True)
    requests = []
    handler_class = cast(type[Handler], server.RequestHandlerClass)
    original = handler_class.do_POST

    def record_post(handler):
        requests.append(handler.path)  # Never retain request headers or bearer values.
        return original(handler)

    monkeypatch.setattr(handler_class, 'do_POST', record_post)
    with server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield store, server.server_port, token, cert, requests
        finally:
            server.shutdown()
            worker.join(timeout=5)


def test_private_registry_accepts_trusted_https_and_revokes_access(tls_registry, monkeypatch):
    store, port, token, cert, _ = tls_registry
    monkeypatch.setenv('SSL_CERT_FILE', str(cert))
    url = f'https://127.0.0.1:{port}'
    client = RemoteStore(url, token)
    assert client.info()['store_id'] == store.info()['store_id']
    with pytest.raises(PermissionDenied):
        RemoteStore(url).list_skills()
    assert revoke_tokens(store.root, 'agent-client') == 1
    with pytest.raises(PermissionDenied, match='revoked'):
        client.list_skills()


@pytest.mark.parametrize('trusted', [False, True], ids=['untrusted-certificate', 'hostname-mismatch'])
def test_https_verification_failure_sends_no_registry_request(tls_registry, tmp_path, monkeypatch, trusted):
    _, port, token, cert, requests = tls_registry
    # The cert only names 127.0.0.1, not localhost. Likewise, the empty trust
    # file excludes the fixture's independently generated certificate.
    empty_ca = tmp_path / 'empty-ca.pem'
    empty_ca.touch()
    monkeypatch.setenv('SSL_CERT_FILE', str(cert if trusted else empty_ca))
    host = 'localhost' if trusted else '127.0.0.1'
    with pytest.raises(SkillstoreError, match='CERTIFICATE_VERIFY_FAILED'):
        RemoteStore(f'https://{host}:{port}', token).info()
    assert requests == []
