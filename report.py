"""Report generation and visualization for backtest results."""

import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

import config
from strategy import TradeResult


def build_trade_table(trades: list[TradeResult]) -> pd.DataFrame:
    """Build a DataFrame from trade results."""
    rows = []
    for t in trades:
        rows.append({
            "Date": t.date,
            "Symbol": t.symbol,
            "Entry": f"${t.entry_price:.4f}",
            "Exit": f"${t.exit_price:.4f}",
            "Shares": t.shares,
            "P&L": f"${t.pnl:.2f}",
            "P&L %": f"{t.pnl_pct:.2%}",
            "Exit Reason": t.exit_reason,
        })
    return pd.DataFrame(rows)


def build_equity_curve(trades: list[TradeResult]) -> pd.Series:
    """Build equity curve from trade results."""
    equity = config.INITIAL_CAPITAL
    curve = []
    for t in trades:
        equity += t.pnl
        curve.append({"date": t.date, "equity": equity})
    df = pd.DataFrame(curve)
    if df.empty:
        return pd.Series(dtype=float)
    return df.set_index("date")["equity"]


def calc_summary(trades: list[TradeResult]) -> dict:
    """Calculate summary statistics."""
    if not trades:
        return {}

    pnls = [t.pnl for t in trades]
    pnl_pcts = [t.pnl_pct for t in trades]
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    # Equity curve for drawdown
    equity_curve = build_equity_curve(trades)
    if not equity_curve.empty:
        peak = equity_curve.cummax()
        drawdown = (equity_curve - peak) / peak
        max_drawdown = drawdown.min()
    else:
        max_drawdown = 0.0

    # Sharpe ratio (annualized, assuming ~252 trading days)
    if len(pnls) > 1:
        avg_return = sum(pnl_pcts) / len(pnl_pcts)
        std_return = math.sqrt(sum((r - avg_return) ** 2 for r in pnl_pcts) / (len(pnl_pcts) - 1))
        sharpe = (avg_return / std_return) * math.sqrt(252) if std_return > 0 else 0.0
    else:
        sharpe = 0.0

    exit_reasons = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    total_pnl = sum(pnls)

    return {
        "Total Trades": len(trades),
        "Winning Trades": len(wins),
        "Losing Trades": len(losses),
        "Win Rate": f"{len(wins)/len(trades):.1%}" if trades else "N/A",
        "Total P&L": f"${total_pnl:.2f}",
        "Avg P&L per Trade": f"${total_pnl/len(trades):.2f}",
        "Avg Win": f"${sum(t.pnl for t in wins)/len(wins):.2f}" if wins else "$0.00",
        "Avg Loss": f"${sum(t.pnl for t in losses)/len(losses):.2f}" if losses else "$0.00",
        "Best Trade": f"${max(pnls):.2f}",
        "Worst Trade": f"${min(pnls):.2f}",
        "Max Drawdown": f"{max_drawdown:.2%}",
        "Sharpe Ratio": f"{sharpe:.2f}",
        "Exit Reasons": exit_reasons,
    }


def print_report(trades: list[TradeResult]):
    """Print backtest report to console."""
    print("\n" + "=" * 70)
    print("  GAP PULLBACK STRATEGY — BACKTEST REPORT")
    print("=" * 70)

    # Summary
    summary = calc_summary(trades)
    print("\n--- Summary ---")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    # Trade table
    df = build_trade_table(trades)
    if not df.empty:
        print("\n--- Trade Details ---")
        print(df.to_string(index=False))

    # Final equity
    total_pnl = sum(t.pnl for t in trades)
    print(f"\n  Initial Capital: ${config.INITIAL_CAPITAL:.2f}")
    print(f"  Final Equity:    ${config.INITIAL_CAPITAL + total_pnl:.2f}")
    print(f"  Return:          {total_pnl/config.INITIAL_CAPITAL:.2%}")
    print("=" * 70)


