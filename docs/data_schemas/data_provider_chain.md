# Data Provider Chain

Provider calls are network-gated. With `network=false`, external providers are skipped.

Fundamentals:

1. SQLite cache.
2. AKShare THS abstract if available.
3. AKShare Eastmoney abstract if available.
4. AKShare statement adapters when available.
5. Tushare placeholder only, no hard dependency or token logging.
6. Sina/mootdx limited fallback notes.

Sectors:

1. SQLite cache.
2. Local CSV.
3. AKShare industry board with `network=true`.
4. AKShare concept board with `network=true`.

Intraday:

1. Local Parquet cache.
2. Local CSV cache.
3. mootdx/AKShare/Tencent only with `network=true`.

mootdx is not documented as a complete financial statement provider.
