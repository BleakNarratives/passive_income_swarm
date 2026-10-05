"""
swarm.forecast — the predictive core
====================================

The analytics engine the rest of the swarm is built to feed. It is deliberately
standard-library only and dependency-free, because a forecasting engine that
cannot boot on the box that runs the business is a liability.

What is actually here
---------------------
* **Models** — naive, seasonal-naive, moving-average, EWMA, Holt's linear
  (level+trend, damped), an OLS linear trend, and a ridge regression over lag
  features. Each takes a numeric series and returns a horizon of predictions.
* **Metrics** — MAE, RMSE, MAPE, sMAPE, R². Honest error, not vibes.
* **Walk-forward backtesting** — expanding-window evaluation, the only fair way
  to compare forecasters on a short series.
* **Model registry** — every model's measured score, persisted, so model
  selection is a record rather than a memory.
* **Bridge to the ledger** — turn raw swarm events into series: job counts,
  reward trajectories, revenue per bucket.

What is *not* here yet (deliberately, see docs/SWARM.md): learned sequence models
and exogenous-driver features. The interface is frozen so they drop in behind
``Forecaster`` without touching callers.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .ledger import Ledger

__all__ = [
    "Forecaster",
    "NaiveModel",
    "SeasonalNaiveModel",
    "MovingAverageModel",
    "EWMAModel",
    "HoltLinearModel",
    "LinearTrendModel",
    "RidgeLinearModel",
    "DEFAULT_MODELS",
    "mae",
    "rmse",
    "mape",
    "smape",
    "r2",
    "backtest",
    "select_best",
    "ModelRegistry",
    "PredictiveEngine",
    "detect_anomalies",
]


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def mae(actual: Sequence[float], predicted: Sequence[float]) -> float:
    pairs = list(zip(actual, predicted))
    if not pairs:
        return 0.0
    return sum(abs(a - p) for a, p in pairs) / len(pairs)


def rmse(actual: Sequence[float], predicted: Sequence[float]) -> float:
    pairs = list(zip(actual, predicted))
    if not pairs:
        return 0.0
    return math.sqrt(sum((a - p) ** 2 for a, p in pairs) / len(pairs))


def mape(actual: Sequence[float], predicted: Sequence[float]) -> float:
    """Mean absolute percentage error; non-positive actuals are skipped."""
    total, n = 0.0, 0
    for a, p in zip(actual, predicted):
        if a == 0:
            continue
        total += abs((a - p) / a)
        n += 1
    return total / n if n else 0.0


def smape(actual: Sequence[float], predicted: Sequence[float]) -> float:
    """Symmetric MAPE — safe when values cross zero."""
    total, n = 0.0, 0
    for a, p in zip(actual, predicted):
        denom = (abs(a) + abs(p)) / 2
        if denom == 0:
            continue
        total += abs(a - p) / denom
        n += 1
    return total / n if n else 0.0


def r2(actual: Sequence[float], predicted: Sequence[float]) -> float:
    if len(actual) < 2:
        return 0.0
    mean = sum(actual) / len(actual)
    ss_tot = sum((a - mean) ** 2 for a in actual)
    ss_res = sum((a - p) ** 2 for a, p in zip(actual, predicted))
    if ss_tot == 0:
        return 1.0 if ss_res == 0 else 0.0
    return 1.0 - ss_res / ss_tot


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class Forecaster:
    """Base class. Subclasses implement :meth:`forecast`."""

    name = "base"

    def forecast(self, series: Sequence[float], horizon: int) -> List[float]:
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name}

    def _flat(self, value: float, horizon: int) -> List[float]:
        return [float(value)] * horizon


class NaiveModel(Forecaster):
    """Last value carried forward. The baseline every model must beat."""

    name = "naive"

    def forecast(self, series: Sequence[float], horizon: int) -> List[float]:
        if not series:
            return [0.0] * horizon
        return self._flat(series[-1], horizon)


class SeasonalNaiveModel(Forecaster):
    name = "seasonal_naive"

    def __init__(self, period: int = 7):
        self.period = max(1, period)

    def forecast(self, series: Sequence[float], horizon: int) -> List[float]:
        if not series:
            return [0.0] * horizon
        if len(series) < self.period:
            return self._flat(series[-1], horizon)
        return [float(series[-self.period + (h % self.period)]) for h in range(horizon)]

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "period": self.period}


class MovingAverageModel(Forecaster):
    name = "moving_average"

    def __init__(self, window: int = 3):
        self.window = max(1, window)

    def forecast(self, series: Sequence[float], horizon: int) -> List[float]:
        tail = list(series)[-self.window :]
        value = sum(tail) / len(tail) if tail else 0.0
        return self._flat(value, horizon)

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "window": self.window}


class EWMAModel(Forecaster):
    name = "ewma"

    def __init__(self, alpha: float = 0.4):
        self.alpha = min(1.0, max(0.01, alpha))

    def forecast(self, series: Sequence[float], horizon: int) -> List[float]:
        if not series:
            return [0.0] * horizon
        level = float(series[0])
        for value in series[1:]:
            level = (1 - self.alpha) * level + self.alpha * value
        return self._flat(level, horizon)

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "alpha": self.alpha}


class HoltLinearModel(Forecaster):
    """Holt's linear method: exponential level + damped trend."""

    name = "holt"

    def __init__(self, alpha: float = 0.5, beta: float = 0.3, damped: float = 0.9):
        self.alpha = alpha
        self.beta = beta
        self.damped = damped

    def forecast(self, series: Sequence[float], horizon: int) -> List[float]:
        s = list(series)
        if not s:
            return [0.0] * horizon
        if len(s) < 2:
            return self._flat(s[0], horizon)
        level = float(s[0])
        trend = float(s[1] - s[0])
        for value in s[1:]:
            prev_level = level
            level = self.alpha * value + (1 - self.alpha) * (level + trend)
            trend = self.beta * (level - prev_level) + (1 - self.beta) * trend
        out: List[float] = []
        phi = self.damped
        acc = 0.0
        for h in range(1, horizon + 1):
            acc += (phi ** h) * trend
            out.append(level + acc)
        return out

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "alpha": self.alpha,
            "beta": self.beta,
            "damped": self.damped,
        }


