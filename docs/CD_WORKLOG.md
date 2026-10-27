# C/D 联合优化工作记录

日期：2026-10-24
工作目录：`C:\Users\Zhang\Desktop\haigong-om-app-main`
Git：当前目录不是 Git 仓库，本轮用本地备份作为恢复点。

## 1. 实际运行环境

| 项 | 实际值 |
| --- | --- |
| 系统 | Windows |
| 系统已注册解释器 | Python 3.12.8（`C:\Users\Zhang\AppData\Local\Programs\Python\Python312\python.exe`） |
| 本轮项目解释器 | Python **3.13.13**（`.venv`，由 `uv` 下载） |
| Python 3.13.13 路径 | `C:\Users\Zhang\AppData\Roaming\uv\python\cpython-3.13-windows-x86_64-none\python.exe` |
| scikit-learn | 1.9.1 |
| numpy | 2.5.4 |
| pandas | 3.1.0 |
| networkx | 3.7 |
| matplotlib | 3.11.2 |
| soundfile | 0.14.0 |
| streamlit | 1.66.1 |

说明：系统里没有找到已注册的 3.13；本机原来的 `python` 指向 3.12.8。
本轮用 `uv` 下载 Python 3.13.13 并创建项目独立 `.venv`，没有使用系统 Python 装依赖。
旧 README / 验收记录里的 Python 3.13.14 是上一轮环境，本轮没有复测 3.13.14。

安装命令：

```bat
uv venv --python 3.13 .venv
uv pip install --python .\.venv\Scripts\python.exe -r requirements.txt
```

## 2. 本地恢复点

备份目录（在项目外，避免被当成源码提交）：

```text
C:\Users\Zhang\Desktop\haigong-om-app-main_cd_backup_20261024_192213
```

包含：`app.py`、`agent/`、`core/`、`spatial_module/`、`scene/`、`docs/`、
`README.md`、`requirements.txt`、`.gitignore`、`samples/README.md`。
没有复制 `.venv/`、`runs/`、模型和录音本体。

原始模型与两段录音未改动，SHA-256：

```text
P01_sample.wav  2DC862956EEF6A1BD4944706BDE8F9D5AA049C5BD330E7BBA2A1F3C480C72607
P04_sample.wav  AAD01FB99E6625CF90934AAEED55B4EC2A62201E4C11AFDC33F56E7003C93886
```

## 3. 实际修改

### 3.1 新增

| 文件 | 作用 |
| --- | --- |
| `core/cd_state.py` | 检测输入指纹（来源/模式/设备映射/文件内容 SHA-256）与规划上下文比较 |
| `core/cd_cases.py` | 读取并校验 `examples/cd_cases.json`；拒绝案例里的 score/threshold |
| `examples/cd_cases.json` | C1/C2/C3 三个手工设定案例及来源声明 |
| `examples/run_cd_cases.py` | 运行三个案例，输出原始 JSON、汇总 CSV、两张路线图和对比图 |
| `check_cd.py` | 23 项实际功能检查入口 |
| `docs/CD_TASK.md` | 本轮任务范围 |
| `docs/CD_WORKLOG.md` | 本文件 |

### 3.2 修改

| 文件 | 修改 |
| --- | --- |
| `app.py` | 输入变化清旧检测/路线/回答；起点/封路变化只清路线；失败清旧结果；Agent 状态同步；报告参数；新增案例对照区 |
| `agent/planner.py` | 修正中文紧挨编号的正则；解析关闭/重新开放通道；默认沿用页面起点/封路 |
| `agent/loop.py` | 记录检测/规划是否尝试；失败不再带出旧检测；记录实际起点和封路 |
| `agent/tools.py` | D 返回 `invalid_input` 时把工具结果改为成功=否 |
| `core/report.py` | 使用 D 返回的实际起点/关闭通道/完整路线；invalid_input 按失败写；更新来源口径 |
| `core/local_modules.py` | 增加 `comparison_planner()` 惰性入口 |
| `spatial_module/plan.py` | 返回基线与耗时；新增 `compare_inspection_plans`；地图渲染复用 |
| `spatial_module/visualize.py` | 新增 `render_plan_comparison`；`render_route` 公开签名不变 |
| `README.md` | 来源口径、Python 版本、C/D 本轮说明、目录清单 |
| `samples/README.md` | 改为当前“合成演示”来源口径，保留 MIMII 作为计划采用参考 |
| `docs/acceptance.md` | 加来源口径更正说明；真实厂区音频项改为“随包合成演示录音” |
| `docs/publish-github.md` | 发布自查项改为检查当前来源口径 |
| `docs/prompts_and_outputs.md` | 注明“真实数据”指实际调模型，不代表真实厂区录音 |

