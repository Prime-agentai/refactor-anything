"""The lock on disk: ``.refactor-anything/lock.json`` plus one ``cases/<module>.jsonl`` per module."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .targets import Target, split_key

DIR = ".refactor-anything"
VERSION = 1


class LockError(RuntimeError):
    pass


def sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@dataclass
class Lock:
    root: Path
    targets: list[Target] = field(default_factory=list)
    python: str = ""
    sys_path: list[str] = field(default_factory=list)  # relative to root where possible
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)  # module -> {file, sha256}
    functions: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> {kind, note, ...}
    commands: list[list[str]] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    git: dict[str, Any] = field(default_factory=dict)
    cases_sha256: str = ""
    cases: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # module -> cases

    @property
    def dir(self) -> Path:
        return self.root / DIR

    # -- persistence ------------------------------------------------------------------
    @staticmethod
    def exists(root: Path) -> bool:
        return (root / DIR / "lock.json").is_file()

    @staticmethod
    def load(root: Path) -> "Lock":
        path = root / DIR / "lock.json"
        if not path.is_file():
            raise LockError(f"no lock in {root} (run: refactor-anything record <module> -- <command>)")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != VERSION:
            raise LockError(f"lock version {data.get('version')} is not supported by this refactor-anything")
        lock = Lock(
            root=root,
            targets=[Target.from_json(t) for t in data.get("targets", [])],
            python=data.get("python", ""),
            sys_path=list(data.get("sys_path", [])),
            sources=dict(data.get("sources", {})),
            functions=dict(data.get("functions", {})),
            commands=[list(c) for c in data.get("commands", [])],
            created=data.get("created", ""),
            updated=data.get("updated", ""),
            git=dict(data.get("git", {})),
            cases_sha256=data.get("cases_sha256", ""),
        )
        for module in lock.sources:
            lock.cases[module] = read_cases(lock.case_file(module))
        return lock

    def case_file(self, module: str) -> Path:
        return self.dir / "cases" / f"{module}.jsonl"

    def save(self) -> None:
        (self.dir / "cases").mkdir(parents=True, exist_ok=True)
        gi = self.dir / ".gitignore"
        if not gi.exists():
            gi.write_text("raw/\nruns/\nhook-state.json\n", encoding="utf-8")
        for module, cases in sorted(self.cases.items()):
            cases.sort(key=lambda c: (c["key"], c["id"]))
            text = "".join(json.dumps(c, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for c in cases)
            self.case_file(module).write_text(text, encoding="utf-8")
        self.cases_sha256 = self.compute_cases_sha256()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.created = self.created or now
        self.updated = now
        data = {
            "version": VERSION,
            "tool": "refactor-anything",
            "created": self.created,
            "updated": self.updated,
            "targets": [t.to_json() for t in self.targets],
            "python": self.python,
            "sys_path": self.sys_path,
            "sources": self.sources,
            "functions": dict(sorted(self.functions.items())),
            "commands": self.commands,
            "git": self.git,
            "cases_sha256": self.cases_sha256,
        }
        (self.dir / "lock.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def compute_cases_sha256(self) -> str:
        h = hashlib.sha256()
        for module in sorted(self.sources):
            h.update(module.encode() + b"\0")
            h.update(self.case_file(module).read_bytes() if self.case_file(module).exists() else b"")
        return h.hexdigest()

    def tampered(self) -> bool:
        """True when the case files on disk no longer match what ``lock.json`` recorded."""
        return bool(self.cases_sha256) and self.compute_cases_sha256() != self.cases_sha256

    # -- queries ----------------------------------------------------------------------
    def all_cases(self, *, stable_only: bool = True) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for module in sorted(self.cases):
            out.extend(c for c in self.cases[module] if not (stable_only and c.get("unstable")))
        return out

    def changed_sources(self) -> list[str]:
        """Modules whose source file differs from the one that was recorded."""
        changed = []
        for module, info in sorted(self.sources.items()):
            path = self.root / info["file"] if info.get("file") else None
            if path is None or sha256_file(path) != info.get("sha256"):
                changed.append(module)
        return changed

    def abs_sys_path(self) -> list[str]:
        return [str((self.root / p).resolve()) if not os.path.isabs(p) else p for p in self.sys_path]


def read_cases(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def module_of(case: dict[str, Any]) -> str:
    return split_key(case["key"])[0]


def rel(root: Path, path: str | Path) -> str:
    p = Path(path).resolve()
    try:
        return p.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(p)
