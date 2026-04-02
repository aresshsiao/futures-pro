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
