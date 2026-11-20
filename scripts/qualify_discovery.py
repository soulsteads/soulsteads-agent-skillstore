"""Qualify native skill discovery with reviewed fixtures and no model calls."""

import argparse
import base64
from contextlib import closing, contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode

from skillstore import cli, package
from skillstore.store import LocalStore, make_actor


SKILL = "skillstore-discovery-fixture"
VERSIONS = ("1.0.0", "2.0.0")
PHASES = ("install-v1", "install-v2", "rollback-v1")
EXTRA_PHASES = ("uninstall", "restore-v2")
ASSET = "references/nested/receipt.txt"
TIMEOUT = 15
OPENCODE_CONTROL = "skillstore-native-control"
OPENCODE_HIDDEN = "skillstore-metadata-control"
CONTROL_DESCRIPTION = "Harmless Skillstore discovery control."


def description(version):
    return f"Harmless Skillstore native discovery fixture, version {version}."


def matches(expected, skills, target, scanned=None):
    fixture_descriptions = {description(version) for version in VERSIONS}
    catalog = [
        skill for skill in skills
        if skill["name"] == SKILL
        or skill.get("id") == SKILL
        or skill["name"] == OPENCODE_HIDDEN
        or skill.get("id") == OPENCODE_HIDDEN
        or skill["description"] in fixture_descriptions
        or (isinstance(skill.get("path"), str) and Path(skill["path"]).is_relative_to(target))
    ]
    if expected is None:
        return not catalog and (scanned is None or not scanned)
    if scanned is None:
        return len(catalog) == 1 and all(catalog[0].get(k) == v for k, v in expected.items())
    return (
        scanned == [expected["path"]]
        and len(catalog) == 1
        and all(catalog[0][key] == expected[key] for key in ("name", "description"))
    )


def fixture_env(root):
    env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "CODEX_HOME", "HERMES_HOME", "TMPDIR"):
        path = root / key.lower()
        path.mkdir(mode=0o700)
        env[key] = str(path)
    return env


@contextmanager
def probe_process(command, cwd, env, evidence, **pipes):
    env = {**env, "PATH": os.pathsep.join((str(command[0].parent), env["PATH"]))}
    with evidence.with_suffix(".stderr").open("wb") as stderr:
        with subprocess.Popen(command, cwd=cwd, env=env, stderr=stderr,
                              start_new_session=True, **pipes) as process:
            try:
                yield process
            finally:
                # Own process group only, including descendants of executable wrappers.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)


def codex_probe(binary, project, env, evidence):
    command = [binary, "app-server", "--stdio"]
    with (evidence.with_suffix(".jsonl").open("wb") as transcript,
          probe_process(command, project, env, evidence, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE) as process):
        pending = b""

        def send(method, request_id=None, params=None):
            message = {"method": method}
            if request_id is not None:
                message.update(id=request_id, params=params)
            process.stdin.write(json.dumps(message).encode() + b"\n")
            process.stdin.flush()

        def reply(request_id):
            nonlocal pending
            deadline = time.monotonic() + TIMEOUT
            received = len(pending)
            while time.monotonic() < deadline and received <= 4 * 1024 * 1024:
                if b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("id") == request_id and "method" not in message:
                        if "error" in message:
                            raise ValueError(f"Codex RPC error: {message['error']}")
                        return message["result"]
                    continue
                remaining = max(0, deadline - time.monotonic())
                if not select.select([process.stdout], [], [], remaining)[0]:
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise ValueError(f"Codex closed stdout before replying to request {request_id}")
                transcript.write(chunk)
                pending += chunk
                received += len(chunk)
            raise ValueError(f"Codex reply {request_id} exceeded time or output limit")

        send("initialize", 1, {"clientInfo": {"name": "skillstore-discovery", "version": "1.0.0"}})
        initialized = reply(1)
        send("initialized")
        send("skills/list", 2, {"cwds": [str(project)], "forceReload": True})
        listing = reply(2)
    groups = listing["data"]
    if not groups or not any(group["cwd"] == str(project) for group in groups):
        raise ValueError("Codex reply has no result for the fixture project")
    return {"initialize": initialized,
            "skills": [skill for group in groups for skill in group["skills"]],
            "errors": [error for group in groups for error in group.get("errors", [])]}


