# 进度交接（2026-10-19，云端第二轮）

模拟赛车遥测分析工具：采集（ACC / AC / iRacing）→ SQLite → 圈对比网页 + 手机实时面板。
技术栈：Python（ctypes 读共享内存、pyirsdk）、SQLite、FastAPI、单页 HTML + uPlot 1.6.32。

## 快速开始

```bash
pip install -r requirements.txt
python -m simtelemetry --game sim      # 合成数据，不开游戏也能跑完整流程（--sim-speed 10 加速）
python -m simtelemetry                 # 自动识别游戏（ACC / AC / iRacing，按进程名）
python -m pytest -q tests              # 60 个测试，全部通过（Linux 云端也能跑）
```

默认监听 `0.0.0.0:8000`：`/` 是圈对比，`/live` 是实时面板。数据库默认在 `data/telemetry.db`。

网页用的 uPlot 默认从 jsDelivr 加载（`/vendor/*` 307 跳转）。想离线用，把这两个文件放进 `web/vendor/`（已 gitignore），服务器会优先用本地的：
`https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.iife.min.js`、`https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.min.css`。

Windows 上不开游戏、走真实 ACC 读取路径的端到端测试：

```bash
python tools/fake_acc.py                                  # 终端 1：往 Local\acpmf_* 写合成数据
python -m simtelemetry --game acc --no-process-check      # 终端 2：照常采集 + 开网页
```

## 文件结构

| 文件 | 作用 |
|---|---|
| `simtelemetry/__main__.py` | 命令行入口：`--game auto/acc/ac/iracing/sim`、`--db`、`--host`、`--port`、`--rate`（默认 50 Hz）、`--ws-hz`、`--sim-speed`、`--no-process-check`、`--no-collect`、`--no-web`；启动时打印局域网地址 |
| `simtelemetry/models.py` | 统一数据模型：`SessionInfo`（游戏/赛道/车型）、`TelemetryFrame`（一帧，统一单位）、`LapRecord`（一圈 + 标记） |
| `simtelemetry/recorder.py` | 切圈 + 打标记：已完成圈数 +1 = 跨圈；有效/出站/进站/完整性标记；等待游戏给出圈时（最多 3 s，否则用估算值）；起终点附近位置回绕处理；计时先于圈数归零时暂存几帧（最多 25 帧 ≈ 0.5 s） |
| `simtelemetry/storage.py` | SQLite（WAL）：`laps` 表 + `samples` 表（删除圈时级联删除）；“干净圈” = 有效 ∧ 完整 ∧ 非出站 ∧ 非进站，只有干净圈参与最快圈 |
| `simtelemetry/analysis.py` | 按赛道距离对齐、重采样、delta、`LiveReference`；游戏不给赛道长度时用完整圈的速度积分估算（`estimate_track_length`） |
| `simtelemetry/collector.py` | 采集线程：约 50 Hz，游戏没开时每 3 s 安静重试；适配器创建失败（缺依赖等）只记一次日志；数据停止更新时查进程判断游戏是否退出；单独的写库线程；新最快圈出现后自动更新实时参考圈 |
| `simtelemetry/live.py` | `LiveHub`：采集线程 → WebSocket 的最新快照 |
| `simtelemetry/server.py` | FastAPI：`/api/combos`、`/api/laps`、`/api/laps/{id}`、`/api/laps/{id}/samples`、`DELETE /api/laps/{id}`、`/api/compare`（含 `track_length_source`: game/estimated）、`/api/status`、`WS /ws/live`、`/vendor/*` |
| `simtelemetry/adapters/base.py` | 适配器统一接口 `GameAdapter`：`connect()` / `read()` / `close()` / `is_running()` |
| `simtelemetry/adapters/__init__.py` | 适配器注册表（`create_adapter(game, sim_speed, check_process)`）+ 按进程名自动识别游戏 |
| `simtelemetry/adapters/winshm.py` | 只读打开命名共享内存，**不会创建**映射 |
| `simtelemetry/adapters/procs.py` | ctypes + Toolhelp32 列出进程名 |
| `simtelemetry/adapters/kunos.py` | ACC / AC1 共用骨架：先查进程（两者映射同名）再打开三个页，`read_consistent()` 读 physics/graphics |
| `simtelemetry/adapters/acc_structs.py` / `acc.py` | ACC 结构体 + 适配器 |
| `simtelemetry/adapters/ac_structs.py` / `ac.py` | AC1 结构体（physics/static 是 ACC 的前缀，graphics 单独定义）+ 适配器 |
| `simtelemetry/adapters/iracing.py` | iRacing 适配器（pyirsdk；先确认映射存在再 `startup()`，吞掉它的 print） |
| `simtelemetry/adapters/simulated.py` | 合成车手（4.2 km 虚构赛道，带出站/进站/压线无效圈）+ `SimulatedAdapter` |
| `web/index.html` | 圈对比页（见下文“已完成”） |
| `web/live.html` | 手机实时面板 |
| `tools/fake_acc.py` | 假 ACC：Windows 上创建同名映射写合成数据（映射已存在就拒绝运行）；`FakeACC` 类跨平台，测试也用它 |
| `tools/dump_frames.py` | 逐帧录制适配器输出成 CSV，用来实车核对细节（见“下一步”） |
| `docs/research.md` | **AC1 结构体、iRacing 变量、进程名的调研记录和来源** |
| `tests/` | `test_pipeline_sim.py`（合成数据全流程 + 赛道长度估算）、`test_analysis.py`、`test_server.py`（REST + WS）、`test_acc_layout.py`（结构体大小/偏移钉死）、`test_adapters.py`（ACC 端到端 + AC1/iRacing 转换 + collector 容错） |

