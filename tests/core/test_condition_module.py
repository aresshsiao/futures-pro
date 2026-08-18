"""
tests/core/test_condition_module.py — 條件單引擎（右邊下單）測試

重點在幾件事：
  1. 觸發方向跟現有觸價單相反 —— 壓力空要「漲上去」才觸發、支撐多要「跌下來」。
  2. 觸發後送的是穿價限價單，不是市價單（追點 = 可接受的滑價上限）。
  3. 同一個條件只能送出一張進場單，不管中間流過幾筆 tick。
  4. 停利／停損是 OCO，只送一張平倉單；跳空同時觸及時一律先認停損。
  5. 「暫停交易」只擋新進場，已進場部位的停損照常運作 —— 否則按下暫停等於裸倉。
  6. 未連線時不觸發（留在 waiting），不然條件會被券商一次打成 failed。

EventBus 是 singleton，每個測試前後都要清乾淨，否則上一個測試建立的模塊
會繼續收事件。
"""
import asyncio
from datetime import datetime

import pytest

from core.condition_module import ConditionModule
from core.event_bus import EventBus
from core.models import (
    Condition, ConditionStatus, Direction, OrderStatus, OrderType, Tick,
)
from core.trade_module import TradeModule

from tests.core.test_trade_module import FakeAdapter


@pytest.fixture(autouse=True)
def clean_bus():
    EventBus().clear()
    yield
    EventBus().clear()


def tick(symbol="TX", price=18000.0):
    return Tick(symbol=symbol, price=price, volume=1, timestamp=datetime.now())


async def build(trading=True, **adapter_kw):
    """建好一組 TradeModule + ConditionModule，並讓 emit_sync 找得到目前的 loop。"""
    EventBus().set_main_loop(asyncio.get_running_loop())
    t, a = TradeModule(), FakeAdapter(**adapter_kw)
    assert await t.set_adapter(a) is True
    cm = ConditionModule(t, db=None)
    if trading:
        await cm.set_trading(True)
    return cm, t, a


async def settle():
    """讓 emit_sync 排進去的 handler 跑完。"""
    for _ in range(4):
        await asyncio.sleep(0)


# ─── 觸發方向與追價 ──────────────────────────────────────────

class TestTrigger:
    def test_limit_price_is_chase_through(self):
        """賣單掛低於觸發價、買單掛高於觸發價 —— 參考圖的 17059 → 17049。"""
        sell = Condition(id="a", symbol="TX", side=Direction.SELL, trigger_price=17059, chase=10)
        buy = Condition(id="b", symbol="TX", side=Direction.BUY, trigger_price=17059, chase=10)
        assert sell.limit_price == 17049
        assert buy.limit_price == 17069

    def test_resistance_short_triggers_on_rise(self):
        """壓力空：漲到壓力價才觸發（跌下去不算）。"""
        c = Condition(id="a", symbol="TX", side=Direction.SELL, trigger_price=18000)
        assert c.is_hit(18000) is True
        assert c.is_hit(18010) is True
        assert c.is_hit(17990) is False

    def test_support_long_triggers_on_fall(self):
        """支撐多：跌到支撐價才觸發。"""
        c = Condition(id="a", symbol="TX", side=Direction.BUY, trigger_price=18000)
        assert c.is_hit(18000) is True
        assert c.is_hit(17990) is True
        assert c.is_hit(18010) is False

    def test_trigger_sends_limit_order_not_market(self):
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, qty=2, chase=10)
            cm._check_conditions(tick("TX", 18000))
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        sent = adapter.placed[0]
        assert sent["order_type"] is OrderType.LIMIT      # 不是市價單
        assert sent["price"] == 17990                     # 觸發價 − 追點
        assert sent["direction"] is Direction.SELL
        assert sent["qty"] == 2
        assert sent["octype"] == "new"
        assert c.status is ConditionStatus.SENT

    def test_triggers_only_once_across_many_ticks(self):
        """狀態沒有當場離開 waiting 的話，送單完成前的每筆 tick 都會再送一張。"""
        async def scenario():
            cm, t, a = await build()
            await cm.add("TX", Direction.SELL, 18000, chase=5)
            for _ in range(5):
                cm._check_conditions(tick("TX", 18010))
            await settle()
            return a

        adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1

    def test_other_symbol_does_not_trigger(self):
        async def scenario():
            cm, t, a = await build()
            await cm.add("TX", Direction.SELL, 18000)
            cm._check_conditions(tick("MTX", 18500))
            await settle()
            return a

        assert asyncio.run(scenario()).placed == []

    def test_paused_trading_blocks_entry(self):
        async def scenario():
            cm, t, a = await build(trading=False)
            c = await cm.add("TX", Direction.SELL, 18000)
            cm._check_conditions(tick("TX", 18010))
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert adapter.placed == []
        assert c.status is ConditionStatus.WAITING   # 留著等啟動，不是被作廢

    def test_disconnected_broker_keeps_condition_waiting(self):
        """未連線就照送，只會把條件一次全打成 failed，使用者得逐筆重設。"""
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000)
            await t.disconnect()
            cm._check_conditions(tick("TX", 18010))
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert adapter.placed == []
        assert c.status is ConditionStatus.WAITING

    def test_rejected_entry_marks_failed_with_reason(self):
        async def scenario():
            # broker_id 空字串 = 券商沒收下這張單
            cm, t, a = await build(broker_id="", last_error="保證金不足")
            c = await cm.add("TX", Direction.SELL, 18000)
            cm._check_conditions(tick("TX", 18010))
            await settle()
            return c

        c = asyncio.run(scenario())
        assert c.status is ConditionStatus.FAILED
        assert "保證金不足" in c.fail_reason


