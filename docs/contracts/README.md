# 指标与计算合同 1.0（设计规格）

这些文件是实现所依据的合同规格。JSON Schema 已接入 `insight_agent/engine.py` 的结构与语义校验，运行时副本位于 `insight_agent/resources/metric-definition.schema.json`。真实解析、不可变发布和本地计算已接入；实现与验收范围见 [验收记录](../acceptance-2026-10-29.md)。下面的示例仍为虚构规则，不是已获业务批准的指标库。

- [MetricDefinition JSON Schema](metric-definition.schema.json)：Draft 2020-12，只容纳明确、可执行的单项规则。
- [手术时长计数示例](examples/surgery-delay-count.json)、[医院运营比率示例](examples/hospital-bed-occupancy.json)：用于结构校验，全部为虚构规则；零值证据哈希是占位符，正式发布必须拒绝。
- [独立手算样例](golden-cases.json)：文档级算术与边界期望，不代替真实文件、模型、解析器和安装包验收。

## 1. 分层和不可变关系

`SpecDraft` 保存未决候选、缺失槽位及候选原文；`MetricDefinition` 只保存 `CLEAR` 且通过结构/语义校验的规则；`DefinitionVersionManifest` 包含全部候选的执行/跳过账本、规则 ID/hash、完整提取清单、原件 hash、解析/提示/模型版本、DomainPack hash 和发布记录。

人工修订仅修正提取错误时引用原文；超出Word的业务补充，必须由维护界面显式生成不可变人工补充说明快照，记录条款、操作者、时间、适用范围与所替代条款。该快照与原Word均列入DefinitionVersionManifest的evidenceDocuments（kind=WORD或USER_AMENDMENT、hash、locator目录、采用/替代关系），sourceEvidence引用真实文档hash/locator/quote。只有patch记录或模型自称授权不能作为新口径证据；发布前检查冲突条款已有明确采用关系。

规则只引用标准字段 ID，不嵌入具体工作簿/Sheet/列或某次查询日期。数据相关 `MappingRevision`、`RelationshipRevision`、`DataSnapshotManifest` 和参数值进入 ComputationContract，避免发布定义后更换数据集就必须重写 Word 版本。定位信息以真实提取文档为准，转换后的 `.docx` 定位同时关联原 `.doc` 和转换产物 hash。

`ruleDigest = SHA256(canonicalJSON(MetricDefinition))` 存在版本 manifest 中，不放进需要计算自身 hash 的 JSON。规范化采用固定版本的 RFC 8785 JCS；所有正式小数为十进制字符串，禁止 NaN/Infinity。中文文本按提取器确定的原文保留，不为计算 hash 随意改写空格；quoteSha256 是实际提取 quote 的 UTF-8 hash。最终结果 hash 排除 runId、生成时间、文本解释和 DOCX 元数据，包含 metricId、组键、精确值、单位、窗口、分子/分母和审计结论。

ComputationContract 必需字段如下（正式运行必须全部固定）：

| 字段组 | 必需内容 |
| --- | --- |
| 标识/版本 | contractSchemaVersion、domainPackId/version/hash、definitionVersionId、rulesDigest、metricId/ruleDigest 清单 |
| 数据 | inputManifestHash、adoption/routingManifestHash、逐文件 SHA、解析快照 hash、字段目录/实体粒度、范围/行数/来源集合 |
| 治理 | mappingRevision/hash、relationshipRevision/hash、字典/分类/去重/预处理版本及证据、主键与冲突政策 |
| 查询 | 确定的指标集合、参数值、窗口/边界/时区/归属、分组、合法过滤条件、比较期间和比较政策（如适用） |
| 执行 | 已编译计划 hash、engine/operator/preparation 版本、精度、缺失/非法/空集/零分母政策、资源上限 |
| 发布 | reviewPolicyVersion、逐指标确定性/AI_REQUIRED 政策、报告准入规则版本 |

合同 hash 不包含当前 Key、运行租约、时间戳等可变基础设施信息。图/模型/提示/模型复核记录另存 RunProvenance，与合同、结果 hash 相连。锁定会话使用冻结的映射和数据快照；修改映射须新建会话，改变允许的查询窗口则新建合同及 run。

## 2. 结构校验之后必须执行的语义检查

JSON Schema 不能验证医学含义、映射是否真实、算术是否正确或是否遗漏文档；编译/发布门禁必须继续执行以下校验，并存检查版本与证据。

