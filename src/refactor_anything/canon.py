"""Canonical, JSON-safe encoding of Python values, so two runs can be compared exactly.

The encoding follows Python's own equality where it is cheap to do so: dict and set
order is ignored, tuples are not lists, ``1`` is not ``1.0``, ``True`` is not ``1``.
Objects are compared by class and attribute state, not by identity, and memory
addresses are stripped from reprs, so a value recorded in one process can be
compared with the same value produced in another.

This module must stay importable with the standard library only: it runs inside the
recorded process and inside the replay process, which may be any Python 3.11+.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import math
import re
import sys
import types
from collections.abc import Mapping
from typing import Any

Canon = Any  # JSON-compatible: None, bool, int, str, list, dict

MAX_DEPTH = 60
MAX_ITEMS = 10_000
_ADDR = re.compile(r" at 0x[0-9A-Fa-f]+")


def qualname(obj: Any) -> str:
    t = obj if isinstance(obj, type) else type(obj)
    return f"{getattr(t, '__module__', '?')}.{getattr(t, '__qualname__', t.__name__)}"


def _key(c: Canon) -> str:
    return json.dumps(c, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _float(x: float) -> Canon:
    if math.isnan(x):
        return {"float": "nan"}
    if math.isinf(x):
        return {"float": "inf" if x > 0 else "-inf"}
    return {"float": repr(x)}


def canon(value: Any) -> Canon:
    """Encode ``value`` into a canonical JSON-compatible structure."""
    return _Encoder().enc(value, 0)


class _Encoder:
    def __init__(self) -> None:
        self.stack: list[int] = []

    def enc(self, v: Any, depth: int) -> Canon:
        if v is None or isinstance(v, str):
            return v
        if isinstance(v, bool):
            return {"bool": v}
        if isinstance(v, enum.Enum):
            return {"enum": f"{qualname(v)}.{v.name}"}
        if isinstance(v, int):
            return {"int": str(v)} if abs(v) > 2**53 else (v if type(v) is int else {"int": str(int(v)), "cls": qualname(v)})
        if isinstance(v, float):
            out = _float(v)
            if type(v) is not float:
                out["cls"] = qualname(v)
            return out
        if isinstance(v, complex):
            return {"complex": [_float(v.real), _float(v.imag)]}
        if isinstance(v, (bytes, bytearray, memoryview)):
            return {"bytes" if isinstance(v, bytes) else "bytearray": bytes(v).hex()}
        if depth > MAX_DEPTH:
            return {"deep": qualname(v)}
        if isinstance(v, (types.FunctionType, types.BuiltinFunctionType, types.MethodType)):
            return {"fn": f"{getattr(v, '__module__', '?')}.{getattr(v, '__qualname__', repr(v))}"}
        if isinstance(v, type):
            return {"type": qualname(v)}
        if isinstance(v, types.ModuleType):
            return {"module": v.__name__}
        if isinstance(v, BaseException):
            return {"exc": qualname(v), "args": self._seq(v.args, depth)}

        if hasattr(v, "__next__") and _is_self_iter(v):
            return {"iterator": qualname(v)}  # its items do not exist yet; only the type is comparable
        vid = id(v)
        if vid in self.stack:
            return {"cycle": len(self.stack) - self.stack.index(vid)}
        self.stack.append(vid)
        try:
            return self._container(v, depth)
        finally:
            self.stack.pop()

    def _seq(self, items: Any, depth: int) -> list[Canon]:
        out: list[Canon] = []
        for i, item in enumerate(items):
            if i >= MAX_ITEMS:
                out.append({"truncated": True})
                break
            out.append(self.enc(item, depth + 1))
        return out

    def _container(self, v: Any, depth: int) -> Canon:
        cls = type(v)
        if cls is list:
            return {"list": self._seq(v, depth)}
        if cls is tuple:
            return {"tuple": self._seq(v, depth)}
        if isinstance(v, tuple) and hasattr(v, "_fields"):  # namedtuple
            return {"namedtuple": qualname(v), "items": self._seq(v, depth)}
        if isinstance(v, (set, frozenset)):
            items = sorted((self.enc(x, depth + 1) for x in v), key=_key)
            return {"frozenset" if isinstance(v, frozenset) else "set": items, **self._cls(v, (set, frozenset))}
        if isinstance(v, Mapping):
            pairs = [[self.enc(k, depth + 1), self.enc(x, depth + 1)] for k, x in v.items()]
            pairs.sort(key=lambda p: _key(p[0]))
            return {"dict": pairs, **self._cls(v, (dict,))}
        if isinstance(v, (list, tuple)):
            return {"seq": qualname(v), "items": self._seq(v, depth)}
        if dataclasses.is_dataclass(v):
            fields = {f.name: self.enc(getattr(v, f.name, None), depth + 1) for f in dataclasses.fields(v)}
            return {"obj": qualname(v), "attrs": fields}
        state = _state(v)
        if state is not None:
            return {"obj": qualname(v), "attrs": {k: self.enc(x, depth + 1) for k, x in sorted(state.items())}}
        return {"repr": _portable_name(v), "text": _ADDR.sub("", _safe_repr(v))}

    @staticmethod
    def _cls(v: Any, plain: tuple[type, ...]) -> dict[str, str]:
        return {} if type(v) in plain else {"cls": qualname(v)}


def _is_stdlib(t: type) -> bool:
    top = (getattr(t, "__module__", "") or "").split(".")[0]
    return top in sys.stdlib_module_names or top == "builtins"


def _portable_name(v: Any) -> str:
    """Stdlib classes move between private modules across versions (pathlib -> pathlib._local in
    3.13); name them by their public top-level module so a lock survives a Python upgrade."""
    t = type(v)
    if _is_stdlib(t):
        return f"{(t.__module__ or '').split('.')[0]}.{t.__qualname__}"
    return qualname(v)


def _is_self_iter(v: Any) -> bool:
    try:
        return iter(v) is v
    except Exception:
        return False


def _state(v: Any) -> dict[str, Any] | None:
    """Attribute state of a user object, or None when its repr describes it better.

    Standard-library objects (Path, Decimal, datetime, UUID, ...) are compared by repr:
    their internal slots hold lazily computed caches that differ between equal values.
    """
    if _is_stdlib(type(v)):
        return None
    d = getattr(v, "__dict__", None)
    attrs: dict[str, Any] = dict(d) if isinstance(d, dict) else {}
    has_slots = False
    for klass in type(v).__mro__:
        slots = klass.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for slot in slots:
            has_slots = True
            if slot not in ("__dict__", "__weakref__") and slot not in attrs and hasattr(v, slot):
                attrs[slot] = getattr(v, slot)
    if not isinstance(d, dict) and not has_slots:
        return None
    if not attrs and getattr(type(v), "__repr__", None) is not getattr(object, "__repr__"):
        return None
    return attrs


def _safe_repr(v: Any) -> str:
    try:
        return repr(v)
    except Exception as exc:  # a broken __repr__ must not break recording
        return f"<unrepresentable {qualname(v)}: {type(exc).__name__}>"


def dumps(c: Canon) -> str:
    return _key(c)


def first_difference(a: Canon, b: Canon, path: str = "$") -> str | None:
    """Return a short path to the first place two canonical values differ, or None."""
    if type(a) is not type(b):
        return path
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}"
            d = first_difference(a[k], b[k], f"{path}.{k}" if k not in ("list", "tuple", "items", "attrs", "dict", "set", "frozenset") else path)
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path} (length {len(a)} != {len(b)})"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else path


def preview(c: Canon, limit: int = 160) -> str:
    """A compact, human-readable rendering of a canonical value."""
    text = _pretty(c)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pretty(c: Canon) -> str:
    if c is None:
        return "None"
    if isinstance(c, str):
        return repr(c)
    if isinstance(c, (int, float)):
        return str(c)
    if isinstance(c, list):
        return "[" + ", ".join(_pretty(x) for x in c) + "]"
    if not isinstance(c, dict):
        return str(c)
    if "bool" in c:
        return str(c["bool"])
    if "float" in c:
        return str(c["float"])
    if "int" in c:
        return str(c["int"])
    if "list" in c:
        return "[" + ", ".join(_pretty(x) for x in c["list"]) + "]"
    if "tuple" in c:
        items = c["tuple"]
        return "(" + ", ".join(_pretty(x) for x in items) + ("," if len(items) == 1 else "") + ")"
    if "dict" in c:
        return "{" + ", ".join(f"{_pretty(k)}: {_pretty(v)}" for k, v in c["dict"]) + "}"
    if "set" in c or "frozenset" in c:
        return "{" + ", ".join(_pretty(x) for x in (c.get("set") or c.get("frozenset") or [])) + "}"
    if "obj" in c:
        name = str(c["obj"]).rsplit(".", 1)[-1]
        return f"{name}(" + ", ".join(f"{k}={_pretty(v)}" for k, v in c["attrs"].items()) + ")"
    if "exc" in c:
        return f"{str(c['exc']).rsplit('.', 1)[-1]}(" + ", ".join(_pretty(x) for x in c["args"]) + ")"
    if "repr" in c:
        return str(c["text"])
    if "bytes" in c:
        return repr(bytes.fromhex(c["bytes"]))
    if "enum" in c:
        return str(c["enum"]).rsplit(".", 2)[-2] + "." + str(c["enum"]).rsplit(".", 1)[-1]
    for k in ("fn", "type", "module", "namedtuple", "seq"):
        if k in c:
            return f"<{k} {c[k]}>" if k in ("fn", "type", "module") else f"{c[k]}{_pretty(c['items'])}"
    return _key(c)
