"""stonewang_daytrade_rtg_1.0 策略 — Streamlit Web UI (交易显示 + 回测)"""

import json
import time
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

# Use stonewang_daytrade_rtg_1.0 config and modules
VERSION_DIR = Path("/Users/stonewang2014/gap-scanner/stonewang_daytrade_rtg_1.0")
STATE_FILE = Path("/Users/stonewang2014/gap-scanner/live_state.json")
import importlib.util, sys
_spec = importlib.util.spec_from_file_location("config", VERSION_DIR / "config.py")
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

st.set_page_config(page_title="stonewang_daytrade_rtg_1.0 交易", page_icon="📊", layout="wide")

# ── Sidebar ──────────────────────────────────────────────────────

st.sidebar.title("stonewang_daytrade_rtg_1.0 交易")
st.sidebar.caption("RTG Vol Breakout · RVOL集中仓位 · 自适应退出")

tab = st.sidebar.radio("导航", ["实盘交易", "策略概览", "交易详情"])

# ══════════════════════════════════════════════════════════════════
# Tab 1: 实盘交易 (核心页面)
# ══════════════════════════════════════════════════════════════════

if tab == "实盘交易":
    st.title("实盘交易")

    state_file = STATE_FILE
    state = None
    if state_file.exists():
        try:
            with open(state_file) as f:
                state = json.load(f)
        except Exception:
            state = None

    # ── 账户资金 ──
    st.subheader("账户资金")

    equity = config.INITIAL_CAPITAL
    bp = cash = lmv = 0.0
    alpaca_positions = []
    acct = None

    try:
        from alpaca.trading.client import TradingClient
        tc = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
                           paper=config.ALPACA_PAPER)
        acct = tc.get_account()
        alpaca_positions = tc.get_all_positions()
        equity = float(acct.equity)
        bp = float(acct.buying_power)
        cash = float(acct.cash)
        lmv = float(acct.long_market_value)
    except Exception as e:
        st.warning(f"无法连接 Alpaca API: {e}")

    pnl = equity - config.INITIAL_CAPITAL
    a1, a2, a3, a4, a5 = st.columns(5)
    a1.metric("权益", f"${equity:,.2f}")
    a2.metric("购买力", f"${bp:,.2f}")
    a3.metric("现金", f"${cash:,.2f}")
    a4.metric("持仓市值", f"${lmv:,.2f}")
    a5.metric("当日盈亏", f"${pnl:+,.2f}",
              delta=f"{pnl/config.INITIAL_CAPITAL:.1%}" if equity > 0 else "")

    # ── 数据源状态 ──
    if state:
        version = state.get("version", "?")
        daily_trades = state.get("daily_trades", 0)
        st.caption(f"v{version} | {config.DATA_FEED} | 今日 {daily_trades} 笔")

    # ── 当前持仓 ──
    st.divider()
    st.subheader("当前持仓")

    if alpaca_positions:
        state_positions = {p["symbol"]: p for p in state.get("positions", [])} if state else {}

        pos_rows = []
        for p in alpaca_positions:
            sym = p.symbol
            cur = float(p.current_price)
            entry = float(p.avg_entry_price)
            upnl = float(p.unrealized_pl)
            upnl_pct = float(p.unrealized_plpc)
            qty = int(float(p.qty))

            row = {
                "股票": sym,
                "数量": qty,
                "入场价": f"${entry:.4f}",
                "现价": f"${cur:.4f}",
                "盈亏": f"${upnl:+,.2f}",
                "盈亏%": f"{upnl_pct:+.1%}",
            }

            sp = state_positions.get(sym, {})
            if sp:
                row["信号"] = sp.get("signal_type", "rtg")
                row["RVOL"] = f"{sp.get('rvol', 0):.1f}×"
                row["止损%"] = f"{sp.get('stop_pct', 0):.0%}"
                row["目标%"] = f"{sp.get('target_pct', 0):.0%}"

            pos_rows.append(row)

        st.dataframe(pd.DataFrame(pos_rows), hide_index=True, use_container_width=True)
    elif state and state.get("positions"):
        pos_rows = []
        for p in state["positions"]:
            pos_rows.append({
                "股票": p["symbol"],
                "信号": p.get("signal_type", "rtg"),
                "数量": p.get("shares", 0),
                "入场价": f"${p.get('entry_price', 0):.4f}",
                "RVOL": f"{p.get('rvol', 0):.1f}×",
                "止损%": f"{p.get('stop_pct', 0):.0%}",
                "目标%": f"{p.get('target_pct', 0):.0%}",
            })
        st.dataframe(pd.DataFrame(pos_rows), hide_index=True, use_container_width=True)
    else:
        st.info("当前无持仓")

    # ── 今日候选股 ──
    if state and state.get("candidates"):
        st.divider()
        st.subheader("今日候选股")

        cand_rows = []
        for c in state["candidates"]:
            cand_rows.append({
                "股票": c["symbol"],
                "跳空": f"+{c['gap_pct']:.1%}",
                "RVOL": f"{c.get('rvol', 0):.1f}×",
                "开盘": f"${c['open_price']:.4f}",
                "昨收": f"${c['prev_close']:.4f}",
            })
        st.dataframe(pd.DataFrame(cand_rows), hide_index=True, use_container_width=True)

    # ── 今日交易明细 ──
    if state and state.get("trades_detail"):
        st.divider()
        st.subheader("今日交易明细")

        trade_rows = []
        total_pnl = 0
        for t in state["trades_detail"]:
            pnl_val = t.get("pnl", 0)
            total_pnl += pnl_val
            trade_rows.append({
                "股票": t["symbol"],
                "类型": t.get("trade_type", "first"),
                "入场": f"${t.get('entry', 0):.4f}",
                "出场": f"${t.get('exit', 0):.4f}",
                "股数": t.get("shares", 0),
                "盈亏": f"${pnl_val:+,.2f}",
                "原因": t.get("reason", ""),
            })

        st.dataframe(pd.DataFrame(trade_rows), hide_index=True, use_container_width=True)
        st.metric("今日总盈亏", f"${total_pnl:+,.2f}")

    if not state:
        st.warning("未找到 live_state.json，实盘未运行")

    # ── Auto refresh ──
    st.divider()
    auto_refresh = st.checkbox("自动刷新 (30秒)", value=True)
    if auto_refresh:
        time.sleep(30)
        st.rerun()

