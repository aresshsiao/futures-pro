"""
main.py — 程式進入點
啟動所有模塊，連接事件，啟動 Web 伺服器。
"""
from __future__ import annotations
import asyncio
import logging
import sys


import uvicorn

from config import settings
from core.event_bus import EventBus
from core.quote_module import QuoteModule
from core.trade_module import TradeModule
from core.models import Direction, OrderType, ScriptMeta, ScriptType, Timeframe
from data.database import Database
from data.bar_builder import BarBuilder
from data.sources.taifex import TaifexImporter
from ui.server import app, register_action, script_engine

# ── Logging ───────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
# sinopac adapter 的 tick debug log（確認 callback 有被觸發）
logging.getLogger("brokers.adapters.sinopac").setLevel(logging.DEBUG)
logger = logging.getLogger("main")

# ── 全域模塊實例 ──────────────────────────────────────

bus = EventBus()
db = Database()
quote = QuoteModule()
trade = TradeModule()
bar_builder = BarBuilder()
taifex = TaifexImporter()
# script_engine 定義在 ui/server.py（理由見該檔案註解），這裡直接重用同一個實例

# 內建 Script 清單（啟動時載入到 script_engine）。
# Volume_Alert 的水平線數值直接沿用 config/settings.py 的 VOLUME_REFERENCE_LINES，
# 讓「爆大量」設定維持單一來源，但實際運算/廣播改走 Script 引擎。
BUILTIN_SCRIPTS = [
    ScriptMeta(
        id="ma_cross", name="MA_Cross", script_type=ScriptType.INDICATOR,
        description="均線交叉指標 (5/20)", enabled=True,
        file_path="scripts/builtin/ma_cross.py",
    ),
    ScriptMeta(
        id="rsi", name="RSI_Signal", script_type=ScriptType.INDICATOR,
        description="RSI 超買超賣", enabled=True,
        file_path="scripts/builtin/rsi.py",
    ),
    ScriptMeta(
        id="volume_alert", name="Volume_Alert", script_type=ScriptType.INDICATOR,
        description="成交量爆量水平線", enabled=True,
        file_path="scripts/builtin/volume_alert.py",
        parameters={"levels": settings.VOLUME_REFERENCE_LINES},
    ),
    ScriptMeta(
        id="breakout", name="Breakout_Strategy", script_type=ScriptType.STRATEGY,
        description="突破策略 (20K高低)", enabled=False,
        file_path="scripts/builtin/breakout.py",
    ),
]


# ═══════════════════════════════════════════════════════════
#  WebSocket Action Handlers
# ═══════════════════════════════════════════════════════════

async def handle_place_order(ws, data: dict):
    """前端: 下單"""
    order = await trade.place_order(
        symbol=data["symbol"],
        direction=Direction(data["direction"]),
        order_type=OrderType(data["order_type"]),
        qty=data["qty"],
        price=data.get("price", 0),
        source=data.get("source", "manual"),
    )
    if order:
        await ws.send_json({
            "type": "order_result",
            "order_id": order.id,
            "status": order.status.value,
        })


async def handle_cancel_order(ws, data: dict):
    """前端: 刪單"""
    ok = await trade.cancel_order(data["order_id"])
    await ws.send_json({"type": "cancel_result", "success": ok})


async def handle_subscribe(ws, data: dict):
    """前端: 訂閱商品"""
    await quote.subscribe(data["symbol"])
    await ws.send_json({"type": "subscribed", "symbol": data["symbol"]})


async def handle_get_history(ws, data: dict):
    """前端: 取得歷史K線
    - 券商已連線 → 從 API 即時查詢
    - 未連線      → fallback 到本地 DB
    """
    symbol    = data["symbol"]
    timeframe = data.get("timeframe", "1")  # "1","3","15","60","日","周","月"
    count     = data.get("count", 300)

    bars_out: list[dict] = []

    if quote.is_connected:
        bars_out = await _get_bars_from_broker(symbol, timeframe, count)

    if not bars_out:
        # Fallback: 從 DB 讀取
        if timeframe in ("日", "周", "月"):
            bars_raw = db.get_bars(symbol, limit=999_999)
            bars_out = _aggregate_bars(bars_raw, timeframe, count)
        else:
            bars_raw = db.get_bars(symbol, limit=count)
            bars_out = [
                {
                    "time": int(b.timestamp.timestamp()) * 1000,
                    "open": b.open, "high": b.high,
                    "low": b.low, "close": b.close,
                    "volume": b.volume,
                    "delivery": b.delivery,
                }
                for b in bars_raw
            ]

    await ws.send_json({
        "type": "history_bars",
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": bars_out,
    })


