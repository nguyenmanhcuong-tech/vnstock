# VNSTOCK Repository Bootstrap

This repository is structured as a control repository for VNSTOCK Gold data integration and backtest operations.

## Purpose
- Keep source-of-truth for code, configs, manifests, and runbooks
- Avoid storing large raw datasets in Git
- Make every sync/backtest reproducible and auditable

## Recommended structure
- `configs/` configuration files
- `docs/` architecture, contracts, runbooks
- `scripts/` runnable entrypoints
- `src/` reusable package code
- `data/samples/` tiny sample datasets only
- `data/manifests/` run and sync manifests
- `outputs/` generated reports and metrics (ignored in Git)

## Standard commands
```bash
python scripts/sync_vnstock_gold.py --config configs/vnstock_gold.yaml
python scripts/run_backtest.py --config configs/backtest_orb.yaml
```

## Notes
- Do not commit secrets
- Do not commit large CSV/Parquet files
- Persist dataset hashes and run manifests for auditability
