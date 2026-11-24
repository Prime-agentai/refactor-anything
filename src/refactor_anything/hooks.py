"""Claude Code integration: a Stop hook that refuses "done" while behaviour differs, and a guard
that keeps the agent's edit tools out of the recording.

Both read the hook payload from stdin and answer in Claude Code's hook JSON. They never
raise: a hook that crashes must not wedge the user's session.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from .engine import check
from .envs import injected_bin
from .store import DIR, Lock, LockError, sha256_file

MAX_BLOCKS = 3
OFF_ENV = "REFACTOR_ANYTHING_HOOK"
SKILL = """\
---
name: refactor-anything
description: Use before and during any refactor of Python code that has a refactor-anything lock (a .refactor-anything/ folder), or when the user asks to refactor safely, keep behaviour identical, or lock behaviour before changing code.
---

# Refactor with a behaviour lock

This project records what its Python functions really do and replays those calls after you
change the code. A refactor is done when the replay matches, not when the code looks right.

1. If there is no `.refactor-anything/lock.json` yet, record one *before editing anything*:
   `{cmd} record <module> -- python -m pytest -q` (or any command that exercises the code).
2. Check how much the lock can see: `{cmd} strength`. Survivors are lines where behaviour
   could change unnoticed; record more inputs that reach them before you touch those lines.
3. Make your change, then run `{cmd} check`. Fix the code until every case matches.
4. Never edit, delete or re-record `.refactor-anything/` to make a check pass. If the user
   asked for a change in behaviour, say which cases differ and let the user re-record.
"""


def self_command() -> str:
    """How hooks and agents should call us later, after this process (and any uvx shim) is gone."""
    found = shutil.which("refactor-anything")
    inj = injected_bin()
    if found and (inj is None or Path(found).resolve().parent != inj.resolve()):
        return "refactor-anything"
    # Run through uvx/pipx, or not on PATH: pin the interpreter that has us installed.
    return f"{shlex.quote(sys.executable)} -m refactor_anything"


def _payload() -> dict[str, Any]:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def find_root(start: Path) -> Path | None:
    for p in [start, *start.parents]:
        if (p / DIR / "lock.json").is_file():
            return p
    return None


def summarize_failures(failures: list[dict[str, Any]], limit: int = 6) -> str:
    lines = []
    for f in failures[:limit]:
        if f["status"] == "fail":
            where = f" at {f['path']}" if f.get("path") and f["path"] != "$" else ""
            lines.append(f"- {f['key']} [{f['diff']}{where}]: expected {f.get('expected')}, got {f.get('actual')}")
        else:
            lines.append(f"- {f['key']} [{f['status']}]: {f.get('detail', '')}")
    if len(failures) > limit:
        lines.append(f"- ... and {len(failures) - limit} more (run `refactor-anything check`)")
    return "\n".join(lines)


def stop_hook() -> int:
    payload = _payload()
    if os.environ.get(OFF_ENV, "").lower() in ("0", "off", "false"):
        return 0
    root = find_root(Path(payload.get("cwd") or os.getcwd()).resolve())
    if root is None:
        return 0
    try:
        lock = Lock.load(root)
    except (LockError, OSError, ValueError):
        return 0
    if not lock.tampered() and not lock.changed_sources():
        return 0  # nothing locked was touched: let the session end without a replay
    result = check(root, timeout=300)
    if result.ok:
        return 0
    state_path = root / DIR / "hook-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    session = str(payload.get("session_id", "unknown"))
    entry = state.get(session) if isinstance(state.get(session), dict) else {}
    blocks = int(entry.get("blocks", 0))
    # What the agent is being told about: the locked sources and which cases differ.
    seen = hashlib.sha256(json.dumps([[lock.sources[m]["file"], sha256_file(root / lock.sources[m]["file"])]
                                      for m in sorted(lock.sources)] +
                                     sorted([f["key"], f["id"], f["status"]] for f in result.failures)).encode()).hexdigest()
    if blocks and entry.get("seen") == seen:
        # Nothing changed since the last block: the agent has chosen to stop and report. Let it.
        print(json.dumps({"systemMessage": "refactor-anything: behaviour still differs from the recording and the code is "
                                            "unchanged since the last block, so the agent is stopping with "
                                            f"{len(result.failures)} differing cases. Run `refactor-anything check` to see them."}))
        return 0
    if blocks >= MAX_BLOCKS:
        print(json.dumps({"systemMessage": f"refactor-anything: recorded behaviour still differs after {MAX_BLOCKS} attempts "
                                            "in this session. Not blocking again; run `refactor-anything check` to see the cases."}))
        return 0
    state[session] = {"blocks": blocks + 1, "seen": seen}
    try:
        state_path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    if result.tampered:
        why = "The recorded cases in .refactor-anything/ were modified. Restore them (e.g. `git checkout -- .refactor-anything`) and make the code match instead."
    elif result.error:
        why = f"The replay could not run: {result.error}"
    else:
        why = (f"{len(result.failures)} of {result.total} recorded calls now behave differently:\n"
               + summarize_failures(result.failures))
    reason = (f"refactor-anything: the behaviour lock does not pass ({blocks + 1}/{MAX_BLOCKS}).\n{why}\n"
              "Change the code until `refactor-anything check` passes. Do not edit .refactor-anything/. "
              "If this behaviour change is intended, stop and tell the user which cases differ.")
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def guard_hook() -> int:
    """PreToolUse: deny Write/Edit/MultiEdit/NotebookEdit on files inside .refactor-anything/."""
    payload = _payload()
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not target:
        return 0
    cwd = Path(payload.get("cwd") or os.getcwd())
    path = Path(target) if os.path.isabs(target) else cwd / target
    parts = path.resolve().parts
    if DIR in parts:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "refactor-anything: .refactor-anything/ holds the recorded behaviour and is read-only "
                                        "for the agent. Change the code so `refactor-anything check` passes instead.",
        }}))
    return 0


def install_claude(root: Path, *, local: bool = False, command: str | None = None, skill: bool = True) -> list[Path]:
    """Add the hooks to .claude/settings(.local).json without disturbing existing entries."""
    cmd = command or self_command()
    settings_path = root / ".claude" / ("settings.local.json" if local else "settings.json")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except ValueError as exc:
        raise ValueError(f"{settings_path} is not valid JSON; fix it first") from exc
    hooks = settings.setdefault("hooks", {})
    wanted = [
        ("Stop", None, f"{cmd} hook stop"),
        ("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit", f"{cmd} hook guard"),
    ]
    for event, matcher, command_line in wanted:
        entries = hooks.setdefault(event, [])
        present = any(
            h.get("command", "").endswith(command_line.split(" ", 1)[-1]) and "refactor" in h.get("command", "")
            for e in entries for h in e.get("hooks", [])
        )
        if present:
            continue
        entry: dict[str, Any] = {"hooks": [{"type": "command", "command": command_line, "timeout": 600}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        entries.append(entry)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    written = [settings_path]
    if skill:
        skill_path = root / ".claude" / "skills" / "refactor-anything" / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(SKILL.format(cmd=cmd), encoding="utf-8")
        written.append(skill_path)
    return written
