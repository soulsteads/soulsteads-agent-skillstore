"""Native Hermes adapter for Skillstore's local backup and restore commands."""

import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from hermes_constants import get_hermes_home

_TOOL = 'skillstore_backup_skills'
_ARCHIVE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.tar\.gz\Z')


def _path(value, setting):
    if (not isinstance(value, str) or not value.startswith('/') or
            any(part in ('', '.', '..') for part in value[1:].split('/')) or '\x00' in value):
        raise ValueError(f'configure an absolute {setting} in the skillstore-backup plugin settings')
    return Path(value)


def _run(ctx, action, archive=None, digest=None):
    executable = _path(ctx.get_config('executable'), 'executable')
    directory = _path(ctx.get_config('backup_dir'), 'backup_dir')
    profile = get_hermes_home().resolve()
    if directory.is_relative_to(profile):
        raise ValueError('backup_dir must be outside the active Hermes profile')
    if action == 'backup':
        args = ['backup-hermes', '--profile-root', str(profile), '--output',
                str(directory / f'hermes-skills-{uuid.uuid4().hex}.tar.gz')]
    else:
        if not isinstance(archive, str) or not _ARCHIVE.fullmatch(archive):
            raise ValueError('provide an archive basename from the configured backup directory')
        output = directory / f'restored-{uuid.uuid4().hex}'
        args = ['restore-hermes', str(directory / archive), '--sha256', digest, '--output', str(output)]
    env = {key: value for key, value in os.environ.items()
           if key in {'HOME', 'PATH', 'LANG', 'TMPDIR'} or key.startswith('LC_')}
    result = subprocess.run([str(executable), *args], stdin=subprocess.DEVNULL, env=env,
                            text=True, capture_output=True, timeout=180)
    if result.returncode:
        try:
            message = json.loads(result.stderr).get('message', 'no receipt returned')
        except (ValueError, AttributeError):
            message = 'no receipt returned; verify the installed Skillstore version and settings'
        raise ValueError(f'Skillstore {action} failed: {message}')
    return json.loads(result.stdout)


def _setup(parser):
    operations = parser.add_subparsers(dest='operation', required=True)
    operations.add_parser('backup', help='write a private skills-only archive')
    restore = operations.add_parser('restore', help='stage a verified archive without changing live skills')
    restore.add_argument('archive', help='archive basename within the configured backup directory')
    restore.add_argument('--sha256', required=True)


def register(ctx):
    def cli(args):
        try:
            result = _run(ctx, args.operation, getattr(args, 'archive', None), getattr(args, 'sha256', None))
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise SystemExit(str(exc)) from None
        print(json.dumps(result))

    def backup(args, **_kwargs):
        if not isinstance(args, dict) or args:
            return json.dumps({'error': 'This tool takes no arguments.'})
        if ctx.get_config('agent_backup', False) is not True:
            return json.dumps({'error': 'Agent backups are disabled for this profile.'})
        try:
            return json.dumps(_run(ctx, 'backup'))
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return json.dumps({'error': str(exc)})

    ctx.register_cli_command(name='skillstore-backup', help='Back up or recover Hermes skills',
                             setup_fn=_setup, handler_fn=cli)
    if ctx.get_config('agent_backup', False) is True:
        ctx.register_tool(name=_TOOL, toolset='skillstore-backup', handler=backup, schema={
            'name': _TOOL,
            'description': 'Create a private local archive of the active Hermes profile skills. Returns a receipt, not files. No upload.',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        })
