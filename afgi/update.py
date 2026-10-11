"""增量更新：每个交易日收盘后只补齐缺失的行情，而不是全量重下。

为什么不能只下载增量片段然后直接拼接
------------------------------------
``adjust_bars`` 的前复权因子是以「传入窗口内最后一根 K 线」为基准 1.0 往历史
递推的。下载 800 根与下载 10 根算出的因子绝对值不同，直接把新片段的前复权价
拼到旧序列上会出现价格跳变，涨跌停判断随之失真。

因此缓存里保存的是 **未复权的原始 K 线 + 除权除息记录**；每次增量更新后，
用合并后的完整窗口统一重算一遍前复权，保证整条序列基准一致。

网络开销对比（全 A 约 5200 只）
------------------------------
- 全量重下：每只 800 根  -> 约 420 万根 K 线，约 4 分钟
- 增量更新：每只约 10 根 -> 约 5 万根 K 线，约 40 秒（主要是重拉除权记录）
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .adjust import adjust_bars
from .config import BAR_COUNT, FUND_CODES, INDEX_CODES
from .tdx import TdxPool, fetch_index_bars, fetch_security_bars, fetch_xdxr
from .universe import load_a_share_universe, load_industry_universe

CACHE_VERSION = 2

# 增量下载时多取几根，覆盖「上次运行时当日 K 线尚未定型」的情况
TAIL_BUFFER = 5
# 除权除息记录：体积小但调用次数多，每次更新都重拉以保证前复权正确
REFRESH_XDXR = True

Progress = Optional[callable]


def _log(msg: str) -> None:
    print(f"[AFGI] {msg}", flush=True)


def _emit(progress: Progress, done: int, total: int, msg: str) -> None:
    if progress is not None:
        progress(done, total, msg)


# ---------------------------------------------------------------------------
# 交易日历与下载计划
# ---------------------------------------------------------------------------
def fetch_market_calendar(pool: TdxPool, count: int = BAR_COUNT) -> Optional[pd.DatetimeIndex]:
    """用上证指数日 K 充当交易日历（判断最新交易日与缺失天数）。"""
    frame = fetch_index_bars(pool, 1, "000001", count)
    if frame.empty:
        return None
    return pd.DatetimeIndex(pd.to_datetime(frame["date"]))


def _last_date(frame: Optional[pd.DataFrame]) -> Optional[pd.Timestamp]:
    if frame is None or frame.empty or "date" not in frame.columns:
        return None
    return pd.to_datetime(frame["date"]).max()


def plan_need(cached_last: Optional[pd.Timestamp], calendar: pd.DatetimeIndex, count: int) -> int:
    """根据缓存最后日期与市场日历，计算需要补下载的根数。"""
    if cached_last is None or calendar is None or len(calendar) == 0:
        return count
    missing = int((calendar > cached_last).sum())
    if missing <= 0:
        return 0
    return int(min(count, missing + TAIL_BUFFER))


def _merge_bars(old: Optional[pd.DataFrame], new: Optional[pd.DataFrame], count: int):
    """按日期合并两段 K 线，新数据覆盖旧数据，并裁剪到窗口长度。"""
    if old is None or old.empty:
        return new
    if new is None or new.empty:
        return old
    df = pd.concat([old, new], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = (
        df.dropna(subset=["date"])
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )
    return df.tail(count).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 全量构建（首次运行）
# ---------------------------------------------------------------------------
def full_build(
    pool: TdxPool,
    count: int = BAR_COUNT,
    progress: Progress = None,
    limit: Optional[int] = None,
) -> Dict:
    """下载全 A 日 K + 除权记录，返回 v2 缓存结构（未复权）。"""
    universe = load_a_share_universe(pool)
    if limit:
        universe = universe[:limit]
    _log(f"A 股股票池 {len(universe)} 只")

    t0 = time.time()
    total = len(universe)

    def job(item):
        market, code, name = item
        bars = fetch_security_bars(pool, market, code, count)
        if bars.empty:
            return None
        return code, {
            "market": market,
            "name": name,
            "bars": bars,
            "xdxr": fetch_xdxr(pool, market, code),
        }

    from concurrent.futures import ThreadPoolExecutor

    stock_raw: Dict[str, Dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for idx, result in enumerate(ex.map(job, universe), 1):
            if result is not None:
                code, payload = result
                stock_raw[code] = payload
            if idx % 500 == 0:
                _emit(progress, idx, total, f"已下载 {idx}/{total} 只标的日K")
    _emit(progress, total, total, "日K下载完成")
    _log(f"日K下载完成 可用 {len(stock_raw)}/{total}，耗时 {time.time()-t0:.0f}s")

    index_frames = {
        key: fetch_index_bars(pool, market, code, count)
        for key, (market, code, _name) in INDEX_CODES.items()
    }
    fund_frames = {
        key: fetch_security_bars(pool, market, code, count)
        for key, (market, code, _name) in FUND_CODES.items()
    }
    industries, industry_desc = load_industry_universe(pool)
    _log(f"行业指数池: {industry_desc}")

    def industry_job(item):
        market, code, _name = item
        return code, fetch_index_bars(pool, market, code, count)

    industry_frames: Dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for code, bars in ex.map(industry_job, industries):
            if bars is not None and not bars.empty:
                industry_frames[code] = bars
    _log(f"行业指数日K 取到 {len(industry_frames)} 个")

    return {
        "version": CACHE_VERSION,
        "stock_raw": stock_raw,
        "index_frames": index_frames,
        "fund_frames": fund_frames,
        "industry_frames": industry_frames,
        "industry_desc": industry_desc,
        "universe_size": len(universe),
        "updated_at": time.time(),
    }


# ---------------------------------------------------------------------------
# 增量更新
# ---------------------------------------------------------------------------
def incremental_update(
    payload: Dict,
    pool: TdxPool,
    count: int = BAR_COUNT,
    progress: Progress = None,
    limit: Optional[int] = None,
) -> Tuple[Dict, Dict]:
    """在已有缓存基础上补齐到最新交易日。

    返回 (新 payload, 统计信息)。统计信息用于判断是否真的产生了新数据。
    """
    stock_raw: Dict[str, Dict] = payload.get("stock_raw", {})
    if not stock_raw:
        raise ValueError("缓存结构不含 stock_raw，无法增量更新，请执行 --refresh 全量重建")

    calendar = fetch_market_calendar(pool, count)
    market_last = calendar.max() if calendar is not None and len(calendar) else None
    cached_last = max(
        (_last_date(item.get("bars")) for item in stock_raw.values()
         if _last_date(item.get("bars")) is not None),
        default=None,
    )
    _log(f"市场最新交易日 {market_last.date() if market_last is not None else '未知'}，"
         f"缓存最新 {cached_last.date() if cached_last is not None else '无'}")

    need = plan_need(cached_last, calendar, count)
    _log(f"计划每只标的补下载 {need} 根" if need else "缓存已是最新，无需补下载")

    # 1) 股票池：找出新上市标的
    universe = load_a_share_universe(pool)
    if limit:
        universe = universe[:limit]
    known = set(stock_raw)
    new_codes = [(m, c, n) for (m, c, n) in universe if c not in known]
    _log(f"当前股票池 {len(universe)} 只，其中新股 {len(new_codes)} 只")

    # 2) 存量标的：补下载增量
    from concurrent.futures import ThreadPoolExecutor

    todo = [(code, item) for code, item in stock_raw.items() if need > 0]
    refreshed = 0

    def update_job(pair):
        code, item = pair
        market = item["market"]
        bars = fetch_security_bars(pool, market, code, need)
        if bars is None or bars.empty:
            return code, None
        merged = _merge_bars(item.get("bars"), bars, count)
        xdxr = fetch_xdxr(pool, market, code) if REFRESH_XDXR else item.get("xdxr")
        return code, {"market": market, "name": item.get("name", ""), "bars": merged, "xdxr": xdxr}

    t0 = time.time()
    if todo:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for idx, (code, result) in enumerate(ex.map(update_job, todo), 1):
                if result is not None:
                    stock_raw[code] = result
                    refreshed += 1
                if idx % 500 == 0:
                    _emit(progress, idx, len(todo), f"已更新 {idx}/{len(todo)} 只标的")
        _log(f"存量标的更新 {refreshed}/{len(todo)}，耗时 {time.time()-t0:.0f}s")

    # 3) 新股：全量下载
    added = 0
    if new_codes:
        t1 = time.time()

        def new_job(item):
            market, code, name = item
            bars = fetch_security_bars(pool, market, code, count)
            if bars.empty:
                return None
            return code, {
                "market": market,
                "name": name,
                "bars": bars,
                "xdxr": fetch_xdxr(pool, market, code),
            }

        with ThreadPoolExecutor(max_workers=8) as ex:
            for result in ex.map(new_job, new_codes):
                if result is not None:
                    code, item = result
                    stock_raw[code] = item
                    added += 1
        _log(f"新股入库 {added}/{len(new_codes)}，耗时 {time.time()-t1:.0f}s")

    # 4) 指数 / ETF / 行业：各自按【自身序列】的最新日期独立判定补下载。
    #    历史 bug：此前一律用「股票缓存的最新日期」判新鲜度（need）——
    #    通达信的指数/ETF 日 K 常滞后到 T+1 才落地，一旦某天没取到，
    #    need 归零后再也不会重试，缺口永久存在且越拉越大。现在每类单独算缺口。
    lag_report = []

    index_frames = payload.get("index_frames", {})
    for key, (market, code, _name) in INDEX_CODES.items():
        old = index_frames.get(key)
        sub = plan_need(_last_date(old), calendar, count)
        if old is None or old.empty:
            index_frames[key] = fetch_index_bars(pool, market, code, count)
            continue
        if sub:
            bars = fetch_index_bars(pool, market, code, sub)
            if bars is not None and not bars.empty:
                merged = _merge_bars(old, bars, count)
                moved = _last_date(merged) != _last_date(old)
                index_frames[key] = merged
                lag_report.append(f"{key}{'→' + str(_last_date(merged).date()) if moved else '仍滞后'}")

    fund_frames = payload.get("fund_frames", {})
    for key, (market, code, _name) in FUND_CODES.items():
        old = fund_frames.get(key)
        sub = plan_need(_last_date(old), calendar, count)
        if old is None or old.empty:
            fund_frames[key] = fetch_security_bars(pool, market, code, count)
            continue
        if sub:
            bars = fetch_security_bars(pool, market, code, sub)
            if bars is not None and not bars.empty:
                merged = _merge_bars(old, bars, count)
                moved = _last_date(merged) != _last_date(old)
                fund_frames[key] = merged
                lag_report.append(f"{key}{'→' + str(_last_date(merged).date()) if moved else '仍滞后'}")

    if lag_report:
        _log(f"指数/ETF 滞后补下载：{', '.join(lag_report)}")

    industry_frames = payload.get("industry_frames", {})
    missing_industry = [c for c, f in industry_frames.items() if f is None or f.empty]
    industry_lag = max(
        (plan_need(_last_date(f), calendar, count)
         for f in industry_frames.values() if isinstance(f, pd.DataFrame) and not f.empty),
        default=0,
    )
    if missing_industry or industry_lag:
        def ind_job(code):
            return code, fetch_index_bars(pool, 1, code, max(industry_lag, 3))
        with ThreadPoolExecutor(max_workers=8) as ex:
            for code, bars in ex.map(ind_job, list(industry_frames)):
                if bars is not None and not bars.empty:
                    industry_frames[code] = _merge_bars(industry_frames.get(code), bars, count)

    payload.update({
        "version": CACHE_VERSION,
        "stock_raw": stock_raw,
        "index_frames": index_frames,
        "fund_frames": fund_frames,
        "industry_frames": industry_frames,
        "universe_size": len(universe),
        "updated_at": time.time(),
    })

    stats = {
        "market_last": str(market_last.date()) if market_last is not None else None,
        "cached_last": str(cached_last.date()) if cached_last is not None else None,
        "need": need,
        "refreshed": refreshed,
        "new_listings": added,
        "universe_size": len(universe),
        "elapsed": round(time.time() - t0, 1),
    }
    return payload, stats


# ---------------------------------------------------------------------------
# 统一前复权
# ---------------------------------------------------------------------------
def build_adjusted_frames(stock_raw: Dict[str, Dict]) -> List[pd.DataFrame]:
    """用完整窗口统一重算前复权，产出下游指标计算所需的 frame 列表。"""
    keep = ["date", "code", "market", "open_adj", "high_adj", "low_adj", "close_adj", "amount", "vol"]
    frames: List[pd.DataFrame] = []
    for code, item in stock_raw.items():
        bars = item.get("bars")
        if bars is None or bars.empty:
            continue
        frame = adjust_bars(bars, item.get("xdxr"))
        frame["code"] = code
        frame["market"] = item.get("market")
        frame = frame[keep].copy()
        for col in ("open_adj", "high_adj", "low_adj", "close_adj"):
            frame[col] = frame[col].astype("float32")
        frame["amount"] = frame["amount"].astype("float64")
        frames.append(frame)
    return frames
