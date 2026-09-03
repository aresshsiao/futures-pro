"""
core/condition_module.py — 條件單引擎（右邊下單）

在壓力價掛空、支撐價掛多。碰到觸發價只是「開始盯」，要等價格從極值回檔
「返點」才真的進場；開了「觸後跟隨」則會一路追極值，進場價跟著極值走。
設計見 ARCHITECTURE.md §7。

目前實作範圍：
  P1 — 條件 CRUD + 持久化 + 觸發 + 回檔進場（waiting → triggered → sent → filled）
  P2 — 停利／停損 OCO 出場（filled → exited）
  P3 — 成本防線（filled → guarded）；觸後跟隨屬於進場端，見 _check_entry

收盤清倉、當沖旗標與重啟對帳（P4）尚未實作。
"""
from __future__ import annotations
import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from config import settings
from core.event_bus import EventBus
from core.models import (
    Condition, ConditionStatus, Direction, Order, OrderStatus, OrderType,
    Position, PositionSide,
)
from core.trade_module import TradeModule

logger = logging.getLogger(__name__)

# 出場原因 → log/畫面用的中文
EXIT_REASON_TEXT = {
    "take_profit": "停利",
    "stop_loss": "停損",
    "cost_guard": "成本防線",
    "session_close": "收盤清倉",
}


