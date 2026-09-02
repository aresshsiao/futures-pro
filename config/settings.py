"""
config/settings.py — 全域設定的載入器

設定值本身在同目錄的 `settings.yaml`，這裡只做三件事：讀檔、缺鍵時報錯、
把值攤平成模組層級的常數。**這個檔案不持有任何預設值** —— 預設值同時寫在
YAML 跟 Python 兩邊，就是遲早會對不起來的兩份真相。

之所以留一層 Python 而不是讓各模塊自己讀 YAML：
  1. 路徑要展開成以專案根目錄為基準的 Path，寫在 YAML 裡只會變成一堆
     容易打錯、換台機器就失效的絕對路徑。
  2. 全專案既有的用法是 `from config import settings` + `settings.X`，
     測試也靠 `monkeypatch.setattr(settings, ...)` 覆寫。攤平成模組屬性
     就完全不必動這些呼叫端。
  3. 常數名寫死在這裡，grep 得到、IDE 也跟得到；用 setattr 迴圈動態產生
     就等於把所有設定項從靜態分析裡藏起來。
"""
from pathlib import Path
from typing import Any

import yaml


class ConfigError(RuntimeError):
    """settings.yaml 缺鍵、型別錯誤或根本讀不到。

    這種錯一律在 import 當下就炸掉，不做 fallback：這裡管的是下單參數與
    路徑，帶著半套設定啟動比啟動失敗危險得多。
    """


BASE_DIR = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "settings.yaml"


def _load(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"讀不到設定檔 {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"設定檔 {path} 格式錯誤: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"設定檔 {path} 的最外層必須是 mapping（key: value）")
    return raw


_cfg = _load(CONFIG_FILE)


def _get(dotted: str) -> Any:
    """依 "a.b.c" 取值，缺鍵直接報錯並指出是哪一項。"""
    node: Any = _cfg
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            raise ConfigError(f"{CONFIG_FILE} 缺少設定項 `{dotted}`")
        node = node[key]
    return node


def _path(dotted: str) -> Path:
    """設定檔裡的路徑一律相對於專案根目錄，展開成絕對路徑再交出去。

    絕對化不是潔癖：相對路徑的意義取決於行程的工作目錄，從 IDE、排程器或
    service 啟動時會指到完全不同的地方，DB 於是「憑空多出一個空的」。
    """
    return (BASE_DIR / str(_get(dotted))).resolve()


def _mapping(dotted: str) -> dict:
    table = _get(dotted)
    if not isinstance(table, dict):
        raise ConfigError(f"{CONFIG_FILE} 的 `{dotted}` 必須是 mapping")
    return table


def _table(dotted: str, default_dotted: str) -> tuple[dict, float]:
    """讀一張「商品 → 數值」對照表與它的 fallback。"""
    return (
        {str(k): float(v) for k, v in _mapping(dotted).items()},
        float(_get(default_dotted)),
    )


# ── 路徑 ─────────────────────────────────────────────
DATA_DIR = _path("paths.data_dir")
DB_PATH = _path("paths.db")
RAW_TAIFEX_DIR = _path("paths.raw_taifex_dir")
SCRIPTS_USER_DIR = _path("paths.scripts_user_dir")
SCRIPTS_BUILTIN_DIR = _path("paths.scripts_builtin_dir")
STATIC_DIR = _path("paths.static_dir")
LOG_DIR = _path("paths.log_dir")

# ── 伺服器 ───────────────────────────────────────────
SERVER_HOST = str(_get("server.host"))
SERVER_PORT = int(_get("server.port"))

# ── 認證 Auth ────────────────────────────────────────
AUTH_SECRET_KEY = str(_get("auth.secret_key"))
AUTH_PASSWORD_HASH = str(_get("auth.password_hash"))
AUTH_TOKEN_EXPIRE_HOURS = int(_get("auth.token_expire_hours"))
# 這串是「設定檔還沒改過」的判斷依據，server 啟動時會據此發警告
AUTH_DEFAULT_SECRET_KEY = "change-this-secret-key-in-production"

