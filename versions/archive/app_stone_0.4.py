"""Stone 0.4 策略 — Streamlit Web UI (回测 + 实时监控)"""

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

st.set_page_config(page_title="Stone 0.4 策略", page_icon="📊", layout="wide")

# ── Sidebar ──────────────────────────────────────────────────────

st.sidebar.title("Stone 0.4 策略")
st.sidebar.caption("三档止盈 · 量价确认再入场 · ATR止损")

tab = st.sidebar.radio("导航", ["策略概览", "运行回测", "实时监控", "交易详情"])

# ── Tab 1: 策略概览 ──────────────────────────────────────────────

if tab == "策略概览":
    st.title("Stone 0.4 策略概览")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("扫描条件")
        st.markdown(f"""
        - 跳空幅度 > **{config.GAP_THRESHOLD:.0%}**
        - 盘前成交量 > **{config.MIN_VOLUME:,}** 股
        - 最低成交额 > **${config.MIN_DOLLAR_VOLUME:,.0f}**
        - 价格区间 **${config.PRICE_MIN}** ~ **${config.PRICE_MAX}**
        """)

        st.subheader("仓位管理")
        st.markdown(f"""
        - 初始资金: **${config.INITIAL_CAPITAL:,.0f}**
        - 仓位比例: **{config.EQUITY_POSITION_RATIO:.0%}** 当前权益
        - 单股上限: **${config.MAX_POSITION_SIZE:,.0f}**
        - 最低仓位: **${config.MIN_POSITION_SIZE:,.0f}**
        - 每日最多持仓: **{config.MAX_POSITIONS_PER_DAY}** 只
        - 每日最多交易: **{config.MAX_DAILY_TRADES}** 笔
        """)

    with col2:
        st.subheader("入场规则")
        st.markdown(f"""
        - 入场确认: **{'是' if config.ENTRY_CONFIRMATION else '否'}**
        - 入场时间: **9:30 ~ 10:00 EST**
        - ATR止损倍数: **{config.STOP_LOSS_ATR_MULT}×**
        - 止损回退: **{config.STOP_LOSS_PCT_FALLBACK:.0%}**
        """)

        st.subheader("首笔出场 — 三档止盈")
        st.markdown(f"""
        - 75%目标: 卖出 **1/4** 仓位
        - 112.5%目标: 卖出 **1/3** 剩余
        - 150%目标: 卖出 **1/3** 剩余
        - 75%后移动止盈: **{config.TRAILING_STOP_PCT_75:.0%}**
        - 112.5%后移动止盈: **{config.TRAILING_STOP_PCT_1125:.0%}**
        - 150%后移动止盈: **{config.TRAILING_STOP_PCT_150:.0%}**
        """)

        st.subheader("再入场规则")
        st.markdown(f"""
        - 量价确认: **收阳 + 量 > 1.2× 均量**
        - 再入场止损: **{config.REENTRY_STOP_PCT:.0%}**
        - 再入场目标: **150%** 回撤卖1/3
        - 再入场移动止盈: **{config.REENTRY_TRAILING_PCT:.0%}**
        - 大幅回调保护: **{config.PULLBACK_STOP_THRESHOLD:.0%}**
        - 收盘强制平仓: **{config.FORCE_CLOSE_TIME}** EST
        """)

    st.divider()
    st.subheader("版本对比")
    versions = [
        ("Stone 0.1", "基线版 ($100K)", "1,121%", "86.6%", "-6.05%", "13.51", "179"),
        ("Stone 0.2", "增加频次版 ($100K)", "399%", "65.2%", "-7.58%", "6.03", "621"),
        ("Stone 0.3", "复利+动态止盈 ($1K)", "54,414%", "87.8%", "-14.73%", "13.63", "181"),
        ("Stone 0.4", "三档+量价再入场 ($1K)", "61,507%", "80.5%", "-14.74%", "14.46", "215"),
    ]
    vdf = pd.DataFrame(versions, columns=["版本", "描述", "总收益率", "胜率", "最大回撤", "夏普比率", "总交易"])
    st.dataframe(vdf, width="stretch", hide_index=True)

# ── Tab 2: 运行回测 ──────────────────────────────────────────────

