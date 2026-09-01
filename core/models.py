"""
core/models.py — 共用資料模型
所有模塊使用的基礎資料結構，確保系統內部格式統一。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from config import settings


# ═══════════════════════════════════════════════════════════
#  列舉型別
# ═══════════════════════════════════════════════════════════

class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"                  # 限價單
    MARKET = "market"                # 市價單
    MARKET_RANGE = "market_range"    # 範圍市價單（券商端 MKP，成交價限制在保護範圍內）
    STOP_BUY = "stop_buy"            # 觸價買 (價格到達後以市價買進)
    STOP_SELL = "stop_sell"          # 觸價賣 (價格到達後以市價賣出)


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
    SENT = "sent"              # 已送單（回檔進場單在券商端）
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
    pullback: int = 0            # 返點 — 從極值回檔幾點才進場（0 = 碰到觸發價就進）
    qty: int = 1
    take_profit: int = 0         # 利點，0 = 不設
    stop_loss: int = 0           # 損點，前端以負數輸入，0 = 不設
    cost_guard: bool = False     # 成本防線
    trail: bool = False          # 觸後跟隨 — 觸發後繼續追極值，進場價跟著極值走
    trigger_extreme: float = 0.0  # 觸發後追到的極值（壓力空取最高、支撐多取最低）
    status: ConditionStatus = ConditionStatus.WAITING
    entry_order_id: str = ""     # 進場委託的內部 id
    entry_price: float = 0.0     # 進場成交均價（出場計算一律以此為基準，不是觸發價）
    entry_filled_qty: int = 0
    exit_order_id: str = ""      # 出場委託的內部 id
    exit_price: float = 0.0      # 出場成交均價
    exit_reason: str = ""        # "take_profit" | "stop_loss" | "cost_guard" | "session_close"
    peak_price: float = 0.0      # 成本防線用：進場後最有利價
    fail_reason: str = ""        # 券商拒絕原因，給前端顯示
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    # ── 進場：觸發 → 回檔確認 ───────────────────────
    # 兩段式。碰到觸發價只是「開始盯」，要等價格從極值回檔「返點」才真的進場——
    # 壓力空在壓力區碰一下就放空常常是接刀，等它回頭才確認賣壓真的存在。

    def is_hit(self, price: float) -> bool:
        """市價是否觸及觸發價（進入盯盤狀態）。

        方向跟 STOP_BUY / STOP_SELL 相反：右邊下單是在壓力/支撐逆勢接單，
        壓力空要「漲上去」才觸發，支撐多要「跌下來」才觸發。
        """
        return price >= self.trigger_price if self.side == Direction.SELL else price <= self.trigger_price

    def update_extreme(self, price: float) -> bool:
        """觸發後追蹤極值（壓力空取最高、支撐多取最低）。回傳極值是否被推進。

        沒開「觸後跟隨」時極值固定在觸發當下，進場價因此是固定的
        `觸發價 ∓ 返點`；開了才會跟著行情繼續走。
        """
        base = self.trigger_extreme or self.trigger_price
        further = price > base if self.side == Direction.SELL else price < base
        if not self.trigger_extreme:
            self.trigger_extreme = max(price, self.trigger_price) if self.side == Direction.SELL \
                else min(price, self.trigger_price)
            return True
        if self.trail and further:
            self.trigger_extreme = price
            return True
        return False

    @property
    def entry_target_price(self) -> float:
        """回檔進場價（畫面上的「掛單價」）= 極值 ∓ 返點。

        壓力空：最高價 − 返點（跌回來才空）；支撐多：最低價 + 返點（彈回來才多）。

        注意這是「價格走到這裡就送進場單」的門檻，不是委託上的限價 ——
        進場走一定範圍市價（見 ConditionModule._enter_position）。
        """
        base = self.trigger_extreme or self.trigger_price
        return base - self.pullback if self.side == Direction.SELL else base + self.pullback

    def entry_hit(self, price: float) -> bool:
        """價格是否已從極值回檔到進場價。"""
        target = self.entry_target_price
        return price <= target if self.side == Direction.SELL else price >= target

    # ── 出場（P2）──────────────────────────────────
    # 一律以 entry_price（實際成交均價）為基準，不是觸發價：
    # 成交價未必等於掛單價，用觸發價算的停損跟真實部位的成本對不起來。

    @property
    def take_profit_price(self) -> float:
        """停利價。0 = 未設停利或還沒進場。"""
        if not self.take_profit or not self.entry_price:
            return 0.0
        tp = abs(self.take_profit)
        return self.entry_price + tp if self.side == Direction.BUY else self.entry_price - tp

    @property
    def stop_loss_price(self) -> float:
        """固定停損價。損點前端以負數輸入，這裡一律取絕對值往不利方向擺。"""
        if not self.stop_loss or not self.entry_price:
            return 0.0
        sl = abs(self.stop_loss)
        return self.entry_price - sl if self.side == Direction.BUY else self.entry_price + sl

    @property
    def cost_guard_threshold(self) -> float:
        """成本防線的啟動門檻（浮盈點數）。用損點而不是利點：
        賺到「夠賠的量」就先立於不敗，不必等到接近停利才保本。"""
        return abs(self.stop_loss) if (self.cost_guard and self.stop_loss) else 0.0

    def best_profit(self) -> float:
        """進場後看過的最大浮盈點數（以 peak_price 計，只增不減）。"""
        if not self.entry_price or not self.peak_price:
            return 0.0
        return (
            self.peak_price - self.entry_price if self.side == Direction.BUY
            else self.entry_price - self.peak_price
        )

    @property
    def active_stop_price(self) -> float:
        """實際生效的停損價 —— 固定停損與保本取「最保護」的那一個。

        多單的停損在下方，愈高愈保護（取 max）；空單反之（取 min）。
        """
        candidates = []
        if self.stop_loss_price:
            candidates.append(self.stop_loss_price)
        # 成本防線一旦啟動（狀態進 guarded）就固定守在進場價
        if self.status == ConditionStatus.GUARDED and self.entry_price:
            candidates.append(self.entry_price)
        if not candidates:
            return 0.0
        return max(candidates) if self.side == Direction.BUY else min(candidates)

    @property
    def exit_direction(self) -> Direction:
        """出場方向 —— 進場的反向。"""
        return Direction.SELL if self.side == Direction.BUY else Direction.BUY

    @property
    def stop_kind(self) -> str:
        """目前生效的停損是哪一種 —— 出場後要看得出來是被什麼掃到的。"""
        stop = self.active_stop_price
        if not stop:
            return ""
        if self.status == ConditionStatus.GUARDED and stop == self.entry_price:
            return "cost_guard"
        return "stop_loss"

    def exit_hit(self, price: float) -> Optional[tuple[str, float]]:
        """檢查現價是否觸及停利/停損，回傳 (原因, 觸發價) 或 None。

        停損用 active_stop_price（含保本與移動停損），不是原始的固定停損價。

        跳空時兩邊可能同一筆 tick 都成立（例如多單開盤直接跳過停損又衝過停利），
        這時一律先認停損 —— 中間的路徑看不到，假設走過最不利的那一邊才安全。
        """
        sl_price = self.active_stop_price
        tp_price = self.take_profit_price
        if self.side == Direction.BUY:
            if sl_price and price <= sl_price:
                return (self.stop_kind, sl_price)
            if tp_price and price >= tp_price:
                return ("take_profit", tp_price)
        else:
            if sl_price and price >= sl_price:
                return (self.stop_kind, sl_price)
            if tp_price and price <= tp_price:
                return ("take_profit", tp_price)
        return None

    @property
    def is_waiting(self) -> bool:
        return self.status == ConditionStatus.WAITING

    @property
    def is_holding(self) -> bool:
        """已進場、還在管理出場的狀態。"""
        return self.status in (ConditionStatus.FILLED, ConditionStatus.GUARDED)

    @property
    def has_entry(self) -> bool:
        """是否已經（或可能已經）在券商端建立部位 —— 刪除這種條件不會平倉。"""
        return self.status in (
            ConditionStatus.SENT, ConditionStatus.FILLED,
            ConditionStatus.GUARDED, ConditionStatus.ORPHANED,
        )


# 商品規格一律走 config/settings.yaml —— 這幾張表以前在 settings、models、
# backtest/engine 各存一份，三份還互有出入（小台手續費、微型台指每點價值）。
def point_value(symbol: str) -> float:
    """每點價值。倉位的浮動損益與成交明細的已實現損益共用同一份對照表。"""
    return settings.POINT_VALUE.get(symbol, settings.POINT_VALUE_DEFAULT)


def tick_size(symbol: str) -> float:
    """最小跳動點。回測的滑價以「跳」為單位，要靠它換算成點數。"""
    return settings.TICK_SIZE.get(symbol, settings.TICK_SIZE_DEFAULT)


def commission_per_lot(symbol: str) -> float:
    """每口手續費。回測用；實單的費用以券商回報為準。"""
    return settings.COMMISSION_PER_LOT.get(symbol, settings.COMMISSION_PER_LOT_DEFAULT)


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
    initial_capital: float = field(default_factory=lambda: settings.BACKTEST_DEFAULT_CAPITAL)
    # 0 = 用該商品的預設手續費（settings.yaml 的 trading.commission_per_lot）。
    # dataclass 的預設值算不到同一個 dataclass 的 symbol 欄位，所以由引擎在
    # 真的要算費用時才解析（見 BacktestEngine._commission）。
    commission: float = 0.0
    slippage_ticks: int = field(
        default_factory=lambda: settings.BACKTEST_DEFAULT_SLIPPAGE_TICKS
    )
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
