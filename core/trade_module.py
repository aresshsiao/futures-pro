"""
core/trade_module.py — 交易模塊
獨立於問價模塊，負責委託管理、成交回報、倉位追蹤。
"""
from __future__ import annotations
import logging
import uuid
from datetime import datetime
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

        # 觸價單本地監控：tick 進來時檢查是否觸發，觸發後由 _execute_stop_order 送市價單
        self.bus.on("tick", self._check_stop_orders)
        self.bus.on("stop_triggered", self._execute_stop_order)
        # 倉位現價跟著 tick 走，浮動損益才不會停在建倉當下的數字
        self.bus.on("tick", self._update_position_price)

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
            # 同步倉位（整份取代，不是合併——券商端才是庫存的唯一真相，
            # 保留上一次連線的殘留會變成畫面上平不掉的幽靈倉位）
            self._positions = {p.symbol: p for p in await adapter.get_positions()}
            # 同步今日成交明細
            self._fills = await adapter.get_fills_today()
            self._broadcast_positions()
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
        octype: str = "auto",
        time_in_force: str = "ROD",
    ) -> Optional[Order]:
        """
        下單入口。觸價單在本地管理，其餘送至券商。

        octype / time_in_force 為券商層的選配參數（新倉平倉別、委託有效期），
        不支援的券商會自行忽略。
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
            symbol, direction, order_type, qty, price, octype, time_in_force
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
                # 立刻脫離 STOP_WAITING，否則送單完成前的每一筆 tick 都會再觸發一次
                order.status = OrderStatus.PENDING
                order.updated_at = datetime.now()
                logger.info(
                    f"[TradeModule] 觸價單觸發: {order.order_type.value} "
                    f"{order.symbol} @{order.price} (市價 {tick.price})"
                )
                self.bus.emit_sync("stop_triggered", order)

    async def _execute_stop_order(self, order: Order) -> None:
        """觸價單觸發後，以市價送至券商。

        觸價單從頭到尾只存在於本地（券商端沒有這張單），所以觸發時要真的補送一張
        市價單出去；沒有這一步的話觸價單只會在畫面上消失，永遠不會成交。
        """
        if self._orders.get(order.id) is not order:
            return  # 不是本模塊的單（EventBus 是全域的，別人的觸價單不該由這裡送出）
        if order.status != OrderStatus.PENDING:
            return  # 已被取消或重複觸發

        if not self.is_connected:
            logger.error("[TradeModule] 未連線，觸價單無法送出: %s", order.id)
            order.status = OrderStatus.REJECTED
            await self.bus.emit("order_update", order)
            return

        direction = Direction.BUY if order.order_type == OrderType.STOP_BUY else Direction.SELL
        try:
            broker_id = await self._adapter.place_order(
                order.symbol, direction, OrderType.MARKET, order.remaining_qty, 0.0,
            )
        except Exception:
            logger.exception("[TradeModule] 觸價單送出失敗: %s", order.id)
            broker_id = ""

        if broker_id:
            order.broker_order_id = broker_id
            order.status = OrderStatus.SUBMITTED
            logger.info("[TradeModule] 觸價單已送出市價單: %s → %s", order.id, broker_id)
        else:
            order.status = OrderStatus.REJECTED
            logger.error("[TradeModule] 觸價單送出失敗: %s", order.id)

        order.updated_at = datetime.now()
        await self.bus.emit("order_update", order)

    def _update_position_price(self, tick) -> None:
        """用即時報價更新倉位現價（不廣播——每個 tick 都推倉位會塞爆 WebSocket，
        前端拿 point_value 自己用最新報價算浮動損益）。"""
        pos = self._positions.get(tick.symbol)
        if pos is not None:
            pos.current_price = tick.price

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
                # 先用成交價當現價，下一筆 tick 進來才會更新；
                # 留 0 的話 unrealized_pnl 會算出整筆倉位的假虧損
                current_price=fill.price,
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

        self._broadcast_positions()

    def _broadcast_positions(self) -> None:
        """推送完整倉位清單。

        不能只推「有變動的那一筆」——倉位平掉時那筆會是 None，前端無從得知
        該刪哪一檔；整份送出去前端直接覆蓋，狀態永遠一致。
        """
        self.bus.emit_sync("positions_update", self.positions)

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

    # ── 改單 ──────────────────────────────────────────

    async def modify_order(self, order_id: str, new_price: float = 0, new_qty: int = 0) -> bool:
        """改價/改量。觸價單直接改本地紀錄，已送出的單請求券商改單。"""
        order = self._orders.get(order_id)
        if not order or not order.is_active:
            return False

        if order.status == OrderStatus.STOP_WAITING:
            if new_price:
                order.price = new_price
            if new_qty:
                order.qty = new_qty
            self.bus.emit_sync("order_update", order)
            return True

        if not self.is_connected:
            return False

        ok = await self._adapter.modify_order(order.broker_order_id, new_price, new_qty)
        if ok:
            if new_price:
                order.price = new_price
            if new_qty:
                order.qty = new_qty
            self.bus.emit_sync("order_update", order)
        return ok

    # ── 帳務查詢（透傳券商 adapter）────────────────────

    @property
    def is_simulation(self) -> bool:
        """目前的交易連線是否為模擬帳號"""
        return bool(self._adapter and self._adapter.is_simulation)

    async def refresh_from_broker(self) -> None:
        """重新跟券商同步倉位與今日成交（連線中途重整、或想確認模擬單有無成交時用）"""
        if not self.is_connected:
            return
        positions = await self._adapter.get_positions()
        self._positions = {p.symbol: p for p in positions}
        self._fills = await self._adapter.get_fills_today()
        self._broadcast_positions()

    async def get_open_orders(self) -> list[Order]:
        """查詢券商端未成交委託（與本地 active_orders 不同：含本程式以外下的單）"""
        if not self.is_connected:
            return []
        return await self._adapter.get_open_orders()

    async def list_accounts(self) -> list[dict]:
        if not self.is_connected:
            return []
        return await self._adapter.list_accounts()

    async def get_account_balance(self) -> dict:
        if not self.is_connected:
            return {}
        return await self._adapter.get_account_balance()

    async def get_margin(self) -> dict:
        """查詢期貨保證金專戶（權益數、可用餘額、風險指標…）"""
        if not self.is_connected:
            return {}
        return await self._adapter.get_margin()

    async def get_position_detail(self, detail_id: int = 0) -> list[dict]:
        if not self.is_connected:
            return []
        return await self._adapter.get_position_detail(detail_id)

    async def get_settlements(self) -> list[dict]:
        if not self.is_connected:
            return []
        return await self._adapter.get_settlements()

    async def get_profit_loss(self, begin_date: str = "", end_date: str = "") -> list[dict]:
        if not self.is_connected:
            return []
        return await self._adapter.get_profit_loss(begin_date, end_date)

    async def get_profit_loss_summary(self, begin_date: str = "", end_date: str = "") -> list[dict]:
        if not self.is_connected:
            return []
        return await self._adapter.get_profit_loss_summary(begin_date, end_date)

    async def get_profit_loss_detail(self, detail_id: int = 0) -> list[dict]:
        if not self.is_connected:
            return []
        return await self._adapter.get_profit_loss_detail(detail_id)
