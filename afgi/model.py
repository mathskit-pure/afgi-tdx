"""九分项合成 -> A 股恐惧贪婪指数（AFGI）。

聚合逻辑与原仓库 enhanced_index.py 完全一致：

* 4 个核心因子（波动率 / 成交 / 股价强度 / 风险偏好）来自市场层面序列，
  缺失时沿用最近一个有效交易日的分数；
* 5 个增强分组由 10 个 A 股底层指标合成，缺失时按 50 分中性值处理；
* 九分项按固定权重加权，总分裁剪到 0-100，再映射到五档状态。
"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from .config import (
    BREADTH_WEIGHTS,
    CORE_RAW_COLUMNS,
    LIMIT_WEIGHTS,
    PROFITABILITY_WEIGHTS,
    RAW_INDICATOR_COLUMNS,
    REVERSE_RAW,
    SENTIMENT_WEIGHTS,
)
from .scoring import apply_status_fallback, classify_state, rolling_percentile_score, safe_score


def score_core_factors(df: pd.DataFrame) -> pd.DataFrame:
    """4 个核心因子打分（含缺失前向填充）。"""
    out = df.copy()
    for name, raw_col in CORE_RAW_COLUMNS.items():
        if raw_col not in out.columns:
            out[f"{name}_score"] = 50.0
            continue
        reverse = raw_col in REVERSE_RAW
        score = rolling_percentile_score(out[raw_col], reverse=reverse)
        status = pd.Series("missing", index=out.index, dtype="object")
        status.loc[pd.to_numeric(out[raw_col], errors="coerce").notna()] = "ok"
        out[f"{name}_score"] = apply_status_fallback(score, status)
        out[f"{name}_status"] = status
    return out


def score_raw_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """10 个底层指标打分（缺失直接取 50）。"""
    out = df.copy()
    for raw_col in RAW_INDICATOR_COLUMNS:
        score_col = f"{raw_col}_score"
        if raw_col not in out.columns:
            out[score_col] = 50.0
            continue
        reverse = raw_col in REVERSE_RAW
        score = rolling_percentile_score(out[raw_col], reverse=reverse)
        status = pd.Series("missing", index=out.index, dtype="object")
        status.loc[pd.to_numeric(out[raw_col], errors="coerce").notna()] = "ok"
        out[score_col] = score.where(status.eq("ok"), 50.0).map(safe_score)
        out[f"{raw_col}_status"] = status
    return out


def _weighted(df: pd.DataFrame, weights: Dict[str, float]) -> pd.Series:
    """按权重合成一个分组得分（逐行裁剪到 0-100）。"""
    total = None
    for col, weight in weights.items():
        if f"{col}_score" not in df.columns:
            continue
        value = pd.to_numeric(df[f"{col}_score"], errors="coerce").map(safe_score) * weight
        total = value if total is None else total + value
    if total is None:
        return pd.Series(50.0, index=df.index)
    return total.map(safe_score)


def build_components(df: pd.DataFrame) -> pd.DataFrame:
    """计算九分项得分与 AFGI 总分。"""
    out = score_core_factors(df)
    out = score_raw_indicators(out)

    out["volatility_sentiment"] = out["volatility_score"]
    out["volume_sentiment"] = out["volume_score"]
    out["price_strength_sentiment"] = out["price_strength_score"]
    out["risk_appetite_sentiment"] = out["risk_appetite_score"]

    out["breadth_sentiment"] = _weighted(out, BREADTH_WEIGHTS)
    out["limit_sentiment"] = _weighted(out, LIMIT_WEIGHTS)
    out["profitability_sentiment"] = _weighted(out, PROFITABILITY_WEIGHTS)
    out["sector_sentiment"] = pd.to_numeric(out["industry_up_ratio_score"], errors="coerce").map(safe_score)
    out["style_risk_appetite"] = pd.to_numeric(
        out["small_large_relative_strength_score"], errors="coerce"
    ).map(safe_score)

    total = None
    for col, weight in SENTIMENT_WEIGHTS.items():
        value = pd.to_numeric(out[col], errors="coerce").map(safe_score) * weight
        total = value if total is None else total + value
    out["afgi"] = total.map(safe_score) if total is not None else 50.0

    out["afgi_state"] = out["afgi"].apply(classify_state)
    out["afgi_change"] = out["afgi"].diff().fillna(0.0)
    out["afgi_ma5"] = out["afgi"].rolling(5, min_periods=1).mean()
    out["afgi_ma20"] = out["afgi"].rolling(20, min_periods=1).mean()
    return out


def component_breakdown(latest: pd.Series) -> pd.DataFrame:
    """把最新一天的九分项整理成展示用表格。"""
    labels = {
        "volatility_sentiment": "波动率情绪",
        "volume_sentiment": "成交情绪",
        "price_strength_sentiment": "股价强度情绪",
        "risk_appetite_sentiment": "风险偏好情绪",
        "breadth_sentiment": "市场广度情绪",
        "limit_sentiment": "涨跌停情绪",
        "profitability_sentiment": "赚钱效应",
        "sector_sentiment": "板块扩散情绪",
        "style_risk_appetite": "风格风险偏好",
    }
    rows = []
    for col, weight in SENTIMENT_WEIGHTS.items():
        value = safe_score(latest.get(col, 50.0))
        rows.append(
            {
                "code": col,
                "name": labels.get(col, col),
                "weight": weight,
                "score": round(value, 2),
                "contribution": round(value * weight, 2),
                "state": classify_state(value),
            }
        )
    return pd.DataFrame(rows)


def latest_snapshot(df: pd.DataFrame) -> Dict[str, object]:
    """最新交易日的指数快照。"""
    if df.empty:
        return {}
    row = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else row
    return {
        "trade_date": str(row.get("trade_date", "")),
        "afgi": round(float(row.get("afgi", 50.0)), 2),
        "state": str(row.get("afgi_state", "--")),
        "change": round(float(row.get("afgi", 50.0)) - float(prev.get("afgi", 50.0)), 2),
        "ma5": round(float(row.get("afgi_ma5", np.nan)), 2) if pd.notna(row.get("afgi_ma5")) else None,
        "ma20": round(float(row.get("afgi_ma20", np.nan)), 2) if pd.notna(row.get("afgi_ma20")) else None,
        "components": component_breakdown(row).to_dict("records"),
    }
