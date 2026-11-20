"""Agent Skills package handling: validation, digests, archives and diffs.

A package is an ordinary Agent Skills directory: ``SKILL.md`` with YAML
frontmatter (``name``, ``description``) plus optional supporting files.
Nothing in this module adds Skillstore-specific content to a package.
"""

from __future__ import annotations

import difflib
import gzip
import hashlib
import io
import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath

import yaml

from .errors import IntegrityError, UsageError, ValidationFailed

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_FILES = 500
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
RESERVED_TOP_LEVEL = {".git", ".hg", ".svn", ".skillstore"}
KNOWN_FRONTMATTER = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
SECRET_PATTERNS = [
    ("private key block", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
]
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$")


# ---------------------------------------------------------------- versions


def parse_version(version: str) -> tuple:
    """Return a sortable key for a ``MAJOR.MINOR.PATCH[-prerelease]`` version."""
    m = SEMVER_RE.match(version or "")
    if not m:
        raise UsageError(f"invalid version {version!r}: expected MAJOR.MINOR.PATCH[-prerelease]")
    major, minor, patch, pre = m.groups()
    if pre is None:
        pre_key: tuple = (1,)
    else:
        parts = []
        for part in pre.split("."):
            if not part or (part.isdigit() and len(part) > 1 and part.startswith("0")):
                raise UsageError("prerelease identifiers must be nonempty and numeric identifiers cannot have leading zeros")
            parts.append((0, int(part), "") if part.isdigit() else (1, 0, part))
        pre_key = (0, *parts)
    return (int(major), int(minor), int(patch), pre_key)


def is_prerelease(version: str) -> bool:
    return parse_version(version)[3] != (1,)


# ---------------------------------------------------------------- file trees


def read_tree(root: str | os.PathLike) -> dict[str, tuple[bytes, bool]]:
    """Read a package directory into ``{posix_path: (content, executable)}``.

    Symlinks and special files are reported as validation errors rather than
    followed, so a package can never reference content outside itself.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValidationFailed(f"{root} is not a directory", report=_report([f"{root} is not a directory"], []))
    files: dict[str, tuple[bytes, bool]] = {}
    problems: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        rel_dir = Path(dirpath).relative_to(root)
        for d in list(dirnames):
            if (Path(dirpath) / d).is_symlink():
                problems.append(f"symlinked directory not allowed: {(rel_dir / d).as_posix()}")
                dirnames.remove(d)
            elif d == "__pycache__":
                dirnames.remove(d)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = (rel_dir / fn).as_posix()
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                problems.append(f"symlink not allowed: {rel}")
                continue
            if not stat.S_ISREG(st.st_mode):
                problems.append(f"special file not allowed: {rel}")
                continue
            if fn.endswith(".pyc"):
                continue
            if st.st_size > MAX_FILE_BYTES:
                problems.append(f"file too large (> {MAX_FILE_BYTES} bytes): {rel}")
                continue
            files[rel] = (p.read_bytes(), bool(st.st_mode & 0o111))
            if len(files) > MAX_FILES or sum(len(content) for content, _ in files.values()) > MAX_TOTAL_BYTES:
                raise ValidationFailed("package exceeds file count or total size limits")
    if problems:
        raise ValidationFailed("package contains unsupported entries", report=_report(problems, []))
    return files


def write_tree(files: dict[str, tuple[bytes, bool]], dest: str | os.PathLike) -> None:
    """Write a tree into ``dest`` (which must not exist or be empty)."""
    dest = Path(dest)
    mkdir_durable(dest)
    for rel, (content, executable) in sorted(files.items()):
        _check_relpath(rel)
        target = dest / rel
        mkdir_durable(target.parent)
        with target.open("wb") as file:
            file.write(content)
            file.flush()
            os.fchmod(file.fileno(), 0o755 if executable else 0o644)
            os.fsync(file.fileno())
    # Persist payload and its directory entries before a journal can announce
    # this tree as a recoverable stage.
    for directory in sorted((p for p in dest.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        sync_directory(directory)
    sync_directory(dest)
    sync_directory(dest.parent)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def mkdir_durable(path: Path) -> None:
    """Persist newly created ancestors and their names in existing parents."""
    missing = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in missing:
        sync_directory(directory)
        sync_directory(directory.parent)


def manifest(files: dict[str, tuple[bytes, bool]]) -> dict[str, dict]:
    return {
        rel: {"sha256": hashlib.sha256(content).hexdigest(), "mode": "755" if exe else "644", "size": len(content)}
        for rel, (content, exe) in sorted(files.items())
    }


def tree_digest(files: dict[str, tuple[bytes, bool]]) -> str:
    """Deterministic content digest of a package tree.

    ``sha256`` over one line per file, sorted by path: ``<mode> <sha256> <path>\\n``.
    Reproducible with standard tools; see docs/FORMAT.md.
    """
    h = hashlib.sha256()
    for rel, entry in manifest(files).items():
        h.update(f"{entry['mode']} {entry['sha256']} {rel}\n".encode())
    return "sha256:" + h.hexdigest()


def digest_dir(root: str | os.PathLike) -> str:
    return tree_digest(read_tree(root))


def _check_relpath(rel: str) -> None:
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts or "\\" in rel or any(ord(c) < 32 for c in rel) or p.as_posix() != rel:
        raise ValidationFailed(f"unsafe path in package: {rel!r}", report=_report([f"unsafe path: {rel!r}"], []))


# ---------------------------------------------------------------- archives


def pack(files: dict[str, tuple[bytes, bool]]) -> bytes:
    """Deterministic ``tar.gz`` of a tree (sorted, zeroed metadata)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, (content, exe) in sorted(files.items()):
            info = tarfile.TarInfo(rel)
            info.size = len(content)
            info.mode = 0o755 if exe else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(content))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def unpack(data: bytes) -> dict[str, tuple[bytes, bool]]:
    """Safely read a ``tar.gz`` into a tree. Only regular files are accepted."""
    files: dict[str, tuple[bytes, bool]] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar:
                if member.isdir():
                    continue
                if not member.isreg():
                    raise ValidationFailed(
                        f"archive entry is not a regular file: {member.name}",
                        report=_report([f"non-regular archive entry: {member.name}"], []),
                    )
                rel = member.name.removeprefix("./")
                _check_relpath(rel)
                if rel in files:
                    raise ValidationFailed(f"duplicate archive entry: {rel}")
                total += member.size
                if member.size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                    raise ValidationFailed("archive exceeds package size limits", report=_report(["archive too large"], []))
                fh = tar.extractfile(member)
                files[rel] = (fh.read() if fh else b"", bool(member.mode & 0o111))
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValidationFailed(f"unreadable package archive: {exc}", report=_report([f"unreadable archive: {exc}"], []))
    return files


