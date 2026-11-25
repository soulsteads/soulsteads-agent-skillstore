"""Explicit installation of ordinary skill trees with receipts and recovery.

The target is a skills directory, not a harness home. Nothing executes a skill
or edits harness configuration. Cooperative sessions prevent replacement while
a caller is using this target; unregistered sessions must be stopped by users.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import secrets
import stat
from pathlib import Path

from . import package as pkg
from .errors import Conflict, NotFound, NotReviewed, UsageError
from .store import now, read_json, write_json


class Installation:
    def __init__(self, target, *, require_external=False):
        raw = Path("/" + str(Path(target).expanduser().absolute()).lstrip("/"))
        if raw == Path("/") or ".." in raw.parts or any(
            p == ".skillstore" or p.startswith(".skillstore-") for p in raw.parts
        ):
            raise UsageError("invalid target: root, '..', and Skillstore state paths are reserved")
        self.root = raw
        self.legacy = raw / ".skillstore"
        self.external = raw.with_name(f".skillstore-{raw.name}")
        self.require_external = require_external
        self._locate()

    @staticmethod
    def _directory(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            raise Conflict(f"installation paths cannot contain symlinks or non-directories: {path}")
        return info

    def _locate(self):
        for p in (*reversed(self.root.parents), self.root):
            if self._directory(p) is None:
                break
        legacy = self._directory(self.legacy)
        external = self._directory(self.external)
        if legacy and external:
            raise Conflict("both legacy and external Skillstore state exist; inspect before proceeding")
        for info in (legacy, external):
            if info and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise Conflict("Skillstore state directory must be owned by the caller with mode 0700")
        return (self.legacy, legacy) if legacy else (self.external, external)

    @property
    def meta(self):
        return self._locate()[0]

    @staticmethod
    def _identity(info):
        return (info.st_dev, info.st_ino) if info else None

    @contextlib.contextmanager
    def lock(self, *, create=True, migration=False, recover=True):
        meta, info = self._locate()
        if info is None:
            if not create:
                yield None
                return
            pkg.mkdir_durable(self.root)
            try:
                self.external.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                pkg.sync_directory(self.external)
                pkg.sync_directory(self.external.parent)
            meta, info = self._locate()
        if info is None:
            raise Conflict("Skillstore state changed while opening it; retry")
        if meta == self.legacy and self.require_external and not migration:
            raise Conflict("this harness requires external state; run migrate-state first")
        try:
            md = os.open(meta, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise Conflict("Skillstore state moved while opening it; retry") from None
        try:
            if self._identity(os.fstat(md)) != self._identity(info):
                raise Conflict("Skillstore state changed while opening it; retry")
            access = os.O_RDWR | os.O_CREAT if recover else os.O_RDONLY
            try:
                fd = os.open("lock", access | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=md)
            except FileNotFoundError:
                raise Conflict("installation lock is missing; run regular status to check the target") from None
            try:
                lock_info = os.fstat(fd)
                if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                    raise Conflict("invalid Skillstore lock file")
                fcntl.flock(fd, fcntl.LOCK_EX)
                bound_path, bound = self._locate()
                lock_path = os.stat("lock", dir_fd=md, follow_symlinks=False)
                if (bound_path != meta or self._identity(bound) != self._identity(info)
                        or self._identity(lock_path) != self._identity(lock_info)):
                    raise Conflict("Skillstore state moved while waiting for its lock; retry")
                if not recover:
                    if os.path.lexists(meta / "transaction.json"):
                        raise Conflict("pending installation transaction; run regular status to recover it")
                elif not migration:
                    self._recover()
                yield meta
            finally:
                os.close(fd)
        finally:
            os.close(md)

    def migrate_state(self):
        with self.lock(create=False, migration=True) as meta:
            if meta is None:
                raise NotFound("no Skillstore state to migrate")
            if meta == self.legacy:
                if os.path.lexists(meta / "transaction.json"):
                    raise Conflict("finish the pending transaction with explicit --target before migration")
                state = self._state()
                self._idle(state)
                if self.external.parent.stat().st_dev != meta.stat().st_dev:
                    raise Conflict("legacy state and its destination must be on the same filesystem")
                # The namespace changes in one rename, carrying the very same lock
                # inode. A waiter that opened the old path rechecks it after locking.
                if self._locate()[0] != meta:
                    raise Conflict("Skillstore state changed before migration; retry")
                os.rename(meta, self.external)
            # Also finish durability on a retry after a post-rename error.
            pkg.sync_directory(self.external)
            pkg.sync_directory(self.external.parent)
            pkg.sync_directory(self.root)
            return {"target": str(self.root), "state": str(self.external)}

    def _state(self):
        p = self.meta / "state.json"
        state = self._metadata(p) if os.path.lexists(p) else {"installed": {}, "sessions": {}}
        if (not all(isinstance(state.get(k), dict) for k in ("installed", "sessions"))
                or not isinstance(state.get("removed", {}), dict)
                or state["installed"].keys() & state.get("removed", {}).keys()):
            raise Conflict("invalid installed, removed, or session state")
        return state

    @staticmethod
    def _metadata(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Conflict(f"invalid Skillstore metadata file: {path.name}")
        value = read_json(path)
        if not isinstance(value, dict):
            raise Conflict(f"invalid Skillstore metadata object: {path.name}")
        return value

    @staticmethod
    def _idle(state):
        if state["sessions"]:
            raise Conflict("active registered sessions use this target; end them before changing installed skills")

    def _same_filesystem(self):
        if self.root.stat().st_dev != self.meta.stat().st_dev:
            raise Conflict("skills and Skillstore state must be on the same filesystem")

    def _name(self, name):
        if not isinstance(name, str) or not pkg.NAME_RE.fullmatch(name):
            raise UsageError("invalid skill name")
        return name

    def _clean(self, name, receipt):
        dest = self.root / self._name(name)
        if dest.is_symlink():
            raise Conflict("installed skill was replaced by a symlink")
        if receipt is None:
            if dest.exists():
                raise Conflict(f"{name} already exists and is not owned by Skillstore")
        elif not dest.is_dir() or pkg.digest_dir(dest) != receipt["digest"]:
            raise Conflict(f"{name} has local changes; preserve them in a separate copy or proposal before replacement")

    def _recover(self):
        meta = self.meta
        journal = meta / "transaction.json"
        if not os.path.lexists(journal):
            return
        self._same_filesystem()
        tx = self._metadata(journal)
        name = self._name(tx.get("name"))
        backup_name = tx.get("backup")
        if not isinstance(backup_name, str) or not re.fullmatch(r"backup-[0-9a-f]{24}", backup_name):
            raise Conflict("invalid Skillstore transaction backup")
        stage_name = tx.get("stage")
        removing = "removed" in tx
        if (set(tx) != {"name", "stage", "backup", "previous", "removed" if removing else "receipt"}
                or stage_name != (None if removing else "stage-" + backup_name[7:])):
            raise Conflict("invalid Skillstore transaction shape")
        previous = tx["previous"]
        receipt = tx["removed" if removing else "receipt"]
        if (not isinstance(receipt, dict) or receipt.get("name") != name
                or (previous is not None and (not isinstance(previous, dict) or previous.get("name") != name))
                or (removing and previous is None)):
            raise Conflict("invalid Skillstore transaction receipt")
        state = self._state()
        self._idle(state)
        current = state["installed"].get(name)
        committed = (state.get("removed", {}).get(name) == receipt and current is None
                     if removing else current == receipt)
        if current != previous and not committed:
            raise Conflict("transaction does not match installed state")
        backup = meta / backup_name
        stage = meta / stage_name if stage_name is not None else None
        dest = self.root / name
        if stage is not None and not os.path.lexists(stage):
            stage = None
        backup_exists = os.path.lexists(backup)
        if stage is not None:
            pkg.verify_digest(pkg.read_tree(stage), receipt["digest"], "staged installation")
        if backup_exists and not previous:
            raise Conflict("unexpected backup during first installation")
        if not committed and (stage is not None or removing) and os.path.lexists(dest):
            self._clean(name, previous)
            if backup_exists:
                raise Conflict("both original and backup exist during recovery; inspect transaction.json")
            os.rename(dest, backup)
            pkg.sync_directory(meta)
            pkg.sync_directory(self.root)
        if previous:
            pkg.verify_digest(pkg.read_tree(backup), previous["digest"], "previous installation backup")
        if stage is not None:
            if committed:
                raise Conflict("staged tree exists after receipt commit")
            self._clean(name, None)
            os.replace(stage, dest)
        self._clean(name, None if removing else receipt)
        pkg.sync_directory(meta)
        pkg.sync_directory(self.root)
        if removing:
            state["installed"].pop(name, None)
            state.setdefault("removed", {})[name] = receipt
        else:
            state["installed"][name] = receipt
            state.get("removed", {}).pop(name, None)
        write_json(meta / "state.json", state)
        journal.unlink()
        pkg.sync_directory(meta)

    def status(self, *, recover=True):
        with self.lock(create=False, recover=recover) as meta:
            state = self._state() if meta else {"installed": {}, "sessions": {}}
            rows = []
            for name, receipt in state["installed"].items():
                try:
                    self._clean(name, receipt)
                    local = "clean"
                except Exception as exc:
                    local = str(exc)
                rows.append({**receipt, "local": local})
            result = {"target": str(self.root), "installed": rows, "sessions": state["sessions"]}
            if state.get("removed"):
                result["removed"] = list(state["removed"].values())
            return result

    def preview(self, store, name, version=None, *, recover=True):
        release = store.release(name, version)
        files = pkg.unpack(store.package(name, release["version"]))
        pkg.verify_digest(files, release["digest"], "download")
        pkg.require_valid(files, name)
        with self.lock(create=False, recover=recover) as meta:
            old = self._state()["installed"].get(name) if meta else None
            self._clean(name, old)
            before = pkg.read_tree(self.root / name) if old else {}
            return {"release": release, "current": old, **pkg.diff_trees(before, files)}

    def install(self, store, name, version=None):
        release = store.release(name, version)
        if release["status"] != "reviewed" or not release.get("review") or release["review"].get("digest_reviewed") != release["digest"]:
            raise NotReviewed("installation requires a reviewed version with its matching reviewed digest")
        files = pkg.unpack(store.package(name, release["version"]))
        pkg.verify_digest(files, release["digest"], "download")
        pkg.require_valid(files, name)
        # Re-check status after download, before publication into the local target.
        fresh = store.release(name, release["version"])
        if fresh["status"] != "reviewed" or fresh["digest"] != release["digest"]:
            raise NotReviewed("release changed or was withdrawn during download")
        receipt = {"name": name, "version": release["version"], "digest": release["digest"],
                   "store": store.location, "provenance": release["provenance"], "review": release["review"]}
        with self.lock():
            return self._replace(name, files, receipt)

    def _replace(self, name, files, receipt):
        self._name(name)
        state = self._state()
        self._idle(state)
        previous = state["installed"].get(name)
        self._clean(name, previous)
        if previous and previous["digest"] == receipt["digest"] and previous["version"] == receipt["version"]:
            return previous
        self._same_filesystem()
        txid = secrets.token_hex(12)
        stage, backup = "stage-" + txid, "backup-" + txid
        pkg.write_tree(files, self.meta / stage)
        retained = previous or state.get("removed", {}).get(name, {})
        history = list(retained.get("history", []))
        if previous:
            history.append({"receipt": {k:v for k,v in previous.items() if k != "history"}, "tree": backup})
        receipt = {**receipt, "installed_at": now(), "history": history}
        write_json(self.meta / "transaction.json", {"name": name, "stage": stage, "backup": backup,
                                                     "previous": previous, "receipt": receipt})
        self._recover()
        return receipt

    def rollback(self, name, version):
        self._name(name)
        with self.lock(create=False) as meta:
            if meta is None:
                raise NotFound("skill has no local installation history")
            state = self._state()
            current = state["installed"].get(name) or state.get("removed", {}).get(name)
            if not current:
                raise NotFound("skill has no local installation history")
            entry = next((e for e in reversed(current["history"]) if e["receipt"]["version"] == version), None)
            if not entry:
                raise NotFound("version is not in local recovery history")
            pkg._check_relpath(entry["tree"])
            files = pkg.read_tree(self.meta / entry["tree"])
            pkg.verify_digest(files, entry["receipt"]["digest"], "rollback snapshot")
            return self._replace(name, files, entry["receipt"])

    def uninstall(self, name):
        """Return to absence without losing the last installed version."""
        self._name(name)
        with self.lock(create=False) as meta:
            if meta is None:
                self._clean(name, None)
                raise NotFound("skill is not installed")
            state = self._state()
            self._idle(state)
            previous = state["installed"].get(name)
            self._clean(name, previous)
            if previous is None:
                removed = state.get("removed", {}).get(name)
                if removed is not None:
                    return removed
                raise NotFound("skill is not installed")
            self._same_filesystem()
            backup = "backup-" + secrets.token_hex(12)
            archived = {k: v for k, v in previous.items() if k != "history"}
            receipt = {**previous, "removed_at": now(), "history": [
                *previous["history"], {"receipt": archived, "tree": backup},
            ]}
            write_json(meta / "transaction.json", {"name": name, "stage": None, "backup": backup,
                                                  "previous": previous, "removed": receipt})
            self._recover()
            return receipt

    def session_begin(self, label):
        with self.lock():
            state = self._state()
            token = secrets.token_hex(16)
            state["sessions"][token] = {"label": label, "started": now(), "versions": {
                name: {k:r[k] for k in ("version", "digest")} for name,r in state["installed"].items()}}
            write_json(self.meta / "state.json", state)
            return {"session": token, **state["sessions"][token]}

    def session_end(self, token):
        with self.lock(create=False) as meta:
            if meta is None:
                raise NotFound("session is not registered")
            state = self._state()
            if token not in state["sessions"]:
                raise NotFound("session is not registered")
            del state["sessions"][token]
            write_json(self.meta / "state.json", state)
            return {"ended": token}