def plot_results(trades: list[TradeResult], output_dir: str = "."):
    """Generate and save equity curve and P&L distribution charts."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    if not trades:
        print("No trades to plot.")
        return

    equity_curve = build_equity_curve(trades)
    pnls = [t.pnl for t in trades]
    pnl_pcts = [t.pnl_pct * 100 for t in trades]

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("Gap Pullback Strategy — Backtest Results", fontsize=14, fontweight="bold")

    # 1. Equity curve
    ax = axes[0, 0]
    if not equity_curve.empty:
        equity_curve.plot(ax=ax, color="#2196F3", linewidth=1.5)
        ax.axhline(y=config.INITIAL_CAPITAL, color="gray", linestyle="--", alpha=0.5)
        ax.set_title("Equity Curve")
        ax.set_ylabel("Equity ($)")
        ax.tick_params(axis="x", rotation=45)

    # 2. P&L distribution
    ax = axes[0, 1]
    colors = ["#4CAF50" if p >= 0 else "#F44336" for p in pnls]
    ax.bar(range(len(pnls)), pnls, color=colors, width=0.8)
    ax.axhline(y=0, color="black", linewidth=0.5)
    ax.set_title("P&L per Trade")
    ax.set_ylabel("P&L ($)")
    ax.set_xlabel("Trade #")

    # 3. P&L % histogram
    ax = axes[1, 0]
    ax.hist(pnl_pcts, bins=20, color="#FF9800", edgecolor="white", alpha=0.8)
    ax.axvline(x=0, color="black", linewidth=0.5)
    ax.set_title("P&L % Distribution")
    ax.set_xlabel("P&L %")
    ax.set_ylabel("Frequency")

    # 4. Exit reason pie
    ax = axes[1, 1]
    exit_counts = {}
    for t in trades:
        exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1
    labels = list(exit_counts.keys())
    sizes = list(exit_counts.values())
    pie_colors = {"take_profit": "#4CAF50", "stop_loss": "#F44336", "force_close": "#FF9800"}
    ax.pie(sizes, labels=labels, autopct="%1.1f%%",
           colors=[pie_colors.get(l, "#999") for l in labels])
    ax.set_title("Exit Reasons")

    plt.tight_layout()
    chart_path = output_path / "backtest_results.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\nChart saved to: {chart_path}")


def _draw_candlestick(ax, bars: list[dict], width_minutes: float = 4.0):
    """Draw candlestick chart on given axes."""
    for bar in bars:
        t = bar["timestamp"]
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        color = "#26A69A" if c >= o else "#EF5350"
        # Wick
        ax.plot([t, t], [l, h], color=color, linewidth=0.8, solid_capstyle="round")
        # Body
        body_bottom = min(o, c)
        body_height = abs(c - o)
        if body_height < 1e-6:
            body_height = abs(h - l) * 0.05  # doji: thin line
        w = width_minutes / (24 * 60)  # convert minutes to day fraction
        ax.bar(t, body_height, bottom=body_bottom, width=w, color=color, edgecolor=color, linewidth=0.5)


def _find_entry_bar_index(bars: list[dict], entry_price: float) -> int:
    """Find the first bar where price reaches the entry level (pullback low)."""
    for i, bar in enumerate(bars):
        if bar["low"] <= entry_price:
            return i
    return 0


def _find_exit_bar_index(bars: list[dict], exit_price: float, exit_reason: str, start_idx: int) -> int:
    """Find the bar where the exit is triggered."""
    for i in range(start_idx, len(bars)):
        bar = bars[i]
        if exit_reason == "stop_loss" and bar["low"] <= exit_price:
            return i
        if exit_reason == "take_profit" and bar["high"] >= exit_price:
            return i
    return len(bars) - 1


def plot_trade_charts(trades: list[TradeResult], output_dir: str = "."):
    """Generate candlestick charts with buy/sell markers for each trade."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    chart_dir = output_path / "trade_charts"
    chart_dir.mkdir(parents=True, exist_ok=True)

    if not trades:
        print("No trades to chart.")
        return

    # Pick top trades to chart (up to 20 most interesting)
    # Prioritize: stop losses first (lessons), then biggest wins
    sorted_trades = sorted(trades, key=lambda t: (t.exit_reason == "stop_loss", abs(t.pnl)), reverse=True)
    trades_to_chart = sorted_trades[:20]

    for trade in trades_to_chart:
        if trade.bars_5m is None or len(trade.bars_5m) < 2:
            continue

        bars = trade.bars_5m

        fig, (ax_price, ax_vol) = plt.subplots(
            2, 1, figsize=(16, 8), height_ratios=[3, 1],
            gridspec_kw={"hspace": 0.15},
        )

        # Draw candlesticks
        _draw_candlestick(ax_price, bars)

        # Find entry and exit bar indices
        entry_idx = _find_entry_bar_index(bars, trade.entry_price)
        exit_idx = _find_exit_bar_index(bars, trade.exit_price, trade.exit_reason, entry_idx + 1)

        entry_time = bars[entry_idx]["timestamp"]
        exit_time = bars[exit_idx]["timestamp"]

        # Draw reference lines
        if trade.open_price > 0:
            ax_price.axhline(y=trade.open_price, color="#2196F3", linestyle="--", linewidth=0.8, alpha=0.6, label=f"Open ${trade.open_price:.4f}")
        if trade.sell_target > 0:
            ax_price.axhline(y=trade.sell_target, color="#4CAF50", linestyle="--", linewidth=0.8, alpha=0.6, label=f"Target ${trade.sell_target:.4f}")
        if trade.stop_price > 0:
            ax_price.axhline(y=trade.stop_price, color="#F44336", linestyle="--", linewidth=0.8, alpha=0.6, label=f"Stop ${trade.stop_price:.4f}")

        # Entry marker (green triangle up)
        ax_price.plot(entry_time, trade.entry_price, marker="^", color="#00E676",
                      markersize=16, markeredgecolor="black", markeredgewidth=1.2, zorder=5)
        ax_price.annotate(
            f"BUY ${trade.entry_price:.4f}",
            xy=(entry_time, trade.entry_price),
            xytext=(15, -25), textcoords="offset points",
            fontsize=9, fontweight="bold", color="#00C853",
            arrowprops=dict(arrowstyle="->", color="#00C853", lw=1.2),
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#E8F5E9", edgecolor="#00C853", alpha=0.9),
        )

        # Exit marker (red triangle down for loss, green diamond for profit)
        is_loss = trade.exit_reason == "stop_loss"
        exit_color = "#FF1744" if is_loss else "#00E676"
        exit_marker = "v" if is_loss else "D"
        exit_label = "SELL" if is_loss else "TP"

        ax_price.plot(exit_time, trade.exit_price, marker=exit_marker, color=exit_color,
                      markersize=14, markeredgecolor="black", markeredgewidth=1.2, zorder=5)
        ax_price.annotate(
            f"{exit_label} ${trade.exit_price:.4f}",
            xy=(exit_time, trade.exit_price),
            xytext=(15, 20), textcoords="offset points",
            fontsize=9, fontweight="bold", color=exit_color,
            arrowprops=dict(arrowstyle="->", color=exit_color, lw=1.2),
            bbox=dict(boxstyle="round,pad=0.3",
                      facecolor="#FFEBEE" if is_loss else "#E8F5E9",
                      edgecolor=exit_color, alpha=0.9),
        )

        # P&L label
        pnl_color = "#4CAF50" if trade.pnl >= 0 else "#F44336"
        pnl_sign = "+" if trade.pnl >= 0 else ""
        ax_price.text(
            0.98, 0.95,
            f"P&L: {pnl_sign}${trade.pnl:.2f} ({pnl_sign}{trade.pnl_pct:.2%})",
            transform=ax_price.transAxes, fontsize=11, fontweight="bold",
            color=pnl_color, ha="right", va="top",
            bbox=dict(boxstyle="round,pad=0.4", facecolor="white", edgecolor=pnl_color, alpha=0.9),
        )

        ax_price.set_title(
            f"{trade.symbol} — {trade.date}  |  {trade.exit_reason.replace('_', ' ').title()}  |  {trade.shares} shares",
            fontsize=12, fontweight="bold",
        )
        ax_price.set_ylabel("Price ($)")
        ax_price.legend(loc="upper left", fontsize=8, framealpha=0.8)
        ax_price.grid(True, alpha=0.2)
        ax_price.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

        # Volume bars
        timestamps = [bar["timestamp"] for bar in bars]
        volumes = [bar["volume"] for bar in bars]
        vol_colors = ["#26A69A" if bars[i]["close"] >= bars[i]["open"] else "#EF5350" for i in range(len(bars))]
        w = 4.0 / (24 * 60)
        ax_vol.bar(timestamps, volumes, width=w, color=vol_colors, alpha=0.7)
        ax_vol.set_ylabel("Volume")
        ax_vol.grid(True, alpha=0.2)
        ax_vol.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

        plt.tight_layout()
        filename = f"{trade.date}_{trade.symbol}_{trade.exit_reason}.png"
        chart_path = chart_dir / filename
        plt.savefig(chart_path, dpi=150, bbox_inches="tight")
        plt.close()

    # Summary chart: grid of 4 representative trades
    _plot_summary_grid(trades_to_chart[:4], chart_dir)

    print(f"\nK-line charts saved to: {chart_dir}/")
    print(f"  Individual charts: {len(trades_to_chart)} files")
    print(f"  Summary grid: trade_charts/summary_grid.png")