没有修改：

- `audio_module/` 下的训练/推理代码；
- `models/pump_v1.joblib`、`models/model_config.json`；
- `samples/*.wav`；
- `scene/*.csv`；
- `requirements.txt`。

## 4. 关键行为

### 4.1 输入变化

检测结果旁边保存一个输入指纹，包含：

- `mock` / `real` 模式；
- 音频来源（内置 / 全场景 / 上传）；
- 每台设备的编号、文件名、文件内容 SHA-256。

因此同名上传文件换内容也会变指纹。指纹不一致时清检测、路线和 Agent 回答；
起点或关闭通道变化只清路线，检测保留。

### 4.2 失败

- 页面手动检测：点击后先清旧结果，再调用 A；失败不会留下上一次的检测/路线。
- 页面手动规划：点击后先清旧路线；D 的 `invalid_input` 由工具改为 `ok=False`。
- Agent：`detections_attempted` / `plan_attempted` 标记本次是否尝试；失败时页面按类型清除。
- 报告：如果拿到 `invalid_input`，写“本次规划失败”，不写距离，不保留旧路线。

### 4.3 编号

规则版正则从 `\bP01\b` 改为前后不接 ASCII 字母数字的匹配：

- `检测P01` → `["P01"]`；
- `检测P01和P04` → `["P01","P04"]`；
- `检测P99` → 工具报未知设备，Agent 失败，页面清旧检测。

### 4.4 起点 / 封路

- 页面用同一个 `plan_start_node` / `plan_closed_edges` 状态；
- Agent 缺省参数沿用页面当前值，不重置成 `S` / 空数组；
- `关闭E11` 加入关闭集合，`重新开放E11` / `取消关闭E11` 从集合移除；
- Agent 规划后通过 pending 状态在下一次 rerun 同步页面下拉框，避免直接改 widget 状态报错。

### 4.5 固定顺序与优化方案

- 复用 D 的 `_analyse_plan`、`_fixed_order_baseline`；
- 两者使用同一批可达目标、同一起点、同样等级约束和同样开放通道；
- 距离由 networkx 按 `length_m` 计算；
- 新增 `baseline` 和 `comparison`，改善比例在基线为 0 时写“不适用”；
- 计算耗时与绘图耗时分开记录，均注明不是性能基准。

### 4.6 来源口径

`model_config.json` 的 `dataset` 字段仍写 MIMII，但 `data_root` 是 `data\demo_raw`，
`n_normal_train=24`、`n_normal_val=8`，没有真实数据训练记录。
本轮按团队要求：

- 当前模型和录音按“合成演示”说明；
- MIMII 只列为计划采用的公开数据集参考；
- 不声称已完成 MIMII 真实数据训练；
- 不修改模型/训练配置制造来源已更新的假象。

## 5. 实际验证

### 5.1 最终检查

命令：

```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python.exe check_cd.py
```

结果：

```text
总计：23 通过 / 0 失败 / 23 项
记录：runs/check_cd/20261024_204400/check_result.json
退出码：0
```

23 项覆盖：

