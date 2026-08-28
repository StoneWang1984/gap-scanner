"""Analyze detailed trade data from the backtest."""

import json
import sys
from collections import Counter, defaultdict

def analyze(filepath):
    with open(filepath) as f:
        data = json.load(f)

    trades = data["trades"]
    print(f"=" * 80)
    print(f"DETAILED TRADE ANALYSIS — {data['label']}")
    print(f"Total trades: {data['total_trades']}, Final equity: ${data['final_equity']:,.2f}")
    print(f"=" * 80)

    # ── 1. Exit Reason Breakdown ──────────────────────────────────────
    print(f"\n{'─' * 80}")
    print("1. EXIT REASON BREAKDOWN")
    print(f"{'─' * 80}")
    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t["exit_reason"]].append(t)

    print(f"{'Exit Reason':<20} {'Count':>6} {'WinRate':>8} {'Avg P&L':>10} {'Avg Hold':>9} {'Total P&L':>11} {'Avg Win':>10} {'Avg Loss':>10}")
    print("-" * 84)
    for reason in sorted(by_reason.keys()):
        group = by_reason[reason]
        wins = [t for t in group if t["pnl"] > 0]
        losses = [t for t in group if t["pnl"] <= 0]
        win_rate = len(wins) / len(group) if group else 0
        avg_pnl = sum(t["pnl"] for t in group) / len(group)
        avg_hold = sum(t["holding_bars"] for t in group) / len(group)
        total_pnl = sum(t["pnl"] for t in group)
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        print(f"{reason:<20} {len(group):>6} {win_rate:>7.1%} ${avg_pnl:>8.2f} {avg_hold:>8.1f}b ${total_pnl:>9.2f} ${avg_win:>8.2f} ${avg_loss:>8.2f}")

    # ── 2. Entry Time of Day Analysis ────────────────────────────────
    print(f"\n{'─' * 80}")
    print("2. ENTRY TIME OF DAY ANALYSIS")
    print(f"{'─' * 80}")

    # Group by hour
    by_hour = defaultdict(list)
    for t in trades:
        hour = int(t["entry_time"].split(":")[0])
        by_hour[hour].append(t)

    print(f"{'Hour':>6} {'Count':>6} {'WinRate':>8} {'Avg P&L':>10} {'Total P&L':>11}")
    print("-" * 45)
    for hour in sorted(by_hour.keys()):
        group = by_hour[hour]
        wins = [t for t in group if t["pnl"] > 0]
        win_rate = len(wins) / len(group) if group else 0
        avg_pnl = sum(t["pnl"] for t in group) / len(group)
        total_pnl = sum(t["pnl"] for t in group)
        print(f"{hour:>6} {len(group):>6} {win_rate:>7.1%} ${avg_pnl:>8.2f} ${total_pnl:>9.2f}")

    # Morning (before 11:00) vs Afternoon
    morning = [t for t in trades if int(t["entry_time"].split(":")[0]) < 11]
    afternoon = [t for t in trades if int(t["entry_time"].split(":")[0]) >= 11]

    print(f"\nMorning (before 11:00): {len(morning)} trades, "
          f"win rate {len([t for t in morning if t['pnl']>0])/len(morning):.1%}" if morning else "Morning: 0 trades",
          f", avg P&L ${sum(t['pnl'] for t in morning)/len(morning):.2f}" if morning else "",
          f", total ${sum(t['pnl'] for t in morning):.2f}" if morning else "")
    print(f"Afternoon (11:00+):    {len(afternoon)} trades, "
          f"win rate {len([t for t in afternoon if t['pnl']>0])/len(afternoon):.1%}" if afternoon else "Afternoon: 0 trades",
          f", avg P&L ${sum(t['pnl'] for t in afternoon)/len(afternoon):.2f}" if afternoon else "",
          f", total ${sum(t['pnl'] for t in afternoon):.2f}" if afternoon else "")

    # ── 3. Holding Time Distribution ─────────────────────────────────
    print(f"\n{'─' * 80}")
    print("3. HOLDING TIME DISTRIBUTION")
    print(f"{'─' * 80}")

    by_hold = defaultdict(list)
    for t in trades:
        by_hold[t["holding_bars"]].append(t)

    print(f"{'Bars':>6} {'Count':>6} {'WinRate':>8} {'Avg P&L':>10} {'Total P&L':>11} {'Exit Reasons'}")
    print("-" * 75)
    for bars in sorted(by_hold.keys()):
        group = by_hold[bars]
        wins = [t for t in group if t["pnl"] > 0]
        win_rate = len(wins) / len(group) if group else 0
        avg_pnl = sum(t["pnl"] for t in group) / len(group)
        total_pnl = sum(t["pnl"] for t in group)
        reasons = Counter(t["exit_reason"] for t in group)
        reason_str = ", ".join(f"{k}:{v}" for k, v in reasons.most_common())
        print(f"{bars:>6} {len(group):>6} {win_rate:>7.1%} ${avg_pnl:>8.2f} ${total_pnl:>9.2f}   {reason_str}")

    # ── 4. Losing Trade Patterns ─────────────────────────────────────
    print(f"\n{'─' * 80}")
    print("4. LOSING TRADE PATTERNS")
    print(f"{'─' * 80}")

    losers = [t for t in trades if t["pnl"] <= 0]
    winners = [t for t in trades if t["pnl"] > 0]

    print(f"\nTotal losers: {len(losers)}, Total winners: {len(winners)}")

    # Loser exit reasons
    loser_reasons = Counter(t["exit_reason"] for t in losers)
    print(f"\nLoser exit reasons: {dict(loser_reasons.most_common())}")
    winner_reasons = Counter(t["exit_reason"] for t in winners)
    print(f"Winner exit reasons: {dict(winner_reasons.most_common())}")

    # Losers by time of day
    loser_morning = [t for t in losers if int(t["entry_time"].split(":")[0]) < 11]
    loser_afternoon = [t for t in losers if int(t["entry_time"].split(":")[0]) >= 11]
    print(f"\nLoser morning entries: {len(loser_morning)}, afternoon: {len(loser_afternoon)}")
    print(f"Loser avg P&L morning: ${sum(t['pnl'] for t in loser_morning)/len(loser_morning):.2f}" if loser_morning else "No morning losers")
    print(f"Loser avg P&L afternoon: ${sum(t['pnl'] for t in loser_afternoon)/len(loser_afternoon):.2f}" if loser_afternoon else "No afternoon losers")

    # Losers by holding time
    loser_hold = Counter(t["holding_bars"] for t in losers)
    print(f"\nLoser holding time distribution: {dict(sorted(loser_hold.items()))}")

    # Stop loss trades — are they all 1-bar holds?
    stop_losses = [t for t in trades if t["exit_reason"] == "stop_loss"]
    print(f"\nStop loss trades: {len(stop_losses)}")
    if stop_losses:
        sl_hold = Counter(t["holding_bars"] for t in stop_losses)
        print(f"  Holding time distribution: {dict(sorted(sl_hold.items()))}")
        sl_avg_pnl = sum(t["pnl"] for t in stop_losses) / len(stop_losses)
        print(f"  Avg P&L: ${sl_avg_pnl:.2f} (should be close to -3% of position)")
        sl_avg_pct = sum(t["pnl_pct"] for t in stop_losses) / len(stop_losses)
        print(f"  Avg P&L%: {sl_avg_pct:.2%}")

    # Red bar exit trades
    red_bar = [t for t in trades if t["exit_reason"] == "red_bar_exit"]
    print(f"\nRed bar exit trades: {len(red_bar)}")
    if red_bar:
        rb_wins = [t for t in red_bar if t["pnl"] > 0]
        print(f"  Wins: {len(rb_wins)}, Losses: {len(red_bar) - len(rb_wins)}")
        print(f"  Avg P&L: ${sum(t['pnl'] for t in red_bar)/len(red_bar):.2f}")
        print(f"  All are 1-bar holds: {all(t['holding_bars'] == 1 for t in red_bar)}")

    # Green-to-red trades
    g2r = [t for t in trades if t["exit_reason"] == "green_to_red"]
    print(f"\nGreen-to-red trades: {len(g2r)}")
    if g2r:
        g2r_wins = [t for t in g2r if t["pnl"] > 0]
        g2r_losses = [t for t in g2r if t["pnl"] <= 0]
        print(f"  Wins: {len(g2r_wins)}, Losses: {len(g2r_losses)}")
        print(f"  Avg P&L (wins): ${sum(t['pnl'] for t in g2r_wins)/len(g2r_wins):.2f}" if g2r_wins else "  No wins")
        print(f"  Avg P&L (losses): ${sum(t['pnl'] for t in g2r_losses)/len(g2r_losses):.2f}" if g2r_losses else "  No losses")
        print(f"  Avg holding bars: {sum(t['holding_bars'] for t in g2r)/len(g2r):.1f}")

    # Three green bars trades
    tgb = [t for t in trades if t["exit_reason"] == "three_green_bars"]
    print(f"\nThree green bars trades: {len(tgb)}")
    if tgb:
        print(f"  All wins: {all(t['pnl'] > 0 for t in tgb)}")
        print(f"  Avg P&L: ${sum(t['pnl'] for t in tgb)/len(tgb):.2f}")
        print(f"  Avg P&L%: {sum(t['pnl_pct'] for t in tgb)/len(tgb):.2%}")

    # ── 5. Key Observations ──────────────────────────────────────────
    print(f"\n{'─' * 80}")
    print("5. KEY OBSERVATIONS")
    print(f"{'─' * 80}")

    # Expected value per trade
    ev = sum(t["pnl"] for t in trades) / len(trades)
    print(f"Expected value per trade: ${ev:.2f}")

    # Profit factor
    gross_profit = sum(t["pnl"] for t in winners)
    gross_loss = abs(sum(t["pnl"] for t in losers))
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    print(f"Profit factor: {pf:.2f} (gross profit ${gross_profit:.2f} / gross loss ${gross_loss:.2f})")

    # Average winner / average loser ratio
    avg_win = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = abs(sum(t["pnl"] for t in losers) / len(losers)) if losers else 0
    wl_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')
    print(f"Avg win / avg loss ratio: {wl_ratio:.2f}")

    # Break-even win rate
    be_wr = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else 0
    print(f"Break-even win rate: {be_wr:.1%} (actual: {len(winners)/len(trades):.1%})")

    # Biggest winner and loser
    biggest_winner = max(trades, key=lambda t: t["pnl"])
    biggest_loser = min(trades, key=lambda t: t["pnl"])
    print(f"\nBiggest winner: {biggest_winner['symbol']} {biggest_winner['date']} "
          f"${biggest_winner['pnl']:+.2f} ({biggest_winner['pnl_pct']:+.2%}) "
          f"via {biggest_winner['exit_reason']}")
    print(f"Biggest loser:  {biggest_loser['symbol']} {biggest_loser['date']} "
          f"${biggest_loser['pnl']:+.2f} ({biggest_loser['pnl_pct']:+.2%}) "
          f"via {biggest_loser['exit_reason']}")

    # Consecutive losses
    max_consec_loss = 0
    cur_consec = 0
    for t in trades:
        if t["pnl"] <= 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0
    print(f"Max consecutive losses: {max_consec_loss}")


if __name__ == "__main__":
    filepath = sys.argv[1] if len(sys.argv) > 1 else "stonewang_daytrade_rtg_3.0/detailed_trades.json"
    analyze(filepath)
