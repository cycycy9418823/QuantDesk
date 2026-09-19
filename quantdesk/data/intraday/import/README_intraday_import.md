# 1 分钟线 CSV 导入说明

这是给真实 1 分钟线数据使用的导入模板。CSV 里不要写说明行，只保留表头和真实数据。

最低要求：symbol + datetime + close。
推荐要求：symbol + datetime + open/high/low/close/volume/amount。
如果只有 time，没有 date，请在导入命令里指定 --target-date。
如果只有 price，系统会临时填充 open/high/low/close，但会标记 price-only，可靠性较低。

支持 QuantDesk、通达信、同花顺、聚宽、掘金、米筐等常见字段，也支持 utf-8、gbk、gb18030。

示例：
python scripts/import_intraday_1m.py --input data/intraday/import/intraday_1m_import.csv --target-date 2026-05-15 --audit-after-import
python scripts/run_intraday_data_workbench.py --target-date 2026-05-15 --import-path data/intraday/import/intraday_1m_import.csv --execute-after-ready

不要把日线 CSV 当成分钟线。系统不会用日线收盘价、最高价或最低价假装盘中成交。
风险提示：不接券商、不真实下单、不构成投资建议。