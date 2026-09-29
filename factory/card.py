"""`factory card --freeze`: commit card + acceptance tests as C0 on `factory/<id>`, push, post `factory/card`.
C0 is built with plumbing on a private index, so the caller's checkout and branch are never touched."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

from factory import ghapp, schemas


def canonical(card: dict) -> bytes:
    """The bytes committed at C0: the card **without** `frozen` (the stamp cannot hash itself)."""
    body = {k: v for k, v in card.items() if k != "frozen"}
    return (json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode()


def card_sha256(card: dict) -> str:
    """== sha256 of the C0 blob, which is what `gate.load_card` hashes."""
    return hashlib.sha256(canonical(card)).hexdigest()


def _git(repo: str, *args: str, env: dict | None = None, data: bytes | None = None) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, input=data,
                          env=None if env is None else {**os.environ, **env}).stdout.decode().strip()


def freeze(repo: str, card_id: str, base: str = "origin/main", push: bool = True) -> dict:
    root = pathlib.Path(repo)
    rel = f".factory/cards/{card_id}.json"
    card = json.loads((root / rel).read_text())
    if "frozen" in card:
        raise SystemExit(f"{card_id} is already frozen at {card['frozen']['c0_sha']}; write a .v2 card")
    schemas.validate("card", card)
    paths = [rel]
    for t in card["acceptance"]["tests"]:
        if hashlib.sha256((root / t["path"]).read_bytes()).hexdigest() != t["sha256"]:
            raise SystemExit(f"acceptance file {t['path']} does not match its sha256 in the card")
        paths.append(t["path"])
    base_sha = _git(repo, "rev-parse", "--verify", base + "^{commit}")
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        _git(repo, "read-tree", base_sha, env=env)
        for p in paths:
            blob = (_git(repo, "hash-object", "-w", "--stdin", data=canonical(card)) if p == rel
                    else _git(repo, "hash-object", "-w", "--", p))
            _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{p}", env=env)
        tree = _git(repo, "write-tree", env=env)
    c0 = _git(repo, "commit-tree", tree, "-p", base_sha, "-m", f"card({card_id}): freeze C0")
    branch = f"factory/{card_id}"
    _git(repo, "branch", branch, c0)                     # fails if the branch exists: cards are immutable
    if push:
        _git(repo, "push", "origin", f"{branch}:refs/heads/{branch}")
    digest = card_sha256(card)
    ghapp.post_status(card["repo"], c0, "factory/card", "success", f"{card_id} frozen sha256:{digest[:12]}")
    card["frozen"] = {"c0_sha": c0, "card_sha256": digest}
    (root / rel).write_text(json.dumps(card, indent=2) + "\n")   # local stamp; C0 holds the unstamped card
    return {"card": card_id, "branch": branch, "base_sha": base_sha, "c0_sha": c0,
            "card_sha256": digest, "status": "success"}


def restamp(repo: str, card_id: str, c0: str) -> int | None:  # C0' is stamped only if its bytes match
    rel = f".factory/cards/{card_id}.json"
    digest = json.loads(pathlib.Path(repo, rel).read_text()).get("frozen", {}).get("card_sha256")
    raw = subprocess.run(["git", "-C", repo, "show", f"{c0}:{rel}"], capture_output=True, check=False).stdout
    if hashlib.sha256(raw).hexdigest() != digest:
        return print(f"{card_id}: card at {c0} != frozen sha256; not restamped", file=sys.stderr) or 2
    for t in json.loads(raw)["acceptance"]["tests"]:  # frozen acceptance bytes must survive the rebase too
        blob = subprocess.run(["git", "-C", repo, "show", f"{c0}:{t['path']}"], capture_output=True, check=False).stdout
        if hashlib.sha256(blob).hexdigest() != t["sha256"]:
            return print(f"{card_id}: {t['path']} at {c0} != card sha256; not restamped", file=sys.stderr) or 2
    ghapp.post_status(json.loads(raw)["repo"], c0, "factory/card", "success", f"{card_id} restamped {digest[:12]}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="factory card")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--freeze", action="store_true")
    mode.add_argument("--validate", metavar="CARD_JSON", help="schema-check a card file; exit 0/1")
    mode.add_argument("--restamp", metavar="C0_SHA", help="restamp a rebased C0")
    p.add_argument("--repo")
    p.add_argument("--card")
    p.add_argument("--base", default="origin/main")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.validate:
        try:
            schemas.validate("card", json.loads(pathlib.Path(a.validate).read_text()))
        except (schemas.SchemaError, ValueError, OSError) as exc:
            print(f"{a.validate}: {exc}", file=sys.stderr)
            return 1
        print(f"{a.validate}: valid card")
        return 0
    if not (a.repo and a.card):
        p.error("--freeze needs --repo and --card")
    if a.restamp:
        return restamp(os.path.expanduser(a.repo), a.card, a.restamp) or 0
    out = freeze(os.path.expanduser(a.repo), a.card, base=a.base)
    print(json.dumps(out) if a.json else f"{out['card']}: C0 {out['c0_sha']} on {out['branch']}")
    return 0