# ─── 進場成交 ───────────────────────────────────────────────

class TestEntryFill:
    def test_full_fill_moves_to_filled_with_avg_price(self):
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, qty=1, chase=10, take_profit=30, stop_loss=-10)
            cm._check_conditions(tick("TX", 18010))
            await settle()
            # 券商回報成交（成交價比掛單價好一點，測試出場基準用的是實際成交價）
            order = t._orders[c.entry_order_id]
            order.filled_qty, order.avg_fill_price = 1, 17992.0
            order.status = OrderStatus.FILLED
            await EventBus().emit("order_update", order)
            return c

        c = asyncio.run(scenario())
        assert c.status is ConditionStatus.FILLED
        assert c.entry_price == 17992.0
        # 停利/停損以實際成交均價為基準，不是觸發價
        assert c.take_profit_price == 17962.0    # 空單：進場 − 利點
        assert c.stop_loss_price == 18002.0      # 空單：進場 + 損點

    def test_partial_fill_stays_sent(self):
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, qty=3)
            cm._check_conditions(tick("TX", 18010))
            await settle()
            order = t._orders[c.entry_order_id]
            order.filled_qty, order.avg_fill_price = 1, 18000.0
            order.status = OrderStatus.PARTIAL
            await EventBus().emit("order_update", order)
            return c

        c = asyncio.run(scenario())
        assert c.status is ConditionStatus.SENT
        assert c.entry_filled_qty == 1


# ─── 出場（P2）──────────────────────────────────────────────

async def entered(cm, t, side=Direction.SELL, entry=18000.0, qty=1, **kw):
    """把一個條件推到「已進場」狀態，回傳它。"""
    c = await cm.add("TX", side, entry, qty=qty, chase=5, **kw)
    cm._check_conditions(tick("TX", entry + (10 if side == Direction.SELL else -10)))
    await settle()
    order = t._orders[c.entry_order_id]
    order.filled_qty, order.avg_fill_price = qty, entry
    order.status = OrderStatus.FILLED
    await EventBus().emit("order_update", order)
    return c


