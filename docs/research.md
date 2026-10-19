# 调研记录（AC1 共享内存、iRacing SDK、进程名）

2026-10-19 在云端查的。上一次（ACC 结构体）的来源写在 PROGRESS.md 的“ACC 共享内存结构体的参考来源”一节。
**凡是标了“待实车确认”的，都要在装了游戏的 Windows 机器上用 `tools/dump_frames.py` 录一段核对。**

## 初代 Assetto Corsa（AC1）共享内存

映射名和 ACC 相同：`Local\acpmf_physics` / `Local\acpmf_graphics` / `Local\acpmf_static`
（游戏内 Python app 用的是不带 `Local\` 的 `acpmf_physics`，从外部进程打开时两者是同一个会话命名空间）。
`#pragma pack(4)`，`wchar_t` 为 2 字节（Linux 上 ctypes 的 `c_wchar` 是 4 字节，所以结构体里统一用 `c_uint16`）。

来源（两份独立实现，逐字段一致）：
- albertowd/live-telemetry：`apps/python/LiveTelemetry/lib/sim_info.py`，文件头写 “Siminfo updated to AC 1.14.3”
  <https://raw.githubusercontent.com/albertowd/live-telemetry/master/apps/python/LiveTelemetry/lib/sim_info.py>
- mdjarv/assettocorsasharedmemory（C#）：`Graphics.cs` / `Physics.cs` / `StaticInfo.cs`，字段带 “since 1.5 / 1.7 / 1.13” 版本注释，
  用 `MemoryMappedFile.OpenExisting("Local\\acpmf_physics")` 打开
  <https://github.com/mdjarv/assettocorsasharedmemory>
- 两者都引用 assettocorsa.net 论坛的 “Shared Memory Reference” 帖（threads/shared-memory-reference.3352）；云端打不开论坛。

用脚本把 sim_info.py 的结构体（换成 2 字节 wchar）和本项目 ACC 结构体逐字段比对过：
- physics：AC1 = ACC physics 从开头到 `localVelocity`，580 字节，偏移和大小全部一致；
- static：AC1 = ACC static 从开头到 `PitWindowEnd`，684 字节，全部一致（ACC 的 `deprecated_1/2` 在 AC1 叫 `airTemp/roadTemp`）；
- graphics：前 252 字节（到 `normalizedCarPosition`）一致；之后 AC1 是 `carCoordinates[3]`(252) `penaltyTime`(264) `flag`(268)
  `idealLineOn`(272) `isInPitLane`(276) `surfaceGrip`(280) `mandatoryPitDone`(284) …，ACC 则插入了 60 辆车的坐标表。
- 分歧：`trackConfiguration` 长度 sim_info.py 写 33、C# 写 15。它的起始偏移（524）不受影响，本项目只读它一个字符串（遇 `\0` 截止）。

字段语义：
- AC1 **没有** `isValidLap`。切弯判定用 physics `numberOfTyresOut`：社区说法是 AC 固定用“超过 2 个轮胎出界”判定
  （SimHub 论坛：值 ≥ 3 视为出界/切弯；InvalidateLap mod 说明：race.ini 的 `ALLOWED_TYRES_OUT` 不生效，游戏总是用 2）。
  - <https://www.simhubdash.com/community-2/dashboard-templates/need-programming-help-to-finish-my-dash-invalid-times-in-assetto-corsa/>
  - <https://assettocorsamods.net/posts/24003>（InvalidateLap，云端只看到搜索摘要）
- `steerAngle`：文档没定义；Overtake 论坛有开发者实测是 -1..1（不是角度）。<https://overtake.gg/threads/programming-c-question.259525>
- `trackSPlineLength`：AC1 里是真实赛道长度（米）；ACC 里实测为 0。
- 挡位编码按和 ACC 相同处理（0 = R、1 = N、2 = 1 挡）——**待实车确认**。

## iRacing（pyirsdk）

变量说明来自 iRacing SDK 运行时的变量表转储：mpapenbr/goirsdk 的 `variables.md`
<https://raw.githubusercontent.com/mpapenbr/goirsdk/v0.6.0/variables.md>

