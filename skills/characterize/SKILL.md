---
name: characterize
description: Pin a repo's CURRENT behaviour with golden-master tests that PASS on the base commit, so later cards have a safety net. Use for characterization cards on repos below L2 (no or weak tests). Frontier lanes only.
---

# characterize

You write **tests only**. Tamper rules are inverted for this card: the only writable paths are `paths.tests`
from `factory.yaml`; anything else changed (source, config, lockfiles, `.factory/**`, `.github/**`) is `tamper`.

## Rules

- Pin what the code **does**, not what it should do. If behaviour looks like a bug, pin it anyway, add the comment
  `# CHARACTERIZED: current behaviour, possibly a bug`, and list it in your reply. Never fix source.
- Cover the public surface: CLI commands and flags, exported functions, HTTP handlers, file outputs.
  Per entry point: one typical input, the edge inputs you can see in the code (empty, missing, invalid), and the error path.
- Golden files live under `paths.tests` (e.g. `tests/golden/<entry>/<case>.txt`); compare byte-for-byte.
  No snapshot auto-update flags, no `--update-snapshots`, no regenerate-on-mismatch.
- Deterministic: fix clocks, seeds, locale, ordering and temp paths; no network.
- Every test asserts on a value. No `assert True`, no "does not raise" as the only check, no skips, no `.only(`.
- No new dependencies. Use the test runner the manifest's `commands.test` already uses; if `commands.test` is null,
  the card must name the runner and it must already be installable offline — otherwise `NEEDS_CONTEXT`.

## Must pass on base

1. Run `commands.test.run` on the unmodified base: exit 0, JUnit report written.
2. Run it a second time: identical result (no flakes).
3. Executed test count ≥ `commands.test.min_tests` and greater than before your change.

Weak characterization tests are the known risk; the mutation spot-check that measures them is P3 spike T5, not this skill.

## Card

The card is written as in `skills/architect-card` §3 with `kind: "characterization"`, `lane: "coder-pro"`,
`scope.writable` = `paths.tests`, `acceptance.tests` = the new test files, `success.run` = the test command.
The architect-card rule "fails on base" is replaced by **passes on base**.

## Stop

Validate with `uv run factory card --validate .factory/cards/<id>.json`. Do not commit or push.
Missing information → one line `NEEDS_CONTEXT: <question>` (not an attempt). Last line of your reply:
`{"card": ".factory/cards/<id>.json", "tests": ["<paths>"], "base_exit": 0, "executed": <int>, "suspected_bugs": [<strings>]}`
