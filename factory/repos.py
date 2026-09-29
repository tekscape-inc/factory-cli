"""Registry twin of scripts/lib/repo.sh: $FACTORY_HOME/repos.tsv rows `name slug biz mode clone` (tab-separated).
The three samples are implicit `auto` rows; `owner-merge` repos never arm and get no App statuses."""
import os
import pathlib
import re
import subprocess

SAMPLES, KEYS = ("sample-cli-py", "sample-web-ts", "sample-legacy-go"), ("name", "slug", "biz", "mode", "clone")

def home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("FACTORY_HOME", "~/.factory")).expanduser()

def rows() -> dict[str, dict]:
    out = {n: dict(zip(KEYS, (n, f"tekscape-inc/{n}", "tekscape", "auto", os.path.expanduser(f"~/factory-samples/{n}"))))
           for n in SAMPLES}
    tsv = home() / "repos.tsv"
    for f in (ln.split("\t") for ln in (tsv.read_text().splitlines() if tsv.exists() else []) if not ln.startswith("#")):
        if len(f) == 5:
            out[f[0]] = {**dict(zip(KEYS, f)), "clone": os.path.expanduser(f[4])}
    return out

def resolve(name: str) -> dict:
    if (r := rows().get(name)) is None:
        raise KeyError(f"unknown repo {name!r} (not in repos.tsv; onboard it with scripts/onboard.sh)")
    return r

def by_slug(slug: str) -> dict | None:
    return next((r for r in rows().values() if r["slug"] == slug), None)

def clone_for(slug: str) -> str:   # unregistered slugs keep the pre-registry default
    return (by_slug(slug) or {}).get("clone") or os.path.expanduser(f"~/factory-samples/{slug.split('/')[1]}")

def overlay_for(repo_path) -> pathlib.Path | None:   # eng-R3: an owner-merge repo's manifest lives outside it (no footprint)
    url = subprocess.run(["git", "-C", str(repo_path), "remote", "get-url", "origin"], capture_output=True, text=True, check=False).stdout
    r = (m := re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", url.strip())) and by_slug(m[1])
    p = r and home() / "biz" / r["biz"] / "manifests" / f"{r['name']}.yaml"
    return p if r and r["mode"] == "owner-merge" and p.exists() else None
