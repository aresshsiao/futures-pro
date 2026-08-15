"""
tests/core/test_fill_ledger.py — 成交明細的新倉/平倉判定與已實現損益

券商的成交回報只有「買 / 賣」，看不出這一筆是進場還是出場；已實現損益要等
結算才查得到。畫面上那兩欄是自己推算的，這裡守住推算的正確性：

  1. 同向加碼是新倉、反向是平倉，反手要能同時算出平掉的損益與新的成本。
  2. 損益要乘上該商品的每點價值（台指 200、小台 50），不是點數。
  3. 留倉單的成本不在今日成交裡 —— 標成平倉、損益留空，不能亂編一個數字。
  4. 逐筆餵（成交回報）與整份重播（對帳）要得到同樣的結果。
"""
from datetime import datetime, timedelta

from core.fill_ledger import FillLedger
from core.models import Direction, Fill, Position, PositionSide


BASE = datetime(2026, 8, 12, 9, 0, 0)


def fill(direction=Direction.BUY, price=18000.0, qty=1, symbol="TX", minute=0):
    return Fill(order_id="B001", symbol=symbol, direction=direction, price=price,
                qty=qty, fee=0.0, timestamp=BASE + timedelta(minutes=minute))


class TestOpenClose:
    def test_first_fill_is_new(self):
        f = FillLedger().apply(fill())
        assert f.oc_type == "new"
        assert f.closed_qty == 0
        assert f.pnl is None

    def test_same_side_is_new(self):
        ledger = FillLedger()
        ledger.apply(fill(qty=1))
        f = ledger.apply(fill(qty=2))
        assert f.oc_type == "new"
        assert f.closed_qty == 0

    def test_opposite_side_is_cover(self):
        ledger = FillLedger()
        ledger.apply(fill(Direction.BUY, 18000.0, 2))
        f = ledger.apply(fill(Direction.SELL, 18010.0, 2))
        assert f.oc_type == "cover"
        assert f.closed_qty == 2

    def test_partial_cover_leaves_position_open(self):
        ledger = FillLedger()
        ledger.apply(fill(Direction.BUY, 18000.0, 3))
        f = ledger.apply(fill(Direction.SELL, 18010.0, 1))
        assert f.oc_type == "cover"
        assert f.closed_qty == 1
        # 剩下 2 口還在，再賣 2 口仍是平倉而不是新倉
        assert ledger.apply(fill(Direction.SELL, 18020.0, 2)).oc_type == "cover"

    def test_reversal_covers_then_opens(self):
        """一張單同時平掉舊部位又建立反向部位：兩件事都要記到。"""
        ledger = FillLedger()
        ledger.apply(fill(Direction.BUY, 18000.0, 1))
        f = ledger.apply(fill(Direction.SELL, 18100.0, 3))
        assert f.oc_type == "cover_new"
        assert f.closed_qty == 1
        assert f.pnl == 100 * 200          # 只算平掉的那 1 口
        # 反手後成本是這筆成交價，接著回補 2 口才不會算出錯誤損益
        back = ledger.apply(fill(Direction.BUY, 18050.0, 2))
        assert back.pnl == 50 * 2 * 200

    def test_each_symbol_tracked_separately(self):
        ledger = FillLedger()
        ledger.apply(fill(Direction.BUY, 18000.0, 1, symbol="TX"))
        f = ledger.apply(fill(Direction.SELL, 500.0, 1, symbol="MTX"))
        assert f.oc_type == "new"          # 小台是另一檔，不是在平台指的多單


class TestRealizedPnl:
    def test_long_profit_uses_point_value(self):
        ledger = FillLedger()
        ledger.apply(fill(Direction.BUY, 18000.0, 1))
        assert ledger.apply(fill(Direction.SELL, 18050.0, 1)).pnl == 50 * 200

    def test_short_profit_is_entry_minus_exit(self):
        ledger = FillLedger()
        ledger.apply(fill(Direction.SELL, 18000.0, 1))
        assert ledger.apply(fill(Direction.BUY, 17950.0, 1)).pnl == 50 * 200

    def test_long_loss_is_negative(self):
        ledger = FillLedger()
        ledger.apply(fill(Direction.BUY, 18000.0, 1))
        assert ledger.apply(fill(Direction.SELL, 17980.0, 1)).pnl == -20 * 200

    def test_uses_symbol_point_value(self):
        """小台一點 50，用台指的 200 去算會把損益放大四倍。"""
        ledger = FillLedger()
        ledger.apply(fill(Direction.BUY, 18000.0, 1, symbol="MTX"))
        assert ledger.apply(fill(Direction.SELL, 18010.0, 1, symbol="MTX")).pnl == 10 * 50

    def test_cover_uses_average_entry(self):
        ledger = FillLedger()
        ledger.apply(fill(Direction.BUY, 18000.0, 1))
        ledger.apply(fill(Direction.BUY, 18100.0, 1))   # 均價 18050
        assert ledger.apply(fill(Direction.SELL, 18150.0, 2)).pnl == 100 * 2 * 200

    def test_average_entry_survives_partial_cover(self):
        """平掉一半不會改變剩下部位的成本，後面那筆損益才算得對。"""
        ledger = FillLedger()
        ledger.apply(fill(Direction.BUY, 18000.0, 2))
        ledger.apply(fill(Direction.SELL, 18100.0, 1))
        assert ledger.apply(fill(Direction.SELL, 18200.0, 1)).pnl == 200 * 200


