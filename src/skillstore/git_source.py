"""Package committed skill files from a checkout or an explicitly selected HTTPS remote."""

from contextlib import suppress
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.parse import urlsplit

from . import package as pkg
from .errors import UsageError, ValidationFailed

_OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_REF = re.compile(r'refs/(?:heads|tags|remotes)/[a-zA-Z0-9._/-]+\Z')
_TREE_LIMIT = 512 * 1024
LOCAL_SOURCE = 'local:git'


def _remote_env(root):
    # No inherited Git config, credential helpers, netrc, proxies, or curl trace.
    env = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(root), 'XDG_CONFIG_HOME': str(root)}
    if ca := os.environ.get('SSL_CERT_FILE'):
        if not Path(ca).is_absolute():
            raise UsageError('SSL_CERT_FILE must be an absolute path for Git remote access')
        env['GIT_SSL_CAINFO'] = ca
    return env


def _git(repo, limit, *args, env=None, fetch=False):
    env = dict(env) if env is not None else {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS='1',
               GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0', GIT_ALLOW_PROTOCOL='https' if fetch else '')
    command = ['git', '--no-optional-locks', '--literal-pathspecs', '-C', str(repo),
               '-c', 'core.hooksPath=/dev/null', *args]
    failure = ('cannot fetch the Git revision over HTTPS; check the URL, TLS trust, access and ref'
               if fetch else 'cannot read the requested Git revision or path')
    timeout = 'timed out fetching the Git remote' if fetch else 'timed out reading Git objects'
    try:
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError as exc:
        raise UsageError('Git-backed skills require the git executable') from exc
    with process, selectors.DefaultSelector() as ready:
        output = bytearray()
        deadline = time.monotonic() + (30 if fetch else 15)
        stream = process.stdout
        assert stream is not None
        ready.register(stream, selectors.EVENT_READ)
        completed = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise ValidationFailed(timeout)
                chunk = os.read(stream.fileno(), min(65536, limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise ValidationFailed('Git source exceeds package limits')
            if process.wait(timeout=max(0.1, deadline - time.monotonic())):
                raise ValidationFailed(failure)
            completed = True
            return bytes(output)
        except subprocess.TimeoutExpired as exc:
            raise ValidationFailed(timeout) from exc
        finally:
            if not completed:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)


def https_source(uri):
    error = 'Git source must be a credential-free HTTPS URL without query or fragment'
    if (not uri or len(uri) > 4096 or
            any(c.isspace() or ord(c) < 32 or c in '?#\\' for c in uri)):
        raise UsageError(error)
    try:
        parsed = urlsplit(uri)
        parsed.port  # Reject malformed ports; don't print raw URLs in diagnostics.
    except ValueError as exc:
        raise UsageError(error) from exc
    if (not uri.startswith('https://') or not parsed.hostname or parsed.username is not None or
            parsed.password is not None or not parsed.path.strip('/') or '%' in parsed.netloc):
        raise UsageError(error)
    return uri


def git_files(repo, path, revision, uri=None, *, remote=None, expected_name=None):
    """Return validated package files and their exact repository/commit/path provenance."""
    if bool(repo) == bool(remote):
        raise UsageError('select exactly one --repo or --git-remote (or SKILLSTORE_GIT_REPO or SKILLSTORE_GIT_REMOTE)')
    if (len(path) > 4096 or any(not part or part.startswith('.') for part in path.split('/')) or
            '\\' in path or any(ord(c) < 32 for c in path)):
        raise UsageError('Git skill path must be a relative directory without hidden or dot components')
    if (len(revision) > 255 or not (revision == 'HEAD' or _OID.fullmatch(revision) or _REF.fullmatch(revision))):
        raise UsageError('Git revision must be HEAD, a full commit ID, or an explicit refs/heads, refs/tags or refs/remotes name')
    if remote:
        if uri is not None:
            raise UsageError('--source is for local checkouts; --git-remote is already the source URL')
        if revision.startswith('refs/remotes/'):
            raise UsageError('select a branch or tag on the Git server, not a local refs/remotes ref')
        return _remote_files(https_source(remote), path, revision, expected_name)
    uri = LOCAL_SOURCE if uri is None or uri == LOCAL_SOURCE else https_source(uri)
    root = Path(repo)
    if not root.is_absolute() or not root.is_dir() or not (root / '.git').exists():
        raise UsageError('Git source requires the absolute root of a local checkout')
    return _committed_files(root, path, revision, uri, expected_name)


def _remote_files(uri, path, revision, expected_name):
    with TemporaryDirectory(prefix='skillstore-git-') as scratch:
        root = Path(scratch)
        env = _remote_env(root)
        _git(root, 4096, 'init', '--bare', '--quiet', '--template=', '.', env=env)
        _git(root, 4096, '-c', 'http.sslVerify=true', '-c', 'http.followRedirects=false',
             '-c', 'fetch.fsckObjects=true', 'fetch', '--quiet', '--depth=1', '--no-tags',
             '--no-recurse-submodules', '--no-auto-gc', '--', uri, revision, env=env, fetch=True)
        return _committed_files(root, path, 'FETCH_HEAD', uri, expected_name, env=env,
                                pinned=revision if _OID.fullmatch(revision) else None)


def _committed_files(root, path, revision, uri, expected_name, *, env=None, pinned=None):
    commit = _git(root, 128, 'rev-parse', '--verify', '--end-of-options', revision + '^{commit}',
                  env=env).decode('ascii').strip()
    if not _OID.fullmatch(commit) or (pinned is not None and commit != pinned):
        raise ValidationFailed('Git did not resolve an exact commit ID')
    rows = _git(root, _TREE_LIMIT, 'ls-tree', '-r', '-l', '-z', '--full-tree', commit, '--', path, env=env)
    files, total = {}, 0
    for row in rows.split(b'\0')[:-1]:
        header, raw = row.split(b'\t', 1)
        mode, kind, blob_id, length = header.decode('ascii').split()
        try:
            name = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValidationFailed('Git skill contains a filename that is not UTF-8') from exc
        if not name.startswith(path + '/') or kind != 'blob' or mode not in ('100644', '100755'):
            raise ValidationFailed('Git skill must be a directory of regular files, without links or submodules')
        rel = name[len(path) + 1:]
        if any(part.startswith('.') for part in rel.split('/')) or not _OID.fullmatch(blob_id):
            raise ValidationFailed('Git skill contains hidden or invalid entries')
        size = int(length)
        total += size
        if len(files) >= pkg.MAX_FILES or size > pkg.MAX_FILE_BYTES or total > pkg.MAX_TOTAL_BYTES:
            raise ValidationFailed('Git source exceeds package limits')
        content = _git(root, size, 'cat-file', 'blob', blob_id, env=env)
        if len(content) != size:
            raise ValidationFailed('Git blob size does not match its tree entry')
        files[rel] = (content, mode == '100755')
    pkg.require_valid(files, expected_name=expected_name)
    return files, {'type': 'git', 'uri': uri, 'revision': commit, 'path': path}
