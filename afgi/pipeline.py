"""端到端流水线：通达信取数 -> 前复权 -> 原始指标 -> 分位数打分 -> AFGI。

运行一次即可得到完整的情绪指数时间序列，并落盘到 data/ 与 output/。
"""
from __future__ import annotations

import math
import pickle
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .adjust import adjust_bars
from .config import BAR_COUNT, FUND_CODES, INDEX_CODES
from .indicators import (
    build_stock_panel,
    calc_industry_up_ratio,
    calc_market_volatility,
    calc_risk_appetite,
    calc_small_large_relative_strength,
    calc_stock_indicators,
)
from .model import build_components, latest_snapshot
from .tdx import TdxPool, fetch_index_bars, fetch_security_bars, fetch_xdxr
from .universe import load_a_share_universe, load_industry_universe

Progress = Optional[Callable[[int, int, str], None]]


def _log(msg: str) -> None:
    print(f"[AFGI] {msg}", flush=True)


def _emit(progress: Progress, done: int, total: int, msg: str) -> None:
    if progress is not None:
        progress(done, total, msg)


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def fetch_stock_frames(
    pool: TdxPool,
    universe,
    count: int = BAR_COUNT,
    progress: Progress = None,
) -> Tuple[List[pd.DataFrame], Dict[str, int]]:
    """并发拉取全 A 日 K 与除权除息，并做前复权。"""
    total = len(universe)
    done = [0]
    frames: List[pd.DataFrame] = []

    def job(item):
        market, code, name = item
        bars = fetch_security_bars(pool, market, code, count)
        if bars.empty:
            return None
        xdxr = fetch_xdxr(pool, market, code)
        frame = adjust_bars(bars, xdxr)
        frame["code"] = code
        frame["market"] = market
        frame["name"] = name
        return frame

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=pool._workers if hasattr(pool, "_workers") else 8) as ex:
        results = list(ex.map(job, universe))

    for frame in results:
        done[0] += 1
        if frame is not None and not frame.empty:
            keep = ["date", "code", "market", "open_adj", "high_adj", "low_adj", "close_adj", "amount", "vol"]
            frame = frame[keep].copy()
            for col in ("open_adj", "high_adj", "low_adj", "close_adj"):
                frame[col] = frame[col].astype("float32")
            frame["amount"] = frame["amount"].astype("float64")
            frames.append(frame)
        if done[0] % 500 == 0:
            _emit(progress, done[0], total, f"已下载 {done[0]}/{total} 只标的日K")
    _emit(progress, total, total, "日K下载完成")
    return frames, {"requested": total, "usable": len(frames)}


def fetch_index_frames(pool: TdxPool, count: int = BAR_COUNT) -> Dict[str, pd.DataFrame]:
    frames = {}
    for key, (market, code, name) in INDEX_CODES.items():
        frames[key] = fetch_index_bars(pool, market, code, count)
        _log(f"指数 {name}({code}) {len(frames[key])} 根")
    return frames


def fetch_fund_frames(pool: TdxPool, count: int = BAR_COUNT) -> Dict[str, pd.DataFrame]:
    frames = {}
    for key, (market, code, name) in FUND_CODES.items():
        frames[key] = fetch_security_bars(pool, market, code, count)
        _log(f"基金 {name}({code}) {len(frames[key])} 根")
    return frames


