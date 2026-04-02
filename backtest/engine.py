"""
backtest/engine.py — 回測引擎
載入歷史資料，模擬 Script 策略的交易表現。
"""
from __future__ import annotations
import logging
import time
from datetime import datetime

import pandas as pd

from core.models import (
    BacktestConfig, BacktestResult, Bar, Direction, Fill,
    OrderType, PositionSide, Timeframe,
)
from data.database import Database
from scripts.engine import ScriptEngine, ScriptContext

logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    回測引擎

    流程:
      1. 載入歷史 Bar 資料
      2. 逐根 Bar 餵入策略 Script
      3. Script 產生訊號 → 模擬成交
      4. 追蹤權益變動
      5. 計算績效指標
    """

    def __init__(self, db: Database, script_engine: ScriptEngine):
        self._db = db
        self._script_engine = script_engine

    def run(self, config: BacktestConfig) -> BacktestResult:
        """執行回測"""
        start_time = time.time()
        logger.info(
            f"[Backtest] 開始: {config.strategy_id} | {config.symbol} "
            f"| {config.start_date} ~ {config.end_date}"
        )

        # 1. 載入歷史資料
        bars = self._db.get_bars(
            config.symbol, config.timeframe,
            start=config.start_date, end=config.end_date,
            limit=99999,
        )
        if len(bars) < 20:
            logger.error("[Backtest] 資料不足")
            return self._empty_result(config, time.time() - start_time)

        # 2. 建立 DataFrame
        df = pd.DataFrame([
            {"open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "volume": b.volume, "timestamp": b.timestamp}
            for b in bars
        ])

        # 3. 逐 Bar 回測
        capital = config.initial_capital
        equity_curve = [capital]
        fills: list[Fill] = []
        position_qty = 0
        position_side: PositionSide | None = None
        avg_price = 0.0
        wins = 0
        losses = 0
        gross_profit = 0.0
        gross_loss = 0.0

        meta = self._script_engine._scripts.get(config.strategy_id)
        module = self._script_engine._modules.get(config.strategy_id)
        if not meta or not module:
            logger.error(f"[Backtest] 找不到策略: {config.strategy_id}")
            return self._empty_result(config, time.time() - start_time)

        for i in range(20, len(df)):
            # 準備到第 i 根 Bar 的歷史切片
            slice_df = df.iloc[:i + 1].copy()
            ctx = ScriptContext(meta, slice_df)

            try:
                module.on_bar(ctx)
            except Exception:
                continue

            current_price = df.iloc[i]["close"]

            for signal in ctx._signals:
                fill_price = current_price + (
                    config.slippage_ticks if signal.direction == Direction.BUY
                    else -config.slippage_ticks
                )

                fill = Fill(
                    order_id=f"bt_{i}",
                    symbol=config.symbol,
                    direction=signal.direction,
                    price=fill_price,
                    qty=signal.qty,
                    fee=config.commission * signal.qty,
                    timestamp=df.iloc[i]["timestamp"],
                )
                fills.append(fill)
                capital -= fill.fee

                # 更新倉位
                if signal.direction == Direction.BUY:
                    if position_side == PositionSide.SHORT:
                        # 平空
                        pnl = (avg_price - fill_price) * position_qty * self._point_value(config.symbol)
                        capital += pnl
                        if pnl > 0:
                            wins += 1
                            gross_profit += pnl
                        else:
                            losses += 1
                            gross_loss += abs(pnl)
                        position_qty = 0
                        position_side = None
                    else:
                        # 開多 / 加碼
                        total_cost = avg_price * position_qty + fill_price * signal.qty
                        position_qty += signal.qty
                        avg_price = total_cost / position_qty
                        position_side = PositionSide.LONG
                else:
                    if position_side == PositionSide.LONG:
                        # 平多
                        pnl = (fill_price - avg_price) * position_qty * self._point_value(config.symbol)
                        capital += pnl
                        if pnl > 0:
                            wins += 1
                            gross_profit += pnl
                        else:
                            losses += 1
                            gross_loss += abs(pnl)
                        position_qty = 0
                        position_side = None
                    else:
                        # 開空 / 加碼
                        total_cost = avg_price * position_qty + fill_price * signal.qty
                        position_qty += signal.qty
                        avg_price = total_cost / position_qty if position_qty else 0
                        position_side = PositionSide.SHORT

            # 計算未實現損益
            unrealized = 0
            if position_qty > 0 and position_side:
                mult = 1 if position_side == PositionSide.LONG else -1
                unrealized = mult * (current_price - avg_price) * position_qty * self._point_value(config.symbol)

            equity_curve.append(capital + unrealized)

        # 4. 計算績效
        total_trades = wins + losses
        total_return = ((equity_curve[-1] - config.initial_capital) / config.initial_capital) * 100
        max_dd = self._max_drawdown(equity_curve)
        sharpe = self._sharpe_ratio(equity_curve)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        duration = time.time() - start_time
        logger.info(
            f"[Backtest] 完成: {total_trades} 筆交易 | "
            f"報酬 {total_return:.1f}% | Sharpe {sharpe:.2f} | "
            f"耗時 {duration:.2f}s"
        )

        return BacktestResult(
            config=config,
            total_return=round(total_return, 2),
            max_drawdown=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 2),
            win_rate=round(win_rate, 1),
            total_trades=total_trades,
            profit_factor=round(profit_factor, 2),
            equity_curve=equity_curve,
            trades=fills,
            duration_seconds=round(duration, 2),
        )

    @staticmethod
    def _max_drawdown(equity: list[float]) -> float:
        peak = equity[0]
        max_dd = 0
        for val in equity:
            peak = max(peak, val)
            dd = (peak - val) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
        return max_dd

    @staticmethod
    def _sharpe_ratio(equity: list[float], risk_free: float = 0.0) -> float:
        if len(equity) < 2:
            return 0.0
        returns = [(equity[i] - equity[i - 1]) / equity[i - 1]
                    for i in range(1, len(equity)) if equity[i - 1] != 0]
        if not returns:
            return 0.0
        import statistics
        avg = statistics.mean(returns)
        std = statistics.stdev(returns) if len(returns) > 1 else 1
        return ((avg - risk_free) / std) * (252 ** 0.5) if std > 0 else 0

    @staticmethod
    def _point_value(symbol: str) -> float:
        return {"TX": 200, "MTX": 50, "TE": 4000, "TF": 1000}.get(symbol, 200)

    @staticmethod
    def _empty_result(config: BacktestConfig, duration: float) -> BacktestResult:
        return BacktestResult(
            config=config, total_return=0, max_drawdown=0,
            sharpe_ratio=0, win_rate=0, total_trades=0,
            profit_factor=0, equity_curve=[config.initial_capital],
            trades=[], duration_seconds=duration,
        )
