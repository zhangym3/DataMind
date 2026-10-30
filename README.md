# Insight Agent · DataMind

Windows 10/11 x64、Python **3.11** 的本地指标分析桌面应用，当前交付版本 **1.0.0-rc1**。使用 PySide6 / Qt WebEngine 展示三栏工作台，后台独立进程通过固定 LangGraph 流程运行。本地解析 Excel / Word，在线模型协助提取和解释，指标数值由本地精确引擎计算。

代码已接入真实持久化、导入、计算与导出；当前是候选交付版本。本轮已验证 Deepseek 真实说明解析和诊断报告链路，真实资料的指标数值、报告版面、独立目标机及业务口径签字仍待验收。详见 [取数回填方案](docs/definition-extraction-remediation-2026-10-30.md)、[本轮验收记录](docs/acceptance-document-fill-2026-10-30.md) 和 [剩余验收条件](docs/unfinished-features.md)。不支持 Windows 7。

## 启动

已打包版本：双击 `dist/InsightAgent/InsightAgent.exe`。分发时保留整个 `InsightAgent` 目录，不能只复制 EXE。目标电脑无需另装 Python、Node.js、Office 或 LibreOffice；旧 `.doc` 使用内置 Python 文本解析器，PDF 页面预览仍需要可用的 LibreOffice 渲染器。

源码运行：双击 `start.bat`，或在项目根目录执行：

```powershell
py -3.11 -m venv .venv311
.\.venv311\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv311\Scripts\python.exe main.py
```

本工程已配置 `.venv311`，无需重复创建。PyCharm 运行配置 `Insight Agent` 使用 `.venv311/Scripts/python.exe`。不要使用旧 `.venv` 或系统的其他 Python 版本。

2026-10-30 的取数回填优化、指标管理恢复、手工新增及[被引用草稿删除修复](docs/definition-deletion-fix-2026-10-30.md)已落入源码，尚未重新构建 `dist`，验证本轮变更请使用源码启动。

独立解析指标说明：上传 `.doc` / `.docx` 后点击右侧“解析指标说明”，也可输入同名命令。无需 Excel，可核对指标目录、公式、条件、待确认项及原文依据；分批保存进度，失败后支持继续解析。在线提取需要已配置的模型服务。

可指定独立数据目录：

```powershell
.\.venv311\Scripts\python.exe main.py --data-dir D:\DataMindWorkspace
```

默认数据目录为 `%LOCALAPPDATA%\DataMind`；设置中可备份、恢复、迁移或切换目录。迁移后按提示重新启动。

## 使用流程

1. 打开“设置 → 模型服务”，可新建多套连接配置，分别填写 HTTPS Base URL、协议、模型名称和 Key，并选择启用及默认连接。支持 OpenAI Responses、Chat Completions 兼容服务和 Anthropic Messages。连接测试即时发送固定 JSON 样例并返回结果，不进入后台处理；测试不会清空或删除 API Key，仅证明连接及 JSON 返回能力。
2. 新建数据对话，选择、拖放或 **Ctrl+V 粘贴 Word 指标说明和 Excel 数据文件**。每个对话使用一份指标说明及多份数据文件；不继承其他对话的口径和数据。
3. 指标说明会自动解析并校验原文证据、全文覆盖和规则。成功后直接用于本对话，无需建库、发布或选版本；解析失败可重试，条款不明确的指标列出无法计算的原因。
4. 点击“开始分析”或描述“计算说明文档中的指标值”。默认计算本次上传的全部数据，同名字段自动匹配；字段含义不一致时通过字段映射确认。首个有效 PLAN/ANALYZE 冻结说明、数据和映射。
5. 含待填位置的说明按原文顺序回填数值或具体缺口，保留规则和公式；查看每处的原文依据、来源审计及处理进度。全部缺口显示“待补充资料”，仍可导出诊断报告，不能视为真实指标已取数成功。可引用明确运行解释、离线冻结重算，或选择同一基线的多个窗口运行生成同比、环比和趋势。
6. 导出 DOCX、XLSX、CSV、JSON 或分析 ZIP。缺少渲染器时 DOCX 标记为版面未核验草稿、PDF 不可用；经渲染并逐页人工检查后才能确认版面通过。

左侧“指标管理”支持导入说明、手工新增和编辑、复核发布、创建新版本、版本比较、设置默认版本和停用。手工指标无需 Word 或在线模型，支持记录数、去重计数、求和、平均值、最小/最大值及条件记录占比；保存草稿并复核发布后，可点击“新建对话使用”，上传 Excel、确认字段映射，再开始分析。已有草稿对话也可在右侧“选择已发布指标版本”。默认版本在选择窗口中预选，新建空白对话仍需显式绑定；对话上传的私有说明继续在所属对话查看。

