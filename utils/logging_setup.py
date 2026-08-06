"""
utils/logging_setup.py — 全系統日誌設定

整個程式只有這裡碰 logging 設定（main.py 啟動時呼叫 setup_logging() 一次），
其他模組一律 `logger = logging.getLogger(__name__)` 就好。

輸出去向：
    console            短格式（時:分:秒），盯盤當下看的
    logs/futures.log   全部訊息，每個交易日一個檔
    logs/error.log     只有 WARNING 以上 — 出事時第一個翻的檔案，不必在報價洗版裡撈
    logs/trade.log     下單／刪單／成交／倉位 — 交易紀錄要能獨立留存回溯
    logs/shioaji.log   永豐 API 自己寫的檔（原本會掉在專案根目錄，一併收進來）

檔案格式帶完整日期、logger 名稱與行號（事後追問題沒行號很痛苦），
console 省略這些以免佔滿螢幕。
"""
from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from config import settings

# console 看的是「剛剛」發生什麼，只留時分秒；檔案要能對到某一天某一秒
CONSOLE_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
CONSOLE_DATEFMT = "%H:%M:%S"
FILE_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s:%(lineno)d] %(message)s"
FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 第三方套件的 DEBUG 對這個系統沒有價值，只會把自己的 log 淹掉
THIRD_PARTY_LEVELS = {
    "urllib3": logging.WARNING,
    "asyncio": logging.WARNING,
    "websockets": logging.WARNING,
    "python_multipart": logging.WARNING,
    "watchfiles": logging.WARNING,
    "matplotlib": logging.WARNING,
    "PIL": logging.WARNING,
}

_configured = False


class TradeFilter(logging.Filter):
    """挑出交易相關訊息，寫進獨立的 trade.log。

    TradeModule 有自己的 logger 很好認；券商 adapter 的報價與交易共用同一個 module
    logger，只能靠訊息裡的 "[XXX Trade]" 標記分辨 —— 標記改了這裡要跟著改。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("core.trade_module"):
            return True
        return "Trade]" in record.getMessage()


def _level(value, default: int) -> int:
    """把設定檔裡的 "INFO" / "debug" 轉成 logging 常數，看不懂就用預設值。"""
    resolved = getattr(logging, str(value).upper(), None)
    return resolved if isinstance(resolved, int) else default


def _utf8_stream(stream):
    """Windows 主控台預設編碼是 cp950，中文 log 會直接炸 UnicodeEncodeError
    （整行訊息就這樣不見了）。轉成 UTF-8，轉不動就退回原樣。"""
    try:
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    return stream


def _file_handler(
    path: Path, level: int, formatter: logging.Formatter,
    backup_days: int, rotate_hour: int, log_filter: logging.Filter | None = None,
) -> logging.Handler:
    """每天固定時間換檔的檔案 handler。

    換檔時間預設 06:00 而不是午夜：夜盤要跑到隔天 05:00，午夜換檔會把同一個交易日
    的 log 切成兩個檔案，回頭查一筆夜盤的單得同時開兩個檔。
    """
    handler = logging.handlers.TimedRotatingFileHandler(
        path,
        when="midnight",
        atTime=_dt.time(hour=rotate_hour),
        backupCount=backup_days,
        encoding="utf-8",
        delay=True,          # 沒訊息就不建檔，避免每次啟動都留下一堆空檔
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    if log_filter is not None:
        handler.addFilter(log_filter)
    return handler


def _install_exception_hooks() -> None:
    """未捕捉的例外預設只印到 stderr，不會進 log 檔 —— 但當機原因正是最該留存的東西。"""
    log = logging.getLogger("unhandled")

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):   # Ctrl+C 是正常關機，別當成錯誤
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("未捕捉的例外", exc_info=(exc_type, exc, tb))

    sys.excepthook = excepthook

    def threadhook(args):
        # 券商的報價／回報 callback 跑在子執行緒，那裡爆掉不會走 sys.excepthook，
        # 沒有這個 hook 就只剩畫面停止更新、log 卻一片安靜。
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical(
            "執行緒 %s 未捕捉的例外",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = threadhook


def setup_logging(log_dir: str | Path | None = None) -> Path:
    """設定全系統日誌，回傳實際使用的 logs 目錄。重複呼叫不會重設。"""
    global _configured

    log_dir = Path(log_dir or getattr(settings, "LOG_DIR", settings.BASE_DIR / "logs"))
    if _configured:
        return log_dir

    console_level = _level(getattr(settings, "LOG_LEVEL", "INFO"), logging.INFO)
    file_level = _level(getattr(settings, "LOG_FILE_LEVEL", "DEBUG"), logging.DEBUG)
    retention = int(getattr(settings, "LOG_RETENTION_DAYS", 30))
    rotate_hour = int(getattr(settings, "LOG_ROTATE_AT_HOUR", 6))
    to_console = bool(getattr(settings, "LOG_TO_CONSOLE", True))
    to_file = bool(getattr(settings, "LOG_TO_FILE", True))

    # shioaji 會在工作目錄自己產生 shioaji.log，用它的環境變數一起收進 logs/。
    # 必須在 import shioaji 之前設定（adapter 都是延後 import，所以來得及）。
    os.environ.setdefault("SJ_LOG_PATH", str(log_dir / "shioaji.log"))

    root = logging.getLogger()
    # 先前若有人呼叫過 basicConfig，留著會讓每行 log 印兩次
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    # root 放最寬鬆的等級，真正的過濾交給各 handler；
    # 否則 console 設 INFO 就會讓檔案永遠拿不到 DEBUG。
    root.setLevel(min(console_level, file_level))

    if to_console:
        console = logging.StreamHandler(_utf8_stream(sys.stdout))
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT, CONSOLE_DATEFMT))
        root.addHandler(console)

    if to_file:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_fmt = logging.Formatter(FILE_FORMAT, FILE_DATEFMT)
        root.addHandler(_file_handler(
            log_dir / "futures.log", file_level, file_fmt, retention, rotate_hour))
        root.addHandler(_file_handler(
            log_dir / "error.log", logging.WARNING, file_fmt, retention, rotate_hour))
        root.addHandler(_file_handler(
            log_dir / "trade.log", logging.INFO, file_fmt, retention, rotate_hour,
            TradeFilter()))

    # 券商 adapter 的等級單獨控制（報價 callback 很吵，平常不需要 DEBUG）
    logging.getLogger("brokers.adapters.sinopac").setLevel(
        _level(getattr(settings, "BROKER_LOG_LEVEL", "INFO"), logging.INFO)
    )
    for name, level in THIRD_PARTY_LEVELS.items():
        logging.getLogger(name).setLevel(level)

    logging.captureWarnings(True)      # warnings.warn() 也進 log，不要只飄在 stderr
    _install_exception_hooks()

    _configured = True
    if to_file:
        logging.getLogger(__name__).info(
            "[Logging] 日誌目錄 %s（保留 %d 天，每日 %02d:00 換檔）",
            log_dir, retention, rotate_hour,
        )
    return log_dir
