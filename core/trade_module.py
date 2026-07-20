"""
core/trade_module.py — 交易模塊
獨立於問價模塊，負責委託管理、成交回報、倉位追蹤。
"""
from __future__ import annotations
import logging
import uuid
from typing import Optional

from core.event_bus import EventBus
from core.models import (
    Direction, Fill, Order, OrderStatus, OrderType, Position,
)
from brokers.base import TradeAdapter

logger = logging.getLogger(__name__)


class TradeModule:
    """
    交易模塊

    職責:
      1. 持有一個 TradeAdapter (可在運行時切換)
      2. 管理內部委託簿
      3. 處理成交回報，更新倉位
      4. 管理觸價單 (本地監控，觸發後送市價單)

    與 QuoteModule 完全獨立 — 可以使用不同券商。
    """

    def __init__(self):
        self.bus = EventBus()
        self._adapter: Optional[TradeAdapter] = None
        self._orders: dict[str, Order] = {}       # id → Order
        self._positions: dict[str, Position] = {}  # symbol → Position
        self._fills: list[Fill] = []

        # 監聽觸價單觸發事件
        self.bus.on("tick", self._check_stop_orders)

    # ── Adapter 管理 ──────────────────────────────────

    @property
    def broker_name(self) -> str:
        return self._adapter.name if self._adapter else "未連線"

    async def set_adapter(self, adapter: TradeAdapter, **credentials) -> bool:
        """切換交易券商"""
        if self._adapter and self._adapter.is_connected():
            await self._adapter.disconnect()

        self._adapter = adapter
        ok = await adapter.connect(**credentials)
        if ok:
            adapter.set_on_order_update(self._on_order_update)
            adapter.set_on_fill(self._on_fill)
            logger.info(f"[TradeModule] 已連線: {adapter.name}")
            # 同步倉位
            positions = await adapter.get_positions()
            for pos in positions:
                self._positions[pos.symbol] = pos
            # 同步今日成交明細
            self._fills = await adapter.get_fills_today()
            await self.bus.emit("trade_connected", adapter.name)
        return ok

    async def disconnect(self) -> None:
        if self._adapter:
            await self._adapter.disconnect()
            await self.bus.emit("trade_disconnected", self._adapter.name)

    @property
    def is_connected(self) -> bool:
        return self._adapter is not None and self._adapter.is_connected()

    # ── 下單 ──────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        direction: Direction,
        order_type: OrderType,
        qty: int,
        price: float = 0.0,
        source: str = "manual",
    ) -> Optional[Order]:
        """
        下單入口。觸價單在本地管理，其餘送至券商。
        """
        order_id = str(uuid.uuid4())[:8]
        order = Order(
            id=order_id,
            symbol=symbol,
            direction=direction,
            order_type=order_type,
            price=price,
            qty=qty,
            source=source,
        )

        # 觸價單：不立即送券商，改為本地監控
        if order_type in (OrderType.STOP_BUY, OrderType.STOP_SELL):
            order.status = OrderStatus.STOP_WAITING
            self._orders[order_id] = order
            logger.info(
                f"[TradeModule] 觸價單掛出: {order_type.value} {symbol} "
                f"@{price} x{qty} (等待觸發)"
            )
            await self.bus.emit("order_placed", order)
            return order

        # 限價/市價單：送券商
        if not self.is_connected:
            logger.error("[TradeModule] 未連線，無法下單")
            order.status = OrderStatus.REJECTED
            return order

        broker_id = await self._adapter.place_order(
            symbol, direction, order_type, qty, price
        )
        order.broker_order_id = broker_id
        order.status = OrderStatus.SUBMITTED
        self._orders[order_id] = order

        logger.info(
            f"[TradeModule] 委託送出: {direction.value} {symbol} "
            f"{order_type.value} @{price} x{qty} → {broker_id}"
        )
        await self.bus.emit("order_placed", order)
        return order

    async def cancel_order(self, order_id: str) -> bool:
        """取消委託"""
        order = self._orders.get(order_id)
        if not order or not order.is_active:
            return False

        # 觸價單：直接本地取消
        if order.status == OrderStatus.STOP_WAITING:
            order.status = OrderStatus.CANCELLED
            await self.bus.emit("order_cancelled", order)
            return True

        # 已送出的單：請求券商取消
        ok = await self._adapter.cancel_order(order.broker_order_id)
        if ok:
            order.status = OrderStatus.CANCELLED
            await self.bus.emit("order_cancelled", order)
        return ok

    # ── 觸價單本地監控 ────────────────────────────────

    def _check_stop_orders(self, tick) -> None:
        """
        每收到一筆 Tick，檢查是否有觸價單需要觸發。
        觸價買：當市價 >= 設定價 → 觸發市價買單
        觸價賣：當市價 <= 設定價 → 觸發市價賣單
        """
        for order in list(self._orders.values()):
            if order.status != OrderStatus.STOP_WAITING:
                continue
            if order.symbol != tick.symbol:
                continue

            triggered = False
            if order.order_type == OrderType.STOP_BUY and tick.price >= order.price:
                triggered = True
            elif order.order_type == OrderType.STOP_SELL and tick.price <= order.price:
                triggered = True

            if triggered:
                order.status = OrderStatus.FILLED  # 先標記，實際交由 async 處理
                logger.info(
                    f"[TradeModule] 觸價單觸發: {order.order_type.value} "
                    f"{order.symbol} @{order.price}"
                )
                self.bus.emit_sync("stop_triggered", order)

    # ── 券商回報處理 ──────────────────────────────────

    def _on_order_update(self, broker_order: Order) -> None:
        """券商回報: 委託狀態變更"""
        for order in self._orders.values():
            if order.broker_order_id == broker_order.broker_order_id:
                order.status = broker_order.status
                order.filled_qty = broker_order.filled_qty
                order.avg_fill_price = broker_order.avg_fill_price
                self.bus.emit_sync("order_update", order)
                break

    def _on_fill(self, fill: Fill) -> None:
        """券商回報: 成交"""
        self._fills.append(fill)
        self.bus.emit_sync("order_filled", fill)
        self._update_position(fill)

    def _update_position(self, fill: Fill) -> None:
        """根據成交更新倉位"""
        from core.models import PositionSide

        pos = self._positions.get(fill.symbol)
        if pos is None:
            side = PositionSide.LONG if fill.direction == Direction.BUY else PositionSide.SHORT
            self._positions[fill.symbol] = Position(
                symbol=fill.symbol, side=side, qty=fill.qty, avg_price=fill.price,
            )
        else:
            is_same_side = (
                (pos.side == PositionSide.LONG and fill.direction == Direction.BUY)
                or (pos.side == PositionSide.SHORT and fill.direction == Direction.SELL)
            )
            if is_same_side:
                # 加碼
                total_cost = pos.avg_price * pos.qty + fill.price * fill.qty
                pos.qty += fill.qty
                pos.avg_price = total_cost / pos.qty if pos.qty else 0
            else:
                # 減碼/反轉
                pos.qty -= fill.qty
                if pos.qty < 0:
                    pos.side = PositionSide.LONG if pos.side == PositionSide.SHORT else PositionSide.SHORT
                    pos.qty = abs(pos.qty)
                    pos.avg_price = fill.price
                elif pos.qty == 0:
                    del self._positions[fill.symbol]
                    self.bus.emit_sync("position_update", None)
                    return

        self.bus.emit_sync("position_update", self._positions.get(fill.symbol))

    # ── 查詢 ──────────────────────────────────────────

    @property
    def active_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.is_active]

    @property
    def positions(self) -> list[Position]:
        return list(self._positions.values())

    @property
    def fills_today(self) -> list[Fill]:
        return list(self._fills)

    async def get_profit_loss_today(self) -> list[dict]:
        """查詢今日已實現損益，用於比對成交明細補上平倉損益"""
        if not self.is_connected:
            return []
        return await self._adapter.get_profit_loss_today()
