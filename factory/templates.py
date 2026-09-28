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
