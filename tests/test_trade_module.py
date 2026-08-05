"""
tests/test_trade_module.py — 交易模塊（倉位 / 委託）測試

重點在兩件事：
  1. 觸價單是「本地單」——券商端沒有這張單，觸發時必須真的補送一張市價單出去。
  2. 倉位變動一律推送整份清單，前端才知道哪一檔被平掉了。

EventBus 是 singleton，每個測試前後都要清乾淨，否則上一個測試註冊的
TradeModule 會繼續收事件（同一顆 bus 會有多個模塊的 handler）。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.event_bus import EventBus
from core.models import (
    Direction, Fill, OrderStatus, OrderType, Position, PositionSide, Tick,
)
from core.trade_module import TradeModule
from datetime import datetime


class FakeAdapter:
    """最小可用的 TradeAdapter 替身，只記錄被呼叫的內容。"""

    name = "測試券商"
    is_simulation = False

    def __init__(self, connected=True, positions=None, broker_id="B001", last_error=""):
        self._connected = connected
        self._positions = positions or []
        self._broker_id = broker_id      # 空字串 = 券商沒收下這張單
        self.last_error = last_error     # 券商拒絕的原因
        self.placed = []
        self.cancelled = []

    async def connect(self, **credentials):
        return self._connected

    async def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    def set_on_order_update(self, cb):
        self.on_order = cb

    def set_on_fill(self, cb):
        self.on_fill = cb

    async def place_order(self, symbol, direction, order_type, qty, price=0.0,
                          octype="auto", time_in_force="ROD"):
        self.placed.append({
            "symbol": symbol, "direction": direction, "order_type": order_type,
            "qty": qty, "price": price, "octype": octype,
        })
        return self._broker_id

    async def cancel_order(self, broker_order_id):
        self.cancelled.append(broker_order_id)
        return True

    async def modify_order(self, broker_order_id, new_price=0, new_qty=0):
        return True

    async def get_positions(self):
        return list(self._positions)

    async def get_open_orders(self):
        return []

    async def get_fills_today(self):
        return []

    async def get_profit_loss_today(self):
        return []


@pytest.fixture(autouse=True)
def clean_bus():
    """EventBus 是全域 singleton，每個 TradeModule 都會往上掛 handler。
    不清乾淨的話，前一個測試建立的模塊會繼續收事件，測試結果互相干擾。"""
    EventBus().clear()
    yield
    EventBus().clear()


def tick(symbol="TX", price=18000.0):
    return Tick(symbol=symbol, price=price, volume=1, timestamp=datetime.now())


async def connect(trade, adapter):
    """連線並讓 emit_sync 找得到目前的 loop（正式環境由 main.py 設定）。"""
    EventBus().set_main_loop(asyncio.get_running_loop())
    assert await trade.set_adapter(adapter) is True


# ─── 觸價單 ─────────────────────────────────────────────────

class TestStopOrders:
    def test_stop_order_not_sent_to_broker_immediately(self):
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            order = await t.place_order("TX", Direction.BUY, OrderType.STOP_BUY, 1, price=18100)
            return order, a

        order, adapter = asyncio.run(scenario())
        assert order.status is OrderStatus.STOP_WAITING
        assert adapter.placed == []          # 還沒觸發，不該送給券商

    def test_stop_buy_triggers_market_order(self):
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            order = await t.place_order("TX", Direction.BUY, OrderType.STOP_BUY, 2, price=18100)
            t._check_stop_orders(tick("TX", 18100))   # 市價 >= 觸發價
            await asyncio.sleep(0)                    # 讓 emit_sync 排的 handler 跑完
            await asyncio.sleep(0)
            return order, a

        order, adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        sent = adapter.placed[0]
        assert sent["order_type"] is OrderType.MARKET   # 觸發後改以市價送出
        assert sent["direction"] is Direction.BUY
        assert sent["qty"] == 2
        assert order.status is OrderStatus.SUBMITTED
        assert order.broker_order_id == "B001"

    def test_stop_sell_triggers_when_price_falls(self):
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            await t.place_order("TX", Direction.SELL, OrderType.STOP_SELL, 1, price=17900)
            t._check_stop_orders(tick("TX", 17899))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return a

        adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1
        assert adapter.placed[0]["direction"] is Direction.SELL

    def test_not_triggered_before_price_reached(self):
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            order = await t.place_order("TX", Direction.BUY, OrderType.STOP_BUY, 1, price=18100)
            t._check_stop_orders(tick("TX", 18099))
            await asyncio.sleep(0)
            return order, a

        order, adapter = asyncio.run(scenario())
        assert order.status is OrderStatus.STOP_WAITING
        assert adapter.placed == []

    def test_other_symbol_does_not_trigger(self):
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            order = await t.place_order("TX", Direction.BUY, OrderType.STOP_BUY, 1, price=18100)
            t._check_stop_orders(tick("MTX", 18500))
            await asyncio.sleep(0)
            return order, a

        order, adapter = asyncio.run(scenario())
        assert order.status is OrderStatus.STOP_WAITING
        assert adapter.placed == []

    def test_repeated_ticks_send_only_one_order(self):
        """送單是 async，若沒有先脫離 STOP_WAITING，後續每個 tick 都會再送一次。"""
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            await t.place_order("TX", Direction.BUY, OrderType.STOP_BUY, 1, price=18100)
            for _ in range(5):
                t._check_stop_orders(tick("TX", 18150))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return a

        adapter = asyncio.run(scenario())
        assert len(adapter.placed) == 1

    def test_rejected_when_disconnected(self):
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            order = await t.place_order("TX", Direction.BUY, OrderType.STOP_BUY, 1, price=18100)
            a._connected = False                      # 觸發前斷線
            t._check_stop_orders(tick("TX", 18100))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return order, a

        order, adapter = asyncio.run(scenario())
        assert order.status is OrderStatus.REJECTED
        assert adapter.placed == []

    def test_cancelled_stop_order_does_not_execute(self):
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            order = await t.place_order("TX", Direction.BUY, OrderType.STOP_BUY, 1, price=18100)
            await t.cancel_order(order.id)
            await t._execute_stop_order(order)   # 已取消，不該補送
            return order, a

        order, adapter = asyncio.run(scenario())
        assert order.status is OrderStatus.CANCELLED
        assert adapter.placed == []


# ─── 一般委託 ───────────────────────────────────────────────

class TestOrders:
    def test_limit_order_sent_to_broker(self):
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            order = await t.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, price=18000)
            return order, a

        order, adapter = asyncio.run(scenario())
        assert order.status is OrderStatus.SUBMITTED
        assert order.broker_order_id == "B001"
        assert adapter.placed[0]["order_type"] is OrderType.LIMIT

    def test_octype_forwarded_to_adapter(self):
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            await t.place_order("TX", Direction.SELL, OrderType.MARKET, 2, octype="cover")
            return a

        adapter = asyncio.run(scenario())
        assert adapter.placed[0]["octype"] == "cover"

    def test_order_rejected_when_disconnected(self):
        async def scenario():
            t = TradeModule()
            return await t.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, price=18000)

        order = asyncio.run(scenario())
        assert order.status is OrderStatus.REJECTED

    def test_cancel_sends_broker_order_id(self):
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            order = await t.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, price=18000)
            ok = await t.cancel_order(order.id)
            return ok, order, a

        ok, order, adapter = asyncio.run(scenario())
        assert ok is True
        assert order.status is OrderStatus.CANCELLED
        assert adapter.cancelled == ["B001"]

    def test_order_rejected_when_broker_returns_no_id(self):
        """券商沒收下（例如帳號未簽署）時回空序號，這時標成「已送出」是最危險的謊——
        使用者會以為單掛出去了。必須是 REJECTED，而且帶上券商講的原因。"""
        async def scenario():
            t = TradeModule()
            a = FakeAdapter(broker_id="", last_error="帳號尚未簽署 API 下單同意書")
            await connect(t, a)
            order = await t.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, price=18000)
            return order, t

        order, trade = asyncio.run(scenario())
        assert order.status is OrderStatus.REJECTED
        assert order.reject_reason == "帳號尚未簽署 API 下單同意書"
        # 不能進委託簿：券商端沒有這張單，留著只會變成刪不掉的幽靈單
        assert trade.active_orders == []

    def test_rejected_order_is_not_broadcast_as_placed(self):
        async def scenario():
            EventBus().set_main_loop(asyncio.get_running_loop())
            placed = []
            EventBus().on("order_placed", lambda o: placed.append(o))
            t = TradeModule()
            await connect(t, FakeAdapter(broker_id=""))
            await t.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, price=18000)
            await asyncio.sleep(0)
            return placed

        assert asyncio.run(scenario()) == []

    def test_cancel_without_broker_id_does_not_call_broker(self):
        """沒有券商序號的單刪不掉，每按一次全刪就對券商多打一發註定失敗的請求。
        直接本地作廢即可。"""
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            order = await t.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, price=18000)
            order.broker_order_id = ""            # 模擬序號遺失
            ok = await t.cancel_order(order.id)
            return ok, order, a

        ok, order, adapter = asyncio.run(scenario())
        assert ok is True
        assert order.status is OrderStatus.CANCELLED
        assert adapter.cancelled == []            # 沒有打到券商

    def test_stop_order_rejection_keeps_reason(self):
        async def scenario():
            t = TradeModule()
            a = FakeAdapter(broker_id="", last_error="保證金不足")
            await connect(t, a)
            order = await t.place_order("TX", Direction.BUY, OrderType.STOP_BUY, 1, price=18100)
            t._check_stop_orders(tick("TX", 18100))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return order

        order = asyncio.run(scenario())
        assert order.status is OrderStatus.REJECTED
        assert order.reject_reason == "保證金不足"

    def test_active_orders_excludes_finished(self):
        async def scenario():
            t, a = TradeModule(), FakeAdapter()
            await connect(t, a)
            keep = await t.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, price=18000)
            drop = await t.place_order("TX", Direction.SELL, OrderType.LIMIT, 1, price=18500)
            await t.cancel_order(drop.id)
            return [o.id for o in t.active_orders], keep.id

        active, keep_id = asyncio.run(scenario())
        assert active == [keep_id]


# ─── 倉位 ───────────────────────────────────────────────────

def fill(symbol="TX", direction=Direction.BUY, price=18000.0, qty=1):
    return Fill(order_id="x", symbol=symbol, direction=direction,
                price=price, qty=qty, fee=0.0, timestamp=datetime.now())


class TestPositions:
    def test_fill_creates_position_with_current_price(self):
        """現價留 0 的話 unrealized_pnl 會拿 0 當現價，算出整筆倉位的假虧損。"""
        t = TradeModule()
        t._update_position(fill(price=18000.0, qty=2))
        pos = t.positions[0]
        assert pos.qty == 2
        assert pos.current_price == 18000.0
        assert pos.unrealized_pnl == 0

    def test_adding_to_position_averages_price(self):
        t = TradeModule()
        t._update_position(fill(price=18000.0, qty=1))
        t._update_position(fill(price=18100.0, qty=1))
        pos = t.positions[0]
        assert pos.qty == 2
        assert pos.avg_price == 18050.0

    def test_closing_position_removes_it(self):
        t = TradeModule()
        t._update_position(fill(direction=Direction.BUY, qty=2))
        t._update_position(fill(direction=Direction.SELL, qty=2))
        assert t.positions == []

    def test_reversing_position_flips_side(self):
        t = TradeModule()
        t._update_position(fill(direction=Direction.BUY, qty=1))
        t._update_position(fill(direction=Direction.SELL, price=18200.0, qty=3))
        pos = t.positions[0]
        assert pos.side is PositionSide.SHORT
        assert pos.qty == 2
        assert pos.avg_price == 18200.0

    def test_tick_updates_current_price(self):
        t = TradeModule()
        t._update_position(fill(price=18000.0, qty=1))
        t._update_position_price(tick("TX", 18050.0))
        assert t.positions[0].current_price == 18050.0

    def test_tick_for_other_symbol_ignored(self):
        t = TradeModule()
        t._update_position(fill(price=18000.0, qty=1))
        t._update_position_price(tick("MTX", 999.0))
        assert t.positions[0].current_price == 18000.0

    def test_broadcast_sends_full_list(self):
        """只推變動的那一筆的話，倉位被平掉時前端不知道要刪哪一檔。"""
        async def scenario():
            EventBus().set_main_loop(asyncio.get_running_loop())
            received = []
            EventBus().on("positions_update", lambda ps: received.append(list(ps)))

            t = TradeModule()
            t._update_position(fill(symbol="TX", qty=1))
            t._update_position(fill(symbol="MTX", qty=1))
            t._update_position(fill(symbol="TX", direction=Direction.SELL, qty=1))
            for _ in range(4):
                await asyncio.sleep(0)
            return received

        received = asyncio.run(scenario())
        assert len(received) == 3
        assert [p.symbol for p in received[-1]] == ["MTX"]   # TX 已平掉，整份清單不再有它

    def test_connect_replaces_stale_positions(self):
        """券商端才是庫存真相，重連時不能保留上一次的殘留部位。"""
        async def scenario():
            EventBus().set_main_loop(asyncio.get_running_loop())
            t = TradeModule()
            t._update_position(fill(symbol="TE", qty=1))      # 上一輪留下的
            adapter = FakeAdapter(positions=[
                Position(symbol="TX", side=PositionSide.LONG, qty=1, avg_price=18000.0),
            ])
            await connect(t, adapter)
            return [p.symbol for p in t.positions]

        assert asyncio.run(scenario()) == ["TX"]


# ─── 損益計算 ───────────────────────────────────────────────

class TestPnl:
    def test_long_pnl_uses_point_value(self):
        p = Position(symbol="TX", side=PositionSide.LONG, qty=2,
                     avg_price=18000.0, current_price=18010.0)
        assert p.point_value == 200
        assert p.unrealized_pnl == 10 * 2 * 200

    def test_short_pnl_inverted(self):
        p = Position(symbol="MTX", side=PositionSide.SHORT, qty=1,
                     avg_price=18000.0, current_price=17990.0)
        assert p.point_value == 50
        assert p.unrealized_pnl == 10 * 1 * 50

    def test_no_price_yet_returns_zero(self):
        p = Position(symbol="TX", side=PositionSide.LONG, qty=1, avg_price=18000.0)
        assert p.unrealized_pnl == 0
