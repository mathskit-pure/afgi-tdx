"""前复权调整。

通达信日 K 是未复权价格，直接用 close/prev_close 计算涨跌幅会在除权除息日
产生虚假的暴跌（例如 10 送 10 会表现为 -50%，被误判成跌停）。
这里用通达信自己的除权除息记录（category=1）做前复权，
把 OHLC 全部还原到同一价格基准上。

复权因子推导（每 10 股口径 -> 每股口径）：
    每股分红 d   = fenhong      / 10
    每股送转 s   = songzhuangu  / 10
    每股配股 p   = peigu        / 10
    配股价   pp  = peigujia

    除权日理论价 = (前收盘 - d + pp * p) / (1 + s + p)
    复权因子 k   = 除权日理论价 / 前收盘

除权日之前的所有价格乘以对应的累计 k，即得到前复权序列。
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd


def _extract_events(records: List[Dict]) -> List[tuple]:
    """从 pytdx 除权记录中取出有效的除权除息事件。"""
    events = []
    for rec in records or []:
        if rec.get("category") != 1:
            continue
        try:
            year = int(rec.get("year"))
            month = int(rec.get("month"))
            day = int(rec.get("day"))
            date = pd.Timestamp(year=year, month=month, day=day)
        except Exception:
            continue

        def num(key: str) -> float:
            try:
                value = float(rec.get(key) or 0.0)
            except Exception:
                return 0.0
            return value if np.isfinite(value) else 0.0

        fenhong = num("fenhong")
        songzhuangu = num("songzhuangu")
        peigu = num("peigu")
        peigujia = num("peigujia")
        if fenhong == 0 and songzhuangu == 0 and peigu == 0:
            continue
        events.append((date, fenhong, songzhuangu, peigu, peigujia))
    events.sort(key=lambda item: item[0])
    return events


def adjust_bars(bars: pd.DataFrame, records: List[Dict]) -> pd.DataFrame:
    """对单只标的的日 K 做前复权。

    bars 需包含 date/open/high/low/close/vol/amount。
    vol 与 amount 不做调整（成交额用于市场活跃度，应保持原始口径）。
    """
    if bars is None or bars.empty:
        return bars
    out = bars.copy().reset_index(drop=True)
    events = _extract_events(records)
    if not events:
        out["close_adj"] = out["close"]
        out["high_adj"] = out["high"]
        out["low_adj"] = out["low"]
        out["open_adj"] = out["open"]
        return out

    dates = pd.to_datetime(out["date"])
    close_raw = pd.to_numeric(out["close"], errors="coerce").to_numpy(dtype="float64")
    factor = np.ones(len(out), dtype="float64")

    for event_date, fenhong, songzhuangu, peigu, peigujia in events:
        pos = int(np.searchsorted(dates.to_numpy(), np.datetime64(event_date), side="left"))
        if pos == 0:
            continue
        prev_close = close_raw[pos - 1]
        if not np.isfinite(prev_close) or prev_close <= 0:
            continue
        d = fenhong / 10.0
        s = songzhuangu / 10.0
        p = peigu / 10.0
        theoretical = (prev_close - d + peigujia * p) / (1.0 + s + p)
        k = theoretical / prev_close
        if not np.isfinite(k) or k <= 0.01 or k >= 100:
            continue
        factor[:pos] *= k

    out["open_adj"] = pd.to_numeric(out["open"], errors="coerce").to_numpy(dtype="float64") * factor
    out["high_adj"] = pd.to_numeric(out["high"], errors="coerce").to_numpy(dtype="float64") * factor
    out["low_adj"] = pd.to_numeric(out["low"], errors="coerce").to_numpy(dtype="float64") * factor
    out["close_adj"] = close_raw * factor
    return out
