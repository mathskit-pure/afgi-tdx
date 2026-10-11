"""0-100 分位数标准化。

移植自原仓库 market_fear_greed/scoring.py：
把原始指标放进自身过去 252 个交易日的分布中取分位数，
样本不足 60 个时使用 50 分中性值；reverse=True 用于恐惧方向指标。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import PERCENTILE_LOOKBACK, PERCENTILE_MIN_PERIODS


def clamp_score(value, neutral: float = 50.0) -> float:
    try:
        numeric = float(value)
    except Exception:
        return neutral
    if not np.isfinite(numeric):
        return neutral
    return float(np.clip(numeric, 0.0, 100.0))


def safe_score(value, neutral: float = 50.0) -> float:
    return clamp_score(value, neutral=neutral)


def _clean_history(series: pd.Series, lookback_window: int = PERCENTILE_LOOKBACK) -> pd.Series:
    clean = pd.to_numeric(pd.Series(series), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if lookback_window and lookback_window > 0:
        clean = clean.tail(lookback_window)
    return clean


def percentile_score(
    series: pd.Series,
    value: float,
    reverse: bool = False,
    min_periods: int = PERCENTILE_MIN_PERIODS,
    lookback_window: int = PERCENTILE_LOOKBACK,
) -> float:
    clean = _clean_history(series, lookback_window=lookback_window)
    if len(clean) < min_periods:
        return 50.0
    try:
        numeric_value = float(value)
    except Exception:
        return 50.0
    if not np.isfinite(numeric_value):
        return 50.0
    score = float((clean <= numeric_value).mean() * 100.0)
    if reverse:
        score = 100.0 - score
    return clamp_score(score)


def rolling_percentile_score(
    series: pd.Series,
    reverse: bool = False,
    min_periods: int = PERCENTILE_MIN_PERIODS,
    lookback_window: int = PERCENTILE_LOOKBACK,
) -> pd.Series:
    """逐日滚动分位数打分（向量化实现，语义与原仓库循环版一致）。"""
    numeric = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    values = numeric.to_numpy(dtype="float64")
    n = len(values)
    scores = np.full(n, np.nan, dtype="float64")

    window = lookback_window if lookback_window and lookback_window > 0 else n
    for idx in range(n):
        start = max(0, idx - window + 1)
        history = values[start : idx + 1]
        history = history[np.isfinite(history)]
        current = values[idx]
        if len(history) < min_periods or not np.isfinite(current):
            continue
        score = float((history <= current).mean() * 100.0)
        scores[idx] = 100.0 - score if reverse else score

    return pd.Series(np.clip(scores, 0.0, 100.0), index=series.index, dtype="float64")


def apply_status_fallback(score: pd.Series, status: pd.Series) -> pd.Series:
    """核心因子的缺失处理：沿用最近一个有效交易日的分数（原仓库 _apply_status_fallback）。

    原仓库对 4 个核心因子使用前向填充，对 5 个增强分组直接给 50 分。
    这里只实现核心因子的前向填充版本。
    """
    status_filled = status.fillna("missing").astype(str).str.lower()
    valid = status_filled.eq("ok") & pd.to_numeric(score, errors="coerce").notna()
    filled = pd.to_numeric(score, errors="coerce").where(valid).ffill()
    return filled.map(safe_score).fillna(50.0)


def classify_state(score: float) -> str:
    """五档情绪状态。"""
    try:
        value = float(score)
    except Exception:
        return "--"
    if not np.isfinite(value):
        return "--"
    if value < 20:
        return "极度恐惧"
    if value < 40:
        return "恐惧"
    if value < 60:
        return "中性"
    if value < 80:
        return "贪婪"
    return "极度贪婪"
