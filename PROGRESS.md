# 进度交接（2026-10-19）

模拟赛车遥测分析工具：采集（ACC / AC / iRacing）→ SQLite → 圈对比网页 + 手机实时面板。
技术栈：Python（ctypes 读共享内存）、SQLite、FastAPI、单页 HTML + uPlot 1.6.32。

## 快速开始

```bash
pip install -r requirements.txt
python -m simtelemetry --game sim      # 合成数据，不开游戏也能跑完整流程
python -m simtelemetry                 # 自动识别游戏（目前 sim、acc 已实现；acc 还没在真游戏上验证）
python -m pytest -q                    # 47 个测试，全部通过（配置在 pyproject.toml）
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
| `simtelemetry/adapters/__init__.py` | 适配器注册表（`_REGISTRY`：游戏 → 模块/类）+ `is_implemented()` + 按进程名自动识别游戏。新增游戏：写模块、在 `_REGISTRY` 里已有占位，模块存在就自动生效 |
| `simtelemetry/adapters/winshm.py` | 只读打开命名共享内存（OpenFileMappingW + MapViewOfFile + VirtualQuery），**不会创建**映射（原因见文件头注释） |
| `simtelemetry/adapters/procs.py` | ctypes + Toolhelp32 列出进程名（不依赖 psutil） |
| `simtelemetry/adapters/acc.py` | ACC 适配器：`ACCAdapter` 只管开/读/关三个共享内存页，`translate()` 是纯函数（挡位 -1、`iLastTime` 的 INT32_MAX → None、`isInPit or isInPitLane`、`status == LIVE`、physics `packetId` 作 tick），Linux 上也能测 |
| `tools/fake_acc.py` | `encode()`：合成车手 → ACC 结构体（测试用）；在 Windows 上直接运行会创建 `Local\acpmf_*` 并 60 Hz 写入，映射已存在时拒绝运行 |
| `simtelemetry/adapters/acc_structs.py` | ACC 三个页面的 ctypes 结构体（physics / graphics / static） |
| `simtelemetry/adapters/simulated.py` | 合成车手（4.2 km 虚构赛道，带出站/进站/压线无效圈）+ `SimulatedAdapter` |
| `simtelemetry/setups/acc.py` | ACC 调教：`load_setup()` 解析 setup.json，`items()` 换算成游戏里显示的值（胎压 psi、刹车平衡 %、车高 mm…），`diff_setups()` 对比两份（默认跳过游戏算出来的派生值）。没登记的字段按原始值放进“其他”，不会丢 |
| `simtelemetry/setups/acc_tables.py` | **自动生成，不要手改**。54 辆车的换算规则，移植自 Race Element（GPL-3） |
| `simtelemetry/setups/__main__.py` | `python -m simtelemetry.setups show a.json` / `diff a.json b.json [--derived]` |
| `tools/gen_acc_setup_tables.py` | 从 Race Element 源码生成 `acc_tables.py`：只做正则 + 受限表达式解析，不执行下载的代码，遇到不认识的写法直接报错。Race Element 更新后：`git clone --depth 1 https://github.com/RiddleTime/race-element` 再跑 `python tools/gen_acc_setup_tables.py <目录>` |
| `web/index.html`、`web/live.html` | 圈对比页、实时面板（见“做到一半”第 2 条） |
| `tests/` | `simdrive.py`（测试共用的合成车手驱动）、`test_pipeline_sim.py`（合成数据 → 切圈 → 存库 → 对比）、`test_analysis.py`（插值/回绕/阶梯重采样）、`test_server.py`（REST + WebSocket）、`test_acc_layout.py`（ACC 结构体大小/偏移）、`test_robustness.py`（未实现游戏、存储/命令行参数） |

## 已完成