# 前端 timeframe 字串 → 分鐘數
_TF_MINUTES = {"1": 1, "3": 3, "15": 15, "60": 60, "日": 1440, "周": 10080, "月": 43200}
# 台指期含夜盤約 1200 分鐘/交易日
_TRADING_MINUTES_PER_DAY = 1200


async def _get_bars_from_broker(symbol: str, timeframe: str, count: int) -> list[dict]:
    """
    從券商 API 抓 M1 K棒，在後端聚合成目標週期後回傳。
    日/周/月 回傳已聚合格式；分鐘K 回傳 M1 原始格式（前端再聚合）。
    """
    import math

    tf_minutes = _TF_MINUTES.get(timeframe, 1)

    # 需要多少根 M1（加 20% 緩衝）
    m1_needed = int(count * tf_minutes * 1.2)
    # 轉換成日數，最多抓 2 年
    trading_days  = max(3, math.ceil(m1_needed / _TRADING_MINUTES_PER_DAY))
    calendar_days = min(int(trading_days * 1.6) + 5, 730)

    try:
        m1_bars = await quote.get_history(symbol, Timeframe.M1, calendar_days * _TRADING_MINUTES_PER_DAY)
    except Exception as e:
        logger.warning("[get_history] API 查詢失敗: %s", e)
        return []

    if not m1_bars:
        return []

    logger.info("[get_history] API 回傳 %d 根 M1，目標週期=%s count=%d", len(m1_bars), timeframe, count)

    if timeframe == "1":
        # M1 直接回傳，前端不再聚合
        return [
            {
                "time": int(b.timestamp.timestamp()) * 1000,
                "open": b.open, "high": b.high,
                "low": b.low, "close": b.close,
                "volume": b.volume, "delivery": b.delivery,
            }
            for b in m1_bars[-count:]
        ]

    if timeframe in ("3", "15", "60"):
        # 分鐘K：回傳 M1 讓前端聚合（與 DB 路徑一致）
        return [
            {
                "time": int(b.timestamp.timestamp()) * 1000,
                "open": b.open, "high": b.high,
                "low": b.low, "close": b.close,
                "volume": b.volume, "delivery": b.delivery,
            }
            for b in m1_bars
        ]

    # 日/周/月：後端聚合後回傳
    tf_sec = tf_minutes * 60
    buckets: dict[int, dict] = {}
    for b in m1_bars:
        ts = int(b.timestamp.timestamp())
        key = (ts // tf_sec) * tf_sec * 1000  # ms
        if key not in buckets:
            buckets[key] = {"time": key, "open": b.open, "high": b.high,
                            "low": b.low, "close": b.close, "volume": b.volume}
        else:
            c = buckets[key]
            c["high"]   = max(c["high"], b.high)
            c["low"]    = min(c["low"],  b.low)
            c["close"]  = b.close
            c["volume"] += b.volume

    result = sorted(buckets.values(), key=lambda x: x["time"])
    return result[-count:]


def _aggregate_bars(bars, timeframe: str, limit: int) -> list[dict]:
    """
    將 M1 bars 聚合為日/周/月，回傳最新 limit 筆。
    日K 以 trading_calendar 表判斷每根 bar 屬於哪個交易日，
    避免時間規則無法處理國定假日的問題。
    """
    from datetime import datetime

    # 取得交易日 session 對照表（日K 才需要）
    sessions = db.get_session_map() if timeframe == "日" else []

    def trade_date_for(ts_unix: int) -> str:
        """日K：查日曆；無資料時 fallback 用時間規則"""
        if sessions:
            return db.bar_to_trade_date(ts_unix, sessions)
        return datetime.fromtimestamp(ts_unix).strftime("%Y-%m-%d")

    def key_fn(b) -> str:
        ts = int(b.timestamp.timestamp())
        dt = b.timestamp
        if timeframe == "日":
            return trade_date_for(ts)
        elif timeframe == "周":
            # 週K：以交易日的週一為 key
            trade_date = trade_date_for(ts) if sessions else dt.strftime("%Y-%m-%d")
            d = datetime.strptime(trade_date, "%Y-%m-%d").date()
            from datetime import timedelta
            monday = d - timedelta(days=d.weekday())
            return monday.strftime("%Y-%m-%d")
        else:  # 月
            trade_date = trade_date_for(ts) if sessions else dt.strftime("%Y-%m-%d")
            return trade_date[:7]  # YYYY-MM

    buckets: dict = {}
    for b in bars:
        k = key_fn(b)
        if k not in buckets:
            # time 用交易日午夜零時，與 CSV 檔名日期一致
            trade_date = datetime.strptime(k[:10], "%Y-%m-%d")
            buckets[k] = {
                "time": int(trade_date.timestamp()) * 1000,
                "open": b.open, "high": b.high,
                "low": b.low, "close": b.close,
                "volume": b.volume,
                "delivery": b.delivery,
            }
        else:
            c = buckets[k]
            c["high"] = max(c["high"], b.high)
            c["low"] = min(c["low"], b.low)
            c["close"] = b.close
            c["volume"] += b.volume

    result = sorted(buckets.values(), key=lambda x: x["time"])
    return result[-limit:]


async def handle_get_positions(ws, data: dict):
    """前端: 查詢倉位"""
    positions = trade.positions
    await ws.send_json({
        "type": "positions",
        "data": [
            {
                "symbol": p.symbol,
                "side": p.side.value,
                "qty": p.qty,
                "avg_price": p.avg_price,
                "current_price": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
            }
            for p in positions
        ],
    })


async def handle_get_orders(ws, data: dict):
    """前端: 查詢委託"""
    orders = trade.active_orders
    await ws.send_json({
        "type": "orders",
        "data": [
            {
                "id": o.id,
                "symbol": o.symbol,
                "direction": o.direction.value,
                "order_type": o.order_type.value,
                "price": o.price,
                "qty": o.qty,
                "filled_qty": o.filled_qty,
                "status": o.status.value,
            }
            for o in orders
        ],
    })


def _load_broker_credentials(broker_id: str) -> dict:
    """從 config/brokers.yaml 讀取指定券商的 credentials。"""
    import yaml
    path = "config/brokers.yaml"
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get(broker_id, {})
    except FileNotFoundError:
        logger.warning("[BrokerConfig] 找不到 %s，請複製 brokers.yaml.example 並填入金鑰", path)
        return {}


async def handle_broker_status(ws, data: dict):
    """前端: 查詢各券商目前連線狀態"""
    await ws.send_json({
        "type": "broker_status",
        "quote": {
            "broker_id": getattr(quote._adapter, "broker_id", None) if quote._adapter else None,
            "name":      quote.broker_name,
            "connected": quote.is_connected,
        },
        "trade": {
            "broker_id": getattr(trade._adapter, "broker_id", None) if trade._adapter else None,
            "name":      trade.broker_name,
            "connected": trade.is_connected,
        }
    })


async def handle_broker_config(ws, data: dict):
    """前端: 連線或斷線指定券商
    data.action    = "connect" | "disconnect"
    data.broker_id = "sinopac" | ...
    """
    action    = data.get("action", "connect")
    broker_id = data.get("broker_id", "")

    if action == "disconnect":
        await quote.disconnect()
        await ws.send_json({
            "type": "broker_config_result",
            "success": True,
            "connected": False,
            "broker_id": broker_id,
            "message": "已斷線",
        })
        return

    # action == "connect"
    ADAPTERS = {
        "sinopac": lambda: __import__(
            "brokers.adapters.sinopac", fromlist=["SinoPacQuoteAdapter"]
        ).SinoPacQuoteAdapter(),
    }

    factory = ADAPTERS.get(broker_id)
    if not factory:
        await ws.send_json({
            "type": "broker_config_result",
            "success": False,
            "broker_id": broker_id,
            "message": f"不支援的券商: {broker_id}",
        })
        return

    credentials = _load_broker_credentials(broker_id)
    if not credentials:
        await ws.send_json({
            "type": "broker_config_result",
            "success": False,
            "broker_id": broker_id,
            "message": "找不到 credentials，請檢查 config/brokers.yaml",
        })
        return

    adapter = factory()
    adapter.broker_id = broker_id  # 方便 handle_broker_status 讀取
    ok = await quote.set_adapter(adapter, **credentials)

    await ws.send_json({
        "type": "broker_config_result",
        "success": ok,
        "connected": ok,
        "broker_id": broker_id,
        "message": "連線成功" if ok else "連線失敗，請確認 API Key 是否正確",
    })


async def handle_import_taifex(ws, data: dict):
    """前端: 期交所相關資料操作
    data.source = "download" → 僅從期交所網站下載 ZIP 到 raw_dir，不解析不入庫
    data.source = "local"    → 解析 raw_dir 內的 ZIP/CSV 並匯入資料庫

    進度推送透過 asyncio.Queue + 50ms polling 實作，
    確保所有 ws.send_json 都在同一個協程上下文執行，避免並發衝突。
    """
    source = data.get("source", "local")
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_progress(current, total, filename, extra):
        """從 executor 執行緒安全地把進度推入 queue。"""
        loop.call_soon_threadsafe(queue.put_nowait, (current, total, filename, extra))

    extra_key = "skipped" if source == "download" else "bars_so_far"

    if source == "download":
        fut = loop.run_in_executor(
            None, lambda: taifex.download_zips(on_progress=on_progress)
        )
    else:
        symbols = data.get("symbols") or None
        directory = data.get("directory", "data/raw/taifex")
        fut = loop.run_in_executor(
            None, lambda: taifex.import_directory(directory, symbols, on_progress=on_progress)
        )

    ws_alive = True

    async def flush_queue():
        nonlocal ws_alive
        while not queue.empty():
            current, total, filename, extra = queue.get_nowait()
            if not ws_alive:
                continue
            try:
                await ws.send_json({
                    "type": "import_progress",
                    "current": current,
                    "total": total,
                    "filename": filename,
                    extra_key: extra,
                })
            except Exception:
                ws_alive = False

    # 等待 executor 完成，每 50ms 排空一次 queue
    while not fut.done():
        await asyncio.sleep(0.05)
        await flush_queue()
    await flush_queue()  # 排空最後殘留的項目

    try:
        result = await fut
    except Exception as e:
        logger.error("[import_taifex] 執行錯誤: %s", e)
        if ws_alive:
            try:
                await ws.send_json({"type": "import_result", "source": source, "error": str(e)})
            except Exception:
                pass
        return

    if not ws_alive:
        return

    try:
        if source == "download":
            downloaded, skipped = result
            await ws.send_json({
                "type": "import_result",
                "source": "download",
                "downloaded": downloaded,
                "skipped": skipped,
                "save_dir": str(taifex._raw_dir),
            })
        else:
            parsed = len(result)
            inserted = db.insert_bars(result)
            await ws.send_json({
                "type": "import_result",
                "source": "local",
                "parsed": parsed,
                "inserted": inserted,
                "summary": db.summary(),
            })
    except Exception as e:
        logger.warning("[import_taifex] WebSocket 已關閉，無法發送結果: %s", e)


async def handle_broker_sync(ws, data: dict):
    """前端: 從券商 API 同步歷史資料"""
    from data.sources.broker_sync import BrokerSync
    from core.models import Timeframe

    if not quote.is_connected:
        await ws.send_json({
            "type": "broker_sync_result",
            "success": False,
            "message": "券商未連線，請先設定並連線券商",
        })
        return

    symbols = data.get("symbols", ["TX", "MTX"])
    timeframes = [Timeframe(tf) for tf in data.get("timeframes", ["1d"])]
    count = data.get("count", 200)

    syncer = BrokerSync(quote._adapter, db)
    results = await syncer.sync_multiple(symbols, timeframes, count)

    await ws.send_json({
        "type": "broker_sync_result",
        "success": True,
        "results": results,
        "total": sum(results.values()),
        "summary": db.summary(),
    })


async def handle_db_summary(ws, data: dict):
    """前端: 查詢資料庫摘要"""
    await ws.send_json({
        "type": "db_summary",
        "data": db.summary(),
    })


# ═══════════════════════════════════════════════════════════
#  Script Engine 事件接線
# ═══════════════════════════════════════════════════════════

def on_bar_complete(bar):
    """每根 K 棒收完時，執行所有啟用的 Script"""
    import pandas as pd

    # 取得足夠的歷史資料給 Script 計算
    bars = db.get_bars(bar.symbol, limit=200)
    if len(bars) < 5:
        return

    df = pd.DataFrame([
        {"open": b.open, "high": b.high, "low": b.low,
         "close": b.close, "volume": b.volume, "timestamp": b.timestamp}
        for b in bars
    ])

    # 執行所有 Script
    indicator_results = script_engine.run_all_on_bar(df)

    # 指標結果 → 廣播到 UI
    for script_id, output in indicator_results.items():
        bus.emit_sync("indicator_output", output)


async def handle_toggle_script(ws, data: dict):
    """前端: 啟用/停用 Script（Scripts 面板的開關）"""
    script_id = data["id"]
    meta = script_engine._scripts.get(script_id)
    if not meta:
        await ws.send_json({"type": "error", "message": f"找不到 script: {script_id}"})
        return

    if meta.enabled:
        script_engine.disable_script(script_id)
    else:
        script_engine.enable_script(script_id)

    await ws.send_json({
        "type": "script_toggled",
        "id": script_id,
        "enabled": meta.enabled,
    })


async def on_strategy_signal(signal):
    """Script 策略產生訊號 → 自動下單"""
    await trade.place_order(
        symbol="TX",  # TODO: 從 signal 或設定中取得
        direction=signal.direction,
        order_type=signal.order_type,
        qty=signal.qty,
        price=signal.price,
        source=f"script:{signal.script_name}",
    )


# ═══════════════════════════════════════════════════════════
#  啟動
# ═══════════════════════════════════════════════════════════

def setup():
    """註冊所有 Action Handler 和事件監聽"""
    # 注意：main loop 改在 ui/server.py 的 FastAPI startup event 中設定
    # （asyncio.get_running_loop()），因為這裡 uvicorn 尚未啟動，
    # 此時呼叫 asyncio.get_event_loop() 拿到的不是 uvicorn 實際運行的 loop，
    # 會導致 EventBus.emit_sync() 從子執行緒排程時找不到正確的 running loop。

    # WebSocket action handlers
    register_action("place_order", handle_place_order)
    register_action("cancel_order", handle_cancel_order)
    register_action("subscribe", handle_subscribe)
    register_action("get_history", handle_get_history)
    register_action("get_positions", handle_get_positions)
    register_action("get_orders", handle_get_orders)
    register_action("broker_status", handle_broker_status)
    register_action("broker_config", handle_broker_config)
    register_action("import_taifex", handle_import_taifex)
    register_action("broker_sync", handle_broker_sync)
    register_action("db_summary", handle_db_summary)
    register_action("toggle_script", handle_toggle_script)

    # 事件接線
    bus.on("bar", on_bar_complete)
    bus.on("script_signal", on_strategy_signal)

    # 載入內建 Script（指標 / 策略）
    for meta in BUILTIN_SCRIPTS:
        script_engine.load_script(meta)

    # 資料庫
    db.connect()
    # 優先從 TWSE 下載完整交易日曆，ZIP 目錄作為補充
    if db.build_calendar_from_twse() == 0:
        db.build_calendar_from_zip_dir("data/raw/taifex")

    logger.info("=" * 60)
    logger.info("  Futures Pro v0.1.0")
    logger.info("  問價模塊: %s", quote.broker_name)
    logger.info("  交易模塊: %s", trade.broker_name)
    logger.info("  資料庫:   %s", db._path)
    logger.info("  Scripts:  %d 個已載入", len(script_engine._scripts))
    logger.info("=" * 60)


def main():
    setup()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8888,
        log_level="info",
    )


if __name__ == "__main__":
    main()