详细步骤与故障处理见 [操作手册](docs/operator-guide.md)。

## 数据与能力边界

- Key 仅写入 Windows 凭据管理器。网页、业务数据库、日志、检查点和备份不保存 Key；更换服务地址/协议/服务商后需要重新输入。
- 本地文件包含真实内容，并非仅名称展示。工作区保存原件、解析快照、规则、结果及运行历史。备份采用带文件名加密的 7z，密码至少 12 字符；恢复后重新配置 Key。
- 模型出站由阶段白名单控制。Excel 明细不发给模型；指标说明原文片段、批准的字段元数据、用户问题或聚合摘要按阶段脱敏后发送到所选服务。模型不生成执行代码、不计算最终指标。
- 支持 `.xlsx/.xls/.docx/.doc`。旧 `.doc` 由内置 `doc2txt`/Antiword 及 `olefile` 回退解析为文本，不保证页面布局、图片文字、修订或嵌入对象；PDF 页面渲染仍需要 LibreOffice。公式不执行，无法证明有效的公式缓存拒绝用于计算。任意附件不能安装技能、脚本或扩展算子。
- 手术室与医院运营分别绑定领域包。已验证的是仓库内的合成规则与手算样例，不能当作医院正式医学口径。首台、连台、时点快照等准备算子当前显式不支持。
- 单文件 50 MiB、单批 20 文件/200 MiB，单 Sheet 10 万行/128 列，单会话合计 30 万行/50 Sheet；工作进程内存上限 2 GiB。复杂关联或宽表仍可能在上限内耗尽内存，处理会失败且不发布部分结果。

## 开发与验证

```powershell
.\.venv311\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv311\Scripts\python.exe -m pytest -q --junitxml=artifacts/acceptance/pytest.xml
.\.venv311\Scripts\python.exe -m ruff check insight_agent main.py --select F
.\.venv311\Scripts\python.exe scripts/verify_ui.py
.\.venv311\Scripts\python.exe scripts/verify_resilience.py
.\.venv311\Scripts\python.exe scripts/build_release.py
.\.venv311\Scripts\python.exe scripts/verify_release.py
.\.venv311\Scripts\python.exe scripts/package_release.py
```

GUI 验收脚本必须串行运行，避免窗口焦点及剪贴板干扰。`verify_capacity.py` 单独生成 30 万行合成数据进行资源实测。所有验收夹具均为合成内容；MockTransport 测试不等同于真实供应商验收。

构建锁定依赖并生成文件 SHA-256 清单、依赖清单和第三方许可证；它提供版本追溯，不保证跨机器逐字节可复现。交付目录中的 `_internal/build-metadata` 保留许可证和运行依赖版本。

## 主要模块

| 模块 | 职责 |
| --- | --- |
| `desktop.py`、`native_settings.py`、`web/` | 桌面 UI、原生设置、剪贴板、WebChannel |
| `service.py`、`store.py`、`worker.py` | 业务接口、SQLite、持久队列、租约与工作进程 |
| `files.py`、`data.py`、`definitions.py`、`manual_metrics.py`、`report_plan.py` | 文件解析、数据准备、指标版本与手工口径、原文位置计划及取数回填 |
| `engine.py`、`domains.py` | 类型安全规则、领域字段及精确计算 |
| `agent.py`、`skills.py`、`models.py` | 固定图、受信能力适配器、在线模型和调用账本 |
| `reports.py`、`comparisons.py`、`maintenance.py` | 报告、比较、备份恢复与诊断 |

## 工程内技能

`skills/` 已导入 `D:\手术数据\skills` 的四个技能，文件解析由其中的 Word 与 Excel 技能负责：

| Skill | 用途 |
| --- | --- |
| [surgery-word-spec](skills/surgery-word-spec/SKILL.md) | 指标定义、原文证据及待确认口径 |
| [surgery-excel-ingest](skills/surgery-excel-ingest/SKILL.md) | Excel 接入、增量补传与覆盖检查 |
| [surgery-metric-calc](skills/surgery-metric-calc/SKILL.md) | 按审核配置计算指标 |
| [surgery-word-report](skills/surgery-word-report/SKILL.md) | 按定义和结果生成 Word 报告 |

四个 ID 已对应 `insight_agent/skills.py` 的受信适配器。Markdown 是开发与任务指引，运行时仍使用 Python 工具合同。Word 与 Excel 技能分别提供文档和数据文件解析入口；指标解析技能已补充工程接口；新文档的口径整理见 [手术室指标审阅](artifacts/metric-spec-review-2026-10-29/review.md)。

规格及对应实现状态见 [方案 F01–F34](docs/backend-api-langgraph-plan.md)、[合同 V01–V12](docs/contracts/README.md) 和 [交付验收记录](docs/acceptance-2026-10-29.md)。