class LinearTrendModel(Forecaster):
    """Ordinary least squares y = a + b*t, extrapolated."""

    name = "linear_trend"

    def forecast(self, series: Sequence[float], horizon: int) -> List[float]:
        s = list(series)
        n = len(s)
        if n == 0:
            return [0.0] * horizon
        if n == 1:
            return self._flat(s[0], horizon)
        xs = list(range(n))
        mx = (n - 1) / 2
        my = sum(s) / n
        denom = sum((x - mx) ** 2 for x in xs) or 1.0
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, s)) / denom
        intercept = my - slope * mx
        return [intercept + slope * (n - 1 + h) for h in range(1, horizon + 1)]


def _solve(matrix: List[List[float]], vector: List[float]) -> List[float]:
    """Gaussian elimination with partial pivoting. Raises on singular input."""
    n = len(matrix)
    aug = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            aug[pivot][col] = 1e-12  # ridge nudge keeps it solvable
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pv = aug[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col] / pv
            for c in range(col, n + 1):
                aug[r][c] -= factor * aug[col][c]
    return [aug[i][n] / aug[i][i] for i in range(n)]


class RidgeLinearModel(Forecaster):
    """Ridge regression over lag features — the workhorse autoregressor.

    Builds a design matrix from the previous ``max(lags)`` observations and
    solves the normal equations with a ridge penalty, then predicts
    recursively for multi-step horizons.
    """

    name = "ridge_linear"

    def __init__(self, lags: Tuple[int, ...] = (1, 2, 3), ridge: float = 1e-3):
        self.lags = tuple(sorted(lags)) or (1,)
        self.ridge = ridge

    def _design(self, series: Sequence[float]) -> Tuple[List[List[float]], List[float]]:
        s = list(series)
        m = max(self.lags)
        X: List[List[float]] = []
        y: List[float] = []
        for t in range(m, len(s)):
            X.append([1.0] + [s[t - lag] for lag in self.lags])
            y.append(s[t])
        return X, y

    def forecast(self, series: Sequence[float], horizon: int) -> List[float]:
        s = list(series)
        m = max(self.lags)
        if len(s) <= m:
            return NaiveModel().forecast(s, horizon)
        X, y = self._design(s)
        p = len(X[0])
        # normal equations: (X^T X + ridge I) w = X^T y
        XtX = [[sum(X[r][i] * X[r][j] for r in range(len(X))) for j in range(p)] for i in range(p)]
        for i in range(p):
            XtX[i][i] += self.ridge
        Xty = [sum(X[r][i] * y[r] for r in range(len(X))) for i in range(p)]
        w = _solve(XtX, Xty)

        out: List[float] = []
        buf = s[:]
        for _ in range(horizon):
            feats = [1.0] + [buf[-lag] for lag in self.lags]
            pred = sum(wi * fi for wi, fi in zip(w, feats))
            out.append(pred)
            buf.append(pred)
        return out

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "lags": list(self.lags), "ridge": self.ridge}


