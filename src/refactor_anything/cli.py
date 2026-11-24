"""refactor-anything command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import __version__, agent, engine, hooks, strength
from .store import DIR, Lock, LockError
from .targets import TargetError, parse

EXIT_OK, EXIT_DIFF, EXIT_ERROR, EXIT_TAMPER = 0, 1, 2, 3


def _err(msg: str) -> int:
    print(f"refactor-anything: {msg}", file=sys.stderr)
    return EXIT_ERROR


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "-"


# -- record ------------------------------------------------------------------------------

def cmd_record(ns: argparse.Namespace) -> int:
    try:
        targets = [parse(t) for t in ns.targets]
    except TargetError as exc:
        return _err(str(exc))
    command = ns.command
    try:
        s = engine.record(Path(ns.root), targets, command, cap=ns.cap, private=ns.private,
                          fresh=ns.fresh, python=ns.python)
    except (engine.RecordError, LockError) as exc:
        return _err(str(exc))
    if s.exit_code != 0:
        print(f"refactor-anything: note: the recorded command exited {s.exit_code}; "
              "its calls were recorded anyway and are now the locked behaviour", file=sys.stderr)
    print(f"\nrecorded {s.new_cases} new calls ({s.unstable} unstable, left out) into {DIR}/")
    width = max([len(k) for k in [*s.per_key, *s.notes, *s.never_called]] + [8])
    print(f"\n  {'function':<{width}}  {'cases':>5}  {'unstable':>8}")
    for k, d in sorted(s.per_key.items()):
        note = f"  ({s.notes[k]})" if k in s.notes else ""
        print(f"  {k:<{width}}  {d['cases']:>5}  {d['unstable']:>8}{note}")
    for k, note in sorted(s.notes.items()):
        if k not in s.per_key:
            print(f"  {k:<{width}}  {0:>5}  {'':>8}  ({note})")
    for k in s.never_called:
        print(f"  {k:<{width}}  {0:>5}  {'':>8}  never called by this command: not locked")
    all_unstable = [k for k, d in s.per_key.items() if d["cases"] == 0 and d["unstable"]]
    if all_unstable:
        print(f"\n  nondeterministic (every call differed on replay): {', '.join(all_unstable)}")
    print("\nnext: `refactor-anything strength` to see what the lock catches, then refactor and `refactor-anything check`")
    return EXIT_OK


# -- check -------------------------------------------------------------------------------

def cmd_check(ns: argparse.Namespace) -> int:
    try:
        res = engine.check(Path(ns.root), python=ns.python, timeout=ns.timeout)
    except LockError as exc:
        return _err(str(exc))
    if ns.json:
        print(json.dumps(res.to_json(), indent=2))
    elif ns.markdown:
        print(_markdown(res))
    else:
        _print_check(res, ns.limit)
    if res.tampered:
        return EXIT_TAMPER
    if res.error and not res.failures:
        return EXIT_ERROR
    return EXIT_OK if res.ok else EXIT_DIFF


def _print_check(res: engine.CheckResult, limit: int) -> None:
    if res.tampered:
        print(f"TAMPERED  {res.error}")
        return
    if res.error:
        print(f"ERROR  {res.error}")
        return
    changed = ", ".join(res.changed) if res.changed else "none"
    for k, counts in sorted(res.per_key.items()):
        total = sum(counts.values())
        bad = total - counts.get("pass", 0)
        mark = "ok  " if not bad else "DIFF"
        print(f"  {mark} {k}  {counts.get('pass', 0)}/{total}")
    for k in res.unlocked:
        print(f"  --   {k}  no stable cases: not locked")
    if res.failures:
        print()
        print(hooks.summarize_failures(res.failures, limit))
    verdict = "PASS" if res.ok else "FAIL"
    print(f"\n{verdict}  {res.passed}/{res.total} recorded calls match in {res.seconds:.2f}s (locked modules changed since recording: {changed})")


def _markdown(res: engine.CheckResult) -> str:
    head = "✅ behaviour unchanged" if res.ok else ("⚠️ recording tampered" if res.tampered else "❌ behaviour changed")
    lines = [f"### refactor-anything: {head}", "",
             f"**{res.passed}/{res.total}** recorded calls match. Locked modules changed: {', '.join(res.changed) or 'none'}.", ""]
    if res.error:
        lines += [f"> {res.error}", ""]
    if res.per_key:
        lines += ["| function | match |", "|---|---|"]
        for k, c in sorted(res.per_key.items()):
            lines.append(f"| `{k}` | {c.get('pass', 0)}/{sum(c.values())} |")
        lines.append("")
    if res.failures:
        lines += ["<details><summary>differences</summary>", "", "```", hooks.summarize_failures(res.failures, 20), "```", "</details>"]
    return "\n".join(lines)


# -- strength ----------------------------------------------------------------------------

def cmd_strength(ns: argparse.Namespace) -> int:
    try:
        rep = strength.run(Path(ns.root), modules=ns.module or None, functions=set(ns.function) if ns.function else None,
                           per_function=ns.per_function, jobs=ns.jobs, python=ns.python, memory_mb=ns.memory_mb)
    except (strength.StrengthError, LockError) as exc:
        return _err(str(exc))
    data = rep.to_json()
    runs = Path(ns.root) / DIR / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "strength.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    if ns.json:
        print(json.dumps(data, indent=2))
    else:
        for k, (killed, total) in rep.per_function().items():
            print(f"  {_pct(killed, total):>4}  {k}  {killed}/{total} mutants caught")
        survivors = data["survivors"]
        if survivors:
            print("\nsurvivors (behaviour could change here and the lock would not notice):")
            for s in survivors[: ns.limit]:
                print(f"  {s['file']}:{s['line']}:{s['col']}  {s['function']}: {s['mutation']}    {s['code']}")
            if len(survivors) > ns.limit:
                print(f"  ... {len(survivors) - ns.limit} more in {DIR}/runs/strength.json")
        print(f"\nlock strength {_pct(rep.killed, rep.total)}: {rep.killed}/{rep.total} mutants caught")
    if ns.min is not None and rep.score * 100 < ns.min:
        print(f"refactor-anything: lock strength {rep.score * 100:.0f}% is below --min {ns.min:g}%", file=sys.stderr)
        return EXIT_DIFF
    return EXIT_OK


# -- status ------------------------------------------------------------------------------

def cmd_status(ns: argparse.Namespace) -> int:
    try:
        lock = Lock.load(Path(ns.root).resolve())
    except LockError as exc:
        return _err(str(exc))
    stable: dict[str, int] = {}
    unstable: dict[str, int] = {}
    for c in lock.all_cases(stable_only=False):
        (unstable if c.get("unstable") else stable)[c["key"]] = (unstable if c.get("unstable") else stable).get(c["key"], 0) + 1
    print(f"lock: {DIR}/lock.json  recorded {lock.created}  updated {lock.updated}")
    print(f"targets: {', '.join(t.spec for t in lock.targets)}   python: {lock.python}")
    if lock.git.get("commit"):
        print(f"git: {lock.git['commit'][:12]}{' (dirty)' if lock.git.get('dirty') else ''}")
    changed = lock.changed_sources()
    print(f"changed since recording: {', '.join(changed) if changed else 'none'}")
    if lock.tampered():
        print("WARNING: the case files were edited after recording")
    for k in sorted(lock.functions):
        note = lock.functions[k].get("note", "")
        print(f"  {stable.get(k, 0):>4} cases  {unstable.get(k, 0):>3} unstable  {k}  {note}")
    return EXIT_OK


# -- go / install / hook -----------------------------------------------------------------

def cmd_go(ns: argparse.Namespace) -> int:
    goal = " ".join(ns.goal).strip()
    if not goal:
        return _err("say what to refactor, e.g. go \"split parse_config into smaller functions\"")
    try:
        res = agent.go(Path(ns.root), goal, agent=ns.agent, rounds=ns.rounds, model=ns.model, timeout=ns.timeout)
    except agent.GoError as exc:
        print(f"refactor-anything: {exc}", file=sys.stderr)
        return EXIT_TAMPER if "changed the recording" in str(exc) else EXIT_ERROR
    except LockError as exc:
        return _err(str(exc))
    if not res.ok:
        print(f"refactor-anything: lock still fails after {res.rounds} rounds; the code is left as the agent wrote it", file=sys.stderr)
        return EXIT_DIFF
    return EXIT_OK


def cmd_install(ns: argparse.Namespace) -> int:
    try:
        written = hooks.install_claude(Path(ns.root), local=ns.local, command=ns.command, skill=not ns.no_skill)
    except ValueError as exc:
        return _err(str(exc))
    for p in written:
        print(f"wrote {p}")
    print("Claude Code will now replay the lock before it stops, and cannot edit .refactor-anything/ with its edit tools.")
    return EXIT_OK


def cmd_hook(ns: argparse.Namespace) -> int:
    try:
        return hooks.stop_hook() if ns.event == "stop" else hooks.guard_hook()
    except Exception as exc:  # a hook must never wedge the session
        print(f"refactor-anything hook error: {exc}", file=sys.stderr)
        return 0


# -- parser ------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="refactor-anything",
        description="Lock what your Python code does, let a coding agent refactor it, and prove nothing changed.")
    p.add_argument("--version", action="version", version=f"refactor-anything {__version__}")
    p.add_argument("-C", "--root", default=".", help="project root (default: current directory)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="run a command and record real calls to the target modules",
                       description="Example: refactor-anything record mypkg.text mypkg.dates:parse -- python -m pytest -q")
    r.add_argument("targets", nargs="+", help="module (all public functions/methods) or module:name,Class.method")
    r.add_argument("--cap", type=int, default=200, help="distinct cases kept per function (default 200)")
    r.add_argument("--private", action="store_true", help="also lock _private functions and methods")
    r.add_argument("--fresh", action="store_true", help="discard the existing lock and record a new baseline")
    r.add_argument("--python", help="interpreter for replays (default: the one the command used)")
    r.set_defaults(func=cmd_record)

    c = sub.add_parser("check", help="replay the recorded calls against the current code")
    c.add_argument("--json", action="store_true")
    c.add_argument("--markdown", action="store_true", help="a PR-comment summary")
    c.add_argument("--limit", type=int, default=10, help="differences to show (default 10)")
    c.add_argument("--timeout", type=float, default=600.0)
    c.add_argument("--python")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("strength", help="how much the lock catches: replay against small mutants of the code")
    s.add_argument("--module", action="append", help="only this module (repeatable)")
    s.add_argument("--function", action="append", help="only mutate this function, e.g. slugify or mod:Class.meth")
    s.add_argument("--per-function", type=int, default=25, help="max mutants per function (default 25)")
    s.add_argument("--jobs", type=int, help="mutants run in parallel (default: CPUs, max 8)")
    s.add_argument("--memory-mb", type=int, default=1024, help="address-space cap per mutant process on Linux (default 1024)")
    s.add_argument("--min", type=float, help="exit 1 when the strength is below this percentage")
    s.add_argument("--json", action="store_true")
    s.add_argument("--limit", type=int, default=15)
    s.add_argument("--python")
    s.set_defaults(func=cmd_strength)

    st = sub.add_parser("status", help="what is locked")
    st.set_defaults(func=cmd_status)

    g = sub.add_parser("go", help="have a coding agent do the refactor, held to the lock")
    g.add_argument("goal", nargs="+", help="what to refactor, in plain words")
    g.add_argument("--agent", default="claude", help="claude, codex, or a shell command that reads the task on stdin")
    g.add_argument("--rounds", type=int, default=3)
    g.add_argument("--model")
    g.add_argument("--timeout", type=float, default=1800.0, help="seconds per agent round")
    g.set_defaults(func=cmd_go)

    i = sub.add_parser("install", help="install the Claude Code Stop hook, edit guard and skill")
    i.add_argument("agent", choices=["claude"])
    i.add_argument("--local", action="store_true", help="write .claude/settings.local.json instead")
    i.add_argument("--command", help="how hooks should call refactor-anything (default: detected)")
    i.add_argument("--no-skill", action="store_true")
    i.set_defaults(func=cmd_install)

    h = sub.add_parser("hook", help="(called by Claude Code) stop | guard")
    h.add_argument("event", choices=["stop", "guard"])
    h.set_defaults(func=cmd_hook)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # `record a b -- cmd`: split at the first `--` so targets and command never mix.
    command: list[str] = []
    if "record" in args and "--" in args:
        cut = args.index("--")
        args, command = args[:cut], args[cut + 1:]
    ns = build_parser().parse_args(args)
    if ns.cmd == "record":
        ns.command = command
    try:
        return int(ns.func(ns))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
