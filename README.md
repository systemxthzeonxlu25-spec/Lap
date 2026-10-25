# Lap

模拟赛车遥测分析工具：采集（ACC / AC / iRacing）→ SQLite → 圈对比网页 + 手机实时面板。

```bash
pip install -r requirements.txt
python -m simtelemetry --game sim      # 合成数据，不开游戏也能跑完整流程
python -m pytest -q
```

查看 / 对比调教（ACC 的 .json 换算成游戏里显示的数值；LMU 的 .svm 直接用游戏写的显示值）：

```bash
python -m simtelemetry.setups diff a.json b.json
```

导入 ACC 导出的 MoTeC 文件（调教里把 “MoTeC 导出圈数” 设成 > 0）：

```bash
python -m simtelemetry.motec "文档/Assetto Corsa Competizione/MoTeC/xxx.ld"
```

把一圈发给朋友对比（网页上每圈的 ⤓ 按钮也可以导出，圈列表“导入圈”导入）：

```bash
python -m simtelemetry.lapfile export 圈ID --note "车手名"
python -m simtelemetry.lapfile import xxx.lap.gz
```

Windows 用户可以直接到 [Releases](../../releases) 下载 `Lap-<版本>-windows-x64.exe`，双击运行，数据存在 exe 旁边的 `data/`（每次 CI 打包的测试版在 Actions → Artifacts → Lap-windows）。

切圈不对、想反馈问题时，录一段原始数据（文件里有赛道/车型，`--record-raw` 时还有玩家名/服务器名，不会上传，发给别人前请自己确认）：

```bash
python -m simtelemetry --game acc --record acc.strec [--record-raw]   # 正常采集，同时录制
python -m simtelemetry --replay acc.strec [--replay-speed 4 | --replay-fast] --db repro.db   # 用录制代替游戏
python -m simtelemetry.replay acc.strec --dry-run                     # 不开网页，直接打印切出来的圈和标记
```

打开 `http://127.0.0.1:8000/`（圈对比）或 `/live`（实时面板）。

目前 `sim`、`acc`、`lmu` 能采集（ACC / LMU 还没在真游戏上验证；LMU 需要在游戏设置里打开 Enable Plugins），AC / iRacing 还在做。架构、进度和已知问题见 [PROGRESS.md](PROGRESS.md)。

## 协议

GPL-3.0-or-later，见 [LICENSE](LICENSE)。选 GPL 是为了能参考同为 GPL-3 的 ACC 社区项目
（`simtelemetry/setups/acc_tables.py` 的调教换算表移植自 [Race Element](https://github.com/RiddleTime/race-element)；
`simtelemetry/motec.py` 的 MoTeC 解析移植自 [gotzl/ldparser](https://github.com/gotzl/ldparser)；
`simtelemetry/adapters/lmu_structs.py` 转写自 MIT 协议的 [TinyPedal/pyLMUSharedMemory](https://github.com/TinyPedal/pyLMUSharedMemory)，保留了原版权声明）；基于本项目的修改版发布时同样需要开源。
