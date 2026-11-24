"""Target specs: which modules and which of their functions are locked.

A spec is ``pkg.module`` (every public function and method defined in it) or
``pkg.module:name,Class.method`` (only those). A case key is ``module:qualname``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_QUAL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


class TargetError(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    module: str
    names: tuple[str, ...] | None = None  # None means: every public function and method

    @property
    def spec(self) -> str:
        return self.module if self.names is None else f"{self.module}:{','.join(self.names)}"

    def to_json(self) -> dict[str, object]:
        return {"module": self.module, "names": list(self.names) if self.names is not None else None}

    @staticmethod
    def from_json(d: dict[str, object]) -> "Target":
        names = d.get("names")
        return Target(str(d["module"]), tuple(str(n) for n in names) if isinstance(names, list) else None)


def parse(spec: str) -> Target:
    spec = spec.strip()
    module, _, rest = spec.partition(":")
    if not _MODULE.match(module):
        raise TargetError(f"not a module name: {module!r} (use the import name, e.g. mypkg.text)")
    if module == "__main__":
        raise TargetError("__main__ cannot be locked: move the code into a module the script imports")
    if not rest:
        return Target(module)
    names = tuple(n.strip() for n in rest.split(",") if n.strip())
    for n in names:
        if not _QUAL.match(n):
            raise TargetError(f"not a function or Class.method name: {n!r}")
    return Target(module, names)


def merge(old: list[Target], new: list[Target]) -> list[Target]:
    """Union of two target lists, one entry per module."""
    by_mod: dict[str, Target] = {t.module: t for t in old}
    for t in new:
        cur = by_mod.get(t.module)
        if cur is None or cur.names is None or t.names is None:
            by_mod[t.module] = Target(t.module, None if (cur is not None and (cur.names is None or t.names is None)) else t.names)
        else:
            by_mod[t.module] = Target(t.module, tuple(dict.fromkeys(cur.names + t.names)))
    return [by_mod[k] for k in sorted(by_mod)]


def key(module: str, qual: str) -> str:
    return f"{module}:{qual}"


def split_key(k: str) -> tuple[str, str]:
    module, _, qual = k.partition(":")
    return module, qual


def is_public(qual: str) -> bool:
    return not any(part.startswith("_") for part in qual.split("."))
