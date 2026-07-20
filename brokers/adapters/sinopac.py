"""
brokers/adapters/sinopac.py — 永豐金 Shioaji Adapter
參考實作，展示如何將券商 API 對接到系統的統一介面。

需安裝: pip install shioaji
文件: https://sinotrade.github.io/
"""
from __future__ import annotations
import logging
import math
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

from core.models import (
    Bar, Direction, Fill, Order, OrderBook, OrderBookLevel,
    OrderType, Position, PositionSide, Tick, Timeframe,
)
from brokers.base import QuoteAdapter, TradeAdapter

logger = logging.getLogger(__name__)

_SHARED_API = None
_SHARED_CONNECTED = False

# login() 的 contracts_cb 會依 SecurityType（"Index"/"Stock"/"Future"/"Option"）逐一回呼，
# 對應該分類的 Contracts 下載完成。這是目前這版 shioaji（1.5.x, Rust 重寫版）唯一
# 實測有效的等待機制——Contracts.status 會提早回報完成、login() 的 contracts_timeout
# 參數也沒有真的擋住，都試過會撲空；但官方 changelog 1.5.1 明確寫著
# "restore login contracts callback compatibility"，所以用回呼來等待。
_CONTRACTS_READY: dict[str, threading.Event] = {
    name: threading.Event() for name in ("Index", "Stock", "Future", "Option")
}


def _on_contracts_fetched(*args):
    """login()/fetch_contracts() 的 contracts_cb；型別上可能不帶參數呼叫，
    也可能帶一個 SecurityType，兩種都要能處理。"""
    security_type = args[0] if args else None
    name = getattr(security_type, "name", None)
    if name in _CONTRACTS_READY:
        _CONTRACTS_READY[name].set()
        logger.info("[SinoPac] Contracts 下載完成: %s", name)
    else:
        # 沒帶參數，或型別不是預期的 SecurityType：保守起見全部標記完成，
        # 避免呼叫端因為等不到特定分類而白白卡滿 timeout。
        for ev in _CONTRACTS_READY.values():
            ev.set()


def _get_shared_api(credentials):
    global _SHARED_API, _SHARED_CONNECTED
    import shioaji as sj
    if _SHARED_API is None:
        _SHARED_API = sj.Shioaji()
    if not _SHARED_CONNECTED:
        for ev in _CONTRACTS_READY.values():
            ev.clear()
        _SHARED_API.login(
            api_key=credentials.get("api_key", ""),
            secret_key=credentials.get("secret_key", ""),
            subscribe_trade=credentials.get("subscribe_trade", True),
            receive_window=10000,
            contracts_cb=_on_contracts_fetched,
        )
        if "cert_path" in credentials:
            _SHARED_API.activate_ca(
                ca_path=credentials["cert_path"],
                ca_passwd=credentials.get("cert_password", ""),
                person_id=credentials.get("person_id", ""),
            )
        _SHARED_CONNECTED = True
    return _SHARED_API


async def _wait_contracts_ready(security_types=("Future", "Index"), timeout: float = 15.0) -> None:
    """等待 login() 的 contracts_cb 回報指定 SecurityType 下載完成（見上方 _CONTRACTS_READY 說明）。"""
    import asyncio
    loop = asyncio.get_running_loop()
    for name in security_types:
        ev = _CONTRACTS_READY.get(name)
        if ev is None or ev.is_set():
            continue
        await loop.run_in_executor(None, ev.wait, timeout)
        if not ev.is_set():
            logger.warning("[SinoPac] 等待 %s 合約下載逾時 (%.0fs)，仍嘗試繼續執行", name, timeout)


def _logout_shared_api():
    global _SHARED_API, _SHARED_CONNECTED
    if _SHARED_API is not None and _SHARED_CONNECTED:
        try:
            _SHARED_API.logout()
        except Exception:
            pass
        _SHARED_CONNECTED = False


# ── 選擇權理論價 (Black-76) ─────────────────────────────
# 台指選擇權標的用期貨價 F 而非現貨指數，Black-76 不需要另外假設股利率。
_RISK_FREE_RATE = 0.015  # 無風險利率假設（年化），台灣短率概估，非即時牌告值


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _black76_price(F: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    """T<=0 或 sigma<=0 時退化為內含價值（到期或無波動率可用）"""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return max(F - K, 0.0) if is_call else max(K - F, 0.0)
    sqrt_t = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc = math.exp(-r * T)
    if is_call:
        return disc * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    return disc * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))


