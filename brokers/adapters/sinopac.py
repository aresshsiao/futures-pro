"""
brokers/adapters/sinopac.py — 永豐金 Shioaji Adapter
參考實作，展示如何將券商 API 對接到系統的統一介面。

需安裝: pip install shioaji
文件: https://sinotrade.github.io/
"""
from __future__ import annotations
import asyncio
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from core.models import (
    Bar, Direction, Fill, Order, OrderBook, OrderBookLevel,
    OrderStatus, OrderType, Position, PositionSide, Tick, Timeframe,
)
from brokers.base import QuoteAdapter, TradeAdapter

logger = logging.getLogger(__name__)

_SHARED_API = None
_SHARED_CONNECTED = False
_SHARED_SIMULATION = False

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
    """取得共用的 Shioaji instance（問價與交易兩個 adapter 共用同一條連線）。

    credentials["simulation"]=True 時走永豐金的模擬環境：帳號、下單、回報、
    庫存查詢全部照跑，但成交只發生在券商的測試主機上，不會動到真實資金，
    適合用來驗證整條下單鏈路。模擬環境沒有憑證機制，activate_ca 會失敗，
    所以這裡直接跳過。

    simulation 是 Shioaji() 的建構子參數，無法在既有 instance 上切換，
    因此模式改變時必須先登出、丟掉舊 instance 再重建。
    """
    global _SHARED_API, _SHARED_CONNECTED, _SHARED_SIMULATION
    import shioaji as sj

    simulation = bool(credentials.get("simulation", False))

    if _SHARED_API is not None and simulation != _SHARED_SIMULATION:
        logger.info("[SinoPac] 切換至%s環境，重建連線", "模擬" if simulation else "正式")
        _logout_shared_api()
        _SHARED_API = None

    if _SHARED_API is None:
        _SHARED_API = sj.Shioaji(simulation=simulation)
        _SHARED_SIMULATION = simulation

        @_SHARED_API.on_event
        def _on_event(resp_code: int, event_code: int, info: str, event: str):
            logger.warning("[SinoPac Event] resp_code=%s event_code=%s info=%s event=%s", resp_code, event_code, info, event)

    if not _SHARED_CONNECTED:
        for ev in _CONTRACTS_READY.values():
            ev.clear()
        accounts = _SHARED_API.login(
            api_key=credentials.get("api_key", ""),
            secret_key=credentials.get("secret_key", ""),
            subscribe_trade=credentials.get("subscribe_trade", True),
            receive_window=10000,
            contracts_cb=_on_contracts_fetched,
        )
        logger.info(
            "[SinoPac] 登入成功（%s環境），可用帳戶 %s 個",
            "模擬" if simulation else "正式",
            len(accounts) if accounts else 0,
        )
        if credentials.get("cert_path") and not simulation:
            _SHARED_API.activate_ca(
                ca_path=credentials["cert_path"],
                ca_passwd=credentials.get("cert_password", ""),
                person_id=credentials.get("person_id", ""),
            )
        _SHARED_CONNECTED = True
    return _SHARED_API


def _is_simulation() -> bool:
    """目前共用連線是否處於模擬環境。"""
    return _SHARED_SIMULATION


# ── Shioaji 回傳值正規化 ────────────────────────────────
# Shioaji 1.5.x 是 Rust 重寫版，同一個欄位在 callback（dict）與查詢 API（物件）
# 兩條路徑拿到的型別不一樣：一邊是字串，一邊是 enum 物件。
# 這幾個 helper 把兩邊統一成 Python 基本型別，順便讓結果可以直接丟進 WebSocket。

def _order_error_text(exc: Exception) -> str:
    """把 Shioaji 的下單錯誤壓成一句能顯示在畫面上的話。

    原始訊息長這樣（前半段是沒有閱讀價值的 request id）：
        place_order: request #P2P/v:.../PYAPI/.../ code: 406, detail: Please sign F00200018xxxxx first.
    有用的只有 detail 後面那段，其中「Please sign ... first」是最常見的一種——
    帳號沒簽 API 下單同意書，程式端怎麼重送都不會成功。
    """
    text = str(exc).strip() or exc.__class__.__name__
    detail = text.split("detail:", 1)[1].strip() if "detail:" in text else text
    if "sign" in detail.lower():
        return f"帳號尚未簽署 API 下單同意書，請至永豐官網完成簽署後重新登入（{detail}）"
    return detail[:200]


def _enum_str(v) -> str:
    """取 enum 的字串值；本來就是字串就原樣回傳。"""
    if v is None:
        return ""
    return str(getattr(v, "value", v))


def _json_safe(v):
    """轉成 JSON 可序列化的型別（enum → 字串、date → ISO 字串）。

    基本型別刻意用 `type(v) in (...)` 而非 isinstance：Shioaji 的 FetchStatus
    是 Rust 綁定型別，isinstance(x, str) 會騙人說 True（mro 只有 object），
    但 json 的 C encoder 檢查真實型別，放行的話會在 send_json 時才炸開。
    """
    if v is None or type(v) in (bool, int, float, str):
        return v
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    # 先取 .value：數值型 enum 才不會因為上面的精確型別判斷被轉成字串
    inner = getattr(v, "value", None)
    if type(inner) in (bool, int, float, str):
        return inner
    return _enum_str(v)


def _obj_to_dict(obj) -> dict:
    """Shioaji 的查詢結果物件都有 .dict()，轉成 JSON 安全的 dict。"""
    if obj is None:
        return {}
    raw = None
    if hasattr(obj, "dict"):
        try:
            raw = obj.dict()
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        return {}
    return {str(k): _json_safe(v) for k, v in raw.items()}


