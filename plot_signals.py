"""Plot buy/sell signal charts — clear price vs time with entry/exit markers."""

from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

from strategy import TradeResult


def _find_entry_bar_index(bars: list[dict], entry_price: float) -> int:
    for i, bar in enumerate(bars):
        if bar["low"] <= entry_price:
            return i
    return 0


def _find_exit_bar_index(bars: list[dict], exit_price: float, exit_reason: str, start_idx: int) -> int:
    for i in range(start_idx, len(bars)):
        bar = bars[i]
        if exit_reason == "stop_loss" and bar["low"] <= exit_price:
            return i
        if exit_reason == "take_profit" and bar["high"] >= exit_price:
            return i
    return len(bars) - 1


def plot_single_trade(trade: TradeResult, ax: plt.Axes):
    """Draw a single trade on given axes with clear buy/sell markers."""
    bars = trade.bars_5m
    if bars is None or len(bars) < 2:
        return

    timestamps = [bar["timestamp"] for bar in bars]
    closes = [bar["close"] for bar in bars]
    highs = [bar["high"] for bar in bars]
    lows = [bar["low"] for bar in bars]

    # Price line with shaded high-low range
    ax.fill_between(timestamps, lows, highs, alpha=0.15, color="#90CAF9", label="_nolegend_")
    ax.plot(timestamps, closes, color="#1565C0", linewidth=1.5, label="Price")

    # Reference lines
    ax.axhline(y=trade.open_price, color="#42A5F5", linestyle="--", linewidth=1, alpha=0.7,
               label=f"Open ${trade.open_price:.4f}")
    ax.axhline(y=trade.sell_target, color="#66BB6A", linestyle="--", linewidth=1, alpha=0.7,
               label=f"Target ${trade.sell_target:.4f}")
    ax.axhline(y=trade.stop_price, color="#EF5350", linestyle="--", linewidth=1, alpha=0.7,
               label=f"Stop ${trade.stop_price:.4f}")

    # Entry and exit indices
    entry_idx = _find_entry_bar_index(bars, trade.entry_price)
    exit_idx = _find_exit_bar_index(bars, trade.exit_price, trade.exit_reason, entry_idx + 1)
    entry_time = bars[entry_idx]["timestamp"]
    exit_time = bars[exit_idx]["timestamp"]

    # BUY marker — large green triangle up
    ax.scatter([entry_time], [trade.entry_price], marker="^", s=250, c="#00E676",
               edgecolors="black", linewidths=1.5, zorder=10)
    ax.annotate(
        f"BUY\n${trade.entry_price:.4f}",
        xy=(entry_time, trade.entry_price),
        xytext=(30, -35), textcoords="offset points",
        fontsize=9, fontweight="bold", color="#1B5E20",
        ha="center",
        bbox=dict(boxstyle="round,pad=0.4", facecolor="#C8E6C9", edgecolor="#2E7D32", alpha=0.95),
        arrowprops=dict(arrowstyle="-|>", color="#2E7D32", lw=2),
    )

    # SELL marker — large red/green triangle down/diamond
    is_loss = trade.exit_reason == "stop_loss"
    if is_loss:
        marker, color, label_text = "v", "#FF1744", "SELL (Stop)"
    elif trade.exit_reason == "force_close":
        marker, color, label_text = "X", "#FF9800", "SELL (EOD)"
    else:
        marker, color, label_text = "D", "#00C853", "SELL (TP)"

    ax.scatter([exit_time], [trade.exit_price], marker=marker, s=250, c=color,
               edgecolors="black", linewidths=1.5, zorder=10)
    ax.annotate(
        f"{label_text}\n${trade.exit_price:.4f}",
        xy=(exit_time, trade.exit_price),
        xytext=(-30, 35), textcoords="offset points",
        fontsize=9, fontweight="bold", color="#B71C1C" if is_loss else "#1B5E20",
        ha="center",
        bbox=dict(boxstyle="round,pad=0.4",
                  facecolor="#FFCDD2" if is_loss else "#C8E6C9",
                  edgecolor="#C62828" if is_loss else "#2E7D32", alpha=0.95),
        arrowprops=dict(arrowstyle="-|>", color="#C62828" if is_loss else "#2E7D32", lw=2),
    )

    # Connect entry to exit with arrow
    ax.annotate("", xy=(exit_time, trade.exit_price), xytext=(entry_time, trade.entry_price),
                arrowprops=dict(arrowstyle="->", color="#9E9E9E", lw=1.2, linestyle=":", connectionstyle="arc3,rad=0.2"))

    # P&L box
    pnl_sign = "+" if trade.pnl >= 0 else ""
    pnl_color = "#2E7D32" if trade.pnl >= 0 else "#C62828"
    ax.text(
        0.98, 0.95,
        f"P&L: {pnl_sign}${trade.pnl:.2f}\n{pnl_sign}{trade.pnl_pct:.2%}\n{trade.shares} shares",
        transform=ax.transAxes, fontsize=10, fontweight="bold",
        color=pnl_color, ha="right", va="top", family="monospace",
        bbox=dict(boxstyle="round,pad=0.5", facecolor="white", edgecolor=pnl_color, alpha=0.95),
    )

    ax.set_title(
        f"{trade.symbol}  {trade.date}",
        fontsize=12, fontweight="bold",
    )
    ax.set_xlabel("Time (EST)")
    ax.set_ylabel("Price ($)")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.85)
    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))


