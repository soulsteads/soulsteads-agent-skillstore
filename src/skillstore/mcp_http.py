"""Stateless MCP Streamable HTTP for the reviewed registry catalog only."""

import json
import re
import sys
from http.server import ThreadingHTTPServer

from .errors import NotFound, NotReviewed, PermissionDenied, SkillstoreError, UsageError
from .mcp import TOOLS, initialize_result, tool_result, validate_arguments
from .package import MAX_NAME, NAME_RE, parse_version
from .server import Handler


PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
MAX_BODY = 64 * 1024
CATALOG_TOOLS = [t for t in TOOLS if t['name'] == 'skillstore_search'] + [{
    **next(t for t in TOOLS if t['name'] == 'skillstore_inspect'),
    'description': 'Inspect the latest or an exact reviewed release, including its digest and provenance.',
}]


def call_catalog(store, name, args):
    validate_arguments(CATALOG_TOOLS, name, args)
    if name == 'skillstore_search':
        rows = store.list_skills(args.get('query'))
        for row in rows:
            row['versions'] = [v for v in row['versions'] if v['status'] == 'reviewed']
        return rows
    skill = args['name']
    version = args.get('version')
    if len(skill) > MAX_NAME or not NAME_RE.fullmatch(skill):
        raise UsageError('invalid skill name')
    if version is not None:
        parse_version(version)
    try:
        release = store.release(skill, version)
    except (NotFound, NotReviewed):
        release = None
    if release is None or release['status'] != 'reviewed':
        raise NotFound('reviewed release not found')
    return release


def rpc_error(code, message, request_id=None):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def rpc_reply(store, request):
    if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
        return 400, rpc_error(-32600, 'invalid request')
    method = request.get('method')
    request_id = request.get('id')
    valid_id = type(request_id) in (str, int)
    if not isinstance(method, str):
        if valid_id and ('result' in request) != ('error' in request) and 'method' not in request:
            return 202, None  # No server-initiated requests in this transport.
        return 400, rpc_error(-32600, 'invalid request')
    if 'id' not in request:
        return 202, None  # Notifications never invoke tools.
    if not valid_id:
        return 400, rpc_error(-32600, 'invalid request ID')
    params = request.get('params', {})
    if not isinstance(params, dict):
        return 200, rpc_error(-32602, 'params must be an object', request_id)
    if method == 'initialize':
        version = params.get('protocolVersion')
        result = initialize_result(version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0])
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': CATALOG_TOOLS}
    elif method == 'tools/call':
        try:
            result = tool_result(call_catalog(store, params.get('name'), params.get('arguments', {})))
        except (UsageError, NotFound) as exc:
            result = tool_result(exc.to_dict(), error=True)
        except (SkillstoreError, OSError, ValueError, TypeError, KeyError):
            return 200, rpc_error(-32603, 'catalog unavailable', request_id)
    else:
        return 200, rpc_error(-32601, 'method not found', request_id)
    return 200, {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def reject_json_constant(value):
    raise ValueError('not JSON: ' + value)


class MCPHandler(Handler):
    timeout = 10
    quiet = True  # Do not log caller-controlled request lines or headers.

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def _empty(self, status, **headers):
        self.send_response(status)
        self.send_header('Content-Length', '0')
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()

    def _admit(self):
        hosts = self.headers.get_all('Host', [])
        port = self.server.server_address[1]
        if (self.headers.get_all('Origin') is not None or len(hosts) != 1
                or hosts[0].lower() not in (f'127.0.0.1:{port}', f'localhost:{port}')):
            self._empty(403)
            return False
        if self.path != '/mcp':
            self._empty(404)
            return False
        try:
            authorized = len(self.headers.get_all('Authorization', [])) == 1 and self._actor() is not None
        except PermissionDenied:
            authorized = False
        if not authorized:
            self._empty(401, **{'WWW-Authenticate': 'Bearer'})
            return False
        versions = self.headers.get_all('MCP-Protocol-Version', ['2025-03-26'])
        if len(versions) != 1 or versions[0] not in PROTOCOL_VERSIONS:
            self._empty(400)
            return False
        return True

    def _no_stream(self):
        try:
            if self._admit():
                self._empty(405, Allow='POST')
        except (OSError, ValueError, TypeError, KeyError):
            self._empty(500)

    do_GET = do_DELETE = do_HEAD = do_OPTIONS = _no_stream

    def do_POST(self):
        try:
            if not self._admit():
                return
            content_types = self.headers.get_all('Content-Type', [])
            if len(content_types) != 1 or self.headers.get_content_type() != 'application/json':
                self._empty(415)
                return
            accepted = [item.partition(';')[0].strip().lower()
                        for header in self.headers.get_all('Accept', ['*/*']) for item in header.split(',')]
            if not {'application/json', 'application/*', '*/*'}.intersection(accepted):
                self._empty(406)
                return
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or self.headers.get_all('Transfer-Encoding') or not re.fullmatch(r'[0-9]+', lengths[0]):
                self._empty(400)
                return
            if len(lengths[0]) > 10 or int(lengths[0]) > MAX_BODY:
                self._empty(413)
                return
            length = int(lengths[0])
            body = self.rfile.read(length)
            if len(body) != length:
                self._empty(400)
                return
            try:
                request = json.loads(body.decode('utf-8'), parse_constant=reject_json_constant)
            except (ValueError, RecursionError):
                self._send(400, rpc_error(-32700, 'invalid JSON'))
                return
            status, result = rpc_reply(self.store, request)
            if result is None:
                self._empty(status)
            else:
                self._send(status, result)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except (OSError, ValueError, TypeError, KeyError):
            self._empty(500)


def make_server(store, port):
    handler = type('BoundMCPHandler', (MCPHandler,), {'store': store})
    return ThreadingHTTPServer(('127.0.0.1', port), handler)


def serve(store, port):
    with make_server(store, port) as httpd:
        sys.stderr.write(f'skillstore MCP listening on http://127.0.0.1:{httpd.server_port}/mcp\n')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
