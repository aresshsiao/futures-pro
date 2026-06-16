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

# ── 圖表 ─────────────────────────────────────────────
# 成交量圖的水平參考線（可依需求增減/調整數值與文字，前端透過 /api/config 取得）
# 當成交量跨越 level 時，前端會用語音播報對應的 label
VOLUME_REFERENCE_LINES = [
    {"level": 1500, "label": "爆1500大量"},
    {"level": 400, "label": "爆400大量"},
]

# K棒（及連動的漲跌幅、損益等）顏色慣例，前端透過 /api/config 取得：
#   "green-up" → 漲＝綠、跌＝紅（國際慣例，預設）
#   "red-up"   → 漲＝紅、跌＝綠（台股慣例）
CANDLE_COLOR_SCHEME = "red-up"