def request_jsonl(runtime, command, project, env, evidence, request):
    with evidence.with_suffix(".jsonl").open("w+b") as transcript:
        with probe_process(command, project, env, evidence, stdin=subprocess.PIPE,
                           stdout=transcript, encoding="utf-8") as process:
            process.communicate(json.dumps(request) + "\n", timeout=TIMEOUT)
            if process.returncode:
                raise ValueError(f"{runtime} probe exited {process.returncode}")
        transcript.seek(0)
        limit = 4 * 1024 * 1024
        output = transcript.read(limit + 1)
    if len(output) > limit:
        raise ValueError(f"{runtime} response exceeded output limit")
    return [json.loads(line) for line in output.splitlines()]


def pi_probe(binary, project, env, evidence):
    command = [binary, "--mode", "rpc", "--offline", "--no-session", "--no-extensions",
               "--no-mcp", "--no-tools", "--no-context-files", "--approve"]
    env = {**env, "PI_CODING_AGENT_DIR": str(Path(env["HOME"]) / ".pi/agent")}
    request = {"id": "skillstore", "type": "get_commands"}
    messages = request_jsonl("Pi", command, project, env, evidence, request)
    replies = [message for message in messages
               if message.get("id") == request["id"]]
    if len(replies) != 1 or replies[0].get("type") != "response" or (
        replies[0].get("command") != request["type"] or replies[0].get("success") is not True
    ):
        raise ValueError("Pi RPC did not return a successful get_commands response")
    commands = replies[0]["data"]["commands"]
    if not isinstance(commands, list):
        raise ValueError("Unexpected Pi command catalog response")
    skills = []
    for entry in commands:
        if entry["source"] == "skill":
            prefix, separator, name = entry["name"].partition(":")
            if prefix != "skill" or not separator or not name:
                raise ValueError("Invalid skill name in Pi command catalog")
            skills.append({"name": name, "description": entry["description"],
                           "path": entry["sourceInfo"]["path"]})
    return {"skills": skills}


def claude_probe(binary, project, env, evidence, source="project"):
    command = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--no-session-persistence", "--setting-sources", source, "--tools", "",
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    env = {**env, "CLAUDE_CONFIG_DIR": str(Path(env["HOME"]) / ".claude"),
           "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1"}
    request = {"type": "control_request", "request_id": "skillstore", "request": {"subtype": "initialize"}}
    messages = request_jsonl("Claude Code", command, project, env, evidence, request)
    replies = [message["response"] for message in messages if message.get("type") == "control_response"
               and message["response"].get("request_id") == request["request_id"]]
    if len(replies) != 1 or replies[0].get("subtype") != "success":
        raise ValueError("Claude Code did not return a successful initialize response")
    payload = replies[0]["response"]
    commands = payload["commands"]
    if not isinstance(commands, list):
        raise ValueError("Unexpected Claude Code command catalog response")
    suffix = f" ({source})"
    skills = [{"name": entry["name"], "description": entry["description"].removesuffix(suffix),
               "source": source if entry["description"].endswith(suffix) else "other"}
              for entry in commands]
    return {"version": payload.get("claude_code_version"), "skills": skills}


def opencode_control(path, name):
    path.parent.mkdir()
    path.write_text(f"---\nname: {name}\ndescription: {CONTROL_DESCRIPTION}\n---\nSynthetic only.\n",
                    encoding="utf-8")
    return {"id": name, "name": name, "description": CONTROL_DESCRIPTION, "path": str(path)}


def opencode_port(stdout, deadline):
    pending = b""
    while b"\n" not in pending and len(pending) < 4096:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stdout], [], [], remaining)[0]:
            break
        chunk = os.read(stdout.fileno(), 4096 - len(pending))
        if not chunk:
            break
        pending += chunk
    if b"\n" not in pending:
        raise ValueError("OpenCode did not return a bounded server handshake")
    url = json.loads(pending.split(b"\n", 1)[0])["url"]
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/?", url)
    if not match or int(match[1]) > 65535:
        raise ValueError("OpenCode did not announce a literal IPv4 loopback endpoint")
    return int(match[1])


