# 海工智维 · 设备运维助手（v0.6：A / C / D + Agent + 日志与报告）

声学B（应用与 Agent）负责的可运行单页应用：**单页 + 单 Agent + 三个工具**，
已把 A 的声音模型、D 的巡检规划、C 的场景绘图接成一条可演示的链路。

Agent 的决策器可切换：**LLM 规划（带白名单校验）/ 规则版**。LLM 只决定「调哪个工具、传什么参数」，
检测分数、路线、距离一律来自 A / D 模块——模型说错话，结果也不会错。

## 数据流（进程内调用，不走 HTTP）

```
上传 WAV ──► A.analyze_audio(path, asset_id) ──► 检测结果列表（schema 1.0）
                                                   │
                                                   ▼
              D.plan_inspection(检测结果, closed_edges) ──► 优先级 / 访问顺序 / 距离
                                                   │
                                                   ▼
                        C.render_route(scene, route_nodes) ──► 路线地图 PNG
```

三家交付的都是**本地 Python 函数**，所以 `real` 模式直接 import，不需要起服务。

## 数据来源与声明（公开仓库必读）

| 内容 | 当前口径 | 性质 |
| --- | --- | --- |
| 检测模型 `models/pump_v1.joblib` | 团队此前的**合成演示包** | **合成演示** |
| `samples/*.wav` | 团队此前的**合成演示录音**，每段 10 s / 16 kHz / 16 bit | **合成演示** |
| `scene/*.csv`（节点、通道、设备编号、距离） | 团队自拟 | **模拟厂区** |
| `knowledge/checklist.json` 中 K-001、K-002 | MIMII、DCASE 2020 Task 2 官方说明 | 公开资料 |
| `knowledge/checklist.json` 中 K-003 至 K-005 | 团队设定 | **自拟规则** |

**来源边界（2026-10-24 核对）**

`models/model_config.json` 里的 `dataset` 字段仍写着 MIMII，但同文件的
`data_root` 是 `data\demo_raw`，训练/验证样本数为 24 / 8，仓库内也没有真实数据训练记录。
因此按团队当前口径：**当前模型和两条录音按合成演示说明，不能据此声称已经完成 MIMII 真实数据训练，
也不能用于评价声音检测准确率。** 本轮不改动模型、训练配置和录音来制造“来源已更新”的假象。

MIMII Dataset（Hitachi, Ltd.，CC BY-SA 4.0，<https://zenodo.org/record/3384387>）
是后续**计划采用**的公开数据集参考。等 A 提供新的训练记录、数据清单和模型后，
再按实际来源恢复/补充许可与引用说明。引用格式：

> H. Purohit, R. Tanabe, K. Ichige, T. Endo, Y. Nikaido, K. Suefusa, and Y. Kawaguchi,
> “MIMII Dataset: Sound Dataset for Malfunctioning Industrial Machine Investigation and Inspection,”
> in *Proc. 4th Workshop on Detection and Classification of Acoustic Scenes and Events (DCASE)*, 2019.

**边界声明**

1. 检测分数是相对异常程度，**不是故障概率**，也不判断具体损坏部件；
2. `scene/` 是模拟厂区，路线与距离仅用于演示调度逻辑，不构成实际作业指导；
3. 当前模型/录音按合成演示口径，换机型或换真实数据须由 A 重新评估并记录；
4. 本仓库为课程团队共同成果，仅用于课程展示；成员分工见 `docs/` 与实施手册。

## 快速开始（Windows）

> 仓库**不含** `.venv`（虚拟环境约 500 MB，已被 `.gitignore` 排除），克隆后先建环境，否则 `.venv\Scripts\...` 会报「系统找不到指定的路径」。

