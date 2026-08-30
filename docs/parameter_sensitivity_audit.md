# Parameter Sensitivity Audit

`v3.2.9-parameter-sensitivity-and-robustness-audit` adds a bounded, local diagnostic for checking whether an existing strategy result is overly dependent on a narrow set of parameters.

This is not a parameter optimizer. It does not search for the highest return, does not recommend “best parameters”, does not modify strategy defaults, does not write strategy configuration, and does not change scoring formulas or backtest return logic. The output is only for local learning, robustness review, and overfitting risk notes; it does not constitute investment advice.

## Command

```powershell
python scripts/qd.py param-sensitivity --symbol 600744 --target-date 2026-05-19 --network false --max-combinations 50 --seed 42
```

Defaults remain conservative:

- `network=false`
- `strict=true`
- `max_combinations=50`
- `seed=42`

In this version, even `network=true` does not actively fetch行情. The flag is kept for future compatibility only.

## What It Checks

- **Metric instability**: large standard deviation across parameter combinations.
- **Narrow peak**: only a very small number of combinations look better than the rest, which may indicate parameter-picking bias.
- **Parameter cliff**: neighboring combinations have sharp jumps, suggesting fragile results.
- **Insufficient trades / windows**: too few observations to judge robustness.
- **Drawdown sensitivity**: max drawdown changes too much under small parameter changes.
- **No robust edge**: all combinations are weak in the current sample.

## Robustness Score

`robustness_score` is a bounded diagnostic score from `0` to `1`. Higher means the audited sample appears less sensitive to the tested perturbations. It is not a return forecast and must not be read as proof that a strategy will work in the future.

If `stats-validate` has not passed, parameter stability has limited meaning. Statistical validation asks whether a result looks stronger than a random baseline; parameter sensitivity asks whether the result changes sharply when parameters are perturbed. They answer different questions and neither one is investment advice.

## Outputs

- `results/parameter_sensitivity_latest.json`
- `reports/parameter_sensitivity_report_latest.md`

These are runtime outputs and do not enter the clean zip.

## Config

`config/parameter_sensitivity.example.json` is an example of small, bounded ranges. A real `config/parameter_sensitivity.json` is optional and must not enter the clean zip.

The audit never writes scan results back to strategy configuration and never expands the search range automatically.
