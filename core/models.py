"""
core/models.py — 共用資料模型
所有模塊使用的基礎資料結構，確保系統內部格式統一。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ═══════════════════════════════════════════════════════════
#  列舉型別
# ═══════════════════════════════════════════════════════════

class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"          # 限價單
    MARKET = "market"        # 市價單
    STOP_BUY = "stop_buy"    # 觸價買 (價格到達後以市價買進)
    STOP_SELL = "stop_sell"  # 觸價賣 (價格到達後以市價賣出)


class OrderStatus(str, Enum):
    PENDING = "pending"        # 委託中
    SUBMITTED = "submitted"    # 已送出
    PARTIAL = "partial"        # 部分成交
    FILLED = "filled"          # 完全成交
    CANCELLED = "cancelled"    # 已取消
    REJECTED = "rejected"      # 被拒絕
    STOP_WAITING = "stop_wait" # 觸價單等待觸發中


class PositionSide(str, Enum):
    LONG = "long"    # 多方
    SHORT = "short"  # 空方


class ConditionStatus(str, Enum):
    """條件單狀態（右邊下單）。主線六態對應前端的六個燈號，見 ARCHITECTURE.md §7.3"""
    WAITING = "waiting"        # 等待觸發
    TRIGGERED = "triggered"    # 已觸發（進場單即將送出）
    SENT = "sent"              # 已送單（追價單在券商端）
    FILLED = "filled"          # 已成交（進場完成）
    GUARDED = "guarded"        # 已守成本（P3）
    EXITED = "exited"          # 已出場（P2）
    # 支線
    CANCELLED = "cancelled"    # 使用者刪除
    FAILED = "failed"          # 進場單被券商拒絕（不自動重試）
    ORPHANED = "orphaned"      # 重啟後對不上倉位，等人工確認（P4）


class Timeframe(str, Enum):
    TICK = "tick"
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    D1 = "1d"
    W1 = "1w"
    MO = "1M"


class ScriptType(str, Enum):
    INDICATOR = "indicator"
    STRATEGY = "strategy"


class PanelType(str, Enum):
    MAIN = "main"
    VOLUME = "volume"
    SUB = "sub"


# ═══════════════════════════════════════════════════════════
#  市場資料
# ═══════════════════════════════════════════════════════════

@dataclass
class Tick:
    """逐筆成交"""
    symbol: str
    price: float
    volume: int
    timestamp: datetime
    buy_price: float = 0.0   # 最佳買價
    sell_price: float = 0.0  # 最佳賣價
    change: float = 0.0      # 漲跌金額（指數用）
    change_pct: float = 0.0  # 漲跌幅 %（指數用）


@dataclass
class Bar:
    """K棒 (OHLCV)"""
    symbol: str
    timeframe: Timeframe
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    delivery: str = ""       # 到期月份，例如 "202412"
    is_closed: bool = False  # 該根K棒是否已收完


@dataclass
class OrderBookLevel:
    """單一價位的委託量"""
    price: float
    qty: int


@dataclass
class OrderBook:
    """五檔報價 (或更多檔)"""
    symbol: str
    timestamp: datetime
    bids: list[OrderBookLevel] = field(default_factory=list)  # 買方 (價高→低)
    asks: list[OrderBookLevel] = field(default_factory=list)  # 賣方 (價低→高)
    last_price: float = 0.0
    last_qty: int = 0


# ═══════════════════════════════════════════════════════════
#  委託 / 成交 / 倉位
# ═══════════════════════════════════════════════════════════

@dataclass
class Order:
    """委託單"""
    id: str                         # 內部委託ID
    symbol: str
    direction: Direction
    order_type: OrderType
    price: float                    # 限價 or 觸價價格 (市價單為0)
    qty: int
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    broker_order_id: str = ""       # 券商端的委託序號
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    source: str = "manual"          # "manual" | "script:{name}" | "stop"
    reject_reason: str = ""         # 被拒絕的原因（券商回的訊息），給前端顯示用

    @property
    def remaining_qty(self) -> int:
        return self.qty - self.filled_qty

    @property
    def is_active(self) -> bool:
        return self.status in (
            OrderStatus.PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIAL,
            OrderStatus.STOP_WAITING,
        )


@dataclass
class Fill:
    """成交回報"""
    order_id: str
    symbol: str
    direction: Direction
    price: float
    qty: int
    fee: float
    timestamp: datetime
    broker_fill_id: str = ""
    # 以下三欄不是券商給的，是 FillLedger 依成交順序推算出來的（見 core/fill_ledger.py）。
    # 券商的成交回報只有「買/賣」，看不出這一筆是進場還是出場，更沒有損益。
    oc_type: str = ""       # "" 未判定 / "new" 新倉 / "cover" 平倉 / "cover_new" 平倉反手
    closed_qty: int = 0     # 這筆成交裡平掉的口數（新倉為 0）
    pnl: Optional[float] = None   # 平倉的已實現損益（未扣手續費）；None = 新倉或成本不明


@dataclass
class Position:
    """倉位"""
    symbol: str
    side: PositionSide
    qty: int
    avg_price: float
    current_price: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        if self.current_price <= 0:
            return 0.0  # 還沒收到報價，避免用 0 當現價算出整筆倉位的假虧損
        multiplier = 1 if self.side == PositionSide.LONG else -1
        return multiplier * (self.current_price - self.avg_price) * self.qty * self.point_value

    @property
    def point_value(self) -> float:
        """每點價值。前端要自己用即時報價算浮動損益，所以隨倉位一起送出去。"""
        return self._get_point_value()

    def _get_point_value(self) -> float:
        return point_value(self.symbol)


@dataclass
class Condition:
    """條件單（右邊下單）— 壓力空 / 支撐多

    語意見 ARCHITECTURE.md §7。P1 只用到觸發與進場相關欄位，
    出場欄位（利點/損點/成本防線/觸後跟隨）先存下來，由 P2/P3 使用。
    """
    id: str
    symbol: str
    side: Direction              # SELL = 壓力空（漲到壓力放空）、BUY = 支撐多（跌到支撐作多）
    trigger_price: float
    chase: int = 0               # 追點 — 進場單穿價的點數（= 可接受的滑價上限）
    qty: int = 1
    take_profit: int = 0         # 利點，0 = 不設（P2）
    stop_loss: int = 0           # 損點，前端以負數輸入，0 = 不設（P2）
    cost_guard: bool = False     # 成本防線（P3）
    trail: bool = False          # 觸後跟隨（P3）
    status: ConditionStatus = ConditionStatus.WAITING
    entry_order_id: str = ""     # 進場委託的內部 id
    entry_price: float = 0.0     # 進場成交均價（出場計算一律以此為基準，不是觸發價）
    entry_filled_qty: int = 0
    exit_order_id: str = ""      # P2
    peak_price: float = 0.0      # 觸後跟隨用：進場後最有利價（P3）
    fail_reason: str = ""        # 券商拒絕原因，給前端顯示
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    @property
    def limit_price(self) -> float:
        """進場的穿價限價。

        賣單掛得比觸發價低、買單掛得比觸發價高，這樣一觸發就會立刻成交，
        但最差成交價被鎖在 chase 點以內 —— 這是追點的意義，不是掛單偏移量。
        用市價單的話滑價無上限（見 ARCHITECTURE.md §7.4）。
        """
        return (
            self.trigger_price - self.chase if self.side == Direction.SELL
            else self.trigger_price + self.chase
        )

    def is_hit(self, price: float) -> bool:
        """市價是否觸及觸發價。

        方向跟 STOP_BUY / STOP_SELL 相反：右邊下單是在壓力/支撐逆勢接單，
        壓力空要「漲上去」才觸發，支撐多要「跌下來」才觸發。
        """
        return price >= self.trigger_price if self.side == Direction.SELL else price <= self.trigger_price

    @property
    def is_waiting(self) -> bool:
        return self.status == ConditionStatus.WAITING

    @property
    def has_entry(self) -> bool:
        """是否已經（或可能已經）在券商端建立部位 —— 刪除這種條件不會平倉。"""
        return self.status in (
            ConditionStatus.SENT, ConditionStatus.FILLED,
            ConditionStatus.GUARDED, ConditionStatus.ORPHANED,
        )


# 每點價值 (台指期=200, 小台指=50, 電子期=4000, 金融期=1000)
POINT_VALUES = {
    "TX": 200, "MTX": 50, "TE": 4000, "TF": 1000,
    "TMF": 10,  # 微型台指
    "TXO": 50,  # 台指選擇權
}


def point_value(symbol: str) -> float:
    """每點價值。倉位的浮動損益與成交明細的已實現損益共用同一份對照表。"""
    return POINT_VALUES.get(symbol, 200)


# ═══════════════════════════════════════════════════════════
#  Script 相關
# ═══════════════════════════════════════════════════════════

@dataclass
class ScriptMeta:
    """Script 元資訊"""
    id: str
    name: str
    script_type: ScriptType
    description: str = ""
    version: str = "1.0"
    author: str = ""
    enabled: bool = False
    file_path: str = ""
    parameters: dict = field(default_factory=dict)  # 可調參數 & 預設值
    # 設定後，該 script 除了在 M1 棒收完時執行，也會依此秒數定時額外執行一次
    # （例如報價語音播報想要比 1 分鐘更頻繁）。None = 只在 M1 棒收完時執行。
    interval_sec: Optional[int] = None
    last_modified: float = 0.0


@dataclass
class IndicatorSeries:
    values: list[float]
    color: str = "#3b82f6"
    panel: PanelType = PanelType.MAIN

@dataclass
class IndicatorOutput:
    """指標計算結果 (供繪圖)"""
    name: str
    series: dict[str, dict]  # e.g. {"ma5": {"values": [...], "color": "#f59e0b", "panel": "main"}}
    # script 自己判斷條件成立時要播報的文字（見 ScriptContext.alert）。
    # 播放與否、播放什麼完全由 script 決定，前端只負責照著念。
    alerts: list[str] = field(default_factory=list)


@dataclass
class StrategySignal:
    """策略訊號"""
    script_name: str
    direction: Direction
    qty: int
    price: float = 0.0       # 0=市價
    order_type: OrderType = OrderType.MARKET
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.now)


# ═══════════════════════════════════════════════════════════
#  回測相關
# ═══════════════════════════════════════════════════════════

@dataclass
class BacktestConfig:
    """回測設定"""
    strategy_id: str
    symbol: str
    timeframe: Timeframe
    start_date: datetime
    end_date: datetime
    initial_capital: float = 1_000_000
    commission: float = 60.0          # 每口手續費
    slippage_ticks: int = 1
    parameters: dict = field(default_factory=dict)


@dataclass
class BacktestResult:
    """回測結果"""
    config: BacktestConfig
    total_return: float       # 總報酬率 %
    max_drawdown: float       # 最大回撤 %
    sharpe_ratio: float
    win_rate: float
    total_trades: int
    profit_factor: float
    equity_curve: list[float]  # 權益曲線
    trades: list[Fill]         # 所有成交紀錄
    duration_seconds: float    # 回測執行時間