def _implied_vol_black76(target: float, F: float, K: float, T: float, r: float, is_call: bool) -> Optional[float]:
    """二分法反推隱含波動率。選項價會隨 sigma 單調遞增，二分法穩定不用算 vega。"""
    if target <= 0 or T <= 0 or F <= 0 or K <= 0:
        return None
    lo, hi = 1e-4, 5.0  # 年化波動率搜尋範圍：0.01% ~ 500%
    if target <= _black76_price(F, K, T, r, lo, is_call):
        return lo
    if target >= _black76_price(F, K, T, r, hi, is_call):
        return None  # 超出合理範圍（例：嚴重偏離市場的殘留舊報價），放棄反推
    for _ in range(60):
        mid = (lo + hi) / 2
        if _black76_price(F, K, T, r, mid, is_call) > target:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# 台指期貨/選擇權日盤 08:45-13:45、夜盤 15:00-隔日05:00
_DAY_START, _DAY_END = (8, 45), (13, 45)
_NIGHT_START, _NIGHT_END = (15, 0), (5, 0)  # 隔天
_TRADING_MINUTES_PER_DAY = (13 * 60 + 45 - (8 * 60 + 45)) + (24 * 60 - (15 * 60) + 5 * 60)  # 300+840=1140
_TRADING_DAYS_PER_YEAR = 252  # 年交易日數概估，台灣期交所歷年約在此區間
_TRADING_MINUTES_PER_YEAR = _TRADING_MINUTES_PER_DAY * _TRADING_DAYS_PER_YEAR


def _trading_minutes_between(now: datetime, until: datetime, trading_dates: list[str]) -> float:
    """算 now~until 之間「實際會有交易」的分鐘數，扣掉日夜盤中間收盤、週末、假日。

    trading_dates 是已排序的 'YYYY-MM-DD' 交易日清單（來自 db.get_trading_dates()）。
    到期日當天的夜盤不算（該契約最後交易日沒有夜盤），這裡不用特別處理——
    until 通常就設在到期日 13:30，自然會把當天的夜盤區間截掉。
    """
    if until <= now or not trading_dates:
        return 0.0

    # now 若落在前一個交易日的夜盤裡（跨過午夜），要往前多抓一天
    lo = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    hi = until.strftime("%Y-%m-%d")
    relevant = [d for d in trading_dates if lo <= d <= hi]
    if not relevant:
        return 0.0

    total_seconds = 0.0
    for d in relevant:
        day = datetime.strptime(d, "%Y-%m-%d")
        day_start = day.replace(hour=_DAY_START[0], minute=_DAY_START[1])
        day_end = day.replace(hour=_DAY_END[0], minute=_DAY_END[1])
        night_start = day.replace(hour=_NIGHT_START[0], minute=_NIGHT_START[1])
        night_end = (day + timedelta(days=1)).replace(hour=_NIGHT_END[0], minute=_NIGHT_END[1])

        for seg_start, seg_end in ((day_start, day_end), (night_start, night_end)):
            clip_start = max(seg_start, now)
            clip_end = min(seg_end, until)
            if clip_end > clip_start:
                total_seconds += (clip_end - clip_start).total_seconds()

    return total_seconds / 60.0


