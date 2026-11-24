import os
from pathlib import Path
import platform
import subprocess
import sys

import pytest


GATE = Path(__file__).resolve().parents[1] / "scripts/release_gate.py"
RELEASE_WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
SOURCE_QUALIFICATION_WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/source-qualification.yml"


def test_release_workflow_smokes_the_canonical_console_script():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert 'release-smoke-wheel/bin/soulsteads-skillstore' in workflow
    assert 'release-smoke-sdist/bin/soulsteads-skillstore' in workflow
    assert 'dist/*.whl' in workflow
    assert 'dist/*.tar.gz' in workflow
    assert 'release-smoke/bin/skillstore' not in workflow


def test_source_qualification_runs_service_and_package_metadata_tests():
    workflow = SOURCE_QUALIFICATION_WORKFLOW.read_text(encoding="utf-8")

    assert "tests/test_service.py" in workflow
    assert "tests/test_package_metadata.py" in workflow


def test_release_gate_allows_the_source_suite_the_ci_job_budget():
    gate = GATE.read_text(encoding="utf-8")

    assert "timeout=600" in gate


def test_release_gate_prefers_canonical_console_script_with_compatibility_fallback():
    gate = GATE.read_text(encoding="utf-8")

    assert '("soulsteads-skillstore", "skillstore")' in gate


def run_gate(repo, scratch, system, env=None):
    return subprocess.run(
        [sys.executable, "-I", "-B", GATE, "--revision", "HEAD", "--scratch", scratch,
         "--expect-os", system],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.skipif(platform.system() not in {"Linux", "Darwin"}, reason="POSIX release gate")
def test_wrong_os_refuses_without_writing_scratch(tmp_path):
    other_os = "Darwin" if platform.system() == "Linux" else "Linux"
    result = run_gate(tmp_path, tmp_path, other_os)
    assert result.returncode != 0
    assert f"expected {other_os}, running on {platform.system()}" in result.stderr
    assert not result.stdout
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(platform.system() not in {"Linux", "Darwin"}, reason="POSIX release gate")
def test_gate_builds_from_committed_bytes_and_does_not_certify_failed_build(tmp_path):
    root = tmp_path.resolve()
    repo = root / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    git_env = {**env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, env=git_env, text=True).strip()

    git("init", "--template=")

    def commit(label):
        git("add", "pyproject.toml")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgSign=false", "commit", "-m", label)
        return git("rev-parse", "HEAD")

    (repo / "pyproject.toml").write_text("# replacement\n")
    replacement = commit("Replacement")
    (repo / "pyproject.toml").write_text("# committed\n")
    original = commit("Fixture")
    git("replace", original, replacement)
    (repo / "pyproject.toml").write_text("# not committed\n")
    (repo / "untracked").touch()

    scratch = root / "scratch"
    scratch.mkdir()
    bin_dir = root / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text('''#!/bin/sh
set -eu
case " $* " in
    *" build "*) ;;
    *) exit 88 ;;
esac
test "$(command -p pwd -P)" = "$PWD"
test "$(command -p cat pyproject.toml)" = '# committed'
test ! -e untracked
test -z "${PYTHONPATH+x}"
printf '%s\\n' 'intentional build failure' >&2
exit 42
''')
    uv.chmod(0o700)
    env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", PYTHONPATH=str(repo / "wrong"))

    result = run_gate(repo, scratch, platform.system(), env)
    assert result.returncode != 0
    assert "intentional build failure" in result.stderr
    assert not result.stdout
    assert (repo / "pyproject.toml").read_text() == "# not committed\n"
    assert (repo / "untracked").is_file()
