"""
tests/test_broker_sinopac.py — 永豐金 Adapter 測試

測試環境沒有真實的 Shioaji 憑證，所有需要連線的測試都用 unittest.mock
模擬 shioaji 模組。兩個關鍵前提：

  1. _SHARED_API 是 module 級別的單例（問價/交易共用同一條連線），
     測試之間必須重置，否則後面的測試會沿用前面那條「已連線」的假 API。
  2. 真實的 login() 會透過 contracts_cb 回報合約下載完成，adapter 用它
     來結束等待（見 _wait_contracts_ready）。mock 的 login 若不呼叫這個
     callback，每次 connect() 都會白等滿 15 秒 timeout。

涵蓋:
  - 連線 / 斷線 / 模擬(simulation)環境切換
  - 合約查詢對照表、歷史K線
  - 行情查詢: ticks / snapshot / contract_info / usage
  - 交易: 下單(含 octype/IOC)、刪單、改單、未成交委託、倉位
  - 回報 callback: 委託回報與成交回報的分派、狀態對照
  - 帳務查詢: margin / balance / accounts / 已實現損益
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import brokers.adapters.sinopac as sinopac
from brokers.adapters.sinopac import SinoPacQuoteAdapter, SinoPacTradeAdapter
from core.models import Direction, OrderStatus, OrderType, PositionSide, Timeframe


# ─── helpers ─────────────────────────────────────────────────

def run(coro):
    """在同步測試中執行 async 函式。"""
    return asyncio.run(coro)


class FakeObj:
    """模擬 Shioaji 的回傳物件：屬性 + .dict()（_obj_to_dict 會優先用 .dict()）。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def dict(self):
        return dict(self.__dict__)


@pytest.fixture(autouse=True)
def reset_shared_api():
    """每個測試前後都清掉共用連線單例，避免測試之間互相污染。"""
    sinopac._SHARED_API = None
    sinopac._SHARED_CONNECTED = False
    sinopac._SHARED_SIMULATION = False
    yield
    sinopac._SHARED_API = None
    sinopac._SHARED_CONNECTED = False
    sinopac._SHARED_SIMULATION = False


def _make_mock_shioaji():
    """回傳 (模擬 shioaji 模組, api 實例, contract 實例)。"""
    sj_mod = MagicMock()
    api = MagicMock()
    sj_mod.Shioaji.return_value = api

    account = FakeObj(account_id="F123456", account_type="Future",
                      broker_id="F00", person_id="A1", username="tester", signed=True)
    api.futopt_account = account

    def _fake_login(**kwargs):
        # 真實的 login() 會逐一回報各 SecurityType 下載完成；
        # 不帶參數呼叫時 adapter 會保守地把所有分類標記為完成。
        cb = kwargs.get("contracts_cb")
        if cb:
            cb()
        return [account]

    api.login.side_effect = _fake_login
    api.logout.return_value = None

    contract_mock = MagicMock()
    contract_mock.code = "TXFR1"
    api.Contracts.Futures.__getitem__.return_value = contract_mock

    return sj_mod, api, contract_mock


def _connected_quote():
    adapter = SinoPacQuoteAdapter()
    sj_mod, api, contract = _make_mock_shioaji()
    with patch.dict(sys.modules, {"shioaji": sj_mod}):
        run(adapter.connect(api_key="K", secret_key="S"))
    return adapter, api, sj_mod, contract


def _connected_trade(**credentials):
    adapter = SinoPacTradeAdapter()
    sj_mod, api, contract = _make_mock_shioaji()
    creds = {"api_key": "K", "secret_key": "S"}
    creds.update(credentials)
    with patch.dict(sys.modules, {"shioaji": sj_mod}):
        run(adapter.connect(**creds))
    return adapter, api, sj_mod, contract


# ─── QuoteAdapter — 初始狀態 / 連線 ──────────────────────────

