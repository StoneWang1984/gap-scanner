"""Stone 0.4.14 实盘监控 Dashboard — with real-time price charts"""

import json
import time
from pathlib import Path

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

VER_DIR = Path(__file__).parent
LOG_FILE = VER_DIR / "live_0414.log"
STATE_FILE = Path(__file__).parent.parent / "live_state.json"
REPORT_DIR = VER_DIR / "daily_reports"
CHART_FILE = VER_DIR / "chart_data.json"

MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"

st.set_page_config(page_title="Stone 0.4.14 Live", page_icon="📊", layout="wide")

st.title("Stone 0.4.14 实盘监控")
st.caption("Re-entry v2 | Auto Scheduler | Real-time Charts")


# ── Helpers ────────────────────────────────────────────────────────
def price_tickformat(price_min, price_max):
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


def ts_to_minutes(ts_str):
    """Convert 'HH:MM' to minutes since midnight (assumes EST)."""
    parts = ts_str.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def utc_ts_to_est_minutes(ts_str):
    """Convert UTC 'HH:MM' to minutes since midnight EST (EDT -4h offset)."""
    parts = ts_str.split(":")
    h, m = int(parts[0]), int(parts[1])
    est_h = (h - 4) % 24  # UTC-4 for EDT (summer); use -5 for EST (winter)
    return est_h * 60 + m


def build_time_ticks():
    """Generate tick marks every 30 min from 09:30 to 16:00."""
    ticks, labels = [], []
    start = ts_to_minutes(MARKET_OPEN)
    end = ts_to_minutes(MARKET_CLOSE)
    for m in range(start, end + 1, 30):
        ticks.append(m)
        labels.append(f"{m // 60:02d}:{m % 60:02d}")
    return ticks, labels


