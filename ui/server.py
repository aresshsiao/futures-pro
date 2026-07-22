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

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi import status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ui.auth import create_token, require_auth, verify_password, ws_require_auth

from core.event_bus import EventBus
from core.models import (
    Bar, Direction, Fill, IndicatorOutput, Order, OrderBook, OrderType, Position, Tick,
)
from scripts.engine import ScriptEngine

logger = logging.getLogger(__name__)

app = FastAPI(title="Futures Pro", version="0.1.0")

# Script 引擎放在這裡（而不是 main.py）是故意的：
# main.py 是用 `python main.py` 啟動的進入點，執行時模組名稱是 "__main__"；
# 如果 script_engine 定義在 main.py，這裡用 `from main import script_engine`
# 會讓 Python 用模組名 "main" 重新 import 一份 main.py，產生第二份、從未呼叫
# setup() 的 script_engine（裡面沒有載入任何 script），導致 /api/scripts
# 永遠回空清單。ui/server.py 一定是被「import」進來而不是直接執行，沒有這個問題。
script_engine = ScriptEngine()


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
        if ws in self._connections:
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
            # broadcast() 逐一 await send_text() 期間，另一個協程（如 websocket_endpoint
            # 的 finally 區塊）可能已經把同一個 ws 從 _connections 移除掉了，這裡要防重複移除。
            if ws in self._connections:
                self._connections.remove(ws)


manager = ConnectionManager()


# ═══════════════════════════════════════════════════════════
#  事件 → WebSocket 橋接
# ═══════════════════════════════════════════════════════════

def setup_event_bridge():
    """將 EventBus 事件轉發到 WebSocket"""
    bus = EventBus()

    async def forward_tick(tick: Tick):
        if tick.symbol == "TAIEX":
            await manager.broadcast({
                "type": "index_tick",
                "price": tick.price,
                "change": tick.change,
                "change_pct": tick.change_pct,
            })
            return
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
            "time": int(bar.timestamp.timestamp() * 1000),
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

    async def forward_indicator_output(output: IndicatorOutput):
        await manager.broadcast({
            "type": "indicator_output",
            "timeframe": "1",
            "name": output.name,
            "series": output.series,
            "alerts": output.alerts,
        })

    async def forward_option_chain(symbol: str, month: str, rows: list[dict]):
        await manager.broadcast({
            "type": "options_t_quote",
            "symbol": symbol,
            "month": month,
            "data": rows,
        })

    bus.on("tick", forward_tick)
    bus.on("bar", forward_bar)
    bus.on("indicator_output", forward_indicator_output)
    bus.on("quote_update", forward_orderbook)
    bus.on("order_placed", forward_order)
    bus.on("order_update", forward_order)
    bus.on("order_cancelled", forward_order)
    bus.on("order_filled", forward_fill)
    bus.on("position_update", forward_position)
    bus.on("option_chain_update", forward_option_chain)

    async def forward_quote_con(name):
        await manager.broadcast({"type": "broker_status_update", "kind": "quote", "connected": True, "name": name})
    async def forward_quote_dis(name):
        await manager.broadcast({"type": "broker_status_update", "kind": "quote", "connected": False, "name": name})
    async def forward_trade_con(name):
        await manager.broadcast({"type": "broker_status_update", "kind": "trade", "connected": True, "name": name})
    async def forward_trade_dis(name):
        await manager.broadcast({"type": "broker_status_update", "kind": "trade", "connected": False, "name": name})

    bus.on("quote_connected", forward_quote_con)
    bus.on("quote_disconnected", forward_quote_dis)
    bus.on("trade_connected", forward_trade_con)
    bus.on("trade_disconnected", forward_trade_dis)


# ═══════════════════════════════════════════════════════════
#  WebSocket 端點
# ═══════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, _: None = Depends(ws_require_auth)):
    await manager.connect(ws)
    try:
        while True:
            # 接收前端的操作指令
            raw = await ws.receive_text()
            msg = json.loads(raw)
            await handle_client_message(ws, msg)
    except WebSocketDisconnect:
        pass
    except RuntimeError as e:
        # manager.broadcast() 在背景協程對同一個 ws 呼叫 send()，若連線這時已經斷了，
        # send() 失敗會把 application_state 標記成 DISCONNECTED，
        # 之後這裡的 receive_text() 就會炸出這個 RuntimeError 而不是乾淨的 WebSocketDisconnect。
        # 長時間操作（如大量 CSV 匯入）中途斷線時特別容易碰到，視同正常斷線處理即可。
        if "Need to call" not in str(e):
            raise
    finally:
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


# Startup hooks（由 main.py 注入，在 event loop ready 後執行，例如 Core 自動連線）
_startup_hooks: list = []


def register_startup_hook(fn):
    """註冊一個 async 啟動函式，會在 FastAPI startup 事件中依序 await 執行。"""
    _startup_hooks.append(fn)


# ═══════════════════════════════════════════════════════════
#  REST API 端點
# ═══════════════════════════════════════════════════════════

class LoginRequest(BaseModel):
    password: str


@app.post("/api/login")
async def login(req: LoginRequest):
    if not verify_password(req.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="密碼錯誤")
    return {"token": create_token()}


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/api/config", dependencies=[Depends(require_auth)])
async def get_config():
    """提供前端可調整的設定，統一從 config/settings.py 讀取。"""
    from config import settings
    return {
        "candle_color_scheme": settings.CANDLE_COLOR_SCHEME,
    }


@app.get("/api/scripts", dependencies=[Depends(require_auth)])
async def get_scripts():
    """
    提供 Scripts 面板顯示用的清單（含原始碼）。
    成交量爆量等水平線指標也包含在內 —— 統一由 script_engine 管理，
    即時運算結果透過 WebSocket 的 "indicator_output" 事件廣播。
    """
    scripts = []
    for meta in script_engine._scripts.values():
        try:
            code = Path(meta.file_path).read_text(encoding="utf-8")
        except OSError:
            code = ""
        scripts.append({
            "id": meta.id,
            "name": meta.name,
            "type": meta.script_type.value,
            "desc": meta.description,
            "enabled": meta.enabled,
            "code": code,
        })
    return {"scripts": scripts}


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
    from config import settings as _s
    if not _s.AUTH_PASSWORD_HASH:
        logger.warning("[Auth] AUTH_PASSWORD_HASH 尚未設定！請執行 `python scripts/gen_password_hash.py` 設定登入密碼。")
    if _s.AUTH_SECRET_KEY == "change-this-secret-key-in-production":
        logger.warning("[Auth] AUTH_SECRET_KEY 使用預設值，請在 config/settings.py 更換為隨機字串。")
    # 在伺服器真正啟動、event loop 開始運行後才存入主 loop，
    # 確保 EventBus.emit_sync() 從子執行緒（如 Shioaji callback）排程時用的是正確的 loop。
    EventBus().set_main_loop(asyncio.get_running_loop())
    setup_event_bridge()
    logger.info("[Server] Futures Pro 伺服器啟動")

    # 執行 Core Service 啟動 hook（自動連線券商 + 訂閱預設商品）
    # 放在此處是因為 emit_sync 需要 running loop，而 loop 到這裡才 ready。
    for hook in _startup_hooks:
        try:
            await hook()
        except Exception:
            logger.exception("[Server] startup hook 執行失敗")