1. 运行环境；
2. 默认样例真实检测（P01 normal 0.4557；P04 suspected_anomaly 0.6187；阈值 0.5455）；
3. 默认规划 S→N04，10 m；
4. 关闭 E11 后 partial，P04 不可达，空顺序，0 m；
5. 重新开放 E11 恢复 ok，10 m；
6. 起点 J01，30 m；
7. invalid_input 提示补录，不加入目标；
8. 全部异常目标不可达仍为 partial；
9. D invalid_input 按失败处理；
10. 输入指纹与规划上下文；
11. 规则版编号/封路解析；
12. Agent 只检测 P01；
13. Agent P99 失败清旧结果；
14. Agent 起点/封路状态；
15. 三个模拟案例与基线；
16. 基线为 0 的改善比例；
17. 案例脚本 JSON/CSV/9 张图；
18. 报告记录实际参数；
19. 页面状态清理与重规划；
20. 页面 Agent 状态同步；
21. 页面案例状态隔离；
22. 页面模式切换清旧结果；
23. 页面同名文件与坏 WAV。

中途真实失败过一次：第一次运行 22 通过 / 1 失败，失败项是检查脚本自己的
`dict(**os.environ, PYTHONIOENCODING="utf-8")` 写法与已有环境变量冲突，
不是业务功能失败。改成先复制环境字典再赋值后，重新运行得到 23/23。

### 5.2 案例脚本

```bat
.venv\Scripts\python.exe examples\run_cd_cases.py
```

实际输出（检查脚本内部也跑过一次，另有单独运行）：

```text
C1 status=ok       优化 P05→P02 70 m；固定 P02→P05 90 m
C2 status=ok       优化 P01→P02→P04 130 m；固定相同 130 m
C3 status=partial  优化 P01→P03 90 m；固定相同 90 m；P02 不可达
```

单独运行输出目录：`runs/cd_cases/case_run_20261024_204017/`。

输出目录形如：

```text
runs/cd_cases/case_run_<时间戳>/
  C1_result.json
  C2_result.json
  C3_result.json
  summary.csv
  manifest.json
  maps/d_plan_.../route_optimized.png
  maps/d_plan_.../route_fixed.png
  maps/d_plan_.../distance_comparison.png
```

### 5.3 页面启动冒烟

```bat
.venv\Scripts\streamlit run app.py --server.headless true --server.port 8599
```

实际探测 `http://127.0.0.1:8599/_stcore/health` 返回 `HTTP 200 / ok`，
服务日志显示 `Uvicorn server started`。探测完成后已关闭测试进程。

### 5.4 默认样例

- P01：`normal`，score `0.4557`；
- P04：`suspected_anomaly`，score `0.6187`；
- 阈值：`0.5455`；
- 默认巡检：`S→N04`，`10.0 m`；
- 关闭 E11：`partial`，`visit_order=[]`，`unreachable_asset_ids=["P04"]`，`0.0 m`。

## 6. 未验证 / 已知限制

1. **没有在 Python 3.12 上运行本轮代码**。本轮实际是 3.13.13；3.12 只在系统里存在，未安装依赖。
2. **没有重新训练、没有真实厂区数据**。当前模型/录音按合成演示口径，不能评价声音检测准确率。
3. **没有运行付费 LLM API 自检**。本轮不需要密钥；Agent 的规则版路径已测。
4. **没有部署、没有推送远程**。
5. 页面手动规划失败（例如 D 因非法参数返回 invalid_input）通过工具层和代码路径检查，
   没有在 UI 里构造非法起点/非法通道；页面按钮本身会先清旧路线再显示失败。
6. 模型文件缺失、依赖损坏等系统级 A 失败没有在本轮故意制造；已有逻辑仍是“直接报错，
   不用模拟数据兜底”。
7. 单次耗时只记录在 JSON/页面展开项中，**不是可靠性能基准**。
8. 画图依赖本机中文字体；没有中文字体时会退化为英文图例。

## 7. 下一轮入口

下一轮目标是：

- 记录已完成设备；
- 从当前位置重新规划剩余任务；
- 封路后更新路线；
- 分别显示已走距离和剩余距离。

本轮保留的结构：

