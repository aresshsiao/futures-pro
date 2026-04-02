"""
data/sources/taifex.py — 期交所資料下載與轉換
手動下載期交所每日行情 CSV → 解析 → 轉換 → 寫入 SQLite
"""
from __future__ import annotations
import csv
import io
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.models import Bar, Timeframe

logger = logging.getLogger(__name__)


class TaifexImporter:
    """
    期交所 (TAIFEX) 資料匯入器

    資料來源:
      https://www.taifex.com.tw/cht/3/futDataDown
      日行情 CSV 格式 (Big5 編碼)

    支援匯入:
      - 日K線 (直接從 CSV)
      - 分K線 (從 tick 資料聚合，需另外下載)

    CSV 欄位對照 (期交所日行情):
      交易日期, 契約, 到期月份(週別), 開盤價, 最高價, 最低價,
      收盤價, 漲跌價, 漲跌%, 成交量, 結算價, 未沖銷契約數, ...
    """

    # 商品代碼對照: 期交所全名 → 系統代碼
    SYMBOL_MAP = {
        "臺股期貨": "TX",
        "小型臺指期貨": "MTX",
        "電子期貨": "TE",
        "金融期貨": "TF",
        "臺指選擇權": "TXO",
        "微型臺指期貨": "MXF",
        "股票期貨": None,  # 跳過個股期貨
    }

    def __init__(self, raw_dir: str = "data/raw/taifex"):
        self._raw_dir = Path(raw_dir)
        self._raw_dir.mkdir(parents=True, exist_ok=True)

    def parse_daily_csv(
        self, file_path: str | Path, encoding: str = "big5"
    ) -> list[Bar]:
        """
        解析期交所日行情 CSV 檔案。

        Args:
            file_path: CSV 檔案路徑
            encoding: 編碼 (期交所預設 big5)

        Returns:
            list[Bar]: 解析後的日K線列表
        """
        path = Path(file_path)
        if not path.exists():
            logger.error(f"[Taifex] 檔案不存在: {path}")
            return []

        bars: list[Bar] = []
        with open(path, "r", encoding=encoding, errors="replace") as f:
            reader = csv.reader(f)

            # 跳過標題列 (可能有多行)
            header_found = False
            for row in reader:
                if not row:
                    continue

                # 尋找包含「交易日期」的標題列
                if not header_found:
                    if any("交易日期" in cell for cell in row):
                        header_found = True
                    continue

                # 解析資料列
                bar = self._parse_row(row)
                if bar:
                    bars.append(bar)

        logger.info(f"[Taifex] 解析完成: {path.name} → {len(bars)} 筆")
        return bars

    def _parse_row(self, row: list[str]) -> Optional[Bar]:
        """解析單一 CSV 資料列"""
        try:
            # 清理空白
            row = [cell.strip() for cell in row]

            if len(row) < 10:
                return None

            # 日期: 2024/01/02 或 20240102
            date_str = row[0].replace("/", "")
            if len(date_str) != 8 or not date_str.isdigit():
                return None

            # 商品名稱
            product_name = row[1].strip()
            symbol = self.SYMBOL_MAP.get(product_name)
            if symbol is None:
                return None  # 跳過不支援的商品

            # 到期月份: 取近月 (通常格式如 "202401")
            delivery = row[2].strip()

            # 價格
            open_price = self._parse_number(row[3])
            high_price = self._parse_number(row[4])
            low_price = self._parse_number(row[5])
            close_price = self._parse_number(row[6])
            volume = int(self._parse_number(row[9]))

            if any(v == 0 for v in [open_price, high_price, low_price, close_price]):
                return None

            timestamp = datetime.strptime(date_str, "%Y%m%d")

            return Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                timestamp=timestamp,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                is_closed=True,
            )

        except (ValueError, IndexError):
            return None

    @staticmethod
    def _parse_number(s: str) -> float:
        """解析數字 (處理逗號和空值)"""
        s = s.strip().replace(",", "")
        if not s or s == "-" or s == "--":
            return 0.0
        try:
            return float(s)
        except ValueError:
            return 0.0

    def import_directory(self, directory: str | Path = None) -> list[Bar]:
        """批量匯入整個目錄下的 CSV 檔案"""
        dir_path = Path(directory) if directory else self._raw_dir
        all_bars: list[Bar] = []

        csv_files = sorted(dir_path.glob("*.csv"))
        logger.info(f"[Taifex] 掃描目錄: {dir_path} → 找到 {len(csv_files)} 個 CSV")

        for f in csv_files:
            bars = self.parse_daily_csv(f)
            all_bars.extend(bars)

        logger.info(f"[Taifex] 匯入完成: 共 {len(all_bars)} 筆 K 線")
        return all_bars
