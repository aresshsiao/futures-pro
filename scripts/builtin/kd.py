__meta__ = {
    "name": "KD",
    "description": "隨機指標 (KD)",
    "type": "indicator",
    "default_params": {"period": 9},
}


def calc(ctx):
    """
    KD 指標 (Stochastic Oscillator)
    預設參數: 週期=9, M1=3, M2=3
    """
    period = int(ctx.param("period", 9))
    
    # 計算 RSV
    low_min = ctx.low.rolling(window=period).min()
    high_max = ctx.high.rolling(window=period).max()
    
    # 避免分母為 0
    diff = high_max - low_min
    diff = diff.replace(0, 1e-9)
    
    rsv = (ctx.close - low_min) / diff * 100
    
    # 計算 K 與 D
    # 使用 ewm (Exponential Weighted Moving Average) 來模擬傳統 KD 的平滑公式
    # K(n) = (2/3) * K(n-1) + (1/3) * RSV(n) => alpha = 1/3
    k = rsv.ewm(alpha=1/3, adjust=False).mean()
    d = k.ewm(alpha=1/3, adjust=False).mean()
    
    # 畫在獨立子圖 (subplot) 中
    ctx.sub_plot("K", k, color="#f59e0b", ref_lines=[80, 20])
    ctx.sub_plot("D", d, color="#3b82f6")
