"""Stone 0.4.10 实盘监控 Dashboard"""

import json
import time
from pathlib import Path

import streamlit as st
import pandas as pd

VER_DIR = Path(__file__).parent
LOG_FILE = VER_DIR / "live_0410.log"
STATE_FILE = Path(__file__).parent.parent / "live_state.json"
REPORT_DIR = VER_DIR / "daily_reports"

st.set_page_config(page_title="Stone 0.4.10 Live", page_icon="📊", layout="wide")

st.title("Stone 0.4.10 实盘监控")
st.caption("Re-entry v2: 半仓 | ATR止损 | 双层止盈 | 保本 | 时间止损 | 自动调度")

# ── 历史日终报告 ──────────────────────────────────────────────────
if REPORT_DIR.exists():
    report_files = sorted(REPORT_DIR.glob("*.json"), reverse=True)[:10]
    if report_files:
        st.subheader("历史交易日报")
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

        # Expandable daily details
        with st.expander("查看每日交易明细"):
            for rf in report_files[:5]:
                try:
                    with open(rf) as f:
                        r = json.load(f)
                    date = r.get("date", rf.stem)
                    trades = r.get("trades", [])
                    if trades:
                        st.markdown(f"**{date}** ({len(trades)} trades, P&L ${r.get('daily_pnl', 0):+,.2f})")
                        t_rows = []
                        for t in trades:
                            t_rows.append({
                                "股票": t.get("symbol", "?"),
                                "类型": t.get("type", "?"),
                                "入场": f"${t.get('entry', 0):.2f}",
                                "出场": f"${t.get('exit', 0):.2f}",
                                "股数": t.get("shares", 0),
                                "出场原因": t.get("exit_reason", "?"),
                                "P&L": f"${t.get('pnl', 0):+,.2f}",
                            })
                        st.dataframe(pd.DataFrame(t_rows), hide_index=True, use_container_width=True)
                except Exception:
                    pass

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

    # 候选股
    if state.get("candidates"):
        st.subheader("今日选股")
        rows = []
        day_highs = state.get("day_highs", {})
        for c in state["candidates"]:
            sym = c["symbol"]
            high = day_highs.get(sym)
            rows.append({
                "股票": sym,
                "跳空": f"+{c['gap_pct']:.1%}",
                "开盘": f"${c['open_price']:.4f}",
                "昨收": f"${c['prev_close']:.4f}",
                "日最高": f"${high:.4f}" if high else "—",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    # 持仓
    if state.get("positions"):
        st.subheader("当前持仓")
        pos_rows = []
        for p in state["positions"]:
            extra = ""
            if p.get("trade_type") == "reentry":
                t1 = "Y" if p.get("reached_target1") else "N"
                be = "Y" if p.get("breakeven_active") else "N"
                bars = p.get("reentry_bar_count", 0)
                extra = f" | t1={t1} be={be} bars={bars}"
            pos_rows.append({
                "股票": p["symbol"],
                "类型": p["trade_type"],
                "数量": p["remaining_shares"],
                "入场价": f"${p['entry_price']:.4f}",
                "止损": f"${p['stop_price']:.4f}",
                "信息": extra,
            })
        st.dataframe(pd.DataFrame(pos_rows), hide_index=True, use_container_width=True)
    else:
        st.info("当前无持仓")

    # 事件日志
    if state.get("events"):
        st.subheader("事件日志 (最近30条)")
        for evt in reversed(state["events"][-30:]):
            if "BUY" in evt or "ENTERED" in evt or "TARGET" in evt or "FILLED" in evt:
                st.success(evt)
            elif "STOP" in evt or "TRAILING" in evt or "FORCE" in evt or "CLOSE" in evt:
                st.error(evt)
            elif "SELL" in evt or "PARTIAL" in evt:
                st.warning(evt)
            else:
                st.info(evt)
else:
    st.warning("未找到 live_state.json — 实盘未运行或状态文件不存在")

# ── 实时日志 ──────────────────────────────────────────────────────
st.divider()
st.subheader("实时日志")

if LOG_FILE.exists():
    with open(LOG_FILE) as f:
        lines = f.readlines()
    tail = lines[-80:] if len(lines) > 80 else lines
    st.code("".join(tail), language="log")
else:
    st.info("日志文件尚未生成（等待实盘启动）")

# ── Auto refresh ──────────────────────────────────────────────────
st.divider()
auto = st.checkbox("自动刷新 (5秒)", value=True)
if auto:
    time.sleep(5)
    st.rerun()
