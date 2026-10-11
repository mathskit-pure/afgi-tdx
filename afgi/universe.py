"""标的池构建：A 股股票池 + 申万行业指数池。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Tuple

from .config import (
    A_PREFIX_BJ,
    A_PREFIX_SH,
    A_PREFIX_SZ,
    INDUSTRY_CODE_RANGE,
    INDUSTRY_LEVEL1_ONLY,
    SW_L1_ALIASES,
    SW_L1_MIN_MATCH,
)
from .tdx import TdxPool, fetch_index_bars, fetch_security_list

Security = Tuple[int, str, str]  # (market, code, name)


def _dedup(rows: List[Dict]) -> List[Dict]:
    seen = set()
    out = []
    for row in rows:
        code = str(row.get("code", ""))
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(row)
    return out


def load_a_share_universe(pool: TdxPool) -> List[Security]:
    """从通达信证券列表筛出沪深京 A 股。"""
    universe: List[Security] = []
    for market, prefixes in ((0, A_PREFIX_SZ), (1, A_PREFIX_SH)):
        rows = _dedup(fetch_security_list(pool, market))
        for row in rows:
            code = str(row.get("code", ""))
            if code.startswith(prefixes):
                universe.append((market, code, str(row.get("name", "")).strip()))

    # 北交所：通达信 market=2，数量异常时跳过，避免混入非股票标的
    bj = _dedup(fetch_security_list(pool, 2))
    bj_stocks = [
        (2, str(r.get("code", "")), str(r.get("name", "")).strip())
        for r in bj
        if str(r.get("code", "")).startswith(A_PREFIX_BJ)
    ]
    if 100 <= len(bj_stocks) <= 400:
        universe.extend(bj_stocks)

    universe.sort(key=lambda item: (item[0], item[1]))
    return universe


def _is_level1(name: str, min_len: int = 2) -> bool:
    """判断是否为申万一级行业。

    通达信的行业指数名会被截断（如「家用电器」显示为「家电」、「建筑装饰」显示为「建筑」），
    因此除精确匹配外，还允许「一级行业名以该名称开头」的缩写匹配。
    """
    if name in SW_L1_ALIASES:
        return True
    if len(name) < min_len:
        return False
    return any(alias.startswith(name) for alias in SW_L1_ALIASES)


def load_industry_universe(pool: TdxPool) -> Tuple[List[Security], str]:
    """构建申万行业指数池（通达信 881xxx）。

    先取证券列表里能拿到的名称，再按代码区间逐个探测有效性：
    列表接口在 881101-881146 区段会失败，但这些代码本身可以正常取日 K，
    直接扫描可以补全农林牧渔、基础化工、钢铁、有色金属、食品饮料等一级板块。
    """
    names: Dict[str, str] = {}
    for row in _dedup(fetch_security_list(pool, 1)):
        code = str(row.get("code", ""))
        if code.startswith("881"):
            names[code] = str(row.get("name", "")).strip()

    start, end = INDUSTRY_CODE_RANGE
    candidates = [str(code) for code in range(start, end)]

    def probe(code: str):
        frame = fetch_index_bars(pool, 1, code, 3)
        return code, (not frame.empty)

    with ThreadPoolExecutor(max_workers=8) as ex:
        checked = list(ex.map(probe, candidates))

    industries: List[Security] = []
    for code, ok in checked:
        if not ok:
            continue
        name = names.get(code) or f"申万行业{code}"
        industries.append((1, code, name))

    if INDUSTRY_LEVEL1_ONLY:
        level1 = [item for item in industries if _is_level1(item[2])]
        if len(level1) >= SW_L1_MIN_MATCH:
            return level1, f"申万一级行业 {len(level1)} 个"
        return industries, f"申万行业指数（各级混合）{len(industries)} 个"

    return industries, f"申万行业指数 {len(industries)} 个（通达信 881xxx，含一/二/三级）"


def board_of(code: str, market: int) -> str:
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if market == 2:
        return "北交所"
    if code.startswith(("000", "001", "002", "003")):
        return "深主板"
    return "沪主板"
