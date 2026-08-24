"""
tests/data/test_conditions_db.py — 條件單持久化

條件必須撐過 server 重啟（ARCHITECTURE.md §7.8），所以存 DB 不是存記憶體。
這裡確認欄位進得去也出得來，以及分期長出的新欄位不會把既有條件洗掉。
"""
from datetime import datetime

import pytest

from core.models import Condition, ConditionStatus, Direction
from data.database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "cond.db")
    d.connect()
    yield d
    d.close()


def make(cid="c1", **kw):
    base = dict(
        symbol="TX", side=Direction.SELL, trigger_price=17059.0,
        pullback=10, qty=2, take_profit=30, stop_loss=-10,
        cost_guard=True, trail=False,
    )
    base.update(kw)
    return Condition(id=cid, **base)


class TestRoundTrip:
    def test_saved_condition_comes_back_identical(self, db):
        c = make()
        db.save_condition(c)
        (back,) = db.load_conditions()

        assert back.id == c.id
        assert back.side is Direction.SELL          # enum 不能退化成字串
        assert back.trigger_price == 17059.0
        assert back.pullback == 10 and back.qty == 2
        assert back.take_profit == 30 and back.stop_loss == -10
        assert back.cost_guard is True and back.trail is False
        assert back.status is ConditionStatus.WAITING

    def test_save_is_upsert_not_duplicate(self, db):
        """狀態每變一次就存一次，同一個 id 不該長出第二列。"""
        c = make()
        db.save_condition(c)
        c.status = ConditionStatus.FILLED
        c.entry_price = 17049.0
        c.entry_filled_qty = 2
        db.save_condition(c)

        rows = db.load_conditions()
        assert len(rows) == 1
        assert rows[0].status is ConditionStatus.FILLED
        assert rows[0].entry_price == 17049.0

    def test_exit_fields_persist(self, db):
        c = make(status=ConditionStatus.EXITED, entry_price=17049.0,
                 exit_price=17020.0, exit_reason="take_profit")
        db.save_condition(c)
        (back,) = db.load_conditions()
        assert back.exit_price == 17020.0
        assert back.exit_reason == "take_profit"

    def test_delete(self, db):
        db.save_condition(make("c1"))
        db.save_condition(make("c2"))
        db.delete_condition("c1")
        assert [c.id for c in db.load_conditions()] == ["c2"]

    def test_loaded_in_created_order(self, db):
        old = make("old", )
        old.created_at = datetime(2026, 1, 1, 9, 0)
        new = make("new")
        new.created_at = datetime(2026, 1, 1, 10, 0)
        db.save_condition(new)
        db.save_condition(old)
        assert [c.id for c in db.load_conditions()] == ["old", "new"]


class TestMigration:
    def test_missing_columns_are_added_without_dropping_rows(self, tmp_path):
        """P2 的出場欄位是後來加的 —— 既有條件不能被 drop 掉重來。"""
        path = tmp_path / "old.db"
        db = Database(path)
        db.connect()
        # 模擬 P1 時期的資料表（沒有 exit_price / exit_reason）
        db._conn.execute("DROP TABLE conditions")
        db._conn.execute("""
            CREATE TABLE conditions (
                id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
                trigger_price REAL NOT NULL, chase INTEGER NOT NULL DEFAULT 0,
                qty INTEGER NOT NULL DEFAULT 1, take_profit INTEGER NOT NULL DEFAULT 0,
                stop_loss INTEGER NOT NULL DEFAULT 0, cost_guard INTEGER NOT NULL DEFAULT 0,
                trail INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'waiting',
                entry_order_id TEXT NOT NULL DEFAULT '', entry_price REAL NOT NULL DEFAULT 0,
                entry_filled_qty INTEGER NOT NULL DEFAULT 0, exit_order_id TEXT NOT NULL DEFAULT '',
                peak_price REAL NOT NULL DEFAULT 0, fail_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
        now = datetime.now().isoformat()
        db._conn.execute(
            "INSERT INTO conditions (id,symbol,side,trigger_price,chase,created_at,updated_at)"
            " VALUES ('legacy','TX','sell',17059,10,?,?)", (now, now),
        )
        db._conn.commit()
        db.close()

        # 重新連線 → 走一次 migration
        db2 = Database(path)
        db2.connect()
        rows = db2.load_conditions()
        db2.close()

        assert len(rows) == 1
        assert rows[0].id == "legacy"
        # chase → pullback 只是改名，使用者填的點數要原封不動留著
        assert rows[0].pullback == 10
        assert rows[0].exit_price == 0.0      # 新欄位吃預設值
        assert rows[0].exit_reason == ""