```bash
git clone https://github.com/xiao-o-o/haigong-om-app.git
cd haigong-om-app
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

依赖装好后启动，**不需要任何密钥**（未配置 LLM 时自动走规则版决策器）：

```bash
.venv\Scripts\streamlit run app.py
```

浏览器打开 http://localhost:8501 。macOS / Linux 把 `.venv\Scripts` 换成 `.venv/bin`。

要求 Python **3.11+**（`core/llm.py` 用标准库 `tomllib` 读配置）；全部测试结论出自 3.13.14。

`models/pump_v1.joblib` 是 sklearn 序列化对象，**必须与训练时的 sklearn 版本同代**（`requirements.txt` 已锁 `scikit-learn>=1.9,<2`），版本不符会在 real 模式报反序列化错误——那不是代码问题，重新导出模型即可。

接真实 LLM 前先跑自检（四步全通过再开页面）：

```bash
.venv\Scripts\python.exe check_llm.py
```

### Python 版本口径（待团队确认）

| 项 | 值 | 说明 |
| --- | --- | --- |
| 实施手册要求 | 3.12 | 同进程 import 三家模块 |
| 本机本轮实测 | 3.13.13 | 项目独立 `.venv`；全部 C/D 检查结论在这个版本上得出 |
| `runtime.txt` 当前值 | python-3.13 | 与实测环境一致，不改动 |

系统只注册了 Python 3.12.8，没有找到 3.13；本轮用 `uv` 下载 Python 3.13.13
并在项目内创建 `.venv`。旧的 3.13.14 记录是上一轮环境，本轮没有复测 3.13.14。
若团队统一到 3.12，先在目标版本上跑这一条，通过再改 `runtime.txt`：

```bash
python -c "import sys, audio_module.infer as m; print(sys.version); print(m.analyze_audio)"
```

模型是 joblib 序列化的 sklearn 对象，跨 Python minor 版本反序列化只在同 sklearn 版本时才稳。
若 3.12 下加载报错，属于 A 的训练环境问题，需要 A 在 3.12 下重新导出 `models/pump_v1.joblib`，B 侧不改代码。

## 演示路径（三种规划状态都已实测）

| 场景 | 操作 | 实测结果 |
| --- | --- | --- |
| 正常流程（真实模型） | 「② 音频检测」选「内置演示录音（P01 正常 / P04 异常）」→ 开始检测 → 「③ 巡检规划」直接生成 | `status=ok`，唯一目标 `P04`，`total_distance_m=10.0` |
| 正常流程（全场景） | 切「全场景模拟（P01–P06）」→ 开始检测 → 规划 | `status=ok`，优先级 `P03→P04→P06`，访问顺序 `P03→P06→P04`，`170.0 m` |
| 部分达成 | 检测 P01+P04 → 规划时临时关闭 `E11`（S→N04） | `status=partial`，`visit_order=[]`，`unreachable_asset_ids=['P04']` |
| 无需巡检 | 只上传 P01（正常） | `status=no_targets`，明确说明没有目标，不凑路线 |

「⑤ Agent 助手」可一句话串起两步：`检测 P01 和 P04，然后关闭 E04 出一条巡检路线`。

### C/D 联合优化（第一轮 + 第二轮）

补丁标识：`CD-R2-20261025`。

第一轮：

- 「③ 巡检规划」新增「规划案例对照（模拟输入）」：读取 `examples/cd_cases.json`，
  实际调用 D 计算固定顺序与优化方案，C 画两张路线图和一张对比图。
- 模拟案例与录音检测状态分开保存，不覆盖 A 的检测结果，也不会写进录音检测报告。
- 命令行检查：`.venv\Scripts\python.exe check_cd.py`；
  案例输出：`.venv\Scripts\python.exe examples\run_cd_cases.py`。

第二轮：

- 新增「巡检执行（模拟）」：记录当前位置、已完成设备、待检设备、累计已走距离和剩余路线。
- 可以按相邻开放通道一步一步走；到达设备节点后由人员确认完成，不自动确认，不重复统计。
- 支持从当前位置关闭/重新开放通道并重规划；已走距离和已完成设备不会被重置。
- 全部待检目标都不可达时显示“受阻”，不会显示“巡检已完成”；只有全部目标确认后才完成。
- 支持从录音检测结果或 C1/C2/C3 手工模拟案例创建任务，两种来源分开保存，可导出任务 JSON。
- 第二轮检查：`.venv\Scripts\python.exe check_cd_mission.py`。
- 任务范围、修改记录、验证结果：`docs/CD_TASK.md`、`docs/CD_WORKLOG.md`；
  精确依赖：`docs/CD_ENV_LOCK_20261025.txt`；
  给 B 的交接说明：`docs/CD_HANDOFF.md`；
  C1 演示步骤：`docs/CD_DEMO_C1.md`。

## 目录结构

```
app.py                  单页主入口（页内五区：总览/检测/规划/资料/Agent）
agent/
  loop.py               单 Agent 主循环：6 轮 / 12 次预算 + 轨迹计时 + 日志出口
  planner.py            决策器：LLM 规划 + 白名单校验 + 规则版回退
  tools.py              三个工具：detect_assets_audio / plan_asset_inspection / get_checklist
