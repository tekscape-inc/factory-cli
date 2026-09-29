"""`factory gate`: the deterministic gate runner (plan v3 §3.1 step 8, §5.1, §5.2, §5.5).

Everything here reads the card and acceptance tests from C0 (`git show`), never from the worktree,
and the manifest from the base SHA. It never posts `factory/judge` (that is `factory judge`)."""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import fnmatch
import hashlib
import importlib.metadata
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import yaml

from factory import repos, schemas
from factory.doctor import junit_counts, run_command

PROTECTED_BUILTIN = (
    ".factory/**", "factory.yaml", ".github/**", "CODEOWNERS", ".github/CODEOWNERS",
    "docs/CODEOWNERS", "orca.yaml", "AGENTS.md", "CLAUDE.md",
    # test configs
    "conftest.py", "**/conftest.py", "pytest.ini", "tox.ini", "jest.config.*", "vitest.config.*",
    "playwright.config.*", ".mocharc*",
)
LOCKFILES = ("uv.lock", "poetry.lock", "Pipfile.lock", "requirements*.txt", "package-lock.json",
             "pnpm-lock.yaml", "yarn.lock", "go.sum", "**/go.sum")
REJECT_MARKERS = re.compile(r"\bt\.Skip(Now)?\(|\.only\(")
FLAG_MARKERS = re.compile(r"@pytest\.mark\.(skip|xfail)|pytest\.skip\(|\bxit\(|\bxdescribe\(|"
                          r"\.skip\(|@unittest\.skip|t\.SkipNow\(")


def git(repo, *args: str, text: bool = True):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=text).stdout


def match(path: str, patterns) -> bool:
    """fnmatch where `*` spans `/`; `**/x` also matches a top-level `x`; `dir/` matches a prefix."""
    for pat in patterns:
        if fnmatch.fnmatchcase(path, pat) or (pat.startswith("**/") and
                                              fnmatch.fnmatchcase(path, pat[3:])):
            return True
        if pat.endswith("/") and path.startswith(pat):
            return True
    return False


def load_card(repo, c0: str, card_id: str) -> tuple[dict, str]:
    """The card as frozen in C0 and the sha256 of its exact bytes. The worktree copy is ignored."""
    blob = git(repo, "show", f"{c0}:.factory/cards/{card_id}.json", text=False)
    return json.loads(blob), hashlib.sha256(blob).hexdigest()


