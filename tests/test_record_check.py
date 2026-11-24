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
    assert "generator function" in out  # lines() is reported, not silently skipped
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
