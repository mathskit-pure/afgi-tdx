"""核心逻辑单元测试：python -m unittest discover -s tests -p "test_*.py" """
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from afgi.adjust import adjust_bars  # noqa: E402
from afgi.config import SENTIMENT_WEIGHTS  # noqa: E402
from afgi.indicators import limit_threshold  # noqa: E402
from afgi.model import build_components  # noqa: E402
from afgi.scoring import classify_state, percentile_score, rolling_percentile_score  # noqa: E402


class TestScoring(unittest.TestCase):
    def test_percentile_monotonic(self):
        series = pd.Series(np.arange(100, dtype="float64"))
        self.assertLess(percentile_score(series, 10.0), percentile_score(series, 90.0))

    def test_percentile_reverse(self):
        series = pd.Series(np.arange(100, dtype="float64"))
        score = percentile_score(series, 90.0, reverse=True)
        self.assertAlmostEqual(score, 100.0 - percentile_score(series, 90.0))

    def test_insufficient_history_returns_neutral(self):
        series = pd.Series(np.arange(10, dtype="float64"))
        self.assertEqual(percentile_score(series, 5.0, min_periods=60), 50.0)

    def test_rolling_matches_pointwise(self):
        series = pd.Series(np.linspace(0, 100, 300))
        rolled = rolling_percentile_score(series)
        self.assertAlmostEqual(rolled.iloc[-1], percentile_score(series, series.iloc[-1]), places=6)

    def test_state_bands(self):
        self.assertEqual(classify_state(10), "极度恐惧")
        self.assertEqual(classify_state(30), "恐惧")
        self.assertEqual(classify_state(50), "中性")
        self.assertEqual(classify_state(70), "贪婪")
        self.assertEqual(classify_state(90), "极度贪婪")


class TestLimitThreshold(unittest.TestCase):
    def test_by_board(self):
        codes = pd.Series(["600000", "300750", "688981", "830799"])
        got = limit_threshold(codes).tolist()
        self.assertAlmostEqual(got[0], 0.095)
        self.assertAlmostEqual(got[1], 0.195)
        self.assertAlmostEqual(got[2], 0.195)
        self.assertAlmostEqual(got[3], 0.295)


class TestAdjustment(unittest.TestCase):
    def test_ten_send_ten_is_not_a_crash(self):
        """10 送 10 会让未复权价格腰斩，复权后日收益应接近 0。"""
        dates = pd.to_datetime(["2024-06-01", "2024-06-02"])
        bars = pd.DataFrame(
            {
                "date": dates,
                "open": [20.0, 10.0],
                "high": [20.0, 10.0],
                "low": [20.0, 10.0],
                "close": [20.0, 10.0],
                "vol": [100.0, 200.0],
                "amount": [2000.0, 2000.0],
            }
        )
        records = [
            {
                "year": 2024, "month": 6, "day": 2, "category": 1,
                "fenhong": 0.0, "songzhuangu": 10.0, "peigu": 0.0, "peigujia": 0.0,
            }
        ]
        out = adjust_bars(bars, records)
        ret = out["close_adj"].iloc[1] / out["close_adj"].iloc[0] - 1.0
        self.assertAlmostEqual(ret, 0.0, places=6)
        # 未复权口径下确实是 -50%
        self.assertAlmostEqual(bars["close"].iloc[1] / bars["close"].iloc[0] - 1.0, -0.5)

    def test_no_events_keeps_raw(self):
        dates = pd.to_datetime(["2024-06-01", "2024-06-02"])
        bars = pd.DataFrame(
            {"date": dates, "open": [1.0, 1.1], "high": [1.0, 1.1],
             "low": [1.0, 1.1], "close": [1.0, 1.1], "vol": [1, 1], "amount": [1, 1]}
        )
        out = adjust_bars(bars, [])
        self.assertTrue(np.allclose(out["close_adj"], bars["close"]))


class TestAggregation(unittest.TestCase):
    def _frame(self, n: int = 300) -> pd.DataFrame:
        rng = np.random.default_rng(7)
        return pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=n, freq="B"),
                "raw_market_volatility": 0.15 + rng.normal(0, 0.03, n).cumsum() * 0.001,
                "raw_amount_ratio": 1.0 + rng.normal(0, 0.1, n),
                "raw_new_high_ratio": np.abs(rng.normal(0.05, 0.02, n)),
                "raw_risk_appetite": rng.normal(0, 0.02, n),
                "up_ratio": np.clip(rng.normal(0.5, 0.15, n), 0, 1),
                "ma20_ratio": np.clip(rng.normal(0.5, 0.15, n), 0, 1),
                "ma60_ratio": np.clip(rng.normal(0.5, 0.15, n), 0, 1),
                "limit_up_down_ratio": np.abs(rng.normal(3, 1.5, n)),
                "open_board_rate": np.clip(rng.normal(0.3, 0.1, n), 0, 1),
                "yesterday_limit_up_return": rng.normal(0.01, 0.02, n),
                "up_5d_ratio": np.clip(rng.normal(0.5, 0.15, n), 0, 1),
                "new_low_60d_ratio": np.clip(rng.normal(0.1, 0.05, n), 0, 1),
                "industry_up_ratio": np.clip(rng.normal(0.5, 0.2, n), 0, 1),
                "small_large_relative_strength": rng.normal(0, 0.01, n),
            }
        )

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(SENTIMENT_WEIGHTS.values()), 1.0)

    def test_all_components_vary(self):
        out = build_components(self._frame()).tail(200)
        for col in SENTIMENT_WEIGHTS:
            self.assertGreater(out[col].std(), 0.0, f"{col} 恒为常数")

    def test_afgi_within_bounds(self):
        out = build_components(self._frame())
        self.assertTrue(out["afgi"].between(0, 100).all())
        self.assertGreater(out["afgi"].std(), 0.0)

    def test_missing_indicator_falls_back_to_neutral(self):
        frame = self._frame()
        frame["industry_up_ratio"] = np.nan
        out = build_components(frame)
        self.assertTrue((out["sector_sentiment"] == 50.0).all())


if __name__ == "__main__":
    unittest.main()
