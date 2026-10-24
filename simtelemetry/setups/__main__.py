"""命令行查看 / 对比 ACC 调教。

    python -m simtelemetry.setups show a.json
    python -m simtelemetry.setups diff a.json b.json [--derived]
"""
from __future__ import annotations

import argparse
import sys
import unicodedata

from .acc import AccSetup, diff_setups, load_setup


def _pad(text: str, width: int, right: bool = False) -> str:
    """按显示宽度补空格（中文占两格）。"""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    fill = " " * max(0, width - w)
    return fill + text if right else text + fill


def _header(s: AccSetup) -> str:
    note = "" if s.has_conversion else "（没有这辆车的换算表，只显示原始值）"
    return f"{s.path.name if s.path else '?'}：{s.car_name}{note}"


def show(s: AccSetup) -> None:
    print(_header(s))
    group = None
    for it in s.items():
        if it.group != group:
            group = it.group
            print(f"\n[{group}]")
        raw = "" if it.value is None else f"  （第 {it.raw} 格）"
        print(f"  {_pad(it.label, 22)}{it.display}{raw}")


def diff(a: AccSetup, b: AccSetup, derived: bool) -> None:
    print("A =", _header(a))
    print("B =", _header(b))
    if a.car != b.car:
        print(f"注意：两份调教不是同一辆车（{a.car} / {b.car}）")
    changes = diff_setups(a, b, include_derived=derived)
    if not changes:
        print("\n没有差异")
        return
    group = None
    for c in changes:
        if c.group != group:
            group = c.group
            print(f"\n[{group}]")
        va = c.a.display if c.a else "（无）"
        vb = c.b.display if c.b else "（无）"
        clicks = f"  {c.clicks:+d} 格" if c.clicks and c.group not in ("策略", "其他") else ""
        print(f"  {_pad(c.label, 22)}{_pad(va, 14, right=True)} → {_pad(vb, 14)}{clicks}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m simtelemetry.setups", description="查看 / 对比 ACC 调教")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show").add_argument("file")
    d = sub.add_parser("diff")
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--derived", action="store_true", help="同时列出游戏算出来的派生值")
    args = p.parse_args(argv)
    try:
        if args.cmd == "show":
            show(load_setup(args.file))
        else:
            diff(load_setup(args.a), load_setup(args.b), args.derived)
    except (OSError, ValueError) as e:
        sys.exit(f"错误：{e}")


if __name__ == "__main__":
    main()