class TestQuoteAdapterConnect:
    def test_not_connected_initially(self):
        adapter = SinoPacQuoteAdapter()
        assert adapter.is_connected() is False
        assert adapter._api is None

    def test_connect_success(self):
        adapter, api, _, _ = _connected_quote()
        assert adapter.is_connected() is True
        assert adapter._api is api
        assert api.login.call_count == 1
        kwargs = api.login.call_args.kwargs
        assert kwargs["api_key"] == "K"
        assert kwargs["secret_key"] == "S"

    def test_connect_failure_returns_false(self):
        adapter = SinoPacQuoteAdapter()
        sj_mod = MagicMock()
        sj_mod.Shioaji.return_value.login.side_effect = RuntimeError("登入失敗")

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            result = run(adapter.connect(api_key="BAD", secret_key="BAD"))

        assert result is False
        assert adapter.is_connected() is False

    def test_disconnect_calls_logout(self):
        adapter, api, _, _ = _connected_quote()
        run(adapter.disconnect())
        assert adapter.is_connected() is False
        api.logout.assert_called_once()

    def test_disconnect_when_not_connected_does_not_raise(self):
        adapter = SinoPacQuoteAdapter()
        run(adapter.disconnect())


# ─── 模擬環境 (simulation) ───────────────────────────────────

class TestSimulationMode:
    def test_production_by_default(self):
        adapter, _, sj_mod, _ = _connected_quote()
        sj_mod.Shioaji.assert_called_once_with(simulation=False)
        assert adapter.is_connected() is True

    def test_simulation_flag_passed_to_shioaji(self):
        adapter = SinoPacTradeAdapter()
        sj_mod, api, _ = _make_mock_shioaji()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.connect(api_key="K", secret_key="S", simulation=True))

        sj_mod.Shioaji.assert_called_once_with(simulation=True)
        assert adapter.is_simulation is True

    def test_simulation_skips_activate_ca(self):
        """模擬環境沒有憑證機制，帶了 cert 也要跳過 activate_ca。"""
        adapter = SinoPacTradeAdapter()
        sj_mod, api, _ = _make_mock_shioaji()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.connect(
                api_key="K", secret_key="S", simulation=True,
                cert_path="/path/cert.pfx", cert_password="pw", person_id="A123456789",
            ))
        api.activate_ca.assert_not_called()

    def test_production_with_cert_calls_activate_ca(self):
        adapter, api, _, _ = _connected_trade(
            cert_path="/path/cert.pfx", cert_password="pw", person_id="A123456789",
        )
        api.activate_ca.assert_called_once()

    def test_switching_mode_rebuilds_instance(self):
        """simulation 是建構子參數，切換模式必須登出重建，不能沿用舊 instance。"""
        adapter, api, sj_mod, _ = _connected_trade()
        assert sinopac._SHARED_SIMULATION is False

        adapter2 = SinoPacTradeAdapter()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter2.connect(api_key="K", secret_key="S", simulation=True))

        api.logout.assert_called_once()          # 舊連線被登出
        assert sj_mod.Shioaji.call_count == 2    # 重新建立 instance
        assert sinopac._SHARED_SIMULATION is True


# ─── QuoteAdapter — 合約 / 歷史K線 ───────────────────────────

class TestContractAndHistory:
    def test_tx_maps_to_txfr1(self):
        adapter, api, _, contract = _connected_quote()
        assert run(adapter._get_contract("TX")) is contract
        api.Contracts.Futures.__getitem__.assert_called_with("TXFR1")

    def test_mtx_maps_to_mxfr1(self):
        adapter, api, _, _ = _connected_quote()
        run(adapter._get_contract("MTX"))
        api.Contracts.Futures.__getitem__.assert_called_with("MXFR1")

    def test_unknown_symbol_returns_none(self):
        adapter, api, _, _ = _connected_quote()
        api.Contracts.Futures.__getitem__.side_effect = KeyError("XXX")
        assert run(adapter._get_contract("XXX", attempts=1)) is None

    def test_history_returns_empty_when_not_connected(self):
        adapter = SinoPacQuoteAdapter()
        assert run(adapter.get_history_bars("TX", Timeframe.D1)) == []

    def test_history_returns_bars(self):
        adapter, api, sj_mod, _ = _connected_quote()
        kbars = MagicMock()
        kbars.ts = [1704153600_000_000_000]  # 2024-01-02 (ns)
        kbars.Open, kbars.High = [18000.0], [18100.0]
        kbars.Low, kbars.Close = [17900.0], [18050.0]
        kbars.Volume = [50000]
        api.kbars.return_value = kbars

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            bars = run(adapter.get_history_bars("TX", Timeframe.D1, count=1))

        assert len(bars) == 1
        assert bars[0].symbol == "TX"
        assert bars[0].open == 18000.0
        assert bars[0].is_closed is True

    def test_history_returns_empty_on_api_exception(self):
        adapter, api, sj_mod, _ = _connected_quote()
        api.kbars.side_effect = RuntimeError("API 錯誤")
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            assert run(adapter.get_history_bars("TX", Timeframe.D1)) == []