def opencode_probe(binary, project, env, evidence, control):
    password = secrets.token_urlsafe(32)
    authorization = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()
    command = [binary, "serve", "--stdio", "--hostname", "127.0.0.1", "--port", "0"]
    server_env = {**env, "OPENCODE_PASSWORD": password, "OPENCODE_DISABLE_AUTOUPDATE": "1",
                  "OPENCODE_DISABLE_MODELS_FETCH": "1"}
    route = "/api/skill?" + urlencode({"directory": str(project)})
    deadline = time.monotonic() + TIMEOUT
    with probe_process(command, project, server_env, evidence, stdin=subprocess.PIPE,
                       stdout=subprocess.PIPE) as process:
        assert process.stdin is not None and process.stdout is not None
        try:
            port = opencode_port(process.stdout, deadline)
            attempts = 0
            while (remaining := deadline - time.monotonic()) > 0:
                if process.poll() is not None:
                    raise ValueError("OpenCode exited before its native catalog was ready")
                attempts += 1
                # No environment proxies, redirects, or endpoints outside the private local server.
                try:
                    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=min(remaining, 2))) as client:
                        client.request("GET", route, headers={"Authorization": authorization})
                        response = client.getresponse()
                        if response.status != 200:
                            raise ValueError(f"OpenCode skill catalog returned HTTP {response.status}")
                        limit = 1024 * 1024
                        raw = response.read(limit + 1)
                except http.client.HTTPException:
                    raise ValueError("OpenCode returned an unusable HTTP response") from None
                if len(raw) > limit:
                    raise ValueError("OpenCode skill catalog exceeded output limit")
                if time.monotonic() >= deadline:
                    break
                evidence.with_suffix(".native.json").write_bytes(raw)
                catalog = json.loads(raw)
                if catalog["location"]["directory"] != str(project):
                    raise ValueError("OpenCode skill catalog is not for the fixture project")
                rows = catalog["data"]
                if not isinstance(rows, list):
                    raise ValueError("Unexpected OpenCode skill catalog response")
                skills = [{"id": row["id"], "name": row["name"], "path": row["path"],
                           "description": row.get("description", "")} for row in rows]
                controls = [row for row in skills if row["id"] == control["id"]]
                if controls:
                    if controls != [control]:
                        raise ValueError("OpenCode returned a mismatched or duplicate discovery control")
                    return {"skills": [row for row in skills if row != control],
                            "control": control, "attempts": attempts}
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            raise ValueError("OpenCode did not discover the positive control before the deadline")
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass  # probe_process owns the group and completes cleanup.


# Run from the supplied source with its own interpreter and native imports.
HERMES_PROBE = """
from inspect import signature
import json
from os import fspath
from pathlib import Path
import sys
from agent.skill_utils import iter_skill_index_files
from tools.skills_tool import find_all_skills

catalog = find_all_skills()
root = Path(sys.argv[1])
try:
    signature(iter_skill_index_files).bind(root, "SKILL.md")
except TypeError:
    files = iter_skill_index_files(root)
else:
    files = iter_skill_index_files(root, "SKILL.md")
paths = sorted(str(Path(path).absolute()) for path in files)
observed = {"skills": catalog, "scan_paths": paths}
Path(sys.argv[2]).write_text(json.dumps(observed, default=fspath), encoding="utf-8")
"""


def hermes_probe(binary, source, target, env, evidence):
    result = evidence.with_suffix(".native.json")
    command = [binary, "-B", "-c", HERMES_PROBE, str(target), str(result)]
    with (evidence.with_suffix(".stdout").open("wb") as stdout,
          probe_process(command, source, env, evidence, stdin=subprocess.DEVNULL,
                        stdout=stdout) as process):
        code = process.wait(timeout=TIMEOUT)
        if code:
            raise ValueError(f"Hermes probe exited {code}")
    return json.loads(result.read_text(encoding="utf-8"))


def command_output(command, cwd, env, evidence):
    result = evidence.with_suffix(".stdout")
    with result.open("wb") as output, probe_process(command, cwd, env, evidence,
                                                   stdin=subprocess.DEVNULL, stdout=output) as process:
        if process.wait(timeout=TIMEOUT):
            raise ValueError(f"{command[0].name} exited unsuccessfully; inspect probe evidence")
    if result.stat().st_size > 1024 * 1024:
        raise ValueError("Native response exceeded output limit")
    return result.read_text(encoding="utf-8")


