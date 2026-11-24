"""Runs inside the replay process: calls the current code with each recorded input and compares.

Invoked as ``python <boot>/refactor_anything/replayer.py JOB RESULT`` with the project's
interpreter. The job is a JSON file; the result is written to a JSON file, never to
stdout, because the code under test may print.
"""

from __future__ import annotations

import base64
import contextlib
import importlib
import io
import json
import os
import pickle
import sys
import time
import types
from typing import Any

if __package__ in (None, ""):  # run as a script: make the sibling modules importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "refactor_anything"

from refactor_anything import canon  # noqa: E402
from refactor_anything.recorder import exc_info  # noqa: E402
from refactor_anything.targets import split_key  # noqa: E402

_MISSING = object()


def resolve(module: str, qual: str) -> Any:
    obj: Any = importlib.import_module(module)
    for part in qual.split("."):
        obj = obj.__dict__.get(part, _MISSING) if isinstance(obj, (types.ModuleType, type)) else getattr(obj, part, _MISSING)
        if obj is _MISSING:
            return _MISSING
    return obj


def _call(module: str, qual: str, kind: str, args: Any, kwargs: Any) -> Any:
    if kind == "function":
        fn = resolve(module, qual)
        if fn is _MISSING or not callable(fn):
            raise _Missing(f"{module}.{qual} no longer exists")
        return fn(*args, **kwargs)
    cls_name, _, attr = qual.rpartition(".")
    cls = resolve(module, cls_name)
    if cls is _MISSING or not isinstance(cls, type):
        raise _Missing(f"class {module}.{cls_name} no longer exists")
    raw = None
    for klass in cls.__mro__:
        if attr in klass.__dict__:
            raw = klass.__dict__[attr]
            break
    if raw is None:
        raise _Missing(f"{module}.{qual} no longer exists")
    if kind == "property":
        if not isinstance(raw, property) or raw.fget is None:
            raise _Missing(f"{module}.{qual} is no longer a property")
        return raw.fget(*args)
    return getattr(cls, attr)(*args, **kwargs)


class _Missing(Exception):
    pass


def _install_mutant(m: dict[str, Any]) -> None:
    name = m["module"]
    mod = types.ModuleType(name)
    mod.__file__ = m["filename"]
    if m.get("package"):
        mod.__path__ = [os.path.dirname(m["filename"])]
        mod.__package__ = name
    else:
        mod.__package__ = name.rpartition(".")[0]
    parent, _, leaf = name.rpartition(".")
    # Import the package (and with it the original module) the normal way first, so modules that
    # import from this one during package start-up see a complete module, not an empty mutant.
    parent_mod = importlib.import_module(parent) if parent else None
    sys.modules[name] = mod
    exec(compile(m["source"], m["filename"], "exec"), mod.__dict__)
    if parent_mod is not None:
        setattr(parent_mod, leaf, mod)


FILL_CAP = 1000


def fill_case(case: dict[str, Any]) -> dict[str, Any]:
    """Re-run the original on a recorded input and consume the iterator it returns (up to FILL_CAP)."""
    module, qual = split_key(case["key"])
    res: dict[str, Any] = {"key": case["key"], "id": case["id"]}
    try:
        stored, kwargs = pickle.loads(base64.b64decode(case["args"]))
        args = list(stored)
        items: list[Any] = []
        end = "open"
        raised: BaseException | None = None
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            it = iter(_call(module, qual, case["kind"], args, kwargs))
            try:
                for _ in range(FILL_CAP):
                    items.append(canon.canon(next(it)))
            except StopIteration:
                end = "stop"
            except Exception as exc:  # raising part-way through is behaviour too
                raised = exc
        res.update(status="filled", gen={"items": items, "end": end}, after=canon.canon([args, kwargs]))
        if raised is not None:
            res["exc"] = exc_info(raised)
    except Exception as exc:  # the iterator raised while consumed: leave the case type-only
        res.update(status="unfilled", detail=f"{type(exc).__name__}: {exc}")
    return res


