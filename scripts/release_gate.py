"""Qualify a reviewed Git revision's installed wheel and sdist on Linux or macOS."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile


SOURCE_TEST = """
from pathlib import Path
import pytest
import skillstore

expected = Path.cwd() / "src/skillstore/__init__.py"
if Path(skillstore.__file__).resolve() != expected:
    raise SystemExit("Source tests did not import the archived Skillstore")
raise SystemExit(pytest.main(["-p", "no:cacheprovider", "--import-mode=importlib", "tests"]))
"""

INSTALLED_CHECK = """
from pathlib import Path
import sys
import skillstore

prefix = Path(sys.argv[1])
if Path(sys.prefix).resolve() != prefix or not Path(skillstore.__file__).resolve().is_relative_to(prefix):
    raise SystemExit("Skillstore did not import from the fresh artifact environment")
"""


def qualify(revision, scratch, system):
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("GIT_", "PYTHON", "PYTEST_")) and k != "VIRTUAL_ENV"
    }

    def run(*args, cwd=None, capture=False, extra_env=None):
        return subprocess.run(
            args, cwd=cwd, env={**env, **(extra_env or {})}, check=True, timeout=300,
            text=True, stdout=subprocess.PIPE if capture else sys.stderr,
        ).stdout

    git = ("git", "--no-replace-objects")
    repo = Path(run(*git, "rev-parse", "--show-toplevel", capture=True).strip()).resolve()
    if scratch.is_relative_to(repo):
        raise ValueError("--scratch must be outside the checkout")
    commit = run(*git, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}",
                 cwd=repo, capture=True).strip()
    root = Path(tempfile.mkdtemp(prefix="skillstore-gate-", dir=scratch))
    print(f"Validating {commit} on {system}; scratch: {root}", file=sys.stderr, flush=True)
    env.update(TMPDIR=str(root), PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    source = root / "source"
    source.mkdir()
    archive = root / "source.tar"
    run(*git, "archive", "--format=tar", "--output", archive, commit, cwd=repo)
    run("tar", "-xf", archive, "-C", source, cwd=root)
    archive.unlink()

    uv = ("uv", "--no-config", "--no-python-downloads")
    dist = root / "dist"
    run(*uv, "build", "--python", sys.executable, "--out-dir", dist, cwd=source)
    wheel = sorted(dist.glob("*.whl"))
    sdist = sorted(dist.glob("*.tar.gz"))
    if len(wheel) != 1 or len(sdist) != 1:
        raise RuntimeError("Expected exactly one wheel and one source distribution")

    def install(prefix, *requirements):
        python = prefix / "bin/python"
        run(*uv, "venv", "--python", sys.executable, prefix, cwd=root)
        run(*uv, "pip", "install", "--no-sources", "--python", python, *requirements, cwd=root)
        return python

    client = install(root / "checks", "pytest>=8", "PyYAML>=6", "mcp")
    run(client, "-B", "-c", SOURCE_TEST, cwd=source, extra_env={"PYTHONPATH": str(source / "src")})
    results = {}
    for kind, artifact in (("wheel", wheel[0]), ("sdist", sdist[0])):
        prefix = root / kind
        python = install(prefix, artifact)
        run(python, "-I", "-B", "-c", INSTALLED_CHECK, prefix, cwd=root)
        result = json.loads(run(
            client, "-I", "-B", source / "scripts/qualify_release.py",
            "--skillstore", prefix / "bin/skillstore", "--scratch", root, cwd=root, capture=True,
        ))
        if result.get("passed") is not True:
            raise RuntimeError(f"Installed {kind} smoke test did not pass")
        with artifact.open("rb") as data:
            digest = hashlib.file_digest(data, "sha256").hexdigest()
        results[kind] = {"filename": artifact.name, "sha256": digest, "checks": result}

    return {
        "passed": True, "commit": commit, "os": system, "os_release": platform.release(),
        "arch": platform.machine(), "python": platform.python_version(), "source_tests": "passed",
        "artifacts": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="reviewed commit or ref in the current Git checkout")
    parser.add_argument("--scratch", type=Path, required=True, help="existing private directory outside the checkout")
    parser.add_argument("--expect-os", required=True, choices=("Linux", "Darwin"))
    args = parser.parse_args()
    system = platform.system()
    if system != args.expect_os:
        parser.error(f"expected {args.expect_os}, running on {system}")
    try:
        summary = qualify(args.revision, args.scratch.resolve(strict=True), system)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"release gate failed: {exc}\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
