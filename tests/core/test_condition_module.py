"""
tests/core/test_condition_module.py — 條件單引擎（右邊下單）測試

重點在幾件事：
  1. 觸發方向跟現有觸價單相反 —— 壓力空要「漲上去」才觸發、支撐多要「跌下來」。
  2. 兩段式進場：碰到觸發價只是開始盯，要等價格從極值回檔「返點」才送單。
  3. 「跟隨」是進場端的語意：觸發後繼續追極值，掛單價跟著極值走。
  4. 停利／停損是 OCO，只送一張平倉單；跳空同時觸及時一律先認停損。
  5. 「暫停交易」只擋新進場，已進場部位的停損照常運作 —— 否則按下暫停等於裸倉。
  6. 未連線時不觸發（留在 waiting），不然條件會被券商一次打成 failed。

EventBus 是 singleton，每個測試前後都要清乾淨，否則上一個測試建立的模塊
會繼續收事件。
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from core.condition_module import ConditionModule
from core.event_bus import EventBus
from core.models import (
    Condition, ConditionStatus, Direction, OrderStatus, OrderType,
    Position, PositionSide, Tick,
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


# ─── 觸發與回檔進場 ─────────────────────────────────────────

class TestTrigger:
    def test_entry_target_is_extreme_minus_pullback(self):
        """掛單價 = 極值 ∓ 返點 —— 參考圖的 17059 觸發、返點 10 → 掛 17049。"""
        sell = Condition(id="a", symbol="TX", side=Direction.SELL, trigger_price=17059, pullback=10)
        buy = Condition(id="b", symbol="TX", side=Direction.BUY, trigger_price=17059, pullback=10)
        assert sell.entry_target_price == 17049      # 壓力空：最高 − 返點
        assert buy.entry_target_price == 17069       # 支撐多：最低 + 返點

    def test_resistance_short_arms_on_rise(self):
        """壓力空：漲到壓力價才進入盯盤（跌下去不算）。"""
        c = Condition(id="a", symbol="TX", side=Direction.SELL, trigger_price=18000)
        assert c.is_hit(18000) is True
        assert c.is_hit(18010) is True
        assert c.is_hit(17990) is False

    def test_support_long_arms_on_fall(self):
        c = Condition(id="a", symbol="TX", side=Direction.BUY, trigger_price=18000)
        assert c.is_hit(18000) is True
        assert c.is_hit(17990) is True
        assert c.is_hit(18010) is False

    def test_touching_trigger_does_not_enter_yet(self):
        """碰到觸發價只是開始盯，沒回檔就不該送單 —— 這是返點的全部意義。"""
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, pullback=10)
            cm._check_conditions(tick("TX", 18000))
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert adapter.placed == []
        assert c.status is ConditionStatus.TRIGGERED
        assert c.trigger_extreme == 18000

    def test_enters_after_pullback(self):
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, qty=2, pullback=10)
            cm._check_conditions(tick("TX", 18004))   # 觸發，極值 18004
            await settle()
            cm._check_conditions(tick("TX", 17996))   # 還沒回檔滿 10 點
            await settle()
            mid = list(a.placed)
            cm._check_conditions(tick("TX", 17994))   # 18004 − 10 → 進場
            await settle()
            return c, a, mid

        c, adapter, mid = asyncio.run(scenario())
        assert mid == []
        assert len(adapter.placed) == 1
        sent = adapter.placed[0]
        # 回檔到價的判斷已經在 tick 上做完，送出去的當下價格就在進場價，
        # 再掛一張限價單只是多一次落空的機會 → 一定範圍市價 + IOC 當場吃掉
        assert sent["order_type"] is OrderType.MARKET_RANGE
        assert sent["price"] == 0.0
        assert sent["tif"] == "IOC"
        assert c.entry_target_price == 17994      # 極值 18004 − 返點 10
        assert sent["direction"] is Direction.SELL
        assert sent["qty"] == 2
        assert sent["octype"] == "new"
        assert c.status is ConditionStatus.SENT

    def test_zero_pullback_enters_on_the_trigger_tick(self):
        """返點 0 = 碰到就進，不必等回檔。"""
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, pullback=0)
            cm._check_conditions(tick("TX", 18000))
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        assert c.entry_target_price == 18000
        assert c.status is ConditionStatus.SENT

    def test_support_long_enters_on_bounce(self):
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.BUY, 18000, pullback=10)
            cm._check_conditions(tick("TX", 17995))   # 跌破支撐，極值（最低）17995
            await settle()
            before = list(a.placed)
            cm._check_conditions(tick("TX", 18005))   # 17995 + 10 → 反彈進場
            await settle()
            return c, a, before

        c, adapter, before = asyncio.run(scenario())
        assert before == []
        assert adapter.placed[0]["direction"] is Direction.BUY
        assert c.entry_target_price == 18005          # 最低 17995 + 返點 10

    def test_without_trail_extreme_is_frozen_at_trigger(self):
        """沒開跟隨：極值停在觸發當下，掛單價固定 = 觸發價 ∓ 返點。"""
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, pullback=10, trail=False)
            cm._check_conditions(tick("TX", 18000))
            await settle()
            cm._check_conditions(tick("TX", 18050))   # 續漲，但極值不跟
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert c.trigger_extreme == 18000
        assert c.entry_target_price == 17990
        assert adapter.placed == []                   # 18050 沒觸及 17990

    def test_trail_follows_the_extreme(self):
        """開了跟隨：續漲時極值跟著走，掛單價一起往上抬。"""
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, pullback=10, trail=True)
            cm._check_conditions(tick("TX", 18000))
            await settle()
            cm._check_conditions(tick("TX", 18050))   # 續漲 → 極值 18050
            await settle()
            targets = c.trigger_extreme, c.entry_target_price
            cm._check_conditions(tick("TX", 18040))   # 回檔 10 點 → 進場
            await settle()
            return c, a, targets

        c, adapter, (extreme, target) = asyncio.run(scenario())
        assert extreme == 18050 and target == 18040
        assert len(adapter.placed) == 1

    def test_trail_extreme_never_moves_backwards(self):
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, pullback=50, trail=True)
            cm._check_conditions(tick("TX", 18060))
            await settle()
            cm._check_conditions(tick("TX", 18030))   # 回檔沒滿 50 點
            await settle()
            return c

        c = asyncio.run(scenario())
        assert c.trigger_extreme == 18060             # 不會被回檔拉低

    def test_enters_only_once_across_many_ticks(self):
        """狀態沒有當場鎖住的話，送單完成前的每筆 tick 都會再送一張。"""
        async def scenario():
            cm, t, a = await build()
            await cm.add("TX", Direction.SELL, 18000, pullback=5)
            for _ in range(5):
                cm._check_conditions(tick("TX", 18000))
                cm._check_conditions(tick("TX", 17990))
            await settle()
            return a

        adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1

    def test_other_symbol_does_not_trigger(self):
        async def scenario():
            cm, t, a = await build()
            await cm.add("TX", Direction.SELL, 18000, pullback=0)
            cm._check_conditions(tick("MTX", 18500))
            await settle()
            return a

        assert asyncio.run(scenario()).placed == []

    def test_paused_trading_blocks_entry(self):
        async def scenario():
            cm, t, a = await build(trading=False)
            c = await cm.add("TX", Direction.SELL, 18000, pullback=0)
            cm._check_conditions(tick("TX", 18010))
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert adapter.placed == []
        assert c.status is ConditionStatus.WAITING   # 留著等啟動，不是被作廢

    def test_pausing_after_trigger_also_blocks_entry(self):
        """已觸發、還在等回檔的也算「新進場」，暫停一樣要擋。"""
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, pullback=10)
            cm._check_conditions(tick("TX", 18000))
            await settle()
            await cm.set_trading(False)
            cm._check_conditions(tick("TX", 17990))
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert adapter.placed == []
        assert c.status is ConditionStatus.TRIGGERED

    def test_disconnected_broker_keeps_condition_waiting(self):
        """未連線就照送，只會把條件一次全打成 failed，使用者得逐筆重設。"""
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, pullback=0)
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
            c = await cm.add("TX", Direction.SELL, 18000, pullback=0)
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
            c = await cm.add("TX", Direction.SELL, 18000, qty=1, pullback=0,
                             take_profit=30, stop_loss=-10)
            cm._check_conditions(tick("TX", 18000))
            await settle()
            # 券商回報成交（成交價跟掛單價不同，測試出場基準用的是實際成交價）
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
            c = await cm.add("TX", Direction.SELL, 18000, qty=3, pullback=0)
            cm._check_conditions(tick("TX", 18000))
            await settle()
            order = t._orders[c.entry_order_id]
            order.filled_qty, order.avg_fill_price = 1, 18000.0
            order.status = OrderStatus.PARTIAL
            await EventBus().emit("order_update", order)
            return c

        c = asyncio.run(scenario())
        assert c.status is ConditionStatus.SENT
        assert c.entry_filled_qty == 1

    def test_ioc_partial_entry_counts_as_entered(self):
        """進場走 IOC，撮不完的部分會被取消 —— 回報是 CANCELLED 而不是 FILLED。
        當成失敗擱在 failed 的話，真的吃到的那幾口就完全沒有停利停損保護。"""
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, qty=3, pullback=0, stop_loss=-10)
            cm._check_conditions(tick("TX", 18000))
            await settle()
            order = t._orders[c.entry_order_id]
            order.filled_qty, order.avg_fill_price = 1, 18000.0   # 3 口只吃到 1 口
            order.status = OrderStatus.CANCELLED
            await EventBus().emit("order_update", order)
            entered_state = (c.status, c.entry_filled_qty)
            a.placed.clear()
            cm._check_conditions(tick("TX", 18010))               # 停損
            await settle()
            return entered_state, a

        (status, filled), adapter = asyncio.run(scenario())
        assert status is ConditionStatus.FILLED
        assert filled == 1
        assert adapter.placed[0]["qty"] == 1        # 只平真正吃到的那 1 口

    def test_entry_with_no_fill_still_fails(self):
        """一口都沒吃到就被取消：那才是真的沒進場，停在 failed 等人工處理。"""
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.SELL, 18000, pullback=0)
            cm._check_conditions(tick("TX", 18000))
            await settle()
            order = t._orders[c.entry_order_id]
            order.status = OrderStatus.CANCELLED
            await EventBus().emit("order_update", order)
            return c

        assert asyncio.run(scenario()).status is ConditionStatus.FAILED


# ─── 出場（P2）──────────────────────────────────────────────

async def entered(cm, t, side=Direction.SELL, entry=18000.0, qty=1, pullback=0, **kw):
    """把一個條件推到「已進場」狀態，回傳它。

    預設返點 0，碰到觸發價就直接進場 —— 出場相關的測試不必再演一次回檔。
    """
    c = await cm.add("TX", side, entry, qty=qty, pullback=pullback, **kw)
    cm._check_conditions(tick("TX", entry))
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
        # 停利是「有到價才要」，掛被動限價等它成交，等不到就繼續持有
        assert sent["order_type"] is OrderType.LIMIT
        assert sent["price"] == 17970
        assert sent["tif"] == "ROD"
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
        # 停損是「一定要出去」，掛限價沒成交等於保護失效 → 一定範圍市價 + IOC
        assert adapter.placed[0]["order_type"] is OrderType.MARKET_RANGE
        assert adapter.placed[0]["price"] == 0.0
        assert adapter.placed[0]["tif"] == "IOC"
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
            c = await cm.add("TX", Direction.SELL, 18000, qty=3, pullback=0, stop_loss=-10)
            cm._check_conditions(tick("TX", 18000))
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

    def test_stop_loss_preempts_resting_take_profit_order(self):
        """停利掛的是 ROD 限價單，「送出去」不等於「出得去」——它可能一直不成交。
        價格反向殺到停損時若照 OCO 擋下來，整筆部位就沒有停損了。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, take_profit=30, stop_loss=-10)
            cm._check_conditions(tick("TX", 18030))   # 停利到價 → 掛限價單
            await settle()
            tp_id = c.exit_order_id
            a.placed.clear()
            cm._check_conditions(tick("TX", 17990))   # 沒成交就反向殺到停損
            await settle()
            return c, a, t._orders[tp_id]

        c, adapter, tp_order = asyncio.run(scenario())
        assert tp_order.status is OrderStatus.CANCELLED   # 先把停利單撤掉
        assert len(adapter.placed) == 1
        assert adapter.placed[0]["order_type"] is OrderType.MARKET_RANGE
        assert adapter.placed[0]["qty"] == 1              # 沒有多送一張，不會變成反向開倉
        assert c.exit_reason == "stop_loss"

    def test_take_profit_does_not_preempt_itself(self):
        """反過來不成立：停利單掛著時再到價一次，不該把自己撤掉重掛。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, take_profit=30, stop_loss=-10)
            a.placed.clear()
            for _ in range(4):
                cm._check_conditions(tick("TX", 18035))
                await settle()
            return a

        assert len(asyncio.run(scenario()).placed) == 1

    def test_partially_filled_exit_only_resends_the_remainder(self):
        """範圍市價是 IOC，撮不完的部分會被取消。重送時若不扣掉已平的口數，
        第二張單就是照原口數再送一次 —— 平完之後還會反手開出新倉。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, qty=3, stop_loss=-10)
            a.placed.clear()
            cm._check_conditions(tick("TX", 17990))
            await settle()
            order = t._orders[c.exit_order_id]           # 3 口只撮到 1 口，其餘取消
            order.filled_qty, order.avg_fill_price = 1, 17990.0
            order.status = OrderStatus.CANCELLED
            await EventBus().emit("order_update", order)
            cm._check_conditions(tick("TX", 17985))      # 下一筆 tick 重送剩下的
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert [p["qty"] for p in adapter.placed] == [3, 2]
        assert c.status is not ConditionStatus.FAILED    # 有出掉幾口就是有進展

    def test_split_exit_price_is_volume_weighted(self):
        """分批出場的均價要按口數加權，不能被最後一張單的均價蓋掉。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, qty=3, stop_loss=-10)
            cm._check_conditions(tick("TX", 17990))
            await settle()
            first = t._orders[c.exit_order_id]
            first.filled_qty, first.avg_fill_price = 1, 17990.0
            first.status = OrderStatus.CANCELLED
            await EventBus().emit("order_update", first)
            cm._check_conditions(tick("TX", 17985))
            await settle()
            second = t._orders[c.exit_order_id]
            second.filled_qty, second.avg_fill_price = 2, 17985.5
            second.status = OrderStatus.FILLED
            await EventBus().emit("order_update", second)
            return c

        c = asyncio.run(scenario())
        assert c.status is ConditionStatus.EXITED
        assert c.exit_price == pytest.approx((17990.0 + 17985.5 * 2) / 3)


# ─── 成本防線與觸後跟隨（P3）────────────────────────────────

class TestCostGuard:
    def test_arms_when_profit_reaches_stop_loss_distance(self):
        """浮盈 ≥ 損點 → 停損移到進場價，狀態轉 guarded。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10,
                              take_profit=50, cost_guard=True)
            cm._check_conditions(tick("TX", 18009))   # 浮盈 9 < 10，還不夠
            await settle()
            before = (c.status, c.active_stop_price)
            cm._check_conditions(tick("TX", 18010))   # 浮盈 10 → 啟動
            await settle()
            return before, c

        (status_before, stop_before), c = asyncio.run(scenario())
        assert status_before is ConditionStatus.FILLED
        assert stop_before == 17990.0                 # 還是原始停損
        assert c.status is ConditionStatus.GUARDED
        assert c.active_stop_price == 18000.0         # 守在進場價

    def test_stays_armed_after_price_falls_back(self):
        """保本是棘輪：價格回落不該讓它失效。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10,
                              take_profit=50, cost_guard=True)
            cm._check_conditions(tick("TX", 18010))
            await settle()
            cm._check_conditions(tick("TX", 18002))   # 回落但還沒碰到保本價
            await settle()
            return c

        c = asyncio.run(scenario())
        assert c.status is ConditionStatus.GUARDED
        assert c.active_stop_price == 18000.0

    def test_guarded_position_exits_at_entry_price(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10,
                              take_profit=50, cost_guard=True)
            cm._check_conditions(tick("TX", 18010))   # 啟動保本
            await settle()
            a.placed.clear()
            cm._check_conditions(tick("TX", 18000))   # 回到成本 → 出場
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        assert adapter.placed[0]["order_type"] is OrderType.MARKET_RANGE   # 保護性出場
        assert c.exit_reason == "cost_guard"

    def test_no_arming_without_stop_loss(self):
        """沒設損點就沒有門檻可言，保本無從啟動。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, take_profit=50, cost_guard=True)
            cm._check_conditions(tick("TX", 18040))
            await settle()
            return c

        c = asyncio.run(scenario())
        assert c.status is ConditionStatus.FILLED
        assert c.active_stop_price == 0.0

    def test_disabled_cost_guard_never_arms(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10,
                              take_profit=50, cost_guard=False)
            cm._check_conditions(tick("TX", 18040))
            await settle()
            return c

        c = asyncio.run(scenario())
        assert c.status is ConditionStatus.FILLED
        assert c.active_stop_price == 17990.0    # 停損沒被移動