def fetch_industry_frames(pool: TdxPool, count: int = BAR_COUNT) -> Tuple[Dict[str, pd.DataFrame], str]:
    """行业指数日 K。行业名称可能重复，因此统一用代码做主键。"""
    industries, desc = load_industry_universe(pool)
    _log(f"行业指数池: {desc}")
    from concurrent.futures import ThreadPoolExecutor

    def job(item):
        market, code, name = item
        bars = fetch_index_bars(pool, market, code, count)
        return (code, name), bars

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(job, industries))
    frames = {code: bars for (code, _), bars in results if bars is not None and not bars.empty}
    _log(f"行业指数日K 取到 {len(frames)} 个")
    return frames, desc


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def assemble(
    stock_frames: List[pd.DataFrame],
    index_frames: Dict[str, pd.DataFrame],
    fund_frames: Dict[str, pd.DataFrame],
    industry_frames: Dict[str, pd.DataFrame],
    progress: Progress = None,
) -> pd.DataFrame:
    _emit(progress, 1, 6, "计算个股原始指标")
    panel = build_stock_panel(stock_frames)
    base = calc_stock_indicators(panel)
    del panel
    if base.empty:
        return base

    _emit(progress, 2, 6, "计算行业上涨占比")
    industry = calc_industry_up_ratio(industry_frames)

    _emit(progress, 3, 6, "计算风格相对强弱")
    style = calc_small_large_relative_strength(index_frames.get("hs300"), index_frames.get("zz1000"))

    _emit(progress, 4, 6, "计算市场波动率")
    vol = calc_market_volatility(fund_frames.get("etf50"))

    _emit(progress, 5, 6, "计算风险偏好")
    risk = calc_risk_appetite(index_frames.get("hs300"), fund_frames.get("bond_etf"))

    _emit(progress, 6, 6, "合成情绪指数")
    df = (
        base.merge(industry, on="date", how="left")
        .merge(style, on="date", how="left")
        .merge(vol, on="date", how="left")
        .merge(risk, on="date", how="left")
        .sort_values("date")
        .reset_index(drop=True)
    )
    df["trade_date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
    df.attrs["index_closes"] = {
        key: frame[["date", "close"]].rename(columns={"close": f"{key}_close"})
        for key, frame in index_frames.items()
        if frame is not None and not frame.empty
    }
    return df


def finalize(df: pd.DataFrame) -> pd.DataFrame:
    """打分 + 九分项合成 + 附加指数收盘价。"""
    out = build_components(df)
    for key, frame in df.attrs.get("index_closes", {}).items():
        frame = frame.copy()
        frame["date"] = pd.to_datetime(frame["date"])
        out = out.merge(frame, on="date", how="left")
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _load_cache(cache_path: Path) -> Optional[Dict]:
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, "rb") as fh:
            payload = pickle.load(fh)
        _log(f"命中本地缓存 {cache_path}")
        return payload
    except Exception as exc:
        _log(f"缓存读取失败，将重新下载: {exc}")
        return None


def _save_cache(cache_path: Path, payload: Dict) -> None:
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(payload, fh)
    tmp.replace(cache_path)
    _log(f"原始数据已缓存到 {cache_path}")


def run(
    data_dir: Path,
    output_dir: Path,
    count: int = BAR_COUNT,
    use_cache: bool = True,
    history_days: Optional[int] = None,
    progress: Progress = None,
    limit: Optional[int] = None,
    offline: bool = False,
    update: bool = True,
) -> pd.DataFrame:
    """端到端运行。

    - ``update=True``（默认）：有缓存时只补齐缺失交易日，再重算全部指标；
    - ``offline=True``：完全不联网，直接用缓存重算（用于快速重出报告）；
    - ``use_cache=False`` / ``--refresh``：忽略缓存，全量重建。
    """
    from .update import (
        CACHE_VERSION,
        build_adjusted_frames,
        full_build,
        incremental_update,
    )

    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    cache_path = data_dir / f"raw_frames{'_limited' if limit else ''}.pkl"
    payload = _load_cache(cache_path) if use_cache else None

    pool = TdxPool()
    stats: Dict[str, object] = {}

    if payload is None:
        payload = full_build(pool, count=count, progress=progress, limit=limit)
        _save_cache(cache_path, payload)
    elif update and not offline:
        # 旧版缓存（v1）只保存了已复权的 frame，无法安全增量，需全量重建
        if payload.get("version") != CACHE_VERSION or not payload.get("stock_raw"):
            _log("旧版缓存不含未复权数据，无法增量更新，改为全量重建")
            payload = full_build(pool, count=count, progress=progress, limit=limit)
        else:
            payload, stats = incremental_update(
                payload, pool, count=count, progress=progress, limit=limit
            )
        _save_cache(cache_path, payload)
    else:
        _log(f"股票池 {payload.get('universe_size', '?')} 只（来自缓存，不联网）")

    if "stock_raw" in payload:
        stock_frames = build_adjusted_frames(payload["stock_raw"])
    else:  # v1 兼容：缓存里已经是复权后的 frame
        stock_frames = payload["stock_frames"]

    _log(f"参与计算的标的 {len(stock_frames)} 只")
    raw = assemble(
        stock_frames,
        payload["index_frames"],
        payload["fund_frames"],
        payload["industry_frames"],
        progress,
    )
    if raw.empty:
        raise RuntimeError("原始指标为空，请检查数据源")

    result = finalize(raw)
    result = result.loc[result["afgi"].notna()].reset_index(drop=True)
    if history_days:
        result = result.tail(history_days).reset_index(drop=True)

    result.to_csv(output_dir / "afgi_timeseries.csv", index=False, encoding="utf-8-sig")
    snapshot = latest_snapshot(result)
    import json

    with open(output_dir / "afgi_latest.json", "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)
    _log(f"结果已写入 {output_dir}")
    result.attrs["industry_desc"] = payload.get("industry_desc", "")
    result.attrs["universe_size"] = payload.get("universe_size", 0)
    result.attrs["update_stats"] = stats
    return result
