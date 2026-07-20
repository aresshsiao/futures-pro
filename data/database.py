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

from core.models import Bar

logger = logging.getLogger(__name__)

DB_PATH = Path("data/futures.db")

# 已知商品清單 (symbol → 中文名)
KNOWN_PRODUCTS: dict[str, str] = {
    "TX":  "臺股期貨",
    "MTX": "小型臺指期貨",
    "TMF": "微型臺指期貨",
}


class Database:
    """
    SQLite 資料庫

    資料表:
      TX  — 臺股期貨 M1 K線 (timestamp, delivery, O, H, L, C, V)
      MTX — 小型臺指期貨 M1 K線
      TMF — 微型臺指期貨 M1 K線
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
        logger.info(f"[Database] 已連線: {self._path}")

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def _migrate_schema(self) -> None:
        tables = {row[0] for row in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        # 刪除舊的廢棄資料表
        for old in ("ticks", "bars", "products"):
            if old in tables:
                self._conn.execute(f"DROP TABLE {old}")
                logger.info("[Database] 已刪除舊資料表: %s", old)

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
        sql = ""
        for symbol in KNOWN_PRODUCTS:
            sql += self._bar_table_sql(symbol)
        sql += """
            CREATE TABLE IF NOT EXISTS trading_calendar (
                trade_date TEXT PRIMARY KEY,  -- YYYY-MM-DD，為期交所公告的交易日
                source     TEXT NOT NULL DEFAULT 'zip'  -- 'zip' | 'manual'
            );
            CREATE TABLE IF NOT EXISTS imported_files (
                filename    TEXT NOT NULL,
                symbol      TEXT NOT NULL,
                file_size   INTEGER NOT NULL,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (filename, symbol)
            );
        """
        self._conn.executescript(sql)
        self._conn.commit()

    def _ensure_bar_table(self, symbol: str) -> None:
        self._conn.executescript(self._bar_table_sql(symbol))
        self._conn.commit()

    # ── Bars 寫入 ─────────────────────────────────────────────

    def insert_bars(self, bars: list[Bar], replace: bool = False) -> int:
        """批量寫入 M1 K線，回傳實際新增/更新筆數。

        replace=False（預設）: 重複的 timestamp 忽略不動，用於券商 API 補資料——
            API 資料只拿來補洞，DB 現有資料視為優先，不會被覆蓋。
        replace=True: 重複的 timestamp 直接覆蓋成新值，用於本地 CSV 匯入——
            期交所官方 CSV 視為最準確的來源，即使 DB 已有同一根棒也要覆蓋更新。
        """
        by_symbol: dict[str, list] = {}
        for b in bars:
            by_symbol.setdefault(b.symbol, []).append(
                (int(b.timestamp.timestamp()), b.delivery,
                 b.open, b.high, b.low, b.close, b.volume)
            )

        verb = "REPLACE" if replace else "IGNORE"
        before = self._conn.total_changes
        for symbol, rows in by_symbol.items():
            self._ensure_bar_table(symbol)
            self._conn.executemany(
                f"""INSERT OR {verb} INTO "{symbol}"
                    (timestamp, delivery, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        self._conn.commit()
        return self._conn.total_changes - before

    # ── 已匯入檔案追蹤 ─────────────────────────────────────────

    def get_imported_files(self, symbols: list[str]) -> dict[str, dict[str, int]]:
        """回傳 {symbol: {filename: file_size}}，用於匯入時判斷檔案是否已處理過（同名同大小視為同一版本）。"""
        result: dict[str, dict[str, int]] = {s: {} for s in symbols}
        if not symbols:
            return result
        placeholders = ",".join("?" * len(symbols))
        rows = self._conn.execute(
            f"SELECT symbol, filename, file_size FROM imported_files WHERE symbol IN ({placeholders})",
            symbols,
        ).fetchall()
        for symbol, filename, file_size in rows:
            result.setdefault(symbol, {})[filename] = file_size
        return result

    def mark_files_imported(self, manifest: list[dict]) -> None:
        """把這次實際匯入（未被跳過）的檔案記錄下來。

        manifest: [{"filename": str, "file_size": int, "symbols": list[str]}, ...]
        """
        now = datetime.now().isoformat()
        rows = [
            (m["filename"], symbol, m["file_size"], now)
            for m in manifest for symbol in m["symbols"]
        ]
        if not rows:
            return
        self._conn.executemany(
            """INSERT OR REPLACE INTO imported_files (filename, symbol, file_size, imported_at)
               VALUES (?, ?, ?, ?)""",
            rows,
        )
        self._conn.commit()

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

    # ── 交易日曆 ───────────────────────────────────────────────

    def build_calendar_from_zip_dir(self, zip_dir: str | Path) -> int:
        """
        掃描目錄下的 Daily_YYYY_MM_DD.zip，把每個檔名代表的交易日
        寫入 trading_calendar 表，回傳新增筆數。
        """
        import re
        pattern = re.compile(r"Daily_(\d{4})_(\d{2})_(\d{2})\.zip", re.IGNORECASE)
        dates: list[str] = []
        for f in Path(zip_dir).glob("*.zip"):
            m = pattern.match(f.name)
            if m:
                dates.append(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")

        if not dates:
            return 0

        before = self._conn.total_changes
        self._conn.executemany(
            "INSERT OR IGNORE INTO trading_calendar (trade_date, source) VALUES (?, 'zip')",
            [(d,) for d in dates],
        )
        self._conn.commit()
        added = self._conn.total_changes - before
        logger.info("[Calendar] 從 ZIP 目錄新增 %d 個交易日", added)
        return added

    def build_calendar_from_twse(self, years: list[int] | None = None) -> int:
        """
        從證交所 API 下載假日清單，推算出交易日，寫入 trading_calendar。
        years=None 時自動從 DB 最早資料年份到當年。
        回傳新增筆數。
        """
        import re
        import requests
        from datetime import date, timedelta

        if years is None:
            # 從 DB 資料判斷需要哪些年份
            rows = self._conn.execute(
                "SELECT MIN(timestamp), MAX(timestamp) FROM TX"
            ).fetchone()
            if not rows or not rows[0]:
                return 0
            start_year = datetime.fromtimestamp(rows[0]).year
            end_year = datetime.fromtimestamp(rows[1]).year
            years = list(range(start_year, end_year + 1))

        # 下載各年假日
        holidays: set[str] = set()
        for year in years:
            url = (
                "https://www.twse.com.tw/rwd/en/holidaySchedule/holidaySchedule"
                f"?response=csv&queryYear={year}"
            )
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                for line in resp.text.splitlines():
                    # 格式: "MM-DD (Weekday),Description"
                    m = re.match(r"(\d{2})-(\d{2})\s*\(", line.strip())
                    if m:
                        holidays.add(f"{year}-{m.group(1)}-{m.group(2)}")
                logger.info("[Calendar] TWSE %d 年假日下載完成 (%d 天)", year, len([h for h in holidays if h.startswith(str(year))]))
            except Exception as e:
                logger.warning("[Calendar] TWSE %d 年假日下載失敗: %s", year, e)

        # 產生交易日：週一到週五 且 不在假日清單內
        all_dates: list[str] = []
        for year in years:
            d = date(year, 1, 1)
            end = date(year, 12, 31)
            while d <= end:
                if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in holidays:
                    all_dates.append(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)

        if not all_dates:
            return 0

        before = self._conn.total_changes
        self._conn.executemany(
            "INSERT OR IGNORE INTO trading_calendar (trade_date, source) VALUES (?, 'twse')",
            [(d,) for d in all_dates],
        )
        self._conn.commit()
        added = self._conn.total_changes - before
        logger.info("[Calendar] 從 TWSE 新增 %d 個交易日（共查詢 %d 年）", added, len(years))
        return added

    def get_trading_dates(self) -> list[str]:
        """回傳所有已知交易日（升冪排列）。"""
        rows = self._conn.execute(
            "SELECT trade_date FROM trading_calendar ORDER BY trade_date"
        ).fetchall()
        return [r[0] for r in rows]

    def get_session_map(self) -> dict[int, str]:
        """
        根據交易日曆建立 timestamp → trade_date 的快速查找結構。

        規則：交易日 T 的 session 涵蓋
          前一個交易日 15:00 ～ 當天 13:44（含）

        回傳：{session_start_unix: trade_date_str, ...}
        供 _aggregate_bars 使用的有序 list[(session_start_unix, trade_date)]。
        """
        from datetime import timedelta
        dates = self.get_trading_dates()
        if not dates:
            return []

        sessions: list[tuple[int, str]] = []
        for i, trade_date in enumerate(dates):
            # session 開始 = 前一個交易日 15:00
            if i == 0:
                # 第一個交易日：session 從當天 00:00 開始（保守處理）
                start_dt = datetime.strptime(trade_date, "%Y-%m-%d")
            else:
                prev_date = dates[i - 1]
                start_dt = datetime.strptime(prev_date, "%Y-%m-%d").replace(hour=15)
            sessions.append((int(start_dt.timestamp()), trade_date))

        return sessions

    def bar_to_trade_date(self, ts: int, sessions: list[tuple[int, str]]) -> str:
        """
        給定一個 M1 bar 的 unix timestamp，用二分搜尋找出對應的交易日。
        sessions 必須是 get_session_map() 的回傳值（已升冪排列）。
        """
        import bisect
        keys = [s[0] for s in sessions]
        idx = bisect.bisect_right(keys, ts) - 1
        if idx < 0:
            # 早於第一個已知 session，用日曆日當 fallback
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        return sessions[idx][1]

    # ── 統計 ──────────────────────────────────────────────────

    def summary(self) -> list[dict]:
        """回傳每個商品的統計"""
        results = []
        for symbol, name in sorted(KNOWN_PRODUCTS.items()):
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
