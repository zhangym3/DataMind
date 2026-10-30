# DataMind 运行接口约定

## 固定注册边界

Skill 由 `insight_agent/skills.py:invoke` 的受信 Python 分支执行，不通过 Markdown 动态执行工具。四个 Skill 分工为 word-spec 的 parse/catalog/extract、excel-ingest 的 parse/prepare_fill/materialize、metric-calc 的 calculate_fill/calculate、word-report 的 render_fill/export。

`runtime_manifest()` 冻结应用、图、状态结构、规则结构、回填结构和提示、来源政策及四个 Skill 清单哈希。当前图为 fixed-workflow/1.1，回填为 document-fill/1.1，来源政策为 local-source-policy/1.1。word-spec 为 1.2.0，其他三个为 1.1.0。旧冻结环境不匹配时不能静默按相同版本重算。

## 回填计划合同

本地 `template(nodes)` 保存 paragraphs 和 slots。slot 包括 id、locator、startOffset、endOffset、placeholder、kind、context、semanticContext，发布后附 plan。继承的日期、章节及计划/接台前文用于核验语义，不能只看“第一手术室X台”这一行。

模型响应 items 每项只允许 `slot,name,method,room,start,end,minutes,reason`。method 为 SURGERY_COUNT、EMERGENCY_COUNT、DURATION_PATIENTS、READ_SUMMARY、UNRESOLVED。每批最多四项，输出额度按连接配置及模型和预算边界确定，恰好覆盖本批目标位置。日期和文字由本地登记缺口。

REPORT candidate 必须与 slot.plan、名称、定位精确一致；全文节点都有 coverage。完整性和语义校验后才能发布清单，不要求已经有数值。schema1.2 的定义 manifest 冻结 reportPlan；分析合同使用 schema1.1，冻结 reportDocumentHash、reportPlan、全部文件原件及快照哈希、用途、运行清单和依赖版本，范围为 ALL_DATA。

技术错误保持失败。UNRESOLVED 需具体原因；REPORT 不等于可执行规则。最终每个 slot 恰好绑定一个 VALUE 或带中文原因的 SKIP，遗漏、多余及重复结果阻止报告提交。

## 模型与恢复

说明脱敏后才可出站，患者行、文件路径、凭据和任意历史对象不允许出站。冻结模型配置 revision；解析中的配置变化不可混入同一代次。文档、提示、结构、运行清单或配置变化建立新代次。

fill_attempt 在第一请求前落库，每个成功批次单独保存。同指纹重试沿用原 budgetId；用量根据供应商 usage 结算，无用量时保守保留预留。调用数、累计 token 和总时长都有上限，失败及截断请求也记账。

应用可配置单次输出上限为 1M（1,000,000）tokens，默认累计输入输出预算也是 1M，已保存或冻结任务仍使用其原预算。网关先按连接配置与已知服务商能力取较小值，再扣除输入的保守预留和累计用量分配本次额度；剩余不足 256 tokens 时停止。官方 api.deepseek.com 的受支持 Flash/V4 Chat 模型按其[接口文档](https://api-docs.deepseek.com/api/create-chat-completion)限制为 393216 tokens。其他服务须使用其支持的额度，服务端能力不能由应用配置提升。回执分别保存 configuredOutputTokens 和 effectiveOutputTokens，响应字节上限为 32 MiB；请求策略版本进入运行清单哈希。

输出截断或多项批次超时仅拆当前失败批次；语义校验错误允许携带具体错误同批纠正一次，再次失败停止，不能把校验错误降为业务缺口。非 DURATION_PATIENTS 的 start/end 必须为空且 minutes 为 0；UNRESOLVED 的原文阈值可保留在 reason。纠正提示进入运行清单指纹。

## 分析准备与接续

PLAN/ANALYZE 只能使用当前对话已校验的定义。会话自有 DRAFT 在有 Excel 且附件用途明确时，复用活动 extract，或每轮最多创建一次准备任务；全 SKIP 的已完成规则仍显示业务缺口。库中手工定义继续走原有校验与审阅入口。

turn.preparationJobId 与 preparationDefinitionId、任务 payload.waitForJobId 在同一事务保存。分析任务保持 QUEUED，让单 Worker 先处理依赖；claim 跳过依赖仍活动的任务，不能在分析任务中阻塞等待队列。提交时已有准备任务的请求须立即登记依赖，避免准备先发布而导致排队分析的版本冲突。

自动发布校验当前会话、说明、附件用途和任务租约；仅在绑定事务内，推进等待该 extract 且版本仍等于发布前版本的 PENDING turn.sessionRevision。其他用户变更不允许被自动接受。发布 manifest 保存 extractionJobId，发布后、任务终态前崩溃可幂等恢复，无需重复请求模型。取消轮次不创建分析运行；依赖失败后报告该任务的具体错误，不引用历史失败或无限重试。准备成功沿用原 turn、requestId 和意图决定继续冻结合同、计算与提交。

## 来源和取数

本地按表头分类 DETAIL、SUMMARY、CANCELLATION、UNRESOLVED，不凭文件名。数量不等于行数；手术唯一键和患者唯一键分别核验，住院号不能替代。重叠明细按实体去重，跨日期同手术键冲突不输出数值。实际手术还需已实施状态或完整实际手术时间，计划记录不能混入实际量。

已有汇总需指标名称、完整 semanticContext 口径、单位、手术室和日期一致；同段多个数字还需“待填位置”列明确 slot ID。同日冲突禁止取值，不累加多个已汇总行。证据记录文件、快照、工作表、行列、slot 和口径哈希。

复杂业务口径、缺实体键、未知非空来源、非法日期时间和冲突均生成明确缺口；技术结构或执行异常不伪装为缺口。数据完整性不能由“文件已导入”推断。

## 旧 AST 路径

无占位符文档继续使用当前 engine.SCHEMA 的 camelCase 规则：CLEAR 带完整合法 rule，SKIP 带原因与真实原文定位，全文 coverage 不可漏项。只有合法规则可以交给旧 calculate。first_event、sequence_classify、snapshot_as_of 等未开放能力保持不可用。

## 输出

filledReport 按原文字符位置回填，保留所有段落、阈值和公式；数值按材料日期展示，缺口使用【待补充：原因】，不填假零。DOCX、XLSX、CSV、JSON、ZIP 均保留正文和缺口。内容检查与版面检查分开，无渲染器只标记版面未核验；PDF 仍依赖渲染器。