class TestExit:
    def test_take_profit_sends_cover_order(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.SELL, 18000, take_profit=30, stop_loss=-10)
            a.placed.clear()
            cm._check_conditions(tick("TX", 17970))   # 空單跌 30 點 → 停利
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        sent = adapter.placed[0]
        assert sent["direction"] is Direction.BUY     # 出場是進場的反向
        assert sent["order_type"] is OrderType.LIMIT
        assert sent["price"] == 17975                 # 停利價 + 追點（買出場掛高）
        assert sent["octype"] == "cover"
        assert c.exit_reason == "take_profit"

    def test_stop_loss_sends_cover_order(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, take_profit=30, stop_loss=-10)
            a.placed.clear()
            cm._check_conditions(tick("TX", 17990))   # 多單跌 10 點 → 停損
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        assert adapter.placed[0]["direction"] is Direction.SELL
        assert adapter.placed[0]["price"] == 17985    # 停損價 − 追點（賣出場掛低）
        assert c.exit_reason == "stop_loss"

    def test_gap_through_both_prefers_stop_loss(self):
        """跳空同時穿過停利與停損：看不到中間路徑，一律先認最不利的那邊。"""
        c = Condition(
            id="a", symbol="TX", side=Direction.BUY, trigger_price=18000,
            take_profit=30, stop_loss=-10, entry_price=18000.0,
        )
        assert c.exit_hit(17980) == ("stop_loss", 17990.0)
        assert c.exit_hit(18040) == ("take_profit", 18030.0)

    def test_exit_sends_only_one_order(self):
        async def scenario():
            cm, t, a = await build()
            await entered(cm, t, Direction.SELL, 18000, take_profit=30, stop_loss=-10)
            a.placed.clear()
            for _ in range(5):
                cm._check_conditions(tick("TX", 17960))
            await settle()
            return a

        assert len(asyncio.run(scenario()).placed) == 1

    def test_exit_fill_moves_to_exited(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.SELL, 18000, take_profit=30, stop_loss=-10)
            cm._check_conditions(tick("TX", 17970))
            await settle()
            order = t._orders[c.exit_order_id]
            order.filled_qty, order.avg_fill_price = 1, 17971.0
            order.status = OrderStatus.FILLED
            await EventBus().emit("order_update", order)
            return c

        c = asyncio.run(scenario())
        assert c.status is ConditionStatus.EXITED
        assert c.exit_price == 17971.0

    def test_exit_qty_follows_actual_filled_qty(self):
        """部分成交後只平掉真正進場的口數，用原設定 qty 會多平出反向部位。"""
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, qty=3, chase=5, stop_loss=-10)
            cm._check_conditions(tick("TX", 18010))
            await settle()
            order = t._orders[c.entry_order_id]
            order.filled_qty, order.avg_fill_price = 3, 18000.0
            order.status = OrderStatus.FILLED
            await EventBus().emit("order_update", order)
            c.entry_filled_qty = 2       # 對帳後只認到 2 口
            a.placed.clear()
            cm._check_conditions(tick("TX", 18010))
            await settle()
            return a

        adapter = asyncio.run(scenario())
        assert adapter.placed[0]["qty"] == 2

    def test_paused_trading_still_runs_stop_loss(self):
        """暫停交易只擋新進場；連停損一起關掉的話，按下暫停就等於裸倉。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10)
            await cm.set_trading(False)
            a.placed.clear()
            cm._check_conditions(tick("TX", 17990))
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        assert c.exit_reason == "stop_loss"

    def test_no_exit_when_tp_sl_not_set(self):
        async def scenario():
            cm, t, a = await build()
            await entered(cm, t, Direction.SELL, 18000)   # 沒設利點/損點
            a.placed.clear()
            cm._check_conditions(tick("TX", 17000))
            cm._check_conditions(tick("TX", 19000))
            await settle()
            return a

        assert asyncio.run(scenario()).placed == []

    def test_rejected_exit_retries_then_gives_up(self):
        """出場被拒不能完全不重試（停損會失效），也不能無限重試（連續轟炸券商）。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10)
            a._broker_id = ""            # 之後的單券商一律不收
            a.last_error = "風控拒絕"
            a.placed.clear()
            for _ in range(6):
                cm._check_conditions(tick("TX", 17990))
                await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == ConditionModule.MAX_EXIT_ATTEMPTS
        assert c.status is ConditionStatus.FAILED
        assert "風控拒絕" in c.fail_reason

    def test_exit_order_cancelled_by_broker_also_gives_up_eventually(self):
        """券商「收下再取消」的路徑也要計次，否則會無限重送平倉單。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10)
            a.placed.clear()
            for _ in range(6):
                cm._check_conditions(tick("TX", 17990))
                await settle()
                if not c.exit_order_id:
                    continue
                order = t._orders[c.exit_order_id]
                order.status = OrderStatus.CANCELLED
                await EventBus().emit("order_update", order)
            return c, a

        c, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == ConditionModule.MAX_EXIT_ATTEMPTS
        assert c.status is ConditionStatus.FAILED


# ─── CRUD ───────────────────────────────────────────────────

class TestCrud:
    def test_triggered_condition_cannot_be_edited(self):
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000)
            cm._check_conditions(tick("TX", 18010))
            await settle()
            return await cm.update(c.id, trigger_price=17000), c

        updated, c = asyncio.run(scenario())
        assert updated is None
        assert c.trigger_price == 18000    # 沒被改掉

    def test_waiting_condition_can_be_edited(self):
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, qty=1)
            return await cm.update(c.id, trigger_price=18050, qty=3)

        c = asyncio.run(scenario())
        assert c.trigger_price == 18050 and c.qty == 3

    def test_remove_condition(self):
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000)
            ok = await cm.remove(c.id)
            return ok, cm.list_conditions()

        ok, remaining = asyncio.run(scenario())
        assert ok is True and remaining == []
