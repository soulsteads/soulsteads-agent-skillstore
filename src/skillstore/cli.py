"""Small JSON CLI; errors have stable codes and nonzero exit status."""
import argparse
import json
import os
import sys
from pathlib import Path

from . import package as pkg
from .errors import SkillstoreError, UsageError
from .install import Installation
from .remote import RemoteStore, open_store
from .store import LocalStore, make_actor
from .targets import plan_target


def target_parser(subparsers, command):
    parser = subparsers.add_parser(command)
    if command == "target":
        parser.add_argument("--harness", required=True)
    else:
        location = parser.add_mutually_exclusive_group(required=True)
        location.add_argument("--target")
        location.add_argument("--harness")
    parser.add_argument("--scope", choices=["project", "profile"], required=command == "target")
    parser.add_argument("--root", required=command == "target")
    return parser


def installation_target(args):
    if args.target is not None:
        if args.scope is not None or args.root is not None:
            raise UsageError("--target cannot be combined with --scope or --root")
        return Installation(args.target)
    if args.scope is None or args.root is None:
        raise UsageError("--harness requires --scope and --root")
    plan = plan_target(args.harness, args.scope, args.root)
    if not plan["managed_install"]:
        raise UsageError("this named target is advisory only", **plan)
    return Installation(plan["target"], require_external=(args.harness, args.scope) != ("codex", "project"))


def git_source_options(parser):
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--repo", help="absolute checkout path (or SKILLSTORE_GIT_REPO)")
    source.add_argument("--git-remote", help="HTTPS Git URL to fetch (or SKILLSTORE_GIT_REMOTE)")
    parser.add_argument("--revision", default="HEAD", help="full commit ID, explicit ref, or HEAD (default)")


def git_location(args):
    if args.repo is not None or args.git_remote is not None:
        return args.repo, args.git_remote
    return os.environ.get("SKILLSTORE_GIT_REPO") or None, os.environ.get("SKILLSTORE_GIT_REMOTE") or None