def restore_acceptance(repo, c0: str, card: dict) -> list[str]:
    """Overwrite each acceptance test in the worktree with its C0 bytes; return restored paths."""
    restored = []
    for t in card["acceptance"]["tests"]:
        dest = pathlib.Path(repo) / t["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(git(repo, "show", f"{c0}:{t['path']}", text=False))
        restored.append(t["path"])
    return restored


@dataclasses.dataclass
class GateResult:
    name: str
    status: str  # pass | fail | absent
    source: str = "builtin"
    command: str | None = None
    exit_code: int | None = None
    tests: dict | None = None
    duration_ms: int = 0
    log_path: str | None = None
    log_sha256: str | None = None

    def to_json(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items()
                if v is not None or k in ("command", "exit_code", "tests")}


@dataclasses.dataclass
class ScopeResult:
    result: str = "pass"  # pass | fail | tamper
    reasons: list = dataclasses.field(default_factory=list)
    flagged: list = dataclasses.field(default_factory=list)
    human_gate_reasons: list = dataclasses.field(default_factory=list)
    files_changed: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    budget: dict = dataclasses.field(default_factory=dict)
    outside_writable: list = dataclasses.field(default_factory=list)
    protected_touched: list = dataclasses.field(default_factory=list)
    new_deps: list = dataclasses.field(default_factory=list)
    acceptance_restored: bool = False
    acceptance_modified_by_worker: list = dataclasses.field(default_factory=list)

    def fail(self, reason: str, tamper: bool = False) -> None:
        self.reasons.append(reason)
        if tamper or self.result != "tamper":
            self.result = "tamper" if tamper else "fail"

    def to_json(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items()
                if k not in ("result", "reasons", "flagged")}


def _added_lines(repo, base: str, head: str) -> list[tuple[str, str]]:
    out, path = [], None
    for line in git(repo, "diff", "--no-renames", "-U0", base, head).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("+") and path:
            out.append((path, line[1:]))
    return out


def scope_check(repo, base: str, head: str, card: dict, manifest: dict) -> ScopeResult:
    """Tamper, budget, writable scope and skip markers for the worker's commits (`base` is C0)."""
    r = ScopeResult(budget=dict(card["diff_budget"]))
    files = []
    for row in git(repo, "diff", "--numstat", "--no-renames", base, head).splitlines():
        add, dele, path = row.split("\t", 2)
        files.append(path)
        r.lines_added += int(add) if add != "-" else 0
        r.lines_deleted += int(dele) if dele != "-" else 0
    r.files_changed = len(files)
    paths = manifest.get("paths", {})
    hard = [*PROTECTED_BUILTIN, *paths.get("protected", [])]
    protected, writable = [*hard, *paths.get("tests", [])], card["scope"]["writable"]
    if char := card.get("kind") == "characterization":  # inverted: tests writable, src protected; tests win over a broad src ('**')
        protected, writable = [*hard, *paths.get("src", [])], paths.get("tests", [])
    deps_ok = bool(card.get("deps", {}).get("allowed_new"))
    acceptance = {t["path"] for t in card["acceptance"]["tests"]}
    required_for = manifest.get("owner_approval", {}).get("required_for", [])
    for f in files:
        if f in acceptance:
            r.acceptance_modified_by_worker.append(f)
        if match(f, LOCKFILES):
            if not deps_ok:
                r.protected_touched.append(f)
            elif "new_dependency" in required_for:
                r.human_gate_reasons.append("new_dependency")
        elif match(f, protected) and not (char and match(f, writable) and not match(f, hard)):
            r.protected_touched.append(f)
        if not match(f, writable):
            r.outside_writable.append(f)
        for p in [p for p in required_for if "/" in p or "*" in p or "." in p]:
            r.human_gate_reasons += [p] if match(f, [p]) and p not in r.human_gate_reasons else []
    if r.acceptance_modified_by_worker:
        r.fail("acceptance_modified", tamper=True)
    if r.protected_touched:
        r.fail("protected_path", tamper=True)
    for path, text in _added_lines(repo, base, head):
        if REJECT_MARKERS.search(text):
            r.fail(f"skip_marker:{path}", tamper=True)
        elif FLAG_MARKERS.search(text):
            r.flagged.append(f"{path}: {text.strip()}")
    if r.outside_writable:
        r.fail("outside_writable")
    if (r.files_changed > r.budget["files"]
            or r.lines_added + r.lines_deleted > r.budget["lines"]):
        r.fail("over_budget")
    return r


def secrets_check(repo, base: str, head: str) -> GateResult:
    """gitleaks on exactly the commits in base..head (8.30: `gitleaks git`; `detect` is deprecated).
    Worker-controlled suppression is ignored: inline `gitleaks:allow` is disabled, and the repo's own
    .gitleaks.toml / .gitleaksignore are bypassed by pointing config/ignore at empty trusted paths."""
    empty = pathlib.Path(tempfile.mkdtemp(prefix="factory-gl-"))
    (empty / "gitleaks.toml").write_text('[extend]\nuseDefault = true\n')
    argv = ["gitleaks", "git", "--log-opts", f"{base}..{head}", "--no-banner", "--redact",
            "--ignore-gitleaks-allow", "--config", str(empty / "gitleaks.toml"),
            "--gitleaks-ignore-path", str(empty), "--exit-code", "1", str(repo)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")}
    p = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    shutil.rmtree(empty, ignore_errors=True)
    return GateResult("secrets", "pass" if p.returncode == 0 else "fail",
                      command=" ".join(argv[:-1]), exit_code=p.returncode)


# ---- P1-T8: manifest gates, acceptance, evidence.json, CI statuses, push + PR ----------------

MANIFEST_GATES, LATE_GATES = ("setup", "build", "lint", "typecheck", "test"), ("boot", "e2e")


class GateError(RuntimeError):
    """The runner cannot proceed (e.g. push on a non-pass result, CI without a token)."""


@dataclasses.dataclass
class Evidence:
    doc: dict  # schema/evidence.schema.json
    reasons: list
    flagged: list
    human_gate: bool
    path: pathlib.Path
    sha256: str
    card: dict
    base_branch: str


def _call(argv: list[str], cwd=None) -> str:
    """The only door to `gh` and `git push` (tests replace it). Never used for factory/judge."""
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(name: str, cmd: dict, source: str, repo: pathlib.Path, run_dir: pathlib.Path) -> GateResult:
    """doctor.run_command (timeout, env, report + min_tests) with output teed to <run_dir>/<name>.log."""
    log = run_dir / f"{name}.log"
    wrapped = {**cmd, "run": f"( {cmd['run']}\n) > {shlex.quote(str(log))} 2>&1"}
    r = run_command(name, wrapped, repo)
    report = repo / cmd["report"] if cmd.get("report") else None
    tests = junit_counts(report) if report and report.exists() else None
    return GateResult(name, r["status"], source, cmd["run"], r["exit"], tests,
                      int(r["duration_s"] * 1000), str(log),
                      hashlib.sha256(log.read_bytes()).hexdigest() if log.exists() else None)


ARTIFACT_KINDS = {".png": "screenshot", ".zip": "trace", ".webm": "video"}
ARTIFACT_MAX_BYTES = 50 * 1024 * 1024  # larger files are listed (size, skipped) but not copied


def collect_artifacts(repo, run_dir, started_at: float, manifest: dict) -> list[dict]:
    """Playwright files under test-results/ and every command `report` newer than `started_at`, copied to
    <run_dir>/artifacts/<rel> and hashed. Regular files inside the repo only: symlinks are never followed."""
    repo, real = pathlib.Path(repo), os.path.realpath(repo)
    found = {repo / c["report"]: "junit" for c in manifest.get("commands", {}).values() if c and c.get("report")}
    for top, dirs, names in os.walk(repo / "test-results"):  # followlinks=False
        found.update({pathlib.Path(top, n): ARTIFACT_KINDS[pathlib.Path(n).suffix] for n in sorted(names)
                      if pathlib.Path(n).suffix in ARTIFACT_KINDS})
        dirs.sort()
    out = []
    for path, kind in found.items():
        if not os.path.realpath(path).startswith(real + os.sep):
            continue
        try:  # open without following links, then check/copy from the fd: no lstat→copy swap window
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        with os.fdopen(fd, "rb") as src:
            st = os.fstat(src.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_mtime < started_at:
                continue
            rel = path.relative_to(repo).as_posix()
            row = {"kind": kind, "path": rel}
            if st.st_size > ARTIFACT_MAX_BYTES:
                row.update(size=st.st_size, skipped="oversize")
            else:
                data = src.read()
                dest = pathlib.Path(run_dir) / "artifacts" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                row["sha256"] = hashlib.sha256(data).hexdigest()
        if rel.startswith("test-results/") and rel.count("/") > 1:
            row["journey"] = rel.split("/")[1]
        out.append(row)
    return out


def _base_executed(repo: pathlib.Path, base: str, cmd: dict) -> int | None:
    """Tests executed by the base SHA's own test command, in a throwaway detached worktree."""
    wt = pathlib.Path(tempfile.mkdtemp(prefix="factory-gate-base-")) / "wt"
    git(repo, "worktree", "add", "-q", "--detach", str(wt), base)
    try:
        return run_command("test", cmd, wt)["executed"]
    finally:
        git(repo, "worktree", "remove", "--force", str(wt))


def run_gate(repo, card_id: str, c0: str, attempt: int = 1, ci: bool = False,
             runs_root=None) -> Evidence:
    """Plan v3 §3.1 step 8: restore acceptance from C0 → secrets → scope → base-SHA manifest
    commands → card success command → boot/e2e; write evidence.json; in --ci post 3 statuses."""
    repo = pathlib.Path(repo).resolve()
    started, t0 = _now(), time.time()
    head = git(repo, "rev-parse", "HEAD").strip()
    base = git(repo, "rev-parse", f"{c0}^").strip()
    card, card_sha = load_card(repo, c0, card_id)
    try:
        raw = git(repo, "show", f"{base}:factory.yaml", text=False)  # base manifest, never head's
    except subprocess.CalledProcessError:   # eng-R3: owner-merge repos keep theirs in the overlay
        if not (ov := repos.overlay_for(repo)):
            raise
        raw = ov.read_bytes()
    manifest = yaml.safe_load(raw)
    gh = manifest["repo"]["github"]
    root = runs_root or f"~/.factory/biz/{manifest['repo']['business']}/runs"
    run_dir = pathlib.Path(root).expanduser() / gh.split("/")[1] / card_id / str(attempt)
    run_dir.mkdir(parents=True, exist_ok=True)

    restored = restore_acceptance(repo, c0, card)
    gates = [secrets_check(repo, base, head)]
    scope = scope_check(repo, c0, head, card, manifest)
    scope.acceptance_restored = bool(restored)
    scope.human_gate_reasons += ["risk_high"] if card["risk_class"] == "high" else []
    if ci and git(repo, "diff", "--name-only", base, head, "--", ".github").strip():
        scope.fail("workflow_changed_vs_base", tamper=True)  # Review focus 1
    gates.append(GateResult("scope", "pass" if scope.result == "pass" else "fail"))
    cmds = manifest["commands"]
    plan = [(n, cmds.get(n), "manifest") for n in MANIFEST_GATES]
    plan += [("acceptance", card["success"], "card")]
    plan += [(n, cmds.get(n), "manifest") for n in LATE_GATES]
    for name, cmd, source in plan:
        gates.append(_run(name, cmd, source, repo, run_dir) if cmd
                     else GateResult(name, "absent", source))
    artifacts = collect_artifacts(repo, run_dir, t0, manifest)

    reasons = [*scope.reasons] + (["secrets"] if gates[0].status != "pass" else [])
    required = {g["name"]: g["required"] for g in manifest.get("gates", [])}
    for g in gates[2:]:
        if g.status == "fail" or (g.status == "absent" and required.get(g.name, g.name == "acceptance")):
            reasons.append(f"gate_{g.status}:{g.name}")
    test = next(g for g in gates if g.name == "test")
    if test.tests is not None:
        floor = _base_executed(repo, base, cmds["test"])
        if floor is not None and test.tests["executed"] < floor:
            reasons.append("tests_below_base")
    tamper = scope.result == "tamper" or "tests_below_base" in reasons
    result = "tamper" if tamper else "fail" if reasons else "pass"

    try:
        version = importlib.metadata.version("factory-standard")
    except importlib.metadata.PackageNotFoundError:
        version = "0.0.0"
    doc = {"schema_version": 1,
           "card": {"id": card_id, "c0_sha": git(repo, "rev-parse", c0).strip(),
                    "card_sha256": card_sha},
           "repo": {"github": gh, "base_sha": base, "head_sha": head,
                    "tree_sha": git(repo, "rev-parse", f"{head}^{{tree}}").strip()},
           "attempt": {"n": attempt, "lane": card["lane"], "started_at": started,
                       "finished_at": _now()},
           "runner": {"factory_version": version, "host": "gha" if ci else "mac-local",
                      "manifest_sha256": hashlib.sha256(raw).hexdigest()},
           "gates": [g.to_json() for g in gates], "scope": scope.to_json(), "artifacts": artifacts,
           "judge": None, "result": result}
    schemas.validate("evidence", doc)
    path = run_dir / "evidence.json"
    path.write_text(json.dumps(doc, indent=2) + "\n")
    ev = Evidence(doc, reasons, scope.flagged, bool(scope.human_gate_reasons),
                  path, hashlib.sha256(path.read_bytes()).hexdigest(), card,
                  manifest["repo"].get("base_branch", "main"))
    if ci:
        post_ci_statuses(ev, repo)
    return ev


def post_ci_statuses(ev: Evidence, repo) -> None:
    """Exactly factory/secrets, factory/scope, factory/gates on head_sha. Never factory/judge."""
    if not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
        raise GateError("--ci needs GITHUB_TOKEN")
    gates_ok = not any(r.startswith("gate_") or r == "tests_below_base" for r in ev.reasons)
    oks = {"secrets": ev.doc["gates"][0]["status"] == "pass",
           "scope": ev.doc["gates"][1]["status"] == "pass", "gates": gates_ok}
    for name, ok in oks.items():
        _call(["gh", "api", "-X", "POST",
               f"repos/{ev.doc['repo']['github']}/statuses/{ev.doc['repo']['head_sha']}",
               "-f", f"state={'success' if ok else 'failure'}", "-f", f"context=factory/{name}",
               "-f", f"description=result={ev.doc['result']} evidence {ev.sha256[:12]}"], cwd=repo)


def push(ev: Evidence, repo) -> str:
    """Push the gated head SHA to factory/<card>, open the PR, post ONE evidence comment, then
    print `HUMAN_GATE <url> reason=<csv>` or `GATED <url>`. Arming is scripts/lib/arm.sh only (§I-A2)."""
    d = ev.doc
    if d["result"] != "pass":
        raise GateError(f"refusing to push: result={d['result']} {ev.reasons}")
    branch = f"factory/{d['card']['id']}"
    _call(["git", "push", "origin", f"{d['repo']['head_sha']}:refs/heads/{branch}"], cwd=repo)
    rows = [f"| {g['name']} | {g['status']} | {g.get('exit_code')} | {g.get('tests') or ''} |"
            for g in d["gates"]]
    summary, body = ev.path.with_name("summary.md"), ev.path.with_name("pr-body.md")
    summary.write_text("\n".join([
        f"### factory gate: `{d['result']}`", "", f"evidence sha256: `{ev.sha256}`",
        (f"head `{d['repo']['head_sha']}` · base `{d['repo']['base_sha']}` · "
         f"C0 `{d['card']['c0_sha']}`"), f"artifacts: {len(d['artifacts'])}", "", "| gate | status | exit | tests |", "|---|---|---|---|",
        *rows, *([""] + [f"flagged: {f}" for f in ev.flagged] if ev.flagged else [])]) + "\n")
    body.write_text(f"Card `{d['card']['id']}` frozen at C0 `{d['card']['c0_sha']}`.\n\n"
                    f"{ev.card['title']}\n\nGate evidence is in the first comment.\n")
    url = _call(["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "-q",
                 ".[0].url // empty"], cwd=repo) or _call([  # attempt n+1 (P3-T6): reuse the open PR
        "gh", "pr", "create", "--base", ev.base_branch, "--head", branch, "--title",
        f"{d['card']['id']}: {ev.card['title']}", "--body-file", str(body)], cwd=repo)
    _call(["gh", "pr", "comment", url, "--body-file", str(summary)], cwd=repo)
    reasons = ",".join(d["scope"]["human_gate_reasons"])
    print(f"HUMAN_GATE {url} reason={reasons}" if ev.human_gate else f"GATED {url}")
    print(url)
    return url


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="factory gate", description=__doc__)
    ap.add_argument("--card", required=True)
    ap.add_argument("--c0", required=True)
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--runs-root")
    ap.add_argument("--ci", action="store_true", help="post factory/{secrets,scope,gates}")
    ap.add_argument("--push", action="store_true", help="on pass: push, PR, evidence comment (never arms)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        ev = run_gate(a.repo, a.card, a.c0, a.attempt, ci=a.ci, runs_root=a.runs_root)
    except subprocess.CalledProcessError as e:  # e.g. no .factory/cards/<id>.json at C0 (bootstrap PRs)
        print(f"factory gate: cannot gate card {a.card!r} at {a.c0[:12]}: {' '.join(e.cmd[:4])} failed "
              f"(rc={e.returncode}) — not a factory card branch?", file=sys.stderr)
        return 2
    print(json.dumps(ev.doc, indent=2) if a.json else f"{ev.doc['result']} {ev.path}")
    for r in ev.reasons:
        print(f"reason: {r}", file=sys.stderr)
    if a.push and ev.doc["result"] == "pass":
        push(ev, a.repo)
    return 0 if ev.doc["result"] == "pass" else 1
