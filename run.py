"""命令行入口：构建 / 更新 A 股市场情绪指数。

    python run.py                       # 增量更新到最新交易日并重算（日常用法）
    python run.py --offline             # 不联网，用本地缓存重算并出报告
    python run.py --refresh             # 忽略缓存，全量重建
    python run.py --history 250         # 只输出最近 250 个交易日
    python run.py --probe               # 只探测可用的通达信行情服务器
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LOCK_TIMEOUT_HOURS = 3


def _progress(done: int, total: int, msg: str) -> None:
    pct = done / max(total, 1) * 100
    print(f"\r[AFGI] {msg}  {done}/{total} ({pct:.0f}%)", end="", flush=True)
    if done >= total:
        print()


def probe() -> int:
    from afgi.config import TDX_HOSTS
    from afgi.tdx import probe_hosts

    print(f"探测 {len(TDX_HOSTS)} 台通达信行情服务器 ...")
    usable = probe_hosts(TDX_HOSTS)
    if not usable:
        print("没有可用于日 K 的服务器，请检查网络或更换 afgi/config.py 中的 TDX_HOSTS")
        return 1
    for host, port, cost in usable:
        print(f"  [OK] {host}:{port}  {cost:.0f} ms")
    print(f"共 {len(usable)} 台可用")
    return 0


class _Tee:
    """同时写 stdout 与日志文件。"""

    def __init__(self, stream, fh) -> None:
        self.stream = stream
        self.fh = fh

    def write(self, text: str) -> int:
        self.stream.write(text)
        try:
            self.fh.write(text)
        except Exception:
            pass
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        try:
            self.fh.flush()
        except Exception:
            pass


def _pid_alive(pid: int) -> bool:
    """判断锁文件里的进程是否还活着（进程被强杀时锁会残留）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess

        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=15,
            )
            return str(pid) in out.stdout
        except Exception:
            return True  # 无法判断时保守处理
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_lock(data_dir: Path, force: bool = False) -> Path:
    """防止定时任务与手动运行并发写同一份缓存。"""
    lock = data_dir / ".update.lock"
    if lock.exists() and not force:
        age_h = (time.time() - lock.stat().st_mtime) / 3600
        try:
            pid = int(lock.read_text(encoding="utf-8").split("|")[0])
        except Exception:
            pid = -1
        if _pid_alive(pid) and age_h < LOCK_TIMEOUT_HOURS:
            print(f"[AFGI] 已有更新任务在运行（PID {pid}，{age_h:.1f} 小时前启动），本次跳过")
            raise SystemExit(3)
        print(f"[AFGI] 清理残留锁（PID {pid}，{age_h:.1f} 小时）")
    lock.write_text(
        f"{os.getpid()}|{datetime.now().isoformat(timespec='seconds')}", encoding="utf-8"
    )
    return lock


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A 股市场情绪指数（通达信数据版）")
    parser.add_argument("--history", type=int, default=None, help="只保留最近 N 个交易日")
    parser.add_argument("--bars", type=int, default=800, help="每只标的缓存的日 K 根数，默认 800")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--output-dir", default=str(ROOT / "output"))
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，全量重建")
    parser.add_argument("--offline", action="store_true", help="不联网，仅用本地缓存重算")
    parser.add_argument("--probe", action="store_true", help="只探测行情服务器")
    parser.add_argument("--limit", type=int, default=None, help="只取前 N 只股票（冒烟测试用）")
    parser.add_argument("--force", action="store_true", help="忽略并发锁强制运行")
    args = parser.parse_args(argv)

    if args.probe:
        return probe()

    from afgi.pipeline import run
    from afgi.report import render_report

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    lock = _acquire_lock(data_dir, force=args.force)
    log_path = data_dir / "update.log"
    start = time.time()
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            mode = "refresh" if args.refresh else "offline" if args.offline else "update"
            fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {mode} =====\n")
            original = sys.stdout
            sys.stdout = _Tee(original, fh)
            try:
                df = run(
                    data_dir=data_dir,
                    output_dir=output_dir,
                    count=args.bars,
                    use_cache=not args.refresh,
                    history_days=args.history,
                    progress=_progress,
                    limit=args.limit,
                    offline=args.offline,
                )
            finally:
                sys.stdout = original

        report_path = render_report(
            df,
            output_dir / "report.html",
            meta={
                "universe_size": df.attrs.get("universe_size", ""),
                "industry_desc": df.attrs.get("industry_desc", ""),
            },
        )
    finally:
        lock.unlink(missing_ok=True)

    latest = df.iloc[-1]
    stats = df.attrs.get("update_stats") or {}
    print()
    print("=" * 56)
    print(f"  计算日     {latest['trade_date']}")
    print(f"  AFGI       {latest['afgi']:.2f}   ({latest['afgi_state']})")
    print(f"  较前一日   {latest['afgi_change']:+.2f}")
    print(f"  20日均线   {latest['afgi_ma20']:.2f}")
    print(f"  样本区间   {len(df)} 个交易日")
    if stats:
        print(f"  更新情况   补下载 {stats.get('need', 0)} 根 / "
              f"更新 {stats.get('refreshed', 0)} 只 / 新股 {stats.get('new_listings', 0)} 只")
    print(f"  产出       {report_path}")
    print(f"  耗时       {time.time()-start:.0f}s")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
