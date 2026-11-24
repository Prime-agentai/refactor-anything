from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from helpers import check_json, edit, make_project, ra, record


def test_record_then_check_passes(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    out = record(root, "textkit").stdout
    assert "textkit:slugify" in out
    assert "textkit:lines" in out  # a generator: recorded as it is consumed
    code, res = check_json(root)
    assert code == 0 and res["ok"], res
    assert res["functions"]["textkit:slugify"] == {"pass": 8}
    assert "textkit:pick" in res["unlocked"]  # random.choice: every case unstable
    assert "textkit:_helper" not in res["functions"]  # private: free to change
    lock = json.loads((root / ".refactor-anything" / "lock.json").read_text())
    assert lock["sources"]["textkit"]["file"] == "textkit.py"
    assert lock["functions"]["textkit:Counter.total"] == {}


def test_behaviour_preserving_refactor_passes(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    edit(root, "textkit.py", "    return [items[i:i + n] for i in range(0, len(items), n)]",
         "    out = []\n    i = 0\n    while i < len(items):\n        out.append(items[i:i + n])\n        i += n\n    return out")
    edit(root, "textkit.py", "def _helper(x):\n    return x + 1", "def _plus_one(x):\n    return 1 + x")
    edit(root, "textkit.py", "return _helper(x) * 3", "return _plus_one(x) * 3")
    code, res = check_json(root)
    assert code == 0 and res["ok"], res
    assert res["changed_modules"] == ["textkit"]


def test_output_change_is_caught(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    edit(root, "textkit.py", "range(0, len(items), n)", "range(0, len(items) - 1, n)")
    p = ra(root, "check")
    assert p.returncode == 1
    assert "DIFF textkit:chunk" in p.stdout
    assert "expected [[0, 1, 2], [3, 4, 5], [6]], got [[0, 1, 2], [3, 4, 5]]" in p.stdout


def test_exception_change_is_caught(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    edit(root, "textkit.py", 'raise ValueError("n must be positive")', 'raise ValueError("n must be > 0")')
    code, res = check_json(root)
    assert code == 1
    (f,) = res["failures"]
    assert f["diff"] == "exception" and "n must be > 0" in f["actual"]


def test_in_place_mutation_is_behaviour(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    edit(root, "textkit.py", "    items.sort()\n", "    items = sorted(items)\n")
    code, res = check_json(root)
    assert code == 1
    (f,) = res["failures"]
    assert f["key"] == "textkit:sort_in_place" and f["diff"] == "mutation"


def test_method_state_change_is_caught(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    edit(root, "textkit.py", "self.counts.get(word, 0) + 1", "self.counts.get(word, 1)")
    code, res = check_json(root)
    assert code == 1
    keys = {f["key"] for f in res["failures"]}
    assert {"textkit:Counter.add", "textkit:Counter.of"} <= keys


def test_removed_function_and_class(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    edit(root, "textkit.py", "def slugify(", "def make_slug(")
    edit(root, "textkit.py", "class Counter:", "class Tally:")
    code, res = check_json(root)
    assert code == 1
    statuses = {(f["key"], f["status"]) for f in res["failures"]}
    assert ("textkit:slugify", "missing") in statuses
    assert ("textkit:Counter.norm", "missing") in statuses
    assert ("textkit:Counter.add", "load-error") in statuses  # recorded self is a textkit.Counter


def test_module_that_no_longer_imports(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    edit(root, "textkit.py", "import re\n", "import re\nimport definitely_not_a_module\n")
    p = ra(root, "check")
    assert p.returncode == 2
    assert "does not import" in p.stdout


def test_select_names_and_private(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit:slugify,Counter.add")
    _, res = check_json(root)
    assert set(res["functions"]) == {"textkit:slugify", "textkit:Counter.add"}
    root2 = make_project(tmp_path / "p2")
    record(root2, "textkit", flags=("--private",))
    _, res2 = check_json(root2)
    assert "textkit:_helper" in res2["functions"]


def test_cap_and_append_and_dedupe(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit:slugify", flags=("--cap", "3"))
    _, res = check_json(root)
    assert res["total"] == 3
    p = record(root, "textkit:chunk")  # appends a second target to the same lock
    assert "textkit:chunk" in p.stdout
    _, res = check_json(root)
    assert set(res["functions"]) == {"textkit:slugify", "textkit:chunk"}
    assert res["functions"]["textkit:slugify"] == {"pass": 8}  # same inputs again are not duplicated


def test_append_refused_after_source_change(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    edit(root, "textkit.py", "range(0, len(items), n)", "range(0, len(items) - 1, n)")
    p = ra(root, "record", "textkit", "--", sys.executable, "run_app.py")
    assert p.returncode == 2 and "no longer match" in p.stderr
    p = ra(root, "record", "textkit", "--fresh", "--", sys.executable, "run_app.py")
    assert p.returncode == 0
    assert check_json(root)[0] == 0


def test_append_allowed_after_safe_refactor(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit:chunk", cmd=("run_app.py",))
    edit(root, "textkit.py", "for i in range(0, len(items), n)]", "for i in range(0, len(items), n)]  # same")
    (root / "more.py").write_text("import textkit\ntextkit.chunk([1, 2, 3, 4, 5], 4)\n")
    record(root, "textkit:chunk", cmd=("more.py",))
    code, res = check_json(root)
    assert code == 0 and res["functions"]["textkit:chunk"] == {"pass": 5}
    assert res["changed_modules"] == []  # the lock now describes today's source


def test_tamper_is_detected(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    cases = root / ".refactor-anything" / "cases" / "textkit.jsonl"
    lines = cases.read_text().splitlines()
    cases.write_text("\n".join(lines[1:]) + "\n")
    p = ra(root, "check")
    assert p.returncode == 3 and "TAMPERED" in p.stdout


def test_record_usage_errors(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    p = ra(root, "record", "textkit")
    assert p.returncode == 2 and "after --" in p.stderr
    p = ra(root, "record", "nosuchmodule", "--", sys.executable, "run_app.py")
    assert p.returncode == 2 and "never imported nosuchmodule" in p.stderr
    p = ra(root, "check")
    assert p.returncode == 2 and "no lock" in p.stderr


def test_unpicklable_input_is_reported(tmp_path: Path) -> None:
    root = make_project(tmp_path, {
        "m.py": "def apply(fn, x):\n    return fn(x)\n\ndef double(x):\n    return 2 * x\n",
        "run.py": "import m\nm.apply(lambda v: v + 1, 1)\nm.double(4)\n",
    })
    out = record(root, "m", cmd=("run.py",)).stdout
    assert "unpicklable" in out
    _, res = check_json(root)
    assert res["functions"] == {"m:double": {"pass": 1}}


def test_child_processes_src_layout_and_existing_sitecustomize(tmp_path: Path) -> None:
    root = make_project(tmp_path, {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "def area(w, h):\n    return w * h\n",
        "site_extra/sitecustomize.py": "import os\nos.environ['THEIR_SITECUSTOMIZE_RAN'] = '1'\n",
        "child.py": "import os, pkg.core\nassert os.environ.get('THEIR_SITECUSTOMIZE_RAN') == '1'\npkg.core.area(2, 3)\n",
        "parent.py": "import subprocess, sys\nsubprocess.run([sys.executable, 'child.py'], check=True)\n",
    })
    sep = os.pathsep
    p = ra(root, "record", "pkg.core", "--", sys.executable, "parent.py",
           extra_env={"PYTHONPATH": f"{root / 'src'}{sep}{root / 'site_extra'}"})
    assert p.returncode == 0, p.stderr
    lock = json.loads((root / ".refactor-anything" / "lock.json").read_text())
    assert lock["sys_path"] == ["src"]
    assert lock["sources"]["pkg.core"]["file"] == "src/pkg/core.py"
    code, res = check_json(root)  # replays without PYTHONPATH: the lock knows where src is
    assert code == 0 and res["functions"] == {"pkg.core:area": {"pass": 1}}


def test_failing_command_still_records_with_note(tmp_path: Path) -> None:
    root = make_project(tmp_path, {"m.py": "def f(x):\n    return x\n", "run.py": "import m, sys\nm.f(1)\nsys.exit(3)\n"})
    p = ra(root, "record", "m", "--", sys.executable, "run.py")
    assert p.returncode == 0 and "exited 3" in p.stderr
    assert check_json(root)[0] == 0


def test_markdown_and_status(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    record(root, "textkit")
    md = ra(root, "check", "--markdown").stdout
    assert "behaviour unchanged" in md and "| `textkit:slugify` | 8/8 |" in md
    st = ra(root, "status").stdout
    assert "changed since recording: none" in st and "textkit:lines" in st


GEN = '''\
def evens(limit):
    n = 0
    while n < limit:
        yield n
        n += 2


def risky(items):
    for x in items:
        if x < 0:
            raise ValueError("negative")
        yield x * 10


def echo():
    got = yield "ready"
    while got is not None:
        got = yield got.upper()
'''

GEN_RUN = '''\
import itertools, gen
list(gen.evens(7))
list(itertools.islice(gen.evens(100), 3))   # partly consumed: only these 3 are locked
try:
    list(gen.risky([1, -1, 2]))
except ValueError:
    pass
e = gen.echo()
next(e); e.send("a"); e.send("b")
'''


def test_generators_are_recorded_lazily(tmp_path: Path) -> None:
    root = make_project(tmp_path, {"gen.py": GEN, "run.py": GEN_RUN})
    record(root, "gen", cmd=("run.py",))
    code, res = check_json(root)
    assert code == 0, res
    assert res["functions"] == {"gen:evens": {"pass": 2}, "gen:risky": {"pass": 1}, "gen:echo": {"pass": 1}}
    # a list-returning rewrite is fine: callers iterate the same items
    edit(root, "gen.py", "def evens(limit):\n    n = 0\n    while n < limit:\n        yield n\n        n += 2\n",
         "def evens(limit):\n    return iter(range(0, limit, 2))\n")
    assert check_json(root)[0] == 0


def test_generator_changes_are_caught(tmp_path: Path) -> None:
    root = make_project(tmp_path, {"gen.py": GEN, "run.py": GEN_RUN})
    record(root, "gen", cmd=("run.py",))
    edit(root, "gen.py", "    while n < limit:", "    while n <= limit:")
    edit(root, "gen.py", 'raise ValueError("negative")', "continue")
    code, res = check_json(root)
    assert code == 1
    by_key = {f["key"]: f for f in res["failures"]}
    assert set(by_key) == {"gen:risky"}  # evens(7) still stops at 6: `<=` is invisible with this input
    assert by_key["gen:risky"]["diff"] == "exception"
    edit(root, "gen.py", "    while n <= limit:", "    while n < limit + 2:")
    code, res = check_json(root)
    f = next(f for f in res["failures"] if f["key"] == "gen:evens")
    assert f["expected"] == "stopped after 4 items" and f["actual"] == "kept yielding after 4 items"


def test_functions_returning_generators_and_iterators(tmp_path: Path) -> None:
    root = make_project(tmp_path, {
        "it.py": "def gen_of(xs):\n    return (x * 2 for x in xs)\n\n\ndef map_of(xs):\n    return map(str, xs)\n\n\n"
                 "def pairs(a, b):\n    return zip(a, b, strict=True)\n",
        "run.py": "import it\nlist(it.gen_of([1, 2]))\nlist(it.map_of([1, 2]))\ntry:\n    list(it.pairs([1, 2], [3]))\n"
                  "except ValueError:\n    pass\n",
    })
    out = record(root, "it", cmd=("run.py",)).stdout
    assert "only its type is locked" not in out
    assert check_json(root)[0] == 0
    edit(root, "it.py", "strict=True", "strict=False")
    code, res = check_json(root)
    assert code == 1 and res["failures"][0]["diff"] == "exception"  # zip raised after 1 item; now it just stops
    edit(root, "it.py", "strict=False", "strict=True")
    edit(root, "it.py", "return (x * 2 for x in xs)", "return (x * 3 for x in xs)")
    edit(root, "it.py", "return map(str, xs)", "return map(repr, [str(x) for x in xs])")
    code, res = check_json(root)
    assert code == 1
    by_key = {f["key"]: f for f in res["failures"]}
    assert by_key["it:gen_of"]["expected"] == "yielded [2, 4]"
    # the map's items were filled in by re-running the original after recording
    assert by_key["it:map_of"]["expected"] == "yielded ['1', '2']" and by_key["it:map_of"]["actual"] == "yielded [\"'1'\", \"'2'\"]"