- D 的 `PlanData` 已有 `start`、`targets`、`reachable`、`visit_order`、`route_nodes`；
- D 返回已带 `start_node`、`closed_edges`、`route_nodes`；
- 页面把检测结果、当前路线、模拟案例分开保存；
- `plan_context` 可以判断路线是否对应当前起点/封路。

建议下一轮新增参数时保持：

```python
plan_inspection(detection_results, start_node="S", closed_edges=None, output_dir="runs")
```

的兼容性，另加可选参数（例如 `completed_assets`、`current_node`），不要改变旧调用方式。

## 8. Windows 复查命令

```bat
cd C:\Users\Zhang\Desktop\haigong-om-app-main
.venv\Scripts\python.exe --version
.venv\Scripts\python.exe check_cd.py
.venv\Scripts\python.exe examples\run_cd_cases.py
.venv\Scripts\streamlit run app.py
```

如果终端中文显示乱码，先执行（PowerShell）：

```powershell
chcp 65001
$env:PYTHONIOENCODING = "utf-8"
```

页面地址：<http://localhost:8501>

---

# 第二轮增量记录（2026-10-25）

补丁标识：`CD-R2-20261025`

## 9. 第二轮检查点与环境

### 9.1 第一轮完成版检查点

```text
C:\Users\Zhang\Desktop\haigong-om-app-main_cd_round1_complete_20261025_073725
```

包含第一轮完成时的 `app.py`、`agent/`、`core/`、`spatial_module/`、`scene/`、
`docs/`、`examples/`、`check_cd.py`、`README.md`、`requirements.txt`、
`runtime.txt`、`.gitignore`、`.streamlit/` 和 `samples/README.md`；
`evidence/round1_check_result.json` 是第一轮 23 项检查记录副本。
没有复制 `.venv/`、`runs/`、模型和录音本体。

### 9.2 精确依赖清单

文件：`docs/CD_ENV_LOCK_20261025.txt`

- 平台：Windows 11；
- 项目解释器：Python 3.13.13（`.venv`）；
- 版本来源：当前 `.venv` 的 `pip freeze --all`；
- 没有重新解析、升级或替换依赖；
- 未验证 Python 3.12、macOS、Linux 或其他平台。

本轮没有重建 `.venv`，没有切换 3.12，没有升级依赖，也没有重新训练模型。

### 9.3 PowerShell 写法

第一轮文档里 `set PYTHONIOENCODING=utf-8` 是 cmd 写法。本轮已改成：

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

## 10. 第二轮修改

### 10.1 新增

| 文件 | 作用 |
| --- | --- |
| `spatial_module/mission.py` | 巡检任务状态、行走、确认、从当前位置重规划、封路、导出 |
| `check_cd_mission.py` | 第二轮 23 项检查入口 |
| `docs/CD_ENV_LOCK_20261025.txt` | 当前 `.venv` 精确依赖清单 |
| `docs/CD_HANDOFF.md` | 给 B 的累计交接说明 |
| `docs/CD_DEMO_C1.md` | C1 页面演示步骤 |

### 10.2 修改

| 文件 | 修改 |
| --- | --- |
| `app.py` | 新增「巡检执行（模拟）」、任务新建/替换、前进、确认、封路重规划、导出、失效提示 |
| `agent/tools.py` | `plan_asset_inspection` 增加可选 `mission` / `mission_action` / `mission_stale` / `source_signature`；旧调用不变 |
| `agent/planner.py` | 上下文带活动任务；默认沿用任务当前位置/封路；区分新建与重规划；支持“从S重新规划”冲突识别 |
| `agent/loop.py` | `AgentResult` 增加 `mission` / `mission_attempted`；活动任务重规划；检测后不把新数据套旧任务 |
| `core/cd_state.py` | 新增检测结果集指纹，任务快照与当前结果不一致时失效 |
| `check_cd.py` | 仅更新规则版/LLM 校验的精确参数断言，加入新增可选字段 `mission_action=auto`；检查项仍为 23 |
| `README.md` | 第二轮功能、检查命令、补丁标识、目录清单 |
| `docs/CD_TASK.md` | 追加第二轮任务范围与验收数值 |
| `docs/CD_WORKLOG.md` | 本记录 |