# ─── 收盤清倉、當沖、重啟對帳（P4）──────────────────────────

class TestSessionClose:
    """收盤清倉是用「上次檢查 → 現在」有沒有跨過收盤時點來判斷的，
    不是比對「現在是不是剛好那一分鐘」—— 系統休眠會讓那一分鐘從來沒被看到。"""

    @staticmethod
    def arm(cm, minutes_ago=1, last_check_minutes_ago=5):
        """把收盤時點設在 minutes_ago 分鐘前，並假裝上次檢查更早之前跑過。"""
        now = datetime.now()
        target = now - timedelta(minutes=minutes_ago)
        cm._close_times = [target.strftime("%H:%M")]
        cm._last_check_at = now - timedelta(minutes=last_check_minutes_ago)

    def test_closes_holdings_at_session_time(self):
        async def scenario():
            cm, t, a = await build()
            await cm.set_options(close_on_end=True)
            c = await entered(cm, t, Direction.BUY, 18000)
            cm._check_conditions(tick("TX", 18020))
            await settle()
            a.placed.clear()
            self.arm(cm)
            await cm._check_session_close()
            return c, a, cm

        c, adapter, cm = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        sent = adapter.placed[0]
        assert sent["direction"] is Direction.SELL     # 多單平倉
        assert sent["order_type"] is OrderType.MARKET_RANGE  # 一定要平掉，不掛限價
        assert sent["octype"] == "cover"
        assert c.exit_reason == "session_close"
        assert cm.trading_enabled is False             # 收盤後不再進新倉

    def test_slept_through_close_time_still_fires(self):
        """筆電從 13:30 睡到 15:00，13:44 那一分鐘從來沒被看到 —— 醒來要補平掉，
        不然部位就這樣留倉過夜。"""
        async def scenario():
            cm, t, a = await build()
            await cm.set_options(close_on_end=True)
            c = await entered(cm, t, Direction.BUY, 18000)
            cm._check_conditions(tick("TX", 18020))
            await settle()
            a.placed.clear()
            # 收盤時點在 1 小時前，上次檢查在 2 小時前（中間整段都在休眠）
            self.arm(cm, minutes_ago=60, last_check_minutes_ago=120)
            await cm._check_session_close()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        assert c.exit_reason == "session_close"

    def test_fresh_start_does_not_backfill(self):
        """晚上才啟動程式，不該把今天下午的收盤清倉補跑一次。"""
        async def scenario():
            cm, t, a = await build()
            await cm.set_options(close_on_end=True)
            await entered(cm, t, Direction.BUY, 18000)
            a.placed.clear()
            now = datetime.now()
            cm._close_times = [(now - timedelta(minutes=60)).strftime("%H:%M")]
            cm._last_check_at = None      # 剛啟動，沒有上一次檢查
            await cm._check_session_close()
            return a

        assert asyncio.run(scenario()).placed == []

    def test_does_nothing_when_close_on_end_off(self):
        async def scenario():
            cm, t, a = await build()
            await entered(cm, t, Direction.BUY, 18000)
            a.placed.clear()
            self.arm(cm)
            await cm._check_session_close()
            return a

        assert asyncio.run(scenario()).placed == []

    def test_runs_once_per_time_point(self):
        """輪詢週期比一分鐘短，同一個時點會被檢查好幾次。"""
        async def scenario():
            cm, t, a = await build()
            await cm.set_options(close_on_end=True)
            await entered(cm, t, Direction.BUY, 18000)
            cm._check_conditions(tick("TX", 18020))
            await settle()
            a.placed.clear()
            self.arm(cm)
            for _ in range(3):
                await cm._check_session_close()
            return a

        assert len(asyncio.run(scenario()).placed) == 1

    def test_falls_back_to_market_without_quote(self):
        """收盤清倉本來就走範圍市價，沒有報價也照樣要平掉。"""
        async def scenario():
            cm, t, a = await build()
            await cm.set_options(close_on_end=True)
            await entered(cm, t, Direction.BUY, 18000)
            cm._last_price.clear()      # 假裝沒收到過任何 tick
            a.placed.clear()
            self.arm(cm)
            await cm._check_session_close()
            return a

        adapter = asyncio.run(scenario())
        assert adapter.placed[0]["order_type"] is OrderType.MARKET_RANGE

    def test_waiting_conditions_are_kept_not_deleted(self):
        """未觸發的條件是使用者辛苦設的，收盤只把總開關關掉，不刪除。"""
        async def scenario():
            cm, t, a = await build()
            await cm.set_options(close_on_end=True)
            await cm.add("TX", Direction.SELL, 18500)
            self.arm(cm)
            await cm._check_session_close()
            return cm

        cm = asyncio.run(scenario())
        assert len(cm.list_conditions()) == 1
        assert cm.list_conditions()[0].status is ConditionStatus.WAITING
        assert cm.trading_enabled is False


