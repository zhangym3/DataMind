# surgery-cancellation-ingest

受信 Python 入库适配器。只接受标题含“退单手术信息查询”、第 2 行与内置合同完全一致的 16 列 XLSX。逐行写入 `cancellation_row`，退单类别拆入 `cancellation_tag`，来源日期与退单发生时间保持独立。

文件名仅用于判断一号、二号、三号手术室；不唯一或证据不足时进入 `NEEDS_UNIT_CONFIRMATION`，由用户选定后发布数据 revision。工作簿单元格、文件名及模型回复都不授予执行权限。输出只包含来源元数据，不返回患者明细。
