"""The one Orca adapter: --json + unwrap (S6); sender = parked coordinator (S3); never --wait (S19);
ack a Delivery only after every row is processed (S4); no Run-wide wipe (S5)."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

FACTORY_HOME = pathlib.Path(os.environ.get("FACTORY_HOME", "~/.factory")).expanduser()
LEDGER = FACTORY_HOME / "ledger.jsonl"
COORDINATOR = FACTORY_HOME / "coordinator.json"
FLAG_ALIASES = {"sender": "--from"}
BODY_FLAGS = {"--body", "--payload", "--result", "--spec", "--command", "--subject", "--objective"}


class OrcaError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def build_argv(group: str, cmd: str | None = None, **flags) -> list[str]:
    """`orca <group> [cmd] --flag value … --json`. True → bare flag; None/False dropped."""
    if flags.pop("wait", False):
        raise ValueError("--wait is refused: poll `check` instead of blocking (S19)")
    argv = ["orca", group] + ([cmd] if cmd else [])
    for key, value in flags.items():
        if value is None or value is False:
            continue
        argv.append(FLAG_ALIASES.get(key, "--" + key.replace("_", "-")))
        if value is not True:
            argv.append(str(value))
    return argv + ["--json"]


def unwrap(doc: dict) -> dict:
    if not doc.get("ok"):
        err = doc.get("error") or {}
        raise OrcaError(err.get("code", "unknown_error"), err.get("message", ""))
    return doc.get("result") or {}


def run(argv: list[str], timeout: int = 120) -> dict:
    """Run one orca command; log {ts, argv, ok, code} to the ledger (body values redacted)."""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        doc = {"ok": 0, "error": {"code": f"exit_{proc.returncode}", "message": proc.stderr[-300:]}}
    ok, code = bool(doc.get("ok")), (doc.get("error") or {}).get("code")
    logged = ["<redacted>" if i and argv[i - 1] in BODY_FLAGS else a for i, a in enumerate(argv)]
    FACTORY_HOME.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), "argv": logged, "ok": ok, "code": code}) + "\n")
    return unwrap(doc)

def orca(group: str, cmd: str | None = None, **flags) -> dict:
    return run(build_argv(group, cmd, **flags))


@dataclass
class Delivery:
    delivery_id: str | None
    count: int
    messages: list[dict]
    completions: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    replayed: bool | None = None  # present only when a delivery is returned (S6)


def _row(msg: dict) -> dict:
    payload = msg.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload) if payload.strip() else {}
    return {"id": msg.get("id"), "type": msg.get("type"), "from": msg.get("from_handle"),
            "taskId": payload.get("taskId"), "dispatchId": payload.get("dispatchId"),
            "outcome": payload.get("outcome"), "rejection": payload.get("_orcaLifecycleRejection"),
            "payload": payload}


def parse_check(doc: dict) -> Delivery:
    """A lifecycle rejection is an alert, never a completion (S4)."""
    res = unwrap(doc)
    rows = [_row(m) for m in res.get("messages") or []]
    return Delivery(
        delivery_id=res.get("deliveryId"), count=res.get("count", len(rows)), messages=rows,
        completions=[r for r in rows if r["type"] == "worker_done" and not r["rejection"]],
        alerts=[r for r in rows if r["rejection"] or r["type"] in ("escalation", "question")],
        replayed=res.get("replayed"))


def check(terminal: str, run_id: str | None = None) -> Delivery:
    return parse_check({"ok": True, "result": orca("orchestration", "check", terminal=terminal,
                                                   run=run_id)})


def ack(delivery_id: str, terminal: str, run_id: str | None = None) -> Delivery:
    """Acknowledge a whole batch (returns the next one). Prefer consume(), which orders it."""
    return parse_check({"ok": True, "result": orca("orchestration", "check", terminal=terminal,
                                                   run=run_id, ack=delivery_id)})


def consume(terminal: str, run_id: str | None, handle: Callable[[Delivery], None]) -> Delivery:
    """check → handle(every row) → ack. If handle raises, the batch stays unacked (replayed)."""
    delivery = check(terminal, run_id)
    if delivery.delivery_id:
        handle(delivery)
        ack(delivery.delivery_id, terminal, run_id)
    return delivery


def ensure_coordinator(run_id: str | None = None, worktree: str | None = None) -> str:
    """Reuse the parked coordinator, else park a new one; rebind the Run to it (S3 step 2)."""
    handle = (json.loads(COORDINATOR.read_text()) if COORDINATOR.exists() else {}).get("handle")
    if handle:
        try:
            orca("terminal", "show", terminal=handle)
        except OrcaError:
            handle = None
    if not handle:
        handle = orca("terminal", "create", worktree=worktree, title="factory-coordinator",
                      command="exec sleep 86400")["terminal"]["handle"]
        COORDINATOR.write_text(json.dumps({"handle": handle}))
    if run_id:
        orca("orchestration", "run-use", id=run_id, sender=handle)
    return handle


def reconcile(run_id: str, placements: dict[str, dict]) -> list[dict]:
    """S3 steps 2–4 after an Orca restart. `placements` maps task id → {repo, worktree, agent}
    because placement is not inherited by --retry-of. Returns one record per retried dispatch."""
    coord = ensure_coordinator(run_id)
    retried = []
    for task, where in placements.items():
        old = orca("orchestration", "dispatch-show", task=task, sender=coord).get("dispatch") or {}
        shown = orca("orchestration", "worker-show", dispatch=old["id"]) if old.get("id") else {}
        if (shown.get("dispatch") or {}).get("status") not in ("failed", "abandoned"):
            continue  # no dispatch, or live, or completed: leave it alone
        orca("orchestration", "worker-abandon", dispatch=old["id"])
        orca("orchestration", "task-update", sender=coord, run=run_id, id=task, status="failed",
             result=json.dumps({"reason": "runtime restarted"}))
        new = orca("orchestration", "worker-start", sender=coord, run=run_id, task=task,
                   retry_of=old["id"], repo=where["repo"], worktree=where["worktree"],
                   agent=where["agent"], setup="skip")
        retried.append({"task": task, "old": old["id"], "new": new.get("dispatchId")})
    return retried
