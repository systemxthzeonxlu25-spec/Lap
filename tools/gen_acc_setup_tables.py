"""从 Race Element 的 C# 源码生成 ACC 调教换算表（simtelemetry/setups/acc_tables.py）。

    git clone --depth 1 https://github.com/RiddleTime/race-element
    python tools/gen_acc_setup_tables.py path/to/race-element

Race Element（https://github.com/RiddleTime/race-element，GPL-3.0）为每辆车写了一个
ICarSetupConversion 实现，把 setup.json 里的“格数”换算成游戏里显示的数值。这个脚本只用
正则 + 受限的表达式解析器读这些文件——不执行任何下载来的代码；遇到不认识的写法直接报错，
而不是猜。Race Element 更新后重新跑一遍即可同步。

每个换算规则输出为下面两种之一（value 按 ndigits 四舍五入，ndigits 为 None 表示不取整）：
    ("lin", a, b, ndigits, index)      value = a + b * raw
    ("table", values, ndigits, index)  value = values[min(raw, len - 1)]
index 为 None 表示用该车轮/该字段自己的原始值；为整数表示固定取原始数组的第几个
（Race Element 的车高只用 rideHeight[0] 和 rideHeight[2]）。
"""
from __future__ import annotations

import ast
import pprint
import re
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "simtelemetry" / "setups" / "acc_tables.py"

# Race Element 方法名 → 我们的字段名
METHODS = {
    "Camber": "camber", "Toe": "toe", "Caster": "caster",
    "AntiRollBarFront": "arb_front", "AntiRollBarRear": "arb_rear",
    "BrakeBias": "brake_bias", "BrakePower": "brake_power",
    "BumpstopRange": "bumpstop_range", "BumpstopRate": "bumpstop_rate",
    "PreloadDifferential": "preload", "SteeringRatio": "steer_ratio", "WheelRate": "wheel_rate",
    "BrakeDucts": "brake_duct", "RearWing": "rear_wing", "RideHeight": "ride_height",
    "Splitter": "splitter",
    "BumpSlow": "bump_slow", "BumpFast": "bump_fast", "ReboundSlow": "rebound_slow",
    "ReboundFast": "rebound_fast",
}
DAMPERS = ("bump_slow", "bump_fast", "rebound_slow", "rebound_fast")
IDENTITY = ("lin", 0, 1, None, None)

TYRE_COMPOUND_BASE = {"DHF2023": 20.3, "DHF2023_GT4": 17.0}   # SetupConverter.TirePressure


# ----------------------------------------------------------------------------- C# 文本处理
def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def matching(src: str, i: int, open_ch: str, close_ch: str) -> int:
    """src[i] == open_ch，返回与之匹配的 close_ch 的位置。"""
    depth = 0
    for j in range(i, len(src)):
        if src[j] == open_ch:
            depth += 1
        elif src[j] == close_ch:
            depth -= 1
            if depth == 0:
                return j
    raise ValueError("括号不匹配")


def read_expr(src: str, i: int) -> str:
    """从 i 开始读一个表达式，直到括号深度为 0 的 ',' ';' 或 '}'。"""
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth == 0 and c in ",;}":
            return src[i:j].strip()
    raise ValueError("表达式没有结束")


def arrays(src: str) -> dict[str, list[float]]:
    out = {}
    pat = re.compile(r"(?:double|int|float)\s*\[\]\s+(\w+)\s*=\s*(?:new\s*(?:double|int|float)?\s*\[\]\s*)?([\[{])")
    for m in pat.finditer(src):
        start = m.end() - 1
        end = matching(src, start, m.group(2), "]" if m.group(2) == "[" else "}")
        nums = [n for n in re.split(r"[,\s]+", src[start + 1:end]) if n]
        out[m.group(1)] = [num(n) for n in nums]
    return out


def num(s: str) -> float:
    s = s.rstrip("dfDFmM")
    v = float(s)
    return int(v) if v.is_integer() and "." not in s else v