# ══════════════════════════════════════════════════════════════════
# Tab 2: 策略概览
# ══════════════════════════════════════════════════════════════════

elif tab == "策略概览":
    st.title("stonewang_daytrade_rtg_1.0 策略概览")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("扫描条件")
        st.markdown(f"""
        - 跳空幅度 > **{config.GAP_THRESHOLD:.0%}**
        - 盘前成交量 > **{config.MIN_VOLUME:,}** 股
        - 最低成交额 > **${config.MIN_DOLLAR_VOLUME:,.0f}**
        - 价格区间 **${config.PRICE_MIN}** ~ **${config.PRICE_MAX}**
        - 杠杆ETF过滤: **启用**
        - 候选股: **Top {config.MAX_CANDIDATES} by RVOL**
        """)

        st.subheader("仓位管理 (RVOL加权)")
        sizing_tiers = getattr(config, "RVOL_SIZING_TIERS", [])
        sizing_str = "\n".join(f"  RVOL>{r:.0f}× → {p:.0%} of equity (同档均分)" for r, p in sizing_tiers) if sizing_tiers else "  (default)"
        st.markdown(f"""
        - 初始资金: **${config.INITIAL_CAPITAL:,.2f}**
        - 仓位分配 (同档候选均分):
        {sizing_str}
        - 最大同时持仓: **{config.MAX_POSITIONS}** 只
        - 每日最大交易: **{config.MAX_DAILY_TRADES}** 笔
        """)

    with col2:
        st.subheader("入场规则 (Red-to-Green)")
        st.markdown(f"""
        - RTG信号: close > open_price + vol >= **{config.RTG_VOLUME_MULT}×** prior + vol >= **{config.RTG_MIN_VOLUME:,}** (RVOL自适应)
        - RVOL≥10× 最低量: **{max(config.RTG_MIN_VOLUME // 3, 5000):,}** | RVOL≥5×: **{max(config.RTG_MIN_VOLUME // 2, 10000):,}**
        - 入场价: open_price × 1.001 (比信号bar收盘更优)
        - 入场时间: **{config.ENTRY_WINDOW_START} ~ {config.ENTRY_WINDOW_END} EST**
        - 再入场: **{'无限制' if config.RTG_REENTRY_MAX >= 99 else '允许 (max ' + str(config.RTG_REENTRY_MAX) + '次)'}**
        """)

        st.subheader("出场规则 (RVOL自适应)")
        exit_tiers = getattr(config, "RVOL_EXIT_TIERS", [])
        if exit_tiers:
            tier_lines = []
            for rvol_min, stop, target, trail_act, trail in exit_tiers:
                tier_lines.append(f"  RVOL>{rvol_min:.0f}×: 止损{stop:.0%}, 目标{target:.0%}, 追踪+{trail_act:.0%}/{trail:.0%}")
            tier_str = "\n".join(tier_lines)
        else:
            tier_str = "  (default)"
        st.markdown(f"""
        - 按RVOL分档:
        {tier_str}
        - 时间限制: **{config.RTG_TIME_LIMIT_SEC // 60}分钟**
        - EOD强制平仓: **{config.FORCE_CLOSE_TIME}** EST
        - 日损失熔断: **{config.MAX_DAILY_LOSS_PCT:.0%}**
        """)

    st.divider()
    st.subheader("rtg_1.0 设计理念")
    st.markdown("""
    - **Red-to-Green Volume Breakout**: 跳空股跌破开盘后收回(open_price) + 成交量放大 → 入场
    - **RVOL加权仓位(同档均分)**: 同档候选股均分档位权益，避免购买力冲突
    - **RVOL自适应最低量**: 高RVOL(≥10×)放宽至10000，避免过滤低流动性高RVOL股
    - **自适应退出**: 高RVOL宽止损(7%)+大目标(50%)，低RVOL紧止损(3%)+小目标(15%)
    - **追踪止损3%/2%**: +3%激活追踪，2% trailing — 锁住利润同时让赢家奔跑
    - **无限再入场**: 止盈退出后若再次出现RTG信号可无限次再入场
    - **SIP数据源**: 覆盖100%成交量
    """)

