from __future__ import annotations

DAILY_TABLES = ["daily_bars_forward_adj", "daily_bars_back_adj", "daily_bars_raw"]


def _daily_table_sql(table: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  open REAL,
  high REAL,
  low REAL,
  close REAL,
  volume REAL,
  amount REAL,
  pct_chg REAL,
  change REAL,
  turnover REAL,
  source TEXT,
  updated_at TEXT,
  row_hash TEXT,
  PRIMARY KEY (symbol, date)
);
"""


def get_schema_sql() -> list[str]:
    sql: list[str] = []
    for table in DAILY_TABLES:
        sql.append(_daily_table_sql(table))
        sql.append(f"CREATE INDEX IF NOT EXISTS idx_{table}_date ON {table}(date);")
        sql.append(f"CREATE INDEX IF NOT EXISTS idx_{table}_symbol_date ON {table}(symbol, date);")
    sql.extend([
        """
CREATE VIEW IF NOT EXISTS daily_bars AS
SELECT *, 'forward' AS adjustment FROM daily_bars_forward_adj;
""",
        """
CREATE TABLE IF NOT EXISTS intraday_partial_daily (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  open REAL,
  high_so_far REAL,
  low_so_far REAL,
  last_price REAL,
  volume_so_far REAL,
  amount_so_far REAL,
  source TEXT,
  is_complete INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  row_hash TEXT,
  PRIMARY KEY (symbol, date, source)
);
""",
        """
CREATE TABLE IF NOT EXISTS fundamentals (
  symbol TEXT NOT NULL,
  report_date TEXT NOT NULL,
  publish_date TEXT,
  revenue REAL,
  net_profit REAL,
  total_assets REAL,
  total_liabilities REAL,
  equity REAL,
  operating_cashflow REAL,
  eps REAL,
  bvps REAL,
  roe REAL,
  pe REAL,
  pb REAL,
  net_profit_deducted REAL,
  gross_profit REAL,
  gross_margin REAL,
  net_margin REAL,
  debt_ratio REAL,
  investing_cashflow REAL,
  financing_cashflow REAL,
  ps REAL,
  market_cap REAL,
  float_market_cap REAL,
  roa REAL,
  roic REAL,
  revenue_yoy REAL,
  net_profit_yoy REAL,
  raw_json TEXT,
  source TEXT,
  updated_at TEXT,
  row_hash TEXT,
  PRIMARY KEY (symbol, report_date)
);
""",
        """
CREATE TABLE IF NOT EXISTS stock_sectors (
  symbol TEXT NOT NULL,
  sector_name TEXT NOT NULL,
  sector_type TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  updated_at TEXT,
  row_hash TEXT,
  PRIMARY KEY (symbol, sector_name, sector_type, source)
);
""",
        """
CREATE TABLE IF NOT EXISTS index_daily (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  open REAL,
  high REAL,
  low REAL,
  close REAL,
  volume REAL,
  amount REAL,
  source TEXT,
  updated_at TEXT,
  row_hash TEXT,
  PRIMARY KEY (symbol, date)
);
""",
        "CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily(date);",
        "CREATE INDEX IF NOT EXISTS idx_index_daily_symbol_date ON index_daily(symbol, date);",
        """
CREATE TABLE IF NOT EXISTS intraday_bars_manifest (
  symbol TEXT NOT NULL,
  period TEXT NOT NULL,
  month TEXT NOT NULL,
  storage_mode TEXT NOT NULL DEFAULT 'parquet',
  storage_path TEXT NOT NULL,
  partition_mode TEXT DEFAULT 'symbol_month',
  row_count INTEGER,
  start_datetime TEXT,
  end_datetime TEXT,
  source TEXT,
  updated_at TEXT,
  PRIMARY KEY (symbol, period, month, storage_path)
);
""",
        """
CREATE TABLE IF NOT EXISTS dragon_tiger (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  reason TEXT NOT NULL,
  buy_amount REAL,
  sell_amount REAL,
  net_amount REAL,
  seat_name TEXT NOT NULL DEFAULT '',
  seat_type TEXT,
  source TEXT,
  updated_at TEXT,
  row_hash TEXT,
  PRIMARY KEY (symbol, date, reason, seat_name)
);
""",
        """
CREATE TABLE IF NOT EXISTS block_trades (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  price REAL NOT NULL DEFAULT 0,
  volume REAL,
  amount REAL NOT NULL DEFAULT 0,
  premium_ratio REAL,
  buyer TEXT,
  seller TEXT,
  source TEXT,
  updated_at TEXT,
  row_hash TEXT,
  PRIMARY KEY (symbol, date, price, amount)
);
""",
        """
CREATE TABLE IF NOT EXISTS index_membership (
  index_symbol TEXT NOT NULL,
  symbol TEXT NOT NULL,
  effective_date TEXT NOT NULL,
  end_date TEXT,
  weight REAL,
  source TEXT,
  updated_at TEXT,
  PRIMARY KEY (index_symbol, symbol, effective_date)
);
""",
        """
CREATE TABLE IF NOT EXISTS data_quality_issues (
  issue_id TEXT PRIMARY KEY,
  symbol TEXT,
  table_name TEXT,
  date TEXT,
  issue_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  message TEXT,
  repair_status TEXT DEFAULT 'unresolved',
  detected_at TEXT,
  repaired_at TEXT,
  source TEXT
);
""",
        """
CREATE TABLE IF NOT EXISTS data_update_log (
  update_id TEXT PRIMARY KEY,
  task_type TEXT NOT NULL,
  source TEXT,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  row_count INTEGER,
  warning_count INTEGER,
  error_count INTEGER,
  details_json TEXT
);
""",
        """
CREATE TABLE IF NOT EXISTS data_source_status (
  source TEXT PRIMARY KEY,
  last_success_at TEXT,
  last_failure_at TEXT,
  status TEXT,
  failure_count INTEGER DEFAULT 0,
  last_error TEXT
);
""",
    ])
    return sql


def get_required_tables() -> list[str]:
    return [
        *DAILY_TABLES,
        "intraday_partial_daily",
        "fundamentals",
        "stock_sectors",
        "index_daily",
        "intraday_bars_manifest",
        "dragon_tiger",
        "block_trades",
        "index_membership",
        "data_quality_issues",
        "data_update_log",
        "data_source_status",
    ]


def get_required_indexes() -> list[str]:
    indexes: list[str] = []
    for table in DAILY_TABLES:
        indexes.extend([f"idx_{table}_date", f"idx_{table}_symbol_date"])
    indexes.extend(["idx_index_daily_date", "idx_index_daily_symbol_date"])
    return indexes


def initialize_schema(db) -> dict:
    for statement in get_schema_sql():
        db.execute(statement)
    return {"ok": True, "status": "passed", "table_count": len(get_required_tables())}
