"""调教文件：解析、换算成游戏里显示的数值、两份对比。支持 ACC（.json）和 LMU（.svm）。"""
from __future__ import annotations

import json

from .acc import AccSetup, load_setup
from .base import Change, Setup, SetupItem, diff_setups
from .lmu import LmuSetup, load_lmu_setup, parse_svm

__all__ = ["AccSetup", "Change", "LmuSetup", "Setup", "SetupItem", "diff_setups", "load_any", "load_lmu_setup",
           "load_setup", "parse_setup"]


def parse_setup(game: str, text: str) -> Setup:
    """按游戏解析调教文件的文本内容。"""
    if game == "acc":
        return load_setup(json.loads(text))
    if game == "lmu":
        return parse_svm(text)
    raise ValueError(f"不支持的游戏：{game}")


def load_any(path) -> Setup:
    """按扩展名猜游戏：.json = ACC，.svm = LMU。"""
    p = str(path).lower()
    if p.endswith(".svm"):
        return load_lmu_setup(path)
    return load_setup(path)
