__meta__ = {
    "name": "MA",
    "description": "均線指標 (可設定多條)",
    "type": "indicator",
    "default_params": {"periods": "5,20,60"},
}

COLORS = ["#f59e0b", "#8b5cf6", "#3b82f6", "#10b981", "#ef4444", "#f97316"]


def calc(ctx):
    raw = str(ctx.param("periods", "5,20,60"))
    periods = [int(p.strip()) for p in raw.split(",") if p.strip().isdigit()]

    for i, period in enumerate(periods):
        ma = ctx.close.rolling(period).mean()
        color = COLORS[i % len(COLORS)]
        ctx.plot(f"MA{period}", ma, color=color)
