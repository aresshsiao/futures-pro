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

    # 商品代碼對照: 期交所中文名 or 英文代碼 → 系統代碼
    # TAIFEX CSV 的「契約」欄有時是中文名，有時直接是英文代碼
    SYMBOL_MAP = {
        # 中文名稱
        "臺股期貨":     "TX",
        "小型臺指期貨": "MTX",
        "微型臺指期貨": "TMF",
        # 英文代碼 (直接對應)
        "TX":  "TX",
        "MTX": "MTX",
        "TMF": "TMF",
    }
    # 支援的商品
    KNOWN_SYMBOLS: dict[str, str] = {
        "TX":  "臺股期貨（大台）",
        "MTX": "小型臺指期貨（小台）",
        "TMF": "微型臺指期貨",
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

                    parsed = self._parse_csv_text(text, source_name=name)
                    bars.extend(parsed)
                    logger.info("[Taifex] 解析 %s/%s → %d 筆", source_name, name, len(parsed))
        except zipfile.BadZipFile as e:
            logger.error("[Taifex] 無效 ZIP (%s): %s", source_name, e)
        return bars

    def _parse_csv_text(self, text: str, source_name: str = "") -> list[Bar]:
        """從 CSV 字串（已解碼）解析 Bar 列表。"""
        bars: list[Bar] = []
        reader = csv.reader(io.StringIO(text))
        header_found = False
        is_tick = False
        
        # 尋找 CSV 標頭
        for row in reader:
            if not row:
                continue
            if any("成交日期" in cell for cell in row):
                header_found = True
                is_tick = True
                logger.debug("[Taifex] 偵測到 Tick 資料表頭: %s", row[:6])
                break
            elif any("交易日期" in cell for cell in row):
                header_found = True
                is_tick = False
                logger.debug("[Taifex] 偵測到 日K線 資料表頭: %s", row[:6])
                break

        if not header_found:
            return []

        # 若是 Tick 資料，呼叫特殊解析與聚合
        if is_tick:
            return self._parse_tick_to_m1_bars(reader, source_name)

        # 否則是 Daily OHLC 資料
        first_data_row_logged = False
        for row in reader:
            if not row:
                continue
            if not first_data_row_logged:
                logger.info("[Taifex] Daily CSV 第一筆資料 (前6欄): %s", row[:6])
                first_data_row_logged = True
            bar = self._parse_row(row)
            if bar:
                bars.append(bar)
        return bars

    def _parse_tick_to_m1_bars(self, reader, source_name: str) -> list[Bar]:
        """
        將 Tick CSV (逐筆明細) 依分鐘 (M1) 聚合為 Bar 列表。
        自動挑選當日成交量最高的主力合約 (近月) 回傳。
        """
        from collections import defaultdict
        
        # vols: symbol -> delivery -> total_volume
        vols = defaultdict(lambda: defaultdict(int))
        # m1_bars: symbol -> delivery -> timestamp -> [open, high, low, close, volume]
        m1_bars = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

        for row in reader:
            if not row or len(row) < 6:
                continue
            
            try:
                date_str = row[0].strip()
                prod_name = row[1].strip()
                delivery = row[2].strip()
                time_str = row[3].strip()
                price_str = row[4].strip()
                qty_str = row[5].strip()

                symbol = self.KNOWN_SYMBOLS.get(prod_name) or self.SYMBOL_MAP.get(prod_name)
                if not symbol:
                    continue  # 跳過不認識的商品

                # 濾除價差單 (例如 202212/202301)
                if "/" in delivery:
                    continue

                price = float(price_str) if price_str and price_str != "-" else 0.0
                if price <= 0:
                    continue
                qty = int(qty_str) if qty_str else 0

                # 截斷秒數變成 M1
                if len(time_str) >= 4:
                    minute_time_str = time_str[:4] + "00"
                else:
                    continue
                
                dt = datetime.strptime(date_str + minute_time_str, "%Y%m%d%H%M%S")

                vols[symbol][delivery] += qty
                b = m1_bars[symbol][delivery][dt]
                if not b:
                    b.extend([price, price, price, price, qty])
                else:
                    b[1] = max(b[1], price)
                    b[2] = min(b[2], price)
                    b[3] = price
                    b[4] += qty

            except (ValueError, IndexError):
                continue

        result_bars: list[Bar] = []
        for symbol, deliveries in m1_bars.items():
            if not deliveries:
                continue
            
            # 依成交量挑選當天的主力合約
            main_delivery = max(deliveries.keys(), key=lambda d: vols[symbol][d])
            main_delivery_bars = deliveries[main_delivery]

            for dt, b in main_delivery_bars.items():
                result_bars.append(Bar(
                    symbol=symbol,
                    timeframe=Timeframe.M1,
                    timestamp=dt,
                    open=b[0],
                    high=b[1],
                    low=b[2],
                    close=b[3],
                    volume=b[4],
                    is_closed=True,
                ))

        logger.info("[Taifex] %s 聚合為 %d 筆 M1 K 線", source_name, len(result_bars))
        return result_bars

    def download_recent(self, symbols: list[str] | None = None) -> list[Bar]:
        """
        從期交所網站下載近 30 個交易日所有 CSV ZIP，
        解析並回傳指定商品的 Bar（ZIP 快取在本地）。
        symbols=None 表示匯入全部支援商品。
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

        return self._filter_symbols(all_bars, symbols)

    # ── 從本地目錄匯入 ────────────────────────────

    def import_directory(
        self,
        directory: str | Path = None,
        symbols: list[str] | None = None,
    ) -> list[Bar]:
        """
        批量匯入整個目錄下的 CSV 和 ZIP 檔案（ZIP 在記憶體中解壓）。
        symbols=None 表示匯入全部支援商品。
        """
        dir_path = Path(directory) if directory else self._raw_dir
        all_bars: list[Bar] = []

        csv_files = sorted(dir_path.glob("*.csv"))
        zip_files = sorted(dir_path.glob("*.zip"))
        logger.info(
            "[Taifex] 掃描目錄: %s → 找到 %d 個 CSV, %d 個 ZIP",
            dir_path, len(csv_files), len(zip_files),
        )

        for f in csv_files:
            all_bars.extend(self.parse_daily_csv(f))

        for f in zip_files:
            all_bars.extend(self._parse_zip_bytes(f.read_bytes(), f.name))

        result = self._filter_symbols(all_bars, symbols)
        logger.info("[Taifex] 匯入完成: 共 %d 筆 K 線 (過濾後 %d 筆)", len(all_bars), len(result))
        return result

    @staticmethod
    def _filter_symbols(bars: list[Bar], symbols: list[str] | None) -> list[Bar]:
        """若 symbols 指定則只保留該商品，否則回傳全部。"""
        if not symbols:
            return bars
        target = set(symbols)
        return [b for b in bars if b.symbol in target]
