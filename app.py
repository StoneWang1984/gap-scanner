"""RTG 2.0 策略 — Streamlit Web UI (交易显示 + 回测)"""

import json
import time
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

VERSION_DIR = Path("/Users/stonewang2014/gap-scanner/stonewang_daytrade_rtg_2.0")
STATE_FILE = Path("/Users/stonewang2014/gap-scanner/live_state.json")
import importlib.util, sys
_spec = importlib.util.spec_from_file_location("config", VERSION_DIR / "config.py")
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

st.set_page_config(page_title="RTG 2.0 交易", page_icon="📊", layout="wide")

# ── Sidebar ──────────────────────────────────────────────────────

st.sidebar.title("RTG 2.0 交易")
st.sidebar.caption("RVOL加权仓位 + 利润保护 + 渐进Trailing")

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

    # rtg_2.0 uses positions array
    state_positions_list = state.get("positions", []) if state else []
    if state and not state_positions_list and state.get("position"):
        state_positions_list = [state["position"]]
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
    st.title("RTG 2.0 策略概览")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("盘前扫描 (9:25启动)")
        st.markdown(f"""
        - 跳空幅度 > **{config.GAP_THRESHOLD:.0%}** (正跳空)
        - 盘前成交量 > **{config.MIN_VOLUME:,}** 股
        - 最低成交额 > **${config.MIN_DOLLAR_VOLUME:,.0f}**
        - 价格区间 **${config.PRICE_MIN}** ~ **${config.PRICE_MAX}**
        - 杠杆ETF / Crypto ETF: **已过滤**
        - 候选股: **Top 5 by RVOL**
        """)

        st.subheader("盘中扫描 (每5分钟)")
        vol_min_ratio = getattr(config, "VOLUME_SCAN_MIN_REL_VOL_RATIO", 3.0)
        st.markdown(f"""
        - 数据源: most-actives + market gainers
        - 相对5分钟量比 ≥ **{vol_min_ratio:.0f}×**
        - 要求阳线 (close > open) + 涨幅 > **0.5%**
        - 过滤负跳空 (gap ≤ 0 → 跳过)
        - 信号类型: **vol_surge** (直接入场, 无需RTG)
        """)

        st.subheader("入场信号")
        st.markdown(f"""
        - **RTG** (盘前候选): close > open_price AND vol ≥ {config.RTG_VOLUME_MULT}x prior
        - **vol_surge** (盘中候选): 5min量比 ≥ {vol_min_ratio:.0f}× + 阳线 + 涨幅>0.5%
        - GapGo: **禁用** (胜率34%)
        - 入场窗口: **{config.ENTRY_WINDOW_START} ~ {config.ENTRY_WINDOW_END} EST**
        """)

        st.subheader("仓位管理")
        max_daily = config.MAX_DAILY_TRADES if config.MAX_DAILY_TRADES > 0 else "无限制"
        rvol_cap = getattr(config, "RVOL_SIZING_CAP", 10.0)
        st.markdown(f"""
        - 当前权益: **${config.INITIAL_CAPITAL:,.2f}**
        - 最大同时持仓: **{config.MAX_POSITIONS}** 只 (集中资金)
        - 每日交易上限: **{max_daily}**
        - 日损失熔断: **{config.MAX_DAILY_LOSS_PCT:.0%}**
        - RVOL封顶: **{rvol_cap:.0f}×** (防止intraday RVOL膨胀)
        """)
        st.subheader("RVOL仓位分级")
        for rvol_min, eq_pct in config.RVOL_SIZING_TIERS:
            st.markdown(f"- RVOL ≥ {rvol_min:.0f}× → **{eq_pct:.0%}** 权益")

    with col2:
        st.subheader("ATR自适应止损 + Gap扩展")
        gap_factor = getattr(config, "GAP_STOP_FACTOR", 0.3)
        st.markdown(f"""
        - **止损 = max(ATR_MULT × ATR, |gap| × {gap_factor}) / entry**
        - 钳位: **{config.ATR_STOP_MIN_PCT:.0%}** ~ **{config.ATR_STOP_MAX_PCT:.0%}**
        - Gap扩展: 跳空股开盘振荡远大于历史ATR, 需要额外空间
        """)
        st.markdown("**ATR乘数分级 (RVOL→乘数):**")
        for rvol_min, atr_mult in config.ATR_MULT_TIERS:
            st.markdown(f"- RVOL ≥ {rvol_min:.0f}× → **{atr_mult:.1f}× ATR** 止损")

        st.subheader("vol_surge 紧止损 (vs rtg)")
        vs_stop = getattr(config, "VOL_SURGE_STOP_MAX_PCT", 0.05)
        vs_trail = getattr(config, "VOL_SURGE_TRAIL_MULT", 1.5)
        vs_trail_max = getattr(config, "VOL_SURGE_TRAIL_MAX_PCT", 0.03)
        st.markdown(f"""
        - 最大止损: **{vs_stop:.0%}** (无gap扩展, 盘中股票波动小)
        - 追踪止损: **{vs_trail:.1f}× ATR** (clamp 0.5%~{vs_trail_max:.0%}%)
        - 对比rtg: 止损8% / 追踪2.0×ATR
        """)

        st.subheader("追踪止损 (无目标价)")
        st.markdown(f"""
        - Trail宽度: **{config.ATR_TRAIL_MULT:.1f}× ATR** (clamp 0.5%~5%)
        - Trail激活: 盈利 > **+{config.RTG_TRAIL_ACTIVATE_PCT:.0%}** 后开始追踪
        - 目标价: **禁用** — 完全由trail + 渐进trail管理退出
        """)

        st.subheader("渐进Trailing Stop")
        for profit_pct, trail_pct in config.PROGRESSIVE_TRAIL_TIERS:
            st.markdown(f"- 利润 > {profit_pct:.0%} → Trail收紧至 **{trail_pct:.1%}**")

        profit_protect = config.DAILY_PROFIT_PROTECT_ENABLED
        protect_delay = getattr(config, "DAILY_PROFIT_PROTECT_DELAY_SEC", 1800)
        st.subheader("日利润保护")
        st.markdown(f"""
        - 启用: **{'是' if profit_protect else '否'}**
        - 触发: 利润跌至峰值的 **{config.DAILY_PROFIT_PROTECT_RATIO:.0%}**
        - 最低激活: **${config.DAILY_PROFIT_PROTECT_MIN:.0f}** (避免小波动触发)
        - 延迟: 开盘后 **{protect_delay//60}分钟** 不激活
        """)

        st.subheader("强制平仓 & Re-entry")
        st.markdown(f"""
        - EOD强平: **{config.FORCE_CLOSE_TIME} EST**
        - Re-entry: **禁用** (opening drive is your only edge)
        """)

    st.divider()
    st.subheader("RTG 2.0 设计理念")
    st.markdown("""
    - **ATR自适应止损**: 止损宽度与实际波动挂钩, 不再用RVOL固定百分比
    - **Gap扩展**: stop = max(ATR_stop, |gap|×0.3), 跳空股开盘需要更大止损空间
    - **RVOL封顶10×**: 防止intraday RVOL=1260×导致50%仓位单笔巨亏
    - **无目标价**: trail比固定target更灵活, 盈利股让利润奔跑
    - **渐进Trailing**: 利润>5%→1.5%, >10%→1%, >15%→0.5%, 逐步锁利
    - **日利润保护**: 峰值利润回撤30%→全仓强平, 30分钟延迟避免开盘误触
    - **vol_surge紧止损**: 盘中信号5%止损/1.5×ATR trail, 区别于rtg的8%/2×ATR
    - **负跳空过滤**: gap≤0的股票不入场做多
    - **No Re-entry**: 首笔退出后不再入场
    - **集中持仓4只**: 50%仓位给A+设定, 不分散火力
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
