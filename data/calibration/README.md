# Confidence-k calibration evidence

Split-conformal calibration of the per-series spread multiplier **k** used by the
tolerance-band confidence score of the Graph 1/2 daily price forecasts.

## Method (identical for every price series)

- Chronos-2 rolling-origin backtests: one forecast origin every 30 days, forecast
  horizons 90 / 180 / 365 days.
- Asymmetric split-conformal nonconformity score per calibration row:
  `max(0, (P50 - actual) / (P50 - P10), (actual - P50) / (P90 - P50))`.
- Chronological 60/40 origin split: the first 26 origins calibrate, the last 18
  validate.
- Series k = the 80% order statistic of the calibration scores:
  rank `ceil((n + 1) * 0.80)` over `n = 16,510` calibration rows -> rank 13,209.
- Validation covers all rows of the last 18 origins pooled across horizons; the
  target is >= 80% of realized actuals inside the k-stretched P10-P90 band.
  Pooled observations are not independent (~18 independent origins).

## Calibrated values

| Series | k | Raw validation coverage | Calibrated validation coverage |
|---|---|---|---|
| Retail (`retail`) | 1.266404 | 79.42% | 90.20% |
| Wholesale (`wholesale`) | 1.084111 | 87.21% | 89.66% |
| Mandi modal (`mandi_modal`) | 1.260045 | 77.86% | 88.97% |

Both wholesale/mandi targets self-report "ABOVE TARGET BAND (OVERCOVERAGE)"
under the evidence CSV's +/-5 percentage-point tolerance rule (calibrated
coverage above the 80% target).

## Files

| File | Content |
|---|---|
| `Untitled8_fixed_reproducible_v2.ipynb` | Retail calibration notebook (executed): prints k = 1.266404, the split/rank geometry, the per-horizon validation table, and saves the retail evidence CSV on its run host. |
| `Untitled8_fixed_reproducible_v3_wholesale_mandi.ipynb` | Wholesale/mandi calibration notebook (method reference; the calibration cell in this shared copy carries no stored execution outputs). |
| `Chronos2_Wholesale_Mandi_K_Evidence.csv` | Reported K, coverage, rank, and split geometry per WM target. |
| `Chronos2_Wholesale_Mandi_Rolling_Backtests.csv` | Raw backtest forecasts (target, origin, date, horizon_days, P10, P50, P90, actual_price) for both WM targets; k is fully reproducible from this file alone. |
| `Chronos2_Wholesale_Mandi_Validation_Forecasts.csv` | Held-out validation rows with the calibrated bands and per-row hit flags. |
| `Chronos2_Wholesale_Mandi_Validation_Metrics_By_Horizon.csv` | Coverage / MAE per horizon and target. |

## Verification independently reproduced (2026-11-06)

Recomputing the full pipeline from `Chronos2_Wholesale_Mandi_Rolling_Backtests.csv`
alone (chronological 60/40 origin split -> nonconformity scores -> 80% order
statistic) reproduces the reported values exactly to within CSV rounding:

| Series | Recomputed k | Reported k | Delta |
|---|---|---|---|
| Wholesale | 1.084111468 | 1.084111 | 4.7e-07 |
| Mandi modal | 1.260044934 | 1.260045 | 6.6e-08 |

The recomputed validation coverage matches the reported table
(wholesale 89.66%, mandi modal 88.97%) and the per-horizon metrics CSV.

## Sensitivity: horizons <= 180 days (API cap)

The pooled calibration includes the 365-day regime (~43% of validation mass) that
the API never serves (cap: 180 days). Re-fitting k on the 90/180-day rows of the
same backtest CSV gives wholesale 1.041855 and mandi modal 1.319127. Both variants
deliver similar on-target coverage (~88-90%); the shipped values keep the pooled
fit so all three series share one identical calibration protocol. The <= 180-day
refit is recorded here as a sensitivity analysis only.
