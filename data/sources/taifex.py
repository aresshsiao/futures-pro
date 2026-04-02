"""
data/sources/taifex.py — 期交所資料下載與轉換
自動從期交所網站下載最近 30 天每日行情 ZIP → 解壓 CSV → 解析 → 存入 SQLite
"""
from __future__ import annotations
import csv
import io
import logging
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from core.models import Bar, Timeframe

logger = logging.getLogger(__name__)


class TaifexImporter:
    """
    期交所 (TAIFEX) 資料匯入器

    資料來源:
      https://www.taifex.com.tw/cht/3/dlFutPrevious30DaysSalesData
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

    # ── 從期交所網站下載 ───────────────────────────

    LISTING_URL = "https://www.taifex.com.tw/cht/3/dlFutPrevious30DaysSalesData"
    DOWNLOAD_PREFIX = "onClick=\"javascript:window.open('"
    DOWNLOAD_SUFFIX = "')\""
    BASE_URL = "https://www.taifex.com.tw"

    def fetch_download_urls(self) -> list[str]:
        """
        解析期交所「近 30 個交易日每日行情下載」頁面，
        回傳所有 CSV ZIP 的完整下載 URL。
        """
        try:
            resp = requests.get(self.LISTING_URL, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("[Taifex] 無法取得下載頁面: %s", e)
            return []

        urls: list[str] = []
        tmp = resp.text
        while self.DOWNLOAD_PREFIX in tmp:
            pos = tmp.find(self.DOWNLOAD_PREFIX)
            tmp = tmp[pos + len(self.DOWNLOAD_PREFIX):]
            pos = tmp.find(self.DOWNLOAD_SUFFIX)
            candidate = tmp[:pos]
            if "CSV" in candidate.upper():
                full_url = candidate if candidate.startswith("http") else self.BASE_URL + candidate
                urls.append(full_url)
            tmp = tmp[pos + len(self.DOWNLOAD_SUFFIX):]

        logger.info("[Taifex] 找到 %d 個 CSV ZIP 連結", len(urls))
        return urls

    def _get_remote_size(self, url: str) -> int:
        """用 HEAD 請求取得伺服器端檔案大小，取不到回傳 -1。"""
        try:
            resp = requests.head(url, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            return int(resp.headers.get("Content-Length", -1))
        except Exception:
            return -1

    def _zip_cache_path(self, url: str) -> Path:
        """根據 URL 決定本地 ZIP 快取路徑。"""
        filename = url.split("/")[-1].split("?")[0] or "taifex.zip"
        if not filename.upper().endswith(".ZIP"):
            filename += ".zip"
        return self._raw_dir / filename

    def _load_zip_bytes(self, url: str) -> Optional[bytes]:
        """
        取得 ZIP 內容（bytes）：
        - 若本地快取存在且與伺服器大小相同 → 直接讀快取
        - 否則重新下載並更新快取
        """
        cache = self._zip_cache_path(url)
        remote_size = self._get_remote_size(url)

        if cache.exists() and remote_size > 0 and cache.stat().st_size == remote_size:
            logger.info("[Taifex] 快取命中 (大小一致 %d bytes): %s", remote_size, cache.name)
            return cache.read_bytes()

        reason = "首次下載" if not cache.exists() else f"大小不符 (本地 {cache.stat().st_size} / 遠端 {remote_size})"
        logger.info("[Taifex] %s — 下載: %s", reason, url)
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("[Taifex] 下載失敗 %s: %s", url, e)
            return None

        cache.write_bytes(resp.content)
        logger.info("[Taifex] 已快取: %s (%d bytes)", cache.name, len(resp.content))
        return resp.content

    def _parse_zip_bytes(self, zip_bytes: bytes, source_name: str = "") -> list[Bar]:
        """
        在記憶體中解析 ZIP，不寫出任何 CSV 檔案。
        支援 Big5 / UTF-8 編碼自動偵測。
        """
        bars: list[Bar] = []
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for name in zf.namelist():
                    if not name.upper().endswith(".CSV"):
                        continue
                    raw = zf.read(name)
                    # 嘗試 Big5，再試 UTF-8
                    for enc in ("big5", "utf-8-sig", "utf-8"):
                        try:
                            text = raw.decode(enc, errors="strict")
                            break
                        except (UnicodeDecodeError, LookupError):
                            text = None
                    if text is None:
                        text = raw.decode("big5", errors="replace")

                    parsed = self._parse_csv_text(text)
                    bars.extend(parsed)
                    logger.info("[Taifex] 解析 %s/%s → %d 筆", source_name, name, len(parsed))
        except zipfile.BadZipFile as e:
            logger.error("[Taifex] 無效 ZIP (%s): %s", source_name, e)
        return bars

    def _parse_csv_text(self, text: str) -> list[Bar]:
        """從 CSV 字串（已解碼）解析 Bar 列表。"""
        bars: list[Bar] = []
        reader = csv.reader(io.StringIO(text))
        header_found = False
        for row in reader:
            if not row:
                continue
            if not header_found:
                if any("交易日期" in cell for cell in row):
                    header_found = True
                continue
            bar = self._parse_row(row)
            if bar:
                bars.append(bar)
        return bars

    def download_recent(self) -> list[Bar]:
        """
        從期交所網站下載近 30 個交易日所有 CSV ZIP：
        - ZIP 快取在本地（僅在伺服器大小不同時重下）
        - CSV 全在記憶體解析，不寫出磁碟
        """
        urls = self.fetch_download_urls()
        if not urls:
            return []

        all_bars: list[Bar] = []
        for url in urls:
            zip_bytes = self._load_zip_bytes(url)
            if zip_bytes is None:
                continue
            bars = self._parse_zip_bytes(zip_bytes, url.split("/")[-1])
            all_bars.extend(bars)

        logger.info("[Taifex] 下載完成: 共 %d 筆 K 線", len(all_bars))
        return all_bars

    # ── 從本地目錄匯入 ────────────────────────────

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