def verify_digest(files: dict[str, tuple[bytes, bool]], expected: str, what: str) -> None:
    actual = tree_digest(files)
    if actual != expected:
        raise IntegrityError(
            f"digest mismatch for {what}: recorded {expected}, actual {actual}; content was altered or corrupted",
            expected=expected,
            actual=actual,
        )


# ---------------------------------------------------------------- validation


def parse_frontmatter(skill_md: bytes) -> tuple[dict, str]:
    """Return ``(frontmatter, body)``; raises ``ValueError`` with a reason."""
    try:
        text = skill_md.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("SKILL.md is not valid UTF-8")
    text = text.lstrip("﻿")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter delimited by '---'")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError("SKILL.md frontmatter is not closed with '---'")
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise ValueError(f"SKILL.md frontmatter is not valid YAML: {exc}")
    if not isinstance(data, dict):
        raise ValueError("SKILL.md frontmatter must be a YAML mapping")
    return data, "\n".join(lines[end + 1 :])


def _report(errors: list[str], warnings: list[str], **extra) -> dict:
    return {"ok": not errors, "errors": errors, "warnings": warnings, **extra}


def validate(files: dict[str, tuple[bytes, bool]], expected_name: str | None = None) -> dict:
    """Validate a tree as an Agent Skills package. Returns a report dict.

    The report is recorded with every submission; ``ok`` is False when any
    error is present. Warnings never block publication.
    """
    errors: list[str] = []
    warnings: list[str] = []
    name = description = None

    if len(files) > MAX_FILES:
        errors.append(f"too many files ({len(files)} > {MAX_FILES})")
    total = sum(len(c) for c, _ in files.values())
    if total > MAX_TOTAL_BYTES:
        errors.append(f"package too large ({total} > {MAX_TOTAL_BYTES} bytes)")
    for rel, (content, exe) in files.items():
        try:
            _check_relpath(rel)
        except ValidationFailed:
            errors.append(f"unsafe path: {rel!r}")
            continue
        if PurePosixPath(rel).parts[0] in RESERVED_TOP_LEVEL:
            errors.append(f"reserved path not allowed in a package: {rel}")
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(content):
                errors.append(f"possible credential ({label}) in {rel}")
        if exe and not content.startswith(b"#!"):
            warnings.append(f"executable file without a shebang line: {rel}")

    if "SKILL.md" not in files:
        errors.append("missing SKILL.md at the package root")
        return _report(errors, warnings, name=None, description=None)

    try:
        fm, body = parse_frontmatter(files["SKILL.md"][0])
    except ValueError as exc:
        errors.append(str(exc))
        return _report(errors, warnings, name=None, description=None)

    name = fm.get("name")
    if not isinstance(name, str) or not name:
        errors.append("frontmatter 'name' is required and must be a string")
        name = None
    else:
        if len(name) > MAX_NAME:
            errors.append(f"frontmatter 'name' is longer than {MAX_NAME} characters")
        if not NAME_RE.match(name):
            errors.append("frontmatter 'name' must be lowercase letters, digits and single hyphens (e.g. 'my-skill')")
        if expected_name and name != expected_name:
            errors.append(f"frontmatter 'name' is {name!r} but the skill is {expected_name!r}")
    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append("frontmatter 'description' is required and must be a non-empty string")
        description = None
    elif len(description) > MAX_DESCRIPTION:
        errors.append(f"frontmatter 'description' is longer than {MAX_DESCRIPTION} characters")
    for key in ("license", "compatibility"):
        if key in fm and not isinstance(fm[key], str):
            errors.append(f"frontmatter '{key}' must be a string")
    if "metadata" in fm and not isinstance(fm["metadata"], dict):
        errors.append("frontmatter 'metadata' must be a mapping")
    if "allowed-tools" in fm and not isinstance(fm["allowed-tools"], (str, list)):
        errors.append("frontmatter 'allowed-tools' must be a string or list")
    for key in fm:
        if key not in KNOWN_FRONTMATTER:
            warnings.append(f"non-standard frontmatter field: {key}")
    if not body.strip():
        warnings.append("SKILL.md has no instructions after the frontmatter")
    if len(body.splitlines()) > 500:
        warnings.append("SKILL.md body is longer than 500 lines; consider moving detail into reference files")
    for target in re.findall(r"\]\(([^)#\s]+)\)", body):
        if "://" in target or target.startswith(("/", "mailto:")):
            continue
        if PurePosixPath(target).as_posix().lstrip("./") not in files:
            warnings.append(f"SKILL.md links to a file that is not in the package: {target}")
    return _report(errors, warnings, name=name, description=description, files=len(files), bytes=total)


