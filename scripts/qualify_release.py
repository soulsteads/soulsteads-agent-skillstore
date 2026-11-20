"""Exercise an installed Skillstore console script using a disposable registry and the MCP SDK."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SKILL = "skillstore-release-fixture"
VERSIONS = ("1.0.0", "2.0.0")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


async def mcp_check(binary, root, env, store, target):
    params = StdioServerParameters(
        command=str(binary), args=[*store, "mcp", *target], cwd=str(root), env=env,
    )
    async with asyncio.timeout(30), stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            require(initialized.model_dump(by_alias=True)["serverInfo"]["name"] == "skillstore", "Unexpected MCP server")
            tools = {tool.name for tool in (await session.list_tools()).tools}
            require("skillstore_uninstall" in tools and "skillstore_rollback" in tools, "Missing lifecycle tools")

            async def call(name, arguments=None):
                reply = await session.call_tool("skillstore_" + name, arguments)
                if reply.model_dump(by_alias=True).get("isError") or len(reply.content) != 1 or reply.content[0].type != "text":
                    raise RuntimeError(f"MCP {name} did not return a successful JSON result")
                return json.loads(reply.content[0].text)

            await call("uninstall", {"name": SKILL})
            require(not (Path(target[-1]) / SKILL).exists(), "MCP uninstall left the skill active")
            status = await call("status")
            require(not status["installed"] and status["removed"][0]["name"] == SKILL, "MCP removal receipt mismatch")
            receipt = await call("rollback", {"name": SKILL, "version": VERSIONS[1]})
            require(receipt["version"] == VERSIONS[1], "MCP did not restore v2")
            return receipt


def qualify(binary, root):
    home = root / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.defpath, "LANG": "C.UTF-8", "TMPDIR": str(root)}

    def cli(*args):
        result = subprocess.run([binary, *args], cwd=root, env=env, capture_output=True,
                                text=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError(f"Installed CLI failed: {result.stderr.strip()}")
        return json.loads(result.stdout)

    registry = root / "registry"
    store = ("--store", str(registry))
    target = ("--target", str(root / "skills"))
    source = root / "source"
    source.mkdir()
    cli("init", registry, "--reviewer", "fixture-reviewer")
    for version in VERSIONS:
        (source / "SKILL.md").write_text(
            f"---\nname: {SKILL}\ndescription: Harmless release fixture.\n---\nVersion {version}\n",
            encoding="utf-8",
        )
        cli(*store, "--actor", "fixture-author", "submit", source, version,
            "--source", "https://example.invalid/fixture", "--revision", version)
        cli(*store, "--actor", "fixture-reviewer", "review", SKILL, version, "approve", "--note", "Fixture approval")
        cli(*store, "preview", SKILL, "--version", version, *target)
        receipt = cli(*store, "install", SKILL, "--version", version, *target)
        require(receipt["version"] == version, "Wrong installed version")
    restored = asyncio.run(mcp_check(binary, root, env, store, target))
    installed, = cli("status", *target)["installed"]
    require(installed == {**restored, "local": "clean"}, "CLI and MCP disagreed on receipt")

    backup = root / "backup"
    backup.mkdir()
    skills = Path(target[-1])
    meta = skills.with_name(".skillstore-" + skills.name)
    shutil.copytree(skills, backup / skills.name)
    shutil.copytree(meta, backup / meta.name)
    shutil.copytree(registry, backup / registry.name)
    require(cli("--store", backup / registry.name, "verify")["ok"], "Restored registry failed verification")
    prior = cli("rollback", SKILL, VERSIONS[0], "--target", backup / skills.name)
    body = (backup / skills.name / SKILL / "SKILL.md").read_text(encoding="utf-8")
    require(prior["version"] == VERSIONS[0] and body.endswith(f"Version {VERSIONS[0]}\n"), "Offline restore failed")
    return {"passed": True, "cli": ["submit", "review", "preview", "install-v1", "install-v2", "status"],
            "mcp": ["initialize", "list_tools", "uninstall", "status", "rollback"],
            "backup_restore": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skillstore", type=Path, required=True, help="absolute path to an installed console script")
    parser.add_argument("--scratch", type=Path, required=True, help="existing directory for the disposable fixture")
    args = parser.parse_args()
    if not args.skillstore.is_absolute() or not os.access(args.skillstore, os.X_OK):
        parser.error("--skillstore must name an executable by absolute path")
    with tempfile.TemporaryDirectory(prefix="skillstore-release-", dir=args.scratch.resolve(strict=True)) as fixture:
        print(json.dumps(qualify(args.skillstore, Path(fixture)), indent=2))


if __name__ == "__main__":
    main()
