import argparse
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType


def test_native_plugin_commands_backup_and_recover_without_touching_live_skills(tmp_path, monkeypatch, capsys):
    profile = tmp_path / 'profile'
    skills = profile / 'skills'
    skills.mkdir(parents=True)
    skill = skills / 'SKILL.md'
    skill.write_text('before backup')
    output = tmp_path / 'backups'
    output.mkdir(mode=0o700)
    root = Path(__file__).resolve().parents[1]
    cli = tmp_path / 'skillstore'
    cli.write_text(f'#!{sys.executable}\nimport os, sys\nassert "SKILLSTORE_TOKEN" not in os.environ\n'
                   f'sys.path.insert(0, {str(root / "src")!r})\n'
                   'from skillstore.cli import main\nraise SystemExit(main())\n')
    cli.chmod(0o700)
    monkeypatch.setenv('SKILLSTORE_TOKEN', 'synthetic-unused-token')
    hermes = ModuleType('hermes_constants')
    setattr(hermes, 'get_hermes_home', lambda: profile)
    monkeypatch.setitem(sys.modules, 'hermes_constants', hermes)
    file = root / 'plugins' / 'skillstore-backup' / '__init__.py'
    spec = importlib.util.spec_from_file_location('skillstore_backup_plugin', file)
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    class Context:
        settings = {'executable': str(cli), 'backup_dir': str(output), 'agent_backup': True}
        tools = {}

        def get_config(self, key, default=None):
            return self.settings.get(key, default)

        def register_cli_command(self, **spec):
            self.parser = argparse.ArgumentParser(prog=spec['name'])
            spec['setup_fn'](self.parser)
            self.handler = spec['handler_fn']

        def register_tool(self, **spec):
            self.tools[spec['name']] = spec

    ctx = Context()
    plugin.register(ctx)
    tool, = ctx.tools.values()
    assert not tool['schema']['parameters']['properties']
    denied = json.loads(tool['handler']({'path': str(tmp_path)}))
    assert 'error' in denied and not list(output.iterdir())
    ctx.handler(ctx.parser.parse_args(['backup']))
    saved = json.loads(capsys.readouterr().out)
    assert 'before backup' not in json.dumps(saved)
    assert 'sha256' in saved
    skill.write_text('live edit must survive')
    ctx.handler(ctx.parser.parse_args(['restore', Path(saved['archive']).name, '--sha256', saved['sha256']]))
    restored = json.loads(capsys.readouterr().out)
    assert (Path(restored['skills']) / 'SKILL.md').read_text() == 'before backup'
    assert skill.read_text() == 'live edit must survive'
    assert 'archive' in json.loads(tool['handler']({}))
    ctx.settings['agent_backup'] = False
    ctx.tools = {}
    plugin.register(ctx)
    assert ctx.tools == {}
