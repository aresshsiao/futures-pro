"""
tests/test_broker_sinopac.py — 永豐金 Adapter 測試

因為測試環境沒有真實的 Shioaji 憑證，所有需要連線的測試
都使用 unittest.mock 模擬 shioaji 模組。

涵蓋:
  - 初始狀態 (is_connected = False)
  - connect() 成功路徑 (mock shioaji)
  - connect() 失敗路徑 (login 拋出例外)
  - disconnect() 正常
  - _get_contract 對照表
  - get_history_bars 成功/失敗路徑 (mock)
  - SinoPacTradeAdapter: 初始狀態 / connect / place_order / cancel_order
  - Integration: 確認 shioaji 套件可被匯入（可選）
"""
import asyncio
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from brokers.adapters.sinopac import SinoPacQuoteAdapter, SinoPacTradeAdapter
from core.models import Direction, OrderType, Timeframe


# ─── helpers ─────────────────────────────────────────────────

def run(coro):
    """在同步測試中執行 async 函式。"""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_mock_shioaji():
    """回傳一個模擬 shioaji 模組與 API 實例。"""
    sj_mod = MagicMock()
    api = MagicMock()
    sj_mod.Shioaji.return_value = api

    # 讓 login() 不拋出例外
    api.login.return_value = None
    api.logout.return_value = None

    # 模擬 Contracts.Futures["TXF"]["HOT"]
    contract_mock = MagicMock()
    api.Contracts.Futures.__getitem__.return_value = {"HOT": contract_mock}

    return sj_mod, api, contract_mock


# ─── QuoteAdapter — 初始狀態 ─────────────────────────────────

class TestQuoteAdapterInitialState:
    def test_not_connected_initially(self):
        adapter = SinoPacQuoteAdapter()
        assert adapter.is_connected() is False

    def test_api_is_none_initially(self):
        adapter = SinoPacQuoteAdapter()
        assert adapter._api is None


# ─── QuoteAdapter — connect ───────────────────────────────────

class TestQuoteAdapterConnect:
    def test_connect_success(self):
        adapter = SinoPacQuoteAdapter()
        sj_mod, api, _ = _make_mock_shioaji()

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            result = run(adapter.connect(api_key="KEY", secret_key="SECRET"))

        assert result is True
        assert adapter.is_connected() is True
        api.login.assert_called_once_with(api_key="KEY", secret_key="SECRET")

    def test_connect_failure_returns_false(self):
        adapter = SinoPacQuoteAdapter()
        sj_mod = MagicMock()
        sj_mod.Shioaji.return_value.login.side_effect = RuntimeError("登入失敗")

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            result = run(adapter.connect(api_key="BAD", secret_key="BAD"))

        assert result is False
        assert adapter.is_connected() is False

    def test_connect_sets_api_instance(self):
        adapter = SinoPacQuoteAdapter()
        sj_mod, api, _ = _make_mock_shioaji()

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.connect(api_key="K", secret_key="S"))

        assert adapter._api is api


# ─── QuoteAdapter — disconnect ───────────────────────────────

class TestQuoteAdapterDisconnect:
    def test_disconnect_after_connect(self):
        adapter = SinoPacQuoteAdapter()
        sj_mod, api, _ = _make_mock_shioaji()

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.connect(api_key="K", secret_key="S"))
            run(adapter.disconnect())

        assert adapter.is_connected() is False
        api.logout.assert_called_once()

    def test_disconnect_when_not_connected_does_not_raise(self):
        adapter = SinoPacQuoteAdapter()
        run(adapter.disconnect())  # _api is None, should not raise


# ─── QuoteAdapter — _get_contract ────────────────────────────

class TestGetContract:
    def test_tx_maps_to_txf(self):
        adapter = SinoPacQuoteAdapter()
        sj_mod, api, contract = _make_mock_shioaji()

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.connect(api_key="K", secret_key="S"))

        result = adapter._get_contract("TX")
        api.Contracts.Futures.__getitem__.assert_called_with("TXF")
        assert result == contract

    def test_mtx_maps_to_mxf(self):
        adapter = SinoPacQuoteAdapter()
        sj_mod, api, _ = _make_mock_shioaji()

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.connect(api_key="K", secret_key="S"))

        adapter._get_contract("MTX")
        api.Contracts.Futures.__getitem__.assert_called_with("MXF")

    def test_unknown_symbol_returns_none(self):
        adapter = SinoPacQuoteAdapter()
        sj_mod, api, _ = _make_mock_shioaji()
        api.Contracts.Futures.__getitem__.side_effect = KeyError("XXX")

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.connect(api_key="K", secret_key="S"))

        result = adapter._get_contract("XXX")
        assert result is None


# ─── QuoteAdapter — get_history_bars ─────────────────────────