def DEFAULT_MODELS() -> List[Forecaster]:
    """The default candidate set, cheapest to richest."""
    return [
        NaiveModel(),
        MovingAverageModel(window=3),
        MovingAverageModel(window=7),
        EWMAModel(alpha=0.4),
        HoltLinearModel(),
        LinearTrendModel(),
        RidgeLinearModel(lags=(1, 2, 3)),
    ]


# ---------------------------------------------------------------------------
# backtesting
# ---------------------------------------------------------------------------
def backtest(
    model: Forecaster,
    series: Sequence[float],
    horizon: int = 1,
    min_train: int = 3,
) -> Dict[str, Any]:
    """Expanding-window walk-forward evaluation. No lookahead."""
    s = list(series)
    actuals: List[float] = []
    preds: List[float] = []
    for t in range(min_train, len(s) - horizon + 1):
        train = s[:t]
        expected = s[t : t + horizon]
        try:
            predicted = model.forecast(train, horizon)
        except Exception:  # a broken model must not sink the sweep
            continue
        actuals.extend(expected)
        preds.extend(predicted[: len(expected)])
    if not actuals:
        return {"fold_size": 0, "mae": None, "rmse": None, "mape": None, "smape": None, "r2": None}
    return {
        "fold_size": len(actuals),
        "mae": round(mae(actuals, preds), 4),
        "rmse": round(rmse(actuals, preds), 4),
        "mape": round(mape(actuals, preds), 4),
        "smape": round(smape(actuals, preds), 4),
        "r2": round(r2(actuals, preds), 4),
    }


def select_best(
    series: Sequence[float],
    models: Optional[Iterable[Forecaster]] = None,
    horizon: int = 1,
) -> Tuple[Forecaster, Dict[str, Any]]:
    """Backtest candidates and return the lowest-RMSE model plus the scoreboard."""
    candidates = list(models or DEFAULT_MODELS())
    board: List[Dict[str, Any]] = []
    for model in candidates:
        result = backtest(model, series, horizon)
        board.append({"model": model.name, **model.describe(), **result})
    scored = [b for b in board if b.get("rmse") is not None]
    if not scored:
        return candidates[0], {"board": board, "winner": candidates[0].name}
    winner = min(scored, key=lambda b: b["rmse"])
    model = next(m for m in candidates if m.name == winner["model"])
    return model, {"board": board, "winner": winner["model"]}


