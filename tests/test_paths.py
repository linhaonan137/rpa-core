"""路径治理回归：确认所有默认路径不依赖 cwd，且相对路径落点固定在项目根。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from engine import paths


def test_project_root_is_absolute_and_exists():
    assert paths.PROJECT_ROOT.is_absolute()
    assert paths.PROJECT_ROOT.is_dir()
    # 应当能找到 engine/ 和 nodes/，说明指向的是项目根而不是别处
    assert (paths.PROJECT_ROOT / "engine").is_dir()
    assert (paths.PROJECT_ROOT / "nodes").is_dir()


@pytest.mark.parametrize("attr", ["FLOWS_DIR", "DB_PATH", "ELEMENTS_PATH", "OUT_DIR"])
def test_defaults_are_absolute(attr):
    p = getattr(paths, attr)
    assert isinstance(p, Path)
    assert p.is_absolute(), f"{attr} 必须是绝对路径，否则依赖 cwd"


def test_defaults_under_project_root():
    assert paths.FLOWS_DIR.parent == paths.PROJECT_ROOT
    assert paths.DB_PATH.parent == paths.PROJECT_ROOT
    assert paths.ELEMENTS_PATH.parent == paths.PROJECT_ROOT


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("RPA_FLOWS", str(tmp_path / "custom_flows"))
    # 重新求值一次（模块级常量已固化，这里验证 _p 的逻辑）
    got = paths._p("RPA_FLOWS", "flows")
    assert got == (tmp_path / "custom_flows").resolve()


def test_no_cwd_dependency(monkeypatch, tmp_path):
    """切到别的目录后，默认路径不应该跟着变。"""
    before = paths.FLOWS_DIR
    monkeypatch.chdir(tmp_path)
    assert paths.FLOWS_DIR == before, "路径常量不应受 cwd 影响"


def test_files_abs_resolves_to_project_root():
    """nodes/files.py 的 _abs：相对路径落到项目根（不是 cwd）。"""
    from nodes import files

    got = files._abs(Path("out/x.txt"))
    assert got.is_absolute()
    assert got == paths.PROJECT_ROOT / "out" / "x.txt"


def test_files_abs_keeps_absolute_input(tmp_path):
    from nodes import files

    p = (tmp_path / "a.txt").resolve()
    assert files._abs(p) == p


def test_ensure_dirs_is_idempotent():
    paths.ensure_dirs()
    paths.ensure_dirs()  # 第二次不报错
    assert paths.FLOWS_DIR.is_dir()
    assert paths.OUT_DIR.is_dir()


# ---------------------------------------------------------------- 打包（frozen）


def test_bootstrap_noop_when_not_frozen():
    assert paths.bootstrap_frozen() == [], "源码运行不该有释放行为"


def test_bootstrap_releases_flows_to_exe_side(monkeypatch, tmp_path):
    """frozen 时把包内 flows 复制到项目根（exe 旁边）。"""
    import sys as _sys

    src = tmp_path / "_mei"
    (src / "flows").mkdir(parents=True)
    (src / "flows" / "a.json").write_text('{"name":"x"}', encoding="utf-8")
    root = tmp_path / "app"
    root.mkdir()

    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setattr(_sys, "_MEIPASS", str(src), raising=False)
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)

    assert paths.bootstrap_frozen() == ["flows"]
    assert (root / "flows" / "a.json").exists()


def test_bootstrap_does_not_overwrite_user_flows(monkeypatch, tmp_path):
    """用户改过的流程不能被包内版本覆盖。"""
    import sys as _sys

    src = tmp_path / "_mei"
    (src / "flows").mkdir(parents=True)
    (src / "flows" / "a.json").write_text("packed", encoding="utf-8")
    root = tmp_path / "app"
    (root / "flows").mkdir(parents=True)
    (root / "flows" / "b.json").write_text("mine", encoding="utf-8")

    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setattr(_sys, "_MEIPASS", str(src), raising=False)
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)

    assert paths.bootstrap_frozen() == [], "已有内容时不复制"
    assert (root / "flows" / "b.json").read_text(encoding="utf-8") == "mine"
    assert not (root / "flows" / "a.json").exists()


def test_bootstrap_handles_missing_meipass(monkeypatch, tmp_path):
    import sys as _sys

    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setattr(_sys, "_MEIPASS", "", raising=False)
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    assert paths.bootstrap_frozen() == []


def test_describe_contains_all_keys():
    d = paths.describe()
    for k in ("frozen", "PROJECT_ROOT", "MEIPASS", "FLOWS_DIR", "DB_PATH", "ELEMENTS_PATH", "OUT_DIR"):
        assert k in d
    assert d["PROJECT_ROOT"] == str(paths.PROJECT_ROOT)
