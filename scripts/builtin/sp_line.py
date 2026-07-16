__meta__ = {
    "name": "Support & Pressure Line",
    "description": "支撐/壓力水平線",
    "type": "indicator",
    "enabled": True,
    "params": {
        "lines": [45500, 45100, 44800, 44500, 44200],
    },
}

# 每條線的 (顏色, 線寬)，依 lines 順序對應
_LINE_PARA = [
    ("#ef4444", "dash", 2),
    ("#ef4444", "dash", 1),
    ("#E6E61E", "dash", 2),
    ("#22c55e", "dash", 1),
    ("#22c55e", "dash", 2),
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
