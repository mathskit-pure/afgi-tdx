"""通达信（TDX）行情数据层。

这是 Tushare 不可用时的替代数据源：走通达信公开行情服务器的 TCP 协议，
不需要 Token、不需要积分，实测可稳定返回历史日 K、指数日 K、除权除息与证券列表。

用法::

    pool = TdxPool()
    bars = pool.call(lambda api: api.get_security_bars(9, 1, "510050", 0, 800))
    results = pool.map(jobs, lambda j, api: api.get_security_bars(9, j[0], j[1], 0, 800))
"""
from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from pytdx.hq import TdxHq_API

from .config import TDX_HOSTS, TDX_RETRIES, TDX_TIMEOUT, TDX_WORKERS

# 日 K 线类别
KLINE_DAILY = 9

BAR_COLUMNS = ["date", "open", "high", "low", "close", "vol", "amount"]


class TdxPool:
    """线程安全的通达信连接池：每个线程持有一条长连接，失败自动换站重连。"""

    def __init__(
        self,
        hosts: Sequence[Tuple[str, int]] = TDX_HOSTS,
        timeout: int = TDX_TIMEOUT,
        retries: int = TDX_RETRIES,
    ) -> None:
        self.hosts = list(hosts)
        self.timeout = timeout
        self.retries = retries
        self._cycle = itertools.cycle(self.hosts)
        self._lock = threading.Lock()
        self._local = threading.local()
        self.stats = {"ok": 0, "fail": 0, "retry": 0}

    # ------------------------------------------------------------------
    def _state(self) -> Dict[str, Any]:
        st = getattr(self._local, "state", None)
        if st is None:
            st = {"api": None, "host": None}
            self._local.state = st
        return st

    def _next_host(self) -> Tuple[str, int]:
        with self._lock:
            return next(self._cycle)

    def _connect(self, st: Dict[str, Any]) -> bool:
        host = self._next_host()
        api = TdxHq_API(heartbeat=False, auto_retry=False, raise_exception=False)
        try:
            api.connect(host[0], host[1], time_out=self.timeout)
            st["api"] = api
            st["host"] = host
            return True
        except Exception:
            st["api"] = None
            st["host"] = None
            return False

    def _drop(self, st: Dict[str, Any]) -> None:
        api = st.get("api")
        if api is not None:
            try:
                api.disconnect()
            except Exception:
                pass
        st["api"] = None
        st["host"] = None

    def call(self, fn: Callable[[TdxHq_API], Any], tries: Optional[int] = None) -> Any:
        """执行单次调用，失败换站重试，全部失败返回 None。"""
        tries = tries or self.retries
        st = self._state()
        for attempt in range(tries):
            if st["api"] is None and not self._connect(st):
                time.sleep(0.2 * (attempt + 1))
                continue
            try:
                result = fn(st["api"])
                self.stats["ok"] += 1
                return result
            except Exception:
                self.stats["retry"] += 1
                self._drop(st)
                time.sleep(0.2 * (attempt + 1))
        self.stats["fail"] += 1
        return None

    # ------------------------------------------------------------------
    def map(
        self,
        jobs: Sequence[Any],
        fn: Callable[[Any, TdxHq_API], Any],
        workers: int = TDX_WORKERS,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[Any]:
        """并发执行批量取数，保持输入顺序。"""
        from concurrent.futures import ThreadPoolExecutor

        results: List[Any] = [None] * len(jobs)
        done = [0]

        def runner(indexed: Tuple[int, Any]):
            idx, job = indexed
            value = self.call(lambda api: fn(job, api))
            results[idx] = value
            done[0] += 1
            if progress is not None:
                progress(done[0], len(jobs))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(runner, enumerate(jobs)))
        return results

    def close(self) -> None:
        self._drop(self._state())


# ---------------------------------------------------------------------------
# 单标的取数封装
# ---------------------------------------------------------------------------
def _bars_to_frame(bars: Optional[List[Dict[str, Any]]]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = pd.DataFrame(bars)
    # 通达信 datetime 带 15:00 的时间戳，统一归一到当日 00:00，避免合并键对不齐
    df["date"] = pd.to_datetime(df["datetime"], errors="coerce").dt.normalize()
    for col in ("open", "high", "low", "close", "vol", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return (
        df[BAR_COLUMNS]
        .dropna(subset=["date"])
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )


def fetch_security_bars(
    pool: TdxPool,
    market: int,
    code: str,
    count: int = 800,
) -> pd.DataFrame:
    """个股 / 场内基金日 K。"""
    bars = pool.call(lambda api: api.get_security_bars(KLINE_DAILY, market, code, 0, count))
    return _bars_to_frame(bars)


def fetch_index_bars(
    pool: TdxPool,
    market: int,
    code: str,
    count: int = 800,
) -> pd.DataFrame:
    """指数日 K（大盘指数与行业指数都走这个接口）。"""
    bars = pool.call(lambda api: api.get_index_bars(KLINE_DAILY, market, code, 0, count))
    return _bars_to_frame(bars)


def fetch_xdxr(pool: TdxPool, market: int, code: str) -> List[Dict[str, Any]]:
    """除权除息信息，用于自己做前复权。

    pytdx 返回字段：year/month/day/category/name，
    category==1（除权除息）时 fenhong/peigujia/songzhuangu/peigu 有效。
    """
    records = pool.call(lambda api: api.get_xdxr_info(market, code))
    if not records:
        return []
    return [dict(r) for r in records]


def fetch_security_list(pool: TdxPool, market: int, max_pages: int = 32) -> List[Dict[str, Any]]:
    """分页拉取某市场的证券列表。

    通达信列表接口偶发返回失败，因此需要逐页重试；空页不能直接判定为结束。
    """
    jobs = [(market, start) for start in range(0, max_pages * 1000, 1000)]
    pages = pool.map(
        jobs,
        lambda job, api: api.get_security_list(job[0], job[1]),
    )
    rows: List[Dict[str, Any]] = []
    for page in pages:
        if page:
            rows.extend(dict(r) for r in page)
    return rows


def probe_hosts(hosts: Sequence[Tuple[str, int]] = TDX_HOSTS) -> List[Tuple[str, int, float]]:
    """探测可返回日 K 的服务器，便于排障与更换站点。"""
    import socket

    usable: List[Tuple[str, int, float]] = []
    for host, port in hosts:
        start = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        try:
            sock.connect((host, port))
            api = TdxHq_API(heartbeat=False, auto_retry=False, raise_exception=False)
            try:
                api.connect(host, port, time_out=5)
                bars = api.get_security_bars(KLINE_DAILY, 0, "000001", 0, 3)
            except Exception:
                bars = None
            finally:
                try:
                    api.disconnect()
                except Exception:
                    pass
            if bars:
                usable.append((host, port, (time.time() - start) * 1000))
        except Exception:
            pass
        finally:
            sock.close()
    return usable
