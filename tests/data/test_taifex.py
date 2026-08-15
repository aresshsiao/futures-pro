"""
tests/test_taifex.py — TAIFEX 資料解析測試

涵蓋:
  - _parse_number: 各種邊界輸入
  - _parse_row: 正常列 / 缺欄 / 不支援商品 / 零價格
  - _parse_csv_text: 日K線 CSV / Tick CSV
  - _parse_zip_bytes: 合成 ZIP in-memory
  - _parse_tick_to_m1_bars: M1 聚合邏輯
  - import_directory: 掃描本地 raw 目錄（integration）
"""
import io
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import Bar, Timeframe
from data.sources.taifex import TaifexImporter


@pytest.fixture
def importer(tmp_path):
    return TaifexImporter(raw_dir=str(tmp_path / "taifex"))


# ─── _parse_number ───────────────────────────────────────────

class TestParseNumber:
    def test_plain_integer(self):
        assert TaifexImporter._parse_number("18000") == 18000.0

    def test_comma_separated(self):
        assert TaifexImporter._parse_number("18,000") == 18000.0

    def test_float(self):
        assert TaifexImporter._parse_number("18000.5") == 18000.5

    def test_empty_string(self):
        assert TaifexImporter._parse_number("") == 0.0

    def test_dash(self):
        assert TaifexImporter._parse_number("-") == 0.0

    def test_double_dash(self):
        assert TaifexImporter._parse_number("--") == 0.0

    def test_whitespace(self):
        assert TaifexImporter._parse_number("  18000  ") == 18000.0

    def test_non_numeric(self):
        assert TaifexImporter._parse_number("N/A") == 0.0


# ─── _parse_row ──────────────────────────────────────────────

class TestParseRow:

    def _make_row(
        self,
        date="20240102",
        product="TX",
        delivery="202401",
        open="18000",
        high="18100",
        low="17900",
        close="18050",
        extra=None,
    ):
        row = [date, product, delivery, open, high, low, close, "50", "0.27", "50000"]
        if extra:
            row.extend(extra)
        return row

    def test_valid_row_tx(self, importer):
        bar = importer._parse_row(self._make_row())
        assert bar is not None
        assert bar.symbol == "TX"
        assert bar.timeframe == Timeframe.D1
        assert bar.open == 18000.0
        assert bar.high == 18100.0
        assert bar.low == 17900.0
        assert bar.close == 18050.0
        assert bar.volume == 50000
        assert bar.timestamp == datetime(2024, 1, 2)

    def test_valid_row_chinese_name(self, importer):
        bar = importer._parse_row(self._make_row(product="臺股期貨"))
        assert bar is not None
        assert bar.symbol == "TX"

    def test_valid_row_mtx(self, importer):
        bar = importer._parse_row(self._make_row(product="MTX"))
        assert bar is not None
        assert bar.symbol == "MTX"

    def test_unknown_product_returns_none(self, importer):
        bar = importer._parse_row(self._make_row(product="UNKNOWN"))
        assert bar is None

    def test_zero_close_returns_none(self, importer):
        bar = importer._parse_row(self._make_row(close="0"))
        assert bar is None

    def test_invalid_date_returns_none(self, importer):
        bar = importer._parse_row(self._make_row(date="not-a-date"))
        assert bar is None

    def test_too_few_columns_returns_none(self, importer):
        bar = importer._parse_row(["20240102", "TX"])
        assert bar is None

    def test_date_with_slash_format(self, importer):
        row = self._make_row(date="2024/01/02")
        bar = importer._parse_row(row)
        assert bar is not None
        assert bar.timestamp == datetime(2024, 1, 2)


# ─── _parse_csv_text (Daily) ─────────────────────────────────

DAILY_CSV = """\
,,,,,,,,,,,,,
交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價
20240102,TX,202401,18000,18100,17900,18050,50,0.27%,50000,18050,100000,18049,18051
20240103,TX,202401,18050,18200,18000,18150,100,0.55%,48000,18150,99000,18149,18151
20240102,MTX,202401,18000,18100,17900,18050,50,0.27%,12000,18050,50000,18049,18051
"""

