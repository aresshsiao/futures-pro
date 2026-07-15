__meta__ = {
    "name": "Volume_Alert",
    "description": "成交量爆量水平線",
    "type": "indicator",
    "enabled": True,
    "default_params": {
        "levels": [
            {"level": 1500, "label": "夜盤大量"},
            {"level": 400, "label": "日盤大量"},
        ]
    },
}


def calc(ctx):
    """
    成交量爆量水平線

    參數:
      levels: [{"level": 1500, "label": "爆1500大量"}, ...]
              （未提供時使用下方預設值）
    """
    levels = ctx.param("levels", [
        {"level": 1500, "label": "夜盤大量"},
        {"level": 400, "label": "日盤大量"},
    ])

    n = len(ctx.volume)
    for item in levels:
        level = item.get("level")
        label = item.get("label") or str(level)
        if not level:
            continue
        # 水平線 = 整段區間都畫同一個值，前端依此畫出參考線 & 判斷量是否跨越
        ctx.vol_plot(label, [level] * n, color="#f59e0b", label=True)
