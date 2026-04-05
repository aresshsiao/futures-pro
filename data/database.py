"""
data/database.py — SQLite 資料庫管理
統一儲存歷史K線資料。來源包括期交所CSV和券商API。
"""
from __future__ import annotations
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.models import Bar, Product

logger = logging.getLogger(__name__)

DB_PATH = Path("data/futures.db")

# 已知商品清單 (symbol → (name, exchange, tick_size, multiplier))
KNOWN_PRODUCTS: dict[str, tuple] = {
    "TX":  ("臺股期貨",     "TAIFEX", 1.0, 200),
    "MTX": ("小型臺指期貨", "TAIFEX", 1.0,  50),
    "TMF": ("微型臺指期貨", "TAIFEX", 1.0,  10),
}


class Database:
    """
    SQLite 資料庫

    資料表:
      products — 商品基本資料 (symbol, name, exchange, tick_size, multiplier)
      TX       — 臺股期貨 M1 K線 (timestamp, O, H, L, C, V)
      MTX      — 小型臺指期貨 M1 K線
      TMF      — 微型臺指期貨 M1 K線
    """

    def __init__(self, path: Path = DB_PATH):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate_schema()
        self._create_tables()
        self._seed_products()
        logger.info(f"[Database] 已連線: {self._path}")

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def _migrate_schema(self) -> None:
        """將舊 bars 單表遷移到各商品獨立資料表"""
        tables = {row[0] for row in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        # 刪除舊的 ticks table
        if "ticks" in tables:
            self._conn.execute("DROP TABLE ticks")
            logger.info("[Database] 已刪除 ticks 資料表")

        # 舊的 bars 單表 → 刪除（資料需重新匯入）
        if "bars" in tables:
            self._conn.execute("DROP TABLE bars")
            self._conn.commit()
            logger.info("[Database] 已刪除舊 bars 資料表，需重新匯入資料")

        # 若商品 table 沒有 delivery 欄位 → 刪除重建
        for symbol in KNOWN_PRODUCTS:
            if symbol in tables:
                cols = {row[1] for row in self._conn.execute(f'PRAGMA table_info("{symbol}")').fetchall()}
                if "delivery" not in cols:
                    self._conn.execute(f'DROP TABLE "{symbol}"')
                    logger.info("[Database] 已刪除舊 %s 資料表，需重新匯入資料", symbol)
        self._conn.commit()

    def _bar_table_sql(self, symbol: str) -> str:
        return f"""
            CREATE TABLE IF NOT EXISTS "{symbol}" (
                timestamp INTEGER PRIMARY KEY,
                delivery  TEXT NOT NULL DEFAULT '',
                open      REAL NOT NULL,
                high      REAL NOT NULL,
                low       REAL NOT NULL,
                close     REAL NOT NULL,
                volume    INTEGER NOT NULL
            );
        """

    def _create_tables(self) -> None:
        sql = """
            CREATE TABLE IF NOT EXISTS products (
                symbol      TEXT PRIMARY KEY,
                name        TEXT NOT NULL DEFAULT '',
                exchange    TEXT NOT NULL DEFAULT 'TAIFEX',
                tick_size   REAL NOT NULL DEFAULT 1.0,
                multiplier  INTEGER NOT NULL DEFAULT 200
            );
        """
        for symbol in KNOWN_PRODUCTS:
            sql += self._bar_table_sql(symbol)
        self._conn.executescript(sql)
        self._conn.commit()

    def _seed_products(self) -> None:
        rows = [
            (sym, name, exch, tick, mult)
            for sym, (name, exch, tick, mult) in KNOWN_PRODUCTS.items()
        ]
        self._conn.executemany(
            """INSERT OR IGNORE INTO products (symbol, name, exchange, tick_size, multiplier)
               VALUES (?, ?, ?, ?, ?)""",
            rows,
        )
        self._conn.commit()

    def _ensure_bar_table(self, symbol: str) -> None:
        """動態建立新商品的 bar 資料表（不在 KNOWN_PRODUCTS 內時）"""
        self._conn.executescript(self._bar_table_sql(symbol))
        self._conn.commit()

    # ── Products ──────────────────────────────────────────────

    def upsert_product(self, product: Product) -> None:
        self._conn.execute(
            """INSERT INTO products (symbol, name, exchange, tick_size, multiplier)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 name=excluded.name,
                 exchange=excluded.exchange,
                 tick_size=excluded.tick_size,
                 multiplier=excluded.multiplier""",
            (product.symbol, product.name, product.exchange,
             product.tick_size, product.multiplier),
        )
        self._conn.commit()

    def get_products(self) -> list[Product]:
        rows = self._conn.execute(
            "SELECT symbol, name, exchange, tick_size, multiplier FROM products ORDER BY symbol"
        ).fetchall()
        return [Product(symbol=r[0], name=r[1], exchange=r[2],
                        tick_size=r[3], multiplier=r[4]) for r in rows]

    # ── Bars 寫入 ─────────────────────────────────────────────

    def insert_bars(self, bars: list[Bar]) -> int:
        """批量寫入 M1 K線 (重複時忽略)，回傳實際新增筆數。"""
        # 依 symbol 分組
        by_symbol: dict[str, list] = {}
        for b in bars:
            by_symbol.setdefault(b.symbol, []).append(
                (int(b.timestamp.timestamp()), b.delivery,
                 b.open, b.high, b.low, b.close, b.volume)
            )

        before = self._conn.total_changes
        for symbol, rows in by_symbol.items():
            self._ensure_bar_table(symbol)
            self._conn.executemany(
                f"""INSERT OR IGNORE INTO "{symbol}"
                    (timestamp, delivery, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        self._conn.commit()
        return self._conn.total_changes - before

    # ── Bars 查詢 ─────────────────────────────────────────────

    def get_bars(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 500,
    ) -> list[Bar]:
        """查詢 M1 K線"""
        from core.models import Timeframe
        query = f'SELECT timestamp, delivery, open, high, low, close, volume FROM "{symbol}" WHERE 1=1'
        params: list = []

        if start:
            query += " AND timestamp >= ?"
            params.append(int(start.timestamp()))
        if end:
            query += " AND timestamp <= ?"
            params.append(int(end.timestamp()))

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        try:
            rows = self._conn.execute(query, params).fetchall()
        except sqlite3.OperationalError:
            return []

        return [
            Bar(
                symbol=symbol,
                timeframe=Timeframe.M1,
                timestamp=datetime.fromtimestamp(r[0]),
                delivery=r[1],
                open=r[2], high=r[3], low=r[4], close=r[5], volume=r[6],
                is_closed=True,
            )
            for r in reversed(rows)
        ]

    def get_bar_count(self, symbol: str) -> int:
        try:
            row = self._conn.execute(f'SELECT COUNT(*) FROM "{symbol}"').fetchone()
            return row[0] if row else 0
        except sqlite3.OperationalError:
            return 0

    def get_date_range(self, symbol: str) -> tuple[str, str]:
        """回傳 (最早日期, 最新日期) ISO 字串"""
        try:
            row = self._conn.execute(
                f'SELECT MIN(timestamp), MAX(timestamp) FROM "{symbol}"'
            ).fetchone()
        except sqlite3.OperationalError:
            return ("", "")
        def _fmt(ts):
            return datetime.fromtimestamp(ts).isoformat() if ts else ""
        return (_fmt(row[0]), _fmt(row[1]))

    # ── 統計 ──────────────────────────────────────────────────

    def summary(self) -> list[dict]:
        """回傳每個商品的統計"""
        products = {p.symbol: p.name for p in self.get_products()}
        results = []
        for symbol, name in sorted(products.items()):
            count = self.get_bar_count(symbol)
            start, end = self.get_date_range(symbol)
            results.append({
                "symbol": symbol,
                "name": name,
                "count": count,
                "start": start,
                "end": end,
            })
        return results
