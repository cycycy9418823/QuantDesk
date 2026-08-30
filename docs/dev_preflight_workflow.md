# Codex 开发预检流程

每次较大改动、启动 UI 或跑验证前，建议先做离线预检：

```bash
python scripts/qd.py dev-preflight --target-date 2026-05-20 --network false
```

dev-preflight 会检查：

- 当前 QuantDesk 版本。
- 本地 A 股交易日历是否可用。
- `data/daily` 是否落后目标完整交易日。
- 应该先运行的 data-update / data-health 命令。

默认不联网。默认行为：

- 不联网。
- 不跑策略。
- 不跑回测。
- 不调用 LLM。
- 不真实下单。
- 不扫描全 A。

若确实需要补齐低价学习池 `included=true` 日线，必须显式联网：

```bash
python scripts/qd.py dev-preflight --target-date 2026-05-20 --enable-network
```

补完数据后推荐顺序：

```bash
python scripts/qd.py data-health --symbols 600744,002115 --target-date 2026-05-20 --network false
python scripts/qd.py daily-retain --symbols 600744,002115 --target-date 2026-05-20 --network false
python scripts/qd.py strategy-validate --symbol 600744 --target-date 2026-05-20 --network false
python scripts/qd.py observation-report --symbol 600744 --target-date 2026-05-20 --network false --llm false
```

本流程只用于本地开发和数据状态诊断，不构成投资建议。