def require_valid(files: dict[str, tuple[bytes, bool]], expected_name: str | None = None) -> dict:
    report = validate(files, expected_name)
    if not report["ok"]:
        raise ValidationFailed("package failed validation: " + "; ".join(report["errors"]), report=report)
    return report


# ---------------------------------------------------------------- diffs


def _is_text(content: bytes) -> bool:
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def diff_trees(
    old: dict[str, tuple[bytes, bool]],
    new: dict[str, tuple[bytes, bool]],
    old_label: str = "old",
    new_label: str = "new",
) -> dict:
    """Summarise and render a unified diff between two trees."""
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    modified = sorted(p for p in set(old) & set(new) if old[p] != new[p])
    chunks: list[str] = []
    for rel in sorted(set(added) | set(removed) | set(modified)):
        a, a_exe = old.get(rel, (b"", False))
        b, b_exe = new.get(rel, (b"", False))
        if not (_is_text(a) and _is_text(b)):
            chunks.append(f"Binary file {rel} differs\n")
            continue
        if a == b and a_exe != b_exe:
            chunks.append(f"Mode change {rel}: {'755' if a_exe else '644'} -> {'755' if b_exe else '644'}\n")
            continue
        chunks.extend(
            difflib.unified_diff(
                a.decode().splitlines(keepends=True),
                b.decode().splitlines(keepends=True),
                fromfile=f"{old_label}/{rel}" if rel in old else "/dev/null",
                tofile=f"{new_label}/{rel}" if rel in new else "/dev/null",
            )
        )
    text = "".join(c if c.endswith("\n") else c + "\n" for c in chunks)
    return {"added": added, "removed": removed, "modified": modified, "changed": bool(added or removed or modified), "diff": text}
