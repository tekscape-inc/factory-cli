"""`factory judge`: the other vendor's CLI, read-only, on a detached checkout of the head SHA. Input =
card from C0 + diff + the harness's evidence.json + gate log tails; the judge never runs tests (S10-P0 f1).
Output = one strict JSON verdict bound to the head SHA, re-checked here and written to <run-dir>/judge.json."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import re
import subprocess
import sys

from factory import ghapp, schemas

SKILL = pathlib.Path(__file__).resolve().parent.parent / "skills" / "judge" / "SKILL.md"
OTHER = {"claude": "codex", "anthropic": "codex", "qwen": "codex", "codex": "claude", "openai": "claude"}
SCRUB = ["env", "-u", "SSH_AUTH_SOCK", "-u", "ANTHROPIC_API_KEY", "-u", "OPENAI_API_KEY"]
CLAUDE_RO = ["claude", "-p", "--output-format", "json", "--allowedTools", "Read,Grep,Glob",
             "--disallowedTools", "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch"]
MAX_DIFF = 200_000
STATE = {"PASS": ("success", "judge PASS"), "BLOCK": ("failure", "judge BLOCK"),
         "HUMAN": ("pending", "human gate")}


class JudgeRunError(RuntimeError):
    """The judge run itself is void (dirty checkout, moved tree); no verdict is written."""


def judge_vendor(author: str) -> str:
    """The judge is never the implementer's vendor; Qwen work is judged by Codex."""
    return OTHER.get(author, "codex")


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _block(head: str, why: str, doc: dict | None = None) -> dict:
    doc = doc or {"criteria": [], "findings": []}
    return {"verdict": "BLOCK", "sha": head, "criteria": doc["criteria"],
            "findings": doc["findings"] + [{"severity": "high", "text": why[:500]}]}


def parse_verdict(text: str, head: str, card: dict) -> dict:
    """Strict: one schema-valid object, sha == head, every criterion assessed, no PASS over a high."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        doc = json.loads(match.group(0)) if match else None
        if not isinstance(doc, dict):
            raise TypeError("no JSON object in judge output")
        schemas.validate("judge", doc)
    except (ValueError, TypeError) as exc:
        return _block(head, f"invalid judge output: {exc}")
    if doc["sha"] != head:
        return _block(head, f"verdict sha {doc['sha']} != head {head}")
    missing = sorted({c["id"] for c in card["acceptance"]["criteria"]} - {c["id"] for c in doc["criteria"]})
    if missing:
        return _block(head, f"criteria not assessed: {', '.join(missing)}", doc)
    if doc["verdict"] == "PASS" and any(f["severity"] == "high" for f in doc["findings"]):
        return _block(head, "PASS with a high finding", doc)
    if doc["verdict"] == "PASS" and not all(c["met"] for c in doc["criteria"]):
        return _block(head, "PASS with an unmet criterion", doc)
    return doc


@contextlib.contextmanager
def checkout(repo: str, sha: str):
    """`git worktree add --detach` at <repo>/../.judge/<sha>, chmod -R a-w; on exit the status must be
    clean and HEAD^{tree} unchanged, else JudgeRunError. The checkout is always removed."""
    co = os.path.join(os.path.dirname(os.path.abspath(repo)), ".judge", sha)
    _git(repo, "worktree", "add", "--detach", co, sha)
    try:
        subprocess.run(["chmod", "-R", "a-w", co], check=True)
        tree0 = _git(co, "rev-parse", "HEAD^{tree}")
        yield co
        if _git(co, "status", "--porcelain", "--ignored") or _git(co, "rev-parse", "HEAD^{tree}") != tree0:
            raise JudgeRunError(f"checkout dirty: {co}")
    finally:
        subprocess.run(["chmod", "-R", "u+w", co], check=False)
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", co], check=False,
                       capture_output=True)


def build_cmd(vendor: str, checkout_dir: str, prompt_file: str) -> list[str]:
    """argv for the read-only judge; the prompt arrives on stdin. Codex form is S10-P0's exactly."""
    if vendor == "claude":
        return SCRUB + CLAUDE_RO
    last = pathlib.Path(prompt_file)
    last = str(last.with_name(last.name.split(".")[0] + ".last.txt"))
    return SCRUB + ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-C",
                    checkout_dir, "--output-last-message", last, "-"]


