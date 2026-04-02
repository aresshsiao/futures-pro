"""
scripts/builtin/rsi.py — RSI 指標
示範 subplot (獨立子圖) 的用法。
"""


def calc(ctx):
    """
    RSI (Relative Strength Index)

    參數:
      period: 計算週期 (預設 14)
      overbought: 超買線 (預設 70)
      oversold: 超賣線 (預設 30)
    """
    period = ctx.param("period", 14)

    delta = ctx.close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    ctx.subplot("RSI", rsi, color="#06b6d4")
