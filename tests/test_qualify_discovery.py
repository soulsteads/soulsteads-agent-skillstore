import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def qualify():
    script = Path(__file__).resolve().parents[1] / "scripts/qualify_discovery.py"
    spec = importlib.util.spec_from_file_location("qualify_discovery", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discovery_requires_active_path_and_current_catalog_entry(qualify, tmp_path):
    target = tmp_path / "skills"
    active = str(target / qualify.SKILL / "SKILL.md")
    backup = str(target / ".skillstore" / "backup-one" / "SKILL.md")
    current = qualify.description("2.0.0")
    old = qualify.description("1.0.0")
    expected = {"name": qualify.SKILL, "description": current, "path": active}
    builtin = {"name": "builtin", "description": "Built-in.", "path": "/builtin/SKILL.md"}
    leaked = {"name": "backup-one", "description": old, "path": backup}

    assert qualify.matches(expected, [builtin, expected], target)
    assert not qualify.matches(expected, [expected, expected], target)
    assert not qualify.matches(expected, [expected, leaked], target)
    assert not qualify.matches(expected, [{**expected, "description": old}], target)
    assert not qualify.matches(expected, [{**expected, "path": backup}], target)

    catalog = [{"name": qualify.SKILL, "description": current}]
    assert qualify.matches(expected, catalog, target, [active])
    assert not qualify.matches(expected, catalog, target, [active, backup])
    assert not qualify.matches(expected, catalog, target, [backup])
    assert not qualify.matches(expected, [{"name": "backup-one", "description": current}], target, [active])
    assert not qualify.matches(expected, [], target, [active])


def test_pin_mismatch_refuses_before_installing(qualify, tmp_path):
    binary = tmp_path / "pi"
    binary.write_text(f"#!{sys.executable}\nimport sys\nassert sys.argv[1:] == ['--version']\nprint('9.9.9')\n")
    binary.chmod(0o700)
    state = tmp_path / "state"
    result = subprocess.run(
        [sys.executable, "-B", qualify.__file__, "--state", str(state), "--pi", str(binary),
         "--expect-version", "pi=1.5.2"], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1
    summary = json.loads(result.stdout)
    assert summary["runtime_versions"]["pi"] == "9.9.9"
    assert "version mismatch" in summary["error"]
    assert not (state / "registry").exists()


@pytest.mark.parametrize("leak", [False, True])
def test_openclaw_requires_controls_and_checks_both_scopes(qualify, tmp_path, leak):
    binary = tmp_path / "openclaw"
    binary.write_text(f"#!{sys.executable}\n" + f'''
import json
import os
from pathlib import Path
import sys

assert sys.argv[1:] == ["skills", "list", "--json"]
assert "OPENCLAW_GATEWAY_URL" not in os.environ
workspace = Path.cwd()
config = json.loads(Path(os.environ["OPENCLAW_CONFIG_PATH"]).read_text())
assert config["agents"]["defaults"] == {{"workspace": str(workspace), "skipBootstrap": True}}
state = Path(os.environ["OPENCLAW_STATE_DIR"])
rows = []
for parent, source in [(workspace, "openclaw-workspace"), (state, "openclaw-managed")]:
    roots = [parent / "skills"]
    if {leak!r}:
        roots.append(parent / ".skillstore-skills")
    for root in roots:
        for path in root.glob("*/SKILL.md"):
            doc = dict(line.split(": ", 1) for line in path.read_text().splitlines() if ": " in line)
            rows.append({{**doc, "source": source, "bundled": False, "eligible": True}})
print(json.dumps({{"workspaceDir": str(workspace), "managedSkillsDir": str(state / "skills"), "skills": rows}}))
''')
    binary.chmod(0o700)
    state = tmp_path / "state"
    result = subprocess.run(
        [sys.executable, "-B", qualify.__file__, "--state", str(state), "--openclaw", str(binary),
         "--full-lifecycle"], capture_output=True, text=True, timeout=15,
    )
    assert result.stdout, result.stderr
    summary = json.loads(result.stdout)
    assert result.returncode == int(leak)
    assert set(summary["runtimes"]) == {"openclaw-project", "openclaw-profile"}
    for rows in summary["runtimes"].values():
        assert list(rows) == [*qualify.PHASES, *qualify.EXTRA_PHASES]
        assert {row["status"] for row in rows.values()} == {"incompatible" if leak else "pass"}


def test_profile_probe_keeps_pi_project_skills_out_of_profile_fixture(qualify, tmp_path):
    binary = tmp_path / "pi"
    binary.write_text(f"#!{sys.executable}\n" + '''
import json
import os
from pathlib import Path
import sys

profile_dir = Path(os.environ["PI_CODING_AGENT_DIR"])
assert profile_dir.is_relative_to(os.environ["HOME"])
req = json.load(sys.stdin)
paths = [*Path.cwd().glob(".agents/skills/*/SKILL.md"), *profile_dir.glob("skills/*/SKILL.md")]
assert len(paths) < 2
commands = []
for p in paths:
    meta = dict(line.split(": ", 1) for line in p.read_text().splitlines() if ": " in line)
    commands.append({"name": "skill:" + meta["name"], "source": "skill", "description": meta["description"],
                     "sourceInfo": {"path": str(p)}})
print(json.dumps({"id": req["id"], "type": "response", "command": req["type"], "success": True,
                  "data": {"commands": commands}}))
''')
    binary.chmod(0o700)
    state = tmp_path / "state"
    result = subprocess.run(
        [sys.executable, "-B", qualify.__file__, "--state", str(state), "--pi", str(binary),
         "--profile-scopes", "--full-lifecycle"], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    rows = json.loads(result.stdout)["runtimes"]
    assert set(rows) == {"pi", "pi-profile"}
    project = Path(rows["pi"]["install-v1"]["expected"]["path"])
    profile = Path(rows["pi-profile"]["install-v1"]["expected"]["path"])
    assert project.parent.parent == state / "pi-project/.agents/skills"
    assert profile.parent.parent == state / "pi-profile/home/.pi/agent/skills"
    assert set(rows["pi-profile"]) == {*qualify.PHASES, *qualify.EXTRA_PHASES}


@pytest.mark.parametrize("filename_parameter", [", filename", ""])
def test_hermes_probe_calls_native_scanner(qualify, tmp_path, filename_parameter):
    (tmp_path / "agent").mkdir()
    (tmp_path / "tools").mkdir()
    pattern = "filename" if filename_parameter else "'SKILL.md'"
    (tmp_path / "agent/skill_utils.py").write_text(
        f"def iter_skill_index_files(root{filename_parameter}):\n"
        f"    return root.rglob({pattern})\n"
    )
    (tmp_path / "tools/skills_tool.py").write_text("def find_all_skills(): return []\n")
    skill = tmp_path / "skills/fixture/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.touch()
    env = qualify.fixture_env(tmp_path)
    observed = qualify.hermes_probe(
        Path(sys.executable), tmp_path, skill.parent.parent, env, tmp_path / "hermes-probe",
    )
    assert observed == {"skills": [], "scan_paths": [str(skill)]}


@pytest.mark.parametrize("with_error", [False, True], ids=["clean", "discovery-error"])
def test_codex_qualification_rejects_incomplete_discovery(qualify, tmp_path, with_error):
    codex = tmp_path / "codex-fixture"
    diagnostic = {"path": "unreadable/SKILL.md", "message": "skill could not be read"}
    errors = [diagnostic] if with_error else []
    codex.write_text(f"#!{sys.executable}\n" + f'''
import json
from pathlib import Path
import sys

assert sys.argv[1:] == ["app-server", "--stdio"]
initialize = json.loads(sys.stdin.readline())
assert initialize["method"] == "initialize"
print(json.dumps({{"id": initialize["id"], "result": {{}}}}), flush=True)
assert json.loads(sys.stdin.readline())["method"] == "initialized"
request = json.loads(sys.stdin.readline())
assert request["method"] == "skills/list"
project, = request["params"]["cwds"]
name = {qualify.SKILL!r}
path = Path(project) / ".agents" / "skills" / name / "SKILL.md"
description, = (line.removeprefix("description: ") for line in path.read_text().splitlines()
                if line.startswith("description: "))
entry = {{"name": name, "description": description, "path": str(path)}}
group = {{"cwd": project, "skills": [entry], "errors": {errors!r}}}
print(json.dumps({{"id": request["id"], "result": {{"data": [group]}}}}), flush=True)
''', encoding="utf-8")
    codex.chmod(0o700)
    state = tmp_path / "state"
    result = subprocess.run(
        [sys.executable, "-B", qualify.__file__, "--state", str(state), "--codex", str(codex)],
        capture_output=True, text=True, timeout=15,
    )
    summary = json.loads(result.stdout)
    assert json.loads((state / "summary.json").read_text()) == summary
    assert summary["passed"] is not with_error
    assert result.returncode == int(with_error)
    phases = summary["runtimes"]["codex"]
    assert list(phases) == list(qualify.PHASES)
    for row in phases.values():
        assert row["observed"]["skills"] == [row["expected"]]
        assert row["observed"]["errors"] == errors
        assert row["status"] == ("probe_error" if with_error else "pass")


@pytest.mark.parametrize("catalog", ["active", "extra", "bad_reply"])
def test_pi_qualification_checks_rpc_catalog(qualify, tmp_path, catalog):
    pi = tmp_path / "pi-fixture"
    pi.write_text(f"#!{sys.executable}\n" + f'''
import json
import os
from pathlib import Path
import sys

assert sys.argv[1:] == ["--mode", "rpc", "--offline", "--no-session", "--no-extensions",
                        "--no-mcp", "--no-tools", "--no-context-files", "--approve"]
assert Path(os.environ["PI_CODING_AGENT_DIR"]).is_relative_to(os.environ["HOME"])
request = json.load(sys.stdin)
assert request["type"] == "get_commands"
target = Path.cwd() / ".agents/skills"
name = {qualify.SKILL!r}
path = target / name / "SKILL.md"
description, = (line.removeprefix("description: ") for line in path.read_text().splitlines()
                if line.startswith("description: "))
entry = {{"name": "skill:" + name, "source": "skill", "description": description,
         "sourceInfo": {{"path": str(path)}}}}
commands = [entry]
case = {catalog!r}
if case == "extra":
    commands.append({{**entry, "name": "skill:extra", "description": "Unexpected skill",
                     "sourceInfo": {{"path": str(target / ".skillstore/extra/SKILL.md")}}}})
print(json.dumps({{"type": "response", "command": request["type"], "id": request["id"],
                  "success": case != "bad_reply", "data": {{"commands": commands}}}}))
''', encoding="utf-8")
    pi.chmod(0o700)
    result = subprocess.run(
        [sys.executable, "-B", qualify.__file__, "--state", str(tmp_path / "state"), "--pi", str(pi)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.stdout, result.stderr
    summary = json.loads(result.stdout)
    expected = {"active": "pass", "extra": "incompatible", "bad_reply": "probe_error"}[catalog]
    assert result.returncode == (0 if catalog == "active" else 1)
    phases = summary["runtimes"]["pi"]
    assert list(phases) == list(qualify.PHASES)
    assert [phase["status"] for phase in phases.values()] == [expected] * 3


@pytest.mark.parametrize("case", ["active", "backup", "wrong_scope", "stale", "wrong_request", "bad_catalog"])
def test_claude_qualification_checks_initialize_catalog(qualify, tmp_path, case):
    claude = tmp_path / "claude-fixture"
    claude.write_text(f"#!{sys.executable}\n" + f'''
import json
import os
from pathlib import Path
import sys

assert sys.argv[1:] == ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                        "--verbose", "--no-session-persistence", "--setting-sources", "project",
                        "--tools", "", "--strict-mcp-config", "--mcp-config", '{{"mcpServers":{{}}}}']
assert os.environ["DISABLE_AUTOUPDATER"] == os.environ["DISABLE_TELEMETRY"] == "1"
assert Path(os.environ["CLAUDE_CONFIG_DIR"]).is_relative_to(os.environ["HOME"])
request = json.load(sys.stdin)
assert request["type"] == "control_request" and request["request"] == {{"subtype": "initialize"}}
name = {qualify.SKILL!r}
path = Path.cwd() / ".claude" / "skills" / name / "SKILL.md"
description, = (line.removeprefix("description: ") for line in path.read_text().splitlines()
                if line.startswith("description: "))
commands = [{{"name": name, "description": description + " (project)"}}]
case = {case!r}
if case == "backup":
    commands.append({{"name": "backup-other", "description": description + " (project)"}})
elif case == "wrong_scope":
    commands[0]["description"] = description + " (user)"
elif case == "stale":
    commands[0]["description"] = description.replace("2.0.0", "1.0.0") + " (project)"
elif case == "bad_catalog":
    commands = {{"name": name, "description": description}}
reply_id = "unrelated" if case == "wrong_request" else request["request_id"]
print(json.dumps({{"type": "control_response", "response": {{"request_id": reply_id,
                  "subtype": "success", "response": {{"commands": commands}}}}}}))
''', encoding="utf-8")
    claude.chmod(0o700)
    result = subprocess.run(
        [sys.executable, "-B", qualify.__file__, "--state", str(tmp_path / "state"),
         "--claude", str(claude)], capture_output=True, text=True, timeout=15,
    )
    assert result.stdout, result.stderr
    assert result.returncode == (0 if case == "active" else 1)
    phases = json.loads(result.stdout)["runtimes"]["claude"]
    expected = {
        "active": ["pass"] * 3,
        "backup": ["incompatible"] * 3,
        "wrong_scope": ["incompatible"] * 3,
        "stale": ["pass", "incompatible", "pass"],
        "wrong_request": ["probe_error"] * 3,
        "bad_catalog": ["probe_error"] * 3,
    }[case]
    assert list(phases) == list(qualify.PHASES)
    assert [row["status"] for row in phases.values()] == expected
    if case == "active":
        assert all("path" not in row["observed"]["skills"][0] for row in phases.values())


@pytest.mark.parametrize("case", ["ready", "leak", "wrong_project", "unsafe_url", "no_control"])
def test_opencode_qualification_waits_for_native_catalog(qualify, tmp_path, case):
    binary = tmp_path / "opencode-fixture"
    binary.write_text(f"#!{sys.executable}\n" + f'''
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
from threading import Thread
from urllib.parse import parse_qs, urlsplit

assert sys.argv[1:] == ["serve", "--stdio", "--hostname", "127.0.0.1", "--port", "0"]
assert os.environ["OPENCODE_DISABLE_AUTOUPDATE"] == os.environ["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
assert not sys.stdin.closed
case = {case!r}
project = Path.cwd()
target = project / ".opencode/skills"
active = target / {qualify.SKILL!r} / "SKILL.md"

def skill(path):
    meta = dict(line.split(": ", 1) for line in path.read_text().splitlines() if ": " in line)
    return {{"id": path.parent.name, "path": str(path), **meta}}

control, = (path for path in target.glob("*/SKILL.md") if path != active)
hidden, = target.parent.glob(".skillstore-skills/*control*/SKILL.md")

class Handler(BaseHTTPRequestHandler):
    count = 0

    def do_GET(self):
        assert self.headers["Authorization"] == "Basic " + b64encode(
            ("opencode:" + os.environ["OPENCODE_PASSWORD"]).encode()).decode()
        request = urlsplit(self.path)
        assert request.path == "/api/skill" and parse_qs(request.query) == {{"directory": [str(project)]}}
        Handler.count += 1
        rows = [skill(active)] if active.exists() else []
        if Handler.count == 1:
            rows = []
        elif case != "no_control":
            rows.append(skill(control))
            if case == "leak":
                rows.append(skill(hidden))
        location = "/wrong-workspace" if case == "wrong_project" else str(project)
        body = json.dumps({{"location": {{"directory": location}}, "data": rows}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

with HTTPServer(("127.0.0.1",0), Handler) as server:
    host = "localhost" if case == "unsafe_url" else "127.0.0.1"
    print(json.dumps({{"url": f"http://{{host}}:{{server.server_port}}"}}), flush=True)
    # A missing catalog control must not pass even if the active entry is listed.
    if case == "no_control":
        server.timeout = 3
        server.handle_request()
        server.handle_request()
    else:
        Thread(target=server.serve_forever, daemon=True).start()
        sys.stdin.buffer.read()
        server.shutdown()
''', encoding="utf-8")
    binary.chmod(0o700)
    state = tmp_path / "state"
    result = subprocess.run(
        [sys.executable, "-B", qualify.__file__, "--state", str(state), "--opencode", str(binary)],
        capture_output=True, text=True, timeout=18,
    )
    assert result.stdout, result.stderr
    summary = json.loads(result.stdout)
    phases = summary["runtimes"]["opencode"]
    status = {"ready": "pass", "leak": "incompatible"}.get(case, "probe_error")
    assert result.returncode == (0 if case == "ready" else 1)
    assert [phases[phase]["status"] for phase in qualify.PHASES] == [status] * 3
    if case == "ready":
        assert all(phases[phase]["observed"]["attempts"] == 2 for phase in qualify.PHASES)
    if case == "unsafe_url":
        assert not any(b"GET " in file.read_bytes() for file in state.glob("opencode-*.stderr"))
