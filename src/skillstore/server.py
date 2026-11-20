"""HTTP access to a store for clients that should not have the directory itself.

One endpoint, ``POST /v1/rpc`` with ``{"method": ..., "params": {...}}``,
exposes the same operations as :class:`skillstore.store.LocalStore`. The
caller's identity always comes from its bearer token, never from request
parameters, so a client holding only a submitter token cannot review.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .errors import (
    Conflict,
    IntegrityError,
    NotFound,
    NotReviewed,
    PermissionDenied,
    SkillstoreError,
    UsageError,
    ValidationFailed,
)
from .store import LocalStore, now, read_json, write_json

MAX_BODY = 40 * 1024 * 1024

# method -> (access, allowed parameter names)
METHODS = {
    "info": ("read", ()),
    "list_skills": ("read", ("query", "include_unreviewed")),
    "skill_info": ("read", ("name",)),
    "release": ("read", ("name", "version")),
    "package": ("read", ("name", "version")),
    "diff": ("read", ("name", "from_version", "to_version")),
    "list_proposals": ("read", ("skill", "status")),
    "proposal": ("read", ("proposal_id",)),
    "proposal_diff": ("read", ("proposal_id",)),
    "audit": ("read", ("limit", "subject")),
    "verify": ("read", ()),
    "submit": ("write", ("package_b64", "version", "source", "changelog")),
    "propose": ("write", ("package_b64", "skill", "rationale", "base_version", "source")),
    "review": ("write", ("name", "version", "decision", "note")),
    "withdraw": ("write", ("name", "version", "reason")),
    "decide_proposal": ("write", ("proposal_id", "decision", "note", "version", "changelog")),
}

HTTP_STATUS = {
    ValidationFailed: 422,
    Conflict: 409,
    IntegrityError: 500,
    NotReviewed: 409,
    PermissionDenied: 403,
    NotFound: 404,
    UsageError: 400,
}


# ---------------------------------------------------------------- tokens


def _tokens_path(store_root: Path) -> Path:
    return Path(store_root) / "access" / "tokens.json"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_tokens(store_root: Path) -> dict:
    path = _tokens_path(store_root)
    return read_json(path) if path.is_file() else {}


def add_token(store_root: Path, actor: str, kind: str = "human") -> str:
    """Create an access token. Only its SHA-256 is stored; the value is shown once."""
    if kind not in ("human", "agent", "service"):
        raise UsageError("kind must be human, agent or service")
    token = "sks_" + secrets.token_urlsafe(24)
    store = LocalStore(store_root)
    with store._lock():
        tokens = load_tokens(store_root)
        tokens[_hash(token)] = {"actor": actor, "kind": kind, "created_at": now()}
        write_json(_tokens_path(store_root), tokens)
        _tokens_path(store_root).chmod(0o600)
    return token


def revoke_tokens(store_root: Path, actor: str) -> int:
    store = LocalStore(store_root)
    with store._lock():
        tokens = load_tokens(store_root)
        keep = {h: t for h, t in tokens.items() if t["actor"] != actor}
        write_json(_tokens_path(store_root), keep)
    return len(tokens) - len(keep)


# ---------------------------------------------------------------- dispatch


def dispatch(store: LocalStore, method: str, params: dict, actor: dict | None):
    if method not in METHODS:
        raise UsageError(f"unknown method {method!r}")
    access, allowed = METHODS[method]
    unknown = set(params) - set(allowed)
    if unknown:
        raise UsageError(f"unknown parameters for {method}: {sorted(unknown)}")
    kwargs = dict(params)
    if "package_b64" in kwargs:
        try:
            kwargs["package_bytes"] = base64.b64decode(kwargs.pop("package_b64"), validate=True)
        except (ValueError, TypeError):
            raise UsageError("package_b64 is not valid base64")
    if access == "write":
        if actor is None:
            raise PermissionDenied(f"{method} requires an access token")
        kwargs["actor"] = actor
    try:
        result = getattr(store, method)(**kwargs)
    except TypeError as exc:
        raise UsageError(f"bad parameters for {method}: {exc}")
    if isinstance(result, bytes):
        return {"package_b64": base64.b64encode(result).decode()}
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = f"skillstore/{__version__}"
    store: LocalStore
    require_token_for_read = False
    quiet = False

    def log_message(self, fmt, *args):  # noqa: D401 - stdlib signature
        if not self.quiet:
            sys.stderr.write("%s %s\n" % (now(), fmt % args))

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _actor(self) -> dict | None:
        header = self.headers.get("Authorization", "")
        if not header:
            return None
        if not header.startswith("Bearer "):
            raise PermissionDenied("unsupported Authorization scheme")
        entry = load_tokens(self.store.root).get(_hash(header[7:].strip()))
        if not entry:
            raise PermissionDenied("unknown or revoked access token")
        return {"actor": entry["actor"], "kind": entry["kind"], "authenticated": True}

    def do_GET(self):
        if self.path == "/v1/health":
            self._send(200, {"ok": True, "service": "skillstore", "version": __version__, "store_id": self.store.config()["store_id"]})
        else:
            self._send(404, {"ok": False, "error": "not_found", "message": "use POST /v1/rpc"})

    def do_POST(self):
        try:
            if self.path != "/v1/rpc":
                raise NotFound("use POST /v1/rpc")
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                raise UsageError("request body missing or too large")
            try:
                request = json.loads(self.rfile.read(length))
                method, params = request["method"], request.get("params") or {}
                if not isinstance(params, dict):
                    raise TypeError
            except (ValueError, KeyError, TypeError):
                raise UsageError('body must be JSON: {"method": "...", "params": {...}}')
            actor = self._actor()
            if actor is None and self.require_token_for_read:
                raise PermissionDenied("this store requires an access token")
            self._send(200, {"ok": True, "result": dispatch(self.store, method, params, actor)})
        except SkillstoreError as exc:
            self._send(HTTP_STATUS.get(type(exc), 500), exc.to_dict())
        except Exception as exc:  # never leak a traceback to clients
            sys.stderr.write(f"{now()} internal error: {exc!r}\n")
            self._send(500, {"ok": False, "error": "error", "message": "internal server error"})


def make_server(store_root: str, host: str = "127.0.0.1", port: int = 0, require_token_for_read: bool = False, quiet: bool = False):
    handler = type("BoundHandler", (Handler,), {"store": LocalStore(store_root), "require_token_for_read": require_token_for_read, "quiet": quiet})
    return ThreadingHTTPServer((host, port), handler)


def serve(store_root: str, host: str, port: int, require_token_for_read: bool = False) -> None:
    httpd = make_server(store_root, host, port, require_token_for_read)
    sys.stderr.write(f"skillstore serving {Path(store_root).resolve()} on http://{host}:{httpd.server_address[1]}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
