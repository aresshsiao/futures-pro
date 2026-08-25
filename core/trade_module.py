"""
core/trade_module.py — 交易模塊
獨立於問價模塊，負責委託管理、成交回報、倉位追蹤。
"""
from __future__ import annotations
import asyncio
import logging
import uuid
from datetime import datetime
from typing import Optional

from core.event_bus import EventBus
from core.fill_ledger import OC_TEXT as _OC_TEXT, FillLedger
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
        # 成交明細的新倉/平倉與已實現損益（券商不給，靠成交順序推算）
        self._ledger = FillLedger()
        self._sync_handle = None                  # 待執行的倉位對帳（見 _schedule_position_sync）
        self._refreshing = False                  # 是否已有一輪對帳在跑（避免連按同步造成並發查詢）

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
            # 同步今日成交明細（含新倉/平倉與已實現損益的推算）
            self._fills = self._replay_fills(await adapter.get_fills_today())
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
            order.reject_reason = "交易券商未連線"
            return order

        try:
            broker_id = await self._adapter.place_order(
                symbol, direction, order_type, qty, price, octype, time_in_force
            )
        except Exception as e:
            logger.exception("[TradeModule] 下單擲出例外: %s %s", direction.value, symbol)
            broker_id, order.reject_reason = "", str(e)

        # 沒拿到委託序號 = 券商沒收下這張單（保證金不足、帳號未簽署、被風控擋下…）。
        # 這裡若照樣標成 SUBMITTED，畫面會顯示一張券商端根本不存在的委託：
        # 使用者以為單已掛出（最危險），而且那張單刪不掉——沒有序號可以送刪單，
        # 每按一次「全刪」就對券商多打一輪查詢。所以一律當拒絕，也不入委託簿。
        if not broker_id:
            order.status = OrderStatus.REJECTED
            order.reject_reason = order.reject_reason or getattr(self._adapter, "last_error", "") or "券商拒絕委託"
            logger.error(
                f"[TradeModule] 委託遭拒: {direction.value} {symbol} "
                f"{order_type.value} @{price} x{qty} — {order.reject_reason}"
            )
            return order

        order.broker_order_id = broker_id
        order.status = OrderStatus.SUBMITTED
        self._orders[order_id] = order

        logger.info(
            f"[TradeModule] 委託送出: {direction.value} {symbol} "
            f"{order_type.value} @{price} x{qty} octype={octype} → {broker_id}"
        )
        await self.bus.emit("order_placed", order)
        self._schedule_position_sync()
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
        if not self.is_connected:
            logger.error("[TradeModule] 未連線，無法刪單: %s", order_id)
            return False

        if not order.broker_order_id:
            # 沒有券商序號的單，券商端不存在，送刪單只是白打一發 API。
            # 直接本地作廢，免得它永遠賴在畫面上、每次「全刪」都再打一次。
            logger.warning("[TradeModule] 委託 %s 無券商序號，本地作廢（券商端無此單）", order_id)
            order.status = OrderStatus.CANCELLED
            order.updated_at = datetime.now()
            await self.bus.emit("order_cancelled", order)
            return True

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
            order.reject_reason = "交易券商未連線"
            await self.bus.emit("order_update", order)
            return

        direction = Direction.BUY if order.order_type == OrderType.STOP_BUY else Direction.SELL
        try:
            broker_id = await self._adapter.place_order(
                order.symbol, direction, OrderType.MARKET, order.remaining_qty, 0.0,
            )
        except Exception as e:
            logger.exception("[TradeModule] 觸價單送出失敗: %s", order.id)
            broker_id, order.reject_reason = "", str(e)

        if broker_id:
            order.broker_order_id = broker_id
            order.status = OrderStatus.SUBMITTED
            logger.info("[TradeModule] 觸價單已送出市價單: %s → %s", order.id, broker_id)
            self._schedule_position_sync()
        else:
            order.status = OrderStatus.REJECTED
            order.reject_reason = (
                order.reject_reason or getattr(self._adapter, "last_error", "") or "券商拒絕委託"
            )
            logger.error("[TradeModule] 觸價單送出失敗: %s — %s", order.id, order.reject_reason)

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
        if not broker_order.broker_order_id:
            # 沒序號就比對不出是哪一張；硬比會對上序號同樣是空的觸價單（本地單），
            # 把還在等待觸發的單改成別人的狀態。
            return
        for order in self._orders.values():
            if order.broker_order_id == broker_order.broker_order_id:
                order.status = broker_order.status
                order.filled_qty = broker_order.filled_qty
                order.avg_fill_price = broker_order.avg_fill_price
                self.bus.emit_sync("order_update", order)
                break

    def _on_fill(self, fill: Fill) -> None:
        """券商回報: 成交"""
        # 標上新倉/平倉與已實現損益後才推出去：畫面上這一列是即時插進去的，
        # 這裡不補，那一列就會一路空著到下一次對帳才有數字。
        self._ledger.apply(fill)
        logger.info(
            "[TradeModule] 成交: %s %s x%s @%s (%s，委託 %s)",
            fill.direction.value, fill.symbol, fill.qty, fill.price,
            _OC_TEXT.get(fill.oc_type, fill.oc_type or "未判定"), fill.order_id,
        )
        self._fills.append(fill)
        # 先讓畫面看到這筆成交，再做帳務：帳務萬一出錯，不該連帶讓成交從畫面上消失
        self.bus.emit_sync("order_filled", fill)
        self._apply_fill_to_order(fill)
        self._update_position(fill)
        # 本地推算完還是要跟券商核對一次：漏接一筆回報，倉位就會一路錯下去
        self._schedule_position_sync()

    def _apply_fill_to_order(self, fill: Fill) -> None:
        """把成交回報反映到對應的委託上。

        委託狀態若只靠「委託回報」更新，市價單就會卡住：成交後券商送的是成交回報，
        不保證會再補一次委託回報，那張單於是永遠停在畫面上的「委託中」——
        實際上早就全部成交了。成交回報的 order_id 就是委託序號，直接拿它把口數補回去。
        """
        if not fill.order_id:
            return
        order = next(
            (o for o in self._orders.values() if o.broker_order_id == fill.order_id), None
        )
        if order is None:
            return   # 本程式以外下的單（或重啟前的單），只進成交明細

        amount = order.avg_fill_price * order.filled_qty + fill.price * fill.qty
        order.filled_qty += fill.qty
        order.avg_fill_price = amount / order.filled_qty if order.filled_qty else fill.price
        order.status = (
            OrderStatus.FILLED if order.filled_qty >= order.qty else OrderStatus.PARTIAL
        )
        order.updated_at = datetime.now()
        logger.info(
            "[TradeModule] 委託 %s 成交進度: %s/%s → %s",
            order.broker_order_id, order.filled_qty, order.qty, order.status.value,
        )
        self.bus.emit_sync("order_update", order)

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

    def _replay_fills(self, fills: list[Fill]) -> list[Fill]:
        """重播券商拿回來的整份成交，標上新倉/平倉與已實現損益。

        券商給的成交只有價量方向，推算欄位得自己重算；重算前要先反推開盤前部位，
        不然留倉單的第一筆平倉會被當成新倉，後面整天的判定跟著全部反過來。
        """
        opening = FillLedger.opening_from(self._positions.values(), fills)
        if opening:
            logger.info(
                "[TradeModule] 開盤前留倉: %s",
                ", ".join(f"{s} {'多' if q > 0 else '空'}{abs(q)}" for s, q in opening.items()),
            )
        return self._ledger.replay(fills, opening)

    # ── 跟券商對帳 ────────────────────────────────────

    # 對帳前的等待秒數：市價單送出到券商把成交結算進庫存之間有時間差，
    # 太快去問會問到還沒更新的舊庫存
    POSITION_SYNC_DELAY = 3.0

    def _schedule_position_sync(self, delay: float | None = None) -> None:
        """排一次跟券商的倉位對帳（下單後、成交後各排一次）。

        本地倉位是靠成交回報一筆一筆推算出來的。回報要是沒進來（模擬環境很常見）
        或漏掉一筆，畫面就會一直停在舊數字 —— 使用者看到部位還在，只能反覆按平倉，
        而每按一次都是真的市價單送到券商，很容易從空單直接反手成多單。
        券商端才是庫存的唯一真相，所以下單／成交後主動去對一次。

        同一時間只留一個待辦：連續下單不會排出一堆重複查詢，把 API 配額燒光。
        """
        if not self.is_connected:
            return
        if self._sync_handle is not None and not self._sync_handle.done():
            return

        loop = self.bus.main_loop
        if loop is None or not loop.is_running():
            return  # 沒有可用的 loop（測試或尚未啟動），跳過即可
        # 成交回報跑在券商的 callback 執行緒，run_coroutine_threadsafe 兩邊都能用
        wait = self.POSITION_SYNC_DELAY if delay is None else delay
        self._sync_handle = asyncio.run_coroutine_threadsafe(self._sync_positions_later(wait), loop)

    async def _sync_positions_later(self, delay: float) -> None:
        await asyncio.sleep(delay)   # 等券商端把成交結算進庫存
        try:
            await self.refresh_from_broker()
        except Exception:
            logger.exception("[TradeModule] 倉位對帳失敗")

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

    def get_order(self, order_id: str) -> Optional[Order]:
        """依內部委託 id 取回委託。撤單後要知道「撤掉之前吃到幾口」時用得到。"""
        return self._orders.get(order_id)

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

        if not self.is_connected or not order.broker_order_id:
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
        if self._refreshing:
            # 使用者按「同步」按鈕沒反應時往往會連按好幾下，每一下都是一整輪券商查詢。
            # 已經有一輪在跑就讓它跑完 —— 結果一樣會廣播給所有人。
            logger.debug("[TradeModule] 已有對帳進行中，略過這次請求")
            return
        self._refreshing = True
        try:
            await self._refresh_from_broker()
        finally:
            self._refreshing = False

    async def _refresh_from_broker(self) -> None:
        positions = await self._adapter.get_positions()

        if not self.is_connected:
            # 查詢過程中才發現連線已失效（token 過期、session 斷掉）。
            # 這時拿到的空清單意思是「查不到」而不是「沒有部位」，蓋上去會讓畫面上的
            # 部位憑空消失 —— 使用者會以為已經平掉，其實倉位還在券商那裡。
            logger.error("[TradeModule] 對帳時發現券商連線失效，保留現有倉位；請重新連線券商")
            await self.bus.emit("trade_disconnected", self.broker_name)
            return

        self._positions = {p.symbol: p for p in positions}

        fills = await self._adapter.get_fills_today()
        # 查詢失敗一樣是回空清單。整份蓋過去的話，畫面上的成交明細會突然全部消失，
        # 所以只有真的拿到資料（或本地本來就是空的）才覆蓋。
        # 沒拿到資料時 ledger 也保持原狀：它靠成交回報累積的成本比重算回來的準
        # （重算只能從券商倉位反推口數，反推不出留倉成本）。
        if fills or not self._fills:
            self._fills = self._replay_fills(fills)

        logger.info(
            "[TradeModule] 跟券商對帳完成: 倉位 %s，今日成交 %d 筆",
            ", ".join(f"{p.symbol} {p.side.value} x{p.qty}" for p in positions) or "無",
            len(self._fills),
        )
        self._broadcast_positions()
        await self.bus.emit("fills_update", self.fills_today)
        await self._reconcile_orders()

    async def _reconcile_orders(self) -> None:
        """用券商端的委託狀態修正本地委託簿。

        成交回報漏接時，本地那張單會一直卡在「委託中」——畫面上看起來還有一張活單，
        使用者可能去刪它或再送一張。券商說它成交了就是成交了。

        只處理券商認得的委託序號：查詢失敗會回空清單，那時不能把本地的單當成消失。
        """
        if not self.is_connected:
            return
        remote = {
            o.broker_order_id: o
            for o in await self._adapter.get_orders_today()
            if o.broker_order_id
        }
        if not remote:
            return

        for order in list(self._orders.values()):
            if not order.is_active or not order.broker_order_id:
                continue
            latest = remote.get(order.broker_order_id)
            if latest is None:
                continue
            if latest.status == order.status and latest.filled_qty == order.filled_qty:
                continue
            logger.info(
                "[TradeModule] 委託對帳: %s %s → %s (成交 %s/%s)",
                order.broker_order_id, order.status.value, latest.status.value,
                latest.filled_qty, order.qty,
            )
            order.status = latest.status
            order.filled_qty = latest.filled_qty
            order.avg_fill_price = latest.avg_fill_price or order.avg_fill_price
            order.updated_at = datetime.now()
            await self.bus.emit("order_update", order)

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
