__meta__ = {
    "name": "Breakout",
    "description": "通道突破策略",
    "type": "strategy",
    "default_params": {"lookback": 20},
}


def on_bar(ctx):
    """
    通道突破策略

    當收盤價突破過去 N 根 Bar 的最高價 → 買進
    當收盤價跌破過去 N 根 Bar 的最低價 → 賣出

    參數:
      lookback: 回看週期 (預設 20)
    """
    lookback = ctx.param("lookback")

    if len(ctx.data) < lookback + 2:
        return

    high_n = ctx.high.rolling(lookback).max()
    low_n = ctx.low.rolling(lookback).min()

    prev_high = high_n.iloc[-2]
    prev_low = low_n.iloc[-2]
    current_close = ctx.close.iloc[-1]

    if current_close > prev_high:
        ctx.buy(1, reason=f"突破 {lookback} 期高點 {prev_high:.0f}")

    elif current_close < prev_low:
        ctx.sell(1, reason=f"跌破 {lookback} 期低點 {prev_low:.0f}")
