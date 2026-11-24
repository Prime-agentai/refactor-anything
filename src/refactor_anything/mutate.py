"""Small AST mutants, to measure how much of a function's behaviour the recording pins down.

Each mutant changes one thing (``<`` to ``<=``, ``+`` to ``-``, ``and`` to ``or``, a
number by one, a return value to ``None``, ...) inside one function or method of a
locked module. A mutant the recorded cases notice is *killed*. A *surviving* mutant is
a line where an agent could change behaviour and the lock would not see it: record
more inputs that reach it, or confirm the change is harmless (an equivalent mutant).
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from typing import Callable

_CMP_SWAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}
_BIN_SWAP: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Add, ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mult, ast.Mod: ast.FloorDiv, ast.Pow: ast.Mult,
}
_SYM = {
    ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
    ast.In: "in", ast.NotIn: "not in", ast.Is: "is", ast.IsNot: "is not",
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
}


@dataclass(frozen=True)
class Mutant:
    function: str  # qualname of the enclosing function, e.g. "slugify" or "Counter.add"
    line: int
    col: int
    description: str
    source: str  # the whole mutated module


@dataclass
class _Site:
    function: str
    node: ast.AST
    describe: str
    apply: Callable[[ast.AST], ast.AST | None]  # mutate a deep copy; return a replacement node or None


def functions_in(tree: ast.Module) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Top-level functions and methods of top-level classes, with their qualnames."""
    out: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append((f"{node.name}.{item.name}", item))
    return out


def _sites(qual: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[_Site]:
    sites: list[_Site] = []
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]  # leave the docstring alone
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.JoinedStr):  # f-string pieces cannot be mutated on their own
                for part in ast.walk(node):
                    part._ra_fstring = True  # type: ignore[attr-defined]
        for node in ast.walk(stmt):
            sites.extend(_node_sites(qual, node))
    return sites


def _node_sites(qual: str, node: ast.AST) -> list[_Site]:
    out: list[_Site] = []
    if isinstance(node, ast.Compare):
        for i, op in enumerate(node.ops):
            swap = _CMP_SWAP.get(type(op))
            if swap is not None:
                def ap(n: ast.AST, i: int = i, swap: type[ast.cmpop] = swap) -> None:
                    assert isinstance(n, ast.Compare)
                    n.ops[i] = swap()
                out.append(_Site(qual, node, f"`{_SYM[type(op)]}` -> `{_SYM[swap]}`", ap))
    elif isinstance(node, (ast.BinOp, ast.AugAssign)):
        bswap = _BIN_SWAP.get(type(node.op))
        if bswap is not None:
            def ap2(n: ast.AST, bswap: type[ast.operator] = bswap) -> None:
                assert isinstance(n, (ast.BinOp, ast.AugAssign))
                n.op = bswap()
            out.append(_Site(qual, node, f"`{_SYM[type(node.op)]}` -> `{_SYM[bswap]}`"
                             + (" (augmented)" if isinstance(node, ast.AugAssign) else ""), ap2))
    elif isinstance(node, ast.BoolOp):
        other = ast.Or if isinstance(node.op, ast.And) else ast.And
        def ap3(n: ast.AST, other: type[ast.boolop] = other) -> None:
            assert isinstance(n, ast.BoolOp)
            n.op = other()
        out.append(_Site(qual, node, "`and` -> `or`" if other is ast.Or else "`or` -> `and`", ap3))
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        out.append(_Site(qual, node, "drop `not`", lambda n: n.operand if isinstance(n, ast.UnaryOp) else None))
    elif isinstance(node, ast.If):
        def ap4(n: ast.AST) -> None:
            assert isinstance(n, ast.If)
            n.test = ast.UnaryOp(op=ast.Not(), operand=n.test)
        out.append(_Site(qual, node, "negate the `if` condition", ap4))
    elif isinstance(node, ast.Constant) and type(node.value) is bool:
        out.append(_const(qual, node, not node.value, f"`{node.value}` -> `{not node.value}`"))
    elif isinstance(node, ast.Constant) and type(node.value) is int:
        out.append(_const(qual, node, node.value + 1, f"`{node.value}` -> `{node.value + 1}`"))
    elif isinstance(node, ast.Constant) and isinstance(node.value, str) and not getattr(node, "_ra_fstring", False):
        new = node.value + "XX" if node.value else "XX"
        out.append(_const(qual, node, new, f"string {_short(node.value)} -> {_short(new)}"))
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and not node.keywords and len(node.args) <= 2:
        out.append(_Site(qual, node, f"drop the `.{node.func.attr}(...)` call",
                         lambda n: n.func.value if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) else None))
    elif isinstance(node, ast.Return) and node.value is not None and not (isinstance(node.value, ast.Constant) and node.value.value is None):
        def ap5(n: ast.AST) -> None:
            assert isinstance(n, ast.Return)
            n.value = ast.Constant(value=None)
        out.append(_Site(qual, node, "return `None` instead", ap5))
    elif isinstance(node, ast.Break):
        out.append(_Site(qual, node, "`break` -> `continue`", lambda n: ast.Continue()))
    elif isinstance(node, ast.Continue):
        out.append(_Site(qual, node, "`continue` -> `break`", lambda n: ast.Break()))
    return out


def _const(qual: str, node: ast.AST, value: bool | int | str, describe: str) -> _Site:
    def apply(_n: ast.AST) -> ast.AST:
        return ast.Constant(value=value)
    return _Site(qual, node, describe, apply)


def _short(text: str) -> str:
    return repr(text if len(text) <= 24 else text[:23] + "…")


def mutants(source: str, *, functions: set[str] | None = None, per_function: int = 25) -> list[Mutant]:
    """Every mutant of ``source`` (capped per function, spread evenly over the body)."""
    tree = ast.parse(source)
    found: list[Mutant] = []
    for qual, fn in functions_in(tree):
        if functions is not None and qual not in functions:
            continue
        sites = _sites(qual, fn)
        if len(sites) > per_function:
            step = len(sites) / per_function
            sites = [sites[int(i * step)] for i in range(per_function)]
        for site in sites:
            found.append(_make(tree, site))
    return found


def _make(tree: ast.Module, site: _Site) -> Mutant:
    # Find the same node in a deep copy by walking both trees in lockstep.
    clone = copy.deepcopy(tree)
    target: ast.AST | None = None
    parent_of: dict[int, tuple[ast.AST, str, int | None]] = {}
    for a, b in zip(ast.walk(tree), ast.walk(clone)):
        for fname, value in ast.iter_fields(b):
            if isinstance(value, list):
                for idx, child in enumerate(value):
                    if isinstance(child, ast.AST):
                        parent_of[id(child)] = (b, fname, idx)
            elif isinstance(value, ast.AST):
                parent_of[id(value)] = (b, fname, None)
        if a is site.node:
            target = b
    assert target is not None
    replacement = site.apply(target)
    if replacement is not None and replacement is not target:
        parent, fname, pos = parent_of[id(target)]
        if pos is None:
            setattr(parent, fname, replacement)
        else:
            getattr(parent, fname)[pos] = replacement
    ast.fix_missing_locations(clone)
    line = getattr(site.node, "lineno", 0)
    col = getattr(site.node, "col_offset", 0)
    return Mutant(site.function, line, col, site.describe, ast.unparse(clone))


def baseline(source: str) -> str:
    """The unmutated module through the same parse/unparse path the mutants take."""
    return ast.unparse(ast.parse(source))