class TestParseCsvTextDaily:
    def test_parses_two_tx_bars(self, importer):
        bars = importer._parse_csv_text(DAILY_CSV)
        tx_bars = [b for b in bars if b.symbol == "TX"]
        assert len(tx_bars) == 2

    def test_parses_one_mtx_bar(self, importer):
        bars = importer._parse_csv_text(DAILY_CSV)
        mtx_bars = [b for b in bars if b.symbol == "MTX"]
        assert len(mtx_bars) == 1

    def test_bar_timeframe_is_d1(self, importer):
        bars = importer._parse_csv_text(DAILY_CSV)
        assert all(b.timeframe == Timeframe.D1 for b in bars)

    def test_no_header_returns_empty(self, importer):
        bars = importer._parse_csv_text("無標頭\n資料列\n")
        assert bars == []

    def test_empty_string_returns_empty(self, importer):
        assert importer._parse_csv_text("") == []


# ─── _parse_csv_text (Tick → M1) ─────────────────────────────

TICK_CSV = """\
成交日期,商品代號,到期月份(週別),成交時間,成交價格,成交數量(B+S)
20240102,臺股期貨,202401,090000,18000,2
20240102,臺股期貨,202401,090001,18010,3
20240102,臺股期貨,202401,090001,18020,1
20240102,臺股期貨,202401,090100,18030,4
20240102,小型臺指期貨,202401,090000,18000,5
"""

class TestParseCsvTextTick:
    def test_tick_produces_m1_bars(self, importer):
        bars = importer._parse_csv_text(TICK_CSV)
        assert len(bars) > 0
        assert all(b.timeframe == Timeframe.M1 for b in bars)

    def test_m1_aggregation_ohlcv(self, importer):
        bars = importer._parse_csv_text(TICK_CSV)
        # 09:00 分鐘: 18000(2), 18010(3), 18020(1) → O=18000 H=18020 L=18000 C=18020 V=6
        # 臺股期貨 → SYMBOL_MAP → "TX"
        tx_0900 = next(
            (b for b in bars if b.symbol == "TX" and b.timestamp.hour == 9
             and b.timestamp.minute == 0 and b.timestamp.second == 0),
            None,
        )
        assert tx_0900 is not None
        assert tx_0900.open == 18000
        assert tx_0900.high == 18020
        assert tx_0900.low == 18000
        assert tx_0900.close == 18020
        assert tx_0900.volume == 6

    def test_tick_skips_spread_contracts(self, importer):
        csv = (
            "成交日期,商品代號,到期月份(週別),成交時間,成交價格,成交數量(B+S)\n"
            "20240102,臺股期貨,202401/202402,090000,18000,2\n"
        )
        bars = importer._parse_csv_text(csv)
        assert bars == []


# ─── _parse_zip_bytes ────────────────────────────────────────

class TestParseZipBytes:
    def _make_zip(self, csv_text: str, filename="daily.csv") -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(filename, csv_text.encode("big5", errors="replace"))
        return buf.getvalue()

    def test_parses_csv_inside_zip(self, importer):
        zip_bytes = self._make_zip(DAILY_CSV)
        bars = importer._parse_zip_bytes(zip_bytes, "test.zip")
        assert len(bars) > 0

    def test_bad_zip_returns_empty(self, importer):
        bars = importer._parse_zip_bytes(b"not a zip file", "bad.zip")
        assert bars == []

    def test_zip_with_non_csv_file_ignored(self, importer):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "這不是 CSV")
        bars = importer._parse_zip_bytes(buf.getvalue(), "test.zip")
        assert bars == []


# ─── import_directory ────────────────────────────────────────