class SinoPacQuoteAdapter(QuoteAdapter):
    """永豐金 — 問價 Adapter"""

    name = "永豐金"

    def __init__(self):
        self._api = None  # shioaji.Shioaji instance
        self._connected = False
        self._tick_callbacks: dict[str, Callable] = {}
        self._book_callbacks: dict[str, Callable] = {}
        self._subscribed: set[str] = set()  # 已訂閱的 symbol，避免重複呼叫 Shioaji

    async def connect(self, **credentials) -> bool:
        """
        credentials:
            api_key: str
            secret_key: str
        """
        try:
            import shioaji as sj

            self._api = _get_shared_api(credentials)
            await _wait_contracts_ready(("Future", "Index"))

            # 全域 tick callback 在 connect 時設定一次，避免每次 subscribe 覆蓋
            @self._api.on_tick_fop_v1()
            def _on_tick(exchange, tick):
                if tick.simtrade:
                    return  # 過濾模擬成交（收盤後的測試資料）
                symbol = self._code_to_symbol(tick.code)
                if symbol is None:
                    return
                logger.debug("[SinoPac] tick %s price=%.0f vol=%d", symbol, float(tick.close), int(tick.volume))
                cb = self._tick_callbacks.get(symbol)
                if cb is None:
                    return
                t = Tick(
                    symbol=symbol,
                    price=float(tick.close),
                    volume=int(tick.volume),
                    timestamp=tick.datetime,   # 已是 datetime 物件
                    buy_price=float(tick.close),
                    sell_price=float(tick.close),
                )
                cb(t)

            @self._api.on_tick_stk_v1()
            def _on_stk_tick(exchange, tick):
                # 僅處理加權指數（這版 shioaji 代碼是 "001"，不是舊版的 "Y9999"）
                if tick.code != "001":
                    return
                cb = self._tick_callbacks.get("TAIEX")
                if cb is None:
                    return
                logger.debug("[SinoPac] TAIEX price=%.2f chg=%.2f", float(tick.close), float(getattr(tick, "price_chg", 0)))
                t = Tick(
                    symbol="TAIEX",
                    price=float(tick.close),
                    volume=int(tick.volume),
                    timestamp=tick.datetime,
                    change=float(getattr(tick, "price_chg", 0.0)),
                    change_pct=float(getattr(tick, "pct_chg", 0.0)),
                )
                cb(t)

            @self._api.on_bidask_fop_v1()
            def _on_bidask(exchange, bidask):
                symbol = self._code_to_symbol(bidask.code)
                if symbol is None:
                    return
                cb = self._book_callbacks.get(symbol)
                if cb is None:
                    return
                logger.debug("[SinoPac] bidask %s: %s", symbol, bidask)
                try:
                    bids_list = []
                    if hasattr(bidask, "bid_price") and hasattr(bidask, "bid_volume"):
                        for i in range(min(5, len(bidask.bid_price))):
                            bids_list.append(OrderBookLevel(price=float(bidask.bid_price[i]), qty=int(bidask.bid_volume[i])))
                    
                    asks_list = []
                    if hasattr(bidask, "ask_price") and hasattr(bidask, "ask_volume"):
                        for i in range(min(5, len(bidask.ask_price))):
                            asks_list.append(OrderBookLevel(price=float(bidask.ask_price[i]), qty=int(bidask.ask_volume[i])))
                except Exception as e:
                    logger.error("Error parsing bidask: %s", e)
                    bids_list = []
                    asks_list = []

                book = OrderBook(
                    symbol=symbol,
                    timestamp=datetime.now(),
                    bids=bids_list,
                    asks=asks_list,
                )
                cb(book)

            self._connected = True
            logger.info("[SinoPac Quote] 登入成功")
            return True
        except Exception:
            logger.exception("[SinoPac Quote] 登入失敗")
            return False

    async def disconnect(self) -> None:
        _logout_shared_api()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    async def subscribe_tick(self, symbol: str, callback: Callable[[Tick], None]) -> None:
        """訂閱逐筆成交（同一 symbol 只發一次訂閱請求）"""
        self._tick_callbacks[symbol] = callback
        if symbol in self._subscribed:
            return
        contract = await self._get_contract(symbol)
        if contract:
            self._api.quote.subscribe(contract, quote_type="tick", version="v1")
            self._subscribed.add(symbol)
            logger.info("[SinoPac] 已訂閱 tick: %s", symbol)

    async def subscribe_orderbook(self, symbol: str, callback: Callable[[OrderBook], None]) -> None:
        """訂閱五檔（同一 symbol 只發一次訂閱請求）"""
        if symbol == "TAIEX":
            return  # 指數無五檔資料
        self._book_callbacks[symbol] = callback
        contract = await self._get_contract(symbol)
        if contract:
            self._api.quote.subscribe(contract, quote_type="bidask", version="v1")

    async def unsubscribe(self, symbol: str) -> None:
        contract = await self._get_contract(symbol)
        if contract:
            self._api.quote.unsubscribe(contract, quote_type="tick")
            self._api.quote.unsubscribe(contract, quote_type="bidask")
        self._tick_callbacks.pop(symbol, None)
        self._book_callbacks.pop(symbol, None)

    async def get_history_bars(self, symbol: str, timeframe: Timeframe, count: int = 200) -> list[Bar]:
        """
        從永豐金取得歷史K線。

        kbars() 回傳的是 M1 分鐘K（奈秒時間戳），
        在此聚合成目標週期後回傳。
        台指期含夜盤約 1200 分鐘/日，估算所需日曆天數。

        R1 滾動合約 (如 TXFR1) 是 Shioaji 官方用於 kbars 連續歷史查詢的合約形式。
        登入後 kbars() 可能需要數秒初始化，空資料時自動重試。
        """
        import asyncio
        import math
        from datetime import date, timedelta

        contract = await self._get_contract(symbol)
        if not contract:
            return []

        today = date.today()

        TF_MINUTES: dict[Timeframe, int] = {
            Timeframe.M1:  1,
            Timeframe.M5:  5,
            Timeframe.M15: 15,
            Timeframe.M30: 30,
            Timeframe.H1:  60,
            Timeframe.D1:  1440,
        }
        tf_minutes = TF_MINUTES.get(timeframe, 1)
        tf_seconds = tf_minutes * 60

        # 估算需要幾個日曆天（台指期含夜盤約 1200 分鐘/日）
        TRADING_MINUTES_PER_DAY = 1200
        trading_days = max(2, math.ceil(count * tf_minutes / TRADING_MINUTES_PER_DAY))
        calendar_days = int(trading_days * 1.6) + 5

        # 單次 kbars 最多查 90 個日曆天（約 108,000 根 M1），防止 timeout
        calendar_days = min(calendar_days, 90)
        start = (today - timedelta(days=calendar_days)).strftime("%Y-%m-%d")
        # TAIFEX 交易日規則：週五 15:00 ~ 週六 05:00 屬於「週一」的交易日。
        # 若在週末使用 today 作為 end，會導致週五夜盤被過濾掉，因此加上 3 天。
        end = (today + timedelta(days=3)).strftime("%Y-%m-%d")

        try:
            all_ts, all_open, all_high, all_low, all_close, all_vol = [], [], [], [], [], []
            
            curr_end_date = today + timedelta(days=3)
            target_start_date = today - timedelta(days=calendar_days)
            
            while curr_end_date > target_start_date:
                chunk_start_date = max(target_start_date, curr_end_date - timedelta(days=29))
                start_str = chunk_start_date.strftime("%Y-%m-%d")
                end_str = curr_end_date.strftime("%Y-%m-%d")
                
                # 登入後 Shioaji 需要數秒初始化，kbars() 可能立即返回空；最多重試 3 次
                # kbars() 是同步阻塞呼叫，用 run_in_executor 避免凍結 asyncio event loop
                kbars = None
                loop = asyncio.get_running_loop()
                for attempt in range(1, 4):
                    _s, _e = start_str, end_str
                    kbars = await loop.run_in_executor(
                        None, lambda: self._api.kbars(contract=contract, start=_s, end=_e)
                    )
                    if kbars and kbars.ts:
                        break
                    if attempt < 3:
                        await asyncio.sleep(2)
                
                if kbars and kbars.ts:
                    all_ts = list(kbars.ts) + all_ts
                    all_open = list(kbars.Open) + all_open
                    all_high = list(kbars.High) + all_high
                    all_low = list(kbars.Low) + all_low
                    all_close = list(kbars.Close) + all_close
                    all_vol = list(kbars.Volume) + all_vol
                elif attempt == 3 and not all_ts:
                    logger.warning("[SinoPac] kbars 無資料: %s %s %s~%s", symbol, timeframe, start_str, end_str)

                curr_end_date = chunk_start_date - timedelta(days=1)

            if not all_ts:
                return []

            logger.info("[SinoPac] kbars %s 取得 %d 根 M1，start=%s", symbol, len(all_ts), target_start_date.strftime("%Y-%m-%d"))

            # 聚合成目標週期
            buckets: dict[int, list] = {}
            for i in range(len(all_ts)):
                # Shioaji 歷史 K 棒的 ts 欄位，是將台灣時間直接視為 UTC 所算出的 epoch，
                # 這會導致瀏覽器轉換時多加了 8 小時。因此需要將其減去 8 小時 (28800 秒) 
                # 使其成為標準的絕對 UTC epoch。
                ts_sec = int(all_ts[i] / 1e9) - 28800
                aligned = (ts_sec // tf_seconds) * tf_seconds
                o, h, l, c, v = all_open[i], all_high[i], all_low[i], all_close[i], all_vol[i]
                if aligned not in buckets:
                    buckets[aligned] = [o, h, l, c, v]
                else:
                    b = buckets[aligned]
                    b[1] = max(b[1], h)
                    b[2] = min(b[2], l)
                    b[3] = c
                    b[4] += v

            result: list[Bar] = [
                Bar(
                    symbol=symbol, timeframe=timeframe,
                    timestamp=datetime.fromtimestamp(ts),
                    open=b[0], high=b[1], low=b[2], close=b[3], volume=b[4],
                    is_closed=True,
                )
                for ts, b in sorted(buckets.items())
            ]
            logger.info("[SinoPac] 聚合後 %s %s: %d 根", symbol, timeframe.value, len(result))
            return result[-count:]

        except Exception:
            logger.exception("[SinoPac] 取得歷史K線失敗 %s %s", symbol, timeframe)
            return []

    def _lookup_contract(self, symbol: str):
        """單次查詢，不重試、不記 log（給 _get_contract 內部輪詢用）"""
        if symbol == "TAIEX":
            try:
                # 加權指數在這版 shioaji 的代碼是 "001"（symbol "TSE001"），不是舊版的 "Y9999"
                return self._api.Contracts.Indexs.TSE["001"]
            except (KeyError, AttributeError):
                return None
        SYMBOL_MAP = {"TX": "TXF", "MTX": "MXF", "TMF": "TMF"}
        sj_symbol = SYMBOL_MAP.get(symbol, symbol)
        try:
            # Contracts.Futures 用 __getitem__ 直接查完整合約代碼（如 "TXFR1"），
            # 不是先用產品代碼 "TXF" 查一層再查一層——那不是合法的查詢路徑。
            return self._api.Contracts.Futures[sj_symbol + "R1"]  # 近月主力, e.g. TXFR1
        except (KeyError, AttributeError):
            return None

    async def _get_contract(self, symbol: str, attempts: int = 6, delay: float = 0.5):
        """將系統代碼轉換為 Shioaji contract。

        _wait_contracts_ready() 已經是主要的等待機制，這裡的重試只是保險——
        萬一 contracts_cb 沒被觸發（保守 fallback 已經全部標記完成）或有殘餘的極短暫 race，
        查一次撲空就直接放棄的話還是可能撲空。
        """
        import asyncio

        for attempt in range(1, attempts + 1):
            contract = self._lookup_contract(symbol)
            if contract is not None:
                return contract
            if attempt < attempts:
                await asyncio.sleep(delay)

        if symbol == "TAIEX":
            logger.warning("[SinoPac] 找不到加權指數合約 (001)")
        else:
            SYMBOL_MAP = {"TX": "TXF", "MTX": "MXF", "TMF": "TMF"}
            logger.warning(f"[SinoPac] 找不到合約: {symbol} → {SYMBOL_MAP.get(symbol, symbol)}")
        return None

    # Shioaji tick 的 code 欄位格式如 "TXFR1"、"MXFR1"，轉回系統代碼
    _CODE_PREFIX_MAP = {"TXF": "TX", "MXF": "MTX", "TMF": "TMF"}

    def _code_to_symbol(self, code: str) -> str | None:
        """將 Shioaji code（如 TXFR1）轉回系統商品代碼（如 TX）"""
        for prefix, symbol in self._CODE_PREFIX_MAP.items():
            if code.startswith(prefix):
                return symbol
        return None

    # ── 選擇權資料 ────────────────────────────────────

    # 排序鍵：W1<W2<W4<W5（週三）< WF1..WF5（週五）< Z（月選）
    # 週三系列：TX1/TX2/TX4/TX5，同時掛牌2個連續週
    # 週五系列：TXU=F1, TXV=F2, TXX=F3, TXY=F4, TXZ=F5，同時掛牌2個連續週
    # TXO：月選（3連續月+2季月），只保留最近月
    _PROD_SORT_SUFFIX = {
        "TX1": "W1", "TX2": "W2", "TX4": "W4", "TX5": "W5",
        "TXU": "WF1", "TXV": "WF2", "TXX": "WF3", "TXY": "WF4", "TXZ": "WF5",
        "TXO": "Z",
    }

    async def get_options_months(self, _symbol: str = "TXO") -> list[str]:
        """回傳所有 TXO 系列產品的到期月份，格式為 "PRODUCT:delivery_month"。

        目前這版 shioaji（1.5.x Rust 重寫版）的 Contracts.Options 沒有 .keys()
        （ContractCategory 只支援 __iter__/__getitem__/get），__iter__ 給的是各商品的
        ContractGroup，逐一走過 group 內的個別合約，用合約自己的 category 欄位
        （如 TXO/TXW1）取得產品代碼，過濾出台指選擇權系列。
        """
        if not self._api:
            return []
        try:
            await _wait_contracts_ready(("Option",))

            seen: set[str] = set()
            entries: list[str] = []
            for group in self._api.Contracts.Options:
                for c in group:
                    prod = getattr(c, "category", "") or ""
                    if not prod.startswith("TX"):
                        continue
                    dm = getattr(c, "delivery_month", "")
                    if not dm:
                        continue
                    key = f"{prod}:{dm}"
                    if key not in seen:
                        seen.add(key)
                        entries.append(key)

            tx_categories = sorted({k.split(":", 1)[0] for k in entries})
            logger.info("[SinoPac] option categories found: %s", tx_categories)

            def sort_key(k: str):
                prod, dm = k.split(":", 1)
                ym = dm[:6]
                # delivery_month 本身帶週別後綴（如 "202607W1"）直接用；
                # 否則從產品代碼推算（TXW1→W1, TXO→Z 月選排最後）
                suffix = dm[6:] or self._PROD_SORT_SUFFIX.get(prod, "Z")
                return (ym, suffix)

            entries.sort(key=sort_key)

            # TXO 月選只保留最近的那一個月（去掉遠月，避免下拉清單過長）
            txo_entries = [k for k in entries if k.startswith("TXO:")]
            if len(txo_entries) > 1:
                nearest_txo = txo_entries[0]  # 已排序，第一個即最近月
                entries = [k for k in entries if not k.startswith("TXO:") or k == nearest_txo]

            logger.info("[SinoPac] get_options_months: %d entries", len(entries))
            return entries
        except Exception as e:
            logger.error("[SinoPac] get_options_months error: %s", e)
            return []

    async def get_options_t_quote(
        self, symbol: str, month: str, spot_price: float = 0.0, trading_dates: list[str] | None = None,
    ) -> list[dict]:
        """month 格式為 "PRODUCT:delivery_month"，例如 "TXW1:202607" 或 "TXO:202607W1"。
        舊格式（純 delivery_month 字串）仍相容，預設 product 為 TXO。

        spot_price > 0 時，用 Black-76（標的 = 期貨價 F，不需股利率）先從最接近價平的
        履約價反推隱含波動率，再套用同一個波動率算每個履約價的理論價，
        回傳 callPremium/putPremium = 市價 - 理論價。
        trading_dates 是交易日曆（'YYYY-MM-DD' 排序清單），用來把到期時間 T 精算到
        「實際交易分鐘數」（扣掉日夜盤中間收盤、週末、假日），沒給就退化成日曆時間概算。
        """
        if not self._api:
            return []
        try:
            import shioaji as sj

            # 解析複合 key
            if ":" in month:
                prod, dm = month.split(":", 1)
            else:
                prod, dm = "TXO", month

            prod_contracts = getattr(self._api.Contracts.Options, prod, None)
            if prod_contracts is None:
                logger.warning("[SinoPac] options product not found: %s", prod)
                return []

            contracts = [c for c in prod_contracts if c.delivery_month == dm]
            if not contracts:
                logger.warning("[SinoPac] get_options_t_quote: no contracts for %s %s", prod, dm)
                return []

            logger.debug("[SinoPac] get_options_t_quote: %d contracts (%s %s)", len(contracts), prod, dm)
            snapshots = self._api.snapshots(contracts)
            logger.debug("[SinoPac] get_options_t_quote: %d snapshots received", len(snapshots))

            strikes: dict = {}
            call_snap_by_strike: dict = {}
            put_snap_by_strike: dict = {}
            delivery_date_str = None
            for contract, snap in zip(contracts, snapshots):
                s = contract.strike_price
                if delivery_date_str is None:
                    delivery_date_str = contract.delivery_date
                if s not in strikes:
                    strikes[s] = {"strike": s, "callPrice": 0, "callChange": 0, "putPrice": 0, "putChange": 0}
                price = snap.close if snap.close > 0 else 0
                # snapshot.change_price 本身已經是有正負號的漲跌點數（跟 tick.price_chg 慣例一致），不必再另外套 change_type 判斷方向
                change = float(getattr(snap, "change_price", 0.0) or 0.0)
                if contract.option_right == sj.constant.OptionRight.Call:
                    strikes[s]["callPrice"] = price
                    strikes[s]["callChange"] = change
                    if price > 0:
                        call_snap_by_strike[s] = price
                else:
                    strikes[s]["putPrice"] = price
                    strikes[s]["putChange"] = change
                    if price > 0:
                        put_snap_by_strike[s] = price

            if spot_price > 0 and delivery_date_str:
                self._apply_theoretical_premium(
                    strikes, spot_price, delivery_date_str, call_snap_by_strike, put_snap_by_strike, trading_dates,
                )

            return [strikes[s] for s in sorted(strikes.keys())]
        except Exception as e:
            logger.exception("[SinoPac] get_options_t_quote error")
            return []

    def _apply_theoretical_premium(
        self, strikes: dict, F_hint: float, delivery_date_str: str,
        call_snap_by_strike: dict, put_snap_by_strike: dict, trading_dates: list[str] | None,
    ) -> None:
        """就地在 strikes[*] 補上 callPremium/putPremium = 市價 - Black-76 理論價

        F_hint（外部傳入的 TX 期貨現價）只當作「找哪個履約價當 ATM」的參考，
        不直接拿來算理論價——TX 期貨跟 TXO 選擇權本身有各自的基差，兩者不一定同步，
        直接用 TX 價格當 F 會讓 call/put 用同一個 IV 卻算出不一致的理論價（違反 put-call parity）。
        改成優先用「該履約價 call/put 都有成交價」的那組，用 put-call parity 反推選擇權
        自己內含的等效期貨價，理論價才會跟市場的 call/put 相對關係一致。
        """
        try:
            expiry = datetime.strptime(delivery_date_str, "%Y/%m/%d") + timedelta(hours=13, minutes=30)
        except ValueError:
            logger.warning("[SinoPac] 無法解析選擇權到期日: %s", delivery_date_str)
            return

        now = datetime.now()
        if trading_dates:
            minutes_left = _trading_minutes_between(now, expiry, trading_dates)
            minutes_left = max(minutes_left, 1.0)  # 到期前最後一刻至少留 1 分鐘避免除以 0
            T = minutes_left / _TRADING_MINUTES_PER_YEAR
        else:
            # 沒有交易日曆可用時的備援：退化成日曆時間概算（不精確，但至少不會整個掛掉）
            seconds_left = max((expiry - now).total_seconds(), 60)
            T = seconds_left / (365 * 24 * 3600)
        r = _RISK_FREE_RATE

        # 找離 F_hint 最近、且 call/put 都有成交價的履約價，用 put-call parity 反推 F：
        # Call - Put = e^(-rT)(F-K)  =>  F = K + (Call-Put)*e^(rT)
        parity_candidates = sorted(
            (k for k in strikes if k in call_snap_by_strike and k in put_snap_by_strike),
            key=lambda k: abs(k - F_hint),
        )
        if parity_candidates:
            atm_strike = parity_candidates[0]
            F = atm_strike + (call_snap_by_strike[atm_strike] - put_snap_by_strike[atm_strike]) * math.exp(r * T)
        else:
            # 找不到 call/put 都有成交價的履約價（太冷門的月份/週別），退回用外部現價定位 ATM
            atm_strike = min(strikes.keys(), key=lambda k: abs(k - F_hint))
            F = F_hint

        iv = None
        if atm_strike in call_snap_by_strike:
            iv = _implied_vol_black76(call_snap_by_strike[atm_strike], F, atm_strike, T, r, is_call=True)
        if iv is None and atm_strike in put_snap_by_strike:
            iv = _implied_vol_black76(put_snap_by_strike[atm_strike], F, atm_strike, T, r, is_call=False)
        if iv is None:
            logger.debug("[SinoPac] get_options_t_quote: ATM %.0f 反推隱含波動率失敗，略過理論價計算", atm_strike)
            return

        for k, row in strikes.items():
            theo_call = _black76_price(F, k, T, r, iv, is_call=True)
            theo_put = _black76_price(F, k, T, r, iv, is_call=False)
            if row["callPrice"] > 0:
                row["callPremium"] = round(row["callPrice"] - theo_call, 2)
            if row["putPrice"] > 0:
                row["putPremium"] = round(row["putPrice"] - theo_put, 2)


class SinoPacTradeAdapter(TradeAdapter):
    """永豐金 — 交易 Adapter"""

    name = "永豐金"

    def __init__(self):
        self._api = None
        self._connected = False
        self._on_order_cb: Optional[Callable] = None
        self._on_fill_cb: Optional[Callable] = None

    async def connect(self, **credentials) -> bool:
        try:
            import shioaji as sj

            self._api = _get_shared_api(credentials)
            await _wait_contracts_ready(("Future",))

            self._setup_callbacks()
            self._connected = True
            logger.info("[SinoPac Trade] 登入成功 (含憑證)")
            return True
        except Exception:
            logger.exception("[SinoPac Trade] 登入失敗")
            return False

    async def disconnect(self) -> None:
        _logout_shared_api()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    _CODE_PREFIX_MAP = {"TXF": "TX", "MXF": "MTX", "TMF": "TMF"}

    def _code_to_symbol(self, code: str) -> str | None:
        """將 Shioaji code（如 TXFR1）轉回系統商品代碼（如 TX）"""
        for prefix, symbol in self._CODE_PREFIX_MAP.items():
            if code.startswith(prefix):
                return symbol
        return None

    def _setup_callbacks(self):
        def on_order(stat, msg):
            if not self._on_order_cb:
                return
            try:
                from core.models import Order, OrderStatus, Direction, OrderType
                order_dict = msg.get('order', {})
                status_dict = msg.get('status', {})
                contract_dict = msg.get('contract', {})
                
                broker_id = order_dict.get('id', '')
                code = contract_dict.get('code', '')
                symbol = self._code_to_symbol(code) or code
                
                direction = Direction.BUY if order_dict.get('action') == 'Buy' else Direction.SELL
                qty = order_dict.get('quantity', 0)
                price = order_dict.get('price', 0.0)
                
                status = OrderStatus.SUBMITTED
                cancel_qty = status_dict.get('cancel_quantity', 0)
                if cancel_qty > 0:
                    status = OrderStatus.CANCELLED
                elif status_dict.get('order_quantity', 0) == 0:
                    status = OrderStatus.FILLED
                
                o = Order(
                    id=broker_id,
                    broker_order_id=broker_id,
                    symbol=symbol,
                    direction=direction,
                    order_type=OrderType.LIMIT,
                    price=price,
                    qty=qty,
                    status=status,
                )
                o.filled_qty = status_dict.get('deal_quantity', 0)
                self._on_order_cb(o)
            except Exception as e:
                logger.error(f"[SinoPac Trade] on_order error: {e}")

        def on_deal(stat, msg):
            if not self._on_fill_cb:
                return
            try:
                from datetime import datetime
                from core.models import Fill, Direction
                # trade_id 官方文件標註「同 FuturesOrder 的 id」，即委託序號本身；
                # ordno 前 5 碼為委託序號、後 3 碼為成交序號，可視為此筆成交的唯一 ID。
                trade_id = msg.get('trade_id', '')
                ordno = msg.get('ordno', '')
                action = msg.get('action', '')
                code = msg.get('code', '')
                price = msg.get('price', 0.0)
                qty = msg.get('quantity', 0)
                ts = msg.get('ts')

                symbol = self._code_to_symbol(code) or code
                direction = Direction.BUY if action == 'Buy' else Direction.SELL

                f = Fill(
                    order_id=trade_id,
                    symbol=symbol,
                    direction=direction,
                    price=price,
                    qty=qty,
                    fee=0.0,
                    timestamp=datetime.fromtimestamp(ts) if ts else datetime.now(),
                    broker_fill_id=ordno,
                )
                self._on_fill_cb(f)
            except Exception as e:
                logger.error(f"[SinoPac Trade] on_deal error: {e}")

        self._api.set_order_callback(on_order)
        # Note: In some versions of Shioaji, deal callback might be set differently or not exist.
        # usually set_order_callback handles both, or there is set_deal_callback.
        if hasattr(self._api, "set_deal_callback"):
            self._api.set_deal_callback(on_deal)

    async def place_order(self, symbol, direction, order_type, qty, price=0.0) -> str:
        import shioaji as sj

        contract = await self._get_contract(symbol)
        if not contract:
            return ""

        action = sj.Action.Buy if direction == Direction.BUY else sj.Action.Sell
        price_type = sj.FuturesPriceType.MKT if order_type == OrderType.MARKET else sj.FuturesPriceType.LMT
        order_lot = sj.FuturesOrder(
            action=action,
            price=price,
            quantity=qty,
            price_type=price_type,
            order_type=sj.OrderType.ROD,
            account=self._api.futopt_account,
        )
        trade_obj = self._api.place_order(contract, order_lot)
        return trade_obj.order.id if trade_obj else ""

    async def cancel_order(self, broker_order_id: str) -> bool:
        try:
            self._api.cancel_order(broker_order_id)
            return True
        except Exception:
            return False

    async def modify_order(self, broker_order_id, new_price=0, new_qty=0) -> bool:
        try:
            self._api.update_order(broker_order_id, price=new_price, qty=new_qty)
            return True
        except Exception:
            return False

    def set_on_order_update(self, callback):
        self._on_order_cb = callback

    def set_on_fill(self, callback):
        self._on_fill_cb = callback

    async def get_positions(self) -> list[Position]:
        try:
            positions = self._api.list_positions(self._api.futopt_account)
            return [
                Position(
                    symbol=p.code,
                    side=PositionSide.LONG if p.direction == "Buy" else PositionSide.SHORT,
                    qty=p.quantity,
                    avg_price=p.price,
                )
                for p in positions
            ]
        except Exception:
            return []

    async def get_open_orders(self) -> list[Order]:
        # TODO: 實作
        return []

    async def get_fills_today(self) -> list[Fill]:
        """查詢今日成交明細（含連線前已成交的部分）"""
        try:
            self._api.update_status(self._api.futopt_account)
            trades = self._api.list_trades()
        except Exception:
            logger.exception("[SinoPac Trade] get_fills_today 查詢失敗")
            return []

        fills: list[Fill] = []
        for trade in trades:
            deals = getattr(trade.status, "deals", None) or []
            if not deals:
                continue
            code = trade.contract.code
            symbol = self._code_to_symbol(code) or code
            direction = Direction.BUY if trade.order.action == "Buy" else Direction.SELL
            for deal in deals:
                ts = getattr(deal, "ts", None)
                fills.append(Fill(
                    order_id=trade.order.id,
                    symbol=symbol,
                    direction=direction,
                    price=deal.price,
                    qty=deal.quantity,
                    fee=0.0,
                    timestamp=datetime.fromtimestamp(ts) if ts else datetime.now(),
                    broker_fill_id=str(getattr(deal, "seq", "")),
                ))

        fills.sort(key=lambda f: f.timestamp, reverse=True)
        return fills

    async def get_profit_loss_today(self) -> list[dict]:
        """查詢今日已實現損益（list_profit_loss，只涵蓋已平倉的部位）"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            records = self._api.list_profit_loss(self._api.futopt_account, today, today)
        except Exception:
            logger.exception("[SinoPac Trade] get_profit_loss_today 查詢失敗")
            return []

        result = []
        for r in records:
            code = getattr(r, "code", "")
            result.append({
                "symbol": self._code_to_symbol(code) or code,
                "quantity": getattr(r, "quantity", 0),
                "cover_price": getattr(r, "cover_price", 0.0),
                "pnl": getattr(r, "pnl", 0.0) or 0.0,
                "fee": getattr(r, "fee", 0) or 0,
                "tax": getattr(r, "tax", 0) or 0,
            })
        return result

    def _lookup_contract(self, symbol):
        SYMBOL_MAP = {"TX": "TXF", "MTX": "MXF", "TMF": "TMF"}
        sj_symbol = SYMBOL_MAP.get(symbol, symbol)
        try:
            # Contracts.Futures 用 __getitem__ 直接查完整合約代碼（如 "TXFR1"）
            return self._api.Contracts.Futures[sj_symbol + "R1"]
        except (KeyError, AttributeError):
            return None

    async def _get_contract(self, symbol, attempts: int = 6, delay: float = 0.5):
        """見 SinoPacQuoteAdapter._get_contract 的說明。"""
        import asyncio

        for attempt in range(1, attempts + 1):
            contract = self._lookup_contract(symbol)
            if contract is not None:
                return contract
            if attempt < attempts:
                await asyncio.sleep(delay)

        SYMBOL_MAP = {"TX": "TXF", "MTX": "MXF", "TMF": "TMF"}
        logger.warning(f"[SinoPac] 找不到合約: {symbol} → {SYMBOL_MAP.get(symbol, symbol)}")
        return None
