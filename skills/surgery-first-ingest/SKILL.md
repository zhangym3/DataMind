# surgery-first-ingest

受信 Python 入库适配器。只接受标题含“每日首台手术查询”、第 3 行与内置合同完全一致的 14 列 XLSX。逐行写入 `first_surgery_row`，保存原始 Excel 行号、行哈希和时分原文/分钟。不得将来源行推断为已实施手术，也不得执行工作簿中的公式或指令。

输入由应用提供：当前对话 ID、工作簿路径和冻结的预期类型 `FIRST_SURGERY`。输出仅含来源 ID、类型、状态、行数和 revision ID；患者行只保存在本地业务表。
