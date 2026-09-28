"""`factory doctor <repo>` — infer/validate factory.yaml, execute its commands in a detached worktree,
grade L0–L3 (plan v3 §4.2). Exit 0 = meets --level, 2 = below, 1 = usage. Never writes to main."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import yaml

from factory import schemas, templates

LEVELS = ("L0", "L1", "L2", "L3")
NEEDS = {"L1": ("build", "lint", "secrets", "boot"), "L2": ("test",), "L3": ("e2e",)}
ORDER = ("setup", "build", "lint", "typecheck", "test", "e2e", "boot")
SECRETS = {"run": "gitleaks git --redact --no-banner --exit-code 1 .", "timeout_s": 300}


class DoctorError(ValueError):
    """The manifest is invalid (schema or cross-field) — a usage error, exit 1."""


def _origin_slug(path: pathlib.Path) -> str:
    url = _git(path, "remote", "get-url", "origin").stdout.strip()
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", url)
    return m.group(1) if m else f"unknown/{path.resolve().name}"


def infer(path) -> dict:
    """`--init`: stack from pyproject.toml / package.json / go.mod. Anything unproven is null."""
    path = pathlib.Path(path)
    cmds = dict.fromkeys(("setup", "build", "lint", "typecheck", "test", "e2e", "boot"))
    langs = []
    if (path / "pyproject.toml").exists():
        langs.append("python")
        cmds["build"] = {"run": "python -m compileall -q ."}
        cmds["test"] = {"run": "pytest -q --junitxml=.factory/junit.xml", "report": ".factory/junit.xml"}
    if (path / "package.json").exists():
        langs.append("typescript" if (path / "tsconfig.json").exists() else "javascript")
        scripts = json.loads((path / "package.json").read_text()).get("scripts", {})
        cmds["setup"] = {"run": "npm ci"}
        for name in ("build", "lint"):  # `npm test` has no proven report -> stays null
            if name in scripts:
                cmds[name] = {"run": f"npm run {name}"}
    if (path / "go.mod").exists():
        langs.append("go")
        cmds["build"] = {"run": "go build ./..."}  # `go test ./...` passes with no tests: null
    return {"version": 1,
            "repo": {"github": _origin_slug(path), "base_branch": "main", "business": "tekscape"},
            "stack": {"languages": langs or ["other"]}, "commands": cmds,
            "paths": {"src": ["**"], "tests": []},
            "risk_class": "high" if cmds["test"] is None else "medium"}


def validate_manifest(m: dict) -> None:
    try:
        schemas.validate("factory", m)
    except schemas.SchemaError as exc:
        raise DoctorError(str(exc)) from None
    if m["commands"].get("test") is None and m["risk_class"] != "high":
        raise DoctorError("cross-field: commands.test is null so risk_class must be high "
                          f"(got {m['risk_class']!r})")


def _junit_executed(report: pathlib.Path) -> int:
    root = ET.parse(report).getroot()
    suites = [root] if root.tag == "testsuite" else root.iter("testsuite")
    return sum(int(s.get("tests", 0)) - int(s.get("skipped", 0)) for s in suites)


def run_command(name: str, cmd: dict, cwd) -> dict:
    """Run one manifest command; a `report` must show >= min_tests executed (catches fake greens)."""
    cwd = pathlib.Path(cwd)
    report = cwd / cmd["report"] if cmd.get("report") else None
    if report and report.exists():
        report.unlink()  # never grade a stale report
    t0 = time.monotonic()
    try:
        p = subprocess.run(["bash", "-c", cmd["run"]], cwd=cwd, capture_output=True, check=False,
                           env={**os.environ, **cmd.get("env", {})},
                           timeout=cmd.get("timeout_s", 900))
        code, log = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as exc:
        code, log = 124, (exc.stdout or b"") + (exc.stderr or b"")
    r = {"exit": code, "duration_s": round(time.monotonic() - t0, 2),
         "log_sha256": hashlib.sha256(log).hexdigest(), "executed": None, "status": "pass"}
    if code != 0:
        r.update(status="fail", reason=f"{name} exited {code}: {log.decode(errors='replace')[-300:]}")
    elif report:
        if not report.exists():
            return {**r, "status": "fail", "reason": f"report {cmd['report']} not written"}
        r["executed"] = n = _junit_executed(report)
        if n < cmd.get("min_tests", 1):
            r.update(status="fail", reason=f"executed {n} < min_tests {cmd.get('min_tests', 1)}")
    return r


def _git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=False)


def run_commands(repo, manifest: dict) -> dict:
    """Every non-null command + the secrets scan, in a fresh detached worktree of origin/<base>."""
    base = manifest["repo"].get("base_branch", "main")
    ref = f"origin/{base}"
    if _git(repo, "remote", "get-url", "origin").returncode == 0:
        _git(repo, "fetch", "-q", "origin", base)
    if _git(repo, "rev-parse", "--verify", "-q", ref).returncode != 0:
        ref = "HEAD"
    sha = _git(repo, "rev-parse", ref).stdout.strip()
    wt = pathlib.Path(f"/tmp/factory-doctor-{sha[:12]}")
    if wt.exists():
        _git(repo, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(wt, ignore_errors=True)
    _git(repo, "worktree", "prune")
    add = _git(repo, "worktree", "add", "--detach", str(wt), ref)
    if add.returncode != 0:
        raise DoctorError(f"git worktree add failed: {add.stderr.strip()}")
    try:
        gates = {"secrets": run_command("secrets", SECRETS, wt)}
        for name in ORDER:
            if manifest["commands"].get(name):
                gates[name] = run_command(name, manifest["commands"][name], wt)
        return {"sha": sha, "gates": gates}
    finally:
        _git(repo, "worktree", "remove", "--force", str(wt))


CONTEXTS = ("factory/secrets", "factory/scope", "factory/gates", "factory/judge")
RULESET = pathlib.Path(__file__).resolve().parent.parent / "github" / "ruleset.json"


def _gh(path: str) -> tuple[bool, object]:
    raw = ["-H", "Accept: application/vnd.github.raw"] if "/contents/" in path else []
    p = subprocess.run(["gh", "api", path, *raw], capture_output=True, text=True, check=False)
    try:
        body = json.loads(p.stdout)
    except ValueError:
        body = p.stdout if p.returncode == 0 else {"message": p.stderr.strip()}
    return p.returncode == 0, body


def _project(live, exp):
    """Live ruleset reduced to the keys github/ruleset.json sets (drops ids, timestamps, links)."""
    if isinstance(exp, dict):
        return {k: _project(live.get(k) if isinstance(live, dict) else None, v) for k, v in exp.items()}
    if isinstance(exp, list) and isinstance(live, list) and len(live) == len(exp):
        def key(x):
            return json.dumps(x.get("type") or x.get("context") if isinstance(x, dict) else x)
        return [_project(a, b) for a, b in zip(sorted(live, key=key), sorted(exp, key=key))]
    return live


def check_github(slug, base, protected=(), gh=_gh, app_id=None) -> dict:
    """Read-only: required contexts strict + judge pinned, ruleset hash, auto-merge, CODEOWNERS."""
    if app_id is None:
        app_id = json.loads((pathlib.Path.home() / ".factory" / "app.json").read_text())["app_id"]
    reasons = []

    def get(rel):
        ok, body = gh(f"repos/{slug}/{rel}".rstrip("/"))
        if not ok:
            msg = body.get("message", body) if isinstance(body, dict) else body
            reasons.append(f"gh api {rel or 'repo'}: HTTP {body.get('status', body.get('__http', '?'))} "
                           f"{msg}" if isinstance(body, dict) else f"gh api {rel}: {msg}")
        return body if ok else None

    rules = get(f"rules/branches/{base}")
    if rules is not None:
        rsc = next((r["parameters"] for r in rules if r["type"] == "required_status_checks"), None)
        if rsc is None:
            reasons.append(f"ruleset drift: {base} has no required_status_checks rule")
        else:
            if not rsc.get("strict_required_status_checks_policy"):
                reasons.append("ruleset drift: required status checks not strict")
            pins = {c["context"]: c.get("integration_id") for c in rsc["required_status_checks"]}
            reasons += [f"ruleset drift: {c} not required" for c in CONTEXTS if c not in pins]
            if "factory/judge" in pins and pins["factory/judge"] != app_id:
                reasons.append(f"ruleset drift: factory/judge pinned to {pins['factory/judge']}, not App {app_id}")
    exp = json.loads(RULESET.read_text().replace('"<APP_ID>"', str(app_id)))
    listed = get("rulesets")
    if listed is not None:
        rid = next((r["id"] for r in listed if r.get("name") == exp["name"]), None)
        live = get(f"rulesets/{rid}") if rid else reasons.append("ruleset drift: no 'factory' ruleset")
        if live is not None:
            got, want = _project(live, exp), _project(exp, exp)  # same key order on both sides
            reasons += [f"ruleset drift: {k} differs from github/ruleset.json" for k in exp if got[k] != want[k]]
    if not (get("") or {}).get("allow_auto_merge"):
        reasons.append("allow_auto_merge is not enabled")
    if protected:
        owners = get("contents/.github/CODEOWNERS") or ""
        covered = {ln.split()[0].lstrip("/") for ln in str(owners).splitlines() if ln.strip() and ln[0] != "#"}
        reasons += [f"CODEOWNERS does not cover protected path {p}" for p in protected
                    if p.lstrip("/") not in covered]
    return {"enforcement": "advisory" if reasons else "enforced", "reasons": reasons}


def grade(statuses: dict) -> str:
    """L0 no build; L1 build+lint+secrets+boot; L2 + test; L3 + e2e. Each level needs the one below."""
    got = "L0"
    for level in LEVELS[1:]:
        if any(statuses.get(g) != "pass" for g in NEEDS[level]):
            break
        got = level
    return got


def exit_for(got: str, want: str) -> int:
    return 0 if LEVELS.index(got) >= LEVELS.index(want) else 2


def _reasons(manifest: dict, gates: dict, want: str, repo, sha: str) -> list[str]:
    out = []
    for level in LEVELS[1 : LEVELS.index(want) + 1]:
        for g in NEEDS[level]:
            if g != "secrets" and manifest["commands"].get(g) is None:
                out.append(f"commands.{g} is null (needed for {level})")
            elif gates.get(g, {}).get("status") != "pass":
                out.append(f"{g}: {gates.get(g, {}).get('reason', 'not run')}")
    wf = _git(repo, "show", f"{sha}:.github/workflows/factory.yml")
    if wf.returncode != 0:
        out.append("no workflow: .github/workflows/factory.yml missing (run `factory doctor --fix`)")
    elif drift := workflow_drift(manifest, wf.stdout):
        out.append(drift)
    return out


def workflow_drift(manifest: dict, text: str) -> str | None:
    """The committed workflow must equal the template rendered at its own pin (the pin may lag)."""
    pin = templates.workflow_pin(text)
    if pin is None or templates.drift_hash(text) != templates.drift_hash(templates.render_workflow(manifest, pin)):
        return "workflow drift: .github/workflows/factory.yml differs from templates/factory.workflow.yml (run --fix)"
    return None


BRANCH = "factory/bootstrap"
CLI_MIRROR = "https://github.com/tekscape-inc/factory-cli.git"


def _gh_cli(*args) -> str:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise DoctorError(f"gh {args[0]} {args[1]}: {p.stderr.strip()}")
    return p.stdout.strip()


def fix(repo, manifest: dict, cli_sha: str, gh=_gh_cli) -> str | None:
    """Render workflow + AGENTS.md block (+ CODEOWNERS if absent) on `factory/bootstrap` from origin/<base>
    and open ONE PR. Never writes the base branch. None = a PR is already open or nothing changed."""
    slug, base = manifest["repo"]["github"], manifest["repo"].get("base_branch", "main")
    manifest = {**manifest, "commands": {**dict.fromkeys(ORDER), **manifest["commands"]}}  # absent == null
    if gh("pr", "list", "--repo", slug, "--head", BRANCH, "--state", "open", "--json", "url", "--jq", ".[].url"):
        return None
    _git(repo, "fetch", "-q", "origin", base)
    wt = pathlib.Path(tempfile.mkdtemp(prefix="factory-fix-")) / "wt"
    if (add := _git(repo, "worktree", "add", "-q", "--detach", str(wt), f"origin/{base}")).returncode:
        raise DoctorError(f"git worktree add failed: {add.stderr.strip()}")
    try:
        agents = (wt / "AGENTS.md").read_text() if (wt / "AGENTS.md").exists() else ""
        old, block = templates.extract_block(agents), templates.render_template("AGENTS.block.md", manifest).strip()
        files = {".github/workflows/factory.yml": templates.render_workflow(manifest, cli_sha),
                 "AGENTS.md": agents.replace(old, block) if old else agents + ("\n" if agents else "") + block + "\n"}
        if not (wt / ".github" / "CODEOWNERS").exists():
            files[".github/CODEOWNERS"] = templates.render_template("CODEOWNERS.tmpl", manifest)
        for rel, text in files.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(text)
        _git(wt, "add", "-A")
        if _git(wt, "diff", "--cached", "--quiet").returncode == 0:
            return None
        for step in (("commit", "-qm", "chore(factory): bootstrap CI workflow, AGENTS.md block, CODEOWNERS"),
                     ("push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")):
            if (r := _git(wt, *step)).returncode:
                raise DoctorError(f"git {step[0]} failed: {r.stderr.strip()}")
        return gh("pr", "create", "--repo", slug, "--base", base, "--head", BRANCH, "--title",
                  "chore(factory): bootstrap", "--body", "Generated by `factory doctor --fix` (P1-T13).")
    finally:
        _git(repo, "worktree", "remove", "--force", str(wt))


def _emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"level {result.get('level')} exit {result['exit']}")
        for r in result["reasons"]:
            print(f"  - {r}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="factory doctor")
    ap.add_argument("repo")
    ap.add_argument("--level", default="L1")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--init", action="store_true", help="write an inferred factory.yaml if absent")
    ap.add_argument("--no-github", action="store_true", help="skip the read-only GitHub checks")
    ap.add_argument("--fix", action="store_true", help="open ONE PR on factory/bootstrap with the generated files")
    ap.add_argument("--cli-sha", help="factory-cli mirror SHA to pin (default: its main)")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 1
    repo = pathlib.Path(a.repo).expanduser()
    fy = repo / "factory.yaml"
    try:
        if a.level not in LEVELS[1:]:
            raise DoctorError(f"--level must be one of L1 L2 L3, got {a.level!r}")
        if not repo.is_dir():
            raise DoctorError(f"not a directory: {repo}")
        if a.init and not fy.exists():
            fy.write_text(yaml.safe_dump(infer(repo), sort_keys=False))
        if not fy.exists():
            raise DoctorError(f"no factory.yaml in {repo} (run with --init)")
        manifest = yaml.safe_load(fy.read_text())
        validate_manifest(manifest)
        if a.fix:
            sha = a.cli_sha or subprocess.run(["git", "ls-remote", CLI_MIRROR, "refs/heads/main"], capture_output=True,
                                              text=True, check=False).stdout[:40]
            print(json.dumps({"pr": fix(repo, manifest, sha), "factory_cli_sha": sha}))
            return 0
        run = run_commands(repo, manifest)
    except DoctorError as exc:
        _emit({"level": None, "exit": 1, "reasons": [str(exc)]}, a.json)
        return 1
    gates = run["gates"]
    level = grade({k: v["status"] for k, v in gates.items()})
    result = {"level": level, "exit": exit_for(level, a.level), "sha": run["sha"],
              "reasons": _reasons(manifest, gates, a.level, repo, run["sha"]), "manifest": manifest, "gates": gates}
    if not a.no_github:  # ADR-0003 §7: enforcement != enforced fails every level
        gh = check_github(manifest["repo"]["github"], manifest["repo"].get("base_branch", "main"),
                          manifest["paths"].get("protected", []))
        result["enforcement"], result["reasons"] = gh["enforcement"], result["reasons"] + gh["reasons"]
        if gh["enforcement"] != "enforced":
            result["exit"] = 2
    _emit(result, a.json)
    return result["exit"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