def methods(src: str) -> dict[str, str]:
    """方法名 → 方法体（块体取 {...} 内部，表达式体取 => 之后到 ;）。"""
    out = {}
    pat = re.compile(r"\b(?:\w+\.)?(" + "|".join(METHODS) + r")\s*\(([^)]*)\)\s*(\{|=>)")
    for m in pat.finditer(src):
        name = m.group(1)
        if m.group(3) == "{":
            end = matching(src, m.end() - 1, "{", "}")
            body = src[m.end():end]
        else:
            body = "return " + read_expr(src, m.end()) + ";"
        if name in out:
            raise ValueError(f"{name} 定义了不止一次")
        out[name] = body
    return out


# ------------------------------------------------------------------------- 表达式 → 规则
def to_python(expr: str) -> str:
    e = expr.replace("Math.Round", "round")
    e = re.sub(r"\(int\)\s*wheel", "w", e)
    e = re.sub(r"\brawValue\b", "raw", e)
    e = re.sub(r"(?<![\w.])\.(\d)", r"0.\1", e)              # .2 → 0.2
    e = re.sub(r"(\d)[dfmDFM]\b", r"\1", e)                  # 13d / 0.1f → 13 / 0.1
    return e


class _Eval:
    """只允许 + - * /、数字、raw、raw[w]、raw[常数]、数组[...]、round(x, n)。"""

    def __init__(self, tables: dict[str, list[float]]):
        self.tables = tables
        self.ndigits: int | None = None
        self.index: int | None = None
        self.table: str | None = None

    def run(self, node, raw: int):
        if isinstance(node, ast.Expression):
            return self.run(node.body, raw)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = self.run(node.operand, raw)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = self.run(node.left, raw), self.run(node.right, raw)
            return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b}.get(type(node.op), a / b if b else 0)
        if isinstance(node, ast.Name) and node.id == "raw":
            return raw
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            sl = node.slice
            if node.value.id == "raw":
                if isinstance(sl, ast.Name) and sl.id == "w":
                    return raw
                if isinstance(sl, ast.Constant) and isinstance(sl.value, int):
                    self.index = sl.value
                    return raw
            elif node.value.id in self.tables:
                if self.table not in (None, node.value.id):
                    raise ValueError("一个表达式里用了两张表")
                self.table = node.value.id
                values = self.tables[node.value.id]
                return values[min(int(self.run(sl, raw)), len(values) - 1)]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "round" \
                and len(node.args) == 2 and isinstance(node.args[1], ast.Constant):
            self.ndigits = node.args[1].value
            return self.run(node.args[0], raw)
        raise ValueError(f"不认识的表达式：{ast.dump(node)}")


def rule(expr: str, tables: dict[str, list[float]]):
    tree = ast.parse(to_python(expr), mode="eval")
    ev = _Eval(tables)
    v0, v1, v2 = (ev.run(tree, r) for r in (0, 1, 2))
    if ev.table is not None:
        n = len(tables[ev.table])
        values = [ev.run(tree, r) for r in range(n)]
        return ("table", values, ev.ndigits, ev.index)
    b = v1 - v0
    if abs((v2 - v1) - b) > 1e-9:
        raise ValueError(f"不是线性的：{expr}")
    return ("lin", clean(v0), clean(b), ev.ndigits, ev.index)


def clean(x: float):
    x = round(x, 9)
    return int(x) if float(x).is_integer() else x


def body_rules(body: str, tables) -> dict[str, tuple] | tuple:
    """返回单个规则，或 {'front': 规则, 'rear': 规则}。"""
    body = " ".join(body.split())
    sides = {}
    for side in ("Front", "Rear"):
        m = re.search(rf"(?:case Position\.{side}\s*:\s*return|Position\.{side}\s*=>)\s*", body)
        if m:
            sides[side.lower()] = rule(read_expr(body, m.end()), tables)
    if sides:
        if set(sides) != {"front", "rear"}:
            raise ValueError("只找到了前或后的一边")
        return sides if sides["front"] != sides["rear"] else sides["front"]
    # 去掉 “if (rawValue > casters.Length - 1) return casters[^1];” 这种越界保护（表规则本身会截断）
    body = re.sub(r"if \(raw\w* > \w+\.Length - 1\) return \w+\[\^1\];", "", body)
    rets = re.findall(r"\breturn\s+", body)
    if len(rets) != 1:
        raise ValueError(f"无法识别的方法体：{body}")
    return rule(read_expr(body, body.index("return") + len("return")), tables)