# ─── QuoteAdapter — 其他行情查詢 ─────────────────────────────

class TestQuoteQueries:
    def test_get_ticks_converts_ns_to_ms(self):
        adapter, api, sj_mod, _ = _connected_quote()
        ticks = MagicMock()
        ticks.ts = [1704153600_000_000_000]
        ticks.close, ticks.volume = [18000.0], [3]
        ticks.bid_price, ticks.bid_volume = [17999.0], [10]
        ticks.ask_price, ticks.ask_volume = [18001.0], [12]
        ticks.tick_type = [1]
        api.ticks.return_value = ticks

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            rows = run(adapter.get_ticks("TX", date="2024-01-02"))

        assert len(rows) == 1
        assert rows[0]["time"] == 1704153600_000
        assert rows[0]["price"] == 18000.0
        assert rows[0]["ask_volume"] == 12

    def test_get_ticks_last_count_passes_last_cnt(self):
        adapter, api, sj_mod, _ = _connected_quote()
        ticks = MagicMock()
        ticks.ts = []
        api.ticks.return_value = ticks

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.get_ticks("TX", query_type="LastCount", last_count=50))

        assert api.ticks.call_args.kwargs["last_cnt"] == 50

    def test_get_ticks_returns_empty_on_exception(self):
        adapter, api, sj_mod, _ = _connected_quote()
        api.ticks.side_effect = RuntimeError("查詢失敗")
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            assert run(adapter.get_ticks("TX")) == []

    def test_get_snapshot(self):
        adapter, api, sj_mod, _ = _connected_quote()
        api.snapshots.return_value = [FakeObj(
            code="TXFR1", ts=1704153600_000_000_000, open=18000.0, high=18100.0,
            low=17900.0, close=18050.0, total_volume=1234,
        )]
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            rows = run(adapter.get_snapshot(["TX"]))

        assert len(rows) == 1
        assert rows[0]["symbol"] == "TX"
        assert rows[0]["close"] == 18050.0
        assert rows[0]["total_volume"] == 1234

    def test_get_contract_info(self):
        adapter, api, sj_mod, contract = _connected_quote()
        contract.code = "TXFR1"
        contract.name = "臺股期貨"
        contract.limit_up = 19000.0
        contract.limit_down = 17000.0
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            info = run(adapter.get_contract_info("TX"))

        assert info["symbol_id"] == "TX"
        assert info["code"] == "TXFR1"
        assert info["limit_up"] == 19000.0

    def test_get_api_usage_computes_pct(self):
        adapter, api, sj_mod, _ = _connected_quote()
        api.usage.return_value = FakeObj(
            connections=2, bytes=500, limit_bytes=1000, remaining_bytes=500,
        )
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            usage = run(adapter.get_api_usage())

        assert usage["used_pct"] == 50.0
        assert usage["remaining_bytes"] == 500


# ─── TradeAdapter — 連線 ─────────────────────────────────────

class TestTradeAdapterConnect:
    def test_not_connected_initially(self):
        adapter = SinoPacTradeAdapter()
        assert adapter.is_connected() is False
        assert adapter._api is None

    def test_connect_success(self):
        adapter, _, _, _ = _connected_trade()
        assert adapter.is_connected() is True

    def test_connect_failure_returns_false(self):
        adapter = SinoPacTradeAdapter()
        sj_mod = MagicMock()
        sj_mod.Shioaji.return_value.login.side_effect = Exception("失敗")
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            assert run(adapter.connect(api_key="BAD", secret_key="BAD")) is False


# ─── TradeAdapter — 下單 / 刪改單 ────────────────────────────

