"""Stone 0.4.17 策略 — Streamlit Web UI (交易显示 + 回测)"""

import json
import time
from pathlib import Path

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

import config
from backtest import run_backtest
from report import calc_summary, build_trade_table, build_equity_curve
from strategy import TradeResult

# Patch config for backtest
config.REENTRY_CUTOFF_TIME = getattr(config, "REENTRY_CUTOFF_TIME", "13:00")
config.SLIPPAGE_ENTRY_PCT = getattr(config, "SLIPPAGE_ENTRY_PCT", 0.005)
config.SLIPPAGE_STOP_PCT = getattr(config, "SLIPPAGE_STOP_PCT", 0.02)
config.SLIPPAGE_TRAILING_PCT = getattr(config, "SLIPPAGE_TRAILING_PCT", 0.01)
config.SLIPPAGE_TARGET_PCT = getattr(config, "SLIPPAGE_TARGET_PCT", 0.003)
config.SLIPPAGE_FORCE_CLOSE_PCT = getattr(config, "SLIPPAGE_FORCE_CLOSE_PCT", 0.01)
config.SLIPPAGE_REENTRY_STOP_PCT = getattr(config, "SLIPPAGE_REENTRY_STOP_PCT", 0.025)

import importlib.util
# Use current 6-tier backtest engine
from backtest import run_backtest as run_backtest_6tier

st.set_page_config(page_title="Stone 0.4.17 交易", page_icon="📊", layout="wide")

# ── Sidebar ──────────────────────────────────────────────────────

st.sidebar.title("Stone 0.4.17 交易")
st.sidebar.caption("WebSocket即时 · SIP数据源 · 6档止盈 · 仓位恢复")

tab = st.sidebar.radio("导航", ["实盘交易", "策略概览", "运行回测", "交易详情"])

# ══════════════════════════════════════════════════════════════════
# Tab 1: 实盘交易 (核心页面)
# ══════════════════════════════════════════════════════════════════

