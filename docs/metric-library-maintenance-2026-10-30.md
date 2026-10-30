# 指标列表、编辑与批量删除验收记录

日期：2026-10-30。适用 `D:\DataMind` 的 Python 3.11 源码。本文补充并更新此前指标管理验收中的维护与删除行为。

## 已实现行为

- 指标管理主表逐项展示指标，默认汇总各指标集最新创建的未删除版本（含草稿）；支持历史版本筛选、全文口径搜索与每页 25 项分页。相同名称、不同原文位置分别展示。
- 导入及解析进度位于下方独立区域，自动展开进行中的处理，保留真实阶段、批次、位置、完成数量、耗时与错误。
- 手工指标通过受控表单编辑；普通 Word 规则支持名称、维护说明、执行状态及完整规则；Word 待填项支持取数计划编辑，并继续校验是否符合原文。
- 草稿直接保存。已发布指标编辑或删除在成功保存时创建新草稿，失败不留下半成品版本；原发布快照、已有对话绑定与历史运行保持原有口径。指定版本筛选自动跟随新草稿。
- **批量删除仅覆盖指标**：逐项勾选、当页全选、跨页与跨搜索保留选择、跨指标集删除、取消选择；切换版本清空选择。确认弹窗列出名称与版本，需维护人和中文“确认删除”。
- 删除整批处于同一数据库事务。任一版本冲突、对象不存在、私有对象、重复选择、已删除对象或活动解析均拒绝整批操作，不产生部分删除或部分新版本。
- 删除记录进入发布 manifest 与冻结分析合同。列表、字段映射建议、对话指标数量、模型指标目录及新计算均尊重删除记录；原文位置保留，回填输出明确显示“指标已删除，不参与计算”。历史版本仍可查阅旧指标。
- 全部指标删除后不允许发布空版本。重新提取保留删除记录；普通规则重新生成标识导致无法保留删除引用时，拒绝替换并保留原草稿，提示上传新版本说明。
- 指标说明提供单份删除，支持草稿和已发布版本。有会话、运行或子版本引用时保留历史快照，从管理列表移除并禁止新绑定；无引用时清除该版本对象。此处不提供说明批量删除。
- 版本差异按有效指标比较，删除显示在 `removed`，维护说明修改显示在 `changed`。

## 接口

`library.metrics` 接受 `version`（默认 `latest`，也可指定版本 ID），返回逐项指标及所属版本摘要。

`definition.metric.edit` 接受 `definition_id`、`revision`、`metric_id`、`changes`。返回保存后的完整草稿；已发布来源返回新草稿 ID。

`definition.metrics.delete` 参数示例：

```json
{
  "selections": [
    {"definition_id": "实际版本ID", "revision": 3, "metric_id": "实际指标ID"}
  ],
  "confirmation": "确认删除",
  "actor": "维护人"
}
```

返回 `deletedCount` 和各来源的 `sourceId`、目标草稿 `id`、`versionLabel`、`deletedCount`。原有 `definition.delete` 继续承担指标说明单份删除。

## 验证

全部使用临时隔离工作区和合成 Word、Excel、手工规则；未改正式业务数据和在线模型配置，模型调用记录为 0。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 后端相关回归 | 176 通过，0 失败，85.58 秒 | 下方命令及 [维护测试](../tests/test_metric_maintenance.py) |
| 指标列表与新增维护 Qt / WebChannel | 98 项断言通过（含按钮存在性检查），无 JavaScript 错误 | [结果](../artifacts/acceptance/library-metrics-ui-verification.json) |
| 原有新增、映射与实际计算界面回归 | 47 项断言通过，本地结果 `3`、`20.0` | [结果](../artifacts/acceptance/metric-management-ui-verification.json) |
| 导入及详细处理进度界面回归 | 29 项断言通过 | [结果](../artifacts/acceptance/library-progress-ui-verification.json) |
| 静态检查 | 本轮 Python 文件 Ruff 通过，两份前端 JavaScript 语法检查通过 | `app.js`、`metric-library.js` |

后端执行命令：

```powershell
.\.venv311\Scripts\python.exe -m pytest tests\test_metric_maintenance.py tests\test_library_metrics.py tests\test_definition_delete.py tests\test_metric_management.py tests\test_report_plan.py tests\test_integration.py tests\test_conversation.py tests\test_provenance.py tests\test_document_workflow.py tests\test_analysis_preparation.py -q
```

覆盖发布编辑失败回滚、普通规则与取数计划编辑、跨指标集整批删除、重启持久化、中文确认、版本冲突、活动解析、权限隔离、删除后重新提取、空版本发布拦截、历史版本保留，以及真实 LangGraph 执行跳过已删除指标、旧运行保持原样。测试仅有依赖库 LangGraph 的未来默认值弃用提醒。

界面已核对 [最新指标列表](../artifacts/acceptance/library-metrics-latest.png)、[历史版本](../artifacts/acceptance/library-metrics-historical.png)、[窄窗口横向滚动](../artifacts/acceptance/library-metrics-narrow.png)、[维护后的列表](../artifacts/acceptance/library-metrics-maintained.png)。

## 使用与边界

进入“指标管理”，勾选指标 →“批量删除指标”→ 核对清单 → 输入“确认删除”。已发布指标的改动需复核发布新草稿，供新对话或准备中的对话显式选用；已锁定对话继续使用原冻结版本。

本轮已更新源码，未重新打包 `dist`。关闭旧应用后通过项目根目录 `start.bat` 启动可加载更新。本轮验收验证功能与版本一致性，合成口径不代表真实业务口径已获批准。
