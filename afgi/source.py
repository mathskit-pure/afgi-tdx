"""数据源选择：优先通达信，全部站点失效时自动切 HTTP 行情源。

2026-10 通达信公开行情站集体失效（TCP 可连、协议无响应），这里保留探测：
只要有任何一台站点能返回日 K 就继续用通达信（数据更细，行业指数覆盖更全），
否则回落到腾讯 HTTP 源，保证每日流水线不会因为单一数据源故障而中断。
"""
from __future__ import annotations

import os
from typing import Optional

from .config import DATA_SOURCE, TDX_HOSTS, TDX_PROBE_HOSTS, TDX_PROBE_TIMEOUT


def _tdx_alive(
    hosts=None,
    limit: int = TDX_PROBE_HOSTS,
    timeout: int = TDX_PROBE_TIMEOUT,
) -> bool:
    """快速探测前几台通达信站点是否真能返回日 K（线程/启动开销都很小）。"""
    import socket

    try:
        from pytdx.hq import TdxHq_API
    except ImportError:  # 依赖缺失视为站点不可用，直接走 HTTP 兜底
        return False

    for host, port in (hosts or TDX_HOSTS)[:limit]:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
        except OSError:
            continue
        api = TdxHq_API(heartbeat=False, auto_retry=False, raise_exception=False)
        try:
            api.connect(host, port, time_out=timeout + 2)
            bars = api.get_security_bars(9, 0, "000001", 0, 2)
        except Exception:
            bars = None
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
        if bars:
            return True
    return False


def make_pool(log=print):
    """按配置返回取数池对象（TdxPool 或 WebPool，接口一致）。"""
    mode = (os.environ.get("AFGI_SOURCE") or DATA_SOURCE or "auto").strip().lower()
    forced_tdx = mode == "tdx"

    if mode in ("auto", "tdx"):
        if _tdx_alive():
            from .tdx import TdxPool

            log("数据源：通达信行情站")
            return TdxPool()
        if forced_tdx:
            raise RuntimeError(
                "强制指定通达信数据源（AFGI_SOURCE=tdx），但探测站点全部不可用"
            )

    from .web import WebPool

    log("数据源：腾讯行情 HTTP（通达信站点不可用，自动切换）")
    return WebPool()
