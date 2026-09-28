"""`factory <command> …` — dispatches to factory.<command>.main(argv). Imports are lazy so
`factory --help` works before every command module exists."""
from __future__ import annotations

import argparse
import importlib
import sys

COMMANDS = {
    "doctor": "validate a repo's factory.yaml and grade it L0–L3",
    "gate": "run the gates for a card and write evidence.json",
    "card": "validate or freeze a card at C0",
    "judge": "run the independent judge and post factory/judge",
    "audit": "sample and record post-merge audits",
    "worker": "start or remove a worker worktree + agent terminal",
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="factory", description="Factory Standard v1 CLI")
    sub = p.add_subparsers(dest="command", metavar="{" + ",".join(COMMANDS) + "}")
    for name, text in COMMANDS.items():
        sub.add_parser(name, help=text, add_help=False)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        parser = _parser()
        if argv and argv[0] not in ("-h", "--help"):
            parser.error(f"unknown command {argv[0]!r}")
        parser.print_help()
        return 0 if argv else 1
    try:
        module = importlib.import_module(f"factory.{argv[0]}")
    except ModuleNotFoundError as exc:
        if exc.name != f"factory.{argv[0]}":
            raise
        print(f"factory {argv[0]}: not implemented yet", file=sys.stderr)
        return 1
    return int(module.main(argv[1:]) or 0)


if __name__ == "__main__":
    sys.exit(main())