## 已完成

- **阶段 1**：50 Hz 采集循环、等待/重试、切圈、出站/进站/无效/不完整标记、存 SQLite。
- **阶段 2**：后端（圈列表、按距离对齐的对比、默认参考圈 = 个人最快干净圈）+ **前端 `web/index.html`**：
  - 选择游戏·赛道·车型 → 圈列表（参考/对比单选、圈时、与最快圈差距、日期、标记、删除）；选中状态写在 URL hash 里，刷新/分享不丢；每 10 s 刷新列表，新圈自动出现；
  - 5 张共享横轴的 uPlot 图：速度、油门↑/刹车↓、转向、挡位（阶梯线）、delta（零线以上红 = 对比圈掉时间、以下绿 = 赚时间）；
    光标和拖选缩放同步（双击或“还原缩放”还原），光标处所有数值显示在图上方的读数区；
  - 横轴：有赛道长度用米（ACC 用估算值，标“≈”），否则用 %；导出 CSV；可展开的数据表（每 2% 一行）；
  - 跟随系统深浅色。颜色：参考蓝 `#2a78d6`（深色 `#3987e5`）、对比橙 `#eb6834`（深色 `#d95926`），dataviz 校验器两种模式都通过；delta 用状态色（critical `#d03b3b` / good `#0ca30c`），主要靠零线上下位置区分。
- **阶段 3**：后端 WebSocket + **前端 `web/live.html`**：始终深色；超大 delta + 居中 delta 条（±1 s 满格，左快右慢）；delta 不可信时变灰并说明原因（没有参考圈 / 维修区 / 出站或不完整圈）；本圈计时、预测圈时、个人最快、上一圈；挡位/速度大字、转速、油门刹车条；本圈无效 / 维修区徽标；保存新圈时弹提示（新最快会标出来）；WebSocket 断线指数退避重连、超过 4.5 s 没消息判定停滞；能用 wakeLock 就用、能全屏就显示全屏按钮；竖屏/横屏两种布局。
- **阶段 4（代码完成，待实车验证）**：ACC、AC1、iRacing 三个适配器都写好了，测试用假共享内存页 / 假 SDK 覆盖了转换逻辑，ACC 还用 `FakeACC`（模拟“计时先归零、圈数晚几帧”的时序）跑通了切圈全流程。
- 修掉了上一轮的已知问题：`--game acc/ac/iracing` 不再因为缺模块报错；适配器创建失败不会再让采集线程退出。
- 测试时的 httpx 弃用警告：`requirements.txt` 加了 `httpx2`（新版 Starlette 优先用它，旧版仍用 httpx）。

## 下一步（需要在装了游戏的 Windows 机器上做）

1. **先跑 `python tools/fake_acc.py` + `python -m simtelemetry --game acc --no-process-check`**，确认 Windows 上真实的共享内存读取路径（winshm）没问题，再开真游戏。
2. 每个游戏用 `python tools/dump_frames.py --game <acc|ac|iracing> --seconds 120` 录一段，核对：
   - 转向正负（本项目约定 **+ = 左**；合成数据就是这样）：ACC / AC1 的 `steerAngle`、iRacing 的 `SteeringWheelAngle` 方向都没有文档，录一段往左打方向看正负，不对就在对应适配器里取反；
   - 挡位编码（ACC/AC1 按 0=R/1=N/2=1 挡减 1）：挂倒挡看 gear 是不是 -1；
   - 跨线时序：过线前后 `lap_count` / `lap_time_ms` / `last_lap_ms` / `pos` 怎么变（录制器最多等圈数 0.5 s，等圈时 3 s）；
   - iRacing：从维修区出发第一次过线时 `LapCompleted` 有没有 +1（细节见 `docs/research.md`）。