core/
  local_modules.py      三家模块唯一 import 入口 + 场景表读取 + 健康检查
  llm.py                OpenAI 兼容客户端（只给决策器用，不生成业务数据）
  report.py             Markdown 报告生成（纯函数，可脱离 Streamlit 测试）
  runlog.py             运行日志：run_id、脱敏、参数摘要
  config.py             运行模式等配置（环境变量注入）
  api_client.py         HTTP 客户端（当前未使用，保留以备后续改为服务化部署）
audio_module/  models/  samples/     A 交付（本轮未改动）
spatial_module/  scene/              C/D（兼容扩展，公开规划/绘图入口保留）
spatial_module/mission.py 巡检执行任务：行走、确认、从当前位置重规划、导出
core/cd_state.py        输入指纹、检测结果指纹与规划上下文比较
core/cd_cases.py        模拟规划案例读取
examples/cd_cases.json  三个手工设定的规划案例
examples/run_cd_cases.py 案例运行脚本（JSON / CSV / 三张图）
check_cd.py             第一轮 C/D 检查入口（23 项）
check_cd_mission.py     第二轮巡检执行检查入口（23 项）
knowledge/checklist.json  检查条目（source_id/标题/URL/适用条件/检查建议）
check_llm.py            LLM 接入自检（配置 / 连通 / 规划 / 越权拦截四步）
runs/                   运行时产物：上传暂存、频谱图、路线图、<run_id>/run_log.json
.streamlit/config.toml  服务配置（0.0.0.0、headless、反向代理 CORS/XSRF）
.streamlit/secrets.toml.example   密钥模板（复制成 secrets.toml 用，后者不入库）
```

## 工具接口契约（与 Agent 的分工边界）

| 工具 | 入参 | 返回 | 谁提供数据 |
| --- | --- | --- | --- |
| `detect_assets_audio(asset_ids, files=None)` | 只接受**设备编号数组**，如 `["P01","P04"]` | `requested_asset_ids` / `missing_asset_ids` / `results`（沿用 A 字段） | A `analyze_audio` |
| `plan_asset_inspection(detection_results=None, closed_edges=None, start_node=None, input_is_mock=False, mission=None, mission_action="auto", mission_stale=False, source_signature="")` | 旧三参数调用不变；有活动任务时传 `mission`，返回任务剩余规划 | 静态规划返回 D 原始字段；任务模式返回 `{mode, action, mission, plan}` | D `plan_inspection` / `spatial_module.mission` |
| `get_checklist(query)` | 关键词，可空 | `items`（`source_id`/`title`/`url`/`conditions`/`advice`/`kind`）+ `sources` | knowledge/checklist.json |

真实签名（本轮用 `inspect.signature` 核对）：

```python
plan_inspection(detection_results, start_node="S", closed_edges=None, output_dir="runs") -> dict
plan_asset_inspection(
    detection_results=None, closed_edges=None, start_node=None, input_is_mock=False,
    mission=None, mission_action="auto", mission_stale=False, source_signature="",
) -> ToolResult
```

`plan_asset_inspection` 的前三个旧参数仍按原顺序兼容；`mission` 之后的参数都有默认值。
没有活动任务时它仍返回 D 的静态规划字段；有活动任务时返回任务模式的 `mission + plan`。

三条边界，改代码时不要破：

1. **路径不由模型指定**。模型只能给设备编号，`asset_id → 文件路径` 的映射由页面注入（`files`），
   模型没有文件系统权限，也没法编造一个不存在的路径；
2. **规划只认检测结果**。没有检测结果直接返回失败提示，不按设备清单猜目标；
3. **`missing_asset_ids` 不等于正常**。请求了但没有音频的设备单独列出，不产生分数，页面会提示。

编号校验在 `planner._validate_plan` 与 `tools.detect_assets_audio` 各做一次：
场景表外的编号（如 P99）一律拒绝并原样报出，不会替换成别的设备。

## 知识库

`knowledge/checklist.json`，3–5 条，字段固定：

| 字段 | 含义 |
| --- | --- |
| `source_id` | 条目编号 K-001… |
| `title` | 标题 |
| `url` | 来源链接；团队自拟规则留空 |
| `conditions` | 适用条件 |
| `advice` | 检查建议 |
| `kind` | `public` 公开资料 / `team` 团队设定 |

`kind=team` 的条目是团队自拟规则，页面标注为「团队设定」，不代表行业标准。
公开条目的链接需人工点开核对一次再引用。

## 日志与报告

- **运行日志**：每次检测、每次 Agent 任务各生成一个独立 `run_id`，写 `runs/<run_id>/run_log.json`。
  含 run_id、`app_version`、起止时间、运行模式、决策器、工具参数摘要、每步 status / result_summary / duration_ms、错误原文、最终结论。
  写盘前统一过 `runlog.redact()`：键名命中黑名单（api_key / authorization / token…）或文本里像密钥的串都会打码。
- **报告**：页面「② 音频检测」可下载 Markdown 报告，分五节：检测结果 / 巡检计划 / 模拟与适用条件 / 人工复核建议 / 运行日志路径。
  所有数字原样取自工具返回，报告里不重算。
- 日志写盘失败不影响任务本身，页面会提示「日志写入失败（不影响结果）」。

## LLM 决策器

三层设计，任一层出问题都不会污染结果：

| 层 | 做什么 | 出问题时 |
| --- | --- | --- |
| LLM | 把任务翻译成工具序列 `{"steps":[...]}` | 超时 / 非 200 / 非 JSON → 回退规则版 |
| 校验 | 工具名白名单、设备编号只能取已送检文件、通道/节点只能取场景表、步数 ≤ 6、先检测后规划 | 任一条不过 → 回退规则版，轨迹写明拒绝原因 |
| 执行 | 循环 + 预算 + 汇总，只认工具真实返回 | 工具失败即停，不兜底、不编造 |

三种模式在侧边栏切换：**自动**（默认，LLM 优先）/ **仅 LLM**（失败即报错，演示用）/ **仅规则版**（离线）。

配置方式（`.streamlit/secrets.toml` 同名键，或环境变量；密钥不落代码）：

```toml
LLM_BASE_URL = "https://api.deepseek.com/v1"   # 必须带版本段，代码只在其后拼 /chat/completions
LLM_MODEL    = "deepseek-flash"             # deepseek-chat 已于 2026-07-24 停用，写它必 404
LLM_TIMEOUT  = 30
LLM_THINKING = "auto"                          # 对 deepseek/qwen 自动关闭思考模式
LLM_API_KEY  = "sk-..."                        # 填真值；不要留空串占位
```

不配置也能完整运行，只是走规则版决策器。

### 接入真实 LLM 的三步

1. 编辑 `.streamlit/secrets.toml`，把 `LLM_API_KEY` 那行的 `#` 去掉并填真 key；
2. 跑自检：`.venv\Scripts\python.exe check_llm.py`，四步全 `[通过]` 才算接通；
3. 启动应用，侧边栏「决策器」选【自动】。演示时可临时切【仅 LLM】证明不是回退。

