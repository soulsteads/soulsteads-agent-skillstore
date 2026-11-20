"""Client for a store served by ``skillstore serve``; mirrors ``LocalStore``."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
import urllib.parse
import ipaddress

from .errors import ERRORS_BY_CODE, SkillstoreError
from .store import LocalStore


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a registry credential to a redirected host.


class RemoteStore:
    def __init__(self, url: str, token: str | None = None, timeout: float = 30.0):
        self.url = url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SkillstoreError("registry URLs must not contain credentials, queries or fragments")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise SkillstoreError("registry URL must use http or https")
        try: local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError: local = parsed.hostname == "localhost"
        if token and parsed.scheme != "https" and not local:
            raise SkillstoreError("authenticated non-loopback registries require HTTPS")
        self.location = self.url
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url + "/v1/rpc", data=json.dumps({"method": method, "params": params}).encode(), headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except ValueError:
                raise SkillstoreError(f"store at {self.url} returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SkillstoreError(f"cannot reach store at {self.url}: {exc}")
        if not payload.get("ok"):
            cls = ERRORS_BY_CODE.get(payload.get("error"), SkillstoreError)
            details = {k: v for k, v in payload.items() if k not in ("ok", "error", "message")}
            raise cls(payload.get("message", "store error"), **details)
        return payload["result"]

    # The ``actor`` arguments exist for signature parity with LocalStore and are
    # ignored: the server derives identity from the access token.

    def info(self):
        return self._call("info")

    def list_skills(self, query=None, include_unreviewed=False):
        return self._call("list_skills", query=query, include_unreviewed=include_unreviewed)

    def skill_info(self, name):
        return self._call("skill_info", name=name)

    def release(self, name, version=None):
        return self._call("release", name=name, version=version)

    def package(self, name, version) -> bytes:
        return base64.b64decode(self._call("package", name=name, version=version)["package_b64"])

    def diff(self, name, from_version, to_version):
        return self._call("diff", name=name, from_version=from_version, to_version=to_version)

    def submit(self, package_bytes, version, actor=None, source=None, changelog=""):
        return self._call("submit", package_b64=base64.b64encode(package_bytes).decode(), version=version, source=source, changelog=changelog)

    def review(self, name, version, decision, actor=None, note=""):
        return self._call("review", name=name, version=version, decision=decision, note=note)

    def withdraw(self, name, version, actor=None, reason=""):
        return self._call("withdraw", name=name, version=version, reason=reason)

    def propose(self, package_bytes, skill, actor=None, rationale="", base_version=None, source=None):
        return self._call(
            "propose",
            package_b64=base64.b64encode(package_bytes).decode(),
            skill=skill,
            rationale=rationale,
            base_version=base_version,
            source=source,
        )

    def list_proposals(self, skill=None, status=None):
        return self._call("list_proposals", skill=skill, status=status)

    def proposal(self, proposal_id):
        return self._call("proposal", proposal_id=proposal_id)

    def proposal_diff(self, proposal_id):
        return self._call("proposal_diff", proposal_id=proposal_id)

    def decide_proposal(self, proposal_id, decision, actor=None, note="", version=None, changelog=None):
        return self._call("decide_proposal", proposal_id=proposal_id, decision=decision, note=note, version=version, changelog=changelog)

    def audit(self, limit=50, subject=None):
        return self._call("audit", limit=limit, subject=subject)

    def verify(self):
        return self._call("verify")


def open_store(spec: str | None = None, token: str | None = None):
    """Open a store from a directory path or an ``http(s)://`` URL."""
    spec = spec or os.environ.get("SKILLSTORE_STORE")
    if not spec:
        from .errors import UsageError

        raise UsageError("no store given: pass --store PATH|URL or set SKILLSTORE_STORE")
    if spec.startswith(("http://", "https://")):
        return RemoteStore(spec, token or os.environ.get("SKILLSTORE_TOKEN"))
    return LocalStore(spec)
