# 验收测试记录

记录日期：2026-10-23 ｜ 环境：Windows，Python 3.13.14，`.venv`，app_version 0.6.0

这张表只写**实际跑过**的项。没跑的一律标「未测」并写原因，不写「应该没问题」。

## 一、功能验收

| # | 验收项 | 操作 | 实际结果 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | 单台检测 | `real` 模式检测 P04（`P04_sample.wav`） | `P04 · suspected_anomaly · 0.6187`，状态 ok，4546 ms | 通过 |
| 2 | 多台检测 | `real` 模式检测 P01 + P04 | `P01 normal 0.4557`、`P04 suspected_anomaly 0.6187`，1 次工具调用 | 通过 |
| 3 | 通道关闭 | 关闭 E04 后规划（基于上一步检测结果） | `status=ok`，`visit_order=["P04"]`，`total_distance_m=10.0` | 通过 |
| 4 | 不可达 | 关闭 E11 后规划，目标含 P04 | `status=partial`，`visit_order=[]`，`unreachable_asset_ids=["P04"]`，`0.0 m` | 通过 |
| 5 | 坏 WAV | 把非 WAV 内容存为 `.wav` 送检（real） | `status=invalid_input`，`reason_code=decode_failed`，notes 带真实解码错误 | 通过 |
| 6 | 未知编号 | 提示词 `检测 P99` | 工具报「未知设备编号：P99…」，Agent `【失败】` 并停止，未替换成其他设备 | 通过 |
| 7 | 无 API | 未配置密钥，决策器=仅 LLM | `【失败】LLM 决策器调用失败：LLM 未配置：LLM_API_KEY，Agent 的 LLM 模式不可用。已按设置停止，未回退规则版。` | 通过 |
| 7b | 无 API（自动） | 未配置密钥，决策器=自动 | `planner=rule`，note 写明「规则版（未配置 LLM：LLM_API_KEY）」，功能完整 | 通过 |
| 8 | LLM 超时 | 假服务延迟 6 s，`LLM_TIMEOUT=3`，决策器=仅 LLM | 3.0 s 后 `【失败】LLM 请求超时（3s）`，未执行任何工具 | 通过 |
| 8b | LLM 超时（自动） | 同上，决策器=自动 | `planner=rule_fallback`，note 写明「LLM 调用失败回退：LLM 请求超时（3s）」 | 通过 |
| 9 | 不同会话隔离 | 两个独立 Streamlit 会话，会话 1 检测 2 台 | 会话 2 的 `detections=0`、`detect_run_id=""`，无数据串用 | 通过 |
| 9b | 真实 LLM 决策 | 配 DeepSeek `deepseek-v4-flash`，决策器=仅 LLM，跑三组提示词 | 三组均 `planner=llm`，0.9–1.2 s 出计划；分数与路线仍由 A / D 产出 | 通过 |
| 9c | LLM 编排知识库 | 提示词 `查一下疑似异常怎么复核` | 只调 `get_checklist`，命中 K-004（复核流程）、K-002，未多调检测或规划 | 通过 |

补充：会话 1 的 `run_id` 为 `20261023-090819-…`，会话 2 未产生 run_id，确认状态不共享。

补充：第 7 / 7b 条采集于**配置密钥之前**。现在 `secrets.toml` 已填真实密钥，复现这两条需临时清空 `LLM_API_KEY`，否则会走通而不是报错。

## 二、机制验收

| # | 项目 | 实际结果 | 判定 |
| --- | --- | --- | --- |
| 10 | 工具入参边界 | 模型只能给 `asset_ids`；路径由页面注入 `files`，工具内解析 | 通过（代码层） |
| 11 | 未知编号双重校验 | `planner._validate_plan` 与 `tools.detect_assets_audio` 各拦一次 | 通过 |
| 12 | 复用检测结果 | 已有检测结果时说「把结果做成巡检计划」，只调 `plan`，不重复检测 | 通过 |
| 13 | 日志脱敏 | 写入含 `api_key`、`Authorization`、`sk-xxxx` 的字段，落盘均为 `***` | 通过 |
| 14 | 日志不阻塞主流程 | 写盘异常时返回空路径，页面提示「日志写入失败（不影响结果）」 | 通过（代码层） |
| 15 | 预算上限 | 6 轮 / 12 次，规划器产出的步数超过 6 时被 `_validate_plan` 拒绝 | 通过（代码层） |
| 16 | 模型缺失不冒充正常 | 改名 `models/pump_v1.joblib` 后 real 检测返回失败并带错误，不回退 mock | 通过（10/22 实测） |
| 17 | 页面无异常 | `AppTest` 跑 mock / real 两种模式，`exception` 为空 | 通过 |

## 三、未测项（写清原因，不当作已通过）

| 项目 | 状态 | 原因 | 谁来补 |
| --- | --- | --- | --- |
| Python 3.12 运行 | **未测** | 本机只有 3.13.14，无 3.12 解释器 | 有 3.12 环境的人按 README「Python 版本口径」那条命令验一次 |
| 公网部署（v0.6 这版） | **未重新发布** | 10/22 发布的是 v0.5；本次改动后需重新发布才生效 | 发布后补一条在线链接与截图 |
| 真实厂区音频 | **未测** | 只有 MIMII 样例，无现场录音 | 有条件时补 |
| 多用户并发 | **未测** | 单机测试，未压测 | 非首版要求 |

## 四、复现命令

```bash
# 全量冒烟（页面无异常）
.venv\Scripts\python.exe -c "from streamlit.testing.v1 import AppTest; at=AppTest.from_file('app.py', default_timeout=300).run(); print(at.exception)"

# 单台 + 多台 + 规划
.venv\Scripts\python.exe -c "from agent.loop import run_agent; r=run_agent('检测 P01 和 P04，然后关闭 E04 出一条巡检路线', mode='real', context={'files':[{'asset_id':'P01','path':'samples/P01_sample.wav'},{'asset_id':'P04','path':'samples/P04_sample.wav'}]}, planner='rule'); print(r.answer); print(r.run_id)"

# 未配置 LLM 的「仅 LLM」
.venv\Scripts\python.exe -c "from agent.loop import run_agent; print(run_agent('检测 P04', mode='real', context={'files':[]}, planner='llm').answer)"
```