elif tab == "运行回测":
    st.title("运行回测")

    n_days = st.slider("回测天数", min_value=30, max_value=180, value=180, step=30)

    if st.button("开始回测", type="primary"):
        with st.spinner("回测运行中，请等待..."):
            trades = run_backtest(n_days=n_days)

        if not trades:
            st.error("回测未产生交易结果")
            st.stop()

        st.session_state["trades"] = trades

        # ── Summary metrics ──
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
        c5.metric("夏普比率", summary["Sharpe Ratio"])

        c6, c7, c8, c9 = st.columns(4)
        c6.metric("总交易", str(summary["Total Trades"]))
        c7.metric("平均盈利", summary["Avg Win"])
        c8.metric("平均亏损", summary["Avg Loss"])
        c9.metric("最佳交易", summary["Best Trade"])

        # ── Equity curve ──
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

        # ── P&L per trade ──
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

        # ── Exit reasons pie ──
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

        # ── Trade table ──
        st.subheader("交易明细")
        trade_df = build_trade_table(trades)
        st.dataframe(trade_df, width="stretch", hide_index=True, height=400)

# ── Tab 3: 实时监控 ──────────────────────────────────────────────

elif tab == "实时监控":
    st.title("实时监控")

    # Read state file
    state_file = Path("live_state.json")
    state = None
    if state_file.exists():
        try:
            with open(state_file) as f:
                state = json.load(f)
        except:
            state = None

    # ── Strategy capital overview (based on $1,000 initial) ──
    st.subheader("策略资金概览")
    initial = config.INITIAL_CAPITAL

    # Calculate realized P&L from today's events
    realized_pnl = 0.0
    if state and state.get("events"):
        for evt in state["events"]:
            pass  # events are informational; realized P&L tracked via positions that exited

    # Calculate unrealized P&L from current positions
    unrealized_pnl = 0.0
    position_value = 0.0
    if state and state.get("positions"):
        try:
            from live_trade import get_snapshots
            syms = [p["symbol"] for p in state["positions"]]
            if syms:
                snaps = get_snapshots(syms)
                for p in state["positions"]:
                    snap = snaps.get(p["symbol"])
                    cur = float(snap.latest_trade.price) if snap and snap.latest_trade else p["entry_price"]
                    unrealized_pnl += (cur - p["entry_price"]) * p["remaining_shares"]
                    position_value += cur * p["remaining_shares"]
        except:
            pass

    total_equity = initial + unrealized_pnl
    total_return_pct = (unrealized_pnl / initial * 100) if initial > 0 else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("初始资金", f"${initial:,.0f}")
    c2.metric("当前权益", f"${total_equity:,.2f}", f"{total_return_pct:+.2f}%")
    c3.metric("浮动盈亏", f"${unrealized_pnl:,.2f}",
              delta=f"{total_return_pct:+.2f}%", delta_color="normal")
    c4.metric("持仓市值", f"${position_value:,.2f}")
    c5.metric("可用资金", f"${initial - position_value:,.2f}")

    # ── Connection status ──
    if state:
        updated = state.get("updated", "未知")
        daily_trades = state.get("daily_trades", 0)
        daily_stopped = state.get("daily_stopped", False)
        st.caption(f"最后更新: {updated} | 今日交易: {daily_trades} | "
                   f"{'⚠️ 已停止交易' if daily_stopped else '✅ 正常交易中'}")

    # ── Gap scan results ──
    st.subheader("今日选股")
    if state and state.get("candidates"):
        cand_data = []
        for c in state["candidates"]:
            cand_data.append({
                "股票": c["symbol"],
                "跳空幅度": f"+{c['gap_pct']:.1%}",
                "开盘价": f"${c['open_price']:.4f}",
                "昨收": f"${c['prev_close']:.4f}",
            })
        st.dataframe(pd.DataFrame(cand_data), width="stretch", hide_index=True)
    elif not state:
        # Fallback: run scan directly
        if st.button("手动扫描Gap", type="primary"):
            with st.spinner("扫描中..."):
                from live_trade import scan_gaps
                results = scan_gaps()
                if results:
                    for r in results:
                        st.write(f"**{r['symbol']}**: gap +{r['gap_pct']:.1%}, open=${r['open_price']:.4f}")
                else:
                    st.info("未发现跳空股票")
    else:
        st.info("等待 live_trade.py 启动...")

    # ── Current positions ──
    if state and state.get("positions"):
        st.subheader("当前持仓")

        # Get live prices for positions
        live_prices = {}
        try:
            from live_trade import get_snapshots
            syms = [p["symbol"] for p in state["positions"]]
            if syms:
                snaps = get_snapshots(syms)
                for sym, snap in snaps.items():
                    if snap and snap.latest_trade:
                        live_prices[sym] = float(snap.latest_trade.price)
        except:
            pass

        # Summary table
        pos_table = []
        for p in state["positions"]:
            cur = live_prices.get(p["symbol"], p["entry_price"])
            pnl = (cur - p["entry_price"]) * p["remaining_shares"]
            pnl_pct = (cur / p["entry_price"] - 1) * 100 if p["entry_price"] > 0 else 0
            pos_table.append({
                "股票": p["symbol"], "类型": p["trade_type"],
                "数量": p["remaining_shares"],
                "入场价": f"${p['entry_price']:.4f}", "当前价": f"${cur:.4f}",
                "盈亏": f"${pnl:.2f}", "盈亏%": f"{pnl_pct:+.2f}%",
                "止损": f"${p['stop_price']:.4f}",
            })
        st.dataframe(pd.DataFrame(pos_table), width="stretch", hide_index=True)

        # Detailed cards
        for p in state["positions"]:
            cur = live_prices.get(p["symbol"], p["entry_price"])
            pnl = (cur - p["entry_price"]) * p["remaining_shares"]
            pnl_pct = (cur / p["entry_price"] - 1) * 100 if p["entry_price"] > 0 else 0
            icon = "🟢" if pnl >= 0 else "🔴"
            with st.expander(f"{icon} **{p['symbol']}** ({p['trade_type']}) — {pnl_pct:+.2f}%", expanded=True):
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("入场价", f"${p['entry_price']:.4f}")
                c2.metric("当前价", f"${cur:.4f}")
                c3.metric("止损价", f"${p['stop_price']:.4f}")
                c4.metric("盈亏", f"${pnl:.2f}", f"{pnl_pct:+.2f}%")

                # Target progress
                if p['trade_type'] == "first":
                    targets = {"75%": p['target_75'], "112.5%": p['target_1125'], "150%": p['target_150']}
                    reached = {"75%": p['reached_75'], "112.5%": p['reached_1125'], "150%": p['reached_150']}
                else:
                    targets = {"目标": p['reentry_target']}
                    reached = {"目标": p['reached_150']}

                tier_str = " | ".join(
                    f"{'✅' if reached.get(k) else '⬜'} {k}: ${v:.4f}"
                    for k, v in targets.items()
                )
                st.markdown(tier_str)

                if p.get('entry_time'):
                    st.caption(f"入场时间: {p['entry_time']}")

    # ── 5-min bar progress ──
    if state and state.get("bar_counts"):
        st.subheader("5分钟Bar累积")
        bar_data = [{"股票": sym, "完成5分钟Bar": count}
                     for sym, count in state["bar_counts"].items()]
        st.dataframe(pd.DataFrame(bar_data), width="stretch", hide_index=True)

    # ── Events log ──
    if state and state.get("events"):
        st.subheader("最近事件")
        for evt in reversed(state["events"][-20:]):
            if "ENTERED" in evt or "TARGET" in evt:
                st.success(evt)
            elif "STOP" in evt or "TRAILING" in evt:
                st.error(evt)
            else:
                st.info(evt)

    # ── Auto refresh ──
    st.divider()
    auto_refresh = st.checkbox("自动刷新 (5秒)", value=True)
    if auto_refresh:
        time.sleep(5)
        st.rerun()

