"""Stone 0.3 策略 — Streamlit Web UI"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

import config
from backtest import run_backtest
from report import calc_summary, build_trade_table, build_equity_curve
from strategy import TradeResult

st.set_page_config(page_title="Stone 0.3 策略", page_icon="📊", layout="wide")

# ── Sidebar ──────────────────────────────────────────────────────

st.sidebar.title("Stone 0.3 策略")
st.sidebar.caption("复利仓位 · 动态部分止盈 · ATR止损")

tab = st.sidebar.radio("导航", ["策略概览", "运行回测", "交易详情"])

# ── Tab 1: 策略概览 ──────────────────────────────────────────────

if tab == "策略概览":
    st.title("Stone 0.3 策略概览")

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
        - 最低仓位: **${config.MIN_POSITION_SIZE:,.0f}**
        - 每日最多持仓: **{config.MAX_POSITIONS_PER_DAY}** 只
        """)

    with col2:
        st.subheader("入场规则")
        st.markdown(f"""
        - 入场确认: **{'是' if config.ENTRY_CONFIRMATION else '否'}**
        - 入场时间: **9:30 ~ 10:00 EST**
        """)

        st.subheader("出场规则")
        st.markdown(f"""
        - 75%目标: 卖出 **1/4** 仓位
        - 150%目标: 卖出 **1/3** 剩余仓位
        - 75%后移动止盈: **{config.TRAILING_STOP_PCT_75:.0%}**
        - 150%后移动止盈: **{config.TRAILING_STOP_PCT_150:.0%}**
        - ATR止损倍数: **{config.STOP_LOSS_ATR_MULT}×**
        - 止损回退: **{config.STOP_LOSS_PCT_FALLBACK:.0%}**
        - 收盘强制平仓: **{config.FORCE_CLOSE_TIME}** EST
        """)

    st.divider()
    st.subheader("版本历史")
    versions = [
        ("Stone 0.1", "基线版 ($100K)", "1,121%", "86.6%", "-6.05%", "13.51"),
        ("Stone 0.2", "增加频次版 ($100K)", "399%", "65.2%", "-7.58%", "6.03"),
        ("Stone 0.3", "复利+动态止盈 ($1K)", "54,414%", "87.8%", "-14.73%", "13.63"),
    ]
    vdf = pd.DataFrame(versions, columns=["版本", "描述", "总收益率", "胜率", "最大回撤", "夏普比率"])
    st.dataframe(vdf, use_container_width=True, hide_index=True)

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

        # Store in session
        st.session_state["trades"] = trades

        # ── Summary metrics ──
        summary = calc_summary(trades)
        total_pnl = sum(t.pnl for t in trades)
        final_equity = config.INITIAL_CAPITAL + total_pnl
        total_return = total_pnl / config.INITIAL_CAPITAL * 100

        st.subheader("核心指标")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("最终资金", f"${final_equity:,.0f}", f"{total_return:+.1f}%")
        c2.metric("总收益率", f"{total_return:.1f}%")
        c3.metric("胜率", summary["Win Rate"])
        c4.metric("最大回撤", summary["Max Drawdown"])
        c5.metric("夏普比率", summary["Sharpe Ratio"])

        c6, c7, c8, c9 = st.columns(4)
        c6.metric("总交易", str(summary["Total Trades"]))
        c7.metric("平均盈利", summary["Avg Win"])
        c8.metric("平均亏损", summary["Avg Loss"])
        c9.metric("最佳交易", summary["Best Trade"])

        # ── Equity curve (Plotly) ──
        st.subheader("权益曲线")
        equity_curve = build_equity_curve(trades)
        if not equity_curve.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=equity_curve.index,
                y=equity_curve.values,
                mode="lines",
                name="权益",
                line=dict(color="#2196F3", width=2),
                fill="tozeroy",
                fillcolor="rgba(33,150,243,0.1)",
            ))
            fig.add_hline(y=config.INITIAL_CAPITAL, line_dash="dash",
                          line_color="gray", annotation_text="初始资金")
            fig.update_layout(
                xaxis_title="日期", yaxis_title="权益 ($)",
                hovermode="x unified", height=400,
                yaxis_tickformat="$,.0f",
            )
            st.plotly_chart(fig, use_container_width=True)

        # ── P&L per trade ──
        st.subheader("每笔交易盈亏")
        pnls = [t.pnl for t in trades]
        colors = ["#4CAF50" if p >= 0 else "#F44336" for p in pnls]
        fig2 = go.Figure()
        fig2.add_trace(go.Bar(
            x=list(range(1, len(pnls) + 1)),
            y=pnls,
            marker_color=colors,
            name="P&L",
        ))
        fig2.add_hline(y=0, line_color="black", line_width=0.5)
        fig2.update_layout(
            xaxis_title="交易序号", yaxis_title="盈亏 ($)",
            height=350, yaxis_tickformat="$,.0f",
        )
        st.plotly_chart(fig2, use_container_width=True)

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
            st.plotly_chart(fig3, use_container_width=True)

        # ── Trade table ──
        st.subheader("交易明细")
        trade_df = build_trade_table(trades)
        st.dataframe(trade_df, use_container_width=True, hide_index=True,
                     height=400)

# ── Tab 3: 交易详情 ──────────────────────────────────────────────

elif tab == "交易详情":
    st.title("交易详情")

    trades = st.session_state.get("trades")
    if not trades:
        st.info("请先在「运行回测」页面执行回测")
        st.stop()

    # Selector
    trade_options = [f"#{i+1} {t.date} {t.symbol} ({t.pnl_pct:.2%})" for i, t in enumerate(trades)]
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

    # Partial sells
    if t.partial_sell_shares > 0:
        st.info(f"75%部分止盈: {t.partial_sell_shares:,}股 @ ${t.partial_sell_price:.4f}")
    if hasattr(t, "partial2_sell_shares") and t.partial2_sell_shares > 0:
        st.info(f"150%部分止盈: {t.partial2_sell_shares:,}股 @ ${t.partial2_sell_price:.4f}")

    if t.trailing_high > t.entry_price:
        st.caption(f"最高价: ${t.trailing_high:.4f} | 移动止盈出场: ${t.trailing_exit_price:.4f}")

    # All trades quick view
    st.divider()
    st.subheader("全部交易概览")
    trade_df = build_trade_table(trades)
    st.dataframe(trade_df, use_container_width=True, hide_index=True, height=350)
