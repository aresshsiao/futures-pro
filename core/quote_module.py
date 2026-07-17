"""
core/quote_module.py — 問價模塊
獨立於交易模塊，負責管理報價連線、分發即時數據。
"""
from __future__ import annotations
import logging
from typing import Optional

from core.event_bus import EventBus
from core.models import Bar, OrderBook, Tick, Timeframe
from brokers.base import QuoteAdapter

logger = logging.getLogger(__name__)


class QuoteModule:
    """
    問價模塊

    職責:
      1. 持有一個 QuoteAdapter (可在運行時切換)
      2. 管理訂閱清單
      3. 收到 Tick/OrderBook 後透過 EventBus 廣播
      4. 提供歷史K線查詢介面

    與 TradeModule 完全獨立 — 可以使用不同券商。
    """

    def __init__(self):
        self.bus = EventBus()
        self._adapter: Optional[QuoteAdapter] = None
        self._subscriptions: set[str] = set()

    # ── Adapter 管理 ──────────────────────────────────

    @property
    def broker_name(self) -> str:
        return self._adapter.name if self._adapter else "未連線"

    async def set_adapter(self, adapter: QuoteAdapter, **credentials) -> bool:
        """切換問價券商"""
        # 先斷開舊的
        if self._adapter and self._adapter.is_connected():
            await self._adapter.disconnect()

        self._adapter = adapter
        ok = await adapter.connect(**credentials)
        if ok:
            logger.info(f"[QuoteModule] 已連線: {adapter.name}")
            # 重新訂閱先前的商品
            for sym in self._subscriptions:
                await self._subscribe_internal(sym)
            await self.bus.emit("quote_connected", adapter.name)
        else:
            logger.error(f"[QuoteModule] 連線失敗: {adapter.name}")
        return ok

    async def disconnect(self) -> None:
        if self._adapter:
            await self._adapter.disconnect()
            await self.bus.emit("quote_disconnected", self._adapter.name)

    @property
    def is_connected(self) -> bool:
        return self._adapter is not None and self._adapter.is_connected()

    # ── 訂閱管理 ──────────────────────────────────────

    async def subscribe(self, symbol: str) -> None:
        """訂閱商品的即時報價（已訂閱則跳過）"""
        already = symbol in self._subscriptions
        self._subscriptions.add(symbol)
        if self.is_connected and not already:
            await self._subscribe_internal(symbol)

    async def unsubscribe(self, symbol: str) -> None:
        self._subscriptions.discard(symbol)
        if self.is_connected:
            await self._adapter.unsubscribe(symbol)

    async def _subscribe_internal(self, symbol: str) -> None:
        """向 adapter 訂閱，並設定回調"""
        await self._adapter.subscribe_tick(symbol, self._on_tick)
        await self._adapter.subscribe_orderbook(symbol, self._on_orderbook)
        logger.info(f"[QuoteModule] 訂閱: {symbol}")

    # ── 資料回調 ──────────────────────────────────────

    def _on_tick(self, tick: Tick) -> None:
        """收到逐筆成交 → 廣播事件"""
        self.bus.emit_sync("tick", tick)

    def _on_orderbook(self, book: OrderBook) -> None:
        """收到五檔更新 → 廣播事件"""
        self.bus.emit_sync("quote_update", book)

    # ── 歷史資料 ──────────────────────────────────────

    async def get_history(
        self, symbol: str, timeframe: Timeframe, count: int = 200
    ) -> list[Bar]:
        """從券商取得歷史K線"""
        if not self.is_connected:
            logger.warning("[QuoteModule] 未連線，無法取得歷史資料")
            return []
        return await self._adapter.get_history_bars(symbol, timeframe, count)

    # ── 選擇權資料 ────────────────────────────────────

    async def get_options_months(self, symbol: str = "TXO") -> list[str]:
        if not self.is_connected:
            return []
        return await self._adapter.get_options_months(symbol)

    async def get_options_t_quote(
        self, symbol: str, month: str, spot_price: float = 0.0, trading_dates: list[str] | None = None,
    ) -> list[dict]:
        if not self.is_connected:
            return []
        return await self._adapter.get_options_t_quote(symbol, month, spot_price, trading_dates)
