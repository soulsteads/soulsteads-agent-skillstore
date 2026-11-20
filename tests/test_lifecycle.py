import contextlib
import io
import json
import os
import tarfile
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from skillstore import package as pkg
from skillstore.errors import Conflict, NotReviewed, PermissionDenied, ValidationFailed
from skillstore.install import Installation
from skillstore.remote import RemoteStore
from skillstore.server import add_token, make_server, revoke_tokens
from skillstore.store import LocalStore, make_actor


def tree(text="Reply with SKILL_VERSION_ONE."):
    return {"SKILL.md": (f"---\nname: receipt-check\ndescription: A harmless receipt check for qualification.\n---\n{text}\n".encode(), False)}


@pytest.fixture
def store(tmp_path):
    return LocalStore.init(tmp_path / "registry", ["reviewer"])


def release(store, version="1.0.0", text="Reply with SKILL_VERSION_ONE."):
    store.submit(pkg.pack(tree(text)), version, make_actor("author"), {"uri":"https://example.org/skills.git","revision":"test-revision"})
    return store.review("receipt-check",version,"approve",make_actor("reviewer"),"Reviewed test fixture")


def test_review_install_update_local_edits_rollback(store,tmp_path):
    release(store)
    dest=Installation(tmp_path/"skills")
    installed=dest.install(store,"receipt-check","1.0.0")
    assert installed["provenance"]["source"]["revision"]=="test-revision"
    release(store,"2.0.0","Reply with SKILL_VERSION_TWO.")
    assert "SKILL_VERSION_TWO" in dest.preview(store,"receipt-check","2.0.0")["diff"]
    (tmp_path/"skills/receipt-check/local.txt").write_text("preserve me")
    with pytest.raises(Conflict): dest.install(store,"receipt-check","2.0.0")
    assert (tmp_path/"skills/receipt-check/local.txt").read_text()=="preserve me"
    (tmp_path/"skills/receipt-check/local.txt").unlink()
    dest.install(store,"receipt-check","2.0.0")
    dest.rollback("receipt-check","1.0.0")
    assert "SKILL_VERSION_ONE" in (tmp_path/"skills/receipt-check/SKILL.md").read_text()
    assert store.verify()["ok"]
    assert len(dest.status()["installed"][0]["history"])==2


def test_unreviewed_self_agent_and_withdrawal_guards(store,tmp_path):
    store.submit(pkg.pack(tree()),"1.0.0",make_actor("author"))
    with pytest.raises(NotReviewed): Installation(tmp_path/"skills").install(store,"receipt-check","1.0.0")
    for actor in (make_actor("author"),make_actor("reviewer","agent")):
        with pytest.raises(PermissionDenied): store.review("receipt-check","1.0.0","approve",actor)
    store.review("receipt-check","1.0.0","approve",make_actor("reviewer"))
    store.withdraw("receipt-check","1.0.0",make_actor("reviewer"),"bad instructions")
    with pytest.raises(NotReviewed): Installation(tmp_path/"skills").install(store,"receipt-check","1.0.0")


def test_proposal_needs_independent_review(store):
    release(store)
    proposal=store.propose(pkg.pack(tree("Reply with PROPOSED.")),"receipt-check",make_actor("worker","agent"),"Clearer response","1.0.0")
    assert store.release("receipt-check")["version"]=="1.0.0"
    with pytest.raises(PermissionDenied): store.decide_proposal(proposal["id"],"accept",make_actor("worker","agent"),version="2.0.0")
    result=store.decide_proposal(proposal["id"],"accept",make_actor("reviewer"),version="2.0.0",note="Reviewed diff")
    assert result["release"]["provenance"]["origin"]["proposal_id"]==proposal["id"]
    assert store.verify()["ok"]


def test_registered_sessions_block_mutation(store,tmp_path):
    release(store);release(store,"2.0.0","New instructions")
    target=Installation(tmp_path/"skills");target.install(store,"receipt-check","1.0.0")
    session=target.session_begin("active harness")
    with pytest.raises(Conflict):target.install(store,"receipt-check","2.0.0")
    reloaded=Installation(tmp_path/"skills")
    assert session["session"] in reloaded.status()["sessions"]
    reloaded.session_end(session["session"])
    reloaded.install(store,"receipt-check","2.0.0")


def test_audit_failure_cannot_publish_reviewed_state(store,monkeypatch):
    store.submit(pkg.pack(tree()),"1.0.0",make_actor("author"))
    def fail(*args,**kwargs): raise OSError("fault: audit unavailable")
    monkeypatch.setattr(store,"_audit",fail)
    with pytest.raises(OSError):store.review("receipt-check","1.0.0","approve",make_actor("reviewer"))
    assert store.release("receipt-check","1.0.0")["status"]=="pending"
    recovered=LocalStore(store.root)
    assert recovered.release("receipt-check","1.0.0")["status"]=="reviewed"
    assert recovered.verify()["ok"]
    assert sum(e["action"]=="version.approved" for e in recovered.audit())==1


def test_install_recovery_after_old_tree_renamed(store,tmp_path,monkeypatch):
    import skillstore.install as install
    release(store); release(store,"2.0.0","Updated instructions")
    target=Installation(tmp_path/"skills");target.install(store,"receipt-check","1.0.0")
    real_replace=install.os.replace
    def fail(source,dest):
        if str(source).split('/')[-1].startswith("stage-"):
            raise OSError("fault: power loss before new tree publish")
        return real_replace(source,dest)
    with monkeypatch.context() as m:
        m.setattr(install.os,"replace",fail)
        with pytest.raises(OSError):target.install(store,"receipt-check","2.0.0")
    recovered=Installation(tmp_path/"skills")
    assert recovered.status()["installed"][0]["version"]=="2.0.0"
    recovered.rollback("receipt-check","1.0.0")
    assert recovered.status()["installed"][0]["local"]=="clean"


