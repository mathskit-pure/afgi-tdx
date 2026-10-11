"""生成独立的 HTML 情绪指数报告。

图表全部用内联 SVG 绘制，不依赖任何 CDN 或本地字体之外的外部资源。
配色遵循 A 股习惯：恐惧=绿，贪婪=红。
"""
from __future__ import annotations

import html
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .config import CORE_RAW_META, RAW_INDICATOR_META, SENTIMENT_WEIGHTS
from .model import component_breakdown
from .scoring import classify_state

# ---------------------------------------------------------------------------
# 配色
# ---------------------------------------------------------------------------
C_FEAR = "#1f9d55"        # 恐惧：绿
C_NEUTRAL = "#e8a33d"     # 中性：橙黄
C_GREED = "#d92b2b"       # 贪婪：红
C_GRID = "#e6e8eb"
C_TEXT = "#1f2328"
C_SUB = "#6b7280"
C_BG = "#ffffff"
C_PANEL = "#f7f8fa"
C_LINE = "#c0392b"
C_LINE_MA = "#7f8c9a"


def _state_color(value: float) -> str:
    if value >= 80:
        return C_GREED
    if value >= 60:
        return "#e06c3b"
    if value >= 40:
        return C_NEUTRAL
    if value >= 20:
        return "#7cb342"
    return C_FEAR


def _fmt(value, digits: int = 2, suffix: str = "") -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "--"
    return f"{value:.{digits}f}{suffix}"


# ---------------------------------------------------------------------------
# SVG 组件
# ---------------------------------------------------------------------------
def gauge_svg(value: float, state: str) -> str:
    """半圆温度计。"""
    cx, cy, r = 210.0, 200.0, 155.0
    start = (cx - r, cy)
    end = (cx + r, cy)
    angle = math.pi * (1 - max(0.0, min(100.0, value)) / 100.0)
    nx = cx + (r - 26) * math.cos(angle)
    ny = cy - (r - 26) * math.sin(angle)
    color = _state_color(value)

    ticks = []
    for tick in range(0, 101, 20):
        a = math.pi * (1 - tick / 100.0)
        x1 = cx + (r + 8) * math.cos(a)
        y1 = cy - (r + 8) * math.sin(a)
        x2 = cx + (r + 18) * math.cos(a)
        y2 = cy - (r + 18) * math.sin(a)
        ticks.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{C_SUB}" stroke-width="1.5"/>'
            f'<text x="{cx + (r + 32) * math.cos(a):.1f}" y="{cy - (r + 32) * math.sin(a) + 4:.1f}" '
            f'font-size="12" fill="{C_SUB}" text-anchor="middle">{tick}</text>'
        )
    ticks = "\n".join(ticks)

    return f"""
<svg viewBox="0 0 420 250" width="100%" style="max-width:420px" role="img">
  <defs>
    <linearGradient id="gaugeGrad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{C_FEAR}"/>
      <stop offset="45%" stop-color="{C_NEUTRAL}"/>
      <stop offset="100%" stop-color="{C_GREED}"/>
    </linearGradient>
  </defs>
  <path d="M {start[0]:.1f} {start[1]:.1f} A {r} {r} 0 0 1 {end[0]:.1f} {end[1]:.1f}"
        fill="none" stroke="url(#gaugeGrad)" stroke-width="24" stroke-linecap="round"/>
  <line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="{C_TEXT}" stroke-width="4" stroke-linecap="round"/>
  <circle cx="{cx}" cy="{cy}" r="9" fill="{C_TEXT}"/>
  <text x="{cx}" y="{cy - 42}" font-size="54" font-weight="700" fill="{color}" text-anchor="middle">{value:.1f}</text>
  <text x="{cx}" y="{cy - 16}" font-size="16" fill="{C_SUB}" text-anchor="middle">{html.escape(state)}</text>
  {ticks}
  <text x="{cx - r}" y="{cy + 26}" font-size="12" fill="{C_SUB}" text-anchor="middle">极度恐惧</text>
  <text x="{cx + r}" y="{cy + 26}" font-size="12" fill="{C_SUB}" text-anchor="middle">极度贪婪</text>
</svg>
"""


