"""Small JSON CLI; errors have stable codes and nonzero exit status."""
import argparse
import json
import os
import sys
from pathlib import Path

from . import package as pkg
from .errors import SkillstoreError, UsageError
from .install import Installation
from .remote import open_store
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


def parser():
    p = argparse.ArgumentParser(prog="skillstore")
    p.add_argument("--store", default=os.environ.get("SKILLSTORE_STORE"))
    p.add_argument("--actor", default=os.environ.get("SKILLSTORE_ACTOR"))
    p.add_argument("--kind", choices=["human", "agent", "service"], default="human")
    s = p.add_subparsers(dest="command", required=True)
    q = s.add_parser("init"); q.add_argument("path"); q.add_argument("--reviewer", action="append", required=True)
    q = s.add_parser("validate"); q.add_argument("path")
    q = s.add_parser("search"); q.add_argument("query", nargs="?", default="")
    q = s.add_parser("inspect"); q.add_argument("name"); q.add_argument("--version")
    q = s.add_parser("submit"); q.add_argument("path"); q.add_argument("version"); q.add_argument("--source", required=True); q.add_argument("--revision", required=True); q.add_argument("--changelog", default="")
    q = s.add_parser("review"); q.add_argument("name"); q.add_argument("version"); q.add_argument("decision", choices=["approve", "reject"]); q.add_argument("--note", required=True)
    q = s.add_parser("withdraw"); q.add_argument("name"); q.add_argument("version"); q.add_argument("--reason", required=True)
    q = s.add_parser("diff"); q.add_argument("name"); q.add_argument("old"); q.add_argument("new")
    target_parser(s, "target")
    for command in ["install", "preview"]:
        q = target_parser(s, command)
        q.add_argument("name")
        q.add_argument("--version")
    q = target_parser(s, "rollback")
    q.add_argument("name")
    q.add_argument("version")
    target_parser(s, "uninstall").add_argument("name")
    target_parser(s, "migrate-state")
    target_parser(s, "status")
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
    target_parser(s, "mcp")
    return p


def execute(a):
    c=a.command
    if c == "target":
        return plan_target(a.harness, a.scope, a.root)
    target = installation_target(a) if hasattr(a, "target") else None
    actor=make_actor(a.actor, a.kind)
    if c == "init": return LocalStore.init(a.path, a.reviewer, actor).info()
    if c == "validate":
        report=pkg.validate(pkg.read_tree(a.path))
        if not report["ok"]: raise UsageError("package validation failed", report=report)
        return report
    if target is not None:
        if c=="status": return target.status()
        if c=="rollback": return target.rollback(a.name,a.version)
        if c=="uninstall": return target.uninstall(a.name)
        if c=="migrate-state": return target.migrate_state()
        if c=="session-begin": return target.session_begin(a.label)
        if c=="session-end": return target.session_end(a.session)
    store=open_store(a.store)
    if c=="search": return store.list_skills(a.query)
    if c=="inspect": return store.release(a.name,a.version) if a.version else store.skill_info(a.name)
    if c=="submit": return store.submit(pkg.pack(pkg.read_tree(a.path)),a.version,actor,{"type":"source","uri":a.source,"revision":a.revision},a.changelog)
    if c=="review": return store.review(a.name,a.version,a.decision,actor,a.note)
    if c=="withdraw": return store.withdraw(a.name,a.version,actor,a.reason)
    if c=="diff": return store.diff(a.name,a.old,a.new)
    if c in ("install","preview") and target is not None: return getattr(target,c)(store,a.name,a.version)
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
        serve(store,target); return None
    if not isinstance(store,LocalStore): raise UsageError("this operation requires direct local store access")
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