if tab == "实盘交易":
    st.title("实盘交易")

    state_file = Path("live_state.json")
    state = None
    if state_file.exists():
        try:
            with open(state_file) as f:
                state = json.load(f)
        except Exception:
            state = None

    # ── 账户资金 ──
    st.subheader("💰 账户资金")

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
        data_feed = state.get("data_feed", "IEX")
        ws_connected = state.get("ws_connected", None)
        version = state.get("version", "?")
        daily_trades = state.get("daily_trades", 0)
        daily_stopped = state.get("daily_stopped", False)
        ws_status = "WS实时" if ws_connected is True else "快照轮询" if ws_connected is False else "未启动"
        stop_status = "⛔已熔断" if daily_stopped else "正常"
        st.caption(f"v{version} | {data_feed} · {ws_status} | 今日 {daily_trades} 笔 | {stop_status}")

    # ── 当前持仓 ──
    st.divider()
    st.subheader("📈 当前持仓")

    if alpaca_positions:
        # Merge Alpaca live data with live_state target prices
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

            # Add strategy data from live_state
            sp = state_positions.get(sym, {})
            if sp:
                row["类型"] = sp.get("trade_type", "first")
                row["止损"] = f"${sp.get('stop_price', 0):.4f}"
                # Show 6-tier targets
                targets = sp.get("targets", [])
                reached = sp.get("reached_list", [])
                if targets:
                    tier_strs = []
                    for ti, t in enumerate(targets):
                        mark = "✓" if ti < len(reached) and reached[ti] else "·"
                        tier_strs.append(f"{mark}{t:.2f}")
                    row["6档目标"] = " | ".join(tier_strs)
                row["最高价"] = f"${sp.get('highest', 0):.4f}"

            pos_rows.append(row)

        st.dataframe(pd.DataFrame(pos_rows), hide_index=True, use_container_width=True)
    elif state and state.get("positions"):
        # Fallback: show from state file
        pos_rows = []
        for p in state["positions"]:
            targets = p.get("targets", [])
            reached = p.get("reached_list", [])
            tier_str = ""
            if targets:
                tier_strs = []
                for ti, t in enumerate(targets):
                    mark = "✓" if ti < len(reached) and reached[ti] else "·"
                    tier_strs.append(f"{mark}{t:.2f}")
                tier_str = " | ".join(tier_strs)
            pos_rows.append({
                "股票": p["symbol"],
                "类型": p.get("trade_type", "first"),
                "数量": p.get("remaining_shares", 0),
                "入场价": f"${p.get('entry_price', 0):.4f}",
                "止损": f"${p.get('stop_price', 0):.4f}",
                "6档目标": tier_str,
            })
        st.dataframe(pd.DataFrame(pos_rows), hide_index=True, use_container_width=True)
    else:
        st.info("当前无持仓")

    # ── 今日候选股 ──
    if state and state.get("candidates"):
        st.divider()
        st.subheader("📋 今日候选股")

        day_highs = state.get("day_highs", {})
        n_stocks = min(config.MAX_POSITIONS_PER_DAY, len(state["candidates"]))
        alloc = equity / n_stocks if n_stocks > 0 else 0

        cand_rows = []
        for c in state["candidates"]:
            sym = c["symbol"]
            high = day_highs.get(sym, 0)
            cand_rows.append({
                "股票": sym,
                "跳空": f"+{c['gap_pct']:.1%}",
                "开盘": f"${c['open_price']:.4f}",
                "昨收": f"${c['prev_close']:.4f}",
                "日最高": f"${high:.4f}" if high else "—",
                "分配金额": f"${alloc:.2f}",
            })
        st.dataframe(pd.DataFrame(cand_rows), hide_index=True, use_container_width=True)

    # ── 交易事件日志 ──
    if state and state.get("events"):
        st.divider()
        st.subheader("📝 交易事件")

        for evt in reversed(state["events"][-30:]):
            if "BUY" in evt or "FILLED" in evt:
                st.success(evt)
            elif "STOP" in evt or "TRAILING" in evt or "FORCE" in evt:
                st.error(evt)
            elif "SELL" in evt or "PARTIAL" in evt or "TARGET" in evt:
                st.warning(evt)
            else:
                st.info(evt)

    if not state:
        st.warning("未找到 live_state.json，实盘未运行")

    # ── Auto refresh ──
    st.divider()
    auto_refresh = st.checkbox("自动刷新 (1分钟)", value=True)
    if auto_refresh:
        time.sleep(60)
        st.rerun()

# ══════════════════════════════════════════════════════════════════
# Tab 2: 策略概览
# ══════════════════════════════════════════════════════════════════