# 走势图默认展示的时间跨度（交易日）。244 ≈ 一年
DEFAULT_RANGE_DAYS = 244

CHART_JS = """
(function () {
  var D = __DATA__;
  var N = D.dates.length;
  var W = 940, H = 330, L = 58, R = 18, T = 18, B = 48;
  var PW = W - L - R, PH = H - T - B;
  var host = document.getElementById('afgiChart');
  var tip = document.getElementById('afgiTip');
  var status = document.getElementById('afgiRange');
  var DEFAULT_DAYS = __DEFAULT_DAYS__;
  var view = [Math.max(0, N - DEFAULT_DAYS), N - 1];
  var drag = { on: false, x0: 0, i0: 0, i1: 0, n: 0 };

  var BANDS = [[0,20,'#e8f5ec'],[20,40,'#f0f7e8'],[40,60,'#fbf7ec'],[60,80,'#fdf0e6'],[80,100,'#fbeae8']];

  function tickText(s, spanDays) {
    if (spanDays <= 95) { return s.slice(5); }
    if (spanDays <= 800) { return s.slice(2, 7); }
    return s.slice(0, 4);
  }
  function spanDays() {
    return (new Date(D.dates[view[1]]) - new Date(D.dates[view[0]])) / 86400000;
  }

  function render() {
    var i0 = view[0], i1 = view[1];
    if (i1 - i0 < 1) { i0 = Math.max(0, Math.min(i0, N - 2)); i1 = Math.min(N - 1, i0 + 1); view = [i0, i1]; }
    var n = i1 - i0 + 1;
    var X = function (i) { return L + PW * (i - i0) / (n - 1); };
    var Y = function (v) { return T + PH * (1 - v / 100); };
    var sd = spanDays();
    var p = [];

    BANDS.forEach(function (b) {
      p.push('<rect x="' + L + '" y="' + Y(b[1]).toFixed(1) + '" width="' + PW +
             '" height="' + (Y(b[0]) - Y(b[1])).toFixed(1) + '" fill="' + b[2] + '"/>');
    });
    [0, 20, 40, 60, 80, 100].forEach(function (v) {
      var y = Y(v).toFixed(1);
      p.push('<line x1="' + L + '" y1="' + y + '" x2="' + (L + PW) + '" y2="' + y +
             '" stroke="#e6e8eb" stroke-width="1"/>');
      p.push('<text x="' + (L - 8) + '" y="' + (Y(v) + 4).toFixed(1) +
             '" font-size="11" fill="#6b7280" text-anchor="end">' + v + '</text>');
    });

    var step = Math.max(1, Math.floor(n / 8));
    for (var i = i0; i <= i1; i += step) {
      var x = X(i).toFixed(1);
      p.push('<line x1="' + x + '" y1="' + T + '" x2="' + x + '" y2="' + (T + PH) +
             '" stroke="#f0f2f4" stroke-width="1"/>');
      p.push('<text x="' + x + '" y="' + (H - B + 18) + '" font-size="11" fill="#6b7280" ' +
             'text-anchor="middle">' + tickText(D.dates[i], sd) + '</text>');
    }

    function pathOf(vals) {
      var d = '', open = false;
      for (var i = i0; i <= i1; i++) {
        var v = vals[i];
        if (v === null || v === undefined || isNaN(v)) { open = false; continue; }
        d += (open ? ' L ' : ' M ') + X(i).toFixed(1) + ' ' + Y(v).toFixed(1);
        open = true;
      }
      return d;
    }
    var area = '', open = false, lastX = null;
    for (var i = i0; i <= i1; i++) {
      var v = D.afgi[i];
      if (v === null || v === undefined || isNaN(v)) { continue; }
      var x = X(i).toFixed(1);
      if (!open) { area = 'M ' + x + ' ' + (T + PH) + ' L ' + x + ' ' + Y(v).toFixed(1); open = true; }
      else { area += ' L ' + x + ' ' + Y(v).toFixed(1); }
      lastX = x;
    }
    if (open) { area += ' L ' + lastX + ' ' + (T + PH) + ' Z'; }

    p.push('<path d="' + area + '" fill="#c0392b" opacity="0.10"/>');
    p.push('<path d="' + pathOf(D.afgi) + '" fill="none" stroke="#c0392b" stroke-width="2"/>');
    p.push('<path d="' + pathOf(D.ma20) + '" fill="none" stroke="#7f8c9a" stroke-width="1.6" stroke-dasharray="5 4"/>');
    p.push('<g transform="translate(' + (L + 8) + ',' + (T + 6) + ')">' +
           '<rect x="0" y="0" width="16" height="3" fill="#c0392b"/>' +
           '<text x="22" y="5" font-size="11" fill="#6b7280">AFGI</text>' +
           '<rect x="62" y="0" width="16" height="3" fill="#7f8c9a"/>' +
           '<text x="84" y="5" font-size="11" fill="#6b7280">20日均线</text></g>');
    p.push('<rect id="afgiSel" x="0" y="' + T + '" width="0" height="' + PH +
           '" fill="#1f2328" opacity="0.10" style="display:none"/>');
    p.push('<line id="afgiCross" y1="' + T + '" y2="' + (T + PH) +
           '" stroke="#9aa4b0" stroke-width="1" style="display:none"/>');
    p.push('<circle id="afgiDot" r="4" fill="#c0392b" style="display:none"/>');
    p.push('<rect id="afgiHit" x="' + L + '" y="' + T + '" width="' + PW + '" height="' + PH +
           '" fill="transparent" style="cursor:crosshair"/>');

    host.innerHTML = '<svg id="afgiSvg" viewBox="0 0 ' + W + ' ' + H + '" width="100%">' + p.join('') + '</svg>';
    if (status) {
      status.textContent = D.dates[view[0]] + ' 至 ' + D.dates[view[1]] + '（' + n + ' 个交易日）';
    }
    syncActive(n);
    bind(X, Y, i0, i1, n);
  }

  function syncActive(n) {
    if (!bar) { return; }
    var isAll = (view[0] === 0 && view[1] === N - 1);
    var atEnd = (view[1] === N - 1);
    Array.prototype.forEach.call(bar.querySelectorAll('button'), function (b) {
      var act = b.getAttribute('data-act');
      var on = false;
      if (act === 'all') { on = isAll; }
      else if (act === 'range') { on = !isAll && atEnd && parseInt(b.getAttribute('data-days'), 10) === n; }
      if (on) { b.classList.add('active'); } else { b.classList.remove('active'); }
    });
  }

  function bind(X, Y, i0, i1, n) {
    var svg = document.getElementById('afgiSvg');
    if (!svg) { return; }
    var sel = document.getElementById('afgiSel');
    var cross = document.getElementById('afgiCross');
    var dot = document.getElementById('afgiDot');
    var hit = document.getElementById('afgiHit');
    drag.i0 = i0; drag.i1 = i1; drag.n = n;

    function toSvgX(e) {
      var r = svg.getBoundingClientRect();
      return (e.clientX - r.left) * (W / r.width);
    }
    function idxAt(sx) {
      var i = i0 + Math.round((sx - L) / PW * (n - 1));
      return Math.max(i0, Math.min(i1, i));
    }

    svg.addEventListener('mousemove', function (e) {
      var sx = toSvgX(e);
      if (drag.on) {
        var a = Math.min(drag.x0, sx), b = Math.max(drag.x0, sx);
        sel.setAttribute('x', a.toFixed(1));
        sel.setAttribute('width', Math.max(0, b - a).toFixed(1));
        sel.style.display = '';
        return;
      }
      var i = idxAt(sx);
      var v = D.afgi[i], m = D.ma20[i];
      cross.setAttribute('x1', X(i).toFixed(1));
      cross.setAttribute('x2', X(i).toFixed(1));
      cross.style.display = '';
      if (v === null || v === undefined || isNaN(v)) { return; }
      dot.setAttribute('cx', X(i).toFixed(1));
      dot.setAttribute('cy', Y(v).toFixed(1));
      dot.style.display = '';
      var r = svg.getBoundingClientRect();
      tip.innerHTML = '<b>' + D.dates[i] + '</b><br/>AFGI ' + v.toFixed(2) +
                      (m === null || m === undefined || isNaN(m) ? '' : '<br/>MA20 ' + m.toFixed(2));
      tip.style.display = 'block';
      tip.style.left = Math.max(0, Math.min(r.width - 150, (e.clientX - r.left + 14))) + 'px';
      tip.style.top = Math.max(4, (e.clientY - r.top - 12)) + 'px';
    });

    svg.addEventListener('mouseleave', function () {
      cross.style.display = 'none';
      dot.style.display = 'none';
      tip.style.display = 'none';
    });

    svg.addEventListener('mousedown', function (e) {
      drag.on = true;
      drag.x0 = toSvgX(e);
      sel.setAttribute('x', drag.x0.toFixed(1));
      sel.setAttribute('width', '0');
      sel.style.display = '';
      e.preventDefault();
    });

    svg.addEventListener('dblclick', function () { view = [0, N - 1]; render(); });
  }

  window.addEventListener('mouseup', function () {
    if (!drag.on) { return; }
    drag.on = false;
    var sel = document.getElementById('afgiSel');
    if (!sel) { return; }
    var w = parseFloat(sel.getAttribute('width') || '0');
    sel.style.display = 'none';
    if (w < 12) { return; }
    var a = parseFloat(sel.getAttribute('x')) - L, b = a + w;
    var na = drag.i0 + Math.round(a / PW * (drag.n - 1));
    var nb = drag.i0 + Math.round(b / PW * (drag.n - 1));
    na = Math.max(0, Math.min(N - 1, na));
    nb = Math.max(0, Math.min(N - 1, nb));
    if (nb - na >= 1) { view = [na, nb]; render(); }
  });

  function setLast(days) {
    view = [Math.max(0, N - days), N - 1];
    render();
  }
  function pan(dir) {
    var span = view[1] - view[0] + 1;
    var shift = Math.max(1, Math.round(span / 4)) * dir;
    var a = Math.max(0, Math.min(N - span, view[0] + shift));
    view = [a, a + span - 1];
    render();
  }

  var bar = document.getElementById('afgiToolbar');
  if (bar) {
    bar.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('button') : null;
      if (!btn) { return; }
      var act = btn.getAttribute('data-act');
      if (act === 'range') { setLast(parseInt(btn.getAttribute('data-days'), 10)); }
      else if (act === 'all') { view = [0, N - 1]; render(); }
      else if (act === 'pan') { pan(parseInt(btn.getAttribute('data-dir'), 10)); }
    });
  }
  render();
})();
"""