`check_llm.py` 依次检查：配置读取 → 连通性（真实发一次请求）→ 规划能力（三个真实任务）→ 越权拦截（故意引用 P99）。任一步失败都给出对应排查方向，退出码非 0。

```bash
.venv\Scripts\python.exe check_llm.py                       # 用 secrets.toml 里的配置
.venv\Scripts\python.exe check_llm.py --planner llm         # 不回退，暴露真实错误（排障用）
.venv\Scripts\python.exe check_llm.py --model deepseek-v4-pro     # 临时换模型，不落盘
```

### 配置优先级与一个坑

优先级：**显式设置的环境变量 > `secrets.toml` > `st.secrets`**。
「显式」指环境变量值与文件值不同——因为 Streamlit 首次读取 secrets 时会把文件里的键**写入 `os.environ`**，两者相同说明那个环境变量是同步来的，不算覆盖。

所以 `.streamlit/secrets.toml` 里**不要写 `LLM_API_KEY = ""` 这种空占位**：空串会被同步进环境变量，把外面真实的 `LLM_API_KEY` 冲成空，表现为「明明配了却显示未配置」。用不到的行直接注释掉。

改 `secrets.toml` 后需重启 streamlit（页面热重载不会重读密钥；`core/llm.py` 内部按文件 mtime 重读，脚本场景无需重启）。

