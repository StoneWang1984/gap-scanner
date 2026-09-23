"""Keep Raising 1.0 策略 — Streamlit Web UI (交易显示 + 策略概览 + 交易详情)"""

import json
import time
from pathlib import Path

import streamlit as st
import pandas as pd

VERSION_DIR = Path("/Users/stonewang2014/gap-scanner/stonewang_daytrade_keep_raising_1.0")
STATE_FILE = Path("/Users/stonewang2014/gap-scanner/live_state.json")
LOG_FILE = VERSION_DIR / "live_rtg.log"

import importlib.util, sys
_spec = importlib.util.spec_from_file_location("config", VERSION_DIR / "config.py")
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

st.set_page_config(page_title="Keep Raising 1.0 交易", page_icon="📊", layout="wide")

# ── Sidebar ──────────────────────────────────────────────────────

st.sidebar.title("Keep Raising 1.0 交易")
st.sidebar.caption("上午RTG + 下午Keep Raising + 利润保护90%")

tab = st.sidebar.radio("导航", ["实盘交易", "策略概览", "交易详情", "日志"])

# ══════════════════════════════════════════════════════════════════
# Tab 1: 实盘交易
# ══════════════════════════════════════════════════════════════════

if tab == "实盘交易":
    st.title("实盘交易")

    state = None
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
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

    # ── 系统状态 ──
    if state:
        version = state.get("version", "?")
        daily_trades = state.get("daily_trades", 0)
        ws_connected = state.get("ws_connected", False)
        status = f"v{version} | {config.DATA_FEED} | 今日 {daily_trades} 笔 | WS {'✓' if ws_connected else '✗'}"
        st.caption(status)

    # ── 当前持仓 ──
    st.divider()
    st.subheader("当前持仓")

    state_positions_list = state.get("positions", []) if state else []
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
                row["止损"] = f"{sp.get('stop_pct', 0):.1%}"
                row["Trail"] = f"{sp.get('trail_pct', 0):.1%}"
                row["最高"] = f"${sp.get('highest', 0):.4f}"

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
                "止损": f"{p.get('stop_pct', 0):.1%}",
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

    # ── 今日交易汇总 ──
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
            trade_type = trades[0].get("trade_type", "")
            final_reason = trades[-1].get("exit_reason", "") or trades[-1].get("reason", "")

            entry_cost = entry_price * total_shares if entry_price > 0 else 0
            pnl_pct = (total_pnl_sym / entry_cost) if entry_cost > 0 else 0

            summary_rows.append({
                "股票": sym,
                "类型": trade_type,
                "买入价": f"${entry_price:.4f}",
                "卖出价": f"${exit_price:.4f}",
                "股数": total_shares,
                "盈亏": f"${total_pnl_sym:+,.2f}",
                "盈亏%": f"{pnl_pct:+.1%}",
                "退出类型": final_reason.replace("_", " ").title(),
            })

        st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)
        st.metric("今日总盈亏", f"${total_pnl:+,.2f}")
    else:
        st.info("今日暂无已完成交易")

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
    st.title("Keep Raising 1.0 策略概览")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("盘前扫描 (9:25启动)")
        st.markdown(f"""
        - 跳空幅度 > **{config.GAP_THRESHOLD:.0%}** (正跳空)
        - 盘前成交量 > **{config.MIN_VOLUME:,}** 股
        - 最低成交额 > **${config.MIN_DOLLAR_VOLUME:,.0f}**
        - 价格区间 **${config.PRICE_MIN}** ~ **${config.PRICE_MAX}**
        - 杠杆ETF / Crypto ETF: **已过滤**
        - 候选股: **Top {config.MAX_CANDIDATES} by RVOL**
        """)

        st.subheader("入场信号 (上午)")
        st.markdown(f"""
        - **RTG** (Red-to-Green): close > open_price + 量能 ≥ {config.RTG_VOLUME_MULT}× 前bar + ≥ {config.RTG_MIN_VOLUME:,}股
        - **Vol Surge** (盘中量能): 5min量比 ≥ 3.0× + close > open×1.005
        - 上午窗口: **{config.ENTRY_WINDOW_START} ~ {config.ENTRY_WINDOW_END} EST**
        """)

        st.subheader("Keep Raising模式 (下午)")
        kr_enabled = getattr(config, "KEEP_RAISING_ENABLED", False)
        kr_start = getattr(config, "KEEP_RAISING_START", "10:30")
        kr_end = getattr(config, "KEEP_RAISING_ENTRY_END", "15:45")
        st.markdown(f"""
        - 启用: **{'是' if kr_enabled else '否'}**
        - 窗口: **{kr_start} ~ {kr_end} EST**
        - 价格: **${getattr(config, 'KEEP_RAISING_PRICE_MIN', 1.0):.0f}** ~ **${getattr(config, 'KEEP_RAISING_PRICE_MAX', 200.0):.0f}**
        - Lookback: **{getattr(config, 'KR_LOOKBACK_MINUTES', 20)}** 分钟1-min bars
        - 评分: up_ratio × consistency × amplitude_quality
        - 阳线比 ≥ **{getattr(config, 'KR_MIN_UP_BARS_RATIO', 0.60):.0%}**
        - 一致性 ≥ **{getattr(config, 'KR_MIN_CONSISTENCY_SCORE', 0.40):.0%}**
        - 振幅质量 ≤ **{getattr(config, 'KR_MAX_AMPLITUDE_RATIO', 0.50):.0%}**
        """)

        st.subheader("仓位管理")
        max_daily = config.MAX_DAILY_TRADES if config.MAX_DAILY_TRADES > 0 else "无限制"
        st.markdown(f"""
        - 当前权益: **${config.INITIAL_CAPITAL:,.2f}**
        - 最大同时持仓: **{config.MAX_POSITIONS}** 只 (全仓单股)
        - 每日交易上限: **{max_daily}**
        - 日损失熔断: **{config.MAX_DAILY_LOSS_PCT:.0%}**
        """)

    with col2:
        st.subheader("ATR止损 + Gap扩展")
        st.markdown(f"""
        - 止损 = max(ATR×乘数, |gap|×{config.GAP_STOP_FACTOR:.0%}) / 入场价
        - 钳位: **{config.ATR_STOP_MIN_PCT:.0%}** ~ **{config.ATR_STOP_MAX_PCT:.0%}**
        - RVOL ≥ 10× → ATR×{config.ATR_MULT_TIERS[0][1]:.1f} | ≥ 5× → ATR×{config.ATR_MULT_TIERS[1][1]:.1f} | else → ATR×{config.ATR_MULT_TIERS[2][1]:.1f}
        - 追踪宽度 = ATR×{config.ATR_TRAIL_MULT:.1f} / 入场价, 钳位 0.5%~5%
        """)

        st.subheader("渐进Trailing (RTG)")
        for tier_profit, tier_trail in config.PROGRESSIVE_TRAIL_TIERS:
            st.markdown(f"- 利润 > {tier_profit:.0%} → trail = **{tier_trail:.1%}**")

        st.subheader("Keep Raising退出")
        kr_tiers = getattr(config, "KR_PROGRESSIVE_TRAIL_TIERS", [])
        st.markdown(f"""
        - 连续红线: **{getattr(config, 'KR_EXIT_CONSEC_DOWN_BARS', 3)}** 根 → 退出
        - 从高点回落: **{getattr(config, 'KR_EXIT_DROP_PCT', 0.005):.1%}** → 退出
        - 最大持仓: **{getattr(config, 'KR_EXIT_MAX_HOLD_MINUTES', 120)}** 分钟
        - 硬止损: **{getattr(config, 'KR_STOP_PCT', 0.03):.0%}** | Trail: **{getattr(config, 'KR_TRAIL_PCT', 0.010):.1%}**
        - Trail激活: **{getattr(config, 'KR_TRAIL_ACTIVATE_PCT', 0.01):.0%}**
        """)
        if kr_tiers:
            st.markdown("**KR渐进Trail:**")
            for tp, tt in kr_tiers:
                st.markdown(f"- 利润 > {tp:.0%} → trail = **{tt:.1%}**")

        st.subheader("日利润保护")
        st.markdown(f"""
        - 利润从峰值回撤 **{1-config.DAILY_PROFIT_PROTECT_RATIO:.0%}** → 平仓价格下行持仓
        - 保留价格上行持仓 (利润增加或亏损减少)
        - 触发后继续交易, 不终止当天
        - 激活阈值: 峰值 ≥ ${config.DAILY_PROFIT_PROTECT_MIN:.0f}
        - 延迟: 开盘后{config.DAILY_PROFIT_PROTECT_DELAY_SEC // 60}分钟
        """)

        st.subheader("强制平仓 & Re-entry")
        st.markdown(f"""
        - EOD强平: **{config.FORCE_CLOSE_TIME} EST**
        - Re-entry: **禁用** (opening drive is your only edge)
        """)

    st.divider()
    st.subheader("Keep Raising 1.0 设计理念")
    st.markdown("""
    - **上午RTG**: 09:30-10:30 与rtg_2.0一致 (gap scan + RTG entry + vol_surge + ATR stops)
    - **下午Keep Raising**: 10:30后扫描振幅小但持续上涨的股票, 全仓买入, 趋势破坏即卖出
    - **KR评分**: score = up_ratio × consistency × amplitude_quality, 选评分最高的股票
    - **KR退出**: 3根连续红线 / 从高点回落0.5% / 持仓超2小时 → 卖出后立即重新扫描
    - **ATR自适应止损**: stop=max(ATR×mult, |gap|×30%), 钳位2%-8%, 跳空股给宽止损
    - **渐进Trailing**: 利润>5%→1.5%, >10%→1%, >15%→0.5%, 让赢家奔跑
    - **日利润保护90%**: 峰值利润回撤10%→平仓下行持仓(保留上行), 继续交易
    - **全仓单股**: 最多1仓, 100%权益全仓买入最佳候选
    - **No Re-entry**: 首笔退出后不再入场 (上午RTG)
    """)

