"""生成 HTTP 行情源使用的 A 股股票池文件（data/universe_web.csv）。

HTTP 源（腾讯）没有稳定的全市场证券列表接口，因此股票池落盘维护：
优先从现有缓存导出，缓存缺失时回退到通达信列表接口（仅当通达信站点可用）。

用法::

    python scripts/build_universe_web.py              # 从 data/raw_frames.pkl 导出
    python scripts/build_universe_web.py --from-tdx   # 从通达信列表接口重建
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from afgi.config import UNIVERSE_FILE  # noqa: E402

OUT = ROOT / UNIVERSE_FILE


def from_cache(cache: Path) -> pd.DataFrame:
    with open(cache, "rb") as fh:
        payload = pickle.load(fh)
    rows = []
    for code, item in (payload.get("stock_raw") or {}).items():
        rows.append({
            "code": str(code).zfill(6),
            "market": int(item.get("market", 0)),
            "name": str(item.get("name") or ""),
        })
    return pd.DataFrame(rows)


def from_tdx() -> pd.DataFrame:
    from afgi.source import make_pool
    from afgi.universe import load_a_share_universe

    pool = make_pool()
    if getattr(pool, "is_web", False):
        raise SystemExit("通达信站点不可用，无法从列表接口重建；请改用缓存导出")
    rows = [{"code": c, "market": m, "name": n} for m, c, n in load_a_share_universe(pool)]
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-tdx", action="store_true")
    ap.add_argument("--cache", default=str(ROOT / "data" / "raw_frames.pkl"))
    args = ap.parse_args()

    frame = from_tdx() if args.from_tdx else from_cache(Path(args.cache))
    if frame.empty:
        raise SystemExit("股票池为空，未写入")
    frame = frame.sort_values(["market", "code"]).drop_duplicates(subset=["code"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(OUT, index=False, encoding="utf-8")
    print(f"已写入 {OUT} ：{len(frame)} 只（市场分布 {frame['market'].value_counts().to_dict()}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