# ---------------------------------------------------------------------------
# model registry
# ---------------------------------------------------------------------------
class ModelRegistry:
    """Persists measured model performance so selection is auditable."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.records = dict(raw.get("models") or {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "models": self.records,
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def record(self, metric: str, name: str, score: Dict[str, Any], params: Optional[Dict[str, Any]] = None) -> None:
        entry = self.records.setdefault(name, {"metrics": {}, "params": params or {}})
        entry["metrics"][metric] = score
        entry["params"] = params or entry.get("params") or {}
        entry["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.save()

    def best_for(self, metric: str) -> Optional[str]:
        scored = [
            (name, e["metrics"][metric].get("rmse"))
            for name, e in self.records.items()
            if metric in e.get("metrics", {}) and e["metrics"][metric].get("rmse") is not None
        ]
        if not scored:
            return None
        return min(scored, key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# anomaly detection
# ---------------------------------------------------------------------------
def detect_anomalies(series: Sequence[float], window: int = 10, z: float = 2.5) -> List[Dict[str, Any]]:
    """Flag points more than ``z`` standard deviations from a trailing mean."""
    s = list(series)
    out: List[Dict[str, Any]] = []
    for i in range(window, len(s)):
        tail = s[i - window : i]
        mean = sum(tail) / len(tail)
        var = sum((x - mean) ** 2 for x in tail) / max(1, len(tail) - 1)
        std = math.sqrt(var)
        if std == 0:
            # A perfectly flat baseline makes any deviation maximally anomalous.
            if s[i] != mean:
                out.append(
                    {
                        "index": i,
                        "value": s[i],
                        "expected": round(mean, 4),
                        "z": 99.9,
                        "direction": "spike" if s[i] > mean else "drop",
                    }
                )
            continue
        score = (s[i] - mean) / std
        if abs(score) >= z:
            out.append(
                {
                    "index": i,
                    "value": s[i],
                    "expected": round(mean, 4),
                    "z": round(score, 3),
                    "direction": "spike" if score > 0 else "drop",
                }
            )
    return out


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
class PredictiveEngine:
    """Ties the ledger, the models and the registry into one analytical core."""

    def __init__(
        self,
        ledger: Optional[Ledger] = None,
        registry: Optional[ModelRegistry] = None,
        models: Optional[Iterable[Forecaster]] = None,
    ):
        self.ledger = ledger
        self.registry = registry
        self.models = list(models or DEFAULT_MODELS())

    # -- series construction ----------------------------------------------
    @staticmethod
    def series_from_events(
        events: Iterable[Any],
        value_fn: Callable[[Any], Optional[float]],
    ) -> List[float]:
        """Project arbitrary ledger events into a numeric series."""
        out: List[float] = []
        for event in events:
            try:
                value = value_fn(event)
            except Exception:
                value = None
            if value is not None:
                out.append(float(value))
        return out

    def revenue_series(self) -> List[float]:
        if not self.ledger:
            return []
        return self.series_from_events(
            self.ledger.query(type="complete"),
            lambda e: e.data.get("result", {}).get("revenue_msat"),
        )

    def reward_series(self, skill_id: Optional[str] = None) -> List[float]:
        if not self.ledger:
            return []
        return self.series_from_events(
            self.ledger.query(type="skill_run"),
            lambda e: e.data.get("reward")
            if (skill_id is None or e.data.get("skill") == skill_id)
            else None,
        )

    def count_series(self, event_type: str) -> List[float]:
        """Cumulative count of an event type, bucketed by event index."""
        if not self.ledger:
            return []
        return [float(i + 1) for i, _ in enumerate(self.ledger.query(type=event_type))]

    # -- forecasting -------------------------------------------------------
    def forecast(
        self,
        series: Sequence[float],
        horizon: int = 1,
        metric: str = "series",
        model: Optional[Forecaster] = None,
    ) -> Dict[str, Any]:
        if not series:
            return {"model": None, "horizon": horizon, "predictions": [], "backtest": None}
        chosen, scoreboard = (model, None) if model else select_best(series, self.models, horizon)
        predictions = chosen.forecast(series, horizon)
        result: Dict[str, Any] = {
            "model": chosen.name,
            "params": chosen.describe(),
            "horizon": horizon,
            "predictions": [round(p, 4) for p in predictions],
            "last_observed": series[-1],
            "backtest": backtest(chosen, series, horizon),
        }
        if scoreboard:
            result["scoreboard"] = scoreboard["board"]
        if self.registry:
            self.registry.record(metric, chosen.name, result["backtest"], chosen.describe())
        return result

    def anomalies(self, series: Sequence[float]) -> List[Dict[str, Any]]:
        return detect_anomalies(series)
