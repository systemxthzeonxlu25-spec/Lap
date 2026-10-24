"""各游戏调教共用的数据结构：一项设置（SetupItem）、一处差异（Change）和两份调教的对比。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class SetupItem:
    key: str                  # 稳定的标识，如 "tyre_pressure.lf"，对比时按它配对
    group: str                # 分组名（各游戏自己定，如 轮胎 / 定位 / 空力 …）
    label: str
    raw: Any                  # 文件里的原始值
    value: float | None       # 换算后的值；没有换算规则时为 None
    unit: str = ""
    derived: bool = False     # 游戏算出来的值 / 不可调的项，不是直接调的；对比时默认不列
    text: str | None = None   # 游戏自己写在文件里的显示值（LMU 的 .svm 有），有就直接用

    @property
    def display(self) -> str:
        if self.text is not None:
            return self.text
        v = self.raw if self.value is None else self.value
        if isinstance(v, float):
            v = f"{v:.{4 if self.derived else 2}f}".rstrip("0")
            v += "0" if v.endswith(".") else ""          # 26.0 而不是 26.
        return f"{v} {self.unit}".strip()


@dataclass(frozen=True, slots=True)
class Change:
    key: str
    group: str
    label: str
    unit: str
    derived: bool
    a: SetupItem | None       # None = 这一边没有这个字段
    b: SetupItem | None

    @property
    def clicks(self) -> int | None:
        """b 相对 a 改了几格（两边都是整数格数时）。"""
        if self.a and self.b and isinstance(self.a.raw, int) and isinstance(self.b.raw, int):
            return self.b.raw - self.a.raw
        return None


class Setup(Protocol):
    """各游戏的调教对象（AccSetup / LmuSetup）都提供这些。"""

    car: str

    @property
    def car_name(self) -> str: ...

    @property
    def has_conversion(self) -> bool: ...

    def items(self) -> list[SetupItem]: ...


# ---------------------------------------------------------------------------- 对比
def _same(a: SetupItem, b: SetupItem) -> bool:
    if isinstance(a.raw, float) or isinstance(b.raw, float):
        try:
            return abs(float(a.raw) - float(b.raw)) < 1e-6
        except (TypeError, ValueError):
            return a.raw == b.raw
    return a.raw == b.raw


def diff_setups(a: Setup, b: Setup, include_derived: bool = False) -> list[Change]:
    """按 key 配对，返回有差异的项（顺序同 a 的字段顺序，b 独有的放最后）。"""
    ia = {it.key: it for it in a.items()}
    ib = {it.key: it for it in b.items()}
    out: list[Change] = []
    for key in [*ia, *(k for k in ib if k not in ia)]:
        x, y = ia.get(key), ib.get(key)
        ref = x or y
        assert ref is not None
        if ref.derived and not include_derived:
            continue
        if x is not None and y is not None and _same(x, y):
            continue
        out.append(Change(key, ref.group, ref.label, ref.unit, ref.derived, x, y))
    return out
