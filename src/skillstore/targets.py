"""Read-only plans for explicitly rooted native skill directories."""

from pathlib import Path
from stat import S_ISDIR

from .errors import UsageError


_PATHS = {
    ("codex", "project"): ".agents/skills",
    ("codex", "profile"): ".agents/skills",
    ("claude-code", "project"): ".claude/skills",
    ("claude-code", "profile"): ".claude/skills",
    ("hermes", "profile"): "skills",
    ("openclaw", "project"): "skills",
    ("openclaw", "profile"): "skills",
    ("opencode", "project"): ".opencode/skills",
    ("opencode", "profile"): "skills",
    ("pi", "project"): ".agents/skills",
    ("pi", "profile"): "skills",
    ("soulsteads", "profile"): "skills",
}

_MANAGED = {
    ("codex", "project"),
    ("codex", "profile"),
    ("claude-code", "project"),
    ("claude-code", "profile"),
    ("hermes", "profile"),
    ("openclaw", "project"),
    ("openclaw", "profile"),
    ("opencode", "project"),
    ("opencode", "profile"),
    ("pi", "project"),
    ("pi", "profile"),
}

def plan_target(harness: str, scope: str, root: str) -> dict:
    preset = (harness, scope)
    if preset not in _PATHS:
        raise UsageError("unsupported harness and scope", harness=harness, scope=scope)

    raw = Path(root)
    if not root or ".." in raw.parts:
        raise UsageError("--root must name an existing directory without '..'")
    # Keep this lexical so a symlink is never accepted as a path alias.
    base = Path("/" + str(raw.absolute()).lstrip("/"))
    if base == Path("/"):
        raise UsageError("--root cannot be the filesystem root")

    target = base / _PATHS[preset]
    for directory in (*reversed(target.parents), target):
        try:
            mode = directory.lstat().st_mode
        except FileNotFoundError:
            if directory == base or directory in base.parents:
                raise UsageError("--root must be an existing directory", path=str(directory)) from None
            break
        if not S_ISDIR(mode):
            raise UsageError("named target paths cannot contain symlinks or non-directories", path=str(directory))

    return {
        "harness": harness,
        "scope": scope,
        "root": str(base),
        "target": str(target),
        "managed_install": preset in _MANAGED,
    }