# ------------------------------------------------------------------------------ 汇总
def car_classes(root: Path) -> dict[str, dict]:
    out = {}
    for f in sorted((root / "Race_Element.Data.ACC" / "SetupParser" / "Cars").rglob("*.cs")):
        src = strip_comments(f.read_text(encoding="utf-8-sig"))
        m = re.search(r"class\s+(\w+)\s*:[^{]*\bICarSetupConversion\b", src)
        if not m:
            continue
        try:
            tables = arrays(src)
            ms = methods(src)
            rules = {METHODS[k]: body_rules(v, tables) for k, v in ms.items()}
            if re.search(r"DamperSetup\s*=>\s*DefaultDamperSetup", src):
                for d in DAMPERS:
                    rules.setdefault(d, IDENTITY)
            missing = set(METHODS.values()) - set(rules)
            if missing:
                raise ValueError(f"缺少 {sorted(missing)}")
            car_class = re.search(r"CarClass\s*=>\s*CarClasses\.(\w+)", src).group(1)
            compound = re.search(r"DryTyreCompound\s*=>\s*DryTyreCompounds\.(\w+)", src).group(1)
        except Exception as e:
            raise SystemExit(f"{f.relative_to(root)}: {e}") from e
        rules["tyre_pressure"] = ("lin", TYRE_COMPOUND_BASE[compound], 0.1, 2, None)
        out[m.group(1)] = {"class": car_class, "rules": rules}
    return out


def factory_maps(root: Path):
    src = strip_comments((root / "Race_Element.Data.ACC" / "SetupParser" / "ConversionFactory.cs")
                         .read_text(encoding="utf-8-sig"))

    def block(name: str) -> str:
        i = src.index(name)
        start = src.index("{", src.index("new()", i))
        return src[start:matching(src, start, "{", "}")]

    conv = dict(re.findall(r"\{\s*(\w+)\s*,\s*new\s+(\w+)\(\)\s*\}", block("Conversions =")))
    names = dict(re.findall(r'\{\s*(\w+)\s*,\s*"([^"]*)"\s*\}', block("CarModelToCarName =")))
    parse = dict(re.findall(r'\{\s*"([^"]+)"\s*,\s*(\w+)\s*\}', block("ParseNames =")))
    return conv, names, parse


def steering_locks(root: Path) -> dict[str, int]:
    """Race_Element.Data.ACC/Cars/SteeringLock.cs：车型 → 方向盘总转角（度，左右满舵之和）。"""
    src = strip_comments((root / "Race_Element.Data.ACC" / "Cars" / "SteeringLock.cs").read_text(encoding="utf-8-sig"))
    return {k: int(v) for k, v in re.findall(r'\{\s*"([^"]+)"\s*,\s*(\d+)\s*\}', src)}


def main(root: Path) -> None:
    classes = car_classes(root)
    locks = steering_locks(root)
    conv, names, parse = factory_maps(root)
    cars = {}
    for car_name, model in sorted(parse.items()):
        cls = conv.get(model)
        if cls is None or cls not in classes:
            print(f"跳过 {car_name}（{model}）：Race Element 里没有它的换算", file=sys.stderr)
            continue
        cars[car_name] = {"name": names.get(model, car_name), "class": classes[cls]["class"],
                          "steer_lock": locks.get(car_name), **dict(sorted(classes[cls]["rules"].items()))}
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                                text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    header = f'''"""ACC 调教换算表。由 tools/gen_acc_setup_tables.py 自动生成，不要手改。

数据移植自 Race Element（https://github.com/RiddleTime/race-element，GPL-3.0），
Race_Element.Data.ACC/SetupParser/Cars/ 和 Cars/SteeringLock.cs，提交 {commit}。
Race Element 的作者与贡献者保留其著作权；本文件按 GPL-3.0-or-later 随本项目发布。
规则格式见生成脚本的文档字符串；steer_lock 是方向盘总转角（度）。
"""
# SPDX-License-Identifier: GPL-3.0-or-later
# flake8: noqa

SOURCE_COMMIT = {commit!r}

CARS = '''
    OUT.write_text(header + pprint.pformat(cars, width=110, sort_dicts=False, compact=True) + "\n", encoding="utf-8")
    print(f"写入 {OUT}：{len(cars)} 辆车")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]))
