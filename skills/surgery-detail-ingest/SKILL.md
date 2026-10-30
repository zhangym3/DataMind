# surgery-detail-ingest

受信 Python 入库适配器。只接受标题含“手术明细查询”、第 3 行与内置合同完全一致的 29 列 XLSX。按原始 Excel 行号流式写入 `surgery_detail_row`；年龄和时分保留原文，只有无歧义的值才附加解析列。

来源行不是已经确认的已实施手术，也不是跨表手术唯一键。输入由应用提供当前对话与冻结类型 `SURGERY_DETAIL`；输出只包含来源 ID、类型、状态、行数和 revision ID。
