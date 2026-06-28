__meta__ = {
    "name": "RSI",
    "description": "RSI 相對強弱指標",
    "type": "indicator",
    "default_params": {"period": 14, "overbought": 70, "oversold": 30},
}


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

    overbought = ctx.param("overbought", 70)
    oversold = ctx.param("oversold", 30)
    ctx.sub_plot("RSI", rsi, color="#06b6d4", ref_lines=[overbought, oversold])
