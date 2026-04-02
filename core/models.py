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
        multiplier = 1 if self.side == PositionSide.LONG else -1
        point_value = self._get_point_value()
        return multiplier * (self.current_price - self.avg_price) * self.qty * point_value

    def _get_point_value(self) -> float:
        """每點價值 (台指期=200, 小台指=50, 電子期=4000, 金融期=1000)"""
        POINT_VALUES = {
            "TX": 200, "MTX": 50, "TE": 4000, "TF": 1000,
            "TXO": 50,  # 台指選擇權
        }
        return POINT_VALUES.get(self.symbol, 200)


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


@dataclass
class IndicatorOutput:
    """指標計算結果 (供繪圖)"""
    name: str                          # 指標名稱
    series: dict[str, list[float]]     # {"ma5": [...], "ma20": [...]}
    overlays: bool = True              # True=疊在K線上, False=獨立子圖
    colors: dict[str, str] = field(default_factory=dict)  # {"ma5": "#f59e0b"}


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
