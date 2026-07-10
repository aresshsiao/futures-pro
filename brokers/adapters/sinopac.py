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

_SHARED_API = None
_SHARED_CONNECTED = False

def _get_shared_api(credentials):
    global _SHARED_API, _SHARED_CONNECTED
    import shioaji as sj
    if _SHARED_API is None:
        _SHARED_API = sj.Shioaji()
    if not _SHARED_CONNECTED:
        _SHARED_API.login(
            api_key=credentials.get("api_key", ""),
            secret_key=credentials.get("secret_key", ""),
            subscribe_trade=credentials.get("subscribe_trade", True),
            receive_window=10000
        )
        if "cert_path" in credentials:
            _SHARED_API.activate_ca(
                ca_path=credentials["cert_path"],
                ca_passwd=credentials.get("cert_password", ""),
                person_id=credentials.get("person_id", ""),
            )
        _SHARED_CONNECTED = True
    return _SHARED_API

def _logout_shared_api():
    global _SHARED_API, _SHARED_CONNECTED
    if _SHARED_API is not None and _SHARED_CONNECTED:
        try:
            _SHARED_API.logout()
        except Exception:
            pass
        _SHARED_CONNECTED = False



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
                # 僅處理加權指數 Y9999
                if tick.code != "Y9999":
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
        contract = self._get_contract(symbol)
        if contract:
            self._api.quote.subscribe(contract, quote_type="tick", version="v1")
            self._subscribed.add(symbol)
            logger.info("[SinoPac] 已訂閱 tick: %s", symbol)

    async def subscribe_orderbook(self, symbol: str, callback: Callable[[OrderBook], None]) -> None:
        """訂閱五檔（同一 symbol 只發一次訂閱請求）"""
        if symbol == "TAIEX":
            return  # 指數無五檔資料
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

    def _get_contract(self, symbol: str):
        """將系統代碼轉換為 Shioaji contract"""
        if symbol == "TAIEX":
            try:
                return self._api.Contracts.Indexs.TSE["Y9999"]
            except (KeyError, AttributeError):
                logger.warning("[SinoPac] 找不到加權指數合約 (Y9999)")
                return None
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

            self._api = _get_shared_api(credentials)

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
                from core.models import Fill, Direction
                trade_id = msg.get('trade_id', '')
                action = msg.get('action', '')
                code = msg.get('code', '')
                price = msg.get('price', 0.0)
                qty = msg.get('quantity', 0)
                
                symbol = self._code_to_symbol(code) or code
                direction = Direction.BUY if action == 'Buy' else Direction.SELL
                
                f = Fill(
                    broker_order_id=trade_id,
                    symbol=symbol,
                    direction=direction,
                    qty=qty,
                    price=price,
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
