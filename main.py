"""
main.py — 程式進入點
啟動所有模塊，連接事件，啟動 Web 伺服器。
"""
from __future__ import annotations
import asyncio
import logging
import sys

import uvicorn

from core.event_bus import EventBus
from core.quote_module import QuoteModule
from core.trade_module import TradeModule
from core.models import Direction, OrderType
from data.database import Database
from data.bar_builder import BarBuilder
from data.sources.taifex import TaifexImporter
from scripts.engine import ScriptEngine
from ui.server import app, register_action

# ── Logging ───────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

# ── 全域模塊實例 ──────────────────────────────────────

bus = EventBus()
db = Database()
quote = QuoteModule()
trade = TradeModule()
bar_builder = BarBuilder()
script_engine = ScriptEngine()
taifex = TaifexImporter()


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
    """前端: 取得歷史K線 (優先從DB，不足則從券商)"""
    from core.models import Timeframe
    symbol = data["symbol"]
    tf = Timeframe(data["timeframe"])
    count = data.get("count", 200)

    # 先查 DB
    bars = db.get_bars(symbol, tf, limit=count)

    # DB 不足 → 從券商補
    if len(bars) < count and quote.is_connected:
        broker_bars = await quote.get_history(symbol, tf, count)
        if broker_bars:
            db.insert_bars(broker_bars)
            bars = db.get_bars(symbol, tf, limit=count)

    await ws.send_json({
        "type": "history_bars",
        "symbol": symbol,
        "timeframe": tf.value,
        "bars": [
            {
                "timestamp": b.timestamp.isoformat(),
                "open": b.open, "high": b.high,
                "low": b.low, "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ],
    })


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


async def handle_broker_config(ws, data: dict):
    """前端: 設定問價/交易券商"""
    # TODO: 根據 data["module"] 和 data["broker_id"] 切換 adapter
    await ws.send_json({"type": "broker_config_result", "success": True})


async def handle_import_taifex(ws, data: dict):
    """前端: 匯入期交所資料"""
    directory = data.get("directory", "data/raw/taifex")
    bars = taifex.import_directory(directory)
    count = db.insert_bars(bars)
    await ws.send_json({
        "type": "import_result",
        "count": count,
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
    bars = db.get_bars(bar.symbol, bar.timeframe, limit=200)
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
    # WebSocket action handlers
    register_action("place_order", handle_place_order)
    register_action("cancel_order", handle_cancel_order)
    register_action("subscribe", handle_subscribe)
    register_action("get_history", handle_get_history)
    register_action("get_positions", handle_get_positions)
    register_action("get_orders", handle_get_orders)
    register_action("broker_config", handle_broker_config)
    register_action("import_taifex", handle_import_taifex)
    register_action("db_summary", handle_db_summary)

    # 事件接線
    bus.on("bar", on_bar_complete)
    bus.on("script_signal", on_strategy_signal)

    # 資料庫
    db.connect()

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
