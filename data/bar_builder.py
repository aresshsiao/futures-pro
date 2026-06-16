"""
data/bar_builder.py — Tick → Bar 聚合器
將即時 Tick 資料聚合為各週期 K 棒，並在 K 棒收完時觸發事件。
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional

from core.event_bus import EventBus
from core.models import Bar, Tick, Timeframe

logger = logging.getLogger(__name__)

# 各週期的秒數
TIMEFRAME_SECONDS = {
    Timeframe.M1: 60,
    Timeframe.M3: 180,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
}


class BarBuilder:
    """
    Tick → Bar 即時聚合器

    監聽 EventBus 的 "tick" 事件，
    聚合為指定週期的 K 棒，
    K 棒收完時透過 EventBus 發送 "bar" 事件。

    支援同時聚合多個週期。
    """

    def __init__(self, timeframes: list[Timeframe] = None):
        self.bus = EventBus()
        self._timeframes = timeframes or [
            Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1,
        ]
        # 每個 (symbol, timeframe) 有一根正在建構中的 Bar
        self._building: dict[tuple[str, Timeframe], Bar] = {}

        # 監聽 tick 事件
        self.bus.on("tick", self.on_tick)

    def on_tick(self, tick: Tick) -> None:
        """收到 Tick → 更新所有週期的 Bar，並推送最新 M1 即時棒給前端"""
        for tf in self._timeframes:
            self._update_bar(tick, tf)

        # 每次 tick 後推送最新 M1 即時棒（is_closed=False），讓前端可即時取得完整 OHLCV
        m1_bar = self._building.get((tick.symbol, Timeframe.M1))
        if m1_bar is not None and not m1_bar.is_closed:
            self.bus.emit_sync("bar", m1_bar)

    def _update_bar(self, tick: Tick, tf: Timeframe) -> None:
        key = (tick.symbol, tf)
        bar = self._building.get(key)

        # 計算當前 Tick 屬於哪根 Bar 的起始時間
        bar_start = self._align_timestamp(tick.timestamp, tf)

        if bar is None or bar.timestamp != bar_start:
            # 舊的 Bar 收完 → 發送事件
            if bar is not None:
                bar.is_closed = True
                self.bus.emit_sync("bar", bar)

            # 開新 Bar
            bar = Bar(
                symbol=tick.symbol,
                timeframe=tf,
                timestamp=bar_start,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=tick.volume,
                is_closed=False,
            )
            self._building[key] = bar
        else:
            # 更新現有 Bar
            bar.high = max(bar.high, tick.price)
            bar.low = min(bar.low, tick.price)
            bar.close = tick.price
            bar.volume += tick.volume

    @staticmethod
    def _align_timestamp(ts: datetime, tf: Timeframe) -> datetime:
        """將時間戳對齊到週期起始點"""
        seconds = TIMEFRAME_SECONDS.get(tf)
        if seconds is None:
            # 日/週/月 K
            if tf == Timeframe.D1:
                return ts.replace(hour=0, minute=0, second=0, microsecond=0)
            elif tf == Timeframe.W1:
                # 對齊到週一
                days_since_monday = ts.weekday()
                return (ts - timedelta(days=days_since_monday)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            else:
                return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # 分鐘/小時 K: 對齊到整數秒
        epoch = ts.timestamp()
        aligned_epoch = (epoch // seconds) * seconds
        return datetime.fromtimestamp(aligned_epoch)

    def get_current_bar(self, symbol: str, tf: Timeframe) -> Optional[Bar]:
        """取得目前正在建構中的 Bar (尚未收完)"""
        return self._building.get((symbol, tf))
