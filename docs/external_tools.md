# QuantDesk External Tools Research

This document records optional external quant tools that may be detected by
QuantDesk. The current RC only documents and detects these packages. It does
not copy third-party source code, does not install them by default, does not
connect live trading, and does not call their strategy, backtest, training, or
broker features.

## Current QuantDesk Main Line

QuantDesk should keep prioritizing:

- Real market quote settlement rules for virtual observation portfolios.
- News fetch status and news health clarity.
- Virtual portfolio review and auditability.
- Daily report reliability.
- Frontend usability for beginners.

External tools are optional integrations only. Missing optional tools must not
break the main QuantDesk workflow.

## Tool Matrix

| Tool | Positioning | QuantDesk Fit | Current Recommendation | Priority | Optional Dependency | Windows Complexity | License Caution | Copy Source? | User Risk | Stability Impact |
|---|---|---|---|---|---|---|---|---|---|---|
| AKShare | China market and finance data interface | Data provider, fallback, data health, news health | Continue improving provider layer first | high | yes | medium, network/API changes can matter | Check upstream license and data source terms before redistribution | no | Data may be delayed, unavailable, or changed by upstream interfaces | high impact because it feeds daily reports |
| vectorbt | Vectorized research and parameter scans | Optional research sandbox for fast historical studies | Detect only now; no strategy execution in RC | medium | yes | medium to high; dependencies can be heavy | Commons Clause caution; use as optional research dependency only | no | Parameter scans can encourage overfitting if misunderstood | medium impact if later isolated as plugin |
| Backtrader | Event-driven backtest framework | Architecture reference or separate plugin experiment | Detect only; do not mix source into main project | low | yes | medium | GPL-3.0 license caution; do not copy source into QuantDesk core | no | Users may confuse framework output with realistic execution | low if kept outside core |
| Microsoft Qlib | AI / factor research platform | Future AI/factor research plugin candidate | Detect only; no model training in RC | low | yes | high; heavy dependency stack | Check Microsoft Qlib license and model/data obligations before use | no | Heavy setup and model results may be misunderstood | high if integrated too early |
| FinRL / FinRL-X | Reinforcement learning research toolkit | Research experiments only, not daily stock selection | Detect only; not suitable for current daily workflow | low | yes | high; ML/RL stack can be fragile | Check project license and dependency licenses before experiments | no | RL examples can be unstable and hard for beginners to validate | high if exposed as decision tool |

## Conclusions

1. The current QuantDesk main line remains quote settlement, news status,
   virtual portfolio review, daily report reliability, and frontend usability.
2. External tools are currently limited to plugin detection and documentation.
3. Do not copy third-party source code into this project.
4. Do not connect live trading or broker APIs.
   In short: QuantDesk does not connect live trading.
5. Backtrader has GPL-3.0 license caution; do not directly mix GPL source into
   the main QuantDesk codebase.
6. vectorbt has Commons Clause caution; treat it as an optional research tool.
7. Qlib is better suited for later AI / factor research plugins.
8. FinRL is better suited for experiments, not current daily stock observation.
9. AKShare remains the priority in the data interface layer, especially
   provider fallback, data health, and news health.

These tools can be useful later, but they must stay optional, auditable, and
clearly separated from the beginner-facing daily decision support workflow.
