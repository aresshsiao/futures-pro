"""
utils/power.py — 程式執行期間防止 Windows 睡眠／關機

夜盤跑到凌晨五點，電腦閒置自動睡眠或被 Windows Update 重新開機，券商回報會
整段漏接、停損條件單也不會觸發。這裡用一條背景執行緒做兩件事：

  1. 防睡眠：SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)。
     這個狀態綁在「呼叫它的執行緒」上，執行緒結束就失效，所以不能在 asyncio
     的 startup hook 裡呼叫一次就走——要在一條活到行程結束的執行緒裡呼叫。
     只擋得住閒置自動睡眠；手動睡眠、闔上筆電螢幕、電池過低擋不住。

  2. 擋關機：建立一個看不見的 top-level 視窗，收到 WM_QUERYENDSESSION 時回
     FALSE，並用 ShutdownBlockReasonCreate 告訴 Windows 原因。關機／重開機／
     登出時 Windows 會停在「有應用程式正在阻止關機」的畫面，列出這裡的原因，
     使用者按「仍要關機」才會繼續——是「提醒你」，不是「不讓你關」。
     必須是 top-level 視窗：message-only 視窗（HWND_MESSAGE）收不到
     WM_QUERYENDSESSION 這種廣播訊息。強制關機（shutdown /f、長按電源鍵）擋不住。

非 Windows 平台全部是 no-op。
"""
from __future__ import annotations

import logging
import sys
import threading

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016

_GUARD_CLASS = "FuturesProPowerGuard"

_lock = threading.Lock()
_thread: threading.Thread | None = None
_hwnd = None
_thread_id = None
# WNDPROC 的 ctypes callback 物件一定要留著參考，被 GC 回收後 Windows 一呼叫就當掉
_wndproc_ref = None
_status = {"prevent_sleep": False, "block_shutdown": False}


def status() -> dict:
    """目前實際生效的防護（啟動失敗的項目會是 False）。"""
    return dict(_status)


def start(prevent_sleep: bool = True, block_shutdown: bool = True,
          reason: str = "期貨交易程式執行中，關機會漏接券商回報") -> dict:
    """啟動防睡眠／擋關機。重複呼叫只會生效一次。回傳實際生效的項目。"""
    global _thread
    if sys.platform != "win32" or not (prevent_sleep or block_shutdown):
        return status()
    with _lock:
        if _thread is not None and _thread.is_alive():
            return status()
        ready = threading.Event()
        _thread = threading.Thread(
            target=_run, args=(prevent_sleep, block_shutdown, reason, ready),
            name="power-guard", daemon=True,
        )
        _thread.start()
    ready.wait(5)
    return status()


def stop() -> None:
    """解除防護（關掉隱藏視窗、結束執行緒，執行緒結束時防睡眠狀態自動失效）。"""
    global _thread
    if sys.platform != "win32":
        return
    import ctypes
    with _lock:
        t = _thread
        if t is None:
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if _hwnd:
            user32.PostMessageW(_hwnd, WM_CLOSE, 0, 0)
        elif _thread_id:
            user32.PostThreadMessageW(_thread_id, 0x0012, 0, 0)   # WM_QUIT
        _thread = None
    t.join(5)


def _run(prevent_sleep: bool, block_shutdown: bool, reason: str, ready: threading.Event) -> None:
    global _hwnd, _thread_id, _wndproc_ref
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    _thread_id = kernel32.GetCurrentThreadId()

    try:
        if prevent_sleep:
            kernel32.SetThreadExecutionState.restype = wintypes.DWORD
            kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
            if kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                _status["prevent_sleep"] = True
                logger.info("[Power] 已啟用防睡眠：程式執行期間電腦不會閒置自動睡眠")
            else:
                logger.error("[Power] 防睡眠啟用失敗 (WinError %s)", ctypes.get_last_error())

        if block_shutdown:
            try:
                _hwnd = _create_guard_window(user32, kernel32, reason)
                _status["block_shutdown"] = True
                logger.info("[Power] 已啟用關機攔截：關機/重新開機時 Windows 會先停下來詢問")
            except OSError as e:
                logger.error("[Power] 關機攔截啟用失敗: %s", e)
    finally:
        ready.set()

    # 訊息迴圈：視窗要靠它收 WM_QUERYENDSESSION；沒有視窗時它單純讓執行緒
    # 活著，防睡眠狀態才不會隨執行緒結束而失效
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    # 視窗類別要跟著註銷：不然下次 start() 會沿用這次註冊、指向已被回收的 WNDPROC
    if _hwnd:
        user32.UnregisterClassW(_GUARD_CLASS, kernel32.GetModuleHandleW(None))
    _status["prevent_sleep"] = _status["block_shutdown"] = False
    _hwnd = None
    _thread_id = None


def _create_guard_window(user32, kernel32, reason: str):
    import ctypes
    from ctypes import wintypes

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ]

    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.ShutdownBlockReasonCreate.restype = wintypes.BOOL
    user32.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_QUERYENDSESSION:
            logger.warning("[Power] 偵測到關機/重新開機/登出要求，已攔截（交易程式仍在執行）")
            return 0          # FALSE = 不同意結束 session
        if msg == WM_ENDSESSION:
            if wparam:
                logger.warning("[Power] 使用者選擇「仍要關機」，系統即將結束 session")
            return 0
        if msg == WM_CLOSE:
            user32.ShutdownBlockReasonDestroy(hwnd)
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    global _wndproc_ref
    _wndproc_ref = WNDPROC(wndproc)

    hinst = kernel32.GetModuleHandleW(None)
    cls = WNDCLASSW()
    cls.lpfnWndProc = _wndproc_ref
    cls.hInstance = hinst
    cls.lpszClassName = _GUARD_CLASS
    if not user32.RegisterClassW(ctypes.byref(cls)):
        raise ctypes.WinError(ctypes.get_last_error())

    # 不呼叫 ShowWindow：視窗一直是隱藏的，但仍是 top-level，收得到關機廣播
    hwnd = user32.CreateWindowExW(
        0, cls.lpszClassName, "Futures Pro", 0, 0, 0, 0, 0, None, None, hinst, None,
    )
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.ShutdownBlockReasonCreate(hwnd, reason):
        logger.warning("[Power] ShutdownBlockReasonCreate 失敗 (WinError %s)，關機畫面不會顯示原因",
                       ctypes.get_last_error())
    return hwnd