### 常见错误对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| HTTP 401 / 403 | 密钥无效、被截断、无该模型权限 | 重新复制 key，确认账号已开通该模型 |
| HTTP 404 | `api_base` 或模型名不对 | `api_base` 到 `/v1` 为止，不含 `/chat/completions`；模型名不能写 `deepseek-chat`（已停用） |
| 响应很慢（>20s） | 模型默认开启了思考模式 | 保持 `LLM_THINKING = "auto"`；DeepSeek V4 默认 effort=high，思考 token 也计费 |
| HTTP 429 | 限流或余额不足 | 稍后重试或换 key |
| 请求超时 | 网络到不了该地址 | 查代理/防火墙，或调大 `LLM_TIMEOUT` |
| 规划被校验拒绝 | 模型编造了设备/通道编号 | 正常防护，会回退规则版并在轨迹写明原因 |

任一失败都不会污染结果，页面照常出数。

## 两种模式的区别

| | 模拟数据（mock） | 真实模型（real） |
| --- | --- | --- |
| 检测 | 固定演示分数（P01/P04 取 A 的样例真实分数），不加载模型 | 调 A 的 `pump_v1` 模型逐文件推理 |
| 规划 | D 的真实算法，输入是模拟分数 | D 的真实算法，输入是真实分数 |
| 页面标注 | 红色警示横幅 | 绿色说明横幅 |

## 输入约束（v1，不做重采样）

- 只接受 **16 kHz 单声道 WAV**，时长 ≥ 1 秒；
- 每个文件必须手动指定设备编号 P01–P06（A 不从文件名推断）；
- 采样率不符、空文件、全零、过短 → A 返回 `invalid_input`，页面如实显示，不算「正常」。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| APP_MODE | mock | mock=模拟检测数据；real=调 A 的真实模型 |
| LLM_BASE_URL | 空 | OpenAI 兼容接口地址，需带版本段，如 `https://api.deepseek.com/v1` |
| LLM_API_KEY | 空 | 密钥；也可写在 `.streamlit/secrets.toml` |
| LLM_MODEL | 空 | 模型名，如 `deepseek-flash`（`deepseek-chat` 已停用） |
| LLM_TIMEOUT | 30 | 读取超时（秒）；超时即回退规则版 |
| LLM_TEMPERATURE | 0 | 决策任务建议 0 |
| LLM_THINKING | auto | `auto`/`1`/`0`，控制是否关闭模型的思考模式；DeepSeek V4 默认思考且思考 token 计费，`auto` 会自动关闭 |

三项 LLM 变量缺任意一项 → 视为未配置，自动走规则版。
`APP_MODE` 只影响默认值，页面侧边栏的「运行模式」以页面选择为准。

## 部署到公网

### 方式一：内置在线发布（已用）

由平台把目录打成单端口 HTTP 服务并给出分享链接，启动命令：

```bash
python -m streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true
```

平台只注入 `PORT`，其余配置都在 `.streamlit/config.toml`。模型文件随包提交，real 模式可直接用。

公网版要接真实 LLM，有两条路：

- **随包带 secrets**：把填好的 `.streamlit/secrets.toml` 一起上传（它只在 `.gitignore` 里，不影响打包）。换 key 需重新发布；
- **平台环境变量**：在发布配置里设 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`，不用把密钥打进包。前提是 `secrets.toml` 里不要留空的同名键（见上一节）。

外部分享前把 key 换成专用、限额的，别用主账号那把。

### 方式二：Streamlit Community Cloud（需自己建仓库）

1. GitHub 新建仓库，推送本目录（`.gitignore` 已排除 `.venv` 与 `runs/`）；
2. share.streamlit.io → New app → 选仓库 / 分支 / `app.py`；
3. `models/pump_v1.joblib`（约 800 KB）必须一起提交，否则 real 模式报「模型文件缺失」；
4. 在 Secrets 面板填 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`（不填则走规则版）；
5. 免费实例会休眠，首次访问约 1 分钟唤醒。

