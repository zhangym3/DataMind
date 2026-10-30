# 指标说明取数与原文回填验收记录

验收日期：2026-10-30。交付对象为当前 Python 3.11 源码，方案见 [取数与回填方案](definition-extraction-remediation-2026-10-30.md)。本轮没有重新构建 `dist` 中的可执行程序。

## 1. 验收结论

说明解析、逐位置取数/计算、原文回填、进度展示和多格式导出的技术链路已通过本机验证。真实 Deepseek 重放已完成，47 个待填位置全部登记并有明确结论。

**真实资料数值验收尚未通过：当前结果为 0 处可信数值、47 处具体缺口。** 解析成功、报告覆盖和真实数值成功分别记录，不能相互替代。不得把原始行数当作台次、把缺失结果填成 0，或自动用系统昨日代替未确定的报告日期。

## 2. 本轮落实的变更

- 新增 `document-fill/1.1` 原文位置合同，保留全部段落、顺序、公式、阈值和占位定位。NUMBER、DATE、TEXT 分开登记。
- 模型使用八字段精简意图，每批最多四个数字位置、输出最多 2048 tokens；模型不生成代码，也不直接计算正式数值。
- 超时或截断只拆分失败批次；语义错误同批最多纠正一次；成功批次独立保存，重试沿用累计预算和指纹，避免已完成批次重复请求或混用版本。
- 四个固定 Skill 贯穿 Word 解析、全部材料核验、本地确定性计算和报告回填；LangGraph 保持 `prepare → compute → audit → commit`。
- 增加简单实际手术量、急诊量和时差患者人数的受控取数；已有结构化汇总必须核对指标、单位、日期、维度及完整上下文。
- 退单、未知来源、实体不明、时间非法和口径冲突均显式保留缺口。遗漏、多余或重复结果均禁止提交报告。
- 对话显示上传和逐位置分析进度、回填正文和缺口；全缺口显示“待补充资料”，允许导出诊断报告。

## 3. 自动化与界面检查

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 全量 Python 测试 | 225 passed，0 failed / 0 errors / 0 skipped，113.03 秒 | [JUnit](../artifacts/acceptance/document-fill-pytest.xml) |
| 后续目录/上传改动回归 | 30 passed，25.51 秒；为全量套件中的相关子集，不累计为 255 项 | [JUnit](../artifacts/acceptance/metric-catalog-followup-pytest.xml) |
| JavaScript 语法 | `node --check insight_agent/web/app.js` 通过 | 命令退出码 0 |
| Python 静态检查 | `ruff check insight_agent main.py --select F` 通过 | All checks passed |
| 文档本地链接 | README、方案、验收记录、手册和剩余清单共五份文档无失效本地链接 | 检查脚本退出码 0 |
| 回填结果界面 | 11 项通过 | [检查清单](../artifacts/acceptance/document-fill-ui-verification.json)、[截图](../artifacts/acceptance/document-fill-ui.png) |
| 失败提示与重试界面 | 9 项通过 | [检查清单](../artifacts/acceptance/failure-ui-verification.json) |
| 独立指标目录界面 | 11 项通过 | [检查清单](../artifacts/acceptance/metric-catalog-ui-verification.json)、[部分完成](../artifacts/acceptance/metric-catalog-partial.png)、[全部完成](../artifacts/acceptance/metric-catalog-ready.png) |
| 当前渲染器只读复核真实重放 | 正文及摘要与已提交报告一致；重复结果被拒绝；0 次模型调用 | [复核记录](../artifacts/acceptance/document-fill-replay-offline-verification.json) |

测试有一项 LangGraph `allowed_objects` 默认值未来变化的弃用提醒，没有测试失败。目录/上传模块在全量测试后有同步更新，因此补跑相关 30 项测试和目录界面验收。

界面检查覆盖了：原文正确位置回填、公式保留、缺口清单、上传进度归属、轮次顺序、清除后历史折叠、原件及审计结果保留、真实错误原因展示、重试幂等、目录进度实时刷新、断点继续及模型文本转义。

复现命令（GUI 脚本串行执行）：

```powershell
.\.venv311\Scripts\python.exe -m pytest -q --junitxml=artifacts\acceptance\document-fill-pytest.xml
.\.venv311\Scripts\python.exe -m pytest tests\test_metric_catalog.py tests\test_metric_upload.py -q --junitxml=artifacts\acceptance\metric-catalog-followup-pytest.xml
node --check insight_agent\web\app.js
.\.venv311\Scripts\python.exe scripts\verify_document_fill_ui.py
.\.venv311\Scripts\python.exe scripts\verify_failure_ui.py
.\.venv311\Scripts\python.exe scripts\verify_metric_catalog_ui.py
```

