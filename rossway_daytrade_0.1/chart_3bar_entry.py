"""3-bar Pullback Confirmation Entry Pattern - Visualization"""
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch

fig, ax = plt.subplots(figsize=(14, 7))
fig.patch.set_facecolor('#1a1a2e')
ax.set_facecolor('#1a1a2e')

open_price = 10.00

# Bars: (open, high, low, close, label, color_override)
bars = [
    (10.00, 10.05, 9.85, 9.90, "Bar 1\n(gap down)", None),
    (9.90, 9.93, 9.75, 9.80, "Bar 2\n(drift)", None),
    (9.80, 9.82, 9.55, 9.60, "Bar 3\n(pullback)", None),
    (9.60, 9.63, 9.50, 9.53, "Bar 4\n(deeper\nbottom)", None),
    (9.53, 9.72, 9.58, 9.70, "Bar 5\nConfirm #1\n(bullish)", '#00e676'),
    (9.70, 9.75, 9.60, 9.63, "Bar 6\nConfirm #2\n(bearish)", '#ff5252'),
    (9.63, 9.82, 9.65, 9.80, "Bar 7\nConfirm #3\n(bullish)", '#00e676'),
]

pullback_price = 9.50
buy_price = 9.80

bar_width = 0.6
spacing = 1.8
x_positions = [i * spacing for i in range(len(bars))]

for i, (o, h, l, c, label, color_override) in enumerate(bars):
    x = x_positions[i]
    is_bullish = c >= o

    if color_override:
        body_color = color_override
        wick_color = color_override
    elif is_bullish:
        body_color = '#26a69a'
        wick_color = '#26a69a'
    else:
        body_color = '#ef5350'
        wick_color = '#ef5350'

    # Wick
    ax.plot([x, x], [l, h], color=wick_color, linewidth=1.5, zorder=2)

    # Body
    body_bottom = min(o, c)
    body_height = abs(c - o)
    if body_height < 0.02:
        body_height = 0.02
    rect = FancyBboxPatch(
        (x - bar_width / 2, body_bottom), bar_width, body_height,
        boxstyle="round,pad=0.02",
        facecolor=body_color, edgecolor=body_color, alpha=0.9, zorder=3
    )
    ax.add_patch(rect)

    # Label
    ax.text(x, l - 0.06, label, ha='center', va='top',
            fontsize=7, color='#b0b0b0', fontfamily='monospace')

# ── Reference lines ──
ax.axhline(y=open_price, color='#ffd54f', linestyle='--', linewidth=1.5, alpha=0.8)
ax.text(x_positions[-1] + 1.0, open_price + 0.02, 'Open Price  $10.00',
        color='#ffd54f', fontsize=10, fontweight='bold', va='bottom')

ax.axhline(y=pullback_price, color='#ff7043', linestyle=':', linewidth=1.5, alpha=0.8)
ax.text(x_positions[-1] + 1.0, pullback_price - 0.02, 'Pullback Bottom  $9.50',
        color='#ff7043', fontsize=10, fontweight='bold', va='top')

# ── Annotations ──
ax.annotate('low < open_price\n(pullback found)',
            xy=(x_positions[2], 9.55), xytext=(x_positions[2] - 1.4, 9.28),
            fontsize=9, color='#ff7043', fontweight='bold',
            arrowprops=dict(arrowstyle='->', color='#ff7043', lw=1.5),
            ha='center', va='top')

ax.annotate('Deeper bottom!\nReset pullback_price',
            xy=(x_positions[3], 9.50), xytext=(x_positions[3] + 1.5, 9.25),
            fontsize=9, color='#ff7043', fontweight='bold',
            arrowprops=dict(arrowstyle='->', color='#ff7043', lw=1.5),
            ha='center', va='top')

# Bracket for 3 confirm bars
bx_start = x_positions[4] - 0.6
bx_end = x_positions[6] + 0.6
by = 9.90
ax.plot([bx_start, bx_start, bx_end, bx_end],
        [by - 0.03, by, by, by - 0.03],
        color='#69f0ae', linewidth=2)
ax.text((bx_start + bx_end) / 2, by + 0.02,
        '3 Confirmation Bars  (low > bottom AND close > bottom)',
        ha='center', va='bottom', fontsize=10, color='#69f0ae', fontweight='bold')

# BUY arrow
buy_x = x_positions[6] + 0.6
ax.annotate('BUY!',
            xy=(buy_x, buy_price), xytext=(buy_x + 1.0, buy_price + 0.15),
            fontsize=16, color='#00e676', fontweight='bold',
            arrowprops=dict(arrowstyle='->', color='#00e676', lw=2.5),
            ha='center', va='center')

# ── Legend ──
legend_items = [
    mpatches.Patch(color='#26a69a', label='Bullish bar  (close > open)'),
    mpatches.Patch(color='#ef5350', label='Bearish bar  (close < open)'),
    mpatches.Patch(color='#00e676', label='Confirmation bullish  (>=1 required)'),
    mpatches.Patch(color='#ff5252', label='Confirmation bearish  (allowed)'),
    plt.Line2D([0], [0], color='#ffd54f', linestyle='--', label='Open price line'),
    plt.Line2D([0], [0], color='#ff7043', linestyle=':', label='Pullback bottom line'),
]
ax.legend(handles=legend_items, loc='upper left', fontsize=9,
          facecolor='#2a2a4a', edgecolor='#555', labelcolor='white')

# ── Rules box ──
rules = (
    "Confirmation Rules:\n"
    "1. confirm_count >= 3\n"
    "2. bullish_count >= 1\n"
    "3. Each bar: low > pullback_price\n"
    "4. Each bar: close > pullback_price\n"
    "\n"
    "Meaning: Price has stabilized\n"
    "above the bottom and started\n"
    "to recover -> safe to enter"
)
ax.text(0.98, 0.45, rules, transform=ax.transAxes,
        fontsize=9, color='white', fontfamily='monospace',
        verticalalignment='center', horizontalalignment='right',
        bbox=dict(boxstyle='round,pad=0.5', facecolor='#2a2a4a', edgecolor='#69f0ae', alpha=0.9))

# ── Axes ──
ax.set_xlim(-1.5, x_positions[-1] + 3.5)
ax.set_ylim(9.10, 10.15)
ax.set_ylabel('Price ($)', color='white', fontsize=12)
ax.tick_params(colors='white', labelsize=9)
ax.spines['bottom'].set_color('#555')
ax.spines['left'].set_color('#555')
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.set_xticks([])

ax.set_title('3-Bar Pullback Confirmation  --  Entry Pattern',
             color='white', fontsize=16, fontweight='bold', pad=15)

plt.tight_layout()
plt.savefig('/Users/stonewang2014/gap-scanner/rossway_daytrade_0.1/chart_3bar_entry.png',
            dpi=150, bbox_inches='tight', facecolor='#1a1a2e')
plt.close()
print("Saved chart_3bar_entry.png")