def line_chart_block(df: pd.DataFrame) -> str:
    """可缩放的情绪指数走势图（纯内联 JS + SVG，无外部依赖）。

    交互：预设区间按钮、图上横向拖拽框选缩放、左右平移、双击复位、悬停查看当日数值。
    """
    if df.empty:
        return "<p>无历史数据</p>"
    work = df.dropna(subset=["afgi"]).copy()
    dates = [pd.to_datetime(d).strftime("%Y-%m-%d") for d in work["date"]]
    afgi = [round(float(v), 2) if pd.notna(v) else None for v in work["afgi"]]
    if "afgi_ma20" in work.columns:
        ma20 = [round(float(v), 2) if pd.notna(v) else None for v in work["afgi_ma20"]]
    else:
        ma20 = [None] * len(work)
    data_json = json.dumps({"dates": dates, "afgi": afgi, "ma20": ma20}, ensure_ascii=False)

    presets = [("近1月", 21), ("近3月", 63), ("近6月", 126), ("近1年", 244), ("近2年", 488)]
    buttons = "\n".join(
        f'    <button type="button" data-act="range" data-days="{days}"'
        f'{" class=\"active\"" if days == DEFAULT_RANGE_DAYS else ""}>{label}</button>'
        for label, days in presets
    )
    return f"""
<div class="chart-toolbar" id="afgiToolbar">
  <span class="toolbar-label">区间</span>
{buttons}
    <button type="button" data-act="all">全部</button>
    <span class="toolbar-sep"></span>
    <button type="button" data-act="pan" data-dir="-1" title="向左平移">‹</button>
    <button type="button" data-act="pan" data-dir="1" title="向右平移">›</button>
    <span class="toolbar-status" id="afgiRange"></span>
</div>
<div class="chart-host">
  <div id="afgiChart"></div>
  <div class="chart-tip" id="afgiTip"></div>
</div>
<div class="chart-hint">在图上横向拖拽可框选缩放，双击恢复全区间，鼠标悬停查看当日数值</div>
<script>{CHART_JS.replace("__DATA__", data_json).replace("__DEFAULT_DAYS__", str(DEFAULT_RANGE_DAYS))}</script>
"""


