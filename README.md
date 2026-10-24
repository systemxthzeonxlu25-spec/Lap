# Lap

模拟赛车遥测分析工具：采集（ACC / AC / iRacing）→ SQLite → 圈对比网页 + 手机实时面板。

```bash
pip install -r requirements.txt
python -m simtelemetry --game sim      # 合成数据，不开游戏也能跑完整流程
python -m pytest -q
```

查看 / 对比 ACC 调教（换算成游戏里显示的数值）：

```bash
python -m simtelemetry.setups diff a.json b.json
```

打开 `http://127.0.0.1:8000/`（圈对比）或 `/live`（实时面板）。

目前 `sim` 和 `acc` 能采集（ACC 还没在真游戏上验证），LMU / AC / iRacing 还在做。架构、进度和已知问题见 [PROGRESS.md](PROGRESS.md)。

## 协议

GPL-3.0-or-later，见 [LICENSE](LICENSE)。选 GPL 是为了能参考同为 GPL-3 的 ACC 社区项目
（`simtelemetry/setups/acc_tables.py` 的调教换算表移植自 [Race Element](https://github.com/RiddleTime/race-element)；
以后的 MoTeC 解析会参考 gotzl/ldparser）；基于本项目的修改版发布时同样需要开源。
