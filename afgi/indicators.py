"""A 股底层原始指标的时序化计算。

全部指标按交易日输出一条序列，公式与原仓库
market_fear_greed/enhanced_index.py::_build_stock_raw_rows_fast 保持一致，
区别只是数据层换成了通达信，并且用前复权价格替代了未复权价格。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .config import (
    LIMIT_THRESHOLD_BJ,
    LIMIT_THRESHOLD_GROWTH,
    LIMIT_THRESHOLD_MAIN,
)

GROWTH_PREFIX = ("300", "301", "688", "689")
BJ_PREFIX = ("8", "4")


def limit_threshold(codes: pd.Series, markets: Optional[pd.Series] = None) -> pd.Series:
    """按板块给出涨跌停阈值。

    主板 10%（用 9.5% 吸收四舍五入误差），创业板/科创板 20%，北交所 30%。
    """
    code = codes.fillna("").astype(str)
    threshold = pd.Series(LIMIT_THRESHOLD_MAIN, index=code.index, dtype="float64")
    threshold.loc[code.str.startswith(GROWTH_PREFIX)] = LIMIT_THRESHOLD_GROWTH
    bj_mask = code.str.startswith(BJ_PREFIX)
    if markets is not None:
        bj_mask = bj_mask | (pd.to_numeric(markets, errors="coerce") == 2)
    threshold.loc[bj_mask] = LIMIT_THRESHOLD_BJ
    return threshold


def _ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = pd.to_numeric(denominator, errors="coerce").replace(0, np.nan)
    return (pd.to_numeric(numerator, errors="coerce") / denominator).replace([np.inf, -np.inf], np.nan)


def build_stock_panel(frames: List[pd.DataFrame]) -> pd.DataFrame:
    """把每只股票的复权日 K 拼成同一张长表，并预计算收益率与涨跌停标记。"""
    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True)
    if panel.empty:
        return panel
    for col in ("close_adj", "high_adj", "low_adj", "amount", "vol"):
        if col in panel.columns:
            panel[col] = pd.to_numeric(panel[col], errors="coerce")
    panel = panel.loc[panel["close_adj"].notna() & (panel["close_adj"] > 0)].copy()
    panel = panel.sort_values(["code", "date"]).reset_index(drop=True)

    # 前收盘 = 同标的上一交易日的复权收盘
    panel["pre_close"] = panel.groupby("code", sort=False)["close_adj"].shift(1)
    panel["ret"] = panel["close_adj"] / panel["pre_close"] - 1.0
    panel["limit_threshold"] = limit_threshold(panel["code"], panel.get("market"))
    panel["is_limit_up"] = panel["ret"] >= panel["limit_threshold"]
    panel["is_limit_down"] = panel["ret"] <= -panel["limit_threshold"]
    panel["touched_limit_up"] = (panel["high_adj"] / panel["pre_close"] - 1.0) >= panel["limit_threshold"]
    panel["opened"] = panel["touched_limit_up"] & ~panel["is_limit_up"]
    return panel


def calc_stock_indicators(panel: pd.DataFrame) -> pd.DataFrame:
    """由个股面板计算 8 个基于个股的原始指标（时序）。"""
    if panel.empty:
        return pd.DataFrame(columns=["date"])

    valid = panel.loc[panel["pre_close"].notna()].copy()
    grouped = valid.groupby("date", sort=True)
    count = grouped["code"].count()

    out = pd.DataFrame(index=count.index)
    out.index.name = "date"
    out["valid_stock_count"] = count
    out["up_ratio"] = _ratio(grouped["ret"].apply(lambda s: (s > 0).sum()), count)

    limit_up = grouped["is_limit_up"].sum()
    limit_down = grouped["is_limit_down"].sum()
    out["limit_up_count"] = limit_up
    out["limit_down_count"] = limit_down
    out["limit_up_down_ratio"] = limit_up / limit_down.clip(lower=1)

    touched = grouped["touched_limit_up"].sum()
    opened = grouped["opened"].sum()
    out["open_board_rate"] = _ratio(opened, touched).fillna(0.0)

    # 收盘价矩阵：滚动均线 / 5 日收益 / 60 日新低 / 252 日新高
    close_matrix = (
        valid.pivot_table(index="date", columns="code", values="close_adj", aggfunc="last")
        .sort_index()
    )
    ma20 = close_matrix.rolling(20, min_periods=20).mean()
    ma60 = close_matrix.rolling(60, min_periods=60).mean()
    ret5 = close_matrix / close_matrix.shift(5) - 1.0
    low60 = close_matrix.rolling(60, min_periods=60).min()
    high252 = close_matrix.rolling(252, min_periods=120).max()

    out["ma20_ratio"] = _ratio((close_matrix > ma20).sum(axis=1), ma20.notna().sum(axis=1))
    out["ma60_ratio"] = _ratio((close_matrix > ma60).sum(axis=1), ma60.notna().sum(axis=1))
    out["up_5d_ratio"] = _ratio((ret5 > 0).sum(axis=1), ret5.notna().sum(axis=1))
    out["new_low_60d_ratio"] = _ratio((close_matrix <= low60).sum(axis=1), low60.notna().sum(axis=1))
    out["raw_new_high_ratio"] = _ratio((close_matrix >= high252).sum(axis=1), high252.notna().sum(axis=1))

    # 昨日涨停股 -> 今日平均收益
    limit_sets = {
        date: set(group.loc[group["is_limit_up"], "code"].astype(str))
        for date, group in valid.groupby("date", sort=True)
    }
    today_returns = {
        date: group.set_index("code")["ret"]
        for date, group in valid.groupby("date", sort=True)
    }
    dates = list(out.index)
    values = []
    for pos, date in enumerate(dates):
        prev = dates[pos - 1] if pos > 0 else None
        codes = limit_sets.get(prev, set()) if prev is not None else set()
        series = today_returns.get(date)
        if not codes or series is None or series.empty:
            values.append(np.nan)
            continue
        selected = series.loc[series.index.astype(str).isin(codes)].dropna()
        values.append(float(selected.mean()) if len(selected) else np.nan)
    out["yesterday_limit_up_return"] = values

    # 全市场成交额（元）与成交活跃度
    amount_by_date = panel.groupby("date", sort=True)["amount"].sum()
    out["total_amount"] = amount_by_date
    out["raw_amount_ratio"] = amount_by_date / amount_by_date.rolling(20, min_periods=5).mean()

    return out.reset_index()


def calc_industry_up_ratio(industry_frames: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """行业上涨占比：上涨行业数 / 有效行业数。

    industry_frames: {行业名称: 日K DataFrame}
    """
    if not industry_frames:
        return pd.DataFrame(columns=["date", "industry_up_ratio", "industry_up_count"])
    series_list = []
    for name, frame in industry_frames.items():
        if frame is None or frame.empty:
            continue
        work = frame[["date", "close"]].copy()
        work["close"] = pd.to_numeric(work["close"], errors="coerce")
        work = work.dropna().sort_values("date").drop_duplicates(subset=["date"], keep="last")
        work["ret"] = work["close"].pct_change()
        series_list.append(work.set_index("date")["ret"].rename(name))
    if not series_list:
        return pd.DataFrame(columns=["date", "industry_up_ratio", "industry_up_count"])
    matrix = pd.concat(series_list, axis=1).sort_index()
    up = (matrix > 0).sum(axis=1)
    valid = matrix.notna().sum(axis=1)
    out = pd.DataFrame({"industry_up_ratio": _ratio(up, valid), "industry_up_count": valid})
    out.index.name = "date"
    return out.reset_index()


def calc_small_large_relative_strength(
    hs300: pd.DataFrame, zz1000: pd.DataFrame, window: int = 5
) -> pd.DataFrame:
    """小盘/大盘相对强弱：中证1000 N 日收益 - 沪深300 N 日收益。"""
    if hs300 is None or hs300.empty or zz1000 is None or zz1000.empty:
        return pd.DataFrame(columns=["date", "small_large_relative_strength"])

    def close_series(frame: pd.DataFrame, name: str) -> pd.Series:
        work = frame[["date", "close"]].copy()
        work["close"] = pd.to_numeric(work["close"], errors="coerce")
        return (
            work.dropna().sort_values("date").drop_duplicates(subset=["date"], keep="last")
            .set_index("date")["close"].rename(name)
        )

    joined = pd.concat([close_series(hs300, "hs300"), close_series(zz1000, "zz1000")], axis=1).dropna()
    if joined.empty:
        return pd.DataFrame(columns=["date", "small_large_relative_strength"])
    small_ret = joined["zz1000"] / joined["zz1000"].shift(window) - 1.0
    large_ret = joined["hs300"] / joined["hs300"].shift(window) - 1.0
    out = pd.DataFrame({"small_large_relative_strength": small_ret - large_ret})
    out.index.name = "date"
    return out.reset_index()


def calc_market_volatility(etf50: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """市场波动率：50ETF 20 日年化历史波动率。"""
    if etf50 is None or etf50.empty:
        return pd.DataFrame(columns=["date", "raw_market_volatility"])
    work = etf50[["date", "close"]].copy()
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work = (
        work.dropna().sort_values("date").drop_duplicates(subset=["date"], keep="last")
        .set_index("date")
    )
    vol = work["close"].pct_change().rolling(window, min_periods=window).std() * math.sqrt(252)
    out = pd.DataFrame({"raw_market_volatility": vol})
    out.index.name = "date"
    return out.reset_index()


def calc_risk_appetite(hs300: pd.DataFrame, bond_etf: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """风险偏好：沪深300 20 日收益 - 国债ETF 20 日收益。"""
    if hs300 is None or hs300.empty or bond_etf is None or bond_etf.empty:
        return pd.DataFrame(columns=["date", "raw_risk_appetite"])

    def close_series(frame: pd.DataFrame, name: str) -> pd.Series:
        work = frame[["date", "close"]].copy()
        work["close"] = pd.to_numeric(work["close"], errors="coerce")
        return (
            work.dropna().sort_values("date").drop_duplicates(subset=["date"], keep="last")
            .set_index("date")["close"].rename(name)
        )

    joined = pd.concat([close_series(hs300, "stock"), close_series(bond_etf, "bond")], axis=1)
    joined["bond"] = joined["bond"].ffill()
    joined = joined.dropna()
    if joined.empty:
        return pd.DataFrame(columns=["date", "raw_risk_appetite"])
    stock_ret = joined["stock"].pct_change(window)
    bond_ret = joined["bond"].pct_change(window)
    out = pd.DataFrame({"raw_risk_appetite": stock_ret - bond_ret})
    out.index.name = "date"
    return out.reset_index()