| 编号 | 必须满足的条件 | 失败结论 |
| --- | --- | --- |
| V01 | 字段/参数/证据 ID 各自唯一；每个 AST、时间、组键、计数键引用已声明；没有未使用且含糊的必需字段 | INVALID_REFERENCE |
| V02 | evidence locator 在manifest登记的Word/人工补充文档中存在，quote/hash 精确匹配且非占位；来源与采用关系有效，足以支撑对象、边界、排除、阈值、缺失政策 | EVIDENCE_INVALID / DEFINITION_INCOMPLETE |
| V03 | 类型及单位严格匹配，字符串不隐式转数字；Decimal/Integer 可做精确数值提升，日期/日期时间不隐式混算 | OPERATOR_TYPE_MISMATCH |
| V04 | timeWindow 的两个参数各有唯一 date_start/date_end 声明，ISO 日期合法、start < end；日期转日界用声明时区；时区存在于随包 tzdata | INVALID_WINDOW |
| V05 | required date 参数的 allowedValues 为空；group_by 只能选择已批准维度；dimension_filter 的允许值/字段由领域包与本地目录共同验证，空列表不代表任意字段 | PARAMETER_NOT_ALLOWED |
| V06 | count 的 countBasis=records；count_distinct 的 fields 与 distinct_entity keys 相同；聚合来源粒度和主键唯一，不能把已汇总行当患者 | GRAIN_MISMATCH / DATA_CONFLICT |
| V07 | 比率双方计数/单位/窗口/分组兼容；百分比 scaleFactor=100；仅定义要求时验证分子成员属于分母，不能把所有比率强制为0至100% | INVALID_RATIO_CONTRACT |
| V08 | 非 ratio 的 zeroDenominatorPolicy=not_applicable，ratio 为 skip_metric；zero_for_count 只用于计数且数据范围完整，未知/缺失不是空集 | INVALID_EMPTY_POLICY |
| V09 | 排除阈值在[0,1]，skip_metric 的阈值为0；允许剔除须有原文/版本化业务政策证据并记录纳入/剔除及分母；缺失事件键不以“排除”绕过唯一性检查 | DEFINITION_INCOMPLETE / DATA_INSUFFICIENT |
| V10 | AST 深度≤12、节点≤256；无聚合嵌套/任意函数/自引用；每组上限10000；参数及准备算子均登记在同一冻结版本 | PLAN_LIMIT_EXCEEDED / UNSUPPORTED_OPERATOR |
| V11 | 唯一字段映射已验证、关联基数/覆盖/冲突通过；标准化快照包含真实范围全量数据，不是画像样本 | MAPPING_UNRESOLVED / DATA_CONFLICT |
| V12 | 依赖 DAG 无环且闭包齐全；派生指标继承依赖的不合格状态；DomainPack/引擎版本与发布 manifest 一致 | DEPENDENCY_INVALID / VERSION_MISMATCH |

以上字符串是稳定错误/处理码；V01–V10 在规则发布/计划编译执行，V11–V12 在具体数据与运行中执行。发布规则不以已有数据集为前提。

## 3. 算子目录和计算语义

| 层次 | 算子/签名 | 固定语义 |
| --- | --- | --- |
| 标量 | field、typed literal、parameter | 仅访问当前合同白名单字段/参数；不接受路径或代码 |
| 标量 | duration_minutes(datetime,datetime) → 精确数值（minute） | 使用完整日期时间；时差由精确微秒差除以60000000计算；负差记 INVALID，不自动加一天 |
| 条件 | eq/ne/gt/gte/lt/lte、in/not_in | 同型比较，数值仅做精确提升；返回 TRUE/FALSE/UNKNOWN |
| 条件 | and/or/not、is_null/is_not_null、all_records | 三值逻辑：NOT UNKNOWN仍UNKNOWN；筛选仅纳入TRUE，UNKNOWN计入缺失审计；is_null 返回确定布尔值 |
| 范围 | timeWindow + date 参数 | 先按约定时间轴/时区筛选；不是独立可执行任意字符串函数 |
| 聚合 | count、count_distinct | 精确整型；仅完整且合法的零匹配集可返回0 |
| 聚合 | sum/mean/min/max(数值) | 精确数值，单位一致；空集按规则跳过；不计算“平均的平均” |
| 聚合 | ratio(aggregate,aggregate) | 保存未舍入分子/分母和有理数表示；显示阶段一次舍入，分母0不生成数值 |
| 分组 | groupBy[] | 属于顶层计划属性；空维度按领域明确政策归入已标记未知组或跳过，排序使用稳定组键 |

join、事件分类、快照选择是 **受注册的准备算子**，不放入模型可任意拼接的规则 AST。首版允许的基数为1:1/1:n/n:1；n:m 仅在领域包有明确桥接实体、权重/去重规则和独立样例时开放，否则拒绝。first_event/sequence_classify/snapshot_as_of 按 DomainPack 提供版本化参数和验证记录；未验收则 capability=false，不能因表中出现名称就执行。

十进制输入按字符串精确解析；整数、有限小数、时差、均值和比率在计算及阈值比较中均使用整数/有理数精确语义，不能先以34位近似值比较边界。逻辑decimal类型允许保留约分后的分子/分母；物化精确结果用十进制字符串或exactNumerator/exactDenominator及单位表示。Decimal上下文精度34位仅是格式化默认值，显示按规则scale/roundingMode直接从精确值一次舍入；超出实现资源上限应报错，不能截断成近似业务值。大整数在 IPC/JS 层传十进制字符串；前端显示值不得反向参与计算。规范化前、关联前后、剔除前后均保留行数/来源集合审计。

阈值“20分钟”、计数实体、过滤口径属于规则，不作为任意查询参数暴露。用户要求改成30分钟时应修订定义并新建会话；用户更换已允许日期/分组时是新合同、新 run，不称为同合同离线重算。

## 4. 样例及负例验收

golden-cases.json 的值由直接手算给出，不由待测引擎生成。结构示例只用于 Schema；涉及正式原文发布需另备真实脱敏 Word/Excel、真实证据 hash 和人工复核记录。

结构负例：缺 requiredFields、添加 python 字段、未知 operator、小数用 JSON 浮点、precision 少 roundingMode、count_distinct 无 fields、数值literal缺unit均必须拒绝。语义负例：引用不存在字段、伪造 evidence、start≥end、countBasis 与计数算子不一致、阈值超界、环形依赖、非法多对多、错误粒度均必须被相应检查拒绝。

版本升级不覆盖旧规则：新增 AST 结构发布新 schema/operator 版本，明确迁移工具、兼容矩阵和回归范围；旧合同不能用“最新版”自动重编译。

本轮附件实际校验范围及待验证项见[校验记录](../design-validation-2026-10-29.md)。
