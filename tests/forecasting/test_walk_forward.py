"""Walk-forward engine: embargo/leakage, reproducibility, ensembles, intervals and metrics."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting import metrics as M
from app.services.forecasting.backtest import BacktestConfig, evaluate, run_walk_forward, select_models
from app.services.forecasting.dataset import Panel
from app.services.forecasting.models import HistoricalMean, SklearnReturn, ZeroReturn, candidate_models


def synthetic_panel(n_days=700, symbols=("A", "B", "C"), horizons=(1, 5), seed=0, signal=0.0) -> Panel:
    """Synthetic pooled panel. y_h = signal * feature + noise; features known at origin."""
    rng = np.random.default_rng(seed)
    cal = pd.bdate_range("2020-01-01", periods=n_days)
    rows = []
    for s in symbols:
        x = rng.normal(0, 1, n_days)
        noise = rng.normal(0, 0.02, n_days)
        daily = signal * np.roll(x, 1) * 0.01 + noise  # return of t+1 depends on feature at t
        close = 100 * np.exp(np.cumsum(daily))
        df = pd.DataFrame({"date": cal, "symbol": s, "f1": x, "vol20": 0.3, "mkt_vol20": 0.2, "close": close,
                           "usable": True, "cal_pos": np.arange(n_days)})
        for h in horizons:
            df[f"y_{h}"] = pd.Series(close).shift(-h).to_numpy() / close - 1
            df[f"target_date_{h}"] = pd.Series(cal).shift(-h).to_numpy()
        rows.append(df)
    data = pd.concat(rows).sort_values(["date", "symbol"]).reset_index(drop=True)
    return Panel(data, "test", ["f1"], horizons, cal, "synthetic")


class RecordingModel:
    """Records the max cal_pos of every training set it sees."""

    key, kind, features = "spy", "return", ["f1"]

    def __init__(self):
        self.calls = []

    def fit(self, X, y, symbols):  # noqa: ANN001
        self.calls.append(int(X["cal_pos"].max()))

    def predict(self, X, symbols):  # noqa: ANN001
        self.calls.append(("predict", int(X["cal_pos"].min())))
        return np.zeros(len(X))


def cfg(**kw):
    base = dict(name="t", hypothesis="t", eval_start=date(2021, 1, 1), holdout_start=date(2022, 1, 1),
                horizons=(1, 5), refit_every=21, min_train_rows=100, interval_min=50, interval_window=200,
                ensemble_min_matured=50, ensemble_window_sessions=100)
    base.update(kw)
    return BacktestConfig(**base)


@pytest.mark.parametrize("h", [1, 5])
def test_training_labels_matured_before_refit(h):
    panel = synthetic_panel(horizons=(h,))
    spy = RecordingModel()
    run_walk_forward(panel, [spy], cfg(horizons=(h,)))
    it = iter(spy.calls)
    for train_max in it:
        kind, test_min = next(it)
        # last training origin + horizon must be strictly before the first test origin (embargo)
        assert train_max + h <= test_min - 1


def test_no_shuffling_and_reproducible():
    panel = synthetic_panel(signal=1.0)
    models = [ZeroReturn(), HistoricalMean(), *[m for m in candidate_models(["f1"]) if m.key == "ridge_tech"]]
    a = run_walk_forward(panel, models, cfg())
    b = run_walk_forward(panel, [ZeroReturn(), HistoricalMean(), *[m for m in candidate_models(["f1"])
                                                                    if m.key == "ridge_tech"]], cfg())
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))
    assert (a.groupby(["model_key", "horizon"])["date"].apply(lambda d: d.is_monotonic_increasing)).all()


def test_signal_is_detected_and_noise_is_not():
    """With a true signal ridge beats zero (DM p small); with pure noise it does not."""
    ridge = lambda: [m for m in candidate_models(["f1"]) if m.key == "ridge_tech"]  # noqa: E731
    for signal, expect_skill in ((3.0, True), (0.0, False)):
        panel = synthetic_panel(signal=signal, horizons=(1,), seed=4)
        c = cfg(horizons=(1,))
        res = evaluate(run_walk_forward(panel, [ZeroReturn(), HistoricalMean(), *ridge()], c), c)
        r = next(x for x in res["by_model"] if x["model"] == "ridge_tech" and x["split"] == "holdout")
        if expect_skill:
            assert r["mse_skill_vs_zero"] > 0.05 and r["dm_pvalue_vs_zero"] < 0.01
        else:
            assert r["mse_skill_vs_zero"] < 0.02


def test_feature_leak_would_be_caught():
    """Sanity check of the harness: a feature that IS the future return gives near-perfect skill,
    proving that the absence of skill on real data is not an artefact of the evaluation code."""
    panel = synthetic_panel(horizons=(1,))
    panel.data["leak"] = panel.data["y_1"]
    leaky = SklearnReturn("leaky", ["leak"], lambda: __import__("sklearn.linear_model").linear_model.LinearRegression())
    c = cfg(horizons=(1,))
    res = evaluate(run_walk_forward(panel, [ZeroReturn(), leaky], c), c)
    r = next(x for x in res["by_model"] if x["model"] == "leaky" and x["split"] == "holdout")
    assert r["mse_skill_vs_zero"] > 0.99


def test_ensembles_and_intervals_use_only_matured_information():
    panel = synthetic_panel(signal=1.0, horizons=(5,))
    c = cfg(horizons=(5,))
    models = [ZeroReturn(), HistoricalMean(), *[m for m in candidate_models(["f1"]) if m.key in ("ridge_tech",
                                                                                                   "hgb_tech")]]
    preds = run_walk_forward(panel, models, c)
    assert {"ens_equal_tech", "ens_invmse_tech"} <= set(preds["model_key"])
    ens = preds[preds["model_key"] == "ens_invmse_tech"]
    w = pd.DataFrame(list(ens["weights"]))
    assert np.allclose(w.sum(axis=1), 1.0)
    first = ens.sort_values("date").iloc[0]
    assert all(abs(v - 1 / len(first["weights"])) < 1e-12 for v in first["weights"].values())  # equal until evidence
    # first predictions have no interval (no matured residuals yet); later ones do
    ridge = preds[preds["model_key"] == "ridge_tech"].sort_values("date")
    assert ridge["lower"].isna().iloc[0] and ridge["lower"].notna().iloc[-1]


def test_interval_coverage_close_to_nominal_on_stationary_data():
    panel = synthetic_panel(n_days=1500, horizons=(1,), seed=7)
    c = cfg(horizons=(1,), eval_start=date(2021, 6, 1), holdout_start=date(2023, 6, 1), interval_window=500)
    res = evaluate(run_walk_forward(panel, [ZeroReturn(), HistoricalMean()], c), c)
    cov = next(x for x in res["by_model"] if x["model"] == "zero" and x["split"] == "holdout")["coverage_90"]
    assert cov == pytest.approx(0.90, abs=0.03)


def test_selection_rule_keeps_baseline_without_significant_skill():
    results = {"by_model": [
        {"model": "ridge_tech", "horizon": 1, "split": "validation", "mse_skill_vs_zero": 0.01,
         "dm_pvalue_vs_zero": 0.4, "rmse": 0.02},
        {"model": "ens_equal_tech", "horizon": 1, "split": "validation", "mse_skill_vs_zero": 0.012,
         "dm_pvalue_vs_zero": 0.3, "rmse": 0.0200},
        {"model": "ens_invmse_tech", "horizon": 1, "split": "validation", "mse_skill_vs_zero": 0.013,
         "dm_pvalue_vs_zero": 0.3, "rmse": 0.01999}]}
    sel = select_models(results, cfg())["1"]
    assert sel["selected"] == "zero"
    assert sel["adaptive_weights_retained"] is False  # 0.05% better is below the 0.5% threshold


# ---- metrics ----------------------------------------------------------------------------------------


def test_regression_metrics_and_dm():
    idx = pd.MultiIndex.from_product([pd.bdate_range("2024-01-01", periods=200), ["A"]], names=["date", "symbol"])
    rng = np.random.default_rng(0)
    actual = rng.normal(0, 0.02, len(idx))
    df = pd.DataFrame({"pred": actual * 0.5, "actual": actual}, index=idx)
    m = M.regression_metrics(df, 1, pd.Series(actual ** 2, index=idx))
    assert m["mse_skill_vs_zero"] == pytest.approx(0.75, abs=1e-6)
    assert m["directional_accuracy"] == 1.0 and m["dm_pvalue_vs_zero"] < 1e-6
    zero = M.regression_metrics(pd.DataFrame({"pred": 0.0, "actual": actual}, index=idx), 1)
    assert zero["directional_accuracy"] is None  # undefined for a constant-zero forecast


def test_classification_metrics_and_status():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.2, 0.8, 5000)
    y = (rng.uniform(size=5000) < p).astype(float) * 2 - 1  # outcomes drawn from p: calibrated
    m = M.classification_metrics(pd.DataFrame({"prob": p, "actual": y * 0.01}), 1)
    assert m["ece"] < 0.03 and m["brier_skill"] > 0
    assert M.probability_status(m) == "calibrated"
    bad = M.classification_metrics(pd.DataFrame({"prob": 1 - p, "actual": y * 0.01}), 1)
    assert M.probability_status(bad) == "not_calibrated"
    assert M.probability_status({"n": 10}) == "insufficient_evidence"


def test_interval_status_and_strategy_costs():
    assert M.interval_status({"coverage_90": 0.89, "interval_n": 1000}) == "calibrated"
    assert M.interval_status({"coverage_90": 0.80, "interval_n": 1000}) == "too_narrow"
    assert M.interval_status({"coverage_90": 0.99, "interval_n": 1000}) == "too_wide"
    days = pd.bdate_range("2024-01-01", periods=300)
    df = pd.DataFrame({"symbol": "A", "date": days, "pred": np.tile([0.01, -0.01], 150), "actual": 0.0})
    res = M.strategy_simulation(df, cost_per_side=0.001)
    assert res["strategy"]["annual_return"] < 0  # pure costs from flipping every day
