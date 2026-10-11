#!/usr/bin/env bash
# 云端每日增量更新：拉代码 → 跑 run.py → 上传页面。
# 用法：  bash cloud/daily.sh          # 只跑更新 + 发布
#        GIT_PULL=1 bash cloud/daily.sh  # 先 git pull 再跑
# 退出码：0 成功 / 3 已有任务在运行（正常跳过）/ 4 主页面上传失败 /
#         5 主页面已上传但 KPI 同步失败 / 其它 = run.py 自身失败码
set -uo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
echo "[daily] 工作目录: $(pwd)"

if [[ "${GIT_PULL:-0}" == "1" ]]; then
    git pull --ff-only || echo "[daily] 警告：git pull 失败，沿用现有代码继续"
fi

$PY run.py --bars 800
rc=$?
if [[ $rc -eq 3 ]]; then
    echo "[daily] SKIP：已有更新任务在运行或并发锁未释放（退出码 3），本次不上传"
    exit 3
fi
if [[ $rc -ne 0 ]]; then
    echo "[daily] FAIL：run.py 退出码 $rc"
    exit $rc
fi

$PY cloud/publish.py