未修改：

- `audio_module/`；
- `models/pump_v1.joblib`、`models/model_config.json`；
- `samples/*.wav`；
- `scene/*.csv`；
- `requirements.txt`。

## 11. 第二轮实现要点

### 11.1 任务状态

`spatial_module.mission` 使用普通字典，由 `app.py` 放在当前 Streamlit Session State 的
`mission` 键中，不使用跨用户全局变量。

主要字段：

```text
mission_id / created_at / updated_at / status
source.kind / label / is_mock / case_id / source_note
source.input_signature / source.result_signature / source.detection_snapshot
initial_targets / pending_assets / completed_assets
current_node / walked_distance_m / remaining_distance_m
closed_edges / reachable_assets / unreachable_assets / expected_total_distance_m
remaining_plan / events / completion_note
```

所有修改函数返回深拷贝，不原地改调用方对象。

### 11.2 状态语义

- `active`：有待检目标，且至少一个可达；
- `blocked`：有待检目标，但当前全部不可达；
- `completed`：所有初始目标都已由人员确认；
- `no_targets`：没有疑似异常目标。

受阻不是完成；只有全部确认后才是 `completed`。
`expected_total_distance_m` 只在没有待检不可达目标时给出。

### 11.3 行走与确认

- `step_forward` 只走当前剩余路线的 `route_nodes[1]`；
- 用 `_available_graph` 检查通道是否可用，按 `length_m` 增加里程；
- 走完一步后重画当前剩余路线图；
- `confirm_asset` 要求设备在当前节点的待检列表中；
- 重复确认、未到节点确认都抛 `MissionError`，里程和完成列表不变。

### 11.4 重规划

`replan_mission` 用任务快照中尚未完成的目标调用 D 的 `plan_inspection`：

- 起点固定为 `current_node`；
- 已完成设备不进入目标；
- 已走距离和当前位置不变；
- 关闭通道作为本次规划输入；
- 重开通道后可以恢复不可达目标；
- 重复重规划只增加事件，不增加里程。

### 11.5 来源失效

录音来源任务保存：

- 音频输入指纹（来源/模式/映射/文件内容）；
- 检测结果集指纹（设备/状态/分数/阈值）。

任一变化，页面显示“任务已失效”，禁止前进和重规划，但保留任务和快照，要求先导出再新建。
模拟案例任务不依赖录音检测，不受录音输入变化影响。

### 11.6 Agent

保持三个工具不变。`plan_asset_inspection` 的旧参数仍可用，新增可选参数：

```python
mission=None, mission_action="auto", mission_stale=False, source_signature=""
```

有活动任务时：

- 重规划使用任务 `current_node`、`pending_assets`、`closed_edges`；
- `start_node` 与任务当前位置冲突时明确失败；
- Agent 不能确认检查完成，也不能改当前位置或已走距离；
- Agent 不能直接替换活动任务，替换必须在页面再次确认。

### 11.7 导出

页面下载 `mission_<mission_id>.json`。包含：

- 来源、任务快照、目标；
- 当前位置、已完成、待检、不可达；
- 已走、剩余、预计总里程；
- 关闭通道、剩余路线；
- `events` 操作事件（created / step / confirm / replan / completed）。

## 12. 第二轮实际验证

### 12.1 第二轮检查

```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python.exe check_cd_mission.py
```

结果：

```text
总计：23 通过 / 0 失败 / 23 项
记录：runs/check_cd_mission/20261025_150218/check_result.json
退出码：0
```

23 项覆盖：