class TestOvernightPosition:
    def test_seeded_position_makes_first_fill_a_cover(self):
        """沒有這個種子，平留倉單的第一筆會被當成新倉，之後整天判定全部反過來。"""
        ledger = FillLedger()
        ledger.seed({"TX": 2})
        f = ledger.apply(fill(Direction.SELL, 18000.0, 2))
        assert f.oc_type == "cover"
        assert f.closed_qty == 2

    def test_seeded_position_has_no_known_cost(self):
        """昨天的進場價不在今日成交裡，損益留給券商結算，不能自己編。"""
        ledger = FillLedger()
        ledger.seed({"TX": 2})
        assert ledger.apply(fill(Direction.SELL, 18000.0, 2)).pnl is None

    def test_position_after_seeded_cover_has_known_cost(self):
        ledger = FillLedger()
        ledger.seed({"TX": 1})
        ledger.apply(fill(Direction.SELL, 18000.0, 1))    # 平掉留倉單，回到空手
        ledger.apply(fill(Direction.BUY, 18000.0, 1))     # 今日新倉，成本已知
        assert ledger.apply(fill(Direction.SELL, 18030.0, 1)).pnl == 30 * 200

    def test_adding_to_seeded_position_keeps_cost_unknown(self):
        """留倉 + 今日加碼的均價算不出來，寧可留空也不要報一個錯的數字。"""
        ledger = FillLedger()
        ledger.seed({"TX": 1})
        ledger.apply(fill(Direction.BUY, 18000.0, 1))
        assert ledger.apply(fill(Direction.SELL, 18500.0, 2)).pnl is None


class TestOpeningFrom:
    def test_derives_overnight_position(self):
        """券商目前多 3 口，今日只買了 1 口 → 開盤前留倉 2 口。"""
        positions = [Position(symbol="TX", side=PositionSide.LONG, qty=3, avg_price=18000.0)]
        opening = FillLedger.opening_from(positions, [fill(Direction.BUY, 18000.0, 1)])
        assert opening == {"TX": 2}

    def test_flat_after_today_only_trades(self):
        positions = [Position(symbol="TX", side=PositionSide.LONG, qty=1, avg_price=18000.0)]
        opening = FillLedger.opening_from(positions, [fill(Direction.BUY, 18000.0, 1)])
        assert opening == {}

    def test_short_position_is_negative(self):
        positions = [Position(symbol="TX", side=PositionSide.SHORT, qty=2, avg_price=18000.0)]
        assert FillLedger.opening_from(positions, []) == {"TX": -2}

    def test_symbol_closed_out_today(self):
        """今日把留倉單平光：目前沒部位，但開盤前是有的。"""
        fills = [fill(Direction.SELL, 18000.0, 2)]
        assert FillLedger.opening_from([], fills) == {"TX": 2}


class TestReplay:
    def test_replay_sorts_by_time(self):
        """對帳拿回來的成交順序不保證；沒排好會拿還沒建立的部位去算平倉。"""
        later = fill(Direction.SELL, 18100.0, 1, minute=5)
        earlier = fill(Direction.BUY, 18000.0, 1, minute=1)
        rows = FillLedger().replay([later, earlier])
        assert [r.oc_type for r in rows] == ["new", "cover"]
        assert rows[1].pnl == 100 * 200

    def test_replay_matches_incremental(self):
        """逐筆餵（成交回報）與整份重播（對帳）必須得到同一份結果。"""
        made = [
            fill(Direction.BUY, 18000.0, 1, minute=0),
            fill(Direction.BUY, 18100.0, 1, minute=1),
            fill(Direction.SELL, 18200.0, 3, minute=2),
        ]
        live = FillLedger()
        incremental = [(f.oc_type, f.closed_qty, f.pnl) for f in (live.apply(f) for f in made)]

        replayed = FillLedger().replay([
            fill(Direction.BUY, 18000.0, 1, minute=0),
            fill(Direction.BUY, 18100.0, 1, minute=1),
            fill(Direction.SELL, 18200.0, 3, minute=2),
        ])
        assert [(f.oc_type, f.closed_qty, f.pnl) for f in replayed] == incremental

    def test_replay_resets_previous_state(self):
        """重播是整份重算，不能疊在上一輪的部位上。"""
        ledger = FillLedger()
        ledger.apply(fill(Direction.BUY, 18000.0, 5))
        rows = ledger.replay([fill(Direction.SELL, 18000.0, 1)])
        assert rows[0].oc_type == "new"
