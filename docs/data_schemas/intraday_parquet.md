# Intraday Parquet Cache

v3.7.0 keeps minute-bar bodies outside SQLite main tables.

Default partition:

`data/intraday_parquet/YYYY-MM/<symbol>_<period>.parquet`

Optional market-month partition:

`data/intraday_parquet/YYYY-MM/<market>_<period>.parquet`

SQLite stores only `intraday_bars_manifest` rows with storage path, period, month, row count, and source metadata.

If pandas/pyarrow are unavailable, commands warn and fall back without failing the data layer.