def component_bars_svg(rows: pd.DataFrame, width: int = 940) -> str:
    """九分项得分横向条形图。"""
    row_h, top, left = 34, 10, 150
    bar_w = width - left - 90
    height = top + row_h * len(rows) + 10
    parts = []
    for i, row in enumerate(rows.itertuples()):
        y = top + i * row_h
        score = float(row.score)
        color = _state_color(score)
        parts.append(
            f'<text x="{left - 10}" y="{y + 18}" font-size="13" fill="{C_TEXT}" text-anchor="end">'
            f'{html.escape(str(row.name))}</text>'
            f'<text x="{left - 10}" y="{y + 30}" font-size="10" fill="{C_SUB}" text-anchor="end">'
            f'权重 {row.weight:.0%}</text>'
            f'<rect x="{left}" y="{y + 6}" width="{bar_w}" height="16" rx="3" fill="{C_PANEL}"/>'
            f'<rect x="{left}" y="{y + 6}" width="{bar_w * score / 100:.1f}" height="16" rx="3" fill="{color}"/>'
            f'<text x="{left + bar_w + 8}" y="{y + 19}" font-size="13" font-weight="600" fill="{color}">'
            f'{score:.1f}</text>'
        )
    return f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">' + "\n".join(parts) + "</svg>"


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def _raw_table(latest: pd.Series) -> str:
    items = []
    for col, (name, direction, desc) in RAW_INDICATOR_META.items():
        raw = latest.get(col, np.nan)
        score = latest.get(f"{col}_score", np.nan)
        items.append(
            f"<tr><td>{html.escape(name)}</td><td>{html.escape(direction)}</td>"
            f"<td>{html.escape(desc)}</td>"
            f"<td class='num'>{_fmt(raw, 4)}</td>"
            f"<td class='num' style='color:{_state_color(float(score)) if pd.notna(score) else C_SUB}'>"
            f"{_fmt(score)}</td></tr>"
        )
    for col, (name, direction, desc) in CORE_RAW_META.items():
        raw = latest.get(col, np.nan)
        score_col = {"raw_market_volatility": "volatility_score",
                     "raw_amount_ratio": "volume_score",
                     "raw_new_high_ratio": "price_strength_score",
                     "raw_risk_appetite": "risk_appetite_score"}[col]
        score = latest.get(score_col, np.nan)
        items.append(
            f"<tr><td>{html.escape(name)}</td><td>{html.escape(direction)}</td>"
            f"<td>{html.escape(desc)}</td>"
            f"<td class='num'>{_fmt(raw, 4)}</td>"
            f"<td class='num' style='color:{_state_color(float(score)) if pd.notna(score) else C_SUB}'>"
            f"{_fmt(score)}</td></tr>"
        )
    return "\n".join(items)


