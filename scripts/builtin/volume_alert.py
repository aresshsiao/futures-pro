__meta__ = {
    "name": "Volume_Alert",
    "description": "成交量爆量水平線",
    "type": "indicator",
    "enabled": True,
    "params": {
        "levels": [
            {"level": 1500, "label": "日盤大量"},
            {"level": 400, "label": "夜盤大量"},
        ]
    },
}

from scripts.engine import ScriptContext

def calc(ctx: ScriptContext):
    """
    成交量爆量水平線

    參數:
      levels: [{"level": 1500, "label": "爆1500大量"}, ...]
    """
    levels = ctx.param("levels")

    n = len(ctx.volume)
    last_volume = ctx.volume.iloc[-1] if n else 0
    for item in levels:
        level = item.get("level")
        label = item.get("label") or str(level)
        if not level:
            continue
        # 水平線 = 整段區間都畫同一個值，前端依此畫出參考線
        ctx.vol_plot(label, [level] * n, color="#f59e0b", dash="solid", label=True)

        # 最新這根棒的量跨過門檻 → 請系統播報。calc() 只會在每根 M1 棒收完時
        # 執行一次（main.py on_bar_complete），所以這裡不會對同一根棒重複觸發。
        if last_volume >= level:
            ctx.alert(label)
