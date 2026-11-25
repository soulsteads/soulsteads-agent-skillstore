"""Native Hermes commands and tools backed by the installed Skillstore CLI."""

import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from hermes_constants import get_hermes_home


_TOOLSET = 'skillstore'
_OPERATIONS = {
    'search': ('Search the configured Skillstore registry for reviewed skills.', (), ('query',)),
    'inspect': ('Inspect the provenance of an exact reviewed Skillstore version.', ('name', 'version'), ()),
    'preview': ('Preview file changes for an exact reviewed Skillstore version.', ('name', 'version'), ()),
    'install': ('Install an exact reviewed Skillstore version into the active Hermes profile.', ('name', 'version'), ()),
    'status': ('Show managed Skillstore installations and local changes in the active Hermes profile.', (), ()),
}
_BASE_ENV = {'HOME', 'PATH', 'LANG', 'TMPDIR'}
_REMOTE_ENV = {'SSL_CERT_FILE', 'SSL_CERT_DIR'}
_ERRORS = {
    2: 'Skillstore rejected the package.',
    3: 'Skillstore reported a conflict. Check local edits, unmanaged skills, sessions, or pending recovery.',
    4: 'Skillstore could not verify package or installation integrity.',
    5: 'The version is not an active reviewed release.',
    6: 'Skillstore denied access. Check the registry permissions and profile token.',
    7: 'Skillstore could not find the requested skill or version.',
    64: 'Skillstore rejected the request. Check the skill name, version, and plugin settings.',
}


def _absolute(value):
    return (isinstance(value, str) and Path(value).is_absolute() and '\x00' not in value
            and '..' not in Path(value).parts)


def _remote_store(value):
    if not isinstance(value, str) or not value.startswith(('http://', 'https://')):
        return False
    # Validate before forming argv: downstream validation cannot prevent a secret
    # embedded in a URL from appearing in the child process's command line.
    try:
        url = urlsplit(value)
        if (url.hostname and '@' not in url.netloc and '?' not in value and '#' not in value
                and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
            return True
    except ValueError:
        pass
    raise ValueError('Set a valid registry base URL without credentials, query, or fragment.')


def _run(ctx, op, arguments):
    _, required, optional = _OPERATIONS[op]
    if (not isinstance(arguments, dict) or not set(required) <= arguments.keys()
            or arguments.keys() - set(required + optional)
            or any(not isinstance(value, str) or '\x00' in value or len(value) > 4096
                   for value in arguments.values())
            or any(not arguments[key].strip() for key in required)):
        raise ValueError('Invalid Skillstore arguments.')

    executable = ctx.get_config('executable')
    if not _absolute(executable):
        raise ValueError('Set the Skillstore plugin executable to an absolute path.')
    argv = [executable]
    remote = False
    if op != 'status':
        store = ctx.get_config('store')
        remote = _remote_store(store)
        if not (remote or _absolute(store)):
            raise ValueError('Set the Skillstore plugin store to an absolute registry path or base URL.')
        argv.extend(['--store', store])
    argv.append(op)
    if op in ('preview', 'install', 'status'):
        argv.extend(['--harness', 'hermes', '--scope', 'profile', '--root', str(get_hermes_home())])
    if op in ('preview', 'status'):
        argv.append('--no-recover')
    if op == 'search':
        argv.extend(['--', arguments.get('query', '')])
    elif 'version' in arguments:
        argv.extend([f'--version={arguments["version"]}', '--', arguments['name']])
    env = {key: value for key, value in os.environ.items()
           if key in _BASE_ENV or key.startswith('LC_') or (remote and key in _REMOTE_ENV)}
    if remote:
        from agent.secret_scope import UnscopedSecretError, get_secret

        # Resolve credentials for this call, not for the gateway's launch profile.
        try:
            token = get_secret('SKILLSTORE_TOKEN')
        except UnscopedSecretError:
            raise ValueError('Skillstore could not resolve the active Hermes profile credential.') from None
        if token:
            env['SKILLSTORE_TOKEN'] = token
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, env=env,
                                encoding='utf-8', timeout=120)
    except subprocess.TimeoutExpired:
        raise ValueError('Skillstore timed out. Check status before retrying an install.') from None
    except (OSError, UnicodeError):
        raise ValueError('Could not run Skillstore. Check the configured executable.') from None
    if result.returncode:
        # The registry can influence stderr; do not relay it to Hermes or its tools.
        raise ValueError(_ERRORS.get(result.returncode, 'Skillstore failed. Check the registry and executable settings.'))
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise ValueError('Skillstore returned an invalid JSON response.') from None
    if op in ('inspect', 'preview'):
        release = data.get('release') if op == 'preview' and isinstance(data, dict) else data
        if not isinstance(release, dict) or release.get('status') != 'reviewed':
            raise ValueError(_ERRORS[5])
    return data


def _setup(parser):
    subparsers = parser.add_subparsers(dest='operation', required=True)
    for op, (description, required, optional) in _OPERATIONS.items():
        sub = subparsers.add_parser(op, help=description)
        for key in required:
            # Hermes dispatches its global --version from args.version.
            if key == 'version':
                sub.add_argument('release_version', metavar='VERSION')
            else:
                sub.add_argument(key)
        for key in optional:
            sub.add_argument(key, nargs='?')


def register(ctx):
    def cli(args):
        op = args.operation
        _, required, optional = _OPERATIONS[op]
        arguments = {key: getattr(args, 'release_version' if key == 'version' else key)
                     for key in required + optional}
        arguments = {key: value for key, value in arguments.items() if value is not None}
        try:
            print(json.dumps(_run(ctx, op, arguments)))
        except ValueError as exc:
            raise SystemExit(str(exc)) from None

    def make_handler(op):
        def handler(arguments, **_kwargs):
            if op == 'install' and ctx.get_config('agent_install', False) is not True:
                return json.dumps({'error': 'Agent installation is disabled for this profile.'})
            try:
                return json.dumps(_run(ctx, op, arguments))
            except ValueError as exc:
                return json.dumps({'error': str(exc)})
        return handler

    ctx.register_cli_command(name='skillstore', help='Browse and manage reviewed Skillstore releases',
                             setup_fn=_setup, handler_fn=cli)
    for op, (description, required, optional) in _OPERATIONS.items():
        if op == 'install' and ctx.get_config('agent_install', False) is not True:
            continue
        name = f'skillstore_{op}'
        ctx.register_tool(name=name, toolset=_TOOLSET, handler=make_handler(op), schema={
            'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': {key: {'type': 'string'} for key in required + optional},
                           'required': list(required), 'additionalProperties': False},
        })
