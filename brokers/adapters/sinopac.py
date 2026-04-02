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

    async def connect(self, **credentials) -> bool:
        """
        credentials:
            api_key: str
            secret_key: str
            person_id: str (可選, 憑證登入用)
        """
        try:
            import shioaji as sj

            self._api = sj.Shioaji()
            self._api.login(
                api_key=credentials.get("api_key", ""),
                secret_key=credentials.get("secret_key", ""),
            )
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
        """訂閱逐筆成交"""
        self._tick_callbacks[symbol] = callback
        contract = self._get_contract(symbol)
        if contract:
            self._api.quote.subscribe(
                contract,
                quote_type="tick",
                version="v1",
            )

            @self._api.on_tick_fop_v1()
            def on_tick(exchange, tick):
                t = Tick(
                    symbol=symbol,
                    price=tick.close,
                    volume=tick.volume,
                    timestamp=datetime.fromtimestamp(tick.datetime / 1e9),
                    buy_price=tick.bid_price,
                    sell_price=tick.ask_price,
                )
                cb = self._tick_callbacks.get(symbol)
                if cb:
                    cb(t)

    async def subscribe_orderbook(self, symbol: str, callback: Callable[[OrderBook], None]) -> None:
        """訂閱五檔"""
        self._book_callbacks[symbol] = callback
        contract = self._get_contract(symbol)
        if contract:
            self._api.quote.subscribe(
                contract,
                quote_type="bidask",
                version="v1",
            )

            @self._api.on_bidask_fop_v1()
            def on_bidask(exchange, bidask):
                book = OrderBook(
                    symbol=symbol,
                    timestamp=datetime.now(),
                    bids=[
                        OrderBookLevel(price=getattr(bidask, f"bid_price_{i+1}", 0),
                                       qty=getattr(bidask, f"bid_volume_{i+1}", 0))
                        for i in range(5)
                    ],
                    asks=[
                        OrderBookLevel(price=getattr(bidask, f"ask_price_{i+1}", 0),
                                       qty=getattr(bidask, f"ask_volume_{i+1}", 0))
                        for i in range(5)
                    ],
                )
                cb = self._book_callbacks.get(symbol)
                if cb:
                    cb(book)

    async def unsubscribe(self, symbol: str) -> None:
        contract = self._get_contract(symbol)
        if contract:
            self._api.quote.unsubscribe(contract, quote_type="tick")
            self._api.quote.unsubscribe(contract, quote_type="bidask")
        self._tick_callbacks.pop(symbol, None)
        self._book_callbacks.pop(symbol, None)

    async def get_history_bars(self, symbol: str, timeframe: Timeframe, count: int = 200) -> list[Bar]:
        """從永豐金取得歷史K線"""
        contract = self._get_contract(symbol)
        if not contract:
            return []

        try:
            kbars = self._api.kbars(
                contract=contract,
                start="2024-01-01",  # TODO: 動態計算起始日期
                end=datetime.now().strftime("%Y-%m-%d"),
            )
            bars = []
            for i in range(len(kbars.Close)):
                bars.append(Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    timestamp=datetime.fromtimestamp(kbars.ts[i] / 1e9),
                    open=kbars.Open[i],
                    high=kbars.High[i],
                    low=kbars.Low[i],
                    close=kbars.Close[i],
                    volume=kbars.Volume[i],
                    is_closed=True,
                ))
            return bars[-count:]
        except Exception:
            logger.exception("[SinoPac] 取得歷史K線失敗")
            return []

    def _get_contract(self, symbol: str):
        """將系統代碼轉換為 Shioaji contract"""
        SYMBOL_MAP = {"TX": "TXF", "MTX": "MXF", "TE": "EXF", "TF": "FXF"}
        sj_symbol = SYMBOL_MAP.get(symbol, symbol)
        try:
            return self._api.Contracts.Futures[sj_symbol]["HOT"]
        except (KeyError, AttributeError):
            logger.warning(f"[SinoPac] 找不到合約: {symbol} → {sj_symbol}")
            return None


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
        SYMBOL_MAP = {"TX": "TXF", "MTX": "MXF", "TE": "EXF", "TF": "FXF"}
        sj_symbol = SYMBOL_MAP.get(symbol, symbol)
        try:
            return self._api.Contracts.Futures[sj_symbol]["HOT"]
        except (KeyError, AttributeError):
            return None
