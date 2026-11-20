"""Line-delimited stdio MCP. No publication/review tool is exposed to agents."""
import json
import sys

from . import __version__
from .errors import SkillstoreError, UsageError


def tool(name,description,properties,required=()):
    return {"name":"skillstore_"+name,"description":description,
        "annotations":{"readOnlyHint":name in ("search","inspect"),"destructiveHint":name in ("install","rollback","uninstall"),"idempotentHint":True,"openWorldHint":name in ("search","inspect","preview","install")},"inputSchema":{
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


def call(store,target,name,args):
    definition=next((t for t in TOOLS if t["name"]==name),None)
    if not definition: raise UsageError("unknown tool")
    schema=definition["inputSchema"]
    if not isinstance(args,dict) or set(args)-set(schema["properties"]) or set(schema["required"])-set(args) or any(not isinstance(v,str) for v in args.values()):
        raise UsageError("invalid tool arguments")
    op=name.removeprefix("skillstore_")
    if op=="search": return store.list_skills(args.get("query"))
    if op=="inspect": return store.release(args["name"],args["version"]) if "version" in args else store.skill_info(args["name"])
    if op=="status": return target.status()
    if op in ("rollback", "uninstall"): return getattr(target,op)(**args)
    return getattr(target,op)(store,**args)


def serve(store,target):
    for line in sys.stdin:
        request=None
        try:
            request=json.loads(line)
            if not isinstance(request,dict): raise ValueError("request must be an object")
            if "id" not in request: continue
            method=request.get("method")
            if method=="initialize":
                result={"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"skillstore","version":__version__}}
            elif method=="ping": result={}
            elif method=="tools/list": result={"tools":TOOLS}
            elif method=="tools/call":
                params=request.get("params",{})
                try:
                    data=call(store,target,params.get("name"),params.get("arguments",{}))
                    result={"content":[{"type":"text","text":json.dumps(data)}],"isError":False}
                except SkillstoreError as exc:
                    result={"content":[{"type":"text","text":json.dumps(exc.to_dict())}],"isError":True}
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    data={"ok":False,"error":"io_or_data_error","message":str(exc)}
                    result={"content":[{"type":"text","text":json.dumps(data)}],"isError":True}
            else:
                print(json.dumps({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32601,"message":"method not found"}}),flush=True);continue
            print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}),flush=True)
        except (ValueError,TypeError,KeyError,AttributeError) as exc:
            print(json.dumps({"jsonrpc":"2.0","id":request.get("id") if isinstance(request,dict) else None,"error":{"code":-32602,"message":str(exc)}}),flush=True)
