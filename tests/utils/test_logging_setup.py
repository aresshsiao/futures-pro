"""
tests/utils/test_logging_setup.py — 日誌設定測試

重點在「事後翻得到」這件事：
  1. 訊息真的落到 logs/ 的檔案，而不是只飄在 console。
  2. 警告以上另外進 error.log，出事時不必在報價洗版裡撈。
  3. 交易訊息另外進 trade.log。
  4. 檔案等級可以比 console 細（螢幕看不下的細節，檔案裡還在）。

setup_logging() 會動到 root logger 這個全域狀態，每個測試前後都要還原。
"""
import logging
import logging.handlers
import sys
from pathlib import Path

import pytest

from utils import logging_setup


@pytest.fixture(autouse=True)
def restore_logging():
    """root logger 是全域的，測完要把 handler 與等級還原，否則會影響其他測試。"""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_hooks = (sys.excepthook, )
    logging_setup._configured = False
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    sys.excepthook, = saved_hooks
    logging_setup._configured = False


def configure(tmp_path, **overrides):
    """用覆寫過的設定跑一次 setup_logging，回傳 logs 目錄。"""
    from config import settings

    defaults = {
        "LOG_LEVEL": "INFO", "LOG_FILE_LEVEL": "DEBUG", "LOG_TO_CONSOLE": False,
        "LOG_TO_FILE": True, "LOG_RETENTION_DAYS": 7, "LOG_ROTATE_AT_HOUR": 6,
    }
    saved = {k: getattr(settings, k, None) for k in {**defaults, **overrides}}
    for k, v in {**defaults, **overrides}.items():
        setattr(settings, k, v)
    try:
        return logging_setup.setup_logging(tmp_path / "logs")
    finally:
        for k, v in saved.items():
            if v is not None:
                setattr(settings, k, v)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


class TestFileOutput:
    def test_writes_to_log_dir(self, tmp_path):
        log_dir = configure(tmp_path)
        logging.getLogger("core.quote_module").info("報價已連線")

        assert log_dir == tmp_path / "logs"
        assert "報價已連線" in read(log_dir / "futures.log")

    def test_warning_also_goes_to_error_log(self, tmp_path):
        log_dir = configure(tmp_path)
        log = logging.getLogger("data.database")
        log.info("一般訊息")
        log.warning("寫入失敗")

        error_log = read(log_dir / "error.log")
        assert "寫入失敗" in error_log
        assert "一般訊息" not in error_log      # error.log 只留警告以上
        assert "一般訊息" in read(log_dir / "futures.log")

    def test_file_can_be_more_verbose_than_console(self, tmp_path):
        """console 設 INFO 不該讓檔案也拿不到 DEBUG。"""
        log_dir = configure(tmp_path, LOG_LEVEL="INFO", LOG_FILE_LEVEL="DEBUG")
        logging.getLogger("data.bar_builder").debug("K棒細節")

        assert "K棒細節" in read(log_dir / "futures.log")

    def test_exception_traceback_is_recorded(self, tmp_path):
        log_dir = configure(tmp_path)
        try:
            raise ValueError("下單爆了")
        except ValueError:
            logging.getLogger("core.trade_module").exception("送單失敗")

        content = read(log_dir / "error.log")
        assert "ValueError: 下單爆了" in content
        assert "Traceback" in content

    def test_repeated_setup_does_not_duplicate_handlers(self, tmp_path):
        """重複呼叫（或先前有人 basicConfig 過）不該讓每行 log 印兩次。"""
        logging.basicConfig()                     # 模擬別處先設定過
        log_dir = configure(tmp_path)
        logging_setup.setup_logging(tmp_path / "logs")
        logging.getLogger("main").info("只該出現一次")

        assert read(log_dir / "futures.log").count("只該出現一次") == 1


class TestTradeLog:
    def test_trade_module_messages_are_kept(self, tmp_path):
        log_dir = configure(tmp_path)
        logging.getLogger("core.trade_module").info("[TradeModule] 委託送出: buy TX")

        assert "委託送出" in read(log_dir / "trade.log")

    def test_broker_trade_messages_are_kept(self, tmp_path):
        """券商 adapter 的報價與交易共用同一個 logger，靠訊息標記分辨。"""
        log_dir = configure(tmp_path)
        broker = logging.getLogger("brokers.adapters.sinopac")
        broker.info("[SinoPac Trade] 委託送出 TX buy x1")
        broker.info("[SinoPac] 收到報價 tick")

        trade_log = read(log_dir / "trade.log")
        assert "委託送出 TX buy x1" in trade_log
        assert "收到報價" not in trade_log        # 報價不該淹掉交易紀錄

    def test_trade_log_is_a_subset_of_main_log(self, tmp_path):
        log_dir = configure(tmp_path)
        logging.getLogger("brokers.adapters.sinopac").info("[SinoPac] 收到報價 tick")

        assert "收到報價" in read(log_dir / "futures.log")


class TestRotation:
    def test_rotates_daily_at_configured_hour(self, tmp_path):
        """夜盤跑到隔天 05:00，午夜換檔會把同一個交易日切成兩個檔案。"""
        configure(tmp_path, LOG_ROTATE_AT_HOUR=6, LOG_RETENTION_DAYS=7)
        handlers = [
            h for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]

        assert len(handlers) == 3                 # futures / error / trade
        for h in handlers:
            assert h.when == "MIDNIGHT"
            assert h.atTime.hour == 6
            assert h.backupCount == 7

    def test_file_output_can_be_disabled(self, tmp_path):
        log_dir = configure(tmp_path, LOG_TO_FILE=False)
        logging.getLogger("main").info("不該產生檔案")

        assert not (log_dir / "futures.log").exists()
