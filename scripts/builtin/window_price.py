__meta__ = {
    "name": "Window Price",
    "description": "往前 N 根 K 棒內的最高 / 最低水平線（60T / 300T / 1200T 等）",
    "type": "indicator",
    "default_params": {
        "periods": "60,300,1200",
    },
}

# 每個週期的顏色 [high, low]
_COLORS = [
    ("#f59e0b", "#f59e0b"),  # 60T  amber
    ("#8b5cf6", "#8b5cf6"),  # 300T purple
    ("#3b82f6", "#3b82f6"),  # 1200T blue
    ("#10b981", "#10b981"),  # extra green
    ("#ef4444", "#ef4444"),  # extra red
]


def calc(ctx):
    raw = str(ctx.param("periods", "60,300,1200"))
    periods = [int(p.strip()) for p in raw.split(",") if p.strip().isdigit()]

    for i, period in enumerate(periods):
        color_h, color_l = _COLORS[i % len(_COLORS)]
        ctx.plot(f"High {period}T", ctx.high.rolling(period).max(), color=color_h)
        ctx.plot(f"Low  {period}T", ctx.low.rolling(period).min(),  color=color_l)
