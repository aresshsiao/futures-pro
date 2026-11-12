"""
tests/gateway/test_broker_reconnect.py — 券商強制重新連線

永豐金的問價與交易共用同一條 session（sinopac._SHARED_API）。守住兩件事：

  1. 只重新連線其中一邊時，要升級成兩邊一起重建——否則另一邊會被留在
     已登出的舊 instance 上（實際症狀：只重連交易後，tick 不再進來、K 棒停住）。
  2. 連線 / 重連 / 斷線要排隊執行。強制重連途中兩邊都是「未連線」，前端看到後
     會自動送 connect；兩條流程交錯對同一條 session 登入登出，結果同上。
"""
import asyncio

import pytest

import main


def run(coro):
    return asyncio.run(coro)


class FakeAdapter:
    def __init__(self, broker_id="sinopac", shared=True):
        self.broker_id = broker_id
        self.shared_session = shared


class FakeModule:
    def __init__(self, adapter):
        self._adapter = adapter
        self._subscriptions = set()
        self.positions = {}
        self.connected = True
        self.disconnect_calls = 0

    @property
    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False


class FakeConditions:
    async def reconcile_with_broker(self, positions):
        return None


@pytest.fixture
def env(monkeypatch):
    quote = FakeModule(FakeAdapter())
    trade = FakeModule(FakeAdapter())
    calls = []

    async def fake_connect(broker_id, kind="both"):
        calls.append(kind)
        if kind in ("quote", "both"):
            quote.connected = True
        if kind in ("trade", "both"):
            trade.connected = True
        return True, "連線成功"

    monkeypatch.setattr(main, "quote", quote)
    monkeypatch.setattr(main, "trade", trade)
    monkeypatch.setattr(main, "conditions", FakeConditions())
    monkeypatch.setattr(main, "_connect_broker_unlocked", fake_connect)
    # module 層級的 Lock 一旦在某個 loop 上等待過就綁定該 loop，每個測試換一把新的
    monkeypatch.setattr(main, "_broker_lock", asyncio.Lock())
    return quote, trade, calls


@pytest.mark.parametrize("kind", ["quote", "trade"])
def test_single_side_reconnect_rebuilds_both_when_shared(env, kind):
    quote, trade, calls = env
    ok, _ = run(main._force_reconnect_broker(kind))
    assert ok
    assert quote.disconnect_calls == 1
    assert trade.disconnect_calls == 1
    assert calls == ["both"]


def test_single_side_reconnect_stays_single_when_not_shared(env):
    quote, trade, calls = env
    quote._adapter.shared_session = False
    run(main._force_reconnect_broker("trade"))
    assert quote.disconnect_calls == 0
    assert trade.disconnect_calls == 1
    assert calls == ["trade"]


def test_connect_waits_for_running_reconnect(env, monkeypatch):
    """強制重連跑到一半時進來的 connect，要等重連結束，而不是同時登入。"""
    quote, trade, _ = env
    active = 0
    overlapped = False

    async def slow_connect(broker_id, kind="both"):
        nonlocal active, overlapped
        active += 1
        overlapped = overlapped or active > 1
        await asyncio.sleep(0.05)
        quote.connected = trade.connected = True
        active -= 1
        return True, "連線成功"

    monkeypatch.setattr(main, "_connect_broker_unlocked", slow_connect)

    async def scenario():
        reconnect = asyncio.create_task(main._force_reconnect_broker("both"))
        await asyncio.sleep(0)       # 讓重連先拿到鎖、進入 slow_connect
        await main._connect_broker("sinopac", "both")
        await reconnect

    run(scenario())
    assert overlapped is False