class TestTradeAdapterOrders:
    def _adapter_with_trade(self):
        adapter, api, sj_mod, contract = _connected_trade()
        trade_obj = MagicMock()
        trade_obj.order.id = "ORDER-001"
        api.place_order.return_value = trade_obj
        return adapter, api, sj_mod, trade_obj

    def test_place_order_returns_broker_id(self):
        adapter, api, sj_mod, _ = self._adapter_with_trade()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            order_id = run(adapter.place_order("TX", Direction.BUY, OrderType.MARKET, 1))
        assert order_id == "ORDER-001"

    def test_place_order_caches_trade_object(self):
        """cancel/update 都只吃 Trade 物件，下單當下就要留著。"""
        adapter, api, sj_mod, trade_obj = self._adapter_with_trade()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, 18000))
        assert adapter._trades["ORDER-001"] is trade_obj

    def test_market_order_forces_price_zero(self):
        adapter, api, sj_mod, _ = self._adapter_with_trade()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.place_order("TX", Direction.BUY, OrderType.MARKET, 2, price=18000))
        kwargs = sj_mod.FuturesOrder.call_args.kwargs
        assert kwargs["price"] == 0
        assert kwargs["price_type"] is sj_mod.FuturesPriceType.MKT

    def test_limit_order_keeps_price(self):
        adapter, api, sj_mod, _ = self._adapter_with_trade()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.place_order("TX", Direction.SELL, OrderType.LIMIT, 3, price=18500))
        kwargs = sj_mod.FuturesOrder.call_args.kwargs
        assert kwargs["price"] == 18500
        assert kwargs["quantity"] == 3
        assert kwargs["price_type"] is sj_mod.FuturesPriceType.LMT
        assert kwargs["action"] is sj_mod.Action.Sell

    def test_octype_and_time_in_force(self):
        adapter, api, sj_mod, _ = self._adapter_with_trade()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.place_order(
                "TX", Direction.BUY, OrderType.LIMIT, 1, 18000,
                octype="cover", time_in_force="IOC",
            ))
        kwargs = sj_mod.FuturesOrder.call_args.kwargs
        assert kwargs["octype"] is sj_mod.FuturesOCType.Cover
        assert kwargs["order_type"] is sj_mod.OrderType.IOC

    def test_place_order_returns_empty_on_exception(self):
        adapter, api, sj_mod, _ = self._adapter_with_trade()
        api.place_order.side_effect = RuntimeError("下單失敗")
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            assert run(adapter.place_order("TX", Direction.BUY, OrderType.MARKET, 1)) == ""

    def test_cancel_order_passes_trade_object(self):
        """cancel_order() 要收 Trade 物件，不是委託序號字串。"""
        adapter, api, sj_mod, trade_obj = self._adapter_with_trade()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, 18000))
            assert run(adapter.cancel_order("ORDER-001")) is True
        api.cancel_order.assert_called_once_with(trade_obj)

    def test_cancel_unknown_order_returns_false(self):
        adapter, api, sj_mod, _ = self._adapter_with_trade()
        api.list_trades.return_value = []
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            assert run(adapter.cancel_order("NOT-EXIST")) is False
        api.cancel_order.assert_not_called()

    def test_cancel_order_syncs_when_not_cached(self):
        """重啟後要刪先前掛的單：本地沒有快取就跟券商同步一次再找。"""
        adapter, api, sj_mod, _ = self._adapter_with_trade()
        remote_trade = MagicMock()
        remote_trade.order.id = "OLD-001"
        api.list_trades.return_value = [remote_trade]

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            assert run(adapter.cancel_order("OLD-001")) is True
        api.update_status.assert_called_once()
        api.cancel_order.assert_called_once_with(remote_trade)

    def test_cancel_order_exception_returns_false(self):
        adapter, api, sj_mod, _ = self._adapter_with_trade()
        api.cancel_order.side_effect = RuntimeError("取消失敗")
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, 18000))
            assert run(adapter.cancel_order("ORDER-001")) is False

    def test_modify_price_only(self):
        adapter, api, sj_mod, trade_obj = self._adapter_with_trade()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, 18000))
            assert run(adapter.modify_order("ORDER-001", new_price=18100)) is True
        api.update_order.assert_called_once_with(trade_obj, price=18100)

    def test_modify_qty_only(self):
        adapter, api, sj_mod, trade_obj = self._adapter_with_trade()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.place_order("TX", Direction.BUY, OrderType.LIMIT, 2, 18000))
            assert run(adapter.modify_order("ORDER-001", new_qty=1)) is True
        api.update_order.assert_called_once_with(trade_obj, qty=1)

    def test_modify_without_change_returns_false(self):
        adapter, api, sj_mod, _ = self._adapter_with_trade()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.place_order("TX", Direction.BUY, OrderType.LIMIT, 1, 18000))
            assert run(adapter.modify_order("ORDER-001")) is False
        api.update_order.assert_not_called()