elif tab == "策略概览":
    st.title("Stone 0.4.17 策略概览")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("扫描条件")
        st.markdown(f"""
        - 跳空幅度 > **{config.GAP_THRESHOLD:.0%}**
        - 盘前成交量 > **{config.MIN_VOLUME:,}** 股
        - 最低成交额 > **${config.MIN_DOLLAR_VOLUME:,.0f}**
        - 价格区间 **${config.PRICE_MIN}** ~ **${config.PRICE_MAX}**
        - 杠杆ETF过滤: **启用**
        """)

        st.subheader("仓位管理")
        st.markdown(f"""
        - 初始资金: **${config.INITIAL_CAPITAL:,.0f}**
        - 仓位比例: **{config.EQUITY_POSITION_RATIO:.0%}** 当前权益
        - 单股上限: **${config.MAX_POSITION_SIZE:,.0f}**
        - 每日最多持仓: **{config.MAX_POSITIONS_PER_DAY}** 只
        - 每日最多交易: **{config.MAX_DAILY_TRADES}** 笔
        """)

    with col2:
        st.subheader("入场规则")
        st.markdown(f"""
        - 入场确认: **{'是' if config.ENTRY_CONFIRMATION else '否'}**
        - 入场时间: **9:30 ~ 10:00 EST**
        - ATR止损倍数: **{config.STOP_LOSS_ATR_MULT}×**
        - 止损上限: **{config.STOP_LOSS_MAX_PCT:.0%}**
        """)

        st.subheader("首笔出场 — 6档止盈")
        retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.25, 0.50, 0.75, 1.00, 1.25, 1.50])
        caps = getattr(config, "TARGET_CAP_TIERS", [0.05, 0.10, 0.15, 0.20, 0.25, 0.35])
        sell_ratios = getattr(config, "PARTIAL_SELL_RATIOS", [1/8]*6)
        trail_pcts = getattr(config, "TRAILING_STOP_PCTS", [0.02, 0.025, 0.03, 0.035, 0.04, 0.05])
        tier_lines = []
        for i in range(len(retracements)):
            tier_lines.append(f"- T{i+1} ({int(retracements[i]*100)}%回撤, cap {int(caps[i]*100)}%): 卖出 **1/{int(round(1/sell_ratios[i]))}** 仓位, 追踪止损 **{trail_pcts[i]:.1%}**")
        tier_lines.append(f"- 总计卖出: **{sum(sell_ratios):.0%}**, 剩余 **{1-sum(sell_ratios):.0%}** 由最终追踪止损保护")
        st.markdown("\n".join(tier_lines))

        st.subheader("再入场规则")
        st.markdown(f"""
        - 再入场止损: **ATR 1.5×** (回退 4%)
        - 再入场比例: **50%** 首笔仓位
        - 再入场截止: **{config.REENTRY_CUTOFF_TIME} EST**
        - 最低回调: **{config.REENTRY_MIN_PULLBACK:.0%}**
        - 日损失熔断: **{config.MAX_DAILY_LOSS_PCT:.0%}**
        - 收盘强制平仓: **{config.FORCE_CLOSE_TIME}** EST
        """)

    st.divider()
    st.subheader("0.4.17 新增特性")
    st.markdown("""
    - **6档分批止盈**: 替代旧3档，更精细地捕捉不同级别的利润
    - **WebSocket 实时交易**: 5min K线完成后1-2秒内下单，逐笔成交即时止损
    - **SIP 数据源**: 覆盖100%成交量（替代IEX 3%）
    - **WS 健康检查**: >120秒无消息自动切回快照轮询
    - **仓位恢复**: 脚本重启自动从 Alpaca 恢复持仓，含6档目标重建
    - **Skip-gap**: 价格跳过多档时一次性卖出所有跳过档位
    - **Pending sell 追踪**: 卖单成交确认后才更新状态，失败自动回滚
    - **Partial fill 处理**: 部分成交时精确调整剩余股数
    - **Bracket order 入场**: 原子性买入+止损，避免 wash trade
    """)

# ══════════════════════════════════════════════════════════════════
# Tab 3: 运行回测
# ══════════════════════════════════════════════════════════════════

elif tab == "运行回测":
    st.title("运行回测")

    n_days = st.slider("回测天数", min_value=30, max_value=180, value=180, step=30)

    if st.button("开始回测", type="primary"):
        with st.spinner("回测运行中，请等待..."):
            config.BACKTEST_DAYS = n_days
            trades_raw = run_backtest_6tier()
            trades = trades_raw

        if not trades:
            st.error("回测未产生交易结果")
            st.stop()

        st.session_state["trades"] = trades

        summary = calc_summary(trades)
        total_pnl = sum(t.pnl for t in trades)
        final_equity = config.INITIAL_CAPITAL + total_pnl
        total_return = total_pnl / config.INITIAL_CAPITAL * 100

        st.subheader("核心指标")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("初始资金", f"${config.INITIAL_CAPITAL:,.0f}")
        c2.metric("最终资金", f"${final_equity:,.0f}", f"{total_return:+.1f}%")
        c3.metric("总收益", f"${total_pnl:,.0f}", f"{total_return:+.1f}%")
        c4.metric("胜率", summary["Win Rate"])
        c5.metric("最大回撤", summary["Max Drawdown"])

        c6, c7, c8, c9 = st.columns(4)
        c6.metric("总交易", str(summary["Total Trades"]))
        c7.metric("平均盈利", summary["Avg Win"])
        c8.metric("平均亏损", summary["Avg Loss"])
        c9.metric("最佳交易", summary["Best Trade"])

        st.subheader("权益曲线")
        equity_curve = build_equity_curve(trades)
        if not equity_curve.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=equity_curve.index, y=equity_curve.values,
                mode="lines", name="权益",
                line=dict(color="#2196F3", width=2),
                fill="tozeroy", fillcolor="rgba(33,150,243,0.1)",
            ))
            fig.add_hline(y=config.INITIAL_CAPITAL, line_dash="dash",
                          line_color="gray", annotation_text="初始资金")
            fig.update_layout(
                xaxis_title="日期", yaxis_title="权益 ($)",
                hovermode="x unified", height=400, yaxis_tickformat="$,.0f",
            )
            st.plotly_chart(fig, width="stretch")

        st.subheader("每笔交易盈亏")
        pnls = [t.pnl for t in trades]
        colors = ["#4CAF50" if p >= 0 else "#F44336" for p in pnls]
        fig2 = go.Figure()
        fig2.add_trace(go.Bar(
            x=list(range(1, len(pnls) + 1)), y=pnls,
            marker_color=colors, name="P&L",
        ))
        fig2.add_hline(y=0, line_color="black", line_width=0.5)
        fig2.update_layout(
            xaxis_title="交易序号", yaxis_title="盈亏 ($)",
            height=350, yaxis_tickformat="$,.0f",
        )
        st.plotly_chart(fig2, width="stretch")

        st.subheader("出场原因分布")
        exit_counts = summary.get("Exit Reasons", {})
        if exit_counts:
            fig3 = go.Figure(data=[go.Pie(
                labels=list(exit_counts.keys()),
                values=list(exit_counts.values()),
                marker_colors=["#4CAF50", "#F44336", "#FF9800", "#2196F3",
                                "#9C27B0", "#00BCD4"][:len(exit_counts)],
                hole=0.4,
            )])
            fig3.update_layout(height=350)
            st.plotly_chart(fig3, width="stretch")

        st.subheader("交易明细")
        trade_df = build_trade_table(trades)
        st.dataframe(trade_df, width="stretch", hide_index=True, height=400)