def openclaw_probe(binary, workspace, state, env, evidence, scope):
    config = state.parent / "openclaw.json"
    env = {**env, "OPENCLAW_STATE_DIR": str(state), "OPENCLAW_CONFIG_PATH": str(config),
           "NODE_NO_WARNINGS": "1"}
    # Reserve the configured loopback port without listening so the client
    # cannot attach to a running gateway and must list the fixture locally.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        config.write_text(json.dumps({"agents": {"defaults": {"workspace": str(workspace), "skipBootstrap": True}},
                                      "gateway": {"port": reservation.getsockname()[1]}}), encoding="utf-8")
        raw = command_output([binary, "skills", "list", "--json"], workspace, env, evidence)
    catalog = json.loads(raw)
    evidence.with_suffix(".native.json").write_text(raw, encoding="utf-8")
    if (catalog["workspaceDir"] != str(workspace) or catalog["managedSkillsDir"] != str(state / "skills")
            or not isinstance(catalog["skills"], list)):
        raise ValueError("OpenClaw returned a catalog for unexpected roots")
    source = "openclaw-workspace" if scope == "project" else "openclaw-managed"
    controls = [row for row in catalog["skills"] if row["name"] == OPENCODE_CONTROL]
    if (len(controls) != 1 or controls[0]["description"] != CONTROL_DESCRIPTION
            or controls[0]["source"] != source or controls[0]["eligible"] is not True):
        raise ValueError("OpenClaw did not discover the exact visible control")
    return {"skills": [row for row in catalog["skills"] if row not in controls], "control": controls[0]}


def check_versions(args, root, env, summary):
    # An unpinned exploratory run is valid but is not evidence for a pinned matrix.
    summary["runtime_versions"] = versions = {}
    if not args.expected_versions:
        return
    executables = {"codex": args.codex, "pi": args.pi, "claude": args.claude,
                   "opencode": args.opencode, "openclaw": args.openclaw, "hermes": args.hermes_python}
    for name, binary in executables.items():
        if not binary:
            continue
        evidence = root / f"{name}-version"
        if name == "hermes":
            git = Path("/usr/bin/git")
            source = args.hermes_source
            if command_output([git, "status", "--porcelain", "--untracked-files=no"], source, env, root / "hermes-dirty"):
                raise ValueError("Hermes source has tracked changes; use a clean checkout for a pinned run")
            command, cwd = [git, "rev-parse", "HEAD"], source
        else:
            command, cwd = [binary, "--version"], root
        versions[name] = actual = command_output(command, cwd, env, evidence).strip()
        if actual != args.expected_versions[name]:
            raise ValueError(f"{name} version mismatch: expected {args.expected_versions[name]!r}, observed {actual!r}")


def stage_releases(root):
    reviewer = make_actor("fixture-reviewer", "human")
    author = make_actor("fixture-author", "human")
    store = LocalStore.init(root / "registry", ["fixture-reviewer"], reviewer)
    for version in VERSIONS:
        markdown = f"---\nname: {SKILL}\ndescription: {description(version)}\n---\nSynthetic discovery fixture.\n"
        tree = {"SKILL.md": (markdown.encode(), False), ASSET: (version.encode(), False)}
        store.submit(package.pack(tree), version, author)
        store.review(SKILL, version, "approve", reviewer, "Harmless discovery fixture")
    return store