## 交付物清单

| 交付项 | 位置 | 状态 |
| --- | --- | --- |
| 主应用 | `app.py` | 完成 |
| Agent | `agent/loop.py`、`agent/planner.py`、`agent/tools.py` | 完成 |
| 知识库 | `knowledge/checklist.json`（5 条，public/team 分开标注） | 完成 |
| 配置示例 | `.streamlit/secrets.toml.example` | 完成 |
| 合并依赖 | `requirements.txt`（A/C/D 三家 + 应用层，已合并） | 完成 |
| 部署说明 | 本 README「部署到公网」节 + `start.sh` + `runtime.txt` | 完成 |
| 在线链接 | 平台发布通道（见「部署到公网 · 方式一」） | v0.6 已发（含 CD-R2 巡检执行） |
| 三组提示词及真实输出 | `docs/prompts_and_outputs.md` | 完成（LLM 决策器真实输出，含 run_id 与耗时） |
| 日志与测试记录 | `docs/acceptance.md`、`runs/<run_id>/run_log.json` | 完成 |
| LLM 接入自检 | `check_llm.py` | 完成 |
| 启动包与演示视频 | 同版本目录包 + 录屏 | 待出（不能代替在线链接） |

## 当前状态

- [x] 单页五区 + Agent 三工具
- [x] 合并 A（audio_module + models + samples）、C/D（spatial_module + scene）
- [x] 工具签名按真实契约重写：`detect_assets_audio(asset_ids)`，返回 `requested_asset_ids / missing_asset_ids / results`
- [x] 端到端链路（A→D→C）在本机跑通（mock 与 real 双模式）
- [x] 部署公网（v0.6 已发，含 LLM 真实接入与巡检执行）
- [x] Agent 决策器接入 LLM（白名单校验 + 规则版回退；未配置时只提供规则版）
- [x] 每次运行独立 run_id + 日志脱敏落盘
- [x] 可下载 Markdown 报告（结果 / 模拟条件 / 人工复核建议分节）
- [x] 第二轮巡检执行（模拟）：当前位置、已完成/待检、封路重规划、已走/剩余距离、任务 JSON
- [x] 知识库结构化 5 条（公开资料 + 团队设定分开标注）
- [ ] Python 3.12 实测（本机只有 3.13，见「Python 版本口径」）
- [x] LLM 决策器的真实输出采集（三组，见 `docs/prompts_and_outputs.md`）

测试记录与三组提示词：

- `docs/acceptance.md`：9 项功能验收的真实结果 + 未测项清单
- `docs/prompts_and_outputs.md`：三组提示词、执行轨迹、Agent 原文回答、run_id
- `docs/publish-github.md`：发布到 GitHub 的完整步骤、推送后自查清单、常见报错

## 待办与风险

| 项 | 说明 |
| --- | --- |
| 发布包密钥 | 线上密钥随部署目录上传，不在仓库里；换 key 后需重新发布才对线上生效 |
| 3.12 口径 | 团队若统一 3.12，先按 README 那条命令验模型加载，再改 `runtime.txt` |
| 密钥外发 | 公网演示前换成限额专用 key，别用主账号那把 |
| A 的路径要求 | A 用 `Path(audio_path).exists()` 判断，传绝对路径最稳；传相对路径会按进程 CWD 解析 |

## 红线（摘自实施手册第十二章）

1. 模拟数据必须显著标明「模拟」，不得作为正式结论；
2. 上传音频只用于检测，不触发任何训练；
3. 模型缺失 / 调用失败直接报错，绝不当成「设备正常」，绝不用模拟数据兜底；
4. 工具调用次数多 ≠ 质量好，预算（6 轮 / 12 次）是硬约束；
5. A 的 `audio_module/`、`models/`、`samples/` 本轮保持原样；C/D 只做兼容扩展，
   公开入口 `render_route` 与 `plan_inspection` 保留，改动记录见 `docs/CD_WORKLOG.md`；
6. 分数是相对异常程度，不是故障概率，也不判断具体损坏部件。