# ─── TradeAdapter — 查詢 ─────────────────────────────────────

def _fake_remote_trade(order_id="A001", code="TXFH6", action="Buy", qty=2,
                       deal_qty=0, cancel_qty=0, status="Submitted", deals=()):
    trade = MagicMock()
    trade.contract = FakeObj(code=code)
    trade.order = FakeObj(id=order_id, action=action, quantity=qty,
                          price=18000.0, price_type="LMT")
    trade.status = FakeObj(id=order_id, status=status, deal_quantity=deal_qty,
                           cancel_quantity=cancel_qty, deals=list(deals))
    return trade


class TestTradeAdapterQueries:
    def test_get_positions_maps_code_to_symbol(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.list_positions.return_value = [
            FakeObj(code="MXFH6", direction="Sell", quantity=3, price=18000.0, last_price=17950.0),
        ]
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            positions = run(adapter.get_positions())

        assert len(positions) == 1
        p = positions[0]
        assert p.symbol == "MTX"          # 轉回系統代碼，每點價值才查得對
        assert p.side is PositionSide.SHORT
        assert p.current_price == 17950.0

    def test_get_positions_returns_empty_on_exception(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.list_positions.side_effect = RuntimeError("查詢失敗")
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            assert run(adapter.get_positions()) == []

    def test_get_open_orders_filters_inactive(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.list_trades.return_value = [
            _fake_remote_trade("A001", status="Submitted"),
            _fake_remote_trade("A002", status="Filled", deal_qty=2),
            _fake_remote_trade("A003", status="Cancelled", cancel_qty=2),
            _fake_remote_trade("A004", status="PartFilled", deal_qty=1),
        ]
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            orders = run(adapter.get_open_orders())

        ids = [o.broker_order_id for o in orders]
        assert ids == ["A001", "A004"]     # 已成交/已取消的不算未成交委託
        assert orders[0].symbol == "TX"
        assert orders[1].status is OrderStatus.PARTIAL

    def test_get_open_orders_computes_avg_fill_price(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.list_trades.return_value = [
            _fake_remote_trade("A001", qty=3, deal_qty=2, status="PartFilled", deals=[
                FakeObj(price=18000.0, quantity=1, seq="1", ts=None),
                FakeObj(price=18010.0, quantity=1, seq="2", ts=None),
            ]),
        ]
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            orders = run(adapter.get_open_orders())

        assert orders[0].avg_fill_price == 18005.0

    def test_get_fills_today(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.list_trades.return_value = [
            _fake_remote_trade("A001", deal_qty=1, status="Filled", deals=[
                FakeObj(price=18000.0, quantity=1, seq="7", ts=None),
            ]),
            _fake_remote_trade("A002", status="Submitted"),  # 沒有 deals，不列入
        ]
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            fills = run(adapter.get_fills_today())

        assert len(fills) == 1
        assert fills[0].symbol == "TX"
        assert fills[0].price == 18000.0
        assert fills[0].broker_fill_id == "7"

    def test_get_orders_today_includes_finished(self):
        """對帳要看得到已成交／已刪的單，才能把本地卡住的委託修正回來。"""
        adapter, api, sj_mod, _ = _connected_trade()
        api.list_trades.return_value = [
            _fake_remote_trade("A001", status="Submitted"),
            _fake_remote_trade("A002", status="Filled", deal_qty=2),
            _fake_remote_trade("A003", status="Cancelled", cancel_qty=2),
        ]
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            orders = run(adapter.get_orders_today())

        assert [o.broker_order_id for o in orders] == ["A001", "A002", "A003"]

    def test_back_to_back_queries_hit_cache(self):
        """對帳會前後腳問成交明細與委託狀態，兩次各打 update_status + list_trades
        等於白花一倍的券商 API。"""
        adapter, api, sj_mod, _ = _connected_trade()
        api.list_trades.return_value = [_fake_remote_trade("A001", status="Submitted")]
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.get_fills_today())
            run(adapter.get_orders_today())

        assert api.list_trades.call_count == 1
        assert api.update_status.call_count == 1

    def test_placing_order_invalidates_cache(self):
        """剛下的單一定要查得到，不能被幾秒前的快取蓋掉。"""
        adapter, api, sj_mod, _ = _connected_trade()
        trade_obj = MagicMock()
        trade_obj.order.id = "NEW-001"
        api.place_order.return_value = trade_obj
        api.list_trades.return_value = [_fake_remote_trade("A001", status="Submitted")]

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.get_orders_today())
            run(adapter.place_order("TX", Direction.BUY, OrderType.MARKET, 1))
            run(adapter.get_orders_today())

        assert api.list_trades.call_count == 2

    def test_get_margin(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.margin.return_value = FakeObj(
            equity=1_000_000, available_margin=800_000,
            initial_margin=200_000, risk_indicator=500.0,
        )
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            margin = run(adapter.get_margin())

        assert margin["equity"] == 1_000_000
        assert margin["available_margin"] == 800_000

    def test_get_margin_returns_empty_on_exception(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.margin.side_effect = RuntimeError("查詢失敗")
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            assert run(adapter.get_margin()) == {}

    def test_get_account_balance_unwraps_list(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.account_balance.return_value = [FakeObj(acc_balance=123456.0, date="2026-08-04")]
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            balance = run(adapter.get_account_balance())
        assert balance["acc_balance"] == 123456.0

    def test_list_accounts_marks_default(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.list_accounts.return_value = [
            FakeObj(account_id="F123456", account_type="Future", broker_id="F00",
                    person_id="A1", username="tester", signed=True),
            FakeObj(account_id="S999", account_type="Stock", broker_id="S00",
                    person_id="A1", username="tester", signed=False),
        ]
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            accounts = run(adapter.list_accounts())

        assert accounts[0]["is_default"] is True    # 與 futopt_account 相同
        assert accounts[1]["is_default"] is False

    def test_get_profit_loss(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.list_profit_loss.return_value = [
            FakeObj(id=11, date="2026-08-04", code="TXFH6", direction="Buy", quantity=1,
                    entry_price=18000.0, cover_price=18050.0, pnl=10000, fee=100, tax=20),
        ]
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            records = run(adapter.get_profit_loss())

        assert records[0]["symbol"] == "TX"
        assert records[0]["pnl"] == 10000
        assert records[0]["id"] == 11

    def test_get_profit_loss_today_delegates(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.list_profit_loss.return_value = []
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            assert run(adapter.get_profit_loss_today()) == []
        begin, end = api.list_profit_loss.call_args.args[1:3]
        assert begin == end     # 今日單日查詢

    def test_get_settlements(self):
        adapter, api, sj_mod, _ = _connected_trade()
        api.list_settlements.return_value = [FakeObj(t_day="2026-08-04", t_money=1000)]
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            rows = run(adapter.get_settlements())
        assert rows[0]["t_money"] == 1000


# ─── TradeAdapter — 回報 callback ────────────────────────────

class TestTradeAdapterCallbacks:
    def _adapter_with_callbacks(self):
        adapter, api, sj_mod, _ = _connected_trade()
        orders, fills = [], []
        adapter.set_on_order_update(orders.append)
        adapter.set_on_fill(fills.append)
        # _setup_callbacks() 把統一入口交給 set_order_callback
        handler = api.set_order_callback.call_args.args[0]
        return adapter, handler, orders, fills

    def test_order_callback_dispatch(self):
        _, handler, orders, fills = self._adapter_with_callbacks()
        handler("FORDER", {
            "operation": {"op_code": "00", "op_msg": ""},
            "order": {"id": "A001", "action": "Buy", "quantity": 2,
                      "price": 18000.0, "price_type": "LMT"},
            "status": {"id": "A001", "status": "Submitted",
                       "deal_quantity": 0, "cancel_quantity": 0},
            "contract": {"code": "TXFH6"},
        })

        assert len(orders) == 1 and not fills
        o = orders[0]
        assert o.symbol == "TX"
        assert o.direction is Direction.BUY
        assert o.order_type is OrderType.LIMIT
        assert o.status is OrderStatus.SUBMITTED

    # OrderState 實際送進 callback 的值（全大寫），不是 "FuturesDeal" 這種好看的名字。
    # 這裡照抄 shioaji 的原值，測試才擋得住「成交被當成委託」這種災難。
    DEAL_STATES = ["FDEAL", "SDEAL"]

    def _deal_msg(self):
        return {
            "trade_id": "A001", "ordno": "AB12345678", "action": "Sell",
            "code": "MXFH6", "price": 18050.0, "quantity": 1, "ts": 1704153600,
        }

    @pytest.mark.parametrize("state", DEAL_STATES)
    def test_deal_callback_dispatch(self, state):
        """成交回報跟委託回報走同一個 callback，用 stat 區分。

        分錯邊的話成交會被當成委託回報解析，欄位全空、整筆成交消失——
        畫面上看不到成交、倉位也不會動。
        """
        _, handler, orders, fills = self._adapter_with_callbacks()
        handler(state, self._deal_msg())

        assert len(fills) == 1 and not orders
        f = fills[0]
        assert f.symbol == "MTX"
        assert f.direction is Direction.SELL
        assert f.qty == 1
        assert f.broker_fill_id == "AB12345678"

    def test_deal_dispatch_uses_real_enum_value(self):
        """直接拿 shioaji 的 OrderState 物件餵進來，確定不是只有字串版本會過。"""
        import shioaji as sj

        _, handler, orders, fills = self._adapter_with_callbacks()
        handler(sj.OrderState.FuturesDeal, self._deal_msg())
        assert len(fills) == 1 and not orders

    def test_unknown_state_falls_back_to_message_shape(self):
        """換版本若連 stat 都認不得，還有訊息結構可以判斷：
        委託回報是巢狀的 order/status，成交回報是平坦的 trade_id/ordno。"""
        _, handler, orders, fills = self._adapter_with_callbacks()
        handler("???", self._deal_msg())
        assert len(fills) == 1 and not orders

    def test_order_report_without_id_is_dropped(self):
        """沒有委託序號的委託回報對不到任何一張單（多半是成交回報跑錯邊），
        丟給上層只會是雜訊。"""
        _, handler, orders, _ = self._adapter_with_callbacks()
        handler("FORDER", {"order": {}, "status": {}, "contract": {}})
        assert orders == []

    def test_rejected_order_marked(self):
        """op_code 非 00 = 委託被券商退回。"""
        _, handler, orders, _ = self._adapter_with_callbacks()
        handler("FORDER", {
            "operation": {"op_code": "99", "op_msg": "價格超出漲跌停"},
            "order": {"id": "A002", "action": "Buy", "quantity": 1, "price": 1.0},
            "status": {"status": "Submitted", "deal_quantity": 0, "cancel_quantity": 0},
            "contract": {"code": "TXFH6"},
        })
        assert orders[0].status is OrderStatus.REJECTED

    def test_status_mapping(self):
        _, handler, orders, _ = self._adapter_with_callbacks()
        cases = [
            ("PendingSubmit", OrderStatus.PENDING),
            ("Submitted", OrderStatus.SUBMITTED),
            ("PartFilled", OrderStatus.PARTIAL),
            ("Filled", OrderStatus.FILLED),
            ("Cancelled", OrderStatus.CANCELLED),
            ("Failed", OrderStatus.REJECTED),
        ]
        for raw, expected in cases:
            handler("FORDER", {
                "order": {"id": "X", "action": "Buy", "quantity": 1, "price": 1.0},
                "status": {"status": raw, "deal_quantity": 0, "cancel_quantity": 0},
                "contract": {"code": "TXFH6"},
            })
        assert [o.status for o in orders] == [e for _, e in cases]

    def test_malformed_message_does_not_raise(self):
        _, handler, orders, fills = self._adapter_with_callbacks()
        handler("FORDER", None)
        handler("FDEAL", {})
        assert orders == [] and fills == []   # 認不出內容就丟掉，重點是不拋例外


# ─── Integration: shioaji 套件可用性 ─────────────────────────

class TestShioajiAvailability:
    def test_shioaji_importable(self):
        try:
            import shioaji
            assert hasattr(shioaji, "Shioaji"), "shioaji 缺少 Shioaji class"
        except ImportError:
            pytest.skip("shioaji 未安裝，跳過此測試 (pip install shioaji)")
