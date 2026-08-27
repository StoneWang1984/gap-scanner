"""RTG 3.0 策略 — Streamlit Web UI (交易显示 + 回测)"""

import json
import time
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

VERSION_DIR = Path("/Users/stonewang2014/gap-scanner/stonewang_daytrade_rtg_3.0")
STATE_FILE = Path("/Users/stonewang2014/gap-scanner/live_rtg3_state.json")
import importlib.util, sys
_spec = importlib.util.spec_from_file_location("config", VERSION_DIR / "config.py")
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

st.set_page_config(page_title="RTG 3.0 交易", page_icon="📊", layout="wide")

# ── Sidebar ──────────────────────────────────────────────────────

st.sidebar.title("RTG 3.0 交易")
st.sidebar.caption("Cycle-based All-in + 1% Trailing Stop")

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
        cycle_idx = state.get("cycle_index", "?")
        next_scan = state.get("next_scan_time", "")
        status = f"v{version} | {config.DATA_FEED} | 今日 {daily_trades} 笔 | 周期 #{cycle_idx}"
        if next_scan:
            status += f" | 下次扫描: {next_scan[11:16]}"
        st.caption(status)

    # ── 当前持仓 ──
    st.divider()
    st.subheader("当前持仓")

    # rtg_3.0 uses single position (dict), not array
    state_position = state.get("position") if state else None
    state_positions_list = [state_position] if state_position else (state.get("positions", []) if state else [])
    state_positions = {p["symbol"]: p for p in state_positions_list}

    if alpaca_positions:
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
                row["Trail"] = f"{sp.get('trail_pct', 0):.1%}"

            pos_rows.append(row)

        st.dataframe(pd.DataFrame(pos_rows), hide_index=True, use_container_width=True)
    elif state_positions_list:
        pos_rows = []
        for p in state_positions_list:
            pos_rows.append({
                "股票": p["symbol"],
                "信号": p.get("signal_type", "rtg"),
                "数量": p.get("shares", 0),
                "入场价": f"${p.get('entry_price', 0):.4f}",
                "RVOL": f"{p.get('rvol', 0):.1f}×",
                "Trail": f"{p.get('trail_pct', 0):.1%}",
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
            final_reason = trades[-1].get("exit_reason", "") or trades[-1].get("reason", "")
            all_reasons = [t.get("exit_reason", "") or t.get("reason", "") for t in trades]
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
            reason = t.get("exit_reason", "") or t.get("reason", "")
            trade_rows.append({
                "股票": t["symbol"],
                "类型": t.get("type", "first"),
                "入场": f"${t.get('entry', 0):.4f}",
                "出场": f"${t.get('exit', 0):.4f}",
                "股数": t.get("shares", 0),
                "盈亏": f"${pnl_val:+,.2f}",
                "退出原因": reason.replace("_", " ").title(),
            })

        st.dataframe(pd.DataFrame(trade_rows), hide_index=True, use_container_width=True)
    else:
        st.info("今日暂无交易记录")

    if not state:
        st.warning("未找到 live_rtg3_state.json，实盘未运行")

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
    st.title("RTG 3.0 策略概览")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("扫描条件 (每5分钟循环)")
        scan_sec = getattr(config, "SCAN_INTERVAL_SEC", 300)
        min_rvol = getattr(config, "MIN_RVOL_TO_TRADE", 3.0)
        st.markdown(f"""
        - 跳空幅度 > **{config.GAP_THRESHOLD:.0%}**
        - 盘前成交量 > **{config.MIN_VOLUME:,}** 股
        - 最低成交额 > **${config.MIN_DOLLAR_VOLUME:,.0f}**
        - 价格区间 **${config.PRICE_MIN}** ~ **${config.PRICE_MAX}**
        - 杠杆ETF过滤: **启用**
        - Crypto ETF过滤: **启用**
        - 候选股: **Top {config.MAX_CANDIDATES} by RVOL**
        - 最低RVOL: **{min_rvol:.1f}×** (低RVOL不入场)
        - 扫描间隔: **每{scan_sec//60}分钟**
        """)

        st.subheader("仓位管理 (All-in)")
        max_daily = config.MAX_DAILY_TRADES if config.MAX_DAILY_TRADES > 0 else "无限制"
        all_in_ratio = getattr(config, "ALL_IN_BP_RATIO", 0.95)
        st.markdown(f"""
        - 初始资金: **${config.INITIAL_CAPITAL:,.2f}**
        - 仓位: **ALL-IN** (购买力 × {all_in_ratio:.0%})
        - 最大同时持仓: **{config.MAX_POSITIONS}** 只
        - 每周期尝试: **Top 3** 候选股
        """)

    with col2:
        st.subheader("入场规则 (RTG + Breakout)")
        st.markdown(f"""
        - RTG信号: close > open_price AND vol >= {config.RTG_VOLUME_MULT}x prior AND vol >= {config.RTG_MIN_VOLUME:,}
        - 突破信号: close > 今日最高价 AND vol >= {getattr(config, 'BREAKOUT_VOLUME_MULT', 1.5)}x prior (盘中量价齐升)
        - 入场窗口: **{config.ENTRY_WINDOW_START} ~ {config.ENTRY_WINDOW_END} EST**
        - 选股: 最高RVOL优先
        """)

        st.subheader("出场规则 (Fixed 1% Trailing)")
        trail_pct = getattr(config, "TRAIL_PCT", 0.01)
        trail_act = getattr(config, "TRAIL_ACTIVATE_PCT", 0.005)
        stop_pct = getattr(config, "STOP_PCT", 0.03)
        st.markdown(f"""
        - 硬止损: **{stop_pct:.0%}** (回补保护)
        - Trailing激活: 利润 > **{trail_act:.1%}**
        - Trailing: **{trail_pct:.0%}** (固定，无渐进)
        - 目标: **{config.TARGET_PCT:.0%}** (安全阀)
        - 周期结束前强制平仓
        - 强制平仓: **{config.FORCE_CLOSE_TIME} EST**
        - 日损失熔断: **{config.MAX_DAILY_LOSS_PCT:.0%}**
        """)

    st.divider()
    st.subheader("RTG 3.0 设计理念")
    st.markdown(f"""
    - **Cycle Machine**: 每{scan_sec//60}分钟一个循环：扫描→选股→全仓买入→1%追踪→强平→重复
    - **All-in**: 每次只用1只股票，95%购买力全仓，集中火力打最强标的
    - **1% Fixed Trailing**: 不用渐进trailing，简单1%追踪止损，利润到0.5%即激活
    - **3% Hard Stop**: 追踪未激活时的回补保护
    - **RTG Signal**: 跳空高开股出现Red-to-Green(量价突破)→市价入场，过滤假突破
    - **Breakout Signal**: 盘中创新高+放量突破，捕获下午量价齐升机会
    - **Min RVOL 3×**: 低于3倍相对成交量的股票不入场，避免低信心标的
    - **T+1兼容**: close_position()兜底处理锁定股
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
