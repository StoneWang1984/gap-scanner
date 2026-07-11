"""Stone 0.4.10 实盘监控 Dashboard — with real-time price charts"""

import json
import time
from pathlib import Path

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

VER_DIR = Path(__file__).parent
LOG_FILE = VER_DIR / "live_0410.log"
STATE_FILE = Path(__file__).parent.parent / "live_state.json"
REPORT_DIR = VER_DIR / "daily_reports"
CHART_FILE = VER_DIR / "chart_data.json"

st.set_page_config(page_title="Stone 0.4.10 Live", page_icon="📊", layout="wide")

st.title("Stone 0.4.10 实盘监控")
st.caption("Re-entry v2 | Auto Scheduler | Real-time Charts")


# ── Helper: price tick format ─────────────────────────────────────
def price_tickformat(price_min, price_max):
    rng = price_max - price_min
    if price_max < 2:
        return ".4f"
    if price_max < 10:
        return ".3f"
    return ".2f"


def price_fmt(price):
    if price is None:
        return "—"
    if abs(price) < 2:
        return f"${price:.4f}"
    if abs(price) < 10:
        return f"${price:.3f}"
    return f"${price:.2f}"


# ── Build chart for one symbol ────────────────────────────────────
def build_symbol_chart(sym, data, chart_type="5min"):
    bars = data.get("bars_5m", []) if chart_type == "5min" else data.get("bars_1m", [])
    events = data.get("events", [])

    if not bars:
        return None

    df = pd.DataFrame(bars)
    all_prices = list(df["o"]) + list(df["h"]) + list(df["l"]) + list(df["c"])
    p_min, p_max = min(all_prices), max(all_prices)
    tfmt = price_tickformat(p_min, p_max)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.75, 0.25], vertical_spacing=0.03)

    if chart_type == "5min":
        fig.add_trace(go.Candlestick(
            x=df["ts"], open=df["o"], high=df["h"], low=df["l"], close=df["c"],
            name="5min", increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
        ), row=1, col=1)
    else:
        fig.add_trace(go.Scatter(
            x=df["ts"], y=df["c"], mode="lines", name="1min close",
            line=dict(color="#2196F3", width=1.5),
        ), row=1, col=1)

    # Volume
    colors = ["#26a69a" if df["c"].iloc[i] >= df["o"].iloc[i] else "#ef5350"
              for i in range(len(df))]
    fig.add_trace(go.Bar(
        x=df["ts"], y=df["v"], marker_color=colors, name="Vol", opacity=0.6,
    ), row=2, col=1)

    # Reference lines: entry, stop, targets
    ref_lines = []
    if "entry_price" in data:
        ref_lines.append(("Entry", data["entry_price"], "#2196F3", "dash"))
    if "stop_price" in data:
        ref_lines.append(("Stop", data["stop_price"], "#F44336", "dash"))
    targets = data.get("targets", {})
    for label, price in targets.items():
        ref_lines.append((f"Tgt {label}", price, "#4CAF50", "dot"))
    if "reentry_target" in data:
        ref_lines.append(("RE Tgt", data["reentry_target"], "#FF9800", "dot"))

    for name, price, color, dash in ref_lines:
        fig.add_hline(y=price, line_dash=dash, line_color=color, line_width=1,
                      annotation_text=f"{name} {price_fmt(price)}",
                      annotation_position="top left", annotation_font_size=9,
                      row=1, col=1)

    # Buy / sell markers
    buy_events = [e for e in events if e["type"] == "buy"]
    sell_events = [e for e in events if e["type"] == "sell"]

    if buy_events:
        fig.add_trace(go.Scatter(
            x=[e["ts"] for e in buy_events],
            y=[e["price"] for e in buy_events],
            mode="markers+text",
            marker=dict(symbol="triangle-up", size=14, color="#00C853",
                        line=dict(width=1, color="#1B5E20")),
            text=[e["label"] for e in buy_events],
            textposition="bottom center", textfont=dict(size=8, color="#1B5E20"),
            name="Buy", hovertemplate="%{text}<br>%{y}",
        ), row=1, col=1)

    if sell_events:
        fig.add_trace(go.Scatter(
            x=[e["ts"] for e in sell_events],
            y=[e["price"] for e in sell_events],
            mode="markers+text",
            marker=dict(symbol="triangle-down", size=14, color="#FF1744",
                        line=dict(width=1, color="#B71C1C")),
            text=[e["label"] for e in sell_events],
            textposition="top center", textfont=dict(size=8, color="#B71C1C"),
            name="Sell", hovertemplate="%{text}<br>%{y}",
        ), row=1, col=1)

    fig.update_layout(
        title=dict(text=f"{sym} — {chart_type.upper()}", font=dict(size=14)),
        height=420, showlegend=False,
        xaxis_rangeslider_visible=False,
        yaxis_tickformat=tfmt,
        yaxis2_title="Vol", yaxis2_tickformat=".2s",
        margin=dict(l=50, r=20, t=40, b=30),
    )
    fig.update_xaxes(type="category", row=1, col=1)
    fig.update_xaxes(type="category", row=2, col=1)

    return fig


