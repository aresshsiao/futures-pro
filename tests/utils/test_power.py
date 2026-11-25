"""
tests/utils/test_power.py — 防睡眠／關機攔截（只在 Windows 上跑）

真的關機沒辦法在測試裡做，這裡直接對隱藏視窗送 WM_QUERYENDSESSION，
確認它回 FALSE（= 不同意結束 session）並且有登記關機原因。
"""
import ctypes
import sys

import pytest

from utils import power

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


@pytest.fixture
def guard():
    status = power.start(prevent_sleep=True, block_shutdown=True, reason="測試中")
    yield status
    power.stop()


def _send(hwnd, msg):
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return user32.SendMessageW(hwnd, msg, 0, 0)


def test_both_guards_active(guard):
    assert guard == {"prevent_sleep": True, "block_shutdown": True}


def test_query_end_session_is_refused(guard):
    assert _send(power._hwnd, power.WM_QUERYENDSESSION) == 0


def test_shutdown_reason_registered(guard):
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    user32.ShutdownBlockReasonQuery.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    buf = ctypes.create_unicode_buffer(256)
    size = wintypes.DWORD(256)
    assert user32.ShutdownBlockReasonQuery(power._hwnd, buf, ctypes.byref(size))
    assert buf.value == "測試中"


def test_start_is_idempotent(guard):
    hwnd = power._hwnd
    power.start()
    assert power._hwnd == hwnd


def test_stop_then_restart():
    power.start()
    power.stop()
    assert power.status() == {"prevent_sleep": False, "block_shutdown": False}
    assert power.start() == {"prevent_sleep": True, "block_shutdown": True}
    power.stop()
