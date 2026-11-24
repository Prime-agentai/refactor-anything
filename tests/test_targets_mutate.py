from __future__ import annotations

import ast

import pytest

from refactor_anything import mutate
from refactor_anything.targets import Target, TargetError, merge, parse

SRC = '''
def f(a, b):
    """doc string stays."""
    if a < b and not a:
        return a + 1
    return f"{a}-{b}"


class K:
    def m(self, xs):
        for x in xs:
            if x == 0:
                break
        xs.sort()
        return True
'''


def test_parse_targets() -> None:
    assert parse("pkg.mod") == Target("pkg.mod")
    assert parse("pkg.mod:f, K.m") == Target("pkg.mod", ("f", "K.m"))
    with pytest.raises(TargetError):
        parse("pkg/mod.py")
    with pytest.raises(TargetError):
        parse("__main__")
    with pytest.raises(TargetError):
        parse("m:a.b.c")


def test_merge_targets() -> None:
    out = merge([Target("a", ("f",))], [Target("a", ("g",)), Target("b")])
    assert out == [Target("a", ("f", "g")), Target("b")]
    assert merge([Target("a", ("f",))], [Target("a")]) == [Target("a")]


def test_mutants_are_valid_single_changes() -> None:
    muts = mutate.mutants(SRC)
    base = mutate.baseline(SRC)
    descriptions = {m.description for m in muts}
    assert "`<` -> `<=`" in descriptions
    assert "`and` -> `or`" in descriptions
    assert "drop `not`" in descriptions
    assert "`+` -> `-`" in descriptions
    assert "`break` -> `continue`" in descriptions
    assert "drop the `.sort(...)` call" in descriptions
    assert "`True` -> `False`" in descriptions
    for m in muts:
        ast.parse(m.source)
        assert m.source != base
        assert "doc string stays." in m.source
    assert {m.function for m in muts} == {"f", "K.m"}
    # f-string pieces are not mutated on their own
    assert not any("'-XX'" in m.source or "-XX" in m.source for m in muts)


def test_mutant_cap_and_filter() -> None:
    assert len(mutate.mutants(SRC, per_function=2)) == 4
    assert {m.function for m in mutate.mutants(SRC, functions={"K.m"})} == {"K.m"}