## 4. 合成端到端数值验收

`tests/test_report_plan.py::test_end_to_end_registered_skills_full_coverage_fill_and_export` 使用有明确手术编号、患者 ID、手术日期、实际状态和完整时间的合成材料，调用正式 Skill 适配器及分析图：

| 原文位置 | 期望与实际结果 |
| --- | --- |
| 总手术量 | 2 |
| 急诊手术量 | 1 |
| 入室至麻醉严格超过 20 分钟的患者人数 | 1 |
| 当日 16:00 拟做手术量 | 缺口：缺少 16:00 排程历史快照 |

四处结果填回正确原文位置，规则段落及 `x100%` 公式未改。JSON、DOCX、XLSX、CSV、ZIP 内容校验全部通过。这证明已实现算子在有充分依据时能够取数并回填，不代表真实材料中的实体和口径已经确认。

## 5. 真实 Deepseek 隔离重放

使用正式数据库的只读备份和托管文件副本，在独立临时目录执行。正式业务记录、已保存模型配置及 API Key 未修改；不向验收记录写入凭据、患者明细或住院号。

正式连接保持 `Deepseek / deepseek-flash`、revision 4、配置输出 4096 tokens、超时 120 秒；回填阶段在单次调用中限制输出为 2048 tokens，没有修改连接配置。

| 项目 | 结果 |
| --- | --- |
| 输入 Word | 46 个原文节点，47 个待填位置：33 个数字、14 个日期/文字 |
| 输入 Excel | 7 份，共 31364 行；该行数不等于手术台次 |
| 解析 | 81.125 秒、11 次请求、36712 tokens |
| 截断恢复 | 1 次输出截断，自动拆分失败批次后恢复 |
| 解析最终状态 | PUBLISHED，47 个 REPORT 位置；不等于 47 个可计算规则 |
| 分析 | 四阶段全部完成并提交；运行状态 NO_ELIGIBLE_METRICS |
| 报告覆盖 | 46 段、47 个唯一绑定、0 处数值、47 处缺口 |
| 导出 | JSON / DOCX / XLSX / CSV / ZIP 的 contentQA 全部 PASS |
| 版面 | DOCX 为 LAYOUT_UNVERIFIED；未进行逐页版面验收 |
| 隔离检查 | sourceRecordsUnchanged=true；重放期间 applicationCodeUnchanged=true |

重放证据：[20261030-100856/report.json](../artifacts/simulations/20261030-latest-analysis/20261030-100856/report.json)。早先失败记录 [20261030-100002/report.json](../artifacts/simulations/20261030-latest-analysis/20261030-100002/report.json) 保留供追溯。

重放后加强了重复结果拒绝检查。使用当前渲染代码，只读读取上述隔离库中的已提交运行重新回填，正文和摘要与已保存结果完全一致；未再次请求模型，未写正式库。

## 6. 尚不能作为业务数值验收通过的内容

- 真实明细尚无已确认且满足当前取数合同的手术/患者唯一键。住院号不能自动代替，首台子集和全量明细也不能相加。
- 首台、连台、延误或停台比率、16:00 计划快照、接台时点、平均最晚等复杂项目的明细算子尚未完成。严格匹配的结构化汇总可取值，其余位置保留具体缺口。
- 日期、科室和时间文字当前只登记和提示，不自动填充；相对日期的业务归属需要确认。
- 当时解析期间点击分析仍需完成后再次点击；后续自动接续修复见第 8 节。
- 旧 `.doc` 的文本解析只保证已提取正文顺序，不保证原字体、表格和分页；PDF 仍需可用渲染器，没有强制要求安装 LibreOffice。
- 独立目标设备、真实业务金标准、版面签收和本轮新版本打包尚未完成。

后续业务验收应以已确认实体键、字段含义、完整时间和复杂指标政策的材料建立人工金标准，逐项对照数值和来源。完成这些条件前，本轮通过范围为解析、报告覆盖、已有算子及诊断闭环。

## 7. 后续上传恢复与 1M 输出配置验证

第 5 节保留当时 4096/2048 的历史验收条件。后续按用户要求将应用可配置输出上限提高到 1M（1,000,000）tokens，移除目录与回填的固定输出封顶；默认累计输入输出预算也是 1M。当前默认连接及一般设置已保存为 1M，原凭据保留。实际请求受已知服务商能力及剩余预算约束；官方 DeepSeek Flash/V4 Chat 按接口上限发送 393216，并在结构化提取阶段关闭默认思考，回执分别记录配置与实际额度。

