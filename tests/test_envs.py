from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from refactor_anything import envs


def test_not_a_venv_injects_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)
    assert envs.injected_bin() is None
    assert envs.user_path("a" + os.pathsep + "b") == "a" + os.pathsep + "b"


def test_runner_venv_is_removed_from_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tool_env = tmp_path / "uv-archive"
    (tool_env / "bin").mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(tool_env))
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(sys, "executable", str(tool_env / "bin" / "python"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "users-own-venv"))
    path = os.pathsep.join([str(tool_env / "bin"), str(tmp_path / "users-own-venv" / "bin"), "/usr/bin"])
    assert envs.user_path(path) == os.pathsep.join([str(tmp_path / "users-own-venv" / "bin"), "/usr/bin"])


def test_activated_venv_is_the_users(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path))
    assert envs.injected_bin() is None