class TestImportDirectory:
    def test_empty_dir_returns_empty(self, importer, tmp_path):
        bars, manifest, skipped = importer.import_directory(tmp_path / "empty")
        assert bars == []
        assert manifest == []
        assert skipped == 0

    def test_import_csv_file(self, importer, tmp_path):
        csv_path = tmp_path / "daily.csv"
        csv_path.write_text(DAILY_CSV, encoding="big5", errors="replace")
        bars, manifest, skipped = importer.import_directory(tmp_path)
        assert len(bars) > 0
        assert skipped == 0

    def test_import_csv_file_manifest_records_symbols_and_size(self, importer, tmp_path):
        csv_path = tmp_path / "daily.csv"
        csv_path.write_text(DAILY_CSV, encoding="big5", errors="replace")
        bars, manifest, skipped = importer.import_directory(tmp_path)
        assert len(manifest) == 1
        entry = manifest[0]
        assert entry["filename"] == "daily.csv"
        assert entry["file_size"] == csv_path.stat().st_size
        assert set(entry["symbols"]) == {"TX", "MTX"}

    def test_import_zip_file(self, importer, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("daily.csv", DAILY_CSV.encode("big5", errors="replace"))
        (tmp_path / "daily.zip").write_bytes(buf.getvalue())
        bars, manifest, skipped = importer.import_directory(tmp_path)
        assert len(bars) > 0

    def test_symbol_filter(self, importer, tmp_path):
        csv_path = tmp_path / "daily.csv"
        csv_path.write_text(DAILY_CSV, encoding="big5", errors="replace")
        bars, manifest, skipped = importer.import_directory(tmp_path, symbols=["TX"])
        assert all(b.symbol == "TX" for b in bars)

    def test_already_imported_file_is_skipped(self, importer, tmp_path):
        csv_path = tmp_path / "daily.csv"
        csv_path.write_text(DAILY_CSV, encoding="big5", errors="replace")
        size = csv_path.stat().st_size
        already_imported = {"TX": {"daily.csv": size}, "MTX": {"daily.csv": size}}

        bars, manifest, skipped = importer.import_directory(
            tmp_path, symbols=["TX", "MTX"], already_imported=already_imported,
        )
        assert bars == []
        assert manifest == []
        assert skipped == 1

    def test_partially_imported_file_is_not_skipped(self, importer, tmp_path):
        csv_path = tmp_path / "daily.csv"
        csv_path.write_text(DAILY_CSV, encoding="big5", errors="replace")
        size = csv_path.stat().st_size
        # 只有 TX 已匯入過，這次還要求 MTX → 不能跳過，仍要重新解析整個檔案
        already_imported = {"TX": {"daily.csv": size}, "MTX": {}}

        bars, manifest, skipped = importer.import_directory(
            tmp_path, symbols=["TX", "MTX"], already_imported=already_imported,
        )
        assert len(bars) > 0
        assert skipped == 0

    def test_changed_file_size_is_not_skipped(self, importer, tmp_path):
        csv_path = tmp_path / "daily.csv"
        csv_path.write_text(DAILY_CSV, encoding="big5", errors="replace")
        already_imported = {"TX": {"daily.csv": 1}, "MTX": {"daily.csv": 1}}  # 大小不符

        bars, manifest, skipped = importer.import_directory(
            tmp_path, symbols=["TX", "MTX"], already_imported=already_imported,
        )
        assert len(bars) > 0
        assert skipped == 0


# ─── Integration: 本地 raw 目錄 ──────────────────────────────

class TestImportLocalRaw:
    """
    若 data/raw/taifex 目錄有 ZIP/CSV 檔案，驗證能正常解析。
    沒有檔案時自動跳過。
    """

    RAW_DIR = "data/raw/taifex"

    def test_raw_dir_parses_without_error(self):
        from pathlib import Path
        raw = Path(self.RAW_DIR)
        if not raw.exists() or not list(raw.glob("*.zip")) + list(raw.glob("*.csv")):
            pytest.skip(f"{self.RAW_DIR} 沒有任何 ZIP/CSV 檔案")

        imp = TaifexImporter(raw_dir=self.RAW_DIR)
        bars, manifest, skipped = imp.import_directory()
        assert isinstance(bars, list)
        assert len(bars) > 0, "raw 目錄有檔案但解析出 0 筆"

    def test_raw_dir_bars_have_valid_ohlcv(self):
        from pathlib import Path
        raw = Path(self.RAW_DIR)
        if not raw.exists() or not list(raw.glob("*.zip")) + list(raw.glob("*.csv")):
            pytest.skip(f"{self.RAW_DIR} 沒有任何 ZIP/CSV 檔案")

        imp = TaifexImporter(raw_dir=self.RAW_DIR)
        bars, manifest, skipped = imp.import_directory()
        for b in bars:
            assert b.high >= b.low,   f"H<L: {b}"
            assert b.volume >= 0,     f"負成交量: {b}"
            assert b.open > 0,        f"開盤價 <= 0: {b}"