使用原始 `手术室领导提供数据新.doc` 和当前默认模型，在隔离目录以 1M 配置重新上传解析：35.94 秒完成，11 次请求全部成功，46 个原文节点已覆盖，目录 READY，25 个候选的明细全部处理完成。候选中可能存在相近名称，数量不作为独立业务指标金标准，也不代表已计算指标数值。正式业务记录未参与此次解析写入。

最终全量回归为 250 passed，1 条依赖弃用提醒；原生设置界面验证 1M 可输入、保存并重新读取。新增预算与协议验证覆盖 Chat、Responses、Anthropic、DeepSeek 服务上限、无 usage 时保守预留及预算不足时停止。相关 Python 文件通过 Ruff F 检查，前端 JavaScript 语法及说明 Skill 校验通过；既有 acceptance 测试文件的 fixture 导入存在原有 Ruff F401/F811 提示。

证据：[在线原文解析](../artifacts/acceptance/metric-upload-live-verification.json)、[原生设置](../artifacts/acceptance/model-output-settings-verification.json)、[当前连接与预算](../artifacts/acceptance/model-output-live-settings-verification.json)、[全量测试](../artifacts/acceptance/metric-catalog-followup-pytest.xml)。本次为源码及设置更新，未重启用户应用，未重新打包。

## 8. 开始分析自动准备与接续

用户在 11:01:31 点击“开始分析”时，分析入口引用了 08:52:11 的历史 `MODEL_TIMEOUT`，以 `DEFINITION_UNAVAILABLE` 直接结束。独立指标目录 READY 仅表示目录与口径提取完成；分析仍需已校验并发布的取数规则。旧入口缺少准备步骤，不能自动等待并继续。

现已将流程落实到说明解析 Skill 和运行接口：有数据且说明用途明确时，PLAN/ANALYZE 复用正在进行的解析，或为本轮创建一次准备任务。分析请求保存持久依赖并让出单 Worker，准备成功后继续同一 turn 和 requestId，无需再次点击。提交时已有解析任务也立即登记依赖，避免解析先发布产生版本冲突。界面在仅说明解析活动时允许开始分析，并显示准备、就绪和继续分析的进度。

自动发布只推进由本次准备产生的会话绑定版本；用户更换资料、修改会话或取消请求仍触发原有检查。本次依赖失败时报告本次错误，不引用历史超时，也不自动无限重试。发布成功后、任务终态落库前崩溃可幂等恢复，不重复模型调用。相关约定见 [说明 Skill](../skills/surgery-word-spec/SKILL.md) 与 [运行接口](../skills/surgery-word-spec/references/datamind-contract.md)。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 本轮全量 Python 回归 | 262 passed，1 条依赖弃用提醒，120.52 秒 | [JUnit](../artifacts/acceptance/analysis-preparation-pytest.xml) |
| 新增自动接续回归 | 12 项通过；覆盖历史超时、活动任务复用、排队发布版本推进、幂等、失败/取消、用户变更与崩溃恢复 | [测试](../tests/test_analysis_preparation.py) |
| 真实 Qt WebChannel 按钮验收 | 21 项通过；历史超时后与解析活动期间均只点击一次，自动提交一个运行，结果为合成值 2 / 1 / 1，保留排程快照缺口 | [检查清单](../artifacts/acceptance/analysis-preparation-ui-verification.json)、[历史超时场景](../artifacts/acceptance/analysis-preparation-old-timeout.png)、[解析活动场景](../artifacts/acceptance/analysis-preparation-active-extraction.png) |
| Python 与前端静态检查 | 相关改动通过 Ruff F；`node --check insight_agent/web/app.js` 通过；说明 Skill 校验通过 | 命令退出码 0 |
| 正式现场只读复核 | 本次说明为 PUBLISHED，47 个候选 / 47 个待填位置；最新 extract 已于北京时间 11:05:39 成功 | [只读状态](../artifacts/acceptance/analysis-preparation-live-status.json) |

本轮回归和按钮验收全部使用隔离临时目录与合成数据，0 次真实模型请求；正式库仅只读检查，未写正式业务记录。本节验证任务恢复和接续，不改变第 6 节真实业务数值尚待验收的范围。此次更新为源码修复，没有重启用户应用或重新打包；关闭并重新打开源码应用后加载修复，当前已发布说明无需重新上传。

```powershell
.\.venv311\Scripts\python.exe -m pytest -q --junitxml=artifacts\acceptance\analysis-preparation-pytest.xml
.\.venv311\Scripts\python.exe scripts\verify_analysis_preparation_ui.py
```
