"""
brokers/base.py — 券商抽象基底類
所有券商 adapter 必須實作這兩個介面。
問價 (QuoteAdapter) 與交易 (TradeAdapter) 完全分離。
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Callable

from core.models import (
    Bar, Direction, Fill, Order, OrderBook, OrderType,
    Position, Tick, Timeframe,
)


class QuoteAdapter(ABC):
    """
    問價 Adapter 介面

    負責：連線、登入、訂閱即時報價、取得歷史K線。
    不負責：下單、查倉位。
    """

    name: str = "base"

    # ── 連線管理 ──────────────────────────────────────

    @abstractmethod
    async def connect(self, **credentials) -> bool:
        """連線 & 登入。回傳是否成功。"""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """斷線"""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    # ── 即時報價 ──────────────────────────────────────

    @abstractmethod
    async def subscribe_tick(
        self, symbol: str, callback: Callable[[Tick], None]
    ) -> None:
        """訂閱逐筆成交"""
        ...

    @abstractmethod
    async def subscribe_orderbook(
        self, symbol: str, callback: Callable[[OrderBook], None]
    ) -> None:
        """訂閱五檔 (或更多檔) 委託簿"""
        ...

    @abstractmethod
    async def unsubscribe(self, symbol: str) -> None:
        """取消訂閱"""
        ...

    # ── 歷史資料 ──────────────────────────────────────

    @abstractmethod
    async def get_history_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int = 200,
    ) -> list[Bar]:
        """取得歷史K線 (從券商API)"""
        ...

    # ── 選擇權資料 (Options) ──────────────────────────

    @abstractmethod
    async def get_options_months(self, symbol: str = "TXO") -> list[str]:
        """取得可交易的選擇權到期月份清單"""
        ...

    @abstractmethod
    async def get_options_t_quote(self, symbol: str, month: str) -> list[dict]:
        """取得指定月份的所有選擇權 T 字報價 (含 Call/Put 快照)"""
        ...


class TradeAdapter(ABC):
    """
    交易 Adapter 介面

    負責：下單、刪單、查詢委託/成交/倉位。
    不負責：報價。
    """

    name: str = "base"

    # ── 連線管理 ──────────────────────────────────────

    @abstractmethod
    async def connect(self, **credentials) -> bool:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    # ── 下單 ──────────────────────────────────────────

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        direction: Direction,
        order_type: OrderType,
        qty: int,
        price: float = 0.0,
    ) -> str:
        """
        送出委託。回傳券商端的委託序號 (broker_order_id)。
        price=0 表示市價單。
        """
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """取消委託。回傳是否成功。"""
        ...

    @abstractmethod
    async def modify_order(
        self, broker_order_id: str, new_price: float = 0, new_qty: int = 0
    ) -> bool:
        """改價/改量"""
        ...

    # ── 回報回調 ──────────────────────────────────────

    @abstractmethod
    def set_on_order_update(self, callback: Callable[[Order], None]) -> None:
        """設定委託回報 callback (狀態變更)"""
        ...

    @abstractmethod
    def set_on_fill(self, callback: Callable[[Fill], None]) -> None:
        """設定成交回報 callback"""
        ...

    # ── 查詢 ──────────────────────────────────────────

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """查詢當前倉位"""
        ...

    @abstractmethod
    async def get_open_orders(self) -> list[Order]:
        """查詢未成交委託"""
        ...

    @abstractmethod
    async def get_fills_today(self) -> list[Fill]:
        """查詢今日成交明細"""
        ...