def qualify(args, root, summary):
    env = fixture_env(root)
    check_versions(args, root, env, summary)
    project = root / "project"
    project.mkdir()
    (project / ".git").mkdir()  # Bound Codex's project discovery to the fixture.
    targets = {}
    openclaw_fixtures = {}
    if args.codex:
        targets["codex"] = ("codex", "project", project)
    if args.hermes_python:
        targets["hermes"] = ("hermes", "profile", Path(env["HERMES_HOME"]))
    pi_project = root / "pi-project"
    if args.pi:
        pi_project.mkdir()
        (pi_project / ".git").mkdir()
        targets["pi"] = ("pi", "project", pi_project)
    claude_project = root / "claude-project"
    if args.claude:
        claude_project.mkdir()
        (claude_project / ".git").mkdir()
        targets["claude"] = ("claude-code", "project", claude_project)
    opencode_project = root / "opencode-project"
    if args.opencode:
        opencode_project.mkdir()
        (opencode_project / ".git").mkdir()
        targets["opencode"] = ("opencode", "project", opencode_project)
    projects = {"codex": project, "pi": pi_project, "claude": claude_project, "opencode": opencode_project}
    profile_fixtures = {}
    if args.profile_scopes:
        for flavor, harness in (("codex", "codex"), ("pi", "pi"), ("claude", "claude-code"), ("opencode", "opencode")):
            if not getattr(args, flavor):
                continue
            runtime = f"{flavor}-profile"
            fixture = root / runtime
            fixture.mkdir()
            isolated = fixture_env(fixture)
            workspace = fixture / "workspace"
            workspace.mkdir()
            (workspace / ".git").mkdir()
            home = Path(isolated["HOME"])
            profile = {"codex": home, "claude": home, "pi": home / ".pi/agent",
                       "opencode": Path(isolated["XDG_CONFIG_HOME"]) / "opencode"}[flavor]
            profile.mkdir(parents=True, exist_ok=True)
            profile_fixtures[runtime] = (workspace, isolated)
            targets[runtime] = (harness, "profile", profile)
    if args.openclaw:
        for scope in ("project", "profile"):
            runtime = f"openclaw-{scope}"
            fixture = root / runtime
            fixture.mkdir()
            workspace, state = fixture / "workspace", fixture / "state"
            workspace.mkdir()
            state.mkdir()
            openclaw_fixtures[runtime] = (workspace, state, fixture_env(fixture))
            targets[runtime] = ("openclaw", scope, workspace if scope == "project" else state)
    store = stage_releases(root)

    for runtime, (harness, scope, native_root) in targets.items():
        flavor = runtime.removesuffix("-profile")
        workspace, native_env = profile_fixtures.get(runtime, (projects.get(flavor, project), env))
        claude_source = "user" if scope == "profile" else "project"
        control = None
        named = ["--harness", harness, "--scope", scope, "--root", str(native_root)]
        parser = cli.parser()
        plan = cli.execute(parser.parse_args(["target", *named]))
        if not isinstance(plan, dict) or not isinstance(path := plan.get("target"), str):
            raise ValueError("Missing target plan")
        target = Path(path)
        phases = summary["runtimes"][runtime] = {}
        active = target / SKILL
        cases: list[tuple[str, str | None]] = list(zip(PHASES, (*VERSIONS, VERSIONS[0])))
        if args.full_lifecycle:
            cases.extend(zip(EXTRA_PHASES, (None, VERSIONS[1])))
        for phase, version in cases:
            if version is None:
                command = ["uninstall", SKILL]
            elif phase in ("rollback-v1", "restore-v2"):
                command = ["rollback", SKILL, version]
            else:
                command = ["--store", str(store.root), "install", SKILL, "--version", version]
            receipt = cli.execute(parser.parse_args([*command, *named]))
            if not isinstance(receipt, dict):
                raise ValueError("Missing lifecycle receipt")
            if version is None and active.exists():
                raise ValueError(f"Removed skill still exists for {runtime}")
            if version is not None and (receipt["version"] != version or (active / ASSET).read_text() != version):
                raise ValueError(f"Fixture version or nested asset mismatch for {runtime}/{phase}")
            expected: dict[str, str | bool] | None = None
            if version is not None:
                expected = {"name": SKILL, "description": description(version)}
                if flavor == "claude":
                    expected["source"] = claude_source  # The initialize catalog does not expose paths.
                elif runtime in openclaw_fixtures:
                    expected["source"] = "openclaw-workspace" if scope == "project" else "openclaw-managed"
                    expected.update(eligible=True, bundled=False)
                else:
                    expected["path"] = str(active / "SKILL.md")
                if flavor == "opencode":
                    expected["id"] = SKILL
                if runtime == "codex-profile":
                    expected["scope"] = "user"
            if phase == PHASES[0]:
                if flavor == "opencode" or runtime in openclaw_fixtures:
                    control = opencode_control(target / OPENCODE_CONTROL / "SKILL.md", OPENCODE_CONTROL)
                if control is not None or runtime in profile_fixtures:
                    meta = cli.installation_target(parser.parse_args(["status", *named])).meta
                    opencode_control(meta / OPENCODE_HIDDEN / "SKILL.md", OPENCODE_HIDDEN)
            evidence = root / f"{runtime}-{phase}"
            row = phases[phase] = {"version": version, "expected": expected}
            try:
                scanned = None
                if flavor == "codex":
                    observed = codex_probe(args.codex, workspace, native_env, evidence)
                elif runtime == "hermes":
                    observed = hermes_probe(args.hermes_python, args.hermes_source,
                                            target, env, evidence)
                    scanned = observed["scan_paths"]
                    if not isinstance(scanned, list) or not all(isinstance(p, str) for p in scanned):
                        raise ValueError("Unexpected Hermes scanner response")
                elif flavor == "pi":
                    observed = pi_probe(args.pi, workspace, native_env, evidence)
                elif flavor == "claude":
                    observed = claude_probe(args.claude, workspace, native_env, evidence, claude_source)
                elif runtime in openclaw_fixtures:
                    workspace, state, native_env = openclaw_fixtures[runtime]
                    observed = openclaw_probe(args.openclaw, workspace, state, native_env, evidence, scope)
                elif flavor == "opencode" and control is not None:
                    observed = opencode_probe(args.opencode, workspace, native_env, evidence, control)
                else:
                    raise ValueError(f"No discovery probe for {runtime}")
                row["observed"] = observed
                if flavor == "codex" and observed["errors"]:
                    raise ValueError("Codex skills/list reported discovery errors")
                skills = observed["skills"]
                fields: dict[str, type] = {"name": str, "description": str}
                if runtime != "hermes" and expected is not None:
                    fields.update({k: type(v) for k, v in expected.items()})
                if not isinstance(skills, list) or not all(
                    isinstance(skill, dict) and all(type(skill.get(k)) is kind for k, kind in fields.items())
                    for skill in skills
                ):
                    raise ValueError(f"Unexpected {runtime} skill catalog response")
                passed = matches(expected, skills, target, scanned)
                row["status"] = "pass" if passed else "incompatible"
            except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
                row.update(status="probe_error", error=str(exc))
    summary["passed"] = all(row["status"] == "pass" for phases in summary["runtimes"].values()
                            for row in phases.values())


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, json.dumps({"passed": False, "status": "usage_error", "error": message}) + "\n")