def _plot_summary_grid(trades: list[TradeResult], chart_dir: Path):
    """Plot a 2x2 grid of trade charts for quick overview."""
    if len(trades) == 0:
        return

    n = min(len(trades), 4)
    fig, axes = plt.subplots(2, 2, figsize=(20, 12))
    fig.suptitle("Gap Pullback Strategy — Trade Examples", fontsize=14, fontweight="bold", y=0.98)

    for idx in range(4):
        ax = axes[idx // 2, idx % 2]
        if idx >= n:
            ax.set_visible(False)
            continue

        trade = trades[idx]
        if trade.bars_5m is None or len(trade.bars_5m) < 2:
            ax.set_visible(False)
            continue

        bars = trade.bars_5m
        _draw_candlestick(ax, bars, width_minutes=3.5)

        entry_idx = _find_entry_bar_index(bars, trade.entry_price)
        exit_idx = _find_exit_bar_index(bars, trade.exit_price, trade.exit_reason, entry_idx + 1)
        entry_time = bars[entry_idx]["timestamp"]
        exit_time = bars[exit_idx]["timestamp"]

        # Reference lines
        if trade.open_price > 0:
            ax.axhline(y=trade.open_price, color="#2196F3", linestyle="--", linewidth=0.7, alpha=0.5)
        if trade.sell_target > 0:
            ax.axhline(y=trade.sell_target, color="#4CAF50", linestyle="--", linewidth=0.7, alpha=0.5)
        if trade.stop_price > 0:
            ax.axhline(y=trade.stop_price, color="#F44336", linestyle="--", linewidth=0.7, alpha=0.5)

        # Entry/exit markers
        is_loss = trade.exit_reason == "stop_loss"
        ax.plot(entry_time, trade.entry_price, marker="^", color="#00E676",
                markersize=12, markeredgecolor="black", markeredgewidth=1, zorder=5)
        exit_marker = "v" if is_loss else "D"
        exit_color = "#FF1744" if is_loss else "#00E676"
        ax.plot(exit_time, trade.exit_price, marker=exit_marker, color=exit_color,
                markersize=10, markeredgecolor="black", markeredgewidth=1, zorder=5)

        pnl_sign = "+" if trade.pnl >= 0 else ""
        pnl_color = "#4CAF50" if trade.pnl >= 0 else "#F44336"
        ax.set_title(
            f"{trade.symbol} {trade.date} | {pnl_sign}${trade.pnl:.0f} ({pnl_sign}{trade.pnl_pct:.1%}) | {trade.exit_reason}",
            fontsize=10, fontweight="bold", color=pnl_color,
        )
        ax.set_ylabel("Price ($)", fontsize=8)
        ax.grid(True, alpha=0.2)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.tick_params(axis="both", labelsize=7)

    plt.tight_layout()
    grid_path = chart_dir / "summary_grid.png"
    plt.savefig(grid_path, dpi=150, bbox_inches="tight")
    plt.close()
