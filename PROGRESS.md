# 进度交接（2026-10-19）

模拟赛车遥测分析工具：采集（ACC / AC / iRacing）→ SQLite → 圈对比网页 + 手机实时面板。
技术栈：Python（ctypes 读共享内存）、SQLite、FastAPI、单页 HTML + uPlot 1.6.32。

## 快速开始

```bash
pip install -r requirements.txt
python -m simtelemetry --game sim      # 合成数据，不开游戏也能跑完整流程
python -m simtelemetry                 # 自动识别游戏（目前只有 sim 能真正采集，见“做到一半”）
python -m pytest -q tests              # 11 个测试，全部通过
```

默认监听 `0.0.0.0:8000`：`/` 是圈对比，`/live` 是实时面板。数据库默认在 `data/telemetry.db`。

## 文件结构

| 文件 | 作用 |
|---|---|
| `simtelemetry/__main__.py` | 命令行入口：`--game auto/acc/ac/iracing/sim`、`--db`、`--host`、`--port`、`--rate`（默认 50 Hz）、`--ws-hz`、`--no-collect`、`--no-web`；启动时打印局域网地址 |
| `simtelemetry/models.py` | 统一数据模型：`SessionInfo`（游戏/赛道/车型）、`TelemetryFrame`（一帧，统一单位）、`LapRecord`（一圈 + 标记） |
| `simtelemetry/recorder.py` | 切圈 + 打标记：已完成圈数 +1 = 跨圈；有效/出站/进站/完整性标记；等待游戏给出圈时（最多 3 s，否则用估算值）；起终点附近位置回绕处理；计时先于圈数归零时暂存几帧 |
| `simtelemetry/storage.py` | SQLite（WAL）：`laps` 表（一圈一条）+ `samples` 表（采样点，删除圈时级联删除）；“干净圈” = 有效 ∧ 完整 ∧ 非出站 ∧ 非进站，只有干净圈参与最快圈 |
| `simtelemetry/analysis.py` | 按赛道距离对齐：t(d) 插值、多通道重采样（挡位用阶梯取值）、delta 曲线（正 = 对比圈更慢）、`LiveReference`（实时 delta，O(log n) 查询） |
| `simtelemetry/collector.py` | 采集线程：约 50 Hz 循环，游戏没开时每 3 s 安静重试，数据停止更新时检查进程是否退出；单独的写库线程；新最快圈出现后自动更新实时参考圈 |
| `simtelemetry/live.py` | `LiveHub`：采集线程 → WebSocket 的最新快照 |
| `simtelemetry/server.py` | FastAPI：`/api/combos`、`/api/laps`、`/api/laps/{id}`、`/api/laps/{id}/samples`、`DELETE /api/laps/{id}`、`/api/compare`、`/api/status`、`WS /ws/live`、`/vendor/*`（本地有 uPlot 就用本地的，否则 307 跳转 jsDelivr） |
| `simtelemetry/adapters/base.py` | 适配器统一接口 `GameAdapter`：`connect()` / `read()` / `close()` / `is_running()` |
| `simtelemetry/adapters/__init__.py` | 适配器注册表 + 按进程名自动识别游戏 |
| `simtelemetry/adapters/winshm.py` | 只读打开命名共享内存（OpenFileMappingW + MapViewOfFile + VirtualQuery），**不会创建**映射（原因见文件头注释） |
| `simtelemetry/adapters/procs.py` | ctypes + Toolhelp32 列出进程名（不依赖 psutil） |
| `simtelemetry/adapters/acc_structs.py` | ACC 三个页面的 ctypes 结构体（physics / graphics / static） |
| `simtelemetry/adapters/simulated.py` | 合成车手（4.2 km 虚构赛道，带出站/进站/压线无效圈）+ `SimulatedAdapter` |
| `web/index.html`、`web/live.html` | **目前只是占位页**（见下文） |
| `tests/` | `test_pipeline_sim.py`（合成数据 → 切圈 → 存库 → 对比）、`test_analysis.py`（插值/回绕/阶梯重采样）、`test_server.py`（REST + WebSocket） |