def main():
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path, help="new directory for private evidence")
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--pi", type=Path)
    parser.add_argument("--claude", type=Path)
    parser.add_argument("--opencode", type=Path)
    parser.add_argument("--openclaw", type=Path, help="installed OpenClaw executable; checks both scopes")
    parser.add_argument("--hermes-python", type=Path)
    parser.add_argument("--hermes-source", type=Path)
    parser.add_argument("--full-lifecycle", action="store_true", help="also verify removal and offline restore")
    parser.add_argument("--profile-scopes", action="store_true",
                        help="also verify isolated profile scopes for selected Codex, Claude Code, Pi, or OpenCode")
    parser.add_argument("--expect-version", metavar="RUNTIME=VALUE", action="append", default=[],
                        help="pin each selected executable to exact --version output (Hermes: clean source commit SHA)")
    args = parser.parse_args()
    if bool(args.hermes_python) != bool(args.hermes_source):
        parser.error("--hermes-python and --hermes-source must be supplied together")
    selected = {name for name in ("codex", "pi", "claude", "opencode", "openclaw") if getattr(args, name)}
    if args.hermes_python:
        selected.add("hermes")
    args.expected_versions = {}
    for pin in args.expect_version:
        name, separator, value = pin.partition("=")
        if not separator or not value or name in args.expected_versions:
            parser.error("expected unique RUNTIME=VALUE pins")
        args.expected_versions[name] = value
    if args.expected_versions and args.expected_versions.keys() != selected:
        parser.error("pin every selected runtime exactly once")
    binaries = (args.codex, args.hermes_python, args.pi, args.claude, args.opencode, args.openclaw)
    if not any(binaries):
        parser.error("select at least one runtime")
    for binary in binaries:
        if binary and not binary.is_absolute():
            parser.error("runtime binary paths must be absolute")
    source = Path(__file__).resolve().parents[1] / "src/skillstore/cli.py"
    imported = Path(cli.__file__).resolve()
    if source.is_file() and source != imported:
        parser.error('imported Skillstore is not this checkout; set PYTHONPATH="$PWD/src"')
    summary = {"passed": False, "runtimes": {}, "skillstore_cli": str(imported)}
    try:
        # Resolve only the parent so an existing symlink at the state path is refused.
        root = args.state.parent.resolve() / args.state.name
        root.mkdir(mode=0o700)
    except OSError as exc:
        print(json.dumps(dict(summary, status="setup_error", error=str(exc))))
        return 1

    try:
        qualify(args, root, summary)
    except Exception as exc:
        summary.update(status="setup_error", error=str(exc))
    payload = json.dumps(summary, indent=2) + "\n"
    (root / "summary.json").write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
