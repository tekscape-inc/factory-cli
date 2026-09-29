"""The judge GitHub App: JWT → installation token (cached) → commit status, one ledger row per post.
The PEM lives only in the login Keychain (`factory.judge-app.pem`, base64); it is never printed or logged."""
from __future__ import annotations

import base64
import datetime as dt
import json
import subprocess
import time
import urllib.request

import jwt

from factory.orca import FACTORY_HOME, LEDGER

API = "https://api.github.com"
APP_JSON = FACTORY_HOME / "app.json"
KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT = "factory.judge-app.pem", "factory"
_CACHE: dict = {}


def _app() -> dict:
    return json.loads(APP_JSON.read_text())


def _pem() -> bytes:
    out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                          "-a", KEYCHAIN_ACCOUNT, "-w"], capture_output=True, check=False)
    if out.returncode:
        raise RuntimeError(f"Keychain item {KEYCHAIN_SERVICE!r} not readable (exit {out.returncode})")
    return base64.b64decode(out.stdout.strip())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):          # never replay the Authorization header to another URL
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(method: str, path: str, auth: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(API + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": auth, "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    with _OPENER.open(req, timeout=30) as resp:
        return json.loads(resp.read() or b"{}")


def app_jwt() -> str:
    """RS256 JWT for the App; iat backdated 60 s for clock drift, lifetime 600 s (GitHub's max)."""
    now = int(time.time())
    claims = {"iat": now - 60, "exp": now + 540, "iss": str(_app()["app_id"])}
    return jwt.encode(claims, _pem(), algorithm="RS256")


def installation_token() -> str:
    """Installation token, reused until 5 minutes before it expires."""
    inst = _app()["installation_id"]
    token, expires = _CACHE.get(inst, ("", 0.0))
    if expires - 300 > time.time():
        return token
    doc = _http("POST", f"/app/installations/{inst}/access_tokens", "Bearer " + app_jwt())
    _CACHE[inst] = (doc["token"], dt.datetime.fromisoformat(doc["expires_at"]).timestamp())
    return doc["token"]


def ledger(row: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), **row}) + "\n")


def post_status(repo: str, sha: str, context: str, state: str, description: str) -> dict:
    """POST /repos/<repo>/statuses/<sha> as the App, then append a `status_post` ledger row."""
    doc = _http("POST", f"/repos/{repo}/statuses/{sha}", "token " + installation_token(),
                {"state": state, "context": context, "description": description[:140]})
    ledger({"kind": "status_post", "repo": repo, "sha": sha, "context": context, "state": state,
            "creator": (doc.get("creator") or {}).get("login")})
    return doc