3. 核对进程名：AC1 的 64 位 exe 是不是 `acs.exe`（看 `Z:\SteamLibrary\steamapps\common\assettocorsa\`）；iRacing 非 DX11 版的 exe 名。
4. 手机实际连一次 `/live`（Windows 防火墙“专用网络”放行），看字号和布局。

## 已知问题 / 可以改进的地方

- ACC 不提供赛道长度（`trackSPlineLength` 实测为 0）：对比图用参考圈的速度积分估算，行驶线比中心线略短，误差一般 1–2%，只影响横轴米数。
- ACC 的 `isValidLap` 在比赛 / hotstint 会话里压线后也常常保持 1（社区实测），这些会话的“有效”标记不可靠；需要官方广播 UDP 接口才能拿到准确值。圈列表目前不显示会话类型（数据库里有 `session_type`），可以加一个标签提醒。
- AC1 没有“本圈有效”字段，用 `numberOfTyresOut > 2` 近似（社区说法：AC 固定按超过 2 个轮胎出界判切弯）；服务器/赛道 mod 的规则可能不同。
- iRacing 没有“本圈有效”变量，用 `PlayerTrackSurface == 出界` 近似；`LapCurrentLapTime` 是 F3 计时框里的估算值。
- 实时面板在局域网 http 下不能用 wakeLock（需要安全上下文），会提示手动调长锁屏时间；iPhone Safari 不支持 Fullscreen API，按钮自动隐藏。
- 圈对比页在手机上能用，但圈列表需要横向滚动；uPlot 的触摸交互有限（主要还是给电脑看的）。

## ACC 共享内存结构体的参考来源

- **Kunos 官方 “ACC Shared Memory Documentation” v1.8.12**（`ACCSharedMemoryDocumentationV1.8.12.pdf`，作者 KUNOS，2022-03-23），
  assettocorsa.net 论坛 “ACC Shared Memory Documentation” 帖子的附件。论坛附件需要登录才能下载，调研时读的是
  GitHub 上 rrennoir/PyAccSharedMemory 仓库里的同一个 PDF（大小和元数据与论坛附件一致，但没有逐字节比对）。
- `#pragma pack(4)` 和映射名 `Local\acpmf_physics/graphics/static` **不在 PDF 里**，来自官方示例头文件 `SharedFileOut.h`（社区转载副本），并且多个独立实现都一致。
- 与 6 个以上独立实现交叉核对过（PyAccSharedMemory、TinyPedal、CrewChief、gabbo76 等）。按文档计算的大小：physics 800、graphics 1588、static 820 字节。
  关键偏移：graphics `completedLaps` 132、`iCurrentTime` 140、`normalizedCarPosition` 248、`isInPitLane` 1236、`isValidLap` 1408；static `carModel` 68、`track` 134。
  这些大小和偏移已经在 `tests/test_acc_layout.py` 里钉死（AC1 的也在里面）。
- 文档本身有歧义的地方：`trackConfiguration` 没写数组长度（按示例头文件取 [33]，代码里标了 TODO）；
  `currentMaxRpm` 和 `AllowTyreBlankets` 文档写 float、头文件写 int（都是 4 字节，不影响偏移）。
- 社区实测的时序：跨线时 `iCurrentTime` 会比 `completedLaps` 早一帧或几帧归零；`iLastTime` 和圈数在同一帧更新；
  游戏退出后共享内存页会保留最后一帧（`status` 可能停在 LIVE），所以要靠进程判断游戏是否退出。

## 本地环境说明（只和原开发机有关，云端不需要照做）

- 代码里**没有写死的 Windows 路径**。数据库默认路径是相对项目根目录的 `data/telemetry.db`，可以用 `--db` 改。
- **只能在 Windows 上运行的部分**：`adapters/winshm.py`（kernel32 共享内存）、`adapters/procs.py`（Toolhelp32）、`tools/fake_acc.py` 的写映射部分、pyirsdk 读 iRacing。在非 Windows 系统上会自动降级（打不开映射 / 返回空进程列表），所以 `--game sim` 和全部测试在 Linux 云端也能跑；真正读游戏数据必须在装了游戏的 Windows 机器上。
- 游戏进程名（用来自动识别游戏，写在 `adapters/__init__.py`）：ACC `AC2-Win64-Shipping.exe`、iRacing `iRacingSim64DX11.exe`、AC `acs_x86.exe` 有资料佐证；AC 64 位 `acs.exe` 还要在游戏目录里核对（见 `docs/research.md`）。
- 原开发机的 Python 环境比较特殊：PATH 上的 `python` 指向一个坏掉的 venv（`E:\Scripts\python.exe`，提示 “No pyvenv.cfg file”），注册表里的 3.12 指向不存在的 `E:\python.exe`；
  能用的只有 Microsoft Store 版 Python 3.12.10，但它对 `AppData\Roaming` 下的文件做了虚拟化，看不到项目文件，所以开发时是把项目复制到临时目录再跑测试的。云端用普通的 venv + `pip install -r requirements.txt` 即可。
- 手机访问需要 Windows 防火墙允许 Python 监听“专用网络”（第一次启动时会弹窗）。局域网 http 页面不能用 `navigator.wakeLock`（必须是安全上下文），iPhone Safari 也不支持 Fullscreen API。
- 游戏安装位置（只供参考）：ACC、AC、iRacing 都在 `Z:\SteamLibrary\steamapps\common\`；游戏目录里没有官方共享内存文档。