def render_report(df: pd.DataFrame, output_path: Path, meta: dict | None = None) -> Path:
    meta = meta or {}
    if df.empty:
        raise ValueError("结果为空，无法生成报告")
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest
    score = float(latest["afgi"])
    state = str(latest["afgi_state"])
    change = score - float(prev["afgi"])
    rows = component_breakdown(latest)

    trade_date = pd.to_datetime(latest["date"]).strftime("%Y-%m-%d")
    start_date = pd.to_datetime(df["date"].iloc[0]).strftime("%Y-%m-%d")
    color = _state_color(score)
    change_color = C_GREED if change > 0 else (C_FEAR if change < 0 else C_SUB)
    change_text = f"{change:+.2f}"

    comp_rows = "\n".join(
        f"<tr><td>{html.escape(str(r['name']))}</td><td class='num'>{r['weight']:.0%}</td>"
        f"<td class='num' style='color:{_state_color(float(r['score']))};font-weight:600'>{r['score']:.2f}</td>"
        f"<td class='num'>{r['contribution']:.2f}</td>"
        f"<td>{html.escape(str(r['state']))}</td></tr>"
        for _, r in rows.iterrows()
    )

    highs = df.tail(250)
    stats = [
        ("区间最高", f"{highs['afgi'].max():.2f}", pd.to_datetime(highs.loc[highs['afgi'].idxmax(), 'date']).strftime("%Y-%m-%d")),
        ("区间最低", f"{highs['afgi'].min():.2f}", pd.to_datetime(highs.loc[highs['afgi'].idxmin(), 'date']).strftime("%Y-%m-%d")),
        ("区间均值", f"{highs['afgi'].mean():.2f}", f"{len(highs)} 个交易日"),
        ("样本区间", f"{len(df)} 个交易日", f"{start_date} ~ {trade_date}"),
    ]
    stat_html = "\n".join(
        f'<div class="stat"><div class="stat-label">{html.escape(k)}</div>'
        f'<div class="stat-value">{html.escape(v)}</div>'
        f'<div class="stat-sub">{html.escape(s)}</div></div>'
        for k, v, s in stats
    )

    universe_size = meta.get("universe_size", "")
    industry_desc = meta.get("industry_desc", "")

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>A 股市场情绪指数 {trade_date}</title>
<style>
  :root {{ --text:{C_TEXT}; --sub:{C_SUB}; --panel:{C_PANEL}; --grid:{C_GRID}; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:32px; background:#f2f4f7; color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:1000px; margin:0 auto; }}
  .card {{ background:{C_BG}; border:1px solid var(--grid); border-radius:12px; padding:24px; margin-bottom:20px; }}
  h1 {{ margin:0 0 4px; font-size:22px; }}
  .sub {{ color:var(--sub); font-size:13px; margin-bottom:20px; }}
  h2 {{ font-size:16px; margin:0 0 14px; padding-left:10px; border-left:3px solid {C_LINE}; }}
  .hero {{ display:flex; gap:32px; align-items:center; flex-wrap:wrap; }}
  .hero-info {{ flex:1; min-width:260px; }}
  .big {{ font-size:44px; font-weight:700; color:{color}; line-height:1.1; }}
  .badge {{ display:inline-block; padding:4px 12px; border-radius:999px; background:{color}; color:#fff;
            font-size:13px; margin-left:8px; vertical-align:middle; }}
  .delta {{ font-size:15px; color:{change_color}; margin-top:6px; }}
  .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:20px; }}
  .stat {{ background:var(--panel); border-radius:8px; padding:12px; }}
  .stat-label {{ font-size:12px; color:var(--sub); }}
  .stat-value {{ font-size:19px; font-weight:600; margin-top:2px; }}
  .stat-sub {{ font-size:11px; color:var(--sub); margin-top:2px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ padding:8px 10px; border-bottom:1px solid var(--grid); text-align:left; }}
  th {{ background:var(--panel); font-weight:600; color:var(--sub); font-size:12px; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .note {{ font-size:12px; color:var(--sub); line-height:1.7; }}
  .note b {{ color:var(--text); }}
  .chart-toolbar {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-bottom:12px; }}
  .chart-toolbar button {{ border:1px solid #dfe3e8; background:#fff; color:var(--text);
      border-radius:6px; padding:5px 11px; font-size:12px; cursor:pointer; font-family:inherit; }}
  .chart-toolbar button:hover {{ border-color:{C_LINE}; color:{C_LINE}; }}
  .chart-toolbar button.active {{ background:{C_LINE}; border-color:{C_LINE}; color:#fff; }}
  .toolbar-label {{ font-size:12px; color:var(--sub); }}
  .toolbar-sep {{ width:1px; height:18px; background:var(--grid); margin:0 4px; }}
  .toolbar-status {{ font-size:12px; color:var(--sub); margin-left:auto; font-variant-numeric:tabular-nums; }}
  .chart-host {{ position:relative; }}
  .chart-host svg {{ display:block; user-select:none; }}
  .chart-tip {{ position:absolute; display:none; pointer-events:none; background:#fff;
      border:1px solid #dfe3e8; border-radius:6px; padding:6px 10px; font-size:12px;
      line-height:1.6; box-shadow:0 2px 10px rgba(0,0,0,.10); white-space:nowrap; z-index:5; }}
  .chart-hint {{ font-size:11px; color:#9aa4b0; margin-top:8px; }}
  @media(max-width:720px){{ .stats{{grid-template-columns:repeat(2,1fr);}} body{{padding:16px;}} }}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>A 股市场情绪指数（AFGI）</h1>
    <div class="sub">数据源：通达信行情接口（pytdx） · 计算日 {trade_date} · 股票池 {universe_size} 只 A 股</div>
    <div class="hero">
      <div>{gauge_svg(score, state)}</div>
      <div class="hero-info">
        <div class="big">{score:.2f}<span class="badge">{html.escape(state)}</span></div>
        <div class="delta">较上一交易日 {change_text}</div>
        <div class="stats">{stat_html}</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>情绪指数走势</h2>
    {line_chart_block(df)}
  </div>

  <div class="card">
    <h2>九大分项</h2>
    {component_bars_svg(rows)}
    <table style="margin-top:18px">
      <thead><tr><th>分项</th><th class="num">权重</th><th class="num">得分</th>
      <th class="num">贡献</th><th>状态</th></tr></thead>
      <tbody>{comp_rows}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>底层原始指标</h2>
    <table>
      <thead><tr><th>指标</th><th>方向</th><th>说明</th><th class="num">原始值</th><th class="num">得分</th></tr></thead>
      <tbody>{_raw_table(latest)}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>数据与方法说明</h2>
    <div class="note">
      <p><b>数据源。</b>原仓库依赖 Tushare Pro（需 Token 且 ETF 日线接口要求 5000 积分）与 AkShare，
      两者在当前环境不可用。本实现改用<b>通达信公开行情服务器（TCP 协议，pytdx）</b>：
      全 A 个股日 K、指数日 K、50ETF/国债 ETF 日 K、申万行业指数日 K、除权除息记录全部取自通达信，
      无需 Token、无积分门槛、无调用频次限制。{html.escape(industry_desc)}。</p>
      <p><b>复权处理。</b>通达信日 K 为未复权价格，直接用 close/prev_close 会在除权日产生虚假暴跌。
      本实现按通达信除权除息记录（category=1）自行计算前复权因子，还原 OHLC 后再计算涨跌幅与涨跌停，
      避免把 10 送 10 误判成跌停。</p>
      <p><b>打分方式。</b>每个原始指标换算成它在自身过去 252 个交易日分布中的分位数（0-100），
      历史样本不足 60 日时取 50 分中性值；反向指标（波动率、炸板率、60 日新低占比）取 100 减去分位数。
      九分项按固定权重加权，总分裁剪到 0-100 后映射到极度恐惧 / 恐惧 / 中性 / 贪婪 / 极度贪婪五档。</p>
      <p><b>用途声明。</b>本项目仅用于量化研究与数据分析，不构成任何投资建议。</p>
    </div>
  </div>
</div>
</body>
</html>
"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_doc, encoding="utf-8")
    return output_path
