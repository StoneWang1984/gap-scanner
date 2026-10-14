"""Momentum Surge 1.0 策略 — Streamlit Web UI (交易显示 + 策略概览 + 交易详情)"""

import json
import time
from pathlib import Path

import streamlit as st
import pandas as pd

VERSION_DIR = Path("/Users/stonewang2014/gap-scanner/stonewang_daytrade_momentum_surge_1.0")
STATE_FILE = Path("/Users/stonewang2014/gap-scanner/live_state.json")
LOG_FILE = VERSION_DIR / "live_rtg.log"

import importlib.util, sys
_spec = importlib.util.spec_from_file_location("config", VERSION_DIR / "config.py")
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)
sys.modules["config"] = config

st.set_page_config(page_title="Momentum Surge 交易", page_icon="📊", layout="wide")

# ── Sidebar ──────────────────────────────────────────────────────

st.sidebar.title("Momentum Surge 1.0")
st.sidebar.caption(f"三叉动量 | {config.RTG_STOP_PCT:.0%}止损+追踪+{config.RTG_TIME_LIMIT_SEC//60}min限时")

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

    daily_pnl_from_trades = 0.0
    try:
        with open("/Users/stonewang2014/gap-scanner/live_state.json") as f:
            _s = json.load(f)
        daily_pnl_from_trades = sum(t.get("pnl", 0) for t in _s.get("trades_detail", []))
    except Exception:
        pass
    pnl = daily_pnl_from_trades if daily_pnl_from_trades != 0.0 else (equity - last_equity if last_equity > 0 else 0.0)
    pnl_pct = pnl / equity if equity > 0 else 0.0
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
                row["Trail"] = f"{sp.get('trail_activate_pct', 0):.0%}/{sp.get('trail_pct', 0):.1%}"
                row["最高"] = f"${sp.get('highest', 0):.4f}"
                row["追踪激活"] = "✓" if sp.get("trail_active", False) else "✗"

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
                "Trail": f"{p.get('trail_activate_pct', 0):.0%}/{p.get('trail_pct', 0):.1%}",
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
            gap_pct = c.get("gap_pct", 0)
            sig = "reversal" if gap_pct < 0 else "rtg"
            cand_rows.append({
                "股票": c["symbol"],
                "信号": sig.upper(),
                "跳空": f"{gap_pct:+.1%}",
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

            entry_ts = trades[0].get("entry_ts", "")
            exit_ts = trades[-1].get("exit_ts", "")

            summary_rows.append({
                "股票": sym,
                "类型": trade_type,
                "买入时间": entry_ts,
                "卖出时间": exit_ts,
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
    st.title("Momentum Surge 1.0 策略概览")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("三叉入场")
        st.markdown(f"""
        **Prong 1: 小跳空扫描 (gap ≥ {config.GAP_THRESHOLD:.0%})**
        - RTG入场: close > open_price + 量能 ≥ {config.RTG_VOLUME_MULT}× 前bar
        - 捕获VEEA/CANG类小gap机会

        **Prong 2: 盘中动量扫描 (每5分钟)**
        - 价格涨 >{getattr(config, 'MOMENTUM_MIN_GAIN_PCT', 0.03):.0%} from open
        - 5min量能 >{getattr(config, 'MOMENTUM_VOLUME_SURGE_MULT', 2.0):.1f}× 均量
        - 突破{getattr(config, 'MOMENTUM_BREAKOUT_LOOKBACK_MIN', 30)}分钟新高

        **Prong 3: 低开反弹 (gap {getattr(config, 'REVERSAL_GAP_MIN', -0.10):+.0%}~{getattr(config, 'REVERSAL_GAP_MAX', -0.03):+.0%})**
        - 价格穿越回open_price上方 + 量能确认
        - RVOL ≥ {getattr(config, 'REVERSAL_MIN_RVOL', 3.0):.1f}×
        """)

        st.subheader("盘前扫描")
        st.markdown(f"""
        - 跳空幅度: gap ≥ **{config.GAP_THRESHOLD:.0%}** 或 gap ≤ **{getattr(config, 'REVERSAL_GAP_MAX', -0.03):+.0%}**
        - 盘前成交量 > **{config.MIN_VOLUME:,}** 股
        - 价格区间 **${config.PRICE_MIN}** ~ **${config.PRICE_MAX}**
        - 杠杆ETF / Crypto ETF: **已过滤**
        - 候选股: **Top {config.MAX_CANDIDATES} by RVOL**
        - 入场窗口: **{config.ENTRY_WINDOW_START} ~ {config.ENTRY_WINDOW_END} EST**
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
        st.subheader("退出规则")
        time_min = config.RTG_TIME_LIMIT_SEC // 60
        trail_act = config.TRAILING_ACTIVATE_PCT if getattr(config, "TRAILING_STOP_ENABLED", False) else 0
        trail_p = config.TRAILING_PCT if getattr(config, "TRAILING_STOP_ENABLED", False) else 0
        st.markdown(f"""
        1. **{config.RTG_STOP_PCT:.0%}硬止损**: 价格跌破入场价×{1-config.RTG_STOP_PCT:.2f} → `stop_loss`
        2. **追踪止损**: 利润达+{trail_act:.0%}激活，回撤{trail_p:.0%}平仓 → `trailing_stop`
        3. **{time_min}分钟限时**: 持有超过{config.RTG_TIME_LIMIT_SEC}秒 → `time_limit`
        4. **日亏损熔断**: 当日亏损达{config.MAX_DAILY_LOSS_PCT:.0%} → `circuit_breaker`
        5. **EOD强平**: **{config.FORCE_CLOSE_TIME} EST** → `force_close`
        """)

        st.subheader("Re-entry")
        re_cd = getattr(config, "REENTRY_COOLDOWN_SEC", 60)
        re_stop_cd = getattr(config, "REENTRY_STOP_COOLDOWN_SEC", 120)
        re_price = getattr(config, "REENTRY_MAX_PRICE_VS_OPEN", 1.15)
        st.markdown(f"""
        - Re-entry: **允许** (最多{config.RTG_REENTRY_MAX}次/股/天)
        - 普通冷却: **{re_cd}秒** | 止损冷却: **{re_stop_cd}秒**
        - 不追高: 入场价 < **{re_price:.0%}** × open_price
        """)

    st.divider()
    st.subheader("Momentum Surge 1.0 设计理念")
    st.markdown(f"""
    - **三叉入口**: 小gap RTG + 盘中动量 + 低开反弹, 覆盖90%日内大涨机会
    - **追踪止损**: +{trail_act:.0%}激活/{trail_p:.0%}回撤, 锁住大波段利润 (回测50%exit)
    - **{time_min}分钟限时**: 比原来3分钟多7分钟, 给动量股更多发展时间
    - **日亏损熔断{config.MAX_DAILY_LOSS_PCT:.0%}**: 安全网, 防止单日大亏
    - **全仓单股**: 最多1仓, 100%权益全仓买入最佳候选
    - **Re-entry**: 允许, 最多{config.RTG_REENTRY_MAX}次/股/天
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
            recent = lines[-200:]
            st.code("\n".join(recent), language="log")
        except Exception as e:
            st.error(f"读取日志失败: {e}")
    else:
        st.info("日志文件不存在")

    if st.button("刷新"):
        st.rerun()
