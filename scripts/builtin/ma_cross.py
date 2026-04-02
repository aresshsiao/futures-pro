"""
scripts/builtin/ma_cross.py — 均線交叉指標
示範 Indicator Script 的標準寫法。

Script 規範:
  - 指標類型必須包含 calc(ctx) 函式
  - 透過 ctx.plot() 輸出線條到 K 線圖
  - 透過 ctx.subplot() 輸出到獨立子圖
  - 透過 ctx.param() 讀取可調參數
"""


def calc(ctx):
    """
    均線交叉指標

    參數:
      fast_period: 快線週期 (預設 5)
      slow_period: 慢線週期 (預設 20)
    """
    fast = ctx.param("fast_period", 5)
    slow = ctx.param("slow_period", 20)

    ma_fast = ctx.close.rolling(fast).mean()
    ma_slow = ctx.close.rolling(slow).mean()

    ctx.plot(f"MA{fast}", ma_fast, color="#f59e0b")
    ctx.plot(f"MA{slow}", ma_slow, color="#8b5cf6")