1. 运行环境与精确依赖清单；
2. C1 创建任务 0 m / 70 m；
3. C2 任务等级约束；
4. 走到 N05、确认 P05；
5. 关/开 E04 重规划；
6. 受阻与完成区分；
7. 未到节点确认、重复确认；
8. 关闭通道不能穿过、完成后不能继续走；
9. 重复重规划不增加里程；
10. 无目标与无效录音提示；
11. 任务快照与输入/结果变化失效；
12. 任务 JSON 导出；
13. 不同任务对象隔离；
14. Agent 重规划与起点冲突；
15. Agent 新建与拒绝替换；
16. 本地模拟 LLM 输出校验（未调用 API）；
17. 模拟 LLM 失败路径（本地替身）；
18. A 模块异常替身；
19. 页面 C1 全流程；
20. 页面 Agent 任务重规划、起点冲突和拒绝替换；
21. 页面刷新/重规划不重复记账；
22. 页面失效与新会话隔离；
23. 页面与静态规划/案例隔离；替换任务需要额外确认并生成新任务编号。

### 12.2 第一轮回归

第一轮检查未删除，重新运行：

```text
总计：23 通过 / 0 失败 / 23 项
记录：runs/check_cd/20261025_150457/check_result.json
退出码：0
```

回归中真实失败过一次：第二轮给规则版计划参数增加了可选字段 `mission_action=auto`，
第一轮检查仍在做“精确字典相等”断言，因此 1 项失败。更新该测试的期望字段后重新运行，
23/23 通过。业务规则没有放宽；检查项数量仍是 23。

### 12.3 C1 完整操作记录

```text
创建：S，已走 0 m，待检 P02/P05，剩余 70 m，预计合计 70 m，active
走两步到 N05：已走 30 m，待检 P02/P05，剩余 40 m
未确认 P05 时尝试继续走：被拒绝，位置仍 N05、里程仍 30 m
确认 P05：已走 30 m，待检 P02，剩余 40 m
关 E04 重规划：已走 30 m，剩余 80 m，预计合计 110 m
重开 E04：已走 30 m，剩余 40 m，预计合计 70 m
关 E09：P02 不可达；已走 30 m，剩余可行 0 m，预计合计不可用；blocked
恢复 E09：已走 30 m，剩余 40 m，active
走三步到 N02：已走 70 m，剩余 0 m，但仍未完成（P02 未确认）
确认 P02：completed，待检空，已完成 P05/P02，已走 70 m
```

任务导出样例：

```text
runs/check_cd_mission/20261025_150218/C1_mission_export.json
runs/check_cd_mission/20261025_150218/C1_completed_export.json
```

`C1_mission_export.json` 来自“走到 N05 并确认 P05 后”的任务，含
`created / step / confirm / replan` 事件、来源 `simulation_case / C1`、
当前位置 N05、已走 30 m、剩余 40 m、待检 P02、已完成 P05。

`C1_completed_export.json` 是完整走完并确认 P02 后的导出，含关/开 E04、关/开 E09、
受阻、恢复、最终 `completed` 的完整事件序列。

### 12.4 页面启动冒烟

本轮页面测试使用 Streamlit `AppTest`，覆盖 C1 全流程、刷新不重复记账、失效提示和会话隔离。
另启动本地 Streamlit（127.0.0.1:8599）探测健康检查，实际返回 `HTTP 200 / ok`，
日志显示 `Uvicorn server started`；探测后已关闭进程。
没有重新做公网部署。

### 12.5 收尾核查补丁（2026-10-25）

本轮收尾实际发现并修复了两个边界问题：

1. **到达待检设备节点但未确认时仍能继续走**。
   原 `step_forward` 只检查路线和通道，没有检查当前节点是否还有未确认设备。
   已在 `spatial_module/mission.py` 增加拦截：当前节点有待确认设备时，
   抛 `MissionError`，当前位置、已走距离、待检列表均不变。
   页面同时把“沿当前路线前进一步”置灰，并提示先确认检查。

2. **确认低等级设备时没有显式检查更高等级的可达目标**。
   原确认逻辑只看“当前节点是否等于设备节点”。已在 `confirm_asset` 增加等级守卫：
   如果还有更高等级、当前可达的待检目标，拒绝确认低等级设备。
   不可达的高等级目标不阻塞可达低等级目标，保持与 D 的 partial 规划一致。
   通过人为把位置放到 P04 的防御性测试，验证不会绕过 P01/P02。

本轮还用 `inspect.signature` 核对了真实签名：

