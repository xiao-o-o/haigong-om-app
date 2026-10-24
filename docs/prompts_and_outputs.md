# 三组提示词与真实输出

采集条件（写清楚是为了别人能复现）：

| 项 | 值 |
| --- | --- |
| 时间 | 2026-10-23 20:47（+08:00） |
| 环境 | Windows，Python 3.13.14，`.venv` |
| 运行模式 | `real`（真调 A 模型与 D 规划） |
| 决策器 | `llm`（DeepSeek `deepseek-v4-flash`，OpenAI 兼容接口） |
| 音频 | `samples/P01_sample.wav`（正常）、`samples/P04_sample.wav`（异常） |
| 采样率 | 16 kHz 单声道 WAV |
| app_version | 0.6.0 |

> **LLM 的职责边界**：模型只输出 `{"steps":[{"tool":...,"args":...}]}`，决定调哪个工具、传什么参数。
> 检测分数、阈值、访问顺序、总距离全部来自 A / D 模块的返回值，模型不参与计算、也改不了数字。
> 三组的 `planner` 均为 `llm`，轨迹里「真实数据」标记表示不是模拟。

每组的 `run_id` 可在 `runs/<run_id>/run_log.json` 里查到完整记录（参数摘要、真实结果、耗时，密钥已脱敏）。

---

## 组 1：单台检测

**提示词**

```
检测 P04 的声音
```

**模型决策**（原文）

```
LLM 规划（deepseek-v4-flash）：用户只要求检测 P04 的声音，上下文中有 P04 的音频文件，
直接调用音频检测工具即可，无需规划巡检。
```

**执行轨迹**

| 轮次 | 工具 | 参数 | status | 耗时 | result_summary |
| --- | --- | --- | --- | --- | --- |
| 1 | `detect_assets_audio` | `{"asset_ids": ["P04"]}` | ok | 5148 ms | 检测 1 台，疑似异常 1 台 |

**Agent 回答（原文）**

```
- 第 1 步 detect_assets_audio（真实数据）：检测 1 台，疑似异常 1 台
```

**结果**：`P04 · suspected_anomaly · score=0.6187`（阈值 0.5455）
**run_id**：`20261023-204707-08972c`

> 耗时说明：5148 ms 里绝大部分是**首次加载 A 的模型文件**，不是 LLM 的耗时。
> 模型常驻后同一批文件检测约 460 ms（见组 2）。答辩时第一次点击会多等几秒，属正常。

---

## 组 2：多台检测 + 关闭通道出路线

**提示词**

```
检测 P01 和 P04，然后关闭 E04 出一条巡检路线
```

**模型决策**（原文）

```
LLM 规划（deepseek-v4-flash）：先对可用音频文件的 P01、P04 做异常检测，
再基于检测结果在关闭 E04 的条件下规划巡检路线
```

**执行轨迹**

| 轮次 | 工具 | 参数 | status | 耗时 | result_summary |
| --- | --- | --- | --- | --- | --- |
| 1 | `detect_assets_audio` | `{"asset_ids": ["P01", "P04"]}` | ok | 460 ms | 检测 2 台，疑似异常 1 台 |
| 2 | `plan_asset_inspection` | `{"closed_edges": ["E04"], "start_node": "S"}` | ok | 639 ms | 规划状态 ok，访问顺序 P04，总距离 10.0 m，不可达 0 台 |

**Agent 回答（原文）**

```
- 第 1 步 detect_assets_audio（真实数据）：检测 2 台，疑似异常 1 台
- 第 2 步 plan_asset_inspection（真实数据）：规划状态 ok，访问顺序 P04，总距离 10.0 m，不可达 0 台
```

**结果**：`P01 · normal · 0.4557`、`P04 · suspected_anomaly · 0.6187`；
规划 `status=ok`，`visit_order=["P04"]`，`total_distance_m=10.0`，`unreachable_asset_ids=[]`
**run_id**：`20261023-204713-de9cd2`

---

## 组 3：查检查资料

**提示词**

```
查一下疑似异常怎么复核
```

**模型决策**（原文）

```
LLM 规划（deepseek-v4-flash）：用户询问疑似异常的复核方法，属于检查标准与资料检索，
应调用 get_checklist 查询相关流程。
```

**执行轨迹**

| 轮次 | 工具 | 参数 | status | 耗时 | result_summary |
| --- | --- | --- | --- | --- | --- |
| 1 | `get_checklist` | `{"query": "疑似异常 复核"}` | ok | 0 ms | 命中 2 条：K-004 疑似异常复核流程（团队设定）；K-002 DCASE 2020 Task 2 机器无监督异常声音检测 |

**Agent 回答（原文）**

```
- 第 1 步 get_checklist（真实数据）：命中 2 条：K-004 疑似异常复核流程（团队设定）；
K-002 DCASE 2020 Task 2 机器无监督异常声音检测
```

**run_id**：`20261023-204715-cc688a`

> 模型自己把「怎么复核」拆成了关键词 `疑似异常 复核`，检索按相关度排序，K-004 排第一。
> 知识条目里 `K-003 / K-004 / K-005` 标 `team`（团队设定），`K-001 / K-002` 标 `public`（公开资料），
> 页面上分开标注，不混为一谈。

---

## 对照：同一组提示词的规则版输出

未配置密钥或 LLM 失败时，同一个问题会走规则版决策器，工具序列基本一致，但没有自然语言决策说明。
采集于 2026-10-23 09:06，`planner=rule`：

| 组 | 规则版轨迹 | run_id |
| --- | --- | --- |
| 1 | `detect_assets_audio {"asset_ids":["P04"]}` | `20261023-090625-3d0e06` |
| 2 | `detect_assets_audio {"asset_ids":["P01","P04"]}` → `plan_asset_inspection {"closed_edges":["E04"]}` | `20261023-090629-a1bf1f` |
| 3 | `get_checklist {"query":"复核"}` | `20261023-090630-2135cd` |

差异：规则版靠关键词判断意图，第三组的 query 直接取命中词「复核」；LLM 版把整句拆成「疑似异常 复核」，
召回更准。

---

## 补测：未配置 LLM 时的「仅 LLM」模式

**提示词**：`检测 P04`，决策器设为「仅 LLM」。

**Agent 回答（原文）**

```
【失败】LLM 决策器调用失败：LLM 未配置：LLM_API_KEY，Agent 的 LLM 模式不可用。已按设置停止，未回退规则版。
```

**run_id**：`20261023-090739-f5d48d`

这条是用来证明「不会偷偷换成规则版」的反例，答辩时可以直接展示。
注意：现在 `secrets.toml` 已填真实密钥，复现这条需临时清空 `LLM_API_KEY`。

---

## 复现方式

```bash
# 页面路径（推荐，能看到完整轨迹与下载按钮）
.venv\Scripts\streamlit run app.py
# 侧边栏：运行模式=真实模型，决策器=仅 LLM
# ② 音频检测 → 音频来源=内置演示录音 → 开始检测
# ⑤ Agent 助手 → 输入上面三句提示词 → 交给 Agent

# 命令行路径（只跑 Agent 层，不开页面）
.venv\Scripts\python.exe -c "from agent.loop import run_agent; r=run_agent('检测 P04 的声音', mode='real', context={'files':[{'asset_id':'P04','path':'samples/P04_sample.wav'}]}, planner='llm'); print(r.answer, r.run_id)"

# 先验证密钥本身可用
.venv\Scripts\python.exe check_llm.py
```