# Shioaji 委託狀態 → 系統 OrderStatus
_SHIOAJI_STATUS_MAP = {
    "PendingSubmit": OrderStatus.PENDING,     # 傳送中
    "PreSubmitted":  OrderStatus.PENDING,     # 預約單（盤前）
    "Submitted":     OrderStatus.SUBMITTED,   # 委託成功
    "PartFilled":    OrderStatus.PARTIAL,
    "Filled":        OrderStatus.FILLED,
    "Cancelled":     OrderStatus.CANCELLED,
    "Failed":        OrderStatus.REJECTED,
    "Inactive":      OrderStatus.REJECTED,    # 失效（例如超過有效期）
}


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
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # ── 選擇權鏈即時報價（訂閱式，取代 snapshots() 輪詢）──
        # chain_key 格式為 "PROD:delivery_month"，跟 get_options_t_quote 的 month 參數一致
        self._option_chain_cache: dict[str, dict[float, dict]] = {}       # chain_key -> {strike: row}
        self._option_code_index: dict[str, tuple[str, float, bool]] = {}  # contract code -> (chain_key, strike, is_call)
        self._option_chain_contracts: dict[str, list] = {}                # chain_key -> [contract, ...]（已訂閱的合約，供取消訂閱用）
        self._option_chain_callback: dict[str, Callable[[list[dict]], None]] = {}
        self._option_chain_delivery_date: dict[str, str] = {}
        self._option_chain_spot_price: dict[str, float] = {}
        self._option_chain_trading_dates: dict[str, Optional[list[str]]] = {}
        self._option_push_pending: set[str] = set()  # 正在等待 debounce flush 的 chain_key

    async def connect(self, **credentials) -> bool:
        """
        credentials:
            api_key: str
            secret_key: str
        """
        try:
            import shioaji as sj

            self._loop = asyncio.get_running_loop()
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

            @self._api.on_quote_fop_v1()
            def _on_option_quote(exchange, quote):
                """選擇權鏈即時報價（訂閱式，取代 snapshots() 輪詢——
                snapshots() 是請求式查詢，官方文件明講不能當即時 feed 反覆輪詢，
                違規會被停權，見 subscribe_options_t_quote）。"""
                info = self._option_code_index.get(quote.code)
                if info is None:
                    return
                chain_key, strike, is_call = info
                row = self._option_chain_cache.setdefault(chain_key, {}).setdefault(
                    strike, {"strike": strike, "callPrice": 0, "callChange": 0, "putPrice": 0, "putChange": 0}
                )
                price = float(getattr(quote, "close", 0.0) or 0.0)
                change = float(getattr(quote, "change_price", 0.0) or 0.0)
                if is_call:
                    row["callPrice"] = price
                    row["callChange"] = change
                else:
                    row["putPrice"] = price
                    row["putChange"] = change
                self._schedule_option_push(chain_key)

            self._connected = True
            logger.info("[SinoPac Quote] 登入成功%s", "（模擬環境）" if _is_simulation() else "")
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

    # ── 選擇權鏈即時報價（訂閱式）──────────────────────

    @staticmethod
    def _option_chain_key(symbol: str, month: str) -> str:
        if ":" in month:
            return month
        return f"{symbol or 'TXO'}:{month}"

    async def subscribe_options_t_quote(
        self, symbol: str, month: str, callback: Callable[[list[dict]], None],
        spot_price: float = 0.0, trading_dates: list[str] | None = None,
    ) -> None:
        """訂閱指定月份選擇權鏈（call+put 全履約價）的即時報價，取代 get_options_t_quote
        的輪詢用法。同一條鏈重複訂閱只會更新 callback/spot_price，不會重打 Shioaji。"""
        chain_key = self._option_chain_key(symbol, month)
        self._option_chain_callback[chain_key] = callback
        self._option_chain_spot_price[chain_key] = spot_price
        self._option_chain_trading_dates[chain_key] = trading_dates

        if chain_key in self._option_chain_contracts:
            return  # 已經訂閱過這條鏈，只是換了 callback/spot_price

        prod, dm = chain_key.split(":", 1)
        prod_contracts = getattr(self._api.Contracts.Options, prod, None)
        if prod_contracts is None:
            logger.warning("[SinoPac] options product not found: %s", prod)
            return
        contracts = [c for c in prod_contracts if c.delivery_month == dm]
        if not contracts:
            logger.warning("[SinoPac] subscribe_options_t_quote: no contracts for %s %s", prod, dm)
            return

        if spot_price > 0:
            strikes = sorted(list(set(c.strike_price for c in contracts)))
            atm_strike = min(strikes, key=lambda x: abs(x - spot_price))
            atm_idx = strikes.index(atm_strike)
            start_idx = max(0, atm_idx - 20)
            end_idx = min(len(strikes), atm_idx + 21)
            target_strikes = set(strikes[start_idx:end_idx])
            contracts = [c for c in contracts if c.strike_price in target_strikes]

        import shioaji as sj

        for c in contracts:
            is_call = c.option_right == sj.constant.OptionRight.Call
            self._option_code_index[c.code] = (chain_key, c.strike_price, is_call)
            self._api.quote.subscribe(c, quote_type=sj.constant.QuoteType.Quote, version="v1")

        self._option_chain_contracts[chain_key] = contracts
        self._option_chain_cache.setdefault(chain_key, {})
        self._option_chain_delivery_date[chain_key] = contracts[0].delivery_date
        logger.info("[SinoPac] 已訂閱選擇權鏈即時報價: %s (%d 口合約)", chain_key, len(contracts))

    async def update_options_spot_price(self, symbol: str, month: str, spot_price: float) -> None:
        """更新某條已訂閱選擇權鏈用於理論價計算的現貨/期貨價，不需要重新訂閱。"""
        chain_key = self._option_chain_key(symbol, month)
        if chain_key in self._option_chain_contracts:
            self._option_chain_spot_price[chain_key] = spot_price

    async def unsubscribe_options_t_quote(self, symbol: str, month: str) -> None:
        import shioaji as sj
        chain_key = self._option_chain_key(symbol, month)
        contracts = self._option_chain_contracts.pop(chain_key, None)
        if not contracts:
            return
        for c in contracts:
            try:
                self._api.quote.unsubscribe(c, quote_type=sj.constant.QuoteType.Quote, version="v1")
            except Exception as e:
                logger.error(f"[SinoPac] unsubscribe_options_t_quote error: {e}")
            self._option_code_index.pop(c.code, None)
        self._option_chain_callback.pop(chain_key, None)
        self._option_chain_cache.pop(chain_key, None)
        self._option_chain_delivery_date.pop(chain_key, None)
        self._option_chain_spot_price.pop(chain_key, None)
        self._option_chain_trading_dates.pop(chain_key, None)
        logger.info("[SinoPac] 已取消訂閱選擇權鏈: %s", chain_key)

    def _schedule_option_push(self, chain_key: str) -> None:
        """從 Shioaji callback 執行緒排程一次 debounced 推送（合併短時間內的多筆更新）。"""
        if not self._loop or chain_key in self._option_push_pending:
            return
        self._option_push_pending.add(chain_key)
        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(self._flush_option_push(chain_key))
        )

    async def _flush_option_push(self, chain_key: str) -> None:
        await asyncio.sleep(0.2)  # 200ms debounce，避免整條鏈同時跳動時狂發訊息
        self._option_push_pending.discard(chain_key)

        cb = self._option_chain_callback.get(chain_key)
        chain = self._option_chain_cache.get(chain_key)
        if not cb or not chain:
            return

        strikes = {k: dict(v) for k, v in chain.items()}  # 複製快照，避免計算中途被下一筆 tick 修改
        spot_price = self._option_chain_spot_price.get(chain_key, 0.0)
        delivery_date_str = self._option_chain_delivery_date.get(chain_key)
        trading_dates = self._option_chain_trading_dates.get(chain_key)
        if spot_price > 0 and delivery_date_str:
            call_snap_by_strike = {k: v["callPrice"] for k, v in strikes.items() if v["callPrice"] > 0}
            put_snap_by_strike = {k: v["putPrice"] for k, v in strikes.items() if v["putPrice"] > 0}
            self._apply_theoretical_premium(
                strikes, spot_price, delivery_date_str, call_snap_by_strike, put_snap_by_strike, trading_dates,
            )

        cb([strikes[k] for k in sorted(strikes.keys())])

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

    # ── 其他行情查詢 ──────────────────────────────────
    # 以下都是請求式（非訂閱）查詢，Shioaji 皆為同步阻塞呼叫，
    # 一律用 run_in_executor 丟到執行緒，避免凍結 asyncio event loop。

    async def get_ticks(
        self,
        symbol: str,
        date: str = "",
        query_type: str = "AllDay",
        time_start: str = "",
        time_end: str = "",
        last_count: int = 0,
    ) -> list[dict]:
        """取得歷史逐筆成交明細（api.ticks）。

        date 空字串 = 今日。query_type:
          AllDay    — 整個交易日
          RangeTime — time_start ~ time_end（格式 "HH:MM:SS.ffffff"）
          LastCount — 最後 last_count 筆（上限 2000）
        """
        import shioaji as sj

        contract = await self._get_contract(symbol)
        if not contract:
            return []

        qt_map = {
            "AllDay": sj.TicksQueryType.AllDay,
            "RangeTime": sj.TicksQueryType.RangeTime,
            "LastCount": sj.TicksQueryType.LastCount,
        }
        qt = qt_map.get(query_type, sj.TicksQueryType.AllDay)
        query_date = date or datetime.now().strftime("%Y-%m-%d")

        kwargs = {"contract": contract, "date": query_date, "query_type": qt}
        if qt == sj.TicksQueryType.RangeTime:
            kwargs["time_start"] = time_start
            kwargs["time_end"] = time_end
        elif qt == sj.TicksQueryType.LastCount:
            kwargs["last_cnt"] = last_count or 100

        try:
            loop = asyncio.get_running_loop()
            ticks = await loop.run_in_executor(None, lambda: self._api.ticks(**kwargs))
        except Exception:
            logger.exception("[SinoPac] 取得逐筆成交失敗 %s %s", symbol, query_date)
            return []

        ts_list = list(getattr(ticks, "ts", []) or [])
        if not ts_list:
            return []

        def _col(name):
            return list(getattr(ticks, name, []) or [])

        close, volume = _col("close"), _col("volume")
        bid_p, bid_v = _col("bid_price"), _col("bid_volume")
        ask_p, ask_v = _col("ask_price"), _col("ask_volume")
        tick_type = _col("tick_type")

        def _at(seq, i, default=0):
            return seq[i] if i < len(seq) else default

        rows = []
        for i, ts in enumerate(ts_list):
            rows.append({
                # ts 是奈秒時間戳，轉成前端慣用的毫秒
                "time": int(ts) // 1_000_000,
                "price": float(_at(close, i, 0.0)),
                "volume": int(_at(volume, i, 0)),
                "bid_price": float(_at(bid_p, i, 0.0)),
                "bid_volume": int(_at(bid_v, i, 0)),
                "ask_price": float(_at(ask_p, i, 0.0)),
                "ask_volume": int(_at(ask_v, i, 0)),
                # tick_type: 1=外盤(買方成交) 2=內盤(賣方成交) 0=無法判斷
                "tick_type": int(_at(tick_type, i, 0)),
            })
        logger.info("[SinoPac] %s %s 逐筆成交 %d 筆", symbol, query_date, len(rows))
        return rows

    async def get_snapshot(self, symbols: list[str]) -> list[dict]:
        """取得多商品即時快照（api.snapshots）。

        這是請求式查詢，永豐金明確禁止當即時 feed 反覆輪詢（會被停權），
        只適合用在「開盤前先補一次現價」這類一次性場合；
        持續更新請用 subscribe_tick / subscribe_orderbook。
        """
        contracts = []
        for sym in symbols:
            c = await self._get_contract(sym)
            if c is not None:
                contracts.append((sym, c))
        if not contracts:
            return []

        try:
            loop = asyncio.get_running_loop()
            snaps = await loop.run_in_executor(
                None, lambda: self._api.snapshots([c for _, c in contracts])
            )
        except Exception:
            logger.exception("[SinoPac] 取得快照失敗: %s", symbols)
            return []

        rows = []
        for (sym, _), s in zip(contracts, snaps or []):
            rows.append({
                "symbol": sym,
                "code": getattr(s, "code", ""),
                "time": int(getattr(s, "ts", 0) or 0) // 1_000_000,
                "open": float(getattr(s, "open", 0.0) or 0.0),
                "high": float(getattr(s, "high", 0.0) or 0.0),
                "low": float(getattr(s, "low", 0.0) or 0.0),
                "close": float(getattr(s, "close", 0.0) or 0.0),
                "average_price": float(getattr(s, "average_price", 0.0) or 0.0),
                "change_price": float(getattr(s, "change_price", 0.0) or 0.0),
                "change_rate": float(getattr(s, "change_rate", 0.0) or 0.0),
                "volume": int(getattr(s, "volume", 0) or 0),
                "total_volume": int(getattr(s, "total_volume", 0) or 0),
                "buy_price": float(getattr(s, "buy_price", 0.0) or 0.0),
                "buy_volume": int(getattr(s, "buy_volume", 0) or 0),
                "sell_price": float(getattr(s, "sell_price", 0.0) or 0.0),
                "sell_volume": int(getattr(s, "sell_volume", 0) or 0),
                "yesterday_volume": int(getattr(s, "yesterday_volume", 0) or 0),
            })
        return rows

    async def get_contract_info(self, symbol: str) -> dict:
        """取得合約規格（漲跌停、參考價、到期日等），下單前檢查價格範圍用。"""
        contract = await self._get_contract(symbol)
        if not contract:
            return {}

        fields = (
            "code", "symbol", "name", "category", "exchange", "unit",
            "delivery_month", "delivery_date", "underlying_kind",
            "limit_up", "limit_down", "reference", "update_date",
        )
        info = {"symbol_id": symbol}
        for f in fields:
            v = getattr(contract, f, None)
            if v is None:
                continue
            info[f] = v if isinstance(v, (int, float, str)) else str(v)
        return info

    async def get_api_usage(self) -> dict:
        """查詢今日 API 流量用量（api.usage）。

        永豐金對每日下載量有上限，歷史資料抓太兇會被擋，
        排查「突然抓不到資料」時先看這個。
        """
        try:
            loop = asyncio.get_running_loop()
            u = await loop.run_in_executor(None, self._api.usage)
        except Exception:
            logger.exception("[SinoPac] 查詢 API 用量失敗")
            return {}

        used = int(getattr(u, "bytes", 0) or 0)
        limit = int(getattr(u, "limit_bytes", 0) or 0)
        return {
            "connections": int(getattr(u, "connections", 0) or 0),
            "bytes": used,
            "limit_bytes": limit,
            "remaining_bytes": int(getattr(u, "remaining_bytes", 0) or 0),
            "used_pct": round(used / limit * 100, 2) if limit else 0.0,
        }

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
        # broker_order_id → shioaji Trade 物件。
        # cancel_order()/update_order() 只吃 Trade 物件本身（不是委託序號字串），
        # 所以下單當下就得留著；重啟後要刪先前掛的單，則靠 _sync_trades() 從券商補回。
        self._trades: dict[str, object] = {}
        self._trades_cache: list | None = None   # _sync_trades 的短期結果快取
        self._trades_cache_at = 0.0

    async def connect(self, **credentials) -> bool:
        try:
            self._api = _get_shared_api(credentials)
            await _wait_contracts_ready(("Future",))

            self._setup_callbacks()
            self._connected = True
            account = self._account()
            logger.info(
                "[SinoPac Trade] 登入成功%s，期貨帳戶=%s",
                "（模擬環境，不會動到真實資金）" if _is_simulation() else "",
                getattr(account, "account_id", None) or "無",
            )
            if account is None:
                logger.warning("[SinoPac Trade] 找不到期貨帳戶（futopt_account），將無法下單")
            return True
        except Exception:
            logger.exception("[SinoPac Trade] 登入失敗")
            return False

    async def disconnect(self) -> None:
        _logout_shared_api()
        self._connected = False
        self._trades.clear()

    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_simulation(self) -> bool:
        return _is_simulation()

    def _account(self):
        """目前使用的期貨/選擇權帳戶（登入後由 Shioaji 自動指定預設帳戶）。"""
        return getattr(self._api, "futopt_account", None)

    async def _run(self, fn, *args, **kwargs):
        """Shioaji 的查詢/下單都是同步阻塞呼叫，丟到執行緒避免凍結 event loop。"""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
        except Exception as e:
            self._note_connection_error(e)
            raise

    # 這些錯誤代表「登入狀態已經沒了」，不是單次查詢失敗。
    # 特徵字串要夠specific —— 用 "401" 這種短字串比對，隨便一個相關序號或價格
    # 裡出現 401 就會把使用者誤判成斷線。
    _AUTH_ERROR_HINTS = (
        "token is expired", "tokenerror", "sessionnotestablished",
        "session error", "unauthorized",
    )

    def _note_connection_error(self, exc: Exception) -> None:
        """認證失效／session 斷掉時，把自己標記成未連線。

        隔夜或電腦休眠後 token 會過期，但本地的 _connected 旗標還是 True ——
        畫面上券商燈號還是綠的、下單按鈕照樣能按，實際上每一發 API 都會失敗。
        寧可誠實顯示斷線讓使用者重新連線，也不要讓人以為單送得出去。
        """
        if not self._connected:
            return
        text = f"{type(exc).__name__} {exc}".lower()
        if not any(hint in text for hint in self._AUTH_ERROR_HINTS):
            return   # 一般的查詢失敗（逾時、單次錯誤）不動連線狀態
        self._connected = False
        logger.error(
            "[SinoPac Trade] 券商連線已失效（%s: %s），請重新連線券商",
            type(exc).__name__, str(exc)[:120],
        )

    _CODE_PREFIX_MAP = {"TXF": "TX", "MXF": "MTX", "TMF": "TMF"}

    def _code_to_symbol(self, code: str) -> str | None:
        """將 Shioaji code（如 TXFR1）轉回系統商品代碼（如 TX）"""
        for prefix, symbol in self._CODE_PREFIX_MAP.items():
            if code.startswith(prefix):
                return symbol
        return None

    # ── 委託/成交回報 ─────────────────────────────────

    def _setup_callbacks(self):
        """設定回報 callback。

        Shioaji 只有 set_order_callback 一個入口，委託回報與成交回報都從這裡進來，
        靠第一個參數 stat（OrderState）區分：
          FuturesOrder / StockOrder → 委託狀態變更
          FuturesDeal  / StockDeal  → 成交明細
        （這版 shioaji 沒有 set_deal_callback，之前掛在那上面的成交回報等於沒接上。）
        """
        def on_event(stat, msg):
            state = _enum_str(stat)
            # 回報有沒有進來，是「畫面倉位對不對」的根本前提；沒有這行 log，
            # 倉位不動時完全分不出是券商沒回報、還是回報進來後被解析錯。
            logger.debug("[SinoPac Trade] 收到回報 stat=%s msg=%s", state, msg)
            try:
                if self._is_deal_report(state, msg or {}):
                    self._handle_deal(msg or {})
                else:
                    self._handle_order(msg or {})
            except Exception:
                logger.exception("[SinoPac Trade] 處理回報失敗 stat=%s msg=%s", state, msg)

        self._api.set_order_callback(on_event)

    @staticmethod
    def _is_deal_report(state: str, msg: dict) -> bool:
        """這筆回報是成交（Deal）還是委託（Order）？

        分錯的代價很大：成交回報是平坦結構（trade_id / ordno / price / quantity），
        被當成委託回報解析的話每個欄位都取不到值，成交就這樣整筆消失 ——
        畫面上看不到成交、倉位不會動，只能等對帳把狀態補回來。

        OrderState 的值是**全大寫**（FDEAL / FORDER / SDEAL / SORDER），
        所以比對一定要忽略大小寫；再用訊息結構兜底，免得換版本又踩一次。
        """
        if state.upper().endswith("DEAL"):
            return True
        if state.upper().endswith("ORDER"):
            return False
        # stat 認不得時看結構：委託回報有巢狀的 order/status，成交回報沒有
        return "order" not in msg and ("trade_id" in msg or "ordno" in msg)

    @staticmethod
    def _resolve_status(raw, qty: int, deal_qty: int, cancel_qty: int) -> OrderStatus:
        """優先採用券商回報的 status 字串，沒有時才用數量推斷。"""
        mapped = _SHIOAJI_STATUS_MAP.get(_enum_str(raw))
        if mapped is not None:
            return mapped
        if cancel_qty > 0:
            return OrderStatus.CANCELLED
        if qty and deal_qty >= qty:
            return OrderStatus.FILLED
        if deal_qty > 0:
            return OrderStatus.PARTIAL
        return OrderStatus.SUBMITTED

    def _handle_order(self, msg: dict) -> None:
        """委託狀態回報（下單成功/失敗、刪改單結果）"""
        if not self._on_order_cb:
            return

        order_dict = msg.get("order", {}) or {}
        status_dict = msg.get("status", {}) or {}
        contract_dict = msg.get("contract", {}) or {}
        operation = msg.get("operation", {}) or {}

        broker_id = str(order_dict.get("id", "") or status_dict.get("id", "") or "")
        if not broker_id:
            # 沒有委託序號的委託回報上層根本對不到任何一張單，硬送過去只會是雜訊。
            # 真正該擔心的是「這其實是成交回報卻被分派到這裡」——把原始訊息留下來，
            # 下次一眼就能看出是哪種結構跑錯邊。
            logger.warning("[SinoPac Trade] 委託回報沒有委託序號，已忽略。原始訊息: %s", msg)
            return

        code = str(contract_dict.get("code", "") or "")
        symbol = self._code_to_symbol(code) or code

        qty = int(order_dict.get("quantity", 0) or 0)
        deal_qty = int(status_dict.get("deal_quantity", 0) or 0)
        cancel_qty = int(status_dict.get("cancel_quantity", 0) or 0)
        price_type = _enum_str(order_dict.get("price_type"))

        status = self._resolve_status(status_dict.get("status"), qty, deal_qty, cancel_qty)

        # op_code "00" 才是成功；其餘代表委託/刪改單被券商退回，op_msg 有原因
        op_code = str(operation.get("op_code", "") or "")
        if op_code and op_code != "00":
            status = OrderStatus.REJECTED
            logger.warning(
                "[SinoPac Trade] 委託遭拒 %s (%s): %s",
                broker_id, op_code, operation.get("op_msg", ""),
            )

        o = Order(
            id=broker_id,
            broker_order_id=broker_id,
            symbol=symbol,
            direction=Direction.BUY if _enum_str(order_dict.get("action")) == "Buy" else Direction.SELL,
            # MKP（範圍市價）跟 MKT 一樣不指定價格，都歸為市價單
            order_type=OrderType.MARKET if price_type in ("MKT", "MKP") else OrderType.LIMIT,
            price=float(order_dict.get("price", 0.0) or 0.0),
            qty=qty,
            status=status,
        )
        o.filled_qty = deal_qty
        logger.info(
            "[SinoPac Trade] 委託回報 %s %s %s x%s 狀態=%s 已成交=%s",
            broker_id or "(無序號)", symbol, o.direction.value, qty, status.value, deal_qty,
        )
        self._on_order_cb(o)

    def _handle_deal(self, msg: dict) -> None:
        """成交回報"""
        if not self._on_fill_cb:
            logger.warning("[SinoPac Trade] 收到成交回報但沒有掛 callback，倉位不會更新")
            return

        # trade_id 官方文件標註「同 FuturesOrder 的 id」，即委託序號本身；
        # ordno 前 5 碼為委託序號、後 3 碼為成交序號，可視為此筆成交的唯一 ID。
        trade_id = str(msg.get("trade_id", "") or "")
        ordno = str(msg.get("ordno", "") or "")
        code = str(msg.get("code", "") or "")
        ts = msg.get("ts")

        qty = int(msg.get("quantity", 0) or 0)
        if not code or qty <= 0:
            # 沒有商品或口數的「成交」不是成交。放行的話 _update_position 會拿
            # 空字串當商品代碼，在倉位表裡長出一個平不掉的幽靈部位。
            logger.warning("[SinoPac Trade] 成交回報缺少商品或口數，已忽略。原始訊息: %s", msg)
            return

        f = Fill(
            order_id=trade_id,
            symbol=self._code_to_symbol(code) or code,
            direction=Direction.BUY if _enum_str(msg.get("action")) == "Buy" else Direction.SELL,
            price=float(msg.get("price", 0.0) or 0.0),
            qty=qty,
            fee=0.0,  # 成交回報不含手續費，需另外查 list_profit_loss
            timestamp=datetime.fromtimestamp(ts) if ts else datetime.now(),
            broker_fill_id=ordno,
        )
        # 券商成交時間 → 我們收到的時間差。回報是推播進來的，這個數字大就是
        # 券商端／網路延遲；數字小但畫面才慢，問題就在我們自己的 event loop。
        lag = (datetime.now() - f.timestamp).total_seconds() if ts else 0.0
        logger.info(
            "[SinoPac Trade] 成交回報 %s %s %s x%s @%s（推播延遲 %.2fs）",
            trade_id or "(無序號)", f.symbol, f.direction.value, f.qty, f.price, lag,
        )
        self._on_fill_cb(f)

    # ── 下單 / 刪改單 ─────────────────────────────────

    async def place_order(
        self, symbol, direction, order_type, qty, price=0.0,
        octype: str = "auto", time_in_force: str = "ROD",
    ) -> str:
        """送出期貨委託，回傳委託序號（失敗回空字串，原因寫在 self.last_error）。

        octype        新倉/平倉別，預設 auto 交給券商依庫存自動判斷
        time_in_force ROD 當日有效 / IOC 立即成交否則取消 / FOK 全部成交否則取消
        """
        import shioaji as sj

        self.last_error = ""

        if self._api is None:
            self.last_error = "交易券商未連線"
            logger.error("[SinoPac Trade] 未連線，無法下單")
            return ""

        contract = await self._get_contract(symbol)
        if not contract:
            self.last_error = f"找不到合約: {symbol}"
            return ""

        account = self._account()
        if account is None:
            self.last_error = "沒有可用的期貨帳戶"
            logger.error("[SinoPac Trade] 沒有可用的期貨帳戶，無法下單")
            return ""

        # 觸價單由 TradeModule 在本地監控，觸發後才以市價送出，正常不會走到這裡；
        # 真的收到就當市價單處理，避免被誤送成 price=0 的限價單。
        if order_type == OrderType.LIMIT:
            price_type = sj.FuturesPriceType.LMT
        else:
            price_type = sj.FuturesPriceType.MKT
            price = 0

        octype_enum = {
            "auto": sj.FuturesOCType.Auto,
            "new": sj.FuturesOCType.New,
            "cover": sj.FuturesOCType.Cover,
            "daytrade": sj.FuturesOCType.DayTrade,
        }.get(str(octype).lower(), sj.FuturesOCType.Auto)

        tif_enum = {
            "ROD": sj.OrderType.ROD,
            "IOC": sj.OrderType.IOC,
            "FOK": sj.OrderType.FOK,
        }.get(str(time_in_force).upper(), sj.OrderType.ROD)

        order_lot = sj.FuturesOrder(
            action=sj.Action.Buy if direction == Direction.BUY else sj.Action.Sell,
            price=price,
            quantity=qty,
            price_type=price_type,
            order_type=tif_enum,
            octype=octype_enum,
            account=account,
        )

        try:
            trade_obj = await self._run(self._api.place_order, contract, order_lot)
        except Exception as e:
            self.last_error = _order_error_text(e)
            logger.error(
                "[SinoPac Trade] 下單失敗 %s %s x%s @%s: %s",
                symbol, direction.value, qty, price, self.last_error,
            )
            logger.debug("[SinoPac Trade] 下單失敗原始錯誤", exc_info=True)
            return ""

        broker_id = str(getattr(getattr(trade_obj, "order", None), "id", "") or "")
        if not broker_id:
            # 沒有委託序號的單既不能刪也不能追蹤成交，當成功記下來只會變成畫面上的幽靈單。
            # 一律當失敗回報，請使用者去券商端確認實際狀態。
            status = getattr(trade_obj, "status", None)
            detail = f"{_enum_str(getattr(status, 'status', ''))} {getattr(status, 'msg', '') or ''}".strip()
            self.last_error = f"券商未回委託序號，請至券商端確認委託是否成立（{detail}）" if detail \
                else "券商未回委託序號，請至券商端確認委託是否成立"
            logger.error(
                "[SinoPac Trade] 下單未取得委託序號 %s %s x%s @%s: %s",
                symbol, direction.value, qty, price, detail or "(無狀態)",
            )
            return ""

        self._trades[broker_id] = trade_obj
        self._invalidate_trades_cache()
        logger.info(
            "[SinoPac Trade] 委託送出%s %s %s %s x%s @%s → %s",
            "（模擬）" if _is_simulation() else "",
            symbol, direction.value, _enum_str(price_type), qty, price, broker_id,
        )
        return broker_id

    # 對帳時會前後腳問成交明細與委託狀態，兩邊都要 _sync_trades；
    # 這麼短的間隔內券商端不會有新資料，重用上一次的結果就好（API 次數直接砍半）
    _TRADES_CACHE_TTL = 2.0

    async def _sync_trades(self) -> list:
        """跟券商同步今日委託，順便重建 broker_order_id → Trade 對照表。

        每次呼叫都是 update_status + list_trades 兩發券商 API，所以剛拿過的結果
        會在 _TRADES_CACHE_TTL 秒內重用。
        """
        if self._api is None:
            return []

        now = time.monotonic()
        if self._trades_cache is not None and now - self._trades_cache_at < self._TRADES_CACHE_TTL:
            return self._trades_cache

        try:
            def _fetch():
                self._api.update_status(self._account())
                return self._api.list_trades()

            trades = await self._run(_fetch)
        except Exception:
            logger.exception("[SinoPac Trade] 同步委託狀態失敗")
            return []

        if not isinstance(trades, list):
            return []

        for t in trades:
            oid = str(getattr(getattr(t, "order", None), "id", "") or "")
            if oid:
                self._trades[oid] = t
        self._trades_cache, self._trades_cache_at = trades, now
        return trades

    def _invalidate_trades_cache(self) -> None:
        """本地剛改動過委託（下單／刪單／改單），快取立刻過期，下次查一定跟券商拿新的。"""
        self._trades_cache = None

    async def _find_trade(self, broker_order_id: str):
        """取得 Shioaji Trade 物件；本地沒有就跟券商同步一次再找。"""
        if not broker_order_id:
            # 空序號永遠找不到，卻會讓每一次呼叫都多打一輪 update_status + list_trades。
            # 前端「全刪」一次就是幾十發券商 API，白白吃掉流量配額。
            return None
        trade = self._trades.get(broker_order_id)
        if trade is not None:
            return trade
        await self._sync_trades()
        return self._trades.get(broker_order_id)

    async def cancel_order(self, broker_order_id: str) -> bool:
        trade = await self._find_trade(broker_order_id)
        if trade is None:
            logger.warning("[SinoPac Trade] 找不到委託 %s，無法刪單", broker_order_id)
            return False
        try:
            await self._run(self._api.cancel_order, trade)
            self._invalidate_trades_cache()
            logger.info("[SinoPac Trade] 刪單送出: %s", broker_order_id)
            return True
        except Exception:
            logger.exception("[SinoPac Trade] 刪單失敗: %s", broker_order_id)
            return False

    async def modify_order(self, broker_order_id, new_price=0, new_qty=0) -> bool:
        """改價/改量。

        期交所規則：改量只能減量，改價只對限價單有效，兩者不能同時改，
        因此這裡一次只送一種（有給新價就改價，否則改量）。
        """
        trade = await self._find_trade(broker_order_id)
        if trade is None:
            logger.warning("[SinoPac Trade] 找不到委託 %s，無法改單", broker_order_id)
            return False

        if new_price:
            kwargs = {"price": new_price}
        elif new_qty:
            kwargs = {"qty": new_qty}
        else:
            logger.warning("[SinoPac Trade] 改單未指定新價格或新數量: %s", broker_order_id)
            return False

        try:
            await self._run(lambda: self._api.update_order(trade, **kwargs))
            self._invalidate_trades_cache()
            logger.info("[SinoPac Trade] 改單送出: %s %s", broker_order_id, kwargs)
            return True
        except Exception:
            logger.exception("[SinoPac Trade] 改單失敗: %s", broker_order_id)
            return False

    def set_on_order_update(self, callback):
        self._on_order_cb = callback

    def set_on_fill(self, callback):
        self._on_fill_cb = callback

    # ── 查詢：倉位 / 委託 / 成交 ───────────────────────

    async def get_positions(self) -> list[Position]:
        """查詢未平倉部位。

        symbol 一律轉回系統代碼（TXFH6 → TX），跟成交回報的口徑一致，
        Position._get_point_value() 也才查得到正確的每點價值。
        """
        try:
            positions = await self._run(self._api.list_positions, self._account())
        except Exception:
            logger.exception("[SinoPac Trade] 查詢倉位失敗")
            return []

        result: list[Position] = []
        for p in positions or []:
            code = str(getattr(p, "code", "") or "")
            result.append(Position(
                symbol=self._code_to_symbol(code) or code,
                side=PositionSide.LONG if _enum_str(getattr(p, "direction", "")) == "Buy" else PositionSide.SHORT,
                qty=int(getattr(p, "quantity", 0) or 0),
                avg_price=float(getattr(p, "price", 0.0) or 0.0),
                current_price=float(getattr(p, "last_price", 0.0) or 0.0),
            ))
        return result

    def _trade_to_order(self, trade) -> Optional[Order]:
        """Shioaji Trade → 系統 Order"""
        order = getattr(trade, "order", None)
        status = getattr(trade, "status", None)
        if order is None or status is None:
            return None

        code = str(getattr(getattr(trade, "contract", None), "code", "") or "")
        qty = int(getattr(order, "quantity", 0) or 0)
        deal_qty = int(getattr(status, "deal_quantity", 0) or 0)
        cancel_qty = int(getattr(status, "cancel_quantity", 0) or 0)
        broker_id = str(getattr(order, "id", "") or getattr(status, "id", "") or "")
        price_type = _enum_str(getattr(order, "price_type", ""))

        o = Order(
            id=broker_id,
            broker_order_id=broker_id,
            symbol=self._code_to_symbol(code) or code,
            direction=Direction.BUY if _enum_str(getattr(order, "action", "")) == "Buy" else Direction.SELL,
            order_type=OrderType.MARKET if price_type in ("MKT", "MKP") else OrderType.LIMIT,
            price=float(getattr(order, "price", 0.0) or 0.0),
            qty=qty,
            status=self._resolve_status(getattr(status, "status", None), qty, deal_qty, cancel_qty),
        )
        o.filled_qty = deal_qty

        deals = getattr(status, "deals", None) or []
        filled = sum(int(getattr(d, "quantity", 0) or 0) for d in deals)
        if filled:
            amount = sum(
                float(getattr(d, "price", 0.0) or 0.0) * int(getattr(d, "quantity", 0) or 0)
                for d in deals
            )
            o.avg_fill_price = amount / filled
        return o

    async def get_orders_today(self) -> list[Order]:
        """查詢今日所有委託，含已成交／已刪單（上層拿來跟本地委託簿對帳）。"""
        orders: list[Order] = []
        for trade in await self._sync_trades():
            try:
                o = self._trade_to_order(trade)
            except Exception:
                logger.exception("[SinoPac Trade] 解析委託失敗: %s", trade)
                continue
            if o is not None:
                orders.append(o)
        return orders

    async def get_open_orders(self) -> list[Order]:
        """查詢尚未成交（還可刪改）的委託。"""
        return [o for o in await self.get_orders_today() if o.is_active]

    async def get_fills_today(self) -> list[Fill]:
        """查詢今日成交明細（含連線前已成交的部分）"""
        fills: list[Fill] = []
        for trade in await self._sync_trades():
            deals = getattr(getattr(trade, "status", None), "deals", None) or []
            if not deals:
                continue
            code = str(getattr(getattr(trade, "contract", None), "code", "") or "")
            symbol = self._code_to_symbol(code) or code
            order = getattr(trade, "order", None)
            direction = Direction.BUY if _enum_str(getattr(order, "action", "")) == "Buy" else Direction.SELL
            for deal in deals:
                ts = getattr(deal, "ts", None)
                fills.append(Fill(
                    order_id=str(getattr(order, "id", "") or ""),
                    symbol=symbol,
                    direction=direction,
                    price=float(getattr(deal, "price", 0.0) or 0.0),
                    qty=int(getattr(deal, "quantity", 0) or 0),
                    fee=0.0,
                    timestamp=datetime.fromtimestamp(ts) if ts else datetime.now(),
                    broker_fill_id=str(getattr(deal, "seq", "")),
                ))

        fills.sort(key=lambda f: f.timestamp, reverse=True)
        return fills

    # ── 查詢：帳務 ────────────────────────────────────

    async def list_accounts(self) -> list[dict]:
        """列出登入後可用的所有帳戶（證券/期貨）。"""
        try:
            accounts = await self._run(self._api.list_accounts)
        except Exception:
            logger.exception("[SinoPac Trade] 查詢帳戶清單失敗")
            return []

        current = getattr(self._account(), "account_id", None)
        rows = []
        for a in accounts or []:
            account_id = str(getattr(a, "account_id", "") or "")
            rows.append({
                "account_id": account_id,
                "account_type": _enum_str(getattr(a, "account_type", "")),
                "broker_id": str(getattr(a, "broker_id", "") or ""),
                "person_id": str(getattr(a, "person_id", "") or ""),
                "username": str(getattr(a, "username", "") or ""),
                "signed": bool(getattr(a, "signed", False)),
                "is_default": bool(account_id and account_id == current),
            })
        return rows

    async def get_account_balance(self) -> dict:
        """查詢證券交割帳戶餘額（期貨保證金請看 get_margin）。"""
        try:
            b = await self._run(self._api.account_balance)
        except Exception:
            logger.exception("[SinoPac Trade] 查詢帳戶餘額失敗")
            return {}
        if isinstance(b, list):
            b = b[0] if b else None
        return _obj_to_dict(b)

    async def get_margin(self) -> dict:
        """查詢期貨保證金專戶（權益數、可用餘額、原始/維持保證金、風險指標…）。

        模擬環境一樣查得到，是驗證模擬下單有沒有真的成交最直接的地方。
        """
        try:
            m = await self._run(self._api.margin, self._account())
        except Exception:
            logger.exception("[SinoPac Trade] 查詢保證金失敗")
            return {}
        return _obj_to_dict(m)

    async def get_position_detail(self, detail_id: int = 0) -> list[dict]:
        """查詢倉位的逐筆進場明細（detail_id=0 → 全部）。"""
        try:
            details = await self._run(self._api.list_position_detail, self._account(), detail_id)
        except Exception:
            logger.exception("[SinoPac Trade] 查詢倉位明細失敗")
            return []
        return [_obj_to_dict(d) for d in details or []]

    async def get_settlements(self) -> list[dict]:
        """查詢交割款（T / T+1 / T+2）"""
        try:
            settlements = await self._run(self._api.list_settlements, self._account())
        except Exception:
            logger.exception("[SinoPac Trade] 查詢交割款失敗")
            return []
        if not isinstance(settlements, list):
            settlements = [settlements] if settlements else []
        return [_obj_to_dict(s) for s in settlements]

    async def get_profit_loss(self, begin_date: str = "", end_date: str = "") -> list[dict]:
        """查詢區間已實現損益（只涵蓋已平倉的部位）。日期空字串 = 今日。"""
        today = datetime.now().strftime("%Y-%m-%d")
        begin = begin_date or today
        end = end_date or begin

        try:
            records = await self._run(self._api.list_profit_loss, self._account(), begin, end)
        except Exception:
            logger.exception("[SinoPac Trade] 查詢已實現損益失敗 %s~%s", begin, end)
            return []

        result = []
        for r in records or []:
            code = str(getattr(r, "code", "") or "")
            result.append({
                "id": getattr(r, "id", 0),               # 供 get_profit_loss_detail 查明細
                "date": _json_safe(getattr(r, "date", "")),
                "symbol": self._code_to_symbol(code) or code,
                "code": code,
                "direction": _enum_str(getattr(r, "direction", "")),
                "quantity": getattr(r, "quantity", 0),
                "entry_price": getattr(r, "entry_price", 0.0),
                "cover_price": getattr(r, "cover_price", 0.0),
                "pnl": getattr(r, "pnl", 0.0) or 0.0,
                "fee": getattr(r, "fee", 0) or 0,
                "tax": getattr(r, "tax", 0) or 0,
            })
        return result

    async def get_profit_loss_today(self) -> list[dict]:
        """查詢今日已實現損益（用於比對成交明細補上平倉損益）"""
        return await self.get_profit_loss()

    async def get_profit_loss_summary(self, begin_date: str = "", end_date: str = "") -> list[dict]:
        """查詢區間已實現損益彙總（依商品彙總，不逐筆列出）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        begin = begin_date or today
        end = end_date or begin
        try:
            records = await self._run(self._api.list_profit_loss_summary, self._account(), begin, end)
        except Exception:
            logger.exception("[SinoPac Trade] 查詢損益彙總失敗 %s~%s", begin, end)
            return []
        return [_obj_to_dict(r) for r in records or []]

    async def get_profit_loss_detail(self, detail_id: int = 0) -> list[dict]:
        """查詢單筆已實現損益的進場明細（detail_id 來自 get_profit_loss 的 id）。"""
        try:
            records = await self._run(self._api.list_profit_loss_detail, self._account(), detail_id)
        except Exception:
            logger.exception("[SinoPac Trade] 查詢損益明細失敗 id=%s", detail_id)
            return []
        return [_obj_to_dict(r) for r in records or []]

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
