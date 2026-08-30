# Backtest Statistical Validation

`v3.2.6-backtest-statistical-validation-and-feature-quality-audit` adds conservative statistical diagnostics for existing backtest results.

It is not a strategy optimizer. It does not search for high returns, does not tune parameters, does not change scoring, and does not change backtest return logic.

`stats-validate` and `param-sensitivity` answer different questions. `stats-validate` asks whether the current sample looks stronger than a random baseline. `param-sensitivity` asks whether the result is fragile when parameters are perturbed. If statistical validation is not passed, parameter stability should be interpreted cautiously and never as proof of future returns.

## Command

```powershell
python scripts/qd.py stats-validate --symbol 600744 --target-date 2026-05-19 --network false --iterations 500 --seed 42
```

`network=false` is the default. This command reads existing local backtest output and does not fetch market data.

## Permutation Test

A permutation test builds a random baseline by reshuffling signs or signal/return pairings and comparing the existing strategy metric with that random distribution. A small `p_value` can show that the current sample is unusual versus the chosen random baseline, but it is not proof of future effectiveness.

## Bootstrap Confidence Intervals

Bootstrap resampling repeatedly samples the existing returns with replacement and estimates intervals such as annual return CI, Sharpe CI, max drawdown CI, and win rate CI. These intervals are sample-based uncertainty estimates, not forecasts.

## Interpretation Boundaries

- Do not rely only on a single annual return number.
- Do not treat `p_value` as proof that future returns will persist.
- If the sample is too small, QuantDesk reports `insufficient_sample`.
- If the permutation test is not passed, reports should say: current samples do not show clear evidence above the random baseline.
- No broker API, no real orders, no investment advice.

## Outputs

- `results/backtest_statistical_validation_latest.json`
- `reports/backtest_statistical_validation_report_latest.md`

Runtime outputs are excluded from clean zip exports.
