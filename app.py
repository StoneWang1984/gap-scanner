"""Stone 1.2 策略 — Streamlit Web UI (交易显示 + 回测)"""

import json
import time
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

# Use stonewang_daytrade_1.2 config and modules
VERSION_DIR = Path("/Users/stonewang2014/gap-scanner/stonewang_daytrade_rtg_1.0")
STATE_FILE = Path("/Users/stonewang2014/gap-scanner/live_state.json")
import importlib.util, sys
_spec = importlib.util.spec_from_file_location("config", VERSION_DIR / "config.py")
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

st.set_page_config(page_title="RTG 1.0 交易", page_icon="📊", layout="wide")

# ── Sidebar ──────────────────────────────────────────────────────

st.sidebar.title("RTG 1.0 交易")
st.sidebar.caption("Red-to-Green · RVOL Adaptive · 1hr Window")

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

    equity = bp = cash = lmv = last_equity = 0.0
    alpaca_positions = []
    acct = None

    try:
        from alpaca.trading.client import TradingClient
        tc = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
                           paper=config.ALPACA_PAPER)
        acct = tc.get_account()
        alpaca_positions = tc.get_all_positions()
        equity = float(acct.equity)
        last_equity = float(acct.last_equity)
        bp = float(acct.buying_power)
        cash = float(acct.cash)
        lmv = float(acct.long_market_value)
    except Exception as e:
        equity = config.INITIAL_CAPITAL
        st.warning(f"无法连接 Alpaca API: {e}")

    pnl = equity - last_equity if last_equity > 0 else 0.0
    pnl_pct = pnl / last_equity if last_equity > 0 else 0.0
    a1, a2, a3, a4, a5 = st.columns(5)
    a1.metric("权益", f"${equity:,.2f}")
    a2.metric("购买力", f"${bp:,.2f}")
    a3.metric("现金", f"${cash:,.2f}")
    a4.metric("持仓市值", f"${lmv:,.2f}")
    a5.metric("当日盈亏", f"${pnl:+,.2f}",
              delta=f"{pnl_pct:+.1%}" if last_equity > 0 else "")

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

    # ── 今日交易汇总 (按股票聚合) ──
    st.divider()
    st.subheader("今日交易汇总")
    if state and state.get("trades_detail"):
        st.divider()
        st.subheader("今日交易汇总")

        from collections import OrderedDict
        stock_trades = OrderedDict()
        for t in state["trades_detail"]:
            sym = t["symbol"]
            if sym not in stock_trades:
                stock_trades[sym] = []
            stock_trades[sym].append(t)

        summary_rows = []
        total_pnl = 0
        for sym, trades in stock_trades.items():
            total_shares = sum(t.get("shares", 0) for t in trades)
            total_pnl_sym = sum(t.get("pnl", 0) for t in trades)
            total_pnl += total_pnl_sym
            entry_price = trades[0].get("entry", 0)
            exit_price = trades[-1].get("exit", 0)
            trade_type = trades[0].get("type", "first")
            final_reason = trades[-1].get("exit_reason", "")
            all_reasons = [t.get("exit_reason", "") for t in trades]
            entry_cost = entry_price * total_shares if entry_price > 0 else 0
            pnl_pct = (total_pnl_sym / entry_cost) if entry_cost > 0 else 0

            reason_display = final_reason.replace("_", " ").title()
            if len(set(all_reasons)) > 1:
                reason_display += f" ({len(trades)}档)"

            summary_rows.append({
                "股票": sym,
                "类型": trade_type,
                "买入价": f"${entry_price:.4f}",
                "卖出价": f"${exit_price:.4f}",
                "股数": total_shares,
                "盈亏": f"${total_pnl_sym:+,.2f}",
                "盈亏%": f"{pnl_pct:+.1%}",
                "退出类型": reason_display,
            })

        st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)
        st.metric("今日总盈亏", f"${total_pnl:+,.2f}")
    else:
        st.info("今日暂无已完成交易")

    # ── 今日交易明细 (逐笔) ──
    st.divider()
    st.subheader("今日交易明细 (逐笔)")
    if state and state.get("trades_detail"):
        trade_rows = []
        for t in state["trades_detail"]:
            pnl_val = t.get("pnl", 0)
            trade_rows.append({
                "股票": t["symbol"],
                "类型": t.get("type", "first"),
                "入场": f"${t.get('entry', 0):.4f}",
                "出场": f"${t.get('exit', 0):.4f}",
                "股数": t.get("shares", 0),
                "盈亏": f"${pnl_val:+,.2f}",
                "退出原因": t.get("exit_reason", "").replace("_", " ").title(),
            })

        st.dataframe(pd.DataFrame(trade_rows), hide_index=True, use_container_width=True)
    else:
        st.info("今日暂无交易记录")

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
    st.title("Stone 1.2 策略概览")

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

        st.subheader("仓位管理")
        max_daily = config.MAX_DAILY_TRADES if config.MAX_DAILY_TRADES > 0 else "无限制"
        st.markdown(f"""
        - 初始资金: **${config.INITIAL_CAPITAL:,.2f}**
        - 每仓: **${config.MIN_POSITION_SIZE}** ~ **${config.MAX_POSITION_SIZE}**
        - 最大同时持仓: **{config.MAX_POSITIONS_PER_DAY}** 只
        - 每日最大交易: **{max_daily}** 笔
        - 候选股: **Top {config.MAX_CANDIDATES}**
        """)

    with col2:
        st.subheader("入场规则 (1-min Pullback)")
        st.markdown(f"""
        - 1分钟K线折返确认: 跌破open_price → 3根bar确认底部 → 市价入场
        - 入场窗口: **9:31 ~ 10:30 EST** (1小时)
        - 入场价 < open_price (ENTRY_BELOW_OPEN过滤)
        - 再入场: 止损后禁止 | 回调≥{config.REENTRY_MIN_PULLBACK:.0%}才允许
        """)

        st.subheader("出场规则 (6-tier Ladder)")
        tiers = config.PROFIT_RETRACEMENT_TIERS
        caps = config.TARGET_CAP_TIERS
        trails = config.TRAILING_STOP_PCTS
        tier_lines = []
        for i in range(len(tiers)):
            tier_lines.append(f"  T{i+1}: {tiers[i]:.0%}回撤 / {caps[i]:.0%}封顶 / trail {trails[i]:.1%}")
        tier_str = "\n".join(tier_lines)
        st.markdown(f"""
        - 6档阶梯卖出 (每档1/8仓位, 共75%):
        {tier_str}
        - ATR止损: ×{config.STOP_LOSS_ATR_MULT} (封顶{config.STOP_LOSS_MAX_PCT:.0%})
        - 时间限制: **{config.FIRST_TRADE_TIME_LIMIT_BARS * 5}分钟** (无T1成交→breakeven退出)
        - EOD强制平仓: **{config.FORCE_CLOSE_TIME}** EST
        - 日损失熔断: **{config.MAX_DAILY_LOSS_PCT:.0%}**
        """)

    st.divider()
    st.subheader("Stone 1.2 设计理念")
    st.markdown("""
    - **Gap Pullback**: 跳空低开股跌破开盘价后确认底部折返 → 市价入场
    - **6-tier OCO Ladder Sell**: T1 polling成交后预挂OCO单(T2-T6)，消除5秒轮询延迟
    - **ATR自适应止损**: ATR×2初始止损，封顶10%最大亏损
    - **Progressive Trailing Stop**: 2%→5%逐档放宽，锁住利润同时让赢家奔跑
    - **Re-entry半仓**: 首笔退出后可二次入场(半仓)，1% trailing stop，止损后禁止
    - **日损失熔断5%**: 累计PnL超5% equity当日停交易
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