## 已完成

- **阶段 1（与游戏无关的部分）**：50 Hz 采集循环、游戏没开时安静等待并重试、切圈、出站/进站/无效/不完整标记、存 SQLite。用合成数据验证过整条链路：出站圈 → 干净圈 → 无效圈 → 进站圈 → 出站圈，圈时取自“游戏”。
- **阶段 2 后端**：圈列表、按距离对齐的对比接口（含 delta，终点 delta 正好等于圈时差）、默认参考圈 = 该赛道+车型的个人最快干净圈。
- **阶段 3 后端**：WebSocket 推送实时快照（速度、挡位、转速、油门/刹车、本圈时间、实时 delta、预测圈时、最近保存的圈），无变化时每 2 s 发一次心跳。
- ACC 结构体定义已写好（逐字段对照官方文档）。

## 做到一半

1. **ACC 适配器 `adapters/acc.py` 还没写。** `acc_structs.py` 已就绪。计划中的映射关系：
   `speedKmh`、`gas`、`brake`、`steerAngle`、`gear - 1`、`rpms`、`completedLaps`、`iCurrentTime`、
   `normalizedCarPosition`、`isInPit or isInPitLane`、`isValidLap`、`iLastTime`（`INT32_MAX` 或 ≤ 0 → None）、
   `status == ACC_LIVE` 作为 is_live、physics `packetId` 作为 tick；用 `read_consistent()` 防止读到写了一半的数据；
   `is_running()` 用 `game_process_running("acc")`；track = static `track`，car = static `carModel`，session_type 来自 graphics `session`。
   还需要一个 Windows 下的测试工具 `tools/fake_acc.py`：用 `mmap` 创建同名映射、写入 `SyntheticDriver` 的数据，端到端测试真实的 ACC 读取路径（如果映射已存在必须拒绝运行，避免写坏游戏的共享内存）。
2. **前端两个页面没做**（`web/index.html`、`web/live.html` 是占位页）。设计已定：
   - 圈对比：选择赛道+车型 → 圈列表（参考/对比单选、圈时、差距、日期、标记、删除）→ 5 张共享横轴的 uPlot 图（速度；油门向上/刹车向下画在同一张图；转向；挡位阶梯线；delta 以 0 为界上红下绿），用 `uPlot.sync(key)` 同步光标和缩放，横轴用米（未知赛道长度时用 %），支持导出 CSV。
   - 实时面板：始终深色，delta 最大 + 居中 delta 条，挡位/速度大字，WebSocket 自动重连，过期检测。
   - 颜色已用 dataviz 校验器检查过：参考圈蓝 `#2a78d6`（深色 `#3987e5`），对比圈橙 `#eb6834`（深色 `#d95926`），色盲安全，两种主题都通过。
3. **阶段 4 没开始**：`adapters/ac.py`（初代 AC）、`adapters/iracing.py`（pyirsdk）不存在。
4. 调研结果（AC1 结构体、iRacing 变量）**没有保存进仓库**，云端需要重新查。

## 已知问题

- `--game acc/ac/iracing` 目前会因为找不到适配器模块而报错；用 `--game auto` 时如果检测到这些游戏进程，采集线程会因同样的 ImportError 退出（`collector._candidates()` 里的 `create_adapter` 没包在 try 里）。没开游戏时不受影响。
- ACC 没有提供赛道长度（`trackSPlineLength` 实测为 0），对比图横轴只能用 %。可行办法：用参考圈的速度积分估算赛道长度。
- ACC 的 `isValidLap` 在比赛 / hotstint 会话里压线后也常常保持 1（社区实测），这些会话的“有效”标记不可靠；需要靠官方广播 UDP 接口才能拿到准确值。
- `steerAngle` 的正负方向（左转是正还是负）文档没写，需要实车记录确认；挡位编码 0=R/1=N/2=1 挡来自社区实测，不在文档里。
- 测试时 Starlette 提示 `httpx` 用于 TestClient 已弃用（建议改用 `httpx2`），只是警告。

