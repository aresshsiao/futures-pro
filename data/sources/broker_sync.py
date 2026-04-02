"""
data/sources/broker_sync.py — 從券商 API 同步歷史資料
使用 QuoteAdapter 取得歷史K線並存入 SQLite。
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brokers.base import QuoteAdapter
    from data.database import Database

from core.models import Timeframe

logger = logging.getLogger(__name__)

# 預設要同步的商品與週期
DEFAULT_SYMBOLS = ["TX", "MTX", "TE", "TF"]
DEFAULT_TIMEFRAMES = [Timeframe.D1, Timeframe.H1, Timeframe.M15, Timeframe.M5]


class BrokerSync:
    """
    從券商 API 同步歷史K線資料到本地 SQLite。

    用法:
        syncer = BrokerSync(quote_adapter, database)
        count = await syncer.sync_symbol("TX", Timeframe.D1, count=500)
    """

    def __init__(self, adapter: "QuoteAdapter", db: "Database"):
        self._adapter = adapter
        self._db = db

    async def sync_symbol(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int = 200,
    ) -> int:
        """
        從券商 API 同步指定商品歷史K線。
        回傳實際寫入筆數。
        """
        if not self._adapter.is_connected():
            logger.warning("[BrokerSync] 券商未連線，無法同步: %s %s", symbol, timeframe.value)
            return 0

        logger.info("[BrokerSync] 同步 %s %s (請求 %d 筆)", symbol, timeframe.value, count)
        try:
            bars = await self._adapter.get_history_bars(symbol, timeframe, count)
        except Exception as e:
            logger.error("[BrokerSync] 取得資料失敗 %s %s: %s", symbol, timeframe.value, e)
            return 0

        if not bars:
            logger.warning("[BrokerSync] 未取得資料: %s %s", symbol, timeframe.value)
            return 0

        inserted = self._db.insert_bars(bars)
        logger.info("[BrokerSync] %s %s → %d 筆寫入", symbol, timeframe.value, inserted)
        return inserted

    async def sync_multiple(
        self,
        symbols: list[str] | None = None,
        timeframes: list[Timeframe] | None = None,
        count: int = 200,
    ) -> dict[str, int]:
        """
        批量同步多個商品 & 週期。
        回傳 {"TX_1d": 500, "TX_1h": 200, ...}
        """
        symbols = symbols or DEFAULT_SYMBOLS
        timeframes = timeframes or DEFAULT_TIMEFRAMES
        results: dict[str, int] = {}

        for symbol in symbols:
            for tf in timeframes:
                key = f"{symbol}_{tf.value}"
                results[key] = await self.sync_symbol(symbol, tf, count)

        total = sum(results.values())
        logger.info("[BrokerSync] 批量同步完成: 共 %d 筆", total)
        return results
