"""
tests/config/test_settings.py — settings.yaml 載入器測試

重點在幾件事：
  1. 缺鍵要炸，而且要說是哪一鍵 —— 靜默套用猜出來的預設值，等於「設定檔說了不算」，
     而這裡管的是下單參數。
  2. 路徑一律以專案根目錄展開成絕對路徑：相對路徑的意義取決於行程的工作目錄，
     從 IDE 或排程器啟動就會指到別的地方（DB 於是「憑空多出一個空的」）。
  3. 商品規格只有這一份 —— point_value / tick_size / commission 以前在
     settings、core.models、backtest.engine 各存一份，三份還互有出入。
"""
from pathlib import Path

import pytest

from config import settings
from core.models import commission_per_lot, point_value, tick_size


class TestLoad:
    def test_rejects_non_mapping(self, tmp_path):
        bad = tmp_path / "settings.yaml"
        bad.write_text("- 1\n- 2\n", encoding="utf-8")
        with pytest.raises(settings.ConfigError, match="mapping"):
            settings._load(bad)

    def test_reports_missing_file_with_path(self, tmp_path):
        with pytest.raises(settings.ConfigError, match="讀不到設定檔"):
            settings._load(tmp_path / "nope.yaml")

    def test_reports_broken_yaml(self, tmp_path):
        bad = tmp_path / "settings.yaml"
        bad.write_text("a: [1, 2\n", encoding="utf-8")
        with pytest.raises(settings.ConfigError, match="格式錯誤"):
            settings._load(bad)


class TestGet:
    def test_missing_key_names_the_key(self):
        with pytest.raises(settings.ConfigError, match="trading.no_such_knob"):
            settings._get("trading.no_such_knob")

    def test_missing_section_names_the_full_path(self):
        with pytest.raises(settings.ConfigError, match="nope.level"):
            settings._get("nope.level")

    def test_reads_nested_value(self):
        assert settings._get("server.port") == settings.SERVER_PORT


class TestPaths:
    """路徑全部是以專案根目錄展開的絕對路徑，跟行程的工作目錄無關。"""

    @pytest.mark.parametrize("name", [
        "DATA_DIR", "DB_PATH", "RAW_TAIFEX_DIR",
        "SCRIPTS_USER_DIR", "SCRIPTS_BUILTIN_DIR", "STATIC_DIR", "LOG_DIR",
    ])
    def test_is_absolute_and_under_base_dir(self, name):
        p = getattr(settings, name)
        assert isinstance(p, Path)
        assert p.is_absolute()
        assert settings.BASE_DIR in p.parents or p == settings.BASE_DIR


class TestProductSpecs:
    """core.models 的查表函式是商品規格的唯一入口，全部回到 settings.yaml。"""

    def test_point_value_reads_settings(self):
        assert point_value("MTX") == settings.POINT_VALUE["MTX"]

    def test_tick_size_reads_settings(self):
        assert tick_size("TE") == settings.TICK_SIZE["TE"]

    def test_commission_reads_settings(self):
        assert commission_per_lot("MTX") == settings.COMMISSION_PER_LOT["MTX"]

    @pytest.mark.parametrize("fn,default_attr", [
        (point_value, "POINT_VALUE_DEFAULT"),
        (tick_size, "TICK_SIZE_DEFAULT"),
        (commission_per_lot, "COMMISSION_PER_LOT_DEFAULT"),
    ])
    def test_unknown_symbol_falls_back(self, fn, default_attr):
        assert fn("NOT_A_SYMBOL") == getattr(settings, default_attr)

    def test_display_names_are_chinese_not_codes(self):
        """語音提示念這張表。念不到就退回代碼，TTS 會把 TX 拆成兩個字母。"""
        assert settings.DISPLAY_NAME["TX"] == "台指期"
        assert all(isinstance(v, str) and v for v in settings.DISPLAY_NAME.values())

    def test_electronic_futures_tick_is_not_one(self):
        """TE 一跳 0.05 點。回測把「跳」當「點」加的話滑價會被灌大 20 倍，
        這條斷言是那個 bug 的守門員。"""
        assert tick_size("TE") == 0.05
        assert tick_size("TX") == 1


class TestTradingSymbols:
    def test_default_symbol_is_selectable(self):
        """選不到的預設商品等於沒設 —— 畫面會停在一個空的下拉選單，
        而使用者只看得到「圖表沒資料」，完全猜不到是設定檔的問題。
        所以這條在載入當下就檢查，不是等到畫面出不來才發現。"""
        assert settings.DEFAULT_SYMBOL in settings.SYMBOLS

    def test_symbols_is_a_non_empty_list_of_str(self):
        assert settings.SYMBOLS
        assert all(isinstance(s, str) and s for s in settings.SYMBOLS)


class TestConditionDefaults:
    """右邊下單面板的新條件預設值。"""

    def test_numeric_fields_are_numbers_not_strings(self):
        """YAML 打成 "10" 的話要在載入當下就炸，而不是等使用者按下送出、
        後端才收到一個字串當返點。"""
        for key in ("pullback", "qty", "take_profit", "stop_loss"):
            assert isinstance(settings.CONDITION_DEFAULTS[key], int)

    def test_flags_are_real_booleans(self):
        for key in ("cost_guard", "trail"):
            assert isinstance(settings.CONDITION_DEFAULTS[key], bool)

    def test_no_default_entry_price(self):
        """壓力價／支撐價不給預設 —— 那是每筆都不同的東西，
        給了只會變成「忘了改就照著送出去」。"""
        assert "resistance" not in settings.CONDITION_DEFAULTS
        assert "support" not in settings.CONDITION_DEFAULTS

    def test_no_configurable_trading_switch(self):
        """「啟動交易」不可設定：開機就自動把昨天留下的條件送進市場，
        是不該存在的選項（見 ARCHITECTURE.md §7.6）。"""
        assert not hasattr(settings, "CONDITION_DEFAULT_TRADING_ENABLED")

    @pytest.mark.parametrize("value", ["false", "no", 0, None])
    def test_non_boolean_flag_is_rejected(self, value):
        """bool("false") 是 True —— 用內建 bool() 收設定，會生出一個永遠打開、
        而且看設定檔怎麼看都看不出來的成本防線。"""
        with pytest.raises(ValueError):
            settings._strict_bool(value)


class TestSingleSourceOfTruth:
    def test_backtest_engine_shares_the_same_table(self):
        """backtest 以前自己藏了一份 {TX:200, MTX:50, TE:4000, TF:1000}。"""
        from backtest.engine import BacktestEngine
        for symbol in settings.POINT_VALUE:
            assert BacktestEngine._point_value(symbol) == point_value(symbol)

    def test_database_uses_configured_db_path(self):
        from data import database
        assert database.DB_PATH == settings.DB_PATH
