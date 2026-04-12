"""
core/event_bus.py — 事件匯流排
系統核心的 Pub/Sub 機制。所有模塊透過事件溝通，避免直接耦合。
"""
from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# 事件處理器型別: 同步 callback 或 async coroutine
EventHandler = Callable[..., Any] | Callable[..., Coroutine]


class EventBus:
    """
    全域事件匯流排 (Singleton)

    使用方式:
        bus = EventBus()

        # 註冊
        bus.on("tick", my_handler)

        # 發送 (async)
        await bus.emit("tick", tick_data)

        # 取消註冊
        bus.off("tick", my_handler)
    """

    _instance: EventBus | None = None

    def __new__(cls) -> EventBus:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._handlers = defaultdict(list)
            cls._instance._once_handlers = defaultdict(list)
        return cls._instance

    # ── 註冊 ──────────────────────────────────────────

    def on(self, event: str, handler: EventHandler) -> None:
        """訂閱事件"""
        if handler not in self._handlers[event]:
            self._handlers[event].append(handler)
            logger.debug(f"[EventBus] +on  {event} → {handler.__qualname__}")

    def once(self, event: str, handler: EventHandler) -> None:
        """訂閱一次性事件 (觸發後自動取消)"""
        self._once_handlers[event].append(handler)

    def off(self, event: str, handler: EventHandler) -> None:
        """取消訂閱"""
        if handler in self._handlers[event]:
            self._handlers[event].remove(handler)
            logger.debug(f"[EventBus] -off {event} ✕ {handler.__qualname__}")

    # ── 發送 ──────────────────────────────────────────

    async def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """
        發送事件，依序呼叫所有訂閱者。
        支援 sync 和 async handler。
        """
        handlers = list(self._handlers.get(event, []))
        once = list(self._once_handlers.pop(event, []))

        for handler in handlers + once:
            try:
                result = handler(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    f"[EventBus] Error in handler {handler.__qualname__} for event '{event}'"
                )

    def set_main_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """儲存主執行緒的 event loop，供跨執行緒呼叫 emit_sync 使用。"""
        self._main_loop = loop

    def emit_sync(self, event: str, *args: Any, **kwargs: Any) -> None:
        """
        同步版本 — 用於非 async 環境（如 Shioaji 的 callback 執行緒、Script 沙箱）。

        若在子執行緒呼叫，必須先呼叫 set_main_loop() 存入主 loop，
        否則 asyncio.ensure_future 可能排進錯誤的 loop。
        """
        main_loop: asyncio.AbstractEventLoop | None = getattr(self, "_main_loop", None)

        # 嘗試取得目前執行緒的 running loop
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is not None and current_loop is main_loop:
            # 在主 async 執行緒內 — 直接排程
            asyncio.ensure_future(self.emit(event, *args, **kwargs), loop=current_loop)
        elif main_loop is not None and main_loop.is_running():
            # 從子執行緒呼叫（Shioaji callback）— 用 thread-safe 方式排程
            main_loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self.emit(event, *args, **kwargs), loop=main_loop)
            )
        else:
            # fallback：無 running loop（通常是測試或腳本環境）
            try:
                loop = asyncio.get_event_loop()
                loop.run_until_complete(self.emit(event, *args, **kwargs))
            except RuntimeError:
                logger.warning("[EventBus] emit_sync: 找不到可用的 event loop，事件 '%s' 被丟棄", event)

    # ── 工具 ──────────────────────────────────────────

    def listeners(self, event: str) -> list[EventHandler]:
        """列出某事件的所有訂閱者"""
        return list(self._handlers.get(event, []))

    def clear(self, event: str | None = None) -> None:
        """清除訂閱。event=None 清除全部。"""
        if event:
            self._handlers.pop(event, None)
            self._once_handlers.pop(event, None)
        else:
            self._handlers.clear()
            self._once_handlers.clear()

    @property
    def stats(self) -> dict[str, int]:
        """統計各事件的訂閱者數量"""
        return {k: len(v) for k, v in self._handlers.items() if v}
