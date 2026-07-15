__meta__ = {
    "name": "Window Price",
    "description": "Window最高/最低水平線",
    "type": "indicator",
    "default_params": {
        "periods": "60,300,1200",
    },
}

# 每個週期的顏色 [high, low]
_COLORS = [
    ("#ef4444", "#22c55e"),
    ("#ef4444", "#22c55e"),
    ("#ef4444", "#22c55e"),
    ("#ef4444", "#22c55e"),
    ("#ef4444", "#22c55e"),
]

_WIDTHS = [1, 2, 3, 4]

from scripts.engine import ScriptContext

def calc(ctx: ScriptContext):
    raw = str(ctx.param("periods"))
    periods = [int(p.strip()) for p in raw.split(",") if p.strip().isdigit()]

    for i, period in enumerate(periods):
        color_h, color_l = _COLORS[i % len(_COLORS)]
        w = _WIDTHS[i % len(_WIDTHS)]
        ctx.plot(f"High {period}T", ctx.high.rolling(period).max(), color=color_h, dash="dot", width=w, label=True)
        ctx.plot(f"Low  {period}T", ctx.low.rolling(period).min(),  color=color_l, dash="dot", width=w, label=True)
