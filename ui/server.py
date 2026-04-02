"""
ui/server.py — FastAPI + WebSocket 伺服器
連接 Python 後端與 React 前端 UI。
"""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from core.event_bus import EventBus
from core.models import (
    Bar, Direction, Fill, Order, OrderBook, OrderType, Position, Tick,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Futures Pro", version="0.1.0")


# ═══════════════════════════════════════════════════════════
#  WebSocket 管理器
# ═══════════════════════════════════════════════════════════

class ConnectionManager:
    """管理所有 WebSocket 連線"""

    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)
        logger.info(f"[WS] 新連線 ({len(self._connections)} 個)")

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.remove(ws)
        logger.info(f"[WS] 斷線 ({len(self._connections)} 個)")

    async def broadcast(self, message: dict) -> None:
        """廣播 JSON 訊息給所有連線的前端"""
        data = json.dumps(message, default=str, ensure_ascii=False)
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.remove(ws)


manager = ConnectionManager()


# ═══════════════════════════════════════════════════════════
#  事件 → WebSocket 橋接
# ═══════════════════════════════════════════════════════════

def setup_event_bridge():
    """將 EventBus 事件轉發到 WebSocket"""
    bus = EventBus()

    async def forward_tick(tick: Tick):
        await manager.broadcast({
            "type": "tick",
            "symbol": tick.symbol,
            "price": tick.price,
            "volume": tick.volume,
            "timestamp": tick.timestamp.isoformat(),
        })

    async def forward_bar(bar: Bar):
        await manager.broadcast({
            "type": "bar",
            "symbol": bar.symbol,
            "timeframe": bar.timeframe.value,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "timestamp": bar.timestamp.isoformat(),
            "is_closed": bar.is_closed,
        })

    async def forward_orderbook(book: OrderBook):
        await manager.broadcast({
            "type": "orderbook",
            "symbol": book.symbol,
            "last_price": book.last_price,
            "bids": [{"price": l.price, "qty": l.qty} for l in book.bids[:5]],
            "asks": [{"price": l.price, "qty": l.qty} for l in book.asks[:5]],
        })

    async def forward_order(order: Order):
        await manager.broadcast({
            "type": "order_update",
            "id": order.id,
            "symbol": order.symbol,
            "direction": order.direction.value,
            "order_type": order.order_type.value,
            "price": order.price,
            "qty": order.qty,
            "filled_qty": order.filled_qty,
            "status": order.status.value,
        })

    async def forward_fill(fill: Fill):
        await manager.broadcast({
            "type": "fill",
            "order_id": fill.order_id,
            "symbol": fill.symbol,
            "direction": fill.direction.value,
            "price": fill.price,
            "qty": fill.qty,
            "fee": fill.fee,
            "timestamp": fill.timestamp.isoformat(),
        })

    async def forward_position(pos: Position | None):
        if pos:
            await manager.broadcast({
                "type": "position_update",
                "symbol": pos.symbol,
                "side": pos.side.value,
                "qty": pos.qty,
                "avg_price": pos.avg_price,
                "unrealized_pnl": pos.unrealized_pnl,
            })

    bus.on("tick", forward_tick)
    bus.on("bar", forward_bar)
    bus.on("quote_update", forward_orderbook)
    bus.on("order_placed", forward_order)
    bus.on("order_update", forward_order)
    bus.on("order_cancelled", forward_order)
    bus.on("order_filled", forward_fill)
    bus.on("position_update", forward_position)


# ═══════════════════════════════════════════════════════════
#  WebSocket 端點
# ═══════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            # 接收前端的操作指令
            raw = await ws.receive_text()
            msg = json.loads(raw)
            await handle_client_message(ws, msg)
    except WebSocketDisconnect:
        manager.disconnect(ws)


async def handle_client_message(ws: WebSocket, msg: dict) -> None:
    """
    處理前端送來的指令。

    訊息格式:
        {"action": "place_order", "data": {...}}
        {"action": "cancel_order", "data": {"order_id": "abc123"}}
        {"action": "subscribe", "data": {"symbol": "TX"}}
        {"action": "get_history", "data": {"symbol": "TX", "timeframe": "15m", "count": 200}}
        ...
    """
    action = msg.get("action", "")
    data = msg.get("data", {})

    # 這些 handler 會在 main.py 中注入實際的模塊實例
    # 這裡只定義路由框架
    handlers = _action_handlers.get(action)
    if handlers:
        await handlers(ws, data)
    else:
        await ws.send_json({"type": "error", "message": f"Unknown action: {action}"})


# Action handler registry (由 main.py 在啟動時注入)
_action_handlers: dict = {}


def register_action(action: str, handler):
    _action_handlers[action] = handler


# ═══════════════════════════════════════════════════════════
#  REST API 端點
# ═══════════════════════════════════════════════════════════

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


# ── 靜態檔案 (React build) ────────────────────────────

static_dir = Path("ui/static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def serve_index():
        return FileResponse(static_dir / "index.html")


# ── 啟動事件 ──────────────────────────────────────────

@app.on_event("startup")
async def startup():
    setup_event_bridge()
    logger.info("[Server] Futures Pro 伺服器啟動")