# ══════════════════════════════════════════════════════════════════
# Tab 4: 交易详情
# ══════════════════════════════════════════════════════════════════

elif tab == "交易详情":
    st.title("交易详情")

    trades = st.session_state.get("trades")
    if not trades:
        st.info("请先在「运行回测」页面执行回测")
        st.stop()

    trade_options = [f"#{i+1} {t.date} {t.symbol} ({t.pnl_pct:.2%})"
                     for i, t in enumerate(trades)]
    selected_idx = st.selectbox("选择交易", range(len(trade_options)),
                                format_func=lambda i: trade_options[i])

    t = trades[selected_idx]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("入场价", f"${t.entry_price:.4f}")
    c2.metric("出场价", f"${t.exit_price:.4f}")
    c3.metric("股数", f"{t.shares:,}")
    c4.metric("盈亏", f"${t.pnl:,.2f}", f"{t.pnl_pct:.2%}")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("开盘价", f"${t.open_price:.4f}")
    c6.metric("150%目标", f"${t.sell_target:.4f}" if t.sell_target else "N/A")
    c7.metric("止损价", f"${t.stop_price:.4f}" if t.stop_price else "N/A")
    c8.metric("出场原因", t.exit_reason.replace("_", " ").title())

    st.subheader("6档止盈详情")
    if t.partial_sells:
        n_tiers = len(t.partial_sells)
        retracements = getattr(config, "PROFIT_RETRACEMENT_TIERS", [0.25, 0.50, 0.75, 1.00, 1.25, 1.50])
        for ti, (price, shares) in enumerate(t.partial_sells):
            if shares > 0:
                pct_label = f"{int(retracements[ti]*100)}%" if ti < len(retracements) else f"T{ti+1}"
                st.markdown(f"- **T{ti+1} ({pct_label})**: {shares:,}股 @ ${price:.4f}")
        total_sold = sum(s for _, s in t.partial_sells)
        st.caption(f"分档共卖出 {total_sold:,}股 / 总 {t.shares:,}股")
    else:
        st.info("无分档卖出")

    if t.trailing_high > t.entry_price:
        st.caption(f"最高价: ${t.trailing_high:.4f} | 移动止盈出场: ${t.trailing_exit_price:.4f}")

    if t.trade_type == "reentry":
        st.info("再入场交易")

    st.divider()
    st.subheader("全部交易概览")
    trade_df = build_trade_table(trades)
    st.dataframe(trade_df, width="stretch", hide_index=True, height=350)
