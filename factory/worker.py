"""`factory worker start|rm`: a bare Orca worktree, pre-trusted for Claude *and* Codex under a lock
(S1, S7), and a Yolo agent launched under `env -i` with an allowlisted environment (ADR-0002 §3)."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import pathlib
import shlex
import subprocess

from factory import orca

ALLOWED = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "TERM",
           "COLORTERM"}
AGENTS = {"claude": "claude --dangerously-skip-permissions",
          "codex": "codex --dangerously-bypass-approvals-and-sandbox"}
CLAUDE_JSON, CODEX_TOML = "~/.claude.json", "~/.codex/config.toml"


def build_env(business: str, lane: str, card: str, c0: str) -> dict[str, str]:
    """Start from {}; copy only ALLOWED + FACTORY_*; per-business dirs when business != tekscape."""
    env = {k: v for k, v in os.environ.items() if k in ALLOWED}
    venv = os.environ.get("VIRTUAL_ENV")
    if venv and "PATH" in env:  # `uv run factory` prepends its venv; the worker must not see it
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(":") if p != f"{venv}/bin")
    env.update(FACTORY_BUSINESS=business, FACTORY_LANE=lane, FACTORY_CARD=card, FACTORY_C0=c0)
    if business != "tekscape":
        b = pathlib.Path("~/.factory/biz").expanduser() / business
        env.update(GH_CONFIG_DIR=str(b / "gh"), GIT_CONFIG_GLOBAL=str(b / "gitconfig"),
                   CLAUDE_CONFIG_DIR=str(b / f"claude-{lane}"), CODEX_HOME=str(b / f"codex-{lane}"))
    return env


@contextlib.contextmanager
def _locked(path: pathlib.Path):
    with open(f"{path}.factory-lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # released when the lock file is closed
        yield


def _replace(path: pathlib.Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.factory-tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _codex_block(path: str) -> str:
    if '"' in path or "\\" in path:
        raise ValueError(f"refusing to write a TOML key for {path!r}")
    return f'\n[projects."{path}"]\ntrust_level = "trusted"\n'


def _edit(worktree: str, add: bool, claude_json=None, codex_toml=None) -> str:
    path = os.path.realpath(worktree)
    cj = pathlib.Path(claude_json or CLAUDE_JSON).expanduser()
    ct = pathlib.Path(codex_toml or CODEX_TOML).expanduser()
    with _locked(cj):
        doc = json.loads(cj.read_text()) if cj.exists() else {}
        projects = doc.setdefault("projects", {})
        if add:
            projects.setdefault(path, {})["hasTrustDialogAccepted"] = True
        else:
            projects.pop(path, None)
        _replace(cj, json.dumps(doc, indent=2))
    with _locked(ct):
        text, block = (ct.read_text() if ct.exists() else ""), _codex_block(path)
        if add and f'[projects."{path}"]' not in text:
            _replace(ct, text + block)
        elif not add and block in text:
            _replace(ct, text.replace(block, "", 1))
    return path


def trust(worktree: str, claude_json=None, codex_toml=None) -> str:
    return _edit(worktree, True, claude_json, codex_toml)


def untrust(worktree: str, claude_json=None, codex_toml=None) -> str:
    return _edit(worktree, False, claude_json, codex_toml)


def start(repo: str, card: str, agent: str, prompt: str, business: str = "tekscape",
          lane: str = "code", c0: str | None = None) -> dict:
    repo = os.path.realpath(os.path.expanduser(repo))
    c0 = c0 or subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    wt = orca.orca("worktree", "create", repo=f"path:{repo}", name=card, setup="skip",
                   no_parent=True)["worktree"]["path"]
    try:
        trust(wt)
        env = build_env(business, lane, card, c0)
        cmd = shlex.join(["env", "-i", *(f"{k}={v}" for k, v in sorted(env.items()))])
        cmd += f" {AGENTS[agent]} {shlex.quote(prompt)}"
        term = orca.orca("terminal", "create", worktree=f"path:{wt}", title=card, command=cmd)
    except Exception:
        rm(wt)
        raise
    return {"worktree": wt, "terminal": term["terminal"]["handle"]}


def rm(worktree: str) -> dict:
    path = untrust(worktree)
    orca.orca("worktree", "rm", worktree=f"path:{path}", force=True)
    return {"removed": path}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="factory worker")
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("start")
    s.add_argument("--repo", required=True), s.add_argument("--card", required=True)
    s.add_argument("--agent", choices=sorted(AGENTS), required=True)
    s.add_argument("--prompt", required=True), s.add_argument("--c0")
    s.add_argument("--business", default="tekscape"), s.add_argument("--lane", default="code")
    r = sub.add_parser("rm")
    r.add_argument("--worktree", required=True)
    for q in (s, r):
        q.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    out = (start(a.repo, a.card, a.agent, a.prompt, a.business, a.lane, a.c0)
           if a.action == "start" else rm(a.worktree))
    print(json.dumps(out) if a.json else "\n".join(f"{k}: {v}" for k, v in out.items()))
    return 0
