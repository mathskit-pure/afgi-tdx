#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AFGI 云端发布器：把 run.py 的产物上传到一个固定页面，并同步 KPI 卡片。

与本地 scripts/refresh_afgi_kpi.py 的区别
----------------------------------------
本地版本依赖主项目（舞弊调整质量因子）的 data/nav.csv、data/afgi_combo_nav.csv、
rebalance_signal.csv 来更新 4 张 KPI 卡；云端仓库里没有这些文件（也不该让云端
去改它们）。因此这里只做【AFGI 自己的那张卡】：数值 / 状态徽章 / 截至日，
其余 KPI 卡一字不动，交给 09:00 的主刷新流水线在它那一侧更新。

用法
----
    python3 cloud/publish.py                 # 上传 AFGI 页面 + 同步 KPI 卡
    python3 cloud/publish.py --skip-kpi      # 只上传 AFGI 页面
    python3 cloud/publish.py --no-import     # 只改本地产物，不上传（排错用）

退出码
------
    0  全部成功（或 KPI 已是最新，幂等跳过）
    3  找不到 output/afgi_latest.json 或报告，跳过发布
    4  AFGI 主页面上传失败
    5  AFGI 主页面已上传，但股池页面 KPI 卡同步失败（不回滚主页面）
    6  AFGI 主页面节点已不存在：放弃上传（绝不新建替代页面）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"

# 市场情绪指数AFGI（覆写，禁止新建）
# 原节点 3ezinMuQRXc9M8TpQsOJgF 已于 2026-10-11 确认永久删除（node-info 报
# 11510 node index not found，search-nodes 也搜不到），资料库没有恢复接口；
# 这是用 output/report.html 重建后的新节点。再被删只能再重建一次并回填这里。
AFGI_NODE = "ddbJ1onUkhBUybcqWTrwG5"
POOL_NODE = "iB8I58mCTooLicPQ0Kyq0M"   # 舞弊调整质量因子·季度动态股池（覆写）

