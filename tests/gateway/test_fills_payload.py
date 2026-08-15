"""
tests/gateway/test_fills_payload.py — 成交明細送給前端前的損益比對

本地推算的損益（FillLedger）馬上就有，但沒扣手續費、也不知道留倉單的進場成本；
券商的 list_profit_loss 是結算後的權威數字，只是它按「已平倉部位」彙總，
對不回單筆成交。這裡守住兩者合併時的規矩：

  1. 對得到券商紀錄就用券商的數字，並且標明不再是推算值。
  2. 對不到就保留本地推算，畫面不能空著。
  3. 新倉不參與比對 —— 同一個價位可能同時有進場與出場成交。
"""
from datetime import datetime, timedelta

from core.fill_ledger import FillLedger
from core.models import Direction, Fill

from main import _merge_fills_with_pnl


BASE = datetime(2026, 8, 12, 9, 0, 0)


def fill(direction=Direction.BUY, price=18000.0, qty=1, symbol="TX", minute=0):
    return Fill(order_id="B001", symbol=symbol, direction=direction, price=price,
                qty=qty, fee=0.0, timestamp=BASE + timedelta(minutes=minute))


def pnl_record(symbol="TX", cover_price=18050.0, quantity=1, pnl=10000.0, fee=100, tax=20):
    return {"symbol": symbol, "cover_price": cover_price, "quantity": quantity,
            "pnl": pnl, "fee": fee, "tax": tax}


def ledger_rows(fills, records, opening=None):
    """走完整條路徑：先推算，再跟券商紀錄比對。"""
    ordered = FillLedger().replay(fills, opening)
    return _merge_fills_with_pnl(ordered, records)


class TestLocalEstimate:
    def test_cover_without_broker_record_keeps_estimate(self):
        """券商還沒結算完（模擬帳號常常整天都查不到）也要看得到損益。"""
        rows = ledger_rows(
            [fill(Direction.BUY, 18000.0, 1, minute=0),
             fill(Direction.SELL, 18050.0, 1, minute=1)],
            [],
        )
        assert rows[1]["pnl"] == 50 * 200
        assert rows[1]["pnl_estimated"] is True

    def test_open_fill_has_no_pnl(self):
        rows = ledger_rows([fill(Direction.BUY, 18000.0, 1)], [])
        assert rows[0]["oc_type"] == "new"
        assert rows[0]["pnl"] is None
        assert rows[0]["pnl_estimated"] is False

    def test_row_carries_oc_type_and_closed_qty(self):
        rows = ledger_rows(
            [fill(Direction.BUY, 18000.0, 2, minute=0),
             fill(Direction.SELL, 18050.0, 1, minute=1)],
            [],
        )
        assert (rows[0]["oc_type"], rows[0]["closed_qty"]) == ("new", 0)
        assert (rows[1]["oc_type"], rows[1]["closed_qty"]) == ("cover", 1)


class TestBrokerOverride:
    def test_broker_record_replaces_estimate(self):
        """推算不含手續費，券商結算的數字才是最終損益。"""
        rows = ledger_rows(
            [fill(Direction.BUY, 18000.0, 1, minute=0),
             fill(Direction.SELL, 18050.0, 1, minute=1)],
            [pnl_record(cover_price=18050.0, quantity=1, pnl=9880.0)],
        )
        assert rows[1]["pnl"] == 9880.0
        assert rows[1]["pnl_estimated"] is False
        assert rows[1]["realized_fee"] == 100
        assert rows[1]["realized_tax"] == 20

    def test_open_fill_at_same_price_does_not_steal_pnl(self):
        """同價位的進場單若也去比對，出場的損益會被掛到進場那一列上。"""
        rows = ledger_rows(
            [fill(Direction.SELL, 18050.0, 1, minute=0),   # 新倉：放空
             fill(Direction.BUY, 18000.0, 1, minute=1),    # 平倉
             fill(Direction.SELL, 18050.0, 1, minute=2)],  # 新倉：又放空，同一個價位
            [pnl_record(cover_price=18000.0, quantity=1, pnl=9880.0)],
        )
        assert rows[0]["pnl"] is None
        assert rows[1]["pnl"] == 9880.0
        assert rows[2]["pnl"] is None

    def test_record_split_across_fills(self):
        """券商把同一次出場彙總成一筆，我們這邊卻是分批成交。"""
        rows = ledger_rows(
            [fill(Direction.BUY, 18000.0, 2, minute=0),
             fill(Direction.SELL, 18050.0, 1, minute=1),
             fill(Direction.SELL, 18050.0, 1, minute=2)],
            [pnl_record(cover_price=18050.0, quantity=2, pnl=20000.0)],
        )
        assert rows[1]["pnl"] == 10000.0
        assert rows[2]["pnl"] == 10000.0

    def test_fill_spanning_multiple_records(self):
        """一筆成交平掉兩批部位時，兩筆損益紀錄都要算進來。"""
        rows = ledger_rows(
            [fill(Direction.BUY, 18000.0, 1, minute=0),
             fill(Direction.BUY, 18010.0, 1, minute=1),
             fill(Direction.SELL, 18050.0, 2, minute=2)],
            [pnl_record(cover_price=18050.0, quantity=1, pnl=10000.0),
             pnl_record(cover_price=18050.0, quantity=1, pnl=8000.0)],
        )
        assert rows[2]["pnl"] == 18000.0
        assert rows[2]["pnl_estimated"] is False

    def test_other_symbol_record_ignored(self):
        rows = ledger_rows(
            [fill(Direction.BUY, 18000.0, 1, minute=0),
             fill(Direction.SELL, 18050.0, 1, minute=1)],
            [pnl_record(symbol="MTX", cover_price=18050.0, pnl=9880.0)],
        )
        assert rows[1]["pnl"] == 50 * 200        # 沒對到，維持本地推算
        assert rows[1]["pnl_estimated"] is True


class TestOvernightCover:
    def test_broker_supplies_pnl_local_cannot_compute(self):
        """平留倉單時進場成本只有券商知道，本地那欄是空的，要靠比對補上。"""
        rows = ledger_rows(
            [fill(Direction.SELL, 18050.0, 1)],
            [pnl_record(cover_price=18050.0, quantity=1, pnl=15000.0)],
            opening={"TX": 1},
        )
        assert rows[0]["oc_type"] == "cover"
        assert rows[0]["pnl"] == 15000.0
        assert rows[0]["pnl_estimated"] is False

    def test_stays_empty_until_broker_settles(self):
        rows = ledger_rows([fill(Direction.SELL, 18050.0, 1)], [], opening={"TX": 1})
        assert rows[0]["oc_type"] == "cover"
        assert rows[0]["pnl"] is None
