"""
brokers/base.py — 券商抽象基底類
所有券商 adapter 必須實作這兩個介面。
問價 (QuoteAdapter) 與交易 (TradeAdapter) 完全分離。
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Callable

from core.models import (
    Bar, Direction, Fill, Order, OrderBook, OrderType,
    Position, Tick, Timeframe,
)


class QuoteAdapter(ABC):
    """
    問價 Adapter 介面

    負責：連線、登入、訂閱即時報價、取得歷史K線。
    不負責：下單、查倉位。
    """

    name: str = "base"

    # ── 連線管理 ──────────────────────────────────────

    @abstractmethod
    async def connect(self, **credentials) -> bool:
        """連線 & 登入。回傳是否成功。"""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """斷線"""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    # ── 即時報價 ──────────────────────────────────────

    @abstractmethod
    async def subscribe_tick(
        self, symbol: str, callback: Callable[[Tick], None]
    ) -> None:
        """訂閱逐筆成交"""
        ...

    @abstractmethod
    async def subscribe_orderbook(
        self, symbol: str, callback: Callable[[OrderBook], None]
    ) -> None:
        """訂閱五檔 (或更多檔) 委託簿"""
        ...

    @abstractmethod
    async def unsubscribe(self, symbol: str) -> None:
        """取消訂閱"""
        ...

    # ── 歷史資料 ──────────────────────────────────────

    @abstractmethod
    async def get_history_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int = 200,
    ) -> list[Bar]:
        """取得歷史K線 (從券商API)"""
        ...

    # ── 選擇權資料 (Options) ──────────────────────────

    @abstractmethod
    async def get_options_months(self, symbol: str = "TXO") -> list[str]:
        """取得可交易的選擇權到期月份清單"""
        ...

    @abstractmethod
    async def get_options_t_quote(
        self, symbol: str, month: str, spot_price: float = 0.0, trading_dates: list[str] | None = None,
    ) -> list[dict]:
        """取得指定月份的所有選擇權 T 字報價 (含 Call/Put 快照)，一次性請求式查詢。

        spot_price > 0 時，會額外用 Black-76 反推 ATM 隱含波動率，
        套用到每個履約價算出理論價，回傳 callPremium/putPremium = 市價 - 理論價。
        trading_dates 是交易日曆，用來把到期時間 T 精算到實際交易分鐘數。

        注意：這是請求式查詢，不適合被前端反覆輪詢當即時 feed 用（永豐金文件明講
        snapshots 這類查詢重複輪詢會被停權）。即時更新請改用下面的訂閱式方法。
        """
        ...

    async def subscribe_options_t_quote(
        self, symbol: str, month: str, callback: Callable[[list[dict]], None],
        spot_price: float = 0.0, trading_dates: list[str] | None = None,
    ) -> None:
        """訂閱指定月份選擇權鏈的即時報價（call+put 全履約價），透過 callback 持續推送
        更新後的完整鏈快照（含理論價，若支援）。取代 get_options_t_quote 的輪詢用法。
        預設不支援（回傳即結束），依券商而定。
        """
        return

    async def update_options_spot_price(self, symbol: str, month: str, spot_price: float) -> None:
        """更新某條已訂閱選擇權鏈用於理論價計算的現貨/期貨價，不需要重新訂閱。"""
        return

    async def unsubscribe_options_t_quote(self, symbol: str, month: str) -> None:
        """取消訂閱指定月份選擇權鏈的即時報價。"""
        return

    # ── 其他行情查詢（選配，預設不支援）────────────────

    async def get_ticks(
        self,
        symbol: str,
        date: str = "",
        query_type: str = "AllDay",
        time_start: str = "",
        time_end: str = "",
        last_count: int = 0,
    ) -> list[dict]:
        """取得歷史逐筆成交明細。

        query_type: "AllDay"（全日）| "RangeTime"（time_start~time_end）| "LastCount"（最後 last_count 筆）
        每筆為 dict: {ts, close, volume, bid_price, bid_volume, ask_price, ask_volume, tick_type}
        """
        return []

    async def get_snapshot(self, symbols: list[str]) -> list[dict]:
        """取得多商品的即時快照（開高低收、量、漲跌幅等）。

        注意：這是請求式查詢，不可當即時 feed 反覆輪詢（券商會停權），
        即時報價請用 subscribe_tick / subscribe_orderbook。
        """
        return []

    async def get_contract_info(self, symbol: str) -> dict:
        """取得合約規格（代碼、名稱、到期日、漲跌停、參考價等）。"""
        return {}

    async def get_api_usage(self) -> dict:
        """查詢 API 流量用量: {bytes, limit_bytes, remaining_bytes, connections}"""
        return {}


class TradeAdapter(ABC):
    """
    交易 Adapter 介面

    負責：下單、刪單、查詢委託/成交/倉位。
    不負責：報價。
    """

    name: str = "base"

    # 最近一次下單/刪改單失敗的原因（券商回的原始訊息）。
    # place_order 失敗只回空字串，光看回傳值無從得知是保證金不足、帳號未簽署還是斷線，
    # 上層要拿得到原因才能顯示在畫面上，不然使用者只會看到一句沒資訊量的「下單失敗」。
    last_error: str = ""

    # ── 連線管理 ──────────────────────────────────────

    @abstractmethod
    async def connect(self, **credentials) -> bool:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    # ── 下單 ──────────────────────────────────────────

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        direction: Direction,
        order_type: OrderType,
        qty: int,
        price: float = 0.0,
        octype: str = "auto",
        time_in_force: str = "ROD",
    ) -> str:
        """
        送出委託。回傳券商端的委託序號 (broker_order_id)。
        price=0 表示市價單。

        **失敗一律回空字串**，並把原因寫進 self.last_error（上層以「空字串 = 沒送出去」
        判定委託被拒絕，所以拿不到委託序號時絕不能回傳假的成功）。

        octype:        "auto" | "new"（新倉）| "cover"（平倉）| "daytrade"（當沖）
        time_in_force: "ROD"（當日有效）| "IOC"（立即成交否則取消）| "FOK"（全部成交否則取消）
        兩者皆為選配，不支援的券商可忽略。
        """
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """取消委託。回傳是否成功。"""
        ...

    @abstractmethod
    async def modify_order(
        self, broker_order_id: str, new_price: float = 0, new_qty: int = 0
    ) -> bool:
        """改價/改量"""
        ...

    # ── 回報回調 ──────────────────────────────────────

    @abstractmethod
    def set_on_order_update(self, callback: Callable[[Order], None]) -> None:
        """設定委託回報 callback (狀態變更)"""
        ...

    @abstractmethod
    def set_on_fill(self, callback: Callable[[Fill], None]) -> None:
        """設定成交回報 callback"""
        ...

    # ── 查詢 ──────────────────────────────────────────

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """查詢當前倉位"""
        ...

    @abstractmethod
    async def get_open_orders(self) -> list[Order]:
        """查詢未成交委託"""
        ...

    async def get_orders_today(self) -> list[Order]:
        """查詢今日所有委託，含已成交／已刪單。

        給上層跟本地委託簿對帳用：市價單成交後券商不一定會再送一次委託回報，
        本地那張單會一直停在「委託中」，得靠這裡的狀態把它修正回來。
        預設不支援（回傳空列表），依券商而定。
        """
        return []

    @abstractmethod
    async def get_fills_today(self) -> list[Fill]:
        """查詢今日成交明細"""
        ...

    async def get_profit_loss_today(self) -> list[dict]:
        """查詢今日已實現損益（依券商而定，非所有券商都支援，預設回傳空列表）

        每筆為 dict: {symbol, quantity, cover_price, pnl, fee, tax}，
        用於比對成交明細中的平倉成交，補上該筆的已實現損益。
        """
        return []

    # ── 帳務查詢（選配，預設不支援）────────────────────

    @property
    def is_simulation(self) -> bool:
        """是否為模擬（測試）帳號。前端用來提示「目前是模擬交易」。"""
        return False

    async def list_accounts(self) -> list[dict]:
        """列出登入帳號下的所有交易帳戶。

        每筆為 dict: {account_id, account_type, broker_id, person_id, signed, username}
        """
        return []

    async def get_account_balance(self) -> dict:
        """查詢帳戶餘額（證券交割款），回傳 {balance, date}"""
        return {}

    async def get_margin(self) -> dict:
        """查詢期貨保證金專戶。

        回傳 dict（欄位依券商而定），常用: {equity, available_margin,
        initial_margin, maintenance_margin, risk_indicator, today_balance, ...}
        """
        return {}

    async def get_position_detail(self, detail_id: int = 0) -> list[dict]:
        """查詢倉位的逐筆進場明細。detail_id=0 表示全部。"""
        return []

    async def get_settlements(self) -> list[dict]:
        """查詢交割款（T/T+1/T+2）"""
        return []

    async def get_profit_loss(self, begin_date: str = "", end_date: str = "") -> list[dict]:
        """查詢區間已實現損益（日期格式 'YYYY-MM-DD'，空字串=今日）"""
        return []

    async def get_profit_loss_summary(self, begin_date: str = "", end_date: str = "") -> list[dict]:
        """查詢區間已實現損益彙總（依商品彙總）"""
        return []

    async def get_profit_loss_detail(self, detail_id: int = 0) -> list[dict]:
        """查詢單筆已實現損益的進場明細（detail_id 來自 get_profit_loss 的 id）"""
        return []
