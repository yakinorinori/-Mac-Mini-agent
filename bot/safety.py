"""緊急停止・権限チェック・安全な後始末。"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager


class EmergencyStop(Exception):
    """緊急停止が発火した場合に送出される例外。"""


class SafetyMonitor:
    """Esc長押しを監視し、緊急停止を発火する(GUI/pyobjcが無い環境では監視をスキップする)。"""

    def __init__(self, esc_long_press_seconds: float = 1.0, corner_margin_px: int = 5):
        self.esc_long_press_seconds = esc_long_press_seconds
        self.corner_margin_px = corner_margin_px
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_flag.set()

    def triggered(self) -> bool:
        return self._stop_flag.is_set()

    def _watch(self) -> None:
        try:
            from Quartz import (
                CGEventTapCreate,
                CGEventTapEnable,
                CFRunLoopAddSource,
                CFRunLoopGetCurrent,
                CFMachPortCreateRunLoopSource,
                CGEventGetIntegerValueField,
                kCGEventKeyDown,
                kCGHeadInsertEventTap,
                kCGSessionEventTap,
                kCGEventTapOptionListenOnly,
                kCGKeyboardEventKeycode,
            )
        except ImportError:
            return  # GUI/pyobjc が無い環境では監視をスキップする

        esc_down_at: float | None = None

        def callback(proxy, event_type, event, refcon):
            nonlocal esc_down_at
            if event_type == kCGEventKeyDown:
                keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                if keycode == 53:  # Esc
                    if esc_down_at is None:
                        esc_down_at = time.time()
                    elif time.time() - esc_down_at >= self.esc_long_press_seconds:
                        self._stop_flag.set()
                else:
                    esc_down_at = None
            return event

        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,
            1 << kCGEventKeyDown,
            callback,
            None,
        )
        if tap is None:
            return

        run_loop_source = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), run_loop_source, "kCFRunLoopCommonModes")
        CGEventTapEnable(tap, True)

        while not self._stop_flag.is_set():
            time.sleep(0.05)


@contextmanager
def released_inputs_on_exit():
    """例外発生時もマウスボタン・修飾キーが押しっぱなしにならないよう保証する。"""
    try:
        yield
    except BaseException:
        _release_all_inputs()
        raise


def _release_all_inputs() -> None:
    try:
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventCreateKeyboardEvent,
            CGEventPost,
            kCGEventLeftMouseUp,
            kCGHIDEventTap,
            kCGMouseButtonLeft,
        )

        up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (0, 0), kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, up)

        for keycode in (56, 60, 59, 58, 55, 54):  # shift/ctrl/opt/cmd 系
            key_up = CGEventCreateKeyboardEvent(None, keycode, False)
            CGEventPost(kCGHIDEventTap, key_up)
    except ImportError:
        pass


def check_expected_app(expected_app_name: str) -> bool:
    """現在のアクティブアプリ名が想定と一致するか確認する。"""
    from AppKit import NSWorkspace

    active_app = NSWorkspace.sharedWorkspace().activeApplication()
    return active_app.get("NSApplicationName", "") == expected_app_name
