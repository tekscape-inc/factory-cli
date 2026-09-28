import hashlib
import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"  # rendered from factory.yaml; drift_hash is the one doctor uses
BEGIN, END = "<!-- factory:begin -->", "<!-- factory:end -->"
_TOKEN = re.compile(r"(?<!\$)\{\{\s*(\*?)([\w.]+)\s*\}\}")  # `${{ … }}` (GitHub Actions) is left alone

def _get(ctx: dict, dotted: str, optional: bool = False):
    cur = ctx
    for key in dotted.split("."):
        if cur is None or (optional and key not in cur):
            return None
        cur = cur[key]  # missing scalar -> KeyError: fail loud, never render a blank
    return cur

def _fmt(v) -> str:
    return "null" if v is None else " ".join(map(str, v)) if isinstance(v, list) else str(v)

def _sub(ctx: dict, line: str, item) -> str:
    return _TOKEN.sub(lambda t: str(item) if t.group(1) else _fmt(_get(ctx, t.group(2))), line)

def render_text(text: str, manifest: dict, **extra) -> str:
    """`{{a.b}}` substitutes; a line holding `{{*a.b}}` repeats per list item (absent/empty -> dropped)."""
    ctx, out = {**manifest, **extra}, []  # extra: non-manifest vars, e.g. FACTORY_STANDARD_SHA
    for line in text.splitlines(keepends=True):
        each = next((t.group(2) for t in _TOKEN.finditer(line) if t.group(1)), None)
        for item in (_get(ctx, each, optional=True) or []) if each else [None]:
            out.append(_sub(ctx, line, item))
    return "".join(out)

def render_template(name: str, manifest: dict, **extra) -> str:
    return render_text((TEMPLATES / name).read_text(), manifest, **extra)

def extract_block(agents_md: str) -> str:  # factory-owned AGENTS.md block, markers included
    i, j = agents_md.find(BEGIN), agents_md.find(END)
    return agents_md[i : j + len(END)] if 0 <= i < j else ""

def drift_hash(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").strip().encode()).hexdigest()

_SETUP = {"typescript": ("actions/setup-node@v4", "node-version", "node", "22"),
          "javascript": ("actions/setup-node@v4", "node-version", "node", "22"),
          "go": ("actions/setup-go@v5", "go-version", "go", "1.22")}
_PIN = re.compile(r'checkout -q "([0-9a-f]{40})"')

def render_workflow(manifest: dict, cli_sha: str) -> str:
    """templates/factory.workflow.yml with the CLI mirror pinned and per-language setup (stack.toolchain wins)."""
    if not re.fullmatch(r"[0-9a-f]{40}", cli_sha):
        raise ValueError(f"FACTORY_CLI_SHA must be 40 hex, got {cli_sha!r}")
    langs, tc = manifest["stack"]["languages"], manifest["stack"].get("toolchain") or {}
    steps = "".join(f"      - uses: {a}\n        with:\n          {k}: \"{tc.get(t, d)}\"\n"
                    for a, k, t, d in dict.fromkeys(_SETUP[x] for x in langs if x in _SETUP))
    if "python" in langs:  # the gate runs the manifest's lint/test; the runner has neither
        steps += "      - run: python -m pip install --quiet ruff pytest\n"
    text = (TEMPLATES / "factory.workflow.yml").read_text().replace("      # {{TOOLCHAIN_STEPS}}\n", steps)
    return render_text(text, manifest, FACTORY_CLI_SHA=cli_sha)

def workflow_pin(text: str) -> str | None:
    m = _PIN.search(text)
    return m.group(1) if m else None
