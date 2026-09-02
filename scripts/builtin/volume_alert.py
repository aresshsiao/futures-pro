__meta__ = {
    "name": "Volume_Alert",
    "description": "成交量爆量水平線",
    "type": "indicator",
    "enabled": True,
    "params": {
        # 只有列在這裡的商品會判斷爆量。門檻是照台指期的量能訂的，
        # 套到小台、微台或加權指數上只會冒出一串跟眼前這檔無關的警示——
        # 而每一檔訂閱中的商品收完 M1 棒都會各跑一次 calc()。
        "symbols": ["TX"],
        # session: "day" 只在日盤判斷、"night" 只在夜盤判斷、省略則兩盤都判斷。
        # 這個欄位不能省成「用 label 猜」：日夜盤的量能差一個數量級，
        # 夜盤那根 1500 口的棒會同時跨過兩條門檻，於是同一根棒念兩次。
        "levels": [
            {"level": 1500, "label": "日盤大量", "session": "day"},
            {"level": 400, "label": "夜盤大量", "session": "night"},
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
      symbols: ["TX"]  —— 只對這些商品播報
      levels:  [{"level": 1500, "label": "日盤大量", "session": "day"}, ...]
    """
    levels = ctx.param("levels") or []
    watched = ctx.param("symbols") or []

    n = len(ctx.volume)
    last_volume = ctx.volume.iloc[-1] if n else 0
    # 用最新這根棒的時間判斷目前是日盤還是夜盤
    day_session = _is_day_session(ctx.data["timestamp"].iloc[-1]) if n else True
    session_now = "day" if day_session else "night"

    # 水平線照畫（圖上本來就只顯示目前這一檔），但只有被盯的商品才播報
    speak_for_symbol = not watched or not ctx.symbol or ctx.symbol in watched

    hit = None
    for item in levels:
        level = item.get("level")
        label = item.get("label") or str(level)
        if not level:
            continue
        # 水平線 = 整段區間都畫同一個值，前端依此畫出參考線
        ctx.vol_plot(label, [level] * n, color="#f59e0b", dash="solid", label=True)

        # 不屬於當前時段的門檻不參與判斷（夜盤不該用日盤的量在比）
        session = item.get("session")
        if session and session != session_now:
            continue
        # 同一根棒可能跨過好幾條門檻，只留最高的那一條 —— 每跨一條念一句的話，
        # 一根爆量棒就會連念好幾聲
        if last_volume >= level and (hit is None or level > hit[0]):
            hit = (level, label)

    if hit and speak_for_symbol:
        # 念出商品名：訂閱多檔時，只說「大量」根本分不出是哪一檔在爆量
        ctx.alert(f"{ctx.symbol_name} {hit[1]}" if ctx.symbol_name else hit[1])