# ══════════════════════════════════════════════════════════════════
# Tab 3: 交易详情
# ══════════════════════════════════════════════════════════════════

elif tab == "交易详情":
    st.title("交易详情")

    state = None
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
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
    c6.metric("类型", t.get("trade_type", ""))

    st.divider()
    st.subheader("全部交易")
    trade_rows = []
    for t in trades:
        trade_rows.append({
            "股票": t["symbol"],
            "类型": t.get("trade_type", ""),
            "入场": f"${t.get('entry', 0):.4f}",
            "出场": f"${t.get('exit', 0):.4f}",
            "股数": t.get("shares", 0),
            "盈亏": f"${t.get('pnl', 0):+,.2f}",
            "原因": t.get("reason", ""),
        })
    st.dataframe(pd.DataFrame(trade_rows), hide_index=True, use_container_width=True)

# ══════════════════════════════════════════════════════════════════
# Tab 4: 日志
# ══════════════════════════════════════════════════════════════════

elif tab == "日志":
    st.title("实盘日志")
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text().strip().split("\n")
            # Show last 200 lines
            recent = lines[-200:]
            st.code("\n".join(recent), language="log")
        except Exception as e:
            st.error(f"读取日志失败: {e}")
    else:
        st.info("日志文件不存在")

    if st.button("刷新"):
        st.rerun()
