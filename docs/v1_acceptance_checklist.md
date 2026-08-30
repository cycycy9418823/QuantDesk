# QuantDesk v1.0 Acceptance Checklist

This checklist is for local manual acceptance. QuantDesk is a local A-share quantitative decision-support tool. It does not connect live trading, does not place orders, does not guarantee returns, and does not provide investment advice.

## 1. Installation

- Run `setup_windows.ps1`.
- Run `create_desktop_shortcuts.cmd` if desktop icons are needed.
- Run `self_check.cmd`.
- Confirm no FAIL items appear.

## 2. Primary 8000 App

- Run `start_ui.cmd` or `python scripts/qd.py app`.
- Confirm `http://127.0.0.1:8000/` loads the current daily research page.
- Confirm Dashboard / Strategy Center / AI 纸面交易员模块可见。
- Confirm the page keeps Research Only / Paper Account Only boundaries visible where applicable.
- Confirm no broker route, real-order, or position-advice action is presented.

## 3. AI Trader Compatibility

- Run `python -m ai_trader app`.
- Confirm `http://127.0.0.1:8001/` loads the compatibility classroom.
- Confirm the page is read-only / explain-only and does not offer real-order actions.

## 4. Real Daily Smoke

- Run `run_daily_smoke.cmd`.
- Confirm a small real daily report can be generated when the network and data source are available.
- Confirm Data Health has rows.
- Confirm News Health has rows, even when news is skipped or unavailable.
- Open the Markdown or Word report.
- Network, SSL, AKShare, empty interfaces, or delayed source updates can cause a smoke run to fail without implying live trading behavior.

## 5. Paper Portfolio

- Add a virtual buy from a candidate card.
- Confirm the UI says it is a virtual record and not a real trade.
- Refresh marks before 15:00 and confirm the position is marked as estimate-only / waiting for close.
- Refresh after 15:00 with a quote dated today and confirm it can settle.
- Refresh after 15:00 with an old quote and confirm it asks to wait for quote update.
- Use virtual sell and confirm no real order is placed.

## 6. Clean Zip

- Run `export_project.ps1`.
- Confirm `dist/quantdesk_clean.zip` exists.
- Confirm the zip excludes `.venv`, runtime caches, `logs`, `reports`, `results`, `state`, and `paper_portfolio.json`.
- Confirm the zip does not include retired sample launchers `run_report_sample.cmd`, `run_ui_sample.cmd`, or `run_ui_sample.sh`.
- Confirm paths inside the zip use `/`.

## 7. Risk Statement

- QuantDesk does not connect live trading.
- Candidate stocks are only observation objects.
- Historical backtests do not represent future returns.
- Paper portfolio results are virtual review records, not real account performance.
- Always check Data Health, News Health, Backtest Risk Summary, and paper-settlement status before using the report for research.