# 与 scripts/refresh_page.py 的 AFGI_BADGE 完全一致（五档 + 未知）
AFGI_BADGE = {
    "极度恐惧": ("#dcfce7", "#14532d"),
    "恐惧":     ("#dcfce7", "#166534"),
    "中性":     ("#fef3c7", "#92400e"),
    "贪婪":     ("#fee2e2", "#b91c1c"),
    "极度贪婪": ("#fecaca", "#7f1d1d"),
    "未知":     ("#f3f4f6", "#6b7280"),
}

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg: str) -> None:
    print(f"[afgi-cloud] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 资料库 skill 路径探测（云端 Linux 与本地 Windows 都能用）
# ---------------------------------------------------------------------------
def find_library_skill() -> Path | None:
    cands: list[Path] = []
    env = os.environ.get("WB_LIBRARY_SKILL")
    if env:
        cands.append(Path(env))
    if os.name == "nt":
        cands.append(Path(os.path.expandvars(
            r"%LOCALAPPDATA%\Programs\WorkBuddy\resources\app.asar.unpacked"
            r"\resources\plugins\workbuddy-builtin\skills\library")))
    else:
        cands += [
            Path("/root/.codebuddy/skills/library"),
            Path("/root/.workbuddy/skills/library"),
            Path.home() / ".codebuddy/skills/library",
            Path.home() / ".workbuddy/skills/library",
        ]
    for c in cands:
        if (c / "page" / "import_html.py").exists():
            return c
    return None


def run(cmd: list[str], timeout: int = 1800) -> str:
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = r.stdout.decode("utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(
            f"命令失败({' '.join(cmd[:3])}): rc={r.returncode}\n"
            f"{out[-1200:]}\n{r.stderr.decode('utf-8', errors='replace')[-800:]}")
    return out


def node_exists(skill: Path, node: str) -> bool:
    """上传前探活：确认目标节点还在。

    节点被删除后 import_html.py 并不会报错退出，而是「新建」一个页面，
    这会往资料库里塞垃圾：实测曾把 AFGI 报告误建成 ONJ32MfcXPSPzKifc5pKlM。
    所以固定节点覆写前必须先探活，不存在就直接放弃，绝不新建。
    """
    r = subprocess.run(
        [sys.executable, str(skill / "page" / "download_page_artifacts.py"),
         "--node-id", node],
        capture_output=True, timeout=300,
    )
    out = r.stdout.decode("utf-8", errors="replace")
    if "KS_ARTIFACTS_OK" in out:
        return True
    log(f"  探活失败：{out.strip()[:300] or r.stderr.decode('utf-8', errors='replace')[:300]}")
    return False


def import_page(skill: Path, html: Path, node: str) -> str:
    """覆写固定页面节点，返回 import_html.py 的原始输出。"""
    out = run([sys.executable, str(skill / "page" / "import_html.py"),
               str(html), "--node-block-id", node])
    if "KS_IMPORT_OK" not in out:
        raise RuntimeError(f"上传失败，未见到 KS_IMPORT_OK\n{out[-1200:]}")
    if node not in out:
        raise RuntimeError(f"上传结果里没有目标节点 {node}，疑似新建了页面\n{out[-1200:]}")
    return out


def pull_live_page(skill: Path, node: str, out_dir: str) -> Path:
    """拉取线上最新版页面当基底，返回入口 HTML 路径。"""
    out = run([sys.executable, str(skill / "page" / "download_page_artifacts.py"),
               "--node-id", node, "--out-dir", out_dir])
    line = [l for l in out.splitlines() if "KS_ARTIFACTS_OK" in l]
    if not line:
        raise RuntimeError(f"拉取线上页面失败，未见到 KS_ARTIFACTS_OK\n{out[-800:]}")
    info = json.loads(line[0].split("KS_ARTIFACTS_OK", 1)[1].strip())
    return Path(info["work_dir"]) / info["entry_html"]


# ---------------------------------------------------------------------------
# KPI 卡片改写：只动「A股恐慌贪婪指数」这一张
# ---------------------------------------------------------------------------
def _set_inner(seg: str, tag_re: str, new_text: str, what: str) -> tuple[str, bool]:
    """替换某个标签的内部文本，保留 <!--pnid:xxx--> 注释与标签自身属性。"""
    m = re.search(tag_re, seg)
    if not m:
        return seg, False
    inner = m.group("inner")
    prefix = ""
    pm = re.match(r"\s*<!--pnid:[^>]*-->\s*", inner)
    if pm:
        prefix = pm.group(0)
    old = m.group(0)
    new = old.replace(inner, prefix + new_text)
    if new == old:
        return seg, False
    log(f"  · {what}: {re.sub(r'<[^>]+>|<!--.*?-->', '', inner).strip()} → {new_text}")
    return seg.replace(old, new, 1), True


def patch_afgi_kpi(html: str, value: float, state: str, date_iso: str) -> tuple[str, bool]:
    """只改 AFGI 那张 KPI 卡：数值 / 状态徽章（含配色）/ 截至日。"""
    m = re.search(r'<div[^>]*\bclass="kpi"[^>]*>(?:(?!</div>).)*?A股恐慌贪婪指数.*?</div>',
                  html, re.S)
    if not m:
        log("  ! 页面上找不到「A股恐慌贪婪指数」KPI 卡，跳过同步")
        return html, False

    seg = m.group(0)
    seg, ok1 = _set_inner(seg, r'<b(?P<attrs>[^>]*)>(?P<inner>.*?)</b>', f"{value:.2f}", "数值")
    bg, fg = AFGI_BADGE.get(state, AFGI_BADGE["未知"])
    seg, ok2 = _set_inner(seg, r'<span[^>]*\bclass="afgi-bd"[^>]*>(?P<inner>.*?)</span>',
                          state, "状态")
    seg = re.sub(r'(\bclass="afgi-bd"[^>]*\bstyle="[^"]*?background:\s*)(#[0-9a-fA-F]{3,8})',
                 rf'\g<1>{bg}', seg, count=1)
    seg = re.sub(r'(\bclass="afgi-bd"[^>]*\bstyle="[^"]*?\bcolor:\s*)(#[0-9a-fA-F]{3,8})',
                 rf'\g<1>{fg}', seg, count=1)
    log(f"  · 徽章配色: bg={bg} fg={fg}")
    seg, ok3 = _set_inner(
        seg,
        r'<span[^>]*style="display:block;[^"]*"[^>]*>(?P<inner>.*?)</span>',
        f"截至 {date_iso}", "截至日")

    if not (ok1 or ok2 or ok3):
        log("  · KPI 卡片已是最新读数，无需重复上传")
        return html, False
    return html.replace(m.group(0), seg, 1), True


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-kpi", action="store_true", help="只上传 AFGI 页面")
    ap.add_argument("--no-import", action="store_true", help="只本地改写，不上传")
    args = ap.parse_args()

    latest = OUTPUT / "afgi_latest.json"
    report = OUTPUT / "report.html"
    if not latest.exists() or not report.exists():
        log("!! 缺少 output/afgi_latest.json 或 output/report.html，跳过发布")
        return 3

    j = read_json(latest)
    raw_date = str(j.get("trade_date", "")).strip()
    date_iso = (f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                if len(raw_date) == 8 and "-" not in raw_date else raw_date)
    value = float(j["afgi"])
    state = str(j.get("state") or "未知")
    log(f"最新读数：{date_iso} → {value:.2f}（{state}）")

    if args.no_import:
        log("--no-import：跳过上传")
        return 0

    skill = find_library_skill()
    if not skill:
        log("!! 找不到资料库 library skill（import_html.py），无法上传；"
            "可用环境变量 WB_LIBRARY_SKILL 指定其路径")
        return 4

    # 1) AFGI 主页面（先探活，节点不在就放弃，避免误建）
    if not node_exists(skill, AFGI_NODE):
        log(f"!! 目标节点 {AFGI_NODE} 已不存在（可能被删除或移入回收站）")
        log("!! 已放弃上传——若继续调用 import_html，它会新建一个垃圾页面")
        log("!! 请到资料库恢复该页面，或新建后把新 nodeId 填进 cloud/publish.py 的 AFGI_NODE")
        return 6

    try:
        out = import_page(skill, report, AFGI_NODE)
        log(f"AFGI 页面已覆写 {AFGI_NODE}：https://www.workbuddy.cn/space/d/{AFGI_NODE}")
        log("  " + [l for l in out.splitlines() if "KS_IMPORT_OK" in l][0].strip())
    except Exception as e:
        log(f"!! AFGI 页面上传失败：{e}")
        return 4

    if args.skip_kpi:
        return 0

    # 2) 股池页面的 AFGI KPI 卡
    work = tempfile.mkdtemp(prefix="afgi-kpi-")
    try:
        entry = pull_live_page(skill, POOL_NODE, work)
        html = entry.read_text(encoding="utf-8")
        log(f"线上基底：{entry.name}（{len(html):,} chars）")
        new_html, changed = patch_afgi_kpi(html, value, state, date_iso)
        if not changed:
            return 0
        out_html = OUTPUT / "pool_page_afgi.html"
        out_html.write_text(new_html, encoding="utf-8")
        import_page(skill, out_html, POOL_NODE)
        log(f"股池页面 KPI 已同步 {POOL_NODE}：https://www.workbuddy.cn/space/d/{POOL_NODE}")
        return 0
    except Exception as e:
        log(f"!! KPI 同步失败（AFGI 主页面已更新，不受影响）：{e}")
        return 5
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
