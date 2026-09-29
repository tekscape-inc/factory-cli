---
name: architect-card
description: Turn one requested behaviour change into a frozen-ready factory card (.factory/cards/<id>.json) plus acceptance tests that FAIL on the base commit. Use when asked to "write card <id>" for a repo with a factory.yaml.
---

# architect-card

You are the architect. You write **a card and its acceptance tests only** — never the implementation.
Worked example: `tests/fixtures/cards/greet-001.json` in `factory-standard`. Schema: `schema/card.schema.json`.

## 0. Refuse or ask first

- Read `factory.yaml` (or, when the repo keeps none, the manifest path the prompt gives you), `AGENTS.md`, and the source the change touches. Do not guess past them.
- If the request is ambiguous, needs a secret, or names code you cannot find: write **nothing**; reply with one line
  `NEEDS_CONTEXT: <the specific question>` and stop. `NEEDS_CONTEXT` returns the card to you (or to John) and is
  **not** an attempt. A worker may also send `NEEDS_CONTEXT` back on your card: answer by writing a new card `<id>.v2`
  (cards are immutable once frozen).
- Feature/fix cards need a repo at L2 (tests collected). Below that, write a `gate-zero` card or use `skills/characterize`.
- More than one behaviour change, or over the diff budget below → split into several cards linked by `depends_on`.

## 1. Acceptance tests (write these first)

- Put them under `paths.tests`, one file per card, named after the behaviour: `--shout` → `tests/test_shout.py`.
- One test per acceptance criterion; assert on observable output (return value, stdout, exit code), not on internals.
- Deterministic: no network, no clock, no randomness, no new dependency.
- **Prove they fail on base**: run the file with the repo's test runner (e.g. `pytest -q tests/test_shout.py`) and
  record the non-zero exit. It must fail because the behaviour is missing (assertion, unknown option), not because
  the test itself is broken (syntax error, wrong fixture). If it passes on base, the card is wrong — rewrite.
- `sha256` of each test file: `shasum -a 256 <path>` (hex only).

## 2. Lane (plan v3 §4.4)

`lane: "qwen"` only if **all** hold, else `"coder-pro"`:

- named files; ≤ 3 files and ≤ 300 changed lines;
- context pack ≤ 40k tokens (the named files + tests + card);
- success command < 3 min, no network, no new dependency;
- exactly one local behaviour change;
- `risk_class` low or medium; `kind` is not `gate-zero` or `characterization`; manifest `lanes.qwen` is not `false`;
- never untested Go or TypeScript.

`diff_budget`: qwen ≤ 3 files / 300 lines; coder-pro from `lanes.default_diff_budget`, else 8 files / 300 lines.
`risk_class`: the higher of the manifest's and the change's own; round up when in doubt (`policy/risk.md`).
**`risk_class` is `high` whenever the change falls in any manifest `owner_approval.required_for` category** (e.g.
`public_api`: a changed signature, new parameter, new CLI flag or removed symbol; `new_dependency`). Categories are not
paths, so the gate cannot detect them; `high` is what makes the gate stop at `HUMAN_GATE` (auto-merge not armed) instead of
letting the judge discover it after auto-merge is armed. Say which category in `spec`.

## 3. The card

Write `.factory/cards/<id>.json` with exactly these keys (no `frozen` — `factory card --freeze` adds it):

```json
{
  "id": "<id>", "repo": "<repo.github>", "kind": "feature|fix|chore|gate-zero|characterization",
  "lane": "qwen|coder-pro", "risk_class": "low|medium|high", "title": "<imperative, ≤ 72 chars>",
  "spec": "<what and why, ≤ 400 words; name the files to change>",
  "acceptance": {
    "criteria": [{"id": "AC1", "text": "<observable behaviour>", "verified_by": "<test path>::<test name>"}],
    "tests": [{"path": "<test path>", "sha256": "<64 hex>"}]
  },
  "success": {"run": "<command running only the acceptance tests>", "timeout_s": 180},
  "scope": {"writable": ["<globs the implementer may change>"], "out_of_scope": ["<prose for the judge>"]},
  "diff_budget": {"files": 3, "lines": 300},
  "deps": {"allowed_new": []},
  "depends_on": [],
  "author": {"architect": {"vendor": "anthropic", "model": "<your model id>"}, "implementer_vendor_excluded_from_judge": true}
}
```

- `scope.writable` never includes acceptance tests, `.factory/**`, `factory.yaml`, `.github/**`, or `paths.protected`.
- Every criterion has a `verified_by` that exists in the acceptance tests.

## 4. Check, then stop

1. `uv run factory card --validate .factory/cards/<id>.json` → exit 0 (fix and re-run until it is).
2. Re-run the acceptance tests on base → still non-zero.
3. Do not edit source, do not commit, do not push — the orchestrator freezes the card as C0.
4. Last line of your reply, one JSON object:
   `{"card": ".factory/cards/<id>.json", "tests": ["<path>"], "base_exit": <non-zero int>, "lane": "<lane>"}`
