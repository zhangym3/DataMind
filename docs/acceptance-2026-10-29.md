# Insight Agent 1.0.0-rc1 验收记录

验收日期：2026-10-29  
目标环境：Windows 10 x64，Python 3.11.15，项目工作区 `D:\DataMind`  
产品范围：本地桌面 UI、文件解析/存储、在线模型适配、受控 LangGraph、精确计算和导出。

## 自动化结果

| 套件 | 结果 | 证据 |
| --- | --- | --- |
| Python 单元/集成测试 | **106 passed**，1 个 LangChain 依赖弃用警告 | `artifacts/acceptance/pytest.xml` |
| Qt/WebEngine GUI | **27 PASS** | `artifacts/acceptance/ui-verification.json` |
| Windows 韧性 | **6 PASS** | `artifacts/acceptance/windows-resilience.json` |
| 容量 | **PASS**，30 万行合成输入，实际值 `150000` | `artifacts/acceptance/capacity.json` |
| 冻结 EXE smoke | **PASS**，无 Python/Office PATH，锁释放正常 | `artifacts/acceptance/frozen-smoke.json` |
| 发布 ZIP | **PASS**，逐文件 CRC/SHA-256 校验 | `artifacts/acceptance/package-verification.json` |

容量测试单次运行：解析约 94.25 秒，分析提交约 33.44 秒，工作进程峰值 RSS 约 927 MiB，低于 2 GiB 限制。该数字来自合成数据和当前机器，不是性能承诺；真实宽表、关联和目标机资源仍需单独测量。

测试使用合成内容。三种在线协议的 MockTransport 测试只验证请求/响应契约、Schema 和失败处理，不等于真实供应商、真实模型质量或费用验收。

## F01–F34 门禁结论

F01–F17、F21–F27、F30–F33 已有代码路径和自动化/契约证据；F18、F20、F28、F32、F34 为部分完成，受 Office/字体/真实环境或真实语料限制；F29 是已设计的 P2 后续功能。逐项入口、代码证据和差距见 [剩余功能清单](unfinished-features.md)。

## 外部验收仍需完成

1. 使用业务人员确认的脱敏 Excel、Word 和正式指标口径，核对映射、关联、覆盖、跳过原因和预期结果。
2. 为至少一个真实在线模型配置 Key，验证鉴权、结构化输出、超时、限流、费用/token、出站载荷和 Key 轮换。Key 不应写入仓库或报告。
3. 在安装 LibreOffice 和中文字体的 Windows 10/11 目标机验证 DOCX 逐页版面和 PDF 预览；旧 `.doc` 文本解析已由内置 Python 路径覆盖，无 LibreOffice 时只保持报告“版面未核验”并禁用 PDF。
4. 在干净目标机解压整个 onedir 目录，完成首次启动、设置、导入、分析、导出、重启恢复、备份恢复和升级演练。
5. 由医院运营/手术室业务人员确认 `hospital_ops/1.0.0` 与 `surgery/1.0.0` 的独立规则和语料。当前仓库没有真实患者明细，也没有声称医学口径已签收。

## 构建与复验命令

所有命令使用项目内 Python 3.11：

```powershell
.\.venv311\Scripts\python.exe -m pytest -q --junitxml=artifacts/acceptance/pytest.xml
.\.venv311\Scripts\python.exe -m ruff check insight_agent main.py --select F
.\.venv311\Scripts\python.exe scripts/verify_ui.py
.\.venv311\Scripts\python.exe scripts/verify_resilience.py
.\.venv311\Scripts\python.exe scripts/build_release.py
.\.venv311\Scripts\python.exe scripts/verify_release.py
.\.venv311\Scripts\python.exe scripts/package_release.py
```

GUI 验收要串行执行，避免剪贴板和窗口焦点争用。发布物是 PyInstaller onedir 便携目录，不是签名安装器；分发时必须保留整个 `dist/InsightAgent` 目录。

## 结论

当前版本达到候选交付（rc1）状态，核心代码链路已落地，自动化验收通过。由于真实模型 Key、真实业务数据/口径、LibreOffice、干净目标机和安装器签名尚未纳入当前环境，不能把本记录表述为全部正式生产验收完成。
