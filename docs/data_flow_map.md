# QuantDesk Data Flow Map

> v2.5.2 — 数据流图，标注每个流程的输入、输出、耗时和安全约束。
>
> 当前 active surface 是 `scripts/quant_api_server.py` + `app/templates/daily_research.html`
> + `app/static/js/daily_research.js`（8000）。历史 `/api/results` /
> `quant_app_server.py` 流程已退休，下面用当前 8000 入口描述。

## A. 首次建库 Backfill

```
data/universe/a_share_symbols.csv
  + start_date / end_date / provider chain
  ↓
  update_market_data.py --backfill --all-a-share --start-date 20150101
  ↓
  data/daily/{symbol}.csv  (每只股票一个 CSV)
  data/cache/update_status.json
  data/cache/cache_inventory.json
```

- **重任务**：可能耗时数小时。
- 仅首次建库或修复历史时运行。
- **不应由 UI 初始加载自动触发。**
- UI 中放在"高级操作"区域，需要用户确认。

## B. 每日增量 Incremental

```
data/daily/{symbol}.csv (已有)
  + expected_latest_trading_day
  + lookback_days (默认 10)
  ↓
  update_market_data.py --all-a-share --incremental --lookback-days 10 --resume
  ↓
  更新后的 data/daily/{symbol}.csv
  data/cache/update_status.json
  data/cache/cache_inventory.json
```

- 只补缺失交易日，本地已最新则跳过。
- **不重新下载 2015 至今全部历史。**
- 历史不足但日期新鲜时只标记 `history_short`，不每天重拉多年历史。
- 通常 5–30 分钟完成。

## C. Retry Failed

```
failed_symbols (from update_status.json)
  + provider chain
  ↓
  update_market_data.py --retry-failed
  ↓
  补齐成功的 data/daily/{symbol}.csv
  更新后的 update_status.json
```

- 只处理上次失败的股票，不影响已成功的。

## D. 股票池更新 Universe Update

```
AKShare / 外部数据源
  ↓
  update_a_share_universe.py
  ↓
  data/universe/a_share_symbols.csv
```

- 只更新股票池，不生成日报，不回补日线。

## E. 生成日报 Daily Report

```
本地 data/daily/*.csv
  + data/universe/a_share_symbols.csv
  + data/cache/update_status.json
  + config/default.yaml
  ↓
  daily_quant_report.py --spot-source cache
  ↓
  reports/*_quant_report.md / .docx
  results/*_candidates.csv / *_long_candidates.csv / *_board_candidates.csv
  results/latest_results.json
```

- **默认只读本地行情**，不默认联网更新。
- 不默认跑 backfill，不默认跑 walk-forward。

## F. 重新读取本地结果

```
results/* latest JSON
  + data/cache/update_status.json
  + data/cache/cache_inventory.json
  + state/ai_trader/*
  ↓
  scripts/quant_api_server.py
  ↓
  /api/daily/overview
  /api/decision-board
  /api/strategy-scorecard
  /api/ai-trader/dashboard
  ↓
  API JSON payloads
```

- **只读**，必须快。
- **不得触发下载、报告生成、CSV 全量扫描。**
- 优先使用 `cache_inventory.json` 避免扫描 5000+ CSV。

## G. UI 初始加载

```
浏览器 → GET /
  ↓
  daily_research.html + daily_research.js
  ↓
  GET /api/launcher/build-id
  GET /api/daily/overview
  GET /api/decision-board
  GET /api/strategy-scorecard
  GET /api/data-health/dashboard
  GET /api/forward-pit/readiness
  GET /api/ai-trader/dashboard
  ↓
  渲染页面
```

- **只读**，不联网，不生成报告。
- 不全量扫描 CSV，不跑更新任务。
- 优先使用轻量 JSON（`update_status.json`, `cache_inventory.json`）。

## H. 8001 AI Trader 兼容入口

```
浏览器 → GET http://127.0.0.1:8001/
  ↓
  ai_trader app compatibility classroom
  ↓
  只读展示 AI 纸面账本与解释型聊天
```

- 8001 是兼容教室页，不替代 8000 主研究台。
- 不连接券商，不下单，不改写 `state/auto_paper_portfolio.json`。
