# C1 巡检执行演示步骤

补丁：`CD-R2-20261025`

## 1. 启动

PowerShell：

```powershell
cd C:\Users\Zhang\Desktop\haigong-om-app-main
chcp 65001
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\streamlit run app.py
```

浏览器打开 <http://localhost:8501>。

## 2. 创建 C1 任务

1. 进入「③ 巡检规划」；
2. 找到「巡检执行（模拟）」；
3. 任务来源选：

```text
C1 · 同等级两台设备：顺序影响距离（手工模拟）
```

4. 点击「创建并开始巡检任务」。

预期：

```text
当前位置 S，已走 0 m，待检 P02/P05，剩余 70 m，预计合计 70 m，状态 active
```

## 3. 走到 N05

点击两次「沿当前路线前进一步」：

```text
S → J01 → N05
已走 30 m，剩余 40 m
```

此时 P05 在 N05，**未确认前“前进一步”按钮应置灰**。
如果尝试越过，任务模块也应拒绝，位置和里程不变。

## 4. 确认 P05

点击「确认当前设备检查完成」。

预期：

```text
当前位置 N05，已走 30 m，待检只剩 P02，剩余 40 m
```

## 5. 关闭 E04 再规划

1. 在「当前任务关闭通道」选择 `E04`；
2. 点击「应用封路并重新规划」。

预期：

```text
当前位置仍 N05，已走仍 30 m
剩余 80 m，预计合计 110 m
```

路线会绕开 E04，不会把人瞬移回 S。

## 6. 重新开放 E04

1. 从「当前任务关闭通道」移除 `E04`；
2. 点击「应用封路并重新规划」。

预期：

```text
已走仍 30 m，剩余恢复 40 m，预计合计 70 m
```

## 7. 关闭 E09 验收受阻

1. 选择 `E09`，应用封路并重新规划。

预期：

```text
P02 不可达；状态 blocked
已走仍 30 m；当前可行剩余 0 m；预计总里程显示“不可用”
页面明确“任务受阻”，不能显示“巡检已完成”
```

## 8. 恢复并完成

1. 移除 E09，应用封路并重新规划；
2. 点击三次「沿当前路线前进一步」到 N02；
3. 此时还未完成，点击「确认当前设备检查完成」确认 P02。

预期：

```text
当前位置 N02，已走 70 m，待检空，已完成 P05/P02
状态 completed，页面显示“巡检已完成”
```

“检查完成”只表示本轮检查结束，不代表设备已修复。

## 9. 导出

点击「导出任务 JSON」，得到：

```text
mission_<mission_id>.json
```

应包含：

```text
source（simulation_case / C1）
pending_assets / completed_assets
current_node / walked_distance_m / remaining_distance_m
closed_edges
events（created / step / confirm / replan / completed）
```

样例可对照交接包：

```text
evidence/C1_mission_export.json
evidence/C1_completed_export.json
```

## 10. 演示后用命令复查

```powershell
.venv\Scripts\python.exe check_cd.py
.venv\Scripts\python.exe check_cd_mission.py
```
