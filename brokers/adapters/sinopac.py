"""
brokers/adapters/sinopac.py — 永豐金 Shioaji Adapter
參考實作，展示如何將券商 API 對接到系統的統一介面。

需安裝: pip install shioaji
文件: https://sinotrade.github.io/
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Callable, Optional

from core.models import (
    Bar, Direction, Fill, Order, OrderBook, OrderBookLevel,
    OrderType, Position, PositionSide, Tick, Timeframe,
)
from brokers.base import QuoteAdapter, TradeAdapter

logger = logging.getLogger(__name__)


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

            self._api = sj.Shioaji()
            self._api.login(
                api_key=credentials.get("api_key", ""),
                secret_key=credentials.get("secret_key", ""),
            )

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
        if self._api:
            self._api.logout()
            self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    async def subscribe_tick(self, symbol: str, callback: Callable[[Tick], None]) -> None:
        """訂閱逐筆成交（同一 symbol 只發一次訂閱請求）"""
        self._tick_callbacks[symbol] = callback
        if symbol in self._subscribed:
            return
        contract = self._get_contract(symbol)
        if contract:
            self._api.quote.subscribe(contract, quote_type="tick", version="v1")
            self._subscribed.add(symbol)
            logger.info("[SinoPac] 已訂閱 tick: %s", symbol)

    async def subscribe_orderbook(self, symbol: str, callback: Callable[[OrderBook], None]) -> None:
        """訂閱五檔（同一 symbol 只發一次訂閱請求）"""
        self._book_callbacks[symbol] = callback
        contract = self._get_contract(symbol)
        if contract:
            self._api.quote.subscribe(contract, quote_type="bidask", version="v1")

    async def unsubscribe(self, symbol: str) -> None:
        contract = self._get_contract(symbol)
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

        contract = self._get_contract(symbol)
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
            # 登入後 Shioaji 需要數秒初始化，kbars() 可能立即返回空；最多重試 3 次
            kbars = None
            for attempt in range(1, 4):
                kbars = self._api.kbars(contract=contract, start=start, end=end)
                if kbars and kbars.ts:
                    break
                logger.warning("[SinoPac] kbars 無資料 (%d/3): %s %s", attempt, symbol, timeframe)
                if attempt < 3:
                    await asyncio.sleep(2)
            else:
                return []

            logger.info("[SinoPac] kbars %s 取得 %d 根 M1，start=%s", symbol, len(kbars.ts), start)

            # 聚合成目標週期
            buckets: dict[int, list] = {}
            for i in range(len(kbars.ts)):
                # Shioaji 歷史 K 棒的 ts 欄位，是將台灣時間直接視為 UTC 所算出的 epoch，
                # 這會導致瀏覽器轉換時多加了 8 小時。因此需要將其減去 8 小時 (28800 秒) 
                # 使其成為標準的絕對 UTC epoch。
                ts_sec = int(kbars.ts[i] / 1e9) - 28800
                aligned = (ts_sec // tf_seconds) * tf_seconds
                o, h, l, c, v = kbars.Open[i], kbars.High[i], kbars.Low[i], kbars.Close[i], kbars.Volume[i]
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

    def _get_contract(self, symbol: str):
        """將系統代碼轉換為 Shioaji contract"""
        SYMBOL_MAP = {"TX": "TXF", "MTX": "MXF", "TMF": "TMF"}
        sj_symbol = SYMBOL_MAP.get(symbol, symbol)
        try:
            return self._api.Contracts.Futures[sj_symbol][sj_symbol + "R1"]  # 近月主力, e.g. TXFR1
        except (KeyError, AttributeError):
            logger.warning(f"[SinoPac] 找不到合約: {symbol} → {sj_symbol}")
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

        使用 api.Contracts.Options.keys() 動態取得所有實際存在的 Option 產品，
        過濾出 TX 開頭的台指選擇權系列（TXO, TXW1, TXW2, TXW4 等）。
        """
        if not self._api:
            return []
        try:
            # _block() 等待 StreamProductContracts 完成載入（_fetched=True）；
            # keys() 本身不呼叫 _block()，若合約尚未完成串流會取到空集合
            self._api.Contracts.Options._block()
            all_categories = list(self._api.Contracts.Options.keys())
            tx_categories = [c for c in all_categories if c.startswith("TX")]
            logger.info("[SinoPac] option categories found: %s", tx_categories)

            seen: set[str] = set()
            entries: list[str] = []
            for prod in tx_categories:
                try:
                    prod_contracts = self._api.Contracts.Options[prod]
                    for c in prod_contracts:
                        dm = getattr(c, "delivery_month", "")
                        if not dm:
                            continue
                        key = f"{prod}:{dm}"
                        if key not in seen:
                            seen.add(key)
                            entries.append(key)
                except Exception:
                    pass

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

    async def get_options_t_quote(self, symbol: str, month: str) -> list[dict]:
        """month 格式為 "PRODUCT:delivery_month"，例如 "TXW1:202607" 或 "TXO:202607W1"。
        舊格式（純 delivery_month 字串）仍相容，預設 product 為 TXO。
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
            for contract, snap in zip(contracts, snapshots):
                s = contract.strike_price
                if s not in strikes:
                    strikes[s] = {"strike": s, "callPrice": 0, "callChange": 0, "putPrice": 0, "putChange": 0}
                price = snap.close if snap.close > 0 else 0
                if contract.option_right == sj.constant.OptionRight.Call:
                    strikes[s]["callPrice"] = price
                else:
                    strikes[s]["putPrice"] = price

            return [strikes[s] for s in sorted(strikes.keys())]
        except Exception as e:
            logger.exception("[SinoPac] get_options_t_quote error")
            return []


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

            self._api = sj.Shioaji()
            self._api.login(
                api_key=credentials.get("api_key", ""),
                secret_key=credentials.get("secret_key", ""),
            )

            # 啟用憑證 (下單需要)
            if "cert_path" in credentials:
                self._api.activate_ca(
                    ca_path=credentials["cert_path"],
                    ca_passwd=credentials.get("cert_password", ""),
                    person_id=credentials.get("person_id", ""),
                )

            self._setup_callbacks()
            self._connected = True
            logger.info("[SinoPac Trade] 登入成功 (含憑證)")
            return True
        except Exception:
            logger.exception("[SinoPac Trade] 登入失敗")
            return False

    async def disconnect(self) -> None:
        if self._api:
            self._api.logout()
            self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def _setup_callbacks(self):
        @self._api.on_order_callback
        def on_order(stat, msg):
            if self._on_order_cb:
                # TODO: 轉換為 Order model
                pass

        @self._api.on_deal_callback
        def on_deal(stat, msg):
            if self._on_fill_cb:
                # TODO: 轉換為 Fill model
                pass

    async def place_order(self, symbol, direction, order_type, qty, price=0.0) -> str:
        import shioaji as sj

        contract = self._get_contract(symbol)
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
        # TODO: 實作
        return []

    def _get_contract(self, symbol):
        SYMBOL_MAP = {"TX": "TXF", "MTX": "MXF", "TMF": "TMF"}
        sj_symbol = SYMBOL_MAP.get(symbol, symbol)
        try:
            return self._api.Contracts.Futures[sj_symbol][sj_symbol + "R1"]
        except (KeyError, AttributeError):
            return None
