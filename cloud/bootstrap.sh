#!/usr/bin/env bash
# 云端首次初始化：装依赖 + 探测通达信行情服务器 + 全量建立行情缓存。
# 用法：  bash cloud/bootstrap.sh
# 说明：  全 A 约 5200 只 × 800 根日 K，首次全量约 4~10 分钟（视服务器与带宽）。
set -euo pipefail

cd "$(dirname "$0")/.."
echo "[bootstrap] 工作目录: $(pwd)"

PY="${PYTHON:-python3}"
echo "[bootstrap] 解释器: $($PY -V 2>&1)"

# 1) 依赖
$PY -m pip install --quiet --upgrade pip
$PY -m pip install --quiet -r requirements.txt
echo "[bootstrap] 依赖安装完成"

# 2) 探测行情服务器（不通就直接失败，避免空跑）
$PY run.py --probe
echo "[bootstrap] 行情服务器探测完成"

# 3) 全量建缓存（幂等：已有缓存时等价于一次增量）
$PY run.py --bars 800
echo "[bootstrap] 缓存建立完成"

# 4) 产物自检
$PY - <<'PY'
import json, pathlib
p = pathlib.Path("output/afgi_latest.json")
if not p.exists():
    raise SystemExit("[bootstrap] 缺少 output/afgi_latest.json，初始化失败")
j = json.loads(p.read_text(encoding="utf-8"))
print("[bootstrap] 最新读数:", j.get("trade_date"), j.get("afgi"), j.get("afgi_state"))
PY

echo "[bootstrap] OK"