| 变量 | 说明（原文） | 单位 / 类型 |
|---|---|---|
| Speed | GPS vehicle speed | m/s |
| Throttle / Brake | 0=off throttle to 1=full throttle / 0=brake released to 1=max pedal force | % (0..1) |
| SteeringWheelAngle / SteeringWheelAngleMax | Steering wheel angle / max angle | rad |
| Gear | -1=reverse 0=neutral 1..n=current gear | int |
| RPM | Engine rpm | float |
| Lap / LapCompleted | Laps started count / Laps completed count | int |
| LapCurrentLapTime | Estimate of players current lap time as shown in F3 box | s |
| LapLastLapTime | Players last lap time | s |
| LapDistPct / LapDist | Percentage distance around lap / Meters traveled from S/F this lap | 0..1 / m |
| OnPitRoad | Is the player car on pit road between the cones | bool |
| PlayerCarInPitStall | Players car is properly in their pitstall | bool |
| PlayerTrackSurface | Players car track surface type | irsdk_TrkLoc |
| IsOnTrack / IsReplayPlaying | Car on track physics running with player in car / replay playing | bool |
| SessionTick / SessionNum | Current update number / Session number | int |

`irsdk_TrkLoc`（pyirsdk 里的常量）：-1 不在场上、0 出界（off_track）、1 维修位、2 接近维修区、3 赛道上。

会话信息 YAML（`ir['WeekendInfo']` 等）：`WeekendInfo.TrackDisplayName / TrackConfigName / TrackLength`（形如 “6.93 km”）、
`DriverInfo.DriverCarIdx` + `DriverInfo.Drivers[].CarIdx / CarScreenName / CarPath`、`SessionInfo.Sessions[].SessionNum / SessionType`。

pyirsdk 1.3.7 源码里看到的行为（`irsdk.py`）：
- `startup()` 先请求 `http://127.0.0.1:32034/get_sim_status?object=simStatus`，失败时 **print** “Failed to connect to sim”；
- 然后 `mmap.mmap(0, MEMMAPFILESIZE, 'Local\\IRSDKMemMapFileName')`——Windows 上映射不存在时会**创建**它，
  所以适配器先用 `OpenFileMappingW` 确认映射存在再调 `startup()`；
- `freeze_var_buffer_latest()` 会等数据就绪事件（最多 32 ms），读完要 `unfreeze_var_buffer_latest()`；
- 会话信息 YAML 按 `SessionInfoUpdate` 缓存，变化时同步重新解析。

待实车确认：
- 从维修区出发第一次过线时 `LapCompleted` 是否 +1（网上没查到确切说法）。录制器两种情况都能切圈：不 +1 时出站圈会被当成“计时倒退”丢掉，不影响后面的圈。
  如果 `LapCompleted` 比过线晚很多帧才变（> 0.5 s），录制器会把那一圈当成重开丢掉——那就要改用 `Lap`。
- `LapLastLapTime` 过线后多久更新（社区反馈会晚一点；录制器最多等 3 s，等不到用估算圈时）。
- `SteeringWheelAngle` 左转为正还是负。
- `PlayerTrackSurface == 0`（出界）是否等同于 iRacing 判的 4 轮出界 / 本圈无效。iRacing 没有“本圈有效”变量。

## 游戏进程名

- ACC：`AC2-Win64-Shipping.exe`（上一次调研，社区资料确认）。
- iRacing：`iRacingSim64DX11.exe`——iRacing 官方支持页提到这个 exe 名，CrewChief 的配置也用它识别 iRacing。
  <https://support.iracing.com/support/solutions/articles/31000162005-failed-createprocessasuser-error>
- AC1：`acs_x86.exe` 是 32 位引擎（Steam 指南）；64 位的 `acs.exe` 只查到间接说法——**在游戏目录里看一眼**
  （`Z:\SteamLibrary\steamapps\common\assettocorsa\`）。`AssettoCorsa.exe` 是启动器，不是赛车进程。