def test_unowned_and_symlink_targets_preserved(store,tmp_path):
    release(store)
    target=tmp_path/"skills";target.mkdir()
    (target/"receipt-check").mkdir();(target/"receipt-check/owned.txt").write_text("mine")
    with pytest.raises(Conflict):Installation(target).install(store,"receipt-check","1.0.0")
    link=tmp_path/"linked";link.symlink_to(target,target_is_directory=True)
    with pytest.raises(Conflict):Installation(link)


def test_concurrent_version_submission_has_one_winner(store):
    def submit(_):
        try:LocalStore(store.root).submit(pkg.pack(tree()),"1.0.0",make_actor("author"));return "created"
        except Conflict:return "conflict"
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(submit,range(4)))
    assert results.count("created")==1
    assert store.verify()["ok"]


def test_http_identity_revocation_and_spoofing(store):
    writer=add_token(store.root,"worker","agent")
    reviewer=add_token(store.root,"reviewer")
    httpd=make_server(str(store.root),port=int(os.environ.get("SKILLSTORE_TEST_PORT","0")),require_token_for_read=True,quiet=True)
    thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
    try:
        url=f"http://127.0.0.1:{httpd.server_port}"
        with pytest.raises(PermissionDenied):RemoteStore(url).info()
        client=RemoteStore(url,writer)
        client.submit(pkg.pack(tree()),"1.0.0")
        with pytest.raises(PermissionDenied):client.review("receipt-check","1.0.0","approve")
        with pytest.raises(Exception):client._call("review",name="receipt-check",version="1.0.0",decision="approve",actor="reviewer")
        r=RemoteStore(url,reviewer).review("receipt-check","1.0.0","approve")
        assert r["review"]["authenticated"] is True
        revoke_tokens(store.root,"worker")
        with pytest.raises(PermissionDenied):client.info()
    finally:httpd.shutdown();httpd.server_close();thread.join()


@pytest.mark.parametrize("path",["../escape","/absolute","a//b","a/./b","a\\b","a\nb"])
def test_noncanonical_archive_paths_rejected(path):
    with pytest.raises(ValidationFailed):pkg._check_relpath(path)


def test_archive_duplicates_and_symlinks_rejected():
    for kind in ("duplicate","symlink"):
        data=io.BytesIO()
        with tarfile.open(fileobj=data,mode="w:gz") as tar:
            for _ in range(2 if kind=="duplicate" else 1):
                entry=tarfile.TarInfo("SKILL.md")
                if kind=="symlink":entry.type=tarfile.SYMTYPE;entry.linkname="/etc/passwd"
                else:entry.size=1
                tar.addfile(entry,io.BytesIO(b"x"))
        with pytest.raises(ValidationFailed):pkg.unpack(data.getvalue())


def test_payload_and_rename_are_durable_before_receipt(store,tmp_path,monkeypatch):
    import skillstore.install as module
    release(store);release(store,"2.0.0","New instructions")
    target=Installation(tmp_path/"skills");target.install(store,"receipt-check","1.0.0")
    events=[]
    real_sync=pkg.sync_directory;real_replace=module.os.replace;real_write=module.write_json
    def sync(path):events.append(("sync",str(path)));real_sync(path)
    def rename(source,dest):events.append(("rename",str(source),str(dest)));real_replace(source,dest)
    def metadata(path,data):events.append(("metadata",str(path)));real_write(path,data)
    monkeypatch.setattr(pkg,"sync_directory",sync)
    monkeypatch.setattr(module.os,"replace",rename)
    monkeypatch.setattr(module,"write_json",metadata)
    target.install(store,"receipt-check","2.0.0")
    journal=next(i for i,e in enumerate(events) if e==("metadata",str(target.meta/"transaction.json")))
    assert any(e[0]=="sync" and Path(e[1]).name.startswith("stage-") for e in events[:journal])
    new_tree=next(i for i,e in enumerate(events) if e[0]=="rename" and Path(e[1]).name.startswith("stage-"))
    receipt=next(i for i,e in enumerate(events) if e==("metadata",str(target.meta/"state.json")))
    assert ("sync",str(target.meta)) in events[new_tree+1:receipt]
    assert ("sync",str(target.root)) in events[new_tree+1:receipt]


def test_mcp_safety_hints_describe_effects():
    from skillstore.mcp import TOOLS
    hints={t["name"]:t["annotations"] for t in TOOLS}
    assert hints["skillstore_install"]["destructiveHint"]
    assert hints["skillstore_rollback"]["destructiveHint"]
    assert hints["skillstore_search"]["openWorldHint"]
    assert not hints["skillstore_status"]["readOnlyHint"]


def test_first_skill_and_nested_store_ancestors_are_durable(tmp_path,monkeypatch):
    synced=[]
    real=pkg.sync_directory
    def sync(path):synced.append(Path(path));real(path)
    monkeypatch.setattr(pkg,"sync_directory",sync)
    root=tmp_path/"new-parent/deeper/registry"
    store=LocalStore.init(root,["reviewer"])
    release(store)
    for expected in [tmp_path,tmp_path/"new-parent",tmp_path/"new-parent/deeper",root,root/"skills",root/"skills/receipt-check",root/"skills/receipt-check/versions"]:
        assert expected in synced,expected
    assert store.verify()["ok"]