# ── 日誌 Logging ─────────────────────────────────────
LOG_LEVEL = str(_get("logging.level"))
BROKER_LOG_LEVEL = str(_get("logging.broker_level"))
LOG_FILE_LEVEL = str(_get("logging.file_level"))
LOG_TO_CONSOLE = bool(_get("logging.to_console"))
LOG_TO_FILE = bool(_get("logging.to_file"))
LOG_RETENTION_DAYS = int(_get("logging.retention_days"))
LOG_ROTATE_AT_HOUR = int(_get("logging.rotate_at_hour"))

# ── Core Service 自動連線 ────────────────────────────
AUTO_CONNECT_BROKER = _get("core_service.auto_connect_broker") or None
AUTO_CONNECT_KIND = str(_get("core_service.auto_connect_kind"))
DEFAULT_SUBSCRIBE_SYMBOLS = list(_get("core_service.default_subscribe_symbols"))

# ── 條件單（右邊下單）────────────────────────────────
CONDITION_SESSION_CLOSE_TIMES = list(_get("condition.session_close_times"))
CONDITION_SESSION_CHECK_SEC = int(_get("condition.session_check_sec"))

# 右邊下單面板的新條件預設值。逐欄轉型而不是整包丟出去：YAML 打成字串時
# 要在載入當下就炸，而不是等使用者按下送出、後端才收到一個 "10" 的返點。
_cond_defaults = _mapping("condition.defaults")


def _strict_bool(v):
    """只收真正的布林。用內建 bool() 的話 `cost_guard: "false"` 會變成 True ——
    一個永遠打開、而且看設定檔怎麼看都看不出來的成本防線。"""
    if not isinstance(v, bool):
        raise ValueError(f"必須是 true / false，不是 {v!r}")
    return v


def _cond_default(key: str, cast):
    if key not in _cond_defaults:
        raise ConfigError(f"{CONFIG_FILE} 缺少設定項 `condition.defaults.{key}`")
    try:
        return cast(_cond_defaults[key])
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"{CONFIG_FILE} 的 `condition.defaults.{key}` 型別錯誤: {e}"
        ) from e


CONDITION_DEFAULTS = {
    "pullback": _cond_default("pullback", int),
    "qty": _cond_default("qty", int),
    "take_profit": _cond_default("take_profit", int),
    "stop_loss": _cond_default("stop_loss", int),
    "cost_guard": _cond_default("cost_guard", _strict_bool),
    "trail": _cond_default("trail", _strict_bool),
}
CONDITION_DEFAULT_DAY_TRADE = _cond_default("day_trade", _strict_bool)
CONDITION_DEFAULT_CLOSE_ON_END = _cond_default("close_on_end", _strict_bool)

# ── 交易 ─────────────────────────────────────────────
DEFAULT_SYMBOL = str(_get("trading.default_symbol"))
DISPLAY_NAME = {str(k): str(v) for k, v in _mapping("trading.display_name").items()}
TICK_SIZE, TICK_SIZE_DEFAULT = _table("trading.tick_size", "trading.tick_size_default")
POINT_VALUE, POINT_VALUE_DEFAULT = _table("trading.point_value", "trading.point_value_default")
COMMISSION_PER_LOT, COMMISSION_PER_LOT_DEFAULT = _table(
    "trading.commission_per_lot", "trading.commission_per_lot_default",
)

# ── 回測 ─────────────────────────────────────────────
BACKTEST_DEFAULT_CAPITAL = float(_get("backtest.default_capital"))
BACKTEST_DEFAULT_SLIPPAGE_TICKS = int(_get("backtest.default_slippage_ticks"))

# ── 前端 UI ──────────────────────────────────────────
CANDLE_COLOR_SCHEME = str(_get("ui.candle_color_scheme"))