# ── Build line chart for one symbol ───────────────────────────────
def build_symbol_chart(sym, data):
    bars_1m = data.get("bars_1m", [])
    bars_5m = data.get("bars_5m", [])
    events = data.get("events", [])

    bars = bars_1m if bars_1m else bars_5m
    if not bars and not events:
        return None

    # Line: pure bar close prices
    line_x = [utc_ts_to_est_minutes(b["ts"]) for b in bars]
    line_y = [b["c"] for b in bars]

    # Volume
    vol_df = pd.DataFrame(bars) if bars else pd.DataFrame(columns=["ts", "v", "c", "o"])
    if not vol_df.empty:
        vol_df["x"] = vol_df["ts"].apply(utc_ts_to_est_minutes)

    # Price range — include event prices so arrows stay in view
    all_y = list(line_y)
    entry_price = data.get("entry_price", 0)
    open_price = bars[0]["o"] if bars else 0
    targets = data.get("targets", {})
    if entry_price: all_y.append(entry_price)
    if data.get("stop_price"): all_y.append(data["stop_price"])
    for p in targets.values(): all_y.append(p)
    if open_price: all_y.append(open_price)
    for evt in events:
        all_y.append(evt["price"])
    p_min, p_max = min(all_y), max(all_y)
    tfmt = price_tickformat(p_min, p_max)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.75, 0.25], vertical_spacing=0.04)

    # High-Low band (shows intra-bar range)
    band_x = [utc_ts_to_est_minutes(b["ts"]) for b in bars]
    band_high = [b["h"] for b in bars]
    band_low = [b["l"] for b in bars]
    fig.add_trace(go.Scatter(
        x=band_x + band_x[::-1],
        y=band_high + band_low[::-1],
        fill="toself", fillcolor="rgba(33,150,243,0.12)",
        line=dict(width=0), hoverinfo="skip", showlegend=False,
    ), row=1, col=1)

    # Price line (close)
    fig.add_trace(go.Scatter(
        x=line_x, y=line_y, mode="lines", name="Price",
        line=dict(color="#2196F3", width=1.5),
    ), row=1, col=1)

    # Volume
    if not vol_df.empty:
        colors = ["#26a69a" if vol_df["c"].iloc[i] >= vol_df["o"].iloc[i] else "#ef5350"
                  for i in range(len(vol_df))]
        fig.add_trace(go.Bar(
            x=vol_df["x"], y=vol_df["v"], marker_color=colors, name="Vol", opacity=0.5,
        ), row=2, col=1)

    # Reference lines — first trade
    target_colors = {"75%": "#FFA726", "112.5%": "#66BB6A", "150%": "#AB47BC"}
    ref_lines = []
    if open_price:
        ref_lines.append(("Open", open_price, "#9E9E9E", "dash"))
    if entry_price:
        ref_lines.append(("Entry", entry_price, "#2196F3", "dash"))
    if data.get("stop_price"):
        ref_lines.append(("Stop", data["stop_price"], "#F44336", "dash"))
    for label, price in targets.items():
        color = target_colors.get(label, "#4CAF50")
        ref_lines.append((f"{label}", price, color, "dot"))
    if data.get("reentry_target"):
        ref_lines.append(("RE Tgt", data["reentry_target"], "#FF9800", "dot"))

    # Reference lines — re-entry trades
    for ri, ref in enumerate(data.get("reentry_refs", [])):
        tag = f"RE{ri+1}"
        if ref.get("entry_price"):
            ref_lines.append((f"{tag} 入", ref["entry_price"], "#00897B", "dash"))
        if ref.get("stop_price"):
            ref_lines.append((f"{tag} 止", ref["stop_price"], "#E65100", "dash"))

    # Place reference lines with overlap avoidance
    # Group lines by price proximity, then assign positions round-robin within each group
    price_range = p_max - p_min if p_max > p_min else 1
    min_gap = price_range * 0.06  # 6% of price range
    ref_positions = ["top right", "bottom right", "top left", "bottom left"]

    # Sort by price, then group close-together lines
    sorted_refs = sorted(ref_lines, key=lambda x: x[1])
    groups = []
    if sorted_refs:
        current_group = [sorted_refs[0]]
        for i in range(1, len(sorted_refs)):
            if abs(sorted_refs[i][1] - sorted_refs[i-1][1]) < min_gap:
                current_group.append(sorted_refs[i])
            else:
                groups.append(current_group)
                current_group = [sorted_refs[i]]
        groups.append(current_group)

    for group in groups:
        for i, (name, price, color, dash) in enumerate(group):
            pos = ref_positions[i % len(ref_positions)]
            fig.add_hline(y=price, line_dash=dash, line_color=color, line_width=1,
                          annotation_text=f"{name} {price_fmt(price)}",
                          annotation_position=pos, annotation_font_size=10,
                          row=1, col=1)

    # Compute sell portions cumulatively — per trade group
    # Separate first-trade and re-entry events for portion calculation
    first_sell_events = [e for e in events if e["type"] == "sell" and e.get("trade_type") != "reentry"]
    reentry_sell_groups = {}
    for e in events:
        if e["type"] == "sell" and e.get("trade_type") == "reentry":
            reentry_sell_groups.setdefault("re", []).append(e)

    # First trade portions: 1/4@75%, 1/3@112.5%, 1/3@150%, rest at exit
    remaining = 100
    first_sell_portions = []
    for e in first_sell_events:
        l = e["label"].upper()
        if "TARGET_75" in l:
            sold = 25
        elif "TARGET_1125" in l or "TARGET_112" in l:
            sold = round(remaining / 3)
        elif "TARGET_150" in l:
            sold = round(remaining / 3)
        elif "TIER" in l and "1" in l:
            sold = remaining // 2
        else:
            sold = remaining
        first_sell_portions.append(sold)
        remaining -= sold

    # Re-entry portions (each re-entry is independent, 100%)
    re_sell_portions = {}
    for gk, g_events in reentry_sell_groups.items():
        re_remaining = 100
        portions = []
        for e in g_events:
            l = e["label"].upper()
            if "TIER" in l and "1" in l:
                sold = re_remaining // 2
            else:
                sold = re_remaining
            portions.append(sold)
            re_remaining -= sold
        re_sell_portions[gk] = portions

    # ── Annotation placement with overlap avoidance ──
    # Arrows always vertical (ax=0), text stacked at different heights
    placed_annotations = []  # (x_minutes, is_above)

    def _compute_ay(x_min, is_above):
        """Compute ay for vertical arrow, stagger text height when close in time."""
        MIN_TIME_GAP = 20  # minutes
        close_count = sum(1 for px, pa in placed_annotations
                          if abs(x_min - px) < MIN_TIME_GAP and pa == is_above)
        ay = (40 + close_count * 26) if is_above else -(40 + close_count * 26)
        placed_annotations.append((x_min, is_above))
        return ay

    # Buy markers — first trade (green) + re-entry (teal)
    buy_events = [e for e in events if e["type"] == "buy"]
    for e in buy_events:
        is_re = e.get("trade_type") == "reentry"
        e_x = utc_ts_to_est_minutes(e["ts"])
        e_y = e["price"]
        pct = (e["price"] - open_price) / open_price if open_price else 0
        if is_re:
            label = f"[再入]买 {price_fmt(e['price'])} {pct:+.1%}"
            arrow_color = "#00897B"
            font_color = "#004D40"
        else:
            label = f"买 {price_fmt(e['price'])} {pct:+.1%}"
            arrow_color = "#00C853"
            font_color = "#1B5E20"

        ay = _compute_ay(e_x, is_above=True)
        fig.add_annotation(
            x=e_x, y=e_y,
            text=label,
            showarrow=True, arrowhead=2, arrowsize=1.2, arrowwidth=2,
            arrowcolor=arrow_color,
            ax=0, ay=ay,
            font=dict(size=14, color=font_color),
            row=1, col=1,
        )

    # Sell markers — first trade (red) + re-entry (orange)
    sell_events = [e for e in events if e["type"] == "sell"]
    first_si = 0
    re_si = 0
    for e in sell_events:
        is_re = e.get("trade_type") == "reentry"
        l = e["label"].upper()
        e_x = utc_ts_to_est_minutes(e["ts"])
        e_y = e["price"]

        if is_re:
            portions = re_sell_portions.get("re", [])
            sold_pct = portions[re_si] if re_si < len(portions) else 100
            pct = (e["price"] - e.get("entry_price", entry_price)) / e.get("entry_price", entry_price) if entry_price else 0
            label = f"[再入]卖{sold_pct}% {price_fmt(e['price'])} {pct:+.1%}"
            arrow_color = "#E65100"
            font_color = "#BF360C"
            re_si += 1
        else:
            sold_pct = first_sell_portions[first_si] if first_si < len(first_sell_portions) else 100
            pct = (e["price"] - entry_price) / entry_price if entry_price else 0
            label = f"卖{sold_pct}% {price_fmt(e['price'])} {pct:+.1%}"
            arrow_color = "#FF1744"
            font_color = "#B71C1C"
            first_si += 1

        ay = _compute_ay(e_x, is_above=False)
        fig.add_annotation(
            x=e_x, y=e_y,
            text=label,
            showarrow=True, arrowhead=2, arrowsize=1.2, arrowwidth=2,
            arrowcolor=arrow_color,
            ax=0, ay=ay,
            font=dict(size=14, color=font_color),
            row=1, col=1,
        )

    # X-axis: full trading day 09:30–16:00
    tick_vals, tick_text = build_time_ticks()

    fig.update_layout(
        title=dict(text=f"{sym} — 价格走势", font=dict(size=14)),
        height=500, showlegend=False,
        xaxis_rangeslider_visible=False,
        yaxis_tickformat=tfmt,
        yaxis2_title="Vol", yaxis2_tickformat=".2s",
        margin=dict(l=60, r=30, t=50, b=40),
    )
    fig.update_xaxes(
        tickmode="array", tickvals=tick_vals, ticktext=tick_text,
        range=[ts_to_minutes(MARKET_OPEN), ts_to_minutes(MARKET_CLOSE)],
        row=1, col=1,
    )
    fig.update_xaxes(
        tickmode="array", tickvals=tick_vals, ticktext=tick_text,
        range=[ts_to_minutes(MARKET_OPEN), ts_to_minutes(MARKET_CLOSE)],
        row=2, col=1,
    )

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
    st.subheader("交易价格图表")

    # Date filter
    all_dates = sorted(set(
        v.get("date", "") for v in chart_data["symbols"].values() if v.get("date")
    ))
    if all_dates:
        date_options = ["全部"] + all_dates
        selected_date = st.selectbox("选择日期", date_options, index=0)
    else:
        selected_date = "全部"

    # Filter symbols by date
    syms = list(chart_data["symbols"].keys())
    if selected_date != "全部":
        syms = [s for s in syms if chart_data["symbols"][s].get("date") == selected_date]

    # ── Merge first trade + re-entry into one chart per symbol per day ──
    def _base_key(key):
        """'AXTX RE (7/6)' → 'AXTX (7/6)'"""
        return key.replace(" RE ", " ")

    grouped = {}
    for s in syms:
        bk = _base_key(s)
        is_re = " RE " in s
        if bk not in grouped:
            grouped[bk] = {"first": None, "re_entries": []}
        if is_re:
            grouped[bk]["re_entries"].append(chart_data["symbols"][s])
        else:
            grouped[bk]["first"] = chart_data["symbols"][s]

    merged_list = []
    for bk, grp in grouped.items():
        first = grp["first"]
        re_entries = grp["re_entries"]
        if first is None:
            first = re_entries.pop(0)
        merged_events = list(first.get("events", []))
        for re_d in re_entries:
            for evt in re_d.get("events", []):
                evt_copy = dict(evt)
                evt_copy["trade_type"] = "reentry"
                merged_events.append(evt_copy)
        merged = dict(first)
        merged["events"] = merged_events
        merged["pnl"] = first.get("pnl", 0) + sum(re_d.get("pnl", 0) for re_d in re_entries)
        if re_entries:
            merged["reentry_refs"] = [
                {"entry_price": re_d.get("entry_price"), "stop_price": re_d.get("stop_price")}
                for re_d in re_entries
            ]
        merged_list.append((bk, merged))

    # P&L summary (use merged data — one entry per symbol per day)
    total_pnl = sum(d.get("pnl", 0) for _, d in merged_list)
    wins = sum(1 for _, d in merged_list if d.get("pnl", 0) > 0)
    total_count = len(merged_list)
    if total_count > 0:
        c1, c2, c3 = st.columns(3)
        c1.metric("交易数", str(total_count))
        c2.metric("胜率", f"{wins/total_count:.0%}" if total_count else "—")
        c3.metric("合计 P&L", f"${total_pnl:+,.2f}")

    # Draw charts
    for sym_key, data in merged_list:
        fig = build_symbol_chart(sym_key, data)
        if fig:
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info(f"{sym_key}: 暂无价格数据")
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
