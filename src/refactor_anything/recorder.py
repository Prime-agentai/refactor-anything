"""Runs inside the recorded process: wraps target functions and appends cases to a JSONL file.

Started from the generated ``sitecustomize`` when ``REFACTOR_ANYTHING_RECORD`` is set.
Every child Python process inherits the variable, so test runners that fork or spawn
workers are recorded too; each process writes its own file.

A case is the pickled input (taken *before* the call, so in-place changes do not leak
into it), the canonical return value or exception, and the canonical state of the
arguments *after* the call (so in-place mutation is part of the locked behaviour).
"""

from __future__ import annotations

import atexit
import base64
import functools
import hashlib
import importlib.abc
import importlib.machinery
import inspect
import json
import os
import pickle
import re
import sys
import threading
import types
from typing import Any, Callable

from . import canon
from .targets import is_public, key

ENV = "REFACTOR_ANYTHING_RECORD"
PICKLE_PROTOCOL = 4
_ADDR = re.compile(r" at 0x[0-9A-Fa-f]+")

_local = threading.local()


def exc_info(exc: BaseException) -> dict[str, str]:
    return {"type": canon.qualname(exc), "msg": _ADDR.sub("", _msg(exc))}


def _msg(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:
        return f"<unprintable {type(exc).__name__}>"


def call_kind(owner: Any, name: str) -> str | None:
    """How a callable found in a module or class body is invoked, or None if unsupported."""
    raw = owner.__dict__.get(name) if isinstance(owner, type) else getattr(owner, name, None)
    if isinstance(raw, staticmethod):
        return "staticmethod" if isinstance(owner, type) else None
    if isinstance(raw, classmethod):
        return "classmethod" if isinstance(owner, type) else None
    if isinstance(raw, property):
        return "property" if isinstance(owner, type) and raw.fget is not None else None
    if isinstance(raw, types.FunctionType):
        return "method" if isinstance(owner, type) else "function"
    return None


def unsupported_reason(fn: Any) -> str | None:
    if inspect.isasyncgenfunction(fn):
        return "async generator"
    if inspect.iscoroutinefunction(fn):
        return "async function"
    return None


def _is_lazy_iterator(x: Any) -> bool:
    try:
        return hasattr(x, "__next__") and iter(x) is x
    except Exception:
        return False


class _Writer:
    def __init__(self, out_dir: str, cap: int) -> None:
        self.out_dir = out_dir
        self.cap = cap
        self.lock = threading.Lock()
        self.fh: Any = None
        self.pid = -1
        self.seen: dict[str, set[str]] = {}
        self.calls: dict[str, int] = {}
        self.noted: set[tuple[str, str]] = set()

    def _file(self) -> Any:
        if self.fh is None or self.pid != os.getpid():  # a forked child gets its own file
            self.pid = os.getpid()
            os.makedirs(self.out_dir, exist_ok=True)
            path = os.path.join(self.out_dir, f"{self.pid}-{os.urandom(4).hex()}.jsonl")
            self.fh = open(path, "a", encoding="utf-8")
            self.seen = {}
            atexit.register(self.close)
        return self.fh

    def close(self) -> None:
        with self.lock:
            if self.fh is not None:
                self.fh.close()
                self.fh = None

    def write(self, rec: dict[str, Any]) -> None:
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            fh = self._file()
            fh.write(line + "\n")
            fh.flush()

    def note(self, k: str, what: str, detail: str) -> None:
        if (k, what) in self.noted:
            return
        self.noted.add((k, what))
        self.write({"meta": what, "key": k, "detail": detail[:300]})

    def wants(self, k: str) -> bool:
        self.calls[k] = self.calls.get(k, 0) + 1
        return len(self.seen.get(k, ())) < self.cap

    def add(self, k: str, cid: str) -> bool:
        ids = self.seen.setdefault(k, set())
        if cid in ids or len(ids) >= self.cap:
            return False
        ids.add(cid)
        return True


class Recorder:
    def __init__(self, config: dict[str, Any]) -> None:
        self.targets: dict[str, list[str] | None] = {t["module"]: t.get("names") for t in config["targets"]}
        self.private = bool(config.get("private", False))
        self.writer = _Writer(config["out"], int(config.get("cap", 200)))
        self.instrumented: set[str] = set()

    # -- selection ----------------------------------------------------------------
    def selected(self, module: str, qual: str) -> bool:
        names = self.targets.get(module)
        if names is not None:
            return qual in names
        leaf = qual.rsplit(".", 1)[-1]
        if leaf.startswith("__") and leaf.endswith("__"):
            return False
        return self.private or is_public(qual)

    # -- instrumentation ----------------------------------------------------------
    def instrument(self, mod: types.ModuleType) -> None:
        name = mod.__name__
        if name in self.instrumented or name not in self.targets:
            return
        self.instrumented.add(name)
        found: list[str] = []
        for attr, obj in list(vars(mod).items()):
            if isinstance(obj, types.FunctionType) and obj.__module__ == name:
                if self.selected(name, attr):
                    self._wrap_into(mod, attr, obj, "function", found)
            elif isinstance(obj, type) and obj.__module__ == name and obj.__qualname__ == attr:
                for mname in list(vars(obj)):
                    qual = f"{attr}.{mname}"
                    if not self.selected(name, qual):
                        continue
                    kind = call_kind(obj, mname)
                    if kind is None:
                        continue
                    self._wrap_method(obj, mname, kind, qual, name, found)
        self.writer.write({"meta": "instrumented", "module": name, "keys": found,
                           "file": getattr(mod, "__file__", None), "python": sys.executable})

    def _wrap_into(self, mod: types.ModuleType, attr: str, fn: Any, kind: str, found: list[str]) -> None:
        k = key(mod.__name__, attr)
        reason = unsupported_reason(fn)
        found.append(k)
        if reason:
            self.writer.note(k, "unsupported", reason)
            return
        setattr(mod, attr, self._wrapper(fn, k, kind))

    def _wrap_method(self, cls: type, mname: str, kind: str, qual: str, module: str, found: list[str]) -> None:
        k = key(module, qual)
        raw = cls.__dict__[mname]
        fn = raw.fget if kind == "property" else (raw.__func__ if kind in ("staticmethod", "classmethod") else raw)
        found.append(k)
        reason = unsupported_reason(fn)
        if reason:
            self.writer.note(k, "unsupported", reason)
            return
        wrapped = self._wrapper(fn, k, kind)
        if kind == "staticmethod":
            setattr(cls, mname, staticmethod(wrapped))
        elif kind == "classmethod":
            setattr(cls, mname, classmethod(wrapped))
        elif kind == "property":
            setattr(cls, mname, property(wrapped, raw.fset, raw.fdel, raw.__doc__))
        else:
            setattr(cls, mname, wrapped)

    def _wrapper(self, fn: Callable[..., Any], k: str, kind: str) -> Callable[..., Any]:
        writer = self.writer
        is_gen = inspect.isgeneratorfunction(fn)

        @functools.wraps(fn)
        def recorded(*args: Any, **kwargs: Any) -> Any:
            if getattr(_local, "busy", False) or not writer.wants(k):
                return fn(*args, **kwargs)
            # A classmethod's first argument is the class: replay supplies it from the lock.
            stored = args[1:] if kind == "classmethod" else args
            _local.busy = True
            try:
                try:
                    blob = pickle.dumps((stored, kwargs), protocol=PICKLE_PROTOCOL)
                except Exception as exc:
                    writer.note(k, "unpicklable", f"{type(exc).__name__}: {exc}")
                    blob = None
            finally:
                _local.busy = False
            if blob is None:
                return fn(*args, **kwargs)
            if is_gen:  # creating a generator runs none of its body: record lazily, as it is consumed
                return self._recording_gen(fn(*args, **kwargs), k, kind, blob, stored, kwargs)
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                self._save(k, kind, blob, stored, kwargs, None, exc)
                raise
            if isinstance(result, types.GeneratorType):  # a function that returns a generator: same as one
                return self._recording_gen(result, k, kind, blob, stored, kwargs)
            # A map/zip/islice/... cannot be consumed here without stealing it from the caller: the
            # items are filled in later by re-running the original on the same input (see engine.fill).
            self._save(k, kind, blob, stored, kwargs, result, None, fill=_is_lazy_iterator(result))
            return result

        recorded.__refactor_anything_original__ = fn  # type: ignore[attr-defined]
        return recorded

    def _recording_gen(self, gen: Any, k: str, kind: str, blob: bytes, args: Any, kwargs: Any) -> Any:
        """Yield what ``gen`` yields (forwarding send()), and record the items the caller consumed."""
        items: list[Any] = []
        sends: list[Any] = []
        end = "open"
        failure: BaseException | None = None
        sent: Any = None
        try:
            while True:
                sends.append(sent)
                try:
                    value = gen.send(sent) if sent is not None else next(gen)
                except StopIteration as stop:
                    end = "stop"
                    return stop.value
                _local.busy = True
                try:
                    items.append(canon.canon(value))
                finally:
                    _local.busy = False
                sent = yield value
        except GeneratorExit:
            close = getattr(gen, "close", None)
            if callable(close):
                close()
            raise
        except BaseException as exc:
            failure = exc
            raise
        finally:
            trace: dict[str, Any] = {"items": items, "end": end}
            if any(v is not None for v in sends):
                try:
                    trace["sends"] = base64.b64encode(pickle.dumps(sends, protocol=PICKLE_PROTOCOL)).decode("ascii")
                except Exception as exc:
                    self.writer.note(k, "unpicklable", f"value sent into the generator: {type(exc).__name__}: {exc}")
                    trace = {}
            if trace:
                self._save(k, kind, blob, args, kwargs, None, failure, gen_trace=trace)

    def _save(self, k: str, kind: str, blob: bytes, args: Any, kwargs: Any, result: Any, exc: BaseException | None,
              gen_trace: dict[str, Any] | None = None, fill: bool = False) -> None:
        if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
            return
        digest = hashlib.sha256(blob)
        if gen_trace is not None:  # the same input consumed to a different depth is a different case
            digest.update(f"|{len(gen_trace['items'])}|{gen_trace['end']}|{exc is not None}".encode())
        cid = digest.hexdigest()[:16]
        _local.busy = True
        try:
            with self.writer.lock:
                fresh = self.writer.add(k, cid)
            if not fresh:
                return
            rec: dict[str, Any] = {"key": k, "kind": kind, "id": cid,
                                   "args": base64.b64encode(blob).decode("ascii")}
            if gen_trace is not None:
                rec["gen"] = gen_trace
            elif exc is None:
                rec["out"] = canon.canon(result)
                if fill:
                    rec["fill"] = True
            if exc is not None:
                rec["exc"] = exc_info(exc)
            rec["after"] = canon.canon([list(args), kwargs])
            self.writer.write(rec)
        except Exception as err:  # recording must never change the program's behaviour
            self.writer.note(k, "record-error", f"{type(err).__name__}: {err}")
        finally:
            _local.busy = False


class _Loader(importlib.abc.Loader):
    def __init__(self, inner: Any, rec: Recorder) -> None:
        self.inner = inner
        self.rec = rec

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType | None:
        create = getattr(self.inner, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module: types.ModuleType) -> None:
        self.inner.exec_module(module)
        self.rec.instrument(module)

    def __getattr__(self, name: str) -> Any:  # get_source, get_code, ... for tools that ask
        return getattr(self.inner, name)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> importlib.machinery.ModuleSpec | None:
        if fullname not in self.rec.targets:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                    spec.loader = _Loader(spec.loader, self.rec)
                return spec
        return None


_active: Recorder | None = None


def start(config: dict[str, Any] | None = None) -> Recorder | None:
    """Install the import hook. Idempotent; a no-op when no config is given or set."""
    global _active
    if _active is not None:
        return _active
    if config is None:
        raw = os.environ.get(ENV)
        if not raw:
            return None
        config = json.loads(raw)
    rec = Recorder(config)
    sys.meta_path.insert(0, _Finder(rec))
    for name in list(rec.targets):
        mod = sys.modules.get(name)
        if mod is not None:
            rec.instrument(mod)
    _active = rec
    return rec