class ConditionModule:
    """
    條件單引擎

    職責:
      1. 條件的 CRUD 與持久化（DB 是條件的單一真相，不是瀏覽器）
      2. 每筆 tick 檢查觸發（碰到觸發價）與回檔（極值 ∓ 返點）
      3. 回檔到價後以一定範圍市價單進場，追蹤成交結果

    與 TradeModule 的關係：共用它的 place_order 送單，但各自獨立訂閱 tick。
    現有的觸價單（STOP_BUY/STOP_SELL）是另一套機制，兩者互不干涉。
    """

    def __init__(self, trade: TradeModule, db=None, close_times=None, check_interval=None,
                 day_trade=None, close_on_end=None):
        self.bus = EventBus()
        self._trade = trade
        self._db = db
        # 收盤清倉的時點與檢查週期（測試會直接指定，正式從 config/settings.py 取）
        self._close_times = list(
            close_times if close_times is not None
            else getattr(settings, "CONDITION_SESSION_CLOSE_TIMES", [])
        )
        self._check_interval = (
            check_interval if check_interval is not None
            else getattr(settings, "CONDITION_SESSION_CHECK_SEC", 20)
        )
        self._conditions: dict[str, Condition] = {}
        # 全域開關預設「暫停」：server 重啟後不該自己把昨天留下的條件送出去，
        # 一定要使用者按下「啟動交易」才會開始送單
        self._trading_enabled = False
        # 當沖（進場一律新倉、出場一律平倉）與收盤清倉。這兩個旗標沒有持久化，
        # 每次重啟都從 settings.yaml 的 condition.defaults 讀 —— 天天手動勾一次
        # 才是常態的話，那本來就該是設定值。跟 close_times 一樣，None = 取設定值，
        # 測試直接指定（否則使用者改了 settings.yaml 就會弄壞測試）。
        # 走 _apply_options() 而不是直接指派：當沖與收盤清倉互相牽動，
        # 直接塞欄位就可能生出「當沖但不清倉」這種設定檔看起來合理、實際矛盾的組合。
        self._day_trade = False
        self._close_on_end = False
        self._apply_options(
            day_trade=(
                day_trade if day_trade is not None
                else getattr(settings, "CONDITION_DEFAULT_DAY_TRADE", False)
            ),
            close_on_end=(
                close_on_end if close_on_end is not None
                else getattr(settings, "CONDITION_DEFAULT_CLOSE_ON_END", False)
            ),
        )
        self._last_price: dict[str, float] = {}   # 收盤清倉要靠它算平倉價
        # 上次檢查收盤清倉的時間。用「區間有沒有跨過收盤時點」判斷，休眠睡過頭也補得回來
        self._last_check_at: Optional[datetime] = None

        # 正在送單的條件 id。狀態圖只有六個燈，中間「已決定送出、券商還沒回覆」
        # 那一小段沒有對應狀態，用這兩個集合擋掉重複送單
        self._entering: set[str] = set()
        self._exiting: set[str] = set()
        self._exit_attempts: dict[str, int] = {}
        # 條件 id → 已平掉的口數。範圍市價是 IOC，撮不完的部分會被取消，
        # 重送時要扣掉已經出去的口數，否則第二張單會照原口數送出去變成反手開倉
        self._exit_filled: dict[str, int] = {}
        # 正在把掛著的停利限價單換成保護性出場的條件 id（見 _may_send_exit）
        self._preempting: set[str] = set()

        self.bus.on("tick", self._check_conditions)
        self.bus.on("condition_triggered", self._enter_position)
        self.bus.on("condition_exit", self._exit_position)
        # 進出場單的成交進度：TradeModule 收到成交回報後會 emit order_update
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
        """server 啟動時載回條件（此時還沒連上券商，先不判斷部位死活）。

        進行中的條件一律先擱置成 orphaned，等 reconcile_with_broker() 拿到
        真實倉位後才決定要不要接手 —— 中間這段時間就算有 tick 進來也不會亂動。
        """
        if self._db is None:
            return 0
        loaded = self._db.load_conditions()
        pending = 0
        for c in loaded:
            if c.has_entry:
                c.status = ConditionStatus.ORPHANED
                c.updated_at = datetime.now()
                self._db.save_condition(c)
                pending += 1
            self._conditions[c.id] = c
        logger.info(
            "[ConditionModule] 載入條件 %d 筆（其中 %d 筆重啟前已進場，待對帳）",
            len(loaded), pending,
        )
        return len(loaded)

    async def reconcile_with_broker(self, positions: list[Position]) -> int:
        """重啟對帳：拿券商的真實倉位決定哪些條件可以繼續管理。

        重啟後最危險的就是「本地以為還有部位」：憑空送出一張平倉單，
        等於平掉別人的倉、或是反向開一筆新倉（見 ARCHITECTURE.md §7.8）。

        比對用**每個商品的淨口數**，不試圖把條件一對一配到某張券商委託上 ——
        券商倉位是彙總後的數字，本來就分不出哪一口屬於哪個條件。
        同商品的條件只要總量對得起來就整組接手，對不起來就整組擱置等人工處理。

        回傳實際恢復管理的條件數。
        """
        restored, orphaned = 0, 0
        by_symbol: dict[str, list[Condition]] = {}
        for c in self._conditions.values():
            if c.status == ConditionStatus.ORPHANED:
                by_symbol.setdefault(c.symbol, []).append(c)

        pos_by_symbol = {p.symbol: p for p in positions}

        for symbol, group in by_symbol.items():
            # 進場單還在路上就重啟的（sent）永遠不接手：那張單成交了沒、成交幾口，
            # 重啟後已經無從得知，猜錯就是拿錯誤的成本去掛停損
            unknown = [c for c in group if not c.entry_filled_qty]
            known = [c for c in group if c.entry_filled_qty]

            expected = sum(
                c.entry_filled_qty if c.side == Direction.BUY else -c.entry_filled_qty
                for c in known
            )
            pos = pos_by_symbol.get(symbol)
            actual = 0
            if pos:
                actual = pos.qty if pos.side == PositionSide.LONG else -pos.qty

            # 條件記的部位要能被真實倉位「涵蓋」：方向一致且真實口數不少於預期。
            # 券商端多出來的部位可能是手動下的單，那不歸這裡管，不影響接手。
            covered = (
                expected != 0
                and (expected > 0) == (actual > 0)
                and abs(actual) >= abs(expected)
            )
            for c in known:
                if covered:
                    # 回到進場後的管理狀態；保本是否已啟動由浮盈重新判斷即可
                    c.status = ConditionStatus.FILLED
                    c.updated_at = datetime.now()
                    self._write_db(c)
                    restored += 1
                else:
                    orphaned += 1
            orphaned += len(unknown)

            if known and not covered:
                logger.warning(
                    "[ConditionModule] %s 對帳不符：條件記錄 %+d 口、券商實際 %+d 口"
                    " → 該商品的條件全部擱置等人工確認",
                    symbol, expected, actual,
                )
            elif known and covered:
                logger.info(
                    "[ConditionModule] %s 對帳相符（%+d 口），恢復管理 %d 筆條件",
                    symbol, expected, len(known),
                )
            if unknown:
                logger.warning(
                    "[ConditionModule] %s 有 %d 筆條件重啟前正在送單，無法確認成交狀況，擱置",
                    symbol, len(unknown),
                )

        if restored or orphaned:
            logger.info("[ConditionModule] 重啟對帳完成：恢復 %d 筆、擱置 %d 筆", restored, orphaned)
            for c in self._conditions.values():
                await self.bus.emit("condition_update", c, False)
        return restored

    async def set_trading(self, enabled: bool) -> None:
        """啟動 / 暫停交易。

        暫停只擋「新的進場」，不影響已進場部位 —— 出場保護一律照常運作，
        否則按下暫停等於裸倉（見 ARCHITECTURE.md §7.6）。
        """
        self._trading_enabled = bool(enabled)
        logger.info("[ConditionModule] 條件單交易%s", "啟動" if enabled else "暫停")
        await self._broadcast_settings()

    def _apply_options(self, day_trade=None, close_on_end=None) -> None:
        """當沖 / 收盤清倉的連動規則。__init__ 與 set_options 共用同一份。

        當沖會自動把收盤清倉一起打開 —— 當沖部位留倉就不是當沖了，
        兩者分開設定只會製造「以為在當沖、實際留倉」的意外；
        反過來，關掉收盤清倉就不算當沖，免得兩個旗標互相矛盾。

        先各自算出目標值再解連動，不是逐一套用：逐一套用的話，
        兩個值同時給（開機從設定檔載入）會互相抵銷 —— day_trade=True 先打開清倉，
        close_on_end=False 又把清倉連同當沖一起關掉，結果是設定檔寫了當沖卻沒生效。
        """
        want_day = self._day_trade if day_trade is None else bool(day_trade)
        want_close = self._close_on_end if close_on_end is None else bool(close_on_end)

        # 當沖蘊含收盤清倉。只在「這一次真的把當沖打開」時讓它壓過清倉的值，
        # 否則使用者取消勾選清倉時會被當沖鎖回去，永遠關不掉。
        if want_day and day_trade:
            want_close = True
        elif not want_close:
            want_day = False

        self._day_trade, self._close_on_end = want_day, want_close
        logger.info(
            "[ConditionModule] 當沖=%s 收盤清倉=%s", self._day_trade, self._close_on_end,
        )

    async def set_options(self, day_trade=None, close_on_end=None) -> None:
        """當沖 / 收盤清倉（來自 UI）。"""
        self._apply_options(day_trade=day_trade, close_on_end=close_on_end)
        await self._broadcast_settings()

    @property
    def settings(self) -> dict:
        return {
            "trading_enabled": self._trading_enabled,
            "day_trade": self._day_trade,
            "close_on_end": self._close_on_end,
        }

    async def _broadcast_settings(self) -> None:
        await self.bus.emit("condition_trading", self.settings)

    # ── CRUD ─────────────────────────────────────────

    async def add(
        self, symbol: str, side: Direction, trigger_price: float, qty: int = 1,
        pullback: int = 0, take_profit: int = 0, stop_loss: int = 0,
        cost_guard: bool = False, trail: bool = False,
    ) -> Condition:
        c = Condition(
            id=str(uuid.uuid4())[:8],
            symbol=symbol, side=side, trigger_price=float(trigger_price),
            pullback=max(0, int(pullback)), qty=max(1, int(qty)),
            take_profit=int(take_profit), stop_loss=int(stop_loss),
            cost_guard=bool(cost_guard), trail=bool(trail),
        )
        self._conditions[c.id] = c
        logger.info(
            "[ConditionModule] 新增條件 %s: %s %s 觸發 %s 追%s口%s",
            c.id, c.symbol, "壓力空" if c.side == Direction.SELL else "支撐多",
            c.trigger_price, c.pullback, c.qty,
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

        for key in ("symbol", "trigger_price", "pullback", "qty",
                    "take_profit", "stop_loss", "cost_guard", "trail"):
            if key in fields and fields[key] is not None:
                setattr(c, key, fields[key])
        if fields.get("side") is not None:
            c.side = Direction(fields["side"])
        c.pullback = max(0, int(c.pullback))
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
        self._exiting.discard(c.id)
        self._preempting.discard(c.id)
        self._exit_attempts.pop(c.id, None)
        self._exit_filled.pop(c.id, None)
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
        # 未連線就不要觸發：照送只會被券商打回票，把一堆條件變成 failed，
        # 使用者還得一筆一筆重設。留在原狀態，等連線回來再說。
        self._last_price[tick.symbol] = tick.price
        if not self._trade.is_connected:
            return

        for c in list(self._conditions.values()):
            if c.symbol != tick.symbol:
                continue
            if c.is_waiting or c.status == ConditionStatus.TRIGGERED:
                # 暫停交易只擋新進場（含已觸發、還在等回檔的），開關只檢查在這一支
                if self._trading_enabled:
                    self._check_entry(c, tick.price)
            elif c.is_holding:
                # 出場保護不受「暫停交易」影響 —— 暫停若連停損一起關掉，
                # 按下暫停就等於裸倉（見 ARCHITECTURE.md §7.6）
                self._check_exit(c, tick.price)

    def _check_entry(self, c: Condition, price: float) -> None:
        """兩段式進場：先碰到觸發價進入盯盤，再等回檔「返點」才送單。"""
        if c.is_waiting:
            if not c.is_hit(price):
                return
            c.status = ConditionStatus.TRIGGERED
            c.update_extreme(price)
            c.updated_at = datetime.now()
            logger.info(
                "[ConditionModule] 條件 %s 觸發: %s %s 觸發價 %s (市價 %s)，"
                "等回檔 %s 點到 %s%s",
                c.id, c.symbol, "壓力空" if c.side == Direction.SELL else "支撐多",
                c.trigger_price, price, c.pullback, c.entry_target_price,
                "（跟隨中）" if c.trail else "",
            )
            # 這個狀態可能維持很久（等回檔），一定要落 DB，重啟才不會退回 waiting
            self._sync_update(c)
            # 觸發的同一筆 tick 就可能已經滿足回檔條件（返點 0，或跳空直接穿過去）
            if not c.entry_hit(price):
                return
        else:
            moved = c.update_extreme(price)
            if moved:
                # 極值被推進 → 進場價跟著走，畫面要看得到（不寫 DB，盤中推算值）
                c.updated_at = datetime.now()
                self._sync_update(c, write_db=False)
            if not c.entry_hit(price):
                return

        # 送單是 async 的，狀態要等券商回覆才會變成 sent；在那之前得自己記住
        # 「這筆正在送」，否則送單完成前的每一筆 tick 都會再送一次
        # （現有觸價單踩過這個坑，見 trade_module._check_stop_orders）
        if c.id in self._entering:
            return
        self._entering.add(c.id)
        c.updated_at = datetime.now()
        logger.info(
            "[ConditionModule] 條件 %s 回檔進場: 極值 %s → 市價 %s，掛 %s",
            c.id, c.trigger_extreme, price, c.entry_target_price,
        )
        self.bus.emit_sync("condition_triggered", c)

    def _may_send_exit(self, c: Condition, reason: str) -> bool:
        """已經有一張出場單在處理時，這筆出場還能不能送。

        一般情況是不能 —— OCO 只送一張，另一邊自然失效。但停利掛的是 ROD 限價單，
        「送出去」不等於「出得去」：它可能一直掛著不成交，而價格反向走到停損。
        這時若照樣擋下來，整筆部位就沒有停損了，完全違背停損的意義。
        所以保護性出場可以搶：先撤掉那張停利單，再送範圍市價（見 _exit_position）。
        """
        if c.id not in self._exiting:
            return True
        return (
            reason in self._PROTECTIVE_EXIT_REASONS
            and c.exit_reason == "take_profit"
            and bool(c.exit_order_id)        # 已經掛上去了才需要搶；還在路上的等它回報
            and c.id not in self._preempting  # 送單是 async 的，別讓每筆 tick 都搶一次
        )

    def _check_exit(self, c: Condition, price: float) -> None:
        """停利／停損（OCO）。只送一張出場單，另一邊失效 —— 例外見 _may_send_exit。

        送單前先更新移動停損與成本防線 —— 停損價要用這一筆 tick 之後的值判斷，
        否則新高的那一筆會用舊停損價比對，跟隨永遠慢一拍。
        這兩件事在出場單掛著的時候也要繼續做：停利單可能一直不成交，
        期間浮盈到門檻就該啟動保本，之後回到成本價才有東西可以搶下那張停利單。
        """
        stop_before = c.active_stop_price
        self._update_peak(c, price)
        armed = self._arm_cost_guard(c)

        hit = c.exit_hit(price)
        if hit is not None and self._may_send_exit(c, hit[0]):
            reason, trigger = hit
            if c.id in self._exiting:
                self._preempting.add(c.id)
                logger.warning(
                    "[ConditionModule] 條件 %s %s：撤掉還沒成交的停利單 %s 改走保護性出場",
                    c.id, EXIT_REASON_TEXT.get(reason, reason), c.exit_order_id,
                )
            self._exiting.add(c.id)
            logger.info(
                "[ConditionModule] 條件 %s %s: 進場 %s → 觸及 %s (市價 %s)",
                c.id, EXIT_REASON_TEXT.get(reason, reason), c.entry_price, trigger, price,
            )
            self.bus.emit_sync("condition_exit", c, reason, trigger)
            return

        # 停損價被推動了才更新畫面。趨勢盤每一筆新高都會動，但沒動的 tick 佔多數，
        # 不比對就是每個 tick 對每筆條件廣播一次。
        if armed or c.active_stop_price != stop_before:
            c.updated_at = datetime.now()
            # 只有狀態變化（進 guarded）才值得寫 DB；移動停損純粹是盤中推算值，
            # 每個新高寫一次 SQLite 只是拿磁碟換沒人要的精度
            self._sync_update(c, write_db=armed)

    def _update_peak(self, c: Condition, price: float) -> None:
        """記錄進場後看過的最有利價（多單取最高、空單取最低）。只增不減。"""
        if not c.entry_price:
            return
        if not c.peak_price:
            c.peak_price = c.entry_price
        better = price > c.peak_price if c.side == Direction.BUY else price < c.peak_price
        if better:
            c.peak_price = price

    def _arm_cost_guard(self, c: Condition) -> bool:
        """浮盈達門檻就把停損移到進場價（保本），狀態轉 guarded。

        用 peak_price（看過的最大浮盈）判斷而不是現價：價格回落不該讓保本失效，
        保本是棘輪，只進不退。回傳是否在這一筆 tick 啟動。
        """
        threshold = c.cost_guard_threshold
        if not threshold or c.status != ConditionStatus.FILLED:
            return False
        if c.best_profit() < threshold:
            return False
        c.status = ConditionStatus.GUARDED
        logger.info(
            "[ConditionModule] 條件 %s 成本防線啟動: 浮盈 %.1f ≥ %.1f，停損移到進場價 %s",
            c.id, c.best_profit(), threshold, c.entry_price,
        )
        return True

    # ── 進場 ─────────────────────────────────────────

    async def _enter_position(self, c: Condition) -> None:
        """回檔到進場價後送出一定範圍市價單（MWP + IOC）。

        「回檔到 entry_target_price（極值 ∓ 返點）」這件事本模塊已經在 tick 上判斷完了，
        單子送出去的當下價格就在進場價 —— 再掛一張限價單等它成交只是多一次落空的機會。
        用範圍市價當場吃掉，成交價又被限制在保護範圍內，不會被掃到離譜的價位。
        IOC：撮不到的部分直接取消，不留一張半死不活的單在盤上。
        """
        if self._conditions.get(c.id) is not c:
            self._entering.discard(c.id)
            return   # 不是本模塊的條件（EventBus 是全域的，別人的條件不該由這裡送單）
        if c.status != ConditionStatus.TRIGGERED:
            self._entering.discard(c.id)
            return   # 已被刪除或已經送過

        price = c.entry_target_price
        order: Optional[Order] = await self._trade.place_order(
            symbol=c.symbol,
            direction=c.side,
            order_type=OrderType.MARKET_RANGE,
            qty=c.qty,
            price=0.0,             # 範圍市價不指定價格，entry_target_price 只決定「何時送」
            source=f"condition:{c.id}",
            octype="new",          # 條件單的進場一律是新倉
            time_in_force="IOC",
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
                "[ConditionModule] 條件 %s 已送出進場單 %s 範圍市價 x%s（回檔到 %s）",
                c.id, order.id, c.qty, price,
            )

        self._entering.discard(c.id)
        c.updated_at = datetime.now()
        await self._persist(c)

    # ── 出場（P2）─────────────────────────────────────

    # 出場單被拒的重試上限。進場被拒是「不做這筆交易」，不重試沒關係；
    # 出場被拒卻是「部位裸著」，完全不重試等於停損失效。但也不能無限重試——
    # 保證金不足之類的拒絕不會自己好，每個 tick 重打一次就是連續轟炸券商 API。
    MAX_EXIT_ATTEMPTS = 3

    # 保護性出場：停損／成本防線／收盤清倉。這幾種都是「一定要出去」，
    # 用一定範圍市價（MWP）+ IOC 當場吃掉；範圍市價又能擋掉流動性瞬間變差時
    # 被掃到離譜價位的成交。停利不在此列：它是「有到價才要」的被動限價單。
    _PROTECTIVE_EXIT_REASONS = ("stop_loss", "cost_guard", "session_close")

    async def _exit_position(self, c: Condition, reason: str, trigger: float) -> None:
        """送出平倉單（octype=cover）。委託方式依出場目的而定：

        - 停利：**被動限價** LMT + ROD，掛在停利價等它成交。停利是「有到價才要」，
          等不到就繼續持有，掛著也不吃價差。
        - 停損／成本防線／收盤清倉：**一定範圍市價** MWP + IOC，見上面的常數說明。
        """
        if self._conditions.get(c.id) is not c or not c.is_holding:
            self._exiting.discard(c.id)
            self._preempting.discard(c.id)
            return

        # 停利限價單可能還掛在盤上（見 _may_send_exit）。保護性出場一定要先把它撤掉：
        # 兩張平倉單同時在盤上，兩張都成交就從平倉變成反向開倉。
        # 先清 exit_order_id 再撤單，撤單回報才不會被當成「這一輪的出場結果」重複計數。
        stale_id, c.exit_order_id = c.exit_order_id, ""
        if stale_id:
            await self._trade.cancel_order(stale_id)
            stale = self._trade.get_order(stale_id)
            # 撤掉之前可能已經吃到幾口，要記進已平量，否則接下來這張照全額送
            if stale and stale.filled_qty:
                self._exit_filled[c.id] = self._exit_filled.get(c.id, 0) + stale.filled_qty

        # 平倉口數以實際進場成交口數為準，不是原本設定的 qty：
        # 部分成交時用 qty 會多平出一筆反向部位。前幾張出場單已經平掉的也要扣掉。
        qty = (c.entry_filled_qty or c.qty) - self._exit_filled.get(c.id, 0)
        if qty <= 0:
            # 分批出場已經把部位平完了，只是最後一張單的回報還沒把狀態帶到 exited。
            # 這時再送一張就不是平倉而是反手開新倉。
            self._exiting.discard(c.id)
            self._preempting.discard(c.id)
            return

        if reason in self._PROTECTIVE_EXIT_REASONS or not trigger:
            order_type, price, tif = OrderType.MARKET_RANGE, 0.0, "IOC"
        else:
            order_type, price, tif = OrderType.LIMIT, trigger, "ROD"
        order: Optional[Order] = await self._trade.place_order(
            symbol=c.symbol,
            direction=c.exit_direction,
            order_type=order_type,
            qty=qty,
            price=price,
            source=f"condition:{c.id}:{reason}",
            octype="cover",
            time_in_force=tif,
        )

        if order is None or order.status == OrderStatus.REJECTED:
            attempts = self._exit_attempts.get(c.id, 0) + 1
            self._exit_attempts[c.id] = attempts
            c.fail_reason = (order.reject_reason if order else "") or "出場單送出失敗"
            self._exiting.discard(c.id)   # 放行，下一筆 tick 再試
            self._preempting.discard(c.id)
            if attempts >= self.MAX_EXIT_ATTEMPTS:
                c.status = ConditionStatus.FAILED
                logger.error(
                    "[ConditionModule] 條件 %s 出場連續失敗 %d 次，停止重試 —— "
                    "部位可能還在，請自行處理: %s", c.id, attempts, c.fail_reason,
                )
            else:
                logger.error(
                    "[ConditionModule] 條件 %s 出場失敗（第 %d 次，會再試）: %s",
                    c.id, attempts, c.fail_reason,
                )
        else:
            c.exit_order_id = order.id
            c.exit_reason = reason
            self._preempting.discard(c.id)   # 搶單完成，exit_reason 已經換人
            # 計數器不在這裡歸零：送單被收下不代表出得去，券商「收下再取消」
            # 的話歸零會讓重試次數永遠回到 1，變成無限重送。等真的成交才清。
            logger.info(
                "[ConditionModule] 條件 %s 已送出%s單 %s %s x%s（觸及 %s）",
                c.id, EXIT_REASON_TEXT.get(reason, reason), order.id,
                "範圍市價" if order_type is OrderType.MARKET_RANGE else f"限價 {price}",
                qty, trigger,
            )

        c.updated_at = datetime.now()
        await self._persist(c)

    # ── 收盤清倉（P4）─────────────────────────────────

    async def run_session_close_watcher(self) -> None:
        """比對時鐘，到收盤清倉時點就把引擎的部位平掉。

        只看時鐘、不打券商 API，所以固定週期輪詢就夠了；用排程器算下一次觸發時間
        反而要處理跨日、夏令、系統休眠喚醒之後補跑等一堆狀況。
        """
        while True:
            try:
                await self._check_session_close()
            except Exception:
                # 這個 watcher 掛掉等於收盤不會清倉，比記一筆 log 嚴重得多，一定要撐住
                logger.exception("[ConditionModule] 收盤清倉檢查失敗")
            await asyncio.sleep(self._check_interval)

    async def _check_session_close(self) -> None:
        """判斷「有沒有跨過收盤時點」，而不是「現在是不是剛好那一分鐘」。

        比對當下分鐘的版本會被系統休眠整個跳過：筆電從 13:30 睡到 15:00，
        13:44 這一分鐘從來沒被看到，部位就這樣留倉過夜。改用「上次檢查時間 → 現在」
        這段區間有沒有涵蓋收盤時點來判斷，睡醒後補跑得到。

        用區間也順便解決了另一頭：程式在晚上才啟動時，_last_check_at 是 None，
        不會有任何區間涵蓋今天下午的 13:44，所以不會莫名其妙補跑一次白天的清倉。
        """
        if not self._close_on_end:
            return
        now = datetime.now()
        previous, self._last_check_at = self._last_check_at, now
        if previous is None:
            return   # 第一次檢查沒有區間可比，只記錄起點

        crossed = None
        for hhmm in self._close_times:
            try:
                hh, mm = (int(x) for x in hhmm.split(":"))
            except ValueError:
                logger.warning("[ConditionModule] 收盤清倉時間格式錯誤，略過: %r", hhmm)
                continue
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            # 跨午夜時（夜盤 04:59），目標時間可能落在「昨天」那一側
            for candidate in (target, target - timedelta(days=1)):
                if previous < candidate <= now:
                    crossed = hhmm
                    break
            if crossed:
                break
        if crossed is None:
            return

        holding = [c for c in self._conditions.values() if c.is_holding]
        late = (now - now.replace(hour=int(crossed[:2]), minute=int(crossed[3:]),
                                  second=0, microsecond=0)).total_seconds()
        logger.info(
            "[ConditionModule] %s 收盤清倉%s：平掉 %d 筆部位，並把交易切回暫停",
            crossed, f"（延遲 {late / 60:.0f} 分鐘，系統休眠？）" if late > 120 else "",
            len(holding),
        )
        for c in holding:
            if c.id in self._exiting:
                continue
            self._exiting.add(c.id)
            await self._exit_position(c, "session_close", self._last_price.get(c.symbol, 0.0))
        # 未觸發的條件不刪（那是使用者辛苦設的），改成把總開關關掉：
        # 收盤後不會再有新進場，明天要不要繼續由使用者自己決定
        if self._trading_enabled:
            await self.set_trading(False)

    # ── 委託回報 ──────────────────────────────────────

    async def _on_order_update(self, order: Order) -> None:
        """追蹤進場單與出場單的成交進度。

        只認 entry_order_id / exit_order_id 對得上的委託；
        同一顆 EventBus 上還有手動下單、其他模塊的委託。
        """
        for c in list(self._conditions.values()):
            if c.entry_order_id == order.id and c.status == ConditionStatus.SENT:
                await self._apply_entry_update(c, order)
                return
            if c.exit_order_id == order.id and c.is_holding:
                await self._apply_exit_update(c, order)
                return

    async def _apply_entry_update(self, c: Condition, order: Order) -> None:
        c.entry_filled_qty = order.filled_qty
        c.entry_price = order.avg_fill_price

        # 進場走 IOC，撮不完的部分會被取消 —— 「吃到幾口就取消」的回報是
        # CANCELLED 而不是 FILLED。只要有成交就是真的有部位，一定要進入
        # filled 開始管出場；當成失敗擱著等於那幾口裸奔，沒有任何停損。
        settled = order.status in (
            OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
        )
        if not settled:
            # 部分成交但單還活著：留在 sent，等它有結果
            pass
        elif order.filled_qty > 0:
            c.status = ConditionStatus.FILLED
            c.peak_price = order.avg_fill_price   # P3 的觸後跟隨從進場價起算
            logger.info(
                "[ConditionModule] 條件 %s 進場完成: %s口 @%s（停利 %s / 停損 %s）%s",
                c.id, c.entry_filled_qty, c.entry_price,
                c.take_profit_price or "—", c.stop_loss_price or "—",
                f"—— 只成交 {order.filled_qty}/{order.qty} 口，其餘已取消"
                if order.filled_qty < order.qty else "",
            )
        else:
            # 一口都沒吃到就被取消/退回：停在 failed 等人工處理，不自動改價重送
            # ——重掛迴圈在跳空時會一路追到底（見 ARCHITECTURE.md §7.4）
            c.status = ConditionStatus.FAILED
            c.fail_reason = order.reject_reason or f"進場單{order.status.value}"
            logger.warning("[ConditionModule] 條件 %s 進場單未成交: %s", c.id, c.fail_reason)

        c.updated_at = datetime.now()
        await self._persist(c)

    async def _apply_exit_update(self, c: Condition, order: Order) -> None:
        # 出場可能分成好幾張單（範圍市價是 IOC，撮不完的部分直接取消），
        # 出場均價要按口數加權，不能被最後一張單的均價蓋過去
        done_qty = self._exit_filled.get(c.id, 0)
        if order.filled_qty:
            total = done_qty + order.filled_qty
            c.exit_price = (
                c.exit_price * done_qty + order.avg_fill_price * order.filled_qty
            ) / total
        if order.status == OrderStatus.FILLED:
            c.status = ConditionStatus.EXITED
            self._exiting.discard(c.id)
            self._preempting.discard(c.id)
            self._exit_attempts.pop(c.id, None)   # 真的出場了才算數
            self._exit_filled.pop(c.id, None)
            logger.info(
                "[ConditionModule] 條件 %s 已出場（%s）: %s → %s",
                c.id, c.exit_reason, c.entry_price, c.exit_price,
            )
        elif order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            # 平倉單被券商收下後又取消/退回：部位還在，放行讓下一筆 tick 重新判斷後再送。
            # 這條路徑同樣要計次 —— 券商若每次都收單再取消，不計次就會無限重送。
            # IOC 部分成交也走這裡（成交幾口、其餘取消），先把成交的口數記下來。
            self._exit_filled[c.id] = done_qty + order.filled_qty
            # 有出掉幾口就是有進展，重試上限是用來擋「怎麼送都出不去」的死循環，
            # 不該把一口一口慢慢平掉的大單也算成失敗
            attempts = 0 if order.filled_qty else self._exit_attempts.get(c.id, 0) + 1
            self._exit_attempts[c.id] = attempts
            c.fail_reason = order.reject_reason or f"出場單{order.status.value}"
            c.exit_order_id = ""
            self._exiting.discard(c.id)
            if attempts >= self.MAX_EXIT_ATTEMPTS:
                c.status = ConditionStatus.FAILED
                logger.error(
                    "[ConditionModule] 條件 %s 出場單連續 %d 次未成交，停止重試 —— "
                    "部位可能還在，請自行處理: %s", c.id, attempts, c.fail_reason,
                )
            elif order.filled_qty:
                logger.warning(
                    "[ConditionModule] 條件 %s 出場單部分成交 %d 口（累計 %d 口），"
                    "剩下的等下一筆 tick 再送", c.id, order.filled_qty, self._exit_filled[c.id],
                )
            else:
                logger.error(
                    "[ConditionModule] 條件 %s 出場單未成交（第 %d 次），部位仍在: %s",
                    c.id, attempts, c.fail_reason,
                )
        c.updated_at = datetime.now()
        await self._persist(c)

    # ── 內部工具 ──────────────────────────────────────

    async def _persist(self, c: Condition) -> None:
        """寫 DB + 廣播。每次狀態變更都要做，前端各分頁才會同步。"""
        self._write_db(c)
        await self.bus.emit("condition_update", c, False)

    def _sync_update(self, c: Condition, write_db: bool = True) -> None:
        """_persist 的同步版，給 tick handler 用。

        tick handler 跑在主 loop 執行緒（emit_sync 會把券商 callback 排回來），
        所以這裡直接寫 sqlite 是安全的 —— 換成別的執行緒就會踩到
        sqlite3 的 check_same_thread。
        """
        if write_db:
            self._write_db(c)
        self.bus.emit_sync("condition_update", c, False)

    def _write_db(self, c: Condition) -> None:
        if self._db is None:
            return
        try:
            self._db.save_condition(c)
        except Exception:
            # 寫檔失敗不該讓引擎停擺：記憶體裡的狀態才是這一輪的依據
            logger.exception("[ConditionModule] 條件 %s 寫入 DB 失敗", c.id)
