# AFGI 云端部署说明

把「每日更新 A 股情绪指数（AFGI）」这条流水线从本机搬到云端，本机不开机也能正常跑。

## 一、为什么能直接搬

- 代码本身路径无关：`run.py` 用 `Path(__file__).parent` 定位全部目录，没有任何 `C:\Users\...` 硬编码。
- 依赖只有 `pandas / numpy / pytdx`，全部可 `pip install`，不需要 Token。
- 行情缓存 `data/raw_frames.pkl` 可以在云端重建：全 A 约 5200 只 × 800 根日 K，首次全量约 4~10 分钟，之后每天增量仅 40 秒左右。
- 代码仓库只有 169KB（缓存与产物已 `.gitignore`），云端 `git pull` 成本几乎为零。

## 二、云端目录与命令

约定云端工作目录：`/workspace/afgi_tdx`（与已有云端任务 `B-score` 同级）。
代码仓库是**公开仓库**，因此云端 `git clone` / `git pull` 免凭证 —— 这是选公开而非私有的唯一原因（仓库内无任何密钥、Token 或隐私数据）。

```bash
# 首次（只需一次）
git clone https://github.com/mathskit-pure/afgi-tdx.git /workspace/afgi_tdx
cd /workspace/afgi_tdx && bash cloud/bootstrap.sh     # 装依赖 + 探测服务器 + 建缓存

# 每天
cd /workspace/afgi_tdx && GIT_PULL=1 bash cloud/daily.sh
```

`cloud/daily.sh` 内部依次执行：

1. `git pull --ff-only`（`GIT_PULL=1` 时）
2. `python3 run.py --bars 800` —— 增量更新到最新交易日，重算指数，刷新 `output/report.html`、`afgi_timeseries.csv`、`afgi_latest.json`
3. `python3 cloud/publish.py` —— 覆写 AFGI 固定页面节点，并同步股池页面的 AFGI 指标卡

## 三、退出码约定（给定时任务判断用）

| 退出码 | 含义 | 处理 |
|---|---|---|
| 0 | 全部成功，或 KPI 已是最新（幂等跳过） | 正常汇报 |
| 3 | 已有更新任务在运行 / 并发锁未释放 | 正常情况，直接说明，不重试、不上传 |
| 4 | 找不到资料库 skill，或 AFGI 主页面上传失败 | 汇报失败，不要新建页面 |
| 5 | AFGI 主页面已上传，但股池页面 KPI 卡同步失败 | 汇报「主页面已更新、KPI 未同步」 |
| 其它 | run.py 自身失败 | 贴出错误摘要 |

## 四、固定页面节点（只允许覆写，禁止新建）

| 页面 | nodeId | 链接 |
|---|---|---|
| 市场情绪指数AFGI | `3ezinMuQRXc9M8TpQsOJgF` | https://www.workbuddy.cn/space/d/3ezinMuQRXc9M8TpQsOJgF |
| 舞弊调整质量因子·季度动态股池 | `iB8I58mCTooLicPQ0Kyq0M` | https://www.workbuddy.cn/space/d/iB8I58mCTooLicPQ0Kyq0M |

`cloud/publish.py` 对股池页面**只改 AFGI 那一张 KPI 卡**（数值 / 状态徽章 / 截至日），其余 KPI 卡与全部图表、回测、持仓明细一概不动 —— 因为云端没有主项目的 `nav.csv`、持仓数据，不该由它去改。

## 五、与本机任务的关系

- 云端与本机**不能同时跑**：两边都会覆写同一个页面节点 `3ezinMuQRXc9M8TpQsOJgF`，且各自维护独立的行情缓存，双跑会互相覆盖。
- 建议：云端任务跑通并通过 1~2 个交易日验证后，把本机那条「每日收盘后更新 A 股情绪指数（AFGI）」暂停（PAUSED），保留其余本机任务不动。
- 回退方案：云端异常时，把本机任务恢复 ACTIVE 即可，两侧脚本完全独立。

## 六、云端定时任务配置

- 名称：每日收盘后更新 A 股情绪指数（AFGI）·云端
- 调度：`FREQ=WEEKLY;BYDAY=TU,WE,TH,FR,SA;BYHOUR=7;BYMINUTE=30`
  （T+1 早上处理 T 日数据，与本机原任务同一时点；指数与 ETF 日 K 落地比个股晚，早上取数才拿得全）
- 运行位置：云端（WorkBuddy 云端任务，不是本机）

可粘贴的完整 prompt（把 `https://github.com/mathskit-pure/afgi-tdx.git` 换成实际地址）：

```
在云端目录 /workspace/afgi_tdx 下执行 A 股市场情绪指数（AFGI）的每日更新，并把结果覆写到固定线上页面。

【调度位置】本任务固定在 T+1 日 07:30 计算 T 日数据，是每日流水线的第一棒：
07:30 AFGI（本任务，云端）→ 09:00 主刷新「舞弊调整质量因子·季度动态股池」（本机）
→ 10:00 PROF 盈利能力模型（本机）。后两棒暂时仍在本机，本任务必须在此之前完成。

执行步骤：
1) 首次运行（目录不存在时）：git clone https://github.com/mathskit-pure/afgi-tdx.git /workspace/afgi_tdx
   然后 cd /workspace/afgi_tdx && bash cloud/bootstrap.sh
   该脚本会装依赖、用 python3 run.py --probe 探测通达信行情服务器、并全量建立行情缓存，
   首次约 5~15 分钟。若目录已存在则跳过本步。
2) 每天：cd /workspace/afgi_tdx && GIT_PULL=1 bash cloud/daily.sh
   内部依次执行 git pull → python3 run.py --bars 800（增量更新 + 重算指数）
   → python3 cloud/publish.py（覆写页面 + 同步 KPI 卡）。

退出码约定：
· 0  全部成功，或 KPI 已是最新读数（幂等跳过，不算失败，不必重试）
· 3  已有更新任务在运行或并发锁未释放 —— 正常情况，直接说明即可，不重试、不上传
· 4  找不到资料库 skill 或 AFGI 主页面上传失败
· 5  AFGI 主页面已上传，但股池页面 KPI 卡同步失败（AFGI 页面不受影响）
· 其它 = run.py 自身失败，贴出错误摘要

校验（必做）：检查 output/afgi_timeseries.csv 最新一行的 14 个 *_status 列是否全为 ok，
最新日期是否等于最近一个交易日。若出现 missing，明确指出是哪几项（历史上 volatility /
risk_appetite / small_large_relative_strength 最易受影响），不要当成正常数据汇报 ——
脚本会按每个标的自身的最新日期独立判定缺口，后续运行会自动持续重试补齐。
若输出显示「补下载 0 根」，说明上一日为非交易日（节假日或周末），直接说明休市即可。

汇报（简体中文，简要）：
· 最新计算日、AFGI 数值与所处状态（极度恐惧/恐惧/中性/贪婪/极度贪婪）、较前一交易日变化、20 日均线；
· 本次补下载了多少根 K 线、新增了多少只股票；
· AFGI 固定页面是否已成功覆写（https://www.workbuddy.cn/space/d/3ezinMuQRXc9M8TpQsOJgF ）；
· 股池页面「A股恐慌贪婪指数」指标卡是否已同步（同步值 / 幂等跳过 / 失败及原因）。

约束：只运行上述命令并汇报，不要修改任何代码或配置；只允许覆写 CLOUD_DEPLOY.md 中列出的
两个固定节点（3ezinMuQRXc9M8TpQsOJgF、iB8I58mCTooLicPQ0Kyq0M），绝不新建页面。
```
