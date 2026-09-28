"""`factory audit`: which PASSes get a second, blind judge after merge, and running it (plan v3 §5.5).
Sampled iff sha256(card_id + c0_sha) % 10 == 0 (fixed at freeze), or the repo has < 10 prior PASSes.
The auditor is the frontier vendor that was not the primary judge. A miss (audit BLOCK with a high
finding) is appended to ~/.factory/incidents.jsonl for John to confirm (Telegram relay is P4)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import time

from factory import judge
from factory.orca import FACTORY_HOME

INCIDENTS = FACTORY_HOME / "incidents.jsonl"
FIRST_N = 10


def runs_root(business: str = "tekscape") -> pathlib.Path:
    return FACTORY_HOME / "biz" / business / "runs"


def prior_passes(repo: str, runs: pathlib.Path) -> int:
    """PASS verdicts recorded under <runs>/<repo>/<card>/<attempt>/judge.json."""
    count = 0
    for path in (pathlib.Path(runs) / repo).glob("*/*/judge.json"):
        try:
            count += json.loads(path.read_text()).get("verdict") == "PASS"
        except (OSError, ValueError):
            continue
    return count


def sampled(card_id: str, c0: str, repo: str | None = None, runs: pathlib.Path | None = None) -> bool:
    if repo is not None and prior_passes(repo, runs or runs_root()) < FIRST_N:
        return True
    return int(hashlib.sha256((card_id + c0).encode()).hexdigest(), 16) % 10 == 0


def auditor_for(judge: str) -> str:
    return {"codex": "claude", "claude": "codex"}[judge]


def run(evidence_path: str, merged_sha: str, judge_vendor: str | None = None,
        repo: str | None = None) -> dict:
    """Re-judge the **merged** commit (diff merged^..merged) blind, as the other vendor; write audit.json."""
    ev = json.loads(pathlib.Path(evidence_path).read_text())
    primary = judge_vendor or judge.judge_vendor((ev["attempt"].get("author") or {}).get("vendor", "qwen"))
    auditor = auditor_for(primary)
    out = judge.run(evidence_path, vendor=auditor, repo=repo, sha=merged_sha, base=f"{merged_sha}^",
                    name="audit")
    miss = out["verdict"] == "BLOCK" and any(f["severity"] == "high" for f in out["findings"])
    if miss:
        INCIDENTS.parent.mkdir(parents=True, exist_ok=True)
        with INCIDENTS.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "kind": "audit_miss", "repo": ev["repo"]["github"],
                                 "card": ev["card"]["id"], "c0_sha": ev["card"]["c0_sha"],
                                 "merged_sha": merged_sha, "judge": primary, "auditor": auditor,
                                 "findings": out["findings"], "confirmed": None}) + "\n")
    return {**out, "auditor": auditor, "miss": miss}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="factory audit")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sampled", help="print true/false: is this card audited after merge?")
    s.add_argument("--card", required=True)
    s.add_argument("--c0", required=True)
    s.add_argument("--repo", required=True, help="repo name, e.g. sample-cli-py")
    s.add_argument("--business", default="tekscape")
    r = sub.add_parser("run", help="blind second judge on the merged SHA → audit.json")
    r.add_argument("--evidence", required=True)
    r.add_argument("--sha", required=True, help="merged (squash) commit on the base branch")
    r.add_argument("--judge", choices=["codex", "claude"], help="primary judge vendor (default: rule)")
    r.add_argument("--repo-path", help="local clone (default ~/factory-samples/<name>)")
    a = p.parse_args(argv)
    if a.cmd == "sampled":
        print(json.dumps(sampled(a.card, a.c0, repo=a.repo, runs=runs_root(a.business))))
        return 0
    try:
        out = run(a.evidence, a.sha, judge_vendor=a.judge,
                  repo=os.path.expanduser(a.repo_path) if a.repo_path else None)
    except judge.JudgeRunError as exc:
        print(f"factory audit: {exc}")
        return 4
    print(json.dumps(out))
    return 1 if out["miss"] else 0
