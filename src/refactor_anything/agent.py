"""`go`: hand a refactor to a coding agent, then hold it to the recording.

The loop is plain code, not a prompt: the agent edits, refactor-anything replays, and the
diff goes back to the agent until the lock passes or the rounds run out. The recording
is fingerprinted before the agent starts; a run in which it changed is refused.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .engine import CheckResult, check
from .hooks import self_command, summarize_failures
from .store import DIR, Lock


class GoError(RuntimeError):
    pass


def fingerprint(root: Path) -> str:
    h = hashlib.sha256()
    base = root / DIR
    for p in sorted([base / "lock.json", *sorted((base / "cases").glob("*.jsonl"))]):
        h.update(p.name.encode() + b"\0")
        h.update(p.read_bytes() if p.exists() else b"<missing>")
    return h.hexdigest()


def task_text(lock: Lock, goal: str, cmd: str) -> str:
    keys = sorted({c["key"] for c in lock.all_cases()})
    listing = "\n".join(f"- `{k}`" for k in keys)
    return f"""# Refactor task

{goal}

## The behaviour lock

The public behaviour of these functions was recorded from real runs ({len(lock.all_cases())} calls):

{listing}

Rules:
- Keep every recorded call's result, exception and argument mutations identical.
  Internal helpers, structure, names of private things and performance are yours to change.
- Run `{cmd} check` after your changes and keep going until it passes.
- Do not edit, delete or re-record anything in `{DIR}/`. The run is rejected if it changes.
"""


def agent_argv(agent: str, prompt: str, *, model: str | None, session: str, first: bool,
               cmd: str) -> tuple[list[str] | str, str | None]:
    """The agent command line and, for custom agents, the text to send on stdin.

    A custom agent is a shell command string, run with ``shell=True``: on Windows that is the only
    way cmd.exe receives its quotes intact (``["cmd", "/c", ...]`` escapes them as ``\\"``).
    """
    if agent == "claude":
        argv = ["claude", "-p", prompt, "--permission-mode", "acceptEdits",
                "--allowedTools", f"Bash({cmd} check:*)", "Bash(python -m pytest:*)", "Read", "Edit", "Write", "Glob", "Grep"]
        argv += ["--session-id", session] if first else ["--resume", session]
        if model:
            argv += ["--model", model]
        return argv, None
    if agent == "codex":
        argv = ["codex", "exec", "--full-auto"]
        if model:
            argv += ["--model", model]
        return argv + [prompt], None
    return agent, prompt


@dataclass
class GoResult:
    ok: bool
    rounds: int
    final: CheckResult | None
    log: list[str] = field(default_factory=list)


def go(root: Path, goal: str, *, agent: str = "claude", rounds: int = 3, model: str | None = None,
       timeout: float = 1800.0, echo: Callable[[str], None] = print) -> GoResult:
    root = root.resolve()
    lock = Lock.load(root)
    cmd = self_command()
    before = check(root)
    if not before.ok:
        raise GoError("the lock does not pass before the agent starts: "
                      + (before.error or f"{len(before.failures)} failing cases") + ". Run `check` and fix that first.")
    fp = fingerprint(root)
    (root / DIR / "TASK.md").write_text(task_text(lock, goal, cmd), encoding="utf-8")
    session = str(uuid.uuid4())
    prompt = task_text(lock, goal, cmd)
    result = GoResult(False, 0, None)
    env = dict(os.environ, REFACTOR_ANYTHING_TASK=str(root / DIR / "TASK.md"))
    for n in range(1, rounds + 1):
        result.rounds = n
        argv, stdin_text = agent_argv(agent, prompt, model=model, session=session, first=(n == 1), cmd=cmd)
        echo(f"refactor-anything: round {n}/{rounds}: running {agent if isinstance(argv, str) else argv[0]}")
        try:
            proc = subprocess.run(argv, cwd=root, env=env, input=stdin_text, text=True, timeout=timeout,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=isinstance(argv, str),
                                  encoding="utf-8", errors="replace")
        except FileNotFoundError as exc:
            raise GoError(f"agent not found: {exc}") from exc
        except subprocess.TimeoutExpired:
            result.log.append(f"round {n}: agent timed out after {timeout:g}s")
            echo(result.log[-1])
            continue
        result.log.append(f"round {n}: agent exited {proc.returncode}")
        if proc.returncode != 0:
            tail = "\n".join((proc.stdout or "").strip().splitlines()[-15:])
            echo(f"refactor-anything: round {n}: the agent exited {proc.returncode}" + (f"; its last output:\n{tail}" if tail else ""))
        if fingerprint(root) != fp:
            raise GoError("the agent changed the recording in .refactor-anything/. Run rejected. "
                          "Restore it (git checkout -- .refactor-anything) and review the agent's code changes by hand.")
        res = check(root)
        result.final = res
        changed = ", ".join(res.changed) or "none"
        if res.ok:
            echo(f"refactor-anything: PASS after round {n}: {res.passed}/{res.total} recorded calls match "
                 f"(locked modules changed: {changed})")
            result.ok = True
            return result
        summary = res.error or summarize_failures(res.failures)
        echo(f"refactor-anything: round {n}: lock fails ({len(res.failures)} of {res.total} differ)\n{summary}")
        prompt = (f"The behaviour lock fails after your change: {len(res.failures)} of {res.total} recorded calls differ.\n"
                  f"{summary}\n\nFix the code (not {DIR}/) so `{cmd} check` passes. The goal is unchanged:\n{goal}")
    return result