## ACC 共享内存结构体的参考来源

- **Kunos 官方 “ACC Shared Memory Documentation” v1.8.12**（`ACCSharedMemoryDocumentationV1.8.12.pdf`，作者 KUNOS，2022-03-23），
  assettocorsa.net 论坛 “ACC Shared Memory Documentation” 帖子的附件。论坛附件需要登录才能下载，调研时读的是
  GitHub 上 rrennoir/PyAccSharedMemory 仓库里的同一个 PDF（大小和元数据与论坛附件一致，但没有逐字节比对）。
- `#pragma pack(4)` 和映射名 `Local\acpmf_physics/graphics/static` **不在 PDF 里**，来自官方示例头文件 `SharedFileOut.h`（社区转载副本），并且多个独立实现都一致。
- 与 6 个以上独立实现交叉核对过（PyAccSharedMemory、TinyPedal、CrewChief、gabbo76 等）。按文档计算的大小：physics 800、graphics 1588、static 820 字节。
  关键偏移：graphics `completedLaps` 132、`iCurrentTime` 140、`normalizedCarPosition` 248、`isInPitLane` 1236、`isValidLap` 1408；static `carModel` 68、`track` 134。
  **还没写把这些大小和偏移固定下来的测试**（计划写 `tests/test_acc_layout.py`）。
- 文档本身有歧义的地方：`trackConfiguration` 没写数组长度（按示例头文件取 [33]，代码里标了 TODO）；
  `currentMaxRpm` 和 `AllowTyreBlankets` 文档写 float、头文件写 int（都是 4 字节，不影响偏移）。
- 社区实测的时序：跨线时 `iCurrentTime` 会比 `completedLaps` 早一帧或几帧归零；`iLastTime` 和圈数在同一帧更新；
  游戏退出后共享内存页会保留最后一帧（`status` 可能停在 LIVE），所以要靠进程判断游戏是否退出。

## 本地环境说明（只和原开发机有关，云端不需要照做）

- 代码里**没有写死的 Windows 路径**。数据库默认路径是相对项目根目录的 `data/telemetry.db`，可以用 `--db` 改。
- **只能在 Windows 上运行的部分**：`adapters/winshm.py`（kernel32 共享内存）和 `adapters/procs.py`（Toolhelp32）。在非 Windows 系统上会自动降级（打不开映射 / 返回空进程列表），所以 `--game sim` 和全部测试在 Linux 云端也能跑；真正读游戏数据必须在装了游戏的 Windows 机器上。
- 游戏进程名（用来自动识别游戏，写在 `adapters/__init__.py`）：ACC `AC2-Win64-Shipping.exe`（已被社区资料确认），AC `acs.exe` / `acs_x86.exe`、iRacing `iRacingSim64DX11.exe`（还未核对）。
- 原开发机的 Python 环境比较特殊：PATH 上的 `python` 指向一个坏掉的 venv（`E:\Scripts\python.exe`，提示 “No pyvenv.cfg file”），注册表里的 3.12 指向不存在的 `E:\python.exe`；
  能用的只有 Microsoft Store 版 Python 3.12.10，但它对 `AppData\Roaming` 下的文件做了虚拟化，看不到项目文件，所以开发时是把项目复制到临时目录再跑测试的。云端用普通的 venv + `pip install -r requirements.txt` 即可。
- 手机访问需要 Windows 防火墙允许 Python 监听“专用网络”（第一次启动时会弹窗）。局域网 http 页面不能用 `navigator.wakeLock`（必须是安全上下文），iPhone Safari 也不支持 Fullscreen API。
- 游戏安装位置（只供参考）：ACC、AC、iRacing 都在 `Z:\SteamLibrary\steamapps\common\`；游戏目录里没有官方共享内存文档。