# ══════════════════════════════════════════════════════════════════
# Tab 3: 交易详情
# ══════════════════════════════════════════════════════════════════

elif tab == "交易详情":
    st.title("交易详情")

    state_file = STATE_FILE
    state = None
    if state_file.exists():
        try:
            with open(state_file) as f:
                state = json.load(f)
        except Exception:
            state = None

    if not state or not state.get("trades_detail"):
        st.info("今日无交易记录")
        st.stop()

    trades = state["trades_detail"]
    trade_options = [f"#{i+1} {t['symbol']} ({t.get('reason', '?')}) P&L ${t.get('pnl', 0):+.2f}"
                     for i, t in enumerate(trades)]
    selected_idx = st.selectbox("选择交易", range(len(trade_options)),
                                format_func=lambda i: trade_options[i])

    t = trades[selected_idx]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("入场价", f"${t.get('entry', 0):.4f}")
    c2.metric("出场价", f"${t.get('exit', 0):.4f}")
    c3.metric("股数", f"{t.get('shares', 0):,}")
    c4.metric("盈亏", f"${t.get('pnl', 0):+,.2f}")

    c5, c6 = st.columns(2)
    c5.metric("出场原因", t.get("reason", "").replace("_", " ").title())
    c6.metric("类型", t.get("trade_type", "first"))

    st.divider()
    st.subheader("全部交易")
    trade_rows = []
    for t in trades:
        trade_rows.append({
            "股票": t["symbol"],
            "类型": t.get("trade_type", "first"),
            "入场": f"${t.get('entry', 0):.4f}",
            "出场": f"${t.get('exit', 0):.4f}",
            "股数": t.get("shares", 0),
            "盈亏": f"${t.get('pnl', 0):+,.2f}",
            "原因": t.get("reason", ""),
        })
    st.dataframe(pd.DataFrame(trade_rows), hide_index=True, use_container_width=True)