- **阶段 1（与游戏无关的部分）**：50 Hz 采集循环、游戏没开时安静等待并重试、切圈、出站/进站/无效/不完整标记、存 SQLite。用合成数据验证过整条链路：出站圈 → 干净圈 → 无效圈 → 进站圈 → 出站圈，圈时取自“游戏”。
- **阶段 2 后端**：圈列表、按距离对齐的对比接口（含 delta，终点 delta 正好等于圈时差）、默认参考圈 = 该赛道+车型的个人最快干净圈。
- **阶段 3 后端**：WebSocket 推送实时快照（速度、挡位、转速、油门/刹车、本圈时间、实时 delta、预测圈时、最近保存的圈），无变化时每 2 s 发一次心跳。
- ACC 结构体定义已写好（逐字段对照官方文档）。

- **调教（ACC）第一步**：换算表 + 解析 + 对比 + 命令行。已用真实调教文件核对过 296 GT3 的换算。

## 做到一半

0. **调教管理后续**：调教库（扫描 `Documents/Assetto Corsa Competizione/Setups`，文件变化时存快照到 SQLite，备注/标签）、
   Web 页面、把调教和圈关联（会话开始时猜“最近改过的那份”，用户确认）。原则：只复制、不改不删游戏目录里的文件。
   已知不确定：`rideHeight` 有 4 个值，Race Element 只用 [0]（前）和 [2]（后），另外两个含义未知；电子类（TC/ABS/引擎图）只显示原始值。
   ACC 的 MoTeC 导出（`.ld` + `.ldx`）可以导入：`.ld` 里的 `LAP_BEACON` 通道实测全是 0，切圈要读 `.ldx` 里的过线时间（微秒），
   参考 gotzl/acctelemetry；`SPEED` 通道看起来是 m/s（未确认）。

1. **ACC 适配器已写完，但还没在真游戏上跑过。** Linux 上用假页面测了整条链路（结构体 → 切圈 → 标记 → 圈时取自游戏）。
   需要在 Windows 上确认：① `python tools/fake_acc.py` + `python -m simtelemetry --game acc` 端到端能切圈；
   ② 真 ACC 里挡位、方向盘左右、进出站标记是否正确。
2. **前端第一版已做好，等你过目**（Playwright 在桌面 / 手机竖屏 / 手机横屏、浅色 / 深色下截图检查过，无横向滚动、无控制台报错）：
   - 圈对比 `web/index.html`：赛道+车型下拉 → 圈列表（参考/对比单选、圈时、与最快干净圈的差距、标记、删除）→ 5 张共享横轴的 uPlot 图。
     顺序：delta（最上面，0 以上红 = 对比圈更慢、以下绿）、速度、油门（0 以上）/刹车（0 以下）、挡位阶梯线、转向。
     颜色只表示哪一圈（参考蓝 / 对比橙）；踏板松开处不画线；光标、框选放大、双击还原五张图同步；图上方一行显示光标处的数值；
     横轴米（赛道长度未知时用 %），刻度取整；导出 CSV；当前选择写在 URL 里，刷新/分享能回到同一个对比。
   - 实时面板 `web/live.html`：始终深色；delta 大字 + 居中 ±2 s 条（灰色 = 出站/进站等仅供参考）；挡位、速度、本圈、预测、上一圈、转速、油门/刹车条；
     存下一圈时弹提示；WebSocket 断线指数退避重连，5 s 没消息视为断线；手机横放时左右分栏。
   - uPlot 默认从 jsDelivr 加载（`/vendor/*` 307 跳转）；离线使用可把 `uPlot.iife.min.js`、`uPlot.min.css`（1.6.32）放进 `web/vendor/`。
3. **阶段 4 没开始**：`adapters/ac.py`（初代 AC）、`adapters/iracing.py`（pyirsdk）不存在。
4. 调研结果（AC1 结构体、iRacing 变量）**没有保存进仓库**，云端需要重新查。

## 已知问题

- `--game acc/ac/iracing` 目前会在启动时直接报“适配器还没有实现”；`--game auto` 检测到这些游戏时只打一条警告并跳过（以前采集线程会因 ImportError 退出，已修复）。
- ACC 没有提供赛道长度（`trackSPlineLength` 实测为 0）：现在每个完整圈按速度积分估算（`analysis.estimate_track_length`，合成数据误差 < 1 m），存在该圈的 `track_length_m` 里。
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
  这些大小和偏移已由 `tests/test_acc_layout.py` 固定下来。
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
