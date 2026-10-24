# Lap

模拟赛车遥测分析工具：采集（ACC / AC / iRacing）→ SQLite → 圈对比网页 + 手机实时面板。

```bash
pip install -r requirements.txt
python -m simtelemetry --game sim      # 合成数据，不开游戏也能跑完整流程
python -m pytest -q
```

打开 `http://127.0.0.1:8000/`（圈对比）或 `/live`（实时面板）。

目前只有 `sim` 能真正采集，ACC / AC / iRacing 适配器还在做。架构、进度和已知问题见 [PROGRESS.md](PROGRESS.md)。

## 协议

GPL-3.0-or-later，见 [LICENSE](LICENSE)。选 GPL 是为了能参考同为 GPL-3 的 ACC 社区项目
（Race Element 的调教换算表、gotzl/ldparser 的 MoTeC 解析）；基于本项目的修改版发布时同样需要开源。
