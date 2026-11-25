"""End-to-end Debian package qualification for the repository-local builder."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts" / "build-deb.sh"
DPKG_DEB = os.environ.get("DPKG_DEB") or shutil.which("dpkg-deb")

@pytest.mark.skipif(not shutil.which("uv") or not DPKG_DEB, reason="requires uv and dpkg-deb")
def test_builder_creates_a_self_contained_debian_payload(tmp_path):
    assert BUILD.is_file(), "repository-local Debian builder is missing"
    dpkg_deb = DPKG_DEB
    assert dpkg_deb is not None
    out_dir = tmp_path / "out"
    tmp_dir = tmp_path / "temporary's"
    tmp_dir.mkdir()
    environment = {**os.environ, "TMPDIR": str(tmp_dir)}
    environment.pop("DPKG_DEB", None)
    result = subprocess.run(
        ["bash", str(BUILD), "--out-dir", str(out_dir)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    packages = list(out_dir.glob("soulsteads-agent-skillstore_*_all.deb"))
    assert len(packages) == 1
    control = subprocess.check_output([dpkg_deb, "-f", str(packages[0])], text=True)
    assert "Package: soulsteads-agent-skillstore" in control
    version = subprocess.check_output([dpkg_deb, "-f", str(packages[0]), "Version"], text=True).strip()
    filename_version = version.replace(":", "%3a")
    assert packages[0].name == f"soulsteads-agent-skillstore_{filename_version}_all.deb"
    assert "Maintainer: Soulsteads" in control.splitlines()
    assert "Depends: python3 (>= 3.11), python3-yaml (>= 6.0)" in control
    listing = subprocess.check_output([dpkg_deb, "-c", str(packages[0])], text=True)
    assert "./usr/bin/soulsteads-skillstore" in listing
    assert "./usr/bin/skillstore" in listing
    assert "./usr/lib/python3/dist-packages/skillstore/bundled_skills/manage-skills/SKILL.md" in listing
    assert "./usr/lib/python3/dist-packages/skillstore/hermes_plugins/skillstore-backup/plugin.yaml" in listing
    assert "./usr/share/doc/soulsteads-agent-skillstore/NOTICE" in listing
    control_dir = tmp_path / "control"
    subprocess.check_call([dpkg_deb, "-e", str(packages[0]), str(control_dir)])
    assert sorted(path.name for path in control_dir.iterdir()) == ["control"]


def test_build_only_workflow_is_pr_and_manual_with_pinned_actions():
    workflow = (ROOT / ".github/workflows/deb.yml").read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "release:" not in workflow
    assert "scripts/build-deb.sh --out-dir" in workflow
    assert "uv pip install --python /usr/bin/python3 --target" in workflow
    assert "SMOKE_PYTHONPATH=" in workflow
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow
