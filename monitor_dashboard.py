"""Stone Monitor Dashboard — 系统监控面板 (port 8502)"""

import json
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

PROJECT_ROOT = Path(__file__).parent.resolve()
MONITOR_LOG = PROJECT_ROOT / "monitor.log"
MONITOR_STATE = PROJECT_ROOT / "monitor_state.json"
TRADING_LOG = PROJECT_ROOT / "versions" / "live_0417.log"
STATE_FILE = PROJECT_ROOT / "live_state.json"

TZ_EST = ZoneInfo("America/New_York")

st.set_page_config(page_title="系统监控", page_icon="🛡️", layout="wide")

st.title("🛡️ Stone 1.0 系统监控")
st.caption("进程状态 | WS连接 | 工作日志")


# ── Helper ────────────────────────────────────────────────────────

def _load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return None


def _parse_latest_report():
    """Parse the most recent complete report from monitor.log."""
    if not MONITOR_LOG.exists():
        return None
    lines = MONITOR_LOG.read_text().splitlines()
    report_lines = []
    found = False
    for line in reversed(lines):
        if "下次报告" in line:
            found = True
        if found:
            report_lines.insert(0, line)
        if "监控报告" in line:
            break
    if not report_lines:
        return None
    return "\n".join(report_lines)


def _tail_file(path, n=60):
    if not path.exists():
        return "日志文件尚未生成"
    with open(path) as f:
        lines = f.readlines()
    return "".join(lines[-n:] if len(lines) > n else lines)


# ══════════════════════════════════════════════════════════════════
# 1. 系统状态概览
# ══════════════════════════════════════════════════════════════════

monitor_state = _load_json(MONITOR_STATE)
trading_state = _load_json(STATE_FILE)
now_est = datetime.now(TZ_EST)

# Monitor process
if monitor_state:
    heartbeat = monitor_state.get("last_heartbeat", "")
    hb_time = None
    if heartbeat:
        try:
            hb_time = datetime.fromisoformat(heartbeat)
            if hb_time.tzinfo is None:
                hb_time = hb_time.replace(tzinfo=TZ_EST)
        except Exception:
            pass
    monitor_age = (now_est - hb_time).total_seconds() / 60 if hb_time else 999
    monitor_alive = monitor_age < 5
else:
    monitor_alive = False
    monitor_age = 999

# Trading process
trading_alive = False
state_age = 999
if trading_state:
    updated_str = trading_state.get("updated", "")
    try:
        updated = datetime.fromisoformat(updated_str)
        now_local = datetime.now()
        if updated.tzinfo is not None:
            updated = updated.astimezone().replace(tzinfo=None)
        state_age = (now_local - updated).total_seconds() / 60
        trading_alive = state_age < 5
    except Exception:
        pass

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("监控系统", "🟢 运行中" if monitor_alive else "🔴 已停止",
          f"心跳 {monitor_age:.1f}分钟前" if monitor_alive else "无心跳")
c2.metric("交易系统", "🟢 运行中" if trading_alive else "🔴 已停止",
          f"状态更新 {state_age:.1f}分钟前" if trading_alive else "无状态更新")

daily_stopped = trading_state.get("daily_stopped", False) if trading_state else False
c3.metric("交易状态", "⛔ 已熔断" if daily_stopped else "✅ 正常",
          f"今日 {trading_state.get('daily_trades', 0)} 笔" if trading_state else "")

# WS / data feed
ws_connected = trading_state.get("ws_connected", None) if trading_state else None
ws_last_msg = trading_state.get("ws_last_msg_age", None) if trading_state else None
data_feed = trading_state.get("data_feed", "—") if trading_state else "—"
if ws_connected is True:
    c4.metric("WS连接", "🟢 已连接", f"{data_feed} | 消息 {ws_last_msg:.0f}s前" if ws_last_msg is not None else f"{data_feed} 实时")
elif ws_connected is False:
    c4.metric("WS连接", "🟡 快照轮询", "WS断连")
else:
    c4.metric("WS连接", "⚪ 未启动", "交易未运行")

# Monitor date
monitor_date = monitor_state.get("trading_date", "—") if monitor_state else "—"
c5.metric("监控日期", monitor_date,
          f"重启 {monitor_state.get('restart_count', 0)} 次" if monitor_state else "")

# ══════════════════════════════════════════════════════════════════
# 2. 监控报告
# ══════════════════════════════════════════════════════════════════

st.divider()
st.subheader("📊 最新监控报告")

latest_report = _parse_latest_report()
if latest_report:
    st.code(latest_report, language="log")
else:
    st.info("暂无监控报告")

# ══════════════════════════════════════════════════════════════════
# 3. 监控内部状态
# ══════════════════════════════════════════════════════════════════

if monitor_state:
    with st.expander("🔍 监控内部状态"):
        fixes = monitor_state.get("fixes_today", [])
        m1, m2 = st.columns(2)
        m1.metric("今日重启次数", str(monitor_state.get("restart_count", 0)))
        m2.metric("今日取消单数", str(monitor_state.get("cancel_count", 0)))

        if fixes:
            st.markdown("**今日修复记录**")
            fix_rows = []
            for f in fixes:
                fix_rows.append({
                    "时间": f.get("time", "")[11:19] if f.get("time") else "",
                    "类型": f.get("type", ""),
                    "描述": f.get("desc", ""),
                    "成功": "✅" if f.get("success") else "❌",
                })
            st.dataframe(fix_rows, hide_index=True, use_container_width=True)
        else:
            st.info("今日无修复记录")

# ══════════════════════════════════════════════════════════════════
# 4. 交易系统日志
# ══════════════════════════════════════════════════════════════════

st.divider()
st.subheader("📜 交易系统日志 (最近60行)")
st.code(_tail_file(TRADING_LOG), language="log")

# ══════════════════════════════════════════════════════════════════
# 5. 监控系统日志
# ══════════════════════════════════════════════════════════════════

st.subheader("📜 监控系统日志 (最近60行)")
st.code(_tail_file(MONITOR_LOG), language="log")

# ══════════════════════════════════════════════════════════════════
# Auto refresh
# ══════════════════════════════════════════════════════════════════

st.divider()
auto = st.checkbox("自动刷新 (1分钟)", value=True)
if auto:
    time.sleep(60)
    st.rerun()
