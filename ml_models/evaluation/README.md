# ml_models/evaluation/

Out-of-sample and walk-forward evaluation reports. **Only** numbers from this
folder are admissible evidence under `personality.md`'s acceptance bar — a
strong in-sample backtest from `backtesting/results/` is not enough.

## Layout

```
ml_models/evaluation/
├── walk_forward/
│   └── <model_filename>/
│       ├── folds.json        # dates of each train/test fold
│       ├── per_fold.parquet  # metrics per fold
│       ├── summary.json      # aggregated Sharpe/DD/etc., with confidence intervals
│       └── plots/            # equity curve, rolling Sharpe, drawdown, ...
└── ablations/
    └── <experiment>.md       # what was added/removed and the delta
```

## Required metrics in summary.json

- `sharpe_oos`, `sortino_oos`
- `max_drawdown`
- `turnover` (annualized)
- `n_trades`
- `concentration` — % of PnL contributed by the top symbol
- `degradation_vs_in_sample` — `(sharpe_is - sharpe_oos) / sharpe_is`

A model is **not** eligible for paper-trading if `degradation_vs_in_sample > 0.30`
or `sharpe_oos < 1.0`.
