import argparse
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from threading import Thread

import pytest

from skillstore import package
from skillstore.install import Installation
from skillstore.server import add_token, make_server, revoke_tokens
from skillstore.store import LocalStore, make_actor


ROOT = Path(__file__).resolve().parents[1]
SKILL = b'---\nname: demo-skill\ndescription: Summarize a short note.\n---\nSummarize the note.\n'


class Context:
    def __init__(self, **settings):
        self.settings = settings
        self.tools = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_cli_command(self, *, name, help, setup_fn, handler_fn):
        self.parser = argparse.ArgumentParser(prog='hermes')
        self.parser.add_argument('--version', action='store_true')
        setup_fn(self.parser.add_subparsers(required=True).add_parser(name, help=help))
        self.command_name = name
        self.cli = handler_fn

    def register_tool(self, *, name, **definition):
        self.tools[name] = definition

    def call(self, op, **arguments):
        return json.loads(self.tools[f'skillstore_{op}']['handler'](arguments))

    def command(self, *args):
        parsed = self.parser.parse_args((self.command_name, *args))
        assert parsed.version is False, 'plugin arguments must not shadow the Hermes version flag'
        self.cli(parsed)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    profile = tmp_path / 'profile'
    profile.mkdir()
    hermes = ModuleType('hermes_constants')
    setattr(hermes, 'get_hermes_home', lambda: profile)
    monkeypatch.setitem(sys.modules, 'hermes_constants', hermes)
    exe = tmp_path / 'soulsteads-skillstore'
    exe.write_text(f'#!{sys.executable}\nimport sys\n'
                   f'sys.path.insert(0, {str(ROOT / "src")!r})\n'
                   'from skillstore.cli import main\nraise SystemExit(main())\n')
    exe.chmod(0o700)
    store = LocalStore.init(str(tmp_path / 'registry'), ['reviewer'], make_actor('reviewer'))
    source = {'type': 'source', 'uri': 'https://example.org/demo.git', 'revision': 'fixture-v1'}
    for version in ('1.0.0', '2.0.0'):
        store.submit(package.pack({'SKILL.md': (SKILL, False)}), version, make_actor('author'), source)
    store.review('demo-skill', '1.0.0', 'approve', make_actor('reviewer'), 'Reviewed demo')
    path = ROOT / 'plugins/skillstore/__init__.py'
    assert path.is_file(), 'missing Hermes catalog plugin'
    spec = importlib.util.spec_from_file_location('skillstore_catalog_plugin', path)
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    return plugin, Context(executable=str(exe), store=str(store.root)), profile


def test_search_is_native_and_cli_use_the_configured_reviewed_catalog(fixture, monkeypatch, capsys):
    plugin, ctx, profile = fixture
    monkeypatch.setenv('SKILLSTORE_STORE', str(profile / 'wrong-registry'))
    plugin.register(ctx)
    result = ctx.call('search', query='-summarize')
    assert result == []
    result = ctx.call('search', query='summarize')
    assert [(row['name'], row['latest_reviewed']) for row in result] == [('demo-skill', '1.0.0')]
    ctx.command('search', 'summarize')
    assert json.loads(capsys.readouterr().out) == result
    assert not list(profile.iterdir())


def test_reviewed_preview_and_terminal_install_stay_in_active_profile(fixture, capsys):
    plugin, ctx, profile = fixture
    plugin.register(ctx)
    assert set(ctx.tools) == {'skillstore_search', 'skillstore_inspect', 'skillstore_preview', 'skillstore_status'}
    args = {'name': 'demo-skill', 'version': '1.0.0'}
    release = ctx.call('inspect', **args)
    assert release['status'] == 'reviewed'
    assert ctx.call('preview', **args)['release']['digest'] == release['digest']
    assert 'error' in ctx.call('inspect', name='demo-skill', version='2.0.0')
    assert 'error' in ctx.call('preview', name='demo-skill', version='2.0.0')
    assert not (profile / 'skills').exists()

    with pytest.raises(SystemExit, match='not an active reviewed release'):
        ctx.command('install', 'demo-skill', '2.0.0')
    assert not (profile / 'skills/demo-skill').exists()
    ctx.command('install', 'demo-skill', '1.0.0')
    assert json.loads(capsys.readouterr().out)['digest'] == release['digest']
    assert (profile / 'skills/demo-skill/SKILL.md').read_bytes() == SKILL

    del ctx.settings['store']
    status = ctx.call('status')
    assert status['target'] == str(profile / 'skills')
    assert status['installed'][0]['local'] == 'clean'


