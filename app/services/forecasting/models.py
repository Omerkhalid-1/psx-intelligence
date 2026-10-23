"""Candidate forecasting models behind a common interface.

Return models predict the h-session forward simple return; direction models predict
P(return > 0). All learned preprocessing (scaling) lives inside each model's sklearn Pipeline
and is fitted on the training window only. Hyperparameters are fixed a priori (not tuned on
the evaluation periods) to limit multiple-testing bias; see docs/research_methodology.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import StandardScaler

SEED = 7


class ReturnModel(Protocol):
    key: str
    kind: str  # "return" | "direction"

    def fit(self, X: pd.DataFrame, y: pd.Series, symbols: pd.Series) -> None: ...

    def predict(self, X: pd.DataFrame, symbols: pd.Series) -> np.ndarray: ...


@dataclass
class ZeroReturn:
    """No-change baseline: predicts a 0% return (price stays where it is)."""

    key: str = "zero"
    kind: str = "return"
    features: list[str] = field(default_factory=list)

    def fit(self, X, y, symbols) -> None:  # noqa: ANN001
        pass

    def predict(self, X, symbols) -> np.ndarray:  # noqa: ANN001
        return np.zeros(len(X))


@dataclass
class HistoricalMean:
    """Drift baseline: each symbol's mean training return (pooled mean for unseen symbols)."""

    key: str = "hist_mean"
    kind: str = "return"
    features: list[str] = field(default_factory=list)
    by_symbol: dict[str, float] = field(default_factory=dict)
    pooled: float = 0.0

    def fit(self, X, y, symbols) -> None:  # noqa: ANN001
        self.by_symbol = y.groupby(symbols.to_numpy()).mean().to_dict()
        self.pooled = float(y.mean())

    def predict(self, X, symbols) -> np.ndarray:  # noqa: ANN001
        return np.array([self.by_symbol.get(s, self.pooled) for s in symbols])


@dataclass
class SklearnReturn:
    key: str
    features: list[str]
    make: Any  # () -> sklearn estimator/pipeline
    kind: str = "return"
    model: Any = None

    def fit(self, X, y, symbols) -> None:  # noqa: ANN001
        self.model = self.make()
        self.model.fit(X[self.features].to_numpy(), y.to_numpy())

    def predict(self, X, symbols) -> np.ndarray:  # noqa: ANN001
        return self.model.predict(X[self.features].to_numpy())


@dataclass
class BaseRateDirection:
    """Direction baseline: each symbol's training frequency of positive returns."""

    key: str = "base_rate"
    kind: str = "direction"
    features: list[str] = field(default_factory=list)
    by_symbol: dict[str, float] = field(default_factory=dict)
    pooled: float = 0.5

    def fit(self, X, y, symbols) -> None:  # noqa: ANN001
        up = (y > 0).astype(float)
        self.by_symbol = up.groupby(symbols.to_numpy()).mean().to_dict()
        self.pooled = float(up.mean())

    def predict(self, X, symbols) -> np.ndarray:  # noqa: ANN001
        return np.array([self.by_symbol.get(s, self.pooled) for s in symbols])


@dataclass
class SklearnDirection:
    key: str
    features: list[str]
    make: Any
    kind: str = "direction"
    model: Any = None

    def fit(self, X, y, symbols) -> None:  # noqa: ANN001
        self.model = self.make()
        self.model.fit(X[self.features].to_numpy(), (y.to_numpy() > 0).astype(int))

    def predict(self, X, symbols) -> np.ndarray:  # noqa: ANN001
        return self.model.predict_proba(X[self.features].to_numpy())[:, 1]


def _ridge() -> Pipeline:
    return make_pipeline(StandardScaler(), Ridge(alpha=50.0))


def _hgb() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=150, min_samples_leaf=100,
                                         l2_regularization=1.0, random_state=SEED)


def _rf() -> RandomForestRegressor:
    return RandomForestRegressor(n_estimators=150, min_samples_leaf=100, max_features=0.5, n_jobs=-1,
                                 random_state=SEED)


def _logit() -> Pipeline:
    return make_pipeline(StandardScaler(), LogisticRegression(C=0.05, max_iter=2000))


def candidate_models(features: list[str], suffix: str = "tech") -> list[ReturnModel]:
    """The fixed candidate set for one feature set. Baselines need no features."""
    return [
        ZeroReturn(),
        HistoricalMean(),
        SklearnReturn(f"ridge_{suffix}", features, _ridge),
        SklearnReturn(f"hgb_{suffix}", features, _hgb),
        SklearnReturn(f"rf_{suffix}", features, _rf),
        BaseRateDirection(),
        SklearnDirection(f"logit_{suffix}", features, _logit),
    ]


def model_hyperparameters(model: ReturnModel) -> dict[str, Any]:
    est = getattr(model, "model", None) or (model.make() if hasattr(model, "make") else None)
    if est is None:
        return {}
    params = est.get_params(deep=True)
    return {k: v for k, v in params.items() if isinstance(v, int | float | str | bool | type(None))}
