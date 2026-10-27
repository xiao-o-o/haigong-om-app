# C/D 累计交接说明（CD-R2-20261025）

日期：2026-10-25
工程：海工智维 · 水泵声音异常复核与巡检安排
补丁标识：`CD-R2-20261025`

## 1. 我们完成了什么

用普通中文说：

- 页面能记住“这次检测结果对应哪组音频”。换了来源、文件内容、设备映射或 mock/real 模式，
  旧检测、旧路线和旧回答会清掉；同名文件换内容也能识别。
- 起点或封路变化时，只清旧路线，保留检测结果，并提示重新规划。
- 规则版现在能正确识别“检测P01”这种中文紧挨编号的写法；“检测P99”会明确报未知设备。
- 新增三个手工模拟案例 C1/C2/C3，比较“固定顺序”和“按距离优化”的路线、距离和改善比例。
- 新增「巡检执行（模拟）」：记录当前位置、已完成、待检、不可达、已走距离和剩余路线。
- 只能沿相邻开放通道一步一步走；到达设备节点后必须人工确认，不能自动完成。
- 到达待检设备但未确认时，不能继续往前走；确认低等级设备也不能绕过更高等级的可达目标。
- 支持从当前位置关闭/重新开放通道并重规划；已走距离、当前位置和已完成设备不会被重置。
- 全部待检目标都不可达时显示“受阻”，不是“完成”；只有全部目标确认后才显示巡检完成。
- 任务可以来自当前录音检测结果，也可以来自 C1/C2/C3 模拟案例，两种来源分开保存。
- 任务可以导出 JSON，包含来源、快照、目标、位置、距离、封路和操作事件。
- Agent 保持三个工具；有活动任务时，“重新规划”使用任务当前位置、剩余目标和封路。
  Agent 不能确认完成、改当前位置或直接替换活动任务；冲突起点会明确失败。

## 2. 两轮累计改了哪些文件

### 新增

```text
core/cd_state.py
core/cd_cases.py
spatial_module/mission.py
examples/cd_cases.json
examples/run_cd_cases.py
check_cd.py
check_cd_mission.py
docs/CD_TASK.md
docs/CD_WORKLOG.md
docs/CD_ENV_LOCK_20261025.txt
docs/CD_HANDOFF.md
docs/CD_DEMO_C1.md
```

### 修改

```text
app.py
README.md
agent/loop.py
agent/planner.py
agent/tools.py
core/local_modules.py
core/report.py
spatial_module/plan.py
spatial_module/visualize.py
samples/README.md
docs/acceptance.md
docs/publish-github.md
docs/prompts_and_outputs.md
```

### 没有改

```text
audio_module/
models/pump_v1.joblib
models/model_config.json
samples/*.wav
scene/*.csv
requirements.txt
```

## 3. 不能只替换 spatial_module

必须一起合并：

- `app.py`：巡检执行页面、状态隔离、任务/静态规划/案例分开写回；
- `agent/tools.py`：`plan_asset_inspection` 的任务可选参数；
- `agent/planner.py`：活动任务上下文、起点冲突、新建/重规划区分；
- `agent/loop.py`：`AgentResult.mission` / `mission_attempted`；
- `core/cd_state.py`：输入指纹、检测结果指纹、规划上下文；
- `core/local_modules.py`：第一轮新增的对照规划惰性入口；
- `core/report.py`：实际起点/封路/完整路线和来源口径；
- `README.md` 与 `docs/`：接口、检查命令、来源口径。

只替换 `spatial_module/` 会缺少页面入口、Agent 状态和输入失效逻辑。

## 4. 必须保留的会话状态、参数和接口

### Session State

```text
mission
mission_error
mission_notice
mission_closed_edges
_pending_mission_closed_edges
```

第一轮已有的状态也必须保留：

```text
detections / detections_is_mock / detection_input_signature
plan_result / plan_context / plan_used_start_node / plan_used_closed_edges
cd_case_result / cd_case_id
last_agent
```

### 新模块接口

```python
spatial_module.mission.create_mission(...)
spatial_module.mission.step_forward(...)
spatial_module.mission.confirm_asset(...)
spatial_module.mission.replan_mission(...)
spatial_module.mission.mission_is_stale(...)
spatial_module.mission.mission_to_json(...)
```

### 原有公开入口

```python
render_route(scene_dir, route_nodes, output_path, closed_edges=None)
plan_inspection(detection_results, start_node="S", closed_edges=None, output_dir="runs") -> dict
```

