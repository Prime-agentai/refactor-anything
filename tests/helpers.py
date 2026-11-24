"""Shared fixtures: a small project on disk and a way to run the real CLI against it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"

TEXTKIT = '''\
import random
import re


class Counter:
    def __init__(self):
        self.counts = {}

    def add(self, word):
        self.counts[word] = self.counts.get(word, 0) + 1
        return self.counts[word]

    @staticmethod
    def norm(word):
        return word.strip().lower()

    @classmethod
    def of(cls, words):
        c = cls()
        for w in words:
            c.add(w)
        return c

    @property
    def total(self):
        return sum(self.counts.values())


def slugify(text, sep="-"):
    """Lowercase ASCII slug."""
    text = re.sub(r"[^a-z0-9]+", sep, text.lower())
    return text.strip(sep)


def chunk(items, n):
    if n <= 0:
        raise ValueError("n must be positive")
    return [items[i:i + n] for i in range(0, len(items), n)]


def pick(items):
    return random.choice(items)


def sort_in_place(items):
    items.sort()


def lines(text):
    for line in text.splitlines():
        yield line


def loud(x):
    print("side effect on stdout", x)
    return x * 2


def _helper(x):
    return x + 1


def uses_helper(x):
    return _helper(x) * 3
'''

RUN_APP = '''\
import textkit
for t in ["Hello World", "  A--b  ", "Ünïcode!", ""]:
    textkit.slugify(t)
    textkit.slugify(t, sep="_")
for n in (1, 2, 3, 0):
    try:
        textkit.chunk(list(range(7)), n)
    except ValueError:
        pass
for _ in range(5):
    textkit.pick(list(range(1000)))
textkit.sort_in_place([3, 1, 2])
c = textkit.Counter.of(["a", "b", "a"])
c.add("z")
c.total
textkit.Counter.norm("  Hi ")
list(textkit.lines("a\\nb"))
textkit.loud(21)
for i in range(4):
    textkit.uses_helper(i)
'''


def env() -> dict[str, str]:
    e = dict(os.environ)
    e["PYTHONPATH"] = str(SRC)
    e["PYTHONDONTWRITEBYTECODE"] = "1"  # same-size edits within a second must not reuse stale bytecode
    e.pop("REFACTOR_ANYTHING_RECORD", None)
    return e


def make_project(root: Path, files: dict[str, str] | None = None) -> Path:
    files = files or {"textkit.py": TEXTKIT, "run_app.py": RUN_APP}
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text) if text.startswith("\n") else text, encoding="utf-8")
    return root


def ra(root: Path, *args: str, stdin: str | None = None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    e = env()
    if extra_env:
        extra = dict(extra_env)
        if "PYTHONPATH" in extra:
            extra["PYTHONPATH"] = str(SRC) + os.pathsep + extra["PYTHONPATH"]
        e.update(extra)
    return subprocess.run([sys.executable, "-m", "refactor_anything", *args], cwd=root, env=e, text=True,
                          capture_output=True, input=stdin, timeout=300)


def record(root: Path, *targets: str, cmd: tuple[str, ...] = ("run_app.py",), flags: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    p = ra(root, "record", *targets, *flags, "--", sys.executable, *cmd)
    assert p.returncode == 0, p.stdout + p.stderr
    return p


def edit(root: Path, name: str, old: str, new: str) -> None:
    p = root / name
    text = p.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {name}"
    p.write_text(text.replace(old, new), encoding="utf-8")


def check_json(root: Path) -> tuple[int, dict[str, Any]]:
    p = ra(root, "check", "--json")
    return p.returncode, json.loads(p.stdout)