def plot_all_signal_charts(trades: list[TradeResult], output_dir: str = "."):
    """Generate clear buy/sell signal charts for all trades."""
    output_path = Path(output_dir)
    signal_dir = output_path / "signal_charts"
    signal_dir.mkdir(parents=True, exist_ok=True)

    trades_with_bars = [t for t in trades if t.bars_5m and len(t.bars_5m) >= 2]
    if not trades_with_bars:
        print("No trades with bar data to plot.")
        return

    # Individual charts
    for trade in trades_with_bars:
        fig, ax = plt.subplots(figsize=(16, 8))
        plot_single_trade(trade, ax)
        plt.tight_layout()
        filename = f"{trade.date}_{trade.symbol}_{trade.exit_reason}.png"
        fig.savefig(signal_dir / filename, dpi=150, bbox_inches="tight")
        plt.close(fig)

    # Summary grids — 4 per page
    n = len(trades_with_bars)
    pages = (n + 3) // 4
    for page in range(pages):
        start = page * 4
        end = min(start + 4, n)
        page_trades = trades_with_bars[start:end]

        rows = 2 if len(page_trades) > 2 else 1
        cols = 2
        fig, axes = plt.subplots(rows, cols, figsize=(22, rows * 7))
        if rows == 1 and cols == 2:
            axes = axes.reshape(1, -1)

        for idx, trade in enumerate(page_trades):
            ax = axes[idx // 2, idx % 2]
            plot_single_trade(trade, ax)

        # Hide unused subplots
        for idx in range(len(page_trades), rows * cols):
            axes[idx // 2, idx % 2].set_visible(False)

        fig.suptitle(
            f"Gap Pullback Strategy — Buy/Sell Signals (Page {page + 1}/{pages})",
            fontsize=14, fontweight="bold", y=1.01,
        )
        plt.tight_layout()
        fig.savefig(signal_dir / f"signals_page_{page + 1}.png", dpi=150, bbox_inches="tight")
        plt.close(fig)

    print(f"\nSignal charts saved to: {signal_dir}/")
    print(f"  Individual: {len(trades_with_bars)} charts")
    print(f"  Grid pages: {pages} pages")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from backtest import run_backtest
    from report import print_report

    trades = run_backtest(n_days=30)
    print_report(trades)
    plot_all_signal_charts(trades)