### Agent 工具

旧调用顺序不变（`detection_results, closed_edges, start_node` 的前三个位置仍兼容）：

```python
plan_asset_inspection(detection_results, closed_edges=None, start_node="S")
```

真实签名（已用 `inspect.signature` 核对）：

```python
plan_asset_inspection(
    detection_results=None,
    closed_edges=None,
    start_node=None,
    input_is_mock=False,
    mission=None,
    mission_action="auto",
    mission_stale=False,
    source_signature="",
) -> ToolResult
```

`mission` 之后的参数都有默认值；无活动任务时仍返回 D 的静态规划字段，
有活动任务时返回 `{mode, action, mission, plan}`。

`AgentResult` 新增：

```text
mission
mission_attempted
```

## 5. B 已有新修改时怎么合

不要整包覆盖 B 的工作目录。建议：

1. 先把 B 当前代码存一个本地检查点；
2. 用交接包里的 `FILE_MANIFEST.csv` 对照文件；
3. 对 `app.py`、`agent/*`、`core/*` 做逐文件比较合并；
4. `spatial_module/mission.py`、`core/cd_state.py`、`core/cd_cases.py` 是新增文件，可直接加入；
5. 保留 B 自己的页面布局、部署配置和密钥配置，不要把 secrets 或 `.venv` 带过去；
6. 合并后先跑 `check_cd.py` 和 `check_cd_mission.py`，再启动页面。

本交接包基于团队最初备份 `haigong-om-app-main_cd_backup_20261024_192213`
和第一轮完成版 `..._cd_round1_complete_20261025_073725`。**不能声称与 B 最新代码无冲突**，
B 必须自行比较合并。

## 6. 已经实际通过的检查

```text
check_cd.py           23 通过 / 0 失败 / 23 项
check_cd_mission.py   23 通过 / 0 失败 / 23 项
```

最终记录在交接包 `evidence/`：

```text
check_cd_final.json
check_cd_mission_final.json
C1_mission_export.json
C1_completed_export.json
```

代表图：

```text
C1_route_optimized.png
C1_route_fixed.png
C1_distance_comparison.png
C1_mission_current_route.png
```

检查覆盖：默认样例、关路/重开、起点变化、中文编号、三个案例、固定/优化对照、
任务创建、行走、确认、等级守卫、未确认不能继续走、封路重规划、受阻/完成区分、
重复确认、重复重规划、刷新不重复记账、会话隔离、Agent 起点冲突、模拟 LLM 全链路、
A 模块异常替身、报告参数一致。

### 交接包合并测试

已在临时工程中用“最初原始备份 + 交接包完整文件”做叠加合并，复制本机未改的
A/模型/录音/场景后运行：

```text
check_cd.py           23 通过 / 0 失败
check_cd_mission.py   23 通过 / 0 失败
Streamlit 健康检查    HTTP 200 / ok
```

本机没有 `git` / `patch` 命令，补丁文件用于 B 参考；合并测试使用完整文件叠加，
不是用 `patch -p1` 机械应用。**合入 B 最新代码前仍需逐文件比较，不能直接覆盖。**

## 7. 仍需后续整体验收完成的内容

- 真实 LLM API（本轮只测规则版和本地伪造的 LLM 输出）；
- 公网部署和线上环境；
- 真实厂区录音、定位硬件、门禁；
- 多人并发调度和浏览器断线重连；
- 完整人工验收和现场安全确认；
- Python 3.12 兼容性（本轮实际为 3.13.13）。

## 8. B 合并后的最短验证

```powershell
chcp 65001
$env:PYTHONIOENCODING = "utf-8"

.venv\Scripts\python.exe check_cd.py
.venv\Scripts\python.exe check_cd_mission.py
.venv\Scripts\streamlit run app.py
```

页面打开后按 `DEMO_C1.md` 走一遍 C1，确认 0→30→80→40→受阻→恢复→70→完成。

## 9. 来源与边界

- 当前模型与两条录音按团队“合成演示”口径，不能声称已完成 MIMII 真实数据训练；
- `models/model_config.json` 的 `dataset` 字段虽写 MIMII，但 `data_root` 是 `data\demo_raw`；
- MIMII 只作为计划采用的公开数据集参考；
- “确认检查完成”不代表设备修复或恢复正常，不修改 A 的检测结论。
