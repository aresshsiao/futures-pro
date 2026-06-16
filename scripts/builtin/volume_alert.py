"""
scripts/builtin/volume_alert.py — 成交量爆量水平線
把「成交量圖要在哪個量級畫水平警示線」變成一般指標 Script，
取代舊版寫死在 config/settings.py + 前端的版本。

水平線數值/文字仍可從 config/settings.py 的 VOLUME_REFERENCE_LINES 設定
（透過 main.py 載入時帶入 ScriptMeta.parameters["levels"]），
也可以不帶參數，使用本檔案內建的預設值。
"""


def calc(ctx):
    """
    成交量爆量水平線

    參數:
      levels: [{"level": 1500, "label": "爆1500大量"}, ...]
              （未提供時使用下方預設值）
    """
    levels = ctx.param("levels", [
        {"level": 1500, "label": "爆1500大量"},
        {"level": 400, "label": "爆400大量"},
    ])

    n = len(ctx.volume)
    for item in levels:
        level = item.get("level")
        label = item.get("label") or str(level)
        if not level:
            continue
        # 水平線 = 整段區間都畫同一個值，前端依此畫出參考線 & 判斷量是否跨越
        ctx.subplot(label, [level] * n, color="#f59e0b")