class TestGetHistoryBars:
    def test_returns_empty_when_not_connected(self):
        adapter = SinoPacQuoteAdapter()
        bars = run(adapter.get_history_bars("TX", Timeframe.D1))
        assert bars == []

    def test_returns_bars_with_mock_api(self):
        adapter = SinoPacQuoteAdapter()
        sj_mod, api, contract = _make_mock_shioaji()

        # 模擬 kbars 回傳
        kbars = MagicMock()
        kbars.ts = [1704153600_000_000_000]  # 2024-01-02 in ns
        kbars.Open = [18000.0]
        kbars.High = [18100.0]
        kbars.Low = [17900.0]
        kbars.Close = [18050.0]
        kbars.Volume = [50000]
        api.kbars.return_value = kbars

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.connect(api_key="K", secret_key="S"))
            bars = run(adapter.get_history_bars("TX", Timeframe.D1, count=1))

        assert len(bars) == 1
        assert bars[0].symbol == "TX"
        assert bars[0].open == 18000.0

    def test_returns_empty_on_api_exception(self):
        adapter = SinoPacQuoteAdapter()
        sj_mod, api, contract = _make_mock_shioaji()
        api.kbars.side_effect = RuntimeError("API 錯誤")

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.connect(api_key="K", secret_key="S"))
            bars = run(adapter.get_history_bars("TX", Timeframe.D1))

        assert bars == []


# ─── TradeAdapter — 初始狀態 ─────────────────────────────────

class TestTradeAdapterInitialState:
    def test_not_connected_initially(self):
        adapter = SinoPacTradeAdapter()
        assert adapter.is_connected() is False

    def test_api_is_none_initially(self):
        adapter = SinoPacTradeAdapter()
        assert adapter._api is None


# ─── TradeAdapter — connect ───────────────────────────────────

class TestTradeAdapterConnect:
    def test_connect_success(self):
        adapter = SinoPacTradeAdapter()
        sj_mod, api, _ = _make_mock_shioaji()

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            result = run(adapter.connect(api_key="KEY", secret_key="SECRET"))

        assert result is True
        assert adapter.is_connected() is True

    def test_connect_with_cert_calls_activate_ca(self):
        adapter = SinoPacTradeAdapter()
        sj_mod, api, _ = _make_mock_shioaji()
        api.activate_ca.return_value = None

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.connect(
                api_key="K",
                secret_key="S",
                cert_path="/path/cert.pfx",
                cert_password="pw",
                person_id="A123456789",
            ))

        api.activate_ca.assert_called_once()

    def test_connect_failure_returns_false(self):
        adapter = SinoPacTradeAdapter()
        sj_mod = MagicMock()
        sj_mod.Shioaji.return_value.login.side_effect = Exception("失敗")

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            result = run(adapter.connect(api_key="BAD", secret_key="BAD"))

        assert result is False


# ─── TradeAdapter — place_order / cancel_order ───────────────

class TestTradeAdapterOrders:
    def _connected_adapter(self):
        adapter = SinoPacTradeAdapter()
        sj_mod, api, contract = _make_mock_shioaji()

        trade_obj = MagicMock()
        trade_obj.order.id = "ORDER-001"
        api.place_order.return_value = trade_obj
        api.cancel_order.return_value = None

        # shioaji enums
        sj_mod.Action.Buy = "Buy"
        sj_mod.Action.Sell = "Sell"
        sj_mod.FuturesPriceType.MKT = "MKT"
        sj_mod.FuturesPriceType.LMT = "LMT"
        sj_mod.FuturesOrder = MagicMock(return_value=MagicMock())
        sj_mod.OrderType.ROD = "ROD"

        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            run(adapter.connect(api_key="K", secret_key="S"))

        return adapter, api, sj_mod

    def test_place_order_returns_broker_id(self):
        adapter, api, sj_mod = self._connected_adapter()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            order_id = run(adapter.place_order("TX", Direction.BUY, OrderType.MARKET, 1))
        assert order_id == "ORDER-001"

    def test_cancel_order_returns_true(self):
        adapter, api, sj_mod = self._connected_adapter()
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            result = run(adapter.cancel_order("ORDER-001"))
        assert result is True

    def test_cancel_order_exception_returns_false(self):
        adapter, api, sj_mod = self._connected_adapter()
        api.cancel_order.side_effect = RuntimeError("取消失敗")
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            result = run(adapter.cancel_order("BAD-ID"))
        assert result is False

    def test_get_positions_returns_list(self):
        adapter, api, sj_mod = self._connected_adapter()
        api.list_positions.return_value = []
        with patch.dict(sys.modules, {"shioaji": sj_mod}):
            positions = run(adapter.get_positions())
        assert positions == []


# ─── Integration: shioaji 套件可用性 ─────────────────────────

class TestShioajiAvailability:
    def test_shioaji_importable(self):
        """若安裝了 shioaji，確認版本資訊可取得。"""
        try:
            import shioaji
            assert hasattr(shioaji, "Shioaji"), "shioaji 缺少 Shioaji class"
        except ImportError:
            pytest.skip("shioaji 未安裝，跳過此測試 (pip install shioaji)")