```text
D plan_inspection(detection_results, start_node='S', closed_edges=None, output_dir='runs') -> dict
Agent plan_asset_inspection(detection_results=None, closed_edges=None, start_node=None,
    input_is_mock=False, mission=None, mission_action='auto', mission_stale=False,
    source_signature='') -> ToolResult
```

README 的工具契约已按真实签名修正，旧的 `detection_results, closed_edges, start_node`
顺序仍然兼容。

模拟 LLM 测试已从“只校验参数”升级为实际经过：

```text
本地伪造 LLM JSON → make_plan → 白名单校验 → Agent 循环
→ plan_asset_inspection → mission.replan_mission
```

验证 C1 在 N05、已确认 P05、已走 30 m 时，关闭 E04 后剩余 80 m。
这不是真实 LLM 测试，没有调用付费 API。

收尾后的最终检查：

```text
check_cd_mission.py：23 通过 / 0 失败 / 23 项
runs/check_cd_mission/20261025_150218/check_result.json

check_cd.py（第一轮回归）：23 通过 / 0 失败 / 23 项
runs/check_cd/20261025_150457/check_result.json
```

### 12.6 交接包临时合并验证

用最初原始备份 `haigong-om-app-main_cd_backup_20261024_192213`
创建临时工程，再叠加交接包中的完整源文件，并复制本机未改的 A/模型/录音/场景用于运行：

```text
临时目录：C:\Users\Zhang\AppData\Local\Temp\opencode\haigong_merge_CD-R2-20261025
check_cd_mission.py：23 通过 / 0 失败
check_cd.py：23 通过 / 0 失败
Streamlit 健康检查：HTTP 200 / ok（127.0.0.1:8600）
```

ZIP 已通过 `testzip`、文件清单 SHA-256、必需文件和禁止内容检查。
本机没有 `git` / `patch` 命令，因此补丁文件作为 B 的参考提供；
合并验证采用“完整源文件叠加”，不是 `patch -p1` 机械应用。
临时合并工程和校验解压目录不在交接 ZIP 内。

## 13. 未测 / 已知限制

1. 没有运行真实 LLM API；第 16、17 项是本地构造/临时替身，不是“真实 LLM 已通过”。
2. 没有在 Python 3.12 上运行；本轮是 3.13.13。
3. 没有真实厂区录音、定位硬件、门禁或多人并发调度。
4. 没有部署公网，没有推送远程仓库。
5. 页面刷新用 AppTest 模拟，没有做浏览器进程级断线重连测试。
6. 任务状态存在 Streamlit Session State，关闭浏览器/会话结束后不保证恢复；当前只保证同会话隔离和导出。
7. 当前路线图是“当前剩余路线”，不画出历史已走轨迹；历史里程和事件在 JSON 中。
8. 单次耗时不是性能基准。

## 14. 第二轮 Windows 复查命令

```powershell
cd C:\Users\Zhang\Desktop\haigong-om-app-main
chcp 65001
$env:PYTHONIOENCODING = "utf-8"

.venv\Scripts\python.exe --version
.venv\Scripts\python.exe check_cd.py
.venv\Scripts\python.exe check_cd_mission.py
.venv\Scripts\streamlit run app.py
```

页面：<http://localhost:8501>

## 15. 给 B 的增量合并提示

需要保留的 Session State：

```text
mission
mission_error
mission_notice
mission_closed_edges
_pending_mission_closed_edges
```

需要保留的接口：

```python
spatial_module.mission.create_mission(...)
spatial_module.mission.step_forward(...)
spatial_module.mission.confirm_asset(...)
spatial_module.mission.replan_mission(...)
spatial_module.mission.mission_is_stale(...)
spatial_module.mission.mission_to_json(...)
```

`plan_asset_inspection` 旧调用不变，新增可选参数：

```python
mission=None, mission_action="auto", mission_stale=False, source_signature=""
```

`AgentResult` 新增：

```text
mission
mission_attempted
```

页面不应把任务的剩余 `plan` 写入第一轮的 `plan_result`；两者必须分开。