def build_prompt(card: dict, diff: str, evidence: dict, tails: dict, artifacts: list, head: str) -> str:
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + f"\n[diff truncated at {MAX_DIFF} bytes; read the checkout]\n"
    logs = "\n".join(f"--- {name} (last 60 lines) ---\n{text}" for name, text in tails.items()) or "(none)"
    return (f"{SKILL.read_text()}\n\n# Head SHA\n{head}\n\n# Card (frozen at C0)\n```json\n"
            f"{json.dumps(card, indent=2)}\n```\n\n# Diff base..head\n```diff\n{diff}\n```\n\n"
            f"# evidence.json (written by the harness, not the worker)\n```json\n"
            f"{json.dumps(evidence, indent=2)}\n```\n\n# Gate log tails\n{logs}\n\n# Artifacts\n"
            f"{json.dumps(artifacts)}\n\nReply with ONE JSON object bound to sha {head}.\n")


def _answer(vendor: str, proc, prompt_file: pathlib.Path) -> str:
    if vendor == "claude":
        with contextlib.suppress(ValueError):
            return json.loads(proc.stdout).get("result", "")
        return proc.stdout
    last = prompt_file.with_name(prompt_file.name.split(".")[0] + ".last.txt")
    return last.read_text() if last.exists() else ""


def run(evidence_path: str, vendor: str | None = None, repo: str | None = None, sha: str | None = None,
        base: str | None = None, name: str = "judge") -> dict:
    """Judge one attempt; `sha`/`base`/`name` let `factory audit` re-judge the merged commit."""
    ev = json.loads(pathlib.Path(evidence_path).read_text())
    run_dir = pathlib.Path(evidence_path).resolve().parent
    repo = os.path.expanduser(repo or f"~/factory-samples/{ev['repo']['github'].split('/')[1]}")
    head, base = sha or ev["repo"]["head_sha"], base or ev["repo"]["base_sha"]
    vendor = vendor or judge_vendor((ev["attempt"].get("author") or {}).get("vendor", "qwen"))
    card = json.loads(_git(repo, "show", f"{ev['card']['c0_sha']}:.factory/cards/{ev['card']['id']}.json"))
    tails = {p.name: "\n".join(p.read_text(errors="replace").splitlines()[-60:])
             for p in sorted(run_dir.glob("**/*.log"))}
    prompt_file = run_dir / f"{name}.prompt.md"
    prompt_file.write_text(build_prompt(card, _git(repo, "diff", f"{base}..{head}"), ev, tails,
                                        ev.get("artifacts", []), head))
    with checkout(repo, head) as co, prompt_file.open() as stdin:
        proc = subprocess.run(build_cmd(vendor, co, str(prompt_file)), stdin=stdin, cwd=co,
                              capture_output=True, text=True, timeout=1800, check=False)
    (run_dir / f"{name}.stderr.txt").write_text(proc.stderr[-4000:])
    verdict = parse_verdict(_answer(vendor, proc, prompt_file), head, card)
    (run_dir / f"{name}.json").write_text(json.dumps(verdict, indent=2) + "\n")
    return {**verdict, "vendor": vendor}


def _pr_head(repo: str, pr: int) -> str:
    return subprocess.run(["gh", "pr", "view", str(pr), "-R", repo, "--json", "headRefOid", "--jq",
                           ".headRefOid"], check=True, capture_output=True, text=True).stdout.strip()


def post(judge_path: str, pr: int) -> int:
    """Post `factory/judge` as the App on the judged SHA — only if the PR head still equals it (exit 3)."""
    path = pathlib.Path(judge_path)
    doc = json.loads(path.read_text())
    schemas.validate("judge", doc)
    repo = json.loads((path.parent / "evidence.json").read_text())["repo"]["github"]
    head = _pr_head(repo, pr)
    if head != doc["sha"]:
        print(f"factory judge --post: PR #{pr} head {head} != judged {doc['sha']}; not posted",
              file=sys.stderr)
        return 3
    state, description = STATE[doc["verdict"]]
    ghapp.post_status(repo, doc["sha"], "factory/judge", state, description)
    print(f"factory/judge={state} on {repo}@{doc['sha']}")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="factory judge")
    p.add_argument("--evidence", help="<run-dir>/evidence.json to judge")
    p.add_argument("--post", metavar="JUDGE_JSON", help="post <run-dir>/judge.json as factory/judge")
    p.add_argument("--pr", type=int, help="PR number whose head must equal the judged SHA")
    p.add_argument("--vendor", choices=["codex", "claude"], help="override the vendor rule")
    p.add_argument("--repo", help="local clone (default ~/factory-samples/<name>)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.post:
        return post(a.post, a.pr) if a.pr else p.error("--post needs --pr")
    if not a.evidence:
        p.error("--evidence or --post is required")
    try:
        out = run(a.evidence, vendor=a.vendor, repo=a.repo)
    except JudgeRunError as exc:
        print(f"factory judge: {exc}", file=sys.stderr)
        return 4
    print(json.dumps(out) if a.json else f"{out['verdict']} {out['sha']} ({out['vendor']})")
    return 0 if out["verdict"] == "PASS" else 1