class TestDayTradeFlag:
    def test_day_trade_turns_on_close_on_end(self):
        """當沖部位留倉就不是當沖了，兩個旗標不該各走各的。"""
        async def scenario():
            cm, t, a = await build()
            await cm.set_options(day_trade=True)
            return cm.settings

        s = asyncio.run(scenario())
        assert s["day_trade"] is True and s["close_on_end"] is True

    def test_turning_off_close_on_end_also_clears_day_trade(self):
        async def scenario():
            cm, t, a = await build()
            await cm.set_options(day_trade=True)
            await cm.set_options(close_on_end=False)
            return cm.settings

        s = asyncio.run(scenario())
        assert s["close_on_end"] is False and s["day_trade"] is False


def position(symbol="TX", side=PositionSide.LONG, qty=1, avg=18000.0):
    return Position(symbol=symbol, side=side, qty=qty, avg_price=avg)


class TestRestartReconcile:
    def test_matching_position_restores_management(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, qty=2, stop_loss=-10)
            c.status = ConditionStatus.ORPHANED        # 模擬重啟後的起始狀態
            restored = await cm.reconcile_with_broker([position(qty=2)])
            return restored, c

        restored, c = asyncio.run(scenario())
        assert restored == 1
        assert c.status is ConditionStatus.FILLED      # 恢復管理

    def test_mismatched_qty_stays_orphaned(self):
        """條件說 2 口、券商只有 1 口 —— 差在哪不知道，不能猜。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, qty=2)
            c.status = ConditionStatus.ORPHANED
            restored = await cm.reconcile_with_broker([position(qty=1)])
            return restored, c

        restored, c = asyncio.run(scenario())
        assert restored == 0
        assert c.status is ConditionStatus.ORPHANED

    def test_opposite_direction_stays_orphaned(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, qty=1)
            c.status = ConditionStatus.ORPHANED
            restored = await cm.reconcile_with_broker([position(side=PositionSide.SHORT, qty=1)])
            return restored, c

        restored, c = asyncio.run(scenario())
        assert restored == 0
        assert c.status is ConditionStatus.ORPHANED

    def test_no_position_at_all_stays_orphaned(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, qty=1)
            c.status = ConditionStatus.ORPHANED
            restored = await cm.reconcile_with_broker([])
            return restored, c

        restored, c = asyncio.run(scenario())
        assert restored == 0
        assert c.status is ConditionStatus.ORPHANED

    def test_extra_broker_qty_still_matches(self):
        """券商端多出來的口數可能是手動下的單，不歸引擎管，不該擋住接手。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, qty=1)
            c.status = ConditionStatus.ORPHANED
            restored = await cm.reconcile_with_broker([position(qty=5)])
            return restored, c

        restored, c = asyncio.run(scenario())
        assert restored == 1 and c.status is ConditionStatus.FILLED

    def test_in_flight_entry_never_resumes(self):
        """重啟前正在送單的條件：成交了沒、成交幾口都無從得知，一律不接手。"""
        async def scenario():
            cm, t, a = await build()
            c = await cm.add("TX", Direction.BUY, 18000, qty=1)
            cm._check_conditions(tick("TX", 17990))
            await settle()
            c.status = ConditionStatus.ORPHANED        # 停在 sent 時重啟
            restored = await cm.reconcile_with_broker([position(qty=1)])
            return restored, c

        restored, c = asyncio.run(scenario())
        assert restored == 0
        assert c.status is ConditionStatus.ORPHANED

    def test_two_conditions_sum_to_position(self):
        """同商品多筆條件用淨口數比對 —— 券商倉位本來就分不出哪一口屬於誰。"""
        async def scenario():
            cm, t, a = await build()
            c1 = await entered(cm, t, Direction.BUY, 18000, qty=1)
            c2 = await entered(cm, t, Direction.BUY, 18010, qty=2)
            c1.status = c2.status = ConditionStatus.ORPHANED
            restored = await cm.reconcile_with_broker([position(qty=3)])
            return restored, c1, c2

        restored, c1, c2 = asyncio.run(scenario())
        assert restored == 2
        assert c1.status is c2.status is ConditionStatus.FILLED

    def test_orphaned_condition_does_not_trade(self):
        """擱置中的條件不能因為價格碰到停損就自己送單。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10)
            c.status = ConditionStatus.ORPHANED
            a.placed.clear()
            cm._check_conditions(tick("TX", 17900))
            await settle()
            return a

        assert asyncio.run(scenario()).placed == []


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
