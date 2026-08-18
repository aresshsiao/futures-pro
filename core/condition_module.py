"""
core/condition_module.py — 條件單引擎（右邊下單）

在壓力價掛空、支撐價掛多，價格碰到就自動追價進場。設計見 ARCHITECTURE.md §7。

目前實作範圍為 **P1**：條件 CRUD + 持久化 + 觸發 + 追價進場，
狀態走到 filled 為止。出場管理（利點/損點/成本防線/觸後跟隨/收盤清倉）
屬於 P2～P4，欄位已經存下來但引擎還不會用。
"""
from __future__ import annotations
import logging
import uuid
from datetime import datetime
from typing import Optional

from core.event_bus import EventBus
from core.models import (
    Condition, ConditionStatus, Direction, Order, OrderStatus, OrderType,
)
from core.trade_module import TradeModule

logger = logging.getLogger(__name__)


class ConditionModule:
    """
    條件單引擎

    職責:
      1. 條件的 CRUD 與持久化（DB 是條件的單一真相，不是瀏覽器）
      2. 每筆 tick 檢查是否觸及觸發價
      3. 觸發後以穿價限價單進場，追蹤成交結果

    與 TradeModule 的關係：共用它的 place_order 送單，但各自獨立訂閱 tick。
    現有的觸價單（STOP_BUY/STOP_SELL）是另一套機制，兩者互不干涉。
    """

    def __init__(self, trade: TradeModule, db=None):
        self.bus = EventBus()
        self._trade = trade
        self._db = db
        self._conditions: dict[str, Condition] = {}
        # 全域開關預設「暫停」：server 重啟後不該自己把昨天留下的條件送出去，
        # 一定要使用者按下「啟動交易」才會開始送單
        self._trading_enabled = False

        self.bus.on("tick", self._check_conditions)
        self.bus.on("condition_triggered", self._enter_position)
        # 進場單的成交進度：TradeModule 收到成交回報後會 emit order_update
        self.bus.on("order_update", self._on_order_update)

    # ── 查詢 ──────────────────────────────────────────

    @property
    def trading_enabled(self) -> bool:
        return self._trading_enabled

    def list_conditions(self) -> list[Condition]:
        return sorted(self._conditions.values(), key=lambda c: c.created_at)

    def get(self, condition_id: str) -> Optional[Condition]:
        return self._conditions.get(condition_id)

    # ── 生命週期 ──────────────────────────────────────

    def load_from_db(self) -> int:
        """server 啟動時載回條件。

        P1 不做重啟對帳（P4 才會比對券商倉位），所以這裡把「進行中」的條件
        直接標成 orphaned 停住 —— 本地以為還有部位、實際上未必，
        自動接手管理就是憑空多一筆交易（見 ARCHITECTURE.md §7.8）。
        """
        if self._db is None:
            return 0
        loaded = self._db.load_conditions()
        orphaned = 0
        for c in loaded:
            if c.has_entry:
                c.status = ConditionStatus.ORPHANED
                c.updated_at = datetime.now()
                self._db.save_condition(c)
                orphaned += 1
            self._conditions[c.id] = c
        logger.info(
            "[ConditionModule] 載入條件 %d 筆（其中 %d 筆重啟前已進場，標為待確認）",
            len(loaded), orphaned,
        )
        return len(loaded)

    async def set_trading(self, enabled: bool) -> None:
        """啟動 / 暫停交易。

        暫停只擋「新的進場」，不影響已進場部位 —— P2 的出場保護一律照常運作，
        否則按下暫停等於裸倉（見 ARCHITECTURE.md §7.6）。
        """
        self._trading_enabled = bool(enabled)
        logger.info("[ConditionModule] 條件單交易%s", "啟動" if enabled else "暫停")
        await self.bus.emit("condition_trading", self._trading_enabled)

    # ── CRUD ─────────────────────────────────────────

    async def add(
        self, symbol: str, side: Direction, trigger_price: float, qty: int = 1,
        chase: int = 0, take_profit: int = 0, stop_loss: int = 0,
        cost_guard: bool = False, trail: bool = False,
    ) -> Condition:
        c = Condition(
            id=str(uuid.uuid4())[:8],
            symbol=symbol, side=side, trigger_price=float(trigger_price),
            chase=max(0, int(chase)), qty=max(1, int(qty)),
            take_profit=int(take_profit), stop_loss=int(stop_loss),
            cost_guard=bool(cost_guard), trail=bool(trail),
        )
        self._conditions[c.id] = c
        logger.info(
            "[ConditionModule] 新增條件 %s: %s %s 觸發 %s 追%s口%s",
            c.id, c.symbol, "壓力空" if c.side == Direction.SELL else "支撐多",
            c.trigger_price, c.chase, c.qty,
        )
        await self._persist(c)
        return c

    async def update(self, condition_id: str, **fields) -> Optional[Condition]:
        """修改條件。已觸發的條件不接受修改 —— 單已經在路上了，改參數只會讓
        畫面與券商端說法不一致；要改就先刪掉重設。"""
        c = self._conditions.get(condition_id)
        if c is None:
            return None
        if not c.is_waiting:
            logger.warning("[ConditionModule] 條件 %s 已是 %s，不接受修改", c.id, c.status.value)
            return None

        for key in ("symbol", "trigger_price", "chase", "qty",
                    "take_profit", "stop_loss", "cost_guard", "trail"):
            if key in fields and fields[key] is not None:
                setattr(c, key, fields[key])
        if fields.get("side") is not None:
            c.side = Direction(fields["side"])
        c.chase = max(0, int(c.chase))
        c.qty = max(1, int(c.qty))
        c.trigger_price = float(c.trigger_price)
        c.updated_at = datetime.now()
        await self._persist(c)
        return c

    async def remove(self, condition_id: str) -> bool:
        """刪除條件。

        注意：刪除**不會平倉**。已經進場的條件被刪掉，部位還在券商那裡，
        只是不再由本引擎管理 —— 這件事會寫進 log，前端也會提示。
        """
        c = self._conditions.pop(condition_id, None)
        if c is None:
            return False
        if c.has_entry:
            logger.warning(
                "[ConditionModule] 刪除已進場的條件 %s（狀態 %s）—— 部位不會被平掉，請自行處理",
                c.id, c.status.value,
            )
        c.status = ConditionStatus.CANCELLED
        c.updated_at = datetime.now()
        if self._db is not None:
            self._db.delete_condition(c.id)
        await self.bus.emit("condition_update", c, True)
        logger.info("[ConditionModule] 刪除條件 %s", c.id)
        return True

    # ── 觸發判斷 ──────────────────────────────────────

    def _check_conditions(self, tick) -> None:
        """每筆 tick 檢查一次。sync handler —— 與 TradeModule._check_stop_orders 同樣的理由：
        tick 可能來自券商 callback 執行緒，這裡只做判斷，實際送單交給 emit_sync 排進主 loop。
        """
        if not self._trading_enabled:
            return
        # 未連線就不要觸發：照送只會被券商打回票，把一堆條件變成 failed，
        # 使用者還得一筆一筆重設。留在 waiting，等連線回來再說。
        if not self._trade.is_connected:
            return

        for c in list(self._conditions.values()):
            if c.status != ConditionStatus.WAITING or c.symbol != tick.symbol:
                continue
            if not c.is_hit(tick.price):
                continue

            # 立刻離開 waiting。送單是 async 的，狀態沒有當場改掉的話，
            # 送單完成前的每一筆 tick 都會再觸發一次，同一個條件送出好幾張單
            # （現有觸價單踩過這個坑，見 trade_module._check_stop_orders）
            c.status = ConditionStatus.TRIGGERED
            c.updated_at = datetime.now()
            logger.info(
                "[ConditionModule] 條件 %s 觸發: %s %s 觸發價 %s (市價 %s) → 掛 %s",
                c.id, c.symbol, "壓力空" if c.side == Direction.SELL else "支撐多",
                c.trigger_price, tick.price, c.limit_price,
            )
            self.bus.emit_sync("condition_triggered", c)

    # ── 進場 ─────────────────────────────────────────

    async def _enter_position(self, c: Condition) -> None:
        """觸發後送出穿價限價單。"""
        if self._conditions.get(c.id) is not c:
            return   # 不是本模塊的條件（EventBus 是全域的，別人的條件不該由這裡送單）
        if c.status != ConditionStatus.TRIGGERED:
            return   # 已被刪除或重複觸發

        order: Optional[Order] = await self._trade.place_order(
            symbol=c.symbol,
            direction=c.side,
            order_type=OrderType.LIMIT,
            qty=c.qty,
            price=c.limit_price,
            source=f"condition:{c.id}",
            octype="new",          # 條件單的進場一律是新倉
        )

        if order is None or order.status == OrderStatus.REJECTED:
            c.status = ConditionStatus.FAILED
            c.fail_reason = (order.reject_reason if order else "") or "送單失敗"
            # 不自動重試：被拒的原因多半是保證金不足/未簽署，重試只會連打券商 API
            logger.error("[ConditionModule] 條件 %s 進場失敗: %s", c.id, c.fail_reason)
        else:
            c.status = ConditionStatus.SENT
            c.entry_order_id = order.id
            logger.info(
                "[ConditionModule] 條件 %s 已送出進場單 %s @%s x%s",
                c.id, order.id, c.limit_price, c.qty,
            )

        c.updated_at = datetime.now()
        await self._persist(c)

    async def _on_order_update(self, order: Order) -> None:
        """追蹤進場單的成交進度。

        只認 entry_order_id 對得上的委託；同一顆 EventBus 上還有其他來源的委託。
        """
        c = next(
            (x for x in self._conditions.values()
             if x.entry_order_id and x.entry_order_id == order.id), None
        )
        if c is None or c.status != ConditionStatus.SENT:
            return

        c.entry_filled_qty = order.filled_qty
        c.entry_price = order.avg_fill_price

        if order.status == OrderStatus.FILLED:
            c.status = ConditionStatus.FILLED
            c.peak_price = order.avg_fill_price   # P3 的觸後跟隨從進場價起算
            logger.info(
                "[ConditionModule] 條件 %s 進場完成: %s口 @%s",
                c.id, c.entry_filled_qty, c.entry_price,
            )
        elif order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            # 穿價單沒吃到就被取消/退回：停在 failed 等人工處理，不自動改價重送
            # ——追價迴圈在跳空時會一路追到底（見 ARCHITECTURE.md §7.4）
            c.status = ConditionStatus.FAILED
            c.fail_reason = order.reject_reason or f"進場單{order.status.value}"
            logger.warning("[ConditionModule] 條件 %s 進場單未成交: %s", c.id, c.fail_reason)
        else:
            # 部分成交：留在 sent，等全部成交才算進場完成
            pass

        c.updated_at = datetime.now()
        await self._persist(c)

    # ── 內部工具 ──────────────────────────────────────

    async def _persist(self, c: Condition) -> None:
        """寫 DB + 廣播。每次狀態變更都要做，前端各分頁才會同步。"""
        if self._db is not None:
            try:
                self._db.save_condition(c)
            except Exception:
                # 寫檔失敗不該讓引擎停擺：記憶體裡的狀態才是這一輪的依據
                logger.exception("[ConditionModule] 條件 %s 寫入 DB 失敗", c.id)
        await self.bus.emit("condition_update", c, False)