# ── Real-time price charts ────────────────────────────────────────
chart_data = None
if CHART_FILE.exists():
    try:
        with open(CHART_FILE) as f:
            chart_data = json.load(f)
    except Exception:
        chart_data = None

if chart_data and chart_data.get("symbols"):
    st.subheader("实时价格图表")

    # Chart type selector
    chart_type = st.radio("图表类型", ["5min 蜡烛图", "1min 折线图"], horizontal=True, index=0)
    ct = "5min" if "5min" in chart_type else "1min"

    syms = list(chart_data["symbols"].keys())
    # Layout: up to 3 columns
    cols = st.columns(min(len(syms), 3))
    for i, sym in enumerate(syms):
        with cols[i % len(cols)]:
            fig = build_symbol_chart(sym, chart_data["symbols"][sym], ct)
            if fig:
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info(f"{sym}: 暂无K线数据")
else:
    st.info("等待交易数据生成图表（chart_data.json）")

# ── 历史日终报告 ──────────────────────────────────────────────────
if REPORT_DIR.exists():
    report_files = sorted(REPORT_DIR.glob("*.json"), reverse=True)[:10]
    if report_files:
        with st.expander("历史交易日报"):
            summary_rows = []
            for rf in report_files:
                try:
                    with open(rf) as f:
                        r = json.load(f)
                    summary_rows.append({
                        "日期": r.get("date", rf.stem),
                        "交易数": r.get("daily_trades", 0),
                        "胜率": f"{r.get('win_rate', 0):.0%}",
                        "Daily P&L": f"${r.get('daily_pnl', 0):+,.2f}",
                        "收盘权益": f"${r.get('account_equity_end', 0):,.2f}",
                    })
                except Exception:
                    pass
            if summary_rows:
                st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)

# ── 实盘状态 ──────────────────────────────────────────────────────
state = None
if STATE_FILE.exists():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        state = None

if state:
    version = state.get("version", "?")
    daily_trades = state.get("daily_trades", 0)
    daily_stopped = state.get("daily_stopped", False)
    updated = state.get("updated", "")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("版本", version)
    c2.metric("今日交易", str(daily_trades))
    c3.metric("状态", "已停止" if daily_stopped else "交易中")
    c4.metric("最后更新", updated)

    with st.expander("选股 / 持仓 / 事件日志"):
        if state.get("candidates"):
            st.markdown("**今日选股**")
            rows = []
            day_highs = state.get("day_highs", {})
            for c in state["candidates"]:
                sym = c["symbol"]
                high = day_highs.get(sym)
                rows.append({
                    "股票": sym, "跳空": f"+{c['gap_pct']:.1%}",
                    "开盘": f"${c['open_price']:.4f}",
                    "昨收": f"${c['prev_close']:.4f}",
                    "日最高": f"${high:.4f}" if high else "—",
                })
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        if state.get("positions"):
            st.markdown("**当前持仓**")
            pos_rows = []
            for p in state["positions"]:
                extra = ""
                if p.get("trade_type") == "reentry":
                    t1 = "Y" if p.get("reached_target1") else "N"
                    be = "Y" if p.get("breakeven_active") else "N"
                    bars = p.get("reentry_bar_count", 0)
                    extra = f" | t1={t1} be={be} bars={bars}"
                pos_rows.append({
                    "股票": p["symbol"], "类型": p["trade_type"],
                    "数量": p["remaining_shares"],
                    "入场价": f"${p['entry_price']:.4f}",
                    "止损": f"${p['stop_price']:.4f}",
                    "信息": extra,
                })
            st.dataframe(pd.DataFrame(pos_rows), hide_index=True, use_container_width=True)
        else:
            st.info("当前无持仓")

        if state.get("events"):
            st.markdown("**事件日志 (最近20条)**")
            for evt in reversed(state["events"][-20:]):
                if "BUY" in evt or "FILLED" in evt or "TARGET" in evt:
                    st.success(evt)
                elif "STOP" in evt or "TRAILING" in evt or "FORCE" in evt or "CLOSE" in evt:
                    st.error(evt)
                elif "SELL" in evt or "PARTIAL" in evt:
                    st.warning(evt)
                else:
                    st.info(evt)
else:
    st.warning("未找到 live_state.json")

# ── 实时日志 ──────────────────────────────────────────────────────
with st.expander("实时日志"):
    if LOG_FILE.exists():
        with open(LOG_FILE) as f:
            lines = f.readlines()
        tail = lines[-60:] if len(lines) > 60 else lines
        st.code("".join(tail), language="log")
    else:
        st.info("日志文件尚未生成")

# ── Auto refresh ──────────────────────────────────────────────────
st.divider()
auto = st.checkbox("自动刷新 (5秒)", value=True)
if auto:
    time.sleep(5)
    st.rerun()