def replay_case(case: dict[str, Any]) -> dict[str, Any]:
    module, qual = split_key(case["key"])
    res: dict[str, Any] = {"key": case["key"], "id": case["id"]}
    try:
        stored, kwargs = pickle.loads(base64.b64decode(case["args"]))
    except Exception as exc:
        res.update(status="load-error", detail=f"recorded input no longer loads: {type(exc).__name__}: {exc}")
        return res
    args = list(stored)
    out: Any = None
    err: BaseException | None = None
    sink = io.StringIO()
    want_gen = case.get("gen")
    got_gen: dict[str, Any] | None = None
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            out = _call(module, qual, case["kind"], args, kwargs)
            if want_gen is not None:
                got_gen = {"items": [], "end": "open"}
                try:
                    it = iter(out)
                except TypeError:
                    res.update(status="fail", diff="output", expected="an iterator", actual="returned " + canon.preview(canon.canon(out)))
                    return res
                sends = pickle.loads(base64.b64decode(want_gen["sends"])) if want_gen.get("sends") else []

                def step(i: int) -> Any:
                    value = sends[i] if i < len(sends) else None
                    if value is None or not hasattr(it, "send"):
                        return next(it)
                    return it.send(value)

                try:
                    for i in range(len(want_gen["items"])):
                        got_gen["items"].append(canon.canon(step(i)))
                    if want_gen["end"] == "stop" or case.get("exc"):
                        step(len(want_gen["items"]))
                        got_gen["end"] = "more"  # it yielded beyond where the original stopped
                except StopIteration:
                    got_gen["end"] = "stop"
                finally:
                    close = getattr(it, "close", None)
                    if callable(close):
                        close()
    except _Missing as exc:
        res.update(status="missing", detail=str(exc))
        return res
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # SystemExit is behaviour too
        err = exc
    got_out: Any = None
    try:
        got_after = canon.canon([args, kwargs])
        if err is None and want_gen is None:
            got_out = canon.canon(out)
    except Exception as exc:
        res.update(status="fail", diff="output", detail=f"result could not be encoded: {type(exc).__name__}: {exc}")
        return res

    want_exc = case.get("exc")
    if want_gen is not None and got_gen is not None:
        if got_gen["items"] != want_gen["items"]:
            res.update(status="fail", diff="output", path=canon.first_difference(want_gen["items"], got_gen["items"]),
                       expected="yielded " + _items_text(want_gen["items"]), actual="yielded " + _items_text(got_gen["items"]))
            return res
        if err is None and want_exc is None and got_gen["end"] != want_gen["end"]:
            res.update(status="fail", diff="output", expected=_end_text(want_gen), actual=_end_text(got_gen))
            return res
    if err is not None or want_exc is not None:
        got_exc = exc_info(err) if err is not None else None
        if got_exc != want_exc:
            res.update(status="fail", diff="exception",
                       expected=_exc_text(want_exc, case.get("out")), actual=_exc_text(got_exc, got_out) if want_gen is None or got_exc else _end_text(got_gen or {"items": [], "end": "open"}))
            return res
    elif want_gen is None and got_out != case.get("out"):
        res.update(status="fail", diff="output", path=canon.first_difference(case.get("out"), got_out),
                   expected=canon.preview(case.get("out")), actual=canon.preview(got_out))
        return res
    if got_after != case.get("after"):
        res.update(status="fail", diff="mutation", path=canon.first_difference(case.get("after"), got_after),
                   expected=canon.preview(case.get("after")), actual=canon.preview(got_after))
        return res
    res["status"] = "pass"
    return res


def _items_text(items: list[Any]) -> str:
    return canon.preview({"list": items})


def _end_text(g: dict[str, Any]) -> str:
    n = len(g["items"])
    return {"stop": f"stopped after {n} items", "more": f"kept yielding after {n} items",
            "open": f"{n} items consumed"}[g["end"]]


def _exc_text(exc: dict[str, str] | None, out: Any) -> str:
    if exc is None:
        return "returned " + canon.preview(out)
    return f"raised {exc['type'].rsplit('.', 1)[-1]}: {exc['msg'][:200]}"


def _limit_memory(mb: int) -> None:
    """A mutant that loops while allocating must die of MemoryError, not take the machine with it."""
    try:
        import resource
    except ImportError:  # Windows: the timeout alone bounds it
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        want = mb * 1024 * 1024
        if hard != resource.RLIM_INFINITY:
            want = min(want, hard)
        if soft == resource.RLIM_INFINITY or soft > want:
            resource.setrlimit(resource.RLIMIT_AS, (want, hard))
    except (ValueError, OSError):  # macOS does not enforce RLIMIT_AS; fine
        pass


def run(job: dict[str, Any]) -> dict[str, Any]:
    for p in reversed(job.get("sys_path", [])):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.chdir(job.get("root", os.getcwd()))
    started = time.perf_counter()
    if job.get("mutant"):
        _limit_memory(int(job.get("memory_mb") or 2048))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                _install_mutant(job["mutant"])
        except Exception as exc:
            return {"results": [], "error": f"mutant did not import: {type(exc).__name__}: {exc}", "seconds": 0.0}
    for module in dict.fromkeys(split_key(c["key"])[0] for c in job["cases"]):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                importlib.import_module(module)
        except BaseException as exc:
            return {"results": [], "error": f"module {module} does not import: {type(exc).__name__}: {exc}", "seconds": 0.0}
    results = [fill_case(c) if job.get("fill") else replay_case(c) for c in job["cases"]]
    return {"results": results, "seconds": round(time.perf_counter() - started, 4)}


def main(argv: list[str]) -> int:
    job_path, result_path = argv[1], argv[2]
    with open(job_path, encoding="utf-8") as fh:
        job = json.load(fh)
    try:
        out = run(job)
    except BaseException as exc:  # e.g. the module itself fails to import
        out = {"results": [], "error": f"{type(exc).__name__}: {exc}"}
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
