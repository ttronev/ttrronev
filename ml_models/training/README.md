# ml_models/training/

Training scripts, hyperparameter configs, and feature-build pipelines.

## Layout (planned)

```
ml_models/training/
├── build_features.py      # data/raw → data/processed/features/
├── build_labels.py        # adds forward-return / triple-barrier labels
├── train_baseline.py      # gradient-boosted classifier on classic TA features
├── train_seq.py           # sequence model (TCN / Transformer) — later
├── configs/
│   ├── baseline_lgbm.yaml
│   └── ...
└── README.md
```

## Conventions

- Every training run writes to `ml_models/models/` with the naming convention
  in that folder's README, plus a JSON sidecar.
- Random seeds are fixed and recorded in the sidecar. No "lucky seed" tuning.
- Feature engineering must be deterministic given the same raw data; document
  any non-determinism (e.g. resampling alignment) explicitly.
- No data from the test split touches the training pipeline. The split is
  time-based and frozen in `data/processed/splits/`.

## Reproducibility checklist (must hold for any model promoted)

- [ ] Pinned dependency versions captured (`pip freeze` snapshot)
- [ ] Random seeds recorded
- [ ] Raw-data hash recorded
- [ ] Feature-build script's git SHA recorded
- [ ] Training script's git SHA recorded
- [ ] Walk-forward eval report saved in `ml_models/evaluation/`
