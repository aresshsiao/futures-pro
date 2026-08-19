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

async def entered(cm, t, side=Direction.SELL, entry=18000.0, qty=1, chase=5, **kw):
    """把一個條件推到「已進場」狀態，回傳它。"""
    c = await cm.add("TX", side, entry, qty=qty, chase=chase, **kw)
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
                              take_profit=50, cost_guard=True, chase=5)
            cm._check_conditions(tick("TX", 18010))   # 啟動保本
            await settle()
            a.placed.clear()
            cm._check_conditions(tick("TX", 18000))   # 回到成本 → 出場
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        assert adapter.placed[0]["price"] == 17995    # 保本價 − 追點
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


class TestTrailingStop:
    def test_stop_follows_new_highs(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10, trail=True)
            stops = []
            for p in (18005, 18020, 18050):
                cm._check_conditions(tick("TX", p))
                await settle()
                stops.append(c.active_stop_price)
            return stops, c

        stops, c = asyncio.run(scenario())
        assert stops == [17995.0, 18010.0, 18040.0]   # 一路跟著最高價往上
        assert c.peak_price == 18050

    def test_stop_never_moves_back_down(self):
        """跟隨只往有利方向動 —— 回檔時停損必須留在原地。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10, trail=True)
            cm._check_conditions(tick("TX", 18050))
            await settle()
            high_stop = c.active_stop_price
            cm._check_conditions(tick("TX", 18042))   # 回檔但沒碰到停損
            await settle()
            return high_stop, c

        high_stop, c = asyncio.run(scenario())
        assert high_stop == 18040.0
        assert c.active_stop_price == 18040.0
        assert c.peak_price == 18050                  # peak 不會被回檔拉低

    def test_short_trails_downward(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.SELL, 18000, stop_loss=-10, trail=True)
            cm._check_conditions(tick("TX", 17950))
            await settle()
            return c

        c = asyncio.run(scenario())
        assert c.peak_price == 17950
        assert c.active_stop_price == 17960.0    # 空單的停損在上方，跟著新低往下

    def test_trailing_stop_triggers_exit(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10, trail=True, chase=5)
            cm._check_conditions(tick("TX", 18050))   # 停損被推到 18040
            await settle()
            a.placed.clear()
            cm._check_conditions(tick("TX", 18040))
            await settle()
            return c, a

        c, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        assert adapter.placed[0]["price"] == 18035    # 移動停損價 − 追點
        assert c.exit_reason == "trail"

    def test_trail_and_cost_guard_take_most_protective(self):
        """兩個都開時取最保護的那個 —— 跟隨走遠後會蓋過保本。"""
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10,
                              cost_guard=True, trail=True)
            cm._check_conditions(tick("TX", 18010))   # 保本啟動（停損 18000）
            await settle()
            armed_stop = c.active_stop_price
            cm._check_conditions(tick("TX", 18060))   # 跟隨推到 18050
            await settle()
            return armed_stop, c

        armed_stop, c = asyncio.run(scenario())
        assert armed_stop == 18000.0
        assert c.status is ConditionStatus.GUARDED
        assert c.active_stop_price == 18050.0
        assert c.stop_kind == "trail"

    def test_disabled_trail_keeps_fixed_stop(self):
        async def scenario():
            cm, t, a = await build()
            c = await entered(cm, t, Direction.BUY, 18000, stop_loss=-10, trail=False)
            cm._check_conditions(tick("TX", 18100))
            await settle()
            return c

        assert asyncio.run(scenario()).active_stop_price == 17990.0


# ─── 收盤清倉、當沖、重啟對帳（P4）──────────────────────────

class TestSessionClose:
    def test_closes_holdings_at_session_time(self):
        async def scenario():
            cm, t, a = await build()
            # 讓收盤時點就是「現在」，不必等真的到 13:44
            cm._close_times = [datetime.now().strftime("%H:%M")]
            await cm.set_options(close_on_end=True)
            c = await entered(cm, t, Direction.BUY, 18000, chase=5)
            cm._check_conditions(tick("TX", 18020))    # 餵一筆報價當平倉價依據
            await settle()
            a.placed.clear()
            await cm._check_session_close()
            return c, a, cm

        c, adapter, cm = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        sent = adapter.placed[0]
        assert sent["direction"] is Direction.SELL     # 多單平倉
        assert sent["price"] == 18015                  # 最後成交價 − 追點
        assert sent["octype"] == "cover"
        assert c.exit_reason == "session_close"
        assert cm.trading_enabled is False             # 收盤後不再進新倉

    def test_does_nothing_when_close_on_end_off(self):
        async def scenario():
            cm, t, a = await build()
            cm._close_times = [datetime.now().strftime("%H:%M")]
            await entered(cm, t, Direction.BUY, 18000)
            a.placed.clear()
            await cm._check_session_close()
            return a

        assert asyncio.run(scenario()).placed == []

    def test_runs_once_per_time_point(self):
        """輪詢週期比一分鐘短，同一個時點會被檢查好幾次。"""
        async def scenario():
            cm, t, a = await build()
            cm._close_times = [datetime.now().strftime("%H:%M")]
            await cm.set_options(close_on_end=True)
            await entered(cm, t, Direction.BUY, 18000)
            cm._check_conditions(tick("TX", 18020))
            await settle()
            a.placed.clear()
            for _ in range(3):
                await cm._check_session_close()
            return a

        assert len(asyncio.run(scenario()).placed) == 1

    def test_falls_back_to_market_without_quote(self):
        """沒有報價就算不出穿價價位 —— 收盤前平不掉部位比滑價嚴重。"""
        async def scenario():
            cm, t, a = await build()
            cm._close_times = [datetime.now().strftime("%H:%M")]
            await cm.set_options(close_on_end=True)
            await entered(cm, t, Direction.BUY, 18000)
            cm._last_price.clear()      # 假裝沒收到過任何 tick
            a.placed.clear()
            await cm._check_session_close()
            return a

        adapter = asyncio.run(scenario())
        assert adapter.placed[0]["order_type"] is OrderType.MARKET

    def test_waiting_conditions_are_kept_not_deleted(self):
        """未觸發的條件是使用者辛苦設的，收盤只把總開關關掉，不刪除。"""
        async def scenario():
            cm, t, a = await build()
            cm._close_times = [datetime.now().strftime("%H:%M")]
            await cm.set_options(close_on_end=True)
            await cm.add("TX", Direction.SELL, 18500)
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
