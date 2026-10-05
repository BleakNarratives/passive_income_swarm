import tempfile
import unittest
from pathlib import Path

from swarm.ledger import Ledger
from swarm.forecast import (
    EWMAModel,
    LinearTrendModel,
    ModelRegistry,
    MovingAverageModel,
    NaiveModel,
    PredictiveEngine,
    RidgeLinearModel,
    backtest,
    detect_anomalies,
    mae,
    mape,
    r2,
    rmse,
    select_best,
    smape,
)


class TestMetrics(unittest.TestCase):
    def test_perfect_prediction_is_zero_error(self):
        self.assertEqual(mae([1, 2, 3], [1, 2, 3]), 0.0)
        self.assertEqual(rmse([1, 2, 3], [1, 2, 3]), 0.0)
        self.assertEqual(mape([1, 2, 3], [1, 2, 3]), 0.0)
        self.assertEqual(smape([1, 2, 3], [1, 2, 3]), 0.0)
        self.assertEqual(r2([1, 2, 3], [1, 2, 3]), 1.0)

    def test_known_errors(self):
        self.assertEqual(mae([2, 4], [1, 1]), 2.0)
        self.assertAlmostEqual(rmse([2, 4], [1, 1]), 5 ** 0.5)
        self.assertEqual(mape([10], [11]), 0.1)

    def test_r2_of_mean_predictor_is_zero(self):
        self.assertAlmostEqual(r2([1, 2, 3, 4], [2.5, 2.5, 2.5, 2.5]), 0.0)


class TestModels(unittest.TestCase):
    def test_naive_carries_last(self):
        self.assertEqual(NaiveModel().forecast([5, 7, 9], 3), [9, 9, 9])

    def test_moving_average_is_flat(self):
        self.assertEqual(MovingAverageModel(window=3).forecast([1, 2, 3, 4], 2), [3.0, 3.0])

    def test_ewma_returns_flat_level(self):
        out = EWMAModel(alpha=0.5).forecast([0, 10], 2)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], out[1])

    def test_linear_trend_extrapolates_line_exactly(self):
        out = LinearTrendModel().forecast([1, 2, 3, 4, 5], 3)
        self.assertAlmostEqual(out[0], 6.0, places=6)
        self.assertAlmostEqual(out[1], 7.0, places=6)
        self.assertAlmostEqual(out[2], 8.0, places=6)

    def test_ridge_recovers_linear_series(self):
        series = [float(i) for i in range(1, 21)]
        out = RidgeLinearModel(lags=(1, 2)).forecast(series, 3)
        self.assertAlmostEqual(out[0], 21.0, places=3)
        self.assertAlmostEqual(out[2], 23.0, places=3)

    def test_empty_series_is_safe(self):
        for model in (NaiveModel(), MovingAverageModel(), RidgeLinearModel()):
            out = model.forecast([], 2)
            self.assertEqual(len(out), 2)
            self.assertTrue(all(v == 0.0 for v in out))

    def test_backtest_walk_forward(self):
        series = [float(i) for i in range(1, 30)]
        result = backtest(LinearTrendModel(), series, horizon=1)
        self.assertGreater(result["fold_size"], 0)
        self.assertLess(result["rmse"], 1e-6)

    def test_select_best_prefers_linear_on_linear_data(self):
        series = [float(i * 2) for i in range(1, 30)]
        model, board = select_best(series, horizon=1)
        self.assertEqual(model.name, "linear_trend")
        self.assertEqual(board["winner"], "linear_trend")


class TestAnomaly(unittest.TestCase):
    def test_spike_detected(self):
        series = [1.0] * 12 + [99.0]
        found = detect_anomalies(series, window=10, z=2.0)
        self.assertTrue(found)
        self.assertEqual(found[-1]["direction"], "spike")


class TestRegistryAndEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_model_registry_records_and_selects(self):
        reg = ModelRegistry(self.root / "models.json")
        reg.record("revenue", "naive", {"rmse": 5.0})
        reg.record("revenue", "holt", {"rmse": 2.0})
        self.assertEqual(reg.best_for("revenue"), "holt")
        reloaded = ModelRegistry(self.root / "models.json")
        self.assertEqual(reloaded.best_for("revenue"), "holt")

    def test_engine_forecasts_inline_series(self):
        engine = PredictiveEngine()
        result = engine.forecast([1, 2, 3, 4, 5], horizon=2, metric="inline")
        self.assertEqual(len(result["predictions"]), 2)
        self.assertIn(result["model"], ("linear_trend", "ridge_linear", "holt"))

    def test_engine_series_from_ledger(self):
        ledger = Ledger(self.root / "logs.jsonl")
        ledger.append("complete", {"result": {"revenue_msat": 100}})
        ledger.append("complete", {"result": {"revenue_msat": 200}})
        ledger.append("skill_run", {"skill": "s", "reward": 0.9})
        engine = PredictiveEngine(ledger=ledger)
        self.assertEqual(engine.revenue_series(), [100.0, 200.0])
        self.assertEqual(engine.reward_series("s"), [0.9])

    def test_engine_forecast_with_data_writes_registry(self):
        ledger = Ledger(self.root / "logs.jsonl")
        for i in range(1, 12):
            ledger.append("skill_run", {"skill": "s", "reward": i / 12.0})
        reg = ModelRegistry(self.root / "models.json")
        engine = PredictiveEngine(ledger=ledger, registry=reg)
        result = engine.forecast(engine.reward_series("s"), horizon=3, metric="reward")
        self.assertEqual(reg.best_for("reward"), result["model"])


if __name__ == "__main__":
    unittest.main()
