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

from datetime import time

from scripts.engine import ScriptContext

# 台指期日盤時段 08:45–13:45（timestamp 為台灣本地時間）
_DAY_START = time(8, 45)
_DAY_END = time(13, 45)


def _is_day_session(ts) -> bool:
    """判斷該棒是否落在日盤時段。無法取得時間時預設為 True（不阻擋）。"""
    try:
        t = ts.time()
    except Exception:
        return True
    return _DAY_START <= t <= _DAY_END


def calc(ctx: ScriptContext):
    """
    成交量爆量水平線

    參數:
      levels: [{"level": 1500, "label": "日盤大量"}, ...]
    """
    levels = ctx.param("levels")

    n = len(ctx.volume)
    last_volume = ctx.volume.iloc[-1] if n else 0
    # 用最新這根棒的時間判斷目前是日盤還是夜盤
    day_session = _is_day_session(ctx.data["timestamp"].iloc[-1]) if n else True
    for item in levels:
        level = item.get("level")
        label = item.get("label") or str(level)
        if not level:
            continue
        # 水平線 = 整段區間都畫同一個值，前端依此畫出參考線
        ctx.vol_plot(label, [level] * n, color="#f59e0b", dash="solid", label=True)

        # 日盤時段不要發出夜盤大量的 alert（水平線仍照畫）
        if day_session and "夜盤" in label:
            continue

        # 最新這根棒的量跨過門檻 → 請系統播報。calc() 只會在每根 M1 棒收完時
        # 執行一次（main.py on_bar_complete），所以這裡不會對同一根棒重複觸發。
        if last_volume >= level:
            ctx.alert(label)
