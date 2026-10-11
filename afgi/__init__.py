"""A 股市场情绪指数（AFGI）——通达信数据版。

原仓库 https://github.com/Quantify-hp/Market-sentiment-index 依赖 Tushare Pro Token
（ETF 日线还需 5000 积分）与 AkShare，两者不可用时本包改用通达信行情接口。
"""
from __future__ import annotations

__version__ = "1.0.0"

from .config import SENTIMENT_WEIGHTS  # noqa: F401
from .model import build_components, component_breakdown, latest_snapshot  # noqa: F401
from .pipeline import run  # noqa: F401
from .scoring import classify_state, rolling_percentile_score  # noqa: F401
