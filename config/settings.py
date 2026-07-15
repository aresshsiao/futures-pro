"""
config/settings.py — 全域設定
"""
from pathlib import Path

# ── 路徑 ─────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "futures.db"
RAW_TAIFEX_DIR = DATA_DIR / "raw" / "taifex"
SCRIPTS_USER_DIR = BASE_DIR / "scripts" / "user"
SCRIPTS_BUILTIN_DIR = BASE_DIR / "scripts" / "builtin"

# ── 伺服器 ───────────────────────────────────────────
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8888

# ── 認證 Auth ────────────────────────────────────────
# 執行 `python scripts/gen_password_hash.py` 產生 AUTH_PASSWORD_HASH
AUTH_SECRET_KEY = "change-this-secret-key-in-production"
AUTH_PASSWORD_HASH = "$2b$12$KbKksoOf0aPH1v8m0IPmZ.aXtOc3w3w8NSkCzKjVccAzHpCCPTrAm"
AUTH_TOKEN_EXPIRE_HOURS = 24

# ── 日誌 Logging ─────────────────────────────────────
# 系統預設的輸出等級 (INFO, DEBUG, WARNING, ERROR)
LOG_LEVEL = "INFO"
# 個別券商底層 API 的日誌等級（因報價跳動頻繁，若不想看到洗版可改為 INFO 或 WARNING）
BROKER_LOG_LEVEL = "INFO"

# ── Core Service 自動連線 ────────────────────────────
# server 啟動、event loop ready 後，Core Service 會自動連線券商並訂閱預設商品，
# 使券商連線的擁有權屬於 Core 而非 browser（見 ARCHITECTURE.md §4.1）。
#   None / ""  → 不自動連線，等 UI 手動觸發
#   "sinopac"  → 啟動時自動連線永豐金
AUTO_CONNECT_BROKER = "sinopac"
# 自動連線成功後自動訂閱的商品清單（Core 只會向券商訂閱一次，多分頁共用）
DEFAULT_SUBSCRIBE_SYMBOLS = ["TX", "TAIEX"]

# ── 交易 ─────────────────────────────────────────────
DEFAULT_SYMBOL = "TX"
TICK_SIZE = {
    "TX": 1, "MTX": 1, "TE": 0.05, "TF": 0.2,
}
POINT_VALUE = {
    "TX": 200, "MTX": 50, "TE": 4000, "TF": 1000,
}
COMMISSION_PER_LOT = {
    "TX": 60, "MTX": 15, "TE": 60, "TF": 60,
}

# ── 回測 ─────────────────────────────────────────────
BACKTEST_DEFAULT_CAPITAL = 1_000_000
BACKTEST_DEFAULT_SLIPPAGE = 1

# K棒（及連動的漲跌幅、損益等）顏色慣例，前端透過 /api/config 取得：
#   "green-up" → 漲＝綠、跌＝紅（國際慣例，預設）
#   "red-up"   → 漲＝紅、跌＝綠（台股慣例）
CANDLE_COLOR_SCHEME = "red-up"
