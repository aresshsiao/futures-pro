"""
tests/test_database.py — SQLite 資料庫測試

涵蓋:
  - 連線與建表
  - insert_bars / get_bars / get_bar_count
  - get_date_range
  - summary()
  - 重複插入 (INSERT OR IGNORE)
  - 多 timeframe 查詢隔離
  - 實際 futures.db 存在性與資料摘要 (integration)
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

# 直接執行時 (python tests/test_database.py) 也能找到專案模組
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import Bar, Timeframe
from data.database import Database, DB_PATH


# ─── Fixtures ────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    """每個測試使用獨立的暫存 DB，測試結束後自動刪除。"""
    d = Database(path=tmp_path / "test.db")
    d.connect()
    yield d
    d.close()


def _make_bar(
    symbol="TX",
    timeframe=Timeframe.D1,
    date="2024-01-02",
    open=18000.0,
    high=18100.0,
    low=17900.0,
    close=18050.0,
    volume=50000,
) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=datetime.fromisoformat(date),
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        is_closed=True,
    )


# ─── 連線與結構 ───────────────────────────────────────────────

class TestConnection:
    def test_connect_creates_bars_table(self, db):
        conn = sqlite3.connect(str(db._path))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "bars" in tables

    def test_connect_creates_ticks_table(self, db):
        conn = sqlite3.connect(str(db._path))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "ticks" in tables

    def test_close_does_not_raise(self, db):
        db.close()  # 再呼叫一次也不應拋出
        db.close()


# ─── insert_bars ─────────────────────────────────────────────

class TestInsertBars:
    def test_insert_single_bar(self, db):
        bar = _make_bar()
        inserted = db.insert_bars([bar])
        assert inserted == 1

    def test_insert_multiple_bars(self, db):
        bars = [_make_bar(date=f"2024-01-0{i}") for i in range(2, 6)]
        inserted = db.insert_bars(bars)
        assert inserted == 4

    def test_duplicate_insert_ignored(self, db):
        bar = _make_bar()
        db.insert_bars([bar])
        inserted_again = db.insert_bars([bar])
        assert inserted_again == 0

    def test_insert_different_symbols_both_stored(self, db):
        tx = _make_bar(symbol="TX")
        mtx = _make_bar(symbol="MTX")
        db.insert_bars([tx, mtx])
        assert db.get_bar_count("TX", Timeframe.D1) == 1
        assert db.get_bar_count("MTX", Timeframe.D1) == 1


# ─── get_bars ────────────────────────────────────────────────

class TestGetBars:
    def test_get_bars_returns_inserted(self, db):
        bar = _make_bar()
        db.insert_bars([bar])
        result = db.get_bars("TX", Timeframe.D1)
        assert len(result) == 1
        b = result[0]
        assert b.symbol == "TX"
        assert b.open == 18000.0
        assert b.close == 18050.0

    def test_get_bars_empty_when_no_data(self, db):
        result = db.get_bars("TX", Timeframe.D1)
        assert result == []

    def test_get_bars_limit(self, db):
        bars = [_make_bar(date=f"2024-01-{i:02d}") for i in range(2, 22)]  # 20 筆
        db.insert_bars(bars)
        result = db.get_bars("TX", Timeframe.D1, limit=5)
        assert len(result) == 5

    def test_get_bars_start_filter(self, db):
        bars = [_make_bar(date=f"2024-0{m}-01") for m in range(1, 6)]  # Jan~May
        db.insert_bars(bars)
        result = db.get_bars("TX", Timeframe.D1, start=datetime(2024, 3, 1))
        dates = [b.timestamp.month for b in result]
        assert all(m >= 3 for m in dates)

    def test_get_bars_end_filter(self, db):
        bars = [_make_bar(date=f"2024-0{m}-01") for m in range(1, 6)]
        db.insert_bars(bars)
        result = db.get_bars("TX", Timeframe.D1, end=datetime(2024, 3, 31))
        dates = [b.timestamp.month for b in result]
        assert all(m <= 3 for m in dates)

    def test_get_bars_timeframe_isolated(self, db):
        db.insert_bars([_make_bar(timeframe=Timeframe.D1)])
        db.insert_bars([_make_bar(timeframe=Timeframe.M1)])
        assert db.get_bar_count("TX", Timeframe.D1) == 1
        assert db.get_bar_count("TX", Timeframe.M1) == 1
        assert len(db.get_bars("TX", Timeframe.D1)) == 1
        assert len(db.get_bars("TX", Timeframe.M1)) == 1

    def test_get_bars_returned_in_asc_order(self, db):
        bars = [_make_bar(date=f"2024-01-{i:02d}") for i in range(2, 7)]
        db.insert_bars(bars)
        result = db.get_bars("TX", Timeframe.D1, limit=10)
        timestamps = [b.timestamp for b in result]
        assert timestamps == sorted(timestamps)

    def test_get_bars_bar_fields_correct(self, db):
        bar = _make_bar(open=100.0, high=110.0, low=90.0, close=105.0, volume=999)
        db.insert_bars([bar])
        b = db.get_bars("TX", Timeframe.D1)[0]
        assert b.high == 110.0
        assert b.low == 90.0
        assert b.volume == 999
        assert b.is_closed is True


# ─── get_bar_count ───────────────────────────────────────────

class TestGetBarCount:
    def test_count_zero_initially(self, db):
        assert db.get_bar_count("TX", Timeframe.D1) == 0

    def test_count_matches_inserts(self, db):
        bars = [_make_bar(date=f"2024-01-{i:02d}") for i in range(2, 12)]
        db.insert_bars(bars)
        assert db.get_bar_count("TX", Timeframe.D1) == 10


# ─── get_date_range ──────────────────────────────────────────

class TestGetDateRange:
    def test_empty_returns_empty_strings(self, db):
        start, end = db.get_date_range("TX", Timeframe.D1)
        assert start == ""
        assert end == ""

    def test_range_min_max_correct(self, db):
        bars = [_make_bar(date=f"2024-0{m}-01") for m in range(1, 6)]
        db.insert_bars(bars)
        start, end = db.get_date_range("TX", Timeframe.D1)
        assert "2024-01-01" in start
        assert "2024-05-01" in end


# ─── summary ─────────────────────────────────────────────────

class TestSummary:
    def test_summary_empty(self, db):
        assert db.summary() == []

    def test_summary_contains_inserted(self, db):
        bars = [_make_bar(date=f"2024-01-{i:02d}") for i in range(2, 5)]
        db.insert_bars(bars)
        rows = db.summary()
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "TX"
        assert row["timeframe"] == Timeframe.D1.value
        assert row["count"] == 3

    def test_summary_multiple_symbols_and_timeframes(self, db):
        db.insert_bars([_make_bar(symbol="TX", timeframe=Timeframe.D1)])
        db.insert_bars([_make_bar(symbol="TX", timeframe=Timeframe.M1)])
        db.insert_bars([_make_bar(symbol="MTX", timeframe=Timeframe.D1)])
        rows = db.summary()
        keys = {(r["symbol"], r["timeframe"]) for r in rows}
        assert ("TX", "1d") in keys
        assert ("TX", "1m") in keys
        assert ("MTX", "1d") in keys


# ─── Integration: 實際 futures.db ────────────────────────────

class TestActualDB:
    """
    讀取實際的 data/futures.db (若存在)。
    這些測試只做「觀察」，不修改資料。
    """

    def test_db_file_exists(self):
        assert DB_PATH.exists(), f"futures.db 不存在: {DB_PATH}"

    def test_db_has_bars_table(self):
        if not DB_PATH.exists():
            pytest.skip("futures.db 不存在")
        conn = sqlite3.connect(str(DB_PATH))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "bars" in tables

    def test_db_summary_not_empty(self):
        if not DB_PATH.exists():
            pytest.skip("futures.db 不存在")
        db = Database(DB_PATH)
        db.connect()
        rows = db.summary()
        db.close()
        assert len(rows) > 0, "DB 裡沒有任何資料，請先執行匯入"

    def test_db_tx_d1_has_data(self):
        if not DB_PATH.exists():
            pytest.skip("futures.db 不存在")
        db = Database(DB_PATH)
        db.connect()
        count = db.get_bar_count("TX", Timeframe.D1)
        db.close()
        assert count > 0, "TX 日K線資料筆數為 0"

    def test_db_bar_ohlcv_sanity(self):
        """驗證 DB 裡的 OHLCV 數值合理（H >= L, V >= 0）"""
        if not DB_PATH.exists():
            pytest.skip("futures.db 不存在")
        db = Database(DB_PATH)
        db.connect()
        bars = db.get_bars("TX", Timeframe.D1, limit=100)
        db.close()
        for b in bars:
            assert b.high >= b.low,   f"H<L: {b}"
            assert b.high >= b.open,  f"H<O: {b}"
            assert b.high >= b.close, f"H<C: {b}"
            assert b.low  <= b.open,  f"L>O: {b}"
            assert b.low  <= b.close, f"L>C: {b}"
            assert b.volume >= 0,     f"負成交量: {b}"
