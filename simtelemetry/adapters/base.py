"""游戏适配器的统一接口。新增游戏 = 新写一个 GameAdapter 子类并在 adapters/__init__.py 注册。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import SessionInfo, TelemetryFrame


class AdapterDisconnected(Exception):
    """游戏退出 / 数据源失效。采集循环会 close() 并回到“等待游戏”状态。"""


class GameAdapter(ABC):
    name: str = ""            # 'acc' / 'ac' / 'iracing' / 'sim'，写入数据库的 game 字段
    display_name: str = ""

    @abstractmethod
    def connect(self) -> bool:
        """尝试连接游戏。游戏没开时返回 False，不得抛异常、不得打日志刷屏。"""

    @abstractmethod
    def read(self) -> tuple[SessionInfo | None, TelemetryFrame] | None:
        """读取一帧。

        返回 None 表示暂时没有可用数据（例如还在菜单里）；
        SessionInfo 为 None 表示已连接但赛道/车型尚未就绪（此时不会记录）；
        数据源失效时抛 AdapterDisconnected。
        """

    @abstractmethod
    def close(self) -> None:
        """释放资源。可重复调用。"""

    def is_running(self) -> bool | None:
        """游戏进程是否在运行（自动检测用）。None = 不知道，交给 connect() 判断。"""
        return None
