"""
scripts/engine.py — Script 執行引擎
類似 XQ XScript / Pine Script 的概念。
Script 不內建在主架構中，而是作為外部插件匯入。
"""
from __future__ import annotations
import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from core.event_bus import EventBus
from core.models import (
    Bar, Direction, IndicatorOutput, OrderType,
    ScriptMeta, ScriptType, StrategySignal,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  Script 可用的 API
# ═══════════════════════════════════════════════════════════

class ScriptContext:
    """
    傳入 Script 的執行上下文。
    Script 透過 ctx 來讀取市場資料 & 發送交易訊號。

    Indicator 使用:
        ctx.plot("ma5", values, color="#f59e0b")

    Strategy 使用:
        ctx.buy(qty=1)
        ctx.sell(qty=1)
        ctx.buy_limit(price=17500, qty=1)
    """

    def __init__(self, meta: ScriptMeta, bars: pd.DataFrame):
        self._meta = meta
        self._bars = bars        # DataFrame with columns: open, high, low, close, volume, timestamp
        self._signals: list[StrategySignal] = []
        
        # 指標繪圖暫存 (dict of IndicatorSeries dict)
        self._plots: dict[str, dict] = {}
        # 這次 calc() 判斷成立、要播報的文字（見 alert()）
        self._alerts: list[str] = []

        self._params = dict(meta.parameters)

    # ── 資料存取 ──────────────────────────────────────

    @property
    def data(self) -> pd.DataFrame:
        """K線資料 (DataFrame)"""
        return self._bars

    @property
    def close(self) -> pd.Series:
        return self._bars["close"]

    @property
    def open(self) -> pd.Series:
        return self._bars["open"]

    @property
    def high(self) -> pd.Series:
        return self._bars["high"]

    @property
    def low(self) -> pd.Series:
        return self._bars["low"]

    @property
    def volume(self) -> pd.Series:
        return self._bars["volume"]

    def param(self, key: str, default: Any = None) -> Any:
        """讀取 Script 參數"""
        return self._params.get(key, default)

    # ── 指標繪圖 (Indicator) ──────────────────────────

    def plot(self, name: str, values: list | pd.Series, color: str = "#3b82f6",
             panel: str = "main", dash: str | list | None = None, width: float = 1.2,
             label: bool = False) -> None:
        """畫一條線 (預設疊在K線上)

        dash  : None/"solid"=實線, "dash"=長虛線, "dot"=點線, "dash-dot"=點劃線,
                或直接傳 canvas setLineDash 陣列，例如 [6, 3]
        width : 線寬 (邏輯像素，預設 1.2)
        label : True 時，在圖表右側價格軸標出這條線目前的點數（不用 hover 也看得到）
        """
        from core.models import PanelType

        if isinstance(values, pd.Series):
            values = [None if pd.isna(x) else x for x in values.tolist()]

        p_type = PanelType.MAIN
        if panel == "volume":
            p_type = PanelType.VOLUME
        elif panel == "sub":
            p_type = PanelType.SUB

        _DASH_PRESETS = {
            "solid":    [],
            "dash":     [6, 3],
            "dot":      [2, 3],
            "dash-dot": [6, 3, 2, 3],
        }
        if dash is None or dash == "solid":
            dash_pattern = []
        elif isinstance(dash, str):
            dash_pattern = _DASH_PRESETS.get(dash, [])
        else:
            dash_pattern = list(dash)

        self._plots[name] = {
            "values": values,
            "color": color,
            "panel": p_type.value,
            "dash":  dash_pattern,
            "width": float(width),
            "label": bool(label),
        }

    def vol_plot(self, name: str, values: list | pd.Series, color: str = "#3b82f6",
                dash: str | list | None = None, width: float = 1.2,
                label: bool = False) -> None:
        """畫在量圖 (等同 panel="volume")"""
        self.plot(name, values, color, panel="volume", dash=dash, width=width, label=label)

    def sub_plot(self, name: str, values: list | pd.Series, color: str = "#3b82f6",
                ref_lines: list[float] | None = None,
                dash: str | list | None = None, width: float = 1.2,
                label: bool = False) -> None:
        """畫在獨立子圖 (等同 panel="sub")，ref_lines 可指定水平參考線 (如 [80, 20])"""
        self.plot(name, values, color, panel="sub", dash=dash, width=width, label=label)
        if ref_lines:
            self._plots[name]["ref_lines"] = ref_lines

    # ── 播報警示 ──────────────────────────────────────

    def alert(self, message: str) -> None:
        """Script 自己判斷條件成立時呼叫，系統只負責照著念出 message。

        由 script 決定「什麼時候該播」跟「要播什麼」，前端完全不需要知道
        這個警示背後的條件（例如量能門檻）是什麼，只負責播放。
        calc() 只會在每根 M1 棒收完時執行一次（見 main.py on_bar_complete），
        所以同一根棒不會重複呼叫，不需要額外去重。
        """
        self._alerts.append(str(message))

    # ── 交易訊號 (Strategy) ───────────────────────────

    def buy(self, qty: int = 1, reason: str = "") -> None:
        """市價買進"""
        self._signals.append(StrategySignal(
            script_name=self._meta.name,
            direction=Direction.BUY,
            qty=qty,
            order_type=OrderType.MARKET,
            reason=reason,
        ))

    def sell(self, qty: int = 1, reason: str = "") -> None:
        """市價賣出"""
        self._signals.append(StrategySignal(
            script_name=self._meta.name,
            direction=Direction.SELL,
            qty=qty,
            order_type=OrderType.MARKET,
            reason=reason,
        ))

    def buy_limit(self, price: float, qty: int = 1, reason: str = "") -> None:
        """限價買進"""
        self._signals.append(StrategySignal(
            script_name=self._meta.name,
            direction=Direction.BUY,
            qty=qty,
            price=price,
            order_type=OrderType.LIMIT,
            reason=reason,
        ))

    def sell_limit(self, price: float, qty: int = 1, reason: str = "") -> None:
        """限價賣出"""
        self._signals.append(StrategySignal(
            script_name=self._meta.name,
            direction=Direction.SELL,
            qty=qty,
            price=price,
            order_type=OrderType.LIMIT,
            reason=reason,
        ))


# ═══════════════════════════════════════════════════════════
#  Script 執行引擎
# ═══════════════════════════════════════════════════════════

def load_meta_from_file(file_path: str, script_id: str, enabled: bool | None = None) -> Optional["ScriptMeta"]:
    """從 script 檔案讀取 __meta__ 並建立 ScriptMeta"""
    path = Path(file_path)
    if not path.exists():
        logger.error(f"[ScriptEngine] 找不到檔案: {path}")
        return None
        
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        mtime = 0.0

    try:
        spec = importlib.util.spec_from_file_location(script_id, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        meta_dict = getattr(module, "__meta__", {})
        name = meta_dict.get("name", script_id)
        description = meta_dict.get("description", "")
        type_str = meta_dict.get("type", "indicator")
        params = dict(meta_dict.get("params", {}))
        script_type = ScriptType.INDICATOR if type_str == "indicator" else ScriptType.STRATEGY
        # enabled 優先用呼叫端傳入的值；未傳入則讀 __meta__["enabled"]，預設 False
        if enabled is None:
            enabled = bool(meta_dict.get("enabled", False))
        interval_sec = meta_dict.get("interval_sec")
        return ScriptMeta(
            id=script_id, name=name, script_type=script_type,
            description=description, enabled=enabled,
            file_path=file_path, parameters=params,
            interval_sec=interval_sec,
            last_modified=mtime,
        )
    except Exception:
        logger.exception(f"[ScriptEngine] 讀取 meta 失敗: {file_path}")
        return None


class ScriptEngine:
    """
    載入並執行 Script (指標 / 策略)。

    Script 規範:
    ─────────────
    每個 Script 是一個 .py 檔案，需包含:

    # 指標 Script:
    def calc(ctx: ScriptContext):
        ma5 = ctx.close.rolling(5).mean()
        ma20 = ctx.close.rolling(20).mean()
        ctx.plot("MA5", ma5, color="#f59e0b")
        ctx.plot("MA20", ma20, color="#8b5cf6")

    # 策略 Script:
    def on_bar(ctx: ScriptContext):
        ma5 = ctx.close.rolling(5).mean()
        ma20 = ctx.close.rolling(20).mean()
        if ma5.iloc[-1] > ma20.iloc[-1] and ma5.iloc[-2] <= ma20.iloc[-2]:
            ctx.buy(1, reason="MA5 上穿 MA20")
    """

    def __init__(self, scripts_dir: str = "scripts/user"):
        self.bus = EventBus()
        self._scripts_dir = Path(scripts_dir)
        self._scripts: dict[str, ScriptMeta] = {}
        self._modules: dict[str, Any] = {}  # 載入的 Python 模組

    # ── Script 管理 ───────────────────────────────────

    def load_script(self, meta: ScriptMeta) -> bool:
        """從檔案載入 Script"""
        path = Path(meta.file_path)
        if not path.exists():
            logger.error(f"[ScriptEngine] 找不到檔案: {path}")
            return False

        try:
            spec = importlib.util.spec_from_file_location(meta.name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # 驗證 Script 結構
            if meta.script_type == ScriptType.INDICATOR and not hasattr(module, "calc"):
                logger.error(f"[ScriptEngine] 指標 Script 缺少 calc() 函式: {meta.name}")
                return False
            if meta.script_type == ScriptType.STRATEGY and not hasattr(module, "on_bar"):
                logger.error(f"[ScriptEngine] 策略 Script 缺少 on_bar() 函式: {meta.name}")
                return False

            self._modules[meta.id] = module
            self._scripts[meta.id] = meta
            logger.info(f"[ScriptEngine] 載入成功: {meta.name} ({meta.script_type.value})")
            return True

        except Exception:
            logger.exception(f"[ScriptEngine] 載入失敗: {meta.name}")
            return False

    def unload_script(self, script_id: str) -> None:
        self._scripts.pop(script_id, None)
        self._modules.pop(script_id, None)

    def enable_script(self, script_id: str) -> None:
        if script_id in self._scripts:
            self._scripts[script_id].enabled = True

    def disable_script(self, script_id: str) -> None:
        if script_id in self._scripts:
            self._scripts[script_id].enabled = False

    @property
    def enabled_indicators(self) -> list[ScriptMeta]:
        return [s for s in self._scripts.values()
                if s.enabled and s.script_type == ScriptType.INDICATOR]

    @property
    def enabled_strategies(self) -> list[ScriptMeta]:
        return [s for s in self._scripts.values()
                if s.enabled and s.script_type == ScriptType.STRATEGY]

    def _check_and_reload(self, script_id: str) -> None:
        """檢查 script 檔案是否有更新，若有則重新載入"""
        meta = self._scripts.get(script_id)
        if not meta:
            return
            
        path = Path(meta.file_path)
        try:
            current_mtime = path.stat().st_mtime
        except FileNotFoundError:
            return
            
        if current_mtime > meta.last_modified:
            logger.info(f"[ScriptEngine] 偵測到 {meta.name} 已修改，重新載入...")
            was_enabled = meta.enabled
            new_meta = load_meta_from_file(meta.file_path, script_id, enabled=was_enabled)
            if new_meta:
                self.load_script(new_meta)

    # ── 執行 ──────────────────────────────────────────

    def run_indicator(
        self, script_id: str, bars: pd.DataFrame
    ) -> Optional[IndicatorOutput]:
        """執行指標 Script，回傳繪圖資料"""
        self._check_and_reload(script_id)
        
        meta = self._scripts.get(script_id)
        module = self._modules.get(script_id)
        if not meta or not module or not meta.enabled:
            return None

        ctx = ScriptContext(meta, bars)
        try:
            module.calc(ctx)
            return IndicatorOutput(
                name=meta.name,
                series=ctx._plots,
                alerts=ctx._alerts,
            )
        except Exception:
            logger.exception(f"[ScriptEngine] 指標執行錯誤: {meta.name}")
            return None

    def run_strategy(
        self, script_id: str, bars: pd.DataFrame
    ) -> list[StrategySignal]:
        """執行策略 Script，回傳交易訊號"""
        self._check_and_reload(script_id)
        
        meta = self._scripts.get(script_id)
        module = self._modules.get(script_id)
        if not meta or not module or not meta.enabled:
            return []

        ctx = ScriptContext(meta, bars)
        try:
            module.on_bar(ctx)
            if ctx._signals:
                for sig in ctx._signals:
                    logger.info(
                        f"[ScriptEngine] 策略訊號: {meta.name} → "
                        f"{sig.direction.value} x{sig.qty} ({sig.reason})"
                    )
                    self.bus.emit_sync("script_signal", sig)
            return ctx._signals
        except Exception:
            logger.exception(f"[ScriptEngine] 策略執行錯誤: {meta.name}")
            return []

    def run_all_on_bar(self, bars: pd.DataFrame) -> dict[str, IndicatorOutput]:
        """
        每收到一根新 Bar 時呼叫。
        執行所有啟用的指標 & 策略。
        """
        indicator_results = {}

        for meta in self.enabled_indicators:
            result = self.run_indicator(meta.id, bars)
            if result:
                indicator_results[meta.id] = result

        for meta in self.enabled_strategies:
            self.run_strategy(meta.id, bars)

        return indicator_results
