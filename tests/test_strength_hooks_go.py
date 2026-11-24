from __future__ import annotations

import json
import sys
from pathlib import Path

from helpers import edit, make_project, ra, record

COUNTDOWN = '''\
def countdown(n):
    out = []
    while n > 0:
        out.append(n)
        n -= 1
    return out


def weak(x):
    if x > 100:
        return "big"
    return "small"
'''


def test_strength_kills_and_lists_survivors(tmp_path: Path) -> None:
    root = make_project(tmp_path, {"cd.py": COUNTDOWN, "run.py": "import cd\ncd.countdown(3)\ncd.weak(1)\n"})
    record(root, "cd", cmd=("run.py",))
    p = ra(root, "strength", "--json")
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data["functions"]["cd:countdown"]["killed"] == data["functions"]["cd:countdown"]["total"]
    # `n -= 1` -> `n += 1` never ends: killed by the timeout, not hung
    survivors = data["survivors"]
    # weak() was only called with 1: flipping `>` to `>=` or 100 to 101 goes unnoticed
    assert any(s["function"] == "weak" and s["line"] == 10 and s["mutation"] == "`>` -> `>=`" for s in survivors)
    assert all(s["function"] == "weak" for s in survivors)
    assert (root / ".refactor-anything" / "runs" / "strength.json").exists()
    p = ra(root, "strength", "--min", "99")
    assert p.returncode == 1 and "below --min" in p.stderr
    assert "survivors" in p.stdout and "cd.py:10" in p.stdout


def test_strength_refuses_when_lock_already_fails(tmp_path: Path) -> None:
    root = make_project(tmp_path, {"cd.py": COUNTDOWN, "run.py": "import cd\ncd.weak(1)\n"})
    record(root, "cd", cmd=("run.py",))
    edit(root, "cd.py", 'return "small"', 'return "tiny"')
    p = ra(root, "strength")
    assert p.returncode == 2 and "does not pass its own lock" in p.stderr


# -- Claude Code hooks ----------------------------------------------------------------

def _stop(root: Path, session: str = "s1", **env: str) -> dict[str, object] | None:
    p = ra(root, "hook", "stop", stdin=json.dumps({"session_id": session, "cwd": str(root), "hook_event_name": "Stop",
                                                    "stop_hook_active": False}), extra_env=env or None)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


def test_stop_hook_blocks_until_behaviour_matches(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    assert _stop(root) is None  # no lock: silent
    record(root, "textkit")
    assert _stop(root) is None  # nothing changed: silent, no replay
    edit(root, "textkit.py", "range(0, len(items), n)", "range(0, len(items) - 1, n)")
    out = _stop(root)
    assert out is not None and out["decision"] == "block"
    assert "textkit:chunk" in str(out["reason"]) and "(1/3)" in str(out["reason"])
    assert _stop(root, REFACTOR_ANYTHING_HOOK="off") is None
    _stop(root)
    _stop(root)
    final = _stop(root)  # fourth time in the same session: let it stop, but say so
    assert final is not None and "decision" not in final and "after 3 attempts" in str(final["systemMessage"])
    edit(root, "textkit.py", "range(0, len(items) - 1, n)", "range(0, len(items), n)")
    assert _stop(root, session="s2") is None


def test_stop_hook_blocks_on_tamper(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    f = root / ".refactor-anything" / "cases" / "textkit.jsonl"
    f.write_text(f.read_text().splitlines()[0] + "\n")
    out = _stop(root)
    assert out is not None and out["decision"] == "block" and "were modified" in str(out["reason"])


def test_guard_denies_edits_to_the_recording(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    for path, denied in [(".refactor-anything/cases/textkit.jsonl", True), (str(root / ".refactor-anything" / "lock.json"), True),
                         ("textkit.py", False)]:
        p = ra(root, "hook", "guard", stdin=json.dumps({"cwd": str(root), "tool_name": "Write", "tool_input": {"file_path": path}}))
        assert p.returncode == 0
        if denied:
            assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        else:
            assert p.stdout.strip() == ""
    p = ra(root, "hook", "guard", stdin="not json")
    assert p.returncode == 0 and p.stdout == ""


def test_install_claude_is_idempotent_and_keeps_other_hooks(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo theirs"}]}]}}))
    for _ in range(2):
        p = ra(root, "install", "claude", "--command", "refactor-anything")
        assert p.returncode == 0, p.stderr
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    stop_cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert stop_cmds == ["echo theirs", "refactor-anything hook stop"]
    (pre,) = data["hooks"]["PreToolUse"]
    assert pre["matcher"] == "Write|Edit|MultiEdit|NotebookEdit" and pre["hooks"][0]["command"] == "refactor-anything hook guard"
    skill = (root / ".claude" / "skills" / "refactor-anything" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: refactor-anything") and "refactor-anything check" in skill
    p = ra(root, "install", "claude", "--local", "--no-skill", "--command", "x")
    assert (root / ".claude" / "settings.local.json").exists()


# -- go loop with a scripted agent ---------------------------------------------------------

GOOD_AGENT = '''\
import pathlib, sys
assert "Refactor task" in sys.stdin.read()
p = pathlib.Path("textkit.py")
p.write_text(p.read_text().replace("    return [items[i:i + n] for i in range(0, len(items), n)]",
    "    starts = range(0, len(items), n)\\n    return [items[s:s + n] for s in starts]"))
'''

FLAKY_AGENT = '''\
import pathlib, sys
prompt = sys.stdin.read()
p = pathlib.Path("textkit.py")
src = p.read_text()
if "lock fails" in prompt:  # second round: the diff came back, fix it
    p.write_text(src.replace("range(0, len(items) - 1, n)", "range(0, len(items), n)"))
else:
    p.write_text(src.replace("range(0, len(items), n)", "range(0, len(items) - 1, n)"))
'''

CHEAT_AGENT = '''\
import pathlib
p = pathlib.Path(".refactor-anything/cases/textkit.jsonl")
p.write_text(p.read_text().splitlines()[0] + "\\n")
'''


def _agent(root: Path, name: str, text: str) -> str:
    (root / name).write_text(text)
    return f'"{sys.executable}" {name}'


def test_go_passes_with_a_good_agent(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    p = ra(root, "go", "--agent", _agent(root, "good.py", GOOD_AGENT), "make chunk readable")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "PASS after round 1" in p.stdout and "locked modules changed: textkit" in p.stdout
    assert "make chunk readable" in (root / ".refactor-anything" / "TASK.md").read_text()


def test_go_feeds_the_diff_back(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    p = ra(root, "go", "--agent", _agent(root, "flaky.py", FLAKY_AGENT), "tidy chunk")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "round 1: lock fails" in p.stdout and "PASS after round 2" in p.stdout


def test_go_gives_up_after_rounds(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    bad = _agent(root, "bad.py", "import pathlib\np = pathlib.Path('textkit.py')\n"
                                 "p.write_text(p.read_text().replace('n must be positive', 'nope'))\n")
    p = ra(root, "go", "--agent", bad, "--rounds", "2", "x")
    assert p.returncode == 1 and "after 2 rounds" in p.stderr


def test_go_rejects_an_agent_that_edits_the_recording(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    p = ra(root, "go", "--agent", _agent(root, "cheat.py", CHEAT_AGENT), "anything")
    assert p.returncode == 3 and "changed the recording" in p.stderr


def test_go_refuses_to_start_on_a_failing_lock(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    edit(root, "textkit.py", "range(0, len(items), n)", "range(0, len(items) - 1, n)")
    p = ra(root, "go", "--agent", "true", "x")
    assert p.returncode == 2 and "before the agent starts" in p.stderr
