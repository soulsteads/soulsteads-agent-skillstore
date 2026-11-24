from pathlib import Path
import tomllib


def test_v020_declares_canonical_console_script_and_compatibility_alias():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert metadata["project"]["version"] == "0.2.0"
    assert metadata["project"]["scripts"] == {
        "soulsteads-skillstore": "skillstore.cli:main",
        "skillstore": "skillstore.cli:main",
    }


def test_sdist_excludes_github_workflows():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert "/.github" in metadata["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
