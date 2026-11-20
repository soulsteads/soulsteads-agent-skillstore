import json
import os
from http.client import HTTPConnection
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest

from skillstore import package as pkg
from skillstore.server import add_token, revoke_tokens
from skillstore.store import LocalStore, make_actor


@pytest.fixture
def registry(tmp_path):
    store = LocalStore.init(tmp_path / 'registry', ['reviewer'])
    for name, version in [('example', '1.0.0'), ('example', '1.1.0'), ('draft', '1.0.0')]:
        package = pkg.pack({'SKILL.md': (f'---\nname: {name}\ndescription: Fixture skill.\n---\nFixture.\n'.encode(), False)})
        store.submit(package, version, make_actor('author'))
    store.review('example', '1.0.0', 'approve', make_actor('reviewer'), 'Reviewed fixture')
    return store


@pytest.fixture
def http(registry):
    from skillstore.mcp_http import make_server

    token = add_token(registry.root, 'client', 'agent')
    with make_server(registry, 0) as server:
        assert server.server_address[0] == '127.0.0.1'
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        try:
            yield server.server_port, token
        finally:
            server.shutdown()
            thread.join(timeout=5)


def rpc(method, params=None, **fields):
    return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}, **fields}


def request(port, token, payload=None, *, headers=None, verb='POST', path='/mcp'):
    options = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
    if token is not None:
        options['Authorization'] = 'Bearer ' + token
    options.update(headers or {})
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    conn = HTTPConnection('127.0.0.1', port, timeout=4)
    try:
        conn.request(verb, path, body if verb == 'POST' else None, options)
        response = conn.getresponse()
        data = response.read()
        return response.status, response.headers, json.loads(data) if data else None
    finally:
        conn.close()


def test_cli_exposes_a_dedicated_listener_command():
    from skillstore.cli import parser

    args = parser().parse_args(['--store', '/registry', 'serve-mcp', '--port', '9876'])
    assert (args.command, args.port) == ('serve-mcp', 9876)


def test_stateless_handshake_and_reviewed_only_catalog(http, registry):
    port, token = http
    audit = registry.audit()
    for version in ['2025-03-26', '2025-06-18', '2025-11-25', 'future']:
        status, headers, msg = request(port, token, rpc('initialize', {'protocolVersion': version}))
        assert status == 200 and headers['Content-Type'] == 'application/json'
        assert 'MCP-Session-Id' not in headers
        assert msg['result']['protocolVersion'] == (version if version != 'future' else '2025-11-25')
        assert msg['result']['capabilities'] == {'tools': {}}
    status, _, msg = request(port, token, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    assert (status, msg) == (202, None)
    assert request(port, token, verb='GET')[0] == 405
    assert request(port, token, verb='DELETE')[0] == 405
    assert request(port, token, rpc('ping'))[2]['result'] == {}
    tools = request(port, token, rpc('tools/list'))[2]['result']['tools']
    assert {t['name'] for t in tools} == {'skillstore_search', 'skillstore_inspect'}
    assert all(t['annotations']['readOnlyHint'] for t in tools)

    def call(operation, **arguments):
        return request(port, token, rpc('tools/call', {'name': 'skillstore_' + operation, 'arguments': arguments}))[2]['result']

    found = json.loads(call('search')['content'][0]['text'])
    assert [r['name'] for r in found] == ['example']
    for args in ({'name': 'example'}, {'name': 'example', 'version': '1.0.0'}):
        assert json.loads(call('inspect', **args)['content'][0]['text']) == registry.release('example', '1.0.0')
    pending = call('inspect', name='example', version='1.1.0')
    assert pending == call('inspect', name='draft') == call('inspect', name='missing')
    assert pending['isError'] and 'draft' not in pending['content'][0]['text']
    for name in ['tracking_list', 'backup_hermes', 'install', 'submit', 'review', 'status']:
        assert call(name)['isError']
    assert call('search', include_unreviewed='true')['isError']
    assert registry.audit() == audit
    registry.withdraw('example', '1.0.0', make_actor('reviewer'), 'Withdraw the fixture')
    assert call('inspect', name='example', version='1.0.0') == pending


def test_network_transport_admission(http, registry):
    port, token = http
    payload = rpc('tools/list')
    for verb in ['GET', 'POST', 'DELETE']:
        assert request(port, token, payload, verb=verb, headers={'Origin': 'https://untrusted.example'})[0] == 403
        status, headers, _ = request(port, None, payload, verb=verb)
        assert status == 401 and headers['WWW-Authenticate'] == 'Bearer'
    assert request(port, token, payload, headers={'Host': 'invalid.example'})[0] == 403
    assert request(port, token, payload, headers={'MCP-Protocol-Version': 'invalid'})[0] == 400
    assert request(port, token, payload, headers={'Content-Type': 'text/plain'})[0] == 415
    assert request(port, token, payload, headers={'Accept': 'text/event-stream'})[0] == 406
    assert request(port, token, {'method': 'list_skills'}, path='/v1/rpc')[0] == 404
    assert request(port, token, b'{' + b' ' * (64 * 1024))[0] == 413
    assert request(port, token, b'{')[2]['error']['code'] == -32700
    assert request(port, token, [payload])[2]['error']['code'] == -32600
    assert request(port, token, rpc('unknown'))[2]['error']['code'] == -32601
    assert request(port, token, rpc('tools/call', ['invalid']))[2]['error']['code'] == -32602
    assert revoke_tokens(registry.root, 'client') == 1
    assert request(port, token, payload)[0] == 401


def test_remote_registry_cannot_be_used_as_listener_backend(tmp_path):
    result = subprocess.run([sys.executable, '-B', '-m', 'skillstore', '--store', 'https://example.org', 'serve-mcp'],
                            cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and not result.stdout
    assert json.loads(result.stderr)['error'] == 'usage_error'
