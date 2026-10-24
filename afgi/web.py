"""HTTP 行情源（腾讯行情）：通达信协议不可用时的等价替代。

为什么需要这一层
----------------
2026-10 起，通达信公开行情站（117.34.114.x 等）出现「TCP 能连上、协议请求
无任何响应」的故障：``get_security_bars`` 一律返回 None，探测 16 台站点全部
不可用。此时整条 AFGI 流水线会卡在取数阶段。

本模块用腾讯行情 HTTP 接口提供与 ``tdx.py`` **完全相同的五个函数签名**，
上层 ``pipeline/update/universe`` 不需要知道数据来自哪里：

    fetch_security_bars(pool, market, code, count)   # 个股 / 场内基金
    fetch_index_bars(pool, market, code, count)      # 指数 / 行业板块
    fetch_xdxr(pool, market, code)                   # 除权除息（HTTP 源自带前复权）
    fetch_security_list(pool, market)                # 证券列表（HTTP 源不支持）
    fetch_board_list(pool)                           # 行业板块清单

与通达信口径的差异
------------------
* 价格：直接取 **前复权**（qfq）序列，因此 ``xdxr`` 返回空列表，
  ``adjust_bars`` 不再二次复权。整条 800 根窗口一起取，基准一致。
* 成交量：单位为「手」，与通达信一致。
* 成交额：接口给的是「万元」，这里统一乘 1e4 换算成「元」，与通达信一致。
* 行业：通达信是 881xxx 申万行业（一/二/三级共 368 个）；HTTP 源改用腾讯
  行业板块（申万一级 31 个），颗粒度更粗但口径一致（都是「上涨行业数 / 有效行业数」）。
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .config import (
    BOARD_MARKET,
    WEB_BOARD_LIST_URL,
    WEB_KLINE_URL,
    WEB_RETRIES,
    WEB_TIMEOUT,
    WEB_WORKERS,
)

BAR_COLUMNS = ["date", "open", "high", "low", "close", "vol", "amount"]

# 通达信 market -> 腾讯代码前缀
MARKET_PREFIX = {0: "sz", 1: "sh", 2: "bj"}

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://gu.qq.com/",
    "Accept": "*/*",
}


class WebPool:
    """与 TdxPool 同接口的 HTTP 取数池。

    上层只用到 ``_workers`` 与 ``stats``，因此这里不实现连接池——urllib
    每次请求独立建连，天然线程安全。
    """

    is_web = True

    def __init__(
        self,
        workers: int = WEB_WORKERS,
        timeout: int = WEB_TIMEOUT,
        retries: int = WEB_RETRIES,
    ) -> None:
        self.workers = workers
        self.timeout = timeout
        self.retries = retries
        self.stats = {"ok": 0, "fail": 0, "retry": 0}

    @property
    def _workers(self) -> int:
        return self.workers

    def close(self) -> None:  # 与 TdxPool 对齐，无实际资源
        return None


def _get(url: str, timeout: int, retries: int) -> Optional[str]:
    """带重试的 GET，全部失败返回 None。"""
    last: Optional[BaseException] = None
    for attempt in range(max(retries, 1)):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        except Exception as exc:  # 网络抖动、限流、502 都在这里重试
            last = exc
            time.sleep(0.3 * (attempt + 1))
    if last is not None:
        return None
    return None


def _symbol(market: int, code: str) -> str:
    """通达信 (market, code) -> 腾讯代码。行业板块直接用 ``pt`` 开头的原码。"""
    code = str(code)
    if int(market) == BOARD_MARKET or code.startswith("pt"):
        return code
    return MARKET_PREFIX.get(int(market), "sh") + code


def _rows_to_frame(rows: Optional[Sequence[Sequence[Any]]]) -> pd.DataFrame:
    """把腾讯 K 线数组转成统一 DataFrame。

    腾讯 newfqkline 每根 K 线：
        [日期, 开, 收, 高, 低, 成交量(手), {}, 换手率, 成交额(万元), ...]
    """
    if not rows:
        return pd.DataFrame(columns=BAR_COLUMNS)
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not row or len(row) < 6:
            continue
        try:
            close = float(row[2])
            vol = float(row[5])
        except (TypeError, ValueError):
            continue
        # 成交额：第 9 位（index 8）为万元；缺失时用 量×价 估算，保证口径非空
        amount: Optional[float] = None
        if len(row) > 8:
            try:
                amount = float(row[8]) * 1e4
            except (TypeError, ValueError):
                amount = None
        if amount is None or amount <= 0:
            amount = close * vol * 100.0
        out.append({
            "date": pd.to_datetime(row[0], errors="coerce"),
            "open": float(row[1]) if row[1] not in (None, "") else close,
            "high": float(row[3]) if row[3] not in (None, "") else close,
            "low": float(row[4]) if row[4] not in (None, "") else close,
            "close": close,
            "vol": vol,
            "amount": amount,
        })
    if not out:
        return pd.DataFrame(columns=BAR_COLUMNS)
    return (
        pd.DataFrame(out)
        .dropna(subset=["date"])
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )


def fetch_bars(pool: WebPool, market: int, code: str, count: int = 800) -> pd.DataFrame:
    """统一取日 K（个股 / 基金 / 指数 / 行业板块）。"""
    sym = _symbol(market, code)
    url = WEB_KLINE_URL.format(symbol=sym, count=int(count))
    raw = _get(url, getattr(pool, "timeout", WEB_TIMEOUT), getattr(pool, "retries", WEB_RETRIES))
    if raw is None:
        pool.stats["fail"] += 1
        return pd.DataFrame(columns=BAR_COLUMNS)
    try:
        payload = json.loads(raw)
    except ValueError:
        pool.stats["fail"] += 1
        return pd.DataFrame(columns=BAR_COLUMNS)
    data = payload.get("data") or {}
    node = data.get(sym) or {}
    rows = node.get("qfqday") or node.get("day") or []
    frame = _rows_to_frame(rows)
    pool.stats["ok" if not frame.empty else "fail"] += 1
    return frame


# ---------------------------------------------------------------------------
# 与 tdx.py 对齐的接口
# ---------------------------------------------------------------------------
def fetch_security_bars(pool: WebPool, market: int, code: str, count: int = 800) -> pd.DataFrame:
    """个股 / 场内基金日 K（已前复权）。"""
    return fetch_bars(pool, market, code, count)


def fetch_index_bars(pool: WebPool, market: int, code: str, count: int = 800) -> pd.DataFrame:
    """指数日 K；``pt`` 开头的行业板块走同一个接口。"""
    return fetch_bars(pool, market, code, count)


def fetch_xdxr(pool: WebPool, market: int, code: str) -> List[Dict[str, Any]]:
    """HTTP 源直接给前复权价，不需要额外除权记录。"""
    return []


def fetch_security_list(pool: WebPool, market: int) -> List[Dict[str, Any]]:
    """HTTP 源没有稳定的全市场证券列表接口，股票池改由本地 universe_web.csv 维护。"""
    return []


def fetch_board_list(pool: WebPool) -> List[Tuple[str, str]]:
    """申万一级行业板块（腾讯 ``board_type=hy``，31 个）。"""
    raw = _get(WEB_BOARD_LIST_URL, getattr(pool, "timeout", WEB_TIMEOUT),
               getattr(pool, "retries", WEB_RETRIES))
    if raw is None:
        return []
    try:
        payload = json.loads(raw)
    except ValueError:
        return []
    items = ((payload.get("data") or {}).get("rank_list") or [])
    out: List[Tuple[str, str]] = []
    for item in items:
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if code:
            out.append((code, name or code))
    return out
