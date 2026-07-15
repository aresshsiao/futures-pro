__meta__ = {
    "name": "MA",
    "description": "均線指標 (可設定多條)",
    "type": "indicator",
    "enabled": True,
    "default_params": {"periods": [5, 20, 60]},
}

COLORS = ["#f59e0b", "#8b5cf6", "#3b82f6", "#10b981", "#ef4444", "#f97316"]

from scripts.engine import ScriptContext

def calc(ctx: ScriptContext):
    periods = ctx.param("periods")

    for i, period in enumerate(periods):
        ma = ctx.close.rolling(period).mean()
        color = COLORS[i % len(COLORS)]
        ctx.plot(f"MA{period}", ma, color=color, label=True)
