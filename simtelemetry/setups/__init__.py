"""调教文件：解析、换算成游戏里显示的数值、两份对比。目前支持 ACC。"""
from .acc import AccSetup, Change, SetupItem, diff_setups, load_setup

__all__ = ["AccSetup", "Change", "SetupItem", "diff_setups", "load_setup"]