def parser():
    p = argparse.ArgumentParser(prog="soulsteads-skillstore")
    p.add_argument("--store", default=os.environ.get("SKILLSTORE_STORE"))
    p.add_argument("--actor", default=os.environ.get("SKILLSTORE_ACTOR"))
    p.add_argument("--kind", choices=["human", "agent", "service"], default="human")
    s = p.add_subparsers(dest="command", required=True)
    q = s.add_parser("init"); q.add_argument("path"); q.add_argument("--reviewer", action="append", required=True)
    q = s.add_parser("validate"); q.add_argument("path")
    q = s.add_parser("backup-hermes", help="write a private, local skills-only archive")
    q.add_argument("--profile-root", required=True)
    q.add_argument("--output", required=True)
    q = s.add_parser("restore-hermes", help="verify and recover a backup into a new private directory")
    q.add_argument("archive")
    q.add_argument("--sha256", required=True)
    q.add_argument("--output", required=True)
    q = s.add_parser("tracking", help="keep private local skill snapshots without a registry")
    q.add_argument("--directory", required=True, help="absolute path to an existing owner-only snapshot directory")
    t = q.add_subparsers(dest="tracking_command", required=True)
    b = t.add_parser("backup", help="snapshot the files in an explicit skills directory")
    b.add_argument("--skills-root", required=True)
    t.add_parser("list", help="list local snapshot IDs, newest first")
    r = t.add_parser("restore", help="verify a snapshot and recover into a new directory")
    r.add_argument("snapshot")
    r.add_argument("--output", required=True)
    q = s.add_parser("search"); q.add_argument("query", nargs="?", default="")
    q = s.add_parser("inspect"); q.add_argument("name"); q.add_argument("--version")
    q = s.add_parser("submit"); q.add_argument("path"); q.add_argument("version"); q.add_argument("--source", required=True); q.add_argument("--revision", required=True); q.add_argument("--changelog", default="")
    q = s.add_parser("submit-git", help="submit one committed skill from a checkout or HTTPS Git remote")
    q.add_argument("path", help="skill directory relative to the repository root")
    q.add_argument("version")
    git_source_options(q)
    q.add_argument("--source", help="optional HTTPS provenance label for a checkout (or SKILLSTORE_GIT_SOURCE)")
    q.add_argument("--changelog", default="")
    q = s.add_parser("git-status", help="compare a Git-backed version with a selected checkout or remote revision")
    q.add_argument("name")
    q.add_argument("--version", required=True)
    git_source_options(q)
    q = s.add_parser("review"); q.add_argument("name"); q.add_argument("version"); q.add_argument("decision", choices=["approve", "reject"]); q.add_argument("--note", required=True)
    q = s.add_parser("withdraw"); q.add_argument("name"); q.add_argument("version"); q.add_argument("--reason", required=True)
    q = s.add_parser("diff"); q.add_argument("name"); q.add_argument("old"); q.add_argument("new")
    target_parser(s, "target")
    for command in ["install", "preview"]:
        q = target_parser(s, command)
        q.add_argument("name")
        q.add_argument("--version")
        if command == "preview":
            q.add_argument("--no-recover", action="store_true", help="refuse pending installation recovery")
    q = target_parser(s, "rollback")
    q.add_argument("name")
    q.add_argument("version")
    target_parser(s, "uninstall").add_argument("name")
    target_parser(s, "migrate-state")
    target_parser(s, "status").add_argument("--no-recover", action="store_true",
                                           help="refuse pending installation recovery")
    target_parser(s, "session-begin").add_argument("label")
    target_parser(s, "session-end").add_argument("session")
    q=s.add_parser("propose"); q.add_argument("name"); q.add_argument("path"); q.add_argument("--base-version", required=True); q.add_argument("--rationale", required=True)
    q=s.add_parser("proposals"); q.add_argument("--name")
    q=s.add_parser("proposal-diff"); q.add_argument("id")
    q=s.add_parser("decide"); q.add_argument("id"); q.add_argument("decision", choices=["accept", "reject"]); q.add_argument("--version"); q.add_argument("--note", required=True)
    s.add_parser("verify"); s.add_parser("audit")
    q=s.add_parser("token"); q.add_argument("name"); q.add_argument("--token-file", required=True)
    q=s.add_parser("revoke"); q.add_argument("name")
    q=s.add_parser("serve"); q.add_argument("--host", default="127.0.0.1"); q.add_argument("--port", type=int, default=8765); q.add_argument("--private-read", action="store_true")
    q = s.add_parser("service", help="render or install a systemd listener unit without starting it")
    service = q.add_subparsers(dest="service_command", required=True)
    install_service = service.add_parser("install", help="write a new listener unit; run systemctl manually afterwards")
    scope = install_service.add_mutually_exclusive_group()
    scope.add_argument("--user", dest="scope", action="store_const", const="user")
    scope.add_argument("--system", dest="scope", action="store_const", const="system")
    install_service.set_defaults(scope="user")
    install_service.add_argument("--run-as", metavar="USER", help="required non-root account for --system")
    install_service.add_argument("--dry-run", action="store_true", help="print the unit without writing it")
    q = s.add_parser("serve-mcp", help="serve the reviewed catalog via bearer-authenticated Streamable HTTP on loopback")
    q.add_argument("--port", type=int, default=8766)
    q = target_parser(s, "mcp")
    q.add_argument("--source-root", help="opt in to contributions from an existing absolute staging directory")
    q.add_argument("--backup-dir", help="opt in to local backups into an existing private directory (named Hermes profile only)")
    return p


