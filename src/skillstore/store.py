"""Directory-backed central skill store.

Layout (plain files, browsable without Skillstore)::

    <store>/store.json                                  identity and review policy
    <store>/skills/<name>/versions/<version>/release.json   provenance, validation, review
    <store>/skills/<name>/versions/<version>/package/       the ordinary Agent Skills package
    <store>/proposals/<id>/proposal.json + package/     improvement proposals (not releases)
    <store>/audit.jsonl                                 append-only, hash-chained history

Version states: ``pending`` (validated candidate, not authoritative),
``reviewed`` (authoritative), ``rejected``, ``withdrawn``. Only an explicit
review by an authorised reviewer who is not the submitter moves a version or
proposal to ``reviewed``; nothing in this module does that implicitly.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, package as pkg
from .errors import Conflict, IntegrityError, NotFound, NotReviewed, PermissionDenied, UsageError, ValidationFailed

FORMAT = 1
STATUSES = ("pending", "reviewed", "rejected", "withdrawn")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, data) -> None:
    """Atomically write JSON (temp file + rename)."""
    pkg.mkdir_durable(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path):
    return json.loads(path.read_text())


def make_actor(actor: str | None = None, kind: str | None = None) -> dict:
    """Identity recorded in provenance. In directory mode this is declared, not authenticated."""
    name = actor or os.environ.get("SKILLSTORE_ACTOR") or os.environ.get("USER") or "unknown"
    return {"actor": name, "kind": kind or os.environ.get("SKILLSTORE_ACTOR_KIND") or "human", "authenticated": False}


def _entry_hash(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class LocalStore:
    """A store in a local or shared directory."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).resolve()
        if not (self.root / "store.json").is_file():
            raise NotFound(f"no skill store at {self.root} (run 'skillstore store init {self.root}')")
        self.location = str(self.root)
        with self._lock():
            pass  # Finish any durable metadata transaction left by a failed process.

    # ------------------------------------------------------------ setup

    @classmethod
    def init(cls, root: str | os.PathLike, reviewers: list[str] | None = None, actor: dict | None = None) -> "LocalStore":
        root = Path(root).resolve()
        if (root / "store.json").exists():
            raise Conflict(f"a store already exists at {root}")
        if root.exists() and any(root.iterdir()):
            raise Conflict(f"{root} exists and is not empty")
        pkg.mkdir_durable(root / "skills")
        pkg.mkdir_durable(root / "proposals")
        pkg.sync_directory(root.parent)
        write_json(
            root / "store.json",
            {
                "format": FORMAT,
                "store_id": "store-" + secrets.token_hex(6),
                "created_at": now(),
                "policy": {"reviewers": sorted(set(reviewers or [])), "allow_self_review": False, "allow_agent_review": False},
            },
        )
        store = cls(root)
        with store._lock():
            store._audit(actor or make_actor(), "store.init", "store", {"reviewers": sorted(set(reviewers or []))})
        return store

    @contextlib.contextmanager
    def _lock(self):
        with open(self.root / ".lock", "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                self._recover()
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def config(self) -> dict:
        return read_json(self.root / "store.json")

    def info(self) -> dict:
        cfg = self.config()
        names = self._skill_names()
        return {
            "store_id": cfg["store_id"],
            "format": cfg["format"],
            "location": self.location,
            "created_at": cfg["created_at"],
            "policy": cfg["policy"],
            "skills": len(names),
            "server_version": __version__,
        }

    def set_reviewers(self, actor: dict, add: list[str] = (), remove: list[str] = ()) -> dict:
        """Change who may review. Only possible with direct access to the store directory."""
        with self._lock():
            cfg = self.config()
            reviewers = (set(cfg["policy"]["reviewers"]) | set(add)) - set(remove)
            cfg["policy"]["reviewers"] = sorted(reviewers)
            write_json(self.root / "store.json", cfg)
            self._audit(actor, "store.reviewers", "store", {"reviewers": cfg["policy"]["reviewers"]})
        return cfg["policy"]

    # ------------------------------------------------------------ audit

    def _audit(self, actor: dict, action: str, subject: str, details: dict | None = None, event_id=None) -> dict:
        path = self.root / "audit.jsonl"
        prev, seq = "0" * 64, 0
        lines = []
        if path.exists():
            lines = path.read_text().splitlines()
            if event_id:
                found = next((json.loads(line) for line in lines if json.loads(line).get("event_id") == event_id), None)
                if found:
                    return found
            if lines:
                last = json.loads(lines[-1])
                prev, seq = last["hash"], last["seq"] + 1
        entry = {
            "seq": seq,
            "at": now(),
            "actor": actor.get("actor"),
            "actor_kind": actor.get("kind"),
            "authenticated": bool(actor.get("authenticated")),
            "action": action,
            "subject": subject,
            "details": details or {},
            "prev": prev,
            "event_id": event_id,
        }
        entry["hash"] = _entry_hash(entry)
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".audit-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join([*lines, json.dumps(entry, sort_keys=True)]) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            with contextlib.suppress(FileNotFoundError): os.unlink(temporary)
        return entry

    def _commit_records(self, records, events):
        """Write-ahead metadata journal. Audit receipts precede authoritative state.

        An interrupted call can have taken effect. Opening the store or taking
        its next write lock idempotently completes the recorded operation.
        """
        transaction = {"records": [{"path": str(path.relative_to(self.root)), "data": data} for path, data in records],
                       "events": [{**event, "event_id": secrets.token_hex(16)} for event in events]}
        write_json(self.root / ".transaction.json", transaction)
        self._recover()

    def _recover(self):
        journal = self.root / ".transaction.json"
        if not journal.exists(): return
        transaction = read_json(journal)
        for event in transaction["events"]:
            self._audit(**event)
        for record in transaction["records"]:
            pkg._check_relpath(record["path"])
            write_json(self.root / record["path"], record["data"])
        journal.unlink()
        pkg.sync_directory(self.root)

    def audit(self, limit: int = 50, subject: str | None = None) -> list[dict]:
        path = self.root / "audit.jsonl"
        entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
        if subject:
            entries = [e for e in entries if e["subject"] == subject or e["subject"].startswith(subject + "@")]
        return entries[-limit:] if limit else entries

    # ------------------------------------------------------------ helpers

    def _skill_names(self) -> list[str]:
        d = self.root / "skills"
        return sorted(p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")) if d.is_dir() else []

    def _version_dir(self, name: str, version: str) -> Path:
        if not pkg.NAME_RE.match(name or ""):
            raise UsageError(f"invalid skill name {name!r}")
        pkg.parse_version(version)
        return self.root / "skills" / name / "versions" / version

    def _releases(self, name: str) -> list[dict]:
        d = self.root / "skills" / name / "versions"
        if not pkg.NAME_RE.match(name or "") or not d.is_dir():
            raise NotFound(f"no skill named {name!r} in the store")
        out = [read_json(p / "release.json") for p in d.iterdir() if not p.name.startswith(".") and (p / "release.json").is_file()]
        return sorted(out, key=lambda r: pkg.parse_version(r["version"]))

    @staticmethod
    def _latest_reviewed(releases: list[dict]) -> dict | None:
        reviewed = [r for r in releases if r["status"] == "reviewed"]
        stable = [r for r in reviewed if not pkg.is_prerelease(r["version"])]
        pool = stable or reviewed
        return pool[-1] if pool else None

    @staticmethod
    def _summary(r: dict) -> dict:
        prov = r["provenance"]
        return {
            "version": r["version"],
            "status": r["status"],
            "digest": r["digest"],
            "submitted_by": prov["submitted_by"]["actor"],
            "submitted_at": prov["submitted_at"],
            "reviewed_by": (r.get("review") or {}).get("reviewer"),
            "changelog": r.get("changelog", ""),
        }

    def _is_reviewer(self, actor: dict) -> bool:
        return actor.get("actor") in self.config()["policy"]["reviewers"]

    def _require_reviewer(self, actor: dict, submitted_by: dict, what: str) -> None:
        policy = self.config()["policy"]
        who = actor.get("actor")
        if who not in policy["reviewers"]:
            raise PermissionDenied(f"{who!r} is not a reviewer for this store; {what} needs an authorised reviewer")
        if actor.get("kind") == "agent" and not policy.get("allow_agent_review"):
            raise PermissionDenied(f"agents may not review in this store; {what} needs a human reviewer")
        if who == submitted_by.get("actor") and not policy.get("allow_self_review"):
            raise PermissionDenied(f"{who!r} submitted this content and cannot also review it")

    # ------------------------------------------------------------ discovery

    def list_skills(self, query: str | None = None, include_unreviewed: bool = False) -> list[dict]:
        terms = (query or "").lower().split()
        out = []
        for name in self._skill_names():
            releases = self._releases(name)
            latest = self._latest_reviewed(releases)
            if not latest and not include_unreviewed:
                continue
            shown = latest or releases[-1]
            haystack = f"{name} {shown.get('description', '')}".lower()
            if any(t not in haystack for t in terms):
                continue
            out.append(
                {
                    "name": name,
                    "description": shown.get("description", ""),
                    "latest_reviewed": latest["version"] if latest else None,
                    "versions": [
                        {"version": r["version"], "status": r["status"]}
                        for r in releases
                        if include_unreviewed or r["status"] in ("reviewed", "withdrawn")
                    ],
                }
            )
        return out

    def skill_info(self, name: str) -> dict:
        releases = self._releases(name)
        latest = self._latest_reviewed(releases)
        shown = latest or releases[-1]
        return {
            "name": name,
            "description": shown.get("description", ""),
            "latest_reviewed": latest["version"] if latest else None,
            "versions": [self._summary(r) for r in releases],
            "open_proposals": [p["id"] for p in self.list_proposals(skill=name, status="open")],
        }

    def release(self, name: str, version: str | None = None) -> dict:
        """Full release record. Without a version: the latest reviewed release."""
        releases = self._releases(name)
        if version is None:
            latest = self._latest_reviewed(releases)
            if not latest:
                raise NotReviewed(f"skill {name!r} has no reviewed version yet")
            return latest
        pkg.parse_version(version)
        for r in releases:
            if r["version"] == version:
                return r
        raise NotFound(f"skill {name!r} has no version {version}")

    def _tree(self, name: str, version: str) -> dict:
        release = self.release(name, version)
        files = pkg.read_tree(self._version_dir(name, version) / "package")
        pkg.verify_digest(files, release["digest"], f"{name}@{version} in store {self.location}")
        return files

    def package(self, name: str, version: str) -> bytes:
        """The package archive, verified against the recorded digest first."""
        return pkg.pack(self._tree(name, version))

    def diff(self, name: str, from_version: str, to_version: str) -> dict:
        old, new = self._tree(name, from_version), self._tree(name, to_version)
        result = pkg.diff_trees(old, new, f"{name}@{from_version}", f"{name}@{to_version}")
        return {"name": name, "from": from_version, "to": to_version, **result}

    # ------------------------------------------------------------ publishing

    def submit(
        self,
        package_bytes: bytes,
        version: str,
        actor: dict,
        source: dict | None = None,
        changelog: str = "",
    ) -> dict:
        """Validate a package and record it as a ``pending`` candidate.

        A candidate is never authoritative: it needs :meth:`review`.
        """
        pkg.parse_version(version)
        try:
            files = pkg.unpack(package_bytes)
            report = pkg.require_valid(files)
        except ValidationFailed as exc:
            with self._lock():
                self._audit(actor, "version.submit_rejected", f"?@{version}", {"errors": (exc.details.get("report") or {}).get("errors", [])})
            raise
        name = report["name"]
        with self._lock():
            vdir = self._version_dir(name, version)
            if vdir.exists():
                raise Conflict(f"{name}@{version} already exists; versions are immutable, choose a new version")
            release = self._write_release(name, version, files, report, actor, source, changelog, origin={"type": "submission"})
            self._audit(actor, "version.submitted", f"{name}@{version}", {"digest": release["digest"], "source": release["provenance"]["source"]})
        return release

    def _write_release(self, name, version, files, report, actor, source, changelog, origin, status="pending", review=None) -> dict:
        vdir = self._version_dir(name, version)
        release = {
            "name": name,
            "version": version,
            "status": status,
            "description": report["description"],
            "digest": pkg.tree_digest(files),
            "files": pkg.manifest(files),
            "changelog": changelog or "",
            "provenance": {
                "store_id": self.config()["store_id"],
                "submitted_by": {"actor": actor.get("actor"), "kind": actor.get("kind"), "authenticated": bool(actor.get("authenticated"))},
                "submitted_at": now(),
                "source": source or {"type": "unspecified"},
                "origin": origin,
            },
            "validation": {**report, "validated_at": now(), "validator": f"skillstore {__version__}"},
            "review": review,
            "withdrawal": None,
        }
        staging = vdir.parent / f".{version}.{secrets.token_hex(4)}.tmp"
        try:
            pkg.write_tree(files, staging / "package")
            write_json(staging / "release.json", release)
            os.replace(staging, vdir)
            pkg.sync_directory(vdir.parent)
            pkg.sync_directory(vdir.parent.parent)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return release

    def review(self, name: str, version: str, decision: str, actor: dict, note: str = "") -> dict:
        """Explicit review of a pending candidate: ``approve`` or ``reject``."""
        if decision not in ("approve", "reject"):
            raise UsageError("decision must be 'approve' or 'reject'")
        with self._lock():
            release = self.release(name, version)
            if release["status"] != "pending":
                raise Conflict(f"{name}@{version} is {release['status']}, not pending review")
            self._require_reviewer(actor, release["provenance"]["submitted_by"], f"reviewing {name}@{version}")
            if decision == "approve":
                files = self._tree(name, version)  # digest re-verified
                pkg.require_valid(files, expected_name=name)  # validation re-run at review time
            release["status"] = "reviewed" if decision == "approve" else "rejected"
            release["review"] = {
                "decision": decision,
                "reviewer": actor["actor"],
                "reviewer_kind": actor.get("kind"),
                "authenticated": bool(actor.get("authenticated")),
                "reviewed_at": now(),
                "note": note,
                "digest_reviewed": release["digest"],
            }
            self._commit_records([(self._version_dir(name, version) / "release.json", release)], [
                {"actor": actor, "action": f"version.{'approved' if decision == 'approve' else 'rejected'}", "subject": f"{name}@{version}", "details": {"digest": release["digest"], "note": note}}])
        return release

    def withdraw(self, name: str, version: str, actor: dict, reason: str) -> dict:
        """Mark a reviewed version as bad. It stays inspectable but is no longer offered."""
        if not reason:
            raise UsageError("a reason is required to withdraw a version")
        with self._lock():
            release = self.release(name, version)
            if release["status"] != "reviewed":
                raise Conflict(f"{name}@{version} is {release['status']}; only reviewed versions can be withdrawn")
            if not self._is_reviewer(actor):
                raise PermissionDenied(f"{actor.get('actor')!r} is not a reviewer for this store")
            release["status"] = "withdrawn"
            release["withdrawal"] = {"by": actor["actor"], "at": now(), "reason": reason}
            self._commit_records([(self._version_dir(name, version) / "release.json", release)], [
                {"actor": actor, "action": "version.withdrawn", "subject": f"{name}@{version}", "details": {"digest": release["digest"], "reason": reason}}])
        return release

    # ------------------------------------------------------------ proposals

    def _proposal_dir(self, proposal_id: str) -> Path:
        if not proposal_id or not proposal_id.replace("-", "").isalnum():
            raise UsageError(f"invalid proposal id {proposal_id!r}")
        d = self.root / "proposals" / proposal_id
        if not (d / "proposal.json").is_file():
            raise NotFound(f"no proposal {proposal_id!r}")
        return d

    def propose(
        self,
        package_bytes: bytes,
        skill: str,
        actor: dict,
        rationale: str,
        base_version: str | None = None,
        source: dict | None = None,
    ) -> dict:
        """Record an improvement proposal for an existing skill.

        A proposal is not a version and cannot be installed. It becomes a
        release only through :meth:`decide_proposal` by a reviewer.
        """
        if not rationale or not rationale.strip():
            raise UsageError("a rationale is required for a proposal")
        base = self.release(skill, base_version)
        if base["status"] not in ("reviewed", "withdrawn"):
            raise NotReviewed(f"proposals must build on a reviewed version; {skill}@{base['version']} is {base['status']}")
        try:
            files = pkg.unpack(package_bytes)
            report = pkg.require_valid(files, expected_name=skill)
        except ValidationFailed as exc:
            with self._lock():
                self._audit(actor, "proposal.submit_rejected", skill, {"errors": (exc.details.get("report") or {}).get("errors", [])})
            raise
        digest = pkg.tree_digest(files)
        if digest == base["digest"]:
            raise Conflict(f"proposal is identical to {skill}@{base['version']}; nothing to propose")
        with self._lock():
            existing = [p.name for p in (self.root / "proposals").iterdir() if p.is_dir() and not p.name.startswith(".")]
            pid = f"p{len(existing) + 1:04d}-{secrets.token_hex(2)}"
            proposal = {
                "id": pid,
                "skill": skill,
                "base_version": base["version"],
                "base_digest": base["digest"],
                "status": "open",
                "rationale": rationale.strip(),
                "digest": digest,
                "files": pkg.manifest(files),
                "submitted_by": {"actor": actor.get("actor"), "kind": actor.get("kind"), "authenticated": bool(actor.get("authenticated"))},
                "submitted_at": now(),
                "source": source or {"type": "unspecified"},
                "validation": {**report, "validated_at": now(), "validator": f"skillstore {__version__}"},
                "decision": None,
            }
            staging = self.root / "proposals" / f".{pid}.tmp"
            pkg.write_tree(files, staging / "package")
            write_json(staging / "proposal.json", proposal)
            os.replace(staging, self.root / "proposals" / pid)
            pkg.sync_directory(self.root / "proposals")
            self._audit(actor, "proposal.submitted", f"{skill}#{pid}", {"digest": digest, "base_version": base["version"]})
        return proposal

    def list_proposals(self, skill: str | None = None, status: str | None = None) -> list[dict]:
        d = self.root / "proposals"
        out = []
        for p in sorted(d.iterdir()) if d.is_dir() else []:
            if not (p / "proposal.json").is_file():
                continue
            rec = read_json(p / "proposal.json")
            if (skill and rec["skill"] != skill) or (status and rec["status"] != status):
                continue
            out.append({k: rec[k] for k in ("id", "skill", "base_version", "status", "rationale", "digest", "submitted_by", "submitted_at", "decision")})
        return out

    def proposal(self, proposal_id: str) -> dict:
        return read_json(self._proposal_dir(proposal_id) / "proposal.json")

    def _proposal_tree(self, proposal_id: str) -> dict:
        d = self._proposal_dir(proposal_id)
        rec = read_json(d / "proposal.json")
        files = pkg.read_tree(d / "package")
        pkg.verify_digest(files, rec["digest"], f"proposal {proposal_id}")
        return files

    def proposal_diff(self, proposal_id: str) -> dict:
        rec = self.proposal(proposal_id)
        base = self._tree(rec["skill"], rec["base_version"])
        result = pkg.diff_trees(base, self._proposal_tree(proposal_id), f"{rec['skill']}@{rec['base_version']}", f"proposal-{proposal_id}")
        return {"id": proposal_id, "skill": rec["skill"], "base_version": rec["base_version"], **result}

    def decide_proposal(
        self,
        proposal_id: str,
        decision: str,
        actor: dict,
        note: str = "",
        version: str | None = None,
        changelog: str | None = None,
    ) -> dict:
        """Reviewer decision on a proposal.

        ``accept`` re-validates the content and publishes it as a new reviewed
        version (``version`` required) whose provenance names the proposal,
        proposer and reviewer. ``reject`` records the reason; nothing is published.
        """
        if decision not in ("accept", "reject"):
            raise UsageError("decision must be 'accept' or 'reject'")
        with self._lock():
            d = self._proposal_dir(proposal_id)
            rec = read_json(d / "proposal.json")
            if rec["status"] != "open":
                raise Conflict(f"proposal {proposal_id} is already {rec['status']}")
            self._require_reviewer(actor, rec["submitted_by"], f"deciding proposal {proposal_id}")
            release = None
            events = []
            if decision == "accept":
                if not version:
                    raise UsageError("accepting a proposal requires the version to publish it as")
                skill = rec["skill"]
                files = self._proposal_tree(proposal_id)  # digest verified
                report = pkg.require_valid(files, expected_name=skill)  # validation re-run
                review = {
                    "decision": "approve",
                    "reviewer": actor["actor"],
                    "reviewer_kind": actor.get("kind"),
                    "authenticated": bool(actor.get("authenticated")),
                    "reviewed_at": now(),
                    "note": note,
                    "digest_reviewed": rec["digest"],
                }
                if self._version_dir(skill, version).exists():
                    release = self.release(skill, version)
                    if release["status"] != "pending" or release["provenance"]["origin"].get("proposal_id") != proposal_id or release["digest"] != rec["digest"]:
                        raise Conflict(f"{skill}@{version} already exists; choose a new version")
                else:
                    release = self._write_release(
                        skill, version, files, report, rec["submitted_by"], rec["source"],
                        changelog or rec["rationale"],
                        origin={"type": "proposal", "proposal_id": proposal_id, "base_version": rec["base_version"]})
                release["status"], release["review"] = "reviewed", review
                events.append({"actor":actor,"action":"version.approved","subject":f"{skill}@{version}","details":{"digest":release["digest"],"note":note,"proposal_id":proposal_id}})
            rec["status"] = "accepted" if decision == "accept" else "rejected"
            rec["decision"] = {
                "decision": decision,
                "reviewer": actor["actor"],
                "decided_at": now(),
                "note": note,
                "released_version": version if decision == "accept" else None,
            }
            records = [(d / "proposal.json", rec)]
            if release:
                records.append((self._version_dir(rec["skill"], version) / "release.json", release))
            events.append({"actor":actor,"action":f"proposal.{rec['status']}","subject":f"{rec['skill']}#{proposal_id}","details":{"note":note,"released_version":rec["decision"]["released_version"]}})
            self._commit_records(records, events)
        return {"proposal": rec, "release": release}

    # ------------------------------------------------------------ integrity

    def verify(self) -> dict:
        """Check every stored package against its digest and the audit chain."""
        problems: list[str] = []
        checked = 0
        entries = self.audit(limit=0)
        prev = "0" * 64
        for i, e in enumerate(entries):
            if e.get("seq") != i or e.get("prev") != prev or _entry_hash(e) != e.get("hash"):
                problems.append(f"audit log entry {i} breaks the hash chain (edited, removed or reordered)")
                break
            prev = e["hash"]
        approved = {e["subject"]: e["details"].get("digest") for e in entries if e["action"] == "version.approved"}
        submitted = {e["subject"]: e["details"].get("digest") for e in entries if e["action"] == "version.submitted"}
        for name in self._skill_names():
            for r in self._releases(name):
                ref = f"{name}@{r['version']}"
                checked += 1
                try:
                    files = pkg.read_tree(self._version_dir(name, r["version"]) / "package")
                    pkg.verify_digest(files, r["digest"], ref)
                except (IntegrityError, ValidationFailed) as exc:
                    problems.append(exc.message)
                recorded = approved.get(ref) if r["status"] in ("reviewed", "withdrawn") else submitted.get(ref)
                if recorded is None:
                    problems.append(f"{ref} is {r['status']} but the audit log has no matching record")
                elif recorded != r["digest"]:
                    problems.append(f"{ref} digest {r['digest']} differs from the audited digest {recorded}")
        for p in self.list_proposals():
            checked += 1
            try:
                self._proposal_tree(p["id"])
            except (IntegrityError, ValidationFailed) as exc:
                problems.append(exc.message)
        return {"ok": not problems, "checked": checked, "audit_entries": len(entries), "problems": problems}
