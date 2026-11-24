"""Lock strength: run the recorded cases against AST mutants of each locked module."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import mutate
from .engine import replay
from .store import Lock, module_of


class StrengthError(RuntimeError):
    pass


@dataclass
class MutantResult:
    function: str
    line: int
    description: str
    killed: bool
    how: str  # "case", "timeout", "import", or "" for a survivor
    code: str  # the original source line, for the report


@dataclass
class ModuleStrength:
    module: str
    file: str
    cases: int
    baseline_seconds: float
    results: list[MutantResult] = field(default_factory=list)

    @property
    def killed(self) -> int:
        return sum(r.killed for r in self.results)


@dataclass
class StrengthReport:
    modules: list[ModuleStrength]

    @property
    def total(self) -> int:
        return sum(len(m.results) for m in self.modules)

    @property
    def killed(self) -> int:
        return sum(m.killed for m in self.modules)

    @property
    def score(self) -> float:
        return self.killed / self.total if self.total else 0.0

    def per_function(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for m in self.modules:
            for r in m.results:
                k = f"{m.module}:{r.function}"
                killed, total = out.get(k, (0, 0))
                out[k] = (killed + r.killed, total + 1)
        return dict(sorted(out.items()))

    def to_json(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4), "killed": self.killed, "total": self.total,
            "functions": {k: {"killed": a, "total": b} for k, a, b in ((k, *v) for k, v in self.per_function().items())},
            "survivors": [
                {"module": m.module, "file": m.file, "function": r.function, "line": r.line,
                 "mutation": r.description, "code": r.code}
                for m in self.modules for r in m.results if not r.killed
            ],
        }


def run(root: Path, *, modules: list[str] | None = None, functions: set[str] | None = None,
        per_function: int = 25, jobs: int | None = None, python: str | None = None,
        memory_mb: int = 1024) -> StrengthReport:
    lock = Lock.load(root.resolve())
    if lock.tampered():
        raise StrengthError("the recorded cases were changed after recording")
    report = StrengthReport([])
    for module, info in sorted(lock.sources.items()):
        if modules and module not in modules:
            continue
        path = lock.root / info["file"]
        source = path.read_text(encoding="utf-8")
        cases = [c for c in lock.all_cases() if module_of(c) == module]
        if not cases:
            continue
        is_pkg = path.name == "__init__.py"
        base_job = {"module": module, "filename": str(path), "package": is_pkg, "source": mutate.baseline(source)}
        base = replay(lock, cases, mutant=base_job, python=python)
        bad = [r for r in base.results if r["status"] != "pass"]
        if base.error or bad:
            why = base.error or f"{len(bad)} of {len(cases)} cases fail before any mutation ({bad[0].get('detail') or bad[0].get('diff')})"
            raise StrengthError(f"{module}: the unmutated module does not pass its own lock: {why}. Run `check` first.")
        ms = ModuleStrength(module, info["file"], len(cases), base.seconds)
        quals: set[str] | None = None
        if functions:
            quals = set()
            for f in functions:
                mod, sep, qual = f.partition(":")
                if not sep:
                    quals.add(f)
                elif mod == module:
                    quals.add(qual)
        muts = mutate.mutants(source, functions=quals, per_function=per_function)
        lines = source.splitlines()
        timeout = max(3.0, base.seconds * 20 + 2.0)

        def one(m: mutate.Mutant) -> MutantResult:
            job = dict(base_job, source=m.source)
            r = replay(lock, cases, mutant=job, timeout=timeout, python=python, memory_mb=memory_mb)
            if r.timed_out:
                killed, how = True, "timeout"
            elif r.error:
                killed, how = True, "import"
            else:
                killed = any(x["status"] != "pass" for x in r.results)
                how = "case" if killed else ""
            code = lines[m.line - 1].strip() if 0 < m.line <= len(lines) else ""
            return MutantResult(m.function, m.line, m.description, killed, how, code)

        workers = jobs or max(1, min(8, (os.cpu_count() or 2)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            ms.results = list(pool.map(one, muts))
        report.modules.append(ms)
    return report
