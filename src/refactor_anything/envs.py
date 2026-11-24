"""Tell the user's environment apart from the one a tool runner (uvx, uv tool run, pipx run) made for us."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _same(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def injected_bin() -> Path | None:
    """Our own venv's script directory if a tool runner put it on PATH, else None.

    When refactor-anything runs from a virtualenv the user has *not* activated, a ``python3`` on
    PATH that points into that venv was put there by the runner. The command being recorded must
    use the user's interpreter, not ours, and hooks must not point at a runner's temporary shim.
    """
    if sys.prefix == sys.base_prefix:
        return None  # not a venv: nothing was injected
    active = os.environ.get("VIRTUAL_ENV")
    if active and _same(active, sys.prefix):
        return None  # the user activated this venv: its python is theirs
    return Path(sys.executable).parent


def user_path(path: str | None = None) -> str:
    """PATH without the runner-injected directory."""
    raw = os.environ.get("PATH", "") if path is None else path
    inj = injected_bin()
    if inj is None:
        return raw
    return os.pathsep.join(p for p in raw.split(os.pathsep) if p and not _same(p, inj))