# ── Tab 4: 交易详情 ──────────────────────────────────────────────

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

    # Detail card
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

    # Three-tier partial sells (Stone 0.4)
    st.subheader("三档止盈详情")
    tier1 = tier2 = tier3 = "未触发"
    if t.partial_sell_shares > 0:
        tier1 = f"**{t.partial_sell_shares:,}股** @ ${t.partial_sell_price:.4f} (75%目标)"
    if t.partial2_sell_shares > 0:
        tier2 = f"**{t.partial2_sell_shares:,}股** @ ${t.partial2_sell_price:.4f} (112.5%目标)"
    if t.partial3_sell_shares > 0:
        tier3 = f"**{t.partial3_sell_shares:,}股** @ ${t.partial3_sell_price:.4f} (150%目标)"

    tc1, tc2, tc3 = st.columns(3)
    tc1.markdown(f"75%: {tier1}")
    tc2.markdown(f"112.5%: {tier2}")
    tc3.markdown(f"150%: {tier3}")

    if t.trailing_high > t.entry_price:
        st.caption(f"最高价: ${t.trailing_high:.4f} | 移动止盈出场: ${t.trailing_exit_price:.4f}")

    # Trade type
    if t.trade_type == "reentry":
        st.info("🔄 再入场交易")

    # All trades quick view
    st.divider()
    st.subheader("全部交易概览")
    trade_df = build_trade_table(trades)
    st.dataframe(trade_df, width="stretch", hide_index=True, height=350)