def execute(a):
    c=a.command
    if c == "target":
        return plan_target(a.harness, a.scope, a.root)
    if c == "backup-hermes":
        from .backup import backup_hermes
        return backup_hermes(a.profile_root, a.output)
    if c == "restore-hermes":
        from .restore import restore_hermes
        return restore_hermes(a.archive, a.sha256, a.output)
    if c == "tracking":
        from . import tracking
        if a.tracking_command == "backup":
            return tracking.create_snapshot(a.directory, a.skills_root)
        if a.tracking_command == "list":
            return tracking.list_snapshots(a.directory)
        return tracking.restore_snapshot(a.directory, a.snapshot, a.output)
    if c == "service":
        from .service import install, invoked_executable
        plan = install(a.store, a.scope, a.run_as, str(invoked_executable(sys.argv[0])), dry_run=a.dry_run)
        return {"path": str(plan.path), "unit": plan.unit, "scope": a.scope,
                "dry_run": a.dry_run, "manager_activated": False}
    if c == "mcp" and a.backup_dir is not None and (a.harness, a.scope) != ("hermes", "profile"):
        raise UsageError("--backup-dir requires --harness hermes --scope profile --root PATH")
    target = installation_target(a) if hasattr(a, "target") else None
    actor=make_actor(a.actor, a.kind)
    if c == "init": return LocalStore.init(a.path, a.reviewer, actor).info()
    if c == "validate":
        report=pkg.validate(pkg.read_tree(a.path))
        if not report["ok"]: raise UsageError("package validation failed", report=report)
        return report
    if target is not None:
        if c=="status": return target.status(recover=not a.no_recover)
        if c=="rollback": return target.rollback(a.name,a.version)
        if c=="uninstall": return target.uninstall(a.name)
        if c=="migrate-state": return target.migrate_state()
        if c=="session-begin": return target.session_begin(a.label)
        if c=="session-end": return target.session_end(a.session)
    store=open_store(a.store)
    if c=="search": return store.list_skills(a.query)
    if c=="inspect": return store.release(a.name,a.version) if a.version else store.skill_info(a.name)
    if c=="submit": return store.submit(pkg.pack(pkg.read_tree(a.path)),a.version,actor,{"type":"source","uri":a.source,"revision":a.revision},a.changelog)
    if c in ("submit-git", "git-status"):
        from .git_source import git_files
        pkg.parse_version(a.version)
        repo, remote = git_location(a)
        if c == "submit-git":
            uri = a.source
            if uri is None and not remote:
                uri = os.environ.get("SKILLSTORE_GIT_SOURCE") or None
            files, source = git_files(repo, a.path, a.revision, uri, remote=remote)
            return store.submit(pkg.pack(files), a.version, actor, source, a.changelog)
        release = store.release(a.name, a.version)
        previous = release["provenance"]["source"]
        if previous.get("type") != "git" or not all(isinstance(previous.get(k), str) for k in ("uri", "path", "revision")):
            raise UsageError("this version was not submitted from a tracked Git source")
        if remote and remote != previous["uri"]:
            raise UsageError("--git-remote must match the Git source recorded for this version")
        files, source = git_files(repo, previous["path"], a.revision, None if remote else previous["uri"],
                                  remote=remote, expected_name=a.name)
        digest = pkg.tree_digest(files)
        return {"name": a.name, "version": a.version, "path": source["path"],
                "recorded_revision": previous["revision"], "revision": source["revision"],
                "recorded_digest": release["digest"], "digest": digest,
                "content_changed": digest != release["digest"]}
    if c=="review": return store.review(a.name,a.version,a.decision,actor,a.note)
    if c=="withdraw": return store.withdraw(a.name,a.version,actor,a.reason)
    if c=="diff": return store.diff(a.name,a.old,a.new)
    if c=="install" and target is not None: return target.install(store,a.name,a.version)
    if c=="preview" and target is not None: return target.preview(store,a.name,a.version,recover=not a.no_recover)
    if c=="propose": return store.propose(pkg.pack(pkg.read_tree(a.path)),a.name,actor,a.rationale,a.base_version)
    if c=="proposals": return store.list_proposals(skill=a.name)
    if c=="proposal-diff": return store.proposal_diff(a.id)
    if c=="decide": return store.decide_proposal(a.id,a.decision,actor,a.note,a.version)
    if c=="verify":
        result=store.verify()
        if not result["ok"]: raise UsageError("store integrity check failed", report=result)
        return result
    if c=="audit": return store.audit()
    if c=="mcp" and target is not None:
        from .mcp import serve
        source = None
        backup = None
        if a.backup_dir is not None:
            from .backup import HermesBackup
            backup = HermesBackup(a.root, a.backup_dir)
            if a.source_root is not None and (
                Path(a.source_root).is_relative_to(a.backup_dir) or Path(a.backup_dir).is_relative_to(a.source_root)
            ):
                raise UsageError("backup and contribution directories must not overlap")
        if a.source_root is not None:
            from .contribution import ContributionSource
            if isinstance(store, RemoteStore) and not store.token:
                raise UsageError("MCP contributions to a remote registry require SKILLSTORE_TOKEN")
            source = ContributionSource(a.source_root)
        serve(store,target,source=source,actor=make_actor(a.actor or "mcp-contributor", "agent"),backup=backup); return None
    if not isinstance(store,LocalStore): raise UsageError("this operation requires direct local store access")
    if c == "serve-mcp":
        from .mcp_http import serve
        return serve(store, a.port)
    from . import server
    if c=="serve": server.serve(str(store.root),a.host,a.port,a.private_read); return None
    if c=="token":
        # Never expose bearer tokens in stdout, shell history, or metadata.
        fd=os.open(a.token_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,"w") as f: f.write(server.add_token(store.root,a.name,a.kind)+"\n")
        except BaseException:
            Path(a.token_file).unlink(missing_ok=True);raise
        return {"token_file":a.token_file,"actor":a.name}
    if c=="revoke": return {"revoked":server.revoke_tokens(store.root,a.name)}
    raise UsageError("unknown command")


def main():
    try:
        result=execute(parser().parse_args())
        if result is not None: print(json.dumps(result,indent=2))
    except SkillstoreError as exc:
        print(json.dumps(exc.to_dict()),file=sys.stderr);return exc.exit_code
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({"ok":False,"error":"io_or_data_error","message":str(exc)}),file=sys.stderr);return 1
    return 0
