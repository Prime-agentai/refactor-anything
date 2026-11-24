from __future__ import annotations

import dataclasses
import decimal
import enum
import pathlib
from collections import OrderedDict, namedtuple

from refactor_anything.canon import canon, first_difference, preview


class Plain:
    def __init__(self, a: object, b: object) -> None:
        self.a = a
        self.b = b


class Slotted:
    __slots__ = ("x", "y")

    def __init__(self) -> None:
        self.x = 1


@dataclasses.dataclass
class DC:
    name: str
    tags: list[str]


class Color(enum.Enum):
    RED = 1


Point = namedtuple("Point", "x y")


def test_python_equality_rules() -> None:
    assert canon({"a": 1, "b": 2}) == canon({"b": 2, "a": 1})
    assert canon({3, 1, 2}) == canon({2, 3, 1})
    assert canon([1, 2]) != canon((1, 2))
    assert canon(1) != canon(1.0)
    assert canon(True) != canon(1)
    assert canon(float("nan")) == canon(float("nan"))
    assert canon(-0.0) != canon(0.0)
    assert canon(2**80) == {"int": str(2**80)}
    assert canon(b"\x00a") == {"bytes": "0061"}


def test_objects_compare_by_class_and_state() -> None:
    assert canon(Plain(1, [2])) == canon(Plain(1, [2]))
    assert canon(Plain(1, [2])) != canon(Plain(1, [3]))
    assert canon(Slotted()) == {"obj": f"{__name__}.Slotted", "attrs": {"x": 1}}
    assert canon(DC("n", ["t"]))["attrs"] == {"name": "n", "tags": {"list": ["t"]}}
    assert canon(Color.RED) == {"enum": f"{__name__}.Color.RED"}
    assert canon(Point(1, 2))["namedtuple"].endswith("Point")
    assert canon(OrderedDict(a=1))["cls"] == "collections.OrderedDict"


def test_stdlib_objects_use_repr_without_addresses() -> None:
    assert canon(pathlib.PurePosixPath("/a/b")) == {"repr": "pathlib.PurePosixPath", "text": "PurePosixPath('/a/b')"}
    assert canon(decimal.Decimal("1.10"))["text"] == "Decimal('1.10')"
    p = pathlib.Path("x")
    str(p)  # fills internal caches; the encoding must not change
    assert canon(p) == canon(pathlib.Path("x"))

    class Opaque:
        __slots__ = ()

    assert " at 0x" not in canon(Opaque())["text"]


def test_cycles_and_exceptions() -> None:
    a: list[object] = [1]
    a.append(a)
    assert canon(a) == {"list": [1, {"cycle": 1}]}
    assert canon(ValueError("x", 2)) == {"exc": "builtins.ValueError", "args": ["x", 2]}


def test_first_difference_and_preview() -> None:
    x = canon({"k": [1, 2, 3]})
    y = canon({"k": [1, 2, 4]})
    assert first_difference(x, x) is None
    assert first_difference(x, y) == "$[0][1][2]"
    assert "length 3 != 2" in (first_difference(canon([1, 2, 3]), canon([1, 2])) or "")
    assert preview(canon(Plain(1, "s"))) == "Plain(a=1, b='s')"
    assert preview(canon((1,))) == "(1,)"
    assert preview(canon(list(range(1000))), 20).endswith("…")
