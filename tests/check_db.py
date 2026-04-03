"""
tests/check_db.py — 資料庫診斷腳本

直接執行即可查看 futures.db 的完整狀況：
    python tests/check_db.py
    python tests/check_db.py --symbol TX --timeframe 1d --limit 20
"""
import argparse
import sqlite3
import sys
from pathlib import Path

# 讓專案根目錄可 import
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH
from data.database import Database
from core.models import Timeframe


def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def check_file():
    section("1. DB 檔案狀態")
    if DB_PATH.exists():
        size_kb = DB_PATH.stat().st_size / 1024
        print(f"  [OK] 存在: {DB_PATH}")
        print(f"  大小  : {size_kb:.1f} KB")
    else:
        print(f"  [!!] 不存在: {DB_PATH}")
        print("    → 請先執行資料匯入")
        sys.exit(1)


def check_tables(conn: sqlite3.Connection):
    section("2. 資料表清單")
    rows = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','index') ORDER BY type, name"
    ).fetchall()
    for name, kind in rows:
        print(f"  {kind:8s}  {name}")


def check_summary(db: Database):
    section("3. K 線資料摘要 (symbol / timeframe)")
    rows = db.summary()
    if not rows:
        print("  （無資料）")
        return

    print(f"  {'Symbol':<8} {'Timeframe':<10} {'Count':>8}  {'Start':<24}  {'End':<24}")
    print(f"  {'-'*7} {'-'*9} {'-'*8}  {'-'*23}  {'-'*23}")
    for r in rows:
        print(
            f"  {r['symbol']:<8} {r['timeframe']:<10} {r['count']:>8}  "
            f"{r['start']:<24}  {r['end']:<24}"
        )


def check_sample(db: Database, symbol: str, timeframe: Timeframe, limit: int):
    section(f"4. 最新 {limit} 筆 K 線 ({symbol} / {timeframe.value})")
    bars = db.get_bars(symbol, timeframe, limit=limit)
    if not bars:
        print(f"  （{symbol} {timeframe.value} 無資料）")
        return

    print(f"  {'日期/時間':<24} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'Volume':>10}")
    print(f"  {'-'*23} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*9}")
    for b in bars[-limit:]:
        print(
            f"  {b.timestamp.isoformat():<24} "
            f"{b.open:>8.0f} {b.high:>8.0f} {b.low:>8.0f} {b.close:>8.0f} {b.volume:>10,}"
        )


def check_ohlcv_sanity(db: Database):
    section("5. OHLCV 資料合理性檢查")
    errors = 0
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT symbol, timeframe, timestamp, open, high, low, close, volume FROM bars").fetchall()
    conn.close()

    for sym, tf, ts, o, h, l, c, v in rows:
        if h < l:
            print(f"  [!!] H<L  {sym} {tf} {ts}: H={h} L={l}")
            errors += 1
        if h < o or h < c:
            print(f"  [!!] H<O/C {sym} {tf} {ts}")
            errors += 1
        if l > o or l > c:
            print(f"  [!!] L>O/C {sym} {tf} {ts}")
            errors += 1
        if v < 0:
            print(f"  [!!] 負成交量 {sym} {tf} {ts}: V={v}")
            errors += 1

    if errors == 0:
        print(f"  [OK] 全部 {len(rows):,} 筆資料 OHLCV 合理")
    else:
        print(f"  [!!] 共發現 {errors} 筆異常")


def check_raw_files():
    section("6. 本地 raw ZIP/CSV 檔案")
    from config.settings import RAW_TAIFEX_DIR
    raw = Path(RAW_TAIFEX_DIR)
    if not raw.exists():
        print(f"  （目錄不存在: {raw}）")
        return

    zips = sorted(raw.glob("*.zip"))
    csvs = sorted(raw.glob("*.csv"))
    print(f"  目錄: {raw}")
    print(f"  ZIP : {len(zips)} 個")
    for f in zips:
        print(f"    {f.name}  ({f.stat().st_size/1024:.1f} KB)")
    print(f"  CSV : {len(csvs)} 個")
    for f in csvs:
        print(f"    {f.name}  ({f.stat().st_size/1024:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(description="futures.db 診斷工具")
    parser.add_argument("--symbol", default="TX", help="查詢商品代碼 (預設: TX)")
    parser.add_argument("--timeframe", default="1d",
                        choices=[tf.value for tf in Timeframe],
                        help="K 線週期 (預設: 1d)")
    parser.add_argument("--limit", type=int, default=10,
                        help="顯示最新 N 筆 K 線 (預設: 10)")
    args = parser.parse_args()

    try:
        tf = Timeframe(args.timeframe)
    except ValueError:
        print(f"無效的 timeframe: {args.timeframe}")
        sys.exit(1)

    check_file()

    conn = sqlite3.connect(str(DB_PATH))
    check_tables(conn)
    conn.close()

    db = Database(DB_PATH)
    db.connect()
    check_summary(db)
    check_sample(db, args.symbol, tf, args.limit)
    check_ohlcv_sanity(db)
    db.close()

    check_raw_files()

    print(f"\n{'─' * 60}")
    print("  診斷完成")
    print(f"{'─' * 60}\n")


if __name__ == "__main__":
    main()
