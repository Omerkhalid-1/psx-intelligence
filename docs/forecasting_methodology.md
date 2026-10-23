# Forecasting methodology

Status: **specification**. Implementation begins in Phase 5. Items marked *Unresolved* depend
on the licensed data's field definitions.

## Definitions

| Term | Definition |
|---|---|
| Session | A PSX trading day on which a validated official close exists for the instrument. |
| Calendar | `market_calendar_sessions`: exchange-wide session dates (Asia/Karachi), built from the licensed feed's session list or PSX holiday notices. Not inferred from a single stock's bars, because a suspended stock has missing bars on valid sessions. |
| Forecast origin `t` | The **last validated completed session** at generation time (never wall-clock "today"). |
| Horizon `h` | 5, 20 or 60 **exchange sessions** after `t` (calendar sessions, not the stock's own bars). |
| Target session | `t + h` on the exchange calendar. Fixed at forecast time and stored. |
| Reference price | Adjusted close at `t`, as known at generation time. |

## Target

Primary target: **forward price return**

```
forward_return(t, h) = adj_close(t + h) / adj_close(t) - 1
```

- **Adjustment basis:** both closes are on the *same* adjustment basis: adjusted for splits and
  bonus issues that occur in `(t, t+h]`. Cash dividends are **not** added back (price return, not
  total return). A total-return variant may be added later as a separate target definition.
- *Unresolved:* whether the licensed feed supplies adjusted prices or corporate-action records.
  Until known, the pipeline stores raw closes plus `corporate_actions`, and computes adjustment
  factors itself.
- **Suspension / missing close at `t+h`:** the forecast is evaluated as `invalid` with reason
  `missing_target_observation`. We do not substitute the next available close, because that would
  silently change the horizon.
- **Unhandled corporate action inside the window** (e.g. merger, symbol change): `invalid` with
  reason `corporate_action_unhandled`.
- **Delisting:** `invalid` with reason `instrument_delisted`. Reported separately in evaluation
  statistics so survivorship effects stay visible.

Secondary target (classification): `1[forward_return(t, h) > 0]`, for directional probability.
Probabilities are only shown as calibrated after calibration is evaluated (see below).

## Point-in-time rules

- Market data: only bars with `session_date <= t`.
- Financial statements: only those with `published_at` (PSX/exchange filing time) `<= ` the close
  of session `t`. Statements with unknown `published_at` are **excluded** (not assumed).
- News/announcements: `published_at <=` close of session `t` (15:30 PKT normal session; exact
  cut-off configurable). Items with unknown publication time are excluded.
- Macro series: by **release date**, not reference period.
- Revisions: the value as first published is used for historical origins. Later revisions are
  stored as new versions with `valid_from`.
- Model fitting: scalers, imputation and feature selection are fitted only on the training
  window of each walk-forward fold.

## Models (in order of introduction)

1. `zero_return`: predicts 0.
2. `historical_mean`: mean `h`-session return over a trailing window (default 750 sessions).
3. `momentum`: sign/scale of trailing return, fitted coefficient.
4. `ridge`: regularised linear regression on technical + market features.
5. `logistic`: directional probability.
6. Gradient boosting (LightGBM): only after 1–5 are evaluated and the pipeline is stable.

Every model is registered with type, hyperparameters, training window, feature-set version and
code version.

## Uncertainty

- **Prediction intervals:** empirical quantiles of out-of-sample residuals from earlier
  walk-forward folds (split-conformal style), per horizon. Reported at 80% by default with
  `empirical_coverage` once enough evaluated forecasts exist. Overlapping horizons make residuals
  autocorrelated, so coverage estimates carry a stated effective sample size.
- **Downside risk:** 5% quantile of predicted return distribution, labelled as model-based.
- **Calibration:** a probability is labelled `calibrated` only if a reliability evaluation on at
  least N (configurable, default 200) out-of-sample forecasts passes. Otherwise the API returns the
  number with `calibration_status: "not_evaluated"`, or omits it.

## Evaluation

- Chronological walk-forward with expanding (or rolling) training window and an **embargo of `h`
  sessions** between training labels and the test origin, to prevent label overlap.
- Metrics per model × horizon: MAE, RMSE, directional accuracy, Brier score (classifier),
  interval coverage, and comparison against the `zero_return` and `historical_mean` baselines on
  identical origins.
- Breakdown by period and market regime (Module 1 classification as known at origin) where the
  sample allows. Sample sizes are always reported.
- No trading-strategy P&L is reported in the initial release. If added, it must state transaction
  costs, taxes (CGT), liquidity and slippage assumptions.

## Live forecast lifecycle

`forecasts` (immutable, inserted before outcome exists) → scheduled evaluator checks whether the
target session's validated close exists → writes `forecast_outcomes` (`evaluated` or `invalid` with
reason). A forecast is never evaluated before its target session has a validated close.
