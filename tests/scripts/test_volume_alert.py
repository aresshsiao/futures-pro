"""
tests/scripts/test_volume_alert.py — 成交量爆量播報

這支 script 曾經在兩種情況下亂叫，兩個都是「calc() 每根棒只跑一次」這個假設
沒說完整造成的：

  1. **同一根棒念好幾次**。夜盤那根量大的棒同時跨過 400 與 1500 兩條門檻，
     而原本只擋掉「日盤時不要念夜盤門檻」，沒擋反過來的方向，於是一根棒
     連念兩句，第二句還把夜盤的量報成「日盤大量」。
  2. **眼前這根 1 分 K 明明沒爆量也會叫**。每一檔訂閱中的商品收完 M1 棒都會
     各跑一次 calc()，而 script 當時看不到自己在算哪一檔 —— 台指期的門檻
     就這樣被套到加權指數與小台上。

所以測試盯的是「一根棒最多一句」與「不是我在盯的商品就閉嘴」。
"""
from datetime import datetime

import pandas as pd
import pytest

from core.models import ScriptMeta, ScriptType
from scripts.builtin import volume_alert
from scripts.engine import ScriptContext

DAY = datetime(2026, 9, 2, 10, 30)     # 日盤
NIGHT = datetime(2026, 9, 2, 22, 30)   # 夜盤


def run(volume, ts=DAY, symbol="TX", params=None):
    """跑一次 calc()，回傳這次要播報的文字。"""
    meta = ScriptMeta(
        id="volume_alert", name="Volume_Alert", script_type=ScriptType.INDICATOR,
        parameters=dict(params if params is not None else volume_alert.__meta__["params"]),
    )
    # 只有最後一根的量會被拿去比對門檻，前面幾根純粹墊資料
    bars = pd.DataFrame([
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": v, "timestamp": ts}
        for v in [1, 1, 1, 1, volume]
    ])
    ctx = ScriptContext(meta, bars, symbol)
    volume_alert.calc(ctx)
    return ctx._alerts


class TestOneAlertPerBar:
    def test_night_bar_over_both_levels_speaks_once(self):
        """夜盤 1500 口同時跨過 400 與 1500 —— 這是「重複好幾次」的來源。"""
        assert len(run(1500, ts=NIGHT)) == 1

    def test_night_big_bar_is_not_labelled_day_session(self):
        """而且不能報成「日盤大量」：夜盤的量拿日盤門檻來講就是錯的。"""
        assert "日盤" not in run(1500, ts=NIGHT)[0]
        assert "夜盤" in run(1500, ts=NIGHT)[0]

    def test_day_bar_over_threshold_speaks_once(self):
        assert len(run(1500, ts=DAY)) == 1
        assert "日盤" in run(1500, ts=DAY)[0]


class TestSessionThresholds:
    def test_day_session_ignores_night_threshold(self):
        """日盤 500 口不算大量 —— 夜盤門檻不該在日盤生效。"""
        assert run(500, ts=DAY) == []

    def test_night_session_uses_night_threshold(self):
        assert len(run(500, ts=NIGHT)) == 1

    def test_quiet_bar_says_nothing(self):
        assert run(100, ts=DAY) == []
        assert run(100, ts=NIGHT) == []


class TestSymbolScope:
    def test_other_symbols_stay_silent(self):
        """加權指數與小台的量能尺度跟大台差一個數量級，
        套用大台門檻就是「最後一分K沒爆量卻在叫」的來源。"""
        assert run(5000, symbol="TAIEX") == []
        assert run(5000, symbol="MTX") == []

    def test_watched_symbol_still_alerts(self):
        assert len(run(1500, symbol="TX")) == 1

    def test_alert_names_the_product(self):
        """訂閱多檔時只說「大量」分不出是哪一檔在爆量。"""
        assert run(1500, symbol="TX")[0].startswith("台指期")

    def test_symbols_can_be_widened_by_params(self):
        params = dict(volume_alert.__meta__["params"], symbols=["TX", "MTX"])
        assert len(run(1500, symbol="MTX", params=params)) == 1


class TestPlotting:
    @pytest.mark.parametrize("ts", [DAY, NIGHT])
    def test_lines_are_drawn_for_both_levels_regardless_of_session(self, ts):
        """播報要挑時段，水平線不用 —— 圖上兩條參考線本來就都該在。"""
        meta = ScriptMeta(
            id="volume_alert", name="Volume_Alert", script_type=ScriptType.INDICATOR,
            parameters=dict(volume_alert.__meta__["params"]),
        )
        bars = pd.DataFrame([
            {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 10, "timestamp": ts}
        ] * 5)
        ctx = ScriptContext(meta, bars, "TX")
        volume_alert.calc(ctx)
        assert set(ctx._plots) == {"日盤大量", "夜盤大量"}