def test_agent_install_requires_explicit_opt_in_and_cannot_override_target(fixture):
    plugin, ctx, profile = fixture
    ctx.settings['agent_install'] = 'true'
    plugin.register(ctx)
    assert 'skillstore_install' not in ctx.tools
    ctx.settings['agent_install'] = True
    plugin.register(ctx)
    args = {'name': 'demo-skill', 'version': '1.0.0'}
    assert 'error' in ctx.call('install', **args, target=str(profile.parent))
    assert 'error' in ctx.call('install', name='demo-skill')
    assert not (profile / 'skills').exists()
    assert ctx.call('install', **args)['version'] == '1.0.0'
    installed = profile / 'skills/demo-skill/SKILL.md'
    installed.write_text('local edit')
    assert 'conflict' in ctx.call('install', **args)['error']
    assert installed.read_text() == 'local edit'
    ctx.settings['agent_install'] = False
    assert 'disabled' in ctx.call('install', **args)['error']


def test_native_preview_and_status_do_not_recover_interrupted_install(fixture, monkeypatch):
    plugin, ctx, profile = fixture
    plugin.register(ctx)
    store = LocalStore(ctx.settings['store'])
    store.review('demo-skill', '2.0.0', 'approve', make_actor('reviewer'), 'Reviewed update')
    target = Installation(profile / 'skills')
    target.install(store, 'demo-skill', '1.0.0')
    import os
    replace = os.replace

    def interrupt(source, dest, *args, **kwargs):
        if Path(source).name.startswith('stage-') and Path(dest).name == 'demo-skill':
            raise OSError('interrupted before publish')
        return replace(source, dest, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', interrupt)
        with pytest.raises(OSError, match='interrupted before publish'):
            target.install(store, 'demo-skill', '2.0.0')
    journal = target.meta / 'transaction.json'
    original = journal.read_bytes()
    assert 'conflict' in ctx.call('status')['error']
    assert 'conflict' in ctx.call('preview', name='demo-skill', version='2.0.0')['error']
    assert journal.read_bytes() == original
    assert not (target.root / 'demo-skill').exists()
    assert target.status()['installed'][0]['version'] == '2.0.0'


def test_remote_native_search_uses_profile_token_and_revocation(fixture, monkeypatch):
    plugin, ctx, _ = fixture
    root = ctx.settings['store']
    token = add_token(root, 'demo-agent', 'agent')
    with make_server(root, require_token_for_read=True, quiet=True) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ctx.settings['store'] = f'http://127.0.0.1:{server.server_port}'
            plugin.register(ctx)
            monkeypatch.delenv('SKILLSTORE_TOKEN', raising=False)
            assert 'denied' in ctx.call('search')['error']
            monkeypatch.setenv('SKILLSTORE_TOKEN', token)
            assert ctx.call('search')[0]['latest_reviewed'] == '1.0.0'
            revoke_tokens(root, 'demo-agent')
            assert 'denied' in ctx.call('search')['error']
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize('shape', ['userinfo', 'query', 'fragment'])
def test_remote_store_configuration_never_puts_url_credentials_in_argv(fixture, monkeypatch, capsys, shape):
    plugin, ctx, _ = fixture
    scheme, host, value = 'https://', 'example.invalid', 'synthetic-fixture'
    suffix = {'userinfo': f'demo:{value}@{host}', 'query': f'{host}?key={value}',
              'fragment': f'{host}#{value}'}
    ctx.settings['store'] = scheme + suffix[shape]
    plugin.register(ctx)
    calls = []

    def record(argv, **kwargs):
        calls.append(argv)
        return plugin.subprocess.CompletedProcess(argv, 0, '[]', '')

    monkeypatch.setattr(plugin.subprocess, 'run', record)
    result = ctx.call('search')
    assert calls == []
    assert 'error' in result and value not in str(result)
    with pytest.raises(SystemExit):
        ctx.command('search')
    assert capsys.readouterr().out == ''
    assert calls == []
