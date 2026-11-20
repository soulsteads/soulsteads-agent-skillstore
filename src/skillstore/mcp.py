"""Shared MCP tool definitions and the local stdio adapter."""
import json
import sys
from urllib.parse import urlsplit

from . import __version__
from . import package as pkg
from .errors import SkillstoreError, UsageError


def tool(name,description,properties,required=()):
    return {"name":"skillstore_"+name,"description":description,
        "annotations":{"readOnlyHint":name in ("search","inspect"),"destructiveHint":name in ("install","rollback","uninstall"),"idempotentHint":name not in ("propose","submit","backup_hermes"),"openWorldHint":name in ("search","inspect","preview","install","propose","submit")},"inputSchema":{
        "type":"object","properties":{k:{"type":"string","description":v} for k,v in properties.items()},
        "required":list(required),"additionalProperties":False}}


TOOLS=[
    tool("search","Discover reviewed portable skills.",{"query":"Search terms"}),
    tool("inspect","Inspect source, immutable digest, review and versions.",{"name":"Skill name","version":"Exact version; omitted lists versions"},["name"]),
    tool("preview","Inspect update diff before selective adoption; refuses local modifications.",{"name":"Skill name","version":"Exact reviewed version"},["name"]),
    tool("install","Install a reviewed skill into the configured target. Refuses active registered sessions or local changes.",{"name":"Skill name","version":"Exact version"},["name","version"]),
    tool("rollback","Restore a prior locally cached reviewed version; preserves history and refuses local changes.",{"name":"Skill name","version":"Prior installed version"},["name","version"]),
    tool("uninstall","Remove a clean Skillstore-owned skill from discovery while preserving offline recovery history.",{"name":"Skill name"},["name"]),
    tool("status","Inspect installed provenance and active session registrations.",{}),
]

CONTRIBUTION_TOOLS = [
    tool("submit", "Send one staged package to the configured registry as a pending version. Does not approve or install it.", {
        "name": "Exact skill name", "path": "Relative package directory under the configured source root",
        "version": "New version", "source_uri": "HTTPS source URL without credentials, query or fragment",
        "revision": "Immutable source revision", "changelog": "Optional summary of changes",
    }, ["name", "path", "version", "source_uri", "revision"]),
    tool("propose", "Send one staged package to the configured registry as an improvement proposal. Does not approve or install it.", {
        "name": "Exact skill name", "path": "Relative package directory under the configured source root",
        "base_version": "Exact released base version", "rationale": "Why the changes are needed",
    }, ["name", "path", "base_version", "rationale"]),
]


BACKUP_TOOL = tool("backup_hermes", "Create a private local archive of the configured Hermes profile's skills. Does not read the rest of the profile or upload anything.", {})


def available_tools(source, backup=None):
    return [*TOOLS, *(CONTRIBUTION_TOOLS if source is not None else []),
            *([BACKUP_TOOL] if backup is not None else [])]


def initialize_result(protocol_version):
    return {"protocolVersion": protocol_version, "capabilities": {"tools": {}},
            "serverInfo": {"name": "skillstore", "version": __version__}}


def tool_result(data, *, error=False):
    return {"content": [{"type": "text", "text": json.dumps(data)}], "isError": error}


def validate_arguments(tools, name, args):
    definition = next((t for t in tools if t["name"] == name), None)
    if definition is None:
        raise UsageError("unknown tool")
    schema = definition["inputSchema"]
    if (not isinstance(args, dict) or set(args) - set(schema["properties"])
            or set(schema["required"]) - set(args) or any(not isinstance(v, str) for v in args.values())):
        raise UsageError("invalid tool arguments")


def _contribute(store, op, args, source, actor):
    name = args["name"]
    if len(name) > pkg.MAX_NAME or not pkg.NAME_RE.fullmatch(name):
        raise UsageError("invalid skill name")
    if any(len(value) > 4096 for value in args.values()):
        raise UsageError("contribution arguments exceed size limit")
    if op == "propose":
        pkg.parse_version(args["base_version"])
        if not args["rationale"].strip():
            raise UsageError("a rationale is required for a proposal")
        data = source.package(args["path"], name)
        return store.propose(data, name, actor=actor, rationale=args["rationale"], base_version=args["base_version"])
    pkg.parse_version(args["version"])
    uri, revision = args["source_uri"], args["revision"]
    parsed = urlsplit(uri)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or "?" in uri or "#" in uri
            or any(c.isspace() or ord(c) < 32 for c in uri)):
        raise UsageError("source_uri must be an HTTPS URL without credentials, query or fragment")
    if not revision or any(c.isspace() or ord(c) < 32 for c in revision):
        raise UsageError("provide an immutable source revision")
    data = source.package(args["path"], name)
    return store.submit(data, args["version"], actor=actor,
                        source={"type": "source", "uri": uri, "revision": revision},
                        changelog=args.get("changelog", ""))


def call(store,target,name,args,*,source=None,actor=None,backup=None):
    validate_arguments(available_tools(source, backup), name, args)
    op=name.removeprefix("skillstore_")
    if op=="search": return store.list_skills(args.get("query"))
    if op=="inspect": return store.release(args["name"],args["version"]) if "version" in args else store.skill_info(args["name"])
    if op in ("propose", "submit"):
        return _contribute(store, op, args, source, actor)
    if op=="backup_hermes" and backup is not None: return backup.create()
    if op=="status": return target.status()
    if op in ("rollback", "uninstall"): return getattr(target,op)(**args)
    return getattr(target,op)(store,**args)


def serve(store,target,*,source=None,actor=None,backup=None):
    for line in sys.stdin:
        request=None
        try:
            request=json.loads(line)
            if not isinstance(request,dict): raise ValueError("request must be an object")
            if "id" not in request: continue
            method=request.get("method")
            if method=="initialize":
                result=initialize_result("2024-11-05")
            elif method=="ping": result={}
            elif method=="tools/list": result={"tools":available_tools(source,backup)}
            elif method=="tools/call":
                params=request.get("params",{})
                try:
                    data=call(store,target,params.get("name"),params.get("arguments",{}),source=source,actor=actor,backup=backup)
                    result=tool_result(data)
                except SkillstoreError as exc:
                    result=tool_result(exc.to_dict(), error=True)
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    data={"ok":False,"error":"io_or_data_error","message":str(exc)}
                    result=tool_result(data, error=True)
            else:
                print(json.dumps({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32601,"message":"method not found"}}),flush=True);continue
            print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}),flush=True)
        except (ValueError,TypeError,KeyError,AttributeError) as exc:
            print(json.dumps({"jsonrpc":"2.0","id":request.get("id") if isinstance(request,dict) else None,"error":{"code":-32602,"message":str(exc)}}),flush=True)
