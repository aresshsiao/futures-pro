__meta__ = {
    "name": "Support & Pressure Line",
    "description": "支撐/壓力水平線",
    "type": "indicator",
    "enabled": True,
    "params": {
        "lines": [45700, 45400, 45100, 44800, 44500],
    },
}

# 每條線的 (顏色, 線寬)，依 lines 順序對應
_LINE_PARA = [
    ("#ff0000", "solid", 1),
    ("#b92525", "dash", 1),
    ("#FFFF00", "solid", 1),
    ("#23c35d", "dash", 1),
    ("#00ff5e", "solid", 1),
]

from scripts.engine import ScriptContext

def calc(ctx: ScriptContext):
    """
    支撐/壓力水平線
    """
    lines = ctx.param("lines")
    n = len(ctx.close)

    for i, level in enumerate(lines):
        color, dash, width = _LINE_PARA[i % len(_LINE_PARA)]
        ctx.plot(f"{level}", [level] * n, color=color, dash=dash, width=width, label=True)
