"""之前出过问题的地方：未实现的游戏、存储参数、命令行参数。"""
from __future__ import annotations

import sqlite3
import time

import pytest

from simtelemetry import __main__ as cli
from simtelemetry import collector as collector_mod
from simtelemetry.adapters import create_adapter, is_implemented
from simtelemetry.collector import Collector
from simtelemetry.live import LiveHub
from simtelemetry.storage import SCHEMA_VERSION, Storage


def test_registry_knows_what_is_implemented():
    assert is_implemented("sim")
    assert not is_implemented("nope")
    with pytest.raises(ValueError):
        create_adapter("nope")


def test_auto_mode_survives_unimplemented_game(tmp_path, monkeypatch):
    # 检测到 ac 进程，但 ac 适配器还没写：以前采集线程会因为 ImportError 直接退出
    monkeypatch.setattr(collector_mod, "detect_running_games", lambda: ["ac"])
    monkeypatch.setattr(collector_mod, "is_implemented", lambda g: g == "sim")
    hub = LiveHub()
    col = Collector(Storage(tmp_path / "c.db"), hub, game="auto", retry_s=0.05)
    col.start()
    try:
        time.sleep(0.3)
        assert col._thread.is_alive()
        assert hub.snapshot()["state"] == "waiting"
    finally:
        col.stop()


def test_storage_rejects_memory_db():
    with pytest.raises(ValueError):
        Storage(":memory:")


def test_storage_refuses_newer_schema(tmp_path):
    path = tmp_path / "future.db"
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    con.close()
    with pytest.raises(RuntimeError):
        Storage(path)


@pytest.mark.parametrize("argv", [
    ["--no-web", "--no-collect"],
    ["--rate", "0"],
    ["--game", "ac", "--no-web"],
])
def test_cli_rejects_bad_args(argv, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "is_implemented", lambda g: g == "sim")   # 不依赖哪些游戏真的写完了
    with pytest.raises(SystemExit) as e:
        cli.main([*argv, "--db", str(tmp_path / "x.db")])
    assert e.value.code == 2


def test_dynamic_adapters_are_visible_to_pyinstaller():
    # 适配器是按名字动态导入的；打包成 exe 时只认 adapters/__init__.py 里 TYPE_CHECKING 那行 import，漏了 exe 里就没有这个游戏
    import re
    from pathlib import Path

    from simtelemetry.adapters import _REGISTRY

    src = (Path(__file__).resolve().parent.parent / "simtelemetry" / "adapters" / "__init__.py").read_text("utf-8")
    listed = set(re.search(r"if TYPE_CHECKING:.*?from \. import ([\w, ]+)", src, re.S).group(1).replace(" ", "").split(","))
    implemented = {mod.lstrip(".") for game, (mod, _) in _REGISTRY.items() if is_implemented(game)}
    assert implemented == listed
