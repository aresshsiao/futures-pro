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

from core.models import Bar, Timeframe

logger = logging.getLogger(__name__)

DB_PATH = Path("data/futures.db")


class Database:
    """
    SQLite 資料庫

    資料表:
      bars — 各週期K線 (symbol, timeframe, timestamp, O, H, L, C, V)
      ticks — 逐筆成交 (symbol, timestamp, price, volume)
    """

    def __init__(self, path: Path = DB_PATH):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()
        logger.info(f"[Database] 已連線: {self._path}")

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS bars (
                symbol    TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                open      REAL NOT NULL,
                high      REAL NOT NULL,
                low       REAL NOT NULL,
                close     REAL NOT NULL,
                volume    INTEGER NOT NULL,
                PRIMARY KEY (symbol, timeframe, timestamp)
            );

            CREATE TABLE IF NOT EXISTS ticks (
                symbol    TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                price     REAL NOT NULL,
                volume    INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_bars_sym_tf
                ON bars(symbol, timeframe, timestamp);

            CREATE INDEX IF NOT EXISTS idx_ticks_sym
                ON ticks(symbol, timestamp);
        """)
        self._conn.commit()

    # ── 寫入 ──────────────────────────────────────────

    def insert_bars(self, bars: list[Bar]) -> int:
        """批量寫入K線 (重複時忽略)"""
        rows = [
            (b.symbol, b.timeframe.value, b.timestamp.isoformat(),
             b.open, b.high, b.low, b.close, b.volume)
            for b in bars
        ]
        self._conn.executemany(
            """INSERT OR IGNORE INTO bars
               (symbol, timeframe, timestamp, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def insert_ticks(self, symbol: str, ticks: list[tuple[str, float, int]]) -> int:
        """批量寫入 Tick: [(timestamp_iso, price, volume), ...]"""
        rows = [(symbol, ts, price, vol) for ts, price, vol in ticks]
        self._conn.executemany(
            "INSERT INTO ticks (symbol, timestamp, price, volume) VALUES (?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    # ── 查詢 ──────────────────────────────────────────

    def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 500,
    ) -> list[Bar]:
        """查詢K線"""
        query = "SELECT * FROM bars WHERE symbol=? AND timeframe=?"
        params: list = [symbol, timeframe.value]

        if start:
            query += " AND timestamp >= ?"
            params.append(start.isoformat())
        if end:
            query += " AND timestamp <= ?"
            params.append(end.isoformat())

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        bars = [
            Bar(
                symbol=r[0],
                timeframe=Timeframe(r[1]),
                timestamp=datetime.fromisoformat(r[2]),
                open=r[3], high=r[4], low=r[5], close=r[6], volume=r[7],
                is_closed=True,
            )
            for r in reversed(rows)
        ]
        return bars

    def get_bar_count(self, symbol: str, timeframe: Timeframe) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol=? AND timeframe=?",
            (symbol, timeframe.value),
        ).fetchone()
        return row[0] if row else 0

    def get_date_range(self, symbol: str, timeframe: Timeframe) -> tuple[str, str]:
        """回傳 (最早日期, 最新日期)"""
        row = self._conn.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM bars WHERE symbol=? AND timeframe=?",
            (symbol, timeframe.value),
        ).fetchone()
        return (row[0] or "", row[1] or "")

    # ── 統計 ──────────────────────────────────────────

    def summary(self) -> list[dict]:
        """回傳每個 (symbol, timeframe) 的統計"""
        rows = self._conn.execute("""
            SELECT symbol, timeframe, COUNT(*),
                   MIN(timestamp), MAX(timestamp)
            FROM bars
            GROUP BY symbol, timeframe
            ORDER BY symbol, timeframe
        """).fetchall()
        return [
            {
                "symbol": r[0],
                "timeframe": r[1],
                "count": r[2],
                "start": r[3],
                "end": r[4],
            }
            for r in rows
        ]
